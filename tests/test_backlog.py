"""信箱深度检查（权限模型改造 Step 5b）。

## 这个文件在测什么，以及为什么它值得单独一个文件

旧模型里有四种"配额"：按日任务数、单次应答字节、按日字节、并发数。
**它们从未真正生效** —— 而根因是把"节点自己的防御"当成了"网络的管制"：

    命令在节点本机执行，网络看不见也管不了它用多少资源。
    "网络允许你干多少"这个问法本身就是错的。

现在只剩一个数：`self_limits.max_backlog`（信箱深度，缺省 20）。
它**真的会被执行**，所以必须有行为测试。

## 与旧 `test_quota_integration.py` 的关系

那个文件已删除。它测的三件事在这里的去向：

    test_concurrent_limit_blocks_second_task  → 删除（并发不再设上限）
    test_daily_quota_blocks_when_exhausted    → 删除（按日配额不存在）
    test_usage_reflects_real_queue_state      → 本文件的 backlog 测试
"""

from __future__ import annotations

import pytest

from nxs.core.errors import PolicyDenied, RuleId
from nxs.core.models import RoleCard
from tests.test_traceability import covers


def _card(node_id: str = "node-a", *, backlog: int = 20) -> RoleCard:
    return RoleCard.from_dict({
        "node_id": node_id,
        "domain": "zone-a",
        "display_name": "A",
        "roles": {"executor": True},
        "capabilities": [{
            "type": "demo.echo", "level": "summary", "mode": "sync",
            "response_fields": ["echo", "level"],
        }],
        "constraints": {
            "max_response_level": "summary",
            "can_issue_types": [],
            "can_receive_from": ["cloud"],
        },
        "self_limits": {"max_backlog": backlog},
    })


def _engine(cards: list[RoleCard]):
    from nxs.control.policy import PolicyEngine
    from nxs.mgmt.config import NexusConfig

    cfg = NexusConfig()
    cfg.nodes = {c.node_id: c for c in cards}
    return PolicyEngine(cfg)


class TestBacklogIsEnforced:
    """★ 信箱满了必须真的拒收 —— 这是本步唯一新增的强制点。"""

    @covers("M5.F7")
    def test_below_limit_passes(self):
        eng = _engine([_card(backlog=5)])
        eng.check_backlog("node-a", pending=4)      # 不抛

    @covers("M5.F7")
    def test_at_limit_rejected(self):
        """★ `pending == limit` 就拒，不是 `> limit`。

        为什么：limit 的语义是"我最多攒这么多"。攒到 20 个时再发一个
        就变 21 个 —— 所以 20 个时已经该拒。

        这个边界很容易写成 `>`，那样实际能攒到 limit+1。
        """
        eng = _engine([_card(backlog=5)])
        with pytest.raises(PolicyDenied) as ei:
            eng.check_backlog("node-a", pending=5)
        assert ei.value.rule_id == RuleId.QT_BACKLOG_FULL

    @covers("M5.F7")
    def test_over_limit_rejected(self):
        eng = _engine([_card(backlog=5)])
        with pytest.raises(PolicyDenied):
            eng.check_backlog("node-a", pending=50)

    @covers("M5.F7")
    def test_default_limit_is_20(self):
        """未声明 max_backlog 的卡片，上限是 20。

        保护默认开启 —— 留一个"不限"的默认等于让所有人默认不受保护。
        """
        card = RoleCard.from_dict({
            "node_id": "n", "domain": "d", "display_name": "N",
            "roles": {"executor": True},
            "capabilities": [{"type": "demo.echo", "level": "summary",
                              "mode": "sync", "response_fields": ["echo"]}],
            "constraints": {"max_response_level": "summary"},
        })
        assert card.self_limits.max_backlog == 20

        eng = _engine([card])
        eng.check_backlog("n", pending=19)          # 19 可以
        with pytest.raises(PolicyDenied):
            eng.check_backlog("n", pending=20)      # 20 满了


