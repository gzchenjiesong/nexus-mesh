"""M15 · 观察窗 WebUI —— 只读的系统观察进程。

设计见 ``docs/design/05-webui-design.md``。四条不可妥协的约束：

**① 只读**
本进程只实现 ``do_GET``。没有 ``do_POST`` / ``do_PUT`` / ``do_DELETE``，
不是"暂时没写"，而是**刻意不存在**。要改变系统状态请用 ``nxs ops``。
理由：写操作会引入 CSRF、二次确认、误操作恢复；而批准/吊销天然低频，
留在 CLI 里反而有 shell 历史作为操作记录。

**② 数据只经运维端点**
不 import 存储后端、不 import ``nxs.app``、不读 sqlite、不碰活的注册表。
只做一件事：用运维密钥调 hub 的 ``/nxs/ops/nodes`` 与 ``/nxs/ops/metrics``。

为什么这条不能妥协：直读存储等于**新增一条绕过策略层的数据通道**。
本项目的铁律是"所有安全策略的唯一裁决处"——数据出口也必须唯一。

**③ 只听 127.0.0.1**
不接受 ``0.0.0.0``。访问靠 SSH 隧道把端口搬过来（与 OpenViking 同一模式）。
这样不需要开防火墙端口、不需要证书 —— 传输加密由 SSH 提供。

**④ token 不落盘**
每次启动重新生成，只打印在终端。写进 HttpOnly cookie 后用于后续请求。
不做账号密码：能 SSH 登录这台机器的人本来就能 ``export NXS_OPS_SECRET``
直接 curl，给他一个密码框只是多一道他不需要的门。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..core.errors import ConfigError
from ..edge.client import OpsClient
from .page import SHELL

log = logging.getLogger(__name__)

#: 允许绑定的地址。**刻意不含 ``0.0.0.0``。**
#:
#: 有人会想"我只在内网用，绑 0.0.0.0 方便"。但那样这个进程就把
#: 节点元数据暴露给了整个网段，而它连 HTTPS 都没有（靠 SSH 提供）。
#: 想从别的机器访问，用 SSH 隧道 —— 一行命令，不用改这里。
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: cookie 名。带 token，HttpOnly + SameSite=Strict。
COOKIE_NAME = "nxs_ui"

#: token 长度（``secrets.token_urlsafe`` 的字节数）。32 字节 ≈ 256 位。
TOKEN_BYTES = 32


class UiServer:
    """只读观察窗。构造即生成 token，:meth:`start` 才监听。"""

    def __init__(self, ops_url: str, ops_secret: bytes, *,
                 host: str = "127.0.0.1", port: int = 8080,
                 refresh_hint_seconds: int = 5) -> None:
        if host not in ALLOWED_HOSTS:
            raise ConfigError(
                f"ui: 拒绝绑定 {host!r}。只允许 {sorted(ALLOWED_HOSTS)} —— "
                f"从别的机器访问请用 SSH 隧道："
                f"ssh -N -L {port}:127.0.0.1:{port} user@server"
            )
        if not ops_url.rstrip("/").endswith("/nxs"):
            raise ConfigError(f"ui: hub 地址必须以 /nxs 结尾: {ops_url!r}")

        self.ops_url = ops_url
        self.host = host
        self.port = port
        self.refresh_hint_seconds = refresh_hint_seconds
        self._ops = OpsClient(ops_url, secret=ops_secret)

        #: 每次启动重新生成，**不落盘、不跨重启复用**。
        self.token = secrets.token_urlsafe(TOKEN_BYTES)

        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def entry_url(self) -> str:
        """带 token 的入口链接。**只打印到终端，不要贴到别处。**"""
        return f"{self.base_url}/?t={self.token}"

    def start(self) -> "UiServer":
        handler = _make_handler(self)
        httpd = ThreadingHTTPServer((self.host, self.port), handler)
        # 端口传 0 时让系统分配，回填真实端口
        self.port = httpd.server_address[1]
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="nxs-ui", daemon=True,
        )
        self._thread.start()
        log.info("观察窗已启动 %s", self.base_url)
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------------
    # 数据聚合
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """拉一次节点与指标。

        **失败不抛异常**，而是把错误放进 ``error`` 字段：
        观察窗最需要工作的时候恰恰是 hub 出问题的时候。
        如果这里抛 500，浏览器只有一个空白页，看不出"是 hub 停了"
        还是"UI 自己坏了"。
        """
        out: dict[str, Any] = {
            "at": datetime.now(timezone.utc).astimezone().isoformat(
                timespec="seconds"),
            # 键名用 hub_url 而不是 hub：后者与示例配置里的节点名撞词，
            # 会让「节点名不得硬编码」的守门（C3）报红，读代码时也容易
            # 误以为这里指的是某个叫 hub 的节点。这里指的是 hub 的地址。
            "hub_url": self.ops_url,
            "nodes": [],
            "metrics": None,
            "error": None,
        }
        try:
            out["nodes"] = list(self._ops.nodes())
        except Exception as exc:  # noqa: BLE001 —— 见上面的说明
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        try:
            out["metrics"] = self._ops.metrics()
        except Exception as exc:  # noqa: BLE001
            # 节点拿到了、指标没拿到 —— 仍然把节点显示出来，只标指标的错
            out["error"] = f"metrics: {type(exc).__name__}: {exc}"
        return out


# ---------------------------------------------------------------------------
# 请求处理
# ---------------------------------------------------------------------------

def _make_handler(server: UiServer) -> type[BaseHTTPRequestHandler]:
    """生成 handler 类。用闭包把 server 传进去，避免全局状态。"""

    class Handler(BaseHTTPRequestHandler):
        #: 版本号不回显 Python 版本（少给一点指纹）
        server_version = "nxs-ui"
        sys_version = ""

        # ---- 输出辅助 --------------------------------------------------

        def _send_bytes(self, status: int, body: bytes, *,
                        content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # 观察窗不该被任何页面内嵌（防点击劫持）
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            # 这里没有外链资源，禁止一切外部加载是零成本的收紧
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'; "
                "img-src 'none'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'none'",
            )
            # token 在 cookie 里，绝不能被 Referer 带到别处
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, body, content_type="application/json; charset=utf-8")

        def _send_text(self, status: int, text: str) -> None:
            self._send_bytes(status, text.encode("utf-8"),
                             content_type="text/plain; charset=utf-8")

        def _redirect(self, location: str, *, cookie: str | None = None) -> None:
            extra = {"Location": location}
            if cookie:
                extra["Set-Cookie"] = cookie
            # 303：把 POST 语义之外的一切都变成 GET，且**换掉 URL**
            # （URL 里的 token 不能留在地址栏/历史里）
            self._send_bytes(HTTPStatus.SEE_OTHER, b"", content_type="text/plain",
                             extra=extra)

        # ---- 鉴权 ------------------------------------------------------

        def _cookie_token(self) -> str:
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE_NAME:
                    return value.strip()
            return ""

        @staticmethod
        def _same(a: str, b: str) -> bool:
            # 常数时间比较：token 是唯一凭据，不该给计时侧信道
            return secrets.compare_digest(a, b)

        def _authorized(self) -> bool:
            return self._same(self._cookie_token(), server.token)

        # ---- 路由（只有 GET） -------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的约定
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            # ① 带 token 的入口：换 cookie 后跳走，让 URL 恢复干净
            query = parse_qs(parsed.query)
            presented = (query.get("t") or [""])[0]
            if presented:
                if not self._same(presented, server.token):
                    # 不区分"token 错"与"没有 token"，少给一点信息
                    self._send_text(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
                    return
                cookie = (
                    f"{COOKIE_NAME}={server.token}; HttpOnly; SameSite=Strict; "
                    f"Path=/"
                )
                self._redirect("/", cookie=cookie)
                return

            # ② 其余一律要求 cookie
            if not self._authorized():
                self._send_text(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
                return

            if path == "/":
                self._send_bytes(HTTPStatus.OK, SHELL.encode("utf-8"),
                                 content_type="text/html; charset=utf-8")
                return
            if path == "/api/state":
                self._send_json(HTTPStatus.OK, server.snapshot())
                return
            self._send_text(HTTPStatus.NOT_FOUND, "not found\n")

        # ---- 显式拒绝写方法 ---------------------------------------------
        #
        # 不写 `do_POST = do_PUT = ... = 405` 那种一行赋值：那样
        # "支持哪些方法"要靠读代码推断。这里逐个显式声明，
        # 并且**每个都把方法名写进响应**，便于对着日志排查。

        def _read_only(self) -> None:
            """405 + ``Allow: GET``。

            带 ``Allow`` 不是形式主义：HTTP 规定 405 必须说明允许哪些方法，
            否则调用方只能猜。而这里"允许什么"正是设计的一部分。
            """
            body = (f"{self.command} not allowed: 观察窗是只读的。"
                    f"要改变状态请用 nxs ops。\n").encode("utf-8")
            self._send_bytes(HTTPStatus.METHOD_NOT_ALLOWED, body,
                             content_type="text/plain; charset=utf-8",
                             extra={"Allow": "GET"})

        def do_POST(self) -> None:      # noqa: N802
            self._read_only()

        def do_PUT(self) -> None:       # noqa: N802
            self._read_only()

        def do_PATCH(self) -> None:     # noqa: N802
            self._read_only()

        def do_DELETE(self) -> None:    # noqa: N802
            self._read_only()

        def do_HEAD(self) -> None:      # noqa: N802
            """HEAD **不是**写方法，但也没实现。

            给它单独一条路径，因为用「观察窗是只读的」来拒绝一个
            语义上只读的方法会让人困惑——那条消息该留给真正的写方法。
            """
            self._send_bytes(HTTPStatus.METHOD_NOT_ALLOWED, b"",
                             content_type="text/plain; charset=utf-8",
                             extra={"Allow": "GET"})

        # ---- 日志 ------------------------------------------------------

        def log_message(self, fmt: str, *args: Any) -> None:
            # 默认实现往 stderr 打每一行请求。5 秒轮询会刷满终端，
            # 反而盖住启动时那行**真正重要**的 token 链接。
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------

def serve_ui(ops_url: str, ops_secret: bytes, *, host: str = "127.0.0.1",
             port: int = 8080) -> tuple[UiServer, str]:
    """启动并返回 ``(server, entry_url)``。调用方负责打印 entry_url。"""
    server = UiServer(ops_url, ops_secret, host=host, port=port).start()
    return server, server.entry_url
