"""核心契约层测试：信封、模型、错误码。

重点覆盖三类容易出错的语义：

* **严格校验** —— 未知字段必须报错，不能静默忽略
* **规范 JSON** —— 签名的确定性，跨序列化往返后仍能验签
* **拒绝不泄露** —— 对外的拒绝响应不得携带任何被拒内容
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import covers

from nxs.core import (
    Artifact,
    AuthFailed,
    Capability,
    ConfigError,
    Envelope,
    Level,
    PolicyDenied,
    RoleCard,
    RuleId,
    Semantics,
    Task,
    TaskStatus,
    envelope_for,
    safe_message,
)

SECRET = b"unit-test-secret-0123456789abcdef"


def make_task(**kw) -> Task:
    base = dict(
        id="t-1", idempotency_key="i-1", type="demo.echo",
        session_id="ses-1", from_node="hub", to_node="node-a",
        params={"text": "hi"},
    )
    base.update(kw)
    return Task(**base)


# ---------------------------------------------------------------------------
# 严格校验
# ---------------------------------------------------------------------------

class TestStrictValidation:
    @covers("C6", "M11.F1")
    def test_rejects_unknown_field_in_role_card(self):
        with pytest.raises(ConfigError, match="未知字段"):
            RoleCard.from_dict({"node_id": "x", "domain": "d", "typo_field": 1})

    @covers("C6", "M11.F1")
    def test_rejects_unknown_field_in_capability(self):
        with pytest.raises(ConfigError, match="未知字段"):
            Capability.from_dict({"type": "a.b", "level": "summary", "oops": True})

    @covers("C6")
    def test_requires_verb_dot_object_naming(self):
        with pytest.raises(ConfigError, match="命名约定"):
            Capability.from_dict({"type": "badname", "level": "summary"})

    @covers("C6")
    def test_rejects_out_of_range_level(self):
        with pytest.raises(ConfigError):
            Capability.from_dict({"type": "a.b", "level": "L9"})

    @covers("C6")
    def test_freetext_must_be_subset_of_response_fields(self):
        with pytest.raises(ConfigError, match="必须同时出现在"):
            Capability.from_dict({
                "type": "a.b", "level": "summary",
                "response_fields": ["x"], "freetext_fields": ["y"],
            })

    @covers("C6")
    def test_duplicate_capability_type_rejected(self):
        with pytest.raises(ConfigError, match="重复"):
            RoleCard.from_dict({
                "node_id": "m", "domain": "d",
                "capabilities": [
                    {"type": "a.b", "level": "summary"},
                    {"type": "a.b", "level": "summary"},
                ],
            })


class TestDefaultsAreDeny:
    """默认值必须落在"拒绝"一侧——漏配不能变成放行。"""

    @covers("C5")
    def test_roles_default_false(self):
        r = RoleCard.from_dict({"node_id": "m", "domain": "d"})
        assert not any([r.roles.issuer, r.roles.executor,
                        r.roles.relay, r.roles.observer])

    @covers("C5")
    def test_constraints_default_empty(self):
        r = RoleCard.from_dict({"node_id": "m", "domain": "d"})
        assert r.constraints.can_issue_types == ()
        assert r.constraints.can_receive_from == ()
        assert r.constraints.can_delegate_to == ()

    @covers("C5")
    def test_level_ordering(self):
        assert Level.METADATA < Level.SUMMARY < Level.FRAGMENT < Level.FULL_TEXT
        assert Level.SUMMARY <= Level.FRAGMENT
        assert not (Level.FULL_TEXT <= Level.SUMMARY)

    @covers("C8")
    def test_level_str_is_readable(self):
        assert str(Level.SUMMARY) == "summary"


# ---------------------------------------------------------------------------
# 信封
# ---------------------------------------------------------------------------

class TestEnvelope:
    @covers("M1.F1", "M1.F2")
    def test_sign_and_verify(self):
        env = envelope_for(SECRET, "hub", make_task())
        env.verify(SECRET)
        assert env.sig.startswith("hmac-sha256:")

    @covers("M1.F4")
    def test_survives_serialization_roundtrip(self):
        """规范 JSON 必须稳定，否则跨进程/跨实现的签名校验会失败。"""
        env = envelope_for(SECRET, "hub", make_task(params={"a": 1, "b": [2, 3]}))
        revived = Envelope.from_dict(env.to_dict())
        revived.verify(SECRET)

    @covers("M1.F2")
    def test_detects_tampered_payload(self):
        env = envelope_for(SECRET, "hub", make_task())
        data = env.to_dict()
        data["task"]["params"]["text"] = "TAMPERED"
        with pytest.raises(AuthFailed) as ei:
            Envelope.from_dict(data).verify(SECRET)
        assert ei.value.rule_id == RuleId.AU_SIGNATURE_INVALID

    @covers("M1.F2")
    def test_detects_impersonated_sender(self):
        env = envelope_for(SECRET, "hub", make_task())
        data = env.to_dict()
        data["sender"] = "node-a"
        with pytest.raises(AuthFailed) as ei:
            Envelope.from_dict(data).verify(SECRET)
        assert ei.value.rule_id == RuleId.AU_SIGNATURE_INVALID

    @covers("M1.F2")
    def test_rejects_wrong_secret(self):
        env = envelope_for(SECRET, "hub", make_task())
        with pytest.raises(AuthFailed):
            env.verify(b"a-different-secret-0123456789")

    @covers("M1.F2")
    def test_rejects_missing_signature(self):
        env = envelope_for(SECRET, "hub", make_task())
        env.sig = ""
        with pytest.raises(AuthFailed):
            env.verify(SECRET)

    @covers("M1.F3")
    def test_rejects_stale_timestamp(self):
        env = envelope_for(SECRET, "hub", make_task())
        env.ts = (datetime.now(timezone.utc)
                  - timedelta(seconds=600)).astimezone().isoformat(timespec="seconds")
        env.sign(SECRET)
        with pytest.raises(AuthFailed) as ei:
            env.verify(SECRET)
        assert ei.value.rule_id == RuleId.AU_REPLAY_DETECTED

    @covers("M1.F3")
    def test_clock_skew_window_is_configurable(self):
        env = envelope_for(SECRET, "hub", make_task())
        env.ts = (datetime.now(timezone.utc)
                  - timedelta(seconds=600)).astimezone().isoformat(timespec="seconds")
        env.sign(SECRET)
        env.verify(SECRET, skew_seconds=3600)  # 放宽后应接受

    @covers("M1.F3")
    def test_ttl_expiry(self):
        task = make_task(ttl=(datetime.now(timezone.utc)
                              - timedelta(seconds=1)).astimezone().isoformat(timespec="seconds"))
        env = envelope_for(SECRET, "hub", task)
        assert env.is_expired()

    @covers("M1.F8")
    def test_rejects_incompatible_major_version(self):
        env = envelope_for(SECRET, "hub", make_task())
        data = env.to_dict()
        data["envelope_version"] = "9.0"
        with pytest.raises(ConfigError, match="主版本"):
            Envelope.from_dict(data)


# ---------------------------------------------------------------------------
# 拒绝语义
# ---------------------------------------------------------------------------

class TestDenyDoesNotLeak:
    @covers("C4")
    def test_wire_format_contains_only_rule_id(self):
        denied = PolicyDenied(RuleId.WL_FIELD_NOT_ALLOWED, detail="internal_note=xyz")
        wire = denied.to_wire()
        assert set(wire) == {"denied", "rule_id", "message"}
        assert "xyz" not in str(wire)
        assert "internal_note" not in str(wire)

    @covers("C4")
    def test_every_rule_has_a_safe_message(self):
        for rid in RuleId:
            msg = safe_message(rid)
            assert msg and msg != rid.value
            # 固定消息必须是英文短句，不含任何变量部分
            assert len(msg) < 60


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------

class TestTaskStatusMachine:
    @covers("M7.F2")
    def test_happy_path(self):
        assert TaskStatus.can_transition(TaskStatus.SUBMITTED, TaskStatus.WORKING)
        assert TaskStatus.can_transition(TaskStatus.WORKING, TaskStatus.COMPLETED)

    @covers("M7.F2", "M7.R3")
    def test_terminal_states_are_final(self):
        for terminal in TaskStatus.TERMINAL:
            assert TaskStatus.TRANSITIONS[terminal] == frozenset()

    @covers("M7.F6")
    def test_input_required_is_bidirectional(self):
        """追问必须可来回，否则执行方只能失败重来。"""
        assert TaskStatus.can_transition(TaskStatus.WORKING, TaskStatus.INPUT_REQUIRED)
        assert TaskStatus.can_transition(TaskStatus.INPUT_REQUIRED, TaskStatus.WORKING)

    @covers("M10.F2")
    def test_semantics_constants(self):
        assert (Semantics.DATA, Semantics.REQUEST) == ("data", "request")


class TestArtifactLevel:
    @covers("M10.R1")
    def test_level_is_carried_by_artifact_not_caller(self):
        """等级由产出方标注；调用方只能在信封里声明参数，无法指定等级。"""
        art = Artifact(level=Level.FRAGMENT, kind="structured", data={"x": 1})
        assert art.level == Level.FRAGMENT

    @covers("M5.F5")
    def test_max_artifact_level(self):
        task = make_task(artifacts=[
            Artifact(level=Level.METADATA, kind="structured", data={}),
            Artifact(level=Level.FRAGMENT, kind="structured", data={}),
        ])
        assert task.max_artifact_level() == Level.FRAGMENT

    @covers("M5.F5")
    def test_empty_artifacts_is_l0(self):
        assert make_task().max_artifact_level() == Level.METADATA


# ---------------------------------------------------------------------------
# 安全断言 S6：产出等级由产出方标注，发起方无法指定
# ---------------------------------------------------------------------------

class TestLevelCannotBeSpecifiedByCaller:
    """设计文档 9.2 把 ``level`` 列为三个"支点字段"之一：

    > 由执行方标注，调用方无法指定 —— 否则调用方可以直接"声明"自己只要
    > 摘要级，却实际拿到片段级的内容。

    这条性质此前**没有专门测试**（只验证了字段存在）。这里用<b>结构性断言</b>
    把它固定下来：不是"跑一遍看行为"，而是断言"接口上根本没有这条路"。
    """

    @covers("M10.R1")
    def test_task_has_no_level_field(self):
        """任务对象上不存在 level —— 发起方没有地方可以放这个声明。"""
        import dataclasses
        names = {f.name for f in dataclasses.fields(Task)}
        assert "level" not in names, (
            "Task 上出现了 level 字段。如果发起方能指定等级，"
            "他就可以声明 summary 却拿到 fragment —— 支点字段的意义就没了。"
        )

    @covers("M10.R1")
    def test_artifact_level_is_mandatory(self):
        """Artifact.level 没有默认值——产出方必须显式承担这个声明。"""
        import dataclasses
        f = next(x for x in dataclasses.fields(Artifact) if x.name == "level")
        assert f.default is dataclasses.MISSING
        assert f.default_factory is dataclasses.MISSING, (
            "Artifact.level 有了默认值 —— 那会让'忘记标注'变成静默的 metadata 或 summary，"
            "而不是一个必须面对的决定。"
        )

    @covers("M10.R1", "M5.F5")
    def test_params_cannot_smuggle_level(self, config):
        """把 level 塞进 params 也不生效——实际上限取自配置，不取自请求。"""
        from nxs.control import PolicyEngine
        from nxs.core import Artifact, Level, PolicyDenied

        engine = PolicyEngine(config)
        # demo.echo 的能力声明是元数据级；发起方试图在 params 里"要"片段级
        task = Task(
            id="t", idempotency_key="i", type="demo.echo",
            session_id="", from_node="hub", to_node="node-a",
            params={"text": "x", "level": "fragment", "max_response_level": "full_text"},
            artifacts=[Artifact(level=Level.FRAGMENT, kind="structured",
                                data={"echo": "x", "level": "fragment"})],
        )
        with pytest.raises(PolicyDenied) as ei:
            engine.check_response("node-a", task)
        assert ei.value.rule_id == RuleId.WL_LEVEL_EXCEEDED, (
            "在 params 里声明 level 竟然影响到了实际上限 —— 这正是设计要防住的事。"
        )

    @covers("M5.F5", "M5.R2")
    def test_effective_ceiling_comes_only_from_config(self, config):
        """实际上限只由配置三方决定，与请求内容无关。"""
        from nxs.control import PolicyEngine
        engine = PolicyEngine(config)
        # 同一个类型、同一个目标，无论请求怎么变，上限恒定
        d1 = engine.check_delivery("hub", "node-a", "demo.echo")
        d2 = engine.check_delivery("hub", "node-a", "demo.echo")
        assert d1.effective_max_level == d2.effective_max_level == Level.METADATA


# ---------------------------------------------------------------------------
# 自报展示字段的清洗
# ---------------------------------------------------------------------------

class TestSelfReportedText:
    """``display_name`` / ``description`` 是**节点自己填**的，属于不可信输入。

    与配置字段的区别：配置是你写的，这两个是别人提交的（自注册入口）。
    所以它们必须在进入注册表时就被清洗，而不是指望每个下游各自防。

    ⚠ 优先防的是**终端注入**与**日志注入**，不是 XSS：

    * ANSI 转义能操纵 ``nxs ops nodes`` 的终端输出（伪造颜色/清屏/覆盖内容），
      运维看终端时会被骗
    * ``\\n`` 能伪造审计 NDJSON 的整行 —— 审计是 append-only 的可取证记录

    XSS 归渲染层管：``<`` ``>`` 是合法文本，这里**不做** HTML 转义。
    """

    @staticmethod
    def _card_with(config, **overrides):
        from nxs.core.models import RoleCard

        raw = dict(config.nodes["node-a"].to_dict())
        raw.update(overrides)
        return RoleCard.from_dict(raw)

    @covers("M3.F1")
    def test_ansi_escape_is_rejected(self, config):
        """★ 终端注入：ANSI 序列能让运维看到伪造的输出。"""
        with pytest.raises(ConfigError) as ei:
            self._card_with(config, display_name="\x1b[2J\x1b[1;31m假警告")
        assert "控制字符" in str(ei.value)
        assert "0x1b" in str(ei.value)

    @covers("M3.F1")
    def test_newline_is_rejected(self, config):
        """★ 日志注入：换行能让审计 NDJSON 一行变两行。"""
        with pytest.raises(ConfigError) as ei:
            self._card_with(config, display_name="名字\n{outcome: completed}")
        assert "控制字符" in str(ei.value)

    @covers("M3.F1")
    def test_overlong_is_rejected(self, config):
        """超长会撑爆任何表格布局。"""
        from nxs.core.models import MAX_DISPLAY_NAME_CHARS

        with pytest.raises(ConfigError) as ei:
            self._card_with(config, display_name="长" * (MAX_DISPLAY_NAME_CHARS + 1))
        assert "超过" in str(ei.value)

    @covers("M3.F1")
    def test_description_has_its_own_limit(self, config):
        from nxs.core.models import MAX_DESCRIPTION_CHARS

        # 允许更长的简介
        card = self._card_with(config, description="简" * MAX_DESCRIPTION_CHARS)
        assert len(card.description) == MAX_DESCRIPTION_CHARS
        # 超一点就拒
        with pytest.raises(ConfigError):
            self._card_with(config, description="简" * (MAX_DESCRIPTION_CHARS + 1))

    @covers("M3.F1")
    def test_html_angle_brackets_are_kept(self, config):
        """★ ``<`` ``>`` 是**合法文本**，不该在这里被改掉。

        转义是渲染层的责任。若服务端擅自把 ``<`` 变成 ``&lt;``：
        * 合法的名字被破坏（``A <-> B`` 变成一堆实体）
        * 而且等于替所有下游决定了渲染方式
        """
        card = self._card_with(config, display_name="A <-> B",
                              description="处理 <任务> 与 & 符号")
        assert card.display_name == "A <-> B"
        assert "<" in card.description

    @covers("M3.F1")
    def test_whitespace_is_trimmed_and_empty_becomes_blank(self, config):
        card = self._card_with(config, display_name="  工作笔记本  ")
        assert card.display_name == "工作笔记本"
        # 纯空格 Trim 后为空 —— 不应该报错，只是没有名字
        assert self._card_with(config, display_name="   ").display_name == ""

    @covers("M3.F1")
    def test_non_string_is_rejected(self, config):
        with pytest.raises(ConfigError):
            self._card_with(config, display_name=123)

    @covers("M3.F1")
    def test_chinese_name_within_limit_passes(self, config):
        """★ 长度按**字符**算，不是字节。

        中文一字符三字节。若按字节限 100，就只够 33 个汉字——中文使用者吃亏。
        """
        name = "工" * 60      # 60 字符 = 180 字节，超过 100 字节但没超 100 字符
        assert len(name.encode("utf-8")) > 100
        card = self._card_with(config, display_name=name)
        assert card.display_name == name
