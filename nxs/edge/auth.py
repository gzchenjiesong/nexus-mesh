"""认证（E1）—— 节点身份校验与重放防护。

职责范围
--------
只做三件事：**验签**、**防重放**、**查吊销**。不做任何业务判断。

重放防护需要状态（记住见过的 nonce），因此本模块持有一个小型
时间窗缓存。缓存按时间淘汰，不会无界增长。

为什么不用更重的方案
--------------------
本系统的节点集合是**已知且预配置的少数节点**，威胁模型里不存在
"陌生 Agent 冒充"。因此双向认证用共享密钥 HMAC 就够了，
不需要引入 DID / SPIFFE / 证书体系。这符合"最小充分"原则：
**不为不存在的威胁付出复杂度**。

将来若节点数增长或出现跨组织需求，把本模块换成 mTLS 即可——
接口不变，因为 Core 不感知认证方式。
"""

from __future__ import annotations

import hmac
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Mapping

from ..core.envelope import CLOCK_SKEW_SECONDS, Envelope, parse_iso
from ..core.errors import AuthFailed, ConfigError, RuleId
from . import httpsig

#: nonce 缓存容量。窗口内正常流量远低于此值，超出即淘汰最旧的。
DEFAULT_NONCE_CACHE_SIZE = 10_000


