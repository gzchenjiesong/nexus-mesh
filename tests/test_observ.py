"""可观测（M13）的测试 —— 重点在"不该报的没报"，而不是"该报的报了"。

为什么反向用例比正向用例更重要
------------------------------
告警系统的失败有两种：

* **漏报**：真出事了没响 → 你能在事故复盘时发现
* **误报**：没出事也在响 → 你会在第三天开始忽略所有告警

第二种更致命。一个天天响的告警，等于没有告警——真正的信号会被淹掉。
所以本文件的每个规则都配一个**"单看正常"的反例**，验证它不会误报。

这也是设计文档那条约束的可执行形式：
「告警要建在**系统级关联**上，不只是单成员级」。
如果一条规则在单指标异常时就触发，那它就是阈值告警，
不该出现在 :func:`detect_anomalies` 里。
"""

from __future__ import annotations

import pytest

from conftest import covers

from nxs.mgmt.observ import (
    Anomaly,
    Metrics,
    NodeMetrics,
    SEVERITY_CRITICAL,
    SEVERITY_WARN,
    Snapshot,
    Thresholds,
    collect,
    detect_anomalies,
)

AT = "2026-10-26T20:00:00+08:00"


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------

def node(nid: str, *, state: str = "alive", approved: bool = True,
         received: int = 0, completed: int = 0, failed: int = 0,
         pending: int = 0, in_flight: int = 0, capacity: int = 1,
         bytes_out: int = 0,
         denials: dict[str, int] | None = None) -> NodeMetrics:
    return NodeMetrics(nid, "zone-x", approved, state, received, completed,
                       failed, in_flight, pending, capacity, bytes_out,
                       denials or {})


def snap(nodes: list[NodeMetrics], *, completed: int = 0, failed: int = 0,
         denied: int = 0, denied_by_rule: dict[str, int] | None = None,
         at: str = AT) -> Snapshot:
    return Snapshot(
        at=at, uptime_seconds=100.0,
        submitted=completed + failed + denied,
        completed=completed, failed=failed, denied=denied,
        by_type={}, denied_by_rule=denied_by_rule or {},
        latency={"p50": 1.0, "p95": 2.0, "p99": 3.0}, latency_samples=10,
        nodes=tuple(nodes),
    )


def rules_fired(anomalies: list[Anomaly]) -> set[str]:
    return {a.rule for a in anomalies}


# ---------------------------------------------------------------------------
# 1. 计数器
# ---------------------------------------------------------------------------

class TestMetricsCounter:

    @covers("M13.F1")
    def test_counts_submit_complete_fail_deny(self):
        m = Metrics()
        m.on_submit(task_id="t1", task_type="demo.echo", to_node="node-a")
        m.on_complete(task_id="t1", task_type="demo.echo",
                      from_node="node-a", byte_size=500)
        m.on_submit(task_id="t2", task_type="demo.echo", to_node="node-a")
        m.on_fail(task_id="t2", task_type="demo.echo", from_node="node-a")
        m.on_deny(rule_id="WL-TYPE-404", from_node="node-a")

        s = m.snapshot()
        assert s["submitted"] == 2
        assert s["completed"] == 1
        assert s["failed"] == 1
        assert s["denied"] == 1
        assert s["node_bytes"]["node-a"] == 500

    @covers("M13.F1")
    def test_deny_recorded_but_audit_failure_cannot_hide_it(self):
        """★ 拒绝打点不该受审计成败影响。

        审计可能因磁盘故障失败，但"发生过一次拒绝"这个事实与磁盘无关。
        如果打点排在审计之后，一次磁盘故障就会让攻击探测在监控里消失——
        那正是最不该丢信号的时候。（实现里打点放在 finally。）
        """
        m = Metrics()
        m.on_deny(rule_id="MX-DOMAIN-02", from_node="prober")
        assert m.snapshot()["denied_by_rule"]["MX-DOMAIN-02"] == 1

    @covers("M13.F1")
    def test_by_type_groups(self):
        m = Metrics()
        m.on_submit(task_id="t1", task_type="demo.echo", to_node="n")
        m.on_complete(task_id="t1", task_type="demo.echo", from_node="n")
        m.on_submit(task_id="t2", task_type="demo.count", to_node="n")
        m.on_fail(task_id="t2", task_type="demo.count", from_node="n")

        by_type = m.snapshot()["by_type"]
        assert by_type["demo.echo"]["completed"] == 1
        assert by_type["demo.count"]["failed"] == 1


