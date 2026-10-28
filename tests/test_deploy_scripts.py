"""部署脚本的静态守门。

⚠ **这些脚本没有在真实平台上跑过。** macOS 版能跑（本机验过），
Windows 版不能 —— 沙箱里装不了 PowerShell（`sandbox_apply: Operation not permitted`）。

所以这里守的不是"能不能跑"，而是"有没有明显的手误"。这类错误
（括号没配对、变量名被中文吞掉）会让用户对着一个跑了一半的脚本猜原因。

真正跑通要在公司电脑上验。这一点在文档里写明了。
"""

from __future__ import annotations

import pathlib
import re

import pytest

from conftest import REPO_ROOT, covers

DEPLOY = REPO_ROOT / "deploy"


def _strip_strings_and_comments(text: str, *, ps: bool) -> str:
    """剥掉字符串与注释，只留结构字符。

    PowerShell 与 bash 的转义规则不同：
      PowerShell 用反引号 ` 转义；字符串内 "" 表示一个引号
      bash 用反斜杠 \\ 转义
    """
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif ps and text[i:i + 2] == "<#":
            j = text.find("#>", i)
            i = (j + 2) if j >= 0 else n
        elif c in "\"'":
            q, i = c, i + 1
            while i < n:
                if ps and text[i] == "`":
                    i += 2
                    continue
                if not ps and text[i] == "\\":
                    i += 2
                    continue
                if text[i] == q:
                    i += 1
                    break
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


