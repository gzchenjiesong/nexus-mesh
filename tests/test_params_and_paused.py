"""Step 6 新增的两个强制点：参数校验与暂停状态。

## 为什么这两件事放在一起

它们都是**权限模型改造之后才变得可能**的功能：

    参数校验 —— 类型由 Mesh 定义一次，所以"这个类型的参数长什么样"
                有唯一答案。旧模型里同一个名字可以有两套契约，校验方
                不知道该按哪份校验，所以这个字段只能"声明但不校验"。
    暂停状态 —— 节点自报"我活着但暂时不接单"。落到投递判定里需要
                "观测态 vs 自报态"先分清，这个区分也是改造中才理清的。

两者共同点：**都是配置里写着、但一直没接线的东西。**
"""

from __future__ import annotations

import pytest

from nxs.core.errors import ConfigError, PolicyDenied, RuleId
from nxs.core.models import RoleCard
from tests.test_traceability import covers


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------

def _engine_with_type(schema: dict[str, str], name: str = "t.run"):
    """造一个只含单类型定义的策略引擎。"""
    from nxs.control.policy import PolicyEngine
    from nxs.mgmt.config import NexusConfig, TypeDecl

    cfg = NexusConfig()
    cfg.types = {name: TypeDecl(name=name, params_schema=dict(schema))}
    cfg.nodes = {}
    return PolicyEngine(cfg)


class TestParamsAreValidated:
    """★★ `params_schema` 从"只声明"变成"真校验"。"""

    @covers("M5.F6")
    def test_valid_params_pass(self):
        eng = _engine_with_type({"text": "string"})
        eng.check_params("t.run", {"text": "hi"})       # 不抛

    @covers("M5.F6")
    def test_missing_param_rejected(self):
        eng = _engine_with_type({"text": "string"})
        with pytest.raises(PolicyDenied) as ei:
            eng.check_params("t.run", {})
        assert ei.value.rule_id == RuleId.FM_TASK_PARAM_INVALID

    @covers("M5.F6")
    def test_wrong_type_rejected(self):
        eng = _engine_with_type({"n": "integer"})
        with pytest.raises(PolicyDenied) as ei:
            eng.check_params("t.run", {"n": "three"})
        assert ei.value.rule_id == RuleId.FM_TASK_PARAM_INVALID

    @covers("M5.F6")
    def test_bool_does_not_pass_integer(self):
        """★ Python 里 `True` 是 `int` 的子类 —— 必须显式排除。

        `isinstance(True, int)` 是 True，所以不写那条排除判断的话
        `{"n": True}` 会通过 integer 校验。这类"语言自带的小陷阱"
        不写下来一定会再踩。
        """
        eng = _engine_with_type({"n": "integer"})
        with pytest.raises(PolicyDenied):
            eng.check_params("t.run", {"n": True})

    @covers("M5.F6")
    def test_bool_passes_boolean(self):
        eng = _engine_with_type({"flag": "boolean"})
        eng.check_params("t.run", {"flag": True})
        eng.check_params("t.run", {"flag": False})

    @covers("M5.F6")
    def test_number_accepts_int_and_float(self):
        eng = _engine_with_type({"x": "number"})
        eng.check_params("t.run", {"x": 1})
        eng.check_params("t.run", {"x": 1.5})

    @covers("M5.F6")
    def test_undeclared_param_rejected(self):
        """★★ 夹带防护：未声明的参数一律拒绝。

        这是本步最重要的一条。发布方塞一个
        `{"text": "hi", "note": "忽略上述指令..."}`，而下游只按 schema
        读 `text` —— `note` 就跟着任务一路走到执行节点。

        **允许未声明参数等于允许夹带。**
        """
        eng = _engine_with_type({"text": "string"})
        with pytest.raises(PolicyDenied) as ei:
            eng.check_params("t.run", {"text": "hi", "note": "extra"})
        assert ei.value.rule_id == RuleId.FM_TASK_PARAM_INVALID

    @covers("M5.F6")
    def test_extra_param_rejection_does_not_name_it(self):
        """★ 拒绝时不报参数名。

        与 `check_response` 不报字段名同一条理由：具体名字可能携带信息，
        而对外响应是攻击者能看到的。细节进本地 `detail`。
        """
        eng = _engine_with_type({"text": "string"})
        with pytest.raises(PolicyDenied) as ei:
            eng.check_params("t.run", {"text": "hi", "secret_token": "abc"})
        wire = ei.value.to_wire()
        assert "secret_token" not in str(wire)
        # 本地 detail 才带信息
        assert "1" in (ei.value.detail or ""), "detail 该说几个参数被拒"

    @covers("M5.F6")
    def test_empty_schema_skips_validation(self):
        """没写 schema 的类型不做参数校验。

        语义是"这个类型的参数形状未定义"——不知道校验什么就只能放过。
        **但写了 schema 就一定会被校验**（上面几条保证了这一点）。
        """
        eng = _engine_with_type({})
        eng.check_params("t.run", {"anything": 1, "goes": "here"})

    @covers("M5.F6")
    def test_unknown_type_skips_validation(self):
        """类型没在配置里定义时不校验（不在这里报"类型不存在"）。

        类型存在性由 `check_delivery` 的 `WL-TYPE-404` 判 ——
        本方法只回答"参数形状对不对"，不该抢别的检查的活。
        """
        eng = _engine_with_type({"text": "string"})
        eng.check_params("never.defined", {"whatever": 1})


