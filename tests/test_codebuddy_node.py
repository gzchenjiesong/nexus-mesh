"""CodeBuddy 桥接节点的测试（``examples/codebuddy_node.py``）。

这个节点与云端两个 Agent 的桥接有**本质区别**：它跑在公司内网，
背后是真实的内网系统。所以测试重点不在"能不能调通"，
而在**边界有没有守住**：

* 工具白名单是不是真的收窄了（而不是"没白名单就不限"）
* 摘要上限是不是真的硬限制（而不是靠 prompt 自觉）
* 能力是不是只有需要的那一个（不多开一条往外送数据的路）
"""

from __future__ import annotations

import json
import logging
import sys

import pytest

from conftest import REPO_ROOT, covers

sys.path.insert(0, str(REPO_ROOT))

from examples.agent_bridge_common import (  # noqa: E402
    SUMMARY_LIMIT,
    TRUNCATION_MARK,
)
from examples.codebuddy_node import (  # noqa: E402
    build_card,
    build_cmd,
    extract_answer,
    make_runner,
)


class TestRoleCard:
    """角色卡：诚实、确定性、不多给能力。"""

    @covers("M5.F1")
    def test_is_deterministic(self):
        """★ 两次构造必须一致 —— 卡片一变，服务端批准自动撤销。"""
        a = build_card("work-laptop", "zone-work", allowed_tools=("Read",))
        b = build_card("work-laptop", "zone-work", allowed_tools=("Read",))
        assert a.to_dict() == b.to_dict()

    @covers("M5.F1")
    def test_only_registers_work_query(self):
        """★ 只开一个能力。

        每多注册一个能力，就多一条能把内网数据送出去的路径。
        `work.fetch`（取原文）刻意不注册 —— 要开的话应该单独决定，
        最好做成"需要运维批准"的能力，而不是一开始就敞开。
        """
        card = build_card("work-laptop", "zone-work", allowed_tools=("Read",))
        types = [c.type for c in card.capabilities]
        assert types == ["work.query"]

    @covers("M5.F1")
    def test_can_issue_and_receive(self):
        """两个方向都要通：能被委派、也能主动问。"""
        card = build_card("work-laptop", "zone-work", allowed_tools=("Read",))
        assert card.roles.executor is True
        assert card.roles.issuer is True
        assert card.constraints.can_issue_types == ("agent.ask",)
        assert card.constraints.can_receive_from == ("zone-work",)

    @covers("M5.F7")
    def test_byte_quota_matches_summary_limit(self):
        """★ 字节配额必须按**摘要上限**算，不是按协议上限。

        如果写成 4096（协议上限），配额数字就与实际能送出去的量不一致 ——
        那会让人误以为"配额还够"，而实际每次只能给 1500。
        """
        card = build_card("work-laptop", "zone-work", allowed_tools=("Read",))
        assert card.constraints.quota.bytes_per_response == SUMMARY_LIMIT

    @covers("M5.F1")
    def test_description_records_tool_whitelist(self):
        """工具白名单要写进 description —— 运维在观察窗里能一眼看到。"""
        card = build_card("work-laptop", "zone-work",
                          allowed_tools=("Bash(projectctl:*)", "Read"))
        assert "projectctl" in card.description


