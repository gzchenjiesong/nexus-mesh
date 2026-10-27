"""把 Hermes Agent 接入 Nexus Mesh 的桥接节点。

用法（在云服务器上）：

    export NXS_COORDINATOR_URL=https://127.0.0.1:18800/nxs
    export NXS_SECRET_HERMES_WORKING=<与服务端一致的密钥>
    export NXS_CA_FILE=/etc/nexus-mesh/tls/ca.pem     # 自签证书必需

    python examples/hermes_node.py --dry-run    # 只注册 + 探活，不领任务
    python examples/hermes_node.py              # 常驻，开始领任务

---

## 它做什么

一个 Nexus 节点，把收到的任务转成**一次 Hermes 单次调用**
（``hermes -z <prompt>``），把返回的文本作为产出回传。

```
协调节点 ──task──> 本桥接 ──prompt──> hermes -z ──text──> 本桥接 ──artifact──> 协调节点
```

## ⚠ 这不是普通的"加一个节点"

**通过 mesh 派任务，等于让远端触发一次 LLM 执行。** 所以在写任何业务逻辑前，
先把三件事定死 —— 下面每一条都是刻意的，不是保守：

### ① 工具集必须收窄

``hermes -z`` 默认带上该 profile 的全部工具（含终端、文件、代码执行）。
如果不收窄，**任何能发任务的节点都能让这台机器执行任意命令**。

所以默认只给 ``safe``：它是 Hermes 内置的最小集
（``web`` + ``vision`` + ``image_gen``，**不含 terminal / file /
code_execution**）。要放宽就用 ``--toolsets`` 显式指定，但请记住：
范围一旦扩大，mesh 里每个能发任务的节点都跟着获得那个能力。

### ② 产出只走自由文本通道

``answer`` 声明为 freetext，会被边界做三件事：剥控制字符、剥指令性前缀、
按 4096 字节截断。这不是完整的注入防护（那不存在），
而是把最低成本的攻击做不成。

### ③ 失败要**显式失败**

``hermes`` 非零退出、超时、或输出为空时，本节点抛异常让任务落 ``failed``。
**绝不返回空字符串充数** —— 那会让调用方以为"它答了，只是没内容"，
比失败更难排查。

---

## 已知限制（MVP）

* **每次任务一次 LLM 调用**，约 10 秒起（简单问答）。复杂问题更久。
  租约靠 SDK 自动续租（默认 5 秒一次），不靠调大租约。
* **会写 Hermes 的会话历史**。``-z`` 每次创建一个 session。
  跑多了历史会涨，需要定期清理（见 ``hermes sessions``）。
* **同一个 profile 与 gateway 并存**。gateway 是常驻服务，本桥接是短进程，
  两者共享 ``HERMES_HOME`` 的记忆与配置，但会话互不干扰。
* **没有对 token 用量做限制**。每条任务都会烧 token（deepseek-v4-pro）。
  要控成本就收紧 ``quota.tasks_per_day``。
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nxs.core.errors import ConfigError, PolicyDenied
from nxs.core.models import Level, RoleCard
from nxs.edge.client import HttpTransport
from nxs.sdk import NodeClient, NodeConfig

# 校验 / 截断 / prompt / 失败语义 与 openclaw 桥接共用一份实现。
# 安全相关的逻辑不能有两份 —— 迟早会不一致。
from examples.agent_bridge_common import (  # noqa: F401 - 部分符号供外部导入
    DEFAULT_TIMEOUT,
    MAX_ANSWER_BYTES,
    MAX_QUESTION_CHARS,
    TRUNCATION_MARK,
    build_prompt as _build_prompt_common,
    finalize_answer,
    make_ask_handler,
    run_subprocess,
    truncate_utf8 as _truncate_utf8,
    validate_question,
)

#: 默认工具集。见模块文档第 ① 条。
DEFAULT_TOOLSETS = "safe"


# ---------------------------------------------------------------------------
# 角色卡
# ---------------------------------------------------------------------------

def build_card(node_id: str, domain: str, *, toolsets: str) -> RoleCard:
    """声明这个节点是什么、能做什么、能被谁委托什么。

    ⚠ **必须确定性生成**：不放时间戳、不放随机串。
    卡片内容一变，服务端的批准会自动撤销（审批针对的是那张卡）。
    """
    return RoleCard.from_dict({
        "node_id": node_id,
        "domain": domain,
        "display_name": "Hermes · working",
        "description": (
            f"云端 Hermes Agent（profile=working），单次问答式执行。"
            f"工具集：{toolsets}"
        ),
        "heartbeat_interval": 30,
        "roles": {
            "executor": True,
            # ★ v0.17：开放发布权。
            #
            # 这是 A2A 协作的前提 —— Agent 之间要能互相问话，而"问话"
            # 就是一次任务投递。不给 issuer，它只能被动等 coordinator
            # 中转，那就不是 mesh 而是消息队列。
            #
            # 范围收在 can_issue_types（见下），只放行 agent.ask 一种。
            "issuer": True,
        },
        "capabilities": [
            {
                "type": "agent.ask",
                "level": "summary",
                "mode": "sync",
                "description": "向 Hermes 提一个问题，取回文字回答",
                "params_schema": {"question": "string"},
                "response_fields": ["answer", "level"],
                # ★ 自由文本字段要显式声明：它是提示注入的主要载体，
                #   两边（节点侧 + 服务端）都会清洗
                "freetext_fields": ["answer"],
            },
        ],
        "constraints": {
            # ★ 硬上限：超过它的产出在**节点侧**就被拦，根本发不出去
            "max_response_level": "summary",
            # 只接受协调域派来的活。写域名而不是节点 ID ——
            # 这个参数匹配的是 src.domain。
            "can_receive_from": [domain],
            # ★ 只放行 agent.ask —— 不写 ["*"]。
            # 白名单越窄，"它能把什么发出去"就越确定。
            "can_issue_types": ["agent.ask"],
            "can_delegate_to": [],
            "quota": {
                # 每次任务都烧 token，所以按日上限刻意压低。
                # 要处理更多就把这个数调大——那是一个明确的成本决定。
                "tasks_per_day": 100,
                "bytes_per_response": MAX_ANSWER_BYTES,
                "bytes_per_day": MAX_ANSWER_BYTES * 100,
                "concurrent_tasks": 2,
            },
        },
    })


# ---------------------------------------------------------------------------
# 业务处理器
# ---------------------------------------------------------------------------

def _hermes_env(hermes_home: str, node_id: str) -> dict[str, str]:
    """子进程的环境变量。

    设两样东西：

    * ``HERMES_HOME`` —— 决定用哪个 profile
    * ``NXS_ACTOR`` —— **告诉 Agent 它自己是谁**

    后者是 A2A 协作的钥匙：Agent 通过 ``nxs-cli ask`` 问别的 Agent 时，
    ``nxs-cli`` 要靠这个名字去查凭据。**密钥不进 Agent 的上下文** ——
    它只知道自己的名字，凭据在 ``/etc/nexus-mesh/agents/<id>.env`` 里。

    **不在这里塞凭据**：Hermes 自己从 profile 的 ``.env`` 读 key，
    桥接不该碰那些东西；同理也不该把 mesh 密钥塞给 Agent。
    """
    env = dict(os.environ)
    env["HERMES_HOME"] = hermes_home
    env["NXS_ACTOR"] = node_id
    return env


def build_prompt(question: str) -> str:
    """把任务参数包成给 Hermes 的 prompt。

    **为什么要加那段前言**：``hermes -z`` 是一个交互式 Agent 的单次模式。
    不说明背景时，它可能会反问、等待确认、或者描述"我打算怎么做"——
    那些在对话里是对的，作为任务产出就没用。

    前言只做三件事：说明这是任务总线转达、要求直答、要求只给结论。
    **不注入任何业务判断** —— 判断留给 Agent 自己。
    """
    return _build_prompt_common(question, via="任务总线")


def make_handler(hermes_cmd: list[str], *, timeout: int, logger: logging.Logger,
                 hermes_home: str, node_id: str):
    """构造 ``agent.ask`` 的处理器。

    :param hermes_cmd: ``hermes`` 的基础命令（含 -z 之前的全部参数）
    :param hermes_home: ``HERMES_HOME`` 的值 —— **必须作为环境变量传**，
        它不是命令行参数。传错的话 hermes 会用默认 profile 而不是
        ``working``，症状是"回答风格不对"而不是报错，很难查。

    校验、截断、失败语义都在 :mod:`examples.agent_bridge_common` 里，
    与 openclaw 桥接共用一份实现。这里只负责"怎么调 hermes"。
    """

    def runner(prompt: str) -> str:
        result = run_subprocess(
            [*hermes_cmd, prompt],
            timeout=timeout,
            logger=logger,
            # HERMES_HOME 决定 profile；NXS_ACTOR 让 Agent 知道自己的身份
            env=_hermes_env(hermes_home, node_id),
            cwd=hermes_home if Path(hermes_home).is_dir() else None,
            what="Hermes",
        )
        return result.stdout or ""

    return make_ask_handler(runner, logger=logger, via="任务总线")


def _truncate_utf8(text: str, limit: int) -> str:
    """按字节上限截断，不切碎 UTF-8 字符。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    cut = encoded[:limit]
    # 退到最后一个完整字符边界
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    return cut.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_hermes_cmd(python: str, model: str | None,
                     toolsets: str) -> list[str]:
    """拼出 ``hermes -z`` 的命令前缀（不含最后的 prompt）。

    ⚠ ``HERMES_HOME`` **不在这里** —— 它是环境变量，不是命令行参数。
    见 :func:`_hermes_env`。
    """
    cmd = [python, "-m", "hermes_cli.main"]
    if model:
        cmd += ["-m", model]
    cmd += ["-t", toolsets, "-z"]
    return cmd


