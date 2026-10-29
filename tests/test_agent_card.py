"""AgentCard 的字段行为（权限模型改造，Step 2 引入、Step 5c 定型）。

⚠ **本文件已按 Step 5c 重写。**

原来它叫 `test_agent_card_step2.py`，里面有两组测试在 Step 5c 之后**完全失效**：

    TestRoleDerivationEquivalence  测 derived_role() —— 那个方法已删除
    TestStep2DidNotChangeJudgement 测"新字段不影响旧字段" —— 旧字段已不存在

那两组的历史价值已经用完了：Step 2 建那张「四种组合等价表」是为了在
改判定前确认对旧行为的理解。现在判定已经切完，旧字段也没了，
再保留它们就是拿测试去测一个不存在的机制。

本文件现在只测**当前真实存在的字段行为**。
"""

from __future__ import annotations

import pytest

from nxs.core.errors import ConfigError
from nxs.core.models import Level, RoleCard, SelfLimits
from tests.test_traceability import covers


def _card(**over) -> RoleCard:
    """最小可用卡片。**`role` 是必填项**（Step 5c 起）。"""
    base = {
        "node_id": "n1",
        "domain": "work",
        "display_name": "A",
        "description": "",
        "role": "worker",
        "capabilities": [{
            "type": "work.query", "level": "summary", "mode": "sync",
            "response_fields": ["answer", "level"],
        }],
    }
    base.update(over)
    return RoleCard.from_dict(base)


class TestRoleIsMandatory:
    """★★ `role` 是必填项 —— 这是 Step 5c 最硬的一条变化。

    为什么必填而不是"留空即默认某角色"：角色是能力与权限的唯一来源。
    留空意味着网络无从判断它能发布什么、能服务什么 —— 那它注册成功
    也什么都干不了。

    **"接入即失败"比"注册成功但静默无效"好**：后者的症状是
    "派了活没反应"，看起来像网络故障。
    """

    @covers("M11.F1")
    def test_role_absent_is_rejected(self):
        with pytest.raises(ConfigError) as ei:
            RoleCard.from_dict({
                "node_id": "n", "domain": "d", "display_name": "N",
                "capabilities": [],
            })
        msg = str(ei.value)
        assert "必填" in msg
        # 错误信息要指出改哪 —— 只说"必填"不够
        assert "roles" in msg, "要告诉读者角色在哪里定义"

    @covers("M11.F1")
    def test_role_empty_string_is_rejected(self):
        with pytest.raises(ConfigError, match="必填"):
            _card(role="")

    @covers("M11.F1")
    def test_role_whitespace_is_rejected(self):
        with pytest.raises(ConfigError, match="必填"):
            _card(role="   ")

    @covers("M11.F1")
    def test_role_accepts_identifier_chars(self):
        assert _card(role="worker").role == "worker"
        assert _card(role="data-reader").role == "data-reader"
        assert _card(role="w_v2").role == "w_v2"

    @covers("M11.F1")
    def test_role_rejects_spaces_and_dots(self):
        """角色名是标识符。带点号多半是拼错了类型名。"""
        with pytest.raises(ConfigError, match="只能含字母"):
            _card(role="work.query")
        with pytest.raises(ConfigError, match="只能含字母"):
            _card(role="data reader")


class TestOldFieldsAreGone:
    """★★ 旧字段必须真的不存在 —— 防止有人加回来。

    ⚠ 这四条不是"洁癖"。`roles.executor` 这类字段的历史是：
    定义了、解析了、写进示例配置、**然后从未被任何逻辑读取**。

    它们给人虚假的安全感：配了 `executor: false` 以为自己关了闸门，
    而实际上"声明了能力就照样能干活"。所以它们必须彻底消失，
    而不是留着但不管。
    """

    @covers("M11.F1")
    def test_no_roles_class(self):
        import nxs.core.models as m

        assert not hasattr(m, "Roles"), (
            "`Roles` 类又回来了。它承载的 5 个布尔里只有 issuer 参与过判定，\n"
            "  现在那个语义由 role.can_issue 表达 —— 角色是权限的预设组合，\n"
            "  比「每个节点自己勾选 5 个复选框」更清楚。"
        )

    @covers("M11.F1")
    def test_no_constraints_class(self):
        import nxs.core.models as m

        assert not hasattr(m, "Constraints"), (
            "`Constraints` 类又回来了。它的四个字段各有归宿：\n"
            "  can_issue_types  → role.can_issue\n"
            "  can_receive_from → 域矩阵\n"
            "  can_delegate_to  → 删除（不做二次委托）\n"
            "  max_response_level → self_limits.max_response_level"
        )

    @covers("M11.F1")
    def test_card_rejects_roles_field(self):
        """旧卡片写 `roles:` 要被拒绝，且错误信息说明去哪改。"""
        with pytest.raises(ConfigError, match="未知字段"):
            RoleCard.from_dict({
                "node_id": "n", "domain": "d", "display_name": "N",
                "role": "worker",
                "roles": {"executor": True},
                "capabilities": [],
            })

    @covers("M11.F1")
    def test_card_rejects_constraints_field(self):
        with pytest.raises(ConfigError, match="未知字段"):
            RoleCard.from_dict({
                "node_id": "n", "domain": "d", "display_name": "N",
                "role": "worker",
                "constraints": {"max_response_level": "summary"},
                "capabilities": [],
            })

    @covers("M11.F1")
    def test_no_derived_role_helper(self):
        """`derived_role()` 是过渡期的桥，桥两端都没了就该拆。"""
        assert not hasattr(RoleCard, "derived_role"), (
            "`derived_role()` 又回来了 —— 它从旧的 roles 布尔推导角色。\n"
            "  Roles 已删除，没有可推导的输入了。"
        )


