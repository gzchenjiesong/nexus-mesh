"""渲染观察窗的三态，验证前端行为。

改前端后「代码看着对」不等于「渲染对」。这个脚本把三种状态的
`/api/state` 数据注入页面，输出静态 HTML，供浏览器读回渲染结果。

用法：
    python3 deploy/preview-ui-state.py            # 生成三种状态
    python3 deploy/preview-ui-state.py stale      # 只生成某一种

⚠ 为什么三态要分别验：它们的视觉差异（横幅、透明度、虚线边框）
是「陈旧数据不与实时混排」这条约束的落实处。混在一起看会漏掉。
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from nxs.ui.page import SHELL
from nxs.ui.server import UiServer

#: ``/nxs/ops/nodes`` 返回的**身份/授权**字段 —— 只有这些。
#:
#: ⚠ 第一版这里把手写的性能字段（received/backlog_ratio/bytes_out）
#: 直接混进了节点 dict，于是预览永远是绿的，而生产上那几列全是「—」。
#: 预览验证的又是假数据 —— 同一个坑的第二次。
#:
#: 现在严格分开：这份**只**放 ops/nodes 真实有的字段，
#: 性能字段进 PERF_NODES，最后由服务端那个函数合并。
OPS_NODES = [
    {"node_id": "hermes-working", "display_name": "Hermes · working",
     "description": "云端 Hermes Agent（profile=working）",
     "domain": "cloud", "state": "alive", "approved": True,
     "revision": 3, "last_seen": "2026-11-13T17:09:41+08:00",
     "role": "agent",
     "role_definition": {"can_issue": True, "serves": ["agent.ask"],
                         "max_response_level": "summary"},
     "may_broadcast": False,
     "capabilities": ["agent.ask"],
     "max_response_level": "summary",
     "self_limits": {"max_concurrent": 2}},
    {"node_id": "work-macbook", "display_name": "CodeBuddy · MacBook",
     "description": "本地开发机执行者",
     "domain": "zone-work", "state": "alive", "approved": True,
     "revision": 1, "last_seen": "2026-11-13T17:09:38+08:00",
     "role": "worker",
     "role_definition": {"can_issue": False, "serves": ["work.query"],
                         "max_response_level": "summary"},
     "may_broadcast": False,
     "capabilities": ["work.query"],
     "max_response_level": "summary",
     "self_limits": {"max_concurrent": 4}},
    {"node_id": "work-desktop", "display_name": "CodeBuddy · 公司电脑",
     "description": "内网执行者（capacity=0 的边缘情形）",
     "domain": "zone-work", "state": "stale", "approved": False,
     "revision": 1, "last_seen": "2026-11-12T09:00:00+08:00",
     "role": "worker",
     "role_definition": {"can_issue": False, "serves": ["work.query"],
                         "max_response_level": "summary"},
     "may_broadcast": False,
     "capabilities": ["work.query"],
     "max_response_level": "summary",
     "self_limits": {}},
]

#: ``metrics.nodes[]`` 返回的**性能**字段 —— 与上面**不重叠**。
#:
#: ⚠ `work-desktop` 的 backlog_ratio 是 None：capacity=0 除零，
#:   前端必须显示 ∞，不能显示 0 或空白（那会被读成"没有积压"）。
PERF_NODES = [
    {"node_id": "hermes-working", "domain": "cloud", "approved": True,
     "state": "alive", "received": 3, "completed": 3, "failed": 0,
     "in_flight": 0, "pending": 0, "capacity": 2,
     "backlog_ratio": 0.0, "bytes_out": 0, "failure_rate": 0.0,
     "denials": {}},
    {"node_id": "work-macbook", "domain": "zone-work", "approved": True,
     "state": "alive", "received": 9, "completed": 6, "failed": 3,
     "in_flight": 1, "pending": 2, "capacity": 4,
     "backlog_ratio": 0.5, "bytes_out": 18432, "failure_rate": 0.333,
     "denials": {"MX-DOMAIN-02": 1}},
    {"node_id": "work-desktop", "domain": "zone-work", "approved": False,
     "state": "stale", "received": 0, "completed": 0, "failed": 0,
     "in_flight": 0, "pending": 3, "capacity": 0,
     "backlog_ratio": None, "bytes_out": 0, "failure_rate": 0.0,
     "denials": {}},
]

#: 预览里展示的节点表数据 —— **由真实合并函数产出**，不手写。
NODES = UiServer._merge_node_metrics(OPS_NODES, {"nodes": PERF_NODES})

METRICS = {
    "uptime_seconds": 5327,
    "totals": {"submitted": 12, "completed": 9, "failed": 3, "denied": 2},
    "latency_ms": {"p50": 68476.4, "p95": 180821.0, "p99": 180821.0},
    "latency_samples": 9,
    "denied_by_rule": {"AU-APPROVE-05": 2},
    "by_type": {"work.query": {"submitted": 12, "completed": 9, "failed": 3}},
    # 真实 metrics 一定含 nodes[]；节点表的性能列就来自这里
    "nodes": PERF_NODES,
    "anomalies": [],
}

AUDIT = [
    {"ts": "2026-11-13T15:29:10+08:00", "outcome": "completed",
     "from_node": "hermes-working", "to_node": "work-macbook",
     "task_type": "work.query", "duration_ms": 68476, "bytes": 259,
     "rule_id": ""},
]

def _probe_like_crash() -> dict:
    """用**真实的** probe_coordinator 生成「卡死」状态，而不是手写一份。

    ⚠ 这里第一版是手写的固定 dict，结果改 probe.py 的文案时预览没跟着变 ——
    预览验证的是假数据，不是真实现。那种"预览绿了、真实输出错"的漏洞
    正是本项目反复踩过的（见「界面在说反话」那三次）。

    做法：临时顶掉 probe 模块的 `_run` / `_read_text`，让它以为自己在
    一台真卡死的机器上读 /proc。
    """
    from nxs.ui import probe as P

    real_run, real_read = P._run, P._read_text
    real_is_dir = P.Path.is_dir

    def fake_run(cmd):
        if "is-active" in cmd:
            return "active"
        if "MainPID" in cmd:
            return "4121514"
        # ss -tln：Recv-Q=6，就是事故现场那个数字
        return ("State  Recv-Q Send-Q Local Address:Port  Peer Address:Port\n"
                "LISTEN 6      5      0.0.0.0:18800       0.0.0.0:*")

    def fake_read(path):
        if path.name == "status":
            return "State:\tS (sleeping)\nThreads:\t2\nVmRSS:\t   28904 kB"
        if path.name == "wchan":
            return "futex_wait_queue"
        return None

    try:
        P._run = fake_run
        P._read_text = fake_read
        P.Path.is_dir = lambda self: True
        return P.probe_coordinator(ops_reachable=False, unit="nexus-mesh",
                                   port=18800)
    finally:
        P._run, P._read_text = real_run, real_read
        P.Path.is_dir = real_is_dir


def _probe_healthy() -> dict:
    """用真实的 probe_coordinator 生成「健康」状态。"""
    from nxs.ui import probe as P

    real_run, real_read = P._run, P._read_text
    real_is_dir = P.Path.is_dir

    def fake_run(cmd):
        if "is-active" in cmd:
            return "active"
        if "MainPID" in cmd:
            return "1051336"
        return ("State  Recv-Q Send-Q Local Address:Port  Peer Address:Port\n"
                "LISTEN 0      5      0.0.0.0:18800       0.0.0.0:*")

    def fake_read(path):
        if path.name == "status":
            return "State:\tS (sleeping)\nThreads:\t8\nVmRSS:\t   29488 kB"
        if path.name == "wchan":
            return "do_epoll_wait"
        return None

    try:
        P._run = fake_run
        P._read_text = fake_read
        P.Path.is_dir = lambda self: True
        return P.probe_coordinator(ops_reachable=True, unit="nexus-mesh",
                                   port=18800)
    finally:
        P._run, P._read_text, = real_run, real_read
        P.Path.is_dir = real_is_dir


STATES: dict[str, dict] = {
    # ① 实时：一切正常
    "live": {
        "at": "2026-11-13T17:10:00+08:00",
        "coordinator_url": "https://127.0.0.1:18800/nxs",
        "nodes": NODES, "metrics": METRICS, "audit": AUDIT,
        "audit_truncated": False, "audit_heartbeats": 997,
        "error": None,
        "stale": False, "stale_since": None, "stale_age_seconds": None,
        "no_history": False,
        "probe": _probe_healthy(),
    },
    # ② 陈旧：coordinator 挂了，用内存末次成功兜底
    "stale": {
        "at": "2026-11-13T17:10:00+08:00",
        "coordinator_url": "https://127.0.0.1:18800/nxs",
        "nodes": NODES, "metrics": METRICS, "audit": AUDIT,
        "audit_truncated": False, "audit_heartbeats": 997,
        "error": "URLError: <urlopen error timed out>",
        "stale": True,
        "stale_since": "2026-11-13T16:52:00+08:00",
        "stale_age_seconds": 1080.0,
        "no_history": False,
        "probe": _probe_like_crash(),
    },
    # ④ 稀疏：样本不足（不画分位数）+ 有组合异常（验 rationale/suggestion）
    "sparse": {
        "at": "2026-11-13T17:10:00+08:00",
        "coordinator_url": "https://127.0.0.1:18800/nxs",
        "nodes": NODES,
        "metrics": {
            "uptime_seconds": 120,
            "totals": {"submitted": 1, "completed": 1, "failed": 0, "denied": 0},
            # 只有 1 个样本 → 必须不显示分位数
            "latency_ms": {"p50": 42.0, "p95": 42.0, "p99": 42.0},
            "latency_samples": 1,
            "denied_by_rule": {},
            "by_type": {"work.query": {"submitted": 1, "completed": 1,
                                       "failed": 0}},
            # ⚠ 真实的 metrics 一定带 nodes[]（as_dict 无条件给）。
            #   这里也必须给 —— 否则节点表的性能列会显示「—」，
            #   预览就成了在验一个生产上不存在的状态。
            "nodes": PERF_NODES,
            "anomalies": [{
                "severity": "warn",
                "rule": "probe_pattern",
                "subject": "work-macbook",
                "message": "同一节点在 60 秒内产生 5 次不同类型拒绝",
                "rationale": (
                    "单看每一项都正常：5 次拒绝分散在不同规则上，"
                    "没有任何单规则超过阈值。但同一个节点短时间内"
                    "试遍多个类型，是权限试探的形态。"
                ),
                "suggestion": (
                    "确认该节点是否在调试；若不是，"
                    "检查它的凭据是否泄露。"
                ),
                "evidence": {"window_seconds": 60, "distinct_types": 5},
            }],
        },
        "audit": AUDIT,
        "audit_truncated": False, "audit_heartbeats": 12,
        "error": None,
        "stale": False, "stale_since": None, "stale_age_seconds": None,
        "no_history": False,
        "probe": _probe_healthy(),
    },
    # ③ 无历史：面板刚启动就遇上不可达
    "no_history": {
        "at": "2026-11-13T17:10:00+08:00",
        "coordinator_url": "https://127.0.0.1:18800/nxs",
        "nodes": [], "metrics": None, "audit": [],
        "audit_truncated": False, "audit_heartbeats": 0,
        "error": "URLError: <urlopen error timed out>",
        "stale": False, "stale_since": None, "stale_age_seconds": None,
        "no_history": True,
        "probe": _probe_like_crash(),
    },
}

RENDERS = (
    "renderStale(REAL);"
    "renderProbe(REAL.probe);"
    "renderTiles(REAL.metrics);"
    "renderPending(REAL.nodes);"
    "renderNodes(REAL.nodes);"
    "renderDenials(REAL.metrics);"
    "renderByType(REAL.metrics);"
    "renderAudit(REAL.audit, REAL.audit_truncated, REAL.audit_heartbeats);"
    "renderAnomalies(REAL.metrics ? REAL.metrics.anomalies : []);"
    "document.getElementById('stamp').textContent='预览';"
)


def build(state_name: str) -> pathlib.Path:
    data = STATES[state_name]
    injected = SHELL.replace(
        "refresh();",
        "const REAL = " + json.dumps(data, ensure_ascii=False) + ";\n" + RENDERS,
    )
    if injected == SHELL:                  # pragma: no cover
        raise SystemExit("注入失败：SHELL 里找不到 'refresh();'")
    out = pathlib.Path(f"/tmp/ui-state-{state_name}.html")
    out.write_text(injected, encoding="utf-8")
    return out


def main() -> int:
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    names = [want] if want else list(STATES)
    for name in names:
        if name not in STATES:
            print(f"  未知状态 {name!r}；可选：{list(STATES)}")
            return 2
        path = build(name)
        print(f"  {name:<12} → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