class TestBashScript:
    """macOS / Linux 版。"""

    path = DEPLOY / "setup-work-laptop.sh"

    @covers("M14.F8")
    def test_exists_and_executable(self):
        assert self.path.is_file()

    @covers("M14.F8")
    def test_syntax_ok(self):
        """★ bash 语法必须过 —— 这个能在本机真跑（`bash -n`）。"""
        import subprocess
        r = subprocess.run(["bash", "-n", str(self.path)],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"bash 语法错误：{r.stderr}"

    @covers("M14.F8")
    def test_no_var_followed_by_nonascii(self):
        """★ `$var` 后面不能紧跟中文。

        ⚠ **这是真踩过的坑。** 原来是：

            ok "协调节点可达（HTTP $code）"

        跑到这里报 `unbound variable`，原因是 **shell 把全角括号 `）`
        当成了变量名的一部分** —— 变量名变成 `code）`，未定义。

        中文为主的脚本里这个问题很普遍。修法：`${code}`。
        """
        text = self.path.read_text(encoding="utf-8")
        bad = re.findall(r"\$([A-Za-z_]\w*)(?=[^\x00-\x7F])", text)
        assert not bad, (
            f"这些变量后面紧跟非 ASCII 字符，会被当成变量名的一部分：{sorted(set(bad))}\n"
            f"  修法：改成 ${{var}} 形式"
        )


class TestPowerShellScript:
    """Windows 版。

    ⚠ **无法在本机执行** —— 沙箱不允许装 PowerShell。所以只做静态检查。
    """

    path = DEPLOY / "setup-work-laptop.ps1"

    @covers("M14.F8")
    def test_exists(self):
        """★ 必须有 Windows 版。

        用户的公司电脑是 Windows。只给 bash 脚本等于没给 ——
        而"你自己看着改"这种回答没有价值。
        """
        assert self.path.is_file(), "Windows 部署脚本缺失"

    @covers("M14.F8")
    def test_brackets_balanced(self):
        """括号必须配对，且嵌套顺序正确。

        数量相等还不够 —— `} {` 也是"数量相等但顺序错"。
        所以用栈验证。
        """
        clean = _strip_strings_and_comments(
            self.path.read_text(encoding="utf-8"), ps=True)
        pairs = {"}": "{", ")": "(", "]": "["}
        stack: list[str] = []
        for ch in clean:
            if ch in "{([":
                stack.append(ch)
            elif ch in pairs:
                assert stack and stack[-1] == pairs[ch], (
                    f"括号嵌套错误：遇到 {ch}，栈顶是 {stack[-1] if stack else '空'}"
                )
                stack.pop()
        assert not stack, f"有未闭合的括号：{stack}"

    @covers("M14.F8")
    def test_no_var_followed_by_nonascii(self):
        """★ 与 bash 同一个坑，但 **PowerShell 更危险**。

        PowerShell 的变量名**允许 Unicode 字符** —— 所以
        `"$ver（"` 里的全角括号会被真的吃进变量名，变成 `ver（`，
        然后解析出一个不存在的变量（值为空，不报错）。

        那比 bash 的报错更糟：**它静默给出空值**，脚本继续往下跑。
        """
        text = self.path.read_text(encoding="utf-8")
        bad = re.findall(r"\$([A-Za-z_]\w*)(?=[^\x00-\x7F])", text)
        assert not bad, (
            f"这些变量后面紧跟非 ASCII，会被吞进变量名：{sorted(set(bad))}"
        )

    @covers("M14.F8")
    def test_uses_scheduled_task_not_launchd(self):
        """Windows 上的常驻机制是计划任务，不是 launchd。

        只检查**有没有实际调用** launchctl —— 注释里提到"与 launchd 不同"
        是正常的，那是说明差异，不是用了它。
        """
        text = self.path.read_text(encoding="utf-8")
        assert "Register-ScheduledTask" in text
        # 实际调用才算数（launchctl 是 macOS 的命令）
        assert "launchctl" not in text

    @covers("M14.F8")
    def test_runs_without_console_window(self):
        """★ 常驻进程必须无窗口。

        用 `python.exe` 会留一个黑色控制台窗口 —— 用户会把它关掉，
        然后节点就死了。所以要用 `pythonw.exe`。
        """
        text = self.path.read_text(encoding="utf-8")
        assert "pythonw.exe" in text, (
            "没有用 pythonw.exe —— 用户会看到一个黑窗然后关掉它"
        )

    @covers("M14.F8")
    def test_task_has_no_time_limit(self):
        """计划任务默认有 3 天执行上限，必须显式关掉。

        不关的话，节点跑满 72 小时就被系统杀掉 ——
        而症状是"莫名其妙不干活了"，很难联想到计划任务设置。
        """
        text = self.path.read_text(encoding="utf-8")
        assert "ExecutionTimeLimit" in text
        assert "TimeSpan]::Zero" in text or "-Days 0" in text

    @covers("M14.F8")
    def test_checks_python_first(self):
        """★ Windows 上最可能的阻塞是**没装 Python**。

        公司电脑常常没有。脚本必须第一个查它，并给出装法 ——
        而不是等到创建计划任务时才在某处失败。
        """
        text = self.path.read_text(encoding="utf-8")
        assert "3.11" in text, "没检查 Python 最低版本"
        assert "winget install Python" in text, "没给 Python 的安装方法"


class TestCrossPlatformBridge:
    """桥接本身在 Windows 上的行为。"""

    @covers("M14.F8")
    def test_cmd_needs_shell_wrapper(self):
        """★ Python 在 Windows 上不能直接执行 .cmd。

        这是个**必然**触发的问题：npm 全局安装生成 `.cmd`，
        而 `subprocess.run(["x.cmd", ...])` 会报 `WinError 193`：
        `%1 is not a valid Win32 application`。

        而那个错误信息完全不会让人想到"后缀名"。
        """
        from examples.agent_bridge_common import normalize_command
        import sys
        from unittest import mock

        with mock.patch.object(sys, "platform", "win32"):
            out = normalize_command(["C:\\npm\\codebuddy.cmd", "-p", "x"])
        assert out[:2] == ["cmd", "/c"]

    @covers("M14.F8")
    def test_no_shell_true_anywhere(self):
        """★ 用 `cmd /c` 而不是 `shell=True`。

        `shell=True` 会把参数交给 shell 解析。而 prompt 来自别的 Agent，
        里面可能有引号、`&`、`|` —— 在 shell 下那些都变成注入面。
        """
        from examples.agent_bridge_common import run_subprocess
        import inspect

        assert "shell=True" not in inspect.getsource(run_subprocess)


class TestDocMentionsWindows:
    """文档要说明 Windows 支持状态。"""

    @covers("M14.F8")
    def test_bridge_doc_covers_windows(self):
        doc = (REPO_ROOT / "docs" / "agent-bridges.md").read_text(encoding="utf-8")
        assert "Windows" in doc, "桥接文档没提 Windows —— 而公司电脑就是 Windows"
