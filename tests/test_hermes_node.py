"""Hermes 桥接节点的测试（``examples/hermes_node.py``）。

这个桥接把一个 LLM Agent 接进了任务总线 —— 也就是说，
**能发任务的节点，就能触发这台机器上的一次 LLM 执行**。
所以它的安全边界比普通节点更需要被钉住。

这里只测**不依赖真实 Hermes** 的部分（纯函数与参数校验）。
真实端到端验证在部署时手工做（见 ``docs/hermes-integration.md``）。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT, covers

sys.path.insert(0, str(REPO_ROOT))

from examples.hermes_node import (  # noqa: E402
    DEFAULT_TOOLSETS,
    MAX_ANSWER_BYTES,
    MAX_QUESTION_CHARS,
    _hermes_env,
    _truncate_utf8,
    build_card,
    build_hermes_cmd,
    build_prompt,
    make_handler,
)


class TestRoleCard:
    """角色卡必须诚实、确定性、且不给多余权限。"""

    @covers("M5.F1")
    def test_card_is_deterministic(self):
        """★ 两次构造必须完全一致。

        卡片一变，服务端的批准会自动撤销（审批针对的是那张卡）。
        所以卡里不能有时间戳、随机串、主机名之类会变的东西。
        """
        a = build_card("hermes-working", "cloud", toolsets="safe")
        b = build_card("hermes-working", "cloud", toolsets="safe")
        assert a.to_dict() == b.to_dict()

    @covers("M5.F1")
    def test_card_declares_executor_only(self):
        """★ 只给执行权，不给发布权。

        开放 ``issuer`` 等于让这个节点能把任务参数当外发通道 ——
        而它的参数来自别的节点，那是一条不该存在的路。
        """
        card = build_card("hermes-working", "cloud", toolsets="safe")
        assert card.roles.executor is True
        assert card.roles.issuer is False
        assert card.roles.relay is False
        assert card.roles.observer is False

    @covers("M5.F1")
    def test_answer_is_declared_freetext(self):
        """``answer`` 是自由文本，必须显式声明。

        不声明的话边界不会清洗它 —— 而它恰好是 LLM 输出，
        最可能携带"伪装成事实的指令"。
        """
        card = build_card("hermes-working", "cloud", toolsets="safe")
        cap = card.capability("agent.ask")
        assert cap is not None
        assert "answer" in cap.freetext_fields
        assert "answer" in cap.response_fields

    @covers("M5.F1")
    def test_can_receive_from_is_a_domain_not_a_node(self):
        """★ ``can_receive_from`` 匹配的是**域**，不是节点 ID。

        这是项目里最容易写错的一个参数（配置注释也曾写错）。
        传进去的是 ``domain`` 参数，就该原样出现在约束里。
        """
        card = build_card("hermes-working", "cloud", toolsets="safe")
        assert card.constraints.can_receive_from == ("cloud",)

    @covers("M5.F7")
    def test_quota_is_conservative(self):
        """★ 每条任务都烧 token，所以日配额刻意压低。

        这不是保守，是成本控制：一个默认 100 会让失控的循环
        一天烧掉几百次 LLM 调用。
        """
        card = build_card("hermes-working", "cloud", toolsets="safe")
        assert card.constraints.quota.tasks_per_day <= 100
        assert card.constraints.quota.concurrent_tasks <= 2

    @covers("M10.F1")
    def test_bytes_per_response_matches_freetext_limit(self):
        """产出字节上限要与桥接自己的截断阈值一致。

        自己截断可以加省略号、可以记日志；被边界截断是静默的。
        """
        card = build_card("hermes-working", "cloud", toolsets="safe")
        assert card.constraints.quota.bytes_per_response == MAX_ANSWER_BYTES


class TestHermesInvocation:
    """调 hermes 的命令与环境必须正确 —— 错了不会报错，只会答错。"""

    @covers("M1.F16")
    def test_default_toolset_is_safe(self):
        """★ 默认工具集必须是 ``safe``（无终端访问）。

        放宽它会把这个能力同时授予 mesh 里每个能发任务的节点。
        """
        assert DEFAULT_TOOLSETS == "safe"

    @covers("M1.F16")
    def test_command_has_toolsets_and_oneshot(self):
        cmd = build_hermes_cmd("/usr/bin/python", None, "safe")
        assert "-t" in cmd and "safe" in cmd
        assert "-z" in cmd, "必须是单次模式，否则会进交互等待"
        # 最后一项留给 prompt，这里不该有
        assert cmd[-1] == "-z"

    @covers("M1.F16")
    def test_model_override_is_optional(self):
        """不指定模型时不传模型名 —— 让 profile 配置决定。

        ⚠ 不能靠"命令里有没有 -m" 判断：``python -m hermes_cli.main``
        本身就带一个 -m（模块标志）。要看的是**模型名**在不在。
        """
        default_cmd = build_hermes_cmd("/usr/bin/python", None, "safe")
        assert "deepseek-v4-pro" not in default_cmd
        # python -m 的那个 -m 是模块标志，不是模型标志
        assert default_cmd.count("-m") == 1

        explicit = build_hermes_cmd("/usr/bin/python", "deepseek-v4-pro", "safe")
        assert "deepseek-v4-pro" in explicit
        assert explicit.count("-m") == 2, "多出来的那个才是模型标志"

    @covers("M1.F16")
    def test_hermes_home_goes_through_env_not_cli(self):
        """★ ``HERMES_HOME`` 是环境变量，不是命令行参数。

        这个错误很隐蔽：传成命令行参数时 hermes **不会报错**，
        只会用默认 profile —— 症状是"回答风格不对"，而不是启动失败。
        """
        env = _hermes_env("/root/.hermes/profiles/working")
        assert env["HERMES_HOME"] == "/root/.hermes/profiles/working"
        # 命令里不该出现 HERMES_HOME
        cmd = build_hermes_cmd("/usr/bin/python", None, "safe")
        assert not any("HERMES_HOME" in part for part in cmd)

    @covers("M1.F16")
    def test_env_does_not_leak_secrets_into_command(self):
        """凭据不该被塞进命令行参数 —— 那会出现在 ps 里。"""
        cmd = build_hermes_cmd("/usr/bin/python", None, "safe")
        joined = " ".join(cmd).lower()
        for bad in ("secret", "token", "api_key", "apikey"):
            assert bad not in joined


class TestPromptBuilding:
    """prompt 的构造方式决定回答是否可用。"""

    @covers("M10.F6")
    def test_prompt_includes_the_question(self):
        p = build_prompt("今天几号？")
        assert "今天几号？" in p

    @covers("M10.F6")
    def test_prompt_asks_for_direct_answer(self):
        """★ 必须要求直答。

        ``hermes -z`` 是交互式 Agent 的单次模式：不说明背景时它会反问、
        等待确认、或描述"我打算怎么做"——那些在对话里对，作为产出没用。
        """
        p = build_prompt("问题")
        assert "不要反问" in p
        assert "不要等待确认" in p

    @covers("M10.F6")
    def test_prompt_marks_answer_as_data(self):
        """要告诉 Agent：回答会作为**数据**返回，不是指令。"""
        p = build_prompt("问题")
        assert "数据" in p


class TestTruncation:
    """多字节截断 —— 中文场景下这条一定会被触发。"""

    @covers("M10.F3")
    def test_short_text_untouched(self):
        assert _truncate_utf8("短文本", 100) == "短文本"

    @covers("M10.F3")
    def test_truncates_on_byte_boundary(self):
        text = "测" * 2000          # 6000 字节
        out = _truncate_utf8(text, 4096)
        assert len(out.encode("utf-8")) <= 4096
        # 不能切碎：结果必须全是完整字符
        assert all(ch == "测" for ch in out)
        assert out.encode("utf-8").decode("utf-8") == out

    @covers("M10.F3")
    def test_multibyte_mixed(self):
        text = "a" + "中" * 1000
        out = _truncate_utf8(text, 100)
        assert len(out.encode("utf-8")) <= 100
        out.encode("utf-8").decode("utf-8")   # 不抛异常即为完整


class TestHandlerValidation:
    """参数校验：**宁可失败，不要用错输入换一个看起来成功的产出**。"""

    @staticmethod
    def _handler(workdir: Path, script: str):
        """用一段 Python 脚本假装成 hermes。"""
        fake = workdir / "fake_hermes.py"
        fake.write_text(script, encoding="utf-8")
        return make_handler(
            [sys.executable, str(fake)],
            timeout=10,
            logger=logging.getLogger("test"),
            hermes_home=str(workdir),
        )

    @covers("M10.F1")
    def test_empty_question_fails(self, workdir):
        handler = self._handler(workdir, "print('不该被调用')")
        with pytest.raises(ValueError, match="question"):
            handler({"question": ""})
        with pytest.raises(ValueError, match="question"):
            handler({})

    @covers("M10.F1")
    def test_overlong_question_fails(self, workdir):
        handler = self._handler(workdir, "print('不该被调用')")
        with pytest.raises(ValueError, match="超长"):
            handler({"question": "x" * (MAX_QUESTION_CHARS + 1)})

    @covers("M10.F1")
    def test_non_string_question_fails(self, workdir):
        handler = self._handler(workdir, "print('不该被调用')")
        with pytest.raises(ValueError):
            handler({"question": 123})

    @covers("M10.F1")
    def test_successful_call_returns_declared_fields(self, workdir):
        handler = self._handler(
            workdir,
            "import sys\n"
            "print('答案是 42')\n",
        )
        out = handler({"question": "6*7?"})
        assert set(out) == {"answer", "level"}, (
            "返回的键必须严格等于 response_fields，多一个就会被边界拒绝"
        )
        assert out["answer"] == "答案是 42"
        assert out["level"] == "summary"

    @covers("M10.F1")
    def test_nonzero_exit_fails_loudly(self, workdir):
        """★ Hermes 失败时任务必须失败，**不能回空串充数**。

        空产出比失败更难查：调用方会以为"它答了，只是没内容"。
        """
        handler = self._handler(
            workdir,
            "import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n",
        )
        with pytest.raises(RuntimeError, match="退出码 3"):
            handler({"question": "问题"})

    @covers("M10.F1")
    def test_empty_output_fails(self, workdir):
        handler = self._handler(workdir, "pass\n")   # 什么都不打印
        with pytest.raises(RuntimeError, match="空内容"):
            handler({"question": "问题"})

    @covers("M10.F3")
    def test_answer_is_truncated_with_notice(self, workdir):
        """超长回答要在**自己这里**截断，并留下可见的省略标记。"""
        handler = self._handler(
            workdir,
            "print('字' * 3000)\n",   # 9000 字节
        )
        out = handler({"question": "问题"})
        assert len(out["answer"].encode("utf-8")) <= MAX_ANSWER_BYTES
        assert "已截断" in out["answer"], (
            "截断要留痕 —— 静默截断会让人以为回答本来就这么短"
        )

    @covers("M1.F16")
    def test_hermes_home_is_passed_to_subprocess(self, workdir):
        """★ 端到端确认环境变量真的传到了子进程。

        这是"传了但没生效"最容易漏测的一处：命令拼对了、参数也传了，
        但子进程环境里没有 —— hermes 会默默用默认 profile。
        """
        handler = self._handler(
            workdir,
            "import os\n"
            "print(os.environ.get('HERMES_HOME', 'MISSING'))\n",
        )
        out = handler({"question": "问题"})
        assert out["answer"] == str(workdir), (
            "HERMES_HOME 没传到子进程 —— hermes 会用错的 profile 且不报错"
        )

    @covers("M1.F16")
    def test_stdin_is_not_connected(self, workdir):
        """``-z`` 不该读 stdin。给它 DEVNULL，避免万一交互时挂住。"""
        handler = self._handler(
            workdir,
            "import sys\n"
            "data = sys.stdin.read() if sys.stdin else ''\n"
            "print('OK' if not data else 'GOT_INPUT')\n",
        )
        out = handler({"question": "问题"})
        assert out["answer"] == "OK"
