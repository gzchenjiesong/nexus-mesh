"""生成观察窗静态预览（不连服务，用假数据渲染）。

为什么需要这个：改前端后「代码看着对」不等于「渲染对」。
这个脚本把真实形状的数据注入页面，再用浏览器读回渲染结果 ——
比看截图更精确，也能捕获 JS 运行时错误。
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from nxs.ui.page import SHELL

# ⚠ 审计数组按**真实顺序**写：升序（最早在前）。
#   真实 read_recent 从文件尾部读，返回文件顺序 = 时间升序；
#   前端 reverse() 之后才是"最新在最上面"。
FAKE = {
    "error": None,
    "nodes": [
        {"node_id": "hermes-working", "display_name": "Hermes · working",
         "description": "云端 Hermes Agent（profile=working）",
         "domain": "cloud", "state": "alive", "approved": True,
         "role": "agent",
         "role_definition": {"can_issue": True, "serves": ["agent.ask"],
                             "max_response_level": "summary"},
         "capabilities": ["agent.ask"],
         "received": 3, "completed": 3, "failed": 0,
         "pending": 0, "in_flight": 0},
        {"node_id": "work-macbook", "display_name": "CodeBuddy · MacBook",
         "description": "本地开发机执行者（work-desktop 的调改替身）",
         "domain": "zone-work", "state": "alive", "approved": True,
         "role": "worker",
         "role_definition": {"can_issue": False, "serves": ["work.query"],
                             "max_response_level": "summary"},
         "capabilities": ["work.query"],
         "received": 9, "completed": 6, "failed": 3,
         "pending": 2, "in_flight": 1},
        {"node_id": "work-desktop", "display_name": "CodeBuddy · 公司电脑",
         "description": "公司内网执行者（尚未接入）",
         "domain": "zone-work", "state": "stale", "approved": False,
         "role": "worker",
         "role_definition": {"can_issue": False, "serves": ["work.query"],
                             "max_response_level": "summary"},
         "capabilities": ["work.query"],
         "received": 0, "completed": 0, "failed": 0,
         "pending": 0, "in_flight": 0},
    ],
    "metrics": {
        "uptime_seconds": 5327,
        "totals": {"submitted": 12, "completed": 9, "failed": 3, "denied": 2},
        "latency_ms": {"p50": 68476.4, "p95": 180821.0, "p99": 180821.0},
        "latency_samples": 9,
        "denied_by_rule": {"AU-APPROVE-05": 2},
        "by_type": {"work.query": {"submitted": 12, "completed": 9, "failed": 3}},
        "anomalies": [],
    },
    "audit": [
        {"ts": "2026-10-30T15:26:11+08:00", "outcome": "ops:approve",
         "from_node": "__ops__", "to_node": "work-macbook",
         "task_type": "", "duration_ms": 8, "bytes": 0, "rule_id": ""},
        {"ts": "2026-10-30T15:27:30+08:00", "outcome": "register:unchanged",
         "from_node": "work-macbook", "to_node": "work-macbook",
         "task_type": "", "duration_ms": 12, "bytes": 0, "rule_id": ""},
        {"ts": "2026-10-30T15:27:41+08:00", "outcome": "denied",
         "from_node": "work-macbook", "to_node": "coordinator",
         "task_type": "agent.ask", "duration_ms": 3, "bytes": 0,
         "rule_id": "AU-APPROVE-05"},
        {"ts": "2026-10-30T15:28:02+08:00", "outcome": "failed",
         "from_node": "hermes-working", "to_node": "work-macbook",
         "task_type": "work.query", "duration_ms": 140221, "bytes": 0,
         "rule_id": ""},
        {"ts": "2026-10-30T15:29:10+08:00", "outcome": "completed",
         "from_node": "hermes-working", "to_node": "work-macbook",
         "task_type": "work.query", "duration_ms": 68476, "bytes": 259,
         "rule_id": ""},
    ],
    "audit_truncated": False,
}

renders = (
    "renderTiles(FAKE.metrics);"
    "renderPending(FAKE.nodes);"
    "renderNodes(FAKE.nodes);"
    "renderDenials(FAKE.metrics);"
    "renderByType(FAKE.metrics);"
    "renderAudit(FAKE.audit, FAKE.audit_truncated);"
    "renderAnomalies(FAKE.metrics.anomalies);"
    "document.getElementById('stamp').textContent='预览';"
)

injected = SHELL.replace(
    "refresh();",
    "const FAKE = " + json.dumps(FAKE, ensure_ascii=False) + ";\n" + renders,
)
if injected == SHELL:      # pragma: no cover —— 只在页面结构变动时触发
    raise SystemExit("注入失败：SHELL 里找不到 'refresh();' 这一行")

out = pathlib.Path("/tmp/ui-preview.html")
out.write_text(injected, encoding="utf-8")
print(f"  已生成 {out}（{len(injected)} 字符）")
