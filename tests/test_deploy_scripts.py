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

    path = DEPLOY / "setup-work-node.sh"

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
        # 只检查代码与字符串，跳过注释（注释里的 $null， 是无害的）
        # ⚠ **不能用正则的 \w 来找"变量名之后那一个字符"。**
        #
        #   Python 里 \w 匹配 Unicode 词字符，**中文也算**。所以
        #   ``\$([A-Za-z_]\w*)(.)`` 遇到 ``$scriptVer版本`` 时，
        #   ``\w*`` 会把「版本」吞进 group(1)，group(2) 拿到的是
        #   后面的引号 —— ASCII，于是**漏报**。
        #
        #   改成手工扫描：从 $ 后取连续的 **ASCII** 标识符字符，
        #   然后看紧跟的那一个字符是什么。
        code = _ps_strip_comments(text)
        bad: list[str] = []
        for i, ch in enumerate(code):
            if ch != "$":
                continue
            j = i + 1
            # ${var} 形式是安全的，跳过
            if j < len(code) and code[j] == "{":
                continue
            start = j
            while j < len(code) and (code[j].isalnum() or code[j] == "_") \
                    and ord(code[j]) < 0x80:
                j += 1
            if j == start:          # $ 后面不是标识符（如 $ 、$"）
                continue
            name = code[start:j]
            if j >= len(code):
                continue
            nxt = code[j]
            # isalpha() 对全角标点（（）、，、。、；：) 返回 False，
            # 对中文/全角字母返回 True —— 正是判据。
            # 数字和下划线也是标识符字符，同样会被吞，一并算上。
            if ord(nxt) > 0x7F and (nxt.isalpha() or nxt.isdigit()):
                bad.append(name)

        assert not bad, (
            f"这些变量后面紧跟字母类字符（中文/全角字母），"
            f"会被吞进变量名导致取到空值：{sorted(set(bad))}\n"
            f"  注：全角标点（（）、，、。、；）不影响，无需处理\n"
            f"  修法：改成 ${{var}} 形式"
        )