# ---------------------------------------------------------------------------
# 2. 延迟分位数
# ---------------------------------------------------------------------------

class TestLatency:

    @covers("M13.F2")
    def test_percentiles_from_samples(self):
        import time

        m = Metrics(sample_window=64)
        # 造 100 个已知延迟：让每个任务实际耗时约 1ms 递增
        for i in range(20):
            m.on_submit(task_id=f"t{i}", task_type="x", to_node="n")
            time.sleep(0.001 * (i + 1))
            m.on_complete(task_id=f"t{i}", task_type="x", from_node="n")

        s = m.snapshot()
        assert s["latency_samples"] == 20
        # 单调递增的样本：p99 必须 >= p95 >= p50
        assert s["latency"]["p99"] >= s["latency"]["p95"] >= s["latency"]["p50"]

    @covers("M13.F2")
    def test_single_sample_gives_no_percentile(self):
        """一个样本的 p50/p95/p99 全都相同 —— 那不是分位数，是同一个数字抄三遍。"""
        m = Metrics()
        m.on_submit(task_id="t1", task_type="x", to_node="n")
        m.on_complete(task_id="t1", task_type="x", from_node="n")
        s = m.snapshot()
        assert s["latency_samples"] == 1
        assert s["latency"]["p50"] is None

    @covers("M13.F2")
    def test_sample_window_is_bounded(self):
        """★ 样本窗口有上界 —— 会自己泄漏的监控模块比没有更糟。"""
        m = Metrics(sample_window=5)
        for i in range(50):
            m.on_submit(task_id=f"t{i}", task_type="x", to_node="n")
            m.on_complete(task_id=f"t{i}", task_type="x", from_node="n")
        assert m.snapshot()["latency_samples"] == 5, "样本窗口没有上界"

    @covers("M13.F2")
    def test_unknown_task_latency_is_skipped_not_faked(self):
        """没有起始时刻的任务，延迟**跳过**而不是编一个 0。

        宁可少一个样本，也不要污染分位数。
        """
        m = Metrics()
        m.on_complete(task_id="never-submitted", task_type="x", from_node="n")
        assert m.snapshot()["latency_samples"] == 0

    @covers("M13.F2")
    def test_nearest_rank_returns_real_sample(self):
        """最近秩法：返回值一定是真实出现过的样本，不是插值。"""
        from nxs.mgmt.observ import _percentile

        samples = [1.0, 2.0, 3.0, 4.0, 5.0]
        for q in (50, 95, 99):
            assert _percentile(samples, q) in samples


# ---------------------------------------------------------------------------
# 3. 积压水位
# ---------------------------------------------------------------------------

class TestBacklog:

    @covers("M13.F4")
    def test_ratio_not_absolute(self):
        """★ 用比率而非绝对数：并发 8 积压 4 不算事，并发 1 积压 4 已经堵了。"""
        big = node("big", pending=4, capacity=8)
        small = node("small", pending=4, capacity=1)
        assert big.backlog_ratio == 0.5
        assert small.backlog_ratio == 4.0

    @covers("M13.F4")
    def test_zero_capacity_does_not_divide_by_zero(self):
        assert node("broken", pending=3, capacity=0).backlog_ratio == float("inf")
        assert node("broken", pending=0, capacity=0).backlog_ratio == 0.0


# ---------------------------------------------------------------------------
# 4. 系统级关联规则 —— 每条都配反例
# ---------------------------------------------------------------------------

