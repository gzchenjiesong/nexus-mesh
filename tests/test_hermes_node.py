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


    @covers("M5.F7")
    def test_backlog_is_conservative(self):
        """★ 每条任务都烧 token，所以信箱深度刻意压低。

        ⚠ 字段换了（权限模型改造 Step 5b）：

            旧：quota.tasks_per_day <= 100 / concurrent_tasks <= 2
            新：self_limits.max_backlog 很小

        **成本控制的意图不变**，但机制变了：不再靠"按日配额拦你"，
        而是靠"队列满了拒收"—— 后者是真能生效的，前者从未生效。
        """
        card = build_card("hermes-working", "cloud", toolsets="safe")
        assert card.self_limits.max_backlog <= 10, (
            "信箱深度必须小 —— 每条任务烧 token，堆积等于烧钱"
        )
        assert card.effective_capacity() <= 2

    @covers("M10.F1")
    def test_response_level_is_summary(self):
        """产出等级必须声明为摘要级。

        ⚠ 原来是查 `quota.bytes_per_response == MAX_ANSWER_BYTES`。
        配额删了（Step 5b），改用等级表达同一件事：
        **这个节点对外只给摘要，不给更细的内容。**

        桥接自己的字节截断（MAX_ANSWER_BYTES）仍然生效 —— 那是节点侧
        的自我裁剪，与网络侧的等级上限是两道独立的闸门。
        """
        from nxs.core.models import Level

        card = build_card("hermes-working", "cloud", toolsets="safe")
        assert card.self_limits.max_response_level is Level.SUMMARY


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
        env = _hermes_env("/root/.hermes/profiles/working", "taskflow")
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
            node_id="test-node",
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


class TestRegistrationOutcomeHandling:
    """★ 「注册失败」与「待批准」必须分开报。

    ⚠ 这条来自实测：``AU-SIG-01``（服务端不认识节点密钥）被桥接报成了
    「尚未批准 —— 这是**正常**的，去批准一下就好」。

    为什么会这样：SDK 的 ``register()`` 把异常吞成
    ``{"outcome": "error", "approved": False}``（它不该让注册失败拖垮
    节点，这个设计本身是对的）。而桥接只看 ``approved``，
    于是**鉴权失败**和**等待批准**长得一模一样。

    后果很实际：人会对着一个签名错误去点批准，然后发现批准了还是不行。
    """

    @covers("M1.F7")
    def test_outcome_error_is_distinguished_from_pending(self):
        """桥接源码必须显式判断 ``outcome == "error"``。"""
        source = (REPO_ROOT / "examples" / "hermes_node.py").read_text(
            encoding="utf-8")
        assert 'outcome == "error"' in source, (
            "桥接没有区分 outcome=error —— 鉴权失败会被误报成待批准"
        )
        # 且要给出针对 AU-SIG-01 的修法提示
        assert "AU-SIG-01" in source, (
            "没给出签名失败的具体修法，用户只能猜"
        )
        assert "systemctl restart nexus-mesh" in source, (
            "AU-SIG-01 的根因之一是「服务端进程环境里没有这个密钥」，"
            "必须提示重启，否则改完 env 文件也不生效"
        )

    @covers("M1.F7")
    def test_dry_run_fails_on_registration_error(self):
        """``--dry-run`` 在注册失败时必须返回非零。

        返回 0 会让「探活成功但注册失败」看起来像一切正常。
        """
        source = (REPO_ROOT / "examples" / "hermes_node.py").read_text(
            encoding="utf-8")
        # 在 outcome == "error" 分支里必须有 return 1
        branch = source.split('outcome == "error"')[1][:1200]
        assert "return 1" in branch, (
            "注册失败分支没有返回非零 —— dry-run 会说「一切正常」"
        )


