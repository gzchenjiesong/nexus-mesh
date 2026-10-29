"""OpenClaw 桥接节点的测试（``examples/openclaw_node.py``）。

与 Hermes 桥接的区别在于**怎么调**和**怎么解析**：
OpenClaw 走的是 ``openclaw agent --json``，返回一个结构化信封。

安全上的共同点委托给了 ``examples/agent_bridge_common.py``。
那部分**不在这里重复测**：``tests/test_hermes_node.py`` 里的
``TestTruncation`` / ``TestHandlerValidation`` 走的已经是同一份
common 实现（hermes 桥接也改用它了），所以覆盖是共享的。

这里只验证"确实用了它、没有本地副本"。

"""

from __future__ import annotations

import json
import logging
import sys

import pytest

from conftest import REPO_ROOT, covers

sys.path.insert(0, str(REPO_ROOT))

from examples.openclaw_node import (  # noqa: E402
    build_card,
    build_cmd,
    extract_answer,
    make_runner,
)


class TestRoleCard:
    """角色卡：诚实、确定性、不多给权限。"""

    @covers("M5.F1")
    def test_card_is_deterministic(self):
        """★ 两次构造必须一致 —— 卡片一变，服务端批准自动撤销。"""
        a = build_card("taskflow", "cloud", agent_id="taskflow", tools_note="x")
        b = build_card("taskflow", "cloud", agent_id="taskflow", tools_note="x")
        assert a.to_dict() == b.to_dict()

    @covers("M5.F1")
    def test_only_executor_role(self):
        """只给执行权。``issuer`` 会让任务参数变成外发通道。"""
        card = build_card("taskflow", "cloud", agent_id="taskflow", tools_note="x")
        assert card.roles.executor is True
        # v0.17：开放发布权（A2A 前提），但类型收窄在 agent.ask
        assert card.roles.issuer is True
        assert card.constraints.can_issue_types == ("agent.ask",)

    @covers("M5.F1")
    def test_answer_declared_freetext(self):
        card = build_card("taskflow", "cloud", agent_id="taskflow", tools_note="x")
        cap = card.capability("agent.ask")
        assert "answer" in cap.freetext_fields
        assert "answer" in cap.response_fields

    @covers("M5.F1")
    def test_can_receive_from_is_domain(self):
        """★ 这个参数匹配的是**域**，不是节点 ID。"""
        card = build_card("taskflow", "cloud", agent_id="taskflow", tools_note="x")
        assert card.constraints.can_receive_from == ("cloud",)

    @covers("M5.F7")
    def test_concurrency_is_one(self):
        """OpenClaw agent turn 不适合并发 —— 同一 agent 并行会互相干扰。"""
        card = build_card("taskflow", "cloud", agent_id="taskflow", tools_note="x")
        # ⚠ 原来是 quota.concurrent_tasks == 1（配额已删，Step 5b）
        #   现在并发意图由 capacity 表达 —— 语义从"配额"降级为"排程提示"
        assert card.effective_capacity() == 1

    @covers("M5.F1")
    def test_description_records_tool_surface(self):
        """工具面要写进 description —— 那是给人看的诚实记录。

        **它不是安全控制**（只是文字），但能让运维在观察窗里
        一眼看到"这个节点没做工具收窄"，而不是要翻文档才知道。
        """
        card = build_card("taskflow", "cloud", agent_id="taskflow",
                          tools_note="工具面未收窄")
        assert "未收窄" in card.description


class TestCommand:
    """调用命令必须带 --json。"""

    @covers("M1.F16")
    def test_uses_json_envelope(self):
        """★ ``--json`` 是必须的。

        默认输出是给人看的多段文本。**解析人类可读输出是脆弱的** ——
        OpenClaw 改一次措辞，桥接就悄悄坏掉（而且很可能是"部分解析成功"）。
        """
        cmd = build_cmd("/n", "/cli.js", "taskflow", "问题")
        assert "--json" in cmd

    @covers("M1.F16")
    def test_targets_the_named_agent(self):
        cmd = build_cmd("/n", "/cli.js", "taskflow", "问题")
        assert "--agent" in cmd
        assert cmd[cmd.index("--agent") + 1] == "taskflow", (
            "agent id 必须紧跟 --agent，否则会调到别的 agent 上"
        )

    @covers("M1.F16")
    def test_prompt_is_last(self):
        """prompt 放在最后 —— 中间插参数会被当成 prompt 的一部分。"""
        cmd = build_cmd("/n", "/cli.js", "taskflow", "我的问题")
        assert cmd[-1] == "我的问题"


