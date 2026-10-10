"""策略执行点（M5）—— 所有安全策略的唯一裁决处。

**任何绕过本模块的投递路径都是设计缺陷。** 本模块是纯逻辑、无状态、
无 I/O 的：它只回答"这次操作允许吗"，不负责记录、投递或计数。
配额等有状态检查通过显式传入用量来完成，因此本模块可以脱离任何
存储与网络做完整测试。

三重检查模型
------------
一次应答是否放行，需要同时满足三个来源的约束：

1. **域间矩阵**（``policy.matrix``）—— 这对信任域之间允许什么
2. **节点自约束**（角色卡的 ``constraints``）—— 这个节点自己声明不接受什么
3. **能力声明**（``capability``）—— 这个任务类型承诺的产出范围

三者取**最严**。举例：矩阵允许片段级，但节点自声明上限摘要级，则实际上限是摘要级。
这是"信任约束与能力正交"在代码上的体现。

拒绝语义
--------
所有拒绝都抛 :class:`PolicyDenied`，**只携带 ``rule_id``**。
不要把被拒内容、字段名、字段值放进异常——它会被序列化进响应体。
需要排查时，细节写本地审计日志（``mgmt/audit.py``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..core.errors import PolicyDenied, RuleId
from ..core.models import Artifact, Capability, Level, RoleCard, Task
from ..mgmt.config import MatrixRule, NexusConfig

#: 单个自由文本字段的字节上限。
#: 自由文本是提示注入的主要载体，也是唯一无法用字段白名单约束的内容形态，
#: 因此用长度硬上限兜底。
MAX_FREETEXT_BYTES = 4096


@dataclass(frozen=True)
class Usage:
    """有状态的用量快照。由调用方（生命周期模块）维护并传入。"""

    tasks_today: int = 0
    bytes_today: int = 0
    concurrent_now: int = 0


@dataclass(frozen=True)
class DeliveryDecision:
    """放行后返回的判定详情，供投递层与审计层使用。"""

    rule: MatrixRule
    #: 三重约束取最小值后的实际上限
    effective_max_level: Level
    task_type: str


class PolicyEngine:
    """策略裁决器。**无状态**，可安全地在多线程间共享。"""

    def __init__(self, config: NexusConfig) -> None:
        self._cfg = config
        self._type_providers: dict[str, set[str]] = {}
        for card in config.nodes.values():
            for cap in card.capabilities:
                self._type_providers.setdefault(cap.type, set()).add(card.node_id)

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------

    @property
    def registered_types(self) -> frozenset[str]:
        """全部已注册的任务类型。

        注意：这是"存在哪些能力"，不是"你能用哪些能力"。
        能力清单本身是发现机制的基础，不属于机密；
        机密的是**跨域可达性**，那由矩阵控制。
        """
        return frozenset(self._type_providers)

    def providers_of(self, task_type: str) -> frozenset[str]:
        return frozenset(self._type_providers.get(task_type, ()))

    def _card(self, node_id: str) -> RoleCard:
        card = self._cfg.nodes.get(node_id)
        if card is None:
            raise PolicyDenied(RuleId.AU_NODE_UNKNOWN)
        return card

    # ------------------------------------------------------------------
    # ① 发布侧：谁能发、能发什么
    # ------------------------------------------------------------------

    def check_publish(self, node_id: str, task_type: str) -> None:
        """校验节点是否有权发布该类型的任务。

        两种失败共用 ``MX-ISSUER-01``，不区分"角色没开"与"类型不在白名单"——
        对调用方而言两者都是"你不能发这个"，区分只会增加探测面。
        """
        card = self._card(node_id)

        if not card.roles.issuer:
            raise PolicyDenied(
                RuleId.MX_ISSUER_NOT_ALLOWED, f"{node_id} roles.issuer=false"
            )

        if not _match_any(card.constraints.can_issue_types, task_type):
            raise PolicyDenied(
                RuleId.MX_ISSUER_NOT_ALLOWED,
                f"{node_id} can_issue_types={card.constraints.can_issue_types}",
            )

    # ------------------------------------------------------------------
    # ② 投递侧：这对域之间允许什么
    # ------------------------------------------------------------------

    def check_delivery(self, from_node: str, to_node: str,
                       task_type: str) -> DeliveryDecision:
        """校验一次跨节点投递。这是矩阵与白名单的主检查点。"""
        src = self._card(from_node)
        dst = self._card(to_node)

        # ---- 类型是否已注册（全局）----
        if task_type not in self._type_providers:
            raise PolicyDenied(
                RuleId.WL_TYPE_NOT_REGISTERED, f"unregistered type {task_type}"
            )

        # ---- 目标是否真的提供该能力 ----
        if to_node not in self._type_providers[task_type]:
            # 类型存在，但目标不提供它 → 按"类型不可用"处理，
            # 不暴露"哪个节点提供什么"的完整映射
            raise PolicyDenied(
                RuleId.WL_TYPE_NOT_REGISTERED,
                f"{to_node} does not provide {task_type}",
            )

        # ---- 域间矩阵（默认拒绝在此落地）----
        rule = self._cfg.policy.find(src.domain, dst.domain)
        if rule is None:
            raise PolicyDenied(
                RuleId.MX_DOMAIN_FORBIDDEN,
                f"no matrix rule for {src.domain} → {dst.domain}",
            )

        if not rule.matches_type(task_type):
            raise PolicyDenied(
                RuleId.MX_DOMAIN_FORBIDDEN,
                f"{task_type} not in {src.domain}→{dst.domain} allow_types",
            )

        # ---- 目标是否接受来自该域的委托 ----
        if not _match_any(dst.constraints.can_receive_from, src.domain):
            raise PolicyDenied(
                RuleId.MX_RECEIVE_NOT_ALLOWED,
                f"{to_node} can_receive_from={dst.constraints.can_receive_from}",
            )

        # ---- 委托链深度：目标是否允许继续转发 ----
        # 当前配置默认 can_delegate_to 为空（不做二次委托）。
        # v0.1 只在任务已标记为"来自二次委托"时检查，链深逻辑见 v0.2。

        # ---- 三重上限取最小 ----
        cap = dst.capability(task_type)
        assert cap is not None  # 前面已确认目标提供该能力
        effective = min(rule.max_level, dst.constraints.max_response_level, cap.level)

        return DeliveryDecision(
            rule=rule, effective_max_level=effective, task_type=task_type
        )

    # ------------------------------------------------------------------
    # ③ 应答侧：产出是否越界（应答边界的策略部分）
    # ------------------------------------------------------------------

    def check_response(self, node_id: str, task: Task) -> None:
        """校验执行方回传的产出是否越界。

        检查四项，任一失败即拒绝：

        1. 每个 artifact 的等级不得超过**三重上限的最小值**
        2. 每个 artifact 的等级不得超过**该能力声明的等级**
        3. 结构化产出的字段必须全部在**应答字段白名单**内
        4. 自由文本字段不得超出长度上限
        """
        card = self._card(node_id)

        # 找到本次任务对应的能力声明；找不到说明该任务不该被执行
        target = task.from_node
        cap = card.capability(task.type)
        if cap is None:
            raise PolicyDenied(
                RuleId.WL_TYPE_NOT_REGISTERED,
                f"{node_id} attempted to respond for unregistered type {task.type}",
            )

        src_card = self._cfg.nodes.get(target)
        if src_card is None:
            raise PolicyDenied(RuleId.AU_NODE_UNKNOWN, f"unknown origin {target}")

        rule = self._cfg.policy.find(src_card.domain, card.domain)
        if rule is None:
            raise PolicyDenied(
                RuleId.MX_DOMAIN_FORBIDDEN,
                f"no matrix rule for {src_card.domain} → {card.domain}",
            )

        ceiling = min(rule.max_level, card.constraints.max_response_level, cap.level)

        for i, art in enumerate(task.artifacts):
            # -- 检查 1 & 2：等级 --
            if art.level > ceiling:
                raise PolicyDenied(
                    RuleId.WL_LEVEL_EXCEEDED,
                    f"artifact[{i}] level={art.level} ceiling={ceiling}",
                )

            # -- 检查 3：字段白名单 --
            fields = _extract_fields(art.data)
            allowed = set(cap.response_fields)
            stray = fields - allowed
            if stray:
                # 只报数量，不报字段名——字段名本身可能携带信息
                raise PolicyDenied(
                    RuleId.WL_FIELD_NOT_ALLOWED,
                    f"artifact[{i}] has {len(stray)} field(s) outside the allow-list",
                )

            # -- 检查 4：自由文本长度 --
            for fname in cap.freetext_fields:
                value = _get_field(art.data, fname)
                if value is None:
                    continue
                if not isinstance(value, str):
                    raise PolicyDenied(
                        RuleId.WL_FREETEXT_NOT_ALLOWED,
                        f"artifact[{i}].{fname} must be a string",
                    )
                size = len(value.encode("utf-8"))
                if size > MAX_FREETEXT_BYTES:
                    raise PolicyDenied(
                        RuleId.WL_FREETEXT_NOT_ALLOWED,
                        f"artifact[{i}].{fname} size={size}",
                    )

    # ------------------------------------------------------------------
    # ④ 有状态检查：配额与并发
    # ------------------------------------------------------------------

    def check_quota(self, node_id: str, *, usage: Usage,
                    planned_bytes: int = 0) -> None:
        """配额检查。用量由调用方统计并传入，本模块自身无状态。"""
        card = self._card(node_id)
        q = card.constraints.quota

        if usage.tasks_today >= q.tasks_per_day:
            raise PolicyDenied(
                RuleId.QT_TASKS_PER_DAY,
                f"{node_id} tasks_today={usage.tasks_today}/{q.tasks_per_day}",
            )
        if planned_bytes > q.bytes_per_response:
            raise PolicyDenied(
                RuleId.QT_BYTES_PER_RESPONSE,
                f"planned={planned_bytes} limit={q.bytes_per_response}",
            )
        if usage.bytes_today + planned_bytes > q.bytes_per_day:
            raise PolicyDenied(
                RuleId.QT_BYTES_PER_DAY,
                f"{node_id} bytes_today={usage.bytes_today}",
            )
        if usage.concurrent_now >= q.concurrent_tasks:
            raise PolicyDenied(
                RuleId.QT_CONCURRENCY,
                f"{node_id} concurrent={usage.concurrent_now}/{q.concurrent_tasks}",
            )

    # ------------------------------------------------------------------
    # ⑤ 委托链
    # ------------------------------------------------------------------

    def check_delegation(self, node_id: str, target_domain: str) -> None:
        """校验节点能否向指定域二次委托。默认禁止（白名单为空即拒绝）。"""
        card = self._card(node_id)
        if not card.constraints.can_delegate_to:
            raise PolicyDenied(
                RuleId.MX_DELEGATION_TOO_DEEP,
                f"{node_id} can_delegate_to is empty",
            )
        if target_domain not in card.constraints.can_delegate_to:
            raise PolicyDenied(
                RuleId.MX_DELEGATION_TOO_DEEP,
                f"{target_domain} not in {card.constraints.can_delegate_to}",
            )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _match_any(patterns: Sequence[str], value: str) -> bool:
    """前缀通配匹配。``"demo.*"`` 匹配 ``demo.echo`` 但不匹配 ``demoX.y``。"""
    for p in patterns:
        if p.endswith(".*"):
            if value.startswith(p[:-1]):
                return True
        elif p == value:
            return True
    return False


def _extract_fields(data: Any) -> set[str]:
    """从结构化产出里提取字段名集合。

    * dict        → 其键
    * list[dict]  → 所有元素的键之并集
    * 其他        → 空集（标量没有字段概念）
    """
    if isinstance(data, Mapping):
        return {str(k) for k in data}
    if isinstance(data, (list, tuple)):
        out: set[str] = set()
        for item in data:
            out |= _extract_fields(item)
        return out
    return set()


def _get_field(data: Any, name: str) -> Any:
    if isinstance(data, Mapping):
        return data.get(name)
    if isinstance(data, (list, tuple)):
        for item in data:
            v = _get_field(item, name)
            if v is not None:
                return v
    return None


def make_task(*, task_id: str, idempotency_key: str, task_type: str,
              from_node: str, to_node: str, params: Mapping[str, Any],
              artifacts: Iterable[Artifact] = (), session_id: str = "") -> Task:
    """构造任务的便捷函数（供测试与 demo 使用）。"""
    return Task(
        id=task_id,
        idempotency_key=idempotency_key,
        type=task_type,
        session_id=session_id,
        from_node=from_node,
        to_node=to_node,
        params=dict(params),
        artifacts=list(artifacts),
    )


__all__ = [
    "MAX_FREETEXT_BYTES",
    "DeliveryDecision",
    "PolicyEngine",
    "Usage",
    "make_task",
]
