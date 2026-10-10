"""内存后端 —— 用于测试与 demo。

**不是"简化版"，而是"另一份完整实现"**：它同样满足 Port 的全部契约
（原子领取、租约、幂等），只是数据不持久化、进程退出即丢。

这样设计的好处：核心逻辑可以用内存后端跑完整测试，不需要起数据库、
不需要清理临时文件、测试之间完全隔离。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from ...core.models import Task, TaskStatus
from ...ports.stores import AuditStore, NodeRecord, QueueStore, RegistryStore


def _lease_deadline(seconds: int) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=seconds)).astimezone().isoformat(timespec="seconds")


class MemoryRegistryStore(RegistryStore):
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, NodeRecord] = {}

    def put(self, record: NodeRecord) -> None:
        with self._lock:
            self._records[record.card.node_id] = record

    def get(self, node_id: str) -> NodeRecord | None:
        with self._lock:
            return self._records.get(node_id)

    def list_all(self) -> Sequence[NodeRecord]:
        with self._lock:
            return tuple(self._records.values())

    def touch(self, node_id: str, *, last_seen: str) -> None:
        with self._lock:
            cur = self._records.get(node_id)
            if cur is None:
                return  # 未知节点的心跳静默忽略，避免心跳成为探测面
            self._records[node_id] = NodeRecord(
                card=cur.card, state=cur.state, last_seen=last_seen,
                revision=cur.revision, approved=cur.approved,
            )


class MemoryQueueStore(QueueStore):
    """内存队列。

    ``claim`` 用锁保证原子性——虽然 CPython 有 GIL，但"读取 + 修改"是
    复合操作，不加锁语义仍可能交错。这里显式加锁，让正确性不依赖 GIL 细节。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
        self._leases: dict[str, str] = {}        # task_id → 租约到期时间
        self._claimed_by: dict[str, str] = {}    # task_id → node_id
        self._by_idem: dict[str, str] = {}       # idempotency_key → task_id
        self._created: dict[str, str] = {}       # task_id → 创建时间

    # ---- 内部 ----

    def _expired_ids(self, *, now: str) -> list[str]:
        return [tid for tid, exp in self._leases.items() if exp and exp <= now]

    def _release_locked(self, task_id: str) -> None:
        """释放租约（调用方须持锁）。不改任务状态——见 release_expired 说明。"""
        self._leases.pop(task_id, None)
        self._claimed_by.pop(task_id, None)

    # ---- QueueStore ----

    def enqueue(self, task: Task) -> None:
        from ...core.envelope import now_iso
        with self._lock:
            self._tasks[task.id] = task
            self._by_idem[task.idempotency_key] = task.id
            self._created.setdefault(task.id, now_iso())

    def claim(self, node_id: str, *, lease_seconds: int, now: str) -> Task | None:
        with self._lock:
            # 惰性释放，避免调用方必须显式先调 release_expired
            for tid in self._expired_ids(now=now):
                self._release_locked(tid)

            for tid, task in self._tasks.items():
                if task.to_node != node_id or tid in self._leases:
                    continue
                if task.status in TaskStatus.TERMINAL:
                    continue
                task.status = TaskStatus.WORKING
                task.attempt += 1
                self._leases[tid] = _lease_deadline(lease_seconds)
                self._claimed_by[tid] = node_id
                return task
            return None

    def update(self, task: Task) -> None:
        with self._lock:
            self._tasks[task.id] = task
            if task.is_terminal:
                self._release_locked(task.id)

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def release_expired(self, *, now: str) -> list[Task]:
        with self._lock:
            released: list[Task] = []
            for tid in self._expired_ids(now=now):
                self._release_locked(tid)
                t = self._tasks.get(tid)
                if t is not None:
                    released.append(t)
            return released

    def list_by_session(self, session_id: str) -> Sequence[Task]:
        with self._lock:
            return tuple(t for t in self._tasks.values() if t.session_id == session_id)

    def find_by_idempotency(self, key: str) -> Task | None:
        with self._lock:
            tid = self._by_idem.get(key)
            return self._tasks.get(tid) if tid else None

    def count_tasks(self, node_id: str, *, since: str | None = None,
                    status: str | None = None) -> int:
        with self._lock:
            n = 0
            for tid, task in self._tasks.items():
                if task.to_node != node_id:
                    continue
                if status is not None and task.status != status:
                    continue
                if since is not None and tid in self._created:
                    if self._created[tid] < since:
                        continue
                n += 1
            return n

    # ---- 仅供测试与诊断，不属于 Port 契约 ----

    def lease_expiry(self, task_id: str) -> str | None:
        with self._lock:
            return self._leases.get(task_id)

    def claimed_by(self, task_id: str) -> str | None:
        with self._lock:
            return self._claimed_by.get(task_id)


class MemoryAuditStore(AuditStore):
    """内存审计。行为与文件版一致，只是不落盘。

    同样**保证写入失败会抛异常**——尽管内存写入几乎不会失败，
    但契约要一致，否则"审计失败必须熔断"这条路径无法被测试覆盖。
    """

    #: 测试钩子：设为非 None 时 ``append`` 抛出该异常。
    fail_with: BaseException | None = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: list[dict[str, Any]] = []

    def append(self, record: dict[str, Any]) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        with self._lock:
            self._records.append(dict(record))

    def read_recent(self, limit: int = 100) -> Sequence[dict[str, Any]]:
        with self._lock:
            return tuple(self._records[-limit:])

    def summary(self) -> dict[str, Any]:
        with self._lock:
            denied_by_rule: dict[str, int] = {}
            completed = denied = 0
            for r in self._records:
                outcome = r.get("outcome")
                if outcome == "denied":
                    denied += 1
                    rid = str(r.get("rule_id", "unknown"))
                    denied_by_rule[rid] = denied_by_rule.get(rid, 0) + 1
                elif outcome == "completed":
                    completed += 1
            return {
                "total": len(self._records),
                "completed": completed,
                "denied": denied,
                "denied_by_rule": dict(sorted(denied_by_rule.items())),
            }


__all__ = ["MemoryAuditStore", "MemoryQueueStore", "MemoryRegistryStore"]
