"""可观测（M13）—— 指标、分位数、以及**系统级关联**异常。

它和审计（M12）的分工
--------------------
这两个模块容易混，但它们要回答完全不同的问题：

======================  ==========================================
审计 M12                 可观测 M13
======================  ==========================================
**事后追溯**：三个月前谁给谁派过活      **此刻状态**：现在系统健康吗
append-only、需落盘、可取证           内存计数、低成本、会丢
读它要扫文件（O(n)）                  读它是取一个内存快照（O(1)）
======================  ==========================================

所以指标**不读审计日志**。这不是偷懒——审计是 NDJSON 追加文件，
长期运行后按 10 万行去算 p95，每次查询都要把整个文件读进内存。
把两种用途混在一起，会让"看一眼监控"变成一次磁盘扫描。

代价：**进程重启后指标归零**。这是刻意的取舍——指标是滚动观测，
不是账本。要跨重启的长期趋势，那属于审计与外部监控系统的事。

⭐ 设计重点：告警建在"组合异常"上
--------------------------------
设计文档的关键约束是「告警要建在**系统级关联**上，不只是单成员级」。

这条约束值得认真对待，因为**单指标阈值告警基本没用**：
"响应字节 > 1MB 告警"会在正常的大文件任务上天天响；
"请求频率 > 100/s 告警"会在正常批量任务上响。
响多了就没人看了，于是真正的异常也被淹没。

真正有价值的是**组合信号**：

* 某节点响应体积大 —— 正常（它可能就是个做汇总的节点）
* 某节点请求频率高 —— 正常（它可能今天活多）
* **两者同时出现** —— 可疑：像是在批量搬运数据

单看每个都正常，组合起来才可疑。这就是本模块
:func:`detect_anomalies` 里每条规则的形态：
**必须有 ``rationale`` 字段说清"为什么单看不算异常"**。
写不出这句话的规则，说明它只是阈值告警，不该放在这里。

⚠ 本模块**只观测、只报告，不自动处置。**
熔断、暂停、吊销属于 M14 的运维动作，需要人做决定。
让监控自己动手，就违背了"人不被绕过"这条底线。
"""

from __future__ import annotations

import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

#: 延迟样本的窗口大小。
#:
#: 为什么要窗口而不是全量：p99 只需要分布形状，1024 个样本已经足够稳定
#: （p99 对应约第 10 个最慢的样本）。全量累积会让内存随运行时长无限增长，
#: 而一个会自己泄漏的监控模块，比没有监控更糟。
DEFAULT_SAMPLE_WINDOW = 1024

#: 异常严重度。刻意只有三档 —— 分得太细会让"该不该叫醒人"变成争论。
SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeMetrics:
    """单个节点视角的指标。"""

    node_id: str
    domain: str
    approved: bool
    state: str
    #: 该节点作为执行方收到的任务数（累计）
    received: int
    completed: int
    failed: int
    #: 已领取、正在执行的任务数（从队列实测，不是计数器的猜测）
    in_flight: int
    #: 已投递、尚未被领取的任务数 —— 这就是"背压水位"
    pending: int
    #: 并发上限。有了它，pending 才能判断"积压是否已经超了承受力"
    capacity: int
    #: 累计应答字节（来自完成的产物）
    bytes_out: int
    #: 该节点发起的拒绝次数（按规则）
    denials: Mapping[str, int]

    @property
    def backlog_ratio(self) -> float:
        """积压水位 = 待办数 / 并发上限。

        ``> 1`` 意味着积压已经超过它一次能处理的数量。
        用比率而不是绝对数：并发上限 8 的节点积压 4 个不算事，
        上限 1 的节点积压 4 个已经明显堵了。
        """
        if self.capacity <= 0:
            return float("inf") if self.pending else 0.0
        return self.pending / self.capacity

    @property
    def failure_rate(self) -> float:
        total = self.completed + self.failed
        return self.failed / total if total else 0.0


