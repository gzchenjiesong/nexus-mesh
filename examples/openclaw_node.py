"""把 OpenClaw Agent 接入 Nexus Mesh 的桥接节点。

用法（在云服务器上）：

    export NXS_COORDINATOR_URL=https://127.0.0.1:18800/nxs
    export NXS_SECRET_TASKFLOW=<与服务端一致的密钥>
    export NXS_CA_FILE=/etc/nexus-mesh/tls/ca.pem     # 自签证书必需

    python examples/openclaw_node.py --audit-tools   # 先看它的工具面（重要）
    python examples/openclaw_node.py --dry-run       # 探活 + 注册，不领任务
    python examples/openclaw_node.py                 # 常驻，开始领任务

## 它做什么

一个 Nexus 节点，把收到的任务转成**一次 OpenClaw agent turn**，把返回的
文字作为产出回传。

```
协调节点 ──task──> 本桥接 ──prompt──> openclaw agent --agent taskflow
                                          │
   协调节点 <──artifact── 本桥接 <──── terminalReply.text
```

## ⚠⚠ 这里有一个比 Hermes 更严的风险，必须先读

`openclaw sandbox explain --agent taskflow` 的实测结果：

```
mode: off            ← 不沙箱，直接跑在主机上
allow: exec, process, read, ls, write, edit, apply_patch, ...
```

**taskflow 默认有 `exec`（执行任意命令）和 `write`/`edit`（写任意文件）。**
而这个桥接进程必须 root 运行（OpenClaw 装在 root 名下，state 在
``/root/.openclaw``）。

于是：**任何能向这个节点发任务的节点，都能在这台服务器上以 root 执行
任意命令。** 这不是推测，是上面那行 allow 列表的直接推论。

对比 Hermes 那边：它的工具集被收窄到 ``safe``（无终端、无文件）。
但 OpenClaw 的限制是**按 agent 配置的**（``agents.entries.<id>.tools``），
改它会同时影响该 agent 在 QQ bot 等所有渠道的行为 —— 所以不能照抄。

### 当前的处置（2026-10-27，用户决定）

**先保持 taskflow 工具面原样**，用于快速验证 Agent 间交互。
风险由下面几层控制，而不是由工具收窄控制：

| 层 | 控制 |
|---|---|
| 谁有发布权 | 只有 ``coordinator`` 有 ``issuer``。其余节点若要发布，需运维批准其角色卡 |
| 矩阵 | ``zone-work → cloud`` 只放行 ``work.*``，**不含 ``agent.ask``** —— 外部域派不进来 |
| 信箱深度 | ``self_limits.max_backlog`` 压低，失控循环烧不了无限次 |
| 审计 | 每次投递记元数据，可事后追溯 |
| 工具面 | **当前未限制** ← 唯一的缺口，等确定后再收窄 |

### 要收窄时的做法

```jsonc
// /root/.openclaw/openclaw.json
"agents": { "entries": { "taskflow": { "tools": { "sandbox": { "tools": {
  "deny": ["exec", "process"]        // 只禁执行类，保留 read/write/edit
}}}}}}
```

配完用 ``openclaw sandbox explain --agent taskflow`` 复核，
**然后重启 OpenClaw gateway**（配置在进程启动时读入）。

> ⚠ 这个改动是 **agent 级**的 —— 会同时影响 taskflow 在 QQ bot 等渠道的行为。
> 如果它在那些渠道里靠 exec 干活，会坏。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nxs.core.errors import PolicyDenied
from nxs.core.models import Level, RoleCard
from nxs.edge.client import HttpTransport
from nxs.sdk import NodeClient, NodeConfig

from examples.agent_bridge_common import (
    DEFAULT_TIMEOUT,
    MAX_ANSWER_BYTES,
    TEXT_KWARGS,
    make_ask_handler,
    run_subprocess,
    scrub_secrets,
)

#: OpenClaw CLI 的入口脚本。node 需要用它来跑。
DEFAULT_CLI = "/usr/local/lib/node_modules/openclaw/dist/index.js"

#: node 可执行文件。OpenClaw 自带的 node，版本可能与系统的不一致。
DEFAULT_NODE = "/root/.hermes/node/bin/node"


# ---------------------------------------------------------------------------
# 角色卡
# ---------------------------------------------------------------------------

def build_card(node_id: str, domain: str, *, agent_id: str,
               tools_note: str) -> RoleCard:
    """声明这个节点是什么、能做什么、能被谁委托什么。

    ⚠ **必须确定性生成**：不放时间戳、不放随机串。
    卡片内容一变，服务端的批准会自动撤销（审批针对的是那张卡）。

    ``tools_note`` 会进 description —— 那是给自己和运维看的诚实记录，
    **不是安全控制**（它只是文字）。真正的控制在 OpenClaw 的 tool policy。
    """
    return RoleCard.from_dict({
        "node_id": node_id,
        "domain": domain,
        "display_name": f"OpenClaw · {agent_id}",
        "description": f"云端 OpenClaw Agent（agent={agent_id}）。{tools_note}",
        "heartbeat_interval": 30,

        # ---- 权限模型改造 Step 2：新字段（与旧字段并存一个版本周期）----
        "role": "agent",
        "may_broadcast": False,
        "self_limits": {
            "max_backlog": 5,        # agent turn 慢，堆积等于白等
            "max_response_level": "summary",
        },
        "capacity": 1,               # OpenClaw agent turn 不宜并发
        "tools": ["openclaw"],

        # ---- 旧字段（待 Step 5 删除）----
        "capabilities": [
            {
                "type": "agent.ask",
                "level": "summary",
                "mode": "sync",
                "description": f"向 OpenClaw agent「{agent_id}」提问，取回文字回答",
                "params_schema": {"question": "string"},
                "response_fields": ["answer", "level"],
                # ★ 自由文本字段要显式声明：它是提示注入的主要载体
                "freetext_fields": ["answer"],
            },
        ],
    })


# ---------------------------------------------------------------------------
# 调 OpenClaw
# ---------------------------------------------------------------------------

def build_cmd(node: str, cli: str, agent_id: str, prompt: str) -> list[str]:
    """拼出调用命令。

    ``--json`` 是必须的：默认输出是给人看的多段文本，
    而 ``--json`` 给的是稳定信封（``runId/status/summary/result``）。
    **解析人类可读输出是脆弱的** —— 措辞一改就坏。
    """
    return [node, cli, "agent", "--agent", agent_id, "--json", "-m", prompt]


def extract_answer(payload: dict) -> str:
    """从 ``--json`` 信封里取回答文本。

    两条路径，按可靠性排序：

    1. ``result.meta.terminalReply.text`` —— 结构化的"最终回复"，
       还带 ``disposition``（是否可见）
    2. ``result.payloads[0].text`` —— 兜底。注意 payloads 可能有**多个**
       （分条回复），取第一个会和"完整回答"不一致，所以只在 1 缺失时用

    取不到就抛异常。**不要返回空串** —— 那会让调用方以为
    "它答了但没内容"，比失败更难查。
    """
    result = payload.get("result") or {}

    meta = result.get("meta") or {}
    terminal = meta.get("terminalReply") or {}
    text = terminal.get("text")
    if isinstance(text, str) and text.strip():
        return text

    payloads = result.get("payloads") or []
    if payloads:
        first = payloads[0] or {}
        text = first.get("text")
        if isinstance(text, str) and text.strip():
            return text

    raise RuntimeError(
        "OpenClaw 返回里找不到可用的回答文本"
        f"（status={payload.get('status')!r}）"
    )


def _secret_for(node_id: str) -> str:
    """取本节点的网格凭据（优先凭据文件，环境变量兜底）。

    理由同 hermes_node._secret_for：桥接 unit 加载了 nxs.env 全文，
    进程环境里不该持有别人的密钥。
    """
    try:
        from nxs.cli.actor import load_actor

        return load_actor(node_id).secret.decode("utf-8")
    except Exception:  # noqa: BLE001
        return os.environ.get(
            "NXS_SECRET_" + node_id.upper().replace("-", "_"), "")


def _actor_env(node_id: str) -> dict[str, str]:
    """子进程环境：继承全部 + 只加 ``NXS_ACTOR``。

    ⚠ **刻意只加一个变量，不整体替换 env** —— OpenClaw 从
    ``~/.openclaw`` 读配置与凭据，清空环境会让它找不到那些东西。
    这与 Hermes 那边不同：那边必须显式给 ``HERMES_HOME`` 才能选对
    profile，所以是"构造"环境；这里只是"附加"。

    ``NXS_ACTOR`` 是 A2A 协作的钥匙：Agent 通过 ``nxs-cli ask``
    问别的 Agent 时，``nxs-cli`` 靠这个名字查凭据。
    **密钥不进 Agent 的上下文** —— 它只知道自己的名字。
    """
    # ★ 剔掉全部凭据 —— 理由同 hermes_node._hermes_env
    env = scrub_secrets(dict(os.environ))
    env["NXS_ACTOR"] = node_id
    return env


def make_runner(cmd_builder, *, timeout: int, logger: logging.Logger,
                node_id: str):
    """构造 runner：吃 prompt，吐 OpenClaw 的回答文本。

    :param cmd_builder: ``prompt -> list[str]``。**不在这里拼命令** ——
        把"跑什么命令"与"怎么解析输出"分开，测试才能直接喂一个假命令，
        而不必 monkeypatch 模块级函数（那种写法会让测试看起来在测
        A，实际测的是 B）。
    """

    def runner(prompt: str) -> str:
        result = run_subprocess(
            cmd_builder(prompt),
            timeout=timeout,
            logger=logger,
            # 只附加 NXS_ACTOR，其余继承 —— 见 _actor_env 的说明
            env=_actor_env(node_id),
            what="OpenClaw",
        )
        try:
            payload = json.loads(result.stdout or "")
        except json.JSONDecodeError as exc:
            # 只记长度与前 200 字符，不记全文 —— stdout 可能很长
            logger.error("OpenClaw stdout 不是合法 JSON（%d 字节）：%s…",
                         len(result.stdout or ""), (result.stdout or "")[:200])
            raise RuntimeError("OpenClaw 输出不是合法 JSON") from exc

        status = payload.get("status")
        if status not in (None, "ok", "success", "completed"):
            # 有些失败会以退出码 0 + status=error 的形式出现，
            # 所以不能只靠退出码判断
            logger.error("OpenClaw status=%r summary=%r",
                         status, payload.get("summary"))
            raise RuntimeError(f"OpenClaw 报告失败（status={status!r}）")

        return extract_answer(payload)

    return runner


# ---------------------------------------------------------------------------
# 工具面审计
# ---------------------------------------------------------------------------

def audit_tools(node: str, cli: str, agent_id: str, logger: logging.Logger) -> int:
    """打印该 agent 的**实际**工具面。这是本文件最重要的一条命令。

    为什么值得单独做一个开关：工具面决定了"mesh 里发一个任务，
    这台机器上究竟能发生什么"。它不是部署细节，是安全边界。
    审计结果应该被**看一眼**，而不是假设。

    走的是 OpenClaw 自己的 ``sandbox explain`` —— 它算的是**有效值**
    （合并了 agent / global / default 三层），比自己读配置拼起来可靠。
    """
    logger.info("查询 agent %r 的实际工具面…", agent_id)
    try:
        result = subprocess.run(
            [node, cli, "sandbox", "explain", "--agent", agent_id],
            capture_output=True, **TEXT_KWARGS, timeout=90,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("查询失败：%s", exc)
        return 1

    text = result.stdout or ""
    if result.returncode != 0 or not text.strip():
        logger.error("查询失败（退出码 %s）：%s",
                     result.returncode, (result.stderr or "").strip()[:300])
        return 1

    print(text)

    # 把最要紧的一句直接指出来，别让它埋在输出里。
    #
    # 判断方式：只看 "allow" 那一行里的工具名。deny 行里出现 exec 是
    # **好事**（说明它被禁了），不能混在一起判断 —— 初版就写错了，
    # 在两边都搜 exec，于是 deny 掉 exec 时也会报"含 exec"。
    allow_line = ""
    for line in text.splitlines():
        if line.strip().startswith("allow"):
            allow_line = line
            break

    dangerous = [tool for tool in ("exec", "process", "write", "edit",
                                   "apply_patch")
                 if tool in allow_line]
    if dangerous:
        print()
        print("⚠ 注意：这个 agent 的 allow 列表含 %s。" % "、".join(dangerous))
        print("   通过 mesh 派一个任务，就能触发它在**这台机器上**做这些事，")
        print("   而桥接进程以 root 运行 —— 等于把 root 权限挂到了任务总线上。")
        print("   要收窄：deny exec + process，见本文件模块文档。")
    else:
        print()
        print("✓ allow 列表里没有 exec/process/write/edit/apply_patch。")
        print("  但仍然要确认：这个结论来自 OpenClaw 的配置，")
        print("  改动配置后需要重启 gateway 才生效。")
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="把 OpenClaw Agent 接入 Nexus Mesh")
    parser.add_argument("--node-id", default=os.environ.get(
        "NXS_NODE_ID", "taskflow"))
    parser.add_argument("--agent-id", default=os.environ.get(
        "OPENCLAW_AGENT", "taskflow"),
        help="要接入的 OpenClaw agent id（默认 taskflow）")
    parser.add_argument("--domain", default=os.environ.get("NXS_DOMAIN", "cloud"))
    parser.add_argument("--url", default=os.environ.get("NXS_COORDINATOR_URL", ""))
    parser.add_argument("--ca-file", default=os.environ.get("NXS_CA_FILE", ""))
    parser.add_argument("--node-bin", default=os.environ.get(
        "OPENCLAW_NODE", DEFAULT_NODE))
    parser.add_argument("--cli", default=os.environ.get("OPENCLAW_CLI", DEFAULT_CLI))
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--audit-tools", action="store_true",
                        help="只打印该 agent 的实际工具面，不注册节点")
    parser.add_argument("--dry-run", action="store_true",
                        help="探活 + 注册，不进入领任务循环")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s · %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("openclaw-node")

    # ---- 前置：可执行文件存在吗 --------------------------------------
    for label, path in (("node", args.node_bin), ("OpenClaw CLI", args.cli)):
        if not Path(path).exists():
            log.error("%s 不存在：%s", label, path)
            return 2

    # ---- 工具面审计（独立开关，先于一切）----------------------------
    if args.audit_tools:
        return audit_tools(args.node_bin, args.cli, args.agent_id, log)

    if not args.url:
        log.error("缺少 --url 或 NXS_COORDINATOR_URL")
        return 2

    # ★ 优先从**凭据文件**读，而不是从环境变量。
    #
    # 为什么：桥接的 systemd unit 用 EnvironmentFile 加载 nxs.env 全文，
    # 于是进程环境里有全部密钥（运维密钥 + 别人的节点密钥）。
    # 虽然传给 Agent 子进程时已经剔除，但**根因是它本不该在进程环境里**。
    #
    # 从 /etc/nexus-mesh/agents/<id>.env 读，只需要自己那一把；
    # 环境变量作为向后兼容的兜底（本地手工运行时用）。
    secret = _secret_for(args.node_id)
    if not secret:
        log.error("缺少凭据：请设置环境变量 NXS_SECRET_%s",
                  args.node_id.upper().replace("-", "_"))
        return 2

    # ---- ① 先探活 agent，再注册 --------------------------------------
    # 顺序刻意：反过来的话 agent 有问题时会先注册成功，
    # 然后每条任务都失败 —— 而原因藏在节点日志里，运维看不见。
    log.info("探活 OpenClaw agent %r（会消耗少量 token）…", args.agent_id)
    try:
        probe = subprocess.run(
            build_cmd(args.node_bin, args.cli, args.agent_id,
                      "只回复两个字：就绪"),
            capture_output=True, **TEXT_KWARGS,
            timeout=min(args.timeout, 120), stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.error("探活失败：%s", exc)
        log.error("不注册节点。先修好 OpenClaw 再试。")
        return 1

    if probe.returncode != 0:
        log.error("探活失败（退出码 %s）：%s",
                  probe.returncode, (probe.stderr or "").strip()[-300:])
        return 1
    try:
        payload = json.loads(probe.stdout or "")
        answer = extract_answer(payload)
    except (json.JSONDecodeError, RuntimeError) as exc:
        log.error("探活输出无法解析：%s", exc)
        return 1
    log.info("探活成功：%r", answer.strip()[:40])

    # ---- ② 接入 ------------------------------------------------------
    transport = HttpTransport(
        args.url,
        node_id=args.node_id,
        secret=secret.encode(),
        ca_file=args.ca_file or None,
    )

    card = build_card(
        args.node_id, args.domain, agent_id=args.agent_id,
        tools_note="工具面未收窄（见 docs/hermes-integration.md 的风险说明）",
    )
    client = NodeClient(
        NodeConfig(
            args.node_id,
            handlers={"agent.ask": make_ask_handler(
                make_runner(
                    lambda prompt: build_cmd(args.node_bin, args.cli,
                                             args.agent_id, prompt),
                    timeout=args.timeout, logger=log,
                    node_id=args.node_id,
                ),
                logger=log, via="任务总线")},
            levels={"agent.ask": Level.SUMMARY},
            card=card,
            heartbeat_interval=30.0,
            renew_interval=5.0,
        ),
        transport,
    )

    try:
        registered = client.register()
    except PolicyDenied as exc:
        log.error("注册被拒：%s", exc)
        return 1
    except OSError as exc:
        log.error("连不上协调节点 %s：%s", args.url, exc)
        if args.ca_file:
            log.error("  已用 CA：%s —— 确认它与服务端是同一张", args.ca_file)
        else:
            log.error("  若是自签证书，加 --ca-file 或设 NXS_CA_FILE")
        return 1

    outcome = registered.get("outcome")
    approved = registered.get("approved")
    log.info("注册结果：outcome=%s approved=%s", outcome, approved)

    # ⚠ 必须把"注册失败"和"还没批准"分开（Hermes 桥接踩过这个坑）：
    # SDK 把异常吞成 {"outcome": "error", "approved": False}，
    # 只看 approved 会把鉴权失败报成"待批准 —— 去批一下就好"，
    # 那会让人对着一个签名错误去点批准。
    if outcome == "error":
        log.error("")
        log.error("注册**失败** —— 这不是「待批准」，是连不上或鉴权不过。")
        hint = registered.get("hint") or ""
        if hint:
            log.error("  原因：%s", hint)
        if "AU-SIG-01" in hint:
            env_key = "NXS_SECRET_" + args.node_id.upper().replace("-", "_")
            log.error("")
            log.error("  AU-SIG-01 = 签名校验失败，服务端不认识本节点的密钥。")
            log.error("  修法：把 %s 写进服务端 env 文件，然后**重启协调节点**",
                      env_key)
            log.error("        （环境变量在进程启动时固定，不重启改了也没用）")
        log.error("")
        return 1

    if not approved:
        log.warning("")
        log.warning("尚未批准 —— 这是**正常**的，新节点不能自行加入。")
        log.warning("  去协调节点上批准：")
        log.warning("    nxs ops approve %s --url <hub-url> --ca-file <ca.pem>",
                    args.node_id)
        log.warning("")

    if args.dry_run:
        log.info("--dry-run：探活与注册%s，不进入领任务循环。",
                 "全部就绪" if approved else "正常，等批准后可接任务")
        return 0

    log.info("开始常驻（agent=%s 超时=%ss）", args.agent_id, args.timeout)
    try:
        client.run_forever()
    except KeyboardInterrupt:
        log.info("已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
