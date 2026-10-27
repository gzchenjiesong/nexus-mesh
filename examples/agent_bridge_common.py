"""Agent 桥接的公共部分：把某个 LLM Agent 接进 Nexus Mesh。

`hermes_node.py` 与 `openclaw_node.py` 都基于这里。

## 为什么抽出来

两边的**业务差异其实很小** —— 一个调 `hermes -z`，一个调 `openclaw agent`。
但下面这些东西必须完全一致，否则会出现"这个节点受保护、那个没有"的漏洞：

* 问题长度上限与参数校验（防超长 prompt 白烧 token）
* 自由文本的字节截断（多字节安全，且**在自己这里截**而不是被边界静默截）
* 给 Agent 的 prompt 措辞（要求直答、标明产出会被当数据看）
* 失败必须显式失败（**绝不回空串充数**）

> ⚠ 这个项目刚因为"同一件事两份实现"出过 bug（`nxs verify` 自己另写了
> 一遍错的域推导，而库函数是对的，480 个测试没抓到）。
> 安全相关的逻辑尤其不能有两份 —— 迟早会不一致。

## 调用方要做的

只两件事：

1. 实现一个 ``runner(question: str) -> str``，内部去调你的 Agent
2. 用 :func:`make_ask_handler` 把它包成节点处理器

其余的（校验、截断、失败语义）都在这里统一。
"""

from __future__ import annotations

import logging
import subprocess
from typing import Callable

#: 自由文本上限，与 ``nxs/control/policy.MAX_FREETEXT_BYTES`` 保持一致。
#: 在节点侧先截到同样的长度，好处是**截断发生在自己这里** ——
#: 自己截可以加省略号、可以记日志；被边界截是静默的。
MAX_ANSWER_BYTES = 4096

#: 截断时加的标记。**它的字节数必须从预算里扣掉** ——
#: 初版硬编码了 12 字节，而这个标记实际是 18 字节
#: （… 3 + （已截断）15），于是截断后仍超限 5 字节。
#: 这种 off-by-N 在多字节文本里特别容易算错：看着是 6 个字符，
#: 实际是 18 字节。所以让代码自己算，别手写数字。
TRUNCATION_MARK = "…（已截断）"

#: 问题文本的输入上限（字符）。防一个超长 prompt 白烧 token。
MAX_QUESTION_CHARS = 4000

#: 单次调用的默认超时（秒）。实测简单问答 9-10 秒，
#: 给到 180 秒是为了容纳"检索 + 归纳"这类活。
#: 超时就判失败，**不要无限等** —— 任务的租约在这种情况下会被调用方视为卡住。
DEFAULT_TIMEOUT = 180

#: runner 的签名：吃一个问题，吐一段文字。
Runner = Callable[[str], str]


#: 传给 Agent 子进程时**必须剔除**的环境变量前缀。
#:
#: ⚠ **这是一条真实的教训。** 桥接的 systemd unit 用
#: ``EnvironmentFile=/etc/nexus-mesh/nxs.env`` 加载凭据，那会把**整个文件**
#: 塞进桥接进程的环境 —— 包括运维密钥和别人的节点密钥。
#: 而 ``_hermes_env`` 用 ``dict(os.environ)`` 继承，于是全部传给了 Agent。
#:
#: Agent 能通过 exec 读自己的环境，所以那一刻起：
#:   · 它能拿到 NXS_OPS_SECRET（能批准/吊销任何节点）
#:   · 它能拿到别的节点的密钥（能冒充别人发任务）
#:
#: 这与"密钥不进 Agent 上下文"的设计直接矛盾。
#:
#: 我原来的测试 ``test_hermes_env_does_not_carry_secret`` 是**假阳性**：
#: 它跑在测试进程里，那个环境本来就没有这些变量，所以断言永远成立。
#: 现在改成显式注入再断言（见 TestActorInjection）。
_SECRET_ENV_PREFIXES = ("NXS_SECRET", "NXS_OPS")


def scrub_secrets(env: dict[str, str]) -> dict[str, str]:
    """剔除环境里的全部凭据变量。

    **白名单式思考的反面**：这里用黑名单（按前缀剔除）而不是白名单，
    因为子进程可能需要任意其他变量（PATH、HOME、语言设置……），
    逐个列举会漏。而凭据的命名有统一前缀，黑名单在这里是可靠的。
    """
    return {
        k: v for k, v in env.items()
        if not any(k.startswith(prefix) for prefix in _SECRET_ENV_PREFIXES)
    }