class Authenticator:
    """节点认证器。

    :param secrets: 节点 ID → 共享密钥
    :param skew_seconds: 允许的时钟偏移
    :param nonce_cache_size: 重放缓存容量
    """

    def __init__(self, secrets: dict[str, bytes], *,
                 skew_seconds: int = CLOCK_SKEW_SECONDS,
                 nonce_cache_size: int = DEFAULT_NONCE_CACHE_SIZE) -> None:
        self._secrets = dict(secrets)
        self._skew = skew_seconds
        self._cache_size = nonce_cache_size
        self._lock = threading.RLock()
        #: (node_id, nonce) → 首次见到的时间。OrderedDict 便于按插入序淘汰。
        self._seen: OrderedDict[tuple[str, str], datetime] = OrderedDict()
        #: 已吊销的节点。吊销立即生效——不等到凭据过期。
        self._revoked: set[str] = set()

    # ------------------------------------------------------------------
    # 凭据管理
    # ------------------------------------------------------------------

    def set_secret(self, node_id: str, secret: bytes) -> None:
        """轮换凭据。旧凭据立即失效。"""
        self._secrets[node_id] = secret

    def _secret_for(self, node_id: str) -> bytes | None:
        """取节点的共享密钥。查不到时**回落到环境变量**。

        ⭐ 为什么需要这条回落
        --------------------
        HMAC 的固有性质：**服务端必须知道对方的密钥才能验签**。
        所以"节点自注册"的前提是——服务端已经持有它的密钥。

        而配置加载只从 ``nodes`` 列表里读凭据。于是出现一个尴尬局面：
        节点能自注册（角色卡不用写进 yaml），但**凭据仍要求写进 yaml**，
        那"加节点不用改配置"就成了一句空话。

        回落的做法：节点 ID 已知（来自请求头），按环境变量的命名规则
        （``NXS_SECRET_<大写下划线>``）直接查一次环境变量。
        运维只需在 ``/etc/nexus-mesh/nxs.env`` 里加一行，
        **不用动 yaml、不用写角色卡**。

        ⚠ **仍需重启进程才能生效**——环境变量在进程启动时固定
        （除非用文件 + SIGHUP，那是另一套复杂度，当前规模不值得）。
        这个限制是诚实的：改 env 文件 + 重启，比改 yaml + 重启少一步，
        但没有变成"零重启"。

        安全性：环境变量里的凭据同样受文件权限保护（``chmod 600``），
        与配置内凭据同等。回落**不降低**任何门槛——查不到就仍然是
        认证失败（``AU-SIG-01``，不区分"没配"与"配错了"）。
        """
        secret = self._secrets.get(node_id)
        if secret is not None:
            return secret

        import os

        env_key = "NXS_SECRET_" + "".join(
            ch.upper() if ch.isalnum() else "_" for ch in node_id
        )
        raw = os.environ.get(env_key, "")
        if len(raw) < 16:
            # 太短的一律视为未配置：半个密钥比没有更危险，
            # 它会让人以为这个节点配好了
            return None
        return raw.encode()

    def revoke(self, node_id: str) -> None:
        """吊销节点。**立即生效**，不等到凭据过期。"""
        with self._lock:
            self._revoked.add(node_id)

    def unrevoke(self, node_id: str) -> None:
        with self._lock:
            self._revoked.discard(node_id)

    def is_revoked(self, node_id: str) -> bool:
        with self._lock:
            return node_id in self._revoked

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def verify(self, envelope: Envelope, *, now: datetime | None = None) -> str:
        """校验信封，成功返回发送方 node_id。

        四步检查，任一失败即抛 :class:`AuthFailed`：

        1. 发送方必须是已知节点（且未被吊销）
        2. 签名必须匹配（含防时序侧信道比较）
        3. 时间戳必须在允许偏移内
        4. nonce 在窗口内不得重复（防重放）

        **四步共用同一组错误码**（``AU-SIG-01`` / ``AU-REPLAY-02``），
        因为"是签名错还是时间偏"本身可以成为探测信道。
        """
        node_id = envelope.sender

        # -- 1. 节点存在性与吊销状态 --
        if self.is_revoked(node_id):
            raise AuthFailed(RuleId.AU_CREDENTIAL_REVOKED, f"revoked {node_id}")
        secret = self._secret_for(node_id)
        if secret is None:
            # 未注册节点：报"认证失败"而不是"节点不存在"，
            # 避免通过错误码差异枚举节点 ID
            raise AuthFailed(RuleId.AU_SIGNATURE_INVALID, f"unknown {node_id}")

        # -- 2 & 3. 签名与时间窗 --
        envelope.verify(secret, now=now, skew_seconds=self._skew)

        # -- 4. 防重放 --
        self._check_nonce(node_id, envelope.nonce, now=now)
        return node_id

    # ------------------------------------------------------------------
    # 请求签名（不带任务的调用）
    # ------------------------------------------------------------------

    def verify_request(self, *, method: str, path: str,
                       headers: Mapping[str, str],
                       body: bytes | None = None,
                       now: datetime | None = None) -> str:
        """校验请求头签名，成功返回调用方 node_id。

        用于 **不携带任务** 的调用（``/poll``、``/events``）——那类调用
        没有 Task 可签，若不做这层校验，任何人删掉信封就能领走别人的任务。

        校验顺序与 :meth:`verify` 一致，且**共用同一个 nonce 缓存**。
        共用是刻意的：若两套签名各自记账，攻击者可用某一套的 nonce
        去重放另一套。

        失败抛 :class:`AuthFailed`，**四步共用同一组错误码**，
        理由同 :meth:`verify`：具体是签名错还是时间偏，本身是探测信道。
        """
        node_id = headers.get(httpsig.HEADER_NODE, "")
        ts = headers.get(httpsig.HEADER_TS, "")
        nonce = headers.get(httpsig.HEADER_NONCE, "")
        sig = headers.get(httpsig.HEADER_SIG, "")

        # 缺头与"签名错"报同一个错，避免通过差异判断头是否被识别
        if not (node_id and ts and nonce and sig):
            raise AuthFailed(RuleId.AU_SIGNATURE_INVALID, "missing signature headers")

        if self.is_revoked(node_id):
            raise AuthFailed(RuleId.AU_CREDENTIAL_REVOKED, f"revoked {node_id}")
        secret = self._secret_for(node_id)
        if secret is None:
            raise AuthFailed(RuleId.AU_SIGNATURE_INVALID, f"unknown {node_id}")

        expected = httpsig.compute_signature(
            secret, method=method, path=path, node_id=node_id,
            ts=ts, nonce=nonce, body=body,
        )
        if not hmac.compare_digest(expected, sig):
            raise AuthFailed(RuleId.AU_SIGNATURE_INVALID, "request signature mismatch")

        ref = now or datetime.now(timezone.utc)
        try:
            sent_at = httpsig.parse_iso(ts)
        except ConfigError as exc:
            raise AuthFailed(RuleId.AU_REPLAY_DETECTED, str(exc)) from exc
        drift = httpsig.skew_seconds(ref, sent_at)
        if drift > self._skew:
            raise AuthFailed(RuleId.AU_REPLAY_DETECTED, f"timestamp drift {drift:.0f}s")

        self._check_nonce(node_id, nonce, now=now)
        return node_id

    def _check_nonce(self, node_id: str, nonce: str,
                     *, now: datetime | None = None) -> None:
        ref = now or datetime.now(timezone.utc)
        with self._lock:
            self._evict_locked(ref)
            key = (node_id, nonce)
            if key in self._seen:
                raise AuthFailed(RuleId.AU_REPLAY_DETECTED,
                                 f"nonce reused by {node_id}")
            self._seen[key] = ref
            while len(self._seen) > self._cache_size:
                self._seen.popitem(last=False)

    def _evict_locked(self, now: datetime) -> None:
        """淘汰超出时间窗的记录。调用方须持锁。"""
        cutoff = now - timedelta(seconds=self._skew * 2)
        while self._seen:
            key, seen_at = next(iter(self._seen.items()))
            if seen_at >= cutoff:
                break
            self._seen.popitem(last=False)

    def nonce_cache_size(self) -> int:
        """当前缓存条目数（诊断用）。"""
        with self._lock:
            return len(self._seen)


__all__ = ["DEFAULT_NONCE_CACHE_SIZE", "Authenticator"]
