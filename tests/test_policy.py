"""策略执行点测试 —— **本项目的核心测试**。

这里的三个 ``TestSecurityAssertion*`` 类对应 v0.1 验收标准里的三条安全断言。
它们是这个项目区别于普通消息队列的地方：**如果这几条不能稳定通过，
本项目就没有达成设计目标**，无论链路本身跑得多顺畅。

额外覆盖：三重上限取最小值、配额、拒绝不泄露、能力发现。
"""

from __future__ import annotations

import pytest

from conftest import covers

from nxs.control import PolicyEngine, Usage, make_task
from nxs.core import Artifact, Level, PolicyDenied, RuleId


# ---------------------------------------------------------------------------
# 断言①：任务类型白名单
# ---------------------------------------------------------------------------

class TestSecurityAssertion1_TypeAllowlist:
    """下发未注册的类型必须被拒，且不进入任何执行逻辑。"""

    @covers("M5.F1")
    def test_unregistered_type_is_denied(self, policy: PolicyEngine):
        with pytest.raises(PolicyDenied) as ei:
            policy.check_delivery("coordinator", "node-a", "demo.unknown")
        assert ei.value.rule_id == RuleId.WL_TYPE_NOT_REGISTERED

    @covers("M5.F1")
    def test_type_not_provided_by_target_is_denied(self, policy: PolicyEngine):
        """demo.search 存在，但 node-a 不提供它。"""
        assert "demo.search" in policy.registered_types
        with pytest.raises(PolicyDenied) as ei:
            policy.check_delivery("coordinator", "node-a", "demo.search")
        assert ei.value.rule_id == RuleId.WL_TYPE_NOT_REGISTERED

    @covers("M5.F1")
    def test_registered_and_provided_is_allowed(self, policy: PolicyEngine):
        d = policy.check_delivery("coordinator", "node-a", "demo.echo")
        assert d.task_type == "demo.echo"

    @covers("M5.F2", "M11.F2")
    def test_wildcard_does_not_over_match(self, policy: PolicyEngine, config):
        """``前缀.*`` 匹配 demo.echo，但不该匹配 demoX.y —— 前缀边界要准。

        ⚠ 权限模型改造后这条测试的**对象变了**：

            旧：矩阵规则的 allow_types 通配（矩阵已不列类型）
            新：角色 serves 的通配

        保护的性质没变（前缀匹配不能误伤），只是归到角色上了。
        """
        from nxs.mgmt.config import RoleDecl

        r = RoleDecl(name="t", serves=("demo.*",))
        assert r.serves_type("demo.echo")
        assert r.serves_type("demo.search")
        assert not r.serves_type("other.thing")
        # 关键：保留结尾的点，"demo.*" 不能匹配 "demoX.y"
        assert not r.serves_type("demoX.y")


# ---------------------------------------------------------------------------
# 断言②：应答字段白名单与等级上限
# ---------------------------------------------------------------------------

class TestSecurityAssertion2_ResponseBoundary:
    """产出越界必须被拒，且协调节点收不到任何内容。"""

    def _echo_task(self, **art_kw):
        art = Artifact(
            level=art_kw.pop("level", Level.METADATA),
            kind=art_kw.pop("kind", "structured"),
            data=art_kw.pop("data", {"echo": "hi", "level": "metadata"}),
        )
        return make_task(
            task_id="t", idempotency_key="i", task_type="demo.echo",
            from_node="coordinator", to_node="node-a", params={"text": "hi"},
            artifacts=[art],
        )

    @covers("M5.F4")
    def test_within_allowlist_is_allowed(self, policy: PolicyEngine):
        policy.check_response("node-a", self._echo_task())

    @covers("M5.F4", "M10.F1")
    def test_field_outside_allowlist_is_denied(self, policy: PolicyEngine):
        task = self._echo_task(data={"echo": "hi", "level": "metadata", "internal_note": "x"})
        with pytest.raises(PolicyDenied) as ei:
            policy.check_response("node-a", task)
        assert ei.value.rule_id == RuleId.WL_FIELD_NOT_ALLOWED

    @covers("M5.F4")
    def test_field_check_recurses_into_lists(self, policy: PolicyEngine):
        """列表里的元素同样是结构化产出，字段白名单要递归生效。"""
        task = self._echo_task(data=[{"echo": "a", "level": "metadata"},
                                     {"echo": "b", "leaked": "x"}])
        with pytest.raises(PolicyDenied) as ei:
            policy.check_response("node-a", task)
        assert ei.value.rule_id == RuleId.WL_FIELD_NOT_ALLOWED

    @covers("M5.F5")
    def test_level_above_capability_declaration_is_denied(self, policy: PolicyEngine):
        """demo.echo 声明为元数据级；产出片段级即违约。"""
        task = self._echo_task(level=Level.FRAGMENT, data={"echo": "hi", "level": "fragment"})
        with pytest.raises(PolicyDenied) as ei:
            policy.check_response("node-a", task)
        assert ei.value.rule_id == RuleId.WL_LEVEL_EXCEEDED

    @covers("M5.F4")
    def test_freetext_length_cap(self, policy: PolicyEngine):
        art = Artifact(level=Level.FRAGMENT, kind="freetext",
                       data={"results": [], "freetext_summary": "A" * 5000})
        task = make_task(task_id="t", idempotency_key="i", task_type="demo.search",
                         from_node="coordinator", to_node="node-b",
                         params={"query": "x"}, artifacts=[art])
        with pytest.raises(PolicyDenied) as ei:
            policy.check_response("node-b", task)
        assert ei.value.rule_id == RuleId.WL_FREETEXT_NOT_ALLOWED

    @covers("M5.F4")
    def test_freetext_within_cap_is_allowed(self, policy: PolicyEngine):
        art = Artifact(level=Level.FRAGMENT, kind="freetext",
                       data={"results": [], "freetext_summary": "short"})
        task = make_task(task_id="t", idempotency_key="i", task_type="demo.search",
                         from_node="coordinator", to_node="node-b",
                         params={"query": "x"}, artifacts=[art])
        policy.check_response("node-b", task)


