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

#: deploy 目录（本文件里几个测试要检查部署脚本）
DEPLOY = REPO_ROOT / "deploy"

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


class TestCredentialResolution:
    """★★ 凭据读取：三个真实踩过的坑。

    ⚠ 用户报「我已经取了 64 位值，但提示缺节点密钥」——
    背后其实是三个 bug 叠在一起，每个都会造成同一个症状。
    """

    @staticmethod
    def _mk(tmp, lines, *, bom=False, name="work-desktop.env"):
        from pathlib import Path
        p = Path(tmp) / name
        body = "\n".join(lines) + "\n"
        data = body.encode("utf-8")
        if bom:
            data = b"\xef\xbb\xbf" + data
        p.write_bytes(data)
        return p

    @covers("M14.F9")
    def test_bom_file_is_readable(self):
        """★ UTF-8 BOM 的凭据文件必须能读。

        **这是最可能的现场原因。**

        Windows PowerShell 5.1 的 ``Set-Content -Encoding UTF8``
        **会写 BOM**。用纯 ``utf-8`` 读时，BOM 留在第一个键名前面：

            '\\ufeffNXS_NODE_ID' → 而代码找的是 'NXS_NODE_ID'

        于是 ``NXS_NODE_ID`` 取不到，文件"明明存在、内容也对"却读不到。

        修法：``read_text(encoding="utf-8-sig")`` —— 自动剥 BOM，
        对没有 BOM 的文件行为完全一致，是安全的兼容写法。
        """
        import shutil
        import tempfile
        import sys
        from pathlib import Path

        sys.path.insert(0, str(REPO_ROOT))
        from nxs.cli.actor import _parse_env_file

        tmp = Path(tempfile.mkdtemp(prefix="nxs-bom-"))
        try:
            p = self._mk(tmp, [
                "NXS_NODE_ID=work-desktop",
                "NXS_SECRET=" + "x" * 64,
            ], bom=True)
            vals = _parse_env_file(p)
            assert "NXS_NODE_ID" in vals, (
                f"BOM 没剥掉，键名是 {list(vals)} —— "
                f"PowerShell 的 -Encoding UTF8 就会写出这种文件"
            )
            assert vals["NXS_NODE_ID"] == "work-desktop"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @covers("M14.F9")
    def test_resolve_reports_reason_not_just_empty(self):
        """★ 取不到密钥时必须给出**原因**，不能只返回空串。

        `except Exception: return ""` 会把 ``load_actor`` 那几条
        很有信息量的诊断（文件不存在 / 缺键 / 列出目录下现有身份）
        全部吞掉，用户只看到「缺节点密钥」—— 而实际原因可能是
        四种完全不同的问题。
        """
        import shutil
        import tempfile
        from pathlib import Path

        from examples.codebuddy_node import _resolve_secret

        tmp = Path(tempfile.mkdtemp(prefix="nxs-nocred-"))
        old = __import__("os").environ.get("NXS_ACTOR_DIR")
        __import__("os").environ["NXS_ACTOR_DIR"] = str(tmp)
        __import__("os").environ.pop("NXS_SECRET_WORK_DESKTOP", None)
        try:
            secret, reason = _resolve_secret("work-desktop")
            assert not secret
            assert reason, "没给失败原因"
            # 原因里应该提到找不到 / 路径
            assert "work-desktop" in reason or "凭据" in reason
        finally:
            if old is None:
                __import__("os").environ.pop("NXS_ACTOR_DIR", None)
            else:
                __import__("os").environ["NXS_ACTOR_DIR"] = old
            shutil.rmtree(tmp, ignore_errors=True)

    @covers("M14.F9")
    def test_credential_hint_matches_load_actor(self):
        """★ 提示里的路径必须与实际查找逻辑一致。

        ⚠ 这里原来手写了个 ``~/.nexus-mesh/agents``，而 ``load_actor``
        用 ``NXS_ACTOR_DIR or /etc/nexus-mesh/agents``。两者在没设
        环境变量时**指向完全不同的地方** —— 于是提示让人把文件放到 A，
        代码去 B 里找，永远对不上。

        **提示不能另写一份路径逻辑，必须复用实际查找的那套。**
        """
        import re

        from examples.codebuddy_node import _credential_hint

        hint = _credential_hint("work-desktop")
        assert hint.endswith("work-desktop.env"), f"提示路径不对：{hint}"

        # 实际查找逻辑里的默认目录必须出现在提示中
        src = (REPO_ROOT / "examples" / "codebuddy_node.py").read_text(encoding="utf-8")
        m = re.search(r"def _credential_hint.*?return", src, re.S)
        assert m and "DEFAULT_ACTOR_DIR" in m.group(0), (
            "提示没有复用 load_actor 的默认目录 —— 会与真实查找逻辑分叉"
        )