class TestBacklogErrorCode:
    """错误码的语义必须正确 —— 它决定发布方怎么应对。"""

    @covers("M5.F7")
    def test_uses_qt_backlog_not_mx(self):
        """★ 必须是 `QT-BACKLOG-07`，不能是任何 `MX-*`。

        区别很重要：

            MX-*           → "你没权限"  → 发布方会去求运维改配置
            QT-BACKLOG-07  → "它来不及干" → 发布方会稍后重试或换节点

        如果错用 MX-*，发布方会以为需要审批，而实际只是等一会儿。
        """
        eng = _engine([_card(backlog=1)])
        with pytest.raises(PolicyDenied) as ei:
            eng.check_backlog("node-a", pending=1)
        rule = ei.value.rule_id
        assert rule == RuleId.QT_BACKLOG_FULL
        assert rule.value.startswith("QT-"), (
            f"信箱满用了 {rule.value} —— 这是资源问题，不是权限问题"
        )

    @covers("M5.F7")
    def test_wire_message_does_not_leak_counts(self):
        """★ 对外响应**不该**带具体数字 —— 这是既有的安全设计。

        ⚠ 这条测试我一开始写反了：要求 message 里带 `pending=3/3`。

        但 `to_wire()` 的注释写得很清楚：**只含 rule_id 与固定消息**。
        原因是不泄露内部状态 —— 否则错误响应本身变成探测通道，
        调用方能借此摸清对端的队列深度、配额、内部标识。

        想看具体数字的场景是**本地调试**，那时用 `str(exc)` 或日志；
        对外只有 `rule_id`。
        """
        eng = _engine([_card(backlog=3)])
        with pytest.raises(PolicyDenied) as ei:
            eng.check_backlog("node-a", pending=3)
        wire = ei.value.to_wire()
        assert set(wire) == {"denied", "rule_id", "message"}
        assert "3" not in wire["message"], (
            f"对外消息泄露了内部数字：{wire['message']!r}"
        )
        # 数字只出现在异常的 detail 里（本地可见，按注释说明"仅用于本地日志"）
        assert "3" in (ei.value.detail or ""), (
            f"detail 应该带数字方便本地排查，实际 {ei.value.detail!r}"
        )


class TestQuotaIsGone:
    """★ 配额必须真的不存在了 —— 防止有人加回来。

    这不是"洁癖"：那几项配额**从未生效**，而"配了但不生效"比没有
    更危险 —— 它给人虚假的安全感，让人以为资源被管控了。
    """

    @covers("M5.F7")
    def test_constraints_has_no_quota_field(self):
        import dataclasses

        from nxs.core.models import Constraints

        names = {f.name for f in dataclasses.fields(Constraints)}
        assert "quota" not in names, (
            "`constraints.quota` 又出现了。\\n"
            "  那几项配额从未生效（Mesh 无法强制节点本地资源），\\n"
            "  要做限额请用 self_limits —— 它是节点自律，语义诚实。"
        )

    @covers("M5.F7")
    def test_quota_error_codes_are_gone(self):
        """★ 四个配额错误码必须不存在。

        它们代表"网络拒绝给你资源"这件事 —— 而网络从来没做过这件事。
        留着它们会让读代码的人以为存在这个机制。
        """
        from nxs.core.errors import RuleId

        gone = ["QT-TASKS-01", "QT-BYTE-02", "QT-BYTE-03", "QT-CONC-05"]
        values = {r.value for r in RuleId}
        still = [c for c in gone if c in values]
        assert not still, (
            f"这些配额错误码又回来了：{still}\\n"
            f"  它们对应的检查从未生效，恢复它们等于恢复一个谎言"
        )

    @covers("M5.F7")
    def test_mesh_self_protection_survives(self):
        """★ 反向：**Mesh 自我保护**两项必须留着。

        它们与节点配额是两回事：

            节点配额   → "你还能不能干活"   → 归节点自己管
            Mesh 自保  → "网络会不会被你拖垮" → 必须由 Mesh 强制

        删配额时很容易顺手把这两个也删掉 —— 那会让单个节点
        打爆网关（速率）或耗尽文件描述符（连接数）。
        """
        from nxs.core.errors import RuleId

        values = {r.value for r in RuleId}
        assert "QT-RATE-04" in values, "请求速率限制被误删了 —— 网关会被打爆"
        assert "QT-CONN-06" in values, "连接数限制被误删了 —— 文件描述符会耗尽"
