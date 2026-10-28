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

#: 默认节点 ID —— **与部署脚本的默认值必须一致**。
#:
#: ⚠ 这条不是形式要求。节点 ID 决定了三件事，任何一处对不上都会失败：
#:
#: 1. 凭据文件名（``<node_id>.env``）
#: 2. 服务端环境变量名（``NXS_SECRET_<NODE_ID 大写>``）
#: 3. 角色卡里的 ``node_id`` 字段
#:
#: 对不上的症状是 ``AU-SIG-01``（签名失败）—— 而那看起来像「密钥写错了」，
#: 实际可能是「两边不是同一个名字」。
#: 有测试守着这个常量与两个部署脚本的默认值一致。
DEFAULT_NODE_ID = "work-desktop"

#: 单次任务允许的最大回合数。
#:
#: ⚠ 初版是 8 —— 太少了。查一次数据往往要走：
#:   找文件 → 读内容 → 跑命令 → 看输出 → 再查一次 → 汇总
#: 8 轮会半路截断，而症状是「回答不完整」，看起来像模型能力问题。
#: 30 轮给足余量；超了说明这个任务本来就该拆开。
DEFAULT_MAX_TURNS = 30


def find_codebuddy() -> str:
    """找 ``codebuddy`` 可执行文件。**按平台找，并按后缀优劣排序。**

    ## 后缀优先级：``.exe`` > ``.cmd`` > ``.ps1``

    ⚠ 这不是洁癖，是实测得出的：

    * **``.exe``**（原生二进制安装）走 ``CreateProcess``，参数以 UTF-16
      逐字传递 —— 中文 prompt 不变形，没有转义层。**首选。**
    * **``.cmd``**（npm 安装）要经 ``cmd.exe`` 中转。中文参数可能因
      控制台代码页（中文系统 936/GBK）乱码；prompt 里的引号、``&``
      也有被解释的风险。
    * **``.ps1``**（npm 安装）要经 PowerShell 中转，还额外受执行策略限制。

    ## 两个"容易看错"的地方

    ① **PowerShell 的 ``Get-Command codebuddy`` 返回 ``.ps1``** ——
       因为它按 PATHEXT 匹配，而 PowerShell 自己的扩展名优先级最高。

    ② **Python 的 ``shutil.which("codebuddy")`` 返回 ``.cmd``** ——
       因为 PATHEXT 默认值里**没有** ``.PS1``。

    所以两边看到的不是同一个文件。部署脚本里必须以**这里的规则**为准，
    否则用户会以为桥接要用 .ps1（实际不是）。

    ## 平台路径

    ========  ========================================================
    macOS     ``~/.local/bin``、``/usr/local/bin``、``/opt/homebrew/bin``
    Windows   ``%LOCALAPPDATA%\\codebuddy\\bin\\codebuddy.exe``（原生）
              ``%APPDATA%\\npm\\codebuddy.cmd``（npm）
    ========  ========================================================
    """
    home = Path.home()

    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        roam = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        # ★ 顺序即优先级：.exe 在前
        candidates = [
            Path(local) / "codebuddy" / "bin" / "codebuddy.exe",
            Path(roam) / "npm" / "codebuddy.exe",
            Path(roam) / "npm" / "codebuddy.cmd",
            Path(roam) / "npm" / "codebuddy.ps1",
        ]
        for cand in candidates:
            if cand.is_file():
                return str(cand)
        # 退回 which（多半是 .cmd，PATHEXT 里没有 .PS1）
        found = shutil.which("codebuddy")
        if found:
            return found
        return str(candidates[0])

    # POSIX
    found = shutil.which("codebuddy")
    if found:
        return found
    for cand in (
        home / ".local" / "bin" / "codebuddy",
        Path("/usr/local/bin/codebuddy"),
        Path("/opt/homebrew/bin/codebuddy"),
        Path("/usr/bin/codebuddy"),
    ):
        if cand.is_file():
            return str(cand)
    return str(home / ".local" / "bin" / "codebuddy")