class TestCommand:
    """命令拼装：三个关键参数一个都不能少。"""

    @covers("M1.F16")
    def test_uses_json_envelope(self):
        """★ ``--output-format json`` 必须带。

        默认输出是给人看的文本，解析它很脆弱 —— 改一次措辞就坏，
        而且可能是"部分解析成功"（更难看出来的那种坏）。
        """
        cmd = build_cmd("/bin/cb", "问题", allowed_tools=("Read",))
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "json"

    @covers("M1.F16")
    def test_never_skips_permissions(self):
        """★★ **绝对不能用 `--dangerously-skip-permissions`。**

        那个开关会放开几乎全部权限（连 HIGH/CRITICAL 命令都只"可能"拦）。
        我们只需要读文件 + 调几个 CLI —— 用 `--allowedTools` 精确表达就够了。

        **能精确表达时就不要用粗放的开关。** 这条测试守着这个决定。
        """
        cmd = build_cmd("/bin/cb", "问题",
                        allowed_tools=("Bash(projectctl:*)", "Read"))
        for flag in ("--dangerously-skip-permissions", "-y"):
            assert flag not in cmd, f"命令里出现了 {flag} —— 那是全权限放行"

    @covers("M1.F16")
    def test_permission_mode_is_dont_ask(self):
        """★ `dontAsk` 而不是默认。

        默认模式下需要授权的操作会**挂起等确认** —— 而 `-p` 是单次执行，
        没人会去点确认，于是任务卡在那里直到租约过期。
        `dontAsk` 让它被拒（快速失败），而不是挂住。
        """
        cmd = build_cmd("/bin/cb", "问题", allowed_tools=("Read",))
        assert "--permission-mode" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"

    @covers("M1.F16")
    def test_carries_tool_whitelist(self):
        """白名单必须原样进命令，一个都不能丢。"""
        tools = ("Bash(projectctl:*)", "Bash(meetingctl:*)", "Read", "Grep")
        cmd = build_cmd("/bin/cb", "问题", allowed_tools=tools)
        assert "--allowedTools" in cmd
        start = cmd.index("--allowedTools") + 1
        # 白名单是紧随其后的一串
        assert tuple(cmd[start:start + len(tools)]) == tools

    @covers("M1.F16")
    def test_no_whitelist_means_no_flag(self):
        """空白名单不加 `--allowedTools`（但 main 会拒绝启动）。"""
        cmd = build_cmd("/bin/cb", "问题", allowed_tools=())
        assert "--allowedTools" not in cmd

    @covers("M1.F16")
    def test_prompt_follows_p_flag(self):
        """prompt 必须紧跟 ``-p``，否则会被当成别的参数。"""
        cmd = build_cmd("/bin/cb", "我的问题", allowed_tools=("Read",))
        assert cmd[cmd.index("-p") + 1] == "我的问题"


class TestExtractAnswer:
    """从信封里取回答。字段名文档矛盾，所以两条路都试。"""

    @covers("M10.F1")
    def test_reads_result_field(self):
        assert extract_answer({"result": "答案甲"}) == "答案甲"

    @covers("M10.F1")
    def test_falls_back_to_response_field(self):
        """★ 两份官方文档说法不一致：一处 `.result`，一处 `.response`。

        所以两条都试。这不是"兼容性偷懒" —— 是**已知的文档矛盾**，
        而在装好之前没法实测哪个对。
        """
        assert extract_answer({"response": "答案乙"}) == "答案乙"

    @covers("M10.F1")
    def test_error_envelope_raises(self):
        with pytest.raises(RuntimeError, match="报告失败"):
            extract_answer({"is_error": True,
                            "subtype": "error_during_execution"})

    @covers("M10.F1")
    def test_missing_text_raises_with_keys(self):
        """★ 找不到回答时，**把实际键名列出来**。

        文档写错字段名时，这条错误信息能让人一眼定位 ——
        而不是对着一个含糊的 "cannot parse" 猜。
        """
        with pytest.raises(RuntimeError) as ei:
            extract_answer({"foo": 1, "bar": 2})
        msg = str(ei.value)
        assert "foo" in msg and "bar" in msg

    @covers("M10.F1")
    def test_blank_result_falls_through(self):
        """``.result`` 是空串时应当继续找 `.response`。"""
        assert extract_answer({"result": "  ", "response": "兜底"}) == "兜底"


