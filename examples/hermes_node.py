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

#: 自由文本上限，与 ``nxs/control/policy.MAX_FREETEXT_BYTES`` 保持一致。
#: 在节点侧先截到同样的长度，好处是**截断发生在自己这里**——
#: 自己截可以加省略号、可以在日志里记一笔；被边界截是静默的。
MAX_ANSWER_BYTES = 4096

#: 截断时加的标记。**它的字节数必须从预算里扣掉** ——
#: 初版硬编码了 12 字节，而这个标记实际是 18 字节
#: （… 3 + （已截断）15），于是截断后仍超限 5 字节。
#: 这种 off-by-N 在多字节文本里特别容易算错：看着是 6 个字符，
#: 实际是 18 字节。所以让代码自己算，别手写数字。
TRUNCATION_MARK = "…（已截断）"

#: 单次调用的默认超时（秒）。10 秒能答完简单问题，给到 180 秒是为了
#: 容纳"检索 + 归纳"这类活。超时就判失败，**不要无限等**——
#: 任务的租约在这种情况下会被调用方视为"卡住"。
DEFAULT_TIMEOUT = 180

#: 问题文本的输入上限（字符）。防一个超长 prompt 白烧 token。
MAX_QUESTION_CHARS = 4000

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
            # ⚠ 不给发布权。开放它等于让这个节点能把任务参数当外发通道，
            #   而它的参数是从别的节点来的 —— 那是一条不该存在的路。
            "issuer": False,
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
            "can_issue_types": [],
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

def _hermes_env(hermes_home: str) -> dict[str, str]:
    """子进程的环境变量。

    只设 ``HERMES_HOME``，其余继承当前进程。**不在这里塞凭据** ——
    Hermes 自己从 profile 的 ``.env`` 读 key，桥接不该碰那些东西。
    """
    env = dict(os.environ)
    env["HERMES_HOME"] = hermes_home
    return env


def build_prompt(question: str) -> str:
    """把任务参数包成给 Hermes 的 prompt。

    **为什么要加那段前言**：``hermes -z`` 是一个交互式 Agent 的单次模式。
    不说明背景时，它可能会反问、等待确认、或者描述"我打算怎么做"——
    那些在对话里是对的，作为任务产出就没用。

    前言只做三件事：说明这是任务总线转达、要求直答、要求只给结论。
    **不注入任何业务判断** —— 判断留给 Agent 自己。
    """
    return (
        "下面这个问题来自另一个 Agent（通过任务总线转达）。"
        "请直接给出答案：不要反问、不要等待确认、不要描述你打算怎么做。\n"
        "你的回答会作为**数据**返回给发起方，因此只给结论与必要依据，"
        "不要输出工具调用细节或执行步骤。\n"
        "\n"
        "问题：\n"
        f"{question}"
    )


def make_handler(hermes_cmd: list[str], *, timeout: int, logger: logging.Logger,
                 hermes_home: str):
    """构造 ``agent.ask`` 的处理器。

    :param hermes_cmd: ``hermes`` 的基础命令（含 -z 之前的全部参数）
    :param hermes_home: ``HERMES_HOME`` 的值 —— **必须作为环境变量传**，
        它不是命令行参数。传错的话 hermes 会用默认 profile 而不是
        ``working``，症状是"回答风格不对"而不是报错，很难查。

    ``-z`` 模式不读 stdin，所以显式给 ``DEVNULL``：万一它想交互，
    直接拿到 EOF 而不是挂在那里等一个永远不会来的输入。
    """

    def handle(params: dict) -> dict:
        question = params.get("question")
        if not isinstance(question, str) or not question.strip():
            # 参数不合法 → 让任务失败。不要"猜一个默认问题"——
            # 那会用一个错的输入换来一个看起来成功的产出。
            raise ValueError("params.question 缺失或为空")

        if len(question) > MAX_QUESTION_CHARS:
            raise ValueError(
                f"params.question 超长（{len(question)} > {MAX_QUESTION_CHARS} 字符）"
            )

        prompt = build_prompt(question)
        logger.info("→ 转给 Hermes（问题 %d 字符）", len(question))

        try:
            result = subprocess.run(
                [*hermes_cmd, prompt],
                capture_output=True,
                text=True,
                timeout=timeout,
                # 不给 stdin：-z 是单次模式，读不到输入正好，
                # 免得它意外进入交互等待
                stdin=subprocess.DEVNULL,
                # HERMES_HOME 必须显式传：它决定用哪个 profile。
                # 不传会退回默认 profile，而症状是"回答不对"不是报错。
                env={**_hermes_env(hermes_home)},
                cwd=hermes_home if Path(hermes_home).is_dir() else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Hermes 超时（>{timeout}s）") from exc

        if result.returncode != 0:
            # ★ 只把退出码与**截断后的 stderr** 记进日志，不进产出。
            #   stderr 可能含环境信息（路径、token 前缀），不该外发。
            tail = (result.stderr or "").strip()[-400:]
            logger.error("Hermes 退出码 %s：%s", result.returncode, tail)
            raise RuntimeError(f"Hermes 执行失败（退出码 {result.returncode}）")

        answer = (result.stdout or "").strip()
        if not answer:
            # 空产出比失败更危险：调用方会以为"它答了但没内容"
            raise RuntimeError("Hermes 返回空内容")

        # 自己先按字节截断（多字节安全），并说明截断了
        raw = answer.encode("utf-8")
        if len(raw) > MAX_ANSWER_BYTES:
            logger.warning("回答 %d 字节，超出上限，截断", len(raw))
            budget = MAX_ANSWER_BYTES - len(TRUNCATION_MARK.encode("utf-8"))
            answer = _truncate_utf8(answer, budget) + TRUNCATION_MARK

        logger.info("← 收到回答（%d 字符）", len(answer))
        return {"answer": answer, "level": "summary"}

    return handle


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
          logger: logging.Logger, hermes_home: str) -> bool:
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
            env=_hermes_env(hermes_home),
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
                 hermes_home=args.hermes_home):
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
                hermes_home=args.hermes_home)},
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

    log.info("注册结果：outcome=%s approved=%s",
             registered.get("outcome"), registered.get("approved"))

    if not registered.get("approved"):
        log.warning("")
        log.warning("尚未批准 —— 这是**正常**的，新节点不能自行加入。")
        log.warning("去协调节点上批准：")
        log.warning("  nxs ops approve %s --url <hub-url> --ca-file <ca.pem>",
                    args.node_id)
        log.warning("")
        log.warning("批准后本进程会在下一次心跳（30 秒内）自动转为可用，"
                    "不用重启。")

    if args.dry_run:
        log.info("--dry-run：探活与注册都正常，不进入领任务循环。")
        return 0

    log.info("开始常驻。工具集=%s 超时=%ss", args.toolsets, args.timeout)
    try:
        client.run_forever()
    except KeyboardInterrupt:
        log.info("已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
