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

    _FIELDS = ("host", "port", "max_body_bytes", "tls_cert", "tls_key")

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any] | None) -> "ServerConfig":
        obj = obj or {}
        _reject_unknown(obj, cls._FIELDS, "server")
        port = obj.get("port", 8443)
        if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
            raise ConfigError(f"server.port: 非法端口 {port!r}")
        max_body = obj.get("max_body_bytes", 256 * 1024)
        if not isinstance(max_body, int) or max_body <= 0:
            raise ConfigError("server.max_body_bytes: 必须是正整数")
        cert, key = obj.get("tls_cert"), obj.get("tls_key")
        if (cert is None) != (key is None):
            raise ConfigError("server: tls_cert 与 tls_key 必须同时提供或同时省略")
        return cls(
            host=str(obj.get("host", "127.0.0.1")),
            port=port,
            max_body_bytes=max_body,
            tls_cert=cert,
            tls_key=key,
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
    """权限矩阵的一条规则。

    **维度是「域 × 类型」，不是「节点 × 节点」。** 这是把权限复杂度从
    O(N²) 压回可控范围的关键：新增节点只需归属到既有域，规则自动继承。
    """

    from_domain: str
    to_domain: str
    allow_types: tuple[str, ...] = ()
    max_level: Level = Level.SUMMARY

    _FIELDS = ("from_domain", "to_domain", "allow_types", "max_level")

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any], where: str) -> "MatrixRule":
        if not isinstance(obj, Mapping):
            raise ConfigError(f"{where}: 期望映射")
        _reject_unknown(obj, cls._FIELDS, where)
        raw_types = obj.get("allow_types") or []
        if not isinstance(raw_types, (list, tuple)) or not raw_types:
            raise ConfigError(f"{where}.allow_types: 必须显式列出至少一个类型模式")
        patterns: list[str] = []
        for p in raw_types:
            if not isinstance(p, str) or not p.strip():
                raise ConfigError(f"{where}.allow_types: 元素必须是非空字符串")
            if p.strip() == "*":
                # 全通配等于取消默认拒绝，必须显式写出来才允许——这里直接禁止，
                # 需要放开时逐个列模式，避免一次误配把整个域打开。
                raise ConfigError(
                    f"{where}.allow_types: 不允许裸通配 '*'；"
                    f"请写成 '<前缀>.*' 显式限定范围"
                )
            patterns.append(p.strip())
        return cls(
            from_domain=str(_require(obj, "from_domain", where, (str,))),
            to_domain=str(_require(obj, "to_domain", where, (str,))),
            allow_types=tuple(patterns),
            max_level=Level.parse(obj.get("max_level", "summary"), f"{where}.max_level"),
        )

    def matches_type(self, task_type: str) -> bool:
        for p in self.allow_types:
            if p.endswith(".*"):
                if task_type.startswith(p[:-1]):  # 保留结尾的点，避免 "demo" 匹配 "demoX.y"
                    return True
            elif task_type == p:
                return True
        return False


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
    ("tls_cert / tls_key", "v0.1 无 HTTP 传输层，配置的 TLS 不会生效"),
    ("max_body_bytes", "同上，传输层未实现"),
    ("rate_limit_per_second", "速率限制未接入；tasks_per_day 与并发是生效的"),
    ("params_schema", "参数 schema 只声明、未校验；格式错误的参数由执行节点自己承担失败"),
    ("bytes_per_day", "应答字节数未按节点累计，故按日字节配额永不触发"),
)


@dataclass
class NexusConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    stores: StoreConfig = field(default_factory=StoreConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    #: 节点角色卡，按 node_id 索引
    nodes: dict[str, RoleCard] = field(default_factory=dict)
    #: 节点凭据，按 node_id 索引。**只存在于内存，不写入任何日志。**
    secrets: dict[str, bytes] = field(default_factory=dict)
    source_path: str = ""

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------

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
            roles = [n for n in ("issuer", "executor", "relay", "observer", "human_interface")
                     if getattr(c.roles, n)]
            lines.append(
                f"  · {mid:<12} domain={c.domain:<8} roles={','.join(roles) or '-':<28} "
                f"caps={[x.type for x in c.capabilities]} max_level={c.constraints.max_response_level}"
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
    _reject_unknown(raw, ("server", "stores", "policy", "nodes"), f"{resolved.name}")

    # ---- 节点与角色卡 ----
    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, (list, tuple)) or not raw_nodes:
        raise ConfigError(f"{resolved.name}: nodes 必须是非空列表")

    nodes: dict[str, RoleCard] = {}
    secrets: dict[str, bytes] = {}
    domains: set[str] = set()

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
        nodes[card.node_id] = card
        domains.add(card.domain)

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

    for dom in policy.auto_approve_domains:
        if dom not in domains:
            raise ConfigError(
                f"policy.auto_approve_domains: 引用了不存在的域 '{dom}'；"
                f"已定义的域为 {sorted(domains)}"
            )

    available_types = {t for card in nodes.values() for t in
                       (c.type for c in card.capabilities)}

    for i, rule in enumerate(policy.rules):
        for side, dom in (("from_domain", rule.from_domain), ("to_domain", rule.to_domain)):
            if dom not in domains:
                raise ConfigError(
                    f"policy.matrix[{i}].{side}: 引用了不存在的域 '{dom}'；"
                    f"已定义的域为 {sorted(domains)}"
                )
        # 矩阵里写的具体类型必须是某个节点真的能提供的能力。
        # 提前报错比运行时"任务投出去没人接"好排查得多。
        # 通配模式（如 "demo.*"）无法静态核对，跳过。
        for pattern in rule.allow_types:
            if not pattern.endswith(".*") and pattern not in available_types:
                raise ConfigError(
                    f"policy.matrix[{i}].allow_types: 类型 '{pattern}' 没有任何节点提供；"
                    f"可用的类型为 {sorted(available_types)}"
                )

    cfg = NexusConfig(
        server=ServerConfig.from_dict(raw.get("server")),
        stores=StoreConfig.from_dict(raw.get("stores")),
        policy=policy,
        nodes=nodes,
        secrets=secrets,
        source_path=str(resolved),
    )
    return cfg


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEMO_SECRET_SUFFIX",
    "UNIMPLEMENTED",
    "EXAMPLE_CONFIG_PATH",
    "ENV_SECRET_PREFIX",
    "MatrixRule",
    "NexusConfig",
    "PolicyConfig",
    "ServerConfig",
    "StoreConfig",
    "demo_env_for",
    "is_example_config",
    "load_config",
]
