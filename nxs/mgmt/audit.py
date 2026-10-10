"""审计留痕（M2）—— 记录"发生了什么"，不记录"内容是什么"。

两条硬约束
----------

**① 只记元数据。**
    审计记录里不出现任务的参数、产物、自由文本。理由很直接：
    如果审计包含业务内容，它自己就变成了数据集中点，而集中点
    正是本系统要避免的东西。攻击者攻破审计日志的收益不应该等于
    攻破所有节点。

**② 写入失败必须熔断。**
    ``append`` 失败时直接抛异常，不吞掉。"不可追溯的协作"比
    "协作中断"更糟——中断可以恢复，而不可追溯意味着永远无法回答
    "当时到底发生了什么"。调用方应据此中止本次操作。

记录形态
--------
NDJSON，每行一条 JSON。这个格式的好处是追加写入天然安全（不需要
维护整体结构）、可以用 ``tail`` 直接查看、坏行不影响其他行。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Sequence

from ..ports.stores import AuditStore


class AuditError(RuntimeError):
    """审计写入失败。**调用方应据此熔断**，不要继续处理。"""


class FileAuditStore(AuditStore):
    """NDJSON 审计后端。

    :param path: 审计文件路径。父目录不存在时自动创建。
    :param fsync: 是否每条都 fsync。默认 False（性能）；
        但**崩溃后可能丢最后几条**。对审计要求严格的场景应置 True。
    """

    def __init__(self, path: str | os.PathLike[str], *, fsync: bool = False) -> None:
        self._path = Path(path)
        self._fsync = fsync
        self._lock = threading.RLock()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AuditError(f"审计目录不可创建: {self._path.parent}: {exc}") from exc

    # ------------------------------------------------------------------
    # AuditStore
    # ------------------------------------------------------------------

    def append(self, record: dict[str, Any]) -> None:
        """追加一条记录。**失败即抛，不降级、不静默。**"""
        line = json.dumps(_whitelist(record), ensure_ascii=False, sort_keys=True)
        with self._lock:
            try:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    if self._fsync:
                        fh.flush()
                        os.fsync(fh.fileno())
            except OSError as exc:
                # 磁盘满、权限变更、句柄耗尽——都必须让调用方知道
                raise AuditError(f"审计写入失败: {self._path}: {exc}") from exc

    def read_recent(self, limit: int = 100) -> Sequence[dict[str, Any]]:
        if not self._path.exists():
            return ()
        with self._lock:
            try:
                lines = self._path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise AuditError(f"审计读取失败: {exc}") from exc

        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # 单行损坏不影响其他行——这是 NDJSON 的容错优势
                continue
        return tuple(out)

    def summary(self) -> dict[str, Any]:
        records = self.read_recent(limit=10_000)
        denied_by_rule: dict[str, int] = {}
        completed = denied = 0
        for r in records:
            outcome = r.get("outcome")
            if outcome == "denied":
                denied += 1
                rid = str(r.get("rule_id", "unknown"))
                denied_by_rule[rid] = denied_by_rule.get(rid, 0) + 1
            elif outcome == "completed":
                completed += 1
        return {
            "total": len(records),
            "completed": completed,
            "denied": denied,
            "denied_by_rule": dict(sorted(denied_by_rule.items())),
        }

    @property
    def path(self) -> Path:
        return self._path


#: 允许写入审计的字段。
#: **白名单而非黑名单**——新增字段必须显式加进来，
#: 这样"不小心把 params 传进审计"这类错误会被结构性挡住。
_ALLOWED_FIELDS = frozenset({
    "ts", "session_id", "trace_id", "task_id", "task_type",
    "from_node", "to_node", "from_domain", "to_domain",
    "outcome", "rule_id", "level", "bytes", "attempt", "lease_seconds",
    "duration_ms", "sanitized", "contains_freetext",
})


def _whitelist(record: dict[str, Any]) -> dict[str, Any]:
    """只保留白名单字段。未被允许的字段会被**静默剔除**。

    静默是有意的：审计写入是热路径，不应因为多传了一个字段而失败。
    而"多传的字段没被记录"这件事本身是安全的默认行为。
    """
    return {k: v for k, v in record.items() if k in _ALLOWED_FIELDS}


def make_record(*, outcome: str, task_type: str = "", task_id: str = "",
                session_id: str = "", trace_id: str = "",
                from_node: str = "", to_node: str = "",
                from_domain: str = "", to_domain: str = "",
                rule_id: str = "", level: str = "", byte_size: int = 0,
                attempt: int = 0, duration_ms: int = 0, **extra: Any) -> dict[str, Any]:
    """构造一条审计记录。

    ``**extra`` 里不在白名单的键会被剔除——调用方可以自由传，
    但只有白名单内的字段会真正落盘。
    """
    from ..core.envelope import now_iso

    record: dict[str, Any] = {
        "ts": now_iso(),
        "outcome": outcome,
        "task_type": task_type,
        "task_id": task_id,
        "session_id": session_id,
        "trace_id": trace_id,
        "from_node": from_node,
        "to_node": to_node,
        "from_domain": from_domain,
        "to_domain": to_domain,
        "rule_id": rule_id,
        "level": level,
        "bytes": byte_size,
        "attempt": attempt,
        "duration_ms": duration_ms,
    }
    record.update(extra)
    return record


__all__ = ["AuditError", "FileAuditStore", "make_record"]
