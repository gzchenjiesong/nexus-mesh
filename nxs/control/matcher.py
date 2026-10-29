"""匹配器（M4）—— 给定能力需求，找出谁能干、按什么顺序试。

它在整个流程里的位置
--------------------
::

    需求(能力 + 上下文)
        ↓
    ┌─────────────┐
    │ M4 匹配器    │  按能力筛出候选，**确定性排序**
    └─────────────┘
        ↓ 有序候选列表
    ┌─────────────┐
    │ M5 策略      │  逐个校验"这次委托允许吗"
    └─────────────┘
        ↓ 第一个通过的
      投递（M6）

为什么排序必须确定性
--------------------
这是设计文档里最直白的一条要求，理由不是洁癖：

**随机会让"同一个输入两次跑出不同结果"，而分布式系统的调试本来就难。**
一旦出现"昨天能跑今天不能"的现象，你无法区分是配置变了、节点变了，
还是匹配器随机选了别人。把随机性从这条链上彻底去掉，
所有"为什么投给了 A 而不是 B"的问题都变成一个可复现的查询。

代价是：负载均衡不如随机均匀。但本系统的前提是**"已知的少数节点"**，
不是"成千上万个对等节点"——那种规模才需要随机化的负载均衡。

⭐ 匹配只看"能力 + 约束"，不看"关系"
-------------------------------------
这是 Mesh 语义与 Coordinator 语义的分界点，也是本模块**最容易写错的地方**。

============================  ==========================================
视角                          回答的问题
============================  ==========================================
匹配器（单节点）              **我能不能干这个活**（能力、等级、接不接）
M5 策略（双方关系）            **这次委托允许吗**（域矩阵、配额、委托链）
============================  ==========================================

所以匹配器**不读域间矩阵**。矩阵描述的是"A 域能不能让 B 域干活"，
那是关系；而匹配器只需要知道"B 说自己能干什么"。

这样分界的实际好处：加一条矩阵规则不会影响候选集合，
只影响"试到第几个才成功"——两个变化源被隔开了，排查时不用同时想两件事。

⭐ 候选列表本身是拓扑信息
-------------------------
"能执行 ``work.calendar`` 的节点有 3 个"这句话，泄漏了网络规模与分布。
所以 :meth:`Matcher.candidates` **只供 coordinator 内部与运维使用**，
绝不原样返回给发起方。对外只有"投递成功"或"投递被拒绝"两种结果，
失败时的错误码与"没有这个类型"完全相同（见 :meth:`Matcher.select`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Sequence

from ..core.models import Level, RoleCard
from ..ports.stores import QueueStore
from .policy import _match_any      # noqa: PLC2701 - 复用通配语义，避免两处规则分叉
from .registry import Registry


@dataclass(frozen=True)
class Requirement:
    """能力需求 —— 匹配器的输入。"""

    #: 任务类型。必须在候选节点的角色卡里被声明。
    task_type: str
    #: 调用方**需要**的应答等级。候选必须能提供**至少**这个等级。
    #:
    #: ⚠ 这是需求下限，不是排序偏好——排序时反而优先选"刚好够用"的。
    min_level: Level = Level.METADATA
    #: 来源域。**只用于"同域优先"排序，不用于过滤。**
    #:
    #: 为什么不用它过滤：能不能跨域由矩阵决定，那是 M5 的事。
    #: 在这里过滤等于把矩阵规则复制一份，两处就会慢慢分叉。
    from_domain: str = ""


@dataclass(frozen=True)
class Candidate:
    """一个候选执行者，附带完整的排序依据。

    把排序依据显式带出来，是为了让"为什么投给了它"成为**可回答的问题**——
    运维可以打印候选列表，一眼看出是谁的哪个指标胜出。
    """

    node_id: str
    domain: str
    #: 该候选**能提供**的应答等级 = ``min(卡片上限, 能力声明等级)``。
    level: Level
    #: 当前在途任务数（working 状态）。
    load: int
    #: 并发上限。用于算负载率——绝对数量会冤枉高并发的大节点。
    capacity: int
    #: 来源域与本节点域相同。仅用于排序偏好。
    same_domain: bool

    @property
    def load_ratio(self) -> Fraction:
        """负载率。用 ``Fraction`` 而不是浮点，保证比较精确且可复现。

        为什么用比率而不是绝对数：并发上限 8 的节点跑 3 个任务不算忙，
        并发上限 1 的节点跑 1 个任务已经满了。只看绝对数会选错。
        """
        # capacity 为 0 是不合法的配置，但宁可当成"满了"也不要除零
        return Fraction(self.load, self.capacity) if self.capacity > 0 else Fraction(1)

    def sort_key(self) -> tuple:
        """排序键。**四级，逐级比较，最后一级保证完全确定性。**

        ① ``same_domain`` —— 同域优先。跨域数据流动本身是成本（也增加暴露面），
           能力相当时没有理由舍近求远
        ② ``level`` —— **等级"刚好够用"的优先**（升序）
        ③ ``load_ratio`` —— 负载轻的优先
        ④ ``node_id`` —— 字典序。**这一级存在的唯一目的就是消灭不确定性**
        """
        return (
            not self.same_domain,
            self.level,          # IntEnum，升序 = 能力更保守的优先
            self.load_ratio,
            self.node_id,
        )


class Matcher:
    """按能力筛出候选，确定性排序。

    :param registry: 节点与能力的权威来源。
    :param queue: 用于统计在途任务数（负载）。
    """

    def __init__(self, registry: Registry, queue: QueueStore) -> None:
        self._registry = registry
        self._queue = queue

    # ------------------------------------------------------------------
    # 匹配
    # ------------------------------------------------------------------

    def candidates(self, req: Requirement) -> list[Candidate]:
        """筛出满足需求的候选，按 :meth:`Candidate.sort_key` 排序。

        **只返回可路由的节点**（已批准 + alive）——未批准或已吊销的节点
        不该出现在候选里，那是"我能不能干"层面的基本前提。

        三个过滤条件，全部来自**节点自己的角色卡**：

        ① 声明了该能力
        ② 能提供的等级 ≥ ``req.min_level``
        ③ 接受来自 ``req.from_domain`` 的委托（``can_receive_from`` 通配匹配）
        """
        out: list[Candidate] = []
        for rec in self._registry.all_records():
            if not rec.routable:
                continue
            card = rec.card
            cap = card.capability(req.task_type)
            if cap is None:
                continue

            # 需求等级：调用方要不到比自己需要更低的东西没有意义，
            # 但候选必须**至少**能给到那么多
            provided = min(card.constraints.max_response_level, cap.level)
            if provided < req.min_level:
                continue

            # 节点自己声明的"谁可以给我派活"
            if req.from_domain and not _match_any(
                card.constraints.can_receive_from, req.from_domain
            ):
                continue

            out.append(Candidate(
                node_id=card.node_id,
                domain=card.domain,
                level=provided,
                load=self._load_of(card.node_id),
                # 排程提示：直接读 capacity（quota 已删，Step 5b）
                capacity=card.effective_capacity(),
                same_domain=bool(req.from_domain) and card.domain == req.from_domain,
            ))

        out.sort(key=lambda c: c.sort_key())
        return out

    def select(self, req: Requirement,
               accept: Callable[[Candidate], bool]) -> Candidate | None:
        """匹配 + 依次校验，返回**第一个通过校验的**候选。

        :param accept: 对单个候选做完整校验（域矩阵、配额、委托链……）的回调，
            通常就是策略层的 ``check_delivery``。返回 ``True`` 表示可以投。

        **为什么在这里做"依次尝试"而不是让调用方自己遍历**：
        "排序 + 依次尝试"是本模块的核心语义（确定性），把它拆开交给
        调用方，每个调用方都得重新实现一遍这个循环，而只要有一处写成
        随机挑一个，确定性就没了。

        **为什么用回调而不是直接依赖 PolicyEngine**：
        匹配器只需要"这个候选行不行"的答案，不需要知道答案是怎么算出来的。
        回调让本模块可以脱离策略层单独测试，依赖方向也保持干净
        （匹配器不知道策略存在，正如它不该知道矩阵存在）。

        :returns: 通过的候选；全都过不了则 ``None``。
            **注意：调用方对外必须把这个 ``None`` 与"没人有这个能力"
            报成同一个错误码**（见模块 docstring）。
        """
        for cand in self.candidates(req):
            if accept(cand):
                return cand
        return None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _load_of(self, node_id: str) -> int:
        """在途任务数。

        只数 ``working``（已被领走、正在执行）。``submitted``（排队中）
        不算负载——队列本来就是用来排队的，把排队中的任务算进来会让
        一个刚被投了几个任务的节点立刻显得很忙，反而把后续任务推向更远的节点。
        """
        return self._queue.count_tasks(node_id, status="working")


__all__ = ["Candidate", "Matcher", "Requirement"]
