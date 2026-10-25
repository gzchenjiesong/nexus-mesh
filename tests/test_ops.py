"""运维接口（M14）的测试 —— 部署之后你唯一的"操作杆"。

为什么运维接口必须自己一套安全模型
----------------------------------
它做的是**权力操作**：批准一个节点、吊销一个节点。这类接口有两个
特有的失败模式，都不能靠"加个认证"解决：

1. **被管理者给自己批条子** —— 如果节点密钥就能调运维接口，
   那么任何被攻破的节点都能自我批准、自我提权。
2. **故障时运维被自己的保护机制锁在门外** —— 如果运维通道和节点
   共用速率限制与并发配额，那么流量洪水时正是最需要运维介入、
   却也最进不来的时刻。

所以本文件验证的不是"功能能用"，而是这两条都不成立。
"""

from __future__ import annotations

import json
import socket

import pytest

from conftest import covers

from nxs.app import NexusApp
from nxs.core.models import RoleCard
from nxs.edge import httpsig
from nxs.edge.client import HttpTransport, OpsClient
from nxs.edge.gateway import Gateway, PREFIX
from nxs.mgmt.config import OPS_NODE_ID, ServerConfig

OPS_SECRET = b"ops-secret-0123456789abcdef0123456789"


def make_card(node_id: str = "work-laptop", *, domain: str = "zone-work") -> RoleCard:
    return RoleCard.from_dict({
        "node_id": node_id,
        "domain": domain,
        "roles": {"executor": True},
        "capabilities": [{
            "type": "work.calendar", "level": "summary", "mode": "sync",
            "response_fields": ["events", "level"],
        }],
        "constraints": {"max_response_level": "summary",
                        "can_receive_from": ["hub"]},
    })


@pytest.fixture
def app():
    from conftest import EXAMPLE_CONFIG, demo_env

    application = NexusApp.from_config_path(
        str(EXAMPLE_CONFIG), env=demo_env(), force_memory=True
    )
    application.start()
    return application


@pytest.fixture
def gateway(app):
    gw = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0),
                 ops_secret=OPS_SECRET)
    gw.start()
    try:
        yield gw
    finally:
        gw.stop()


@pytest.fixture
def gateway_without_ops(app):
    """没有运维密钥的网关 —— 运维面应当**整体不存在**。"""
    gw = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0),
                 ops_secret=b"")
    gw.start()
    try:
        yield gw
    finally:
        gw.stop()


@pytest.fixture
def node_client(gateway):
    secret = b"work-laptop-secret-0123456789abcdef"
    gateway.authenticator.set_secret("work-laptop", secret)
    return HttpTransport(gateway.base_url, node_id="work-laptop", secret=secret)


