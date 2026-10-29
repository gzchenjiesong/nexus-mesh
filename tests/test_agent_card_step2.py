"""AgentCard 新增字段（权限模型改造 Step 2）。

⚠ **本文件测「数据结构与派生」，不测判定切换。**

判定要到 Step 3 才切。现在测判定的唯一结果是把旧行为固化成断言，
反而会成为 Step 3 的障碍。

## 本文件最重要的一件事

`TestRoleDerivationEquivalence` —— 把「旧 roles 布尔」与「新 role」
在四种组合下的结论逐一对齐。

它的作用**不是**"证明迁移安全"（没有迁移），而是：

> 在动判定逻辑之前，确认**我对旧行为的理解是对的**。

Step 3 要改 `check_publish()`。改之前必须先确定"以前到底是怎么判的"——
而人的记忆在这里不可靠。这张表就是镜子。
"""

from __future__ import annotations

import itertools

import pytest

from nxs.core.errors import ConfigError
from nxs.core.models import Level, RoleCard, SelfLimits
from tests.test_traceability import covers


def _card(**over) -> RoleCard:
    base = {
        "node_id": "n1", "domain": "work",
        "display_name": "A", "description": "",
        "roles": {"executor": True},
        "capabilities": [{
            "type": "work.query", "level": "summary", "mode": "sync",
            "response_fields": ["answer", "level"],
        }],
        "constraints": {"max_response_level": "summary",
                        "can_issue_types": [], "can_receive_from": ["cloud"]},
    }
    base.update(over)
    return RoleCard.from_dict(base)


class TestSelfLimits:
    """节点自律限额。"""

    @covers("M11.F1")
    def test_defaults(self):
        s = SelfLimits()
        assert s.max_backlog == 20, "缺省必须是 20，不是不限"
        assert s.max_response_level is Level.FULL_TEXT, (
            "自限的默认必须是「不额外限制」。\n"
            "  ⚠ 初版写成 SUMMARY，结果所有未声明自限的节点上限都被压到摘要级 ——\n"
            "  症状是 WL-LEVEL-002，而它看起来像 role 配错了。"
        )

    @covers("M11.F1")
    def test_missing_section_means_no_extra_limit(self):
        """旧卡片没有 self_limits 节 → 不额外收紧。

        这是"自律"语义的必然结论：自限是节点主动收紧，
        没声明 = 没收紧。
        """
        c = _card()
        assert c.self_limits.max_backlog == 20
        assert c.self_limits.max_response_level is Level.FULL_TEXT
        # 关键：effective 应等于约束（而不是被自限压到 summary）
        assert c.effective_max_level() is c.constraints.max_response_level

    @covers("M11.F1")
    def test_rejects_zero_backlog(self):
        """backlog=0 要拒绝，并指向正确的表达方式。

        "我不想接任务"应该用 state=paused 表达，而不是把队列设成 0 ——
        后者会让发布方收到一个含义不明的拒绝。
        """
        with pytest.raises(ConfigError) as ei:
            _card(self_limits={"max_backlog": 0})
        msg = str(ei.value)
        assert "≥1" in msg
        assert "paused" in msg, "错误信息要指出正确做法"

    @covers("M11.F1")
    def test_rejects_bool_backlog(self):
        """Python 里 True 是 int 的子类 —— 要拦住，否则 max_backlog=true 能过。"""
        with pytest.raises(ConfigError, match="期望整数"):
            _card(self_limits={"max_backlog": True})

    @covers("M11.F1")
    def test_roundtrip(self):
        c = _card(self_limits={"max_backlog": 3,
                               "max_response_level": "metadata"})
        back = RoleCard.from_dict(c.to_dict())
        assert back == c


