"""示例节点 —— 中性 demo，不含任何真实场景。

两个节点刻意设计成不同的信任档位，用来演示"能力很强 ≠ 权限很大"：

* ``node_a``：应答上限 **摘要级**（`summary`），最严约束的模拟
* ``node_b``：应答上限 **片段级**（`fragment`），且有一个**自由文本**字段
  （用于演示自由文本的清洗与标注）

这些名字（node-a / node-b / demo.*）都是**通用占位**。
生产环境的节点名单与任务类型应放在 ``config/local.yaml``（已 gitignore）。
"""

from __future__ import annotations

import time

from nxs.core.models import Level
from nxs.sdk import NodeConfig


# ---------------------------------------------------------------------------
# node-a 的处理器：模拟"最严约束"的一侧
# ---------------------------------------------------------------------------

def _echo(params):
    """原样回显。用于验证链路是否通。"""
    return {"echo": str(params.get("text", "")), "level": "metadata"}


def _count(params):
    """统计输入条数。演示"应答只含白名单字段"。"""
    raw = params.get("items", "")
    items = [x for x in str(raw).split(",") if x.strip()]
    return {"count": len(items), "level": "summary"}


NODE_A = NodeConfig(
    node_id="node-a",
    handlers={
        "demo.echo": _echo,
        "demo.count": _count,
    },
    levels={
        # 必须与 config 里声明的 level 一致——声明与实现不符会被边界拒绝
        "demo.echo": Level.METADATA,
        "demo.count": Level.SUMMARY,
    },
    poll_interval=0.05,
)


# ---------------------------------------------------------------------------
# node-b 的处理器：模拟约束较宽松、且有自由文本产出的一侧
# ---------------------------------------------------------------------------

def _search(params):
    """检索并返回结果 + 一段自然语言摘要。

    **``freetext_summary`` 是自由文本字段**——它是最容易承载
    提示注入的内容形态，因此边界会对它做清洗并在来源标注里
    打上 ``contains_freetext``，让接收方知道这段不可原样信任。
    """
    query = str(params.get("query", ""))
    return {
        "results": [f"hit-1 for {query}", f"hit-2 for {query}"],
        "freetext_summary": f"找到 2 条与「{query}」相关的结果。",
        "level": "fragment",
    }


def _slow(params):
    """耗时任务。用于验证租约续期与断电恢复。"""
    seconds = float(params.get("seconds", 1))
    time.sleep(seconds)
    return {"slept": seconds, "level": "summary"}


NODE_B = NodeConfig(
    node_id="node-b",
    handlers={
        "demo.echo": _echo,
        "demo.search": _search,
        "demo.slow": _slow,
    },
    levels={
        "demo.echo": Level.METADATA,
        "demo.search": Level.FRAGMENT,
        "demo.slow": Level.SUMMARY,
    },
    poll_interval=0.05,
)


#: 供 demo 与测试统一取用
ALL_NODES = (NODE_A, NODE_B)


def by_id(node_id: str) -> NodeConfig:
    for m in ALL_NODES:
        if m.node_id == node_id:
            return m
    raise KeyError(node_id)


__all__ = ["ALL_NODES", "NODE_A", "NODE_B", "by_id"]