def _raw(port: int, raw: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(raw)
        sock.shutdown(socket.SHUT_WR)
        out = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            out.append(chunk)
        return b"".join(out)


def _status(raw: bytes) -> int:
    return int(raw.split(b"\r\n", 1)[0].split(b" ")[1])


def _ops_request(port: int, action: str, payload: dict,
                 secret: bytes = OPS_SECRET,
                 node_id: str = OPS_NODE_ID,
                 path: str | None = None) -> bytes:
    body = json.dumps(payload).encode()
    signed_path = path or f"{PREFIX}/ops/{action}"
    headers = httpsig.sign_request(
        secret, method="POST", path=signed_path, node_id=node_id, body=body,
    )
    return _raw(port, (
        f"POST {PREFIX}/ops/{action} HTTP/1.0\r\nHost: x\r\n"
        + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        + f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body)


# ---------------------------------------------------------------------------
# 1. 运维面默认关闭
# ---------------------------------------------------------------------------

class TestOpsSurfaceIsOptIn:
    """没配密钥时，运维端点**整体不存在**，而不是"存在但拒绝"。"""

    @covers("M14.F1")
    def test_disabled_when_env_absent(self, app, monkeypatch):
        """环境变量不存在时，运维面关闭。

        这里只验开关本身；「关闭时端点返回 501」由
        :meth:`test_empty_secret_keeps_ops_closed` 在真实端口上验证。
        """
        import nxs.edge.gateway as gw_module

        monkeypatch.delenv(gw_module.ENV_OPS_SECRET, raising=False)
        assert gw_module._ops_secret_from_env() is None
        gw = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0),
                     ops_secret=None)
        assert gw.ops_enabled is False

    @covers("M14.F1")
    def test_enabled_when_env_present(self, app, monkeypatch):
        """环境变量存在且够长时，运维面开启。"""
        import nxs.edge.gateway as gw_module

        monkeypatch.setenv(gw_module.ENV_OPS_SECRET, OPS_SECRET.decode())
        gw = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0))
        try:
            assert gw.ops_enabled is True
        finally:
            gw.stop()

    @covers("M14.F1")
    def test_empty_secret_keeps_ops_closed(self, gateway_without_ops):
        assert gateway_without_ops.ops_enabled is False
        raw = _ops_request(gateway_without_ops.port, "nodes", {})
        assert _status(raw) == 501

    @covers("M14.F1")
    def test_short_secret_does_not_enable_ops(self, monkeypatch):
        """过短的密钥视为未配置 —— **半个密钥比没有更危险**，
        因为它让人以为运维面是受保护的。"""
        import nxs.edge.gateway as gw_module

        monkeypatch.setenv(gw_module.ENV_OPS_SECRET, "tooshort")
        assert gw_module._ops_secret_from_env() is None

    @covers("M14.F1")
    def test_ops_node_id_cannot_be_taken_by_a_real_node(self):
        """保留身份 ``__ops__`` 不能作为节点 ID —— 否则该节点就是管理员。"""
        from nxs.core.errors import ConfigError
        from nxs.mgmt.config import load_config

        from conftest import REPO_ROOT

        raw = (REPO_ROOT / "config" / "nxs.example.yaml").read_text(encoding="utf-8")
        # 把某个节点的 node_id 改成保留身份
        tampered = raw.replace("node_id: hub", f"node_id: {OPS_NODE_ID}", 1)
        assert tampered != raw, "测试自身要求替换必须生效"

        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(tampered)
            path = Path(handle.name)
        try:
            env = {
                f"NXS_SECRET_{OPS_NODE_ID.upper().replace('_', '_')}": "x" * 32,
                "NXS_SECRET_NODE_A": "y" * 32,
                "NXS_SECRET_NODE_B": "z" * 32,
            }
            with pytest.raises(ConfigError) as excinfo:
                load_config(path, env=env)
            assert OPS_NODE_ID in str(excinfo.value)
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 2. 正常运维流程
# ---------------------------------------------------------------------------

