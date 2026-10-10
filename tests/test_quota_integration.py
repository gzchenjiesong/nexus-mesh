"""配额在真实装配下是否生效。

这组测试的存在理由是：配额曾经是一个**假实现**——它统计"注册记录数"
冒充任务数，结果永远通过，从外部完全看不出来。这类"看起来在工作、
实际不对"的代码比缺失更危险，因此需要端到端的验证。

验证方式：真的把配额填满，然后确认投递被拒。
"""

from __future__ import annotations

import dataclasses

import pytest

from conftest import covers

from nxs.app import NexusApp
from nxs.core.errors import PolicyDenied, RuleId


@pytest.fixture
def app(config):
    a = NexusApp(config, _stores(config))
    a.start()
    for mid in ("node-a", "node-b"):
        a.approve(mid)
    return a


def _stores(config):
    from nxs.app import build_stores
    return build_stores(config, force_memory=True)


class TestConcurrencyQuota:
    """并发配额是精确的——它直接查队列里 working 状态的任务数。"""

    @covers("M5.F7")
    def test_concurrent_limit_blocks_second_task(self, app):
        """node-a 的 concurrent_tasks=1：领走一个后不能再投第二个。"""
        app.transport.publish(from_node="hub", to_node="node-a",
                              task_type="demo.echo", params={"text": "1"},
                              idempotency_key="c1")
        assert app.usage_of("node-a").concurrent_now == 0

        claimed = app.mailbox.poll("node-a")
        assert claimed is not None
        assert app.usage_of("node-a").concurrent_now == 1

        with pytest.raises(PolicyDenied) as ei:
            app.transport.publish(from_node="hub", to_node="node-a",
                                  task_type="demo.echo", params={"text": "2"},
                                  idempotency_key="c2")
        assert ei.value.rule_id == RuleId.QT_CONCURRENCY

    @covers("M5.F7")
    def test_concurrency_frees_after_completion(self, app):
        """任务完成后并发额度立即释放。"""
        from nxs.core.models import Artifact, Level
        app.transport.publish(from_node="hub", to_node="node-a",
                              task_type="demo.echo", params={"text": "1"},
                              idempotency_key="c1")
        task = app.mailbox.poll("node-a")
        app.transport.complete("node-a", task, [
            Artifact(level=Level.METADATA, kind="structured",
                     data={"echo": "1", "level": "metadata"})])

        assert app.usage_of("node-a").concurrent_now == 0
        app.transport.publish(from_node="hub", to_node="node-a",
                              task_type="demo.echo", params={"text": "2"},
                              idempotency_key="c2")   # 不应抛异常

    @covers("M5.F7")
    def test_usage_reflects_real_queue_state(self, app):
        """用量不是估算值——直接反映后端队列的真实状态。"""
        assert app.usage_of("node-a").concurrent_now == 0
        for i in range(1):
            app.transport.publish(from_node="hub", to_node="node-a",
                                  task_type="demo.echo", params={"text": str(i)},
                                  idempotency_key=f"k{i}")
        app.mailbox.poll("node-a")
        assert app.usage_of("node-a").concurrent_now == 1
        assert app.usage_of("node-a").tasks_today >= 1


class TestDailyQuota:
    """按日任务数配额。"""

    @covers("M5.F7")
    def test_daily_count_is_real(self, app):
        """tasks_today 取自后端统计，不是常数 0 或 1。"""
        for i in range(3):
            app.transport.publish(from_node="hub", to_node="node-b",
                                  task_type="demo.echo", params={"text": str(i)},
                                  idempotency_key=f"d{i}")
        assert app.usage_of("node-b").tasks_today == 3

    @covers("M5.F7", "M11.F1")
    def test_daily_quota_blocks_when_exhausted(self, config):
        """把配额调到很小，确认投递真的被拦住。"""
        nodes = dict(config.nodes)
        card = nodes["node-b"]
        tight = dataclasses.replace(
            card,
            constraints=dataclasses.replace(
                card.constraints,
                quota=dataclasses.replace(card.constraints.quota, tasks_per_day=2),
            ),
        )
        nodes["node-b"] = tight
        cfg = dataclasses.replace(config, nodes=nodes)

        a = NexusApp(cfg, _stores(cfg))
        a.start()
        a.approve("node-b")

        for i in range(2):
            a.transport.publish(from_node="hub", to_node="node-b",
                                task_type="demo.echo", params={"text": str(i)},
                                idempotency_key=f"q{i}")

        with pytest.raises(PolicyDenied) as ei:
            a.transport.publish(from_node="hub", to_node="node-b",
                                task_type="demo.echo", params={"text": "over"},
                                idempotency_key="q-over")
        assert ei.value.rule_id == RuleId.QT_TASKS_PER_DAY


class TestNoFakeStats:
    """回归：确保用量统计不会被改回"看起来合理的常数"。"""

    @covers("M5.F7")
    def test_usage_changes_with_activity(self, app):
        before = app.usage_of("node-b").tasks_today
        app.transport.publish(from_node="hub", to_node="node-b",
                              task_type="demo.echo", params={"text": "x"},
                              idempotency_key="stat-1")
        after = app.usage_of("node-b").tasks_today
        assert after == before + 1, "用量统计必须随活动变化"

    @covers("M6.R1")
    def test_denied_task_does_not_consume_quota(self, app):
        """被拒的投递不应消耗配额——否则可以用拒绝把额度刷满。"""
        before = app.usage_of("node-a").tasks_today
        with pytest.raises(PolicyDenied):
            app.transport.publish(from_node="hub", to_node="node-a",
                                  task_type="demo.unknown", params={})
        assert app.usage_of("node-a").tasks_today == before
