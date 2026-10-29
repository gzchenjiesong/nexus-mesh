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
import logging.handlers   # 显式导入：RotatingFileHandler 在子模块里
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
    TEXT_KWARGS,
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
#: codebuddy 监听的本地端口。``0`` = 让系统自动分配空闲端口。
#:
#: ⚠ **不要用默认值（不加 --port）。** codebuddy 会复用它记住的那个
#: "stable port"，而 WorkBuddy / CodeBuddy IDE 是同一引擎的桌面版，
#: 也监听那个端口 —— 两者同时跑时 CLI 直接起不来：
#:
#:   Error: listen EADDRINUSE: address already in use 127.0.0.1:52098
#:
#: 而这个错误的特征是 **stdout 空 / stderr 有内容 / 退出码 0**，
#: 只看 stdout 的话完全查不出来。
DEFAULT_PORT = 0

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

        # ---- 权限模型改造 Step 2：新字段（与旧字段并存一个版本周期）----
        #
        # `role` 选什么由**部署时**决定 —— 角色定义的是"driver 能干嘛、
        # worker 能干嘛"，具体某个节点用哪个身份接入不属于代码的事。
        # 这里跟随旧卡片现状填 driver（与 issuer=True 等价），
        # 待新部署时再按实际需要选择。
        "role": "driver",
        "may_broadcast": False,
        "self_limits": {
            # 单线程执行，队列深度按任务耗时定。
            # 若单个任务常超过 2 分钟，应调小到 5～10，避免调用方白等。
            "max_backlog": 20,
            "max_response_level": "summary",
        },
        "capacity": 1,               # CodeBuddy 单会话，不并发
        "tools": list(allowed_tools) if allowed_tools else [],

        # ---- 旧字段（待 Step 5 删除）----
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
            # ⚠ ``can_receive_from`` 是「**谁可以派任务给我**」，
            #   不是「我自己属于哪个域」。
            #
            #   初版写成了 ``[domain]``（即 zone-work），于是云端
            #   （cloud 域）派任务过来直接被拒：
            #
            #       PolicyDenied: denied[MX-RECEIVE-03]
            #
            #   而错误码看起来像"域不匹配 / 卡片不一致"，很难联想到
            #   是这一项写错了方向。实际语义是"接收白名单"。
            #
            #   这个节点的用途就是**接受云端派来的查询任务**，
            #   所以必须允许 cloud。自己的域也保留（同域调试用）。
            "can_receive_from": sorted({domain, "cloud"}),
            "can_issue_types": ["agent.ask"],
            "can_delegate_to": [],
        },
    })


# ---------------------------------------------------------------------------
# 调 CodeBuddy
# ---------------------------------------------------------------------------

def build_cmd(bin_path: str, prompt: str, *,
              allowed_tools: tuple[str, ...],
              permission_mode: str = "dontAsk",
              max_turns: int = DEFAULT_MAX_TURNS,
              extra_dirs: tuple[str, ...] = (),
              port: int = DEFAULT_PORT) -> list[str]:
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
    # ⚠ --port 0 = 让系统自动分配一个空闲端口。
    #
    #   不加这个参数时，codebuddy 会去抢它记着的那个「stable port」。
    #   而 WorkBuddy / CodeBuddy IDE 是同一个引擎的桌面版，**也监听那个端口**。
    #   于是 CLI 启动时直接失败：
    #
    #     Error: listen EADDRINUSE: address already in use 127.0.0.1:52098
    #
    #   最恶劣的是这个错误的表现：**stdout 空、stderr 有内容、退出码 0**。
    #   只看 stdout 的代码会以为"它什么都没说"，而真正原因在 stderr 里。
    #   （实测：macOS 上 codebuddy 2.177.1 + WorkBuddy 同时在跑时必现）
    cmd.extend(["--port", str(port)])
    # 单次模式不支持后台任务，显式关掉以免产生跨轮事件
    cmd.extend(["--max-turns", str(max_turns)])
    return cmd


