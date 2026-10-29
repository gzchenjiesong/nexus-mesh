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
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..core.errors import PolicyDenied, RuleId
from ..core.models import Artifact, Capability, Level, RoleCard, Task
from ..mgmt.config import MatrixRule, NexusConfig, RoleDecl

#: 单个自由文本字段的字节上限。
#: 自由文本是提示注入的主要载体，也是唯一无法用字段白名单约束的内容形态，
#: 因此用长度硬上限兜底。
MAX_FREETEXT_BYTES = 4096


# ⚠ `Usage` 类已删除（权限模型改造 Step 5b）。
#
#   它是配额用量快照（按日任务数 / 字节 / 并发）。配额删了，它也就没有
#   使用者了 —— 现在只需要一个数：目标信箱里攒了多少个待办。
#   那个数由 ``app.backlog_of()`` 从队列直接数，不需要专门的快照类型。


@dataclass(frozen=True)
class DeliveryDecision:
    """放行后返回的判定详情，供投递层与审计层使用。"""

    rule: MatrixRule
    #: 三重约束取最小值后的实际上限
    effective_max_level: Level
    task_type: str


class PolicyEngine:
    """策略裁决器。**无状态**，可安全地在多线程间共享。

    :param card_resolver: 角色卡来源。省略时只从配置取——那意味着
        **通过 HTTP 自注册的节点对策略层不可见**，它们会被当成"未知节点"。

        ⭐ 装配时应传入 ``registry.card``，让判定以**注册表**为准。
        这一步是自注册能真正生效的前提：节点注册进去之后，策略层必须
        能看见它的能力与约束，否则"注册成功但永远无法参与协作"。

        之所以用回调而不是直接依赖 Registry：策略层不该知道注册表的存在，
        它只需要一个"给我 node_id、还我角色卡"的函数。依赖方向保持在
        "策略 ← 注册表"之外。
    :param cards_source: 全部角色卡的枚举源。省略时只枚举配置里的节点。

        与 ``card_resolver`` 是两件事：那个回答"某个节点是什么"，
        这个回答"现在都有谁"。**类型索引依赖后者**——如果只解析单个节点
        却用静态的类型表，自注册节点提供的能力仍然不可见（``WL-TYPE-404``）。
    """

    def __init__(self, config: NexusConfig, *,
                 card_resolver: Callable[[str], RoleCard | None] | None = None,
                 cards_source: Callable[[], Sequence[RoleCard]] | None = None) -> None:
        self._cfg = config
        self._resolve: Callable[[str], RoleCard | None] = (
            card_resolver if card_resolver is not None
            else lambda node_id: config.nodes.get(node_id)
        )
        self._all: Callable[[], Sequence[RoleCard]] = (
            cards_source if cards_source is not None
            else lambda: tuple(config.nodes.values())
        )

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 角色解析（权限模型改造 Step 3）
    # ------------------------------------------------------------------

    def role_of(self, card: RoleCard) -> RoleDecl | None:
        """这张卡片以什么角色接入。

        ★ **返回 None 表示"尚未迁移到角色模型"**，调用方应退回旧判据。

        取值顺序：

          ① 卡片显式声明的 ``role`` → 从配置的角色表里查
          ② 卡片未声明（旧卡片）→ 用 ``derived_role()`` 从旧标志推导，
             再去角色表里查

        第 ② 步是过渡期的桥：让还没改配置的部署也能走新判定。
        Step 5 删除旧标志后，这一步一起删。
        """
        if not self._cfg.roles:
            return None          # 配置里没有角色表 → 无从判定

        name = card.role or card.derived_role()
        if not name:
            return None          # 既无声明也无从推导（无角色）

        return self._cfg.roles.get(name)

    @property
    def registered_types(self) -> frozenset[str]:
        """全部已定义的任务类型。

        注意：这是"存在哪些类型"，不是"你能用哪些类型"。
        类型清单本身是发现机制的基础，不属于机密；
        机密的是**跨域可达性**，那由矩阵控制。

        ## 权限模型改造后的来源变化

            有 types 配置 → 从配置读（唯一权威）
            无 types 配置 → 从各节点 capabilities 汇总（旧路径）

        新路径下类型是**全局定义**的，所以同名类型不可能有两套契约 ——
        这正是改造要修的问题。

        **每次现算，不缓存。** 节点数是"少数已知节点"，遍历开销可忽略；
        缓存反而要处理失效，而漏失效意味着新注册的能力用不了。
        """
        if self._cfg.types:
            return frozenset(self._cfg.types)
        return frozenset(
            cap.type for card in self._all() for cap in card.capabilities
        )

    def providers_of(self, task_type: str) -> frozenset[str]:
        """哪些节点能服务某个类型。

        ## 权限模型改造后的来源变化

            有 roles 配置 → 看每个节点 role 的 serves（权威）
            无 roles 配置 → 看每个节点声明的 capabilities（旧路径）

        新路径下"能服务什么"由角色决定，不再由节点自己声明。
        这样同角色的节点能力一致，审批时看到的就是全部。
        """
        if not self._cfg.roles:
            return frozenset(
                card.node_id for card in self._all()
                if card.capability(task_type) is not None
            )

        out = set()
        for card in self._all():
            role = self.role_of(card)
            if role is None:
                # 显式声明了 role 但配置里没有这个角色 —— 那是配置错误，
                # 加载时就该被拦住；这里保守处理为"不服务任何类型"
                continue
            if role.serves_type(task_type):
                out.add(card.node_id)
        return frozenset(out)

    def _card(self, node_id: str) -> RoleCard:
        card = self._resolve(node_id)
        if card is None:
            raise PolicyDenied(RuleId.AU_NODE_UNKNOWN)
        return card

    # ------------------------------------------------------------------
    # ① 发布侧：谁能发、能发什么
    # ------------------------------------------------------------------

    def check_publish(self, node_id: str, task_type: str) -> None:
        """校验节点是否有权发布任务。

        ## 权限模型改造后才变成现在这样

        判据从「issuer 标志 + 逐类型白名单」简化成「角色的 can_issue」。

        以前是两道：先看 `roles.issuer`，再看 `can_issue_types` 是否含该类型。
        现在只有一道：角色允不允许发布。**类型不再作为发布权的维度** ——
        类型由 Mesh 全局定义，能发什么不再是"谁的权限"，而是
        "这个类型存不存在"（由 ``check_delivery`` 判）。

        ## 向后兼容

        配置里没有 ``roles`` 表时（尚未迁移的部署）退回旧判据。
        Step 5 删除旧字段后，这条回退路径一起删。

        ## 错误信息

        失败时**不区分**"角色不允许"与"类型不存在" ——
        对调用方而言都是"你不能发这个"，区分只会增加探测面。
        """
        card = self._card(node_id)

        role = self.role_of(card)
        if role is not None:
            # ---- 新路径：角色说了算 ----
            if role.can_issue:
                return
            raise PolicyDenied(
                RuleId.MX_ISSUER_NOT_ALLOWED,
                f"{node_id} role={role.name!r} cannot issue",
            )

        # ---- 旧路径：尚未迁移的部署（Step 5 删除）----
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
        """校验一次跨节点投递。

        ## 权限模型改造后的检查链

            ① 类型是否存在（types 配置，或旧路径的 capabilities 汇总）
            ② 目标能否服务它（role.serves，或旧路径的 capabilities）
            ③ 域是否可达（矩阵，纯可达性）
            ④ 应答上限 = min(role 上限, 节点自限)

        ## 与旧版的差异

        删掉了两道检查：

          · **矩阵的类型白名单** —— 类型权限归 role，矩阵只管可达性
          · **`can_receive_from` 逐来源比对** —— 域矩阵已经承担这件事

        后者值得说明：`can_receive_from` 是"节点自己声明谁能给它派活"，
        属于旧模型"逐项申请权限"的思路。域矩阵是运维定义的围栏，
        权力归属更清楚 —— 节点不该自己决定谁能触达它。

        ⚠ 代价：**同域内的 driver 现在是等价的。** 域隔离挡住跨域乱发，
        但域内没有第二道限制。角色少、域内 driver 少是这套模型的前提。
        """
        src = self._card(from_node)
        dst = self._card(to_node)

        # ---- ① 类型是否存在 ----
        if task_type not in self.registered_types:
            raise PolicyDenied(
                RuleId.WL_TYPE_NOT_REGISTERED, f"unregistered type {task_type}"
            )

        # ---- ② 目标能否服务 ----
        if to_node not in self.providers_of(task_type):
            # 类型存在，但目标不服务它 → 按"类型不可用"处理，
            # 不暴露"哪个节点服务什么"的完整映射
            raise PolicyDenied(
                RuleId.WL_TYPE_NOT_REGISTERED,
                f"{to_node} does not serve {task_type}",
            )

        # ---- ③ 域间可达性（默认拒绝在此落地）----
        rule = self._cfg.policy.find(src.domain, dst.domain)
        if rule is None:
            raise PolicyDenied(
                RuleId.MX_DOMAIN_FORBIDDEN,
                f"no matrix rule for {src.domain} → {dst.domain}",
            )

        # ---- ④ 应答上限：多方独立来源取最小 ----
        #
        # 新路径：min(角色上限, 节点自限)
        # 旧路径：min(节点约束, 能力声明) —— 尚未迁移的部署
        role = self.role_of(dst)
        if role is not None:
            cap = dst.capability(task_type)
            parts = [role.max_response_level, dst.self_limits.max_response_level]
            if cap is not None:      # 旧卡片仍声明了能力等级，一并收紧
                parts.append(cap.level)
            effective = Level(min(p.value for p in parts))
        else:
            cap = dst.capability(task_type)
            assert cap is not None, "旧路径下前面已确认目标提供该能力"
            effective = Level(min(dst.constraints.max_response_level.value,
                                  cap.level.value))

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

        src_card = self._resolve(target)
        if src_card is None:
            raise PolicyDenied(RuleId.AU_NODE_UNKNOWN, f"unknown origin {target}")

        rule = self._cfg.policy.find(src_card.domain, card.domain)
        if rule is None:
            raise PolicyDenied(
                RuleId.MX_DOMAIN_FORBIDDEN,
                f"no matrix rule for {src_card.domain} → {card.domain}",
            )

        # ---- 等级上限：多方独立来源取最小 ----
        #
        # 改造后：min(角色上限, 节点自限, 这次能力的产出等级)
        # 旧路径：min(节点约束, 能力产出等级)
        #
        # 矩阵不再参与 —— 它只管可达性，不管能到多细。
        #
        # ⚠ 保留"多处取最小"这个性质本身是必须的：
        #   它是防止单点配错导致越界的唯一保障。
        role = self.role_of(card)
        if role is not None:
            ceiling = Level(min(role.max_response_level.value,
                                card.self_limits.max_response_level.value,
                                cap.level.value))
        else:
            ceiling = Level(min(card.constraints.max_response_level.value,
                                cap.level.value))

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

    def check_backlog(self, node_id: str, *, pending: int) -> None:
        """信箱深度检查。

        ## 这不是"配额"，是节点自己的防御

        旧模型这里叫 `check_quota`，检查按日任务数、应答字节、并发数。
        **那几项从未生效** —— 而根因是把"节点自己的防御"当成了
        "网络的管制"：

            命令在节点本机执行，网络看不见也管不了它用多少资源。
            "网络允许你干多少"这个问法本身就是错的。

        现在只检查一件事：**目标信箱是不是已经攒满了。**

        ## 为什么必须检查

        不设上限意味着积压可以无限增长 —— 发布方会以为自己投递成功，
        而节点可能已经压了几百个。节点唯一的防御是主动说 `paused`，
        但它可能还没意识到就被淹了。

        `max_backlog` 由节点自己声明（缺省 20），语义是
        「我最多攒这么多，超了请别再发了」。

        ## 被拒之后

        发布方应该**稍后重试或换节点**，而不是以为"我被限权了"。
        所以错误码是 `QT-BACKLOG-07`（"对方的队列满了"），
        而不是任何 `MX-*`（"你没权限"）。
        """
        card = self._card(node_id)
        limit = card.self_limits.max_backlog
        if pending >= limit:
            raise PolicyDenied(
                RuleId.QT_BACKLOG_FULL,
                f"{node_id} pending={pending}/{limit}",
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
    "make_task",
]
