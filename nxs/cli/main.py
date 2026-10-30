"""`nxs-cli` 的入口：参数解析 + 统一错误处理。

设计原则：**每条失败都要给出可执行的下一步**。
只抛一个 ``MX-ISSUER-01`` 给 Agent，它既不知道含义也不知道怎么修 ——
那等于把排查成本推给了一个不会查文档的东西。
"""

from __future__ import annotations

import argparse
import sys

from ..core.errors import ConfigError, PolicyDenied
from .actor import resolve_actor
from .commands import COMMANDS, DEFAULT_ASK_TIMEOUT, CliError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nxs-cli",
        description="以某个节点身份使用 Nexus Mesh（Agent 用的工具）",
        epilog=(
            "示例：\n"
            "  nxs-cli --as taskflow whoami\n"
            "  nxs-cli --as taskflow ask --to hermes-working \"单体还是微服务？\"\n"
            "  nxs-cli --as taskflow task t-abc123\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--as", dest="actor", default=None, metavar="NODE_ID",
                   help="以哪个节点身份调用。省略时读 NXS_ACTOR 环境变量"
                        "（桥接进程会自动注入）")
    p.add_argument("--json", action="store_true",
                   help="输出 JSON（便于程序解析）")

    sub = p.add_subparsers(dest="cmd", required=True, metavar="<命令>")

    sub.add_parser("whoami", help="我是谁：身份牌、角色、能力、自限、批准状态")
    sub.add_parser("nodes", help="网络里有谁（当前有协议限制，见输出）")

    sp = sub.add_parser("ask", help="问一个 Agent 并等它的回答")
    sp.add_argument("--to", required=True, metavar="NODE_ID", help="目标节点")
    sp.add_argument("question", help="问题文本")
    sp.add_argument("--type", default="agent.ask", help="任务类型（默认 agent.ask）")
    sp.add_argument("--timeout", type=int, default=DEFAULT_ASK_TIMEOUT,
                    help=f"最长等待秒数（默认 {DEFAULT_ASK_TIMEOUT}）")
    sp.add_argument("--idempotency-key", default=None,
                    help="幂等键（重试同一件事时用同一个，避免重复执行）")

    sp = sub.add_parser("publish", help="发任务但不等待（长任务用）")
    sp.add_argument("--to", required=True, metavar="NODE_ID")
    sp.add_argument("--type", required=True, help="任务类型")
    sp.add_argument("--params", default="{}", help="参数 JSON（默认 {}）")
    sp.add_argument("--idempotency-key", default=None)

    sp = sub.add_parser("task", help="查任务状态与产出")
    sp.add_argument("task_id", metavar="TASK_ID")

    sub.add_parser("inbox", help="我的信箱（当前有协议限制，见输出)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        actor = resolve_actor(args.actor)
    except ConfigError as exc:
        print(f"身份解析失败：\n{exc}", file=sys.stderr)
        return 2

    handler = COMMANDS.get(args.cmd)
    if handler is None:  # pragma: no cover - argparse 已挡住
        print(f"未知命令：{args.cmd}", file=sys.stderr)
        return 2

    try:
        return handler(actor, args)
    except CliError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except PolicyDenied as exc:
        # 没被业务层翻译过的拒绝：至少把 rule_id 和身份说清楚
        print(f"\n被拒：{exc.rule_id.value}", file=sys.stderr)
        print(f"  身份：{actor.node_id}　服务端：{actor.url}", file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(f"\n配置问题：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        # 连不上：最常见的是 CA 不对或服务端没起
        print(f"\n连不上服务端 {actor.url}：{exc}", file=sys.stderr)
        print(f"  若服务端用自签证书，确认 {actor.source} 里的 "
              f"NXS_CA_FILE 指向正确的 CA。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