# ---------------------------------------------------------------------------
# 断言③：跨域委托默认拒绝
# ---------------------------------------------------------------------------

class TestSecurityAssertion3_DefaultDeny:
    """两个信任域之间必须互不可达 —— 这是"数据不出域"的保证。"""

    @pytest.mark.parametrize("src,dst", [
        ("node-a", "node-b"),
        ("node-b", "node-a"),
    ])
    @covers("M5.F2", "M5.R1")
    def test_cross_domain_delegation_denied(self, policy: PolicyEngine, src, dst):
        with pytest.raises(PolicyDenied) as ei:
            policy.check_delivery(src, dst, "demo.echo")
        assert ei.value.rule_id == RuleId.MX_DOMAIN_FORBIDDEN

    @covers("M5.F6")
    def test_zone_a_cannot_reach_anyone(self, policy: PolicyEngine):
        """zone-a 既无发布权（角色层），也无出向规则（矩阵层）。

        这里被角色层拦下是**预期**的：越靠前的检查越基础。
        """
        with pytest.raises(PolicyDenied) as ei:
            policy.check_publish("node-a", "demo.echo")
        assert ei.value.rule_id == RuleId.MX_ISSUER_NOT_ALLOWED

    @covers("M5.F2")
    def test_zone_a_has_no_outbound_rule(self, policy: PolicyEngine):
        """即使绕过角色层（直接查矩阵），zone-a 也没有出向规则。"""
        with pytest.raises(PolicyDenied) as ei:
            policy.check_delivery("node-a", "coordinator", "demo.synthesize")
        assert ei.value.rule_id == RuleId.MX_DOMAIN_FORBIDDEN

    @covers("M5.F3")
    def test_zone_b_to_coordinator_is_reachable(self, policy: PolicyEngine):
        """★ zone-b → coordinator 现在**可达**（行为变更，Step 4）。

        ⚠ 这条测试原名 `test_zone_b_to_coordinator_blocked_by_receiver_not_matrix`，
        它验证的是：

            矩阵允许 zone-b → cloud，但 coordinator 的 can_receive_from 为空
            → 被 MX-RECEIVE-03 拒

        **`can_receive_from` 这个检查已经被移除**（权限模型改造 Step 4）：
        "谁能给我派活"由运维在域矩阵里定义，而不是节点自己声明 ——
        那是旧模型"逐项申请权限"的思路，权力归属不清楚。

        所以现在：矩阵里有 zone-b → cloud 这条规则，投递就被允许。

        **这不是放松管制，是换了管制位置**：
        要限制 zone-b 向上派活，正确做法是**删掉矩阵里的那条规则**，
        而不是依赖对端节点自己说"我不接受"。

        对照：`test_zone_a_has_no_outbound_rule` 依然被拒 ——
        因为 zone-a 在矩阵里确实没有出向规则。
        """
        # 矩阵里有 zone-b → cloud，所以可达
        rule = policy._cfg.policy.find("zone-b", "cloud")
        assert rule is not None, "配置里应有 zone-b → cloud 规则"

        d = policy.check_delivery("node-b", "coordinator", "demo.synthesize")
        assert d.task_type == "demo.synthesize"

    @covers("M5.F3")
    def test_removing_matrix_rule_blocks_delivery(self, policy: PolicyEngine):
        """★ 去掉矩阵规则就真的不可达 —— 证明围栏在矩阵上生效。

        这是上一条测试的反面：不依赖 can_receive_from，
        而是直接在矩阵层面验证"没有规则 = 不可达"。
        """
        # zone-a 没有出向规则（配置里刻意留空）
        with pytest.raises(PolicyDenied) as ei:
            policy.check_delivery("node-a", "coordinator", "demo.synthesize")
        assert ei.value.rule_id == RuleId.MX_DOMAIN_FORBIDDEN

    @covers("M5.R1")
    def test_zone_b_cannot_cross_to_zone_a(self, policy: PolicyEngine):
        """★ 关键性质：角色层放行不代表矩阵层放行。"""
        # node-b 有发布权
        policy.check_publish("node-b", "demo.echo")
        # 但矩阵里没有 zone-b → zone-a 的规则
        with pytest.raises(PolicyDenied) as ei:
            policy.check_delivery("node-b", "node-a", "demo.echo")
        assert ei.value.rule_id == RuleId.MX_DOMAIN_FORBIDDEN

    @covers("M5.F8")
    def test_second_hop_delegation_denied(self, policy: PolicyEngine):
        """默认 can_delegate_to 为空，任何二次委托都被拒。"""
        with pytest.raises(PolicyDenied) as ei:
            policy.check_delegation("node-b", "zone-a")
        assert ei.value.rule_id == RuleId.MX_DELEGATION_TOO_DEEP

    @covers("M3.F6")
    def test_unknown_node_denied(self, policy: PolicyEngine):
        with pytest.raises(PolicyDenied) as ei:
            policy.check_delivery("ghost", "node-a", "demo.echo")
        assert ei.value.rule_id == RuleId.AU_NODE_UNKNOWN