class TestEffectiveMaxLevel:
    """★ 上限取 min —— 「多方独立来源、各自只能收紧」这个性质要留住。"""

    @covers("M11.F1")
    def test_node_can_tighten_below_constraint(self):
        """节点自限比约束更紧 → 取节点的。"""
        c = _card(constraints={"max_response_level": "fragment",
                               "can_issue_types": [], "can_receive_from": ["cloud"]},
                  self_limits={"max_response_level": "metadata"})
        assert c.effective_max_level() is Level.METADATA

    @covers("M11.F1")
    def test_constraint_bounds_looser_node_limit(self):
        """★ 反向：节点想放宽也不行 —— 约束更紧时取约束。

        这一条是关键。如果只测"取 min"而不测这个方向，
        一个永远返回 self_limits 的实现也能通过。
        """
        c = _card(constraints={"max_response_level": "metadata",
                               "can_issue_types": [], "can_receive_from": ["cloud"]},
                  self_limits={"max_response_level": "fragment"})
        assert c.effective_max_level() is Level.METADATA, (
            "节点不能通过 self_limits 突破 constraints 的上限"
        )

    @covers("M11.F1")
    def test_equal_values(self):
        c = _card(constraints={"max_response_level": "summary",
                               "can_issue_types": [], "can_receive_from": ["cloud"]},
                  self_limits={"max_response_level": "summary"})
        assert c.effective_max_level() is Level.SUMMARY

    @covers("M11.F1")
    def test_default_self_limit_does_not_tighten(self):
        """★ 未声明自限时，effective 必须等于约束上限，不能被压到 summary。

        这是 Step 4 踩到的坑：默认值写成 summary 会让所有节点上限降级，
        而症状 WL-LEVEL-002 看起来像 role 配错了。
        """
        c = _card(constraints={"max_response_level": "fragment",
                               "can_issue_types": [], "can_receive_from": ["cloud"]})
        assert c.effective_max_level() is Level.FRAGMENT

    @covers("M11.F1")
    def test_public_dict_reports_effective_not_raw(self):
        """对外摘要要给"实际生效"的值。

        给原始字段会让对方自己去算 min —— 而算错的责任不该在对方。
        """
        c = _card(constraints={"max_response_level": "fragment",
                               "can_issue_types": [], "can_receive_from": ["cloud"]},
                  self_limits={"max_response_level": "metadata"})
        pub = c.to_public_dict()
        assert pub["max_response_level"] == str(Level.METADATA)
        # constraints 里仍是原值（那是事实记录，不该被篡改）
        assert c.constraints.max_response_level is Level.FRAGMENT


class TestEffectiveCapacity:
    """排程提示。语义从"配额"降级而来，旧卡片行为必须不变。"""

    @covers("M11.F1")
    def test_falls_back_to_quota(self):
        """★ 未声明 capacity → 退回 quota.concurrent_tasks。

        这条保证旧卡片零影响。如果它红了，说明改造破坏了向后兼容。
        """
        c = _card(constraints={"max_response_level": "summary",
                               "can_issue_types": [], "can_receive_from": ["cloud"],
                               "quota": {"concurrent_tasks": 7}})
        assert c.capacity == 0, "未声明应为 0"
        assert c.effective_capacity() == 7

    @covers("M11.F1")
    def test_declared_capacity_wins(self):
        c = _card(capacity=3,
                  constraints={"max_response_level": "summary",
                               "can_issue_types": [], "can_receive_from": ["cloud"],
                               "quota": {"concurrent_tasks": 7}})
        assert c.effective_capacity() == 3

    @covers("M11.F1")
    def test_rejects_negative(self):
        with pytest.raises(ConfigError, match="不能为负"):
            _card(capacity=-1)


class TestRoleField:
    """`role` 字段的解析与校验。"""

    @covers("M11.F1")
    def test_absent_means_undeclared(self):
        """省略 role（旧卡片）→ 空字符串，不是报错。"""
        assert _card().role == ""

    @covers("M11.F1")
    def test_accepts_identifier_chars(self):
        assert _card(role="worker").role == "worker"
        assert _card(role="data-reader").role == "data-reader"
        assert _card(role="w_v2").role == "w_v2"

    @covers("M11.F1")
    def test_rejects_spaces_and_dots(self):
        """角色名是标识符。带点号/空格说明多半是拼错了类型名。"""
        with pytest.raises(ConfigError, match="只能含字母"):
            _card(role="work.query")
        with pytest.raises(ConfigError, match="只能含字母"):
            _card(role="data reader")


class TestRoleDerivationEquivalence:
    """★★ 四种组合：旧布尔推导出的角色，与显式声明的角色必须一致。

    ⚠ **这组测试的作用是在切判定之前确认对旧行为的理解。**

    Step 3 要改 `check_publish()`。如果那时才发现"原来 publisher
    的判定和我想的不一样"，改动就已经建立在错误前提上了。

    所以这里把四种组合的**结论**列成表，让它可被审阅。
    """

    #: (issuer, executor) → 期望角色
    EXPECTED = {
        (True, True): "driver",
        (False, True): "worker",
        (True, False): "publisher",
        (False, False): "",          # 无角色：既不能发也不能被执行
    }

    @covers("M11.F1")
    @pytest.mark.parametrize("issuer,executor", list(EXPECTED))
    def test_derivation_table(self, issuer, executor):
        c = _card(roles={"issuer": issuer, "executor": executor})
        assert c.derived_role() == self.EXPECTED[(issuer, executor)]

    @covers("M11.F1")
    @pytest.mark.parametrize("issuer,executor", list(EXPECTED))
    def test_declared_role_matches_derivation(self, issuer, executor):
        """★ 显式声明 role 的卡片，必须与推导一致。

        不一致意味着卡片自相矛盾 —— 那正是审批时最该被看见的东西。
        """
        want = self.EXPECTED[(issuer, executor)]
        if not want:
            pytest.skip("无角色组合没有可声明的值")
        c = _card(roles={"issuer": issuer, "executor": executor}, role=want)
        assert c.role_is_consistent() is True

    @covers("M11.F1")
    def test_mismatch_is_detectable(self):
        """声明的 role 与旧标志冲突时必须能被查出来。

        改造过程中如果出现这种卡片，说明有人只改了一半 ——
        而那时行为会变得难以解释（发布权依据 role，路由依据 capabilities）。
        """
        c = _card(roles={"issuer": False, "executor": True}, role="driver")
        assert c.role_is_consistent() is False

    @covers("M11.F1")
    def test_undeclared_role_is_always_consistent(self):
        """未声明 role 的旧卡片不参与对账 —— 否则所有旧卡片都会被判不一致。"""
        for issuer, executor in itertools.product((True, False), repeat=2):
            c = _card(roles={"issuer": issuer, "executor": executor})
            assert c.role_is_consistent() is True


