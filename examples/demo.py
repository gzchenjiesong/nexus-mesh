"""九步验收 Demo —— 项目的可执行说明书。

``make demo`` 跑的就是这个文件。九步里最要紧的是 **4、5、6 步**：
它们验证的是这个项目**区别于普通消息队列**的地方。

    1  启动核心
    2  两个节点注册（并批准）
    3  正常任务：投递 → 领取 → 执行 → 回传 → 完成
    4  ★ 安全断言①：未注册的任务类型被拒
    5  ★ 安全断言②：越界字段被拒，且核心收不到任何内容
    6  ★ 安全断言③：跨信任域委托被拒
    7  ★ 断电恢复：节点被 kill 后任务自动回到队列

若 4、5、6 有任何一条不能稳定复现地通过，本项目就没有达成设计目标。

两种跑法
--------
* ``nxs demo``       —— 同进程传输（快，无网络依赖）
* ``nxs demo --net`` —— **同一套断言，走真实 HTTP**

``--net`` 不是另一份实现：九步代码完全共用，只有 ``transports`` 换了。
这正是"传输可替换"的证明——如果节点逻辑真的与传输解耦，
换个传输跑同一套断言应当不需要改任何一行流程代码。
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

def build(force_memory: bool = True, config_path: str | None = None) -> NexusApp:
    """用示例配置装配一个内存版核心。

    demo 用内存后端是刻意的——它让每次运行都从干净状态开始，
    且不需要清理临时文件。真实部署用 ``stores: sqlite``。

    :param config_path: 省略时用仓库自带的示例配置。
        demo 断言的笑是**示例拓扑**（coordinator / node-a / node-b），
        换配置只在拓扑一致时有意义。
    """
    cfg_path = Path(config_path) if config_path else (
        Path(__file__).resolve().parent.parent / "config" / "nxs.example.yaml"
    )
    # 协调节点的 ID 是 coordinator（术语表规定的叫法）；
    # 它的**域**叫 cloud —— 域是位置，节点是角色，两者刻意不同名。
    env = {
        f"NXS_SECRET_{m.upper().replace('-', '_')}":
            f"{m}-demo-secret-0123456789abcdef"
        for m in ("coordinator", "node-a", "node-b")
    }
    app = NexusApp.from_config_path(str(cfg_path), env=env, force_memory=force_memory)
    app.start()
    # 演示环境里自动批准两个节点（真实部署应显式 approve）
    for mid in ("node-a", "node-b"):
        app.approve(mid)
    return app


def clients(app: NexusApp, transports: dict[str, Any] | None = None) -> dict[str, NodeClient]:
    if transports is None:
        t = app.transport
        return {
            "node-a": NodeClient(NODE_A, t),
            "node-b": NodeClient(NODE_B, t),
        }
    return {
        "node-a": NodeClient(NODE_A, transports["node-a"]),
        "node-b": NodeClient(NODE_B, transports["node-b"]),
    }


def make_transports(app: NexusApp, *, net: bool) -> tuple[dict[str, Any], Any]:
    """按模式准备传输。

    同进程模式下三个节点共用同一个传输对象（本来就是同一个进程）；
    网络模式下各自是独立的 HTTP 客户端——**每个节点有自己的密钥**，
    这与真实部署一致。

    :return: (传输表, 网关或 None)
    """
    if not net:
        t = app.transport
        return {"coordinator": t, "node-a": t, "node-b": t}, None

    from nxs.edge.client import HttpTransport
    from nxs.edge.gateway import Gateway

    # 用系统分配的端口，避免 8443 被占用时 demo 演不出来
    from dataclasses import replace

    gateway = Gateway(app, server_config=replace(app.config.server, port=0)).start()
    transports = {
        node_id: HttpTransport(gateway.base_url, node_id=node_id, secret=secret)
        for node_id, secret in app.config.secrets.items()
    }
    return transports, gateway


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(net: bool = False, config_path: str | None = None) -> int:
    total = 9
    mode = "HTTP 传输（真实网络）" if net else "同进程传输"
    print(c("\n  Nexus Mesh · 九步验收 Demo", "b"))
    print(c(f"  传输：{mode}", "dim"))
    print(c("  " + "─" * 62, "dim"))

    app = build(config_path=config_path)
    transports, gateway = make_transports(app, net=net)
    coordinator = transports["coordinator"]
    cl = clients(app, transports)
    failures: list[str] = []
    try:
        failures = _run_steps(app, coordinator, transports, cl, net=net)
    finally:
        if gateway is not None:
            gateway.stop()

    # ---- 结论 ----
    print(c("\n  " + "─" * 62, "dim"))
    if failures:
        print(c(f"  {len(failures)} 项未通过：", "bad"))
        for f in failures:
            print(c(f"    · {f}", "bad"))
        return 1
    print(c("  九步全部通过 ✓", "ok"))
    print(c("  其中第 4、5、6 步是安全断言——它们才是这个项目的意义所在。", "dim"))
    return 0


def _run_steps(app: NexusApp, coordinator: Any, transports: dict[str, Any],
               cl: dict[str, NodeClient], *, net: bool) -> list[str]:
    """九步流程本体。**两种传输共用这一段**，不复制两份。"""
    total = 9
    failures: list[str] = []
    sess = "ses-demo-1"

    # ---- 1 ----
    step(1, total, "启动核心")
    ok(f"配置加载自 {Path(app.config.source_path).name}")
    backend = "memory"
    detail(f"节点 {len(app.config.nodes)} 个 · 矩阵规则 "
           f"{len(app.config.policy.rules)} 条 · 后端 {backend}")
    if net:
        detail(f"网关监听 {coordinator.base_url}（节点出站连入，网关不主动连节点）")

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
    d = coordinator.publish(from_node="coordinator", to_node="node-a",
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
        coordinator.publish(from_node="coordinator", to_node="node-a",
                    task_type="demo.unknown", params={}, session_id=sess)
        failures.append("第 4 步：未注册类型竟被接受")
        print(c("       ✗ 未注册类型竟被接受", "bad"))
    except PolicyDenied as e:
        denied(e.rule_id.value, "类型未注册，不进入任何执行逻辑")
        detail(f"（命中规则详情仅入本地审计：{e.detail}）")

    # ---- 5 ----
    step(5, total, "★ 安全断言②  越界字段（核心收不到内容）")
    # 构造一个"诚实但越界"的节点：它产出了白名单之外的字段
    t5 = coordinator.publish(from_node="coordinator", to_node="node-a",
                     task_type="demo.echo", params={"text": "x"},
                     session_id=sess, idempotency_key="idem-5",
                     ).task
    leaked_payload = {"echo": "x", "level": "metadata", "internal_note": "不该外传的内容"}
    try:
        # ★ 由 node-a 自己的传输回传。**不能让 coordinator 代它回传**——
        #   服务端只接受"任务收件人本人"的回传（S11）。
        #   同进程模式下三个节点共用一个传输对象，但传的 node_id 仍是 node-a，
        #   所以两种模式走的是同一条逻辑。
        transports["node-a"].complete("node-a", t5, [
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
        transports["node-a"].publish(from_node="node-a", to_node="coordinator",
                                     task_type="demo.synthesize", params={},
                                     session_id=sess)
        failures.append("第 6a 步：无发布权的节点竟能发布")
        print(c("       ✗ 无发布权的节点竟能发布", "bad"))
    except PolicyDenied as e:
        denied(e.rule_id.value, "node-a  roles.issuer=false → 角色层拦截")

    # 6b 矩阵层：节点有发布权，但跨信任域仍被拒
    try:
        transports["node-b"].publish(from_node="node-b", to_node="node-a",
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
        transports["node-a"].publish(from_node="node-a", to_node="node-b",
                                     task_type="demo.echo", params={"text": "x"},
                                     session_id=sess)
    except PolicyDenied as e:
        denied(e.rule_id.value, "node-a → node-b（反方向同样不可达）")

    # ---- 7 ----
    step(7, total, "★ 断电恢复：节点被 kill 后任务自动回到队列")
    t7 = coordinator.publish(from_node="coordinator", to_node="node-a",
                     task_type="demo.echo", params={"text": "recover"},
                     session_id=sess, idempotency_key="idem-7").task
    claimed = transports["node-a"].poll("node-a")
    ok(f"任务 {claimed.id} 被领取（attempt={claimed.attempt}）")
    detail("模拟节点进程被 kill：不回传、不报告、无任何信号")

    # 时间推进到租约之后（demo 里直接把租约时间设到过去）
    future = (datetime.now(timezone.utc)
              + timedelta(seconds=app.lifecycle.lease_seconds + 5)
              ).astimezone().isoformat(timespec="seconds")
    released = app.lifecycle.release_expired(now=future)
    ok(f"租约超时 → 自动释放 {len(released)} 个任务（零人工干预）")

    reclaimed = transports["node-a"].poll("node-a")
    if reclaimed is not None and reclaimed.id == claimed.id:
        ok(f"重启后自动接续  attempt={reclaimed.attempt}（重试次数累积）")
        cl["node-a"].run_once()
    else:
        failures.append("第 7 步：任务未能自动回到队列")
        print(c("       ✗ 任务未能自动回到队列", "bad"))

    # ---- 8 ----
    step(8, total, "★ 自动匹配：不指定目标，也能派出活且可复现")
    # 前七步都是 Direct 投递（写死 to_node）。这一步验证 M4：
    # 不指定目标时，系统按能力筛候选、确定性排序、逐个过策略。
    detail("候选名单（运维视角，不经 HTTP 暴露）：")
    rows = app.transport.candidates(task_type="demo.echo", from_node="coordinator")
    for r in rows:
        print(f"         {r['node_id']:<10} load={r['load']}/{r['capacity']} "
              f"level={r['level']} same_domain={r['same_domain']}")

    auto = coordinator.publish(from_node="coordinator", task_type="demo.echo",
                       params={"text": "auto"}, session_id=sess,
                       idempotency_key="idem-8")
    ok(f"自动选中 {auto.task.to_node}（未指定 to_node）")

    # ★ 确定性：同样的输入连算 10 次，顺序必须逐字相同。
    #   随机挑选会让"昨天能跑今天不能"变成不可复现的问题。
    runs = [
        [r["node_id"] for r in
         app.transport.candidates(task_type="demo.echo", from_node="coordinator")]
        for _ in range(10)
    ]
    if len({tuple(r) for r in runs}) == 1:
        ok(f"确定性：连算 10 次顺序一致 → {runs[0]}")
    else:
        failures.append("第 8 步：候选排序不确定")
        print(c("       ✗ 候选排序不确定（同一输入出现多种顺序）", "bad"))
    detail("排序依据：同域优先 → 等级刚好够用 → 负载率低 → 节点 ID 字典序")

    # ---- 9 ----
    step(9, total, "★ 可观测：单看都正常的指标，合起来才说明问题")
    from nxs.mgmt.observ import collect, detect_anomalies

    # 前八步已经产生了若干拒绝。先看指标本身
    snap = collect(app.metrics, registry=app.registry,
                   queue=app.stores.queue)
    payload = snap.as_dict()
    ok(f"提交 {payload['totals']['submitted']} · "
       f"完成 {payload['totals']['completed']} · "
       f"拒绝 {payload['totals']['denied']}")
    if payload["denied_by_rule"]:
        detail("拒绝分布：" + ", ".join(
            f"{k}×{v}" for k, v in sorted(payload["denied_by_rule"].items())))

    anomalies = detect_anomalies(snap)
    detail("异常规则只报「组合异常」——单指标超阈值不算，那种告警会响到没人看")
    if anomalies:
        for a in anomalies:
            print(f"         {c('●', 'warn')} [{a.severity}] {a.rule} · {a.subject}")
            print(f"           {a.message}")
            detail(f"为什么单看正常：{a.rationale[:50]}…")
    else:
        ok("当前无组合异常（健康）")

    # ★ 演示：制造一次"权限试探"，看它是否被识别
    #   单撞一条规则是配错（良性），撞多种互不相关的规则才像在摸边界。
    #
    #   三笔**全部由 coordinator 发出**——这是刻意的：规则按节点归集，
    #   分散到不同节点上就凑不出"同一个节点撞多种规则"这个形态，
    #   而那正是要被识别的信号。
    detail("现在制造一次试探：让 coordinator 依次撞 3 种不同的规则")
    probes = [
        ("未知类型", lambda: coordinator.publish(
            from_node="coordinator", to_node="node-a", task_type="demo.ghost",
            params={}, session_id=sess)),
        ("未知节点", lambda: coordinator.publish(
            from_node="coordinator", to_node="ghost-node", task_type="demo.echo",
            params={}, session_id=sess)),
        ("矩阵不允许", lambda: coordinator.publish(
            # 矩阵里**没有** coordinator → coordinator 这条规则（示例配置只配了
            # coordinator→zone-a、coordinator→zone-b、zone-b→coordinator）。协调节点把活派给自己
            # 这件事本身没有意义，所以没有规则——正是"默认拒绝"的体现。
            from_node="coordinator", to_node="coordinator", task_type="demo.synthesize",
            params={}, session_id=sess)),
    ]
    hit: list[str] = []
    for label, fn in probes:
        try:
            fn()
            detail(f"{label} → （竟然成功）")
        except PolicyDenied as e:
            hit.append(e.rule_id.value)
            detail(f"{label} → {e.rule_id.value}")

    after = detect_anomalies(
        collect(app.metrics, registry=app.registry, queue=app.stores.queue)
    )
    probed = [a for a in after if a.rule == "probe_pattern"]
    if probed:
        ok(f"识别为权限试探：{probed[0].message}")
        detail("★ 单看每一次拒绝都正常（默认拒绝必然拒），"
               "但撞到多种不同规则，更像在逐条试探边界")
        detail(f"建议：{probed[0].suggestion[:60]}…")
    else:
        # 不算失败，但要说清为什么没触发——否则读者不知道是漏了还是没到条件
        detail(f"未触发试探告警（已命中 {len(set(hit))} 种规则："
               f"{', '.join(sorted(set(hit)))}）")
        detail("规则要求**同一节点**撞到 3 种以上互不相关的规则")

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
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
