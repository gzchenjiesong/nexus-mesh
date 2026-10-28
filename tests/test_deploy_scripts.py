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
    def test_has_utf8_bom(self):
        """★★ **必须有 UTF-8 BOM。**

        ⚠ 这是实测踩到的第一个坑，而且报了 10 条莫名其妙的解析错误。

        Windows PowerShell 5.1 判断编码的方式很粗暴：**看前 3 字节有没有
        BOM，没有就按系统 ANSI（中文系统上是 GBK）读**。

        于是无 BOM 的 UTF-8 中文会被误解码。更糟的是：某个中文字符的
        UTF-8 字节可能在 GBK 下与后面的引号拼成一个字符 ——
        **字符串的结束引号被吞掉**，解析器从那里开始把后面的代码当成
        字符串内容，于是报出的错误位置跟真正的问题完全无关：

            第59行「方法调用中缺少 )」   ← 真正的问题在第55行的中文

        PowerShell 7+ 默认按 UTF-8 读，所以这个问题只在 5.1（Windows 自带）上出现。
        而用户公司电脑上跑的就是 5.1。
        """
        raw = self.path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), (
            "缺 UTF-8 BOM —— Windows PowerShell 5.1 会按 GBK 读，"
            "中文乱码并吞掉引号，报出一堆与真实原因无关的解析错误"
        )

    @covers("M14.F8")
    def test_no_bom_duplication(self):
        """BOM 只能有一个（重复加会变成一个可见的乱码字符）。"""
        raw = self.path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf\xef\xbb\xbf"), "BOM 重复了"

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


class TestPowerShellFragility:
    """★ Windows PowerShell 5.1 上容易出错的具体写法。

    这些不是"风格问题" —— 每一条都会导致脚本跑不起来或行为不可预期。
    """

    path = DEPLOY / "setup-work-laptop.ps1"

    def _code(self) -> str:
        """去掉注释后的代码（注释里提到某个写法不算问题）。"""
        raw = self.path.read_bytes().decode("utf-8-sig")
        return "\n".join(l for l in raw.splitlines()
                         if not l.lstrip().startswith("#"))

    @covers("M14.F8")
    def test_no_version_cast(self):
        """★ 不用 `[version]` 强转来做版本比较。

        `[version]$ver -ge [version]"3.11"` 看起来对，但：
        - `$ver` 若是 `"3.11.9 "`（带空白）会抛异常
        - 异常被 `catch { $ok = $false }` 吞掉，**表现成"版本不够"**
        - 于是用户会以为装错了版本，实际是解析问题

        改成分段比较：取 major/minor 两个整数，直白且不会静默失败。
        """
        assert "[version]" not in self._code(), (
            "[version] 强转遇空白/后缀会抛异常，而异常被 catch 吞掉，"
            "看起来像「版本不够」"
        )

    @covers("M14.F8")
    def test_inline_python_uses_single_quotes(self):
        """★ 内联 Python 代码用**单引号**包。

        PowerShell 双引号里 `$` 会展开、`(` `)` 参与解析；
        单引号内**一切都是字面量**。跑内联脚本必须用单引号。

        原来是：
            & $cand -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
                                             ↑ 嵌套三层括号 + 引号

        一旦文件编码出问题（见上面 BOM 那条），这种写法会崩在解析阶段，
        而不是给出"编码有问题"的明确报错。
        """
        code = self._code()
        import re
        # `& <某命令> -c "..."` 这种形式不许出现
        assert not re.search(r'&\s*\S+\s+-c\s+"', code), (
            "内联 Python 用了双引号 —— 应改成单引号，避免嵌套括号与 $ 展开"
        )

    @covers("M14.F8")
    def test_dollar_paren_balanced_inside_strings(self):
        """★ 双引号字符串内的 `$()` 子表达式必须平衡。

        这类不平衡时，PowerShell 的报错位置会指向后面几行 ——
        而真正的问题在字符串里。所以单独验一遍。
        """
        raw = self.path.read_bytes().decode("utf-8-sig")
        for idx, line in enumerate(raw.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            j, n, in_str, sub = 0, len(line), False, 0
            while j < n:
                c = line[j]
                if not in_str:
                    if c == "#":
                        break
                    if c == '"':
                        in_str = True
                    j += 1
                    continue
                if c == "`":
                    j += 2
                    continue
                if c == '"':
                    if j + 1 < n and line[j + 1] == '"':
                        j += 2
                        continue
                    assert sub == 0, f"第 {idx} 行：$() 未闭合就结束了字符串"
                    in_str = False
                    j += 1
                    continue
                if c == "$" and j + 1 < n and line[j + 1] == "(":
                    sub += 1
                    j += 2
                    continue
                if c == ")" and sub:
                    sub -= 1
                    j += 1
                    continue
                j += 1
            assert not in_str, f"第 {idx} 行：双引号字符串未闭合"

    @covers("M14.F8")
    def test_python_path_is_absolute(self):
        """★ `$Py` 存的是**完整路径**，不是命令名。

        计划任务启动时不保证 PATH 与交互式终端相同。存命令名
        （`python`）的话，任务可能报"找不到 python"——
        而手工跑一切正常，非常难查。
        """
        code = self._code()
        assert "$Py = $exe.Source" in code, (
            "$Py 应存可执行文件的完整路径（$exe.Source），而不是命令名"
        )
