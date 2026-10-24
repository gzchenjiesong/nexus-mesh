"""统一信封（Envelope）。

信封是**投递容器**，任务（``Task``）是**内容**。把两者分开的好处：
传输与认证逻辑只关心信封，业务与策略逻辑只关心任务，互不污染。

签名覆盖范围
------------
``HMAC-SHA256(sender ‖ ts ‖ nonce ‖ canonical_json(task))``

刻意**不包含** sig 字段自身，也**不包含** envelope_version——版本是元数据，
不参与完整性校验，这样升级格式时不必因版本号变化而重新签名。

规范 JSON（canonical JSON）
---------------------------
键排序 + 紧凑分隔符 + UTF-8 + 不转义非 ASCII。任何一处不一致都会导致
签名校验在不同实现间失败，因此本模块提供的 :func:`canonical_bytes` 是
**唯一允许的序列化入口**。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping

from .errors import ConfigError, RuleId
from .models import Task, _reject_unknown, _require

ENVELOPE_VERSION = "1.0"

#: 签名时间窗。超出即视为重放。
CLOCK_SKEW_SECONDS = 120


def canonical_bytes(payload: Any) -> bytes:
    """把一个可 JSON 化的对象转成**确定性**字节串。

    这是签名的唯一合法输入形式。不要在别处自己 ``json.dumps``——
    参数上任何一点差异（空格、键序、ensure_ascii）都会让签名失效。
    """

    def _default(obj: Any) -> Any:
        if isinstance(obj, Enum):
            return obj.name
        if isinstance(obj, (set, frozenset)):
            return sorted(obj)
        raise TypeError(f"无法序列化类型 {type(obj).__name__}")

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_default,
    ).encode("utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"时间戳格式非法: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def new_nonce() -> str:
    return secrets.token_hex(8)


@dataclass
class Envelope:
    """一次投递的完整描述。"""

    sender: str
    task: Task
    envelope_version: str = ENVELOPE_VERSION
    session_id: str = ""
    trace_id: str = ""
    ts: str = field(default_factory=now_iso)
    nonce: str = field(default_factory=new_nonce)
    sig: str = ""

    _FIELDS = (
        "envelope_version", "session_id", "trace_id",
        "sender", "ts", "nonce", "sig", "task",
    )

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self, *, include_sig: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "envelope_version": self.envelope_version,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "sender": self.sender,
            "ts": self.ts,
            "nonce": self.nonce,
            "task": _task_to_dict(self.task),
        }
        if include_sig:
            out["sig"] = self.sig
        return out

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any]) -> "Envelope":
        if not isinstance(obj, Mapping):
            raise ConfigError("envelope: 期望映射")
        _reject_unknown(obj, cls._FIELDS, "envelope")

        version = str(obj.get("envelope_version", ENVELOPE_VERSION))
        if version.split(".")[0] != ENVELOPE_VERSION.split(".")[0]:
            raise ConfigError(
                f"envelope: 不兼容的主版本 {version!r}（本实现支持 {ENVELOPE_VERSION}）"
            )

        raw_task = _require(obj, "task", "envelope", (Mapping,))
        return cls(
            sender=_require(obj, "sender", "envelope", (str,)),
            task=Task.from_dict(raw_task, "envelope.task"),
            envelope_version=version,
            session_id=str(obj.get("session_id", "")),
            trace_id=str(obj.get("trace_id", "")),
            ts=str(obj.get("ts", "")),
            nonce=str(obj.get("nonce", "")),
            sig=str(obj.get("sig", "")),
        )

    # ------------------------------------------------------------------
    # 签名
    # ------------------------------------------------------------------

    def signing_payload(self) -> bytes:
        """被签名的规范字节串。sig 字段本身不参与。"""
        return canonical_bytes(
            {
                "sender": self.sender,
                "ts": self.ts,
                "nonce": self.nonce,
                "task": _task_to_dict(self.task),
            }
        )

    def compute_signature(self, secret: bytes) -> str:
        mac = hmac.new(secret, self.signing_payload(), hashlib.sha256)
        return f"hmac-sha256:{mac.hexdigest()}"

    def sign(self, secret: bytes) -> "Envelope":
        self.sig = self.compute_signature(secret)
        return self

    def verify(self, secret: bytes, *, now: datetime | None = None,
               skew_seconds: int = CLOCK_SKEW_SECONDS) -> None:
        """校验签名与时间窗。

        失败时抛 :class:`AuthFailed`——**异常里只有 rule_id，没有细节**，
        因为认证失败的具体原因（签名错？时间偏？）本身可以成为探测信道。

        重放检测需要 nonce 去重，那依赖存储，因此由调用方（``edge/auth.py``）
        在通过本方法后再做一次 nonce 检查。
        """
        from .errors import AuthFailed  # 延迟导入避免循环

        if not self.sig:
            raise AuthFailed(RuleId.AU_SIGNATURE_INVALID, "missing signature")

        expected = self.compute_signature(secret)
        # compare_digest 防时序侧信道
        if not hmac.compare_digest(expected, self.sig):
            raise AuthFailed(RuleId.AU_SIGNATURE_INVALID, "signature mismatch")

        ref = now or datetime.now(timezone.utc)
        try:
            ts = parse_iso(self.ts)
        except ConfigError as exc:
            raise AuthFailed(RuleId.AU_REPLAY_DETECTED, str(exc)) from exc

        drift = abs((ref - ts).total_seconds())
        if drift > skew_seconds:
            raise AuthFailed(
                RuleId.AU_REPLAY_DETECTED, f"timestamp drift {drift:.0f}s"
            )

    def is_expired(self, now: datetime | None = None) -> bool:
        """任务自身 TTL 是否已过。过期的任务作废，不再投递。"""
        if not self.task.ttl:
            return False
        ref = now or datetime.now(timezone.utc)
        return ref > parse_iso(self.task.ttl)


# ---------------------------------------------------------------------------
# Task ←→ dict（Level 用可读的 "summary" 形式，便于人工排查日志）
# ---------------------------------------------------------------------------

def _task_to_dict(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "idempotency_key": task.idempotency_key,
        "type": task.type,
        "session_id": task.session_id,
        "from_node": task.from_node,
        "to_node": task.to_node,
        "status": task.status,
        "mode": task.mode,
        "params": dict(task.params),
        "artifacts": [
            {
                "level": str(a.level),
                "kind": a.kind,
                "data": a.data,
                "provenance": dict(a.provenance),
            }
            for a in task.artifacts
        ],
        "semantics": task.semantics,
        "ttl": task.ttl,
        "attempt": task.attempt,
        "trace_id": task.trace_id,
    }


def task_to_wire(task: Task) -> dict[str, Any]:
    """把任务序列化成**线格式**。

    与信封内的 ``task`` 表示完全一致——刻意复用同一实现，
    否则"信封里的任务"与"响应的任务"会出现两种形状，
    客户端要写两套解析，迟早不一致。
    """
    return _task_to_dict(task)


def envelope_for(secret: bytes, sender: str, task: Task, *,
                 session_id: str = "", trace_id: str = "",
                 ttl_seconds: int | None = None) -> Envelope:
    """构造并签名一个信封的便捷函数。"""
    if ttl_seconds is not None and not task.ttl:
        task.ttl = (
            datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        ).astimezone().isoformat(timespec="seconds")
    env = Envelope(
        sender=sender,
        task=task,
        session_id=session_id or task.session_id,
        trace_id=trace_id or task.trace_id or f"tr-{secrets.token_hex(4)}",
    )
    return env.sign(secret)


__all__ = [
    "CLOCK_SKEW_SECONDS",
    "ENVELOPE_VERSION",
    "Envelope",
    "canonical_bytes",
    "envelope_for",
    "new_nonce",
    "now_iso",
    "parse_iso",
    "task_to_wire",
]