class TestOpsWorks:
    """部署后真正要用的路径。"""

    @covers("M14.F2", "M14.F5")
    def test_approve_flow_end_to_end(self, gateway, node_client):
        """自注册 → 未批准 → 运维批准 → 可接收任务。"""
        node_client.register(make_card())
        ops = OpsClient(gateway.base_url, secret=OPS_SECRET)

        nodes = {n["node_id"]: n for n in ops.nodes()}
        assert nodes["work-laptop"]["approved"] is False

        result = ops.approve("work-laptop")
        assert result["approved"] is True
        assert result["domain"] == "zone-work"

        # 批准后心跳仍然保持批准
        assert node_client.register(make_card())["approved"] is True

    @covers("M14.F3", "S10")
    def test_revoke_takes_effect_on_both_layers(self, gateway, node_client):
        """★ 吊销必须同时作用于**注册表**与**认证层**。

        只改注册表的话，旧凭据仍能通过签名校验——
        节点"已吊销"却还能发请求，那是最危险的假安全。
        """
        node_client.register(make_card())
        ops = OpsClient(gateway.base_url, secret=OPS_SECRET)
        ops.approve("work-laptop")
        assert node_client.register(make_card())["approved"] is True

        ops.revoke("work-laptop")

        # 认证层应立即拒绝（AU-CRED-03），而不是"能签名但被策略拦"
        from nxs.core.errors import PolicyDenied

        with pytest.raises(PolicyDenied) as excinfo:
            node_client.register(make_card())
        assert excinfo.value.rule_id.value == "AU-CRED-03"

    @covers("M14.F4")
    def test_release_sweeps_expired_leases(self, gateway):
        ops = OpsClient(gateway.base_url, secret=OPS_SECRET)
        result = ops.release()
        assert "released" in result
        assert "marked_stale" in result

    @covers("M14.F5")
    def test_nodes_lists_unapproved_too(self, gateway, node_client):
        """未批准的节点必须能看到 —— 否则运维不知道有谁在等着被批。"""
        node_client.register(make_card())
        ops = OpsClient(gateway.base_url, secret=OPS_SECRET)
        rows = ops.nodes()
        pending = [r for r in rows if not r["approved"]]
        assert any(r["node_id"] == "work-laptop" for r in pending)

    @covers("M14.F6")
    def test_ops_actions_are_audited(self, gateway, node_client, app):
        """运维动作必须留痕 —— 否则"谁给谁放了行"无法回答。"""
        node_client.register(make_card())
        OpsClient(gateway.base_url, secret=OPS_SECRET).approve("work-laptop")

        records = [
            r for r in app.stores.audit.read_recent(limit=100)
            if str(r.get("outcome", "")).startswith("ops:")
        ]
        assert records, "运维动作没有留下审计"
        assert any(r["to_node"] == "work-laptop" for r in records)
        assert any(r["from_node"] == OPS_NODE_ID for r in records)


# ---------------------------------------------------------------------------
# 3. 权限边界（本文件的重点）
# ---------------------------------------------------------------------------

