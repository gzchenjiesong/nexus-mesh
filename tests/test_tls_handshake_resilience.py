"""网关的 TLS 握手韧性 —— 半开连接不得冻结服务。

## ⚠ 这一节来自一次 13 天的生产事故（2026-11-13 修复）

### 现象

```
systemctl:  nexus-mesh active            ← systemd 认为它活着
18800:      LISTEN，Recv-Q=6             ← 6 个连接排队，无进程 accept
HTTPS 探测:  HTTP 000，5 秒超时
日志:       止于 2026-10-31 18:19:59（13 天无输出）
进程:       CPU 0.0%，Threads=2
NRestarts:  0
```

主线程**一直在正常跑** 5 秒维护循环（`py-spy` 抓到 `ticks: 241834`），
所以 systemd 完全看不出异常。卡住的是 gateway 线程。

### 根因

`gateway.py` 把**监听套接字**整体 wrap 成 SSL：

```python
httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
```

而 Python 的 `SSLSocket.accept()` **内部会同步做 TLS 握手**
（`do_handshake_on_connect` 默认 `True`），且**继承的 socket 没有超时**：

```
py-spy 抓到的栈：
  Gateway._serve_locked (gateway.py:428)
    BaseServer.serve_forever
      SSLSocket.accept                    ← accept 内部
        SSLContext.wrap_socket            do_handshake_on_connect: True
          SSLSocket.do_handshake
            timeout: None                 ← ★ 无超时
          addr: ("14.212.83.239", 32509)
```

于是：**一个 TCP 连接建立但不发 ClientHello → 永久阻塞 → 整个网关冻结。**

### 为什么这必须是「默认行为」的问题，而不是「被攻击」

用户的直觉是对的：先分清「有人攻击」还是「我们实现有问题」。
查证结果是**外部 IP 发起**（`14.212.83.239`，中国电信，从未通过认证，
不扫其他端口，不是高频扫描器）。客户端侧也已排除 ——
`nxs/edge/client.py` 全用高层 `urlopen`，TCP 与 TLS 由 urllib 内部连续完成，
中间没有用户代码，**不产生半开连接**。

**但结论不是「所以不用修」。** 真正的问题是：

> 为什么一个不需要任何凭据、不需要任何协议的静默 TCP 连接，
> 能让协调节点冻结 13 天，而 systemd 认为它健康？

换成客户端 bug、机房网络抖动、某个忘记关闭的 `nc` 窗口 —— 结果完全一样。
**这是「默认行为脆弱」。** 任何监听公网端口的服务都必须假设会收到
畸形连接与半开连接，这是行业常态而非假设。

### 修法要点（三条，缺一不可）

1. **监听套接字不 wrap** —— 它不该带 TLS 状态
2. **握手必须可超时** —— 否则单连接冻结（这是本条的核心）
3. **握手失败不能杀掉 accept 循环** —— 丢那一条，继续下一条
"""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import covers


# ---------------------------------------------------------------------------
# 工具：造一张自签证书
# ---------------------------------------------------------------------------

def make_self_signed(tmp: Path) -> tuple[Path, Path]:
    """用 openssl 造一套自签证书。

    ⚠ **刻意用 openssl 命令行而不是 cryptography**：本项目运行时依赖
    只有 PyYAML，为测试引入密码学库会破掉那条纪律（``deploy/gen-tls.sh``
    也是同样的取舍）。openssl 是部署环境的既有依赖。
    """
    if shutil.which("openssl") is None:      # pragma: no cover
        pytest.skip("openssl 不可用，跳过 TLS 测试")

    cert = tmp / "server.pem"
    key = tmp / "server.key"
    result = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048",
         "-keyout", str(key), "-out", str(cert),
         "-days", "1", "-nodes", "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:               # pragma: no cover
        pytest.skip(f"openssl 生成证书失败: {result.stderr[:200]}")
    return cert, key


def silent_tcp_connection(port: int) -> socket.socket:
    """建立一个 TCP 连接，但**故意不发 TLS ClientHello**。

    这就是冻结生产的那个形态：TCP 三次握手完成，应用层一个字节都不发。
    扫描器的 banner 探测、崩溃客户端留下的孤儿连接、忘记关闭的 `nc`，
    都会产生它。
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.settimeout(10)
    return sock


def https_get(port: int, timeout: float = 5.0) -> int:
    """发一个真正的 HTTPS 请求，返回状态码。失败则抛异常。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # 自签证书，不校验链
    req = urllib.request.Request(f"https://127.0.0.1:{port}/nxs/health")
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.status


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

