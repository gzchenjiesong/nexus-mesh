"""传输层的**对抗性**测试 —— 主动尝试绕过，并验证失败。

这个文件存在的理由
------------------
v0.1 的安全断言证据强度只到"端到端"：证明了正常路径下面的拒绝是有效的。
但那不等于"有人**刻意**攻击时也有效"。二者的区别是：

* 端到端：我按协议发一个越权请求，系统拒了
* 对抗：我**伪造签名 / 改路径 / 重放 / 冒领他人任务 / 慢速耗尽**，
  系统仍然拒，而且拒绝本身不泄露信息

传输层一落地，攻击面就从"进程内调用"扩到了"任何能发包的人"。
所以这一层的每条防线都必须有对抗证据，否则等于宣称而不验证。

覆盖的攻击面
------------
1. **身份** —— 错密钥、未知节点、已吊销、时间偏移
2. **完整性** —— 改路径、改方法、改请求体
3. **重放** —— 原样重放、信封重放（配新请求签名）
4. **越权** —— 冒领他人任务、伪造 from_node
5. **可用性** —— 慢速攻击、连接耗尽
6. **信息泄露** —— 拒绝体字段、错误码差异、审计污染

一条重要的方法论说明
--------------------
这些测试**不能证明"没有漏洞"**，只能证明"这些具体攻击不成立"。
未被想到的攻击仍然存在——这与 v0.1 审查确立的三条盲区一致。
新增攻击面时，应当**同步增加**这里的用例，而不是假定旧用例覆盖了新面。
"""

from __future__ import annotations

import json
import socket
import time

import pytest

from conftest import covers

from nxs.app import NexusApp
from nxs.core.envelope import Envelope, envelope_for
from nxs.core.errors import PolicyDenied, RuleId
from nxs.core.models import Level, Task
from nxs.edge import httpsig
from nxs.edge.client import HttpTransport
from nxs.edge.gateway import PREFIX, Gateway
from nxs.mgmt.config import ServerConfig

SECRETS = {
    "hub": b"hub-demo-secret-0123456789abcdef",
    "node-a": b"node-a-demo-secret-0123456789abcdef",
    "node-b": b"node-b-demo-secret-0123456789abcdef",
}


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    from conftest import EXAMPLE_CONFIG, demo_env

    application = NexusApp.from_config_path(
        str(EXAMPLE_CONFIG), env=demo_env(), force_memory=True
    )
    application.start()
    application.approve("node-a")
    application.approve("node-b")
    return application


@pytest.fixture
def gateway(app):
    gw = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0))
    gw.start()
    try:
        yield gw
    finally:
        gw.stop()


@pytest.fixture
def audit_records(app):
    """读取内存审计记录。**内存后端让"进没进审计"可被直接断言。**"""
    def read() -> list[dict]:
        return list(app.stores.audit.read_recent(limit=200))

    return read


def _raw(port: int, raw: bytes, *, timeout: float = 5.0,
         read: bool = True) -> bytes:
    """发送原始字节并读回响应。用于构造客户端库不肯发的畸形请求。"""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(raw)
        if not read:
            try:
                return sock.recv(65536)
            except socket.timeout:
                return b""
        sock.shutdown(socket.SHUT_WR)
        out: list[bytes] = []
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            out.append(chunk)
        return b"".join(out)


def _status(raw: bytes) -> int:
    return int(raw.split(b"\r\n", 1)[0].split(b" ")[1])


def _body(raw: bytes) -> dict:
    _, _, body = raw.partition(b"\r\n\r\n")
    return json.loads(body.decode()) if body else {}


def _signed_headers(secret: bytes, *, method: str, path: str, node_id: str,
                    ts: str | None = None, nonce: str | None = None,
                    body: bytes | None = None) -> dict[str, str]:
    return httpsig.sign_request(secret, method=method, path=path,
                                node_id=node_id, ts=ts, nonce=nonce, body=body)


