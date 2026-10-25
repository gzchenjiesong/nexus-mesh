"""注册中心（C1）—— 节点身份与能力的唯一权威。

职责
----
维护角色卡、能力索引、心跳与存活状态。回答"有谁在、各自擅长什么"。

三条设计约束
------------
1. **新节点不能自行加入。** 注册后 ``approved=False``，需人工批准
   （配置里的域白名单，或运维接口）。这与"已知的少数节点 + 预配置信任"
   这个前提一致。
2. **stale 不删除，只标记。** 查不到历史状态就没法回答"当时它在线吗"。
3. **能力索引来自配置，不来自代码。** 节点集合变化不应触发任何代码修改。

⭐ 两个注册入口，语义必须分开
------------------------------
============================  ==========================================
方法                          来源与信任
============================  ==========================================
:meth:`register`             **可信**：配置文件或运维动作
:meth:`register_from_node`   **不可信**：节点经 HTTP 自报
============================  ==========================================

分开的理由是一个具体的提权路径：v0.1 的 ``register`` 里写着
``approved = existing.approved or approved``——**已批准的节点重发一次
注册就能改自己的角色卡而保留批准**。在"只有运维能改配置"的年代这没问题，
但一旦开放自注册，任何已批准节点都能把 ``max_response_level``
从 summary 抬到 full_text，一次 HTTP 请求完成提权。

所以：**节点自注册永远不带来批准；卡片一有变化，批准立即撤销。**
审批是对"这张卡片"的批准，不是对这个节点 ID 的终身授权。
"""

from __future__ import annotations

from dataclasses import replace

from ..core.errors import PolicyDenied, RuleId
from ..core.models import RoleCard
from ..core.models import Level
from ..ports.stores import NodeRecord, NodeState, RegistryStore
from ..core.envelope import now_iso, parse_iso

#: :meth:`Registry.register_from_node` 的三种结果。
REGISTER_CREATED = "created"          # 首次注册，待批准
REGISTER_UNCHANGED = "unchanged"      # 卡片未变，仅刷新心跳
REGISTER_CARD_CHANGED = "card_changed"  # 卡片变了，批准已撤销


