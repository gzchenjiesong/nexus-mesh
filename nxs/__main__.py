"""命令行入口。

四个子命令：

    nxs verify  配置自检：加载配置、校验合法性、打印生效的规则
    nxs demo    跑七步验收 demo（同 ``make demo``）
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
    """跑七步验收 demo。``--net`` 时改走真实 HTTP（同一套断言）。"""
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
            "  nxs demo                           跑七步验收 demo（同进程）\n"
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

    sp = sub.add_parser("demo", help="跑七步验收 demo")
    add_common(sp)
    sp.add_argument("--net", action="store_true",
                    help="改走真实 HTTP（同一套七步断言，验证传输层）")
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


if __name__ == "__main__":
    raise SystemExit(main())