class TestRunner:
    """runner：把 CodeBuddy 的失败转成明确异常。"""

    @staticmethod
    def _runner(workdir, script: str):
        fake = workdir / "fake_cb.py"
        fake.write_text(script, encoding="utf-8")
        return make_runner(
            lambda prompt: [sys.executable, str(fake)],
            timeout=15, logger=logging.getLogger("t"), node_id="work-laptop",
        )

    @covers("M10.F1")
    def test_success(self, workdir):
        runner = self._runner(
            workdir, "import json\nprint(json.dumps({'result': '摘要内容'}))\n")
        assert runner("问题") == "摘要内容"

    @covers("M10.F1")
    def test_non_json_fails(self, workdir):
        runner = self._runner(workdir, "print('纯文本输出')\n")
        with pytest.raises(RuntimeError, match="不是合法 JSON"):
            runner("问题")

    @covers("M10.F1")
    def test_nonzero_exit_fails(self, workdir):
        runner = self._runner(
            workdir, "import sys\nprint('boom', file=sys.stderr)\nsys.exit(2)\n")
        with pytest.raises(RuntimeError, match="退出码 2"):
            runner("问题")

    @covers("M14.F3")
    def test_no_secret_leaked_to_subprocess(self, workdir):
        """★ 子进程环境里不能有 mesh 凭据。

        CodeBuddy 能 exec，读得到自己的环境。密钥传进去就等于
        放进了 LLM 的上下文 —— 那之后它就不再是秘密。
        """
        import os

        marker = {"NXS_SECRET_TESTLEAK": "x" * 64, "NXS_OPS_SECRET": "y" * 64}
        saved = {k: os.environ.get(k) for k in marker}
        os.environ.update(marker)
        try:
            runner = self._runner(
                workdir,
                "import json, os\n"
                "leak = [k for k in os.environ if k.startswith(('NXS_SECRET','NXS_OPS'))]\n"
                "print(json.dumps({'result': ','.join(leak) or 'NONE'}))\n",
            )
            assert runner("问题") == "NONE"
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class TestSummaryLimitEnforced:
    """★★ 「只回传摘要」必须由**代码**执行，不能靠 prompt 自觉。

    这是本节点最重要的边界。光在 prompt 里写"只要摘要"是不够的 ——
    被提示注入的 Agent 照样会回传原文，而它自己认为在"配合"。

    所以上限由桥接进程截断，并在末尾留标记，让调用方知道"还有内容"。
    """

    @staticmethod
    def _handler(workdir, reply: str, *, limit: int):
        fake = workdir / "fake_reply.py"
        fake.write_text(
            "import json\n"
            f"print(json.dumps({{'result': {reply!r}}}))\n",
            encoding="utf-8",
        )
        from examples.agent_bridge_common import make_ask_handler

        runner = make_runner(
            lambda p: [sys.executable, str(fake)],
            timeout=15, logger=logging.getLogger("t"), node_id="work-laptop",
        )
        return make_ask_handler(runner, logger=logging.getLogger("t"),
                                limit=limit)

    @covers("M10.F3")
    def test_long_reply_is_truncated(self, workdir):
        """超长回复被截断，且带标记。"""
        long_text = "这是一段很长的会议纪要原文。" * 200   # 远超 1500 字节
        handler = self._handler(workdir, long_text, limit=SUMMARY_LIMIT)
        out = handler({"question": "查会议纪要"})
        answer = out["answer"]
        assert answer.endswith(TRUNCATION_MARK), "截断后必须留标记"
        assert len(answer.encode("utf-8")) <= SUMMARY_LIMIT

    @covers("M10.F3")
    def test_truncation_mark_not_exceeding_limit(self, workdir):
        """★ 截断后的总长度（含标记）不能超上限。

        off-by-N 很容易算错：标记看着 6 个字符，实际 18 字节。
        所以预算要**减掉标记的真实字节数**。
        """
        from examples.agent_bridge_common import truncate_utf8

        limit = 100
        handler = self._handler(workdir, "字" * 500, limit=limit)
        answer = handler({"question": "问题"})["answer"]
        assert len(answer.encode("utf-8")) <= limit, (
            f"截断后 {len(answer.encode('utf-8'))} 字节 > 上限 {limit}"
        )
        # 顺带确认多字节没被切碎
        assert "\ufffd" not in answer

    @covers("M10.F3")
    def test_short_reply_untouched(self, workdir):
        """短回复原样返回，不该乱加标记。"""
        handler = self._handler(workdir, "简短摘要", limit=SUMMARY_LIMIT)
        assert handler({"question": "问题"})["answer"] == "简短摘要"

    @covers("M10.F3")
    def test_summary_limit_is_smaller_than_protocol_limit(self):
        """★ 摘要上限必须**小于**协议上限。

        如果两者相等，那"只给摘要"就没意义了 —— 协议上限是
        "允许送出去的量"，摘要上限是"业务上愿意送出去的量"。
        后者必须更小，否则这道闸门形同不存在。
        """
        from examples.agent_bridge_common import MAX_ANSWER_BYTES

        assert SUMMARY_LIMIT < MAX_ANSWER_BYTES, (
            f"摘要上限 {SUMMARY_LIMIT} 不小于协议上限 {MAX_ANSWER_BYTES} —— "
            f"那这道闸门就没起作用"
        )


