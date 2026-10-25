"""Port 抽象层 —— 接口定义，**不含任何实现**。

这层的价值是**可替换与可测试**：核心逻辑只依赖这里的接口，
因此可以用内存实现跑完整测试，也可以在不改核心代码的前提下换存储后端。

关于"可迁移"
------------
请注意一个区分：本层的意义**不是**"让系统可以搬到别的服务器"。
搬迁能力来自本系统是一个**独立服务**——各节点按配置重新连入即可。
而 Python 运行时、第三方库、数据库驱动属于**部署依赖**，是系统自身
实现方式带来的正常约束，不需要也无法清除。

依赖方向
--------
``backends/`` 依赖 ``ports/``，**反向依赖即设计错误**。
接口里不得出现任何具体数据库的类型或异常。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..core.models import RoleCard, Task


# ---------------------------------------------------------------------------
# 存活状态
# ---------------------------------------------------------------------------

class NodeState:
    """节点存活状态。

    ``STALE`` 与 ``REVOKED`` 的区别很重要：前者是"暂时联系不上"（可能只是
    心跳超时），后者是"已明确吊销"。**两者都不参与路由，但都不删除记录**——
    审计需要回答"当时它是否在线、是否被吊销"。
    """

    ALIVE = "alive"
    STALE = "stale"
    REVOKED = "revoked"

    ALL = (ALIVE, STALE, REVOKED)


@dataclass(frozen=True)
class NodeRecord:
    """注册表里的一条记录。"""

    card: RoleCard
    state: str = NodeState.ALIVE
    #: 最近一次心跳时间（ISO8601）
    last_seen: str = ""
    #: 卡片变更次数。历史版本不保留在注册表里，但版本号可用于排查"何时改的"
    revision: int = 1
    #: 是否已通过人工批准。**新节点默认 False，不能自行加入。**
    approved: bool = False

    @property
    def routable(self) -> bool:
        """是否可以接收任务。未批准 / 非 alive 都不可路由。"""
        return self.approved and self.state == NodeState.ALIVE


# ---------------------------------------------------------------------------
# 三个 Store 接口
# ---------------------------------------------------------------------------

class RegistryStore(ABC):
    """角色卡与存活状态的存储。"""

    @abstractmethod
    def put(self, record: NodeRecord) -> None:
        """写入或覆盖一条节点记录。"""

    @abstractmethod
    def get(self, node_id: str) -> NodeRecord | None:
        """按 ID 读取。不存在返回 None。"""

    @abstractmethod
    def list_all(self) -> Sequence[NodeRecord]:
        """列出全部记录（含 stale / revoked，用于审计与运维）。"""

    @abstractmethod
    def touch(self, node_id: str, *, last_seen: str) -> None:
        """更新心跳时间。节点不存在时静默忽略（不抛异常，避免心跳放大攻击面）。"""


class QueueStore(ABC):
    """任务队列的存储。**必须保证"取任务"是原子的。**

    原子性是硬要求：如果两个执行实例同时取到同一个任务，
    就会出现重复执行。对"代发消息"这类有副作用的操作，后果是重复投递。
    """

    @abstractmethod
    def enqueue(self, task: Task) -> None:
        """投递任务。同 id 已存在时行为由实现决定（调用方应先查幂等键）。"""

    @abstractmethod
    def claim(self, node_id: str, *, lease_seconds: int, now: str) -> Task | None:
        """取出一个待办任务并**原子地**标记为已被领取。

        返回 None 表示队列为空。领取后任务必须带上租约到期时间，
        超时未完成则任务应被释放回队列（见 :meth:`release_expired`）。
        """

    @abstractmethod
    def update(self, task: Task) -> None:
        """更新任务（状态、产物、重试次数等）。"""

    @abstractmethod
    def renew(self, task_id: str, *, lease_seconds: int, now: str) -> bool:
        """延长任务租约。**返回 False 表示该任务当前没有被持有**。

        长任务必须定期续租，而不是把租约时间调大——
        调大意味着"断电后要等更久才恢复"，那是把可靠性换成便利性。

        返回 ``False`` 有两种情形，实现**不需要区分**（也无法可靠区分）：
        任务已落终态、或租约已过期被释放。两种情况调用方的动作相同：
        **停止执行并放弃回传**，因为任务已经回到队列、
        可能已被别人领走，再回传就会产生重复。
        """

    @abstractmethod
    def get(self, task_id: str) -> Task | None:
        """按 ID 读取任务。"""

    @abstractmethod
    def release_expired(self, *, now: str) -> list[Task]:
        """把所有租约过期的任务释放回队列，返回被释放的任务。

        这是"断电恢复零人工干预"的关键：进程被 kill 后，它领取的任务
        会因租约超时而自动回到队列，无需任何人工介入。
        """

    @abstractmethod
    def list_by_session(self, session_id: str) -> Sequence[Task]:
        """列出某个会话下的全部任务，用于会话视图与审计聚合。"""

    @abstractmethod
    def find_by_idempotency(self, key: str) -> Task | None:
        """按幂等键查找。用于阻止重复提交。"""

    @abstractmethod
    def count_tasks(self, node_id: str, *, since: str | None = None,
                    status: str | None = None) -> int:
        """统计某节点名下的任务数。

        :param since: 只统计创建时间不早于该时刻的任务（ISO8601）。
            ``None`` 表示不限时间。
        :param status: 只统计该状态的任务。``None`` 表示全部状态。

        配额检查依赖它，所以**必须由后端真实实现**——
        返回一个估算值会让配额形同虚设，而且从外部完全看不出来。
        """


class AuditStore(ABC):
    """审计落盘。

    **只记元数据，不记业务内容。** 这不是实现细节，而是设计约束：
    审计日志如果包含业务内容，它自己就变成了数据集中点，
    而集中点正是本系统要避免的东西。
    """

    @abstractmethod
    def append(self, record: dict[str, Any]) -> None:
        """追加一条审计记录。

        **写入失败必须抛异常**，不要静默吞掉——调用方需要据此熔断。
        "不可追溯的协作"比"协作中断"更糟。
        """

    @abstractmethod
    def read_recent(self, limit: int = 100) -> Sequence[dict[str, Any]]:
        """读取最近的记录，供运维查询。"""

    @abstractmethod
    def summary(self) -> dict[str, Any]:
        """汇总统计：按命中规则分组的拒绝次数等。"""


__all__ = [
    "AuditStore",
    "NodeRecord",
    "NodeState",
    "QueueStore",
    "RegistryStore",
]
