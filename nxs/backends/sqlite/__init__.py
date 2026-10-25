"""SQLite 后端 —— 持久化实现。

与内存后端满足同一套契约，区别只在数据落盘、进程重启后仍在。
这对"断电恢复"是必要的：内存后端在进程重启后就丢了队列，
而真实部署里协调节点重启后任务必须还在。

实现要点
--------
* **原子领取靠 ``BEGIN IMMEDIATE``**。SQLite 默认的延迟事务在多进程
  竞争下可能两个连接同时读到同一行，因此领取操作用立即写锁。
* **单文件、零运维**。整个存储就是一个 ``.db`` 文件，备份即拷贝。
* **不引入 ORM**。SQL 直接写，量级很小，少一层抽象少一处坑。
* ``check_same_thread=False`` + 自持锁：SQLite 连接默认不能在多线程
  共享，这里显式串行化访问，让语义可预期。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from ...core.envelope import parse_iso, to_utc_iso
from ...core.models import Artifact, RoleCard, Task
from ...ports.stores import NodeRecord, QueueStore, RegistryStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id   TEXT PRIMARY KEY,
    card_json   TEXT NOT NULL,
    state       TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    revision    INTEGER NOT NULL DEFAULT 1,
    approved    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    type            TEXT NOT NULL,
    session_id      TEXT NOT NULL DEFAULT '',
    from_node     TEXT NOT NULL,
    to_node       TEXT NOT NULL,
    status          TEXT NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'sync',
    params_json     TEXT NOT NULL,
    artifacts_json  TEXT NOT NULL,
    semantics       TEXT NOT NULL DEFAULT 'data',
    ttl             TEXT,
    attempt         INTEGER NOT NULL DEFAULT 0,
    trace_id        TEXT NOT NULL DEFAULT '',
    lease_expires   TEXT,
    created_at      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(to_node, created_at);

CREATE INDEX IF NOT EXISTS idx_tasks_to_node ON tasks(to_node, status);
CREATE INDEX IF NOT EXISTS idx_tasks_session   ON tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_lease     ON tasks(lease_expires);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def _utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _lease_deadline(seconds: int, *, now: str | None = None) -> str:
    """租约到期时间。**统一存 UTC**，否则跨时区比较会静默失效。

    SQLite 里租约是 TEXT 列，比较用 ``WHERE lease_expires<=?`` —— 那也是
    字典序比较，与时区表示不一致时同样会判错。所以存与查都必须归一化
    （见 :func:`nxs.core.envelope.to_utc_iso`）。

    :param now: 基准时刻。省略时取真实时钟；**调用方给了就必须用**，
        否则注入时间（测试、时钟校正）失效。
    """
    base = parse_iso(to_utc_iso(now)) if now else datetime.now(timezone.utc)
    return (base + timedelta(seconds=seconds)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 序列化（保持与核心 dict 形态一致，便于人工排查数据库内容）
# ---------------------------------------------------------------------------

def _card_to_json(card: RoleCard) -> str:
    return json.dumps({
        "node_id": card.node_id,
        "domain": card.domain,
        "display_name": card.display_name,
        "card_version": card.card_version,
        "reachability": card.reachability,
        "heartbeat_interval": card.heartbeat_interval,
        "roles": {
            "issuer": card.roles.issuer,
            "executor": card.roles.executor,
            "relay": card.roles.relay,
            "observer": card.roles.observer,
            "human_interface": card.roles.human_interface,
        },
        "capabilities": [
            {
                "type": c.type,
                "level": str(c.level),
                "mode": c.mode,
                "description": c.description,
                "params_schema": dict(c.params_schema),
                "response_fields": list(c.response_fields),
                "freetext_fields": list(c.freetext_fields),
            }
            for c in card.capabilities
        ],
        "constraints": {
            "max_response_level": str(card.constraints.max_response_level),
            "can_issue_types": list(card.constraints.can_issue_types),
            "can_receive_from": list(card.constraints.can_receive_from),
            "can_delegate_to": list(card.constraints.can_delegate_to),
            "quota": {
                "tasks_per_day": card.constraints.quota.tasks_per_day,
                "bytes_per_response": card.constraints.quota.bytes_per_response,
                "bytes_per_day": card.constraints.quota.bytes_per_day,
                "concurrent_tasks": card.constraints.quota.concurrent_tasks,
            },
        },
    }, ensure_ascii=False)


def _task_payload(task: Task) -> tuple[str, str]:
    params = json.dumps(dict(task.params), ensure_ascii=False)
    arts = json.dumps([
        {"level": str(a.level), "kind": a.kind, "data": a.data,
         "provenance": dict(a.provenance)}
        for a in task.artifacts
    ], ensure_ascii=False)
    return params, arts


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        type=row["type"],
        session_id=row["session_id"],
        from_node=row["from_node"],
        to_node=row["to_node"],
        status=row["status"],
        mode=row["mode"],
        params=json.loads(row["params_json"]),
        artifacts=[Artifact.from_dict(a) for a in json.loads(row["artifacts_json"])],
        semantics=row["semantics"],
        ttl=row["ttl"],
        attempt=row["attempt"],
        trace_id=row["trace_id"],
    )


def _row_to_record(row: sqlite3.Row) -> NodeRecord:
    return NodeRecord(
        card=RoleCard.from_dict(json.loads(row["card_json"])),
        state=row["state"],
        last_seen=row["last_seen"],
        revision=row["revision"],
        approved=bool(row["approved"]),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class SqliteRegistryStore(RegistryStore):
    def __init__(self, path: str | Path) -> None:
        self._conn = _connect(Path(path))
        self._lock = threading.RLock()

    def put(self, record: NodeRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO nodes(node_id, card_json, state, last_seen,"
                " revision, approved) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(node_id) DO UPDATE SET"
                " card_json=excluded.card_json, state=excluded.state,"
                " last_seen=excluded.last_seen, revision=excluded.revision,"
                " approved=excluded.approved",
                (record.card.node_id, _card_to_json(record.card), record.state,
                 record.last_seen, record.revision, int(record.approved)),
            )

    def get(self, node_id: str) -> NodeRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nodes WHERE node_id=?", (node_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def list_all(self) -> Sequence[NodeRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM nodes ORDER BY node_id"
            ).fetchall()
        return tuple(_row_to_record(r) for r in rows)

    def touch(self, node_id: str, *, last_seen: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE nodes SET last_seen=? WHERE node_id=?",
                (last_seen, node_id),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

class SqliteQueueStore(QueueStore):
    """持久化队列。

    "领取"用 ``BEGIN IMMEDIATE`` 包住「查询 + 更新」，确保多进程竞争下
    同一任务只会被一个执行方拿到。这是原子性的唯一保障。
    """

    def __init__(self, path: str | Path) -> None:
        self._conn = _connect(Path(path))
        self._lock = threading.RLock()

    # -- 私有：释放过期租约（调用方须持锁）--

    def _release_expired_locked(self, *, now: str,
                                node_id: str | None = None) -> list[sqlite3.Row]:
        # ★ 查询前把 now 归一化成 UTC —— 库里存的是 UTC，
        #   而调用方传的可能是任意时区表示。字符串比较只在
        #   时区表示一致时才等价于时间比较。
        ref = to_utc_iso(now)
        if node_id is None:
            where, args = "lease_expires IS NOT NULL AND lease_expires<=?", (ref,)
        else:
            where = ("to_node=? AND lease_expires IS NOT NULL"
                     " AND lease_expires<=?")
            args = (node_id, ref)

        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE {where}", args
        ).fetchall()
        if rows:
            self._conn.execute(
                f"UPDATE tasks SET lease_expires=NULL WHERE {where}", args
            )
        return list(rows)

    # -- QueueStore --

    def enqueue(self, task: Task) -> None:
        """投递任务。

        **刻意使用普通 INSERT 而非 INSERT OR REPLACE。**

        原因：``OR REPLACE`` 在 ``idempotency_key`` 冲突时会**静默删除
        已存在的行**再插入新行。这意味着"用同一个幂等键但不同 task id
        重复提交"会悄悄丢掉原任务——包括它已经积累的状态与尝试次数。

        那正好破坏了幂等键存在的意义。宁可让冲突抛异常暴露出来：
        调用方（``TaskLifecycle.submit``）本就应先查幂等键，
        走到这里冲突就说明有逻辑错误，应该被看见而不是被吞掉。
        """
        params, arts = _task_payload(task)
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks(id, idempotency_key, type, session_id,"
                " from_node, to_node, status, mode, params_json, artifacts_json,"
                " semantics, ttl, attempt, trace_id, lease_expires, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
                (task.id, task.idempotency_key, task.type, task.session_id,
                 task.from_node, task.to_node, task.status, task.mode,
                 params, arts, task.semantics, task.ttl, task.attempt, task.trace_id,
                 _utc_now()),
            )

    def claim(self, node_id: str, *, lease_seconds: int, now: str) -> Task | None:
        deadline = _lease_deadline(lease_seconds, now=now)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # 顺手释放本节点的过期租约，调用方不必先 sweep
                self._release_expired_locked(now=now, node_id=node_id)
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE to_node=? AND lease_expires IS NULL"
                    " AND status NOT IN ('completed','failed','canceled')"
                    " ORDER BY rowid LIMIT 1",
                    (node_id,),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None

                self._conn.execute(
                    "UPDATE tasks SET status='working', attempt=attempt+1,"
                    " lease_expires=? WHERE id=?",
                    (deadline, row["id"]),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        task = _row_to_task(row)
        task.status = "working"
        task.attempt = row["attempt"] + 1
        return task

    def update(self, task: Task) -> None:
        params, arts = _task_payload(task)
        with self._lock:
            if task.is_terminal:
                # 终态：释放租约，任务不再可领取
                self._conn.execute(
                    "UPDATE tasks SET status=?, params_json=?, artifacts_json=?,"
                    " semantics=?, attempt=?, ttl=?, lease_expires=NULL WHERE id=?",
                    (task.status, params, arts, task.semantics, task.attempt,
                     task.ttl, task.id),
                )
            else:
                # 中间态：保留租约（仍在执行中，不能让别处领走）
                self._conn.execute(
                    "UPDATE tasks SET status=?, params_json=?, artifacts_json=?,"
                    " semantics=?, attempt=?, ttl=? WHERE id=?",
                    (task.status, params, arts, task.semantics, task.attempt,
                     task.ttl, task.id),
                )

    def renew(self, task_id: str, *, lease_seconds: int, now: str) -> bool:
        """延长租约。只对"当前仍被持有且未落终态"的任务生效。

        用 ``UPDATE ... WHERE`` 的返回行数判断，而不是"先查再改"——
        后者在多进程下有竞态：查到持有、改之前被释放，就会错误地"续活"
        一个已经回队列的任务。
        """
        deadline = _lease_deadline(lease_seconds, now=now)
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE tasks SET lease_expires=? WHERE id=? AND lease_expires IS NOT NULL"
                " AND status NOT IN ('completed','failed','canceled')",
                (deadline, task_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        return _row_to_task(row) if row else None

    def release_expired(self, *, now: str) -> list[Task]:
        with self._lock:
            rows = self._release_expired_locked(now=now)
        return [_row_to_task(r) for r in rows]

    def list_by_session(self, session_id: str) -> Sequence[Task]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE session_id=? ORDER BY rowid", (session_id,)
            ).fetchall()
        return tuple(_row_to_task(r) for r in rows)

    def find_by_idempotency(self, key: str) -> Task | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE idempotency_key=?", (key,)
            ).fetchone()
        return _row_to_task(row) if row else None

    def count_tasks(self, node_id: str, *, since: str | None = None,
                    status: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM tasks WHERE to_node=?"
        args: list[str] = [node_id]
        if since is not None:
            sql += " AND created_at >= ?"
            args.append(since)
        if status is not None:
            sql += " AND status=?"
            args.append(status)
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return int(row["n"]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["SqliteQueueStore", "SqliteRegistryStore"]
