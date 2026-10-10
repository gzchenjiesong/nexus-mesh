"""管理面：配置中心与审计留痕。

两者都**横切**各层：配置决定流转规则，审计记录流转事实。
"""

from .audit import AuditError, FileAuditStore, make_record
from .config import (
    DEFAULT_CONFIG_PATH,
    ENV_SECRET_PREFIX,
    EXAMPLE_CONFIG_PATH,
    UNIMPLEMENTED,
    MatrixRule,
    NexusConfig,
    PolicyConfig,
    ServerConfig,
    StoreConfig,
    load_config,
)

__all__ = [
    "DEFAULT_CONFIG_PATH", "ENV_SECRET_PREFIX", "EXAMPLE_CONFIG_PATH",
    "UNIMPLEMENTED", "AuditError", "FileAuditStore",
    "MatrixRule", "NexusConfig", "PolicyConfig", "ServerConfig", "StoreConfig",
    "load_config", "make_record",
]
