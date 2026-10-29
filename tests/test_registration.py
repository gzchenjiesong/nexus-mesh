"""节点自注册的测试 —— 部署的前提，也是三条提权路径的防线。

为什么这个文件单独存在
----------------------
"节点能自己注册进来"是部署的必要条件：公司电脑上的 agent 不该要求
云端改配置重启才能加入。但**开放自注册就等于开放了一条提权路径**，
而这条路径上的每一步看起来都很合理：

1. 节点自己声明能力 → 那它就能声明自己会做更多事
2. 节点自己声明数据等级 → 那它就能声明自己能返回原文
3. 节点自己声明信任域 → 那它就能声称自己属于高权限域

所以本文件的重点不是"注册能成功"，而是**这三条都不能成功**。

核心设计：审批针对「那张卡片」，不是 node_id
--------------------------------------------
v0.1 的 ``register`` 里写着 ``approved = existing.approved or approved``。
在"只有运维能改配置"的年代没问题，开放自注册后就成了提权捷径：
已批准节点重发一次注册、把 ``max_response_level`` 抬到 ``full_text``、
批准状态原样保留——一次 HTTP 请求完成提权。

修法是区分来源：配置/运维走 :meth:`Registry.register`（可信），
节点自报走 :meth:`Registry.register_from_node`（不可信，永不批准）。
"""

from __future__ import annotations

import json
import socket

import pytest

from conftest import covers

from nxs.app import NexusApp
from nxs.control.registry import (
    REGISTER_CARD_CHANGED,
    REGISTER_CREATED,
    REGISTER_UNCHANGED,
    Registry,
)
from nxs.core.errors import RuleId
from nxs.core.models import RoleCard
from nxs.edge import httpsig
from nxs.edge.client import HttpTransport
from nxs.edge.gateway import Gateway, PREFIX
from nxs.mgmt.config import ServerConfig


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def make_card(node_id: str = "work-laptop", *, domain: str = "zone-work",
              level: str = "summary") -> RoleCard:
    """造一张**确定性**的角色卡。

    ⚠ 确定性很重要：卡片里若含时间戳或随机串，每次重启都会"卡片变了"，
    批准就会反复失效。这是部署时最容易踩的坑。
    """
    return RoleCard.from_dict({
        "node_id": node_id,
        "domain": domain,
        "display_name": "Company Laptop",
        "heartbeat_interval": 30,
        "role": "worker","capabilities": [{
            "type": "work.calendar",
            "level": level,
            "mode": "sync",
            "description": "会议安排摘要",
            "params_schema": {"day": "string"},
            "response_fields": ["events", "level"]
        }]})


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
    gw = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0))
    gw.start()
    try:
        yield gw
    finally:
        gw.stop()


@pytest.fixture
def work_client(gateway):
    """一个配置里**完全不存在**的节点：模拟公司电脑上的 agent。"""
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


def _body(raw: bytes) -> dict:
    _, _, body = raw.partition(b"\r\n\r\n")
    return json.loads(body.decode()) if body else {}


# ---------------------------------------------------------------------------
# 1. 注册可用性（部署前提）
# ---------------------------------------------------------------------------

class TestRegistrationWorks:
    """注册必须能用 —— 否则加节点就要改云端配置重启。"""

    @covers("M3.F8")
    def test_new_node_can_self_register(self, work_client):
        """配置里不存在的节点能自己注册进来。"""
        result = work_client.register(make_card())
        assert result["outcome"] == REGISTER_CREATED
        assert result["state"] == "alive"

    @covers("M3.F8", "M3.R1")
    def test_first_registration_is_not_approved(self, work_client):
        """**首次注册永远不批准** —— 新节点不能自行加入。"""
        result = work_client.register(make_card())
        assert result["approved"] is False
        assert result["hint"] == "waiting for operator approval"

    @covers("M3.F11")
    def test_repeated_register_behaves_as_heartbeat(self, work_client, app):
        """卡片不变时，重复注册 = 心跳：只刷新存活，不动批准与版本。"""
        work_client.register(make_card())
        app.approve("work-laptop")

        again = work_client.register(make_card())
        assert again["outcome"] == REGISTER_UNCHANGED
        assert again["approved"] is True
        assert again["revision"] == 1, "卡片没变不该前进 revision"

    @covers("M3.F8")
    def test_approved_node_keeps_approval_while_card_stable(self, work_client, app):
        """批准后的健康路径：反复上报都不掉批准。"""
        work_client.register(make_card())
        app.approve("work-laptop")
        for _ in range(5):
            assert work_client.register(make_card())["approved"] is True