class TestRefusesToStartWithoutWhitelist:
    """★ 没有工具白名单时必须拒绝启动，**不能静默降级成"不限工具"**。

    静默降级是最糟的处理：节点启动了、能收任务了，但实际上
    CodeBuddy 拥有全部工具（含任意 Bash）—— 等于把公司电脑的 shell
    挂到了任务总线上，而运维看日志只会看到"启动成功"。
    """

    @covers("M14.F8")
    def test_main_exits_when_no_whitelist(self, monkeypatch):
        import examples.codebuddy_node as mod

        monkeypatch.setattr(sys, "argv", [
            "codebuddy_node.py", "--url", "https://x/nxs",
            "--dry-run",
        ])
        monkeypatch.delenv("CODEBUDDY_ALLOWED_TOOLS", raising=False)
        monkeypatch.setenv("NXS_SECRET_WORK_LAPTOP", "s" * 64)
        # 不走网络：白名单检查在探活之前
        rc = mod.main()
        assert rc == 2, "没有白名单时应当拒绝启动"

    @covers("M14.F8")
    def test_whitelist_from_env(self, monkeypatch):
        """环境变量方式也要能配（部署时更省事）。

        ⚠ 这里要同时给全**三样**：url / 白名单 / 凭据。
        只给白名单的话，新的"一次列全缺失项"逻辑仍会返回 2 ——
        因为密钥也没有。那是对的：配置不全就该统一报 2，
        好过一条一条试。
        """
        import examples.codebuddy_node as mod

        # 造一份凭据文件（避免依赖真实密钥）
        #
        # ⚠ 不用 pytest 的 tmp_path —— 沙箱的 sitecustomize 拦了 Path.mkdir，
        #   那个夹具会报 FileExistsError。用 tempfile 自己建。
        import shutil
        import tempfile

        from pathlib import Path

        tmp = Path(tempfile.mkdtemp(prefix="nxs-cb-"))
        # ⚠ NXS_ACTOR_DIR 指向**凭据目录本身**，不是父目录。
        #   load_actor 自己不再套一层 agents/。
        actor_dir = tmp
        (actor_dir / "work-desktop.env").write_text(
            "NXS_NODE_ID=work-desktop\n"
            "NXS_COORDINATOR_URL=https://x/nxs\n"
            f"NXS_SECRET={'s' * 64}\n",
            encoding="utf-8")

        monkeypatch.setattr(sys, "argv", [
            "codebuddy_node.py", "--url", "https://x/nxs", "--dry-run",
        ])
        monkeypatch.setenv("CODEBUDDY_ALLOWED_TOOLS", "Read,Grep")
        monkeypatch.setenv("NXS_ACTOR_DIR", str(tmp))
        monkeypatch.delenv("CODEBUDDY_BIN", raising=False)
        try:
            # 三样齐了 → 越过配置检查，往下走到探活。
            # 探活找不到 codebuddy 二进制 → 返回 1（而不是 2 = 配置不全）
            rc = mod.main()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        assert rc == 1, "配置齐全后应当越过配置检查，进入探活阶段"

    @covers("M14.F8")
    def test_lists_all_missing_at_once(self, monkeypatch, caplog):
        """★ 缺配置时要**一次列全**，不要让用户一条一条试。

        用户实际踩过：先报"缺 --url"，设好再跑又报"缺凭据"，
        再设好又报"缺白名单" —— 三次往返。

        而且只说"缺 X"不说"该怎么给"，等于把排查推给用户。
        """
        import examples.codebuddy_node as mod

        monkeypatch.setattr(sys, "argv", ["codebuddy_node.py", "--dry-run"])
        for v in ("NXS_COORDINATOR_URL", "CODEBUDDY_ALLOWED_TOOLS",
                  "NXS_ACTOR_DIR", "NXS_NODE_ID"):
            monkeypatch.delenv(v, raising=False)

        import logging as _logging
        caplog.set_level(_logging.ERROR)

        rc = mod.main()
        assert rc == 2

        # ⚠ logging 默认走 handler 到 stderr，capsys 抓不到 —— 用 caplog
        err = caplog.text
        # 三项都要提到（不能只报第一个）
        assert "NXS_COORDINATOR_URL" in err
        assert "CODEBUDDY_ALLOWED_TOOLS" in err
        assert "NXS_SECRET_WORK_DESKTOP" in err
        # 要给"怎么设"的示例，不只是"缺什么"
        assert "env:" in err or "SetEnvironmentVariable" in err or "$env:" in err
        # 要指向一键写好的办法
        assert "setup-work-node.ps1" in err


