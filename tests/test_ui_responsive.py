"""★ 窄屏适配：表格转卡片堆叠（2026-11-13）。

## 为什么这次才修

交接文档 §8 的验收清单里有一条：

    - [ ] 390px 宽不横向溢出（**表格多，这条最容易挂**）

它一直没做。实测 390px 下节点表宽 716px，整页横向溢出 **358px** ——
手机上打开要横滚 3 屏才能看到「水位」「回传」，而那两个正是判断
"节点是否堵住"的关键列。所以这不是"不够好看"，是**核心信息看不到**。

## 做法与两个踩过的坑

**做法**：窄屏下把每行变成卡片，列名靠 ``td::before { content: attr(data-label) }``。
``data-label`` 由 ``labelCells()`` 在渲染后按表头**自动**填 —— 这样新增
表格自动适配，不用逐表写媒体查询。

**坑 1 · 只改 tr/td 的 display 不够**

第一版只改了 ``tr`` 和 ``td``，``table`` 与 ``tbody`` 仍是
``table`` / ``table-row-group``，会按内容自动撑宽 —— 实测仍溢出 11px。
卡片化的前提是整条链都脱离表格布局。

**坑 2 · 断点不能拍脑袋**

第一版断点定在 680px，390px 修好了，但 681–749px 区间继续溢出 ——
只是把坏点挪到了别人身上。逐 10px 扫实测：

    690px → 58px     710px → 38px     730px → 18px
    700px → 48px     720px → 28px     740px →  8px
    750px → 不溢出（表格模式的最小可用宽度）

所以断点取 760px（750 + 10px 余量）。

**坑 3 · 类选择器的优先级**

``.log th, .log td { padding: 7px 11px; white-space: nowrap; }`` 是类选择器
（特异性 0,1,1），元素选择器 ``tbody td``（0,0,2）**赢不了它**。症状是
padding 没生效、且 ``nowrap`` 让日志表直接撑破容器。窄屏规则必须写
``table tbody td`` / ``table.log tbody td``（0,1,2）才压得住。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys

import pytest

from conftest import REPO_ROOT, covers
from nxs.ui.page import SHELL

#: 实测得到的表格模式最小可用宽度（逐 10px 扫描 660–820 的结果）
TABLE_MIN_WIDTH = 750

#: 断点。写成「<760 用卡片」。
BREAKPOINT = 760


class TestBreakpointValue:
    """★ 断点必须覆盖表格模式的溢出区间。"""

    @covers("M15.F10")
    def test_breakpoint_matches_measured_value(self):
        """★★ 断点不能小于表格的最小可用宽度。

        ⚠ 第一版拍在 680px，结果 681–749px 区间继续溢出。这条测试就是
        把那次教训钉住：断点 < 750 就会出现"修好一档、坏另一档"。
        """
        m = re.search(r"@media \(max-width: (\d+)px\)", SHELL)
        assert m, "找不到响应式断点"
        got = int(m.group(1))
        assert got >= TABLE_MIN_WIDTH - 1, (
            f"断点是 {got}px，但表格模式要到 {TABLE_MIN_WIDTH}px 才不溢出 —— "
            f"{got + 1}–{TABLE_MIN_WIDTH - 1}px 区间会横向溢出"
        )
        assert got == BREAKPOINT - 1, (
            f"断点写的是 {got}（即 <{got + 1} 用卡片），与文档约定的 "
            f"<{BREAKPOINT} 不一致"
        )

    @covers("M15.F10")
    def test_breakpoint_documented_with_measurements(self):
        """★ 断点的依据要写在注释里 —— 否则下次有人会随手改。

        ⚠「为什么是 760」这个信息只存在于我当时的测量里。不写下来，
        后来人看到 760 会以为是随手拍的，改成 640「看起来更规整」，
        然后 641–749 又坏掉。
        """
        i = SHELL.find("@media (max-width: ")
        assert i > 0
        context = SHELL[max(0, i - 2000):i]
        assert "750" in context, "注释里没给出实测的临界宽度"
        assert "溢出" in context, "注释里没说明这是溢出驱动的决定"


class TestCardsDoNotLoseInformation:
    """★★ 卡片化最容易翻车的是「信息丢了」而不是「样式难看」。

    ⚠ 少一列 = 少一个判断依据。而这个面板的全部意义就是「给人看运行情况」，
    所以窄屏可以长、可以换行，但不能少东西。
    """

    @covers("M15.F10")
    def test_label_cells_uses_table_headers(self):
        """★★ 列名必须取自表头，不能手写。"""
        assert "function labelCells" in SHELL, "没有 labelCells"
        # 必须读 thead th 作为来源
        i = SHELL.find("function labelCells")
        j = SHELL.find("\n}", i)
        body = SHELL[i:j]
        assert 'querySelectorAll("thead th")' in body, (
            "没有从表头取列名 —— 手写列名会在加列时漏掉"
        )
        assert "setAttribute(\"data-label\"" in body or \
               "setAttribute('data-label'" in body, "没写入 data-label"

    @covers("M15.F10")
    def test_label_cells_skips_title_and_colspan(self):
        """★ 标题列与空态行不该带列名标签。

        ``row-title``（节点名/简介）是卡片标题，带「名称 / 简介:」很怪；
        带 ``colspan`` 的是空态提示行，那是一句话而不是字段。
        """
        i = SHELL.find("function labelCells")
        j = SHELL.find("\n}", i)
        body = SHELL[i:j]
        assert "row-title" in body, "没跳过标题列"
        assert "colspan" in body, "没跳过空态行"

    @covers("M15.F10")
    def test_every_table_render_calls_label_cells(self):
        """★★★ 每一处渲染表格的地方都必须调 labelCells。

        ⚠ 漏掉一处的表现是：那张表在手机上只显示值、**没有列名**，
        读起来是一串无意义的数字。而这不会有任何报错 —— 页面"看起来
        有内容"。所以必须逐一核对调用点数量。
        """
        # 4 张表：节点 / 拒绝 / 任务类型 / 运行日志
        calls = SHELL.count("labelCells(") - 1   # 减掉函数定义那一处
        assert calls >= 4, (
            f"只有 {calls} 处调用了 labelCells，但有 4 张表 —— "
            "漏掉的那张在手机上会没有列名"
        )

    @covers("M15.F10")
    def test_node_name_is_row_title(self):
        """★ 节点名/简介列要标成 row-title（卡片标题）。"""
        i = SHELL.find("function renderNodes")
        j = SHELL.find("\n}", i)
        body = SHELL[i:j]
        assert 'el("td", "row-title")' in body, (
            "节点名那一列没标 row-title —— 卡片里会带「名称 / 简介:」标签"
        )


class TestCSSSpecificity:
    """★★ 窄屏规则必须压得住基础规则。

    ⚠⚠ **这里踩过一个很隐蔽的坑。** 基础样式里有：

        .log th, .log td { padding: 7px 11px; white-space: nowrap; }

    类选择器特异性 0,1,1，比我第一版写的元素选择器 ``tbody td``（0,0,2）
    更高 —— 所以窄屏规则**完全没生效**。症状：padding 还是 7px 11px，
    而且 ``nowrap`` 让日志表直接撑破容器（溢出 11px）。

    这种失败特别难查，因为 CSS 文件里两段规则看着都对。
    """

    @staticmethod
    def _mobile_block() -> str:
        i = SHELL.find("@media (max-width: ")
        assert i > 0, "找不到窄屏媒体查询"
        depth = 0
        for j in range(i, len(SHELL)):
            if SHELL[j] == "{":
                depth += 1
            elif SHELL[j] == "}":
                depth -= 1
                if depth == 0:
                    return SHELL[i:j + 1]
        raise AssertionError("媒体查询块未闭合")

    @staticmethod
    def _td_rule_body() -> str:
        """取出窄屏里设置 ``td`` 的那条规则**体**（不含 ::before）。

        ⚠ 必须精确到这条规则，不能只看整个 media 块 —— 因为
        ``td::before`` 里也写了 ``white-space: normal``（重置标签的换行），
        整个块里搜会让判据永远成立。第一版就是这么假绿的。
        """
        block = TestCSSSpecificity._mobile_block()
        m = re.search(
            r"table tbody td,\s*\n\s*table\.log tbody td\s*\{(.*?)\n    \}",
            block, re.S)
        assert m, "找不到窄屏的 td 规则"
        return m.group(1)

    @covers("M15.F10")
    def test_resets_whitespace_nowrap(self):
        """★★★ 必须解除 ``.log td`` 的 ``white-space: nowrap``。

        ⚠ 不解除的话，日志表的长内容不换行，直接把容器撑破 ——
        实测就是那剩下的 11px。

        ⚠⚠ 判据只能看 **td 规则体**。第一版写成「整个 media 块里含
        white-space: normal」，而 ``td::before`` 里本来就有这一句，
        于是把 td 的那句删掉测试照样绿。这是第 N 次栽在同一个模式上：
        **判据落在"这段文本里有没有某个词"，而不是"这条规则做了什么"。**
        """
        body = self._td_rule_body()
        assert "white-space: normal" in body, (
            "窄屏的 td 规则没重置 white-space —— "
            ".log td 的 nowrap 会撑破容器"
        )

    @covers("M15.F10")
    def test_td_selector_has_enough_specificity(self):
        """★★ td 的窄屏规则必须带类（或 table 前缀）才压得住 ``.log td``。"""
        block = self._mobile_block()
        # 找出所有设置 td 样式的选择器
        assert "table tbody td" in block or "table.log tbody td" in block, (
            "td 规则用的是裸元素选择器，特异性 0,0,2 压不住 .log td (0,1,1)"
        )

    @covers("M15.F10")
    def test_table_and_tbody_become_block(self):
        """★★ ``table`` 与 ``tbody`` 也必须改 display。

        ⚠ 只改 tr/td 的话，表格仍是 table 布局，会按内容自动撑宽 ——
        实测残留溢出 11px。
        """
        block = self._mobile_block()
        m = re.search(r"table,\s*tbody,\s*tr,\s*td,\s*th\s*\{[^}]*display:\s*block",
                      block)
        assert m, "table/tbody 没改成 block —— 仍是表格布局，会按内容撑宽"

    @covers("M15.F10")
    def test_long_content_can_wrap(self):
        """★ 长文本（能力列表、错误信息）必须能断行。"""
        body = self._td_rule_body()
        assert "overflow-wrap" in body or "word-break" in body, (
            "没允许长词断行 —— 超长的节点 ID 或错误信息会撑破卡片"
        )
        assert "min-width: 0" in body, (
            "flex 子项缺 min-width:0 —— 长内容不会收缩，照样溢出"
        )


class TestRealBrowserResponsive:
    """★★ 真浏览器扫全宽度区间。

    ⚠ 单测只能看 CSS 文本，看不到"实际渲染出多宽"。而这次的缺陷本质
    就是**渲染宽度**。所以必须真跑。

    ⚠ 扫 20 个宽度而不是只看 390px —— 第一版只验 390，结果把坏点
    挪到了 681–749px。
    """

    @staticmethod
    def _has_playwright() -> bool:
        import shutil as sh

        if sh.which("node") is None:
            return False
        probe = subprocess.run(["node", "-e", "require('playwright')"],
                               cwd="/tmp/uitest", capture_output=True,
                               check=False)
        return probe.returncode == 0

    @covers("M15.F10")
    def test_no_overflow_across_widths_and_states(self):
        if not self._has_playwright():
            pytest.skip("没有 node + playwright 环境（/tmp/uitest）")

        import shutil as sh

        # ① 生成四个状态页
        subprocess.run([sys.executable,
                        str(REPO_ROOT / "deploy" / "preview-ui-state.py")],
                       check=True, capture_output=True, timeout=120)
        # ② 拷到有 playwright 的目录再跑（模块解析需要）
        target = "/tmp/uitest/_nxs_verify_responsive.js"
        sh.copy(REPO_ROOT / "deploy" / "verify-responsive.js", target)

        result = subprocess.run(["node", target], cwd="/tmp/uitest",
                                capture_output=True, text=True, timeout=600,
                                check=False)
        out = result.stdout
        assert result.returncode == 0, (
            "响应式验收失败：\n" + (out + result.stderr)[-2500:]
        )
        assert "全部无横向溢出" in out, out[-1500:]
        assert "模式切换正确: 是" in out, (
            "断点两侧的模式切换不对 —— 检查 759/760 的行为\n" + out[-1000:]
        )