# ---------------------------------------------------------------------------
# 2. 提权路径（本文件的重点）
# ---------------------------------------------------------------------------

class TestPrivilegeEscalationBlocked:
    """三条提权路径都必须被结构性挡住，而不是靠判断。"""

    @covers("M3.F8", "M3.R3", "S12")
    def test_card_change_revokes_approval(self, work_client, app):
        """★ 已批准节点改卡片 → 批准立即撤销。

        这是 v0.1 遗留提权路径的回归测试。老逻辑是
        ``approved = existing.approved or approved``，
        改卡片不掉批准，于是一次请求就能把数据等级抬到 full_text。
        """
        work_client.register(make_card(level="summary"))
        app.approve("work-laptop")
        assert app.registry.record("work-laptop").approved is True

        escalated = work_client.register(make_card(level="full_text"))
        assert escalated["outcome"] == REGISTER_CARD_CHANGED
        assert escalated["approved"] is False, "改卡片竟保留了批准 —— 提权成功"
        assert app.registry.record("work-laptop").approved is False

    @covers("M3.F8", "S12")
    def test_escalation_within_same_card_structure_blocked(self, work_client, app):
        """换一种抬法（新增能力）同样被撤批准。"""
        work_client.register(make_card())
        app.approve("work-laptop")

        raw = make_card().to_dict()
        raw["capabilities"].append({
            "type": "work.mail", "level": "summary", "mode": "sync",
            "response_fields": ["mail", "level"]
        })
        result = work_client.register(RoleCard.from_dict(raw))
        assert result["approved"] is False

    @covers("M3.F9", "S12")
    def test_cannot_register_on_behalf_of_another_node(self, gateway, work_client):
        """★ 签名身份与卡片身份不符 → 拒绝。

        否则 node-b 签个名就能提交一张 ``node_id=node-a`` 的卡片，
        直接覆盖 node-a 的注册（含能力与约束）。
        """
        impostor_card = make_card(node_id="node-a")   # 冒充已存在的节点
        with pytest.raises(Exception) as excinfo:
            work_client.register(impostor_card)
        # 客户端会在本地就拒（省一次往返），服务端也必须拒
        assert "node_id" in str(excinfo.value) or "不符" in str(excinfo.value)

    @covers("M3.F9", "S12")
    def test_server_side_rejects_mismatched_identity(self, gateway):
        """即使绕过客户端，**服务端也必须拒** —— 这是真正的防线。"""
        secret = b"work-laptop-secret-0123456789abcdef"
        gateway.authenticator.set_secret("work-laptop", secret)

        card = make_card(node_id="node-a")            # 冒充
        body = json.dumps({"card": card.to_dict()}).encode()
        headers = httpsig.sign_request(
            secret, method="POST", path=f"{PREFIX}/register",
            node_id="work-laptop", body=body,
        )
        raw = _raw(gateway.port, (
            f"POST {PREFIX}/register HTTP/1.0\r\nHost: x\r\n"
            + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            + f"Content-Length: {len(body)}\r\n\r\n"
        ).encode() + body)

        assert _status(raw) == 403
        assert _body(raw)["rule_id"] == RuleId.FM_CARDCARD_INVALID.value
        # node-a 的记录不该被这条请求改动
        assert gateway.app.registry.record("node-a").card.node_id == "node-a"

    @covers("M3.F10", "S13")
    def test_configured_domain_wins_over_self_claimed(self, gateway, app):
        """★ 节点自报的信任域被配置覆盖。

        ``domain`` 决定适用哪组矩阵规则。若节点能自选，
        它可以声称 ``domain="cloud"``——而 cloud 域通常有发布权。
        **信任域是运维划的边界，不是节点自己挑的房间。**
        """
        secret = b"node-a-demo-secret-0123456789abcdef"
        claimed = make_card(node_id="node-a", domain="cloud")   # 自称高权限域
        client = HttpTransport(gateway.base_url, node_id="node-a", secret=secret)

        result = client.register(claimed)
        assert result["domain"] == "zone-a", (
            f"节点自报域 {claimed.domain!r} 竟被采信，实际记为 {result['domain']!r}"
        )
        assert app.registry.record("node-a").card.domain == "zone-a"

    @covers("M3.F10", "S13")
    def test_unknown_node_domain_is_taken_but_never_approved(self, work_client):
        """配置里没有的节点：采信自报域，但**必然未批准**。

        这个组合是安全的：域只是"运维批准时看到的标签"，
        没批准就什么都做不了。运维可以在 status 里看到它自称属于哪个域。
        """
        result = work_client.register(make_card(domain="zone-work"))
        assert result["domain"] == "zone-work"
        assert result["approved"] is False


