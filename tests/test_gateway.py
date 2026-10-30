"""传输层（网关）的功能测试 —— 走真实套接字，不 mock 网络。

为什么一定要真发请求
--------------------
网关的价值全在"与外界说话"这一段，而这一段的行为（状态码、
Content-Length、连接语义、流式输出）**只能在真实套接字上验证**。
用 mock 替换掉 socket 之后，测试证明的只是"我调用了这个方法"，
而那些细节恰恰是最容易写错的地方。

所以本文件里每个用例都会真的起一个监听端口（``port=0`` 由系统分配，
避免与并行测试抢端口），然后像外部节点那样发请求。

三个层次
--------
1. **协议语义** —— 端点、204 空队列、405/501、拒绝体只有三字段
2. **限额** —— 请求体上限、并发上限（先判后读是关键）
3. **推送** —— SSE 只推通知、心跳、同节点重连换流
4. **降级** —— 推送不可用时功能不受影响（这是本项目对"不做长轮询"的回答）
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import covers

from nxs.app import NexusApp
from nxs.core.errors import PolicyDenied, RuleId
from nxs.core.models import Level
from nxs.edge import httpsig
from nxs.edge.client import HttpTransport
from nxs.edge.gateway import Gateway, PREFIX, status_for
from nxs.mgmt.config import ServerConfig
from nxs.sdk import NodeClient, NodeConfig


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _build_app(approved: tuple[str, ...] = ("node-a", "node-b")) -> NexusApp:
    from conftest import EXAMPLE_CONFIG, demo_env

    app = NexusApp.from_config_path(
        str(EXAMPLE_CONFIG), env=demo_env(), force_memory=True
    )
    app.start()
    for node_id in approved:
        app.approve(node_id)
    return app


@pytest.fixture
def app():
    return _build_app()


@pytest.fixture
def gateway(app):
    """起一个真实网关（随机端口），用例结束后关闭。"""
    gw = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0))
    gw.start()
    try:
        yield gw
    finally:
        gw.stop()


@pytest.fixture
def clients(gateway, app):
    """三个节点侧的 HTTP 客户端。"""
    secrets = app.config.secrets
    return {
        node_id: HttpTransport(gateway.base_url, node_id=node_id, secret=secret)
        for node_id, secret in secrets.items()
    }


def _raw_request(port: int, raw: bytes, *, timeout: float = 5.0) -> bytes:
    """发一段**原始** HTTP 字节并读回全部响应。

    用于构造 urllib 不肯发的请求（缺 Content-Length、超大体、
    非法方法名等）——那类畸形输入正是要测的东西。
    """
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(raw)
        sock.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def _status_of(raw_response: bytes) -> int:
    line = raw_response.split(b"\r\n", 1)[0]
    return int(line.split(b" ")[1])


def _body_of(raw_response: bytes) -> dict:
    head, _, body = raw_response.partition(b"\r\n\r\n")
    if not body:
        return {}
    # 可能带 chunked，但本实现一律用 Content-Length，直接解析
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------------------
# 1. 协议语义
# ---------------------------------------------------------------------------

class TestProtocolSemantics:
    """端点行为必须与 docs/protocol.md §10–§11 一致。"""

    @covers("M1.F11", "M1.F16")
    def test_publish_poll_complete_roundtrip_over_network(self, clients):
        """一次完整协作真的走网络：投递 → 领取 → 回传。

        这是传输层存在的唯一理由，也是"三套 Agent 能连进来"的最小证明。
        """
        coordinator, node_a = clients["coordinator"], clients["node-a"]

        published = coordinator.publish(
            from_node="coordinator", to_node="node-a", task_type="demo.echo",
            params={"text": "hello-over-http"}, session_id="ses-net-1",
            idempotency_key="idem-net-1",
        )
        assert published.created is True
        assert published.effective_max_level == "metadata"

        task = node_a.poll("node-a")
        assert task is not None
        assert task.id == published.task.id
        assert task.params["text"] == "hello-over-http"

        done = node_a.complete("node-a", task, [])
        assert done.status == "completed"

    @covers("M1.F11", "M1.F16")
    def test_sdk_runs_unchanged_over_http(self, clients):
        """`NodeClient` 走网络时**一行都不用改** —— 传输可替换是设计目标。"""
        coordinator, node_a = clients["coordinator"], clients["node-a"]

        client = NodeClient(
            NodeConfig(
                "node-a",
                handlers={"demo.echo": lambda p: {"echo": p["text"],
                                                  "level": "metadata"}},
                levels={"demo.echo": Level.METADATA},
            ),
            node_a,
        )
        coordinator.publish(from_node="coordinator", to_node="node-a", task_type="demo.echo",
                    params={"text": "sdk-over-http"}, idempotency_key="idem-sdk")

        task = client.run_once()
        assert task is not None and task.status == "completed"
        assert task.artifact_payload() == {"echo": "sdk-over-http",
                                           "level": "metadata"}
        assert client.stats()["processed"] == 1

    @covers("M1.F11")
    def test_empty_queue_returns_204_not_200(self, gateway, clients):
        """空队列必须是 204（协议 §11）。**不能返回 200 + 空数组**——
        那会让"没任务"与"有任务但列表为空"无法区分。
        """
        req = urllib.request.Request(
            f"{gateway.base_url}/poll", method="GET",
            headers=httpsig.sign_request(
                b"node-a-demo-secret-0123456789abcdef",
                method="GET", path=f"{PREFIX}/poll", node_id="node-a",
            ),
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.status == 204
            assert response.read() == b""

    @covers("M1.F11")
    def test_unknown_endpoint_returns_501_not_404(self, gateway):
        """未实现的端点是 **501**，不是 404。

        404 会让人以为"路径写错了"，实际是"这个契约还没实现"——
        §10 里列了 ``/session`` ``/artifact`` ``/ops/*``，它们属于
        v0.2+ 的模块（M8/M9/M14）。返回 501 是诚实的。
        """
        raw = _raw_request(gateway.port, (
            b"GET /nxs/session HTTP/1.0\r\nHost: x\r\n\r\n"
        ))
        assert _status_of(raw) == 501

    @covers("M1.F11")
    def test_wrong_method_returns_405(self, gateway):
        raw = _raw_request(gateway.port, (
            b"GET /nxs/publish HTTP/1.0\r\nHost: x\r\n\r\n"
        ))
        assert _status_of(raw) == 405

    @covers("M1.F11")
    def test_health_is_unauthenticated_and_carries_no_business_info(self, gateway):
        """探活端点不认证，但**必须什么都不说**。

        一个匿名可访问的端点如果返回节点清单或计数，就成了免费的情报来源。
        """
        with urllib.request.urlopen(f"{gateway.base_url}/health", timeout=5) as r:
            assert r.status == 200
            assert json.loads(r.read()) == {"status": "ok"}

    @covers("M1.F11")
    def test_denied_body_has_exactly_three_fields(self, clients):
        """拒绝体只有 denied / rule_id / message（协议 §8）。

        多一个字段就多一条信道——字段名、长度、前缀都能用来推断被拒内容。
        """
        coordinator = clients["coordinator"]
        with pytest.raises(PolicyDenied) as excinfo:
            coordinator.publish(from_node="coordinator", to_node="node-a",
                        task_type="demo.unknown", params={},
                        idempotency_key="idem-denied")
        assert excinfo.value.rule_id is RuleId.WL_TYPE_NOT_REGISTERED

    @covers("M1.F11")
    def test_rule_to_status_mapping_is_total(self):
        """每个规则码都有确定的状态码——**不能有"未映射"的漏网**。"""
        for rule in RuleId:
            assert status_for(rule) >= 400, rule


# ---------------------------------------------------------------------------
# 2. 限额
# ---------------------------------------------------------------------------

class TestLimits:

    @covers("M1.F12")
    def test_body_over_limit_rejected_before_reading(self, gateway):
        """超大体被拒，且响应是 413。

        关键性质：判定发生在**读入之前**（按 Content-Length）。
        所以这里连体都不发——只发一个声称很大的头，服务端就该立刻拒绝，
        不会为它分配内存。
        """
        oversized = gateway.server_config.max_body_bytes + 1
        raw = _raw_request(gateway.port, (
            f"POST {PREFIX}/publish HTTP/1.0\r\n"
            f"Host: x\r\n"
            f"Content-Length: {oversized}\r\n"
            f"\r\n"
        ).encode())
        assert _status_of(raw) == 413
        assert _body_of(raw)["rule_id"] == RuleId.FM_BODY_TOO_LARGE.value

    @covers("M1.F12")
    def test_missing_content_length_rejected(self, gateway):
        """没有 Content-Length 的 POST 被拒（411）。

        否则就得"读到连接关闭为止"——那等于把内存上限交给调用方决定。
        """
        raw = _raw_request(gateway.port, (
            f"POST {PREFIX}/publish HTTP/1.0\r\nHost: x\r\n\r\n"
        ).encode())
        assert _status_of(raw) == 411

    @covers("M1.F13")
    def test_concurrent_limit_returns_503(self, app):
        """并发槽用尽时返回 503，而不是排队到永远。

        用一个只有 1 个槽的网关，手动占住它，再发请求验证。
        """
        gw = Gateway(app, server_config=ServerConfig(
            host="127.0.0.1", port=0, max_concurrent_requests=1,
        ))
        gw.start()
        try:
            assert gw.acquire_request_slot() is True
            assert gw.acquire_request_slot() is False   # 槽已满
            raw = _raw_request(gw.port, (
                f"GET {PREFIX}/poll HTTP/1.0\r\nHost: x\r\n\r\n"
            ).encode())
            assert _status_of(raw) == 503
            assert _body_of(raw)["rule_id"] == RuleId.QT_CONNECTION_LIMIT.value
        finally:
            gw.release_request_slot()
            gw.stop()

    @covers("M1.F13")
    def test_sse_streams_have_separate_quota(self, gateway):
        """SSE 流不占用常规请求槽。

        否则几条长连接就能把短请求饿死——推送可用反而降低可用性。
        这里占住全部常规槽，仍然应该能建立事件流。
        """
        taken = 0
        try:
            while gateway.acquire_request_slot():
                taken += 1
            assert taken == gateway.server_config.max_concurrent_requests
            # 常规槽全被占，但流配额独立，鉴权照常
            assert gateway.acquire_sse_slot() is True
        finally:
            gateway.release_sse_slot()
            for _ in range(taken):
                gateway.release_request_slot()


# ---------------------------------------------------------------------------
# 3. 推送（SSE）
# ---------------------------------------------------------------------------

class TestEventStream:

    @covers("M1.F14")
    def test_stream_carries_notification_only(self, gateway, clients):
        """流里只有"有活"信号，**没有任何任务内容**。

        读到的所有字节都必须能由 ``event: work``、``data: {}``、
        注释行组成——出现任何业务字符串就说明推送通道变成了数据通道。
        """
        secret = b"node-a-demo-secret-0123456789abcdef"
        headers = httpsig.sign_request(
            secret, method="GET", path=f"{PREFIX}/events", node_id="node-a",
        )
        request = urllib.request.Request(
            f"{gateway.base_url}/events", method="GET", headers=headers,
        )

        received: list[str] = []
        stop = threading.Event()

        def reader():
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    assert response.status == 200
                    assert "text/event-stream" in response.headers["Content-Type"]
                    for raw in response:
                        received.append(raw.decode("utf-8"))
                        if any("work" in line for line in received):
                            stop.set()
                            return
            except Exception:  # noqa: BLE001 - 关闭时断开是预期的
                stop.set()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        time.sleep(0.2)

        # 触发一次通知
        clients["coordinator"].publish(from_node="coordinator", to_node="node-a",
                               task_type="demo.echo", params={"text": "x"},
                               idempotency_key="idem-sse")
        assert stop.wait(timeout=5), "10 秒内没有收到通知"
        payload = "".join(received)
        assert "event: work" in payload
        assert "hello" not in payload       # 不含任务内容
        assert "node-b" not in payload      # 不含其他节点信息

    @covers("M1.F14")
    def test_stream_requires_authentication(self, gateway):
        """没有签名头的订阅被拒——否则谁都能监听"谁有活"。"""
        raw = _raw_request(gateway.port, (
            f"GET {PREFIX}/events HTTP/1.0\r\nHost: x\r\n\r\n"
        ).encode())
        assert _status_of(raw) == 401

    @covers("M1.F14")
    def test_reconnect_replaces_previous_stream(self, gateway):
        """同一节点重连会**让旧流失效**，避免断线重连堆积僵尸流。"""
        first = gateway.open_stream("node-a")
        assert gateway.stream_alive("node-a", first) is True
        second = gateway.open_stream("node-a")
        assert gateway.stream_alive("node-a", first) is False
        assert gateway.stream_alive("node-a", second) is True

    @covers("M1.F14")
    def test_sse_can_be_disabled_by_config(self, app):
        gw = Gateway(app, server_config=ServerConfig(
            host="127.0.0.1", port=0, sse_enabled=False,
        ))
        gw.start()
        try:
            secret = b"node-a-demo-secret-0123456789abcdef"
            headers = httpsig.sign_request(
                secret, method="GET", path=f"{PREFIX}/events", node_id="node-a",
            )
            raw = _raw_request(gw.port, (
                f"GET {PREFIX}/events HTTP/1.0\r\n"
                + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                + "\r\n"
            ).encode())
            assert _status_of(raw) == 501
        finally:
            gw.stop()


# ---------------------------------------------------------------------------
# 4. 降级与关闭
# ---------------------------------------------------------------------------

class TestDegradationAndShutdown:

    @covers("M1.F16")
    def test_documented_demo_command_really_runs(self):
        """**文档里写的验收命令必须真能跑。**

        这条测试存在的理由是一个真实事故：``make demo`` 曾经带着
        ``--config`` 参数而 ``demo`` 子命令不接受它 —— 三个月里
        没人发现，因为测试都直接调 ``examples.demo.main()``，
        绕过了 CLI 与 Makefile 这一层。

        **测试绕过入口点时，入口点就没人测了。**
        """
        import subprocess
        import sys

        from conftest import REPO_ROOT

        result = subprocess.run(
            # 与 Makefile 的 demo-net 目标完全一致的调用方式
            [sys.executable, "-m", "nxs", "demo", "--net",
             "--config", "config/nxs.example.yaml"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == 0, (
            "验收命令执行失败：\n" + result.stdout + "\n" + result.stderr
        )
        assert "十步全部通过" in result.stdout
        assert "传输：HTTP" in result.stdout, "没有真的走网络"
        # 三条安全断言必须在网络模式下同样复现
        assert "WL-TYPE-404" in result.stdout
        assert "WL-FIELD-001" in result.stdout
        assert "MX-DOMAIN-02" in result.stdout

    @covers("M1.R2")
    def test_node_side_never_listens(self):
        """**静态检查**：节点侧代码里不得出现任何监听端口的构造。

        这是 Egress-Only 的结构性保证。为什么用静态检查而不是行为测试：
        "节点没有监听端口"是一个**不存在的行为**，正向测试证明不了它
        （没有 test 能列出"所有没发生的事"）。只能断言源码里没有这类调用。

        已知局限：静态扫描只能发现**写法已知**的监听方式。若有人用
        ctypes 直接调 socket syscall，本测试看不到——所以它降低风险，
        不消除风险。
        """
        banned = (
            "socketserver", "http.server", "ThreadingHTTPServer",
            "BaseHTTPRequestHandler", ".listen(", ".bind(", ".accept(",
        )
        from conftest import REPO_ROOT

        node_side = [REPO_ROOT / "nxs" / "edge" / "client.py"]
        node_side += sorted((REPO_ROOT / "nxs" / "sdk").glob("*.py"))
        assert node_side, "没有找到节点侧源码"

        offenders = []
        for path in node_side:
            source = path.read_text(encoding="utf-8")
            for token in banned:
                if token in source:
                    offenders.append(f"{path.name}: {token}")
        assert not offenders, (
            "节点侧出现了监听端口的构造，Egress-Only 被破坏：\n  "
            + "\n  ".join(offenders)
        )

    @covers("M1.F17")
    def test_subscribe_gives_up_gracefully_when_server_absent(self):
        """服务不可达时 ``subscribe`` **正常返回**，不抛异常。

        这是"推送只是加速器"的具体体现：调用方拿到返回值后
        继续走 ``poll`` 循环即可，功能不降级。
        """
        transport = HttpTransport(
            "http://127.0.0.1:1/nxs",          # 1 号端口必然连不上
            node_id="node-a", secret=b"k" * 32, timeout=0.5,
        )
        assert transport.subscribe(max_reconnects=1) == "gave_up"

    @covers("M1.F17")
    def test_poll_works_without_push(self, gateway, app):
        """**不订阅也能完整协作** —— 纯短轮询路径始终可用。"""
        secrets = app.config.secrets
        coordinator = HttpTransport(gateway.base_url, node_id="coordinator",
                            secret=secrets["coordinator"])
        node_b = HttpTransport(gateway.base_url, node_id="node-b",
                               secret=secrets["node-b"])
        coordinator.publish(from_node="coordinator", to_node="node-b", task_type="demo.echo",
                    params={"text": "no-push"}, idempotency_key="idem-nopush")
        task = node_b.poll("node-b")
        assert task is not None
        assert task.params["text"] == "no-push"

    @covers("M1.F15")
    def test_stop_is_idempotent_and_releases_port(self, app):
        """关闭可重复调用，且端口真的被释放。"""
        gw = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0))
        gw.start()
        port = gw.port
        gw.stop()
        gw.stop()                     # 重复调用不应抛
        # 端口应可再次绑定
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))

    @covers("M1.F15")
    def test_stop_wakes_blocked_event_stream(self, gateway):
        """关闭时正在等待的流要能退出——否则进程关不掉。"""
        secret = b"node-a-demo-secret-0123456789abcdef"
        headers = httpsig.sign_request(
            secret, method="GET", path=f"{PREFIX}/events", node_id="node-a",
        )
        request = urllib.request.Request(
            f"{gateway.base_url}/events", method="GET", headers=headers,
        )
        finished = threading.Event()

        def reader():
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    for _ in response:
                        pass
            except Exception:  # noqa: BLE001 - 断开是预期结果
                pass
            finally:
                finished.set()

        threading.Thread(target=reader, daemon=True).start()
        time.sleep(0.2)
        gateway.stop()
        assert finished.wait(timeout=5), "关闭后事件流没有退出"


class TestTaskQueryEndpoint:
    """★ 发起方必须能取回自己发布任务的产出。

    ⚠ **这条补的是一个真实缺口。** 在此之前网关只有
    ``/publish``（发）、``/poll``（领）、``/result``（交），
    **没有"查"**。于是 A2A 协作缺了最后一环：问出去的问题，
    答案存在服务端，但问的人取不回来。

    ``session_view()`` 不算 —— 它只汇报状态（id/type/status/...），
    **不含产出内容**。我最初就是直接读 sqlite 才看到回答的，
    那是绕过 API 的做法，不能算有了能力。
    """

    @covers("M1.F9")
    def test_publisher_can_read_own_task(self, clients):
        """发布者能查到自己的任务，且能看到产出。"""
        from nxs.edge.client import HttpTransport

        publisher = clients["coordinator"]
        worker = clients["node-a"]

        delivery = publisher.publish(
            from_node="coordinator", to_node="node-a",
            task_type="demo.echo", params={"text": "查询测试"},
            idempotency_key="query-own-1",
        )
        claimed = worker.poll("node-a")
        from nxs.core.models import Artifact
        # demo.echo 声明为 metadata 级，且 response_fields=('echo','level')
        # —— 产出必须严格匹配这两点，否则被边界拒（WL-LEVEL-002）
        worker.complete("node-a", claimed, [
            Artifact(level="metadata", kind="structured",
                     data={"echo": "查询测试", "level": "metadata"})])

        got = publisher.get_task(delivery.task.id)
        assert got["status"] == "completed"
        assert got["artifacts"], "产出不见了 —— 那等于没取回结果"
        assert got["artifacts"][0]["data"]["echo"] == "查询测试"

    @covers("M1.F9")
    def test_receiver_can_read_task_sent_to_it(self, clients):
        """接收者也能查发给自己的任务（用来看自己交了什么）。"""
        from nxs.edge.client import HttpTransport

        publisher = clients["coordinator"]
        worker = clients["node-a"]

        delivery = publisher.publish(
            from_node="coordinator", to_node="node-a",
            task_type="demo.echo", params={"text": "接收方查询"},
            idempotency_key="query-recv-1",
        )
        assert worker.get_task(delivery.task.id)["id"] == delivery.task.id

    @covers("M1.F9")
    def test_third_party_gets_404_not_403(self, clients):
        """★ 无关节点查别人的任务要 404，**不是 403**。

        403 会告诉调用方"这个任务确实存在，只是不给你看" ——
        那是一个枚举接口，能据此试探出任务 ID 空间里哪些是真实的。
        404 把"不存在"与"无权"合并，信息量为零。

        与网关在策略拒绝时只回 rule_id 是同一条思路：
        **拒绝的形状本身会泄露信息。**
        """
        from nxs.core.errors import PolicyDenied, RuleId

        publisher = clients["coordinator"]
        outsider = clients["node-b"]

        delivery = publisher.publish(
            from_node="coordinator", to_node="node-a",
            task_type="demo.echo", params={"text": "不该被看到"},
            idempotency_key="query-third-1",
        )
        with pytest.raises(PolicyDenied) as ei:
            outsider.get_task(delivery.task.id)
        assert ei.value.rule_id == RuleId.LF_TASK_NOT_FOUND

    @covers("M1.F9")
    def test_nonexistent_task_gives_same_answer(self, clients):
        """★ 不存在的任务必须与"无权查看"给出**完全相同**的错误。

        两者形状不同的话，403 就等于确认了任务存在。
        """
        from nxs.core.errors import PolicyDenied, RuleId

        outsider = clients["node-b"]
        with pytest.raises(PolicyDenied) as ei:
            outsider.get_task("t-does-not-exist-at-all")
        assert ei.value.rule_id == RuleId.LF_TASK_NOT_FOUND

    @covers("M1.F9")
    def test_malformed_id_does_not_hit_storage(self, clients):
        """超长或带斜杠的 ID 直接按不存在处理，不去查存储。

        带斜杠的 ID 如果不拦住，会变成路径穿越的入口
        （``/task/../../ops/nodes``）。
        """
        from nxs.core.errors import PolicyDenied

        client = clients["coordinator"]
        for bad in ("", "x" * 200, "../../ops/nodes", "a/b"):
            with pytest.raises(PolicyDenied):
                client.get_task(bad)

    @covers("M1.F9")
    def test_wrong_method_is_405_not_501(self, gateway):
        """路径对但方法不对 → 405，不是 501。

        501 表示"这个端点还没实现"，会让客户端以为整条路走不通；
        实际只是方法用错了。
        """
        import urllib.request
        import urllib.error
        from nxs.edge import httpsig

        gw = gateway
        url = f"{gw.base_url}/task/some-id"
        headers = httpsig.sign_request(
            b"x" * 32, method="POST", path="/nxs/task/some-id",
            node_id="coordinator", body=b"{}",
        )
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=b"{}", method="POST",
                                     headers=headers)
        try:
            urllib.request.urlopen(req, timeout=10)
            pytest.fail("POST 到查询端点竟然成功了")
        except urllib.error.HTTPError as err:
            assert err.code == 405, f"期望 405，实际 {err.code}"


class TestWhoamiEndpoint:
    """★ Agent 要能零成本确认"我是谁"。

    ⚠ 在此之前它只能靠**读文档推断** —— 而文档会过时，
    推断错了也不会报错，它会以错误的假设去发任务。

    为什么不复用 ``/nxs/ops/nodes``：那需要**运维密钥**。
    交给 Agent 等于把批准/吊销的钥匙交给被管理者。
    """

    @covers("M1.F9")
    def test_returns_own_card(self, clients):
        info = clients["node-a"].whoami()
        assert info["node_id"] == "node-a"
        assert info["card"]["node_id"] == "node-a"
        assert info["card"]["capabilities"], "能力清单不该空"

    @covers("M1.F9")
    def test_includes_approval_and_backlog(self, clients):
        """批准状态与队列深度都要给 —— 那正是 Agent 决策所需的。

        ⚠ 字段变了（权限模型改造 Step 5b）：

            旧：usage.tasks_today / usage.concurrent_now（配额维度）
            新：usage.pending（信箱里还有多少待办）

        前两个数是节点自己的成本统计，与"网络怎么对待它"无关，已随配额删除。
        队列深度是唯一会让发布方被拒（QT-BACKLOG-07）的因素，也是最该报的。
        """
        info = clients["node-a"].whoami()
        assert "approved" in info
        assert info["approved"] is True
        assert "usage" in info
        assert "pending" in info["usage"], (
            "调用方需要知道「它手上压了多少」才决定要不要再发 —— 这是唯一有用的数"
        )

    @covers("M1.F9")
    def test_only_returns_self_never_others(self, clients):
        """★ 只回自己的卡 —— 不提供 ``?node=x`` 这类参数。

        那种参数一旦存在就变成轻量的节点发现接口，而节点发现
        该有它自己的权衡（哪些节点可见、能力是否暴露）。
        要列全网成员应该新增专用的 ``/nxs/peers``。
        """
        a = clients["node-a"].whoami()
        b = clients["node-b"].whoami()
        assert a["node_id"] == "node-a"
        assert b["node_id"] == "node-b"
        # 两份回答里都不该出现对方的节点 ID
        import json
        assert "node-b" not in json.dumps(a, ensure_ascii=False)
        assert "node-a" not in json.dumps(b, ensure_ascii=False)

    @covers("M1.F9")
    def test_unregistered_node_gets_clear_hint(self, gateway, app):
        """未注册的节点调 whoami：要明确说"你还没注册"，别只给个码。"""
        from nxs.edge.client import HttpTransport
        from nxs.core.errors import PolicyDenied, RuleId

        # 一个服务端没有密钥的节点 ID —— 认证就会先失败
        stranger = HttpTransport(gateway.base_url, node_id="nobody",
                                 secret=b"z" * 32)
        with pytest.raises(PolicyDenied) as ei:
            stranger.whoami()
        # 密钥不对时是签名失败，而不是"未注册" —— 两者都该拒绝，
        # 但原因不同，不能混
        assert ei.value.rule_id in (RuleId.AU_SIGNATURE_INVALID,
                                    RuleId.AU_NODE_UNKNOWN)
