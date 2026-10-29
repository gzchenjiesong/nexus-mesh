"""错误码与异常体系。

设计要点
--------
拒绝（deny）与失败（fail）是两件事，不要混用：

* **拒绝（Deny）** —— 策略判定不允许。返回 ``rule_id`` 与一条**不含任何业务内容**
  的固定消息。这是安全设计的一部分：错误信息本身也是信道，泄露被拒内容的前缀、
  摘要或细节，等于绕过了应答边界。
* **失败（Fail）** —— 执行过程中出错。可以携带技术性细节（堆栈、超时原因），
  因为这些信息对攻击者没有额外价值。

命名约定
--------
``rule_id`` 形如 ``<域>-<对象>-<序号>``：

===========  ==========================================
前缀          含义
===========  ==========================================
``FM-``       格式 / schema 校验
``AU-``       认证与凭据
``MX-``       权限矩阵
``WL-``       白名单（任务类型 / 应答字段 / 数据等级）
``QT-``       配额与速率
``LF-``       任务生命周期
===========  ==========================================
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RuleId(str, Enum):
    """策略规则的稳定标识符。

    取值一旦发布就不再变更——审计日志与外部告警都会引用它们。
    需要更细的粒度时新增取值，不要修改既有取值。
    """

    # ---------- FM- 格式与 schema ----------
    FM_ENVELOPE_INVALID = "FM-SCHEMA-01"
    FM_TASK_PARAM_INVALID = "FM-PARAM-02"
    FM_CARDCARD_INVALID = "FM-CARD-03"
    #: v0.2 新增（传输层）：请求体超过 ``server.max_body_bytes``。
    #: 传输层必须在**读入之前**用 Content-Length 判断，否则上限形同虚设。
    FM_BODY_TOO_LARGE = "FM-BODY-04"

    # ---------- AU- 认证与凭据 ----------
    AU_SIGNATURE_INVALID = "AU-SIG-01"
    AU_REPLAY_DETECTED = "AU-REPLAY-02"
    AU_CREDENTIAL_REVOKED = "AU-CRED-03"
    AU_NODE_UNKNOWN = "AU-NODE-04"

    # ---------- MX- 权限矩阵 ----------
    MX_DOMAIN_FORBIDDEN = "MX-DOMAIN-02"
    MX_ISSUER_NOT_ALLOWED = "MX-ISSUER-01"
    #: 节点尚未批准 / 不可路由。
    #:
    #: ⚠ 这个码是从已删除的 ``MX-RECEIVE-03`` 拆出来的
    #: （权限模型改造 Step 4）。那一个码原来同时管三件事：
    #:
    #:     域不可达    → MX-DOMAIN-02
    #:     未批准      → 本码
    #:     任务不属于你 → LF-TASK-404
    #:
    #: 一个码覆盖三种语义，排障时必须读代码才知道这次是谁不许谁。
    AU_NOT_APPROVED = "AU-APPROVE-05"
    MX_DELEGATION_TOO_DEEP = "MX-DELEGATE-04"

    # ---------- WL- 白名单 ----------
    WL_TYPE_NOT_REGISTERED = "WL-TYPE-404"
    WL_FIELD_NOT_ALLOWED = "WL-FIELD-001"
    WL_LEVEL_EXCEEDED = "WL-LEVEL-002"
    WL_FREETEXT_NOT_ALLOWED = "WL-FREETEXT-003"    #: 目标信箱已达 depth 上限（节点自报的 self_limits.max_backlog）。
    #:
    #: ⚠ 这是**节点自己的防御**，不是网络配额。发布方收到它应该
    #: 稍后重试或换节点 —— 而不是以为"我被限权了"。
    QT_BACKLOG_FULL = "QT-BACKLOG-07"
    QT_RATE_LIMITED = "QT-RATE-04"    #: v0.2 新增（传输层）：入站并发连接/请求超过 ``server.max_concurrent_requests``。
    #: 与 ``QT-CONC-05``（某节点任务并发配额）刻意分开：那是业务配额，
    #: 这是传输层自保，两者触发源不同、处置也不同。
    QT_CONNECTION_LIMIT = "QT-CONN-06"

    # ---------- LF- 生命周期 ----------
    LF_LEASE_HELD = "LF-LEASE-01"
    LF_TASK_EXPIRED = "LF-EXPIRE-02"
    LF_INVALID_TRANSITION = "LF-STATE-03"
    LF_DUPLICATE_IDEMPOTENCY = "LF-IDEM-04"
    #: v0.15 新增（查询）：该任务不存在，**或者调用方看不到它**。
    #:
    #: 两种情况刻意共用一个取值。区分它们就等于提供一个枚举接口：
    #: 攻击者可以据此判断某个 task_id 是否真实存在。
    #: 与"策略拒绝时只回 rule_id、不回内容"是同一条思路 ——
    #: **拒绝的形状本身会泄露信息。**
    LF_TASK_NOT_FOUND = "LF-TASK-404"


#: 拒绝时回给调用方的**固定消息**。
#:
#: 刻意保持极简：只有规则标识符，不含被拒内容、字段名、字段值、
#: 长度、前若干字符等任何可用于推断内容的线索。
#: 需要排查问题时，细节记在**本地**审计日志里（见 mgmt/audit）。
_SAFE_MESSAGES: dict[RuleId, str] = {
    RuleId.FM_ENVELOPE_INVALID: "envelope rejected",
    RuleId.FM_TASK_PARAM_INVALID: "task parameters rejected",
    RuleId.FM_CARDCARD_INVALID: "role card rejected",
    RuleId.FM_BODY_TOO_LARGE: "request rejected",
    RuleId.AU_SIGNATURE_INVALID: "authentication failed",
    RuleId.AU_REPLAY_DETECTED: "request rejected",
    RuleId.AU_CREDENTIAL_REVOKED: "authentication failed",
    RuleId.AU_NODE_UNKNOWN: "node not registered",
    RuleId.MX_DOMAIN_FORBIDDEN: "delegation not permitted",
    RuleId.MX_ISSUER_NOT_ALLOWED: "publish not permitted",
    RuleId.AU_NOT_APPROVED: "not approved",
    RuleId.MX_DELEGATION_TOO_DEEP: "delegation not permitted",
    RuleId.WL_TYPE_NOT_REGISTERED: "task type not available",
    RuleId.WL_FIELD_NOT_ALLOWED: "response rejected",
    RuleId.WL_LEVEL_EXCEEDED: "response rejected",
    RuleId.WL_FREETEXT_NOT_ALLOWED: "response rejected",
    RuleId.QT_BACKLOG_FULL: "backlog full",
    RuleId.QT_RATE_LIMITED: "rate limited",
    RuleId.QT_CONNECTION_LIMIT: "service busy",
    RuleId.LF_LEASE_HELD: "task unavailable",
    RuleId.LF_TASK_EXPIRED: "task expired",
    RuleId.LF_INVALID_TRANSITION: "state transition rejected",
    RuleId.LF_DUPLICATE_IDEMPOTENCY: "duplicate request",
}


def safe_message(rule_id: RuleId) -> str:
    """返回规则的固定对外消息。未知规则一律降级为最保守的措辞。"""
    return _SAFE_MESSAGES.get(rule_id, "request rejected")


class NexusError(Exception):
    """所有 Nexus 异常的基类。"""


@dataclass
class PolicyDenied(NexusError):
    """策略拒绝。

    只携带 ``rule_id``——**不要把被拒的内容放进这个异常**，
    因为它会被序列化进 HTTP 响应体送回调用方。细节请写本地审计日志。

    :param rule_id: 命中的规则
    :param detail: **仅用于本地日志**，绝不进入对外响应。默认 None。
    """

    rule_id: RuleId
    detail: str | None = None

    def __str__(self) -> str:  # pragma: no cover - 便于调试
        return f"denied[{self.rule_id.value}]"

    def to_wire(self) -> dict[str, str]:
        """序列化为对外响应。**只含 rule_id 与固定消息。**"""
        return {
            "denied": "true",
            "rule_id": self.rule_id.value,
            "message": safe_message(self.rule_id),
        }


class AuthFailed(NexusError):
    """认证失败。对外同样只暴露 rule_id。"""

    def __init__(self, rule_id: RuleId, detail: str | None = None) -> None:
        super().__init__(rule_id.value)
        self.rule_id = rule_id
        self.detail = detail


class TaskExecutionError(NexusError):
    """执行期错误。这类信息对外可以带技术细节，因为它不泄露业务内容。"""


class ConfigError(NexusError):
    """配置非法。启动时应直接失败，不要带着坏配置运行。"""