def probe(hermes_cmd: list[str], *, timeout: int,
          logger: logging.Logger, hermes_home: str, node_id: str) -> bool:
    """``--dry-run`` 用：先确认 Hermes 本身能跑通。

    这一步很重要 —— 如果 Hermes 调不通，注册成节点只会让每条任务
    都以"执行失败"收场，而原因藏在节点日志里，运维看不到。
    """
    logger.info("探活 Hermes（会消耗少量 token）…")
    try:
        result = subprocess.run(
            [*hermes_cmd, "只回复两个字：就绪"],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=_hermes_env(hermes_home, node_id),
            cwd=hermes_home if Path(hermes_home).is_dir() else None,
        )
    except subprocess.TimeoutExpired:
        logger.error("Hermes 探活超时（>%ss）", timeout)
        return False
    if result.returncode != 0:
        logger.error("Hermes 探活失败（退出码 %s）：%s",
                     result.returncode, (result.stderr or "").strip()[-300:])
        return False
    logger.info("Hermes 探活成功：%r", (result.stdout or "").strip()[:40])
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把 Hermes Agent 接入 Nexus Mesh")
    parser.add_argument("--node-id", default=os.environ.get(
        "NXS_NODE_ID", "hermes-working"))
    parser.add_argument("--domain", default=os.environ.get("NXS_DOMAIN", "cloud"))
    parser.add_argument("--url", default=os.environ.get("NXS_COORDINATOR_URL", ""))
    parser.add_argument("--ca-file", default=os.environ.get("NXS_CA_FILE", ""),
                        help="信任的 CA（hub 用自签证书时必需）")
    parser.add_argument("--hermes-python", default=os.environ.get(
        "HERMES_PYTHON", "/usr/local/lib/hermes-agent/venv/bin/python"))
    parser.add_argument("--hermes-home", default=os.environ.get(
        "HERMES_HOME", "/root/.hermes/profiles/working"))
    parser.add_argument("--model", default=os.environ.get("HERMES_MODEL", ""),
                        help="覆盖模型（默认沿用 profile 配置）")
    parser.add_argument("--toolsets", default=os.environ.get(
        "HERMES_TOOLSETS", DEFAULT_TOOLSETS),
        help=f"工具集，逗号分隔。**默认 {DEFAULT_TOOLSETS}**（无终端访问）。"
             f"放宽之前请读模块文档第 ① 条")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--dry-run", action="store_true",
                        help="只探活 + 注册，不进入领任务循环")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s · %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("hermes-node")

    if not args.url:
        log.error("缺少 --url 或 NXS_COORDINATOR_URL")
        return 2

    secret = os.environ.get(
        "NXS_SECRET_" + args.node_id.upper().replace("-", "_"), "")
    if not secret:
        log.error("缺少凭据：请设置环境变量 NXS_SECRET_%s",
                  args.node_id.upper().replace("-", "_"))
        return 2

    hermes_cmd = build_hermes_cmd(
        args.hermes_python, args.model or None, args.toolsets)

    # ---- ① 先确认 Hermes 本身能跑 ------------------------------------
    # 顺序刻意：先探活再注册。反过来的话，Hermes 有问题时会先注册成功，
    # 然后每条任务都失败 —— 而失败原因在节点日志里，运维看不见。
    if not probe(hermes_cmd, timeout=min(args.timeout, 60), logger=log,
                 hermes_home=args.hermes_home, node_id=args.node_id):
        log.error("Hermes 探活失败，不注册节点。先修好 Hermes 再试。")
        return 1

    # ---- ② 接入 mesh -------------------------------------------------
    transport = HttpTransport(
        args.url,
        node_id=args.node_id,
        secret=secret.encode(),
        ca_file=args.ca_file or None,
    )

    card = build_card(args.node_id, args.domain, toolsets=args.toolsets)
    client = NodeClient(
        NodeConfig(
            args.node_id,
            handlers={"agent.ask": make_handler(
                hermes_cmd, timeout=args.timeout, logger=log,
                hermes_home=args.hermes_home, node_id=args.node_id)},
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

    # ⚠ **必须把"注册失败"和"还没批准"分开。**
    #
    # SDK 把注册异常吞成了 ``{"outcome": "error", "approved": False}``
    # （它不该让注册失败拖垮节点，这个设计是对的）。
    # 但如果这里只看 ``approved``，签名错误、密钥不匹配、证书问题
    # 全都会被报成"尚未批准 —— 这是正常的，去批准一下就好"。
    # 那会让人对着一个鉴权失败去点批准，白折腾。
    #
    # 实测踩过：``AU-SIG-01``（服务端不认识这个节点的密钥）
    # 被误报成"待批准"。
    if outcome == "error":
        log.error("")
        log.error("注册**失败** —— 这不是「待批准」，是连不上或鉴权不过。")
        hint = registered.get("hint") or ""
        if hint:
            log.error("  原因：%s", hint)
        if "AU-SIG-01" in hint:
            log.error("")
            log.error("  AU-SIG-01 = 签名校验失败，通常是服务端不认识本文节点的密钥。")
            log.error("  修法：把 NXS_SECRET_%s 写进服务端的环境变量文件，"
                      "**然后重启协调节点**（环境变量在进程启动时固定）：",
                      args.node_id.upper().replace("-", "_"))
            log.error("    echo 'NXS_SECRET_%s=<同一个值>' >> /etc/nexus-mesh/nxs.env",
                      args.node_id.upper().replace("-", "_"))
            log.error("    systemctl restart nexus-mesh")
        elif "AU-NODE-04" in hint:
            log.error("  该节点尚未在服务端登记，且服务端没有它的密钥。")
        elif "CERTIFICATE" in hint.upper() or "SSL" in hint.upper():
            log.error("  证书校验失败 —— 确认 --ca-file 指向与服务端同一张 CA。")
        log.error("")
        return 1

    if not approved:
        # 走到这里才是真的"待批准"
        log.warning("")
        log.warning("尚未批准 —— 这是**正常**的，新节点不能自行加入。")
        log.warning("去协调节点上批准：")
        log.warning("  nxs ops approve %s --url <hub-url> --ca-file <ca.pem>",
                    args.node_id)
        log.warning("")
        log.warning("批准后本进程会在下一次心跳（30 秒内）自动转为可用，"
                    "不用重启。")

    if args.dry_run:
        if approved:
            log.info("--dry-run：探活、注册、批准全部就绪，不进入领任务循环。")
        else:
            log.info("--dry-run：探活与注册正常，等批准后可接任务。")
        return 0

    log.info("开始常驻。工具集=%s 超时=%ss", args.toolsets, args.timeout)
    try:
        client.run_forever()
    except KeyboardInterrupt:
        log.info("已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
