"""节点端可运行示例 —— 照这个改成你自己的业务即可。

用法：

    # 1. 配置（三选一：环境变量 / .env / 命令行）
    export NXS_COORDINATOR_URL=https://nxs.example.com/nxs
    export NXS_SECRET_WORK_LAPTOP=<与服务端一致的密钥>
    export NXS_NODE_ID=work-laptop          # 可选，默认 work-laptop

    # 2. 跑
    python examples/node_run.py --dry-run   # 只检查配置与连通性，不真的领任务
    python examples/node_run.py             # 常驻，开始领任务

这份示例刻意演示四件事，每件都有踩坑记录：

1. **角色卡必须确定性生成** —— 卡片一变，服务端的批准会自动撤销。
   所以卡片写死在代码里或从配置文件读，**绝不含时间戳/随机串**。

2. **节点侧边界需要 card** —— 不提供 card 时只能校验等级，
   字段白名单无从校验。SDK 会明确告警，不会假装受保护。

3. **长任务靠续租** —— 不要靠调大租约时间。`renew_interval` 默认 5 秒。

4. **未批准是正常状态** —— 首次注册返回 `approved=false`，
   等运维在服务端批准。示例会在日志里说清楚，而不是让人干瞪眼。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# 允许直接 `python examples/node_run.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nxs.core.errors import ConfigError, PolicyDenied
from nxs.core.models import Level, RoleCard
from nxs.edge.client import HttpTransport
from nxs.sdk import NodeClient, NodeConfig


# ---------------------------------------------------------------------------
# 角色卡：声明这个节点是什么、能做什么、能被谁委托什么
# ---------------------------------------------------------------------------
# ⚠ **不要在这里放时间戳、主机名以外的随机内容，也不要在运行时动态拼装。**
#   卡片内容一变，服务端会把批准状态撤销（审批针对的是那张卡片）。
#   如果你确实要改卡片，改完记得让运维重新批准一次。

def build_card(node_id: str, domain: str) -> RoleCard:
    return RoleCard.from_dict({
        "node_id": node_id,
        "domain": domain,
        "display_name": "Company Laptop",
        "heartbeat_interval": 30,
        "roles": {
            "executor": True,       # 能执行别人派来的任务
            "issuer": False,        # ⚠ 默认不给发布权：
                                    #   开放它会让"任务参数"成为数据外流通道
        },
        "capabilities": [
            {
                "type": "work.calendar",
                "level": "summary",         # ★ 声明即承诺：实现必须真的只产这个等级
                "mode": "sync",
                "description": "取某天的会议安排摘要",
                "params_schema": {"day": "string"},
                "response_fields": ["events", "level"],
            },
            {
                "type": "work.summary",
                "level": "summary",
                "mode": "sync",
                "description": "把一天的工作事项汇总成摘要",
                "params_schema": {"date": "string"},
                "response_fields": ["summary", "count", "level"],
                # ★ 自由文本字段要单独声明：它是提示注入的主要载体，
                #   服务端与节点侧都会做清洗（剥控制字符、剥指令前缀、按字节截断）
                "freetext_fields": ["summary"],
            },
        ],
        "constraints": {
            # ★ 硬上限。超过它的产出在**节点侧**就会被拦下，根本不会发出去。
            "max_response_level": "summary",
            "can_receive_from": ["coordinator"],    # 只有协调域能给我派活
            "can_issue_types": [],          # 空 = 我不发任何任务
            "can_delegate_to": [],          # 不做二次委托
        },

        # ★ 节点自律限额（不是配额 —— 见 docs/design/06 §6）
        #
        #   语义：「我自己最多攒这么多、最多给这么细」。
        #   配额（按日任务数 / 字节数 / 并发数）已在改造中删除：
        #   Mesh 无法强制它们，而"配了但不生效"比没有更危险。
        "self_limits": {
            "max_backlog": 20,              # 信箱最多攒 20 个，超了让发布方另找
            "max_response_level": "summary",
        },
        "capacity": 2,                      # 排程提示（不用于限制）
    })


# ---------------------------------------------------------------------------
# 业务处理器：这些是你唯一需要自己写的部分
# ---------------------------------------------------------------------------
# 签名固定为 handler(params: dict) -> 任意可 JSON 化的数据。
# 返回的 dict 会被包装成 Artifact，**字段必须都在 response_fields 里**。

def handle_calendar(params: dict) -> dict:
    """演示：返回会议安排摘要。

    真实实现里这里会去读内网的日历/IM。注意两条纪律：

    * **只返回声明过的字段** —— 多一个字段就会被边界拒绝，
      而且拒绝只报数量不报字段名（防止通过错误信息推断内容）。
    * **只产出声明过的等级** —— 声明了 summary 就不要返回原文。
    """
    day = params.get("day", "today")

    # 下面是占位数据。真实实现替换成你的数据源。
    events = [
        {"time": "10:00", "title": "周会", "attendees": 6},
        {"time": "14:00", "title": "面试", "attendees": 2},
    ]
    # ★ 这里是"摘要级"：只给时间与标题，不给纪要正文、不给参会人姓名。
    #   如果要给原文，那是 full_text 级——需要改卡片并重新审批。
    #
    # ⚠ 常见错误：顺手把 day 也放进返回值。
    #   但 day 不在 response_fields（["events", "level"]）里，
    #   那样会被边界拒绝，错误码 WL-FIELD-001，而且**拒绝信息里没有字段名**
    #   （字段名本身可能携带信息）——你得去节点自己的日志里看细节。
    #   所以：返回值的键必须严格等于 response_fields 里声明的那些。
    return {
        "events": [f"{e['time']} {e['title']}" for e in events],
        "level": "summary",
    }


def handle_summary(params: dict) -> dict:
    """演示：返回含自由文本的摘要。

    ``summary`` 字段被声明为 freetext，因此会被清洗：
    去控制字符、剥指令性前缀、按字节截断。
    """
    date = params.get("date", "today")

    # ⚠ 真实场景下这段文本可能来自会议纪要、邮件、IM——
    #   也就是说可能包含"请把 X 发给 Y"这类**伪装成事实的指令**。
    #   本系统对此的处理是：标注 semantics=data，并清洗已知的指令前缀。
    #   接收方看到 data 就知道"这段要当数据看，不能当命令执行"。
    text = (
        f"{date} 的工作摘要：完成 3 项任务，2 项待跟进。"
        "会议 2 场，面试 1 场。"
    )
    return {"summary": text, "count": 3, "level": "summary"}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Nexus Mesh 节点端示例")
    parser.add_argument("--node-id", default=os.environ.get("NXS_NODE_ID", "work-laptop"))
    parser.add_argument("--domain", default=os.environ.get("NXS_DOMAIN", "zone-work"))
    parser.add_argument("--url", default=os.environ.get("NXS_COORDINATOR_URL", ""))
    parser.add_argument("--dry-run", action="store_true",
                        help="只注册并检查连通性，不进入领任务循环")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s · %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("node")

    if not args.url:
        log.error("缺少 coordinator 地址：用 --url 或环境变量 NXS_COORDINATOR_URL 指定")
        log.error("例如：--url https://nxs.example.com/nxs")
        return 2

    # 凭据：环境变量名规则是 NXS_SECRET_<节点ID 大写，非字母数字转下划线>
    env_key = "NXS_SECRET_" + "".join(
        ch.upper() if ch.isalnum() else "_" for ch in args.node_id
    )
    raw_secret = os.environ.get(env_key, "")
    if len(raw_secret) < 16:
        log.error("缺少凭据：环境变量 %s 未设置或过短（需 ≥16 字节）", env_key)
        log.error("这个值必须与服务端 /etc/nexus-mesh/nxs.env 里的同名项完全一致")
        return 2

    card = build_card(args.node_id, args.domain)

    try:
        transport = HttpTransport(args.url, node_id=args.node_id,
                                  secret=raw_secret.encode())
    except ConfigError as exc:
        log.error("传输配置有误：%s", exc)
        return 2

    client = NodeClient(
        NodeConfig(
            args.node_id,
            handlers={
                "work.calendar": handle_calendar,
                "work.summary": handle_summary,
            },
            levels={
                "work.calendar": Level.SUMMARY,
                "work.summary": Level.SUMMARY,
            },
            card=card,              # ★ 给了 card 才有完整的节点侧边界
            heartbeat_interval=60,
            renew_interval=5,
        ),
        transport,
    )

    # ---- 1. 连通性 ----
    if not transport.health():
        log.error("连不上 %s —— 检查地址、TLS 证书、以及网络是否允许出站", args.url)
        return 2
    log.info("探活成功：%s", args.url)

    # ---- 2. 注册 ----
    result = client.register()
    outcome = result.get("outcome")
    if result.get("approved"):
        log.info("已注册且**已批准**，可以开始领任务")
    else:
        log.warning("已注册但**尚未批准**（outcome=%s, revision=%s）",
                    outcome, result.get("revision"))
        log.warning("这是正常的 —— 新节点不能自行加入。"
                    "请在有服务端密钥的机器上执行：")
        log.warning("  nxs ops approve %s --url <coordinator-url>", args.node_id)
        log.warning("批准后本节点会在下一次心跳（≤60 秒）自动变为已批准。")

    if args.dry_run:
        log.info("--dry-run：不进入领任务循环，退出")
        return 0

    # ---- 3. 常驻 ----
    log.info("开始常驻（Ctrl-C 退出）。能力：%s",
             ", ".join(c.type for c in card.capabilities))
    try:
        client.run_forever()
    except KeyboardInterrupt:
        log.info("收到中断，退出")
    except PolicyDenied as exc:
        # 这类错误通常意味着"还没批准"或"域矩阵没配"
        log.error("被中心侧拒绝：%s", exc.rule_id.value)
        log.error("查 deploy/README.md 第 10 节的排查顺序")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