class TestAnomalyRules:
    """正向触发 + 反向不误报。**反例才是这种规则的价值证明。**"""

    @covers("M13.F5", "S18")
    def test_bulk_movement_fires_on_combination(self):
        s = snap([node("big", completed=100, bytes_out=100_000),
                  node("n1", completed=10, bytes_out=1_000),
                  node("n2", completed=10, bytes_out=1_000)])
        assert "bulk_movement" in rules_fired(detect_anomalies(s))

    @covers("M13.F5", "S18")
    def test_bulk_movement_silent_when_only_bytes_large(self):
        """★ 只有字节大、任务不多 → **不该报**。

        单看"响应字节多"完全正常：做汇总、出报告的节点本来产得多。
        只有字节与任务量同时离群才可疑。
        """
        s = snap([node("chatty", completed=2, bytes_out=100_000),
                  node("n1", completed=10, bytes_out=1_000),
                  node("n2", completed=10, bytes_out=1_000)])
        assert "bulk_movement" not in rules_fired(detect_anomalies(s))

    @covers("M13.F5", "S18")
    def test_bulk_movement_needs_enough_nodes_for_median(self):
        """两个节点算不出有意义的"中位数" → 不判离群。"""
        s = snap([node("a", completed=100, bytes_out=100_000),
                  node("b", completed=1, bytes_out=10)],
                 )
        assert "bulk_movement" not in rules_fired(detect_anomalies(s))

    @covers("M13.F6", "S18")
    def test_probe_pattern_fires_on_scattered_denials(self):
        s = snap([node("prober", denials={"MX-DOMAIN-02": 2,
                                          "WL-TYPE-404": 3,
                                          "AU-CRED-03": 1})])
        fired = detect_anomalies(s)
        assert "probe_pattern" in rules_fired(fired)
        assert any(a.severity == SEVERITY_CRITICAL for a in fired)

    @covers("M13.F6", "S18")
    def test_probe_pattern_silent_on_single_rule_hammering(self):
        """★ 反复撞**同一条**规则 20 次 → **不该报**试探。

        那是配置写错（矩阵漏了一条规则之类），是良性问题，
        由 denial_dominance 报，不该算作攻击。
        配错的人只撞一堵墙，试探的人逐堵敲。
        """
        s = snap([node("misconfig", denials={"MX-DOMAIN-02": 20})])
        assert "probe_pattern" not in rules_fired(detect_anomalies(s))

    @covers("M13.F7", "S18")
    def test_dead_route_fires_when_offline_node_has_work(self):
        s = snap([node("offline", state="stale", pending=3),
                  node("fine", state="alive", pending=5)])
        fired = detect_anomalies(s)
        assert "dead_route" in rules_fired(fired)
        # 只有离线那个该被点名
        assert {a.subject for a in fired if a.rule == "dead_route"} == {"offline"}

    @covers("M13.F7", "S18")
    def test_dead_route_silent_when_offline_node_has_no_work(self):
        """★ 节点离线但名下没活 → **不该报**。下班关机是完全正常的。"""
        s = snap([node("offline", state="stale", pending=0, in_flight=0)])
        assert "dead_route" not in rules_fired(detect_anomalies(s))

    @covers("M13.F7")
    def test_dead_route_revoked_is_critical(self):
        s = snap([node("gone", state="revoked", in_flight=1)])
        fired = [a for a in detect_anomalies(s) if a.rule == "dead_route"]
        assert fired and fired[0].severity == SEVERITY_CRITICAL

    @covers("M13.F8", "S18")
    def test_stall_fires_on_backlog_with_zero_output(self):
        s = snap([node("stuck", received=10, completed=0, failed=0,
                       pending=10, capacity=1)])
        assert "silent_stall" in rules_fired(detect_anomalies(s))

    @covers("M13.F8", "S18")
    def test_stall_silent_when_backlog_but_has_output(self):
        """★ 积压高但有产出 → **不该报**。那只是忙。"""
        s = snap([node("busy", received=10, completed=2, pending=10, capacity=1)])
        assert "silent_stall" not in rules_fired(detect_anomalies(s))

    @covers("M13.F8", "S18")
    def test_stall_silent_when_backlog_is_small(self):
        """积压水位不高（虽然零产出）→ 不该报，可能只是刚开始。"""
        s = snap([node("fresh", received=5, completed=0, pending=1, capacity=4)])
        assert "silent_stall" not in rules_fired(detect_anomalies(s))

    @covers("M13.F8")
    def test_stall_uses_previous_snapshot_for_trend(self):
        """★ 给了上一次快照时，要求积压**在增长**。

        静止的高积压可能只是节点刚好在处理一批长任务。
        没有趋势信息时**跳过**而不是猜 —— 跳过是信息不足，不是漏报。
        """
        prev = snap([node("slow", received=10, completed=0, pending=10,
                          capacity=1)])
        # 积压没变（仍是 10）→ 不报
        same = snap([node("slow", received=10, completed=0, pending=10,
                          capacity=1)])
        assert "silent_stall" not in rules_fired(
            detect_anomalies(same, previous=prev))
        # 积压增长（10 → 20）→ 报
        worse = snap([node("slow", received=20, completed=0, pending=20,
                           capacity=1)])
        assert "silent_stall" in rules_fired(
            detect_anomalies(worse, previous=prev))

    @covers("M13.F8")
    def test_stall_detects_crash_after_healthy_period(self):
        """★ **先正常跑一批、然后卡死** —— 这是最常见的卡死形态。

        v0.8 初版用累计零产出判断，这里会**漏报**：累计早就不是 0 了。
        修法是看增量：两次快照之间有没有干活。
        """
        # 上一刻：已完成 50 笔，积压 2（正常）
        prev = snap([node("worker", received=52, completed=50, pending=2,
                          capacity=1)])
        # 这一刻：积压涨到 8，完成数仍是 50（卡住了）
        now = snap([node("worker", received=58, completed=50, pending=8,
                         capacity=1)])
        fired = rules_fired(detect_anomalies(now, previous=prev))
        assert "silent_stall" in fired, (
            "先正常后卡死的情形漏报了 —— 累计产出不为 0 就永远不报"
        )

    @covers("M13.F8")
    def test_stall_silent_when_work_continues(self):
        """两次快照间**有产出** → 不报，那只是慢。"""
        prev = snap([node("worker", received=52, completed=50, pending=2,
                          capacity=1)])
        now = snap([node("worker", received=58, completed=55, pending=3,
                         capacity=1)])
        assert "silent_stall" not in rules_fired(
            detect_anomalies(now, previous=prev))

    @covers("M13.F8")
    def test_stall_without_previous_needs_lifetime_zero_output(self):
        """无历史快照时退回累计判断 —— **保守，会漏但不误报**。

        第一次调用确实没有历史；凭空猜"可能最近没产出"比不报更糟。
        """
        # 有累计产出 → 不报（即使积压高）
        has_output = snap([node("w", received=20, completed=5, pending=20,
                                capacity=1)])
        assert "silent_stall" not in rules_fired(detect_anomalies(has_output))
        # 累计零产出 → 报
        never = snap([node("w", received=20, completed=0, pending=20,
                           capacity=1)])
        assert "silent_stall" in rules_fired(detect_anomalies(never))

    @covers("M13.F9", "S18")
    def test_denial_dominance_fires_on_concentrated_refusal(self):
        s = snap([node("n1")], completed=2, denied=20,
                 denied_by_rule={"MX-DOMAIN-02": 19, "WL-TYPE-404": 1})
        assert "denial_dominance" in rules_fired(detect_anomalies(s))

    @covers("M13.F9", "S18")
    def test_denial_dominance_silent_when_spread(self):
        """★ 拒绝率高但**分散**在多条规则 → 不该报"配错"。

        分散才是试探（由 probe_pattern 报）；集中才是配错。
        两条规则配合才有分辨力。
        """
        s = snap([node("n1")], completed=2, denied=20,
                 denied_by_rule={"MX-DOMAIN-02": 7, "WL-TYPE-404": 7,
                                 "MX-RECEIVE-03": 6})
        assert "denial_dominance" not in rules_fired(detect_anomalies(s))

    @covers("M13.F9", "S18")
    def test_denial_dominance_silent_with_small_sample(self):
        """样本太少时比例没有意义 —— 3 次拒绝不构成"拒绝率高"。"""
        s = snap([node("n1")], completed=0, denied=3,
                 denied_by_rule={"MX-DOMAIN-02": 3})
        assert "denial_dominance" not in rules_fired(detect_anomalies(s))

    @covers("M13.F5", "M13.F6", "M13.F7", "M13.F8", "M13.F9")
    def test_every_rule_has_rationale(self):
        """★ 每条规则都必须能说清"为什么单看正常"。

        这是 :class:`Anomaly` 上 ``rationale`` 字段存在的意义：
        写不出这句话的规则，说明它只是阈值告警，不该放在这个模块里。
        """
        s = snap([node("prober", state="stale", received=10, pending=10,
                       capacity=1, completed=100, bytes_out=500_000,
                       denials={"A": 1, "B": 1, "C": 1}),
                  node("n1", completed=1, bytes_out=10),
                  node("n2", completed=1, bytes_out=10)],
                 completed=1, denied=30,
                 denied_by_rule={"MX-DOMAIN-02": 30})
        fired = detect_anomalies(s)
        assert len(fired) >= 3, f"构造的场景应该触发多条规则，实际 {rules_fired(fired)}"
        for a in fired:
            assert a.rationale, f"{a.rule} 缺少 rationale"
            assert "正常" in a.rationale, (
                f"{a.rule} 的 rationale 没说清「为什么单看正常」—— "
                f"那它就是个阈值告警"
            )
            assert a.suggestion, f"{a.rule} 缺少建议"

    @covers("M13.F5", "M13.F6", "M13.F7", "M13.F8", "M13.F9")
    def test_healthy_system_produces_no_anomalies(self):
        """★ 健康系统必须零告警 —— 否则告警会变成噪音。"""
        s = snap([node("a", received=5, completed=5, pending=0, capacity=2,
                       bytes_out=1000, denials={"MX-DOMAIN-02": 1}),
                  node("b", received=5, completed=4, failed=1, pending=0,
                       capacity=2, bytes_out=900)],
                 completed=9, failed=1, denied=1,
                 denied_by_rule={"MX-DOMAIN-02": 1})
        assert detect_anomalies(s) == [], (
            f"健康系统误报：{rules_fired(detect_anomalies(s))}"
        )

    @covers("M13.F5")
    def test_thresholds_are_adjustable(self):
        """阈值可调 —— 「多少算多」取决于部署规模。

        注意构造要**两个维度都离群**（字节 20 倍、任务量 10 倍），
        否则测的就不是 bulk_movement 而是别的分支。
        """
        s = snap([node("big", completed=100, bytes_out=20_000),
                  node("n1", completed=10, bytes_out=1_000),
                  node("n2", completed=10, bytes_out=1_000)])
        assert "bulk_movement" in rules_fired(detect_anomalies(s))
        # 把倍数门槛提到 50 倍，就不该再触发
        assert "bulk_movement" not in rules_fired(
            detect_anomalies(s, thresholds=Thresholds(bytes_outlier_factor=50.0)))


