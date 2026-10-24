"""请求签名 —— 不携带任务的调用的身份证明。

为什么需要第二套签名
--------------------
信封（``core/envelope.py``）的签名输入是 **任务内容**，因此它天然只能用于
"带任务的调用"（投递、回传）。而 ``/poll``、``/events`` 这类调用**不携带任务**，
却又必须证明调用方是谁 —— 否则任何人都能来领取别人的任务。

于是分两层：

============  ==========================  ==============================
层            签名输入                    用于
============  ==========================  ==============================
信封签名      sender ‖ ts ‖ nonce ‖ task   ``/publish`` ``/result`` ``/fail``
请求签名      方法 ‖ 路径 ‖ 节点 ‖ ts ‖ nonce ‖ body 摘要
                                          ``/poll`` ``/events``
============  ==========================  ==============================

**两套签名共用同一个 nonce 缓存与时间窗**（都在 ``edge/auth.py`` 的
``Authenticator`` 里）。这是刻意的：如果分开计，攻击者就能用一套的
nonce 去重放另一套。

签名为什么要覆盖方法与路径
--------------------------
只签 body 的话，攻击者可以把一个合法的 ``/poll`` 请求原样重放成
``/result``（甚至改路径到 ``/publish``）。把方法与路径纳入签名输入后，
**请求的语义本身被签名保护**，改动即失效。

查询串包含在 ``path`` 里一起签，避免"路径合法但参数被改"。

为什么不带请求头
----------------
头部（User-Agent、Host 等）由中间设备改写是常态。签进去会让系统在
反向代理后面莫名失效。**只签对语义有影响的东西。**
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Mapping

from ..core.envelope import canonical_bytes, now_iso

#: 签名相关请求头。集中定义，避免客户端与服务端写错字面量。
HEADER_NODE = "X-NXS-Node"
HEADER_TS = "X-NXS-Ts"
HEADER_NONCE = "X-NXS-Nonce"
HEADER_SIG = "X-NXS-Sig"

#: 客户端需要发出的全部签名头。
SIGNATURE_HEADERS = (HEADER_NODE, HEADER_TS, HEADER_NONCE, HEADER_SIG)

#: 签名的标签前缀。与信封签名统一，便于审计里一眼看出用了哪种凭据。
SIG_PREFIX = "hmac-sha256:"


def new_nonce() -> str:
    """生成一次性随机值。与信封的 nonce 同一量级（8 字节）。"""
    return secrets.token_hex(8)


def body_digest(body: bytes | None) -> str:
    """请求体摘要。

    **空体也要参与签名**（用空串的 SHA256），否则"有没有请求体"
    本身成为可被改动的自由度。
    """
    return hashlib.sha256(body or b"").hexdigest()


def request_payload(*, method: str, path: str, node_id: str, ts: str,
                    nonce: str, body: bytes | None = None) -> bytes:
    """构造被签名的规范字节串。

    :param path: **包含查询串**的完整路径（如 ``/nxs/poll?wait=0``）
    """
    return canonical_bytes({
        "body_sha256": body_digest(body),
        "method": method.upper(),
        "node": node_id,
        "nonce": nonce,
        "path": path,
        "ts": ts,
    })


def compute_signature(secret: bytes, *, method: str, path: str, node_id: str,
                      ts: str, nonce: str, body: bytes | None = None) -> str:
    mac = hmac.new(
        secret,
        request_payload(method=method, path=path, node_id=node_id,
                        ts=ts, nonce=nonce, body=body),
        hashlib.sha256,
    )
    return SIG_PREFIX + mac.hexdigest()


def sign_request(secret: bytes, *, method: str, path: str, node_id: str,
                 ts: str | None = None, nonce: str | None = None,
                 body: bytes | None = None) -> dict[str, str]:
    """生成一组可直接放进请求头的签名头。"""
    ts = ts or now_iso()
    nonce = nonce or new_nonce()
    sig = compute_signature(secret, method=method, path=path, node_id=node_id,
                            ts=ts, nonce=nonce, body=body)
    return {
        HEADER_NODE: node_id,
        HEADER_TS: ts,
        HEADER_NONCE: nonce,
        HEADER_SIG: sig,
    }


def parse_iso(value: str) -> datetime:
    """时间戳解析。与信封共用同一实现，避免两处格式判定不一致。"""
    from ..core.envelope import parse_iso as _parse

    return _parse(value)


def skew_seconds(a: datetime, b: datetime) -> float:
    return abs((a - b).total_seconds())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def missing_headers(headers: Mapping[str, str]) -> tuple[str, ...]:
    """返回缺失的签名头名字。用于给出**不泄露身份信息**的统一错误。"""
    return tuple(name for name in SIGNATURE_HEADERS if not headers.get(name))


__all__ = [
    "HEADER_NODE",
    "HEADER_NONCE",
    "HEADER_SIG",
    "HEADER_TS",
    "SIG_PREFIX",
    "SIGNATURE_HEADERS",
    "body_digest",
    "compute_signature",
    "missing_headers",
    "new_nonce",
    "parse_iso",
    "request_payload",
    "sign_request",
    "skew_seconds",
    "utcnow",
]