class TestExtractAnswer:
    """从 JSON 信封里取回答。取不到必须抛，不能返回空串。"""

    @covers("M10.F1")
    def test_prefers_terminal_reply(self):
        """优先 ``result.meta.terminalReply.text`` —— 它是结构化的最终回复。"""
        payload = {
            "status": "ok",
            "result": {
                "meta": {"terminalReply": {"disposition": "visible",
                                           "text": "答案甲"}},
                "payloads": [{"text": "答案乙"}],
            },
        }
        assert extract_answer(payload) == "答案甲"

    @covers("M10.F1")
    def test_falls_back_to_payloads(self):
        """terminalReply 缺失时用 payloads[0].text。"""
        payload = {
            "result": {"meta": {}, "payloads": [{"text": "兜底答案"}]},
        }
        assert extract_answer(payload) == "兜底答案"

    @covers("M10.F1")
    def test_raises_when_no_text(self):
        """★ 取不到就抛，**不要返回空串**。

        空产出比失败更难查：调用方会以为"它答了，只是没内容"。
        """
        with pytest.raises(RuntimeError, match="找不到"):
            extract_answer({"result": {"meta": {}, "payloads": []}})

    @covers("M10.F1")
    def test_raises_on_blank_text(self):
        """只有空白也算没有内容。"""
        with pytest.raises(RuntimeError):
            extract_answer({"result": {"meta": {"terminalReply":
                                                {"text": "   "}}}})

    @covers("M10.F1")
    def test_tolerates_missing_result(self):
        """整个 result 缺失时也要给出明确异常，而不是 KeyError。"""
        with pytest.raises(RuntimeError):
            extract_answer({"status": "error"})


class TestRunner:
    """runner 要把 OpenClaw 的各种失败转成明确异常。"""

    @staticmethod
    def _runner(workdir, script: str):
        """用一个只 print 的 Python 脚本冒充 OpenClaw CLI。

        通过注入 ``cmd_builder`` 实现，**不 monkeypatch 模块函数** ——
        那样写测试会让它看起来在测命令拼装，实际测的是解析。
        """
        fake = workdir / "fake_node.py"
        fake.write_text(script, encoding="utf-8")
        return make_runner(
            lambda prompt: [sys.executable, str(fake)],
            timeout=15, logger=logging.getLogger("test"),
            node_id="taskflow",
        )

    @covers("M10.F1")
    def test_success_path(self, workdir):
        envelope = {
            "status": "ok",
            "result": {"meta": {"terminalReply": {"text": "正常的答案"}}},
        }
        runner = self._runner(
            workdir,
            "import json\n" f"print(json.dumps({envelope!r}))\n",
        )
        assert runner("问题") == "正常的答案"

    @covers("M10.F1")
    def test_non_json_output_fails(self, workdir):
        """输出不是 JSON 时要明确失败 —— 说明 CLI 行为变了。"""
        runner = self._runner(workdir, "print('这不是 JSON')\n")
        with pytest.raises(RuntimeError, match="不是合法 JSON"):
            runner("问题")

    @covers("M10.F1")
    def test_status_error_fails_even_with_exit_zero(self, workdir):
        """★ 退出码 0 但 status=error 也算失败。

        OpenClaw 有些失败是以"正常退出 + status 字段"表达的，
        只看退出码会漏掉。
        """
        envelope = {"status": "error", "summary": "模型调用失败",
                    "result": {"meta": {}}}
        runner = self._runner(
            workdir,
            "import json\n" f"print(json.dumps({envelope!r}))\n",
        )
        with pytest.raises(RuntimeError, match="报告失败"):
            runner("问题")

    @covers("M10.F1")
    def test_nonzero_exit_fails(self, workdir):
        runner = self._runner(
            workdir, "import sys\nprint('boom', file=sys.stderr)\nsys.exit(2)\n")
        with pytest.raises(RuntimeError, match="退出码 2"):
            runner("问题")


