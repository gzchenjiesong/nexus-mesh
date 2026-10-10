"""追溯体系。

本包提供**可执行的编号追溯**——追溯表是"跑出来"的，不是"抄出来"的。

* :mod:`traceability.registry` —— 编号清单（唯一真相源，纯数据无依赖）
* ``MATRIX.md`` —— 由 ``tests/test_traceability.py`` 自动生成的追溯矩阵

用法::

    from traceability import registry
    print(registry.summary())
    print(registry.items_of_module("M5"))

为什么把清单写成代码
--------------------
文档里的映射表会腐烂：改了代码没人记得回来改文档，而漏改不报错。
**把清单写成代码，再让测试去校验它**，才能保证它始终与实现一致。

这与 ``CONTRIBUTING.md`` 的 C7（配置字段必须被使用）是同一个道理：
凡是被声明出来的东西，都要有地方校验它。
"""

from __future__ import annotations

from . import registry

__all__ = ["registry"]
