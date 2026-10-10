"""Port 抽象层 —— 只定义接口，不含实现。

依赖方向：``backends/`` 依赖本层，**反向依赖即设计错误**。
"""

from .stores import (
    AuditStore,
    NodeRecord,
    NodeState,
    QueueStore,
    RegistryStore,
)

__all__ = [
    "AuditStore", "NodeRecord", "NodeState", "QueueStore", "RegistryStore",
]