class TestAuditToolsJudgement:
    """★ ``--audit-tools`` 的判断逻辑：只看 allow 行，别把 deny 当成风险。

    ⚠ 初版这里写错过：在整段输出里搜 exec，于是**已经 deny 掉 exec 时
    也会报"含 exec"** —— 那会让收窄之后的人以为没收窄成功。
    deny 行里出现 exec 是好事。
    """

    @covers("M14.F8")
    def test_only_allow_line_is_inspected(self):
        """源码必须只取 allow 那一行来判断。"""
        source = (REPO_ROOT / "examples" / "openclaw_node.py").read_text(
            encoding="utf-8")
        assert 'startswith("allow")' in source, (
            "必须只解析 allow 行 —— 在整段输出里搜 exec 会把 deny 当成风险"
        )

    @covers("M14.F8")
    def test_audit_command_exists(self):
        """必须有独立的工具面审计开关。

        工具面决定"发一个任务，这台机器上能发生什么"。
        它不是部署细节，是安全边界 —— 应该能一条命令看到，
        而不是假设或翻文档。
        """
        source = (REPO_ROOT / "examples" / "openclaw_node.py").read_text(
            encoding="utf-8")
        assert "--audit-tools" in source
        assert "sandbox" in source and "explain" in source, (
            "应该用 OpenClaw 自己的 sandbox explain 取**有效值**，"
            "而不是自己读配置拼"
        )


class TestSharedSafetyLogic:
    """安全逻辑必须与 Hermes 桥接共用一份实现。"""

    @covers("M10.F3")
    def test_uses_common_module(self):
        """★ 截断与校验不能在两个桥接里各写一份。

        这个项目刚因为"同一件事两份实现"出过 bug
        （nxs verify 另写了一遍错的域推导）。安全相关的尤其不能有两份。
        """
        source = (REPO_ROOT / "examples" / "openclaw_node.py").read_text(
            encoding="utf-8")
        assert "from examples.agent_bridge_common import" in source
        assert "make_ask_handler" in source

    @covers("M10.F3")
    def test_no_local_truncation_implementation(self):
        """不该有本地副本的截断实现。"""
        source = (REPO_ROOT / "examples" / "openclaw_node.py").read_text(
            encoding="utf-8")
        assert "def truncate_utf8" not in source, (
            "本地又写了一份截断 —— 应该用 agent_bridge_common 的"
        )


class TestActorInjection:
    """★ 桥接必须告诉 Agent「你是谁」（A2A 协作的钥匙）。

    与 Hermes 那边的差别：这里是**附加**一个变量，不整体替换 env ——
    OpenClaw 从 ~/.openclaw 读配置与凭据，清空环境会让它找不到。
    """

    @covers("M14.F3")
    def test_actor_added_without_replacing_env(self):
        import os
        from examples.openclaw_node import _actor_env

        os.environ["SOME_EXISTING_VAR"] = "keep-me"
        try:
            env = _actor_env("taskflow")
            assert env["NXS_ACTOR"] == "taskflow"
            # ★ 原有变量必须还在 —— 否则 OpenClaw 找不到自己的配置
            assert env.get("SOME_EXISTING_VAR") == "keep-me"
        finally:
            os.environ.pop("SOME_EXISTING_VAR", None)

    @covers("M14.F3")
    def test_no_secret_leaked(self):
        from examples.openclaw_node import _actor_env

        env = _actor_env("taskflow")
        leaked = [k for k in env if "NXS_SECRET" in k.upper()]
        assert not leaked, f"密钥不该进 Agent 环境：{leaked}"

    @covers("M14.F3")
    def test_actor_reaches_subprocess(self, workdir):
        import logging
        from examples.openclaw_node import make_runner

        fake = workdir / "echo_actor_oc.py"
        fake.write_text(
            "import json, os\n"
            "print(json.dumps({'status':'ok','result':{'meta':{'terminalReply':"
            "{'text': os.environ.get('NXS_ACTOR','MISSING')}}}}))\n",
            encoding="utf-8",
        )
        runner = make_runner(
            lambda p: [sys.executable, str(fake)],
            timeout=15, logger=logging.getLogger("t"), node_id="taskflow",
        )
        assert runner("问题") == "taskflow"