def extract_answer(payload) -> str:
    """从 ``--output-format json`` 的输出里取出回答。

    ## ⚠⚠ 真实结构与我最初的假设**完全不同**

    我原来以为是一个信封对象，找 ``result`` / ``response`` 字段。
    用真实 codebuddy 2.177.1 实测，它返回的是**事件数组**：

        [
          {"type": "message",  "role": "user",      "content": [...]},
          {"type": "file-history-snapshot", ...},
          {"type": "reasoning", "content": "[]"},
          {"type": "message",  "role": "assistant", "content": [...]},
          {"type": "result",   "subtype": "success", "is_error": false,
           "result": "就绪",                    ← ★ 答案在这里
           "session_id": "...", "duration_ms": 10867, "num_turns": 3,
           "usage": {...}, "permission_denials": []}
        ]

    所以必须**先判断 type 再取字段**，而不是直接当 dict 找键。

    ## 为什么要按 ``type == "result"`` 找而不是取最后一个

    数组里 ``result`` 确实在末尾，但顺序不是契约。按 type 找更稳 ——
    而且如果哪天插入了新事件类型，取最后一个会拿到错的东西。

    ## 兼容旧形状

    文档另一处说返回 ``{"response": ...}``。虽然实测不是，
    但保留兜底：万一是对象形状，也能取出来。**能兼容就兼容，
    但主要路径必须按实测来。**
    """
    # ---- 形状一：事件数组（实测的形状）----
    if isinstance(payload, list):
        # 找 type == "result" 的最后一个（理论上只有一个）
        results = [e for e in payload
                   if isinstance(e, dict) and e.get("type") == "result"]
        if results:
            last = results[-1]
            if last.get("is_error"):
                detail = last.get("subtype") or last.get("error") or "unknown"
                raise RuntimeError(f"CodeBuddy 报告失败（{detail}）")
            text = last.get("result")
            if isinstance(text, str) and text.strip():
                return text
            raise RuntimeError(
                f"result 事件里没有可用文本（键：{sorted(last)}）"
            )
        # 没有 result 事件：可能全是 message，退而取 assistant 的最后一条
        for e in reversed(payload):
            if (isinstance(e, dict) and e.get("type") == "message"
                    and e.get("role") == "assistant"):
                text = _text_from_content(e.get("content"))
                if text:
                    return text
        raise RuntimeError(
            f"事件数组里既没有 type=result 也没有 assistant 消息"
            f"（共 {len(payload)} 个事件，类型："
            f"{sorted({e.get('type') for e in payload if isinstance(e, dict)})}）"
        )

    # ---- 形状二：单个对象（文档提到的旧形状，兜底）----
    if isinstance(payload, dict):
        if payload.get("is_error"):
            detail = payload.get("subtype") or payload.get("error") or "unknown"
            raise RuntimeError(f"CodeBuddy 报告失败（{detail}）")
        for key in ("result", "response"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        raise RuntimeError(
            f"输出对象里找不到回答文本（现有键：{sorted(payload)}）"
        )

    raise RuntimeError(f"无法解析的 json 顶层类型：{type(payload).__name__}")


def _text_from_content(content) -> str:
    """从 message 的 content 里抽纯文本。

    content 的形状是 ``[{"type": "output_text", "text": "..."}]``
    —— 不是字符串。这是实测确认的。
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                # output_text / input_text / text 都算
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(p for p in parts if p).strip()
    return ""


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


#: 宿主应用（WorkBuddy / CodeBuddy 桌面版）注入的变量前缀，**必须剔除**。
#:
#: ⚠⚠ **这是实测挖出来的，而且它伪装成"未登录"。**
#:
#: 桥接通常运行在宿主应用的进程树里（比如 WorkBuddy 的 Bash 工具），
#: 于是继承了宿主注入的一大堆运行时状态。实测：本机有 **210 个**
#: 额外环境变量，其中 78 个 `CODEBUDDY_*`、30 个 `WORKBUDDY_*`。
#:
#: 后果：codebuddy CLI 会按这些变量去**宿主的服务端点**认证，
#: 而不是用户登录时用的那个端点。于是：
#:
#:     完整环境      → Authentication required（1.6 秒快速失败）
#:     env -i 干净   → 正常返回"好了"（7 秒，真的调了模型）
#:
#: 而用户明明在 CLI 里登录过 —— 所以这个报错**完全是假象**，
#: 会让人往"没登录"方向排查，方向全错。
#:
#: 剔除这些前缀后实测恢复正常。二分定位过：单独注入任何一个都
#: 不触发，但整批一起就会 —— 所以按前缀整批剔除，不做精细挑选。
_HOST_INJECTED_PREFIXES = (
    "WORKBUDDY_",       # 宿主的应用名/路径/版本/VLM token 等
    "ACC_",             # ★ 宿主的**产品配置**（含 endpoint / authentication）
    "CLIENT_INFO_",     # 宿主的客户端标识
    "CLAUDE_",          # 宿主的 CLAUDE_* 兼容变量
    "SANDBOX_",         # 宿主的沙箱 IPC
    "WECOM_",           # 宿主的企微 CLI 配置
    "TENCENT_",         # 宿主的目标标识
    "GALILEO_",         # 宿主的遥测
    "INSTALL_",         # 宿主的安装状态
    "STAGING_", "BACKUP_APP_PATH", "NEW_APP_PATH", "CURRENT_APP_PATH",
    "UPDATE_", "TARGET_",
)

#: `CODEBUDDY_*` 里**要保留**的那些 —— 它们是**用户配置的认证**，
#: 剔除会让人没法认证。其余（宿主注入的运行时状态）一律剔除。
_CODEBUDDY_KEEP = frozenset({
    "CODEBUDDY_API_KEY",
    "CODEBUDDY_AUTH_TOKEN",
    "CODEBUDDY_INTERNET_ENVIRONMENT",
    "CODEBUDDY_API_KEY_DISABLED",
    "CODEBUDDY_CONFIG_DIR",
    "CODEBUDDY_BIN",
    "CODEBUDDY_ALLOWED_TOOLS",
    "CODEBUDDY_WORKDIR",
    "CODEBUDDY_EXTRA_DIRS",
    "CODEBUDDY_DEBUG",
})


def _is_host_injected(key: str) -> bool:
    """这个变量是不是宿主应用注入的（会干扰子进程）。"""
    if key.startswith("CODEBUDDY_"):
        return key not in _CODEBUDDY_KEEP
    return key.startswith(_HOST_INJECTED_PREFIXES)


def _actor_env(node_id: str) -> dict[str, str]:
    """子进程环境：继承 + 加 ``NXS_ACTOR``，但要做两层剔除。

    ## 第一层：网格自己的密钥（``NXS_SECRET`` / ``NXS_OPS``）

    CodeBuddy 能 exec，读得到自己的环境。传给它就等于把密钥
    放进了 LLM 的上下文 —— 那之后它就不再是秘密。

    ## 第二层：宿主应用注入的运行时状态（``_HOST_INJECTED_PREFIXES``）

    ⚠ **这一层是实测踩出来的，而且极其隐蔽。**

    宿主（WorkBuddy / CodeBuddy 桌面版）会往子进程环境里塞一堆
    自己的状态，其中 ``ACC_PRODUCT_CONFIG_PATH`` 这类会改变
    codebuddy CLI 的**产品配置**——包括它该去哪个端点认证。

    带着这些变量，CLI 会去宿主的端点认证，于是报
    ``Authentication required`` —— 而用户明明登录过。
    这个假象会把人引向"没登录"的错误方向。

    ## 为什么不干脆 env -i 清空

    因为 CodeBuddy 需要 PATH 找 CLI 工具、需要 HOME 找
    ``~/.codebuddy`` 配置、需要语言/编码设置。**清空它就跑不起来。**
    所以是"精准剔除"而不是"清空"。
    """
    env = scrub_secrets(dict(os.environ))
    removed = [k for k in env if _is_host_injected(k)]
    for k in removed:
        del env[k]
    env["NXS_ACTOR"] = node_id
    return env


def _resolve_actor(node_id: str):
    """加载凭据文件，返回 ``(Actor|None, 失败原因)``。

    ⚠ **凭据文件里写了 url 和 ca_file，必须用上。**

    初版只取了 ``secret``，同一份文件里的 ``NXS_COORDINATOR_URL``
    和 ``NXS_CA_FILE`` 被丢掉了 —— 于是用户明明在凭据文件里配好了，
    程序还要求他在环境变量里再设一遍。而 ``--check-credential``
    能读到那些值 —— **自相矛盾，同一份配置不该要配两遍。**
    """
    from nxs.cli.actor import load_actor

    try:
        actor = load_actor(node_id)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
    return actor, ""


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


#: 已知的失败特征 → 明确的处理建议。
#:
#: ⚠ 这两条都是**实测出来的**（macOS + codebuddy 2.177.1），
#: 而且它们的表现都是「stdout 空、退出码 0」——
#: 只看 stdout 和退出码完全查不出来，必须读 stderr。
_KNOWN_FAILURES: tuple[tuple[str, str], ...] = (
    (
        "EADDRINUSE",
        "端口被占用：codebuddy 想复用它记住的本地端口，"
        "而 WorkBuddy / CodeBuddy IDE（同一引擎的桌面版）正在监听那个端口。\n"
        "        → 本脚本已默认传 --port 0（自动分配），若仍报这个，"
        "说明有别的东西在抢端口"
    ),
    (
        "Authentication required",
        "**CLI 未登录**（注意：与 IDE 的登录状态是两回事，见下）。\n"
        "\n"
        "        ⚠ **CodeBuddy IDE 登录了不代表 CLI 登录了。**\n"
        "          两者是独立应用，凭据存在完全不同的地方：\n"
        "            · IDE（Electron）→ 自己的应用数据目录\n"
        "                              + Safe Storage 加密\n"
        "            · CLI            → <配置目录>/.credentials.json\n"
        "                              （实测确认：写文件，带 refreshToken）\n"
        "          所以 IDE 里显示已登录，CLI 这边仍然是未登录。\n"
        "\n"
        "        两条路，选一条：\n"
        "\n"
        "        【A 推荐】交互式登录一次:\n"
        "            codebuddy\n"
        "          进去后按提示选登录方式（国内选 Chinese Site）完成授权，\n"
        "          然后 Ctrl+C 退出，回来重跑本命令。\n"
        "\n"
        "          ⚠ 一个常见误解：**这不是「每次重启都要重新登录」。**\n"
        "            凭据会持久化到 <配置目录>/.credentials.json（实测确认），\n"
        "            而且带 refreshToken 会自动续期 —— 登录一次能长期用。\n"
        "            只有 refreshToken 失效（长期不用 / 被吊销 / 改密码）\n"
        "            才需要重新登录。\n"
        "\n"
        "        【B 备选】API Key（不用浏览器，但**有泄露风险，见下**）:\n"
        "            $env:CODEBUDDY_API_KEY = \"<你的 API Key>\"\n"
        "            $env:CODEBUDDY_INTERNET_ENVIRONMENT = \"internal\"   # 国内版必须\n"
        "          两个变量缺一不可 —— 只设 API Key 会 401。\n"
        "          API Key 从 https://copilot.tencent.com 获取。\n"
        "\n"
        "          ⚠ 环境变量是**进程级**的：终端关了、机器重启了就没了。\n"
        "            要让常驻任务一直能用，必须写进**用户级**环境变量：\n"
        "              [Environment]::SetEnvironmentVariable(\n"
        "                  \"CODEBUDDY_API_KEY\", \"<key>\", \"User\")\n"
        "              [Environment]::SetEnvironmentVariable(\n"
        "                  \"CODEBUDDY_INTERNET_ENVIRONMENT\", \"internal\", \"User\")\n"
        "\n"
        "          ⚠⚠ **安全问题（重要）**：环境变量对 CodeBuddy 子进程可见，\n"
        "            而它能执行 Bash —— LLM 一句 `echo $CODEBUDDY_API_KEY`\n"
        "            就能把长期密钥读进上下文。**所以 B 方案不如 A 安全。**\n"
        "            若必须用 B，考虑官方支持的 apiKeyHelper：用脚本临时取\n"
        "            token，环境里不落长期密钥（见包内 docs/cn%2Fcli%2Fiam.md）。"
    ),
    (
        "not logged in",
        "**CLI 未登录**。→ 交互式跑一次 `codebuddy` 完成登录；"
        "或用 CODEBUDDY_API_KEY + CODEBUDDY_INTERNET_ENVIRONMENT（见 --check-tools 说明）",
    ),
)


def explain_failure(stderr: str) -> str:
    """从 stderr 里认出已知失败模式，返回该怎么做。空串表示没匹配上。"""
    for needle, advice in _KNOWN_FAILURES:
        if needle in stderr:
            return advice
    return ""


def _probe_once(bin_path: str, args: list[str], *, timeout: int,
                 logger: logging.Logger, label: str,
                 env: dict[str, str] | None = None
                 ) -> tuple[bool, str, str, int]:
    """跑一次探活，返回 ``(成功, stdout, stderr, 退出码)``。

    ⚠ **一律把 stderr 拿出来。** 初版只看 stdout，于是输出为空时
    只能报"输出不是 JSON："——冒号后面什么都没有，等于没给信息。
    而 codebuddy 的失败原因（未登录、参数不认、权限被拒）
    通常都在 stderr 里。
    """
    cmd = [bin_path, *args]
    logger.debug("执行：%s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, **TEXT_KWARGS,
                           timeout=timeout, stdin=subprocess.DEVNULL,
                           # ⚠ **必须传清理过的环境。**
                           #   不传就继承父进程的完整环境 —— 而桥接跑在
                           #   宿主应用（WorkBuddy/桌面版）的进程树里，
                           #   会继承它注入的产品配置，导致 CLI 去错误的
                           #   端点认证，报出假的 "Authentication required"。
                           #   （实测：完整环境必失败，清理后正常）
                           env=env)
    except subprocess.TimeoutExpired:
        logger.error("  [%s] 超时 %d 秒", label, timeout)
        return False, "", "", -1
    except OSError as exc:
        logger.error("  [%s] 跑不起来：%s", label, exc)
        return False, "", str(exc), -1

    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    ok = r.returncode == 0 and bool(out)
    return ok, out, err, r.returncode


def _report_probe(label: str, ok: bool, out: str, err: str, rc: int,
                  logger: logging.Logger) -> None:
    """把一次探活的结果完整打出来 —— 好与坏都要能看到细节。"""
    if ok:
        logger.info("  ✓ %s（%d 字节）", label, len(out))
        return
    logger.error("  ✗ %s", label)
    logger.error("      退出码：%s", rc)
    logger.error("      stdout：%d 字节%s", len(out),
                 "" if out else "（空）")
    if out:
        for line in out.splitlines()[:5]:
            logger.error("        | %s", line[:160])
    logger.error("      stderr：%d 字节%s", len(err),
                 "" if err else "（空）")
    if err:
        for line in err.splitlines()[:10]:
            logger.error("        ! %s", line[:200])
        # ★ 认出已知模式，直接说该怎么做
        advice = explain_failure(err)
        if advice:
            logger.error("")
            logger.error("      → %s", advice)
    else:
        logger.error("        （没有 stderr —— 说明它自己认为成功了，"
                     "但没往 stdout 写东西）")


def _system_default_path() -> str | None:
    """取系统级默认 PATH（与交互式 shell 无关）。取不到返回 None。"""
    entries: list[str] = []
    paths_file = Path("/etc/paths")
    if paths_file.is_file():
        try:
            entries += [ln.strip() for ln in
                        paths_file.read_text(encoding="utf-8").splitlines()
                        if ln.strip() and not ln.strip().startswith("#")]
        except OSError:
            return None
    # /etc/paths.d 里的补充（homebrew 等会写这里）
    d = Path("/etc/paths.d")
    if d.is_dir():
        try:
            for f in sorted(d.iterdir()):
                try:
                    entries += [ln.strip() for ln in
                                f.read_text(encoding="utf-8").splitlines()
                                if ln.strip()]
                except OSError:
                    continue
        except OSError:
            pass
    # ⚠ **Linux 没有 ``/etc/paths``**（那是 macOS 的约定）。
    #
    #   初版取不到就 ``return None``，调用方于是"判断不了"→ 一律返回 True，
    #   等于在 Linux 上这个检查完全失效。而 CI 跑在 Ubuntu 上，
    #   对应的测试断言 ``is False`` 必然失败 —— 本地 macOS 测不出来。
    #
    #   Linux 的默认 PATH 用 FHS 约定的那几条；这不是从环境读的，
    #   而是发行版默认值，足够用来判断"某个目录是不是用户自己加的"。
    if not entries:
        import sys as _sys

        if _sys.platform.startswith("linux"):
            entries = [
                "/usr/local/sbin", "/usr/local/bin",
                "/usr/sbin", "/usr/bin", "/sbin", "/bin",
            ]
        else:
            return None

    # 按平台补上"确实是系统级、但可能没写进配置文件"的目录。
    # 刻意**不含** ~/.local/bin：它就是我们要检测的嫌疑对象。
    import sys as _sys

    extras = ("/usr/local/bin",)
    if _sys.platform == "darwin":
        extras = ("/opt/homebrew/bin", "/usr/local/bin")
    for extra in extras:
        if extra not in entries:
            entries.append(extra)
    return ":".join(entries)


def _available_in_noninteractive_shell(bin_path: str) -> bool:
    """检查这个 codebuddy **能不能被非交互环境找到**。

    ## 为什么要专门检查这个

    macOS/Linux 上，用户常把 CLI 装在 ``~/.local/bin``，靠 ``~/.zshrc``
    把它加进 PATH。而 ``.zshrc`` **只对交互式 shell 生效**。

    常驻节点由 launchd / systemd / 计划任务拉起，是**非交互**环境，
    只认 ``/etc/paths`` 这类系统级配置 —— 于是服务一启动就
    ``command not found``。

    最恶劣的症状：手动跑 ``--check-tools`` 全绿，装成服务后每条任务都失败。

    ## 判定方式（一个容易写错的地方）

    ⚠ **不能只看 `command -v <name>`。** 那样只要名字出现在系统 PATH 的
    *任意* 目录里就返回真 —— 即使传入的路径在 ``~/.local/bin``。
    实测过：先建了 ``/opt/homebrew/bin/codebuddy`` 软链接，
    再检查 ``~/.local/bin/codebuddy``，两者都返回 True，检查形同虚设。

    正确做法：看**这个路径所在的目录**在不在系统默认 PATH 里。
    """
    import subprocess as _sp

    system_path = _system_default_path()
    if system_path is None:
        return True   # 判断不了就不误报

    bin_dir = str(Path(bin_path).resolve().parent)
    # 软链接的原目标可能在别处，所以两个都算（软链接本身所在的目录
    # 才是"被找到"的关键，用未 resolve 的父目录）
    link_dir = str(Path(bin_path).parent)

    dirs = system_path.split(":")
    if link_dir in dirs or bin_dir in dirs:
        # 目录在系统 PATH 里 —— 再确认真的能 command -v 到
        name = Path(bin_path).name
        try:
            r = _sp.run(["/bin/sh", "-c", f"command -v {name}"],
                        capture_output=True, **TEXT_KWARGS, timeout=15,
                        env={"HOME": str(Path.home()), "PATH": system_path})
        except (OSError, _sp.TimeoutExpired):
            return True
        return r.returncode == 0 and bool((r.stdout or "").strip())

    return False


def _detect_cli_auth() -> dict:
    """探测 **CLI 自己**的登录状态，返回 ``{"ok", "summary", "detail"}``。

    ## 为什么不能只看 IDE

    CodeBuddy IDE 和 CLI 是两个独立应用（虽然同一引擎）：

    ======  ================================  ==========================
    应用     凭据存放                           实测证据
    ======  ================================  ==========================
    IDE      Electron 应用数据目录 +            keychain 里有
             Safe Storage                      "CodeBuddy CN Safe Storage"
    CLI      OS 凭据库（独立 service 名）        没有对应条目 = 未登录
    ======  ================================  ==========================

    所以「IDE 里已登录」对 CLI **没有任何帮助**。

    ## 判定顺序（与官方文档一致）

    ``CODEBUDDY_AUTH_TOKEN`` > ``apiKeyHelper`` > ``CODEBUDDY_API_KEY`` > 交互登录
    """
    import sys as _sys

    # ① 环境变量方式 —— 最明确，优先
    token = os.environ.get("CODEBUDDY_AUTH_TOKEN", "")
    if token:
        return {"ok": True, "summary": "CODEBUDDY_AUTH_TOKEN（环境变量）",
                "detail": ""}

    api_key = os.environ.get("CODEBUDDY_API_KEY", "")
    if api_key:
        env_scope = os.environ.get("CODEBUDDY_INTERNET_ENVIRONMENT", "")
        # ⚠ 官方文档明确要求：国内版必须同时设 CODEBUDDY_INTERNET_ENVIRONMENT=internal，
        #   只设 API Key 会 401。实测确认过：错误里会带
        #   (auth-type:...,token-type:ApiKey,token-length:N) 但仍 401。
        if env_scope:
            return {"ok": True,
                    "summary": f"CODEBUDDY_API_KEY（环境={env_scope}）",
                    "detail": ""}
        return {
            "ok": False,
            "summary": "CODEBUDDY_API_KEY 已设，但缺 CODEBUDDY_INTERNET_ENVIRONMENT",
            "detail": (
                "⚠ 只设 CODEBUDDY_API_KEY 会返回 401。官方文档要求同时设：\n"
                "    国内版  → CODEBUDDY_INTERNET_ENVIRONMENT=internal\n"
                "    海外版  → 不设（或留空）\n"
                "    iOA     → ioa\n"
                "    专享版  → cloudhosted（还需在 settings.json 配 endpoint）\n"
                "  补上环境变量再重试；或改用交互式登录。"
            ),
        }

    # ② OS 凭据库
    if _sys.platform == "darwin":
        found, names = _keychain_codebuddy_entries()
        if found:
            return {"ok": True, "summary": f"macOS Keychain（{names}）",
                    "detail": ""}
        return {
            "ok": False,
            "summary": "未检测到 CLI 凭据",
            "detail": (
                "macOS Keychain 里没有 codebuddy CLI 的凭据条目。\n"
                "  注意：能看到的 'CodeBuddy CN Safe Storage' 是 **IDE** 的，\n"
                "        不是 CLI 的 —— 两者不通用。"
            ),
        }

    if _sys.platform == "win32":
        return {
            "ok": False,
            "summary": "未检测到环境变量方式的凭据",
            "detail": (
                "没有 CODEBUDDY_API_KEY / CODEBUDDY_AUTH_TOKEN。\n"
                "  CLI 也可能已通过交互登录把凭据存进了 Windows 凭据管理器，\n"
                "  那种情况本函数读不出来（需要 DPAPI）。\n"
                "  → 直接往下试一次就知道了；若报 401，按提示登录。"
            ),
        }

    return {"ok": True, "summary": "（无法判定，直接试）", "detail": ""}


def _keychain_codebuddy_entries() -> tuple[bool, str]:
    """在 macOS Keychain 里找 CLI 的凭据条目。

    ⚠ 只看 **service 名**，不读凭据内容 —— 那是敏感数据。
    """
    import subprocess as _sp

    try:
        r = _sp.run(["security", "dump-keychain"], capture_output=True,
                    **TEXT_KWARGS, timeout=20)
    except (OSError, _sp.TimeoutExpired):
        return False, ""

    if r.returncode != 0:
        return False, ""

    # 从 dump 里抽 svce 值
    names = []
    for line in r.stdout.splitlines():
        s = line.strip()
        if '"svce"' in s and "blob" in s:
            # 形如：  "svce"<blob>="XXX"
            val = s.rsplit("=", 1)[-1].strip().strip('"')
            names.append(val)

    # CLI 的条目名里通常带 codebuddy 且**不**含 "Safe Storage"
    # （Safe Storage 是 Electron 应用用来加密本地存储的，与 CLI 无关）
    cli_names = [n for n in names
                 if "codebuddy" in n.lower() and "safe storage" not in n.lower()
                 and "workbuddy" not in n.lower()]
    if cli_names:
        return True, ", ".join(sorted(set(cli_names))[:3])
    return False, ""


def check_tools(bin_path: str, logger: logging.Logger) -> int:
    """确认 CodeBuddy 能被**程序**调用。注册之前必须先过这一关。

    ⚠ 顺序刻意：反过来的话桥接会先注册成功，然后每条任务都失败 ——
    而原因藏在节点日志里，运维在协调节点上完全看不见。

    ## 分层探活，而不是一步到位

    初版只跑一次「json + 一个 prompt」，输出为空时报
    「输出不是 JSON：」—— 什么信息都没有。而失败可能在任意一层：

    ==========  ====================================================
    层           能排除什么
    ==========  ====================================================
    --version    二进制本身能不能跑
    text 模式    -p / 权限模式 / 登录状态（不涉及 JSON 格式）
    json 模式    --output-format json 是否被支持、字段叫什么
    ==========  ====================================================

    分层之后，哪一层断一目了然。
    """
    if not Path(bin_path).exists():
        logger.error("找不到 %s", bin_path)
        logger.error("  安装：npm install -g @tencent-ai/codebuddy-code")
        logger.error("  或：irm https://www.codebuddy.cn/cli/install.ps1 | iex")
        return 1

    # 全程用**清理过的环境** —— 与真实任务执行走同一条路径。
    # 否则 --check-tools 会在污染环境里得出"未登录"的假结论。
    probe_env = _actor_env("__probe__")

    # ---- ① 版本 ----
    ok, out, err, rc = _probe_once(bin_path, ["--version"], timeout=60,
                                   logger=logger, label="--version",
                                   env=probe_env)
    if ok:
        logger.info("版本：%s", out.splitlines()[0])
    else:
        _report_probe("--version", ok, out, err, rc, logger)
        logger.error("  → 二进制本身跑不起来，先解决这个")
        return 1

    # ---- 认证方式预检 ----
    #
    # ⚠ **CodeBuddy IDE 登录了不代表 CLI 登录了。**
    #
    #   两者是独立应用，凭据存的地方不同（实测确认）：
    #     · IDE（Electron）→ ~/Library/Application Support/CodeBuddy CN
    #                        + 系统 Safe Storage（"CodeBuddy CN Safe Storage"）
    #     · CLI            → 操作系统凭据库，service 名与 IDE 不同
    #
    #   所以先在 keychain / 凭据管理器里找 CLI 自己的那条；
    #   找不到就说明 CLI 没登录过，直接给两条路，不用浪费一次调用去试。
    auth_hint = _detect_cli_auth()
    # ---- 检查"非交互环境能不能找到它" ----
    #
    # ⚠ 这是个真踩到的坑，而且很隐蔽：
    #
    #   codebuddy 若只装在 `~/.local/bin`，而 PATH 是靠 `~/.zshrc` 加的，
    #   那么**只有交互式终端能用**。
    #   `.zshrc` 只在交互式 shell 里被读取 ——
    #   而常驻节点是计划任务/launchd 拉起来的，那是**非交互环境**，
    #   根本不读 `.zshrc`，于是 `codebuddy: command not found`。
    #
    #   症状：手动跑 `--dry-run` 一切正常，装成服务后全挂。
    #   （macOS 实测：/etc/paths 里没有 ~/.local/bin，
    #     干净 login shell 也找不到它）
    #
    #   修法：放进 /etc/paths 里已有的目录（如 /usr/local/bin、/opt/homebrew/bin），
    #         或软链接过去。
    if not _available_in_noninteractive_shell(bin_path):
        logger.warning("")
        logger.warning("⚠ %s 只在**交互式终端**里可用", bin_path)
        logger.warning("   常驻节点由计划任务/launchd 拉起，那是非交互环境，")
        logger.warning("   PATH 不读 ~/.zshrc —— 服务启动后会 command not found。")
        logger.warning("")
        logger.warning("   修法（选一个）：")
        logger.warning("     sudo ln -sf %s /usr/local/bin/  # 需 sudo", bin_path)
        logger.warning("     ln -sf %s /opt/homebrew/bin/    # Apple Silicon 免 sudo",
                       bin_path)
        logger.warning("")

    logger.info("认证方式：%s", auth_hint["summary"])

    # ⚠ 预检**只在该阻断时阻断**。
    #
    #   "检测不到凭据" ≠ "一定没登录" —— 我的探测有盲区：
    #     · macOS：不知道 CLI 实际用的 keychain service 名，
    #       条目名可能不匹配我的过滤规则
    #     · Windows：凭据管理器内容需要 DPAPI 才能读，根本读不到
    #
    #   所以"检测不到"只警告、继续试。否则会**假阴性阻断**：
    #   用户明明登录了，脚本却说没登录，还不肯试 —— 那比不预检更糟。
    #
    #   唯一确定会失败、值得提前阻断的：API Key 设了但缺
    #   CODEBUDDY_INTERNET_ENVIRONMENT —— 实测必 401。
    if auth_hint["summary"].startswith("CODEBUDDY_API_KEY 已设") \
            and not auth_hint["ok"]:
        logger.error("")
        for line in auth_hint["detail"].splitlines():
            logger.error("  %s", line)
        logger.error("")
        logger.error("  这个组合确定会 401，先不跑了。补上环境变量再重试。")
        return 1

    if not auth_hint["ok"]:
        logger.warning("")
        for line in auth_hint["detail"].splitlines():
            logger.warning("  %s", line)
        logger.warning("")
        logger.warning("  ⚠ 但这只是**预检**，可能漏判 —— 还是试一次。")
        logger.warning("     若下面报 401，就按提示登录或配 API Key。")

    logger.info("下面两次会消耗少量额度…")

    # ---- ② text 模式（不碰 JSON）----
    #
    # ⚠ 这一步刻意**不带** --output-format json。
    #   如果它也失败，说明问题在 -p / 权限模式 / 登录，与 JSON 无关。
    ok_t, out_t, err_t, rc_t = _probe_once(
        bin_path,
        ["-p", "回复两个字：就绪", "--permission-mode", "dontAsk",
         "--port", str(DEFAULT_PORT)],
        timeout=180, logger=logger, label="text 模式", env=probe_env)
    _report_probe("text 模式（-p + dontAsk）", ok_t, out_t, err_t, rc_t, logger)

    if not ok_t:
        logger.error("")
        logger.error("  → text 模式都不出东西，问题不在 JSON 格式。按顺序查：")
        logger.error("     1. 是否登录过？先跑一次交互式 `codebuddy` 完成登录")
        logger.error("     2. 手动验证：在终端里跑下面这条，看有没有输出")
        logger.error("        %s -p \"回复就绪\" --permission-mode dontAsk", bin_path)
        logger.error("     3. 若手动有输出而这里没有，可能是 stdin 被当成空管道 ——")
        logger.error("        把 --dry-run 换个目录再试（避开项目配置影响）")
        return 1
    logger.info("  → 原始输出：%r", out_t[:120])

    # ---- ③ json 模式 ----
    ok_j, out_j, err_j, rc_j = _probe_once(
        bin_path,
        ["-p", "回复两个字：就绪", "--output-format", "json",
         "--permission-mode", "dontAsk", "--port", str(DEFAULT_PORT)],
        timeout=180, logger=logger, label="json 模式", env=probe_env)
    _report_probe("json 模式（--output-format json）", ok_j, out_j, err_j, rc_j, logger)

    if not ok_j:
        logger.error("")
        logger.error("  → text 模式能出、json 模式不出。可能：")
        logger.error("     · 这个版本的 --output-format json 行为不同")
        logger.error("     · 输出被写到了别处（看上面的 stderr）")
        logger.error("     兜底：桥接可以用 text 模式跑，只是要改 extract_answer()")
        return 1

    # ---- ④ 信封结构 ----
    try:
        payload = json.loads(out_j)
    except json.JSONDecodeError:
        logger.error("  ✗ json 模式的输出不是合法 JSON")
        for line in out_j.splitlines()[:8]:
            logger.error("      | %s", line[:200])
        return 1

    # ⚠ payload 是**事件数组**，不是对象 —— 不能 sorted(payload)
    #   （那会试图比较 dict，报 TypeError）。只统计类型。
    if isinstance(payload, list):
        kinds = [e.get("type") for e in payload if isinstance(e, dict)]
        logger.info("  ✓ 事件数组：%d 个，类型 %s",
                    len(payload), sorted(set(map(str, kinds))))
    else:
        logger.info("  ✓ 输出对象：键 %s", sorted(payload))
    try:
        answer = extract_answer(payload)
    except RuntimeError as exc:
        logger.error("  ✗ %s", exc)
        logger.error("     → 字段名与预期不符，改 extract_answer()")
        return 1

    logger.info("  ✓ 取出回答：%r", answer.strip()[:60])
    logger.info("")
    logger.info("CodeBuddy 可以被程序调用 —— 可以往下走注册了。")
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

class _BomUtf8RotatingFileHandler(logging.handlers.RotatingFileHandler):
    r"""写 UTF-8 **带 BOM** 的轮转文件处理器。

    ## 为什么必须带 BOM

    日志文件本身是 UTF-8 写的（这点没错），但 **Windows 自带的
    PowerShell 5.1 的 ``Get-Content`` 默认按系统 ANSI 编码解码** ——
    中文 Windows 上是 GBK / 代码页 936。

    于是用户执行最自然的那条命令::

        Get-Content "$HOME\.nexus-mesh\logs\work-desktop.log" -Tail 25

    拿到的全是乱码::

        10:26:56 INFO  codebuddy-node 路 鐗堟湰锛?.178.0

    实测对照（"版本" 的 UTF-8 字节 ``e7 89 88 e6 9c ac`` 按 GBK 解码）::

        鐗堟湰   ← 正是用户看到的样子

    ## BOM 为什么能解决

    PowerShell 5.1 的 ``Get-Content`` 内部用
    ``StreamReader(detectEncodingFromByteOrderMarks)`` ——
    **有 BOM 就正确识别为 UTF-8**，不需要用户加任何参数。

    ## 为什么不用「让用户加 -Encoding UTF8」

    那等于把问题推给用户。而且实际上没人会记得加 ——
    我自己在写这条命令时就没想到。**默认就对**才是对的。

    ## 为什么不用 utf-8-sig 编码器图省事

    直接用 ``encoding="utf-8-sig"`` 有个陷阱：``RotatingFileHandler``
    以 **append 模式**打开文件，而 ``utf-8-sig`` 每次打开都会写 BOM ——
    **追加到已有文件时会往文件中间插一个 BOM**，反而更糟。

    所以这里重写 ``_open()``：**只在新文件（或轮转后的新文件）为空时
    写一次 BOM**。这样：
      · 首次创建 → 有 BOM
      · 重启后追加 → 不重复写
      · 轮转后的新文件 → 有 BOM（``super()._open()`` 会在新路径上重开）
    """

    _BOM = "\ufeff"

    def _open(self):  # noqa: D102 - 继承自父类
        stream = super()._open()
        try:
            # append 模式下 tell() 就是文件末尾位置。
            # 为 0 说明是空文件（新建 / 刚轮转出的新文件）→ 需要 BOM。
            if stream.tell() == 0:
                stream.write(self._BOM)
                stream.flush()
        except Exception:  # noqa: BLE001 - 写不了 BOM 不该让日志挂掉
            pass
        return stream


def _setup_logging(verbose: bool, log_file: str) -> None:
    """配置日志：**始终写 stderr**，可选再写一个轮转文件。

    ## 为什么必须有 ``--log-file``

    计划任务 / systemd / launchd 拉起的进程**没有终端**。
    其中 **Windows 计划任务不捕获 stderr** —— 输出直接丢弃。

    于是用户装完计划任务后，如果节点起不来，**什么线索都没有**：
    没有日志文件、没有控制台，任务状态只说"上次运行结果"。
    这对"交给别人试用"是致命的：装不上就是装不上，无从排查。

    （systemd 好一些，默认进 journald，``journalctl -u xxx`` 能看。
    launchd 也能把 stdout/stderr 重定向到文件。
    Windows 计划任务两样都没有，只能由程序自己写。）

    ## 为什么轮转

    这是常驻进程，一天几千行很正常。不轮转会无限增长，最终写满磁盘 ——
    而磁盘满之后的表现是"任务开始莫名失败"，与日志本身无关，
    很难联想到是日志把盘写满了。
    """
    # ⚠ **不要用 ``basicConfig(force=True, handlers=[...])``。**
    #
    #   那会**清掉 root logger 上已有的 handler** —— 包括调用方（或测试框架）
    #   装的。实测：pytest 的 ``caplog`` 就是这么失效的，
    #   表现为"日志明明打了，caplog 里什么都抓不到"。
    #
    #   对可嵌入的模块来说更糟：宿主可能已经配好了自己的日志格式，
    #   我们一进来就把它抹了。
    #
    #   改成**增量添加 + 幂等标记**：只加自己那两个 handler，
    #   不碰别人的；重复调用也不会叠加。
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    if not any(getattr(h, "_nxs_node_handler", False) for h in root.handlers):
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s · %(message)s",
            datefmt="%H:%M:%S",
        )

        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream._nxs_node_handler = True       # type: ignore[attr-defined]
        root.addHandler(stream)

        if log_file:
            path = Path(log_file).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            # 用带 BOM 的处理器 —— 见 _BomUtf8RotatingFileHandler 的说明。
            # 不加 BOM 的话，PowerShell 5.1 的 Get-Content 会按 GBK 解码，
            # 中文全是乱码。
            file_handler = _BomUtf8RotatingFileHandler(
                path,
                maxBytes=5 * 1024 * 1024,   # 5 MB
                backupCount=3,              # 共 4 个文件，最多 20 MB
                encoding="utf-8",           # BOM 由 _open() 单独写
            )
            file_handler.setFormatter(formatter)
            file_handler._nxs_node_handler = True   # type: ignore[attr-defined]
            root.addHandler(file_handler)


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
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"codebuddy 监听的本地端口（默认 {DEFAULT_PORT} = 自动分配）。"
                             "遇到 EADDRINUSE 就该留默认值 —— 那是与 "
                             "WorkBuddy/CodeBuddy IDE 抢端口")
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
    parser.add_argument("--log-file", default="", metavar="PATH",
                        help="额外把日志写入文件（轮转：5 MB × 3）。"
                             "⚠ **常驻/计划任务下几乎必须加** —— "
                             "计划任务不捕获 stderr，不加就完全没有日志可查")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose, args.log_file)
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
    # ★ 先加载凭据文件 —— 它可能已经带了 url 和 ca_file
    actor, secret_error = _resolve_actor(args.node_id)
    secret = actor.secret.decode("utf-8") if actor else ""

    # 优先级：命令行 > 环境变量 > 凭据文件（文件兜底，免得配两遍）
    if not args.url and actor and actor.url:
        args.url = actor.url
    if not args.ca_file and actor and actor.ca_file:
        args.ca_file = actor.ca_file

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
                        port=args.port,
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
        # ⚠ **必须说清"还没在跑"。**
        #
        #   ``--dry-run`` 打印「全部就绪」很容易被理解成"装好了、可以收任务了"。
        #   但它的语义是**检查完就退出** —— 不发心跳。
        #
        #   而协调节点把心跳超时（3 个间隔）的节点标记为 stale，
        #   stale 不可路由，派任务过来直接拒：
        #       PolicyDenied: denied[MX-RECEIVE-03]
        #
        #   这个错误码看起来像"域不匹配 / 卡片不对"，很难联想到
        #   "节点根本没在跑"。实测踩过两次。
        if approved:
            log.info("")
            log.info("⚠ 但**现在还没有节点在跑** —— --dry-run 检查完就退出。")
            log.info("   不收任务、不发心跳。超时后协调节点会把它判为 stale，")
            log.info("   那时派任务过来会被拒（MX-RECEIVE-03，看着像域不匹配）。")
            log.info("")
            log.info("   下一步（二选一）：")
            log.info("     python %s                     ← 前台常驻，窗口别关",
                     "examples\\codebuddy_node.py")
            log.info("     powershell ... -InstallTask                    "
                     "← 装成计划任务，关窗口也在跑")
        return 0

    log.info("开始常驻（node=%s 域=%s）", args.node_id, args.domain)
    try:
        client.run_forever()
    except KeyboardInterrupt:
        log.info("已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
