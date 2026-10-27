"""命令行入口。

四个子命令：

    nxs verify  配置自检：加载配置、校验合法性、打印生效的规则
    nxs demo    跑九步验收 demo（同 ``make demo``）
    nxs serve   核心常驻：启动 HTTP 网关 + 定期维护（sweep）
    nxs status  查看当前节点状态与审计摘要

关于 HTTP 端点
--------------
v0.2 起 **``serve`` 会真的监听端口**（``nxs/edge/gateway.py``）。
节点侧用 ``nxs/edge/client.py`` 的 ``HttpTransport`` 接入——
它实现与同进程传输相同的 ``Transport`` 协议，因此 ``NodeClient``
一行都不用改。

设计上刻意有两条路径：同进程（demo/测试）与 HTTP（真实部署）。
两者走同一套策略与边界逻辑，只是后者多了序列化与认证。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .app import NexusApp
from .core.errors import ConfigError, NexusError
from .edge.gateway import Gateway
from .mgmt.config import (
    DEFAULT_CONFIG_PATH,
    EXAMPLE_CONFIG_PATH,
    demo_env_for,
    is_example_config,
    resolve_config_path,
)

# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

_C = {"ok": "\033[32m", "bad": "\033[31m", "warn": "\033[33m",
      "dim": "\033[90m", "b": "\033[1m", "off": "\033[0m"}


def _color(text: str, key: str) -> str:
    return f"{_C[key]}{text}{_C['off']}" if sys.stdout.isatty() else text


def _info(msg: str) -> None:
    print(msg)


def _ok(msg: str) -> None:
    print(f"{_color('OK', 'ok')}   {msg}")


def _err(msg: str) -> None:
    print(f"{_color('FAIL', 'bad')} {msg}", file=sys.stderr)


def _warn(msg: str) -> None:
    print(f"{_color('WARN', 'warn')} {msg}")


def _load_app(args: argparse.Namespace, *, force_memory: bool = True) -> NexusApp:
    """加载配置并装配应用。

    若实际使用的是**示例配置**，自动注入一组临时凭据——
    让 clone 下来的人不必先配环境变量就能跑。真实配置仍必须提供
    环境变量凭据，示例配置里不放任何"默认密钥"。
    """
    resolved = resolve_config_path(args.config)
    if is_example_config(resolved):
        _info(_color(f"使用示例配置 {resolved}（自动注入临时凭据）", "dim"))
        return NexusApp.from_config_path(str(resolved),
                                         env=demo_env_for(resolved),
                                         force_memory=force_memory)
    return NexusApp.from_config_path(str(resolved), force_memory=force_memory)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace) -> int:
    """配置自检。**任何一项失败即返回非零**，不要带着坏配置启动。"""
    _info(_color(f"Nexus Mesh {__version__} · 配置自检", "b"))
    _info("")

    try:
        app = _load_app(args)
    except ConfigError as exc:
        _err(f"配置校验失败：{exc}")
        return 2
    except NexusError as exc:
        _err(f"初始化失败：{exc}")
        return 2

    cfg = app.config
    _ok(f"配置加载：{cfg.source_path}")
    _info("")
    _info(cfg.describe())
    _info("")

    # -- 一致性检查：角色卡声明 vs 节点实现 --
    checks: list[tuple[str, bool, str]] = []

    # 1. 每个能力的应答字段非空（否则任何产出都会被拒）
    for card in cfg.nodes.values():
        for cap in card.capabilities:
            name = f"{card.node_id}/{cap.type}"
            ok = bool(cap.response_fields)
            checks.append((f"{name} 声明了应答字段白名单", ok,
                           "" if ok else "response_fields 为空，任何产出都会被拒"))

    # 2. 每个节点的能力等级不得超过自身上限
    for card in cfg.nodes.values():
        for cap in card.capabilities:
            name = f"{card.node_id}/{cap.type}"
            ok = cap.level <= card.constraints.max_response_level
            checks.append((f"{name} 能力等级 ≤ 节点上限", ok,
                           "" if ok else
                           f"{cap.level} > {card.constraints.max_response_level}"))

    # 3. 矩阵里引用的域都已定义（config.load 已校验，这里复述结论）
    domains = {c.domain for c in cfg.nodes.values()}
    for i, rule in enumerate(cfg.policy.rules):
        ok = rule.from_domain in domains and rule.to_domain in domains
        checks.append((f"矩阵[{i}] {rule.from_domain}→{rule.to_domain} 域存在", ok, ""))

    # 4. 凭据长度
    for mid, secret in cfg.secrets.items():
        ok = len(secret) >= 16
        checks.append((f"{mid} 凭据长度充足", ok,
                       "" if ok else f"仅 {len(secret)} 字节"))

    failed = [c for c in checks if not c[1]]
    for label, ok, hint in checks:
        mark = _color("✓", "ok") if ok else _color("✗", "bad")
        _info(f"  {mark} {label}" + (f"   {_color(hint, 'warn')}" if hint else ""))

    _info("")
    if failed:
        _err(f"{len(failed)} 项检查未通过")
        return 1
    _ok(f"{len(checks)} 项检查全部通过")
    return 0


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------

def cmd_demo(args: argparse.Namespace) -> int:
    """跑九步验收 demo。``--net`` 时改走真实 HTTP（同一套断言）。"""
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    try:
        from examples.demo import main as demo_main
    except ImportError as exc:
        _err(f"无法加载 demo 模块：{exc}")
        _err(f"请在仓库根目录运行（当前解析到的根：{root}）")
        return 2
    return demo_main(net=args.net, config_path=args.config)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> int:
    """核心常驻：**启动 HTTP 网关 + 定期维护**。

    两件事并行：
    * **网关** —— 接受节点连接（节点出站连入），处理投递/领取/回传
    * **维护循环** —— 标记 stale 节点、释放过期租约。
      **维护动作是断电恢复的驱动源**：只要它在跑，被中断的任务
      最终都会自动回到队列，无需人工干预。
    """
    try:
        app = _load_app(args, force_memory=args.memory)
    except ConfigError as exc:
        _err(f"配置校验失败：{exc}")
        return 2

    app.start()

    _info(_color(f"Nexus Mesh {__version__}", "b"))
    _info(f"  配置      : {app.config.source_path}")
    _info(f"  存储      : queue={app.config.stores.queue} "
          f"registry={app.config.stores.registry} audit={app.config.stores.audit}")
    _info(f"  数据目录  : {app.config.stores.data_dir}")
    _info(f"  维护间隔  : {args.interval}s")
    _info("")
    _info(_color("节点状态", "b"))
    _info(app.registry.describe())
    _info("")

    if not args.memory:
        _warn("已使用持久化后端；如需每次从干净状态启动，加 --memory")

    gateway = Gateway(app)
    try:
        gateway.start()
    except ConfigError as exc:
        _err(f"网关启动失败：{exc}")
        return 2

    cfg = app.config.server
    _info(_color("传输层", "b"))
    _info(f"  监听      : {gateway.base_url}")
    _info(f"  TLS       : {'已启用' if cfg.tls_cert else '未启用（仅限本地/内网）'}")
    _info(f"  推送(SSE) : {'启用' if cfg.sse_enabled else '关闭'}"
          f" · 心跳 {cfg.sse_keepalive_seconds}s")
    _info(f"  限额      : 体 {cfg.max_body_bytes}B · "
          f"并发 {cfg.max_concurrent_requests} · 流 {cfg.max_sse_streams}")
    _info(f"  运维接口  : "
          + (_color("启用", "ok") if gateway.ops_enabled
             else _color("未启用（未设置 NXS_OPS_SECRET）", "warn")))
    _info("")
    _info(_color(f"开始常驻（Ctrl-C 退出）", "dim"))

    ticks = 0
    try:
        while True:
            time.sleep(args.interval)
            result = app.sweep()
            ticks += 1
            if result["marked_stale"] or result["released"]:
                _info(f"[tick {ticks}] 标记 stale {result['marked_stale']} 个 · "
                      f"释放过期租约 {result['released']} 个")
            if args.max_ticks and ticks >= args.max_ticks:
                _info(f"达到 --max-ticks ({args.max_ticks})，退出")
                break
    except KeyboardInterrupt:
        _info("")
        _info("收到中断，正在关闭网关…")
    finally:
        gateway.stop()
    _info("已停止")
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    """查看节点状态与审计摘要。"""
    try:
        app = _load_app(args)
    except ConfigError as exc:
        _err(f"配置校验失败：{exc}")
        return 2
    app.start()

    _info(_color("节点状态", "b"))
    _info(app.registry.describe())
    _info("")
    _info(_color("能力索引", "b"))
    idx = app.registry.capability_index()
    if not idx:
        _info(_color("  （空：没有可路由的节点——都需要先 approve）", "dim"))
    for t, providers in idx.items():
        _info(f"  {t:<22} ← {', '.join(providers)}")
    _info("")
    _info(_color("审计摘要", "b"))
    s = app.audit_summary()
    _info(f"  总记录 {s['total']} · 完成 {s['completed']} · 拒绝 {s['denied']}")
    for rule, cnt in s.get("denied_by_rule", {}).items():
        _info(f"    · {rule:<16} × {cnt}")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    """查看运行时指标与**系统级关联**异常。

    两种用法，刻意分开：

    * ``nxs metrics``（本机）—— 直连本地进程状态。
      适合开发、demo、以及"服务就在这台机器上"的场景。
      注意它读的是**本进程内存**，所以只有同进程运行时才有数据。
    * ``nxs metrics --url ...`` —— 走运维端点读**远端运行中的服务**。
      这是生产环境的正常用法。

    两者共用同一份渲染逻辑（:func:`_render_metrics`）：
    同样的问题不该因为数据从哪来而有两套答案。
    """
    if args.url:
        return _metrics_remote(args)

    from .mgmt.observ import collect

    try:
        app = _load_app(args)
    except ConfigError as exc:
        _err(f"配置校验失败：{exc}")
        return 2
    app.start()

    snap = collect(app.metrics, registry=app.registry,
                   queue=app.stores.queue)
    _render_metrics(snap.as_dict(), json_out=args.json)
    return 0


def _metrics_remote(args: argparse.Namespace) -> int:
    """从运行中的 hub 读指标（走运维通道）。"""
    import os

    from .edge.client import OpsClient
    from .mgmt.config import ENV_OPS_SECRET, MIN_SECRET_LENGTH

    raw = os.environ.get(ENV_OPS_SECRET, "")
    if len(raw) < MIN_SECRET_LENGTH:
        _err(f"缺少运维凭据：环境变量 {ENV_OPS_SECRET} 未设置或过短")
        return 2
    try:
        ops = OpsClient(args.url, secret=raw.encode())
        payload = ops.metrics()
    except NexusError as exc:
        _err(str(exc))
        return 1
    _render_metrics(payload, json_out=args.json)
    return 0


def _pad(text: str, width: int) -> str:
    """按**终端显示宽度**左对齐。

    为什么不能直接用 f-string 的 ``:<14``：那个按字符数算，
    而中文在终端占两格。一列中文混一个英文，表格就歪了。

    运维工具歪掉的表格不只是难看——它会让人看错行，
    尤其在"字节数"和"失败数"这种挨着的列上。
    """
    import unicodedata

    shown = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
                for ch in text)
    return text + " " * max(0, width - shown)


def _render_metrics(data: dict, *, json_out: bool = False) -> None:
    """渲染指标与异常。**CLI 与运维端点共用同一份数据形状。**"""
    import json as _json

    if json_out:
        print(_json.dumps(data, ensure_ascii=False, indent=2))
        return

    totals = data.get("totals", {})
    _info(_color("运行时指标", "b") + f"  （已运行 {data.get('uptime_seconds', 0):.0f}s）")
    _info(f"  提交 {totals.get('submitted', 0)} · 完成 {totals.get('completed', 0)} · "
          f"失败 {totals.get('failed', 0)} · 拒绝 {totals.get('denied', 0)}")

    lat = data.get("latency_ms") or {}
    samples = data.get("latency_samples", 0)
    if samples >= 2:
        _info(f"  延迟 p50={lat.get('p50')}ms  p95={lat.get('p95')}ms  "
              f"p99={lat.get('p99')}ms  （{samples} 个样本）")
    else:
        _info(_color(f"  延迟：样本不足（{samples} 个），暂不算分位数", "dim"))

    by_type = data.get("by_type") or {}
    if by_type:
        _info("")
        _info(_color("按任务类型", "b"))
        for tname in sorted(by_type):
            stat = by_type[tname]
            total = stat.get("completed", 0) + stat.get("failed", 0)
            rate = (stat.get("failed", 0) / total) if total else 0.0
            _info(f"  {_pad(tname, 22)} 完成 {stat.get('completed', 0):<6} "
                  f"失败 {stat.get('failed', 0):<6} 失败率 {rate:.0%}")

    rules = data.get("denied_by_rule") or {}
    if rules:
        _info("")
        _info(_color("拒绝分布", "b"))
        for rule, cnt in sorted(rules.items(), key=lambda kv: -kv[1]):
            _info(f"  {_pad(rule, 18)} × {cnt}")

    nodes = data.get("nodes") or []
    if nodes:
        _info("")
        _info(_color("节点", "b"))
        _info(_color(
            "  " + _pad("节点", 14) + _pad("域", 12) + _pad("状态", 10)
            + _pad("收到", 8) + _pad("完成", 8) + _pad("失败", 8)
            + _pad("待办", 8) + _pad("执行中", 9) + _pad("水位", 8) + "字节", "b"))
        for n in nodes:
            mark = "" if n.get("approved") else "  [未批准]"
            backlog = n.get("backlog_ratio")
            backlog_text = "∞" if backlog is None else f"{backlog:.1f}×"
            _info("  " + _pad(n["node_id"], 14) + _pad(n["domain"], 12)
                  + _pad(n["state"], 10) + _pad(str(n["received"]), 8)
                  + _pad(str(n["completed"]), 8) + _pad(str(n["failed"]), 8)
                  + _pad(str(n["pending"]), 8) + _pad(str(n["in_flight"]), 9)
                  + _pad(backlog_text, 8) + str(n["bytes_out"]) + mark)

    # ---- 异常 ----
    anomalies = data.get("anomalies") or []
    _info("")
    if not anomalies:
        _info(_color("系统级关联异常：无", "ok"))
        _info(_color(
            "  （只报「组合异常」——单指标超阈值不在此列，那种告警会响到没人看）",
            "dim"))
        return

    _info(_color(f"系统级关联异常：{len(anomalies)} 条", "bad"))
    for a in anomalies:
        sev = a.get("severity", "info")
        color = {"critical": "bad", "warn": "warn"}.get(sev, "dim")
        _info("")
        _info(f"  {_color('●', color)} [{sev}] {a['rule']} · {a['subject']}")
        _info(f"      {a['message']}")
        # rationale 是这类规则存在的资格证明，必须显示出来——
        # 否则运维会问"这凭什么算异常"
        _info(_color(f"      为什么单看正常：{a['rationale']}", "dim"))
        _info(_color(f"      建议：{a['suggestion']}", "dim"))


def cmd_candidates(args: argparse.Namespace) -> int:
    """列出某任务类型的候选执行者，以及**为什么是这个顺序**。

    ⚠ 这是**运维视角**的命令：它读的是 hub 本地状态，
    不经 HTTP 暴露。"谁能干这个活"是拓扑信息。

    设计意图：让"自动匹配为什么投给了 A 而不是 B"成为一个
    可以直接查询的问题，而不是靠翻日志猜。
    """
    try:
        app = _load_app(args)
    except ConfigError as exc:
        _err(f"配置校验失败：{exc}")
        return 2
    app.start()
    if not args.task_type:
        _err("缺少任务类型：用 --type 指定，例如 --type demo.echo")
        return 2
    try:
        rows = app.transport.candidates(
            task_type=args.task_type,
            from_node=args.from_node or "",
            min_level=args.min_level,
        )
    except NexusError as exc:
        _err(str(exc))
        return 1

    _info(_color(f"候选执行者 · {args.task_type}", "b")
          + (f"  （来源 {args.from_node}）" if args.from_node else ""))
    if not rows:
        _info(_color(
            "  （无候选）可能原因：没有节点声明该能力 / 等级不够 / "
            "节点未批准或已吊销", "warn"))
        # ⭐ 主动指出"是不是有节点在等批准"。
        #    运维最常卡在这里：节点明明声明了能力，却因为没批准而"看不见"。
        #    这里只在本机 CLI 显示，不构成对外泄漏（运维本来就能看节点列表）。
        pending = [
            rec.card.node_id for rec in app.registry.all_records()
            if not rec.approved and rec.card.capability(args.task_type)
        ]
        if pending:
            _info("")
            _info(_color(
                f"  其中 {len(pending)} 个节点声明了该能力但**尚未批准**："
                f"{', '.join(sorted(pending))}", "warn"))
            _info(_color(
                "  批准：nxs ops approve <节点ID> --url <hub-url>", "dim"))
        return 0

    _info(_color(
        f"  {'#':<3}{'节点':<14}{'域':<12}{'等级':<10}{'负载':<10}{'同域':<6}", "b"))
    for i, r in enumerate(rows, 1):
        _info(f"  {i:<3}{r['node_id']:<14}{r['domain']:<12}{r['level']:<10}"
              f"{str(r['load']) + '/' + str(r['capacity']):<10}"
              f"{'✓' if r['same_domain'] else '-':<6}")

    _info("")
    _info(_color(
        "排序依据：① 同域优先 ② 等级刚好够用 ③ 负载率低 ④ 节点 ID 字典序。"
        "同样的输入必定选出同一个。", "dim"))
    _info(_color(
        "⚠ 候选只是「能不能干」。真的投得出去还要过策略矩阵——"
        "这里是运维视角，节点拿不到这份清单。", "dim"))
    return 0


# ---------------------------------------------------------------------------
# doctor（部署自检）
# ---------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    """部署前自检。**专门检查那些"能启动但会出事"的配置。**

    与 ``nxs verify`` 的分工：
    * ``verify`` 查**配置本身**是否自洽（结构、引用、凭据长度）
    * ``doctor`` 查**这个部署能不能长期稳定运行**（持久化、权限、端口、
      TLS、运维通道）

    后者要抓的是"跑起来一切正常、出事时才发现"的问题：
    用 memory 后端跑生产、数据目录不可写、运维通道没开导致故障时进不来、
    角色卡非确定性导致每次重启批准都失效。
    """
    import os
    import socket
    from pathlib import Path as _Path

    from .mgmt.config import ENV_OPS_SECRET, MIN_SECRET_LENGTH, OPS_NODE_ID

    production = args.production
    _info(_color(f"Nexus Mesh {__version__} · 部署自检", "b")
          + (f"  {_color('（生产模式）', 'warn')}" if production else ""))
    _info("")

    try:
        # ⭐ 只加载配置，**不装配存储**。
        #    doctor 检查的是"部署条件"，不该因为数据目录建不了就放弃所有检查——
        #    那恰恰是它最该报告的问题。装配存储会把 OSError 提前抛出来，
        #    让人看不到后面那些同样重要的检查结果。
        from .mgmt.config import load_config

        resolved = resolve_config_path(args.config)
        env = demo_env_for(resolved) if is_example_config(resolved) else None
        cfg = load_config(resolved, env=env)
    except ConfigError as exc:
        _err(f"配置校验失败：{exc}")
        return 2
    except NexusError as exc:
        _err(f"初始化失败：{exc}")
        return 2
    except OSError as exc:
        _err(f"无法读取配置：{exc}")
        return 2

    #: (标签, 状态, 提示)。状态三态：
    #:   "ok"   通过
    #:   "warn" 非生产模式下不视为失败，但**必须显式说出来** ——
    #:          把它显示成 ✓ 会让人以为已经检查过了
    #:   "fail" 失败
    checks: list[tuple[str, str, str]] = []

    def add(label: str, status: str, hint: str = "") -> None:
        checks.append((label, status, hint))

    def check(label: str, ok: bool, hint: str = "",
              *, warn_when_not_production: bool = False) -> None:
        """便捷封装：失败项在非生产模式下降级为 warn。"""
        if ok:
            add(label, "ok")
        elif warn_when_not_production and not production:
            add(label, "warn", hint)
        else:
            add(label, "fail", hint)

    # ---- 持久化 ----
    # ⭐ 这是最容易犯、最难发现的部署错误：memory 后端跑起来完全正常，
    #    重启之后所有节点注册与在途任务全没，节点得重新注册、重新批准。
    persistent = (cfg.stores.queue != "memory"
                  and cfg.stores.registry != "memory")
    check("存储后端是持久化的（重启不丢节点与任务）", persistent,
          "queue/registry 用了 memory —— 重启后所有节点注册与在途任务全部丢失。"
          "生产环境必须用 sqlite",
          warn_when_not_production=True)

    # ---- 数据目录可写 ----
    data_dir = _Path(cfg.stores.data_dir)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".nxs-doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add(f"数据目录可写（{data_dir}）", "ok")
    except OSError as exc:
        add(f"数据目录可写（{data_dir}）", "fail",
            f"{type(exc).__name__}: {exc}")

    # ---- 端口可用 ----
    host, port = cfg.server.host, cfg.server.port
    if port == 0:
        check("监听端口为系统分配（仅适合测试）", not production,
              "生产环境应固定端口")
    else:
        try:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((host, port))
            add(f"端口可用（{host}:{port}）", "ok")
        except OSError as exc:
            # 服务正在运行时端口被占是**正常的**，所以只算提示
            add(f"端口可用（{host}:{port}）", "warn",
                f"已被占用（若服务正在运行则正常）: {exc}")

    # ---- TLS ----
    # ⭐ 分两种情况，不能一刀切：
    #   · 监听 0.0.0.0 且无 TLS → **真的危险**（明文暴露在公网），必须失败
    #   · 监听 127.0.0.1 且无 TLS → 合理。前面通常有 Caddy/Nginx 终止 TLS，
    #     部署指南推荐的正是这个组合。这里一刀切会让推荐做法通不过自检，
    #     运维的唯一出路是忽略报错——那就把自检变成了噪音。
    tls_ok = bool(cfg.server.tls_cert and cfg.server.tls_key)
    local_only = host in ("127.0.0.1", "::1", "localhost")
    if tls_ok:
        cert_ok = _Path(cfg.server.tls_cert).exists()
        key_ok = _Path(cfg.server.tls_key).exists()
        check("TLS 证书文件存在", cert_ok and key_ok,
              "证书或私钥路径不存在")
    elif local_only:
        add("TLS 由前置代理终止（只监听回环地址）", "ok",
            "")
        _info(_color(
            "      ⚠ 请自行确认代理确实配了 TLS："
            "curl -v https://<域名>/nxs/health", "dim"))
    else:
        check("TLS 已配置（或改为只监听回环地址）", False,
              f"监听 {host} 但没配 tls_cert/tls_key —— 公网明文传输。"
              f"要么配证书，要么改 host: 127.0.0.1 并在前面放 Caddy/Nginx",
              warn_when_not_production=True)

    # ---- 运维通道 ----
    # ⭐ 运维通道没开 = 故障时进不来。这是"平时无感、出事致命"的一项。
    ops_raw = os.environ.get(ENV_OPS_SECRET, "")
    ops_ok = len(ops_raw) >= MIN_SECRET_LENGTH
    check(f"运维通道已启用（{ENV_OPS_SECRET}）", ops_ok,
          f"未设置 {ENV_OPS_SECRET}（或过短）。故障时无法远程批准/吊销节点，"
          f"只能改配置重启",
          warn_when_not_production=True)

    # ---- 自动批准 ----
    auto = cfg.policy.auto_approve_domains
    check("未自动批准任何信任域", not auto,
          f"auto_approve_domains={auto}：该域下所有节点（含未来新接入的）"
          f"自动获批，跳过了人工审批",
          warn_when_not_production=True)

    # ---- 节点 ID 保留字 ----
    reserved = [n for n in cfg.nodes if n == OPS_NODE_ID]
    check("没有节点占用保留的运维身份", not reserved,
          f"{reserved} 与运维身份冲突")

    # ---- 角色卡确定性（防止"每次重启批准都失效"）----
    try:
        from .mgmt.config import load_config as _reload

        env_for_reload = {
            f"NXS_SECRET_{k.upper().replace('-', '_')}": v.decode()
            for k, v in cfg.secrets.items()
        }
        again = _reload(_Path(cfg.source_path), env=env_for_reload)
        stable = all(cfg.nodes[k] == again.nodes.get(k) for k in cfg.nodes)
        check("角色卡是确定性的（同一配置两次加载一致）", stable,
              "角色卡含随机/时间内容：每次重启都会被判为「卡片已变更」，"
              "批准会自动撤销")
    except (ConfigError, NexusError, OSError):
        add("角色卡是确定性的（无法复验）", "warn",
            "配置无法二次加载，跳过该项")

    # ---- 输出 ----
    failed = [c for c in checks if c[1] == "fail"]
    warned = [c for c in checks if c[1] == "warn"]
    marks = {"ok": _color("✓", "ok"), "warn": _color("!", "warn"),
             "fail": _color("✗", "bad")}
    for label, status, hint in checks:
        _info(f"  {marks[status]} {label}"
              + (f"\n      {_color(hint, 'warn')}" if hint else ""))

    _info("")
    if failed:
        _err(f"{len(failed)} 项检查未通过")
        return 1
    if warned:
        if production:
            _err(f"{len(warned)} 项在生产模式下不可接受")
            return 1
        _warn(f"{len(warned)} 项需要关注（非生产模式，不视为失败）")
        return 0
    _ok(f"{len(checks)} 项检查全部通过")
    return 0


# ---------------------------------------------------------------------------
# ops（远程运维）
# ---------------------------------------------------------------------------

def cmd_ops(args: argparse.Namespace) -> int:
    """运维动作：批准 / 吊销 / 释放租约 / 查节点。

    **通过 HTTP 打到运行中的 hub**，所以不需要停服务、不需要碰它的数据库。
    凭据来自环境变量 ``NXS_OPS_SECRET``；未配置时 hub 的运维端点整体关闭。
    """
    import os

    from .edge.client import OpsClient
    from .mgmt.config import ENV_OPS_SECRET, MIN_SECRET_LENGTH

    url = args.url or os.environ.get("NXS_HUB_URL", "")
    if not url:
        _err("缺少 hub 地址：用 --url 指定，或设置环境变量 NXS_HUB_URL")
        _err("例如：--url https://nxs.example.com/nxs")
        return 2

    raw_secret = os.environ.get(ENV_OPS_SECRET, "")
    if len(raw_secret) < MIN_SECRET_LENGTH:
        _err(f"缺少运维凭据：环境变量 {ENV_OPS_SECRET} 未设置或过短"
             f"（需 ≥{MIN_SECRET_LENGTH} 字节）")
        _err("hub 端必须用**同一个值**启动，否则运维端点不会开启")
        return 2

    try:
        ops = OpsClient(url, secret=raw_secret.encode())
        if args.action == "nodes":
            rows = ops.nodes()
            if args.json:
                import json as _json
                print(_json.dumps({"nodes": rows}, ensure_ascii=False, indent=2))
                return 0
            if not rows:
                _info(_color("（注册表为空）", "dim"))
                return 0
            # 运维看节点，最先要看懂的是「它叫什么、是不是活着、能不能用」。
            # 所以名称与批准状态在左侧；能力放最后（它是长文本，会挤掉前面）。
            _info(_color("  " + _pad("名称", 20) + _pad("节点", 14)
                         + _pad("域", 12) + _pad("状态", 10)
                         + _pad("批准", 6) + "能力", "b"))
            for r in rows:
                mark = _color("✓", "ok") if r["approved"] else _color("·", "warn")
                caps = ",".join(r["capabilities"]) or "-"
                name = r.get("display_name") or "—"
                _info("  " + _pad(name, 20) + _pad(r["node_id"], 14)
                      + _pad(r["domain"], 12) + _pad(r["state"], 10)
                      + _pad(mark, 6) + caps)
                # 简介单独一行：它是自由文本，塞进表格会拖垮列宽。
                # ⚠ 这里**不清洗**——控制字符已在卡片解析时被拒，
                #   但若将来有别的入口绕过，终端注入的风险仍在。
                if r.get("description"):
                    _info(_color(f"  {'':20}{r['description']}", "dim"))
            _info("")
            _info(_color("  用 --json 取完整字段（含简介、角色、应答上限）", "dim"))
            return 0

        if args.action in ("approve", "revoke"):
            if not args.node:
                _err(f"{args.action} 需要节点 ID")
                return 2
            result = (ops.approve(args.node) if args.action == "approve"
                      else ops.revoke(args.node))
            verb = "已批准" if args.action == "approve" else "已吊销"
            _ok(f"{verb} {result['node_id']}  "
                f"state={result['state']} approved={result['approved']}")
            if args.action == "approve":
                _info(_color(
                    "⚠ 批准绑定的是它**当前那张角色卡**——"
                    "若节点之后改了卡片，批准会自动撤销，需要重新批准。", "dim"))
            return 0

        if args.action == "release":
            result = ops.release()
            _ok(f"已执行维护：标记 stale {result.get('marked_stale', 0)} 个 · "
                f"释放过期租约 {result.get('released', 0)} 个")
            return 0

        _err(f"未知动作 {args.action}")
        return 2
    except NexusError as exc:
        _err(str(exc))
        return 1


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nxs",
        description="Nexus Mesh —— 面向异构 Agent 的自托管协作网络",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  nxs verify                         自检示例配置\n"
            "  nxs demo                           跑九步验收 demo（同进程）\n"
            "  nxs demo --net                     同一套断言，走真实 HTTP\n"
            "  nxs serve -c config/local.yaml     常驻：监听端口 + 定期维护\n"
            "  nxs status                         查看节点与审计\n\n"
            "配置路径也可用环境变量 NXS_CONFIG 指定。\n"
            "节点凭据从环境变量读取：NXS_SECRET_<节点ID大写>。\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<命令>")

    def add_common(sp):
        # default 保持 None：让配置层决定"默认用哪个"，
        # 它会在 local.yaml 不存在时回退到示例配置。
        sp.add_argument("-c", "--config", default=None,
                        help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}，"
                             f"不存在时回退 {EXAMPLE_CONFIG_PATH}）")

    sp = sub.add_parser("verify", help="配置自检")
    add_common(sp)
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("demo", help="跑九步验收 demo")
    add_common(sp)
    sp.add_argument("--net", action="store_true",
                    help="改走真实 HTTP（同一套九步断言，验证传输层）")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("serve", help="核心常驻（含定期维护）")
    add_common(sp)
    sp.add_argument("--interval", type=float, default=5.0,
                    help="维护间隔秒数（默认 5）")
    sp.add_argument("--memory", action="store_true",
                    help="强制使用内存后端（不持久化）")
    sp.add_argument("--max-ticks", type=int, default=0,
                    help="跑够 N 个维护周期即退出（0 表示不退出；用于测试）")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("status", help="查看节点状态与审计摘要")
    add_common(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser(
        "candidates",
        help="列出某任务类型的候选执行者（运维视角，不经 HTTP 暴露）")
    add_common(sp)
    sp.add_argument("--type", dest="task_type", required=True,
                    help="任务类型，例如 demo.echo")
    sp.add_argument("--from-node", default="",
                    help="来源节点 ID（用于计算同域优先）")
    sp.add_argument("--min-level", default=None,
                    help="需要的最低应答等级：metadata/summary/fragment/full_text")
    sp.set_defaults(func=cmd_candidates)

    sp = sub.add_parser(
        "metrics",
        help="运行时指标与系统级关联异常（本机进程状态，或 --url 读远端）")
    add_common(sp)
    sp.add_argument("--url", default=None,
                    help="读运行中的 hub（走运维通道）。省略时读本进程内存，"
                         "仅在同进程运行时有数据")
    sp.add_argument("--json", action="store_true",
                    help="输出 JSON（便于喂给其他工具）")
    sp.set_defaults(func=cmd_metrics)

    sp = sub.add_parser(
        "doctor", help="部署前自检（持久化 / 权限 / TLS / 运维通道）")
    add_common(sp)
    sp.add_argument("--production", action="store_true",
                    help="按生产标准检查（memory 后端、未配 TLS、无运维通道都会判失败）")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser(
        "ops", help="远程运维：批准 / 吊销 / 释放租约 / 查节点")
    sp.add_argument("action", choices=["approve", "revoke", "release", "nodes"],
                    help="运维动作")
    sp.add_argument("--json", action="store_true",
                    help="JSON 输出（仅 nodes 动作；便于喂给其他工具）")
    sp.add_argument("node", nargs="?", default=None,
                    help="节点 ID（approve / revoke 需要）")
    sp.add_argument("--url", default=None,
                    help="hub 地址，如 https://nxs.example.com/nxs"
                         "（也可用环境变量 NXS_HUB_URL）")
    sp.set_defaults(func=cmd_ops)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except NexusError as exc:
        _err(str(exc))
        return 1
    except OSError as exc:
        # 兜底：任何命令都不该把 OSError 的栈直接甩给运维
        _err(f"文件系统错误：{exc}")
        return 1
    except KeyboardInterrupt:
        _info("")
        _info("已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