# ---------------------------------------------------------------------------
# 3. 注册端点自身的防线
# ---------------------------------------------------------------------------

class TestRegisterEndpointHardening:

    @covers("M3.F8")
    def test_unauthenticated_register_rejected(self, gateway):
        card = make_card()
        body = json.dumps({"card": card.to_dict()}).encode()
        raw = _raw(gateway.port, (
            f"POST {PREFIX}/register HTTP/1.0\r\nHost: x\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode() + body)
        assert _status(raw) == 401
        assert gateway.app.registry.record("work-laptop") is None

    @covers("M3.F8")
    def test_malformed_card_rejected(self, gateway, work_client):
        """角色卡结构非法 → 拒（FM-CARD-03），并说明原因在本地日志。"""
        card = make_card().to_dict()
        card["unexpected_field"] = "boom"     # 未知字段必须被拒
        gateway.authenticator.set_secret(
            "work-laptop", b"work-laptop-secret-0123456789abcdef")
        body = json.dumps({"card": card}).encode()
        headers = httpsig.sign_request(
            b"work-laptop-secret-0123456789abcdef", method="POST",
            path=f"{PREFIX}/register", node_id="work-laptop", body=body,
        )
        raw = _raw(gateway.port, (
            f"POST {PREFIX}/register HTTP/1.0\r\nHost: x\r\n"
            + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            + f"Content-Length: {len(body)}\r\n\r\n"
        ).encode() + body)
        assert _status(raw) == 400
        assert _body(raw)["rule_id"] == RuleId.FM_CARDCARD_INVALID.value

    @covers("M3.F12")
    def test_registration_is_audited_without_card_content(self, gateway, work_client, app):
        """注册事件进审计，**但不含卡片内容**。

        能力描述是节点自己写的自由文本。把它记进审计，
        等于让未验证输入进入唯一的追溯依据。
        """
        work_client.register(make_card())
        records = [
            r for r in app.stores.audit.read_recent(limit=100)
            if str(r.get("outcome", "")).startswith("register:")
        ]
        assert records, "注册事件没有留下审计"
        blob = json.dumps(records, ensure_ascii=False)
        assert "会议安排摘要" not in blob, "审计里出现了卡片描述文本"
        assert "capabilities" not in blob


# ---------------------------------------------------------------------------
# 4. 注册中心层（不经 HTTP 的纯逻辑）
# ---------------------------------------------------------------------------

class TestRegistryLayer:

    @covers("M1.F18")
    def test_secret_can_live_only_in_environment(self, monkeypatch):
        """★ 凭据只在环境变量里也要能用。

        这条来自一次真实的部署踩坑：v0.3 让节点能自注册了（角色卡不用
        写进 yaml），但 ``load_config`` 只从 ``nodes`` 列表读凭据——
        于是"新节点仍要写进 yaml"，自注册等于白做。

        HMAC 的固有性质决定了服务端**必须**知道对方密钥，
        所以这里做的不是"取消预置"，而是把预置降到最小摩擦：
        在 ``nxs.env`` 里加一行即可，**不用动 yaml、不用写角色卡**。
        """
        monkeypatch.setenv("NXS_SECRET_LAPTOP_TWO",
                           "env-only-secret-0123456789abcdef")

        from nxs.backends.memory import MemoryRegistryStore
        from nxs.edge.auth import Authenticator
        from nxs.edge.httpsig import sign_request
        from nxs.edge.gateway import PREFIX

        auth = Authenticator({})           # 配置里没有任何凭据
        body = b'{"ok":true}'
        headers = sign_request(
            b"env-only-secret-0123456789abcdef", method="POST",
            path=f"{PREFIX}/register", node_id="laptop-two", body=body,
        )
        sender = auth.verify_request(
            method="POST", path=f"{PREFIX}/register", headers=headers, body=body,
        )
        assert sender == "laptop-two"

    @covers("M1.F18")
    def test_short_env_secret_is_treated_as_missing(self, monkeypatch):
        """过短的环境变量凭据视为未配置 —— 半个密钥比没有更危险。"""
        monkeypatch.setenv("NXS_SECRET_TINY", "short")

        from nxs.edge.auth import Authenticator
        from nxs.edge.gateway import PREFIX
        from nxs.edge.httpsig import sign_request

        auth = Authenticator({})
        body = b"{}"
        headers = sign_request(
            b"short", method="POST", path=f"{PREFIX}/register",
            node_id="tiny", body=body,
        )
        import pytest as _pytest

        from nxs.core.errors import RuleId
        from nxs.edge.auth import AuthFailed

        with _pytest.raises(AuthFailed) as excinfo:
            auth.verify_request(method="POST", path=f"{PREFIX}/register",
                                headers=headers, body=body)
        assert excinfo.value.rule_id is RuleId.AU_SIGNATURE_INVALID

    @covers("M1.F18")
    def test_configured_secret_takes_precedence(self, monkeypatch):
        """配置里有凭据时以配置为准，环境变量不得覆盖。

        反过来说：环境变量是**回落**，不是覆盖。
        否则一个残留的环境变量就能悄悄改掉某个节点的凭据。
        """
        monkeypatch.setenv("NXS_SECRET_BOTH", "env-version-secret-0123456789")

        from nxs.edge.auth import Authenticator
        from nxs.edge.gateway import PREFIX
        from nxs.edge.httpsig import sign_request

        auth = Authenticator({"both": b"config-version-secret-0123456789"})
        body = b"{}"

        # 环境变量那把签不过
        headers_env = sign_request(b"env-version-secret-0123456789",
                                   method="POST", path=f"{PREFIX}/register",
                                   node_id="both", body=body)
        from nxs.core.errors import RuleId
        from nxs.edge.auth import AuthFailed

        with pytest.raises(AuthFailed) as excinfo:
            auth.verify_request(method="POST", path=f"{PREFIX}/register",
                                headers=headers_env, body=body)
        assert excinfo.value.rule_id is RuleId.AU_SIGNATURE_INVALID

        # 配置那把能签过
        headers_cfg = sign_request(b"config-version-secret-0123456789",
                                   method="POST", path=f"{PREFIX}/register",
                                   node_id="both", body=body)
        assert auth.verify_request(
            method="POST", path=f"{PREFIX}/register",
            headers=headers_cfg, body=body,
        ) == "both"

    @covers("M3.F8", "S12")
    def test_register_from_node_never_approves(self):
        from nxs.backends.memory import MemoryRegistryStore

        reg = Registry(MemoryRegistryStore())
        record, outcome = reg.register_from_node(make_card())
        assert outcome == REGISTER_CREATED
        assert record.approved is False

        # 即使先手动批准，改卡片也会撤销
        reg.approve("work-laptop")
        assert reg.record("work-laptop").approved is True
        _, outcome2 = reg.register_from_node(make_card(level="fragment"))
        assert outcome2 == REGISTER_CARD_CHANGED
        assert reg.record("work-laptop").approved is False

    @covers("M3.F8")
    def test_trusted_register_still_lets_operator_approve(self):
        """可信入口（配置/运维）保持原语义：显式 approved 能直接生效。"""
        from nxs.backends.memory import MemoryRegistryStore

        reg = Registry(MemoryRegistryStore())
        record = reg.register(make_card(), approved=True)
        assert record.approved is True