def truncate_utf8(text: str, limit: int) -> str:
    """按字节上限截断，不切碎 UTF-8 字符。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    cut = encoded[:limit]
    # 退到最后一个完整字符边界
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    return cut.decode("utf-8", errors="ignore")


def validate_question(params: dict) -> str:
    """取出并校验 ``params.question``。

    **宁可失败，不要用错的输入换一个看起来成功的产出。**
    所以这里不"猜一个默认问题"，也不把非字符串强转成字符串。
    """
    question = params.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("params.question 缺失或为空")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(
            f"params.question 超长（{len(question)} > {MAX_QUESTION_CHARS} 字符）"
        )
    return question


def build_prompt(question: str, *, via: str) -> str:
    """把任务参数包成给 Agent 的 prompt。

    :param via: 说明这次调用是怎么来的（如 ``任务总线``），用于让 Agent
        知道自己在被程序调用，而不是在跟人对话。

    **为什么要加那段前言**：这些 Agent 都是交互式的，不说明背景时
    它们可能会反问、等待确认、或者描述"我打算怎么做" ——
    那些在聊天里是对的，作为任务产出就没用。

    前言只做三件事：说明这是任务转达、要求直答、要求只给结论。
    **不注入任何业务判断** —— 判断留给 Agent 自己。
    """
    return (
        f"下面这个问题来自另一个 Agent（通过{via}转达）。"
        "请直接给出答案：不要反问、不要等待确认、不要描述你打算怎么做。\n"
        "你的回答会作为**数据**返回给发起方，因此只给结论与必要依据，"
        "不要输出工具调用细节或执行步骤。\n"
        "\n"
        "问题：\n"
        f"{question}"
    )


def finalize_answer(raw: str, logger: logging.Logger) -> dict:
    """把 Agent 的原始输出整理成节点产出。

    **空产出比失败更危险**：调用方会以为"它答了，只是没内容"。
    所以这里对空值抛异常，而不是返回空字符串。

    返回的键必须严格等于角色卡里的 ``response_fields`` ——
    多一个就会被边界拒绝，而且拒绝信息里不含字段名（字段名本身可能带信息）。
    """
    answer = (raw or "").strip()
    if not answer:
        raise RuntimeError("Agent 返回空内容（不是失败，但没有可回传的内容）")

    encoded = answer.encode("utf-8")
    if len(encoded) > MAX_ANSWER_BYTES:
        logger.warning("回答 %d 字节，超出上限，截断", len(encoded))
        budget = MAX_ANSWER_BYTES - len(TRUNCATION_MARK.encode("utf-8"))
        answer = truncate_utf8(answer, budget) + TRUNCATION_MARK

    logger.info("← 收到回答（%d 字符）", len(answer))
    return {"answer": answer, "level": "summary"}


def run_subprocess(
    cmd: list[str],
    *,
    timeout: int,
    logger: logging.Logger,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    what: str = "Agent",
) -> subprocess.CompletedProcess:
    """跑一个子进程，把"失败"整理成可读的异常。

    **只把退出码与截断后的 stderr 记进日志，不进产出** ——
    stderr 可能含环境信息（路径、token 前缀），不该外发。

    这里刻意不在异常消息里带 stderr 内容：异常会往上冒到节点框架，
    而框架可能把它写进任务记录。日志才是 stderr 该待的地方。
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            # 不给 stdin：这些都是单次调用模式，读不到输入正好，
            # 免得意外进入交互等待
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{what} 超时（>{timeout}s）") from exc

    if result.returncode != 0:
        tail = (result.stderr or "").strip()[-400:]
        logger.error("%s 退出码 %s：%s", what, result.returncode, tail)
        raise RuntimeError(f"{what} 执行失败（退出码 {result.returncode}）")

    return result


def make_ask_handler(
    runner: Runner,
    *,
    logger: logging.Logger,
    via: str = "任务总线",
) -> Callable[[dict], dict]:
    """把 ``runner`` 包成 ``agent.ask`` 的处理器。

    :param runner: 吃问题文本，吐 Agent 的原始文字输出
    """

    def handle(params: dict) -> dict:
        question = validate_question(params)
        logger.info("→ 转给 Agent（问题 %d 字符）", len(question))
        raw = runner(build_prompt(question, via=via))
        return finalize_answer(raw, logger)

    return handle