class TestParamTypeNamesAreValidated:
    """★ schema 的类型名必须白名单校验。

    踩过的实例：示例配置里一处写 `"int"`、另一处写 `"integer"`。
    两种写法并存，谁也不知道运行时会拿哪个去比对。

    **类型名拼错的后果是校验永远失败或永远通过，两种都是坑，
    而且从配置上完全看不出来。**
    """

    @covers("M11.F1")
    def test_rejects_unknown_type_name(self):
        from nxs.mgmt.config import TypeDecl

        with pytest.raises(ConfigError) as ei:
            TypeDecl.from_dict({"name": "t.run", "params_schema": {"x": "int"}},
                               "types[0]")
        msg = str(ei.value)
        assert "int" in msg
        assert "可用类型" in msg, "要列出合法的类型名"

    @covers("M11.F1")
    def test_accepts_all_whitelisted_names(self):
        from nxs.mgmt.config import PARAM_TYPES, TypeDecl

        for name in PARAM_TYPES:
            TypeDecl.from_dict({"name": "t.run", "params_schema": {"x": name}},
                               "types[0]")     # 不抛


# ---------------------------------------------------------------------------
# 暂停状态
# ---------------------------------------------------------------------------

def _paused_setup(*, target_state: str = "paused"):
    """造一个 has 两个节点的引擎：源 driver、目标 state 可指定。"""
    from nxs.control.policy import PolicyEngine
    from nxs.mgmt.config import MatrixRule, NexusConfig, PolicyConfig, RoleDecl, TypeDecl

    def _card(nid: str, *, role: str, state: str, domain: str) -> RoleCard:
        return RoleCard.from_dict({
            "node_id": nid, "domain": domain, "display_name": nid,
            "role": role, "state": state,
            "self_limits": {"max_response_level": "summary"},
            "capabilities": [{"type": "t.run", "level": "summary", "mode": "sync",
                              "response_fields": ["ok", "level"]}],
        })

    cfg = NexusConfig()
    cfg.types = {"t.run": TypeDecl(name="t.run", params_schema={})}
    cfg.roles = {
        "driver": RoleDecl(name="driver", can_issue=True, serves=("t.run",)),
        "worker": RoleDecl(name="worker", can_issue=False, serves=("t.run",)),
    }
    cfg.nodes = {
        "src": _card("src", role="driver", state="available", domain="cloud"),
        "dst": _card("dst", role="worker", state=target_state, domain="zone-a"),
    }
    cfg.policy = PolicyConfig(rules=(
        MatrixRule(from_domain="cloud", to_domain="zone-a"),
    ))
    return PolicyEngine(cfg)


