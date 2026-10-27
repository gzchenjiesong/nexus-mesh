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
        """``demo.*`` 匹配 demo.echo，但不该匹配 demoX.y —— 前缀边界要准。"""
        rule = config.policy.find("cloud", "zone-b")   # find() 的参数是**域**
        assert rule is not None
        assert rule.matches_type("demo.echo")
        assert rule.matches_type("demo.search")
        assert not rule.matches_type("other.thing")
        assert not rule.matches_type("demoX.y")


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
    def test_zone_b_to_coordinator_blocked_by_receiver_not_matrix(self, policy: PolicyEngine):
        """zone-b → coordinator 有矩阵规则，但 coordinator 的 can_receive_from 为空。

        两种拒绝是**不同层次**的原因，不应混为一谈：
        * zone-a → coordinator：矩阵层没有规则（``MX-DOMAIN-02``）
        * zone-b → coordinator：矩阵层有规则，但接收方不接受（``MX-RECEIVE-03``）
        """
        with pytest.raises(PolicyDenied) as ei:
            policy.check_delivery("node-b", "coordinator", "demo.synthesize")
        assert ei.value.rule_id == RuleId.MX_RECEIVE_NOT_ALLOWED

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
    @covers("M5.F6")
    def test_coordinator_can_publish_wildcard_covered_type(self, policy: PolicyEngine):
        policy.check_publish("coordinator", "demo.echo")
        policy.check_publish("coordinator", "demo.synthesize")

    @covers("M5.F6")
    def test_node_without_issuer_role_cannot_publish(self, policy: PolicyEngine):
        with pytest.raises(PolicyDenied) as ei:
            policy.check_publish("node-a", "demo.echo")
        assert ei.value.rule_id == RuleId.MX_ISSUER_NOT_ALLOWED

    @covers("M5.F6")
    def test_coordinator_cannot_publish_outside_its_allowlist(self, policy: PolicyEngine):
        with pytest.raises(PolicyDenied) as ei:
            policy.check_publish("coordinator", "other.thing")
        assert ei.value.rule_id == RuleId.MX_ISSUER_NOT_ALLOWED


# ---------------------------------------------------------------------------
# 配额
# ---------------------------------------------------------------------------

class TestQuota:
    @covers("M5.F7")
    def test_daily_task_quota(self, policy: PolicyEngine):
        with pytest.raises(PolicyDenied) as ei:
            policy.check_quota("node-a", usage=Usage(tasks_today=200))
        assert ei.value.rule_id == RuleId.QT_TASKS_PER_DAY

    @covers("M5.F7")
    def test_concurrency_quota(self, policy: PolicyEngine):
        """node-a 的 concurrent_tasks 是 1。"""
        with pytest.raises(PolicyDenied) as ei:
            policy.check_quota("node-a", usage=Usage(concurrent_now=1))
        assert ei.value.rule_id == RuleId.QT_CONCURRENCY

    @covers("M5.F7")
    def test_per_response_byte_quota(self, policy: PolicyEngine):
        with pytest.raises(PolicyDenied) as ei:
            policy.check_quota("node-a", usage=Usage(), planned_bytes=70000)
        assert ei.value.rule_id == RuleId.QT_BYTES_PER_RESPONSE

    @covers("M5.F7")
    def test_within_quota_is_allowed(self, policy: PolicyEngine):
        policy.check_quota("node-a", usage=Usage(tasks_today=5, bytes_today=1000))


# ---------------------------------------------------------------------------
# 拒绝不泄露
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
    @covers("M5.F9")
    def test_registered_types(self, policy: PolicyEngine):
        assert policy.registered_types >= {
            "demo.echo", "demo.count", "demo.search", "demo.slow", "demo.synthesize",
        }

    @covers("M5.F9")
    def test_providers_are_config_driven(self, policy: PolicyEngine):
        """提供者来自配置，而非代码硬编码——这是"独立基础设施"的判据。"""
        assert policy.providers_of("demo.echo") == frozenset({"node-a", "node-b"})
        assert policy.providers_of("demo.count") == frozenset({"node-a"})
        assert policy.providers_of("nonexistent.type") == frozenset()


