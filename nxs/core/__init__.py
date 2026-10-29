"""核心契约层。

**零宿主依赖、零存储依赖** —— 只用标准库，可用内存实现跑完整测试。
本层不得出现任何 Agent 名称、域名、特定任务类型的特判；
一切节点相关的判断都必须来自 mgmt/config 加载的配置数据。
"""

from .envelope import ENVELOPE_VERSION, Envelope, canonical_bytes, envelope_for
from .errors import (
    AuthFailed,
    ConfigError,
    NexusError,
    PolicyDenied,
    RuleId,
    TaskExecutionError,
    safe_message,
)
from .models import (
    Artifact,
    Capability,
    Level,
    RoleCard,
    SelfLimits,
    Semantics,
    Task,
    TaskMode,
    TaskStatus,
)

__all__ = [
    "ENVELOPE_VERSION", "Artifact", "AuthFailed", "Capability", "ConfigError",
    "Envelope", "Level", "NexusError", "PolicyDenied",
    "RoleCard", "RuleId", "SelfLimits", "Semantics", "Task", "TaskExecutionError",
    "TaskMode", "TaskStatus", "canonical_bytes", "envelope_for", "safe_message",
]
