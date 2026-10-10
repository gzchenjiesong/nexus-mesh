"""任务生命周期（D2）—— 状态机、租约、幂等。

本模块处理"任务从提交到终态"的全过程。它是 Core 的一部分，
因此**只依赖 Port，不感知任何存储实现**。

三个关键机制
------------

**幂等（idempotency key）**
    每个任务带一个由发起方生成的幂等键。重复提交同一个键不会产生
    第二个任务，而是返回已有任务。**这是断电重试不产生重复副作用的前提**——
    对"代发消息""提交表单"这类有副作用的操作，重复执行的后果是真实的。

**租约（lease）**
    执行方领取任务时获得一个限时的租约。租约到期仍未完成，任务自动回到
    队列。**这是"断电恢复零人工干预"的实现**：进程被 kill 后无需任何
    人工介入，任务会自己回来。

**状态机**
    ``submitted → working ⇄ input_required → completed / failed / canceled``
    终态不可回退。``input_required`` 是唯一可双向的中转态，它让执行方
    能向发起方追问，而不必失败重来。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from ..core.envelope import now_iso, parse_iso
from ..core.errors import PolicyDenied, RuleId
from ..core.models import Task, TaskMode, TaskStatus
from ..ports.stores import QueueStore

#: 默认租约时长（秒）。
#: 取值权衡：太短会让正常的长任务被误判为中断（导致重复执行）；
#: 太长会让断电后的恢复延迟变大。15 秒适配"秒级到分钟级"的任务谱，
#: 长任务应通过续租延长，而不是靠调大这个值。
DEFAULT_LEASE_SECONDS = 15

#: 默认最大尝试次数。超过即标记 failed，不再重试。
DEFAULT_MAX_ATTEMPTS = 3


class TaskLifecycle:
    """任务生命周期的唯一入口。

    所有状态变更都必须经此模块完成——直接改 ``task.status`` 会绕过
    状态机校验与租约管理。
    """

    def __init__(self, store: QueueStore, *,
                 lease_seconds: int = DEFAULT_LEASE_SECONDS,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        self._store = store
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    @property
    def lease_seconds(self) -> int:
        return self._lease_seconds

    # ------------------------------------------------------------------
    # 提交
    # ------------------------------------------------------------------

    def submit(self, *, task_type: str, from_node: str, to_node: str,
               params: dict, session_id: str = "",
               idempotency_key: str | None = None,
               mode: str = TaskMode.SYNC) -> tuple[Task, bool]:
        """提交一个任务。

        :returns: ``(task, created)``。``created=False`` 表示命中幂等键，
            返回的是**已有的**任务，调用方应据此避免重复提交。

        幂等键省略时自动生成——但这种情况下重试会创建新任务，
        所以**有副作用的操作必须显式传键**。
        """
        key = idempotency_key or f"auto-{secrets.token_hex(8)}"

        existing = self._store.find_by_idempotency(key)
        if existing is not None:
            return existing, False

        task = Task(
            id=f"t-{secrets.token_hex(6)}",
            idempotency_key=key,
            type=task_type,
            session_id=session_id,
            from_node=from_node,
            to_node=to_node,
            status=TaskStatus.SUBMITTED,
            mode=mode,
            params=dict(params),
            trace_id=f"tr-{secrets.token_hex(4)}",
        )
        self._store.enqueue(task)
        return task, True

    # ------------------------------------------------------------------
    # 领取（执行方侧）
    # ------------------------------------------------------------------

    def claim(self, node_id: str, *, now: str | None = None) -> Task | None:
        """领取一个待办任务。返回 None 表示队列为空。

        领取即意味着：状态置 ``working``、尝试次数 +1、租约开始计时。
        """
        ref = now or now_iso()
        return self._store.claim(
            node_id, lease_seconds=self._lease_seconds, now=ref
        )

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------

    def _transition(self, task: Task, target: str) -> Task:
        if not TaskStatus.can_transition(task.status, target):
            raise PolicyDenied(
                RuleId.LF_INVALID_TRANSITION,
                f"{task.id}: {task.status} → {target} not allowed",
            )
        task.status = target
        return task

    def request_input(self, task: Task, question: str) -> Task:
        """执行方向发起方追问。

        这是 ``input_required`` 存在的意义：让执行方在中途要信息时
        不必失败重来。追问内容会附在任务的 params 里一并回传，
        **因此它同样受应答边界约束**（见 data/boundary.py）。
        """
        self._transition(task, TaskStatus.INPUT_REQUIRED)
        task.params = {**dict(task.params), "_question": question}
        self._store.update(task)
        return task

    def complete(self, task: Task, artifacts) -> Task:
        """标记完成。**

        调用方应在此前先跑过应答边界（``data/boundary.py``），
        本模块不做内容校验——职责分离：边界管"能不能给"，
        生命周期管"给到了没有"。
        """
        self._transition(task, TaskStatus.COMPLETED)
        task.artifacts = list(artifacts)
        self._store.update(task)
        return task

    def fail(self, task: Task, reason: str) -> Task:
        """标记失败。

        失败原因只记在任务对象上，**不自动对外回传**——
        是否需要告知发起方由调用方决定（避免把内部错误细节当信息漏出去）。
        """
        self._transition(task, TaskStatus.FAILED)
        task.params = {**dict(task.params), "_fail_reason": reason}
        self._store.update(task)
        return task

    def cancel(self, task: Task) -> Task:
        self._transition(task, TaskStatus.CANCELED)
        self._store.update(task)
        return task

    # ------------------------------------------------------------------
    # 租约与恢复
    # ------------------------------------------------------------------

    def release_expired(self, *, now: str | None = None) -> list[Task]:
        """释放所有租约过期的任务，返回被释放的任务。

        由调用方定期触发。释放后任务回到队列，可被重新领取——
        **这是断电自愈的核心**：不需要知道执行方为什么消失，
        只需要知道"它超过约定时间没交活"，任务就自动回来。

        被释放的任务**保持 ``working`` 状态且 ``attempt`` 已递增**。
        不重置状态是刻意的：重试次数要累积，否则会无限重试。
        """
        ref = now or now_iso()
        released = self._store.release_expired(now=ref)

        final: list[Task] = []
        for task in released:
            if task.attempt >= self._max_attempts:
                # 超过重试上限：标记失败并落终态，不再回到队列
                try:
                    self.fail(task, f"exceeded max attempts ({self._max_attempts})")
                except PolicyDenied:
                    pass  # 已处于终态则忽略
            else:
                final.append(task)
        return final

    def renewal_deadline(self, task: Task) -> str:
        """计算续租后的新到期时间（供长任务使用）。"""
        return (datetime.now(timezone.utc)
                + timedelta(seconds=self._lease_seconds)).astimezone().isoformat(
            timespec="seconds")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> Task | None:
        return self._store.get(task_id)

    def session_view(self, session_id: str) -> dict:
        """一次会话的任务视图。

        注意：本方法**只汇报状态，不做任何决策**。任务图由发起方决定，
        这里只把它记录的样子呈现出来。
        """
        tasks = list(self._store.list_by_session(session_id))
        return {
            "session_id": session_id,
            "total": len(tasks),
            "tasks": [
                {
                    "id": t.id,
                    "type": t.type,
                    "status": t.status,
                    "from": t.from_node,
                    "to": t.to_node,
                    "attempt": t.attempt,
                    "level": str(t.max_artifact_level()),
                }
                for t in sorted(tasks, key=lambda x: x.id)
            ],
            "by_status": _count_by(tasks, lambda t: t.status),
        }

    def is_expired(self, task: Task) -> bool:
        if not task.ttl:
            return False
        return parse_iso(now_iso()) > parse_iso(task.ttl)


def _count_by(items, keyfn) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        k = keyfn(it)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "TaskLifecycle",
]