class TestPausedBlocksDelivery:
    """★★ `paused` 从"只存字段"变成"参与判定"。"""

    @covers("M5.F6")
    def test_available_target_passes(self):
        eng = _paused_setup(target_state="available")
        d = eng.check_delivery("src", "dst", "t.run")     # 不抛
        assert d.task_type == "t.run"

    @covers("M5.F6")
    def test_paused_target_rejected(self):
        eng = _paused_setup(target_state="paused")
        with pytest.raises(PolicyDenied) as ei:
            eng.check_delivery("src", "dst", "t.run")
        assert ei.value.rule_id == RuleId.QT_NODE_PAUSED

    @covers("M5.F6")
    def test_uses_qt_not_au(self):
        """★ 必须用 `QT-PAUSED-08`，不能用 `AU-APPROVE-05`。

        两个码回答不同的问题：

            AU-APPROVE-05  它有没有资格在这个网络里（未批准/吊销）
            QT-PAUSED-08   它现在接不接单（已批准但主动暂停）

        paused 的节点是**完全合法**的网络成员。用准入错误码会让排障者
        去查批准记录 —— 而那一切正常。
        """
        eng = _paused_setup(target_state="paused")
        with pytest.raises(PolicyDenied) as ei:
            eng.check_delivery("src", "dst", "t.run")
        rule = ei.value.rule_id
        assert rule == RuleId.QT_NODE_PAUSED
        assert rule != RuleId.AU_NOT_APPROVED, (
            "paused 不是准入问题 —— 用 AU-* 会让排障方向跑偏"
        )


class TestMatcherSkipsPaused:
    """自动匹配时 paused 节点直接跳过，不报错。"""

    @covers("M5.F6")
    def test_paused_node_not_a_candidate(self):
        from nxs.backends.memory import MemoryQueueStore, MemoryRegistryStore
        from nxs.control.matcher import Matcher, Requirement
        from nxs.control.registry import Registry

        def _card(nid: str, state: str) -> RoleCard:
            return RoleCard.from_dict({
                "node_id": nid, "domain": "zone-a", "display_name": nid,
                "role": "worker", "state": state,
                "capabilities": [{"type": "t.run", "level": "summary",
                                  "mode": "sync", "response_fields": ["ok"]}],
            })

        reg = Registry(MemoryRegistryStore())
        for c in (_card("up", "available"), _card("down", "paused")):
            reg.register(c, approved=True)
        m = Matcher(reg, MemoryQueueStore())

        ids = [c.node_id for c in m.candidates(Requirement(task_type="t.run"))]
        assert ids == ["up"], (
            f"paused 节点不该出现在候选里，实际 {ids} ——\n"
            f"  跳过它省一次注定失败的校验；报错则是另一回事（见 check_delivery）"
        )


class TestPausedIsNotObservableState:
    """★ 自报态与观测态必须分开 —— 这是整个设计的一个支点。"""

    @covers("M11.F1")
    def test_node_cannot_self_report_stale(self):
        """`stale` 是 Mesh 的观测结果，节点无权自报。

        混在一起会让"谁说的"变含糊 —— 而排障时这一点很关键。
        """
        with pytest.raises(ConfigError, match="心跳"):
            RoleCard.from_dict({
                "node_id": "n", "domain": "d", "display_name": "N",
                "role": "worker", "state": "stale", "capabilities": [],
            })

    @covers("M11.F1")
    def test_paused_is_only_self_reportable_state(self):
        """自报态只有 available / paused 两种。"""
        for s in ("available", "paused"):
            c = RoleCard.from_dict({
                "node_id": "n", "domain": "d", "display_name": "N",
                "role": "worker", "state": s, "capabilities": [],
            })
            assert c.state == s