@dataclass(frozen=True)
class Snapshot:
    """某一时刻的完整指标快照。**不可变**，可以安全地传来传去。"""

    at: str
    #: 进程已运行秒数
    uptime_seconds: float
    #: 全局计数
    submitted: int
    completed: int
    failed: int
    denied: int
    #: 按任务类型分组的完成/失败数
    by_type: Mapping[str, Mapping[str, int]]
    #: 按拒绝规则分组
    denied_by_rule: Mapping[str, int]
    #: 延迟分位数（毫秒）。``None`` 表示样本不足
    latency: Mapping[str, float | None]
    #: 延迟样本数
    latency_samples: int
    #: 节点视角
    nodes: Sequence[NodeMetrics]
    #: 网关层计数（若网关未启动则为空）
    gateway: Mapping[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """转成可直接 JSON 序列化的结构。"""
        return {
            "at": self.at,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "totals": {
                "submitted": self.submitted,
                "completed": self.completed,
                "failed": self.failed,
                "denied": self.denied,
            },
            "by_type": {k: dict(v) for k, v in self.by_type.items()},
            "denied_by_rule": dict(self.denied_by_rule),
            "latency_ms": dict(self.latency),
            "latency_samples": self.latency_samples,
            "gateway": dict(self.gateway),
            "nodes": [
                {
                    "node_id": n.node_id,
                    "domain": n.domain,
                    "approved": n.approved,
                    "state": n.state,
                    "received": n.received,
                    "completed": n.completed,
                    "failed": n.failed,
                    "in_flight": n.in_flight,
                    "pending": n.pending,
                    "capacity": n.capacity,
                    "backlog_ratio": round(n.backlog_ratio, 2)
                    if n.backlog_ratio != float("inf") else None,
                    "bytes_out": n.bytes_out,
                    "failure_rate": round(n.failure_rate, 3),
                    "denials": dict(n.denials),
                }
                for n in self.nodes
            ],
        }


# ---------------------------------------------------------------------------
# 计数器
# ---------------------------------------------------------------------------

class Metrics:
    """运行时指标计数器。**线程安全**，开销极低。

    每个打点方法只做常数级工作（一次计数、一次 deque 追加）。
    它位于每个任务的关键路径上，所以不能有分配、不能有 I/O、不能有锁竞争
    —— 这里是 ``threading.Lock`` 而不是 ``RLock``，并且临界区里
    **只做内存操作**。

    ⚠ **不记录任何业务内容。** 只有数量、类型名、规则码、节点 ID、字节数。
    类型名与节点 ID 本来就是配置里的公开标识，不属于业务内容。
    """

    def __init__(self, *, sample_window: int = DEFAULT_SAMPLE_WINDOW) -> None:
        if sample_window <= 0:
            raise ValueError("sample_window 必须为正")
        self._lock = threading.Lock()
        self._started_at = time.monotonic()

        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._denied = 0

        self._by_type: dict[str, Counter[str]] = {}
        self._denied_by_rule: Counter[str] = Counter()

        #: 端到端延迟样本（毫秒）。deque 有 maxlen，自动丢弃最旧的。
        self._latency_ms: deque[float] = deque(maxlen=sample_window)
        #: 在途任务的开始时刻：task_id → monotonic 秒
        self._inflight_since: dict[str, float] = {}

        self._node_received: Counter[str] = Counter()
        self._node_completed: Counter[str] = Counter()
        self._node_failed: Counter[str] = Counter()
        self._node_bytes: Counter[str] = Counter()
        self._node_denials: dict[str, Counter[str]] = {}

    # -- 打点 --

    def on_submit(self, *, task_id: str, task_type: str,
                  to_node: str) -> None:
        """任务被投递。**这是延迟的计时起点。**"""
        now = time.monotonic()
        with self._lock:
            self._submitted += 1
            self._node_received[to_node] += 1
            self._inflight_since[task_id] = now
            self._bucket(task_type)[self._bucket_key("submitted")] += 1

    def on_complete(self, *, task_id: str, task_type: str,
                    from_node: str, byte_size: int = 0) -> None:
        """任务完成。这里结算延迟与字节数。"""
        now = time.monotonic()
        with self._lock:
            self._completed += 1
            self._node_completed[from_node] += 1
            if byte_size:
                self._node_bytes[from_node] += byte_size
            self._bucket(task_type)["completed"] += 1
            self._settle_latency(task_id, now)

    def on_fail(self, *, task_id: str, task_type: str,
                from_node: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._failed += 1
            self._node_failed[from_node] += 1
            self._bucket(task_type)["failed"] += 1
            self._settle_latency(task_id, now)

    def on_deny(self, *, rule_id: str, from_node: str = "") -> None:
        with self._lock:
            self._denied += 1
            self._denied_by_rule[rule_id] += 1
            if from_node:
                self._node_denials.setdefault(from_node, Counter())[rule_id] += 1

    # -- 快照 --

    def snapshot(self, *, uptime_hint: float | None = None) -> dict[str, Any]:
        """取一份计数快照（不含节点/队列状态，那由 :func:`collect` 合并）。"""
        with self._lock:
            return {
                "uptime_seconds": (uptime_hint if uptime_hint is not None
                                   else time.monotonic() - self._started_at),
                "submitted": self._submitted,
                "completed": self._completed,
                "failed": self._failed,
                "denied": self._denied,
                "by_type": {k: dict(v) for k, v in self._by_type.items()},
                "denied_by_rule": dict(self._denied_by_rule),
                "latency": {
                    "p50": _percentile(self._latency_ms, 50),
                    "p95": _percentile(self._latency_ms, 95),
                    "p99": _percentile(self._latency_ms, 99),
                },
                "latency_samples": len(self._latency_ms),
                "node_received": dict(self._node_received),
                "node_completed": dict(self._node_completed),
                "node_failed": dict(self._node_failed),
                "node_bytes": dict(self._node_bytes),
                "node_denials": {k: dict(v) for k, v in self._node_denials.items()},
            }

    # -- 内部 --

    @staticmethod
    def _bucket_key(name: str) -> str:
        return name

    def _bucket(self, task_type: str) -> Counter[str]:
        return self._by_type.setdefault(task_type, Counter())

    def _settle_latency(self, task_id: str, now: float) -> None:
        """结算延迟。**签名要求调用方持锁。**

        找不到起始时刻时有三种可能：进程刚重启、任务不是本进程投的
        （比如切了主备）、或者任务早被清理。三种情况都**静默跳过**——
        宁可少一个样本，也不要编造一个假的延迟。
        """
        started = self._inflight_since.pop(task_id, None)
        if started is None:
            return
        self._latency_ms.append(max(0.0, (now - started) * 1000.0))


def _percentile(samples: Sequence[float], q: float) -> float | None:
    """最近秩法分位数（nearest-rank）。

    刻意用最近秩而不是插值：**返回值一定是真实出现过的样本**。
    对延迟指标来说，这比"插出来的 87.3ms"更好解释——
    运维问"p95 是多少"时，答"最慢的第 5% 里最快的那个是 92ms"
    比答一个计算出来的小数更有说服力。

    样本不足 2 个时返回 ``None``：一个样本的 p50/p95/p99 全都相同，
    那不是分位数，是同一个数字被抄了三遍。
    """
    if len(samples) < 2:
        return None
    ordered = sorted(samples)
    # nearest-rank：第 ceil(q/100 * n) 个（1-based）
    import math

    rank = math.ceil(q / 100.0 * len(ordered))
    rank = min(max(rank, 1), len(ordered))
    return round(ordered[rank - 1], 1)


# ---------------------------------------------------------------------------
# 异常：系统级关联
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Anomaly:
    """一条异常发现。

    ``rationale`` 不是文档装饰，它是**这条规则存在的资格证明**：
    必须能说清"为什么单独看每个指标都不算异常"。
    写不出这句话的规则，说明它只是阈值告警，不该放在这个模块里。
    """

    rule: str
    severity: str
    subject: str
    message: str
    rationale: str
    suggestion: str
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Thresholds:
    """异常判定阈值。

    全部有默认值，但**都允许调**——因为"多少算多"取决于部署规模。
    默认值是按"两三个人、十来台机器"的小规模定的：
    那个规模下，任何持续的组合异常都值得看一眼，不必精确。
    """

    #: 字节量高于全局中位数的多少倍算"异常大"
    bytes_outlier_factor: float = 5.0
    #: 任务量高于全局中位数的多少倍算"异常多"
    volume_outlier_factor: float = 5.0
    #: 至少要有几个节点才谈得上"中位数"（少于这个数不判离群）
    min_nodes_for_outlier: int = 3
    #: 单节点撞到几种不同拒绝规则算"系统性试探"
    distinct_denials_for_probe: int = 3
    #: 积压水位（待办 / 并发上限）超过多少算堵
    backlog_warn_ratio: float = 3.0
    #: 判定"零产出"时，至少要有这么多任务才算数（避免刚启动就误报）
    min_tasks_for_stall: int = 3


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def detect_anomalies(snap: Snapshot, *,
                     thresholds: Thresholds | None = None,
                     previous: Snapshot | None = None) -> list[Anomaly]:
    """在快照上跑全部系统级关联规则。**纯函数，不改任何状态。**

    :param previous: 上一次快照。给了才能判趋势（见 ``silent_stall``）。
        没给时依赖趋势的规则会被跳过——**跳过而不是猜**。

    返回按严重度排序的发现（critical 在前）。
    """
    t = thresholds or Thresholds()
    found: list[Anomaly] = []
    found.extend(_check_bulk_movement(snap, t))
    found.extend(_check_probe_pattern(snap, t))
    found.extend(_check_dead_route(snap, t))
    found.extend(_check_stall(snap, t, previous))
    found.extend(_check_denial_surge(snap, t))

    order = {SEVERITY_CRITICAL: 0, SEVERITY_WARN: 1, SEVERITY_INFO: 2}
    found.sort(key=lambda a: (order.get(a.severity, 9), a.rule))
    return found


def _check_bulk_movement(snap: Snapshot, t: Thresholds) -> list[Anomaly]:
    """批量搬运：**响应体积大 + 任务量多**，同时出现才可疑。

    为什么这条必须是组合判断：

    * 单看"响应字节多" —— 完全正常。做汇总、做大文件报告的节点本来就产得多
    * 单看"收到的任务多" —— 也正常。今天活多的节点就该多

    但两者**同时**远超同类水平，形态就很像"有人在用它批量导出数据"。
    这时才值得看一眼——不是断定为攻击，而是值得人看一眼。
    """
    nodes = [n for n in snap.nodes if n.completed > 0]
    if len(nodes) < t.min_nodes_for_outlier:
        return []

    bytes_median = _median([n.bytes_out for n in nodes])
    volume_median = _median([n.completed for n in nodes])
    # 中位数为 0 时倍数没有意义（除以零，或放大到荒谬）
    if bytes_median <= 0 or volume_median <= 0:
        return []

    out: list[Anomaly] = []
    for n in nodes:
        bytes_ratio = n.bytes_out / bytes_median
        volume_ratio = n.completed / volume_median
        if (bytes_ratio >= t.bytes_outlier_factor
                and volume_ratio >= t.volume_outlier_factor):
            out.append(Anomaly(
                rule="bulk_movement",
                severity=SEVERITY_WARN,
                subject=n.node_id,
                message=(
                    f"{n.node_id} 的应答字节是全体的 {bytes_ratio:.1f} 倍，"
                    f"完成量是全体的 {volume_ratio:.1f} 倍"
                ),
                rationale=(
                    "字节多本身正常（汇总类节点就该产得多），任务多也正常"
                    "（今天活多）。但两者同时远超同类，形态接近批量导出——"
                    "单看任一指标都不会触发。"
                ),
                suggestion=(
                    "确认这个节点是否本就承担汇总/导出职责。"
                    "若不是，看它的角色卡等级上限是否该收紧。"
                ),
                evidence={
                    "bytes_out": n.bytes_out,
                    "bytes_median": int(bytes_median),
                    "completed": n.completed,
                    "volume_median": int(volume_median),
                },
            ))
    return out


def _check_probe_pattern(snap: Snapshot, t: Thresholds) -> list[Anomaly]:
    """权限试探：**同一个节点撞到多种不同的拒绝规则**。

    为什么这条必须是组合判断：

    * 单看"有拒绝" —— 完全正常。本系统默认拒绝，没配好的调用必然被拒
    * 单看"某条规则命中很多" —— 也正常。通常意味着一处配置写错了，
      修一下就好，是良性问题

    真正可疑的是**一个节点撞到三种以上互不相关的规则**：
    那不是"某条规则配错"，而是"在摸清边界在哪"。
    配错的人只会反复撞同一堵墙，试探的人会挨个墙敲。
    """
    out: list[Anomaly] = []
    for n in snap.nodes:
        distinct = len(n.denials)
        total = sum(n.denials.values())
        if distinct >= t.distinct_denials_for_probe:
            out.append(Anomaly(
                rule="probe_pattern",
                severity=SEVERITY_CRITICAL,
                subject=n.node_id,
                message=(
                    f"{n.node_id} 触发了 {distinct} 种不同的拒绝规则"
                    f"（共 {total} 次）：{', '.join(sorted(n.denials))}"
                ),
                rationale=(
                    "有拒绝很正常（默认拒绝必然拒）。反复撞同一条规则通常只是"
                    "配置写错，是良性的。但一个节点撞到多种互不相关的规则，"
                    "更像在逐条试探边界——配错不会这么分散。"
                ),
                suggestion=(
                    f"先确认是不是该节点角色卡写错了。若不像，考虑 "
                    f"nxs ops revoke {n.node_id} 并查它的审计轨迹。"
                ),
                evidence={"denials": dict(n.denials), "total": total},
            ))
    return out


def _check_dead_route(snap: Snapshot, t: Thresholds) -> list[Anomaly]:
    """给"死人"派活：**节点不可路由，但名下仍有任务**。

    为什么这条必须是组合判断：

    * 单看"某节点 stale" —— 正常，可能只是关了电脑下班了
    * 单看"队列里有待办任务" —— 正常，队列本来就该有待办

    组合起来才是问题：**给一个已知离线的节点派活，那批任务会一直等到
    租约超时才回队列**。单看两边都合理，合起来是在白等。

    这类问题的隐蔽之处：它不会报错，只是慢。慢到用户以为系统坏了。
    """
    out: list[Anomaly] = []
    for n in snap.nodes:
        if n.state == "alive":
            continue
        if n.pending <= 0 and n.in_flight <= 0:
            continue
        waiting = n.pending + n.in_flight
        severity = (SEVERITY_CRITICAL if n.state == "revoked"
                    else SEVERITY_WARN)
        out.append(Anomaly(
            rule="dead_route",
            severity=severity,
            subject=n.node_id,
            message=(
                f"{n.node_id} 状态为 {n.state}，但名下仍有 {waiting} 个任务"
                f"（待领取 {n.pending}，执行中 {n.in_flight}）"
            ),
            rationale=(
                "节点离线很正常（下班关机），队列有待办也很正常。"
                "但给已知离线的节点派活，那批任务只能等租约超时才回队列——"
                "不会报错，只会慢，慢到让人以为系统坏了。"
            ),
            suggestion=(
                f"任务会等租约自动回队列，不必人工干预；但若该节点已废弃，"
                f"应 nxs ops revoke {n.node_id} 并清掉名下任务"
                if n.state != "revoked" else
                f"{n.node_id} 已吊销却仍有任务在途，检查是否有残留投递"
            ),
            evidence={"state": n.state, "pending": n.pending,
                      "in_flight": n.in_flight},
        ))
    return out


def _check_stall(snap: Snapshot, t: Thresholds,
                 previous: Snapshot | None) -> list[Anomaly]:
    """静默卡死：**积压很高，但近期没有任何产出**。

    为什么这条必须是组合判断：

    * 单看"积压高" —— 正常，可能一下子来了一批任务
    * 单看"这一会儿没完成任何任务" —— 正常，任务正在算

    组合起来才说明卡住了：**积压超过并发上限数倍，而近期零产出**。
    有积压说明有活；零产出说明一个都没做完。不是慢，是不动。

    ⭐ 判据分两档，因为"零产出"有两种含义
    ----------------------------------------
    v0.8 初版用的是**累计**零产出（``completed + failed == 0``）——
    那会漏掉最常见的情形：**节点先正常跑了一批，然后才卡死**。
    那时累计产出早已不为 0，规则就永远不响了。

    所以有 ``previous`` 时改用**增量**判断：

    ==================  ==========================================
    有 previous         看两次快照之间有没有产出（准确）
    无 previous         退回累计判断（保守，会漏但不误报）
    ==================  ==========================================

    前者才是生产环境的常态（``nxs metrics`` 反复调用）。
    \"退回累计\"不是偷懒：第一次调用时确实没有历史信息，
    凭空猜一个"可能最近没产出"比不报更糟。
    """
    out: list[Anomaly] = []
    prev_by_node = {n.node_id: n for n in previous.nodes} if previous else {}

    for n in snap.nodes:
        if n.state != "alive" or not n.approved:
            continue
        if n.received < t.min_tasks_for_stall:
            continue
        backlog = n.backlog_ratio
        if backlog < t.backlog_warn_ratio:
            continue

        produced_total = n.completed + n.failed
        prev = prev_by_node.get(n.node_id) if previous is not None else None

        if prev is not None:
            # ★ 增量判断：这两次快照之间有没有干活
            produced_delta = produced_total - (prev.completed + prev.failed)
            if produced_delta > 0:
                # 有产出 —— 那只是慢，不是卡死
                continue
            if n.pending <= prev.pending:
                # 积压没增长 —— 可能只是刚处理完一批，静止的高积压不代表卡住
                continue
            detail_note = f"两次采样间产出 0（累计 {produced_total}）"
        else:
            # 无历史：只有"从没产出过"才敢报，否则会误伤正在跑长任务的节点
            if produced_total > 0:
                continue
            detail_note = "自启动以来零产出"

        out.append(Anomaly(
            rule="silent_stall",
            severity=SEVERITY_CRITICAL,
            subject=n.node_id,
            message=(
                f"{n.node_id} 积压 {n.pending} 个（并发上限 {n.capacity}，"
                f"水位 {backlog:.1f}×），{detail_note}"
            ),
            rationale=(
                "积压多很正常（可能刚来一批活），暂时没完成也正常"
                "（正算着）。但积压超过上限数倍且**近期零产出**，"
                "说明不是慢，是不动——有积压证明有活进来，"
                "零产出证明一个都没出去。"
            ),
            suggestion=(
                "先看该节点日志是否卡在某处。若进程已死，"
                "任务会在租约超时后自动回队列；确认后可用 "
                "nxs ops release 提前释放。"
            ),
            evidence={"pending": n.pending, "capacity": n.capacity,
                      "backlog_ratio": round(backlog, 2),
                      "received": n.received, "produced_total": produced_total,
                      "produced_delta": (produced_total
                                         - (prev.completed + prev.failed)
                                         if prev is not None else None)},
        ))
    return out


def _check_denial_surge(snap: Snapshot, t: Thresholds) -> list[Anomaly]:
    """拒绝结构异常：**整体拒绝率很高，但集中在少数规则上**。

    为什么这条必须是组合判断：

    * 单看"拒绝率高" —— 在默认拒绝的系统里很正常
    * 单看"某规则命中多" —— 正常

    组合起来指向一个具体的事：**某一条规则几乎吃掉了所有拒绝**。
    这几乎总是配置问题（比如矩阵漏了一条本该有的规则），
    而不是攻击——攻击通常分散在多条规则上（见 ``probe_pattern``）。

    两条规则配合起来才有分辨力：集中的拒绝 = 配错，分散的拒绝 = 试探。
    """
    if snap.denied < 10:      # 样本太少，比例没有意义
        return []
    total_ops = snap.completed + snap.failed + snap.denied
    if total_ops <= 0:
        return []
    deny_rate = snap.denied / total_ops
    if deny_rate < 0.5:
        return []

    ranked = sorted(snap.denied_by_rule.items(), key=lambda kv: -kv[1])
    if not ranked:
        return []
    top_rule, top_count = ranked[0]
    share = top_count / snap.denied
    if share < 0.8:
        return []

    return [Anomaly(
        rule="denial_dominance",
        severity=SEVERITY_WARN,
        subject="*",
        message=(
            f"拒绝率 {deny_rate:.0%}，其中 {share:.0%} 集中在 {top_rule}"
            f"（{top_count}/{snap.denied}）"
        ),
        rationale=(
            "默认拒绝的系统里拒绝率高很正常。但只要看分布就能分辨："
            "**一条规则吃掉绝大部分拒绝**通常是矩阵或角色卡漏配，是良性的；"
            "分散在多条规则上才像试探（后者由 probe_pattern 判）。"
        ),
        suggestion=(
            f"检查 {top_rule} 对应的配置：矩阵 allow_types 是否漏了类型、"
            f"角色卡 can_receive_from 是否写窄了。"
        ),
        evidence={"deny_rate": round(deny_rate, 3), "top_rule": top_rule,
                  "top_count": top_count, "denied": snap.denied},
    )]


# ---------------------------------------------------------------------------
# 组装快照
# ---------------------------------------------------------------------------

def collect(metrics: Metrics, *, registry: Any, queue: Any,
            gateway_stats: Any = None,
            uptime_hint: float | None = None) -> Snapshot:
    """把计数器与**真实状态**合并成一份快照。

    为什么不全用计数器：计数器只知道"我打点过什么"。
    ``pending`` / ``in_flight`` 这类状态从队列实测更可靠——
    它们会被租约超时、重试、人工释放改变，而计数器追不上这些变化。

    混合来源的原则：**计数用计数器，状态用真源。**
    计数器负责"发生过多少次"，队列负责"现在是什么样"。

    :param registry: :class:`nxs.control.registry.Registry`
    :param queue: :class:`nxs.ports.stores.QueueStore`
    :param gateway_stats: :class:`nxs.edge.gateway.GatewayStats`，可选。
    """
    from ..core.envelope import now_iso

    base = metrics.snapshot(uptime_hint=uptime_hint)
    received = base["node_received"]
    completed = base["node_completed"]
    failed = base["node_failed"]
    bytes_out = base["node_bytes"]
    denials_by_node = base["node_denials"]

    nodes: list[NodeMetrics] = []
    for rec in registry.all_records():
        card = rec.card
        node_id = card.node_id
        nodes.append(NodeMetrics(
            node_id=node_id,
            domain=card.domain,
            approved=rec.approved,
            state=rec.state,
            received=received.get(node_id, 0),
            completed=completed.get(node_id, 0),
            failed=failed.get(node_id, 0),
            # ★ 从队列实测，不是计数器的猜测
            in_flight=queue.count_tasks(node_id, status="working"),
            pending=queue.count_tasks(node_id, status="submitted"),
            # 用 effective_capacity：`capacity` 未声明时退回旧的
            # quota.concurrent_tasks，所以旧卡片行为完全不变。
            # 声明了就用它——语义从"配额"降级为"排程提示"。
            capacity=card.effective_capacity(),
            bytes_out=bytes_out.get(node_id, 0),
            denials=denials_by_node.get(node_id, {}),
        ))

    gateway: dict[str, int] = {}
    if gateway_stats is not None:
        gateway = {
            "requests": gateway_stats.requests,
            "rejected": gateway_stats.rejected,
            "sse_streams_opened": gateway_stats.sse_streams_opened,
        }

    return Snapshot(
        at=now_iso(),
        uptime_seconds=base["uptime_seconds"],
        submitted=base["submitted"],
        completed=base["completed"],
        failed=base["failed"],
        denied=base["denied"],
        by_type=base["by_type"],
        denied_by_rule=base["denied_by_rule"],
        latency=base["latency"],
        latency_samples=base["latency_samples"],
        nodes=tuple(sorted(nodes, key=lambda n: n.node_id)),
        gateway=gateway,
    )


__all__ = [
    "Anomaly",
    "DEFAULT_SAMPLE_WINDOW",
    "Metrics",
    "NodeMetrics",
    "SEVERITY_CRITICAL",
    "SEVERITY_INFO",
    "SEVERITY_WARN",
    "Snapshot",
    "Thresholds",
    "collect",
    "detect_anomalies",
]
