"""信箱（D1）—— 投递与领取的薄封装。

它把"策略判定 → 生命周期提交"串起来，保证**没有任何路径能绕过策略**。
这是本模块存在的唯一理由：如果调用方可以直接调 ``TaskLifecycle.submit``，
就会有绕过策略的路径。

设计要点
--------
* **投递即校验。** 先过策略引擎，再进队列。顺序不能反过来——
  否则非法任务会短暂出现在队列里，可能被领取。
* **空队列返回 None，不做长轮询。** 节点通过周期性轮询取任务。
  长轮询在受限网络下不可靠（连接会被中间设备切断）。
* **配额在投递侧扣。** 拒绝时不消耗配额，避免用拒绝把配额刷满。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..control.policy import PolicyEngine, Usage
from ..control.registry import Registry
from ..core.errors import PolicyDenied, RuleId
from ..core.models import Task, TaskMode
from .task import TaskLifecycle


@dataclass(frozen=True)
class DeliveryResult:
    """投递结果。命中幂等键时 ``created=False``。"""

    task: Task
    created: bool
    effective_max_level: str


class Mailbox:
    """信箱：投递与领取的唯一入口。"""

    def __init__(self, policy: PolicyEngine, registry: Registry,
                 lifecycle: TaskLifecycle) -> None:
        self._policy = policy
        self._registry = registry
        self._lifecycle = lifecycle

    # ------------------------------------------------------------------
    # 投递
    # ------------------------------------------------------------------

    def deliver(self, *, from_node: str, to_node: str, task_type: str,
                params: dict, session_id: str = "",
                idempotency_key: str | None = None,
                mode: str = TaskMode.SYNC,
                usage: Usage | None = None) -> DeliveryResult:
        """投递一个任务。

        检查顺序（**每一步失败都不会留下痕迹**）：

        ① 发布方有权发布该类型（``MX-ISSUER-01``）
        ② 目标节点可路由（未吊销、已批准、alive）
        ③ 域间矩阵允许这次投递，且类型在允许范围（``MX-DOMAIN-02``）
        ④ 目标接受来自该域的委托（``MX-RECEIVE-01``）
        ⑤ 配额充足（``QT-*``）

        通过后才写入队列。
        """
        # ① 发布权限
        self._policy.check_publish(from_node, task_type)

        # ② 目标可路由
        self._registry.require_routable(to_node)

        # ③④ 域间矩阵 + 目标接受范围
        decision = self._policy.check_delivery(from_node, to_node, task_type)

        # ⑤ 配额（拒绝时不扣减）
        self._policy.check_quota(to_node, usage=usage or Usage())

        # 提交（内部处理幂等）
        task, created = self._lifecycle.submit(
            task_type=task_type,
            from_node=from_node,
            to_node=to_node,
            params=params,
            session_id=session_id,
            idempotency_key=idempotency_key,
            mode=mode,
        )
        return DeliveryResult(
            task=task, created=created,
            effective_max_level=str(decision.effective_max_level),
        )

    # ------------------------------------------------------------------
    # 领取
    # ------------------------------------------------------------------

    def poll(self, node_id: str) -> Task | None:
        """领取一个待办任务。返回 None 表示信箱为空。

        领取前会顺带刷新一次该节点的心跳——因为"来取任务"本身就是
        "我还活着"的最强证据，比单独发心跳更可靠。
        """
        rec = self._registry.record(node_id)
        if rec is None:
            raise PolicyDenied(RuleId.AU_NODE_UNKNOWN)
        if rec.state == "revoked":
            raise PolicyDenied(RuleId.AU_CREDENTIAL_REVOKED)
        if not rec.approved:
            raise PolicyDenied(RuleId.MX_RECEIVE_NOT_ALLOWED,
                               f"{node_id} not approved")

        self._registry.heartbeat(node_id)
        self._registry.resurrect(node_id)
        return self._lifecycle.claim(node_id)

    # ------------------------------------------------------------------
    # 运维辅助
    # ------------------------------------------------------------------

    def sweep(self) -> dict[str, int]:
        """一次周期性维护：标记 stale + 释放过期租约。

        由调用方按固定间隔触发。返回两个计数，便于观测。
        这也是"断电恢复零人工干预"的驱动源——只要这个维护在跑，
        被中断的任务最终都会回到队列。
        """
        stale = self._registry.sweep()
        released = self._lifecycle.release_expired()
        return {"marked_stale": len(stale), "released": len(released)}


__all__ = ["DeliveryResult", "Mailbox"]
