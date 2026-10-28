"""Nexus Mesh — a self-hosted coordination network for heterogeneous AI agents.

分层约定（依赖方向严格单向，反向依赖即设计错误）::

    Adapter(edge)  →  Core(core/control/data)  →  Port(ports)  ←  Backend(backends)

* ``core``      契约对象与错误码，零依赖零副作用
* ``edge``      对外协议与认证，Core 不感知外部协议
* ``control``   注册与策略，决定"怎么走"
* ``data``      信箱、生命周期、应答边界，负责"走"
* ``mgmt``      配置与审计，横切以上各层
* ``ports``     接口定义，不含实现
* ``backends``  接口实现，可替换
"""

__version__ = "0.19.0"
__all__ = ["__version__"]