class TestOpsAuthorization:

    @covers("M14.R2")
    def test_node_secret_cannot_call_ops(self, gateway, node_client):
        """★ **被管理者不能给自己批条子。**

        节点用自己的密钥签名调用 ops 接口 → 必须被拒。
        这条一旦破了，整套权限矩阵就失去意义：
        任何节点都能先把自已批准、再把等级抬到 full_text。
        """
        node_client.register(make_card())
        node_secret = b"work-laptop-secret-0123456789abcdef"

        raw = _ops_request(gateway.port, "approve", {"node_id": "work-laptop"},
                           secret=node_secret, node_id="work-laptop")
        assert _status(raw) == 403
        # 批准状态**没有**被改动
        assert gateway.app.registry.record("work-laptop").approved is False

    @covers("M14.R2")
    def test_node_secret_with_ops_identity_still_rejected(self, gateway, node_client):
        """节点冒充 ``__ops__`` 身份也没用 —— 签名密钥不对。"""
        node_client.register(make_card())
        node_secret = b"work-laptop-secret-0123456789abcdef"
        raw = _ops_request(gateway.port, "approve", {"node_id": "work-laptop"},
                           secret=node_secret, node_id=OPS_NODE_ID)
        assert _status(raw) == 401
        assert gateway.app.registry.record("work-laptop").approved is False

    @covers("M14.R1")
    def test_ops_not_blocked_by_node_rate_limit(self, gateway, node_client):
        """★ 节点把速率打满之后，**运维仍然进得来**。

        这是 M14.R1 的核心：运维通道不与节点共享配额。
        否则流量洪水时运维会被自己的保护机制锁在门外——
        而那正是唯一需要运维的时刻。
        """
        from nxs.edge.gateway import RateLimiter

        gateway.rate_limiter = RateLimiter(1)      # 极限收紧节点配额

        # 先把节点配额打爆
        for _ in range(30):
            try:
                node_client.poll("work-laptop")
            except Exception:  # noqa: BLE001 - 被限流是预期的
                pass

        # 运维动作必须照常成功
        ops = OpsClient(gateway.base_url, secret=OPS_SECRET)
        assert ops.nodes() is not None, "运维被节点限流挡住了"

    @covers("M14.R1")
    def test_ops_not_blocked_by_exhausted_concurrency(self, app):
        """常规并发槽被占满时，运维仍可操作。"""
        gw = Gateway(app, server_config=ServerConfig(
            host="127.0.0.1", port=0, max_concurrent_requests=1,
        ), ops_secret=OPS_SECRET)
        gw.start()
        try:
            assert gw.acquire_request_slot() is True
            raw = _ops_request(gw.port, "nodes", {})
            assert _status(raw) == 200, "并发槽占满时运维被拒"
        finally:
            gw.release_request_slot()
            gw.stop()

    @covers("M14.F2")
    def test_ops_requires_authentication(self, gateway):
        body = json.dumps({"node_id": "work-laptop"}).encode()
        raw = _raw(gateway.port, (
            f"POST {PREFIX}/ops/approve HTTP/1.0\r\nHost: x\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode() + body)
        assert _status(raw) == 401

    @covers("M14.F2")
    def test_unknown_node_approve_rejected(self, gateway):
        ops = OpsClient(gateway.base_url, secret=OPS_SECRET)
        from nxs.core.errors import ConfigError

        with pytest.raises(ConfigError):
            ops.approve("no-such-node")

    @covers("M14.F2")
    def test_unknown_ops_action_returns_501(self, gateway):
        raw = _ops_request(gateway.port, "selfdestruct", {})
        assert _status(raw) == 501


# ---------------------------------------------------------------------------
# 4. 运维 CLI（部署后真正会被敲的那几条命令）
# ---------------------------------------------------------------------------

class TestOpsCli:
    """CLI 必须真的能用 —— 而不是"代码里有这个函数"。

    这条教训来自一个真实事故：``make demo`` 曾经带着 ``--config``
    而子命令不接受它，三个月没人发现，因为测试都绕过了 CLI 直接调函数。
    **绕过入口点的测试，测不到入口点。**
    """

    @covers("M14.F7")
    def test_cli_lists_nodes_end_to_end(self, gateway, node_client):
        import os
        import subprocess
        import sys

        from conftest import REPO_ROOT

        node_client.register(make_card())

        env = dict(os.environ)
        env["NXS_OPS_SECRET"] = OPS_SECRET.decode()
        result = subprocess.run(
            [sys.executable, "-m", "nxs", "ops", "nodes",
             "--url", gateway.base_url],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "work-laptop" in result.stdout
        assert "zone-work" in result.stdout

    @covers("M14.F7")
    def test_cli_approve_then_node_gets_approved(self, gateway, node_client):
        import os
        import subprocess
        import sys

        from conftest import REPO_ROOT

        node_client.register(make_card())
        env = dict(os.environ)
        env["NXS_OPS_SECRET"] = OPS_SECRET.decode()

        result = subprocess.run(
            [sys.executable, "-m", "nxs", "ops", "approve", "work-laptop",
             "--url", gateway.base_url],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "已批准" in result.stdout
        assert gateway.app.registry.record("work-laptop").approved is True

    @covers("M14.F7")
    def test_cli_without_secret_fails_loudly(self, gateway):
        """缺少凭据时要**说清楚缺什么**，而不是给一个含糊的失败。"""
        import os
        import subprocess
        import sys

        from conftest import REPO_ROOT

        env = dict(os.environ)
        env.pop("NXS_OPS_SECRET", None)
        result = subprocess.run(
            [sys.executable, "-m", "nxs", "ops", "nodes",
             "--url", gateway.base_url],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, env=env,
        )
        assert result.returncode == 2
        assert "NXS_OPS_SECRET" in result.stdout + result.stderr
