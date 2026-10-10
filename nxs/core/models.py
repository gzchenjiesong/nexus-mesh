"""核心数据模型。

本模块定义系统的契约对象。它的职责边界是：

* **只做结构与取值校验**，不做任何策略判断（策略在 ``control/policy.py``）
* **不感知存储与传输**（存储经 Port，传输在 edge 层）
* **不引入第三方依赖**——全部基于标准库 dataclasses

严格性约定
----------
所有 ``from_dict`` 都**拒绝未知字段**。这是刻意的：配置或报文里的拼写错误
如果被静默忽略，会变成"看起来生效、实际没生效"的安全漏洞。宁可启动时报错。
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Iterable, Mapping

from .errors import ConfigError, RuleId


# ---------------------------------------------------------------------------
# 数据等级
# ---------------------------------------------------------------------------

class Level(IntEnum):
    """应答数据的敏感等级。

    用 ``IntEnum`` 是为了让"不超过上限"这个判断可以直接比较：
    ``artifact.level <= constraint.max_response_level``。

    **命名约定（C 规范）**
    --------------------
    成员名与线上值都使用**语义化长名**，不使用 ``L0``/``L1`` 这类短编号。理由：

    * **可搜索** —— 在代码与日志里 grep ``summary`` 能精确定位；
      grep ``L1`` 会命中大量无关内容（十六进制串、变量名片段、其他体系的编号）
    * **自解释** —— 读日志时不必查表就知道 ``"fragment"`` 比 ``"summary"`` 敏感
    * **避免体系冲突** —— 项目里同时存在多个编号体系（模块 M、需求 R、
      功能 F、测试 T、安全 S、准则 C …），再让等级占用 ``L`` 会持续撞号

    线上值即成员名的小写形式（如 ``"summary"``），代码与协议用同一个名字。
    """

    METADATA = 0   # 元数据：不含实体信息，"有这条记录""进度到某阶段"
    SUMMARY = 1    # 摘要：经过抽象，不含可还原原文的信息
    FRAGMENT = 2   # 片段：原文的一部分，具备还原价值
    FULL_TEXT = 3  # 原文：完整文件、对话记录、全文

    @classmethod
    def parse(cls, value: Any, where: str = "") -> "Level":
        """解析等级。接受成员名（大小写不敏感）或等价的整数。"""
        if isinstance(value, cls):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            try:
                return cls(value)
            except ValueError as exc:
                raise ConfigError(f"{where}: 等级取值超出范围: {value}") from exc
        if isinstance(value, str):
            key = value.strip().upper().replace("-", "_")
            # 注意：__members__ 是 Enum 的内置属性，不是本项目的术语
            if key in cls.__members__:
                return cls[key]
            # 兼容旧写法 L0-L3，但**不推荐**——会在日志里报一次提示
            legacy = {"L0": "METADATA", "L1": "SUMMARY", "L2": "FRAGMENT", "L3": "FULL_TEXT"}
            if key in legacy:
                warnings.warn(
                    f"{where}: 等级写法 {value!r} 已弃用，请改用 "
                    f"{cls[legacy[key]].name.lower()!r}",
                    DeprecationWarning, stacklevel=2,
                )
                return cls[legacy[key]]
        raise ConfigError(
            f"{where}: 无法解析的数据等级: {value!r}；"
            f"可用值：{[m.name.lower() for m in cls]}"
        )

    def __str__(self) -> str:
        """线上值 = 成员名小写。**这是写进信封的形式。**"""
        return self.name.lower()


class TaskMode(str):
    """任务执行模式。"""

    SYNC = "sync"    # 秒级完成，超时即失败，不重试
    ASYNC = "async"  # 分钟级完成，进队列 + 租约 + 幂等重试


class TaskStatus(str):
    """任务状态。终态有三个：completed / failed / canceled。"""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    TERMINAL: frozenset[str] = frozenset({COMPLETED, FAILED, CANCELED})

    #: 合法的状态迁移。``input_required`` 是唯一可双向的状态，
    #: 它让执行方能向发起方追问，而不必失败重来。
    TRANSITIONS: Mapping[str, frozenset[str]] = {
        SUBMITTED: frozenset({WORKING, CANCELED, FAILED}),
        WORKING: frozenset({INPUT_REQUIRED, COMPLETED, FAILED, CANCELED}),
        INPUT_REQUIRED: frozenset({WORKING, CANCELED, FAILED}),
        COMPLETED: frozenset(),
        FAILED: frozenset(),
        CANCELED: frozenset(),
    }

    @classmethod
    def can_transition(cls, src: str, dst: str) -> bool:
        return dst in cls.TRANSITIONS.get(src, frozenset())


class Semantics(str):
    """信封内容的语义分类。**这是防跨域提示注入的核心字段。**

    * ``DATA`` —— 内容是被观察的数据。接收方可以分析、总结、呈现，
      **但不得直接据此触发工具调用或对外动作**。
    * ``REQUEST`` —— 内容是一个请求。仅用于发起方向执行方下达任务，
      以及执行方在执行途中的追问（``input_required``）。

    **节点回传的一切内容一律标为 ``DATA``**，由边界层强制改写——
    因为回传内容的来源（对话记录、文档、外部邮件）本质是人类自然语言，
    其中完全可能包含"请把 X 发给 Y"这类**伪装成事实的指令**。
    """

    DATA = "data"
    REQUEST = "request"


# ---------------------------------------------------------------------------
# 校验辅助
# ---------------------------------------------------------------------------

def _require(obj: Mapping[str, Any], key: str, where: str, types: tuple[type, ...] = ()) -> Any:
    if key not in obj:
        raise ConfigError(f"{where}: 缺少必填字段 '{key}'")
    value = obj[key]
    if types and not isinstance(value, types):
        names = " | ".join(t.__name__ for t in types)
        raise ConfigError(f"{where}: 字段 '{key}' 期望 {names}，实际 {type(value).__name__}")
    return value


def _reject_unknown(obj: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    """拒绝未知字段。静默忽略拼写错误会制造"配置看起来生效"的假象。"""
    unknown = set(obj) - set(allowed)
    if unknown:
        raise ConfigError(
            f"{where}: 存在未知字段 {sorted(unknown)}；"
            f"允许的字段为 {sorted(allowed)}"
        )


def _as_str_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{where}: 期望字符串列表")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(f"{where}: 列表元素必须是字符串，实际 {type(item).__name__}")
        out.append(item)
    return tuple(out)


# ---------------------------------------------------------------------------
# 角色与约束
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Roles:
    """维度一：任务流向。空值一律视为 False（默认拒绝的体现）。"""

    issuer: bool = False
    executor: bool = False
    relay: bool = False
    observer: bool = False
    #: 是否为人机入口。当前仅有一个人机入口，但**由配置表达而非架构特例**，
    #: 这样将来加入第二个入口只是多配一个节点，不改代码。
    human_interface: bool = False

    _FIELDS = ("issuer", "executor", "relay", "observer", "human_interface")

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any] | None, where: str = "roles") -> "Roles":
        obj = obj or {}
        if not isinstance(obj, Mapping):
            raise ConfigError(f"{where}: 期望映射")
        _reject_unknown(obj, cls._FIELDS, where)
        kwargs = {k: bool(obj.get(k, False)) for k in cls._FIELDS}
        return cls(**kwargs)


@dataclass(frozen=True)
class Capability:
    """维度二：能力供给。一个具名的任务类型声明。

    ``response_fields`` 是**应答字段白名单**——"这个任务类型能返回哪些字段"
    在**注册时**就固定下来，而不是运行时判断。这让越权字段成为结构事实，
    而非依赖过滤逻辑写对。
    """

    type: str
    level: Level
    mode: str = TaskMode.SYNC
    params_schema: Mapping[str, str] = field(default_factory=dict)
    response_fields: tuple[str, ...] = ()
    #: 只能返回自由文本的字段。适用更严格的长度上限与实体扫描，
    #: 且必须在呈现给用户时明确标注"含自由文本"。
    freetext_fields: tuple[str, ...] = ()
    description: str = ""

    _FIELDS = (
        "type", "level", "mode", "params_schema",
        "response_fields", "freetext_fields", "description",
    )

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any], where: str = "capability") -> "Capability":
        if not isinstance(obj, Mapping):
            raise ConfigError(f"{where}: 期望映射")
        _reject_unknown(obj, cls._FIELDS, where)

        ctype = _require(obj, "type", where, (str,))
        if not ctype or "." not in ctype:
            # 命名约定 <verb>.<object>，如 demo.echo —— 便于按前缀做粗粒度策略
            raise ConfigError(f"{where}: 任务类型 '{ctype}' 不符合 <verb>.<object> 命名约定")

        level = Level.parse(obj.get("level", "metadata"), f"{where}.level")

        mode = obj.get("mode", TaskMode.SYNC)
        if mode not in (TaskMode.SYNC, TaskMode.ASYNC):
            raise ConfigError(f"{where}: mode 必须是 sync 或 async，实际 {mode!r}")

        schema = obj.get("params_schema") or {}
        if not isinstance(schema, Mapping):
            raise ConfigError(f"{where}.params_schema: 期望映射")
        for k, v in schema.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ConfigError(f"{where}.params_schema: 键与值都必须是字符串")

        response_fields = _as_str_list(obj.get("response_fields"), f"{where}.response_fields")
        freetext_fields = _as_str_list(obj.get("freetext_fields"), f"{where}.freetext_fields")

        stray = set(freetext_fields) - set(response_fields)
        if stray:
            raise ConfigError(
                f"{where}: freetext_fields {sorted(stray)} 必须同时出现在 response_fields 中"
            )

        return cls(
            type=ctype,
            level=level,
            mode=mode,
            params_schema=dict(schema),
            response_fields=response_fields,
            freetext_fields=freetext_fields,
            description=str(obj.get("description", "")),
        )

    @property
    def is_freetext_capable(self) -> bool:
        return bool(self.freetext_fields)


@dataclass(frozen=True)
class Quota:
    """配额。默认值刻意保守——新增节点不会因为漏配而获得无限额度。"""

    tasks_per_day: int = 200
    bytes_per_response: int = 64 * 1024
    bytes_per_day: int = 4 * 1024 * 1024
    concurrent_tasks: int = 1

    _FIELDS = ("tasks_per_day", "bytes_per_response", "bytes_per_day", "concurrent_tasks")

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any] | None, where: str = "quota") -> "Quota":
        obj = obj or {}
        if not isinstance(obj, Mapping):
            raise ConfigError(f"{where}: 期望映射")
        _reject_unknown(obj, cls._FIELDS, where)
        kwargs: dict[str, int] = {}
        for k in cls._FIELDS:
            if k in obj:
                v = obj[k]
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    raise ConfigError(f"{where}.{k}: 必须是非负整数，实际 {v!r}")
                kwargs[k] = v
        return cls(**kwargs)


@dataclass(frozen=True)
class Constraints:
    """维度三：信任约束。**与能力正交——能力不能突破约束。**

    "能力很强的 Agent" 不等于 "权限很大的 Agent"，这个区分就落在这个类上。
    """

    max_response_level: Level = Level.SUMMARY
    #: 作为 Issuer 时可发布的任务类型。**空元组 = 完全不可发布**。
    can_issue_types: tuple[str, ...] = ()
    #: 谁可以给它派任务（按信任域）。**空元组 = 无人可派**。
    can_receive_from: tuple[str, ...] = ()
    #: 允许二次委托的目标域。**空元组 = 不做二次委托**（当前默认）。
    can_delegate_to: tuple[str, ...] = ()
    quota: Quota = field(default_factory=Quota)

    _FIELDS = (
        "max_response_level", "can_issue_types", "can_receive_from",
        "can_delegate_to", "quota",
    )

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any] | None, where: str = "constraints") -> "Constraints":
        obj = obj or {}
        if not isinstance(obj, Mapping):
            raise ConfigError(f"{where}: 期望映射")
        _reject_unknown(obj, cls._FIELDS, where)
        return cls(
            max_response_level=Level.parse(
                obj.get("max_response_level", "summary"), f"{where}.max_response_level"
            ),
            can_issue_types=_as_str_list(obj.get("can_issue_types"), f"{where}.can_issue_types"),
            can_receive_from=_as_str_list(obj.get("can_receive_from"), f"{where}.can_receive_from"),
            can_delegate_to=_as_str_list(obj.get("can_delegate_to"), f"{where}.can_delegate_to"),
            quota=Quota.from_dict(obj.get("quota"), f"{where}.quota"),
        )


@dataclass(frozen=True)
class RoleCard:
    """节点的角色卡——身份与能力的唯一权威声明。

    它是 A2A Agent Card 的对应物，但**显式增加了"信任约束"这一维**。
    A2A 的卡片只描述能力，因为它的前提是跨组织的对等伙伴；
    本系统的节点信任等级并不对等，所以约束必须显式声明。
    """

    node_id: str
    domain: str
    display_name: str = ""
    card_version: str = "1.0.0"
    #: 可达性。当前所有节点均为 egress-only（无入站端口）。
    reachability: str = "egress-only"
    heartbeat_interval: int = 60
    roles: Roles = field(default_factory=Roles)
    capabilities: tuple[Capability, ...] = ()
    constraints: Constraints = field(default_factory=Constraints)

    _FIELDS = (
        "card_version", "node_id", "display_name", "domain", "reachability",
        "heartbeat_interval", "roles", "capabilities", "constraints",
    )

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any], where: str = "role_card") -> "RoleCard":
        if not isinstance(obj, Mapping):
            raise ConfigError(f"{where}: 期望映射")
        _reject_unknown(obj, cls._FIELDS, where)

        node_id = _require(obj, "node_id", where, (str,))
        domain = _require(obj, "domain", where, (str,))
        if not node_id or not domain:
            raise ConfigError(f"{where}: node_id 与 domain 不可为空")

        hb = obj.get("heartbeat_interval", 60)
        if not isinstance(hb, int) or isinstance(hb, bool) or hb <= 0:
            raise ConfigError(f"{where}.heartbeat_interval: 必须是正整数")

        raw_caps = obj.get("capabilities") or []
        if not isinstance(raw_caps, (list, tuple)):
            raise ConfigError(f"{where}.capabilities: 期望列表")
        caps = tuple(
            Capability.from_dict(c, f"{where}.capabilities[{i}]")
            for i, c in enumerate(raw_caps)
        )

        seen: set[str] = set()
        for c in caps:
            if c.type in seen:
                raise ConfigError(f"{where}: 任务类型 '{c.type}' 重复声明")
            seen.add(c.type)

        return cls(
            node_id=node_id,
            domain=domain,
            display_name=str(obj.get("display_name", "")),
            card_version=str(obj.get("card_version", "1.0.0")),
            reachability=str(obj.get("reachability", "egress-only")),
            heartbeat_interval=hb,
            roles=Roles.from_dict(obj.get("roles"), f"{where}.roles"),
            capabilities=caps,
            constraints=Constraints.from_dict(obj.get("constraints"), f"{where}.constraints"),
        )

    def capability(self, task_type: str) -> Capability | None:
        for c in self.capabilities:
            if c.type == task_type:
                return c
        return None

    def to_public_dict(self) -> dict[str, Any]:
        """对外呈现用的摘要。不含配额等内部约束细节。"""
        return {
            "node_id": self.node_id,
            "display_name": self.display_name,
            "domain": self.domain,
            "roles": {
                "issuer": self.roles.issuer,
                "executor": self.roles.executor,
                "human_interface": self.roles.human_interface,
            },
            "capabilities": [
                {"type": c.type, "level": str(c.level), "mode": c.mode}
                for c in self.capabilities
            ],
            "max_response_level": str(self.constraints.max_response_level),
        }


# ---------------------------------------------------------------------------
# 产物与任务
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Artifact:
    """任务的产出。

    **``level`` 由执行方在产出时标注，调用方无法指定。**
    否则攻击者可以直接"声明"自己只要摘要级，却拿到片段级的内容。
    """

    level: Level
    kind: str
    data: Any
    #: 来源可溯：谁产的、命中哪条规则、是否经过脱敏
    provenance: Mapping[str, Any] = field(default_factory=dict)

    _FIELDS = ("level", "kind", "data", "provenance")

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any], where: str = "artifact") -> "Artifact":
        if not isinstance(obj, Mapping):
            raise ConfigError(f"{where}: 期望映射")
        _reject_unknown(obj, cls._FIELDS, where)
        kind = str(obj.get("kind", "structured"))
        if kind not in ("structured", "freetext", "reference"):
            raise ConfigError(f"{where}: kind 非法: {kind!r}")
        prov = obj.get("provenance") or {}
        if not isinstance(prov, Mapping):
            raise ConfigError(f"{where}.provenance: 期望映射")
        return cls(
            level=Level.parse(_require(obj, "level", where), f"{where}.level"),
            kind=kind,
            data=obj.get("data"),
            provenance=dict(prov),
        )


@dataclass
class Task:
    """一个任务。生命周期与租约逻辑在 ``data/task.py``。"""

    id: str
    #: 幂等键（必填，由发起方生成）。**没有它，断电重试会产生重复副作用。**
    idempotency_key: str
    type: str
    session_id: str
    from_node: str
    to_node: str
    status: str = TaskStatus.SUBMITTED
    mode: str = TaskMode.SYNC
    params: Mapping[str, Any] = field(default_factory=dict)
    artifacts: list[Artifact] = field(default_factory=list)
    semantics: str = Semantics.DATA
    ttl: str | None = None
    attempt: int = 0
    trace_id: str = ""

    _FIELDS = (
        "id", "idempotency_key", "type", "session_id", "from_node", "to_node",
        "status", "mode", "params", "artifacts", "semantics", "ttl", "attempt", "trace_id",
    )

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any], where: str = "task") -> "Task":
        if not isinstance(obj, Mapping):
            raise ConfigError(f"{where}: 期望映射")
        _reject_unknown(obj, cls._FIELDS, where)

        status = str(obj.get("status", TaskStatus.SUBMITTED))
        if status not in TaskStatus.TRANSITIONS:
            raise ConfigError(f"{where}: 未知状态 {status!r}")

        semantics = str(obj.get("semantics", Semantics.DATA))
        if semantics not in (Semantics.DATA, Semantics.REQUEST):
            raise ConfigError(f"{where}: semantics 必须是 data 或 request")

        params = obj.get("params") or {}
        if not isinstance(params, Mapping):
            raise ConfigError(f"{where}.params: 期望映射")

        raw_arts = obj.get("artifacts") or []
        if not isinstance(raw_arts, (list, tuple)):
            raise ConfigError(f"{where}.artifacts: 期望列表")

        return cls(
            id=_require(obj, "id", where, (str,)),
            idempotency_key=_require(obj, "idempotency_key", where, (str,)),
            type=_require(obj, "type", where, (str,)),
            session_id=str(obj.get("session_id", "")),
            from_node=_require(obj, "from_node", where, (str,)),
            to_node=_require(obj, "to_node", where, (str,)),
            status=status,
            mode=str(obj.get("mode", TaskMode.SYNC)),
            params=dict(params),
            artifacts=[Artifact.from_dict(a, f"{where}.artifacts[{i}]")
                       for i, a in enumerate(raw_arts)],
            semantics=semantics,
            ttl=obj.get("ttl"),
            attempt=int(obj.get("attempt", 0)),
            trace_id=str(obj.get("trace_id", "")),
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TaskStatus.TERMINAL

    def artifact_payload(self) -> Any:
        """便于测试与呈现的简化视图。"""
        if not self.artifacts:
            return None
        if len(self.artifacts) == 1:
            return self.artifacts[0].data
        return [a.data for a in self.artifacts]

    def max_artifact_level(self) -> Level:
        if not self.artifacts:
            return Level.METADATA
        return max(a.level for a in self.artifacts)


__all__ = [
    "Artifact",
    "Capability",
    "Constraints",
    "Level",
    "Quota",
    "RoleCard",
    "Roles",
    "RuleId",
    "Semantics",
    "Task",
    "TaskMode",
    "TaskStatus",
]
