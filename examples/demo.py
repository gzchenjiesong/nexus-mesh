"""七步验收 Demo —— 项目的可执行说明书。

``make demo`` 跑的就是这个文件。七步里最要紧的是 **4、5、6 步**：
它们验证的是这个项目**区别于普通消息队列**的地方。

    1  启动核心
    2  两个节点注册（并批准）
    3  正常任务：投递 → 领取 → 执行 → 回传 → 完成
    4  ★ 安全断言①：未注册的任务类型被拒
    5  ★ 安全断言②：越界字段被拒，且核心收不到任何内容
    6  ★ 安全断言③：跨信任域委托被拒
    7  ★ 断电恢复：节点被 kill 后任务自动回到队列

若 4、5、6 有任何一条不能稳定复现地通过，本项目就没有达成设计目标。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nxs.app import NexusApp
from nxs.core.errors import PolicyDenied
from nxs.core.models import Artifact, Level, Semantics
from nxs.sdk import NodeClient

# 允许直接 `python examples/demo.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from examples.nodes import NODE_A, NODE_B  # noqa: E402

# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------

_C = {
    "ok": "\033[32m", "bad": "\033[31m", "warn": "\033[33m",
    "dim": "\033[90m", "b": "\033[1m", "off": "\033[0m",
}


def _tty() -> bool:
    return sys.stdout.isatty()


def c(text: str, color: str) -> str:
    return f"{_C[color]}{text}{_C['off']}" if _tty() else text


def step(n: int, total: int, title: str) -> None:
    print(f"\n{c(f'[{n}/{total}]', 'b')} {title}")


def ok(msg: str) -> None:
    print(f"       {c('OK', 'ok')}  {msg}")


def denied(rule_id: str, msg: str) -> None:
    print(f"       {c('REJECTED', 'bad')}  {c(rule_id, 'warn')}  {c(msg, 'dim')}")


def detail(msg: str) -> None:
    print(f"       {c(msg, 'dim')}")


# ---------------------------------------------------------------------------
# 核心装配
# ---------------------------------------------------------------------------

def build(force_memory: bool = True) -> NexusApp:
    """用示例配置装配一个内存版核心。

    demo 用内存后端是刻意的——它让每次运行都从干净状态开始，
    且不需要清理临时文件。真实部署用 ``stores: sqlite``。
    """
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "nxs.example.yaml"
    env = {
        f"NXS_SECRET_{m.upper().replace('-', '_')}":
            f"{m}-demo-secret-0123456789abcdef"
        for m in ("hub", "node-a", "node-b")
    }
    app = NexusApp.from_config_path(str(cfg_path), env=env, force_memory=force_memory)
    app.start()
    # 演示环境里自动批准两个节点（真实部署应显式 approve）
    for mid in ("node-a", "node-b"):
        app.approve(mid)
    return app


def clients(app: NexusApp) -> dict[str, NodeClient]:
    t = app.transport
    return {
        "node-a": NodeClient(NODE_A, t),
        "node-b": NodeClient(NODE_B, t),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    total = 7
    print(c("\n  Nexus Mesh · 七步验收 Demo", "b"))
    print(c("  " + "─" * 62, "dim"))

    app = build()
    cl = clients(app)
    failures: list[str] = []

    # ---- 1 ----
    step(1, total, "启动核心")
    ok(f"配置加载自 {Path(app.config.source_path).name}")
    detail(f"节点 {len(app.config.nodes)} 个 · 矩阵规则 "
           f"{len(app.config.policy.rules)} 条 · 后端 memory")

    # ---- 2 ----
    step(2, total, "节点注册与批准")
    for mid in ("node-a", "node-b"):
        rec = app.registry.record(mid)
        card = rec.card
        caps = ", ".join(f"{cp.type}({cp.level})" for cp in card.capabilities)
        roles = "executor" + (", issuer" if card.roles.issuer else "")
        ok(f"{mid:<10} domain={card.domain:<7} roles={roles:<16} "
           f"max_level={card.constraints.max_response_level}")
        detail(f"{'':10} {caps}")

    # ---- 3 ----
    step(3, total, "正常任务：投递 → 领取 → 执行 → 回传")
    sess = "ses-demo-1"
    d = app.transport.publish(from_node="hub", to_node="node-a",
                              task_type="demo.echo", params={"text": "hello"},
                              session_id=sess, idempotency_key="idem-1")
    ok(f"投递成功  task={d.task.id}  实际上限={d.effective_max_level}")
    task = cl["node-a"].run_once()
    if task and task.status == "completed":
        ok(f"执行完成  status={task.status}  payload={task.artifact_payload()}")
    else:
        failures.append("第 3 步：正常任务未完成")
        print(c("       ✗ 正常任务未完成", "bad"))

    # ---- 4 ----
    step(4, total, "★ 安全断言①  未注册的任务类型")
    try:
        app.transport.publish(from_node="hub", to_node="node-a",
                              task_type="demo.unknown", params={}, session_id=sess)
        failures.append("第 4 步：未注册类型竟被接受")
        print(c("       ✗ 未注册类型竟被接受", "bad"))
    except PolicyDenied as e:
        denied(e.rule_id.value, "类型未注册，不进入任何执行逻辑")
        detail(f"（命中规则详情仅入本地审计：{e.detail}）")

    # ---- 5 ----
    step(5, total, "★ 安全断言②  越界字段（核心收不到内容）")
    # 构造一个"诚实但越界"的节点：它产出了白名单之外的字段
    from examples.nodes import NODE_A as _A
    honest = NodeClient(_A, app.transport)
    t5 = app.transport.publish(from_node="hub", to_node="node-a",
                               task_type="demo.echo", params={"text": "x"},
                               session_id=sess, idempotency_key="idem-5",
                               ).task
    leaked_payload = {"echo": "x", "level": "metadata", "internal_note": "不该外传的内容"}
    try:
        app.transport.complete("node-a", t5, [
            Artifact(level=Level.METADATA, kind="structured", data=leaked_payload)
        ])
        failures.append("第 5 步：越界字段竟被放行")
        print(c("       ✗ 越界字段竟被放行", "bad"))
    except PolicyDenied as e:
        wire = e.to_wire()
        denied(e.rule_id.value, f"对外响应 = {wire}")
        assert "internal_note" not in str(wire), "泄露了字段名！"
        detail("对外响应不含字段名、不含内容——详情仅入本地审计")

    # ---- 6 ----
    step(6, total, "★ 安全断言③  控制是逐层收窄的（角色 → 白名单 → 矩阵）")

    # 6a 角色层：最严节点连发布权都没有
    try:
        app.transport.publish(from_node="node-a", to_node="hub",
                              task_type="demo.synthesize", params={}, session_id=sess)
        failures.append("第 6a 步：无发布权的节点竟能发布")
        print(c("       ✗ 无发布权的节点竟能发布", "bad"))
    except PolicyDenied as e:
        denied(e.rule_id.value, "node-a  roles.issuer=false → 角色层拦截")

    # 6b 矩阵层：节点有发布权，但跨信任域仍被拒
    try:
        app.transport.publish(from_node="node-b", to_node="node-a",
                              task_type="demo.echo", params={"text": "x"},
                              session_id=sess)
        failures.append("第 6b 步：跨信任域委托竟被允许")
        print(c("       ✗ 跨信任域委托竟被允许", "bad"))
    except PolicyDenied as e:
        denied(e.rule_id.value,
               "node-b → node-a  有发布权，但矩阵无 zone-b→zone-a 规则")
        detail("★ 角色层放行、矩阵层拦截——两层互相独立，这是关键性质")

    # 6c 反向同理
    try:
        app.transport.publish(from_node="node-a", to_node="node-b",
                              task_type="demo.echo", params={"text": "x"},
                              session_id=sess)
    except PolicyDenied as e:
        denied(e.rule_id.value, "node-a → node-b（反方向同样不可达）")

    # ---- 7 ----
    step(7, total, "★ 断电恢复：节点被 kill 后任务自动回到队列")
    t7 = app.transport.publish(from_node="hub", to_node="node-a",
                               task_type="demo.echo", params={"text": "recover"},
                               session_id=sess, idempotency_key="idem-7").task
    claimed = app.mailbox.poll("node-a")
    ok(f"任务 {claimed.id} 被领取（attempt={claimed.attempt}）")
    detail("模拟节点进程被 kill：不回传、不报告、无任何信号")

    # 时间推进到租约之后（demo 里直接把租约时间设到过去）
    future = (datetime.now(timezone.utc)
              + timedelta(seconds=app.lifecycle.lease_seconds + 5)
              ).astimezone().isoformat(timespec="seconds")
    released = app.lifecycle.release_expired(now=future)
    ok(f"租约超时 → 自动释放 {len(released)} 个任务（零人工干预）")

    reclaimed = app.mailbox.poll("node-a")
    if reclaimed is not None and reclaimed.id == claimed.id:
        ok(f"重启后自动接续  attempt={reclaimed.attempt}（重试次数累积）")
        cl["node-a"].run_once()
    else:
        failures.append("第 7 步：任务未能自动回到队列")
        print(c("       ✗ 任务未能自动回到队列", "bad"))

    # ---- 审计摘要 ----
    print(c("\n  审计摘要", "b"))
    s = app.audit_summary()
    print(f"       总记录 {s['total']} · 完成 {s['completed']} · 拒绝 {s['denied']}")
    if s.get("denied_by_rule"):
        for rule, cnt in s["denied_by_rule"].items():
            print(f"       {c('·', 'dim')} {rule:<16} × {cnt}")

    view = app.lifecycle.session_view(sess)
    print(f"       会话 {sess}: {view['total']} 个任务 "
          f"（{view['by_status']}）")
    detail("审计只记元数据，不含任务参数与产出内容")

    # ---- 结论 ----
    print(c("\n  " + "─" * 62, "dim"))
    if failures:
        print(c(f"  {len(failures)} 项未通过：", "bad"))
        for f in failures:
            print(c(f"    · {f}", "bad"))
        return 1
    print(c("  七步全部通过 ✓", "ok"))
    print(c("  其中第 4、5、6 步是安全断言——它们才是这个项目的意义所在。", "dim"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
