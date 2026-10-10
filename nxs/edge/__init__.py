"""接入层（Adapter）：认证与对外协议。

Core 不感知外部协议的存在——换传输方式只需替换本层。
"""

from .auth import DEFAULT_NONCE_CACHE_SIZE, Authenticator

__all__ = ["DEFAULT_NONCE_CACHE_SIZE", "Authenticator"]