class TestActorInjection:
    """★ 桥接必须告诉 Agent「你是谁」。

    ⚠ 这是 A2A 协作的钥匙。Agent 通过 `nxs-cli ask` 问别的 Agent 时，
    `nxs-cli` 靠 `NXS_ACTOR` 这个名字去查凭据。

    没有它的话，Agent 的 shell 里 `nxs-cli` 会说"无法确定身份" ——
    而那是一条它自己修不了的错误（它不知道自己的节点 ID）。

    **密钥刻意不进 Agent 上下文**：它只知道名字，
    凭据在 /etc/nexus-mesh/agents/<id>.env 里。
    """

    @covers("M14.F3")
    def test_hermes_env_carries_actor(self):
        from examples.hermes_node import _hermes_env

        env = _hermes_env("/root/.hermes/profiles/working", "hermes-working")
        assert env["NXS_ACTOR"] == "hermes-working"
        # profile 选择也要在
        assert env["HERMES_HOME"] == "/root/.hermes/profiles/working"

    @covers("M14.F3")
    def test_hermes_env_does_not_carry_secret(self):
        """★ 只给名字，**不给密钥**。

        ⚠ **这条测试原来是个假阳性。** 初版直接检查 ``_hermes_env()``
        的返回里有没有 key 名含 SECRET —— 但它跑在测试进程里，
        那个环境本来就没有 ``NXS_SECRET_*``，所以断言永远成立。

        真实情况是：桥接的 systemd unit 用
        ``EnvironmentFile=/etc/nexus-mesh/nxs.env`` 加载了**整个文件**，
        于是桥接进程环境里有运维密钥和所有节点的密钥；
        ``_hermes_env`` 用 ``dict(os.environ)`` 继承 —— 全传给了 Agent。

        那是实测发现的：``tr '\0' '\n' < /proc/<pid>/environ`` 里
        赫然躺着 NXS_OPS_SECRET 和别人的 NXS_SECRET_*。

        修法：现在这条测试**先真的注入**密钥变量，再断言它们被剔除。
        只有这样它才能抓到回归。
        """
        import os

        from examples.hermes_node import _hermes_env

        injected = {
            "NXS_SECRET_TASKFLOW": "s" * 64,
            "NXS_SECRET_COORDINATOR": "c" * 64,
            "NXS_OPS_SECRET": "o" * 64,
            "NXS_SECRET_HERMES_WORKING": "h" * 64,
            # 一个不该被剔的作对照
            "NXS_ACTOR_KEEP_ME": "keep"
        }
        saved = {k: os.environ.get(k) for k in injected}
        os.environ.update(injected)
        try:
            env = _hermes_env("/tmp/profile", "taskflow")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        leaked = sorted(k for k in env if k.startswith(("NXS_SECRET", "NXS_OPS")))
        assert not leaked, (
            f"这些凭据泄漏到了 Agent 的子进程环境：{leaked}\n"
            f"  Agent 能 exec，所以它读得到 —— 等于给了它运维密钥与别人的身份。"
        )
        # 对照：非凭据的变量必须保留（否则会破坏 Agent 的运行环境）
        assert env.get("NXS_ACTOR_KEEP_ME") == "keep"
        assert env.get("NXS_ACTOR") == "taskflow"

    @covers("M14.F3")
    def test_actor_reaches_subprocess(self, workdir):
        """端到端：子进程里真的能读到 NXS_ACTOR。"""
        import logging
        from examples.hermes_node import make_handler

        fake = workdir / "echo_actor.py"
        fake.write_text(
            "import os\nprint(os.environ.get('NXS_ACTOR', 'MISSING'))\n",
            encoding="utf-8",
        )
        handler = make_handler(
            [sys.executable, str(fake)],
            timeout=15, logger=logging.getLogger("t"),
            hermes_home=str(workdir), node_id="taskflow",
        )
        assert handler({"question": "问题"})["answer"] == "taskflow"


class TestProbe:
    """★ 探活路径必须被测到 —— 它的缺失让一个真 bug 上线了。

    ⚠ 事故记录：v0.17 我把 ``_hermes_env`` 的签名加了 ``node_id``，
    改了 ``make_handler`` 那条路径，**但漏了 ``probe()`` 里的调用**。
    测试全绿（它们只走 make_handler），部署后桥接直接起不来：

        TypeError: _hermes_env() missing 1 required positional argument

    **改了签名就要扫全部调用点**。而更该记住的是：
    ``probe()`` 是 main() 里的必经路径（先探活再注册），
    之前一条测试都没有 —— 那是覆盖盲区。
    """

    @staticmethod
    def _fake(workdir, body: str):
        f = workdir / "fake_probe.py"
        f.write_text(body, encoding="utf-8")
        return [sys.executable, str(f)]

    @covers("M1.F16")
    def test_probe_succeeds(self, workdir):
        import logging
        from examples.hermes_node import probe

        cmd = self._fake(workdir, "print('就绪')\n")
        assert probe(cmd, timeout=15, logger=logging.getLogger("t"),
                     hermes_home=str(workdir), node_id="taskflow") is True

    @covers("M1.F16")
    def test_probe_reports_failure_on_nonzero(self, workdir):
        import logging
        from examples.hermes_node import probe

        cmd = self._fake(workdir,
                         "import sys\nsys.stderr.write('boom')\nsys.exit(2)\n")
        assert probe(cmd, timeout=15, logger=logging.getLogger("t"),
                     hermes_home=str(workdir), node_id="taskflow") is False

    @covers("M1.F16")
    def test_probe_passes_node_id_to_subprocess(self, workdir):
        """★ 探活时也要传 NXS_ACTOR —— 就是这里漏了导致桥接起不来。

        probe 里跑的子进程同样需要知道身份：它有可能是 Agent 自己的
        启动检查（比如它想调 nxs-cli 确认网络可达）。
        更重要的是：**签名一致** —— 两处调用同一个函数，
        参数必须都对，否则一处能跑一处崩。
        """
        import logging
        from examples.hermes_node import probe

        cmd = self._fake(
            workdir,
            "import os, sys\n"
            "sys.exit(0 if os.environ.get('NXS_ACTOR') == 'taskflow' else 3)\n",
        )
        assert probe(cmd, timeout=15, logger=logging.getLogger("t"),
                     hermes_home=str(workdir), node_id="taskflow") is True, \
            "probe 没把 NXS_ACTOR 传给子进程"

    @covers("M1.F16")
    def test_probe_timeout_returns_false(self, workdir):
        import logging
        from examples.hermes_node import probe

        cmd = self._fake(workdir, "import time\ntime.sleep(30)\n")
        assert probe(cmd, timeout=2, logger=logging.getLogger("t"),
                     hermes_home=str(workdir), node_id="taskflow") is False