class TestActorDirIsAgentDir:
    """★★ ``NXS_ACTOR_DIR`` 指的是「装 .env 的那个目录」。

    ⚠ 部署脚本初版把它设成了 ``~/.nexus-mesh``（上一级），
    而凭据文件按提示放在 ``~/.nexus-mesh/agents/work-desktop.env``。

    于是 ``load_actor`` 去找：

        ~/.nexus-mesh/work-desktop.env      ← 差一层，永远不存在

    而用户看到的现象是"文件明明在那儿，就是读不到"。

    ## 约定从哪来

    服务器上 ``NXS_ACTOR_DIR`` 未设，代码用默认值
    ``/etc/nexus-mesh/agents``，而凭据就在 ``/etc/nexus-mesh/agents/*.env``。
    也就是说这个变量名里的 "DIR" 指的是**装凭据文件的目录本身**。
    """

    @covers("M14.F9")
    def test_ps1_sets_actor_dir_to_agent_dir(self):
        """ps1 必须把 NXS_ACTOR_DIR 设成 $AgentDir，不是 $ConfDir。"""
        text = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        # 找 envPairs 里那一行
        line = next((l for l in text.splitlines()
                     if '"NXS_ACTOR_DIR"' in l and "=" in l), "")
        assert line, "找不到 NXS_ACTOR_DIR 的设置"
        assert "$AgentDir" in line, (
            f"NXS_ACTOR_DIR 没有指向 $AgentDir：{line.strip()}\n"
            f"  它必须指向装 .env 的目录，不是它的上一级"
        )
        assert "$ConfDir" not in line, (
            f"NXS_ACTOR_DIR 指向了 $ConfDir（上一级）—— 差一层目录：{line.strip()}"
        )

    @covers("M14.F9")
    def test_bash_sets_actor_dir_to_agent_dir(self):
        """bash 版同样。"""
        text = (DEPLOY / "setup-work-node.sh").read_text(encoding="utf-8")
        # 提示里 export 的那行
        hits = [l for l in text.splitlines()
                if "NXS_ACTOR_DIR" in l and "AGENT_DIR" in l]
        assert hits, "bash 版没有把 NXS_ACTOR_DIR 指向 AGENT_DIR"

    @covers("M14.F9")
    def test_conf_dir_not_read_from_actor_dir(self):
        """★ 配置根目录不能从 NXS_ACTOR_DIR 读 —— 那是 agents 目录。

        初版 ``CONF_DIR="${NXS_ACTOR_DIR:-$HOME/.nexus-mesh}"`` 把两者
        混成一个变量。如果用户已经正确设了 NXS_ACTOR_DIR（指向 agents），
        再跑脚本就会得到 ``agents/agents``。
        """
        sh = (DEPLOY / "setup-work-node.sh").read_text(encoding="utf-8")
        assert 'CONF_DIR="${NXS_CONF_DIR' in sh, (
            "CONF_DIR 应该读 NXS_CONF_DIR，而不是 NXS_ACTOR_DIR"
        )

        ps = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        conf_line = next((l for l in ps.splitlines()
                          if l.strip().startswith("$ConfDir") and "=" in l), "")
        assert "NXS_CONF_DIR" in conf_line, (
            f"$ConfDir 应该读 NXS_CONF_DIR：{conf_line.strip()}"
        )