class TestSelfLimits:
    """节点自律限额（不是配额）。"""

    @covers("M11.F1")
    def test_defaults(self):
        s = SelfLimits()
        assert s.max_backlog == 20, "缺省必须是 20，不是不限"
        assert s.max_response_level is Level.FULL_TEXT, (
            "自限的默认必须是「不额外限制」——\n"
            "  自限是节点主动收紧，没声明就是没收紧。\n"
            "  初版写成 SUMMARY，导致所有未声明自限的节点上限被压到摘要级。"
        )

    @covers("M11.F1")
    def test_missing_section_means_no_extra_limit(self):
        """卡片没有 self_limits 节 → 不额外收紧。"""
        c = _card()
        assert c.self_limits.max_backlog == 20
        assert c.self_limits.max_response_level is Level.FULL_TEXT
        assert c.effective_max_level() is Level.FULL_TEXT

    @covers("M11.F1")
    def test_rejects_zero_backlog(self):
        """「不想接任务」该用 state=paused 表达，不是把队列设成 0。"""
        with pytest.raises(ConfigError) as ei:
            _card(self_limits={"max_backlog": 0})
        msg = str(ei.value)
        assert "≥1" in msg
        assert "paused" in msg, "错误信息要指出正确做法"

    @covers("M11.F1")
    def test_rejects_bool_backlog(self):
        """Python 里 True 是 int 的子类 —— 不拦住的话 max_backlog=true 能过。"""
        with pytest.raises(ConfigError, match="期望整数"):
            _card(self_limits={"max_backlog": True})

    @covers("M11.F1")
    def test_roundtrip(self):
        c = _card(self_limits={"max_backlog": 3, "max_response_level": "metadata"})
        assert RoleCard.from_dict(c.to_dict()) == c


class TestEffectiveMaxLevel:
    """节点侧愿意给出的上限。"""

    @covers("M11.F1")
    def test_reflects_self_limit(self):
        c = _card(self_limits={"max_response_level": "metadata"})
        assert c.effective_max_level() is Level.METADATA

    @covers("M11.F1")
    def test_public_dict_reports_it(self):
        """对外报自己这一侧的值 —— 语义是「我对自己的承诺」。

        ⚠ 别指望它报"实际能拿到多细"：那要连同角色上限一起算，
        发生在投递时（check_delivery）。对外报自己这一侧更诚实。
        """
        c = _card(self_limits={"max_response_level": "metadata"})
        assert c.to_public_dict()["max_response_level"] == str(Level.METADATA)


class TestEffectiveCapacity:
    """排程提示（不是配额）。"""

    @covers("M11.F1")
    def test_undeclared_defaults_to_one(self):
        """未声明 → 按 1 算。

        为什么不是 0：负载率是 `pending / capacity`，分母 0 会除零。
        "按 1 算"意味着保守假设它单线程 —— 匹配器因此不会把多个任务堆给它。
        """
        c = _card()
        assert c.capacity == 0, "字段本身仍为 0（表示未声明）"
        assert c.effective_capacity() == 1

    @covers("M11.F1")
    def test_declared_wins(self):
        assert _card(capacity=3).effective_capacity() == 3

    @covers("M11.F1")
    def test_rejects_negative(self):
        with pytest.raises(ConfigError, match="不能为负"):
            _card(capacity=-1)


class TestToolsAndState:
    """自律项：工具清单与可用性状态。"""

    @covers("M11.F1")
    def test_tools_default_empty(self):
        assert _card().tools == ()

    @covers("M11.F1")
    def test_tools_exposed_publicly(self):
        """工具清单必须对外可见 —— 对方要知道它能做什么。

        ⚠ 但它是**自律项**：Mesh 只记录不限制。这里只验"能被看见"。
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
        assert _card(may_broadcast=True).to_public_dict()["may_broadcast"] is True

    @covers("M11.F1")
    def test_independent_of_role(self):
        """它是独立 flag，不该从 role 推导。

        worker 也可以被单独授予广播权（虽然实际不该批）——
        正交正是把它做成 flag 的理由。
        """
        c = _card(role="worker", may_broadcast=True)
        assert c.may_broadcast is True
        assert c.role == "worker"


class TestSerializationRoundtrip:
    """★ `to_dict` 必须能被 `from_dict` 原样读回。

    注册流程靠这个：节点提交 to_dict，服务端 from_dict。
    任何字段漏进/漏出都会让注册后的卡片与节点意图不一致 ——
    而那个不一致在审批时看不出来。
    """

    @covers("M11.F1")
    def test_full_card_roundtrip(self):
        c = _card(
            role="driver", may_broadcast=True,
            self_limits={"max_backlog": 7, "max_response_level": "fragment"},
            capacity=4, tools=["Read", "Bash"], state="paused",
        )
        back = RoleCard.from_dict(c.to_dict())
        assert back == c

    @covers("M11.F1")
    def test_minimal_card_roundtrip(self):
        c = _card()
        assert RoleCard.from_dict(c.to_dict()) == c

    @covers("M11.F1")
    def test_public_dict_has_no_secrets(self):
        """对外摘要不带 self_limits 之类的内部数据。

        ⚠ `max_response_level` 是例外 —— 它必须是公开的（对方要知道
        能拿到多细）。但 `max_backlog` 是本机队列深度，与对方无关。
        """
        pub = _card().to_public_dict()
        assert "backlog" not in str(pub), "内部队列深度不该出现在对外摘要里"
        assert "self_limits" not in pub
