"""`nxs-cli` 的命令实现。

六个子命令，都是 Agent 真正会用到的：

============  ==========================================================
`whoami`      我是谁：身份牌、能力、约束、配额、批准状态
`nodes`       网络里有谁：可路由的节点与各自能力
`ask`         问一个 Agent 并**等结果**（A2A 的主用法）
`publish`     发任务但不等待（长任务用）
`task`        查某任务的状态与产出
`inbox`       我的信箱：发给我的、进行中的任务
============  ==========================================================

## 一个刻意的设计：`ask` 是"发 + 轮询 + 取结果"

`ask` 不是一个新的协议动作 —— 它把三件已有的动作串起来：
``publish`` → 周期性 ``get_task`` → 拿到 ``completed`` 就返回。

为什么不做成服务端阻塞调用：那会让网关持有连接等 LLM 跑完
（实测 5–10 秒，复杂任务更久），把长连接的资源问题和业务时长
绑在一起。客户端轮询是笨办法，但它让网关保持无状态。

## 输出：默认人可读，`--json` 给机器

Agent 读 ``--json`` 更稳（不用解析排版），人读默认输出更舒服。
**两者都由同一份数据渲染** —— 不各写一套，否则迟早不一致。
"""

from __future__ import annotations

import json
import sys
import time
import uuid

from ..core.errors import ConfigError, PolicyDenied
from ..edge.client import HttpTransport
from .actor import Actor

#: `ask` 的默认最长等待（秒）。
#:
#: 实测单次 LLM 问答 5–10 秒。给到 180 秒是为了容纳"检索 + 归纳"；
#: 再久就该用 `publish` + `task` 两段式，而不是让命令行挂着。
DEFAULT_ASK_TIMEOUT = 180

#: 轮询间隔（秒）。2 秒是刻意的：足够快（人感觉不到明显滞后），
#: 又不至于把网关打出无意义的请求。任务本来就延迟在秒级。
POLL_INTERVAL = 2.0


class CliError(Exception):
    """CLI 层的可预期失败。main 里统一转成非零退出码 + 人话。"""


def _transport(actor: Actor) -> HttpTransport:
    return HttpTransport(actor.url, node_id=actor.node_id,
                         secret=actor.secret, ca_file=actor.ca_file)