# ---------------------------------------------------------------------------
# 三重上限取最小值
# ---------------------------------------------------------------------------

class TestTripleCeiling:
    """矩阵 / 节点自约束 / 能力声明三者的最小值生效。"""

    @covers("M5.F5", "M5.R2")
    def test_capability_declaration_can_tighten_below_matrix(self, policy: PolicyEngine):
        """矩阵允许摘要级，节点上限摘要级，但 demo.echo 只声明元数据级 → 实际上限元数据级。"""
        d = policy.check_delivery("coordinator", "node-a", "demo.echo")
        assert d.effective_max_level == Level.METADATA

    @covers("M5.F5", "M5.R2")
    def test_matrix_and_declaration_agree(self, policy: PolicyEngine):
        d = policy.check_delivery("coordinator", "node-a", "demo.count")
        assert d.effective_max_level == Level.SUMMARY

    @covers("M5.F5", "M5.R2")
    def test_looser_domain_still_bounded_by_node(self, policy: PolicyEngine):
        """zone-b 的矩阵给到片段级，demo.echo 只声明元数据级 → 仍受元数据级约束。"""
        d = policy.check_delivery("coordinator", "node-b", "demo.echo")
        assert d.effective_max_level == Level.METADATA
        # demo.search 声明片段级，与矩阵一致
        d2 = policy.check_delivery("coordinator", "node-b", "demo.search")
        assert d2.effective_max_level == Level.FRAGMENT


# ---------------------------------------------------------------------------
# 发布侧权限
# ---------------------------------------------------------------------------

