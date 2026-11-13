"""★ 凭据失效 → 自动恢复（2026-11-13）。

## 要解决的问题

实测确认：UI token **没有 TTL**，它在 ``UiServer.__init__`` 里生成一次，
进程重启才变。所以「过期」只有两种成因：

    服务重启（部署/改配置）    → token 立刻换，旧标签全失效
    浏览器关掉                 → cookie 是 session cookie，没 Max-Age

而我那天连做了 5 次部署。每次之后，正在看的面板都会变成「凭据已失效」，
而当时的页面在教用户「去服务器上跑 journalctl」——**用户站在浏览器里，
手边没有终端**。

## 解法（用户选定的方向：失效即自动恢复）

    · 401 页面自动跳回 hub 的 /api/nexus-enter 换新 token
    · cookie 加 Max-Age，关浏览器不再丢

**不放松安全边界**：token 仍随机、仍重启即换、仍不落盘。变的只是
「失效之后怎么办」——从"要人操心"变成"页面自己接上"。

## 这个文件守的三件事

1. **不能放松鉴权** —— 没有任何配置能让未认证请求直接拿到数据
2. **不能循环跳转** —— 401 → 跳 → 401 → 跳 会变成刷不完的页面
3. **不能注入** —— recover_url 进 href 与 <script> 两个位置
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request

import pytest

from conftest import covers
from nxs.ui.page import unauthorized_page

SECRET = b"ui-test-secret-0123456789abcdef"
HUB_ENTER = "https://hub.example.com/api/nexus-enter"


def _get(url: str, cookie: str = "", host: str = "") -> tuple[int, str, bytes]:
    """发 GET，返回 (状态码, Location, 正文)。不跟随跳转。"""
    request = urllib.request.Request(url)
    if cookie:
        request.add_header("Cookie", cookie)
    if host:
        request.add_header("Host", host)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):  # noqa: D102
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=10) as resp:
            return (resp.status, resp.headers.get("Location", ""),
                    resp.read())
    except urllib.error.HTTPError as exc:
        return (exc.code, exc.headers.get("Location", ""), exc.read())


class TestRecoverUrlIsValidated:
    """★★ recover_url 会进 ``href`` 与 ``<script>``，必须严格校验。

    来源是运维配置（unit 的 Environment / 命令行参数），不是用户输入 ——
    所以这不是信任边界问题。**但配置写错一个引号，401 页面就成了 XSS 载体**，
    而它偏偏是个安全敏感页面。宁可直接拒绝。
    """

    @covers("M15.F2")
    def test_rejects_non_http_schemes(self):
        from nxs.ui.server import _clean_recover_url

        for bad in ("javascript:alert(1)", "data:text/html,x",
                    "//evil.example.com/x", "file:///etc/passwd",
                    "ftp://x/y", ""):
            assert _clean_recover_url(bad) is None, f"{bad!r} 不该通过"

    @covers("M15.F2")
    def test_rejects_markup_and_quotes(self):
        from nxs.ui.server import _clean_recover_url

        for bad in (
            'https://hub.example.com/api/nexus-enter"><script>alert(1)</script>',
            "https://hub.example.com/api/nexus-enter'; alert(1); //",
            "https://hub.example.com/a</script><script>alert(1)</script>",
            "https://hub.example.com/a b",
            "https://hub.example.com/a\\b",
        ):
            assert _clean_recover_url(bad) is None, f"{bad!r} 不该通过"

    @covers("M15.F2")
    def test_accepts_normal_url(self):
        from nxs.ui.server import _clean_recover_url

        assert _clean_recover_url(HUB_ENTER) == HUB_ENTER
        assert _clean_recover_url("  " + HUB_ENTER + "  ") == HUB_ENTER
        assert _clean_recover_url("http://127.0.0.1:8002/api/nexus-enter")


class TestRecoverPage:
    """401 页面的两种形态：有恢复入口 / 没有。"""

    @covers("M15.F2")
    def test_without_recover_url_no_script_no_button(self):
        """★★ 没配置时**不能**留下半截脚本或死按钮。

        这是刻意留的降级：SSH 隧道场景下浏览器可能连不到公网 hub，
        硬跳过去只会白屏。那种场景该给手动说明。
        """
        page = unauthorized_page(None)

        assert page.lstrip().startswith("<!DOCTYPE html>")
        assert "立即重新进入" not in page, "没配恢复入口却给了按钮 —— 点了会 404"
        assert "nxs_ui_recover_at" not in page, (
            "没配恢复入口却注入了自动跳转脚本 —— 会跳到 undefined"
        )
        # 手动兜底必须还在
        assert "journalctl" in page

    @covers("M15.F2")
    def test_with_recover_url_has_button_and_autojump(self):
        page = unauthorized_page(HUB_ENTER)

        assert "立即重新进入" in page
        assert HUB_ENTER in page, "恢复入口没进页面"
        # 自动跳转脚本
        assert "window.location.href" in page, "没有自动跳转"
        assert "sessionStorage" in page, "没有防循环标记"

    @covers("M15.F2")
    def test_placeholder_is_fully_substituted(self):
        """★ 占位符必须全部替换掉 —— 漏一个会原文显示给人看。"""
        for url in (None, HUB_ENTER):
            page = unauthorized_page(url)
            leftovers = re.findall(r"__[A-Z_]+__", page)
            assert not leftovers, f"占位符没被替换：{leftovers}"

    @covers("M15.F2")
    def test_both_forms_still_say_it_is_not_a_failure(self):
        """★ 两种形态都要保留「这不代表系统出了问题」。"""
        for url in (None, HUB_ENTER):
            assert "不代表系统出了问题" in unauthorized_page(url)


class TestNoInfiniteRedirectLoop:
    """★★★ 自动跳转必须有防循环 —— 这是这个功能最危险的地方。

    ## 循环是怎么发生的

        旧标签 → 401 → 自动跳 hub → hub 从 journald 取 token → 303 回面板
                 → 若那张 token 也失效（比如 UI 恰在 hub 取到 token 之后又重启）
                 → 401 → 自动跳 hub → …

    结果是页面每两三秒自己刷一次，用户什么都做不了，而且看不出原因。
    比原来的"要手动取链接"糟得多。

    ## 判据

    页面里必须有时间窗保护，且第二个分支要**放弃自动跳**并说明情况。
    这里直接查页面里那两条分支的语义，不是查"有没有 sessionStorage 这个词"。
    """

    @covers("M15.F2")
    def test_guard_window_present(self):
        page = unauthorized_page(HUB_ENTER)

        # 有时间窗常量
        m = re.search(r"GUARD_MS\s*=\s*(\d+)", page)
        assert m, "没有防循环的时间窗常量"
        assert int(m.group(1)) >= 30000, (
            f"时间窗只有 {int(m.group(1))}ms —— 太短，来不及让人看清发生了什么"
        )

    @covers("M15.F2")
    def test_second_visit_gives_up_autojump(self):
        """★★ 窗口内的第二次不再自动跳，改为告知用户。

        判据落在**行为**上：窗口内那条分支必须是 `return`（放弃跳转），
        而不是改个计数继续跳。
        """
        page = unauthorized_page(HUB_ENTER)

        # 找出"在窗口内"的那个 if 块，它必须先 return 再走到 setItem
        window_branch = re.search(
            r"if\s*\(\s*last\s*&&\s*waited\s*<\s*GUARD_MS\s*\)\s*\{(.*?)\n  \}",
            page, re.S)
        assert window_branch, "找不到「在时间窗内」的判断分支"
        body = window_branch.group(1)
        assert "return" in body, (
            "时间窗内的分支没有 return —— 会继续往下跳，等于没有防循环"
        )
        assert "未成功" in body, (
            "放弃自动跳时要说明「刚试过但没成功」，否则用户不知道为什么不动"
        )

    @covers("M15.F2")
    def test_manual_button_always_available(self):
        """★ 手动按钮不受防循环限制 —— 用户主动点是在表达意图。

        ⚠ 如果连按钮都被窗口挡住，循环发生时用户就彻底没出路了。
        按钮是 ``<a href>``，不带条件渲染，从结构上就不可能被挡。
        """
        page = unauthorized_page(HUB_ENTER)
        assert re.search(r'<a class="btn"[^>]*href="[^"]*nexus-enter', page), (
            "恢复按钮不是普通链接 —— 它必须随时可点"
        )


class TestServerPicksRecoverByHost:
    """服务端按 ``Host`` 决定给不给恢复入口。

    | 访问方式 | Host | 给恢复入口？ |
    |---|---|---|
    | 经 hub 反代 | nexus.example.com | 给（浏览器能到公网 hub） |
    | SSH 隧道 / 本机 | 127.0.0.1:18080 | 不给（就在终端旁，给手动说明） |

    ⚠ 这不是安全判断（未认证请求一样被拒），是**场景判断**：隧道场景下
    浏览器未必连得上公网域名，硬跳只会白屏。
    """

    @pytest.fixture
    def server(self):
        from nxs.ui import UiServer

        srv = UiServer("http://127.0.0.1:1/nxs", SECRET, port=0,
                       recover_url=HUB_ENTER).start()
        yield srv
        srv.stop()

    @covers("M15.F2")
    def test_external_host_gets_recover(self, server):
        code, _, body = _get(f"{server.base_url}/",
                             host="nexus.example.com")
        assert code == 401
        assert "立即重新进入" in body.decode("utf-8")

    @covers("M15.F2")
    def test_local_host_does_not_get_recover(self, server):
        for host in ("127.0.0.1:18080", "localhost:18080"):
            code, _, body = _get(f"{server.base_url}/", host=host)
            assert code == 401
            text = body.decode("utf-8")
            assert "立即重新进入" not in text, (
                f"Host={host} 是隧道场景，不该跳公网 hub"
            )
            assert "journalctl" in text, "隧道场景要给手动取链接的命令"

    @covers("M15.F2")
    def test_no_recover_configured_never_shows_button(self):
        """没配置 recover_url 时，即使 Host 是外部域名也不给。"""
        from nxs.ui import UiServer

        srv = UiServer("http://127.0.0.1:1/nxs", SECRET, port=0).start()
        try:
            assert srv.recover_url is None
            code, _, body = _get(f"{srv.base_url}/", host="nexus.example.com")
            assert code == 401
            assert "立即重新进入" not in body.decode("utf-8")
        finally:
            srv.stop()


class TestCookiePersists:
    """★ cookie 要有 Max-Age —— 否则关掉浏览器就得重新取链接。

    ⚠ 原来**没有** Max-Age，是 session cookie。而 token 本身根本没有 TTL，
    所以"短命 cookie"保护不了任何东西，只让人多跑一趟。

    ⚠ 但注意它**不等于** token 活 30 天：token 一变（重启），cookie 里的
    旧值立刻失效。这个属性只解决"关浏览器"那一半 —— 另一半靠自动恢复。
    """

    @covers("M15.F2")
    def test_set_cookie_has_max_age(self):
        from nxs.ui import UiServer

        srv = UiServer("http://127.0.0.1:1/nxs", SECRET, port=0).start()
        try:
            request = urllib.request.Request(f"{srv.base_url}/?t={srv.token}")

            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **kw):  # noqa: D102
                    return None

            opener = urllib.request.build_opener(_NoRedirect)
            try:
                opener.open(request, timeout=10)
                cookie = ""
            except urllib.error.HTTPError as exc:
                cookie = exc.headers.get("Set-Cookie", "")

            assert cookie, "换 cookie 的响应里没有 Set-Cookie"
            assert "Max-Age=" in cookie, (
                f"cookie 没有 Max-Age（仍是 session cookie）：{cookie}"
            )
            # 安全属性一个都不能少
            assert "HttpOnly" in cookie
            assert "SameSite=Strict" in cookie
            assert "Path=/" in cookie
        finally:
            srv.stop()


class TestAutoJumpInRealBrowser:
    """★★ 自动跳转与防循环 —— 必须在**真浏览器**里验。

    单元测试只能看脚本文本，看不到「3 秒后是否真的跳了」「第二次是否
    真的不跳」。而这两件事恰好是防循环的关键。

    ⚠ **第一版验证脚本在这里搞错了建法**：用两个 ``newPage()`` 测
    「第二次不再跳」，结果是假阴性 —— Playwright 的 ``newPage()`` 会新建
    BrowserContext，sessionStorage 自然隔离，第二条当然读不到第一条写的标记。

    而真实的循环发生在**同一个标签页**里：

        401 → 自动跳 hub → 303 回 /?t=… → 若又失效 → 401（同一 tab）

    所以必须复用同一个 page。这条值得记：测「跨请求的状态保持」时，
    隔离环境的建法本身就会决定结论 —— 隔离了就等于每次都是"第一次"。

    数据来自 ``deploy/export-401-page.py`` 导出的**真实页面**，
    恢复目标只做域名替换（换成本地 /enter），其余一字不动。
    """

    @staticmethod
    def _has_node_and_playwright() -> bool:
        import shutil
        import subprocess

        if shutil.which("node") is None:
            return False
        probe = subprocess.run(
            ["node", "-e", "require('playwright')"],
            cwd="/tmp/uitest", capture_output=True, check=False)
        return probe.returncode == 0

    @covers("M15.F2")
    def test_autojump_then_guard_then_manual(self):
        # ⚠ 不用 tmp_path fixture：这台机器上它会报 FileExistsError
        #   （pytest 的临时目录创建与宿主策略冲突，见过多次）。
        import shutil
        import sys
        import subprocess

        if not self._has_node_and_playwright():
            pytest.skip("没有 node + playwright 环境（/tmp/uitest）")

        from conftest import REPO_ROOT

        # ① 导出真实页面
        subprocess.run([sys.executable,
                        str(REPO_ROOT / "deploy" / "export-401-page.py")],
                       check=True, capture_output=True, timeout=60)
        # ② 把验证脚本复制到有 playwright 的目录再跑（模块解析需要）
        script = REPO_ROOT / "deploy" / "verify-401-autojump.js"
        target = "/tmp/uitest/_nxs_verify_401.js"
        shutil.copy(script, target)

        result = subprocess.run(["node", target], cwd="/tmp/uitest",
                                capture_output=True, text=True, timeout=180,
                                check=False)
        out = result.stdout
        assert result.returncode == 0, (out + result.stderr)[-2500:]

        # 四条行为逐项核对 —— 不是查"脚本文本里有没有某个词"
        checks = {
            "自动跳转生效: true": "首次进入必须真的跳走",
            "防循环生效（没再跳）: true": "同一标签页第二次不能再跳",
            "有解释为什么不动: true": "不跳时必须说明原因",
            "手动可用: true": "按钮随时可点",
            "窗口过后恢复自动跳: true": "时间窗过后要重新允许",
        }
        missing = [why for line, why in checks.items() if line not in out]
        assert not missing, (
            "这些行为没通过（真浏览器实测）：\n  "
            + "\n  ".join(missing)
            + "\n\n脚本输出：\n" + out[-2000:]
        )