class TestSilentTlsConnectionDoesNotFreezeGateway:
    """★★★ 一个不发 ClientHello 的 TCP 连接，**不得**冻结网关。

    ⚠ 这是那场 13 天事故的直接回归测试。
    """

    @pytest.fixture
    def tls_gateway(self):
        """起一个真的 TLS 网关（自签证书 + 系统分配的端口）。"""
        from conftest import EXAMPLE_CONFIG, demo_env
        from nxs.app import NexusApp
        from nxs.edge.gateway import Gateway
        from nxs.mgmt.config import ServerConfig

        tmp = Path(tempfile.mkdtemp(prefix="nxs-tls-"))
        cert, key = make_self_signed(tmp)

        app = NexusApp.from_config_path(
            str(EXAMPLE_CONFIG), env=demo_env(), force_memory=True)
        app.start()
        gw = Gateway(
            app,
            server_config=ServerConfig(
                host="127.0.0.1", port=0,
                tls_cert=str(cert), tls_key=str(key),
            ),
        ).start()
        try:
            yield gw, gw.port
        finally:
            gw.stop()

    @covers("M1.F19")
    def test_first_request_after_silent_connection_succeeds_fast(self, tls_gateway):
        """★★★ 静默连接存在时，**第一次**请求就要成功，且不能慢。

        ⚠ 这条比「最终能成功」严格得多，而它对应的是真实的生产后果。

        修复过程中我先做了一版「在 get_request 里设超时」的改法。
        实测结果是：

            ① 正常 HTTPS            → 200
            ② 建 TCP 不发 ClientHello
            ③ **第一次**正常请求    → 失败，卡满 10 秒
            ④ 第二次正常请求        → 200

        **超时把「永久冻结」变成了「每次卡 10 秒」，问题没解决。**
        因为调用方的超时往往比 10 秒还短 —— 对发起方来说，
        服务看起来仍然是坏的，只是会在 10 秒后自愈。

        根因是位置不对：`get_request()` 在 serve_forever 的**单线程循环**
        里被调用，只要它耗时，整个循环就在等。握手必须移到工作线程
        （`process_request`），accept 循环才能立刻返回。

        所以这条断言同时卡两个点：

            · 状态 200   —— 不能失败
            · 耗时 < 2s  —— 不能靠"等超时"过关
        """
        _gw, port = tls_gateway
        assert https_get(port) == 200, "前置条件失败"

        silent = silent_tcp_connection(port)
        try:
            time.sleep(0.3)
            started = time.time()
            status = https_get(port, timeout=8)
            elapsed = time.time() - started
            assert status == 200, f"第一次请求失败（{status}）"
            assert elapsed < 2.0, (
                f"第一次请求耗时 {elapsed:.1f}s —— 它在等半开连接的握手超时。\n"
                "  说明握手仍在 accept 循环里做，只是加了超时。\n"
                "  正确做法：握手移到工作线程（process_request），\n"
                "  让 accept 循环拿完裸 socket 立刻返回。"
            )
        finally:
            silent.close()

    @covers("M1.F19")
    def test_silent_connection_does_not_block_others(self, tls_gateway):
        """★★★ 核心断言：静默连接存在时，正常 HTTPS 仍能完成。

        修复前的行为（已实测复现）：

            ① 正常 HTTPS           → 200
            ② 建 TCP 不发 ClientHello
            ③ 再来正常 HTTPS       → TimeoutError: _ssl.c:1015

        修复后：③ 应当照常 200。
        """
        _gw, port = tls_gateway

        # ① 先确认它本来是好的
        assert https_get(port) == 200, "前置条件失败：网关本身不通"

        # ② 制造半开连接（就是冻结生产的那个形态）
        silent = silent_tcp_connection(port)
        try:
            time.sleep(0.3)          # 让服务端确实执行到握手

            # ③ 关键断言
            status = https_get(port, timeout=8)
            assert status == 200, (
                f"静默连接存在时 HTTPS 返回 {status}，应当仍为 200。\n"
                "  若这里超时/失败，说明握手没有超时保护，\n"
                "  单个 TCP 连接即可冻结整个网关 —— 那正是 13 天事故的成因。"
            )
        finally:
            silent.close()

    @covers("M1.F19")
    def test_many_silent_connections_do_not_block(self, tls_gateway):
        """★ 多个静默连接也不该阻塞 —— 防止「只挡住一个」的伪修复。

        只处理一个连接就够了吗？不一定：如果实现是"遇到坏连接就
        跳出循环再重进"，多个仍会累积。这条用 5 个一起压。
        """
        _gw, port = tls_gateway
        assert https_get(port) == 200

        silent = [silent_tcp_connection(port) for _ in range(5)]
        try:
            time.sleep(0.3)
            assert https_get(port, timeout=8) == 200, (
                "5 个静默连接就把网关挡死了 —— 修复只处理了单个"
            )
        finally:
            for s in silent:
                s.close()

    @covers("M1.F19")
    def test_silent_connection_is_reclaimed(self, tls_gateway):
        """★ 静默连接应被**回收**，不是永远挂着。

        握手超时的意义不只是"不阻塞" —— 还要让那条连接最终消失，
        否则半开连接会慢性累积到 fd 耗尽。
        """
        gw, port = tls_gateway
        silent = silent_tcp_connection(port)

        # 握手超时后服务端应主动关闭 → 客户端读会得到 EOF/错误
        claimed = False
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    data = silent.recv(1024)
                    if data == b"":
                        claimed = True       # 对端关闭
                        break
                except (ConnectionResetError, socket.timeout):
                    claimed = True
                    break
                time.sleep(0.5)
        finally:
            silent.close()

        assert claimed, (
            "静默连接 20 秒内既没被关闭也没超时 —— 它可能一直挂在服务端。\n"
            "  半开连接会累积，最终耗尽 fd。握手超时必须真的生效。"
        )

    @covers("M1.F19")
    def test_handshake_failure_does_not_kill_accept_loop(self, tls_gateway):
        """★ 握手失败（垃圾数据）只丢该连接，不能让 accept 循环退出。

        一个往 TLS 端口发明文 HTTP 的客户端、一个用错协议的扫描器，
        都会触发握手失败。若那让 serve_forever 抛异常退出，
        服务就永久死了 —— 那是比阻塞更糟的失败模式。
        """
        _gw, port = tls_gateway
        assert https_get(port) == 200

        # 往 TLS 端口发明文 —— 握手必然失败
        bad = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            bad.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            time.sleep(0.5)
            try:
                bad.recv(1024)       # 服务端应发 TLS alert 或直接关
            except (ConnectionResetError, socket.timeout, OSError):
                pass
        finally:
            bad.close()

        time.sleep(0.3)
        assert https_get(port, timeout=8) == 200, (
            "握手失败之后网关就不响应了 —— accept 循环被异常杀掉了。\n"
            "  这比阻塞更糟：服务永久死亡，而日志里只有一条 traceback。"
        )

    @covers("M1.F19")
    def test_plain_http_still_survives_after_all_attacks(self, tls_gateway):
        """★ 兜底：上面所有攻击做完，服务仍能连续处理多个正常请求。

        单次成功可能是巧合（比如恰好赶上一次超时释放）。
        连续 3 次正常请求都成功，才说明循环真的健康。
        """
        _gw, port = tls_gateway

        silent = [silent_tcp_connection(port) for _ in range(3)]
        bad = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            bad.sendall(b"\x00\x01\x02\x03 plain garbage\r\n\r\n")
            time.sleep(0.3)
            for i in range(3):
                assert https_get(port, timeout=8) == 200, (
                    f"第 {i+1} 个正常请求失败 —— 循环已受损"
                )
        finally:
            for s in silent:
                s.close()
            bad.close()


class TestGatewayStillWorksOverPlainHttp:
    """★ 修复不能破坏明文模式（不配证书时的行为）。

    TLS 与明文走的是同一条 accept 路径，改 ``get_request`` 时
    很容易把明文那支弄坏 —— 而生产上 ``nxs demo`` / 同机测试都用明文。
    """

    @covers("M1.F19")
    def test_plain_http_gateway_works(self):
        from conftest import EXAMPLE_CONFIG, demo_env
        from nxs.app import NexusApp
        from nxs.edge.gateway import Gateway
        from nxs.mgmt.config import ServerConfig

        app = NexusApp.from_config_path(
            str(EXAMPLE_CONFIG), env=demo_env(), force_memory=True)
        app.start()
        gw = Gateway(app, server_config=ServerConfig(
            host="127.0.0.1", port=0)).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{gw.port}/nxs/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
        finally:
            gw.stop()
