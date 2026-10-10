"""SQLite 后端测试。

重点验证**内存后端覆盖不到的部分**：持久化、跨进程竞争下的原子性、
以及"进程重启后队列还在"——后者是断电恢复的实际前提。

内存与 SQLite 共用同一套 Port 契约，因此这里也顺带验证两者行为一致。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from conftest import covers

from nxs.backends.sqlite import SqliteQueueStore, SqliteRegistryStore
from nxs.control import PolicyEngine, Registry
from nxs.data import Mailbox, TaskLifecycle
from nxs.ports import NodeState


@pytest.fixture
def db_path(workdir):
    return workdir / "nxs.db"


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

class TestPersistence:
    @covers("M7.F8")
    def test_queue_survives_reopen(self, db_path):
        """★ 进程重启后队列必须还在——这是断电恢复的实际前提。"""
        s1 = SqliteQueueStore(db_path)
        s1.enqueue(_task("t-1", "i-1", "node-a"))
        s1.close()

        s2 = SqliteQueueStore(db_path)          # 模拟进程重启
        got = s2.get("t-1")
        assert got is not None
        assert got.id == "t-1" and got.status == "submitted"
        s2.close()

    @covers("M3.F7")
    def test_registry_survives_reopen(self, db_path, config):
        s1 = SqliteRegistryStore(db_path)
        reg1 = Registry(s1)
        for card in config.nodes.values():
            reg1.register(card, approved=True)
        s1.close()

        s2 = SqliteRegistryStore(db_path)
        reg2 = Registry(s2)
        rec = reg2.record("node-a")
        assert rec is not None
        assert rec.approved
        # 角色卡的所有维度都要完整还原
        card = rec.card
        assert card.domain == "zone-a"
        assert card.roles.executor and not card.roles.issuer
        assert card.constraints.max_response_level.value == 1
        assert [c.type for c in card.capabilities] == ["demo.echo", "demo.count"]
        assert card.capability("demo.echo").response_fields == ("echo", "level")
        s2.close()

    @covers("M3.F7")
    def test_freetext_fields_survive_roundtrip(self, db_path, config):
        """自由文本声明不能因为序列化而丢失——否则边界会漏掉这类字段。"""
        s1 = SqliteRegistryStore(db_path)
        Registry(s1).register(config.nodes["node-b"], approved=True)
        s1.close()

        s2 = SqliteRegistryStore(db_path)
        cap = Registry(s2).require_card("node-b").capability("demo.search")
        assert cap.freetext_fields == ("freetext_summary",)
        s2.close()


# ---------------------------------------------------------------------------
# 原子性
# ---------------------------------------------------------------------------

class TestAtomicClaim:
    @covers("M6.F3")
    def test_concurrent_claim_yields_exactly_one_winner(self, db_path):
        """★ 多线程并发领取同一任务，只能有一个成功。

        这是原子性的核心验证：如果两个执行方拿到同一个任务，
        有副作用的操作会被重复执行。
        """
        setup = SqliteQueueStore(db_path)
        setup.enqueue(_task("t-1", "i-1", "node-a"))
        setup.close()

        winners: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker(n: int) -> None:
            store = SqliteQueueStore(db_path)
            try:
                barrier.wait()          # 让 8 个线程尽量同时发起
                got = store.claim("node-a", lease_seconds=60,
                                  now=_now())
                if got is not None:
                    with lock:
                        winners.append(f"w{n}")
            finally:
                store.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(winners) == 1, f"应当只有一个赢家，实际 {len(winners)} 个"

    @covers("M6.F3")
    def test_second_claim_after_first_is_empty(self, db_path):
        store = SqliteQueueStore(db_path)
        store.enqueue(_task("t-1", "i-1", "node-a"))
        first = store.claim("node-a", lease_seconds=60, now=_now())
        second = store.claim("node-a", lease_seconds=60, now=_now())
        assert first is not None and second is None
        store.close()


# ---------------------------------------------------------------------------
# 租约与幂等
# ---------------------------------------------------------------------------

class TestLeaseAndIdempotency:
    @covers("M7.F4", "M7.F8")
    def test_expired_lease_is_reclaimable(self, db_path):
        store = SqliteQueueStore(db_path)
        store.enqueue(_task("t-1", "i-1", "node-a"))
        store.claim("node-a", lease_seconds=1, now=_now())

        future = _now(offset=5)
        released = store.release_expired(now=future)
        assert [t.id for t in released] == ["t-1"]

        again = store.claim("node-a", lease_seconds=60, now=future)
        assert again is not None and again.id == "t-1"
        assert again.attempt == 2
        store.close()

    @covers("M7.F4")
    def test_claim_lazily_releases_own_expired_lease(self, db_path):
        """claim 内部会释放本节点的过期租约，调用方不必先 sweep。"""
        store = SqliteQueueStore(db_path)
        store.enqueue(_task("t-1", "i-1", "node-a"))
        store.claim("node-a", lease_seconds=1, now=_now())

        # 不显式调 release_expired，直接再领
        got = store.claim("node-a", lease_seconds=60, now=_now(offset=5))
        assert got is not None and got.attempt == 2
        store.close()

    @covers("M7.F4")
    def test_lease_is_not_released_by_other_nodes_claim(self, db_path):
        """A 的租约不该被 B 的领取动作释放——否则 A 的任务会被 B 抢走。"""
        store = SqliteQueueStore(db_path)
        store.enqueue(_task("t-1", "i-1", "node-a"))
        store.enqueue(_task("t-2", "i-2", "node-b"))

        store.claim("node-a", lease_seconds=1, now=_now())     # A 领走 t-1
        store.claim("node-b", lease_seconds=60, now=_now(offset=5))  # B 领 t-2

        # A 的 t-1 仍在租约中（1 秒已过，但 B 的领取不应影响它）
        assert store.get("t-1").status == "working"
        store.close()

    @covers("M7.F1")
    def test_idempotency_key_is_unique(self, db_path):
        store = SqliteQueueStore(db_path)
        store.enqueue(_task("t-1", "same-key", "node-a"))
        with pytest.raises(sqlite3.IntegrityError):
            store.enqueue(_task("t-2", "same-key", "node-a"))
        store.close()

    @covers("M7.F1")
    def test_find_by_idempotency(self, db_path):
        store = SqliteQueueStore(db_path)
        store.enqueue(_task("t-1", "key-1", "node-a"))
        assert store.find_by_idempotency("key-1").id == "t-1"
        assert store.find_by_idempotency("nope") is None
        store.close()


# ---------------------------------------------------------------------------
# 与内存后端行为一致
# ---------------------------------------------------------------------------

class TestContractParity:
    """两个后端必须表现一致——否则"换后端不改代码"这个承诺是假的。"""

    @covers("M7.F7", "M7.F8", "M8.F1")
    def test_full_flow_on_sqlite(self, db_path, config):
        registry = Registry.from_config(config, SqliteRegistryStore(db_path))
        for mid in config.nodes:
            registry.approve(mid)
        queue = SqliteQueueStore(db_path)
        lifecycle = TaskLifecycle(queue, lease_seconds=1)
        mailbox = Mailbox(PolicyEngine(config), registry, lifecycle)

        d = mailbox.deliver(from_node="hub", to_node="node-a",
                            task_type="demo.echo", params={"text": "hi"},
                            session_id="ses-1")
        assert d.created and d.effective_max_level == "metadata"

        task = mailbox.poll("node-a")
        assert task is not None and task.status == "working"

        from nxs.core import Artifact, Level
        lifecycle.complete(task, [Artifact(level=Level.METADATA, kind="structured",
                                           data={"echo": "hi", "level": "metadata"})])

        view = lifecycle.session_view("ses-1")
        assert view["by_status"] == {"completed": 1}
        queue.close()


def _task(task_id: str, idem: str, to_node: str):
    from nxs.core import Task
    return Task(id=task_id, idempotency_key=idem, type="demo.echo",
                session_id="", from_node="hub", to_node=to_node,
                params={"text": "x"})


def _now(offset: int = 0) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=offset)).astimezone().isoformat(timespec="seconds")


class TestNoSilentReplace:
    """回归测试：曾用 INSERT OR REPLACE，会静默吞掉幂等冲突。

    这个 bug 的后果很隐蔽——用同一个幂等键重复提交时，
    原任务被静默替换，其已积累的状态与 attempt 计数全部丢失。
    对"断电后重试"这个核心场景来说，这等于幂等键失效。
    """

    @covers("M7.F1", "M7.R2")
    def test_duplicate_idempotency_key_raises_not_silently_replaces(self, db_path):
        store = SqliteQueueStore(db_path)
        original = _task("t-original", "key-x", "node-a")
        store.enqueue(original)
        store.claim("node-a", lease_seconds=60, now=_now())   # 原任务已被领取
        assert store.get("t-original").attempt == 1

        with pytest.raises(sqlite3.IntegrityError):
            store.enqueue(_task("t-duplicate", "key-x", "node-a"))

        # 原任务必须完好无损——状态与尝试次数都不能被抹掉
        survivor = store.get("t-original")
        assert survivor is not None, "原任务被静默删除了"
        assert survivor.attempt == 1
        assert survivor.status == "working"
        store.close()