def describe_executable(path: str) -> str:
    """说明这个可执行形态的优劣（给日志与自检用）。"""
    low = path.lower()
    if low.endswith(".exe"):
        return "原生二进制 —— 参数不经 shell，中文无乱码风险（最佳）"
    if low.endswith(".cmd") or low.endswith(".bat"):
        return ("npm shim（.cmd）—— 经 cmd.exe 中转，中文 prompt 可能因"
                "代码页乱码；建议改装原生二进制")
    if low.endswith(".ps1"):
        return ("npm shim（.ps1）—— 经 PowerShell 中转，受执行策略限制；"
                "建议改装原生二进制")
    return "未知形态"

#: ``codebuddy`` 的路径。启动时按平台探测，可用 --bin / CODEBUDDY_BIN 覆盖。
DEFAULT_BIN = find_codebuddy()

#: 「摘要」约束。**这是行为引导，不是保障** —— 保障是 SUMMARY_LIMIT。
#:
#: ⚠ 措辞上有个容易出错的地方：只写"给摘要"会让 agent 以为
#: "不该动手、直接空手回答"。而这个节点的全部价值在于它**能自己去查**。
#: 所以先说清"该怎么干就怎么干"，再收窄输出。
SUMMARY_PROMPT_NOTE = (
    "【工作方式】\n"
    "- 这是一个真实任务，不是闲聊。需要查数据就**主动使用你的工具和技能**"
    "（读文件、搜索、执行命令、调用已安装的 skill），不要凭记忆猜测。\n"
    "- 查不到就如实说查不到，不要编造。\n"
    "\n"
    "【输出约束】\n"
    "- 你的最终回答会被回传到一个外部系统（云端的任务总线）。\n"
    "- 因此**只给摘要与关键数据点**：结论、状态、时间、负责人、数量。\n"
    "- 不要粘贴原文、不要贴完整文档、不要贴会议记录全文。\n"
    "- 不要描述你用了哪些工具、执行了哪些步骤 —— 那些留在本机。\n"
    "- 回答控制在 500 字以内。"
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
              max_turns: int = DEFAULT_MAX_TURNS,
              extra_dirs: tuple[str, ...] = ()) -> list[str]:
    """拼出调用命令。

    ## 这个节点的定位：**派任务给它，而不是指挥它**

    早期版本把白名单收窄成"只允许某个取数 CLI"，本意是安全。
    但那等于把 CodeBuddy 降级成一个脚本执行器 —— 它真正擅长的是：
    自己判断该用哪个 skill、该读哪个文件、该跑什么命令。

    所以设计上要给它足够的工具面：
    ``Read`` / ``Grep`` / ``Glob``（找信息）、``Bash``（执行）、
    ``WebFetch``（内部 HTTP API）。
    安全边界不靠"少给工具"，而靠下面三条，它们更硬：

    1. 只注册 ``work.query`` 一种能力（不开放任意任务类型）
    2. 回答硬截断到 ``SUMMARY_LIMIT``（原文出不了内网）
    3. 域矩阵只允许特定来源

    ## 参数说明

    ``--output-format json``
        默认输出是给人看的文本。**解析人类可读输出是脆弱的** ——
        改一次措辞就坏，还可能是"部分解析成功"。答案在 ``.result``。

    ``--permission-mode dontAsk``
        `-p` 模式下没人点确认框。默认模式会让需要授权的操作**挂起**，
        直到租约过期 —— 比失败更糟。``dontAsk`` 让它被拒，快速失败。

    ``--max-turns``
        ⚠ 这个值不能太小。查一次数据可能要：找文件 → 读 → 跑命令 →
        看输出 → 再查一次 → 汇总。8 轮不够（初版就是 8，会半路截断）。
        默认 30，可用 ``--max-turns`` 调。

    ``--add-dir``
        业务数据可能不在工作目录里。这些目录要加入信任范围，
        否则 ``Read`` 会弹审批（而 ``dontAsk`` 下等于被拒）。

    ## 为什么不给 ``--dangerously-skip-permissions``

    那个参数会放开几乎全部权限。白名单 + ``dontAsk`` 已经能表达
    "能用什么、不能用就快速失败"，不需要粗放的开关。
    真要拦危险命令，用 settings.json 的 ``permissions.deny`` —— 那是可审计的。
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
    for d in extra_dirs:
        cmd.extend(["--add-dir", d])
    # 单次模式不支持后台任务，显式关掉以免产生跨轮事件
    cmd.extend(["--max-turns", str(max_turns)])
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
                node_id: str, workdir: str | None = None):
    """构造 runner：吃 prompt，吐 CodeBuddy 的回答文本。

    :param workdir: CodeBuddy 的工作目录。**这决定了它能发现哪些项目级 skill**
        —— ``<workdir>/.codebuddy/skills/`` 下的会被加载。
        也决定相对路径的基准。
    """

    def runner(prompt: str) -> str:
        result = run_subprocess(
            cmd_builder(prompt),
            timeout=timeout,
            logger=logger,
            # 只附加 NXS_ACTOR，其余继承（但剔掉凭据）——
            # CodeBuddy 要从 ~/.codebuddy 读配置，不能清空环境
            env=_actor_env(node_id),
            cwd=workdir,
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


def _resolve_secret(node_id: str) -> tuple[str, str]:
    """取本节点的网格凭据，返回 ``(密钥, 失败原因)``。

    ⚠ **不要用 ``except Exception: return ""`` 吞掉错误。**

    这里原来就是那么写的，后果是：用户明明建了凭据文件，脚本只报
    「缺节点密钥」—— 而真正的原因可能是

      · 文件建在了别的目录（默认目录是 ``/etc/nexus-mesh/agents``，
        Windows 上会变成 ``C:/etc/nexus-mesh/agents``）
      · 文件名对不上（``work_desktop.env`` vs ``work-desktop.env``）
      · 内容是 UTF-8 BOM 的（PowerShell 5.1 的 -Encoding UTF8 就会）
      · 缺 NXS_SECRET 这一行

    这四种情况的修法完全不同，而"缺节点密钥"一个都指不出来。

    所以：把 ``load_actor`` 的诊断信息原样带出去。
    """
    from nxs.cli.actor import load_actor

    env_var = "NXS_SECRET_" + node_id.upper().replace("-", "_")

    # ① 先试凭据文件（生产路径）
    file_error = ""
    try:
        secret = load_actor(node_id).secret.decode("utf-8")
        if secret:
            return secret, ""
        file_error = "凭据文件里 NXS_SECRET 是空的"
    except Exception as exc:  # noqa: BLE001
        # 保留原始消息 —— load_actor 的报错很有信息量
        # （会列出目录下现有哪些身份文件）
        file_error = str(exc)

    # ② 环境变量兜底（本地调试方便）
    env_secret = os.environ.get(env_var, "")
    if env_secret:
        return env_secret, ""

    return "", file_error


def _secret_for(node_id: str) -> str:
    """只要密钥（不需要诊断时的简化版）。"""
    return _resolve_secret(node_id)[0]


def _credential_hint(node_id: str) -> str:
    """给出凭据文件应该放在哪 —— **与 load_actor 用同一套规则**。

    ⚠ 之前这里手写了个 ``~/.nexus-mesh/agents``，而 ``load_actor`` 的
    默认值是 ``/etc/nexus-mesh/agents``。在 Windows 上前者是
    ``C:\\Users\\X\\.nexus-mesh\\agents``，后者是
    ``C:\\etc\\nexus-mesh\\agents`` —— 两个完全不同的地方。

    于是提示让人把文件放到 A，代码去 B 里找，永远对不上。
    **提示里的路径必须来自实际查找逻辑，不能另写一份。**
    """
    from nxs.cli.actor import DEFAULT_ACTOR_DIR

    directory = Path(os.environ.get("NXS_ACTOR_DIR") or DEFAULT_ACTOR_DIR)
    # ⚠ 只返回路径，不掺杂说明。
    #
    #   初版把「（注意：没设 NXS_ACTOR_DIR……）」拼进了返回值里，
    #   于是这个函数既像"取路径"又像"给提示"，调用方没法干净地用。
    #   数据就是数据，提示由调用方逐行打印。
    return str(directory / f"{node_id}.env")


def _actor_dir_is_default() -> bool:
    """是否在用代码的默认凭据目录（即用户没设 NXS_ACTOR_DIR）。"""
    return not os.environ.get("NXS_ACTOR_DIR")


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def check_credential(node_id: str, logger: logging.Logger) -> int:
    """检查凭据文件的完整状态。**只读，不连网。**

    ⚠ 为什么需要单独一条命令：

    凭据读不到有四种原因，修法完全不同 ——

    ==============================  ==========================
    原因                             修法
    ==============================  ==========================
    文件在该在的地方不存在             换目录 / 建文件
    文件名对不上（下划线 vs 连字符）    改名
    内容有 UTF-8 BOM                 重写为 ASCII
    缺 NXS_SECRET 这一行             补上
    ==============================  ==========================

    而"缺节点密钥"这五个字一个都指不出来。所以给它一条专门的命令，
    把每层的实际状态都摆出来。
    """
    from nxs.cli.actor import DEFAULT_ACTOR_DIR, _parse_env_file

    env_var = "NXS_SECRET_" + node_id.upper().replace("-", "_")
    actor_dir = os.environ.get("NXS_ACTOR_DIR")
    directory = Path(actor_dir or DEFAULT_ACTOR_DIR)
    path = directory / f"{node_id}.env"

    logger.info("节点 ID  : %s", node_id)
    logger.info("凭据目录 : %s%s", directory,
                "" if actor_dir else "  ← 来自代码默认值（NXS_ACTOR_DIR 未设）")
    logger.info("目标文件 : %s", path)
    logger.info("")

    ok = True

    # ① 目录
    if directory.is_dir():
        logger.info("  ✓ 凭据目录存在")
        existing = sorted(p.stem for p in directory.glob("*.env"))
        if existing:
            logger.info("    目录下现有身份：%s", existing)
            if node_id not in existing:
                logger.warning("    ⚠ 里面没有 %r —— 文件名对不上？", node_id)
                similar = [e for e in existing
                           if e.replace("_", "-") == node_id.replace("_", "-")]
                if similar:
                    logger.warning("      有名字相似的文件：%s"
                                   "（下划线 vs 连字符？）", similar)
                ok = False
        else:
            logger.info("    目录下还没有任何 .env 文件")
            ok = False
    else:
        logger.error("  ✗ 凭据目录不存在：%s", directory)
        logger.error("    → 部署脚本会创建它，或手工：mkdir \"%s\"", directory)
        ok = False

    # ② 文件
    if not path.is_file():
        logger.error("  ✗ 凭据文件不存在")
        ok = False
    else:
        logger.info("  ✓ 凭据文件存在（%d 字节）", path.stat().st_size)

        # ③ BOM
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            logger.warning("    ⚠ 文件带 UTF-8 BOM —— 老版本会因此读不到第一个键")
            logger.warning("      （现在已兼容，但建议重写为 ASCII 更干净）")

        # ④ 解析
        try:
            vals = _parse_env_file(path)
            keys = sorted(vals)
            logger.info("    解析出的键：%s", keys)
            for need in ("NXS_NODE_ID", "NXS_SECRET"):
                if need not in vals:
                    logger.error("    ✗ 缺 %s", need)
                    ok = False
                elif not vals[need]:
                    logger.error("    ✗ %s 是空的", need)
                    ok = False
            if "NXS_NODE_ID" in vals and vals["NXS_NODE_ID"] != node_id:
                logger.warning("    ⚠ 文件里的 NXS_NODE_ID=%r 与节点 ID %r 不一致",
                               vals["NXS_NODE_ID"], node_id)
            if "NXS_SECRET" in vals:
                s = vals["NXS_SECRET"]
                logger.info("    NXS_SECRET 长度 %d（正常应为 64）%s",
                            len(s), "" if len(s) == 64 else "  ← 长度不对")
                if len(s) != 64:
                    ok = False
        except Exception as exc:  # noqa: BLE001
            logger.error("    ✗ 解析失败：%s", exc)
            ok = False

    # ⑤ 整体判定（走真实代码路径）
    logger.info("")
    secret, err = _resolve_secret(node_id)
    if secret:
        logger.info("  ✓ 能取到密钥（%d 字节）—— 凭据可用", len(secret))
        return 0

    logger.error("  ✗ 取不到密钥")
    if err:
        for line in err.splitlines():
            logger.error("    %s", line)
    logger.error("")
    logger.error("  环境变量兜底：$env:%s = %r", env_var,
                 "（未设）" if not os.environ.get(env_var) else "（已设）")
    logger.error("")
    logger.error("  修法（PowerShell，把 <密钥> 换成你取到的 64 位值）：")
    logger.error("    $secret = \"<密钥>\"")
    logger.error("    $dir = \"%s\"", directory)
    logger.error("    New-Item -ItemType Directory -Force -Path $dir | Out-Null")
    logger.error("    @(")
    logger.error("      \"NXS_NODE_ID=%s\"", node_id)
    logger.error("      \"NXS_COORDINATOR_URL=%s\"",
                 os.environ.get("NXS_COORDINATOR_URL", "https://<IP>:18800/nxs"))
    logger.error("      \"NXS_CA_FILE=<ca.pem 路径>\"")
    logger.error("      \"NXS_SECRET=$secret\"")
    logger.error("    ) | Set-Content -Path (Join-Path $dir \"%s.env\") -Encoding ASCII",
                 node_id)
    logger.error("")
    logger.error("  ⚠ -Encoding ASCII 是必须的：UTF8 会加 BOM")
    return 1


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
        "NXS_NODE_ID", DEFAULT_NODE_ID))
    parser.add_argument("--domain", default=os.environ.get(
        "NXS_DOMAIN", "zone-work"))
    parser.add_argument("--url", default=os.environ.get("NXS_COORDINATOR_URL", ""))
    parser.add_argument("--ca-file", default=os.environ.get("NXS_CA_FILE", ""))
    parser.add_argument("--bin", default=os.environ.get("CODEBUDDY_BIN", DEFAULT_BIN))
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--allowed-tool", action="append", default=None,
                        metavar="SPEC",
                        help="允许的工具（可重复），如 'Read' 'Bash' 'WebFetch'。 "
                             "省略时从环境变量 CODEBUDDY_ALLOWED_TOOLS 读（逗号分隔）")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS,
                        help=f"单次任务最大回合数（默认 {DEFAULT_MAX_TURNS}）。"
                             "太小会半路截断，症状是「回答不完整」")
    parser.add_argument("--workdir", default=os.environ.get("CODEBUDDY_WORKDIR", ""),
                        help="CodeBuddy 的工作目录。决定它能发现哪些项目级 skill"
                             "（<workdir>/.codebuddy/skills/）。"
                             "也可用环境变量 CODEBUDDY_WORKDIR")
    parser.add_argument("--add-dir", action="append", default=None,
                        metavar="DIR",
                        help="额外信任目录（可重复）。业务数据不在工作目录里时必须加，"
                             "否则 Read 会弹审批（dontAsk 下等于被拒）。"
                             "省略时从 CODEBUDDY_EXTRA_DIRS 读（逗号分隔）")
    parser.add_argument("--allow-raw", action="store_true",
                        help="⚠ 允许回传更长的内容（提升上限到 4096 字节）。"
                             "默认只给 1500 字节摘要 —— 见模块文档")
    parser.add_argument("--check-tools", action="store_true",
                        help="只检查 CodeBuddy 是否可用，不注册节点")
    parser.add_argument("--check-credential", action="store_true",
                        help="只检查凭据文件状态（在哪找、找到什么、有什么问题），"
                             "不连接任何服务")
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

    if args.check_credential:
        return check_credential(args.node_id, log)

    if args.check_tools:
        return check_tools(args.bin, log)

    # ⚠ 一次把**所有**缺失项列出来，而不是查到哪个说哪个。
    #
    #   用户实际踩过：先报"缺 --url"，设好再跑，又报"缺凭据"，
    #   再设好，又报"缺白名单" —— 三次往返。
    #   而且只说"缺 X"没说"该怎么给"，等于把排查成本推给用户。
    secret, secret_error = _resolve_secret(args.node_id)

    missing: list[tuple[str, str]] = []
    if not args.url:
        missing.append(("协调节点地址", "NXS_COORDINATOR_URL"))
    if not os.environ.get("CODEBUDDY_ALLOWED_TOOLS") and not args.allowed_tool:
        missing.append(("工具白名单", "CODEBUDDY_ALLOWED_TOOLS"))
    if not secret:
        missing.append(("节点密钥", f"NXS_SECRET_{args.node_id.upper().replace('-', '_')}"))

    if missing:
        log.error("配置不完整 —— 缺 %d 项：", len(missing))
        for i, (what, var) in enumerate(missing, 1):
            log.error("  %d) %s  →  环境变量 %s", i, what, var)
        log.error("")
        log.error("最快的做法：先把这几项设进当前窗口，再重跑本命令。")

        # 只打印**这一项**的值从哪来，不打印密钥本身
        log.error("")
        if any(v == "NXS_COORDINATOR_URL" for _, v in missing):
            log.error("  [协调节点地址] 用部署脚本报的那个，形如：")
            log.error("    $env:NXS_COORDINATOR_URL = \"https://<服务器IP>:18800/nxs\"")
        if any(v == "CODEBUDDY_ALLOWED_TOOLS" for _, v in missing):
            log.error("  [工具白名单] 推荐这一组（够 Agent 自主查数据）：")
            log.error("    $env:CODEBUDDY_ALLOWED_TOOLS = \"Read,Grep,Glob,Bash,WebFetch\"")
        if any(v.startswith("NXS_SECRET_") for _, v in missing):
            log.error("  [节点密钥]")
            if secret_error:
                # ★ 把 load_actor 的原始诊断原样带出来。
                #   它会说明是"文件不存在"还是"缺键"还是"内容矛盾"，
                #   并列出目录下现有哪些身份文件 —— 这些信息足够定位。
                for line in secret_error.splitlines():
                    log.error("    %s", line)
                log.error("")
            log.error("    期望位置：%s", _credential_hint(args.node_id))
            if _actor_dir_is_default():
                log.error("      ⚠ 没设 NXS_ACTOR_DIR，上面是代码的默认值（服务器路径）。")
                log.error("        部署脚本 -WriteEnv 会把它设成 ~/.nexus-mesh/agents")
            log.error("    文件内容应为四行（KEY=VALUE）：")
            log.error("      NXS_NODE_ID=%s", args.node_id)
            log.error("      NXS_COORDINATOR_URL=%s",
                      args.url or "https://<服务器IP>:18800/nxs")
            log.error("      NXS_CA_FILE=<ca.pem 的绝对路径>")
            log.error("      NXS_SECRET=<从服务器取到的 64 位值>")
            log.error("")
            log.error("    ⚠ 写文件时的两个坑：")
            log.error("      · 别用 -Encoding UTF8（PowerShell 5.1 会加 BOM，")
            log.error("        导致第一个键名读不到）→ 用 -Encoding ASCII")
            log.error("      · 文件名必须正好是 %s.env", args.node_id)
        log.error("")
        log.error("或者让部署脚本一次写好（它会校验 CA、CodeBuddy、白名单）：")
        log.error("  powershell -ExecutionPolicy Bypass -File deploy\\setup-work-node.ps1 -WriteEnv")
        return 2

    # 工具白名单：命令行优先，其次环境变量
    allowed = args.allowed_tool
    if allowed is None:
        raw = os.environ.get("CODEBUDDY_ALLOWED_TOOLS", "")
        allowed = [t.strip() for t in raw.split(",") if t.strip()]
    allowed_tools = tuple(allowed)

    # 额外信任目录：命令行优先，其次环境变量
    extra = args.add_dir
    if extra is None:
        raw = os.environ.get("CODEBUDDY_EXTRA_DIRS", "")
        extra = [d.strip() for d in raw.split(",") if d.strip()]
    extra_dirs = tuple(d for d in extra if d)

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
                        max_turns=args.max_turns,
                        extra_dirs=extra_dirs,
                    ),
                    timeout=args.timeout, logger=log, node_id=args.node_id,
                    workdir=args.workdir or None,
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
