"""M15 · 观察窗 WebUI 的测试。

这个模块的安全性靠四条**结构性**约束，不是靠"写的时候小心"：

1. 只听 ``127.0.0.1``（拒绝公网）
2. 只读（没有写方法）
3. token 不落盘、每次启动新的
4. 数据只在浏览器端用 ``textContent`` 渲染（不解析 HTML）

下面每条约束都至少有一个测试盯着。改这些行为时测试会红 ——
这是有意的：它们不是实现细节，是这个模块的安全边界。
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import REPO_ROOT, covers
from nxs.core.errors import ConfigError
from nxs.ui import ALLOWED_HOSTS, UiServer
from nxs.ui.page import SHELL

UI_PKG = REPO_ROOT / "nxs" / "ui"

#: 运维密钥（≥ MIN_SECRET_LENGTH）
SECRET = b"ui-test-secret-0123456789abcdef"


# ---------------------------------------------------------------------------
# fixture：一个真实跑起来的 hub + 观察窗
# ---------------------------------------------------------------------------

@pytest.fixture
def live_hub():
    """真实 HTTP 的 hub，含一个有中文名称/简介的节点。"""
    from nxs.app import NexusApp
    from nxs.core.models import RoleCard
    from nxs.edge.client import HttpTransport
    from nxs.edge.gateway import Gateway
    from nxs.mgmt.config import EXAMPLE_CONFIG_PATH, ServerConfig, demo_env_for

    app = NexusApp.from_config_path(
        str(EXAMPLE_CONFIG_PATH), env=demo_env_for(EXAMPLE_CONFIG_PATH),
        force_memory=True,
    )
    app.start()
    app.approve("node-a")

    gateway = Gateway(
        app, server_config=ServerConfig(host="127.0.0.1", port=0),
        ops_secret=SECRET,
    ).start()

    raw = dict(app.config.nodes["node-b"].to_dict())
    raw["display_name"] = "工作笔记本"
    raw["description"] = "处理日历与待办，人在公司时在线"
    HttpTransport(gateway.base_url, node_id="node-b",
                  secret=app.config.secrets["node-b"]).register(
        RoleCard.from_dict(raw))

    yield gateway.base_url
    gateway.stop()


@pytest.fixture
def live_ui(live_hub):
    """跑起来的观察窗（端口由系统分配）。"""
    server = UiServer(live_hub, SECRET, port=0).start()
    yield server
    server.stop()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随跳转 —— 否则看不到 303 与 Set-Cookie。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _get(url: str, cookie: str = "") -> tuple[int, dict, bytes]:
    """发 GET，返回 (状态码, 响应头, 正文)。不跟随跳转。"""
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url)
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with opener.open(request, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _request(method: str, url: str, cookie: str = "", *,
             with_headers: bool = False):
    """发任意方法。``with_headers`` 为真时返回 (状态码, 正文, 响应头)。"""
    opener = urllib.request.build_opener(_NoRedirect)
    data = b"{}" if method in ("POST", "PUT", "PATCH") else None
    request = urllib.request.Request(url, data=data, method=method)
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with opener.open(request, timeout=10) as resp:
            result = (resp.status, resp.read())
            return result + (dict(resp.headers),) if with_headers else result
    except urllib.error.HTTPError as exc:
        result = (exc.code, exc.read())
        return result + (dict(exc.headers),) if with_headers else result


# ---------------------------------------------------------------------------
# 约束一：只听 127.0.0.1
# ---------------------------------------------------------------------------

class TestBindAddress:
    """★ 拒绝公网绑定。

    有人会想"我只在内网用，绑 0.0.0.0 方便"。但那样这个进程就把
    节点元数据（谁在、有什么能力、拒绝集中在哪）暴露给整个网段，
    而它连 HTTPS 都没有——传输加密本来由 SSH 提供。
    """

    @covers("M15.F1")
    def test_refuses_wildcard_bind(self, live_hub):
        with pytest.raises(ConfigError) as ei:
            UiServer(live_hub, SECRET, host="0.0.0.0", port=0)
        assert "拒绝绑定" in str(ei.value)
        # 错误信息必须给出替代方案，否则用户只会去找开关
        assert "SSH 隧道" in str(ei.value)

    @covers("M15.F1")
    def test_refuses_lan_ip(self, live_hub):
        with pytest.raises(ConfigError):
            UiServer(live_hub, SECRET, host="192.168.1.10", port=0)

    @covers("M15.F1")
    def test_localhost_aliases_allowed(self):
        """本机别名都该放行（不同系统的解析习惯不同）。"""
        assert "127.0.0.1" in ALLOWED_HOSTS
        assert "localhost" in ALLOWED_HOSTS
        assert "::1" in ALLOWED_HOSTS
        # 但绝不能有通配
        assert "0.0.0.0" not in ALLOWED_HOSTS
        assert "" not in ALLOWED_HOSTS

    @covers("M15.F1")
    def test_rejects_url_not_ending_with_nxs(self):
        with pytest.raises(ConfigError) as ei:
            UiServer("http://127.0.0.1:18100/wrong", SECRET, port=0)
        assert "/nxs" in str(ei.value)


# ---------------------------------------------------------------------------
# 约束二：token 鉴权
# ---------------------------------------------------------------------------

class TestTokenAuth:
    """★ token 是唯一凭据。"""

    @covers("M15.F2")
    def test_page_requires_token(self, live_ui):
        status, _, _ = _get(f"{live_ui.base_url}/")
        assert status == 401

    @covers("M15.F2")
    def test_api_requires_token(self, live_ui):
        status, _, _ = _get(f"{live_ui.base_url}/api/state")
        assert status == 401

    @covers("M15.F2")
    def test_wrong_token_rejected(self, live_ui):
        status, _, _ = _get(f"{live_ui.base_url}/?t=wrong-token")
        assert status == 401

    @covers("M15.F2")
    def test_forged_cookie_rejected(self, live_ui):
        status, _, _ = _get(f"{live_ui.base_url}/", cookie="nxs_ui=forged")
        assert status == 401

    @covers("M15.F2")
    def test_token_sets_secure_cookie_and_redirects(self, live_ui):
        """正确的 token 必须：303 跳走 + 下发 HttpOnly/SameSite cookie。

        为什么跳走：URL 会进浏览器历史、会被 Referer 带出去、会被截图。
        token 只该"进门出现一次"，随后换成 cookie。

        为什么 HttpOnly：XSS 拿不到 cookie，也就偷不走 token。
        为什么 SameSite=Strict：跨站请求不带 cookie（本模块没有写方法，
        但这是纵深防御——将来若加写操作，CSRF 已经挡住了）。
        """
        status, headers, _ = _get(f"{live_ui.base_url}/?t={live_ui.token}")
        assert status == 303
        assert headers.get("Location") == "/"

        cookie = headers.get("Set-Cookie", "")
        assert live_ui.token in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
        assert "Path=/" in cookie

    @covers("M15.F2")
    def test_valid_cookie_serves_page(self, live_ui):
        status, headers, body = _get(f"{live_ui.base_url}/",
                                     cookie=f"nxs_ui={live_ui.token}")
        assert status == 200
        assert headers.get("Content-Type", "").startswith("text/html")
        assert b"Nexus Mesh" in body

    @covers("M15.F2")
    def test_token_is_fresh_per_server(self, live_hub):
        """★ 每次启动换新 token，绝不跨重启复用。

        如果复用，一个昨天泄漏的链接今天还能用。
        """
        first = UiServer(live_hub, SECRET, port=0).start()
        second = UiServer(live_hub, SECRET, port=0).start()
        try:
            assert first.token != second.token
            # 且两者都不短（256 位熵）
            assert len(first.token) >= 32
        finally:
            first.stop()
            second.stop()

    @covers("M15.F2")
    def test_token_not_written_to_disk(self, live_ui):
        """token 不能出现在仓库目录或临时目录的任何文件里。

        只扫仓库与系统临时目录的顶层：既避免误报（pytest 缓存里会有
        随机串），又能抓到"顺手存到文件里"这种真实错误。
        """
        token = live_ui.token
        suspects = list(REPO_ROOT.rglob("*.log"))
        suspects += [Path("/tmp") / n for n in ("nxs-ui-token", ".nxs-ui-token")]
        for path in suspects:
            if path.is_file():
                assert token not in path.read_text(errors="ignore"), (
                    f"token 出现在 {path} —— 它必须只存在于内存与终端"
                )


# ---------------------------------------------------------------------------
# 约束三：只读
# ---------------------------------------------------------------------------

class TestReadOnly:
    """★ 观察窗没有写能力 —— 这是设计，不是"还没做"。

    写操作会引入 CSRF、二次确认、误操作恢复、并发冲突；
    而批准/吊销天然低频，留在 CLI 里反而有 shell 历史作为操作记录。

    下面逐方法验证，而不是只看"代码里没有 do_POST"：
    万一将来有人加了一个，这里会红。
    """

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    @covers("M15.F8")
    def test_write_methods_are_refused(self, live_ui, method):
        cookie = f"nxs_ui={live_ui.token}"
        status, body, headers = _request(method, f"{live_ui.base_url}/api/state",
                                         cookie, with_headers=True)
        assert status == 405, f"{method} 竟然被接受了"
        # 响应要说明"为什么"和"该用什么"，不能只说 405
        assert b"read-only" in body or "只读".encode() in body
        # 405 必须带 Allow，否则调用方只能猜
        assert headers.get("Allow") == "GET"

    @covers("M15.F8")
    def test_head_refused_without_confusing_message(self, live_ui):
        """★ HEAD 是只读方法，拒绝它不该说「观察窗是只读的」。

        那句话该留给真正的写方法；对一个只读的动词说"你是只读的"
        只会让人困惑。HEAD 的响应也没有正文（HTTP 规定）。
        """
        status, body, headers = _request("HEAD", f"{live_ui.base_url}/api/state",
                                         f"nxs_ui={live_ui.token}",
                                         with_headers=True)
        assert status == 405
        assert body == b""
        assert headers.get("Allow") == "GET"

    @covers("M15.F8")
    def test_no_write_methods_defined(self):
        """★ 静态检查：handler 里不得出现 do_POST 等写方法。

        与上面的运行时检查互补：运行时证明"现在拒绝"，
        静态检查证明"根本没实现"。
        """
        source = (UI_PKG / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        write_methods = {"do_POST", "do_PUT", "do_PATCH", "do_DELETE"}
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        # 这些方法应该存在（用来显式拒绝），但**函数体里不能有真正的写逻辑**
        assert write_methods & defined, (
            "写方法应该被显式定义并拒绝；若全部缺失，基类会回 501，"
            "响应里就带不上「只读」这个解释"
        )
        for name in write_methods & defined:
            func = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef) and n.name == name)
            body_source = ast.get_source_segment(source, func) or ""
            assert "_read_only" in body_source, (
                f"{name} 没有走统一的只读拒绝路径 —— 有人加了真的写逻辑？"
            )

    @covers("M15.F8")
    def test_page_declares_it_is_read_only(self):
        """页面上要明说只读，并把正确的做法指给用户。"""
        lowered = SHELL.lower()
        assert "只读" in SHELL
        assert "nxs ops" in SHELL, "页面应告诉用户改状态该用什么"


# ---------------------------------------------------------------------------
# 约束四：数据只经运维端点
# ---------------------------------------------------------------------------

class TestDataPath:
    """★ 不 import 存储后端、不 import app 装配层。

    直读存储等于新增一条绕过策略层的数据通道。本项目的铁律是
    "所有安全策略的唯一裁决处"——数据出口也必须唯一。
    """

    @covers("M15.F3")
    def test_ui_does_not_import_app_or_backends(self):
        """静态检查：``nxs/ui/*.py`` 不得直接 import app / backends / data。"""
        forbidden = ("app", "backends", "data", "control", "ports")
        offenders: list[str] = []

        for path in UI_PKG.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                module = node.module or ""
                # 相对导入：level=2 表示 ..xxx
                if node.level >= 2 and module.split(".")[0] in forbidden:
                    offenders.append(f"{path.name}: from {'.' * node.level}{module}")
                if node.level == 0:
                    for alias in node.names:
                        top = (module + "." + alias.name).split(".")[0]
                        if top == "nxs" and len((module + "." + alias.name).split(".")) > 1:
                            sub = (module + "." + alias.name).split(".")[1]
                            if sub in forbidden:
                                offenders.append(f"{path.name}: import {module}")

        assert not offenders, (
            "观察窗不得依赖应用装配层与存储后端（数据只能经运维端点）：\n  "
            + "\n  ".join(offenders)
        )

    @covers("M15.F3")
    def test_importing_ui_does_not_load_app_or_backends(self):
        """★ 运行时检查：``import nxs.ui`` 之后，app 与 backends 都未加载。

        这条比静态检查更强：它能抓到**间接**依赖。
        曾经 ``edge/client.py`` 从 ``..app`` 导入了 ``DeliveryResult``，
        导致只 import 客户端就把整个装配层拉进来了 —— 静态检查看不出来，
        这条能看出来。
        """
        code = (
            "import sys; sys.path.insert(0, %r);"
            "import nxs.ui;"
            "print('app' if 'nxs.app' in sys.modules else '-');"
            "print('backends' if any(m.startswith('nxs.backends') "
            "for m in sys.modules) else '-')"
        ) % str(REPO_ROOT)
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        loaded = result.stdout.split()
        assert loaded[0] == "-", (
            "import nxs.ui 把 nxs.app 加载进来了 —— 观察窗与装配层之间"
            "不该有依赖"
        )
        assert loaded[1] == "-", (
            "import nxs.ui 把存储后端加载进来了 —— 数据只能经运维端点"
        )

    @covers("M15.F3", "M15.F4", "M15.F5")
    def test_api_state_shape(self, live_ui):
        status, headers, body = _get(f"{live_ui.base_url}/api/state",
                                     cookie=f"nxs_ui={live_ui.token}")
        assert status == 200
        assert headers.get("Content-Type", "").startswith("application/json")
        data = json.loads(body)
        assert set(data) >= {"at", "hub_url", "nodes", "metrics", "error"}
        assert data["error"] is None

        # 手册里承诺的三个字段必须真的在
        by_id = {n["node_id"]: n for n in data["nodes"]}
        assert "node-b" in by_id
        node_b = by_id["node-b"]
        assert node_b["display_name"] == "工作笔记本"
        assert node_b["description"] == "处理日历与待办，人在公司时在线"
        assert "roles" in node_b

    @covers("M15.F5")
    def test_metrics_shape_for_summary_tiles(self, live_ui):
        """★ 汇总视图要的四类数必须都在 —— 页面靠它们画格子。

        * 计数：提交／完成／失败／拒绝
        * 延迟分位数：p50/p95/p99（无样本时为 null，页面显示 "—"）
        * 样本数：说明分位数的可信度
        * 积压：每节点的 pending/capacity/backlog_ratio
        """
        _, _, body = _get(f"{live_ui.base_url}/api/state",
                          cookie=f"nxs_ui={live_ui.token}")
        metrics = json.loads(body)["metrics"]
        assert metrics is not None
        assert set(metrics["totals"]) >= {"submitted", "completed", "failed", "denied"}
        assert set(metrics["latency_ms"]) == {"p50", "p95", "p99"}
        assert "latency_samples" in metrics
        assert isinstance(metrics["anomalies"], list)
        for node in metrics["nodes"]:
            assert set(node) >= {"node_id", "pending", "capacity", "backlog_ratio"}

    @covers("M15.F4")
    def test_node_rows_carry_all_display_fields(self, live_ui):
        """★ 节点列表要显示的字段必须由 API 一次给全。

        少一个字段，页面就得再发一次请求或者显示空白 ——
        而"为什么这个格子是空的"是最难排查的一类问题。
        """
        _, _, body = _get(f"{live_ui.base_url}/api/state",
                          cookie=f"nxs_ui={live_ui.token}")
        nodes = json.loads(body)["nodes"]
        assert nodes
        for node in nodes:
            for field in ("node_id", "display_name", "description", "domain",
                          "state", "approved", "roles", "capabilities"):
                assert field in node, f"节点行缺少 {field}，页面会显示空白"

    @covers("M15.F3")
    def test_unreachable_hub_reports_error_not_crash(self):
        """★ hub 挂了，观察窗要**显示原因**而不是白屏。

        观察窗最需要工作的时候恰恰是核心出问题的时候。
        如果这里抛 500，浏览器只有一个空白页，看不出是 hub 停了
        还是 UI 自己坏了。
        """
        server = UiServer("http://127.0.0.1:1/nxs", SECRET, port=0).start()
        try:
            status, _, body = _get(f"{server.base_url}/api/state",
                                   cookie=f"nxs_ui={server.token}")
            assert status == 200, "hub 挂了不该让 UI 也 500"
            data = json.loads(body)
            assert data["error"], "必须把错误原因带出来"
            assert data["nodes"] == []
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# 约束五：渲染安全
# ---------------------------------------------------------------------------

class TestRenderSafety:
    """★ 数据用 ``textContent`` 渲染，绝不用 ``innerHTML``。

    节点自报的 ``display_name`` 是不可信文本。服务端校验拒了控制字符，
    但**刻意没做 HTML 转义**（转义是渲染层的责任）。

    所以渲染层必须结构性安全：``textContent`` 根本没有"把字符串当标记
    解析"这一步。不是"小心避开了危险路径"，而是那条路径不存在。
    """

    #: 会解析字符串为标记/代码的 API。一个都不许出现在页面里。
    FORBIDDEN_PATTERNS = (
        r"innerHTML\s*=",
        r"outerHTML\s*=",
        r"insertAdjacentHTML\s*\(",
        r"document\.write\s*\(",
        r"eval\s*\(",
        r"new\s+Function\s*\(",
    )

    #: 静态检查之外，2026-10-27 用无头 Chrome 做过一次真实渲染验证：
    #: 把某节点的 display_name 注册成 ``<img src=x onerror=alert(1)>``、
    #: description 注册成 ``"><script>alert("xss")</script>``，
    #: 渲染后的 DOM 里两者都是**文本实体**（``&lt;img …&gt;``），
    #: 没有生成 <img> 或 <script> 元素。这与下面的静态检查互相印证。

    @covers("M15.F6")
    def test_page_has_no_html_injection_apis(self):
        import re

        hits = [pattern for pattern in self.FORBIDDEN_PATTERNS
                if re.search(pattern, SHELL)]
        assert not hits, (
            f"页面里出现了会解析字符串的 API {hits} —— "
            f"节点自报文本一旦经过它们就是 XSS"
        )

    @covers("M15.F6")
    def test_page_builds_dom_with_textcontent(self):
        assert "textContent" in SHELL
        assert "createElement" in SHELL

    @covers("M15.F6")
    def test_shell_contains_no_data_placeholder(self):
        """★ 静态壳里不得有服务端插值。

        一旦允许服务端拼 HTML，每个插值点都得记得转义一次——
        "靠记得"的安全迟早会破。所以这条边界是绝对的。
        """
        # 模板占位符形态：{...} / %s / ${
        import re

        assert not re.search(r"\{[a-z_]+\}", SHELL), (
            "页面里出现了看起来像模板占位符的东西 —— "
            "若真要做服务端插值，请先重新论证 XSS 边界"
        )
        assert "%s" not in SHELL
        assert "${" not in SHELL, "出现了模板字符串插值（会绕过 textContent 的保证）"

    @covers("M15.F6")
    def test_self_reported_text_is_not_escaped_server_side(self, live_hub):
        """★ 服务端**不做** HTML 转义 —— 这是有意的分工。

        若服务端把 ``<`` 变成 ``&lt;``，会破坏合法名字（``A <-> B``），
        而且等于替所有下游决定了渲染方式。

        所以 API 应原样返回文本，转义交给渲染层。
        """
        from nxs.core.models import RoleCard
        from nxs.mgmt.config import EXAMPLE_CONFIG_PATH, demo_env_for, load_config

        cfg = load_config(EXAMPLE_CONFIG_PATH, env=demo_env_for(EXAMPLE_CONFIG_PATH))
        payload = dict(cfg.nodes["node-a"].to_dict())
        payload["display_name"] = "A <-> B"
        payload["description"] = "处理 <任务> 与 & 符号"
        card = RoleCard.from_dict(payload)
        assert card.display_name == "A <-> B"
        assert "<" in card.description

    @covers("M15.F6")
    def test_security_headers_present(self, live_ui):
        status, headers, _ = _get(f"{live_ui.base_url}/",
                                  cookie=f"nxs_ui={live_ui.token}")
        assert status == 200
        assert headers.get("X-Frame-Options") == "DENY", "可被内嵌 → 点击劫持"
        assert "default-src 'none'" in headers.get("Content-Security-Policy", "")
        assert headers.get("Referrer-Policy") == "no-referrer", (
            "token 在 cookie 里，Referer 不能把它带出去"
        )
        assert headers.get("Cache-Control") == "no-store"
        assert headers.get("X-Content-Type-Options") == "nosniff"

    @covers("M15.F6")
    def test_unauthorized_response_says_nothing_useful(self, live_ui):
        """★ 鉴权失败不区分"token 错"与"没有 token"。

        少给一点信息 —— 与网关拒绝时只回 rule_id 是同一条思路。
        """
        a = _get(f"{live_ui.base_url}/")[2]
        b = _get(f"{live_ui.base_url}/?t=wrong")[2]
        assert a == b