class TestWindowsCompat:
    """★ Windows 上的两个必然踩到的坑。

    公司电脑是 Windows，而之前所有代码都是在 macOS/Linux 上验的。
    下面两条不是"理论风险"，是**一定会发生**的行为差异。
    """

    @covers("M14.F8")
    def test_cmd_suffix_wrapped_with_cmd_c(self):
        """★ `.cmd` 必须经 `cmd /c` 启动，否则 WinError 193。

        Python 的 subprocess 在 Windows 上**不能直接执行 .cmd/.bat**。
        而 npm 全局安装的 CLI 在 Windows 上生成的正是 `.cmd`：

            npm i -g @tencent-ai/codebuddy-code
            → %APPDATA%\\npm\\codebuddy.cmd      ← 不是 .exe

        所以只要用户选了 npm 方式，桥接就一条任务都跑不了 ——
        而错误信息是 `WinError 193: %1 is not a valid Win32 application`，
        很难让人联想到"后缀名"这个原因。
        """
        import sys
        from unittest import mock

        from examples.agent_bridge_common import normalize_command

        cmd = ["C:\\Users\\x\\AppData\\Roaming\\npm\\codebuddy.cmd", "-p", "hi"]
        with mock.patch.object(sys, "platform", "win32"):
            out = normalize_command(cmd)
        assert out[:2] == ["cmd", "/c"]
        assert out[2] == cmd[0], "原命令要完整保留在 cmd /c 后面"

    @covers("M14.F8")
    def test_exe_not_wrapped(self):
        """`.exe` 不需要包装 —— 多加一层 cmd /c 会改变参数解析。"""
        import sys
        from unittest import mock

        from examples.agent_bridge_common import normalize_command

        cmd = ["C:\\cb\\codebuddy.exe", "-p", "hi"]
        with mock.patch.object(sys, "platform", "win32"):
            assert normalize_command(cmd) == cmd

    @covers("M14.F8")
    def test_posix_untouched(self):
        """★ 其它平台必须**原样返回**。

        `.cmd` 的处理不能被"顺便"应用到 macOS/Linux ——
        那会给每个命令凭空加一层 shell，改变引号与转义语义。
        """
        import sys
        from unittest import mock

        from examples.agent_bridge_common import normalize_command

        cmd = ["/usr/local/bin/codebuddy", "-p", "hi"]
        for plat in ("darwin", "linux"):
            with mock.patch.object(sys, "platform", plat):
                assert normalize_command(cmd) == cmd

    @covers("M14.F8")
    def test_does_not_use_shell_true(self):
        """★ 不用 `shell=True`，用 `cmd /c`。

        `shell=True` 会把参数交给 shell 解析，而 prompt 里可能有
        引号、`&`、`|` —— 在 shell 下那些会变成命令注入面。

        `cmd /c` + 列表参数保留了"逐个传参"的语义，没有这层风险。
        """
        from examples.agent_bridge_common import run_subprocess
        import inspect

        src = inspect.getsource(run_subprocess)
        assert "shell=True" not in src, (
            "run_subprocess 用了 shell=True —— prompt 里的特殊字符会变成注入面"
        )

    @covers("M14.F8")
    def test_find_codebuddy_covers_platforms(self):
        """★ 找可执行文件要按平台找，不能写死路径。

        写死 `~/.local/bin/codebuddy` 的话，Windows 上永远找不到 ——
        而症状是"没装 codebuddy"，用户会以为真的要重装一遍。
        """
        from examples.codebuddy_node import find_codebuddy
        import inspect

        src = inspect.getsource(find_codebuddy)
        # 三个平台都要有分支
        assert "win32" in src
        assert "LOCALAPPDATA" in src or "AppData" in src
        assert "codebuddy.exe" in src
        assert "codebuddy.cmd" in src
        # PATH 优先
        assert "shutil.which" in src