class TestCheckToolsLayered:
    """★ 探活要分层，且**每一层都要能看到细节**。

    ⚠ 用户报的现象：`--dry-run` 输出

        输出不是 JSON：
        不注册节点。先修好 CodeBuddy 再试。

    冒号后面什么都没有 —— 因为初版只看 stdout，而 stdout 是空的。
    stderr 里可能有真正的原因（未登录、参数不认），但被丢掉了。

    而且初版只有"一次 json 探活"这一层，失败时无法判断是
    -p 有问题、权限模式有问题、还是 JSON 格式有问题。
    """

    @staticmethod
    def _fake(tmp, name, body):
        from pathlib import Path
        p = Path(tmp) / name
        p.write_text("#!/bin/bash\n" + body, encoding="utf-8")
        p.chmod(0o755)
        return p

    @staticmethod
    def _mk_tmp():
        import tempfile
        from pathlib import Path
        return Path(tempfile.mkdtemp(prefix="nxs-probe-"))

    @covers("M14.F8")
    def test_silent_success_is_reported_clearly(self):
        """★ returncode 0 但 stdout 空 —— 必须说清"它认为成功但没输出"。

        这正是用户遇到的情况。含糊的"输出不是 JSON"没法定位。
        """
        import logging
        import shutil
        from examples.codebuddy_node import check_tools

        tmp = self._mk_tmp()
        try:
            fake = self._fake(tmp, "silent.sh", (
                'if [ "$1" = "--version" ]; then echo "2.177.1"; exit 0; fi\n'
                'exit 0\n'))
            log = logging.getLogger("t-silent")
            records = []
            h = logging.Handler()
            h.emit = lambda r: records.append(r.getMessage())
            log.addHandler(h)
            log.setLevel(logging.DEBUG)

            rc = check_tools(str(fake), log)
            assert rc == 1
            text = "\n".join(records)
            assert "退出码：0" in text, "没报退出码"
            assert "stdout：0 字节" in text, "没报 stdout 长度"
            assert "没往 stdout 写东西" in text, (
                "没解释「它自己认为成功了」这个关键事实"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @covers("M14.F8")
    def test_stderr_is_surfaced(self):
        """★ stderr 必须显示出来。

        未登录、参数不认这类原因都在 stderr 里。
        初版把它丢了，于是用户只能靠猜。
        """
        import logging
        import shutil
        from examples.codebuddy_node import check_tools

        tmp = self._mk_tmp()
        try:
            fake = self._fake(tmp, "err.sh", (
                'if [ "$1" = "--version" ]; then echo "2.177.1"; exit 0; fi\n'
                'echo "Error: not logged in" >&2\n'
                'exit 1\n'))
            log = logging.getLogger("t-err")
            records = []
            h = logging.Handler()
            h.emit = lambda r: records.append(r.getMessage())
            log.addHandler(h)
            log.setLevel(logging.DEBUG)

            rc = check_tools(str(fake), log)
            assert rc == 1
            text = "\n".join(records)
            assert "not logged in" in text, (
                "stderr 的内容没有出现在输出里 —— 用户无法知道真正原因"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @covers("M14.F8")
    def test_text_mode_tried_before_json(self):
        """★ 必须先试 text 模式，再试 json。

        这样失败时能区分：是 -p 有问题，还是 --output-format json 有问题。
        一步到位只能得到"没输出"，无从定位。
        """
        import logging
        import shutil
        from examples.codebuddy_node import check_tools

        tmp = self._mk_tmp()
        try:
            # text 模式有输出，json 模式沉默 → 应该报"text 能出、json 不出"
            fake = self._fake(tmp, "textonly.sh", (
                'if [ "$1" = "--version" ]; then echo "2.177.1"; exit 0; fi\n'
                'for a in "$@"; do\n'
                '  if [ "$a" = "json" ]; then exit 0; fi\n'
                'done\n'
                'echo "就绪"\n'))
            log = logging.getLogger("t-textonly")
            records = []
            h = logging.Handler()
            h.emit = lambda r: records.append(r.getMessage())
            log.addHandler(h)
            log.setLevel(logging.DEBUG)

            rc = check_tools(str(fake), log)
            assert rc == 1
            text = "\n".join(records)
            assert "text 模式（-p + dontAsk）" in text
            assert "✓" in text, "text 模式应当被标为通过"
            assert "json 模式" in text
            assert "text 模式能出、json 模式不出" in text, (
                "没有把两层结果对照起来说明"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @covers("M14.F8")
    def test_happy_path_extracts_answer(self):
        """正常情况要跑通，并验证回答字段。"""
        import logging
        import shutil
        from examples.codebuddy_node import check_tools

        tmp = self._mk_tmp()
        try:
            fake = self._fake(tmp, "good.sh", (
                'if [ "$1" = "--version" ]; then echo "2.177.1"; exit 0; fi\n'
                'for a in "$@"; do\n'
                '  if [ "$a" = "json" ]; then\n'
                '    echo \'{"result":"就绪","is_error":false}\'; exit 0\n'
                '  fi\n'
                'done\n'
                'echo "就绪"\n'))
            log = logging.getLogger("t-good")
            records = []
            h = logging.Handler()
            h.emit = lambda r: records.append(r.getMessage())
            log.addHandler(h)
            log.setLevel(logging.DEBUG)

            rc = check_tools(str(fake), log)
            assert rc == 0, "\n".join(records)
            assert "可以往下走注册" in "\n".join(records)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRealCodebuddyFindings:
    """★★ 用**真实 codebuddy 2.177.1** 实测出来的两个陷阱。

    ⚠ 这些不是推测 —— 我在 macOS 上装了 codebuddy 2.177.1
    （与用户的版本完全一致），跑出来的现象：

    ==========================  ========  ========  =====================
    命令                          退出码     stdout    stderr
    ==========================  ========  ========  =====================
    -p ... （不加 --port）           1      0 字节    EADDRINUSE:52098
    -p ... --port 0                0      0 字节    Authentication required
    ==========================  ========  ========  =====================

    **两行的 stdout 都是 0 字节。** 而用户报的现象正是
    「输出不是 JSON：」后面一片空白 —— 因为旧代码只打印 stdout。

    所以两个陷阱都会造成同一个症状，而修法完全不同：
      · 端口冲突 → 加 --port 0
      · 未登录   → 交互式登录
    """

    @covers("M14.F8")
    def test_build_cmd_passes_port(self):
        """★ build_cmd 必须显式传 --port。

        不传时 codebuddy 会去抢它记住的"stable port"
        （默认 auto-assign 只对 --serve 生效，交互模式会复用固定端口）。
        而 WorkBuddy / CodeBuddy IDE 是同一引擎的桌面版，也监听那个端口。

        实测：不加 --port 时必现
            Error: listen EADDRINUSE: address already in use 127.0.0.1:52098
        """
        from examples.codebuddy_node import build_cmd

        cmd = build_cmd("/x/cb", "q", allowed_tools=("Read",))
        assert "--port" in cmd, (
            "build_cmd 没传 --port —— 会与 WorkBuddy/CodeBuddy IDE 抢端口"
        )
        assert cmd[cmd.index("--port") + 1] == "0", (
            "--port 应该是 0（自动分配），而不是某个固定值"
        )

    @covers("M14.F8")
    def test_port_is_configurable(self):
        """端口要能显式指定（排查时有用）。"""
        from examples.codebuddy_node import build_cmd

        cmd = build_cmd("/x/cb", "q", allowed_tools=("Read",), port=45789)
        assert cmd[cmd.index("--port") + 1] == "45789"

    @covers("M14.F8")
    def test_explains_eaddrinuse(self):
        """端口冲突要给出明确解释，不只是把 stderr 打出来。"""
        from examples.codebuddy_node import explain_failure

        advice = explain_failure(
            "Error: listen EADDRINUSE: address already in use 127.0.0.1:52098")
        assert advice, "没认出 EADDRINUSE"
        assert "端口" in advice
        # 要提到是谁在抢
        assert "WorkBuddy" in advice or "CodeBuddy IDE" in advice, (
            "没说清是谁在抢端口 —— 用户不知道要关掉什么"
        )

    @covers("M14.F8")
    def test_explains_not_logged_in(self):
        """未登录要给出明确的登录步骤。

        真实原文：
            Authentication required. Please use /login command to sign in
        """
        from examples.codebuddy_node import explain_failure

        advice = explain_failure(
            "Authentication required. Please use /login command to sign in "
            "to your account")
        assert advice, "没认出未登录"
        assert "未登录" in advice
        # ⚠ 原文里虽然写着 "use /login command"，但实测**没有 /login 这个
        #   CLI 子命令** —— 正确做法是交互式跑 `codebuddy`，进 REPL 后
        #   按提示选登录方式。照抄原文的 /login 会让人在 shell 里白试。
        assert "codebuddy" in advice, "没给出可执行的登录办法"
        assert "CODEBUDDY_API_KEY" in advice, "没给 API Key 这条无人值守的路"

    @covers("M14.F8")
    def test_unknown_error_returns_empty(self):
        """不认识的错误不要乱给建议 —— 空串，让原始 stderr 说话。"""
        from examples.codebuddy_node import explain_failure

        assert explain_failure("something completely unknown") == ""

    @covers("M14.F8")
    def test_probe_checks_stdout_not_only_returncode(self):
        """★ 不能只看退出码 —— 未登录时退出码是 0。

        实测：未登录 → 退出码 **0**、stdout 空、stderr 有内容。
        所以 `if rc != 0: 报错` 这种写法会漏掉它，
        直接走到 json.loads("") 失败，报出"输出不是 JSON"。
        """
        import inspect
        from examples.codebuddy_node import _probe_once

        src = inspect.getsource(_probe_once)
        assert "returncode == 0 and bool(out)" in src or \
               ("returncode" in src and "bool(out)" in src), (
            "探活判定只看 returncode，没检查 stdout 是否为空 —— "
            "未登录时 returncode 是 0，会被误判为成功"
        )


class TestIdeAuthIsNotCliAuth:
    """★★ **CodeBuddy IDE 登录了 ≠ CLI 登录了。**

    ⚠ 用户实测反馈：「我的 codebuddy IDE 是有登录的，为啥还会提示没登录」

    我在 macOS 上查清了原因（有真实环境，可复现）：

    ==========  ======================================  ==================
    应用         凭据存放                                  实测证据
    ==========  ======================================  ==================
    IDE          Electron 应用数据目录                    keychain 里有
                 + Safe Storage                         "CodeBuddy CN
                                                        Safe Storage"
    CLI          操作系统凭据库（独立 service 名）           **没有**对应条目
    ==========  ======================================  ==================

    两者是同一引擎的两个**独立应用**，凭据完全隔离。
    所以 IDE 里显示已登录，对 CLI 没有任何帮助。
    """

    @covers("M14.F9")
    def test_detect_auth_token_env(self, monkeypatch):
        """CODEBUDDY_AUTH_TOKEN 是最高优先级（官方文档明确）。"""
        from examples.codebuddy_node import _detect_cli_auth

        monkeypatch.setenv("CODEBUDDY_AUTH_TOKEN", "tok-123")
        r = _detect_cli_auth()
        assert r["ok"] is True
        assert "AUTH_TOKEN" in r["summary"]

    @covers("M14.F9")
    def test_detect_api_key_with_env(self, monkeypatch):
        """API Key + INTERNET_ENVIRONMENT 才算配好。"""
        from examples.codebuddy_node import _detect_cli_auth

        monkeypatch.delenv("CODEBUDDY_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("CODEBUDDY_API_KEY", "key-123")
        monkeypatch.setenv("CODEBUDDY_INTERNET_ENVIRONMENT", "internal")
        r = _detect_cli_auth()
        assert r["ok"] is True
        assert "internal" in r["summary"]

    @covers("M14.F9")
    def test_api_key_without_env_blocks(self, monkeypatch):
        """★ 只设 API Key、缺 INTERNET_ENVIRONMENT —— 必须阻断。

        官方文档明确要求两者同时设。实测确认：只设 Key 会
        ``401 ... (token-type:ApiKey)`` —— 奇怪的是错误里显示
        token 已读取，但仍 401，很容易误判成 "Key 无效"。

        所以这个组合要在跑之前就拦住，别白耗一次调用。
        """
        from examples.codebuddy_node import _detect_cli_auth

        monkeypatch.delenv("CODEBUDDY_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("CODEBUDDY_API_KEY", "key-123")
        monkeypatch.delenv("CODEBUDDY_INTERNET_ENVIRONMENT", raising=False)
        r = _detect_cli_auth()
        assert r["ok"] is False
        assert "INTERNET_ENVIRONMENT" in r["detail"]
        # 要列出各版本该填什么
        for v in ("internal", "ioa", "cloudhosted"):
            assert v in r["detail"], f"没说明 {v} 这个取值"

    @covers("M14.F9")
    def test_no_credential_only_warns_not_blocks(self, monkeypatch):
        """★ 检测不到凭据时**只警告，不能阻断**。

        这个判定有盲区：
          · macOS：不知道 CLI 实际用的 keychain service 名
          · Windows：凭据管理器内容需要 DPAPI，读不到

        如果"检测不到"就阻断，会产生**假阴性**：
        用户明明登录了，脚本却说没登录，还不肯试一次 ——
        那比完全不预检更糟。
        """
        from examples.codebuddy_node import _detect_cli_auth

        for v in ("CODEBUDDY_AUTH_TOKEN", "CODEBUDDY_API_KEY",
                  "CODEBUDDY_INTERNET_ENVIRONMENT"):
            monkeypatch.delenv(v, raising=False)

        r = _detect_cli_auth()
        # 允许 ok=False（检测不到），但 detail 里不能出现"不跑了"这种阻断语气
        # 真正的阻断判断在 check_tools 里，只对 API Key 缺环境变量生效
        assert "detail" in r
        assert "不跑了" not in r["detail"], (
            "预检函数不该自己下阻断结论 —— 那会让调用方无法区分"
            "「确定失败」和「检测不到」"
        )

    @covers("M14.F9")
    def test_keychain_filter_excludes_safe_storage(self):
        """★ Keychain 过滤必须排除 Electron 的 Safe Storage 条目。

        「CodeBuddy CN Safe Storage」是 **IDE** 用来加密本地存储的，
        与 CLI 无关。若把它当成 CLI 凭据，就会误判"已登录" ——
        然后探活得到 401，反而更混乱。
        """
        import inspect
        from examples.codebuddy_node import _keychain_codebuddy_entries

        src = inspect.getsource(_keychain_codebuddy_entries)
        assert "safe storage" in src.lower(), (
            "没排除 Safe Storage 条目 —— 那是 IDE 的，会误判为 CLI 已登录"
        )

    @covers("M14.F9")
    def test_advice_mentions_ide_not_cli(self):
        """未登录的建议里必须点明「IDE ≠ CLI」。

        否则用户会一直困惑："我明明登录了"。
        """
        from examples.codebuddy_node import explain_failure

        advice = explain_failure(
            "Authentication required. Please use /login command to sign in")
        assert "IDE" in advice, "没提到 IDE"
        assert "不代表" in advice or "不是" in advice, (
            "没说清 IDE 登录不等于 CLI 登录"
        )
        # 两条路都要给
        assert "codebuddy" in advice, "没给交互登录的办法"
        assert "CODEBUDDY_API_KEY" in advice, "没给 API Key 的办法"