class TestToolsAndState:
    """自律项：工具清单与可用性状态。"""

    @covers("M11.F1")
    def test_tools_default_empty(self):
        assert _card().tools == ()

    @covers("M11.F1")
    def test_tools_exposed_publicly(self):
        """工具清单必须对外可见 —— 对方要知道它能做什么。

        ⚠ 但它是**自律项**：Mesh 只记录不限制。文档里已经写明，
        这里只验"能被看见"，不验"被强制"。
        """
        c = _card(tools=["Read", "Grep", "Bash"])
        assert c.to_public_dict()["tools"] == ["Read", "Grep", "Bash"]

    @covers("M11.F1")
    def test_state_default_available(self):
        assert _card().state == "available"

    @covers("M11.F1")
    def test_state_paused_accepted(self):
        assert _card(state="paused").state == "paused"

    @covers("M11.F1")
    def test_rejects_stale_as_self_reported(self):
        """★ `stale` 不能作为自报状态。

        它是 Mesh 的观测结果（心跳超时），不是节点想报就报的东西。
        混在一起会让"谁说的"变含糊 —— 而排障时这一点很关键。
        """
        with pytest.raises(ConfigError) as ei:
            _card(state="stale")
        msg = str(ei.value)
        assert "stale" in msg
        assert "心跳" in msg, "要说明 stale 由心跳判定"

    @covers("M11.F1")
    def test_rejects_unknown_state(self):
        with pytest.raises(ConfigError, match="无效"):
            _card(state="busy")


class TestMayBroadcast:
    """广播 flag：独立于角色。"""

    @covers("M11.F1")
    def test_default_false(self):
        assert _card().may_broadcast is False

    @covers("M11.F1")
    def test_exposed_publicly(self):
        """必须对外可见 —— 对方要知道它会不会发广播。"""
        c = _card(may_broadcast=True)
        assert c.to_public_dict()["may_broadcast"] is True

    @covers("M11.F1")
    def test_independent_of_role(self):
        """它是独立 flag，不该被 role 推导出来。

        worker 也可以被单独授予广播权（虽然实际不该批），
        这说明两者是正交的 —— 而正交正是把它做成 flag 的理由。
        """
        c = _card(roles={"issuer": False, "executor": True},
                  role="worker", may_broadcast=True)
        assert c.may_broadcast is True
        assert c.derived_role() == "worker"


class TestStep2DidNotChangeJudgement:
    """★★ Step 2 的纪律：判定必须完全不变。

    如果这里红了，说明"只加数据"的承诺被破坏了 ——
    那会让 Step 3 无法归因。
    """

    @covers("M11.F1")
    def test_old_card_still_parses_identically(self):
        """旧格式卡片解析后的关键属性必须与改造前一致。"""
        c = _card()
        assert c.constraints.can_issue_types == ()
        assert c.constraints.can_receive_from == ("cloud",)
        assert c.constraints.max_response_level is Level.SUMMARY
        assert c.roles.executor is True
        assert c.roles.issuer is False

    @covers("M11.F1")
    def test_new_fields_do_not_affect_old_constraints(self):
        """填了新字段，旧约束的原始值不能被改动。"""
        c = _card(
            role="worker", capacity=5,
            self_limits={"max_backlog": 3, "max_response_level": "metadata"},
            tools=["Read"], state="paused", may_broadcast=True,
        )
        # 旧字段原封不动
        assert c.constraints.max_response_level is Level.SUMMARY
        assert c.constraints.quota.concurrent_tasks == 1
        assert c.roles.executor is True