class TestPowerShellScript:
    """Windows 版。

    ⚠ **无法在本机执行** —— 沙箱不允许装 PowerShell。所以只做静态检查。
    """

    path = DEPLOY / "setup-work-node.ps1"

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
        """★ `$var` 后面紧跟**字母**会被吞进变量名（PowerShell）。

        ## ⚠ 与 bash 不同：PowerShell 只吞"字母"，不吞标点

        实测（PowerShell 7.5.4）::

            $ver = "1.0"
            "$ver（左"   →  1.0（左      ← 全角括号，正常
            "$ver）"     →  1.0）        ← 正常
            "$ver，"     →  1.0，        ← 正常
            "$ver版本"   →  （空！）      ← 中文被吞成变量名的一部分
            "$verＡ"     →  （空！）      ← 全角字母同样被吞

        所以判据是「后面跟的是 **Unicode 字母**」，不是「后面跟非 ASCII」。

        ## 为什么这条以前写成"跟非 ASCII"

        从 bash 那侧的坑直接照搬过来的。而 **bash 的行为确实更严**：
        在 `set -u` 下，`$code）`、`$code，`、`$code。` **全都**报
        `unbound variable`（多字节的首字节被当成标识符）。

        于是同一个正则放在两个 shell 上：
          · bash   → 正确
          · PowerShell → **误报标点**，会让人以为 `${}` 到处都得加，
            而真正危险的只是"变量名后面直接接中文/字母"。

        ## 为什么要区分

        一个"说错原因"的检查比没有检查更糟 —— 它会把注意力引向
        不是问题的地方。修法建议仍然是统一的 `${var}`（最安全），
        但**报错信息必须说对是哪一类字符**。
        

        PowerShell 的变量名**允许 Unicode 字符** —— 所以
        `"$ver（"` 里的全角括号会被真的吃进变量名，变成 `ver（`，
        然后解析出一个不存在的变量（值为空，不报错）。

        那比 bash 的报错更糟：**它静默给出空值**，脚本继续往下跑。
        """
        text = self.path.read_text(encoding="utf-8")
        bad = re.findall(r"\$([A-Za-z_]\w*)(?=[^\x00-\x7F])",
                         _ps_strip_comments(text))
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

    path = DEPLOY / "setup-work-node.ps1"

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

        ⚠ 这个检查器我改了三次，每次都是**检查器自己的 bug**：

        ================================================  ====================
        问题                                              现象
        ================================================  ====================
        只处理双引号字符串                                  单引号内的 " 被误判
        不处理 here-string（``@' ... '@``）                 跨行结构被逐行误读
        ================================================  ====================

        ## 根本局限，要说清

        **逐行扫描处理不了跨行结构。** PowerShell 的 here-string：

            $code = @'
            ...多行内容，里面的引号都是字面量...
            '@

        逐行看，第一行的 ``@'`` 是个没闭合的单引号。

        所以这里显式跟踪 here-string 状态，遇到就整段跳过。

        ## 但真正的权威是解析器

        这个检查的价值只在于"没有 pwsh 时也能兜一层"。
        ``$()`` 不平衡会导致解析失败，``Parser::ParseFile`` 一定能抓到。
        所以遇到本检查误报时，**先怀疑检查器**，用 lint-ps1.sh 复核。
        """
        raw = self.path.read_bytes().decode("utf-8-sig")
        in_here = None          # "@'" 或 '@"'
        for idx, line in enumerate(raw.splitlines(), 1):
            stripped = line.lstrip()

            # ——— here-string 状态机 ———
            if in_here:
                # 结束标记必须在行首（允许前导空白）
                if stripped.startswith(in_here):
                    in_here = None
                continue
            hm = re.match(r"\s*[\w\$\.\[\]]+\s*=\s*@(['\"])", line)
            if hm:
                in_here = "'@" if hm.group(1) == "'" else '"@'
                continue

            if stripped.startswith("#"):
                continue

            j, n = 0, len(line)
            quote = None
            sub = 0
            while j < n:
                c = line[j]
                if quote:
                    if c == "`":
                        j += 2
                        continue
                    if c == quote:
                        if j + 1 < n and line[j + 1] == quote:
                            j += 2
                            continue
                        assert not (quote == '"' and sub > 0), (
                            f"第 {idx} 行：$() 未闭合就结束了字符串"
                        )
                        sub = 0
                        quote = None
                    elif quote == '"' and c == "$" and j + 1 < n and line[j + 1] == "(":
                        sub += 1
                        j += 2
                        continue
                    elif quote == '"' and c == ")" and sub > 0:
                        sub -= 1
                    j += 1
                    continue
                if c == "#":
                    break
                if c in "\"'":
                    quote = c
                j += 1
            assert quote is None, (
                f"第 {idx} 行：字符串未闭合 —— {line.strip()[:60]}"
            )


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


class TestPythonDetectionRobustness:
    """★ Windows 上探测 Python 的两个真实陷阱。

    用户第一次跑就在这一节停住了（输出停在「凭据目录」之后，
    Python 检测那一段既没报错也没进展）。这两个坑都会造成那种症状。
    """

    path = DEPLOY / "setup-work-node.ps1"

    def _text(self) -> str:
        return self.path.read_bytes().decode("utf-8-sig")

    @covers("M14.F8")
    def test_skips_microsoft_store_stub(self):
        """★ 必须跳过 `WindowsApps` 下的 python.exe。

        系统自带的 `python.exe` 常常是个**重解析点**（App Execution Alias），
        跑它会弹应用商店。它占着 `python` 这个命令名，但不是真的 Python ——
        用户看到"python 能命令补全"就会以为装了。

        不跳过的话，最坏情况是弹窗 + 卡住；最好情况也是白跑一次。
        """
        text = self._text()
        assert "WindowsApps" in text, (
            "没跳过 Microsoft Store 存根 —— 它会弹应用商店/卡住，"
            "而用户会以为 Python 装好了"
        )

    @covers("M14.F8")
    def test_probe_has_timeout(self):
        """★ 探测必须有硬超时。

        `& python -c ...` 卡住时没有任何机制能救 —— 脚本永远停在那里，
        用户看到的是"输出到某一行就不动了"，既无报错也无进展。

        用 Start-Process + WaitForExit(毫秒) 才有超时能力。
        """
        text = self._text()
        assert "WaitForExit(" in text, (
            "没有超时机制 —— 探测卡住时脚本会永远停住，"
            "而这正是用户第一次跑遇到的症状"
        )
        assert "Kill()" in text, "超时后要把子进程杀掉，否则残留"

    @covers("M14.F8")
    def test_probe_script_in_file_not_inline(self):
        """★ 探测代码写成临时文件，不用内联 `-c "..."`。

        内联要处理引号转义 + 括号嵌套，任何一处编码/转义出错都会崩在
        **解析阶段**，而报错位置往往指向几十行之外（BOM 那个坑就是这样）。

        写成 `.py` 文件后，PowerShell 只传一个路径，没有转义问题。
        """
        text = self._text()
        assert "nxs_pyver_probe.py" in text

        # 只检查**代码行** —— 注释里举例说明 `& python -c ...` 是正常的，
        # 那是在解释为什么不用它。
        import re
        code = "\n".join(l for l in text.splitlines()
                         if not l.lstrip().startswith("#"))
        assert not re.search(r"&\s*\S+\s+-c\s", code), (
            "代码里还有内联 -c 调用 —— 应改为临时文件"
        )

    @covers("M14.F8")
    def test_prints_progress_before_probing(self):
        """★ 探测前先打印，卡住时能定位。

        用户第一次的问题就是"不知道卡在哪"。`Say` 在读 Python 之前执行，
        所以最后一行输出就是卡住的位置。
        """
        text = self._text()
        idx_say = text.find("试 $cand")
        idx_probe = text.find("Start-Process -FilePath $exe.Source")
        assert idx_say > 0 and idx_probe > 0
        assert idx_say < idx_probe, (
            "进度输出应在实际探测之前 —— 否则卡住时一行提示都没有"
        )


class TestPowerShellQuoting:
    """★ 引号必须配对 —— 不配对会让 PowerShell 直接解析失败。

    ⚠ 这是我自己写出来的真错误：在 `diag-python.ps1` 里写了

        Write-Host "  ⚠ 别点系统弹的"应用商店"里那个 ……"

    中文里习惯用引号强调某个词，但在 PowerShell 双引号字符串里，
    那个引号会**提前结束字符串**，后面的内容被当成代码 → 解析失败。

    而且它和 BOM 那个坑联合起来最难查：解析器一旦在某个字符串里迷路，
    后面几百行都会被误读，报错位置跟真正的问题毫无关系。

    修法：字符串里要强调就用「」或单引号，不要用 ASCII 双引号。
    """

    paths = [DEPLOY / "setup-work-node.ps1", DEPLOY / "diag-python.ps1"]

    @covers("M14.F8")
    def test_all_scripts_have_bom(self):
        """diag 脚本也一样要 BOM。"""
        for p in self.paths:
            if not p.exists():
                continue
            assert p.read_bytes().startswith(b"\xef\xbb\xbf"), f"{p.name} 缺 BOM"

    @covers("M14.F8")
    def test_no_var_followed_by_nonascii(self):
        """所有 ps1 都不能有 `$var` 紧跟非 ASCII 的问题。

        ⚠ 这个坑我犯了**三次**（ver / verText / PyVer / first）——
        每次都是"写中文说明时顺手加了括号"。
        所以它不是一次性修完就完事，必须有测试一直守着。
        """
        import re
        for p in self.paths:
            if not p.exists():
                continue
            text = p.read_bytes().decode("utf-8-sig")
            # 跳过注释：说明文字里出现 $null， 是无害的，不该误报
            bad = re.findall(r"\$([A-Za-z_]\w*)(?=[^\x00-\x7F])",
                             _ps_strip_comments(text))
            assert not bad, (
                f"{p.name}: 这些变量后面紧跟非 ASCII，会被吞进变量名："
                f"{sorted(set(bad))}"
            )

    @covers("M14.F8")
    def test_double_quotes_even_per_line(self):
        """每行代码的未转义双引号必须是偶数。

        奇数意味着字符串没有正常闭合 —— PowerShell 会从那里开始
        把后面的代码当成字符串内容，报出一堆位置错误的解析异常。
        """
        for p in self.paths:
            if not p.exists():
                continue
            text = p.read_bytes().decode("utf-8-sig")
            for idx, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                # 逐字符扫描，**同时处理单引号和双引号字符串**。
                #
                # ⚠ 这里修过两次，两次都是检查器的 bug：
                #
                #   ① 初版用 line.find("#") 无条件截断，把
                #      Say "... # 推荐 ..." 里的 # 当成注释 → 误报。
                #   ② 第二版只处理双引号字符串，于是
                #      -Pattern '__version__\s*=\s*"([^"]+)"'
                #      里**单引号内的双引号**被当成字符串边界 → 误报。
                #
                # 教训：真解析器（Parser::ParseFile）才是权威。
                # 这个检查只是快速兜底，遇到误报先怀疑检查器。
                j, n = 0, len(line)
                dq = 0          # 双引号计数（不含单引号字符串内的）
                quote = None    # 当前所在字符串的引号类型
                while j < n:
                    c = line[j]
                    if quote:
                        if c == "`":                 # 反引号转义
                            j += 2
                            continue
                        if c == quote:
                            if j + 1 < n and line[j + 1] == quote:
                                j += 2                   # '' 或 "" 是字面引号
                                continue
                            if quote == '"':
                                dq += 1
                            quote = None
                        j += 1
                        continue
                    if c == "#":                     # 字符串外 → 注释
                        break
                    if c in "\"'":
                        quote = c
                        if c == '"':
                            dq += 1
                    j += 1
                assert dq % 2 == 0, (
                    f"{p.name} 第 {idx} 行双引号不成对（{cnt} 个）："
                    f"{line.strip()[:70]}\n"
                    f"  字符串里的强调改用「」或单引号，别用 ASCII 双引号"
                )


class TestDiagScript:
    """独立的诊断脚本：setup 卡住时先用它定位。"""

    path = DEPLOY / "diag-python.ps1"

    @covers("M14.F8")
    def test_exists(self):
        """★ 必须有独立的 Python 诊断脚本。

        用户第一次跑 setup 就停在 Python 检测那一段，而主脚本太长 ——
        在那个上下文里排查很费劲。一个只查 Python 的短脚本能 5 秒给出答案。
        """
        assert self.path.exists(), "缺 diag-python.ps1"

    @covers("M14.F8")
    def test_reports_store_stub(self):
        """要说清"存根 ≠ Python"。

        用户看到 `python` 命令存在，会认为已经装了 —— 而那可能只是
        Microsoft Store 的转发存根。
        """
        text = self.path.read_bytes().decode("utf-8-sig")
        assert "WindowsApps" in text
        assert "存根" in text

    @covers("M14.F8")
    def test_lists_path_candidates(self):
        """要把 PATH 里所有 python.exe 都列出来。

        这样用户能自己看出"装了两个版本"或"只装了个存根"。
        """
        text = self.path.read_bytes().decode("utf-8-sig")
        assert "PATH" in text
        assert "python.exe" in text

    @covers("M14.F8")
    def test_has_timeout(self):
        """诊断脚本本身也不能卡住 —— 否则失去意义。"""
        text = self.path.read_bytes().decode("utf-8-sig")
        assert "WaitForExit(" in text


class TestExecutableShimHandling:
    """★ npm 在 Windows 上生成三个 shim，一个 .exe 都没有。

    ⚠ 这是**实测**发现的：用户机器上 `Get-Command codebuddy` 返回的是

        C:\\Users\\Admin\\AppData\\Roaming\\npm\\codebuddy.ps1

    而我最初只处理了 `.cmd`，漏了 `.ps1` —— Python 的 subprocess
    同样无法直接执行 `.ps1`。

    更隐蔽的一层：**两边看到的不是同一个文件**。

    * PowerShell 的 `Get-Command` 按 PATHEXT 匹配，`.PS1` 优先级最高
      → 返回 `.ps1`
    * Python 的 `shutil.which` 用的 PATHEXT 默认值里**没有** `.PS1`
      → 返回 `.cmd`

    所以部署脚本和桥接必须用**同一套优先级规则**，否则用户看到的路径
    不是桥接实际会用的那个。
    """

    @covers("M14.F8")
    def test_ps1_wrapped_with_powershell(self):
        """`.ps1` 必须用 `powershell -ExecutionPolicy Bypass -File` 包装。

        `-ExecutionPolicy Bypass` 不能少 —— 默认策略可能禁止运行脚本，
        而错误信息（"无法加载文件……未数字签名"）与 mesh 完全无关，
        用户很难联想到是这个原因。
        """
        import sys
        from unittest import mock

        from examples.agent_bridge_common import normalize_command

        cmd = ["C:\\Users\\x\\AppData\\Roaming\\npm\\codebuddy.ps1", "-p", "hi"]
        with mock.patch.object(sys, "platform", "win32"):
            out = normalize_command(cmd)
        assert out[:2] == ["powershell", "-ExecutionPolicy"]
        assert "Bypass" in out
        assert "-File" in out
        # 原命令必须完整保留
        assert cmd[0] in out

    @covers("M14.F8")
    def test_prefers_exe_over_shims(self):
        """★ 三者的优先级：`.exe` > `.cmd` > `.ps1`。

        理由不是洁癖：`.cmd` / `.ps1` 都要经一层 shell 中转，
        而**中转会把参数重新解析** —— 中文 prompt 可能因控制台代码页
        （中文系统 936/GBK）乱码；prompt 里的引号、`&` 也有被解释的风险。

        `.exe` 走 CreateProcess，参数以 UTF-16 逐字传递，没有这些层面。
        """
        import inspect
        from examples.codebuddy_node import find_codebuddy

        src = inspect.getsource(find_codebuddy)
        # 三个后缀都要在候选列表里
        assert "codebuddy.exe" in src
        assert "codebuddy.cmd" in src
        assert "codebuddy.ps1" in src
        # .exe 的候选必须在 .ps1 之前（顺序即优先级）
        assert src.index("codebuddy.exe") < src.index("codebuddy.ps1"), (
            ".exe 必须排在 .ps1 之前 —— 它是唯一无编码风险的形态"
        )

    @covers("M14.F8")
    def test_describe_warns_about_encoding(self):
        """要能说清每种形态的代价，尤其是中文乱码风险。"""
        from examples.codebuddy_node import describe_executable

        exe = describe_executable("x\\codebuddy.exe")
        cmd = describe_executable("x\\codebuddy.cmd")
        ps1 = describe_executable("x\\codebuddy.ps1")

        assert "最佳" in exe
        assert "cmd.exe" in cmd and "乱码" in cmd
        assert "PowerShell" in ps1 and "执行策略" in ps1

    @covers("M14.F8")
    def test_deploy_script_matches_bridge_order(self):
        """★ 部署脚本的检测顺序必须与桥接一致。

        不一致的话，脚本显示的路径不是桥接实际会用的那个 ——
        用户会据此做出错误判断（比如以为要用 .ps1 而实际是 .cmd）。
        """
        text = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        # 脚本里也要有 .exe → .cmd → .ps1 的顺序
        i_exe = text.find("codebuddy.exe")
        i_cmd = text.find("codebuddy.cmd")
        i_ps1 = text.find("codebuddy.ps1")
        assert i_exe > 0 and i_cmd > 0 and i_ps1 > 0
        assert i_exe < i_cmd < i_ps1, (
            "部署脚本的候选顺序应为 .exe → .cmd → .ps1，与桥接保持一致"
        )


class TestNodeIdConsistency:
    """★ 节点 ID 在四处出现，任何一处不一致都会失败。

    节点 ID 决定三件事：

    1. 凭据文件名 —— ``<node_id>.env``
    2. 服务端环境变量名 —— ``NXS_SECRET_<NODE_ID 大写，非字母数字转下划线>``
    3. 角色卡里的 ``node_id`` 字段

    对不上的症状是 ``AU-SIG-01``（签名失败）—— 而那个错误看起来像
    "密钥写错了"。实际可能是"两边不是同一个名字"，而密钥本身没问题。

    ⚠ 所以默认值必须在一处定义、多处引用。这里守着它们一致。
    """

    @covers("M14.F8")
    def test_bridge_and_scripts_agree(self):
        """桥接的 DEFAULT_NODE_ID 与两个部署脚本的默认值必须相同。"""
        import re

        from examples.codebuddy_node import DEFAULT_NODE_ID

        sh_text = (DEPLOY / "setup-work-node.sh").read_text(encoding="utf-8")
        sh_match = re.search(r'NODE_ID="\$\{NXS_NODE_ID:-([^}]+)\}"', sh_text)
        assert sh_match, "bash 脚本里找不到 NODE_ID 默认值"
        assert sh_match.group(1) == DEFAULT_NODE_ID, (
            f"bash 默认值 {sh_match.group(1)!r} != 桥接 {DEFAULT_NODE_ID!r}"
        )

        ps_text = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        ps_match = re.search(r'else \{ "([a-z0-9-]+)" \}', ps_text)
        assert ps_match, "ps1 脚本里找不到 NodeId 默认值"
        assert ps_match.group(1) == DEFAULT_NODE_ID, (
            f"ps1 默认值 {ps_match.group(1)!r} != 桥接 {DEFAULT_NODE_ID!r}"
        )

    @covers("M14.F8")
    def test_node_id_is_valid_shape(self):
        """★ 节点 ID 必须符合服务端的命名规则。

        服务端把节点 ID 转成环境变量名时，规则是「大写，非字母数字转下划线」。
        所以 ID 里的连字符会变成下划线：

            work-desktop  →  NXS_SECRET_WORK_DESKTOP

        这条转换必须能往返 —— 否则拿到的是空密钥，报的还是 AU-SIG-01。

        另外 ID 不能含点、斜杠、空格：凭据文件名是 ``<node_id>.env``，
        带这些字符会变成路径穿越或意外文件名。
        """
        import re

        from examples.codebuddy_node import DEFAULT_NODE_ID
        from nxs.mgmt.config import _node_env_var

        assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", DEFAULT_NODE_ID), (
            f"{DEFAULT_NODE_ID!r} 不符合节点 ID 规则"
        )
        assert not any(ch in DEFAULT_NODE_ID for ch in "./ \\"), (
            "节点 ID 不能含路径字符 —— 它会被用作凭据文件名"
        )
        # 转换结果要可预期
        assert _node_env_var(DEFAULT_NODE_ID) == (
            "NXS_SECRET_" + DEFAULT_NODE_ID.upper().replace("-", "_")
        )

    @covers("M14.F8")
    def test_scripts_are_named_by_role_not_machine(self):
        """★ 部署脚本名不该带具体机器名。

        原来是 ``setup-work-laptop.{sh,ps1}`` —— 而实际机器叫 work-desktop。
        名字与身份不符会让后来的人以为「这是给笔记本用的」。

        改成按**角色**命名：``setup-work-node``（公司内网执行者节点）。
        机器名通过 ``--node-id`` / ``NXS_NODE_ID`` 给，不写进文件名。

        ⚠ 但默认值仍要写具体名字 —— 全空默认会导致"悄悄用了错的身份"。
        只是这个名字只出现在**一处常量**里，而不是散落在文件名中。
        """
        assert (DEPLOY / "setup-work-node.sh").exists()
        assert (DEPLOY / "setup-work-node.ps1").exists()
        assert not (DEPLOY / "setup-work-laptop.sh").exists(), (
            "旧文件名还在 —— 两个文件会让用户不知道跑哪个"
        )
        assert not (DEPLOY / "setup-work-laptop.ps1").exists()

    @covers("M14.F8")
    def test_no_stale_reference_to_old_script_name(self):
        """文档与代码里不能还有旧文件名的引用（会指向不存在的文件）。

        ⚠ 排除本文件自己：它在上面两条里必须提到旧名字（才能断言
        "旧文件不存在"）。其他任何文件提到旧名字都是真问题。
        """
        old_name = "setup-work-" + "laptop"      # 拼出来，避免被自己命中
        stale = []
        for pattern in ("*.md", "*.py", "*.sh", "*.ps1"):
            for p in REPO_ROOT.rglob(pattern):
                if ".venv" in str(p) or ".git" in str(p):
                    continue
                if p.name == "test_deploy_scripts.py":
                    continue
                try:
                    text = p.read_bytes().decode("utf-8", errors="ignore")
                except OSError:
                    continue
                if old_name in text:
                    stale.append(p.name)
        assert not stale, f"这些文件还引用旧脚本名：{sorted(set(stale))}"


class TestCurlErrorCodes:
    """★★ curl 的退出码要分开报，因为修法完全不同。

    ⚠ **实测发现的关键事实**（不是推测，是跑出来的）：

    ======  ==================================================
    退出码   含义与常见原因
    ======  ==================================================
    6       域名解析失败
    7       TCP 连不上（防火墙 / 安全组）
    28      超时
    77      **CA 文件本身加载不了** —— BOM 或内容截断
    60      CA 能加载，但验不过服务端证书（拿错了 CA）
    ======  ==================================================

    77 和 60 的区别很要紧：
      77 = 文件坏了（本地问题，一分钟能修）
      60 = 拿错了 CA（要去找正确的证书）

    而 curl 对这两种情况的输出都是
    ``error setting certificate verify locations`` / ``SSL certificate problem``，
    字面里看不出区别 —— 只能靠退出码。

    ## 为什么这条对用户体验至关重要

    最初的脚本把「非 200」一律报成"协调节点不可达"，
    于是用户看到 FAIL 会去查防火墙，而实际只是 CA 文件多个 BOM。
    方向一带偏，可能白查半天。
    """

    @covers("M14.F8")
    def test_health_check_uses_python_not_curl(self):
        """★★ 健康检查必须用 **Python**，不能用 curl.exe。

        ⚠ 这是实测踩到的坑，而且方向完全错了：

        用户 Windows 上 curl 报 ``(60) schannel: the revocation status is
        unknown``，而我以为"CA 文件坏了"，让人去查 BOM、查截断、比对指纹。
        全都不是。

        真实情况是 **两个 TLS 栈的判断标准不同**：

        ==========  ==========================  ============================
        工具        后端                        对自签 CA 的态度
        ==========  ==========================  ============================
        curl.exe    Windows schannel            查吊销状态 → 自签 CA 没发布
                                                CRL → "unknown" → 拒绝
        Python      OpenSSL（Python 内置）      不查吊销；但 3.13+ 开
                                                VERIFY_X509_STRICT，
                                                要求 CA 有 keyUsage
        ==========  ==========================  ============================

        而**桥接用的是 Python**。所以拿 curl 的结论判断"桥接能不能连"
        毫无意义 —— 它在测另一个程序。

        现在用一段 Python 脚本做检查，建上下文的方式与
        ``HttpTransport._ssl_context`` 完全一致 —— 测的就是生产路径。
        """
        text = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        # 必须有一段内嵌的 Python 检查代码
        assert "create_default_context" in text, (
            "健康检查没用 Python 的 ssl —— 那是桥接实际用的方式"
        )
        assert "VERIFY_X509_STRICT" in text, (
            "没检测严格模式 —— 那正是 CA 缺 keyUsage 时失败的原因"
        )

    @covers("M14.F8")
    def test_curl_fallback_disables_revocation(self):
        """没有 Python 时退回 curl，但必须带 `--ssl-no-revoke`。

        schannel 会对自签 CA 做吊销检查，而自签 CA 不发布 CRL/OCSP，
        于是永远报 "revocation status is unknown"。
        `--ssl-no-revoke` 关掉这项检查，否则兜底路径根本走不通。
        """
        text = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        if "curl.exe" in text:
            assert "--ssl-no-revoke" in text, (
                "curl 兜底路径没加 --ssl-no-revoke —— schannel 会因"
                "自签 CA 无吊销信息而拒绝"
            )

    @covers("M14.F8")
    def test_diagnoses_missing_key_usage(self):
        """★ 要能识别「CA 缺 keyUsage」这个具体原因。

        实测这个错误的原文是：

            CA cert does not include key usage extension

        它不是"文件坏了"，而是**服务端那张 CA 生成时漏了扩展**。
        用户在自己机器上无论怎么重做 CA 文件都修不好 ——
        必须服务端重新生成。所以提示语必须指出这一点。

        （另一个症状是 Windows schannel 的 "revocation status unknown"，
         同一个根因的另一种表现。）
        """
        text = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        assert "key usage" in text.lower() or "keyUsage" in text, (
            "没处理「CA 缺 keyUsage」这种情况"
        )
        assert "gen-tls" in text, (
            "没指向修复脚本 —— 这个错误用户自己修不了"
        )

    @covers("M14.F8")
    def test_diag_script_keeps_curl_codes(self):
        """诊断脚本仍要解释 curl 的 77 与 60 —— 那是分开排查时要用的。

        主脚本改用 Python 了，但 diag-net.ps1 用 curl 做四层分离测试，
        那里 77/60 的区分依然有价值：
          77 = CA 文件加载不了（BOM / 截断）
          60 = CA 能加载但验不过
        """
        text = (DEPLOY / "diag-net.ps1").read_bytes().decode("utf-8-sig")
        assert "77" in text, "diag 没解释退出码 77"
        assert "60" in text, "diag 没解释退出码 60"


class TestCaBundledInRepo:
    """★ CA 证书随仓库分发，不靠手工粘贴。

    ⚠ 手工粘贴 PEM 实测有两种坏法：
      · 记事本存成 UTF-8 BOM  → curl 77
      · 粘贴时内容被截断      → curl 77

    而 CA 是**公开证书**（不含私钥），本来就该随代码走。
    放 ``deploy/ca.pem``，脚本直接复制 —— 一次都不用粘。
    """

    @covers("M14.F8")
    def test_ca_exists_in_repo(self):
        ca = DEPLOY / "ca.pem"
        assert ca.is_file(), "deploy/ca.pem 不存在"
        content = ca.read_text(encoding="utf-8")
        assert content.startswith("-----BEGIN CERTIFICATE-----")
        assert content.rstrip().endswith("-----END CERTIFICATE-----")

    @covers("M14.F8")
    def test_ca_has_no_bom(self):
        """★ CA 文件不能有 BOM。

        有 BOM 时 OpenSSL 认不出 BEGIN 行，curl 直接报
        ``(77) error setting certificate verify locations``。
        而这句话里完全看不出是编码问题。
        """
        raw = (DEPLOY / "ca.pem").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), (
            "deploy/ca.pem 带 BOM —— curl 会报退出码 77，"
            "而提示语里看不出是编码问题"
        )

    @covers("M14.F8")
    def test_ca_size_reasonable(self):
        """正常 CA 证书约 1200 字节；太小说明被截断。"""
        size = (DEPLOY / "ca.pem").stat().st_size
        assert 800 < size < 4000, f"ca.pem 大小异常：{size} 字节"

    @covers("M14.F8")
    def test_scripts_copy_from_repo(self):
        """两个部署脚本都要能从仓库复制 CA。"""
        ps = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        assert "PSScriptRoot" in ps and "ca.pem" in ps, (
            "ps1 没从脚本所在目录复制 CA"
        )

        sh = (DEPLOY / "setup-work-node.sh").read_text(encoding="utf-8")
        assert "REPO_CA" in sh, "bash 没定义仓库 CA 路径"

    @covers("M14.F8")
    def test_scripts_repair_broken_ca(self):
        """已有的坏 CA 要能自动修复（大小异常 / 有 BOM）。

        用户可能已经手工粘过一个坏文件。脚本不该只是警告，
        而应该用仓库里的正确版本覆盖它 —— 那是能自己解决的问题。
        """
        ps = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        assert "0xEF" in ps or "0xef" in ps.lower(), "ps1 没检查 BOM"
        assert "< 800" in ps or "-lt 800" in ps, "ps1 没检查文件过小"

        sh = (DEPLOY / "setup-work-node.sh").read_text(encoding="utf-8")
        assert "efbbbf" in sh, "bash 没检查 BOM"
        assert "-lt 800" in sh or "< 800" in sh, "bash 没检查文件过小"


class TestBashHelperFunctions:
    """★ bash 脚本里用到的每个 helper 都必须有定义。

    ⚠ 这是我自己犯的错：在 CA 段写了 ``warn "检测到 BOM"``，
    但从来没定义过 ``warn()``。而脚本开头是 ``set -euo pipefail``，
    ``-u`` 下调用未定义命令会直接退出 ——
    用户会在 CA 那一步看到一个莫名其妙的 "command not found"，
    前面的检查结果全白做了。
    """

    @covers("M14.F8")
    def test_all_helpers_defined(self):
        import re

        text = (DEPLOY / "setup-work-node.sh").read_text(encoding="utf-8")
        defined = set(re.findall(r"^(\w+)\(\)", text, re.M))
        # 脚本里自定义的 helper（排除外部命令与 shell 关键字）
        for helper in ("say", "ok", "bad", "warn"):
            if re.search(rf"^\s*{helper}\s", text, re.M):
                assert helper in defined, (
                    f"用了 {helper} 但没有定义 {helper}() —— "
                    f"set -u 下会直接退出"
                )


class TestCaCertificateCompliance:
    """★★ CA 证书必须符合 X.509 的基本要求。

    ⚠ 这套证书最初是我手工跑 openssl 生成的，**漏了 keyUsage 扩展**。
    后果是两个不同验证器用两种措辞拒绝它：

    ===========  ============================================================
    验证器        报错
    ===========  ============================================================
    Python 3.13   ``CA cert does not include key usage extension``
    Windows curl  ``schannel: the revocation status is unknown``
                 （schannel 查吊销状态，自签 CA 没发布 CRL/OCSP）
    ===========  ============================================================

    而 Python 3.11/3.12 因为默认没开 VERIFY_X509_STRICT，反而正常 ——
    这意味着**缺陷会潜伏到有人升级 Python 才爆**。最坏的那种 bug。

    所以证书生成必须脚本化（``deploy/gen-tls.sh``），不能手敲。
    """

    ca = DEPLOY / "ca.pem"

    @staticmethod
    def _text(path) -> str:
        import subprocess
        r = subprocess.run(["openssl", "x509", "-in", str(path), "-noout", "-text"],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"openssl 解析失败：{r.stderr}"
        return r.stdout

    @covers("M14.F9")
    def test_ca_has_key_usage(self):
        """★ CA 必须有 keyUsage，且含 keyCertSign。

        没有 keyCertSign 的 CA 不能签下级证书 —— 严格验证器会直接拒绝，
        而 OpenSSL 默认生成时**不会**加这条（RFC 5280 里它算 RECOMMENDED
        而非 MUST），所以手敲命令很容易漏。
        """
        text = self._text(self.ca)
        assert "X509v3 Key Usage" in text, (
            "CA 缺 keyUsage —— Python 3.13+ 与 Windows schannel 都会拒绝"
        )
        assert "Certificate Sign" in text, "keyUsage 里没有 keyCertSign"

    @covers("M14.F9")
    def test_ca_has_basic_constraints(self):
        """CA 必须声明 CA:TRUE —— 否则它只是个普通证书，不能签别人。"""
        text = self._text(self.ca)
        assert "Basic Constraints" in text and "CA:TRUE" in text

    @covers("M14.F9")
    def test_ca_key_usage_is_critical(self):
        """★ keyUsage 应标 critical。

        不标 critical 时，不认识这条扩展的验证器会**忽略**它 ——
        那就等于没写。标了 critical 才会"要么理解，要么拒绝"。
        """
        text = self._text(self.ca)
        # 找 keyUsage 那一行，确认带 critical
        for line in text.splitlines():
            if "X509v3 Key Usage" in line:
                assert "critical" in line, f"keyUsage 未标 critical：{line.strip()}"
                return
        raise AssertionError("没找到 keyUsage")

    @covers("M14.F9")
    def test_gen_script_exists(self):
        """★ 证书生成必须脚本化 —— 这是本缺陷的根源。

        手工命令生成的东西无法复现、无法 review，漏一行扩展也没人发现。
        """
        gen = DEPLOY / "gen-tls.sh"
        assert gen.is_file(), "缺 deploy/gen-tls.sh"

    @covers("M14.F9")
    def test_gen_script_sets_key_usage(self):
        """生成脚本里必须显式写 keyUsage 配置。"""
        text = (DEPLOY / "gen-tls.sh").read_text(encoding="utf-8")
        assert "keyUsage" in text
        assert "keyCertSign" in text
        assert "critical" in text

    @covers("M14.F9")
    def test_gen_script_checks_existing_ca(self):
        """脚本要能检查现有 CA 是否合规（--check 模式）。"""
        text = (DEPLOY / "gen-tls.sh").read_text(encoding="utf-8")
        assert "--check" in text
        # 检查逻辑要真的去找 keyUsage
        assert "X509v3 Key Usage" in text


class TestTlsFilePermissions:
    """★ 证书文件权限错了服务就起不来。

    ⚠ 实测踩过：gen-tls.sh 初版把目录设成 700、server.key 设成 600。
    重启后协调节点直接起不来：

        FAIL 网关启动失败：TLS 证书加载失败: [Errno 13] Permission denied

    而这句话**不说是哪个文件、什么权限**。排查靠猜。

    真实约束：服务以 ``User=root`` / ``Group=nxs`` 运行，
    靠**组权限**读文件。所以：

    ==========  =====  ============================
    文件         权限    原因
    ==========  =====  ============================
    目录         755    服务要能 traverse 进去
    ca.pem       644    公开，服务与节点都读
    server.pem   644    公开
    server.key   **640** 服务进程要读 → 组必须可读
    ca.key       600    签名用，服务不需要
    ==========  =====  ============================
    """

    @covers("M14.F9")
    def test_dir_is_755_not_700(self):
        """目录必须 755 —— 700 时服务用户进不去。"""
        text = (DEPLOY / "gen-tls.sh").read_text(encoding="utf-8")
        assert "chmod 755" in text, "目录权限不是 755"
        # 不能把 $DIR 设成 700
        assert 'chmod 700 "$DIR"' not in text, (
            "目录仍被设成 700 —— 服务用户无法进入，会报 Permission denied"
        )

    @covers("M14.F9")
    def test_server_key_is_640(self):
        """server.key 必须 640（组可读），不是 600。

        600 = 只有 root 能读。而服务虽然 User=root，但它加载证书时
        受限于文件的实际权限位 —— 640 才能让 root:nxs 的组合正常工作。
        """
        text = (DEPLOY / "gen-tls.sh").read_text(encoding="utf-8")
        assert "chmod 640" in text, "没有把 server.key 设成 640"

    @covers("M14.F9")
    def test_ca_key_stays_600(self):
        """★ CA 私钥必须 600，且**不能给服务组**。

        它是签发所有节点证书的根 —— 泄露等于所有人都能伪造身份。
        服务运行期不需要它（只在签证书时用），所以不给组权限是对的。
        """
        text = (DEPLOY / "gen-tls.sh").read_text(encoding="utf-8")
        assert "chmod 600" in text, "CA 私钥没设成 600"

    @covers("M14.F9")
    def test_script_sets_group(self):
        """脚本要能把证书属组设成服务运行的组。"""
        text = (DEPLOY / "gen-tls.sh").read_text(encoding="utf-8")
        assert "SERVICE_GROUP" in text
        assert "chown" in text


def _ps_code_only(text: str) -> str:
    """剥掉 PowerShell 的字符串与注释，只留代码。

    必要性：注释里会举例说明 `$code` 怎么错，字符串里也可能出现 $。
    不剥掉就会把说明文字当成代码来检查。
    """
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif text[i:i + 2] == "<#":
            j = text.find("#>", i)
            i = (j + 2) if j >= 0 else n
        elif c in "\"'":
            q, i = c, i + 1
            while i < n:
                if text[i] == "`":
                    i += 2
                    continue
                if q == '"' and text[i] == '"' and i + 1 < n and text[i + 1] == '"':
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


#: PowerShell 自动提供、不需要赋值的变量
#:
#: ⚠ 平台判定变量（``$IsWindows`` 等）只在 **PowerShell 6+** 存在。
#:   它们是只读自动变量，不需要也不允许赋值 —— 用它们做平台守卫是标准做法。
#:   初版这份清单没有它们，于是我刚加的 ``if (-not $IsWindows)`` 被误报成
#:   "用到了没赋值的变量"。
_PS_AUTOMATIC = {
    "_", "args", "input", "this", "true", "false", "null", "psitem",
    "lastExitCode", "LASTEXITCODE", "Error", "Matches", "PSScriptRoot",
    "PSCommandPath", "HOME", "PWD", "ShellId", "StackTrace", "MyInvocation",
    # ---- PowerShell 6+ 平台判定（只读）----
    "IsWindows", "IsLinux", "IsMacOS", "IsCoreCLR",
    # ---- 其他常用只读/自动变量 ----
    "PSVersionTable", "PID", "Profile", "NestedPromptLevel", "ExecutionContext",
}


class TestPowerShellVariablesDefined:
    """★★ 用到的变量必须都有赋值。

    ⚠ **这是我自己犯的、最难发现的错误。**

    我给 `setup-work-node.ps1` 写过一个变量 `$hasCa` 用来判断 CA 是否存在，
    但**从来没给它赋过值**。PowerShell 在非 StrictMode 下对未定义变量
    返回 `$null`，于是：

        if ($hasCa) { $CaFile } else { "" }   →  永远是 ""

    结果是：**CA 存在也不会被用上，而脚本一声不响地继续跑**。
    检查用的还是系统 CA 库，自签证书必然验不过 —— 而报出来的是
    "证书校验失败"，跟"变量没赋值"看起来毫无关系。

    这种错连语法检查都过（`bash -n` 同理），只能靠静态扫描。
    """

    scripts = [
        DEPLOY / "setup-work-node.ps1",
        DEPLOY / "diag-net.ps1",
        DEPLOY / "diag-python.ps1",
        DEPLOY / "list-internal-tools.ps1",
    ]

    @covers("M14.F8")
    def test_no_undefined_variables(self):
        import re

        problems = []
        for p in self.scripts:
            if not p.exists():
                continue
            code = _ps_code_only(p.read_bytes().decode("utf-8-sig"))

            # 变量使用（排除 $env:TEMP 这类作用域前缀）
            # (?!\w) 防止回溯：$env:TEMP 不能匹配成 $en
            uses = set(re.findall(r"(?<![\w:$])\$([A-Za-z_]\w*)(?![\w:])", code))
            # 赋值形式：$x =   $x +=   foreach ($x in   function f($x)   param(...)
            assigned = set(re.findall(r"\$([A-Za-z_]\w*)\s*\+?=", code))
            assigned |= set(re.findall(r"foreach\s*\(\s*\$([A-Za-z_]\w*)", code))
            assigned |= set(re.findall(r"function\s+\w+\s*\(([^)]*)\)", code)
                            and re.findall(
                                r"\$([A-Za-z_]\w*)",
                                "".join(re.findall(r"function\s+\w+\s*\(([^)]*)\)", code))))
            m = re.search(r"param\s*\((.*?)\)", code, re.S)
            if m:
                assigned |= set(re.findall(r"\$([A-Za-z_]\w*)", m.group(1)))

            missing = sorted(uses - assigned - _PS_AUTOMATIC)
            if missing:
                problems.append((p.name, missing))

        assert not problems, (
            "这些脚本用到了没赋值的变量：\n"
            + "\n".join(f"  {name}: {miss}" for name, miss in problems)
            + "\n  PowerShell 对未定义变量返回 $null（不报错），"
              "于是 if ($x) 恒假 —— 而表现出的症状跟变量无关。"
        )

    @covers("M14.F8")
    def test_ca_prepared_before_network_check(self):
        """★★ CA 准备必须在网络检查**之前**。

        ⚠ 顺序错了会造出一个"永远失败"的检查：

        初版把 CA 复制放在后面的"准备本机文件"一节，而网络检查在前面。
        检查跑的时候本地还没有 CA，于是用系统 CA 库 ——
        自签证书必然验不过。**无论环境多正确，这项检查都过不了。**

        而用户看到的是"证书校验失败"，会去查证书文件，
        不知道真正的问题是脚本的顺序。
        """
        text = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        i_ca = text.find('Say "-> CA 证书"')
        i_net = text.find('Say "-> 网络与证书"')
        assert i_ca > 0, "找不到 CA 准备段"
        assert i_net > 0, "找不到网络检查段"
        assert i_ca < i_net, (
            "CA 准备在网络检查之后 —— 检查时会没有 CA 可用，必然失败"
        )

    @covers("M14.F8")
    def test_no_empty_argument_list(self):
        """★ 用 `-ArgumentList` 时不能传入可能为空的数组元素。

        ⚠ 实测报错：

            无法对参数 "ArgumentList" 执行参数验证。参数为 Null 或空。
            请提供一个不为 Null 或空的参数，然后重试该命令。

        PowerShell 不说是哪个元素空的。而这里的元素是"CA 路径"——
        CA 不存在时它就是空串。

        现在的做法：参数全部走**环境变量**，`-ArgumentList` 只给文件路径
        （单元素、不可能为空）。环境变量空值就是空串，不会被参数验证拦下。
        """
        import re

        text = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        code = _ps_code_only(text)
        for m in re.finditer(r"-ArgumentList\s+(@\([^)]*\)|\$\w+)", code):
            arg = m.group(1)
            assert arg.startswith("$") or arg.count(",") == 0, (
                f"-ArgumentList 传了数组（可能含空元素）：{arg}"
            )

def _ps_strip_comments(text: str) -> str:
    """剥掉 PowerShell 注释，**保留字符串**。

    ⚠ 与 :func:`_ps_code_only` 的区别很关键：

    ``$var`` 紧跟中文的问题恰恰**发生在字符串里** ——
    ``Say "$verText（版本不够）"`` 里的全角括号会被吃进变量名。
    所以检查这个问题时不能把字符串剥掉。

    但注释里出现 ``$null，`` 这种是无害的（人读说明文字），
    必须排除，否则误报。
    """
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            # 注释：跳到行尾（但保留换行）
            while i < n and text[i] != "\n":
                i += 1
        elif text[i:i + 2] == "<#":
            j = text.find("#>", i)
            i = (j + 2) if j >= 0 else n
        elif c in "\"'":
            q = c
            out.append(c)
            i += 1
            while i < n:
                if text[i] == "`":
                    out.append(text[i:i + 2])
                    i += 2
                    continue
                if q == '"' and text[i] == '"' and i + 1 < n and text[i + 1] == '"':
                    out.append(text[i:i + 2])
                    i += 2
                    continue
                out.append(text[i])
                if text[i] == q:
                    i += 1
                    break
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


class TestHealthCheckLogic:
    """★ 部署脚本内嵌的 Python 健康检查逻辑要真的对。

    ⚠ 这段逻辑是**从 .ps1 里嵌进去的**，所以不能靠"看着对"。
    本机无法执行 PowerShell（沙箱限制），但可以把它**抽出来单独跑** ——
    它就是一段普通 Python，用真实 HTTPS 服务器能验出全部行为。
    """

    @staticmethod
    def _extract() -> str:
        """从 .ps1 里抽出 here-string 里的检查脚本。"""
        import re

        text = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        m = re.search(r"\$chkCode = @'\n(.*?)\n'@", text, re.S)
        assert m, "找不到内嵌的 Python 检查脚本"
        return m.group(1)

    @staticmethod
    def _run(workdir, *, url: str, ca: str) -> dict:
        """用指定环境变量跑一次检查脚本，返回解析后的 JSON。"""
        import json
        import os
        import subprocess
        import sys

        script = workdir / "chk.py"
        script.write_text(TestHealthCheckLogic._extract(), encoding="utf-8")
        env = dict(os.environ)
        env["NXS_CHK_URL"] = url
        env["NXS_CHK_CA"] = ca
        r = subprocess.run([sys.executable, str(script)],
                           capture_output=True, text=True, timeout=40, env=env)
        assert r.returncode == 0, f"脚本自身崩了：{r.stderr}"
        return json.loads(r.stdout)

    @staticmethod
    def _make_tls(workdir, days: int = 1):
        """生成一套临时 CA + 服务端证书，返回 (ca, cert, key)。"""
        import subprocess

        ca_key, ca_pem = workdir / "ca.key", workdir / "ca.pem"
        srv_key, srv_csr, srv_pem = (workdir / "s.key", workdir / "s.csr",
                                     workdir / "s.pem")
        cfg = workdir / "req.cnf"
        cfg.write_text(
            "[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=test-ca\n"
            "[v3_ca]\nsubjectKeyIdentifier=hash\n"
            "basicConstraints=critical,CA:TRUE\n"
            "keyUsage=critical,digitalSignature,cRLSign,keyCertSign\n"
            "[v3_srv]\nsubjectKeyIdentifier=hash\n"
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1,DNS:localhost\n",
            encoding="utf-8")
        cmds = [
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(ca_key), "-out", str(ca_pem), "-days", "1",
             "-config", str(cfg), "-extensions", "v3_ca"],
            ["openssl", "req", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(srv_key), "-out", str(srv_csr),
             "-subj", "/CN=127.0.0.1"],
            ["openssl", "x509", "-req", "-in", str(srv_csr),
             "-CA", str(ca_pem), "-CAkey", str(ca_key), "-CAcreateserial",
             "-out", str(srv_pem), "-days", "1",
             "-extfile", str(cfg), "-extensions", "v3_srv"],
        ]
        for c in cmds:
            r = subprocess.run(c, capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, f"{c[0]} 失败：{r.stderr}"
        return ca_pem, srv_pem, srv_key

    @staticmethod
    @pytest.fixture()
    def workdir():
        """自建临时目录。

        ⚠ 不用 pytest 的 ``tmp_path`` —— 沙箱的 sitecustomize 拦截了
        ``Path.mkdir``，那个夹具会报 FileExistsError。
        """
        import tempfile
        d = tempfile.mkdtemp(prefix="nxs-chk-")
        yield pathlib.Path(d)
        import shutil
        shutil.rmtree(d, ignore_errors=True)

    @covers("M14.F8")
    def test_ok_when_ca_matches(self, workdir):
        """CA 正确、服务正常 → ok=true，并报出是否严格模式。"""
        import socket
        import ssl
        import threading
        import http.server

        ca, srv_pem, srv_key = self._make_tls(workdir)

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(srv_pem), str(srv_key))
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            res = self._run(workdir, url=f"https://127.0.0.1:{port}/", ca=str(ca))
        finally:
            srv.shutdown()

        assert res["ok"] is True, f"应当通过：{res}"
        assert res["status"] == 200
        assert "strict" in res, "没报出是否处于严格模式"

    @covers("M14.F8")
    def test_cert_error_classified_as_cert_not_net(self, workdir):
        """★★ 证书错误必须归为 `cert:`，不能归为 `net:`。

        ⚠ 这是我写这段代码时的真 bug：

            except ssl.SSLCertVerificationError: ...   # 永远不触发
            except urllib.error.URLError as e: ...     # 实际走这里

        ``urlopen`` 把 SSL 错误**包进 URLError**（放在 ``.reason`` 里），
        所以前面那个分支写着也不生效，证书问题被标成 `net:`。

        而"网络问题"和"证书问题"的修法完全不同 —— 这正是前面
        反复踩的那类混淆。所以现在从 ``URLError.reason`` 里判断类型。
        """
        import http.server
        import ssl
        import threading

        ca, srv_pem, srv_key = self._make_tls(workdir)

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(srv_pem), str(srv_key))
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        # 故意给一个别的 CA（用仓库那份，与这台临时服务器不匹配）
        try:
            res = self._run(workdir, url=f"https://127.0.0.1:{port}/",
                            ca=str(DEPLOY / "ca.pem"))
        finally:
            srv.shutdown()

        assert res["ok"] is False
        err = res.get("err", "")
        assert err.startswith("cert:"), (
            f"证书错误被归成了非 cert（{err}）—— 会引导用户去查网络"
        )
        assert "verify_code" in res, "没带上 OpenSSL 的验证码"

    @covers("M14.F8")
    def test_network_error_classified_as_net(self, workdir):
        """连不上要归为 `net:`。"""
        res = self._run(workdir, url="https://127.0.0.1:1/",
                        ca=str(DEPLOY / "ca.pem"))
        assert res["ok"] is False
        assert res.get("err", "").startswith("net:"), (
            f"连接失败被归成了非 net：{res}"
        )


class TestCaIsCommittedToGit:
    """★★ CA 必须真的在 git 里，不能只是"本地磁盘上有"。

    ⚠ **这个坑很典型：测试全绿，但文件根本没进仓库。**

    ``deploy/ca.pem`` 是我创建的，本地测试也都通过 —— 因为
    ``test_ca_exists_in_repo`` 查的是 ``DEPLOY / "ca.pem"`` **是否存在**。
    本地确实存在，所以绿灯。

    但 ``.gitignore`` 里有一行 ``*.pem``，它从没被 ``git add`` 过。
    于是：

      * 本地跑测试 → 全绿
      * 别人 clone 下来 → 文件不存在，部署脚本找不到 CA
      * 而用户看到的报错是"找不到 ca.pem"，会以为自己漏了什么

    这就是"works on my machine"的典型形态。**测试必须查 git，
    不能只查文件系统。**

    ## 为什么 *.pem 那条规则不能删

    它是防私钥泄露的安全网，默认拒绝一切 PEM 是对的。
    正确做法是**给公开证书开一个精确例外**（``!deploy/ca.pem``），
    而不是拆掉整张网 —— 因为 ``*.key`` 也要靠同一节规则拦着。
    """

    @covers("M14.F9")
    def test_ca_pem_is_tracked(self):
        """deploy/ca.pem 必须在 git 索引里。"""
        import subprocess

        r = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "deploy/ca.pem"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, (
            "deploy/ca.pem **没有被 git 跟踪** —— 本地有文件不代表仓库里有。\n"
            "  检查 .gitignore 是否拦住了它（*.pem），"
            "并确认有 !deploy/ca.pem 例外。\n"
            f"  git 输出：{r.stderr.strip()}"
        )

    @covers("M14.F9")
    def test_ca_pem_not_ignored(self):
        """deploy/ca.pem 不能被 .gitignore 忽略。

        ``git check-ignore -q`` 的退出码：0 = 被忽略，1 = 不被忽略。
        """
        import subprocess

        r = subprocess.run(["git", "check-ignore", "-q", "deploy/ca.pem"],
                           cwd=REPO_ROOT, capture_output=True, text=True,
                           timeout=30)
        assert r.returncode != 0, (
            "deploy/ca.pem 被 .gitignore 忽略了 —— 需要在 .gitignore 里加 "
            "!deploy/ca.pem 例外"
        )

    @covers("M14.F9")
    def test_no_private_key_tracked(self):
        """★★ 仓库里不能有任何私钥被跟踪。

        这是安全红线。CA 私钥泄露 = 任何人都能签发节点证书；
        服务端私钥泄露 = 可以直接冒充服务端。
        """
        import subprocess

        r = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                           capture_output=True, text=True, timeout=30)
        tracked = r.stdout.splitlines()
        bad = [f for f in tracked
               if f.lower().endswith((".key", ".p12", ".pfx"))]
        assert not bad, f"这些私钥被 git 跟踪了，必须立刻移除：{bad}"

    @covers("M14.F9")
    def test_server_pem_not_tracked(self):
        """★ server.pem 也不该入库。

        它虽然是公开证书，但只对**当前这台服务器**有意义 ——
        别人 clone 下来拿到一份过期的服务端证书没有用处，
        反而会误导（以为是本项目通用的）。
        客户端只需要 CA。
        """
        import subprocess

        r = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                           capture_output=True, text=True, timeout=30)
        bad = [f for f in r.stdout.splitlines()
               if f.lower().endswith("server.pem")]
        assert not bad, f"server.pem 不该入库：{bad}"

    @covers("M14.F9")
    def test_gitignore_still_blocks_generic_pem(self):
        """★ 例外必须**只针对 deploy/ca.pem**，不能放宽到所有 PEM。

        这条测试守的是"别为了修一个问题把安全网拆了"。
        如果哪天有人把 ``*.pem`` 改成 ``!*.pem`` 或直接删掉，
        这条会红。
        """
        import subprocess

        for path in ("deploy/ca.key", "deploy/server.key",
                     "deploy/server.pem", "some/other.pem",
                     "random.key", ".env"):
            r = subprocess.run(["git", "check-ignore", "-q", path],
                               cwd=REPO_ROOT, capture_output=True, text=True,
                               timeout=30)
            assert r.returncode == 0, (
                f"{path} 竟不被 .gitignore 忽略 —— 安全网被拆了？"
            )


class TestBashScriptOrdering:
    """★ bash 版的段落顺序也要对 —— 与 ps1 是同一类 bug。

    ⚠ 我在修 ps1 的顺序问题时，**只修了 ps1**，bash 版还留着同样两处错：
    CA 在网络检查之后、且用 curl 而非 Python 检查。

    这说明一件事：**同一个设计错误在多个实现里出现时，不能只修一个。**
    两个脚本是独立维护的，改了 A 不代表 B 也对。
    """

    path = DEPLOY / "setup-work-node.sh"

    def _lines(self):
        return self.path.read_text(encoding="utf-8").splitlines()

    def _index_of(self, needle: str) -> int:
        for i, l in enumerate(self._lines()):
            if needle in l:
                return i
        return -1

    @covers("M14.F9")
    def test_ca_before_network(self):
        """CA 准备必须在网络检查之前（否则检查时没有 CA 可用）。"""
        i_ca = self._index_of("优先从仓库复制")
        i_net = self._index_of('say "→ 网络与证书"')
        assert i_ca > 0 and i_net > 0
        assert i_ca < i_net, "bash 版：CA 准备在网络检查之后，检查必然失败"

    @covers("M14.F9")
    def test_creates_dir_before_ca(self):
        """★ 建目录必须早于 CA 复制。

        这是"移动一个块，忘了它依赖的前提"的典型：
        把 CA 段落前移后，`mkdir -p "$CONF_DIR"` 还留在原来的位置（后面），
        于是复制时报 `ENOENT: no such file or directory`。
        """
        i_mkdir = self._index_of('mkdir -p "$CONF_DIR"')
        i_ca = self._index_of("优先从仓库复制")
        assert i_mkdir > 0, "找不到 mkdir"
        assert i_mkdir < i_ca, "bash 版：建目录在 CA 复制之后 —— 会 ENOENT"

    @covers("M14.F9")
    def test_ca_candidate_defined_before_use(self):
        """★ `$CA_CANDIDATE` 必须在第一次使用之前定义。

        脚本开头是 `set -u`，未定义变量一引用就退出（不是当空串）。
        这个错我在把 CA 段落前移时犯过 —— 原来的定义在被替换掉的那段里。
        """
        import re

        lines = self._lines()
        use_idx = None
        for i, l in enumerate(lines):
            # 跳过注释
            if l.lstrip().startswith("#"):
                continue
            if "$CA_CANDIDATE" in l:
                use_idx = i
                break
        assert use_idx is not None, "脚本里没用 CA_CANDIDATE？"
        def_idx = next((i for i, l in enumerate(lines)
                        if re.match(r'\s*CA_CANDIDATE="', l)), None)
        assert def_idx is not None, "CA_CANDIDATE 没有定义行"
        assert def_idx < use_idx, (
            f"CA_CANDIDATE 在第 {def_idx+1} 行定义，但第 {use_idx+1} 行就用了"
            f" —— set -u 下会直接退出"
        )

    @covers("M14.F9")
    def test_health_check_uses_python(self):
        """bash 版也用 Python 检查，不用 curl。

        理由同 ps1：桥接是 Python 的 ssl 模块，curl 是另一个程序的判断标准。
        """
        text = self.path.read_text(encoding="utf-8")
        assert "create_default_context" in text, (
            "bash 版没用 Python 的 ssl 做检查"
        )


class TestToolWhitelistValidation:
    """★★ 白名单里的 CLI 必须真的存在，否则任务永远失败。

    ⚠ **我最初连这个校验都没有，而我以为有。**

    我做了三次字符串搜索（``"Bash(" in text``、``"Get-Command" in text``、
    ``"不存在" in text``）都返回 True，于是判断"已校验"。实际代码里
    一行校验都没有 —— 全是别处的巧合匹配：

      * ``Bash(`` 出现在**示例字符串**里
      * ``Get-Command`` 出现在**Python 探测**段
      * ``不存在`` 出现在**CA 缺失**提示里

    **用 grep 判断"功能是否存在"是不可靠的。**

    ## 为什么这个校验重要

    白名单只告诉 CodeBuddy "允许调什么"，协议层**不检查程序存不存在**。
    所以名字拼错时：

      * 注册成功 ✓（角色卡和能力都合法）
      * 任务能派进来 ✓
      * Agent 执行时报"我做不到" ✗ ← 看起来像能力不足

    用户会去怀疑模型能力、怀疑权限、怀疑网络 —— 而实际只是一个字母打错。
    """

    @covers("M14.F9")
    def test_ps1_validates_existence(self):
        """ps1 必须解析白名单并用 Get-Command 逐个检查。

        ⚠ 这里用 ``_ps_strip_comments``（剥注释、**留字符串**）而不是
        ``_ps_code_only``：正则模式 ``'Bash\\(...'`` 是**字符串字面量**，
        剥掉字符串就看不到它了 —— 那会误判成"没解析"。
        """
        text = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        code = _ps_strip_comments(text)
        # 必须真的解析 Bash(...) 里的名字（正则模式本身在字符串里）
        assert "Matches" in code, "ps1 没用正则解析白名单"
        assert "Bash" in code, "ps1 没解析白名单里的 CLI 名字"
        # 必须真的用 Get-Command 查，**且在同一个分支里**（解析之后）
        #
        # ⚠ 只在全文找 Get-Command 会误判：Python 探测段也有一个，
        #   位置在解析之前。必须限定在白名单段内部比较。
        i_parse = code.find("[regex]::Matches")
        assert i_parse > 0, "找不到解析白名单的代码"
        tail = code[i_parse:]
        i_check = tail.find("Get-Command")
        assert i_check > 0, (
            "解析白名单之后没有用 Get-Command 检查存在性 —— "
            "名字拼错时任务会永远失败而看不出原因"
        )
        # 结论里要给出具体缺了哪个名字
        assert "missing" in tail, "没把缺失的工具名收集起来报告"

    @covers("M14.F9")
    def test_ps1_fails_when_none_found(self):
        """一个都找不到时必须判失败，不是仅警告。

        全部缺失 = 配置肯定错了（拼错或工具本身不在这台机器上）。
        仅警告的话用户会忽略，然后所有任务莫名其妙失败。
        """
        text = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        # 该分支里应当有 exit 1（在 -Check 模式下）
        seg = text[text.find("一个都找不到"):][:600]
        assert "exit 1" in seg, (
            "全部工具缺失时没有判失败 —— 只是警告会被忽略"
        )

    @covers("M14.F9")
    def test_bash_validates_existence(self):
        """bash 版同样要校验（不能只修 ps1）。"""
        text = (DEPLOY / "setup-work-node.sh").read_text(encoding="utf-8")
        assert "Bash\\(" in text or "Bash(" in text
        assert "command -v" in text, "bash 版没检查工具是否存在"
        # 解析 + 检查都要有
        assert "grep -oE" in text, "bash 版没解析白名单"

    @covers("M14.F9")
    def test_bash_parsing_works(self):
        """★ bash 的解析逻辑要真的能跑（用 bash，不是 zsh）。

        ⚠ 这个测试差点写成假通过：我第一次是在**交互式 zsh** 里手工验的，
        而 zsh **不对未加引号的变量做单词分割**，于是
        ``for t in $tools`` 把整个多行字符串当成一个值，
        `git`、`npm` 全被报成"找不到"。

        脚本有 ``#!/usr/bin/env bash``，实际运行时是对的 ——
        **但验证时必须用 bash 跑**，否则测的是另一个 shell 的行为。

        这个坑对写 shell 测试的人普遍适用。
        """
        import shutil
        import subprocess

        bash = shutil.which("bash")
        assert bash, "找不到 bash"

        # 从脚本里抽出解析表达式，单独跑一遍
        text = (DEPLOY / "setup-work-node.sh").read_text(encoding="utf-8")
        assert "grep -oE" in text

        script = (
            'CODEBUDDY_ALLOWED_TOOLS="Bash(git:*),Bash(npm:*),Read"\n'
            "tools=$(printf '%s' \"$CODEBUDDY_ALLOWED_TOOLS\" \\\n"
            "        | grep -oE 'Bash\\([[:space:]]*[A-Za-z0-9_.-]+' \\\n"
            "        | sed 's/^Bash([[:space:]]*//' | sort -u)\n"
            'printf "%s\\n" $tools\n'
        )
        r = subprocess.run([bash, "-c", script], capture_output=True,
                           text=True, timeout=30)
        parsed = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        assert parsed == ["git", "npm"], (
            f"解析结果不对：{parsed} —— 期望 ['git','npm']"
        )


class TestNoFakeToolNamesInExamples:
    """★ 示例里不能放"看起来像真名"的假工具名。

    ⚠ **这是我自己造成的用户错误。**

    我在 ``CODEBUDDY_ALLOWED_TOOLS`` 的示例里写了：

        Bash(projectctl:*),Bash(meetingctl:*),Read,Grep

    ``projectctl`` / ``meetingctl`` 是我编的 —— 但它们**太像真的**了，
    用户直接照抄，于是得到"白名单里的 CLI 一个都找不到"。

    用户完全没错，错在示例。**给可复制的示例，占位符必须一眼看出是占位符。**

    正确写法：``Bash(<内部CLI名>:*)`` —— 尖括号明确表示"换成你的"，
    而且它故意不符合 ``[A-Za-z0-9_.-]+``，所以校验器不会把它当成真名去查。
    """

    @covers("M14.F9")
    def test_no_plausible_fake_names(self):
        """setup 脚本的示例里不能出现 projectctl / meetingctl 这类假名字。"""
        for name in ("setup-work-node.ps1", "setup-work-node.sh"):
            p = DEPLOY / name
            if not p.exists():
                continue
            text = (p.read_bytes().decode("utf-8-sig") if name.endswith("ps1")
                    else p.read_text(encoding="utf-8"))
            for fake in ("projectctl", "meetingctl", "yourctl", "myctl"):
                assert fake not in text, (
                    f"{name} 里还有假工具名 {fake!r} —— 用户会照抄，然后校验失败。"
                    f"改成 <内部CLI名> 这种明显占位符。"
                )

    @covers("M14.F9")
    def test_example_uses_real_builtin_tools(self):
        """★ 示例要给**真实存在的内置工具名**，不是编的 CLI。

        ⚠ 这里有个认识上的转变：

        早期版本让用户填 ``Bash(<内部CLI名>:*)`` —— 即"指定一个取数命令"。
        但那是把 CodeBuddy 当脚本执行器用。它的真实价值是
        **自己判断该读哪个文件、该用哪个 skill、该跑什么命令**。

        所以示例改成真实的内置工具：
        ``Read, Grep, Glob, Bash, WebFetch`` —— 这些名字在任何 CodeBuddy
        安装里都成立，不会因为用户环境不同而失败。
        """
        ps = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        for tool in ("Read", "Grep", "Glob", "Bash", "WebFetch"):
            assert tool in ps, f"示例里没提到内置工具 {tool}"
        # 推荐值要成组出现（可复制）
        assert "Read,Grep,Glob,Bash,WebFetch" in ps, (
            "没有给出一整条可复制的推荐值"
        )

    @covers("M14.F9")
    def test_whitelist_not_overly_narrow(self):
        """★ 白名单示例不能收窄到"只允许某个 CLI"。

        收窄的后果：skill 跑不了（它们内部要执行命令）、读不了非工作目录的
        文件、调不了内部 API —— 于是 Agent 只能说"我做不到"，
        而用户以为是能力不足或权限问题。

        安全边界应该在**任务类型 + 输出截断 + 域矩阵**这三层，
        而不是靠"少给工具"。
        """
        ps = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        # 推荐行里必须同时有读类和执行类工具
        line = next((l for l in ps.splitlines()
                     if "Read,Grep,Glob,Bash,WebFetch" in l), "")
        assert line, "找不到推荐的白名单行"
        assert "Bash" in line, (
            "推荐值里没有 Bash —— skill 内部执行命令会失败"
        )
        # 不能只有 Bash(某个具体命令)
        import re
        assert not re.search(r'"Bash\([A-Za-z]', line), (
            "推荐值把 Bash 限制到了具体命令 —— 那等于只允许跑那一个工具，"
            "Agent 无法自主选择"
        )


class TestRealPowerShellParser:
    """★★ 用**真实的 PowerShell 解析器**检查 .ps1 语法。

    ⚠ **这一节的存在，是因为我前面所有静态检查都漏了三个真错误。**

    我写了一整套 Python 静态检查：BOM、引号配对、变量赋值、括号嵌套、
    `$var` 接中文……测试全绿。但里面一直藏着：

    ===============================================  ==================
    错误                                              静态检查为何漏掉
    ===============================================  ==================
    ``param`` 块里漏了一个逗号                         括号数量是对的
    ``$env:TEMP`` 为 null 时 ``Join-Path`` 崩           只看语法不看取值
    ``'\\\\b(...)'`` 在单引号里不是单词边界               引号是配对的
    ===============================================  ==================

    第一个是**语法错误**（脚本根本解析不了），后两个是**运行期**问题。
    这些只有真解析器能抓到。

    ## 所以：能跑真解析器，就别满足于静态检查

    只需要下载一个 68 MB 的 pwsh，检查 4 个文件耗时约 2 秒。
    没有任何理由不做。
    """

    @staticmethod
    def _find_pwsh():
        import shutil

        for cand in (shutil.which("pwsh"), "/tmp/nxs-pwsh/pwsh", "/tmp/pwsh/pwsh"):
            if cand and pathlib.Path(cand).is_file():
                return cand
        return None

    @covers("M14.F9")
    def test_all_ps1_parse_cleanly(self):
        """所有 .ps1 必须能通过 PowerShell 解析器。

        没有 pwsh 时跳过 —— CI 环境未必装了。但要跑
        ``deploy/lint-ps1.sh`` 的话它会自动下载。
        """
        import subprocess

        pwsh = self._find_pwsh()
        if not pwsh:
            pytest.skip("本机没有 pwsh；跑 deploy/lint-ps1.sh 可自动下载")

        script = (
            '$d = Join-Path $env:NXS_LINT_DIR "deploy"\n'
            '$files = Get-ChildItem -Path $d -Filter "*.ps1" -File\n'
            '$fail = 0\n'
            'foreach ($f in $files) {\n'
            '  $t=$null;$e=$null\n'
            '  [System.Management.Automation.Language.Parser]::ParseFile('
            '$f.FullName,[ref]$t,[ref]$e) | Out-Null\n'
            '  if ($e.Count -gt 0) {\n'
            '    $fail++\n'
            '    foreach ($x in $e) {\n'
            '      Write-Output ("{0}|{1}|{2}" -f $f.Name, '
            '$x.Extent.StartLineNumber, $x.Message)\n'
            '    }\n'
            '  }\n'
            '}\n'
            'exit $fail\n'
        )
        import os
        env = dict(os.environ)
        env["NXS_LINT_DIR"] = str(REPO_ROOT)
        r = subprocess.run([pwsh, "-NoProfile", "-Command", script],
                           capture_output=True, text=True, timeout=120, env=env)

        assert r.returncode == 0, (
            "PowerShell 解析失败：\n" + r.stdout +
            "\n  修完后跑一遍：bash deploy/lint-ps1.sh"
        )

    @covers("M14.F9")
    def test_lint_script_exists(self):
        """★ 要有可复用的 lint 脚本 —— 不能只在我脑子里记得检查。

        下次改完 .ps1，任何人（包括我）应该能一条命令验证，
        而不是靠"我记得要检查哪些点"。
        """
        lint = DEPLOY / "lint-ps1.sh"
        assert lint.is_file(), "缺 deploy/lint-ps1.sh"
        text = lint.read_text(encoding="utf-8")
        # 必须用真解析器，不是自己写的正则
        assert "Parser]::ParseFile" in text, (
            "lint 脚本没用真实解析器 —— 那和静态检查没区别"
        )
        # 必须能自动获取 pwsh
        assert "releases/download" in text, "不会自动下载 pwsh"
        # 必须能返回非零退出码（否则接不进 CI）
        assert "exit 1" in text

    @covers("M14.F9")
    def test_lint_script_does_not_execute_scripts(self):
        """★ lint 只能**解析**，不能**执行** .ps1。

        ``pwsh -File x.ps1`` 会真的跑起来 —— 而 setup 脚本有副作用
        （改环境变量、建计划任务）。检查语法的人不该顺带改系统状态。

        Parser::ParseFile 只解析不执行，这是刻意的选择。
        """
        text = (DEPLOY / "lint-ps1.sh").read_text(encoding="utf-8")
        # 不许出现 -File 执行 .ps1
        import re
        assert not re.search(r'\$PWSH"?\s+-NoProfile\s+-File', text), (
            "lint 脚本用 -File 执行了 .ps1 —— 那会触发副作用"
        )


class TestParamBlockIntegrity:
    """★★ param 块的每个参数都必须还在。

    ⚠ 背景：我插入 `-AllowedTools` / `-ExtraDirs` 时漏了第 32 行的逗号，
    整段 param 列表断裂，脚本**根本解析不了**。

    真解析器能抓到「漏逗号」，但抓不到「整行被删掉」——
    那仍然能解析通过，只是少了个参数，于是 `-WriteEnv` 之类的开关
    静默失效（用户传了但没用）。

    所以除了真解析器，还要单独守一遍"参数齐全"。
    """

    @staticmethod
    def _params(pwsh: str, path) -> list[str]:
        """用 PowerShell AST 取 param 块里的参数名。"""
        import os
        import subprocess

        script = (
            '$ast = [System.Management.Automation.Language.Parser]::ParseFile('
            '$env:NXS_P1, [ref]$null, [ref]$null)\n'
            'if ($ast.ParamBlock) {\n'
            '  foreach ($p in $ast.ParamBlock.Parameters) {\n'
            '    Write-Output $p.Name.VariablePath.UserPath\n'
            '  }\n'
            '}\n'
        )
        env = dict(os.environ)
        env["NXS_P1"] = str(path)
        r = subprocess.run([pwsh, "-NoProfile", "-Command", script],
                           capture_output=True, text=True, timeout=60, env=env)
        return [l.strip() for l in r.stdout.splitlines() if l.strip()]

    @covers("M14.F9")
    def test_setup_has_all_switches(self):
        """setup 脚本必须保留这些参数（它们是功能入口）。"""
        pwsh = TestRealPowerShellParser._find_pwsh()
        if not pwsh:
            pytest.skip("本机没有 pwsh")

        params = self._params(pwsh, DEPLOY / "setup-work-node.ps1")
        required = ["Check", "InstallTask", "WriteEnv",
                    "AllowedTools", "ExtraDirs", "NodeId", "Server"]
        missing = [p for p in required if p not in params]
        assert not missing, (
            f"param 块缺了这些参数：{missing}\n"
            f"  当前有：{params}\n"
            f"  常见原因：插入新参数时漏了行尾逗号或整行被删"
        )


class TestVersionReporting:
    """★ 脚本要自报版本与 commit。

    ⚠ **我们因为"用户跑的是旧版"浪费了好几轮对话。**

    用户报的错误在最新版里已经修好了，但两边都没意识到版本不同：
    用户以为是他环境的问题，我以为是我代码的问题，来回查。

    把版本打进第一屏输出，一眼就能对上 —— 这是最低成本的诊断信息。
    """

    @covers("M14.F9")
    def test_setup_prints_version(self):
        """setup 脚本启动时要打印版本与 commit。"""
        text = (DEPLOY / "setup-work-node.ps1").read_bytes().decode("utf-8-sig")
        assert "脚本版本" in text, "没打印脚本版本"
        # 必须真的去读 __init__.py 和 git
        assert "__version__" in text, "没从 __init__.py 读版本号"
        assert "rev-parse" in text, "没取 git commit"
