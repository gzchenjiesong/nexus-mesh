"""M15 · 观察窗 WebUI。

只读的系统观察进程，靠 SSH 隧道访问。设计见
``docs/design/05-webui-design.md``。

对外只暴露三样：:class:`UiServer`、:func:`serve_ui`、:data:`ALLOWED_HOSTS`。
"""

from .server import ALLOWED_HOSTS, UiServer, serve_ui

__all__ = ["ALLOWED_HOSTS", "UiServer", "serve_ui"]
