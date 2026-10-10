"""存储后端实现 —— 可替换。

当前提供 ``memory``（测试与 demo）与 ``sqlite``（持久化）。
新增后端只需实现 ``ports/`` 里的接口，核心代码一行不改。
"""

from . import memory

__all__ = ["memory"]