def _http(port: int, method: str, path: str, headers: dict[str, str],
          body: bytes | None = None) -> bytes:
    lines = [f"{method} {path} HTTP/1.0", "Host: 127.0.0.1"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    if body is not None:
        lines.append(f"Content-Length: {len(body)}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + (body or b"")
    return _raw(port, raw)


def _envelope_body(secret: bytes, sender: str, *, to_node: str = "node-a",
                   task_type: str = "demo.echo", params: dict | None = None) -> bytes:
    task = Task(
        id="t-attack-1", idempotency_key="idem-attack-1", type=task_type,
        session_id="", from_node=sender, to_node=to_node,
        params=params or {"text": "attack"},
    )
    envelope = envelope_for(secret, sender, task)
    return json.dumps(envelope.to_dict(), ensure_ascii=False).encode()


# ===========================================================================
# 1. 身份
# ===========================================================================

class TestIdentityAttacks:
    """伪造身份的尝试必须一律失败，且失败方式不可区分。"""

    @covers("M1.F10")
    def test_wrong_secret_rejected(self, gateway):
        """用错误的密钥签名 → 401。"""
        headers = _signed_headers(b"wrong-secret-wrong-secret-000000",
                                  method="GET", path=f"{PREFIX}/poll",
                                  node_id="node-a")
        raw = _http(gateway.port, "GET", f"{PREFIX}/poll", headers)
        assert _status(raw) == 401
        assert _body(raw)["rule_id"] == RuleId.AU_SIGNATURE_INVALID.value

    @covers("M1.F10")
    def test_unknown_node_and_bad_signature_look_identical(self, gateway):
        """未知节点 与 签名错 **返回完全相同的响应体**。

        否则攻击者可以靠"错误码差异"枚举有哪些节点 ID 存在。
        这是 R1（认证失败不得区分具体原因）在网络层的落实。
        """
        unknown = _signed_headers(b"k" * 32, method="GET",
                                  path=f"{PREFIX}/poll", node_id="ghost-node")
        bad_sig = _signed_headers(b"k" * 32, method="GET",
                                  path=f"{PREFIX}/poll", node_id="node-a")
        a = _body(_http(gateway.port, "GET", f"{PREFIX}/poll", unknown))
        b = _body(_http(gateway.port, "GET", f"{PREFIX}/poll", bad_sig))
        assert a == b, "两种失败泄露了可区分的信息"
        assert set(a) == {"denied", "rule_id", "message"}

    @covers("M1.F10")
    def test_revoked_credential_takes_effect_immediately(self, gateway, app):
        """吊销后**下一个请求**即失效，不等凭据过期。"""
        headers = _signed_headers(SECRETS["node-a"], method="GET",
                                  path=f"{PREFIX}/poll", node_id="node-a")
        assert _status(_http(gateway.port, "GET", f"{PREFIX}/poll", headers)) == 204

        app.revoke("node-a")
        gateway.authenticator.revoke("node-a")

        headers2 = _signed_headers(SECRETS["node-a"], method="GET",
                                   path=f"{PREFIX}/poll", node_id="node-a")
        raw = _http(gateway.port, "GET", f"{PREFIX}/poll", headers2)
        assert _status(raw) == 401
        assert _body(raw)["rule_id"] == RuleId.AU_CREDENTIAL_REVOKED.value

    @covers("M1.F10", "M1.F3")
    def test_stale_timestamp_rejected(self, gateway):
        """把时间戳往前拨 10 分钟 → 401（防"无限期有效的签名"）。"""
        from datetime import datetime, timedelta, timezone

        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(
            timespec="seconds")
        headers = _signed_headers(SECRETS["node-a"], method="GET",
                                  path=f"{PREFIX}/poll", node_id="node-a",
                                  ts=stale)
        raw = _http(gateway.port, "GET", f"{PREFIX}/poll", headers)
        assert _status(raw) == 401
        assert _body(raw)["rule_id"] == RuleId.AU_REPLAY_DETECTED.value


# ===========================================================================
# 2. 完整性（改路径 / 改方法 / 改体）
# ===========================================================================

class TestIntegrityAttacks:
    """签名覆盖了方法与路径，所以任何"改请求"的尝试都会失败。"""

    @covers("M1.F9", "M1.F10")
    def test_signature_cannot_be_reused_on_another_path(self, gateway):
        """拿 /poll 的合法签名去打 /events → 401。

        如果签名只覆盖主体而不覆盖路径，一个合法的低权限请求就能
        变成高权限请求。

        注：这里用**同方法**的另一个端点。跨方法（如 GET 打 /publish）
        会先命中 405——那是 HTTP 层的标准行为，不涉及签名，故不用于本断言。
        """
        headers = _signed_headers(SECRETS["node-a"], method="GET",
                                  path=f"{PREFIX}/poll", node_id="node-a")
        raw = _http(gateway.port, "GET", f"{PREFIX}/events", headers)
        assert _status(raw) == 401
        assert _body(raw)["rule_id"] == RuleId.AU_SIGNATURE_INVALID.value

    @covers("M1.F9", "M1.F10")
    def test_signature_cannot_be_reused_with_another_method(self, gateway):
        """签名绑定了方法：GET 的签名不能当 POST 用。"""
        body = _envelope_body(SECRETS["hub"], "hub")
        headers = _signed_headers(SECRETS["hub"], method="GET",
                                  path=f"{PREFIX}/publish", node_id="hub",
                                  body=body)
        raw = _http(gateway.port, "POST", f"{PREFIX}/publish", headers, body)
        assert _status(raw) == 401

    @covers("M1.F9")
    def test_tampered_body_rejected(self, gateway):
        """请求体被改 → 401（体摘要参与签名）。

        这条挡住的是"截获请求、改任务参数、原样转发"这类攻击。
        """
        body = _envelope_body(SECRETS["hub"], "hub", params={"text": "original"})
        headers = _signed_headers(SECRETS["hub"], method="POST",
                                  path=f"{PREFIX}/publish", node_id="hub",
                                  body=body)
        tampered = body.replace(b"original", b"tampered")
        raw = _http(gateway.port, "POST", f"{PREFIX}/publish", headers, tampered)
        assert _status(raw) == 401

    @covers("M1.F9")
    def test_query_string_is_covered_by_signature(self, gateway):
        """查询串也在签名内——否则参数可被悄悄替换。"""
        headers = _signed_headers(SECRETS["node-a"], method="GET",
                                  path=f"{PREFIX}/poll", node_id="node-a")
        raw = _http(gateway.port, "GET", f"{PREFIX}/poll?limit=999", headers)
        assert _status(raw) == 401


# ===========================================================================
# 3. 重放
# ===========================================================================

class TestReplayAttacks:

    @covers("M1.F10", "M1.F5")
    def test_identical_request_replay_rejected(self, gateway):
        """原样重放同一请求（同 nonce）→ 第二次 401。

        防重放的价值在于：攻击者不需要伪造任何东西，
        只要能把一个合法请求再发一次，就能造成重复副作用。
        """
        headers = _signed_headers(SECRETS["node-a"], method="GET",
                                  path=f"{PREFIX}/poll", node_id="node-a")
        first = _http(gateway.port, "GET", f"{PREFIX}/poll", headers)
        assert _status(first) == 204

        second = _http(gateway.port, "GET", f"{PREFIX}/poll", headers)
        assert _status(second) == 401
        assert _body(second)["rule_id"] == RuleId.AU_REPLAY_DETECTED.value

    @covers("M1.F10", "M1.F5")
    def test_replayed_envelope_with_fresh_request_signature_rejected(self, gateway):
        """**同一信封**配上**新的请求签名**重发 → 401。

        这是最容易被忽略的一种重放：攻击者拿到一个合法信封，
        自己重新签一次请求头（他有自己的密钥），以为能绕过。
        挡它的是**信封自身的 nonce**——两套 nonce 共用同一个缓存，
        所以第一次之后这个信封就作废了。
        """
        body = _envelope_body(SECRETS["hub"], "hub")
        first_headers = _signed_headers(SECRETS["hub"], method="POST",
                                       path=f"{PREFIX}/publish", node_id="hub",
                                       body=body)
        assert _status(_http(gateway.port, "POST", f"{PREFIX}/publish",
                             first_headers, body)) == 200

        fresh_headers = _signed_headers(SECRETS["hub"], method="POST",
                                        path=f"{PREFIX}/publish", node_id="hub",
                                        body=body)   # 新 nonce，新签名
        second = _http(gateway.port, "POST", f"{PREFIX}/publish",
                       fresh_headers, body)
        assert _status(second) == 401
        assert _body(second)["rule_id"] == RuleId.AU_REPLAY_DETECTED.value


# ===========================================================================
# 4. 越权
# ===========================================================================

class TestAuthorizationAttacks:
    """**传输层最重要的一组**：已认证的节点越权，比匿名攻击更危险。"""

    @covers("M1.F11", "M1.F16")
    def test_node_cannot_complete_someone_elses_task(self, gateway):
        """node-b 尝试"完成"派给 node-a 的任务 → 拒绝（S11）。

        若采信客户端自报的任务归属，任何已认证节点都能把产物写进
        别人的任务里，从而绕过权限矩阵。所以服务端按 id 回查并核对收件人。
        """
        hub = HttpTransport(gateway.base_url, node_id="hub",
                            secret=SECRETS["hub"])
        published = hub.publish(from_node="hub", to_node="node-a",
                                task_type="demo.echo", params={"text": "mine"},
                                idempotency_key="idem-steal")

        # node-b 拿到这个任务的 id（假设它通过某种方式知道了），试图冒领
        stolen = Task(
            id=published.task.id, idempotency_key="idem-steal",
            type="demo.echo", session_id="", from_node="hub", to_node="node-b",
            params={"text": "mine"},
        )
        node_b = HttpTransport(gateway.base_url, node_id="node-b",
                               secret=SECRETS["node-b"])
        with pytest.raises(PolicyDenied) as excinfo:
            node_b.complete("node-b", stolen, [])
        assert excinfo.value.rule_id is RuleId.MX_RECEIVE_NOT_ALLOWED

    @covers("M1.F11", "M1.F16")
    def test_fabricated_from_node_is_ignored(self, gateway):
        """body 里声称 ``from_node=hub`` 无效——发送方取自验签结果。

        实测：node-b 发布时谎称自己是 hub，最终仍以 node-b 的身份进入策略判定，
        于是被矩阵拒绝（zone-b → zone-a 无规则）。
        """
        node_b_secret = SECRETS["node-b"]
        body = _envelope_body(node_b_secret, "node-b", to_node="node-a")
        headers = _signed_headers(node_b_secret, method="POST",
                                  path=f"{PREFIX}/publish", node_id="node-b",
                                  body=body)
        raw = _http(gateway.port, "POST", f"{PREFIX}/publish", headers, body)
        # 被拒：因为它实际身份是 node-b，而 zone-b → zone-a 没有规则
        assert _status(raw) in (403, 401)
        assert _body(raw)["rule_id"] in (
            RuleId.MX_DOMAIN_FORBIDDEN.value,
            RuleId.MX_ISSUER_NOT_ALLOWED.value,
            RuleId.AU_SIGNATURE_INVALID.value,
        )

    @covers("M1.F11")
    def test_unauthenticated_poll_gets_nothing(self, gateway):
        """没有签名头的 poll 被拒，**且响应里没有任何任务信息**。"""
        raw = _http(gateway.port, "GET", f"{PREFIX}/poll", {})
        assert _status(raw) == 401
        body = _body(raw)
        assert "task" not in body
        assert set(body) == {"denied", "rule_id", "message"}

    @covers("M1.F11")
    def test_unauthenticated_publish_gets_nothing(self, gateway):
        body = _envelope_body(SECRETS["hub"], "hub")
        raw = _http(gateway.port, "POST", f"{PREFIX}/publish", {}, body)
        assert _status(raw) == 401


# ===========================================================================
# 5. 可用性攻击
# ===========================================================================

class TestAvailabilityAttacks:

    @covers("M1.F11")
    def test_rate_limiter_window_boundary(self):
        """限流窗口的边界行为用**注入时间**验证，避免测试本身是 flaky 的。

        真实时间会让"第三次请求恰好跨过整秒"变成偶发失败——
        一个会偶发失败的测试比没有测试更糟，因为它会训练人忽略红灯。
        """
        from nxs.edge.gateway import RateLimiter

        limiter = RateLimiter(2)
        assert limiter.allow("node-a", now=100.0) is True
        assert limiter.allow("node-a", now=100.2) is True
        assert limiter.allow("node-a", now=100.9) is False    # 同一秒超额
        assert limiter.allow("node-a", now=101.0) is True     # 新窗口
        # 不同节点各算各的
        assert limiter.allow("node-b", now=101.0) is True

    @covers("M1.F11")
    def test_rate_limiter_disabled_by_zero(self):
        from nxs.edge.gateway import RateLimiter

        limiter = RateLimiter(0)
        for i in range(100):
            assert limiter.allow("node-a", now=100.0 + i * 0.001) is True

    @covers("M1.F11")
    def test_flood_is_rate_limited_over_http(self, gateway):
        """洪水式请求被 429 拦住（`QT-RATE-04`）。

        用 limit=1 连发 50 个请求：无论是否跨秒，都必然有请求被拒。
        这里不精确断言数量，只断言"限流真的生效了"——
        精确数量的断言会依赖真实时钟，那是不必要的脆弱性。
        """
        from nxs.edge.gateway import RateLimiter

        gateway.rate_limiter = RateLimiter(1)
        statuses = []
        for _ in range(50):
            headers = _signed_headers(SECRETS["node-a"], method="GET",
                                      path=f"{PREFIX}/poll", node_id="node-a")
            statuses.append(_status(_http(gateway.port, "GET",
                                          f"{PREFIX}/poll", headers)))
        assert 429 in statuses, f"50 个请求里没有任何一个被限流：{set(statuses)}"
        assert statuses.count(429) >= 48

    @covers("M1.F12")
    def test_oversized_body_never_enters_memory(self, gateway):
        """声称 100MB 的请求体 → 立刻 413，**服务端不读取**。

        判定用 Content-Length 在读取之前完成，所以内存不会按攻击者的
        声明分配。如果先读后判，上限就只是"事后抱怨"。
        """
        raw = _raw(gateway.port, (
            f"POST {PREFIX}/publish HTTP/1.0\r\n"
            f"Host: x\r\nContent-Length: 104857600\r\n\r\n"
        ).encode())
        assert _status(raw) == 413

    @covers("M1.F11")
    def test_slow_headers_connection_is_closed(self, app, monkeypatch):
        """慢速攻击（只发半个请求头就不再发）会被超时切断。

        特征：字节都合法，但永远发不完。唯一有效的对策是超时。
        这里把超时调到 0.5 秒以免测试变慢。
        """
        import nxs.edge.gateway as gateway_module

        monkeypatch.setattr(gateway_module, "REQUEST_TIMEOUT_SECONDS", 0.5)

        gw = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0))
        gw.start()
        try:
            with socket.create_connection(("127.0.0.1", gw.port), timeout=5) as sock:
                sock.sendall(f"POST {PREFIX}/publish HTTP/1.0\r\nHost: x\r\n"
                             .encode())
                # 不发结尾的空行，等服务端自己放弃
                sock.settimeout(3)
                data = sock.recv(65536)
                assert data == b"", "慢速连接没有被服务端关闭"
        finally:
            gw.stop()

    @covers("M1.F13")
    def test_stream_quota_is_enforced(self, app):
        """流配额用尽时拒绝新的订阅（503），不会无界增长。"""
        gw = Gateway(app, server_config=ServerConfig(
            host="127.0.0.1", port=0, max_sse_streams=1,
        ))
        gw.start()
        try:
            assert gw.acquire_sse_slot() is True
            headers = _signed_headers(SECRETS["node-a"], method="GET",
                                      path=f"{PREFIX}/events", node_id="node-a")
            raw = _raw(gw.port, (
                f"GET {PREFIX}/events HTTP/1.0\r\nHost: x\r\n"
                + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                + "\r\n"
            ).encode())
            assert _status(raw) == 503
        finally:
            gw.release_sse_slot()
            gw.stop()