class Registry:
    """注册中心。

    :param store: 存储后端。本类不含任何存储细节。
    :param stale_after_factor: 心跳间隔的倍数，超过即标记 stale。
        取 3 是有意的偏保守值——误标 stale 会让任务暂时无法投递（可恢复），
        而误判 alive 会把任务投给已离线的节点（不可恢复）。
    """

    def __init__(self, store: RegistryStore, *, stale_after_factor: int = 3) -> None:
        self._store = store
        self._stale_factor = stale_after_factor

    # ------------------------------------------------------------------
    # 注册与批准
    # ------------------------------------------------------------------

    def register(self, card: RoleCard, *, approved: bool = False) -> NodeRecord:
        """注册或更新一个节点 —— **可信来源**（配置 / 运维动作）。

        与 :meth:`register_from_node` 的区别仅在"卡片变化时怎么处理批准"：
        这里是运维改的，按运维给的 ``approved`` 走；那里是节点自己说的，
        一律撤销批准。

        **不要用本方法处理 HTTP 入站的注册请求** —— 那会让节点能自行提权。
        """
        existing = self._store.get(card.node_id)
        if existing is None:
            record = NodeRecord(
                card=card, state=NodeState.ALIVE, last_seen=now_iso(),
                revision=1, approved=approved,
            )
        else:
            card_changed = existing.card != card
            record = replace(
                existing,
                card=card,
                state=NodeState.ALIVE,
                last_seen=now_iso(),
                revision=existing.revision + 1 if card_changed else existing.revision,
                approved=approved if card_changed else (existing.approved or approved),
            )
        self._store.put(record)
        return record

    def register_from_node(self, card: RoleCard) -> tuple[NodeRecord, str]:
        """节点自注册 —— **不可信来源**。返回 ``(记录, 结果)``。

        三条规则：

        * **首次注册**：``approved=False``，等运维批准
        * **卡片未变**：只刷新心跳与存活状态，批准状态不动
          （这让节点可以频繁上报，不必担心掉批准）
        * **卡片有变化**：``approved=False`` —— **即使该节点原本已批准**

        第三条是安全的支点：审批的对象是**那张卡片**，不是那个 node_id。
        节点想扩大自己的能力或数据等级，就得重新走一次审批。

        为什么不判断"这次变更是收紧还是放宽"：那需要一套权限偏序比较，
        而任何比较逻辑写错都直接变成提权漏洞。**宁可让"自我降级"也走一次
        审批，也不留一个可能判错的自动放行。** 换取的是实现上不可能出错。
        """
        existing = self._store.get(card.node_id)

        if existing is None:
            record = NodeRecord(
                card=card, state=NodeState.ALIVE, last_seen=now_iso(),
                revision=1, approved=False,     # ★ 自注册永不带来批准
            )
            self._store.put(record)
            return record, REGISTER_CREATED

        if existing.card == card:
            # 卡片没变：按心跳处理。保留批准状态与 revision。
            record = replace(
                existing, state=NodeState.ALIVE, last_seen=now_iso(),
            )
            self._store.put(record)
            return record, REGISTER_UNCHANGED

        # 卡片变了：撤销批准，revision 前进
        record = replace(
            existing,
            card=card,
            state=NodeState.ALIVE,
            last_seen=now_iso(),
            revision=existing.revision + 1,
            approved=False,                 # ★ 关键：不继承旧批准
        )
        self._store.put(record)
        return record, REGISTER_CARD_CHANGED

    def approve(self, node_id: str) -> NodeRecord:
        """人工批准一个节点接入。"""
        rec = self._store.get(node_id)
        if rec is None:
            raise PolicyDenied(RuleId.AU_NODE_UNKNOWN, f"approve unknown {node_id}")
        new = replace(rec, approved=True, state=NodeState.ALIVE, last_seen=now_iso())
        self._store.put(new)
        return new

    def revoke(self, node_id: str) -> NodeRecord:
        """吊销节点。

        **不删除记录**——吊销之后仍然需要能回答"它当时在不在、被吊销于何时"。
        状态置 ``REVOKED`` 后立即不再参与路由。
        """
        rec = self._store.get(node_id)
        if rec is None:
            raise PolicyDenied(RuleId.AU_NODE_UNKNOWN, f"revoke unknown {node_id}")
        new = replace(rec, state=NodeState.REVOKED, approved=False)
        self._store.put(new)
        return new

    # ------------------------------------------------------------------
    # 心跳与存活
    # ------------------------------------------------------------------

    def heartbeat(self, node_id: str) -> None:
        """记录心跳。未知节点静默忽略（不报错，避免成为枚举接口）。"""
        self._store.touch(node_id, last_seen=now_iso())

    def sweep(self, *, now: str | None = None) -> list[str]:
        """把心跳超时的节点标记为 stale，返回被标记的节点列表。

        由调用方定期触发（例如每次 poll 之前）。**只标记不删除。**
        """
        ref = parse_iso(now) if now else parse_iso(now_iso())
        marked: list[str] = []
        for rec in self._store.list_all():
            if rec.state != NodeState.ALIVE or not rec.last_seen:
                continue
            deadline = rec.card.heartbeat_interval * self._stale_factor
            if (ref - parse_iso(rec.last_seen)).total_seconds() > deadline:
                self._store.put(replace(rec, state=NodeState.STALE))
                marked.append(rec.card.node_id)
        return marked

    def resurrect(self, node_id: str) -> None:
        """把一个 stale 节点恢复为 alive（收到新心跳时调用）。"""
        rec = self._store.get(node_id)
        if rec is not None and rec.state == NodeState.STALE:
            self._store.put(replace(rec, state=NodeState.ALIVE, last_seen=now_iso()))

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def record(self, node_id: str) -> NodeRecord | None:
        return self._store.get(node_id)

    def card(self, node_id: str) -> RoleCard | None:
        rec = self._store.get(node_id)
        return rec.card if rec else None

    def require_card(self, node_id: str) -> RoleCard:
        rec = self._store.get(node_id)
        if rec is None:
            raise PolicyDenied(RuleId.AU_NODE_UNKNOWN)
        return rec.card

    def require_routable(self, node_id: str) -> RoleCard:
        """要求节点可路由（已批准 + alive）。"""
        rec = self._store.get(node_id)
        if rec is None:
            raise PolicyDenied(RuleId.AU_NODE_UNKNOWN)
        if rec.state == NodeState.REVOKED:
            raise PolicyDenied(RuleId.AU_CREDENTIAL_REVOKED)
        if not rec.routable:
            raise PolicyDenied(RuleId.MX_RECEIVE_NOT_ALLOWED,
                               f"{node_id} state={rec.state} approved={rec.approved}")
        return rec.card

    def all_records(self) -> tuple[NodeRecord, ...]:
        return tuple(self._store.list_all())

    def all_cards(self) -> tuple[RoleCard, ...]:
        """全部角色卡（含未批准）。

        策略层的类型索引需要它——**不能只枚举配置里的节点**，
        否则自注册节点提供的能力在策略层不可见，投递会被判为
        ``WL-TYPE-404``（"类型未注册"），而实际上它就在注册表里。
        """
        return tuple(rec.card for rec in self._store.list_all())

    def capability_index(self) -> dict[str, tuple[str, ...]]:
        """能力 → 可路由的提供者列表。

        **只返回可路由的（已批准 + alive）**，这样匹配器不需要再判一次状态。
        """
        idx: dict[str, list[str]] = {}
        for rec in self._store.list_all():
            if not rec.routable:
                continue
            for cap in rec.card.capabilities:
                idx.setdefault(cap.type, []).append(rec.card.node_id)
        return {k: tuple(sorted(v)) for k, v in sorted(idx.items())}

    def describe(self) -> str:
        """人类可读的节点列表。**不含标题**——标题由调用方给。"""
        lines: list[str] = []
        for rec in sorted(self._store.list_all(), key=lambda r: r.card.node_id):
            c = rec.card
            flags = []
            if not rec.approved:
                flags.append("未批准")
            if rec.state != NodeState.ALIVE:
                flags.append(rec.state)
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(
                f"  · {c.node_id:<12} domain={c.domain:<8} "
                f"state={rec.state:<8} caps={len(c.capabilities)}"
                f" max_level={c.constraints.max_response_level}{suffix}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 便捷构造：从配置初始化（供 demo 与测试使用）
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config, store: RegistryStore, *,
                    approved_domains: tuple[str, ...] | None = None) -> "Registry":
        """用配置里的节点初始化注册中心。

        :param approved_domains: 自动批准的信任域。
            **省略时取配置里的 ``policy.auto_approve_domains``（默认为空）。**

        省略时走配置是刻意的：把具体的域名写在代码默认值里，
        就等于"换个环境要改代码"——那违反本项目的核心纪律。
        """
        if approved_domains is None:
            approved_domains = config.policy.auto_approve_domains
        reg = cls(store)
        for card in config.nodes.values():
            reg.register(card, approved=card.domain in approved_domains)
        return reg


__all__ = ["Registry"]