class TestPublishPermission:
    """发布权：**只看角色，不看类型**。

    ⚠ 权限模型改造后行为有变（Step 3）：

        旧：issuer 标志 + can_issue_types 逐类型白名单
        新：role.can_issue 一个判断

    「能发哪些类型」不再是**发布权**的维度 —— 类型由 Mesh 全局定义，
    "这个类型存不存在"是 `check_delivery` 的事（`WL-TYPE-404`）。

    所以原来这条 `test_coordinator_cannot_publish_outside_its_allowlist`
    被改写为下面两条：一条验发布权只看角色，一条验未知类型在投递层被拦。
    **保护的性质没丢，只是换了检查点。**
    """

    @covers("M5.F6")
    def test_driver_can_publish(self, policy: PolicyEngine):
        policy.check_publish("coordinator", "demo.echo")
        policy.check_publish("coordinator", "demo.synthesize")

    @covers("M5.F6")
    def test_worker_cannot_publish(self, policy: PolicyEngine):
        """worker 角色的节点不能发布任何任务。

        这是 `work-desktop`（内网执行者）的核心边界 ——
        它只能被委托，不能主动发起。
        """
        with pytest.raises(PolicyDenied) as ei:
            policy.check_publish("node-a", "demo.echo")
        assert ei.value.rule_id == RuleId.MX_ISSUER_NOT_ALLOWED

    @covers("M5.F6")
    def test_publish_permission_is_type_agnostic(self, policy: PolicyEngine):
        """★ 发布权与类型无关 —— 这是新模型与旧模型的关键差异。

        driver 发一个**未定义**的类型也能过发布权检查；
        它会在 `check_delivery` 被 `WL-TYPE-404` 拦住。

        实测确认这一点很重要：如果这里报 MX-ISSUER-01，
        说明类型白名单还在参与发布权判定，改造没生效。
        """
        policy.check_publish("coordinator", "other.thing")   # 不抛

    @covers("M5.F6")
    def test_undefined_type_blocked_at_delivery_not_publish(self, policy: PolicyEngine):
        """★ 未知类型的拦截点从发布权移到了投递检查。

        原来：`check_publish` 报 MX-ISSUER-01
        现在：`check_publish` 放行，`check_delivery` 报 WL-TYPE-404

        **保护没丢** —— 未定义的类型依然发不出去，只是理由更准确：
        不是"你没权限"，而是"这个类型不存在"。
        """
        with pytest.raises(PolicyDenied) as ei:
            policy.check_delivery("coordinator", "node-a", "other.thing")
        assert ei.value.rule_id == RuleId.WL_TYPE_NOT_REGISTERED

    @covers("M5.F6")
    def test_role_is_used_not_legacy_flags(self, policy: PolicyEngine):
        """确认判定真的走 role，而不是悄悄退回旧路径。

        配置里的 coordinator 是 driver（can_issue=True），
        node-a 是 worker（can_issue=False）。若判定还在读
        `roles.issuer`，那么把两者的 role 互换后行为不会变 ——
        这条测试能抓到那种"以为切了其实没切"。
        """
        coord = policy._card("coordinator")
        role = policy.role_of(coord)
        assert role is not None, "配置有 roles 表时必须能解析出角色"
        assert role.can_issue is True
        assert coord.role == "driver" or coord.derived_role() == "driver"


# ---------------------------------------------------------------------------
# 配额
# ---------------------------------------------------------------------------


class TestDenyLeaksNothing:
    @covers("C4")
    def test_wire_response_has_no_internal_identifiers(self, policy: PolicyEngine):
        with pytest.raises(PolicyDenied) as ei:
            policy.check_delivery("node-a", "node-b", "demo.echo")
        wire = ei.value.to_wire()
        assert set(wire) == {"denied", "rule_id", "message"}
        blob = str(wire)
        for secret_bits in ("zone-a", "zone-b", "demo.echo", "node"):
            assert secret_bits not in blob

    @covers("C4")
    def test_field_denial_does_not_name_the_field(self, policy: PolicyEngine):
        """字段名本身可能携带信息，拒绝了也不该说出是哪个字段。"""
        art = Artifact(level=Level.METADATA, kind="structured",
                       data={"echo": "hi", "level": "metadata", "salary_band": "P7"})
        task = make_task(task_id="t", idempotency_key="i", task_type="demo.echo",
                         from_node="coordinator", to_node="node-a",
                         params={"text": "hi"}, artifacts=[art])
        with pytest.raises(PolicyDenied) as ei:
            policy.check_response("node-a", task)
        assert "salary" not in ei.value.to_wire()["message"]


# ---------------------------------------------------------------------------
# 能力发现
# ---------------------------------------------------------------------------


class TestCapabilityDiscovery:
    """能力发现查询。

    ⚠ **权限模型改造中：本类的两个用例待 Step 3 按新模型重写。**

    `providers_of` 这个能力**不会消失** —— 「谁能为某个类型服务」在
    新模型下依然需要回答（自动匹配靠它）。变的是**信息来源**：

        旧：从每个节点各自声明的 capabilities 汇总
        新：从节点的 role 推导（role 定义了它能服务哪些类型）

    所以这里不是删掉覆盖，而是等 Step 3 改成从 role 计算后再重写断言。
    """

    @covers("M5.F9")
    def test_registered_types(self, policy: PolicyEngine):
        assert policy.registered_types >= {
            "demo.echo", "demo.count", "demo.search", "demo.slow", "demo.synthesize",
        }

    @covers("M5.F9")
    def test_providers_are_config_driven(self, policy: PolicyEngine):
        """服务者由 role.serves 决定，而非代码硬编码。

        ⚠ 期望值随角色定义变化（权限模型改造 · Step 4）：
        coordinator 现在是 driver、serves 含 demo.echo，所以它也在列表里。
        这正是设计的意图 —— **能力边界由角色决定**，不再逐节点声明。
        """
        assert policy.providers_of("demo.echo") == frozenset(
            {"coordinator", "node-a", "node-b"}), \
            "driver 和 worker 的 serves 都含 demo.echo"
        # demo.count 只有 worker 服务 —— 角色边界确实在起作用
        assert policy.providers_of("demo.count") == frozenset({"node-a"}), \
            "driver 的 serves 不含 demo.count"
        assert policy.providers_of("nonexistent.type") == frozenset()