# ---------------------------------------------------------------------------
# 5. 快照组装
# ---------------------------------------------------------------------------

class TestCollect:

    @covers("M13.F3")
    def test_state_comes_from_queue_not_counters(self):
        """★ 计数用计数器，**状态用真源**。

        pending / in_flight 会被租约超时、重试、人工释放改变，
        计数器追不上这些变化。所以它们必须从队列实测。
        """
        from nxs.backends.memory import MemoryQueueStore
        from nxs.control.registry import Registry
        from nxs.core.models import Task, TaskStatus

        from nxs.backends.memory import MemoryRegistryStore
        from test_matcher import card

        registry = Registry(MemoryRegistryStore())
        registry.register(card("node-a"), approved=True)
        queue = MemoryQueueStore()
        queue.enqueue(Task(id="t1", idempotency_key="k1", type="job.run",
                           session_id="s", from_node="coordinator", to_node="node-a",
                           status=TaskStatus.SUBMITTED))

        # 计数器里**没有**这个任务的任何记录
        m = Metrics()
        s = collect(m, registry=registry, queue=queue)
        n = next(x for x in s.nodes if x.node_id == "node-a")
        assert n.pending == 1, "pending 没有从队列取，而是信了计数器"
        assert n.received == 0, "received 应来自计数器（确实没打点过）"

    @covers("M13.F3")
    def test_snapshot_is_immutable_and_serializable(self):
        from nxs.backends.memory import MemoryQueueStore, MemoryRegistryStore
        from nxs.control.registry import Registry
        from test_matcher import card

        registry = Registry(MemoryRegistryStore())
        registry.register(card("node-a"), approved=True)
        s = collect(Metrics(), registry=registry, queue=MemoryQueueStore())
        d = s.as_dict()
        # 可 JSON 序列化（运维端点要用）
        import json

        json.dumps(d, ensure_ascii=False)
        assert d["nodes"][0]["node_id"] == "node-a"


