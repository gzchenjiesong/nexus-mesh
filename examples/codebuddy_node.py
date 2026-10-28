"""把 CodeBuddy Code 接入 Nexus Mesh —— 公司电脑上的执行者节点。

用法（在公司电脑上）：

    export NXS_ACTOR_DIR="$HOME/.nexus-mesh/agents"       # macOS 没有 /etc 权限
    export NXS_COORDINATOR_URL=https://43.138.222.206:18800/nxs
    export NXS_CA_FILE="$HOME/.nexus-mesh/ca.pem"

    python examples/codebuddy_node.py --check-tools       # 先看它能不能跑
    python examples/codebuddy_node.py --dry-run           # 探活 + 注册
    python examples/codebuddy_node.py                     # 常驻

## 为什么接 CodeBuddy 而不是 WorkBuddy 桌面版

WorkBuddy 是 Electron 应用，**没有 headless 入口** —— 唯一能被程序唤醒的
方式是 automations，延迟分钟级。而 CodeBuddy Code 是同一引擎的 CLI，
有 `-p` 单次执行模式和 `daemon`。

所以：**接 CodeBuddy，用它的能力，报 mesh 节点的身份。**

## ⭐ 这个节点的核心价值：凭证不出内网

公司内部系统的 API「只允许必要的 CLI 工具访问」。
以前的做法是让云上的 Agent 拿 token 直接查 —— 那意味着 token 上云。

现在改成：

```
云上 Agent ──"查一下 XX 项目本周进度"──> 本节点
                                          │
                                   调公司的 CLI 工具（用本机凭证）
                                          │
云上 Agent <──── 摘要（≤1500 字节）────────┘
```

**云上的 Agent 只拥有「请求查询」的能力，不拥有「查询本身」的能力。**
它看不到凭证，也看不到原文。

## ⚠⚠ 但结果要过云 —— 所以摘要限制是硬要求

凭证留在内网只解决了一半。**回传内容会写入云服务器的 queue.db。**

所以本节点：

* 只注册 ``work.query`` 能力，回传内容**硬限制 1500 字节**
* 上限由桥接进程执行，**不是靠 prompt 要求 Agent 自觉**
  （被提示注入的 Agent 照样会回传原文，而它以为自己在配合）
* prompt 里同时写明"只给摘要"，但那是行为引导，不是保障

原文要留在内网。真要取原文，那是另一个决定 —— 见
``--allow-raw`` 的说明（默认关闭，且需要协议层单独开能力）。

## ⚠ 工具白名单：这里比云端两个 Agent 做得更干净

云端 taskflow 的 allow 列表含 `exec`/`process`/`write`（未收窄，靠矩阵兜）。
本节点不同 —— CodeBuddy 支持**细粒度工具白名单**，所以直接限定：

    --allowedTools "Bash(<内部CLI>:*)" "Read" "Grep"

含义：只能读文件、搜文件、以及调用**指定的那几个 CLI**。
不能执行任意命令，不能写文件。这让"执行者"这个角色的攻击面小得多。

**不给 `--dangerously-skip-permissions`**：`-p` 模式下需要授权的操作会被
拦住而不是挂起等确认 —— 那正是我们要的行为（挂起会耗尽任务租约）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
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
    SUMMARY_LIMIT,
    make_ask_handler,
    run_subprocess,
    scrub_secrets,
)

def find_codebuddy() -> str:
    """找 ``codebuddy`` 可执行文件。**按平台找，不写死路径。**

    ⚠ 三个平台三个位置，写死一个就有一边跑不起来：

    ========  ==========================================================
    macOS     ``~/.local/bin/codebuddy``（原生二进制安装）
              ``/usr/local/bin/codebuddy``、``/opt/homebrew/bin/codebuddy``
    Linux     ``~/.local/bin/codebuddy``
    Windows   ``%USERPROFILE%\\AppData\\Local\\codebuddy\\bin\\codebuddy.exe``
              以及 npm 全局下的 ``codebuddy.cmd``
    ========  ==========================================================

    先看 PATH（最可靠 —— 用户装了之后 PATH 就该是对的），
    再退回已知的几个固定位置。
    """
    exe = "codebuddy.exe" if sys.platform == "win32" else "codebuddy"

    # ① PATH 上有就用它 —— 这是用户装完后的正常状态
    found = shutil.which("codebuddy")
    if found:
        return found

    # ② 退回已知安装位置
    home = Path.home()
    candidates: list[Path] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        candidates += [
            Path(local) / "codebuddy" / "bin" / exe,
            # npm 全局在 Windows 上生成 .cmd 而不是 .exe
            home / "AppData" / "Roaming" / "npm" / "codebuddy.cmd",
        ]
    else:
        candidates += [
            home / ".local" / "bin" / exe,
            Path("/usr/local/bin") / exe,
            Path("/opt/homebrew/bin") / exe,      # macOS Apple Silicon
            Path("/usr/bin") / exe,
        ]

    for cand in candidates:
        if cand.is_file():
            return str(cand)

    # 都找不到：返回第一个期望位置。调用方会给出"没装"的报错与安装指引。
    return str(candidates[0]) if candidates else exe


#: ``codebuddy`` 的路径。启动时按平台探测，可用 --bin / CODEBUDDY_BIN 覆盖。
DEFAULT_BIN = find_codebuddy()

#: 「摘要」约束。**这是行为引导，不是保障** —— 保障是 SUMMARY_LIMIT。
SUMMARY_PROMPT_NOTE = (
    "【重要约束】你的回答会被回传到一个外部系统。"
    "因此**只给摘要与关键数据点**：结论、状态、时间、负责人、数量。"
    "不要粘贴原文、不要贴完整文档、不要贴会议记录全文。"
    "如果资料很长，只提炼要点。回答控制在 500 字以内。"
)


# ---------------------------------------------------------------------------
# 角色卡
# ---------------------------------------------------------------------------

def build_card(node_id: str, domain: str, *,
               allowed_tools: tuple[str, ...] = ()) -> RoleCard:
    """声明本节点是什么、能做什么。

    ⚠ **必须确定性生成**：不放时间戳、不放随机串。
    卡片一变，服务端的批准自动撤销。

    ## 为什么只有 `work.query` 一个能力

    刻意不注册 ``work.fetch``（取原文）之类的。理由：

    * 每多一个能力，就多一条能往外送数据的路
    * 真需要原文时再单独加 —— 那时可以做成"需要运维批准"的能力，
      而不是一开始就敞开

    ``max_response_level`` 给 ``summary``：协议层的级别限制。
    它与 ``SUMMARY_LIMIT``（字节数）是**两道不同的闸门**，都要有。
    """
    tools_note = (f"允许的工具：{', '.join(allowed_tools)}" if allowed_tools
                  else "未限制工具（不建议）")
    return RoleCard.from_dict({
        "node_id": node_id,
        "domain": domain,
        "display_name": "CodeBuddy · 公司电脑",
        "description": (
            "公司内网执行者。凭证留在内网，只回传摘要。"
            f"{tools_note}"
        ),
        "heartbeat_interval": 30,
        "roles": {
            "executor": True,
            # ★ 给发布权：需要能主动问云端 Agent（比如"这个数据该按什么口径取"）
            "issuer": True,
        },
        "capabilities": [
            {
                "type": "work.query",
                "level": "summary",
                "mode": "sync",
                "description": (
                    "查询项目进度、会议纪要摘要等内部数据。"
                    "**只返回摘要**，凭证与原文都留在内网。"
                ),
                "params_schema": {"question": "string"},
                "response_fields": ["answer", "level"],
                "freetext_fields": ["answer"],
            },
        ],
        "constraints": {
            # 摘要级 —— 与 SUMMARY_LIMIT 配合，构成双层限制
            "max_response_level": "summary",
            "can_receive_from": [domain],
            "can_issue_types": ["agent.ask"],
            "can_delegate_to": [],
            "quota": {
                "tasks_per_day": 200,
                # ★ 按**摘要上限**算字节配额，不是按协议上限。
                #   这样配额数字与实际能送出去的量一致。
                "bytes_per_response": SUMMARY_LIMIT,
                "bytes_per_day": SUMMARY_LIMIT * 200,
                "concurrent_tasks": 1,   # CodeBuddy 单会话，不并发
            },
        },
    })


# ---------------------------------------------------------------------------
# 调 CodeBuddy
# ---------------------------------------------------------------------------

def build_cmd(bin_path: str, prompt: str, *,
              allowed_tools: tuple[str, ...],
              permission_mode: str = "dontAsk",
              timeout: int = DEFAULT_TIMEOUT) -> list[str]:
    """拼出调用命令。

    ## 三个必须的参数

    ``--output-format json``
        默认输出是给人看的文本。**解析人类可读输出是脆弱的** ——
        改一次措辞就坏，还可能是"部分解析成功"。
        加这个拿稳定信封，答案在 ``.result``。

    ``--permission-mode dontAsk``
        `-p` 模式下需要授权的操作**被拒**而不是挂起等确认。
        挂起会耗尽任务租约 —— 那比失败更糟（失败至少能重试）。

    ``--allowedTools``
        细粒度白名单。**这是本节点最重要的安全控制**，见模块文档。

    ## 为什么不给 ``--dangerously-skip-permissions``

    那个参数会放开几乎全部权限（连 HIGH/CRITICAL 命令都只"可能"拦）。
    我们只需要读文件 + 调几个 CLI，用白名单精确表达就够了 ——
    **能精确表达时，就不要用粗放的开关。**
    """
    cmd = [
        bin_path,
        "-p", prompt,
        "--output-format", "json",
        "--permission-mode", permission_mode,
    ]
    if allowed_tools:
        cmd.append("--allowedTools")
        cmd.extend(allowed_tools)
    # 单次模式不支持后台任务，显式关掉以免产生跨轮事件
    cmd.extend(["--max-turns", "8"])
    return cmd


def extract_answer(payload: dict) -> str:
    """从 ``--output-format json`` 的信封里取回答。

    文档里说答案在 ``.result``。但**两份官方文档说法不一致**
    （另一处写 ``{"response": ..., "metadata": ...}``），所以这里
    两条路径都试，并在都失败时报出实际的键 —— 便于现场定位。

    ⚠ 取不到就抛异常。**不要返回空串** —— 那会让调用方以为
    "它答了但没内容"，比失败难查。
    """
    if payload.get("is_error"):
        detail = payload.get("subtype") or payload.get("error") or "unknown"
        raise RuntimeError(f"CodeBuddy 报告失败（{detail}）")

    for key in ("result", "response"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value

    # 把实际键名列出来 —— 这样文档写错时现场就能看出来
    raise RuntimeError(
        f"CodeBuddy 输出里找不到回答文本（现有键：{sorted(payload)}）"
    )


def make_runner(cmd_builder, *, timeout: int, logger: logging.Logger,
                node_id: str):
    """构造 runner：吃 prompt，吐 CodeBuddy 的回答文本。"""

    def runner(prompt: str) -> str:
        result = run_subprocess(
            cmd_builder(prompt),
            timeout=timeout,
            logger=logger,
            # 只附加 NXS_ACTOR，其余继承（但剔掉凭据）——
            # CodeBuddy 要从 ~/.codebuddy 读配置，不能清空环境
            env=_actor_env(node_id),
            what="CodeBuddy",
        )
        try:
            payload = json.loads(result.stdout or "")
        except json.JSONDecodeError as exc:
            logger.error("CodeBuddy stdout 不是合法 JSON（%d 字节）：%s…",
                         len(result.stdout or ""), (result.stdout or "")[:200])
            raise RuntimeError("CodeBuddy 输出不是合法 JSON") from exc
        return extract_answer(payload)

    return runner


def _actor_env(node_id: str) -> dict[str, str]:
    """子进程环境：继承 + 加 ``NXS_ACTOR``，但**剔掉全部凭据**。

    为什么剔：本进程的 systemd/launchd 环境里可能有 mesh 密钥；
    而 CodeBuddy 能 exec，所以它读得到。传给它就等于把密钥
    放进了 LLM 的上下文。

    为什么保留其余：CodeBuddy 要从 ``~/.codebuddy`` 读配置、
    要能找 PATH 里的 CLI 工具。清空环境会让它什么都干不了。
    """
    env = scrub_secrets(dict(os.environ))
    env["NXS_ACTOR"] = node_id
    return env


def _secret_for(node_id: str) -> str:
    """取本节点的网格凭据（优先凭据文件，环境变量兜底）。"""
    try:
        from nxs.cli.actor import load_actor

        return load_actor(node_id).secret.decode("utf-8")
    except Exception:  # noqa: BLE001
        return os.environ.get(
            "NXS_SECRET_" + node_id.upper().replace("-", "_"), "")


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def check_tools(bin_path: str, logger: logging.Logger) -> int:
    """确认 CodeBuddy 能用。**注册之前必须先过这一关。**

    ⚠ 顺序刻意：反过来的话桥接会先注册成功，然后每条任务都失败 ——
    而原因藏在节点日志里，运维在协调节点上完全看不见。

    这里只验证"能不能跑一次最小的无头调用"，不验证公司内网的 CLI ——
    后者需要在真实环境里试（``--allowedTools`` 里配的那些工具）。
    """
    if not Path(bin_path).exists():
        logger.error("找不到 %s", bin_path)
        logger.error("  安装：npm install -g @tencent-ai/codebuddy-code")
        logger.error("  或：curl -fsSL https://www.codebuddy.cn/cli/install.sh | bash")
        return 1

    try:
        ver = subprocess.run([bin_path, "--version"], capture_output=True,
                             text=True, timeout=60, stdin=subprocess.DEVNULL)
        logger.info("版本：%s", (ver.stdout or "").strip() or "(取不到)")
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("跑不起来：%s", exc)
        return 1

    logger.info("试一次最小无头调用（会消耗额度）…")
    try:
        probe = subprocess.run(
            [bin_path, "-p", "只回复两个字：就绪",
             "--output-format", "json", "--permission-mode", "dontAsk"],
            capture_output=True, text=True, timeout=180,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("调用失败：%s", exc)
        return 1

    raw = (probe.stdout or "").strip()
    if probe.returncode != 0:
        logger.error("退出码 %s：%s", probe.returncode, (probe.stderr or "")[-300:])
        logger.error("  常见原因：未登录。先跑一次交互式 `codebuddy` 完成登录。")
        return 1

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("输出不是 JSON：%s", raw[:300])
        return 1

    # ★ 这里顺便验证"答案在哪个字段" —— 两份文档说法不一致，实测一次就定
    logger.info("返回信封的键：%s", sorted(payload))
    try:
        answer = extract_answer(payload)
    except RuntimeError as exc:
        logger.error("%s", exc)
        logger.error("  → 说明文档写的字段名不对，改 extract_answer()")
        return 1
    logger.info("探活成功：%r", answer.strip()[:40])
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="把 CodeBuddy Code 接入 Nexus Mesh（公司内网执行者）")
    parser.add_argument("--node-id", default=os.environ.get(
        "NXS_NODE_ID", "work-laptop"))
    parser.add_argument("--domain", default=os.environ.get(
        "NXS_DOMAIN", "zone-work"))
    parser.add_argument("--url", default=os.environ.get("NXS_COORDINATOR_URL", ""))
    parser.add_argument("--ca-file", default=os.environ.get("NXS_CA_FILE", ""))
    parser.add_argument("--bin", default=os.environ.get("CODEBUDDY_BIN", DEFAULT_BIN))
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--allowed-tool", action="append", default=None,
                        metavar="SPEC",
                        help="允许的工具（可重复）。如 'Bash(projectctl:*)' 'Read'。 "
                             "省略时从环境变量 CODEBUDDY_ALLOWED_TOOLS 读（逗号分隔）")
    parser.add_argument("--allow-raw", action="store_true",
                        help="⚠ 允许回传更长的内容（提升上限到 4096 字节）。"
                             "默认只给 1500 字节摘要 —— 见模块文档")
    parser.add_argument("--check-tools", action="store_true",
                        help="只检查 CodeBuddy 是否可用，不注册节点")
    parser.add_argument("--dry-run", action="store_true",
                        help="探活 + 注册，不进入领任务循环")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s · %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("codebuddy-node")

    if args.check_tools:
        return check_tools(args.bin, log)

    if not args.url:
        log.error("缺少 --url 或 NXS_COORDINATOR_URL")
        return 2

    # 工具白名单：命令行优先，其次环境变量
    allowed = args.allowed_tool
    if allowed is None:
        raw = os.environ.get("CODEBUDDY_ALLOWED_TOOLS", "")
        allowed = [t.strip() for t in raw.split(",") if t.strip()]
    allowed_tools = tuple(allowed)

    if not allowed_tools:
        # ⚠ 不静默降级成"不限工具"。那等于把整个 shell 交出去。
        log.error("没有指定工具白名单。")
        log.error("  用 --allowed-tool 'Bash(<你的CLI>:*)' --allowed-tool 'Read'")
        log.error("  或设 CODEBUDDY_ALLOWED_TOOLS='Bash(projectctl:*),Read'")
        log.error("")
        log.error("  为什么拒绝启动：不给白名单意味着 CodeBuddy 能用全部工具，")
        log.error("  包括任意 Bash —— 那这个节点就等于把公司电脑的 shell 挂到了任务总线上。")
        return 2

    # ---- ① 先探活，再注册 --------------------------------------------
    if check_tools(args.bin, log) != 0:
        log.error("不注册节点。先修好 CodeBuddy 再试。")
        return 1

    secret = _secret_for(args.node_id)
    if not secret:
        log.error("缺少凭据：请建 NXS_ACTOR_DIR/<node_id>.env 或设 NXS_SECRET_%s",
                  args.node_id.upper().replace("-", "_"))
        return 2

    # ---- ② 接入 ------------------------------------------------------
    transport = HttpTransport(
        args.url,
        node_id=args.node_id,
        secret=secret.encode(),
        ca_file=args.ca_file or None,
    )

    limit = 4096 if args.allow_raw else SUMMARY_LIMIT
    if args.allow_raw:
        log.warning("⚠ --allow-raw 已开启：回传上限提到 4096 字节。"
                    "确认这些内容可以离开内网。")

    card = build_card(args.node_id, args.domain, allowed_tools=allowed_tools)
    client = NodeClient(
        NodeConfig(
            args.node_id,
            handlers={"work.query": make_ask_handler(
                make_runner(
                    lambda prompt: build_cmd(
                        args.bin, prompt,
                        allowed_tools=allowed_tools,
                        timeout=args.timeout,
                    ),
                    timeout=args.timeout, logger=log, node_id=args.node_id,
                ),
                logger=log, via="任务总线",
                limit=limit,
                prompt_note="" if args.allow_raw else SUMMARY_PROMPT_NOTE,
            )},
            levels={"work.query": Level.SUMMARY},
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
        log.error("  若是自签证书，用 --ca-file 或设 NXS_CA_FILE")
        return 1

    outcome = registered.get("outcome")
    approved = registered.get("approved")
    log.info("注册结果：outcome=%s approved=%s", outcome, approved)
    log.info("能力：work.query（上限 %d 字节）· 工具白名单：%s",
             limit, ", ".join(allowed_tools))

    if outcome == "error":
        log.error("注册**失败** —— 这不是「待批准」，是连不上或鉴权不过。")
        hint = registered.get("hint") or ""
        if hint:
            log.error("  原因：%s", hint)
        return 1

    if not approved:
        log.warning("尚未批准 —— 新节点不能自行加入。")
        log.warning("  在协调节点上：nxs ops approve %s --url ... --ca-file ...",
                    args.node_id)

    if args.dry_run:
        log.info("--dry-run：探活与注册%s。",
                 "全部就绪" if approved else "正常，等批准后可接任务")
        return 0

    log.info("开始常驻（node=%s 域=%s）", args.node_id, args.domain)
    try:
        client.run_forever()
    except KeyboardInterrupt:
        log.info("已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