class TestRoleFieldsAreLabelsNotGates:
    """`roles.*` 里哪些是闸门、哪些只是标签 —— 这个区分反直觉，必须锁定。

    ⚠ 背景：2026-10-27 实测发现 `roles.executor` **不构成准入闸门**。
    直觉上 `executor: false` 像是「这个节点只发不收」的总开关，
    但一个 `executor: false` 却声明了能力的节点，能完整走完
    投递 → 领取 → 执行 → 回传（真机验证过）。

    这不是 bug，是设计取舍：设计文档对 Executor 的要求写的是
    「能力声明 + can_receive_from + 应答等级上限」——**没有**说要看
    `roles.executor` 这个布尔值。两者冲突时系统相信 `capabilities`，
    因为它更具体、更可验证。

    本类用注入的卡片源直接构造，不依赖 fixture 的默认配置：
    这样"我在测什么"在代码里一眼看得出来。
    """

    @staticmethod
    def _engine_with(config: NexusConfig, card: RoleCard) -> PolicyEngine:
        """构造策略引擎：目标节点用改过的卡，其余节点用配置里的原卡。

        ⚠ resolver 必须覆盖**所有**节点，不能只解析目标节点：
        否则 from_node（如 coordinator）会被判成 `AU-NODE-04` 未知节点，
        测试挂在一个与本意无关的检查上。
        """
        def resolve(node_id: str):
            if node_id == card.node_id:
                return card
            return config.nodes.get(node_id)

        def all_cards():
            return tuple(
                card if nid == card.node_id else c
                for nid, c in config.nodes.items()
            )

        return PolicyEngine(config, card_resolver=resolve, cards_source=all_cards)

    @staticmethod
    def _base_card(config: NexusConfig) -> RoleCard:
        return config.nodes["node-a"]

    @covers("M5.F1")
    def test_executor_flag_does_not_block_execution(self, config: NexusConfig):
        """★ executor=false 但声明了能力 → 投递**会通过**。

        当前的有意行为，不是漏实现。若将来改成闸门，这条会红。
        """
        from dataclasses import replace

        base = self._base_card(config)
        card = replace(base, roles=replace(base.roles, executor=False))
        assert card.capabilities, "前提：这张卡仍声明了能力"

        engine = self._engine_with(config, card)
        # 能力还在 → 不该被拒
        engine.check_delivery("coordinator", card.node_id, card.capabilities[0].type)

    @covers("M5.F1")
    def test_capabilities_are_the_real_gate(self, config: NexusConfig):
        """★ 真正的闸门是 capabilities：没声明能力的节点一律被拒。

        这才是「限制某节点干活」的正确做法 —— 而不是设 executor=false。
        """
        from dataclasses import replace

        base = self._base_card(config)
        card = replace(base, capabilities=(),
                       roles=replace(base.roles, executor=True))
        engine = self._engine_with(config, card)

        with pytest.raises(PolicyDenied) as ei:
            engine.check_delivery("coordinator", card.node_id, "demo.echo")
        assert ei.value.rule_id == RuleId.WL_TYPE_NOT_REGISTERED

    @covers("M5.F6")
    def test_issuer_flag_IS_a_gate(self, policy: PolicyEngine):
        """对照：`roles.issuer` **是**闸门（与 executor 不同）。

        这个对比是本类的重点：同样叫 roles.*，行为不一样。
        看不懂差异的人会以为"关了 executor 就安全了"。
        """
        with pytest.raises(PolicyDenied) as ei:
            policy.check_publish("node-a", "demo.echo")   # node-a 无发布权
        assert ei.value.rule_id == RuleId.MX_ISSUER_NOT_ALLOWED
