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
from typing import TYPE_CHECKING, Any, Callable

from ..control.policy import PolicyEngine, Usage
from ..control.registry import Registry
from ..core.errors import ConfigError, PolicyDenied, RuleId
from ..core.models import Level, Task, TaskMode
from .task import TaskLifecycle

if TYPE_CHECKING:                      # 仅类型检查用，避免循环导入
    from ..control.matcher import Matcher


@dataclass(frozen=True)
class DeliveryResult:
    """投递结果。命中幂等键时 ``created=False``。"""

    task: Task
    created: bool
    effective_max_level: str


class Mailbox:
    """信箱：投递与领取的唯一入口。"""

    def __init__(self, policy: PolicyEngine, registry: Registry,
                 lifecycle: TaskLifecycle, *,
                 matcher: "Matcher | None" = None) -> None:
        self._policy = policy
        self._registry = registry
        self._lifecycle = lifecycle
        # 省略时自动模式不可用（Direct 投递不受影响）。
        # 之所以允许省略：纯 Direct 部署与大量既有测试不需要它，
        # 强制要求会让"只想投给指定节点"的场景凭空多一个依赖。
        self._matcher = matcher

    # ------------------------------------------------------------------
    # 投递
    # ------------------------------------------------------------------

    def deliver(self, *, from_node: str, to_node: str | None = None,
                task_type: str, params: dict, session_id: str = "",
                idempotency_key: str | None = None,
                mode: str = TaskMode.SYNC,
                min_level: Level = Level.METADATA,
                usage_for: Callable[[str], Usage] | None = None) -> DeliveryResult:
        """投递一个任务。

        :param to_node: 目标节点。**省略时走自动匹配**（M4）：
            按能力筛出候选、确定性排序、逐个过策略校验，取第一个通过的。
        :param usage_for: 给定节点 ID、返回其当前用量快照的回调。

            ⚠ **为什么是回调而不是直接传 ``Usage``**：自动模式下目标节点
            要等匹配完成才知道，调用方无法提前算。传死值会导致配额
            按错误的节点计算，等于配额失效。

            ⚠ **省略时按零用量处理**（与 v0.5 之前的行为一致）。
            生产装配**必须**传入，否则按日配额永不触发——
            注意这与 v0.1 就存在的 ``bytes_per_day`` 未实现是两回事：
            那个是统计缺失，这个只是没接线。

        检查顺序（**每一步失败都不会留下痕迹**）：

        ⓪ 幂等命中 → 直接返回已有任务（**必须先于一切校验**，理由见下）
        ① 发布方有权发布该类型（``MX-ISSUER-01``）
        ② 目标节点可路由（未吊销、已批准、alive）
        ③ 域间矩阵允许这次投递，且类型在允许范围（``MX-DOMAIN-02``）
        ④ 目标接受来自该域的委托（``MX-RECEIVE-01``）
        ⑤ 配额充足（``QT-*``）

        ⭐ 为什么幂等检查要提到最前面
        ----------------------------
        自动匹配的结果**可能随时间变化**（负载变了、节点被吊销了）。
        若幂等检查留在最后（在 :meth:`TaskLifecycle.submit` 里），
        就会出现：重试同一笔投递时先走了匹配，而此刻候选恰好都不可用，
        于是报"类型不可用"——**可那笔任务其实早就成功了**。
        调用方会以为失败并再做一次业务补偿，那才是真正的重复副作用。

        幂等命中时还要求**归属一致**（``from_node`` 相同的任务）：
        否则猜到别人幂等键的节点就能通过这条快捷路径探测到
        他人任务的存在与内容（返回体里含 params）。
        归属不符时当作未命中，继续走正常流程。
        """
        # ⓪ 幂等（带归属校验）
        if idempotency_key:
            existing = self._lifecycle.find_by_idempotency(idempotency_key)
            if existing is not None and existing.from_node == from_node:
                return DeliveryResult(
                    task=existing, created=False,
                    # 已有任务当初通过过校验，但现在配置可能变了。
                    # 这里**不重算**：重算失败就会返回一个"任务存在但算不出等级"
                    # 的怪状态。保守给当前任务实际能用的上限。
                    effective_max_level=str(self._effective_level(existing)),
                )

        # ① 发布权限
        self._policy.check_publish(from_node, task_type)

        # ②③④ 目标确定 + 全部校验
        if to_node is None:
            to_node, decision = self._select_target(
                from_node=from_node, task_type=task_type, min_level=min_level,
            )
        else:
            self._registry.require_routable(to_node)
            decision = self._policy.check_delivery(from_node, to_node, task_type)

        # ⑤ 配额（拒绝时不扣减）
        usage = usage_for(to_node) if usage_for is not None else Usage()
        self._policy.check_quota(to_node, usage=usage)

        # 提交（幂等已在 ⓪ 处理，这里的二次检查只是纵深）
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
    # 自动匹配（M4）
    # ------------------------------------------------------------------

    def _select_target(self, *, from_node: str, task_type: str,
                       min_level: Level) -> tuple[str, Any]:
        """按能力自动选目标。返回 ``(node_id, 策略判定)``。

        ⭐ **失败时的错误码是刻意统一的。**

        "没有节点声明这个能力"、"有节点但等级不够"、"有候选但都被策略拒绝"
        —— 三种情况对外一律报 ``WL-TYPE-404``。理由：后两种若单独报错，
        等于告诉调用方"确实有人能做，只是不给你做"，那就是把
        **网络拓扑与权限布局**泄露出去（"谁有这能力"本身也是信息）。

        真实原因写进本地审计（``mailbox.match`` 记录候选数量与拒绝码），
        运维查得到，调用方查不到。这与"拒绝时不泄露细节"的整体约定一致。
        """
        if self._matcher is None:
            # 没装匹配器却要求自动匹配 —— 是部署配置问题，不是攻击
            raise ConfigError(
                "自动匹配不可用：Mailbox 未装配 Matcher。"
                "请改用显式 to_node，或在组装时传入 matcher。"
            )

        from ..control.matcher import Requirement

        req = Requirement(
            task_type=task_type,
            min_level=min_level,
            from_domain=self._registry.domain_of_safe(from_node),
        )
        candidates = self._matcher.candidates(req)
        if not candidates:
            raise PolicyDenied(
                RuleId.WL_TYPE_NOT_REGISTERED,
                f"no routable provider for {task_type} "
                f"(min_level={min_level})",
            )

        last_denied: PolicyDenied | None = None
        for cand in candidates:
            try:
                decision = self._policy.check_delivery(
                    from_node, cand.node_id, task_type
                )
            except PolicyDenied as denied:
                last_denied = denied
                continue
            return cand.node_id, decision

        # 全部候选都被策略拒绝：对外统一成"类型不可用"，
        # 真实拒绝码留在 detail 里（只进本地日志，不进响应体）
        raise PolicyDenied(
            RuleId.WL_TYPE_NOT_REGISTERED,
            f"all {len(candidates)} candidate(s) denied; "
            f"last={last_denied.rule_id.value if last_denied else 'n/a'}",
        )

    def _effective_level(self, task: Task) -> Level:
        """已有任务当前实际可用的等级上限。

        幂等命中时用于填充 ``effective_max_level``，不参与放行判断。
        算不出来时退到最保守的 ``metadata`` —— 这个值只影响调用方
        对"能拿到多细"的预期，宁可少说也不要多说。
        """
        try:
            decision = self._policy.check_delivery(
                task.from_node, task.to_node, task.type
            )
            return decision.effective_max_level
        except PolicyDenied:
            return Level.METADATA

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