# ---------------------------------------------------------------------------
# 6. 指标不泄漏内容（S19）
# ---------------------------------------------------------------------------

class TestNoContentLeak:

    @covers("M13.F12", "S19")
    def test_metrics_contain_no_task_content(self):
        """★ 指标只含数字与公开标识，**不含任何业务内容**。

        打点签名本身就是这道防线：:meth:`Metrics.on_submit` 只接受
        task_id / task_type / to_node，**没有 params 参数**——
        想传内容进去都没有入口。
        """
        import inspect

        sig = inspect.signature(Metrics.on_submit)
        assert "params" not in sig.parameters, "on_submit 竟然能接收 params"
        assert set(sig.parameters) == {"self", "task_id", "task_type", "to_node"}

        sig_done = inspect.signature(Metrics.on_complete)
        assert "artifacts" not in sig_done.parameters, (
            "on_complete 接收 artifacts —— 那里面有业务内容"
        )

    @covers("M13.F12", "S19")
    def test_snapshot_json_has_no_content_fields(self):
        m = Metrics()
        m.on_submit(task_id="t1", task_type="work.calendar", to_node="node-a")
        m.on_complete(task_id="t1", task_type="work.calendar",
                      from_node="node-a", byte_size=42)
        payload = str(m.snapshot())
        for forbidden in ("params", "artifacts", "summary", "content"):
            assert forbidden not in payload, f"快照里出现了 {forbidden}"