def _pp(payload: dict) -> None:
    """打印 JSON（便于人读，键顺序保持稳定）。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------

def cmd_whoami(actor: Actor, args) -> int:
    """我是谁：身份牌、能力、约束、配额、批准状态。

    走 ``GET /nxs/whoami`` —— 服务端直接回我的卡片。
    **不用运维端点**（那要运维密钥，Agent 不该有），
    也不靠读文档推断（会过时，且推错了不报错）。
    """
    t = _transport(actor)
    try:
        info = t.whoami()
    except PolicyDenied as exc:
        from ..core.errors import RuleId
        if exc.rule_id == RuleId.AU_NODE_UNKNOWN:
            raise CliError(
                f"服务端还没有 {actor.node_id!r} 这个节点。\n"
                f"  首次接入请运行桥接进程注册一次"
                f"（examples/hermes_node.py 或 openclaw_node.py）。"
            ) from exc
        raise CliError(f"查询失败：{exc.rule_id.value}") from exc

    if args.json:
        _pp(info)
        return 0

    card = info.get("card") or {}
    usage = info.get("usage") or {}
    print(f"身份　：{info.get('node_id')}")
    print(f"状态　：{info.get('state')}　批准："
          f"{'是' if info.get('approved') else '否（需运维批准）'}")
    print(f"名称　：{card.get('display_name', '')}")
    print(f"域　　：{card.get('domain', '')}")
    desc = card.get("description")
    if desc:
        print(f"简介　：{desc}")

    caps = card.get("capabilities") or []
    if caps:
        print("能力　：")
        for c in caps:
            print(f"  · {c.get('type')}　等级={c.get('level')}"
                  f"　模式={c.get('mode')}")
    else:
        print("能力　：（无 —— 声明了能力才能被派任务）")

    cons = card.get("constraints") or {}
    print(f"接收　：来自 {cons.get('can_receive_from') or '（无）'}")
    print(f"发布　：{cons.get('can_issue_types') or '（无发布权）'}")
    q = cons.get("quota") or {}
    if q:
        print(f"配额　：日 {q.get('tasks_per_day')} 笔"
              f"　并发 {q.get('concurrent_tasks')}")
    if usage:
        print(f"今日　：已用 {usage.get('tasks_today', 0)} 笔"
              f"　进行中 {usage.get('concurrent_now', 0)}")
    return 0


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------

def cmd_nodes(actor: Actor, args) -> int:
    """网络里有谁。

    ⚠ **当前协议的真实限制**：没有节点级的「列全网成员」端点。

    现有的 ``/nxs/ops/nodes`` 需要**运维密钥** —— 把它交给 Agent
    等于让被管理者持有管理钥匙（那个端点同时能批准/吊销节点）。

    所以这个命令目前只能告诉你：**你自己**是谁、能问谁。
    真正的 ``GET /nxs/peers``（节点级、只返回可路由节点的
    能力类型与域名，不暴露简介与配额）还没实现，已记入待办。

    之所以保留这个命令而不是删掉：Agent 的 skill 文档里会写
    "用 nxs-cli nodes 看网络"，如果这个命令不存在，那条指引就是错的。
    现在它存在，且诚实地说出限制。
    """
    t = _transport(actor)
    try:
        info = t.whoami()
    except PolicyDenied as exc:
        raise CliError(f"先确认自己的身份失败：{exc.rule_id.value}") from exc

    peers_note = (
        "当前协议没有节点级的『列全网成员』端点（/nxs/ops/nodes 需要运维密钥）。\n"
        "  待办：新增 GET /nxs/peers —— 只返回可路由节点的能力类型与域名，\n"
        "        不暴露简介、配额等自述信息，这样节点能发现彼此而不看到更多。\n"
        "  过渡做法：直接 ask 一个已知节点 ID（运维会告诉你有哪些）。"
    )
    if args.json:
        _pp({"self": info.get("node_id"),
             "limitation": "no_peers_endpoint",
             "note": peers_note})
        return 0

    print(f"我：{info.get('node_id')}")
    print()
    print(peers_note)
    return 0


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------

def cmd_ask(actor: Actor, args) -> int:
    """问一个 Agent，等它的回答。

    把三件已有动作串起来：``publish`` → 轮询 ``get_task`` → 返回产出。
    **不是**新的协议动作（见模块文档）。
    """
    if not args.to:
        raise CliError("缺少 --to <节点ID>")
    if not args.question:
        raise CliError("缺少问题文本")

    t = _transport(actor)
    key = args.idempotency_key or f"ask-{uuid.uuid4().hex[:12]}"

    try:
        delivery = t.publish(
            from_node=actor.node_id, to_node=args.to,
            task_type=args.type, params={"question": args.question},
            idempotency_key=key,
        )
    except PolicyDenied as exc:
        # 把 rule_id 翻译成"下一步该做什么"，而不是只丢一个编码
        raise CliError(_explain_denial(exc, actor.node_id, args.to)) from exc

    task_id = delivery.task.id
    if not args.json:
        # 先给一个"已经发出去了"的反馈 —— 否则命令行看起来像卡住了
        print(f"已投递给 {args.to}（任务 {task_id}），等待回答…", flush=True)

    deadline = time.monotonic() + args.timeout
    last_status = None
    while time.monotonic() < deadline:
        got = t.get_task(task_id)
        status = got.get("status")
        if status != last_status and not args.json:
            if last_status is not None:
                print(f"  状态：{status}", flush=True)
            last_status = status

        if status == "completed":
            return _emit_answer(actor, got, args)
        if status in ("failed", "cancelled"):
            raise CliError(
                f"任务 {task_id} {status}。\n"
                f"  用 `nxs-cli task {task_id}` 看详情。"
            )
        time.sleep(POLL_INTERVAL)

    raise CliError(
        f"等待超时（{args.timeout} 秒），任务 {task_id} 仍在进行。\n"
        f"  它没有丢 —— 用 `nxs-cli task {task_id}` 稍后取结果，\n"
        f"  或下次用 --timeout 给更长时间。"
    )


def _emit_answer(actor: Actor, task: dict, args) -> int:
    """输出最终回答。"""
    artifacts = task.get("artifacts") or []
    answer = ""
    level = ""
    if artifacts:
        first = artifacts[0] or {}
        data = first.get("data") or {}
        answer = data.get("answer") or json.dumps(data, ensure_ascii=False)
        level = first.get("level") or ""

    if args.json:
        _pp({"task_id": task.get("id"), "from": task.get("from_node"),
             "to": task.get("to_node"), "status": task.get("status"),
             "level": level, "answer": answer,
             "provenance": (artifacts[0] or {}).get("provenance") if artifacts else None})
        return 0

    print()
    print(answer if answer else "（任务完成，但没有可显示的产出）")
    return 0


def _explain_denial(exc: PolicyDenied, me: str, target: str) -> str:
    """把拒绝码翻译成"下一步做什么"。

    ⚠ 只说 rule_id 是不够的：运维/Agent 拿到 ``MX-ISSUER-01``
    并不知道该改哪一处。这里给的是**可执行的下一步**。
    """
    from ..core.errors import RuleId

    rule = exc.rule_id
    head = f"投递被拒：{rule.value}"
    tips = {
        RuleId.MX_ISSUER_NOT_ALLOWED:
            f"你的角色卡没有发布权（issuer=false 或 can_issue_types 不含该类型）。\n"
            f"  需要运维修改 {me} 的角色卡并重新批准。",
        RuleId.MX_DOMAIN_FORBIDDEN:
            f"矩阵里没有从你的域到 {target} 所在域的规则。\n"
            f"  需要运维在 policy.matrix 里加一条。",
        RuleId.MX_RECEIVE_NOT_ALLOWED:
            f"{target} 不接受你所在域的委托，或它当前不可路由（未批准/stale）。\n"
            f"  用 `nxs-cli whoami` 确认自己，找运维确认 {target} 的状态。",
        RuleId.WL_TYPE_NOT_REGISTERED:
            f"没有任何节点提供类型 {target!r} 对应的能力。\n"
            f"  检查 --type 是否拼错。",
        RuleId.QT_TASKS_PER_DAY:
            "今日任务配额用完了。等明天，或让运维调大 quota.tasks_per_day。",
        RuleId.QT_CONCURRENCY:
            f"{target} 的并发已满。稍后重试。",
        RuleId.AU_NODE_UNKNOWN:
            f"{target} 不是已知节点。检查节点 ID。",
    }
    tip = tips.get(rule)
    return f"{head}\n  {tip}" if tip else head


# ---------------------------------------------------------------------------
# publish / task / inbox
# ---------------------------------------------------------------------------

def cmd_publish(actor: Actor, args) -> int:
    """发任务但不等待。适合长任务，之后用 `task` 取结果。"""
    if not args.to:
        raise CliError("缺少 --to <节点ID>")
    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as exc:
        raise CliError(f"--params 不是合法 JSON：{exc}") from exc
    if not isinstance(params, dict):
        raise CliError("--params 必须是一个 JSON 对象")

    t = _transport(actor)
    key = args.idempotency_key or f"pub-{uuid.uuid4().hex[:12]}"
    try:
        delivery = t.publish(from_node=actor.node_id, to_node=args.to,
                             task_type=args.type, params=params,
                             idempotency_key=key)
    except PolicyDenied as exc:
        raise CliError(_explain_denial(exc, actor.node_id, args.to)) from exc

    out = {"task_id": delivery.task.id, "to": args.to,
           "type": args.type, "created": delivery.created,
           "idempotency_key": key}
    if args.json:
        _pp(out)
    else:
        dup = "" if delivery.created else "（幂等命中，复用已有任务）"
        print(f"已投递 {delivery.task.id} → {args.to}{dup}")
        print(f"  取结果：nxs-cli task {delivery.task.id}")
    return 0


def cmd_task(actor: Actor, args) -> int:
    """查任务状态与产出。"""
    if not args.task_id:
        raise CliError("缺少任务 ID")
    t = _transport(actor)
    try:
        got = t.get_task(args.task_id)
    except PolicyDenied as exc:
        from ..core.errors import RuleId
        if exc.rule_id == RuleId.LF_TASK_NOT_FOUND:
            raise CliError(
                f"任务 {args.task_id} 不存在，**或者你看不到它**。\n"
                f"  只有发布者与接收者能查 —— 两个身份回同一个 404，\n"
                f"  所以无法区分「不存在」与「无权」。"
            ) from exc
        raise CliError(f"查询被拒：{exc.rule_id.value}") from exc

    if args.json:
        _pp(got)
        return 0

    print(f"任务　：{got.get('id')}")
    print(f"类型　：{got.get('type')}")
    print(f"状态　：{got.get('status')}")
    print(f"流向　：{got.get('from_node')} → {got.get('to_node')}")
    arts = got.get("artifacts") or []
    if arts:
        print("产出　：")
        for a in arts:
            print(f"  等级={a.get('level')} 类型={a.get('kind')}")
            data = a.get("data") or {}
            text = data.get("answer") or json.dumps(data, ensure_ascii=False)
            for line in str(text).splitlines():
                print(f"    {line}")
    else:
        print("产出　：（还没有）")
    return 0


def cmd_inbox(actor: Actor, args) -> int:
    """我的信箱 —— 当前协议下能查到的部分。

    ⚠ **诚实的限制**：协议里没有「列出我的任务」的节点级端点。
    `/poll` 是领任务（会改变状态），不能用来"看"。
    所以这个命令只能：
      · 报告本进程有资格领取的类型
      · 提示用 `task <id>` 查已知任务

    真正需要的是一个只读的 `GET /nxs/inbox`。已记入待办。
    """
    msg = (
        f"身份：{actor.node_id}\n"
        f"当前协议没有节点级的『列出我的任务』端点。\n"
        f"  /poll 是领取（会改变任务状态），不能用来查看。\n"
        f"  已知任务可以用：nxs-cli task <任务ID>\n"
        f"  待办：新增 GET /nxs/inbox（只读、按 to_node 过滤）"
    )
    if args.json:
        _pp({"node_id": actor.node_id, "limitation": "no_inbox_endpoint"})
    else:
        print(msg)
    return 0


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------

COMMANDS = {
    "whoami": cmd_whoami,
    "ask": cmd_ask,
    "publish": cmd_publish,
    "task": cmd_task,
    "inbox": cmd_inbox,
}
