"""控制面：注册中心与策略执行点。

负责回答两个问题：**"这件事该由谁做"** 与 **"这次操作允许吗"**。
本层不含任何执行逻辑——它只做判定，不做事。
"""

from .policy import (
    MAX_FREETEXT_BYTES,
    DeliveryDecision,
    PolicyEngine,
    make_task,
)
from .registry import Registry

__all__ = [
    "MAX_FREETEXT_BYTES", "DeliveryDecision", "PolicyEngine", "Registry",
    "make_task",
]