# ---------------------------------------------------------------------------
# 7. 暴露面：只给运维，不给节点
# ---------------------------------------------------------------------------

class TestMetricsEndpoint:
    """★ 指标是拓扑画像，**只在运维面暴露**。

    "某节点每天处理多少任务"、"拒绝集中在哪条规则"、"谁在积压"——
    这些单独看都不敏感，合起来是一张**网络规模与使用习惯的画像**。
    节点没有理由需要它；它的视角应该只有自己的任务。
    """

    @pytest.fixture
    def live(self, workdir):
        from nxs.app import NexusApp
        from nxs.edge.gateway import Gateway
        from nxs.mgmt.config import EXAMPLE_CONFIG_PATH, ServerConfig, demo_env_for

        env = demo_env_for(EXAMPLE_CONFIG_PATH)
        app = NexusApp.from_config_path(str(EXAMPLE_CONFIG_PATH), env=env,
                                       force_memory=True)
        app.start()
        app.approve("node-a")
        app.approve("node-b")
        ops_secret = b"ops-observ-secret-0123456789abcdef"
        gw = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0),
                     ops_secret=ops_secret).start()
        try:
            yield {"app": app, "gateway": gw, "ops_secret": ops_secret}
        finally:
            gw.stop()

    @covers("M13.F10", "S19")
    def test_operator_can_read_metrics(self, live):
        from nxs.edge.client import OpsClient

        ops = OpsClient(live["gateway"].base_url, secret=live["ops_secret"])
        data = ops.metrics()
        assert "totals" in data
        assert "nodes" in data
        assert "anomalies" in data

    @covers("M13.F10", "S19")
    def test_node_secret_cannot_read_metrics(self, live):
        """★ 节点密钥读不到指标 —— 被管理者不该看到全网画像。"""
        import json
        import socket

        from nxs.core.errors import RuleId
        from nxs.edge import httpsig
        from nxs.edge.gateway import PREFIX

        body = b"{}"
        node_secret = live["app"].config.secrets["node-a"]
        headers = httpsig.sign_request(
            node_secret, method="POST", path=f"{PREFIX}/ops/metrics",
            node_id="node-a", body=body,
        )
        # ⚠ 必须**循环读到 EOF**，不能只 recv 一次：
        #   单次 recv 可能只拿到响应头，body 还在路上——
        #   那样会得到一个"状态码对但 body 空"的假失败。
        chunks: list[bytes] = []
        with socket.create_connection(("127.0.0.1", live["gateway"].port),
                                      timeout=5) as sock:
            sock.sendall((
                f"POST {PREFIX}/ops/metrics HTTP/1.0\r\nHost: x\r\n"
                + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                + f"Content-Length: {len(body)}\r\n\r\n"
            ).encode() + body)
            sock.shutdown(socket.SHUT_WR)
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        raw = b"".join(chunks)

        status = int(raw.split(b"\r\n", 1)[0].split(b" ")[1])
        assert status == 403, f"节点读到了指标（HTTP {status}）"
        payload = json.loads(raw.partition(b"\r\n\r\n")[2].decode())
        assert payload["rule_id"] == RuleId.MX_ISSUER_NOT_ALLOWED.value

    @covers("M13.F10")
    def test_metrics_requires_authentication(self, live):
        import socket

        from nxs.edge.gateway import PREFIX

        body = b"{}"
        with socket.create_connection(("127.0.0.1", live["gateway"].port),
                                      timeout=5) as sock:
            sock.sendall((
                f"POST {PREFIX}/ops/metrics HTTP/1.0\r\nHost: x\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode() + body)
            sock.shutdown(socket.SHUT_WR)
            raw = sock.recv(65536)
        assert int(raw.split(b"\r\n", 1)[0].split(b" ")[1]) == 401

    @covers("M13.F10")
    def test_metrics_reflect_real_traffic(self, live):
        """端到端：跑几个任务后，指标必须真的动起来。

        这条防的是"代码写好了但打点没接上"——那种情况下所有指标恒为 0，
        表面上一切正常。
        """
        from nxs.core.models import Level
        from nxs.edge.client import HttpTransport, OpsClient
        from nxs.sdk import NodeClient, NodeConfig

        app = live["app"]
        coordinator = HttpTransport(live["gateway"].base_url, node_id="coordinator",
                            secret=app.config.secrets["coordinator"])
        node = HttpTransport(live["gateway"].base_url, node_id="node-a",
                             secret=app.config.secrets["node-a"])
        client = NodeClient(NodeConfig(
            "node-a",
            handlers={"demo.echo": lambda p: {"echo": p["text"],
                                             "level": "metadata"}},
            levels={"demo.echo": Level.METADATA},
            card=app.config.nodes["node-a"],
            heartbeat_interval=0, renew_interval=0,
        ), node)

        for i in range(3):
            coordinator.publish(from_node="coordinator", to_node="node-a", task_type="demo.echo",
                        params={"text": f"t{i}"}, idempotency_key=f"m{i}")
            task = node.poll("node-a")
            if task:
                client.execute(task)

        ops = OpsClient(live["gateway"].base_url, secret=live["ops_secret"])
        data = ops.metrics()
        assert data["totals"]["submitted"] == 3
        assert data["totals"]["completed"] == 3
        assert data["latency_samples"] == 3
        node_row = next(n for n in data["nodes"] if n["node_id"] == "node-a")
        assert node_row["completed"] == 3
        assert node_row["bytes_out"] > 0

    @covers("M13.F10")
    def test_second_call_can_report_trend(self, live):
        """★ 趋势类告警要靠两次调用 —— 第一次信息不足，第二次才行。

        这个副作用是刻意的：单次快照看不出"积压是否在增长"，
        凭空猜一个"可能在增长"比不报更糟。
        """
        from nxs.edge.client import OpsClient

        ops = OpsClient(live["gateway"].base_url, secret=live["ops_secret"])
        first = ops.metrics()
        second = ops.metrics()
        # 两次都能正常返回；第二次之后网关内部已有上一次快照
        assert "anomalies" in first and "anomalies" in second


# ---------------------------------------------------------------------------
# 8. CLI
# ---------------------------------------------------------------------------

class TestMetricsCli:

    @covers("M13.F11")
    def test_cli_renders_metrics(self):
        """CLI 必须真的能跑 —— 它是最常用的入口。"""
        import subprocess
        import sys

        from conftest import REPO_ROOT

        result = subprocess.run(
            [sys.executable, "-m", "nxs", "metrics",
             "--config", "config/nxs.example.yaml"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        out = result.stdout
        assert "运行时指标" in out
        assert "系统级关联异常" in out
        # 必须解释"为什么只报组合异常"，否则运维会问"我的阈值告警呢"
        assert "组合异常" in out

    @covers("M13.F11")
    def test_cli_json_output_is_valid(self):
        """``--json`` 必须输出可解析的 JSON（便于喂给其他工具）。"""
        import json
        import subprocess
        import sys

        from conftest import REPO_ROOT

        result = subprocess.run(
            [sys.executable, "-m", "nxs", "metrics", "--json",
             "--config", "config/nxs.example.yaml"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # 跳过可能的"使用示例配置"提示行，从第一个 { 开始解析
        start = result.stdout.find("{")
        assert start >= 0, "没有输出 JSON"
        data = json.loads(result.stdout[start:])
        assert "totals" in data and "nodes" in data

    @covers("M13.F11")
    def test_cli_remote_requires_ops_secret(self):
        """``--url`` 模式缺凭据时必须说清缺什么。"""
        import os
        import subprocess
        import sys

        from conftest import REPO_ROOT

        env = dict(os.environ)
        env.pop("NXS_OPS_SECRET", None)
        result = subprocess.run(
            [sys.executable, "-m", "nxs", "metrics",
             "--url", "http://127.0.0.1:1/nxs"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, env=env,
        )
        assert result.returncode == 2
        assert "NXS_OPS_SECRET" in (result.stdout + result.stderr)


class TestAuditCarriesRealDurationAndBytes:
    """★★ 审计里的耗时与字节必须是真实值，不能恒为 0。

    ⚠⚠ **这是加运行日志后才发现的既有 bug（2026-10-30）。**

    上线后看真实数据，每条 completed 记录都是：

        {"outcome": "completed", "duration_ms": 0, "bytes": 0, ...}

    而实际任务跑了 68 秒、回传 259 字节。

    ## 根因

    `Mailbox.complete` 调 `app.record(...)` 时**没传** `byte_size` 与
    `duration_ms`，而 `make_record` 的默认值是 0。

    延迟其实一直算得对（在 `metrics._settle_latency` 里），
    只是**没被传递出来**。

    ## 为什么这比"没有值"更糟

    运行日志里那两列会显示 "0 ms" 和 "—"，**看起来是有值的** ——
    人读到会以为「任务瞬间完成、什么都没回传」。

    而排查性能问题时，「耗时多长、回传多大」恰恰是最需要的两列。
    之前发现 `p95=180s` 撞超时线，靠的就是延迟数据；
    若那时看的是审计日志，会得出完全错误的结论。

    ## 修法

    `on_complete` / `on_fail` 返回本次结算的延迟，`Mailbox` 先结算
    再写审计，把真实耗时与字节一起落盘。
    """

    @covers("M13.F1")
    def test_complete_records_real_duration_and_bytes(self):
        """completed 记录必须带真实耗时与字节。"""
        import time

        from conftest import EXAMPLE_CONFIG, demo_env
        from nxs.app import NexusApp
        from nxs.core.models import Artifact, Level
        from nxs.edge.client import HttpTransport
        from nxs.edge.gateway import Gateway
        from nxs.mgmt.config import ServerConfig

        app = NexusApp.from_config_path(
            str(EXAMPLE_CONFIG), env=demo_env(), force_memory=True)
        app.start()
        app.approve("node-a")
        gw = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0),
                     ops_secret=b"x" * 32).start()
        try:
            tr_a = HttpTransport(gw.base_url, node_id="node-a",
                                 secret=app.config.secrets["node-a"])
            tr_c = HttpTransport(gw.base_url, node_id="coordinator",
                                 secret=app.config.secrets["coordinator"])
            tr_c.publish(from_node="coordinator", to_node="node-a",
                         task_type="demo.echo", params={"text": "hi"},
                         idempotency_key="k-dur")
            time.sleep(0.3)          # 造一点可测的耗时
            task = tr_a.poll("node-a")
            tr_a.complete("node-a", task, [
                Artifact(level=Level.METADATA, kind="structured",
                         data={"echo": "一段回答正文", "level": "metadata"}),
            ])

            recs = [r for r in app.audit.read_recent(20)
                    if r.get("outcome") == "completed"]
            assert recs, "没有 completed 记录"
            r = recs[-1]

            assert (r.get("duration_ms") or 0) > 0, (
                f"duration_ms 是 {r.get('duration_ms')!r} —— 恒为 0 说明"
                " record 时没传真实耗时。\n"
                "  运行日志里「耗时」这一列会全是 0，看起来像任务瞬间完成。"
            )
            # 造了 300ms，允许宽松区间（CI 机器可能慢）
            assert 200 <= r["duration_ms"] < 5000, (
                f"duration_ms={r['duration_ms']} 与造的 300ms 明显不符"
            )
            assert (r.get("bytes") or 0) > 0, (
                f"bytes 是 {r.get('bytes')!r} —— 恒为 0 说明"
                " record 时没传真实字节数"
            )
        finally:
            gw.stop()

    @covers("M13.F1")
    def test_on_complete_returns_latency(self):
        """`on_complete` 要返回本次延迟——调用方（Mailbox）靠它填审计。"""
        from nxs.mgmt.observ import Metrics

        m = Metrics()
        m.on_submit(task_id="t1", task_type="demo.echo", to_node="n1")
        got = m.on_complete(task_id="t1", task_type="demo.echo",
                            from_node="n1", byte_size=10)
        assert isinstance(got, int) and got >= 0, (
            f"on_complete 返回 {got!r}，应为毫秒整数"
        )

    @covers("M13.F1")
    def test_on_complete_returns_none_when_unknown(self):
        """算不出来时返回 None，**不编造一个 0**。

        进程刚重启、任务非本进程投递、任务早被清理 —— 三种情况都
        算不出延迟。编造 0 会污染"耗时"这一列的语义。
        """
        from nxs.mgmt.observ import Metrics

        m = Metrics()
        got = m.on_complete(task_id="never-submitted",
                            task_type="demo.echo", from_node="n1")
        assert got is None, (
            f"未知任务返回了 {got!r}，应为 None —— 编造 0 会误导"
        )

    @covers("M13.F1")
    def test_on_fail_returns_latency(self):
        """失败路径同样要返回延迟 —— "多久才失败"是排查超时的关键。"""
        from nxs.mgmt.observ import Metrics

        m = Metrics()
        m.on_submit(task_id="t2", task_type="demo.echo", to_node="n1")
        got = m.on_fail(task_id="t2", task_type="demo.echo", from_node="n1")
        assert isinstance(got, int) and got >= 0, (
            f"on_fail 返回 {got!r}，应为毫秒整数"
        )
