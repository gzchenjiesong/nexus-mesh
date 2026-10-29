"""配置中心（M1）。

一切与"节点是谁、能做什么"相关的判断都必须来自这里加载的数据，
**代码里不得出现任何节点名称或任务类型的特判**。这是本系统能否称为
"独立基础设施"的判据：如果加一个节点需要改代码，它就不是基础设施。

设计约束
--------
* **配置非法即拒绝启动** —— 带着坏配置运行比启动失败危险得多
* **拒绝未知字段** —— 拼写错误被静默忽略会制造"配置看起来生效"的假象
* **凭据不落库** —— secret 优先从环境变量读取，示例文件只放占位符
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..core.errors import ConfigError
from ..core.models import (
    Level,
    RoleCard,
    _as_str_list,
    _reject_unknown,
    _require,
)

#: 凭据对应的环境变量前缀：``NXS_SECRET_<NODE_ID 大写，非字母数字转下划线>``
ENV_SECRET_PREFIX = "NXS_SECRET_"

MIN_SECRET_LENGTH = 16

#: 运维身份的保留 ID。**真实节点不可占用它。**
#:
#: 运维接口（批准/吊销/释放租约）用这个身份签名，凭据来自 ``NXS_OPS_SECRET``。
#: 用双下划线包围是刻意的：它不像任何正常的节点命名习惯，不会与真实节点混淆。
OPS_NODE_ID = "__ops__"

#: 运维密钥的环境变量名。**未设置时运维接口整体关闭**（默认无攻击面）。
ENV_OPS_SECRET = "NXS_OPS_SECRET"

#: 允许的存储后端
QUEUE_BACKENDS = ("memory", "sqlite")
AUDIT_BACKENDS = ("file",)


def _node_env_var(node_id: str) -> str:
    """节点 ID → 凭据环境变量名。拼法必须与 :func:`demo_env_for` 一致。"""
    safe = "".join(ch.upper() if ch.isalnum() else "_" for ch in node_id)
    return f"{ENV_SECRET_PREFIX}{safe}"


# ---------------------------------------------------------------------------
# 分节配置
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8443
    #: 请求体上限。应答本身另有按节点的字节配额，这里只是传输层的兜底。
    max_body_bytes: int = 256 * 1024
    #: TLS 证书路径。为 None 时以明文 HTTP 运行（仅限本地 demo 与测试）。
    tls_cert: str | None = None
    tls_key: str | None = None
    #: 是否启用 SSE 通知流。关掉也不影响功能——节点退回固定周期轮询。
    sse_enabled: bool = True
    #: SSE 心跳间隔（秒）。中间设备通常 30~60 秒空闲就断，取更小的值更稳。
    sse_keepalive_seconds: int = 15
    #: 常规请求并发上限。**不含** SSE 流——那条单独计。
    max_concurrent_requests: int = 64
    #: SSE 流上限。同一节点重连时旧流会被要求退出，不会无限堆积。
    max_sse_streams: int = 16

    _FIELDS = ("host", "port", "max_body_bytes", "tls_cert", "tls_key",
               "sse_enabled", "sse_keepalive_seconds",
               "max_concurrent_requests", "max_sse_streams")

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any] | None) -> "ServerConfig":
        obj = obj or {}
        _reject_unknown(obj, cls._FIELDS, "server")
        port = obj.get("port", 8443)
        # port=0 表示"由操作系统分配"（测试与 demo 用，避免抢固定端口）
        if not isinstance(port, int) or isinstance(port, bool) or not (0 <= port <= 65535):
            raise ConfigError(f"server.port: 非法端口 {port!r}")
        max_body = obj.get("max_body_bytes", 256 * 1024)
        if not isinstance(max_body, int) or max_body <= 0:
            raise ConfigError("server.max_body_bytes: 必须是正整数")
        cert, key = obj.get("tls_cert"), obj.get("tls_key")
        if (cert is None) != (key is None):
            raise ConfigError("server: tls_cert 与 tls_key 必须同时提供或同时省略")

        sse_enabled = obj.get("sse_enabled", True)
        if not isinstance(sse_enabled, bool):
            raise ConfigError("server.sse_enabled: 必须是布尔值")

        keepalive = obj.get("sse_keepalive_seconds", 15)
        if not isinstance(keepalive, int) or isinstance(keepalive, bool) \
                or not (1 <= keepalive <= 60):
            raise ConfigError("server.sse_keepalive_seconds: 必须在 1..60 之间")

        max_req = obj.get("max_concurrent_requests", 64)
        if not isinstance(max_req, int) or isinstance(max_req, bool) or max_req <= 0:
            raise ConfigError("server.max_concurrent_requests: 必须是正整数")

        max_streams = obj.get("max_sse_streams", 16)
        if not isinstance(max_streams, int) or isinstance(max_streams, bool) \
                or max_streams <= 0:
            raise ConfigError("server.max_sse_streams: 必须是正整数")

        return cls(
            host=str(obj.get("host", "127.0.0.1")),
            port=port,
            max_body_bytes=max_body,
            tls_cert=cert,
            tls_key=key,
            sse_enabled=sse_enabled,
            sse_keepalive_seconds=keepalive,
            max_concurrent_requests=max_req,
            max_sse_streams=max_streams,
        )


@dataclass(frozen=True)
class StoreConfig:
    queue: str = "memory"
    registry: str = "memory"
    audit: str = "file"
    #: 所有运行时可写状态（数据库、审计日志）的根目录。
    #: 该目录已在 .gitignore 中排除。
    data_dir: str = ".nxs"

    _FIELDS = ("queue", "registry", "audit", "data_dir")

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any] | None) -> "StoreConfig":
        obj = obj or {}
        _reject_unknown(obj, cls._FIELDS, "stores")
        queue = str(obj.get("queue", "memory"))
        registry = str(obj.get("registry", "memory"))
        audit = str(obj.get("audit", "file"))
        if queue not in QUEUE_BACKENDS:
            raise ConfigError(f"stores.queue: 未知后端 {queue!r}，可选 {QUEUE_BACKENDS}")
        if registry not in QUEUE_BACKENDS:
            raise ConfigError(f"stores.registry: 未知后端 {registry!r}，可选 {QUEUE_BACKENDS}")
        if audit not in AUDIT_BACKENDS:
            raise ConfigError(f"stores.audit: 未知后端 {audit!r}，可选 {AUDIT_BACKENDS}")
        return cls(
            queue=queue, registry=registry, audit=audit,
            data_dir=str(obj.get("data_dir", ".nxs")),
        )


@dataclass(frozen=True)
class MatrixRule:
    """域间可达性的一条规则。

    ## 权限模型改造后它只管一件事

    旧版这条规则同时管两件事：**这对域之间能不能通**、**能通哪些任务类型**、
    **上限多细**。三个问题缠在一个配置项里，出问题时无法判断是哪一侧设错了。

    现在它只回答第一个：**这批数据能被谁触达。**

        类型权限 → 由 role 决定（Step 3 已切）
        应答上限 → 由 role + 节点自限取 min（本步切换）
        可达性   → 本规则

    ## 代价（必须知晓）

    被批准进某个域的 driver，可以向目标域发**任意已定义类型**的任务。

    控制面从「域 × 类型」两级收成「域 + 角色」两级 —— 仍然是两条独立约束，
    只是换了维度：域管数据围栏，角色管类型权限。

    ⚠ 这个代价意味着：**角色数量少是这套模型的前提。**
    角色超过 5 个、或某个域内的 driver 超过 5 个，就该重新审视。
    """

    from_domain: str
    to_domain: str

    _FIELDS = ("from_domain", "to_domain")

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any], where: str) -> "MatrixRule":
        if not isinstance(obj, Mapping):
            raise ConfigError(f"{where}: 期望映射")

        # ⚠ 旧字段被显式拒绝，并指向正确位置 ——
        #   静默忽略会让人以为配了 type 限制而实际没有，那是安全误解。
        legacy = [k for k in ("allow_types", "max_level") if k in obj]
        if legacy:
            raise ConfigError(
                f"{where}: 字段 {legacy} 已被移除。\n"
                f"  域矩阵现在只表达可达性（from_domain → to_domain）。\n"
                f"  · 类型权限 → 由 role.serves 决定\n"
                f"  · 应答上限 → 由 role.max_response_level 与节点 self_limits 取 min"
            )

        _reject_unknown(obj, cls._FIELDS, where)
        return cls(
            from_domain=str(_require(obj, "from_domain", where, (str,))),
            to_domain=str(_require(obj, "to_domain", where, (str,))),
        )


@dataclass(frozen=True)
class PolicyConfig:
    rules: tuple[MatrixRule, ...] = ()
    #: 单节点速率上限（每秒请求数）。0 表示不限。
    rate_limit_per_second: int = 20
    #: **启动时自动批准的信任域。默认空 = 不批准任何人。**
    #:
    #: 为什么是配置项而不是代码里的默认值：把具体域名写进基础设施代码
    #: 就等于"加一个节点要改代码"。放在配置里，节点集合完全由数据决定。
    #:
    #: 默认空也符合"默认拒绝"——新节点必须被显式批准才能接入。
    auto_approve_domains: tuple[str, ...] = ()

    _FIELDS = ("matrix", "rate_limit_per_second", "auto_approve_domains")

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any] | None) -> "PolicyConfig":
        obj = obj or {}
        _reject_unknown(obj, cls._FIELDS, "policy")
        raw = obj.get("matrix") or []
        if not isinstance(raw, (list, tuple)):
            raise ConfigError("policy.matrix: 期望列表")
        rules = tuple(
            MatrixRule.from_dict(r, f"policy.matrix[{i}]") for i, r in enumerate(raw)
        )
        rate = obj.get("rate_limit_per_second", 20)
        if not isinstance(rate, int) or isinstance(rate, bool) or rate < 0:
            raise ConfigError("policy.rate_limit_per_second: 必须是非负整数")
        return cls(
            rules=rules,
            rate_limit_per_second=rate,
            auto_approve_domains=_as_str_list(
                obj.get("auto_approve_domains"), "policy.auto_approve_domains"),
        )

    def find(self, from_domain: str, to_domain: str) -> MatrixRule | None:
        """查找生效规则。**找不到即拒绝**——默认拒绝在此落地。"""
        for r in self.rules:
            if r.from_domain == from_domain and r.to_domain == to_domain:
                return r
        return None


# ---------------------------------------------------------------------------
# 总配置
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = "config/local.yaml"

#: 回退用的示例配置。``DEFAULT_CONFIG_PATH`` 不存在时使用它——
#: 这样 clone 下来就能直接跑 ``nxs verify`` / ``nxs demo``，不必先建 local.yaml。
EXAMPLE_CONFIG_PATH = "config/nxs.example.yaml"

#: **已配置但尚未实现**的项：(字段名, 说明)。
#:
#: 这些配置会被正常解析与校验，但不会产生任何行为。沉默地接受它们是危险的——
#: 使用者会以为某项防护已生效。因此：
#:   1. ``describe()`` 会把它们打印出来
#:   2. ``tests/test_architecture.py`` 会核对它们与本清单一致
#:   3. ``docs/architecture.md`` 有对应的「已知缺口」一节
#:
#: **新增配置项时，要么实现它，要么加到这里。**
UNIMPLEMENTED: tuple[tuple[str, str], ...] = (
    ("params_schema",
     "参数 schema 只声明、未校验；格式错误的参数由执行节点自己承担失败。"
     "⚠ Step 6 会真正校验它（TaskType 全局定义后，校验方与定义方是同一个）"),
    ("TypeDecl.delivery",
     "**权限模型改造遗留**：广播（broadcast）的展开逻辑未实现，"
     "当前所有任务都按单播处理。等 group/广播能力落地后接线"),
)


@dataclass(frozen=True)
class DomainDecl:
    """一个信任域的声明。

    ⭐ **信任域是第一类实体，不是从节点卡片推导出来的附庸。**

    为什么必须显式声明：矩阵的维度是「域 × 类型」，而工作电脑上的节点
    **在协调节点启动时还不存在**（它靠自注册接入）。如果域只能从节点推导，
    生产配置里就写不出 `coordinator → zone-work` 这条规则——启动时校验会报
    "引用了不存在的域"。

    顺带解决另一个问题：节点自报的域必须属于已声明的域之一。
    否则它可以自创一个域名，让运维在批准时看着一个陌生的域无从判断。
    """

    name: str
    description: str = ""


@dataclass(frozen=True)
class TypeDecl:
    """一个任务类型的**全局定义**。

    ⭐ **这是权限模型改造的地基：任务类型必须只有一个定义方。**

    旧模型里类型是从各节点卡片的 `capabilities` 汇总出来的，于是同一个
    `work.query` 可以由两个节点声明成两套契约（不同的 `params_schema`、
    不同的 `response_fields`），而系统不报错 —— 因为"谁说得对"没有答案。

    实测确认过这个漏洞：构造两张都声明 `work.query` 但 `response_fields`
    不同的卡片，两个都能成功注册。

    现在类型由运维定义一次，节点只能**引用**（通过角色）。一个名字只有一种含义。
    """

    name: str
    #: 交付模式。当前只支持 unicast / broadcast。
    #: multicast（群组多播）等 group 做了再加 —— 不预留用不了的枚举值。
    delivery: str = "unicast"
    #: 该类型承诺的产出等级
    max_level: Level = Level.SUMMARY
    #: 应答字段白名单。空元组表示"该类型不限制字段"（仅对 demo 类型宽容）
    response_fields: tuple[str, ...] = ()
    #: 自由文本字段（提示注入的主要载体，两边都会清洗）
    freetext_fields: tuple[str, ...] = ()
    #: 参数 schema。当前仅声明，Step 6 才真正校验
    params_schema: dict[str, str] = field(default_factory=dict)
    description: str = ""

    _FIELDS = ("name", "delivery", "max_level", "response_fields",
               "freetext_fields", "params_schema", "description")

    #: 支持的交付模式。**刻意只有两个** —— multicast 等 group 真做了再加，
    #: 否则又是一个"枚举里有、但用不了"的值。
    DELIVERY_MODES = ("unicast", "broadcast")

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any], where: str) -> "TypeDecl":
        if not isinstance(obj, Mapping):
            raise ConfigError(f"{where}: 期望映射")
        _reject_unknown(obj, cls._FIELDS, where)

        name = str(_require(obj, "name", where, (str,)))
        # 命名约定 <verb>.<object>，与 RoleCard 的 capability 校验保持一致
        if not name or "." not in name:
            raise ConfigError(
                f"{where}.name: '{name}' 不符合 <verb>.<object> 命名约定"
                f"（如 work.query）"
            )

        delivery = str(obj.get("delivery", "unicast")).strip().lower()
        if delivery not in cls.DELIVERY_MODES:
            raise ConfigError(
                f"{where}.delivery: '{delivery}' 不受支持；"
                f"可选 {list(cls.DELIVERY_MODES)}"
                + ("（multicast 等 group 能力落地后再加）"
                   if delivery == "multicast" else "")
            )

        fields = obj.get("response_fields") or []
        if not isinstance(fields, (list, tuple)):
            raise ConfigError(f"{where}.response_fields: 期望列表")
        ft = obj.get("freetext_fields") or []
        if not isinstance(ft, (list, tuple)):
            raise ConfigError(f"{where}.freetext_fields: 期望列表")

        freetext = tuple(str(x) for x in ft)
        rf = tuple(str(x) for x in fields)
        # 自由文本字段必须是应答字段的子集 —— 否则声明了一个永远不可能出现的字段
        extra = [x for x in freetext if rf and x not in rf]
        if extra:
            raise ConfigError(
                f"{where}.freetext_fields: {extra} 不在 response_fields 里"
            )

        schema = obj.get("params_schema") or {}
        if not isinstance(schema, Mapping):
            raise ConfigError(f"{where}.params_schema: 期望映射")

        return cls(
            name=name,
            delivery=delivery,
            max_level=Level.parse(obj.get("max_level", "summary"),
                                  f"{where}.max_level"),
            response_fields=rf,
            freetext_fields=freetext,
            params_schema=dict(schema),
            description=str(obj.get("description", "")),
        )

    def serves(self, task_type: str) -> bool:
        """是否就是这个类型（类型定义本身不做通配匹配）。"""
        return self.name == task_type


@dataclass(frozen=True)
class RoleDecl:
    """一个**角色**：能力与权限的预设组合。

    ⭐ **Agent 不逐项申请权限，而是从这些角色里选一个。**

    审批只需回答一个问题：允许这个 Agent 以这个角色加入这个域吗。

    与旧模型的关键区别：旧模型把权限拆成 5 个布尔标志 + 逐类型白名单，
    让每个节点自己拼装。实测下来 5 个标志里有 4 个从不参与判定，
    而某"内网执行者"被批出了发布权却没人回头看过。
    """

    name: str
    #: 能否发布任务。这是**唯一**需要审批的能力（其他都是节点自律）。
    can_issue: bool = False
    #: 能服务哪些任务类型。支持 `前缀.*` 通配。
    serves: tuple[str, ...] = ()
    #: 该类角色的节点最多能产出多细的内容（与节点自限取 min）
    max_response_level: Level = Level.SUMMARY
    description: str = ""

    _FIELDS = ("name", "can_issue", "serves", "max_response_level", "description")

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any], where: str) -> "RoleDecl":
        if not isinstance(obj, Mapping):
            raise ConfigError(f"{where}: 期望映射")
        _reject_unknown(obj, cls._FIELDS, where)

        name = str(_require(obj, "name", where, (str,)))
        if not name.strip():
            raise ConfigError(f"{where}.name: 不能为空")

        raw_serves = obj.get("serves") or []
        if not isinstance(raw_serves, (list, tuple)):
            raise ConfigError(f"{where}.serves: 期望列表")
        serves: list[str] = []
        for s in raw_serves:
            if not isinstance(s, str) or not s.strip():
                raise ConfigError(f"{where}.serves: 元素必须是非空字符串")
            if s.strip() == "*":
                # 与矩阵同理：裸通配等于取消默认拒绝
                raise ConfigError(
                    f"{where}.serves: 不允许裸通配 '*'；"
                    f"请写成 '<前缀>.*' 显式限定范围"
                )
            serves.append(s.strip())

        can_issue = bool(obj.get("can_issue", False))

        # ⭐ 空角色校验：既不能发布、又不服务任何类型 —— 那它什么都做不了。
        #
        # 这不是"理论上不该有"，而是配置时很容易写出来的东西：
        # 想定义一个 publisher，忘了写 can_issue: true，于是得到一个
        # 既不能发也不能收的角色。批准这样的角色会让运维以为放行了权限，
        # 实际什么都没放。
        if not can_issue and not serves:
            raise ConfigError(
                f"{where} ('{name.strip()}'): 这个角色既不能发布任务"
                f"（can_issue=false）又不服务任何类型（serves 为空）—— "
                f"它什么都做不了。\n"
                f"  · 只执行的节点：写 serves: [类型]\n"
                f"  · 只发布的角色：写 can_issue: true\n"
            )

        return cls(
            name=name.strip(),
            can_issue=can_issue,
            serves=tuple(serves),
            max_response_level=Level.parse(
                obj.get("max_response_level", "summary"),
                f"{where}.max_response_level"),
            description=str(obj.get("description", "")),
        )

    def serves_type(self, task_type: str) -> bool:
        """能否服务某个任务类型。`前缀.*` 通配，保留结尾的点避免误匹配。"""
        for p in self.serves:
            if p.endswith(".*"):
                if task_type.startswith(p[:-1]):
                    return True
            elif task_type == p:
                return True
        return False


@dataclass
class NexusConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    stores: StoreConfig = field(default_factory=StoreConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    #: 信任域声明，按名字索引。为空时退化为"从节点卡片推导"（向后兼容）。
    domains: dict[str, DomainDecl] = field(default_factory=dict)
    #: 任务类型的**全局定义**，按名字索引。
    #:
    #: ⚠ 改造中：Step 1 只做解析与静态校验，判定逻辑仍走旧的
    #: 「从节点 capabilities 汇总」。等 Step 3/4 切过去后，这份定义
    #: 才是唯一权威。
    #:
    #: 为空表示"尚未迁移"——旧配置照常工作。
    types: dict[str, TypeDecl] = field(default_factory=dict)
    #: 角色定义，按名字索引。同样为空表示尚未迁移。
    roles: dict[str, RoleDecl] = field(default_factory=dict)
    #: 节点角色卡，按 node_id 索引
    nodes: dict[str, RoleCard] = field(default_factory=dict)
    #: 节点凭据，按 node_id 索引。**只存在于内存，不写入任何日志。**
    secrets: dict[str, bytes] = field(default_factory=dict)
    # ⚠ `local_node` 已删除（权限模型改造 Step 5）。
    #
    #   **Mesh 不再是网络里的一个节点，它只提供网络能力。**
    #
    #   旧模型的困境：协调节点是"本进程内的节点"，没有任何东西会给它发心跳，
    #   于是启动 3 个心跳周期后必然被判 stale。为此加了这个配置项让它自己续命。
    #
    #   **那个 hack 的存在本身就是"mesh 不该是节点"的证据** ——
    #   一个需要专门写配置、写续命逻辑、还要在文档里解释半天的东西，
    #   说明它的定位错了。
    #
    #   现在：配置里的每个节点都是**外部节点**，必须自己心跳。
    #   没人给 coordinator 心跳，它就变 stale —— 这是**正确**的，
    #   因为它确实没人在跑。
    source_path: str = ""

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------

    def known_domains(self) -> set[str]:
        """全部已知信任域。

        声明了 ``domains`` 就用声明；否则从节点卡片推导（v0.1 的旧行为）。
        """
        if self.domains:
            return set(self.domains)
        return {c.domain for c in self.nodes.values()}

    def domain_of(self, node_id: str) -> str | None:
        card = self.nodes.get(node_id)
        return card.domain if card else None

    def nodes_in_domain(self, domain: str) -> list[RoleCard]:
        return [c for c in self.nodes.values() if c.domain == domain]

    def task_types(self) -> dict[str, tuple[str, ...]]:
        """全部已注册的任务类型 → (提供它的节点列表)。"""
        out: dict[str, list[str]] = {}
        for card in self.nodes.values():
            for cap in card.capabilities:
                out.setdefault(cap.type, []).append(card.node_id)
        return {k: tuple(sorted(v)) for k, v in sorted(out.items())}

    def describe(self) -> str:
        """人类可读的配置摘要（不含凭据）。

        **刻意不声称未实现的功能。** 早先这里会打印 `[TLS]` 与
        "速率上限 20/秒"，但两者当时都没有接入逻辑——那样的输出会
        给人"这项防护已生效"的错觉，比不打印更危险。

        现在的做法：只报告真实生效的配置，并在末尾列出**已知未实现项**。
        """
        lines = [
            f"配置来源      : {self.source_path or '(内置默认)'}",
            f"监听          : {self.server.host}:{self.server.port}",
            f"存储后端      : queue={self.stores.queue} "
            f"registry={self.stores.registry} audit={self.stores.audit}",
            f"数据目录      : {self.stores.data_dir}",
            f"矩阵规则数    : {len(self.policy.rules)}",
            "",
            f"节点（{len(self.nodes)}）：",
        ]
        for mid in sorted(self.nodes):
            c = self.nodes[mid]
            lines.append(
                f"  · {mid:<12} domain={c.domain:<8} role={c.role:<10} "
                f"caps={[x.type for x in c.capabilities]} "
                f"max_level={c.effective_max_level()}"
            )
        types = self.task_types()
        lines.append("")
        lines.append(f"已注册任务类型（{len(types)}）：")
        for t, providers in types.items():
            lines.append(f"  · {t:<22} ← {', '.join(providers)}")

        # 已知未实现项：**必须显式列出**。
        # 这些配置项会被解析、会被校验，但不会有任何行为——
        # 沉默地接受它们等于让使用者以为自己获得了某种控制。
        lines.append("")
        lines.append("⚠ 已配置但尚未实现（见 docs/architecture.md「已知缺口」）：")
        for name, why in UNIMPLEMENTED:
            lines.append(f"  · {name:<22} {why}")
        return "\n".join(lines)


def _parse_domains(obj: Any) -> dict[str, DomainDecl]:
    """解析 ``domains`` 段。省略时返回空字典（= 旧式配置，从节点推导域）。"""
    if obj is None:
        return {}
    if not isinstance(obj, (list, tuple)):
        raise ConfigError("domains: 期望列表")
    out: dict[str, DomainDecl] = {}
    for i, entry in enumerate(obj):
        where = f"domains[{i}]"
        if not isinstance(entry, Mapping):
            raise ConfigError(f"{where}: 期望映射")
        _reject_unknown(entry, ("name", "description"), where)
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{where}.name: 必须是非空字符串")
        if name in out:
            raise ConfigError(f"{where}: 域 '{name}' 重复声明")
        out[name] = DomainDecl(
            name=name, description=str(entry.get("description", "")),
        )
    if not out:
        raise ConfigError("domains: 声明了但不能为空列表（要么省略，要么至少一个域）")
    return out



def _parse_types(obj: Any, *, source: str) -> dict[str, TypeDecl]:
    """解析 ``types:`` 节。省略时返回空 dict（= 尚未迁移）。"""
    if obj is None:
        return {}
    if not isinstance(obj, (list, tuple)):
        raise ConfigError(f"{source}.types: 期望列表")
    out: dict[str, TypeDecl] = {}
    for i, entry in enumerate(obj):
        where = f"{source}.types[{i}]"
        decl = TypeDecl.from_dict(entry, where)
        if decl.name in out:
            raise ConfigError(
                f"{where}: 任务类型 '{decl.name}' 重复定义。\n"
                f"  ⚠ 类型必须全局唯一 —— 旧模型允许两个节点各声明一套同名"
                f"类型的契约，那是本次改造要修的问题。"
            )
        out[decl.name] = decl
    if not out:
        raise ConfigError(f"{source}.types: 声明了该节但内容为空")
    return out


def _parse_roles(obj: Any, *, source: str,
                 types: Mapping[str, TypeDecl]) -> dict[str, RoleDecl]:
    """解析 ``roles:`` 节，并校验它引用的类型都已定义。"""
    if obj is None:
        return {}
    if not isinstance(obj, (list, tuple)):
        raise ConfigError(f"{source}.roles: 期望列表")
    out: dict[str, RoleDecl] = {}
    for i, entry in enumerate(obj):
        where = f"{source}.roles[{i}]"
        decl = RoleDecl.from_dict(entry, where)
        if decl.name in out:
            raise ConfigError(f"{where}: 角色 '{decl.name}' 重复定义")

        # ⭐ 角色引用的类型必须已定义。
        #
        # 提前报错比运行时"任务投出去没人能接"好排查得多 ——
        # 后者的症状是"派了活没反应"，而这看起来像网络问题。
        if types:
            for pattern in decl.serves:
                if pattern.endswith(".*"):
                    prefix = pattern[:-1]
                    if not any(tn.startswith(prefix) for tn in types):
                        raise ConfigError(
                            f"{where}.serves: 通配 '{pattern}' 匹配不到任何"
                            f"已定义的类型；已定义 {sorted(types)}"
                        )
                elif pattern not in types:
                    raise ConfigError(
                        f"{where}.serves: 类型 '{pattern}' 未在 types 中定义；"
                        f"已定义 {sorted(types)}"
                    )
        out[decl.name] = decl
    if not out:
        raise ConfigError(f"{source}.roles: 声明了该节但内容为空")
    return out


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        import yaml  # 延迟导入：核心模块不依赖它，只有配置层需要
    except ImportError as exc:  # pragma: no cover
        raise ConfigError(
            "缺少依赖 PyYAML。请执行：pip install -e '.[dev]'"
        ) from exc

    if not path.exists():
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"配置文件解析失败 {path}: {exc}") from exc
    if raw is None:
        raise ConfigError(f"配置文件为空: {path}")
    if not isinstance(raw, Mapping):
        raise ConfigError(f"配置文件顶层必须是映射: {path}")
    return raw


# ---------------------------------------------------------------------------
# 示例配置的临时凭据
# ---------------------------------------------------------------------------

DEMO_SECRET_SUFFIX = "-demo-secret-0123456789abcdef"


def is_example_config(path: str | os.PathLike[str]) -> bool:
    """判断给定路径是否指向仓库自带的示例配置。"""
    try:
        return Path(path).resolve() == Path(EXAMPLE_CONFIG_PATH).resolve()
    except OSError:  # pragma: no cover
        return False


def demo_env_for(path: str | os.PathLike[str]) -> dict[str, str]:
    """为示例配置生成一组可用的临时凭据。

    **仅在路径是示例配置时使用。** 真实配置必须走环境变量——
    因为示例配置是公开的，它里面的任何"默认密钥"都等于没有密钥。

    生成方式是从节点 ID 派生（而非随机），这样同一份示例配置
    在多次运行、多个进程之间可以互相认证（demo 需要）。
    """
    raw = _load_yaml(Path(path))
    nodes = raw.get("nodes") or []
    env: dict[str, str] = {}
    for entry in nodes:
        if not isinstance(entry, Mapping):
            continue
        mid = entry.get("node_id")
        if not isinstance(mid, str):
            continue
        key = _node_env_var(mid)
        env[key] = mid + DEMO_SECRET_SUFFIX
    return env


def resolve_config_path(path: str | os.PathLike[str] | None = None, *,
                        env: Mapping[str, str] | None = None) -> Path:
    """解析出实际会使用的配置路径（含回退）。

    单独抽出来是为了让 CLI 能在加载**之前**判断"这次用的是不是示例配置"，
    从而决定要不要注入临时凭据。
    """
    env = dict(os.environ if env is None else env)
    explicit = path or env.get("NXS_CONFIG")
    resolved = Path(explicit or DEFAULT_CONFIG_PATH)
    if explicit is None and not resolved.exists():
        fallback = Path(EXAMPLE_CONFIG_PATH)
        if fallback.exists():
            resolved = fallback
    return resolved


def load_config(path: str | os.PathLike[str] | None = None, *,
                env: Mapping[str, str] | None = None) -> NexusConfig:
    """加载并校验配置。

    :param path: 配置路径。省略时读 ``NXS_CONFIG`` 环境变量，再退回默认路径。
    :param env: 用于注入凭据的环境变量映射，默认取 ``os.environ``。
    :raises ConfigError: 任何校验失败。调用方应让进程启动失败，不要降级运行。
    """
    resolved = resolve_config_path(path, env=env)
    # 归一化：后续读取凭据要用这个 dict，不能直接用调用方传入的 None
    env = dict(os.environ if env is None else env)
    raw = _load_yaml(resolved)
    _reject_unknown(
        raw,
        ("server", "stores", "policy", "domains", "nodes",
         "types", "roles"),
        f"{resolved.name}",
    )

    # ---- 节点与角色卡 ----
    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, (list, tuple)) or not raw_nodes:
        raise ConfigError(f"{resolved.name}: nodes 必须是非空列表")

    nodes: dict[str, RoleCard] = {}
    secrets: dict[str, bytes] = {}
    #: 从节点卡片推导出的域（旧式配置用）
    node_domains: set[str] = set()
    #: 显式声明的域（新式配置用；非空即表示"接受自注册节点"）
    declared_domains = _parse_domains(raw.get("domains"))

    for i, entry in enumerate(raw_nodes):
        where = f"nodes[{i}]"
        if not isinstance(entry, Mapping):
            raise ConfigError(f"{where}: 期望映射")
        allowed = set(RoleCard._FIELDS) | {"secret"}
        _reject_unknown(entry, allowed, where)

        card = RoleCard.from_dict(
            {k: v for k, v in entry.items() if k != "secret"}, where
        )
        if card.node_id in nodes:
            raise ConfigError(f"{where}: 节点 ID 重复 '{card.node_id}'")
        if card.node_id == OPS_NODE_ID:
            # 运维身份是保留的。若允许真实节点占用它，
            # 该节点就能调用运维接口（批准/吊销），等于把钥匙交给被管理者。
            raise ConfigError(
                f"{where}: '{OPS_NODE_ID}' 是保留的运维身份，不可作为节点 ID"
            )
        nodes[card.node_id] = card
        node_domains.add(card.domain)

        # 凭据优先取环境变量；配置文件里的只是开发便利，示例文件用占位符
        env_key = _node_env_var(card.node_id)
        secret = env.get(env_key) or entry.get("secret")
        if not secret:
            raise ConfigError(
                f"{where} ({card.node_id}): 缺少凭据。"
                f"请设置环境变量 {env_key}，或在配置中提供 secret"
            )
        if isinstance(secret, str) and secret.startswith("<") and secret.endswith(">"):
            raise ConfigError(
                f"{where} ({card.node_id}): 凭据仍是占位符 {secret!r}，请替换为真实值"
            )
        secret_b = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        if len(secret_b) < MIN_SECRET_LENGTH:
            raise ConfigError(
                f"{where} ({card.node_id}): 凭据过短（{len(secret_b)} < {MIN_SECRET_LENGTH} 字节）"
            )
        secrets[card.node_id] = secret_b

    # ---- 策略矩阵引用的域与类型必须真实存在 ----
    policy = PolicyConfig.from_dict(raw.get("policy"))

    # ---- 任务类型与角色（权限模型改造，Step 1）----
    #
    # 两节都是可选的：省略即表示"该部署尚未迁移到角色模型"，
    # 判定仍走旧的 capabilities 汇总。这样现有配置不受影响。
    declared_types = _parse_types(raw.get("types"), source=resolved.name)
    declared_roles = _parse_roles(raw.get("roles"), source=resolved.name,
                                  types=declared_types)

    # ---- 配置里每个节点的角色必须在 roles 段里定义过 ----
    #
    # ⚠ 这道校验是 Step 5c 的必要配套：`role` 改成必填之后，
    #   写错角色名（或配置里根本没有 roles 段）的症状会是
    #   "节点注册成功但什么都干不了"—— 而那看起来像网络故障。
    #
    #   启动时就拦住，错误信息直接列出可用角色。
    if nodes:
        if not declared_roles:
            raise ConfigError(
                f"{resolved.name}: 配置里定义了节点，但没有 `roles:` 段。\n"
                f"  ⚠ 每个节点都必须以某个角色接入 —— 角色决定了它能不能发布任务、\n"
                f"  能服务哪些类型。没有角色表，所有节点都会被拒。\n"
                f"  加上 roles: 段（如 worker / driver / publisher），\n"
                f"  并给每个节点写 `role: <角色名>`。"
            )
        bad = {c.node_id: c.role for c in nodes.values()
               if c.role not in declared_roles}
        if bad:
            raise ConfigError(
                f"这些节点的 role 不在 roles 段里：{bad}\n"
                f"  已定义的角色：{sorted(declared_roles)}\n"
                f"  ⚠ 角色名拼错不会被静默接受 —— 那会让节点注册成功但无权做任何事。"
            )

    # ⭐ 严格模式 vs 自注册模式
    #
    # 两项静态校验（域必须存在、类型必须有人提供）的前提是
    # **"所有节点都写在配置里"**。这个前提在自注册时代不再成立：
    # 工作电脑上的节点在协调节点启动时并不存在。
    #
    # 所以分两级：
    #   · 没声明 domains（demo / 测试）→ 全部节点都在配置里 → 严格校验
    #   · 声明了 domains（生产）       → 接受自注册节点 → 域按声明校验，
    #                                    类型不做静态核对（无法核对）
    #
    # 声明 domains 本身就是"这个部署会接纳自注册节点"的表态，
    # 所以用它作为开关是准确的，不需要再加一个额外的开关。
    known = set(declared_domains) if declared_domains else node_domains
    strict = not declared_domains

    for dom in policy.auto_approve_domains:
        if dom not in known:
            raise ConfigError(
                f"policy.auto_approve_domains: 引用了不存在的域 '{dom}'；"
                f"已定义的域为 {sorted(known)}"
            )

    # 声明了域时，节点卡片里的域也必须在声明之列（防配置笔误）
    if declared_domains:
        stray = {c.node_id: c.domain for c in nodes.values()
                 if c.domain not in declared_domains}
        if stray:
            raise ConfigError(
                f"以下节点的域未在 domains 中声明：{stray}；"
                f"已声明的域为 {sorted(declared_domains)}"
            )

    available_types = {t for card in nodes.values() for t in
                       (c.type for c in card.capabilities)}

    for i, rule in enumerate(policy.rules):
        for side, dom in (("from_domain", rule.from_domain), ("to_domain", rule.to_domain)):
            if dom not in known:
                raise ConfigError(
                    f"policy.matrix[{i}].{side}: 引用了不存在的域 '{dom}'；"
                    f"已定义的域为 {sorted(known)}"
                )
        # ⚠ 矩阵不再列类型，所以这里没有"类型必须有人提供"的校验了。
        #
        #   这曾经是一道有用的静态检查（提前发现"任务发出去没人接"），
        #   但它绑在已被移除的 `allow_types` 上。
        #
        #   替代检查在 types 段：`_parse_roles` 已保证角色引用的类型
        #   都在 types 里定义过，而 `_parse_types` 保证类型不重复。
        #   所以"引用了一个不存在的类型"依然会在启动时被拦住。

    # ---- 本进程承载的节点 ----
    #
    # 校验存在性是刻意的：写错一个 node_id 时，如果静默忽略，症状是
    # "协调节点过一会儿变 stale"——而那看起来完全与配置无关。
    # 这类"配错了但不报错"的问题最难查，所以在加载时就拦住。
    # ⚠ local_node 的解析与校验已删除（Step 5）——
    #   这个概念不存在了：mesh 不承载任何节点。

    cfg = NexusConfig(
        server=ServerConfig.from_dict(raw.get("server")),
        stores=StoreConfig.from_dict(raw.get("stores")),
        policy=policy,
        domains=declared_domains,
        types=declared_types,
        roles=declared_roles,
        nodes=nodes,
        secrets=secrets,
        source_path=str(resolved),
    )
    return cfg


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "RoleDecl",
    "TypeDecl",
    "DEMO_SECRET_SUFFIX",
    "UNIMPLEMENTED",
    "EXAMPLE_CONFIG_PATH",
    "ENV_OPS_SECRET",
    "ENV_SECRET_PREFIX",
    "DomainDecl",
    "MatrixRule",
    "NexusConfig",
    "OPS_NODE_ID",
    "PolicyConfig",
    "ServerConfig",
    "StoreConfig",
    "demo_env_for",
    "is_example_config",
    "load_config",
]