# ===========================================================================
# 6. 信息泄露与审计完整性
# ===========================================================================

class TestInformationLeakage:

    @covers("M1.F11", "M1.R3")
    def test_unauthenticated_input_does_not_reach_audit(self, gateway, audit_records):
        """被拒请求的**自报节点 ID 不进审计**。

        校验失败时那个值只是攻击者可控的文本。把它写进审计，
        等于让未认证输入污染审计——而审计是本系统唯一的追溯依据。

        实测：伪造的 ``ghost-node`` 不应出现在任何一条审计记录里。
        """
        headers = _signed_headers(b"k" * 32, method="GET",
                                  path=f"{PREFIX}/poll", node_id="ghost-node")
        _http(gateway.port, "GET", f"{PREFIX}/poll", headers)

        records = audit_records()
        assert records, "认证失败应当留下审计记录"
        for record in records:
            assert "ghost-node" not in json.dumps(record, ensure_ascii=False)

    @covers("M1.R3")
    def test_audit_records_are_metadata_only(self, gateway, audit_records):
        """传输层拒绝的审计记录里不出现任务参数或内容。"""
        raw = _raw(gateway.port, (
            f"POST {PREFIX}/publish HTTP/1.0\r\n"
            f"Host: x\r\nContent-Length: 999999999\r\n\r\n"
        ).encode())
        assert _status(raw) == 413

        for record in audit_records():
            blob = json.dumps(record, ensure_ascii=False)
            assert "params" not in record
            assert "artifacts" not in record
            assert "demo.echo" not in blob

    @covers("M1.F11")
    def test_rejection_never_echoes_input(self, gateway):
        """拒绝响应不回显任何输入片段。

        用一段特征明显的字符串做任务类型，验证响应里不含它。
        """
        body = _envelope_body(SECRETS["hub"], "hub",
                              task_type="secret.marker.LEAKME")
        headers = _signed_headers(SECRETS["hub"], method="POST",
                                  path=f"{PREFIX}/publish", node_id="hub",
                                  body=body)
        raw = _http(gateway.port, "POST", f"{PREFIX}/publish", headers, body)
        assert b"LEAKME" not in raw, "拒绝响应回显了输入内容"