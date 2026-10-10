"""数据面：信箱、生命周期、应答边界。

负责"把任务送到、把状态记对、把产出管好"。
本层不做策略判定——判定在 ``control/policy.py``，本层只执行判定结果。
"""

from .boundary import ResponseBoundary
from .mailbox import DeliveryResult, Mailbox
from .task import DEFAULT_LEASE_SECONDS, DEFAULT_MAX_ATTEMPTS, TaskLifecycle

__all__ = [
    "DEFAULT_LEASE_SECONDS", "DEFAULT_MAX_ATTEMPTS", "DeliveryResult",
    "Mailbox", "ResponseBoundary", "TaskLifecycle",
]
