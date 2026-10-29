"""网关（M1）—— HTTP 传输层。

职责范围
--------
只做四件事：**收请求、验身份、限流、转交给 Core**。
它**不做任何业务判断**：能不能发、能不能给，分别归 M5 策略执行点与
M10 应答边界。网关只回答"你是谁、你有没有超速、这个包能不能收"。

为什么网关不能做业务判断
------------------------
一旦网关开始理解任务内容或权限，它就成了第二个决策点。两个决策点意味着
两个可能不一致的实现，而**不一致的那一处就是绕过口**。所以这里刻意只做
机械的搬运。

与同进程传输的关系
------------------
网关把请求转交给 :class:`nxs.app.InProcessTransport`，
因此**与 demo 走的是同一套代码路径**（策略 → 投递 → 边界 → 生命周期）。
网络层只增加两件事：序列化与认证。这也是为什么 demo 验证过的行为
在真实部署里同样成立。

Egress-Only 如何被保持
----------------------
**节点侧永不监听端口。** 节点发起连接到本网关（出站），网关只被动接受。
连推送（SSE）也是**节点先出站订阅**、网关再沿这条已建立的连接写数据——
不存在"网关主动连接节点"的路径。这是拓扑事实，不是编码约定。

两套签名
--------
* 带任务的调用（``/publish`` ``/result`` ``/fail``）→ 用信封签名，
  签名覆盖任务内容
* 不带任务的调用（``/poll`` ``/events``）→ 用请求签名，签名覆盖
  方法 + 路径 + 节点 + 时间 + nonce（见 ``edge/httpsig.py``）

SSE 只推通知，不推数据
----------------------
事件流里只有"你有活要取"这一个信号，**不含任务内容、不含节点清单**。
原因是事件流不参与信封签名与防重放，**不能作为数据通道**。
这样即使某条流被旁路监听，也没有内容可泄露；而取任务仍要一次
带签名的 ``/poll``，防重放与权限校验一个都不少。

这也顺带回答了设计文档里"不做长轮询"的顾虑：**领取路径仍然是短轮询**，
SSE 只是把"什么时候去轮询"从固定周期变成事件驱动。流被中间设备切断时，
节点退回固定周期轮询即可，功能不受影响。
"""

from __future__ import annotations

import json
import logging
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

from ..app import InProcessTransport, NexusApp
from ..core.envelope import Envelope, task_to_wire
from ..core.errors import (
    AuthFailed,
    ConfigError,
    NexusError,
    PolicyDenied,
    RuleId,
    safe_message,
)
from ..core.models import Artifact, RoleCard, Task, TaskStatus
from ..mgmt.config import ENV_OPS_SECRET, MIN_SECRET_LENGTH, OPS_NODE_ID, ServerConfig
from ..mgmt.audit import AuditError, make_record
from . import httpsig
from .auth import Authenticator

log = logging.getLogger("nxs.edge.gateway")

#: 所有端点前缀。与 ``nxs`` 技术标识符一致（见命名体系）。
PREFIX = "/nxs"

#: 读取一个请求（请求行 + 头 + 体）的套接字超时，防慢速攻击。
#: 慢速攻击的特征是"每个字节都合法，但永远发不完"——超时是唯一有效的对策。
REQUEST_TIMEOUT_SECONDS = 15

#: SSE 心跳间隔上限（配置值更小则用配置值）。
_MAX_KEEPALIVE = 60


# ---------------------------------------------------------------------------
# 事件中枢
# ---------------------------------------------------------------------------

class EventHub:
    """SSE 通知中枢。

    **只保存"哪个节点被通知过"的代数计数，不保存任何内容。**
    这是一个刻意的设计：中枢里没有可泄露的东西，因此它被攻破
    也不会造成内容泄露。

    代数（generation）而不是标志位，是为了避免"通知落在两次等待之间"的丢失：
    等待方记住自己见过的代数，返回时对比，不同即说明有新事件。
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._gen: dict[str, int] = {}
        self._closed = False

    def bump(self, node_id: str) -> None:
        """通知某节点"可能有活"。**不携带内容。**"""
        with self._cv:
            self._gen[node_id] = self._gen.get(node_id, 0) + 1
            self._cv.notify_all()

    def generation(self, node_id: str) -> int:
        with self._cv:
            return self._gen.get(node_id, 0)

    def wait_change(self, node_id: str, seen: int, timeout: float) -> bool:
        """等待代数相对 ``seen`` 发生变化。

        返回 ``True`` 表示有新事件；``False`` 表示超时或中枢已关闭。

        **调用方应在发送响应头之前先取一次 :meth:`generation` 作为 ``seen``**，
        否则"握手期间发生的事件"会被漏掉（要等下一个心跳周期才发现）。
        """
        deadline = time.monotonic() + timeout
        with self._cv:
            while not self._closed and self._gen.get(node_id, 0) == seen:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
            return not self._closed and self._gen.get(node_id, 0) != seen

    def close(self) -> None:
        """关闭中枢，唤醒所有等待者。**优雅关闭时调用。**"""
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    @property
    def closed(self) -> bool:
        with self._cv:
            return self._closed


# ---------------------------------------------------------------------------
# 内部控制流异常
# ---------------------------------------------------------------------------

class _Reject(Exception):
    """传输层拒绝。携带 HTTP 状态码与规则标识符。

    与 :class:`PolicyDenied` 区分：那个是**策略**判定，
    这个是**传输层**判定（体积、并发、格式、速率）。两者最终以相同的
    三字段格式返回，但审计里的 rule_id 能区分来源。
    """

    def __init__(self, status: int, rule_id: RuleId,
                 detail: str | None = None) -> None:
        super().__init__(rule_id.value)
        self.status = status
        self.rule_id = rule_id
        self.detail = detail

    def to_wire(self) -> dict[str, str]:
        return {
            "denied": "true",
            "rule_id": self.rule_id.value,
            "message": safe_message(self.rule_id),
        }


# ---------------------------------------------------------------------------
# 速率限制
# ---------------------------------------------------------------------------

class RateLimiter:
    """每节点每秒请求数限制（固定窗口计数）。

    为什么放在传输层而不是策略层：策略层的配额是**业务**配额
    （每天多少任务、多少字节），回答"这个节点该不该做这件事"；
    这里是**自保**，回答"这台机器会不会被压垮"。两者触发源不同、
    处置也不同，混在一起会让策略层承担它不该承担的职责。

    **必须在认证之后执行**：限流需要身份。先限流再认证的话，
    攻击者可以伪造任意节点 ID 来互相"借"配额。

    固定窗口的已知代价：窗口边界处允许瞬时 2× 突发。
    对自保而言可接受——精确的滑动窗口要存每个请求的时间戳，
    内存随流量增长，而它防的不是精确计费。**明确不做滑动窗口。**
    """

    def __init__(self, limit_per_second: int) -> None:
        self._limit = max(0, int(limit_per_second))
        self._lock = threading.Lock()
        #: node_id → (当前秒桶, 已用次数)。键只可能是已认证节点，
        #: 因此字典大小由节点数决定，不会因流量增长。
        self._windows: dict[str, tuple[int, int]] = {}

    @property
    def limit(self) -> int:
        return self._limit

    def allow(self, node_id: str, *, now: float | None = None) -> bool:
        """记一次请求并返回是否放行。``limit<=0`` 表示不限。"""
        if self._limit <= 0:
            return True
        second = int(now if now is not None else time.monotonic())
        with self._lock:
            bucket, used = self._windows.get(node_id, (second, 0))
            if bucket != second:
                bucket, used = second, 0
            used += 1
            self._windows[node_id] = (bucket, used)
            return used <= self._limit

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


#: 规则 → HTTP 状态码。刻意集中定义，避免每个处理函数各写一套。
_STATUS_BY_RULE: dict[RuleId, int] = {
    RuleId.FM_ENVELOPE_INVALID: 400,
    RuleId.FM_TASK_PARAM_INVALID: 400,
    RuleId.FM_CARDCARD_INVALID: 400,
    RuleId.FM_BODY_TOO_LARGE: 413,
    RuleId.AU_SIGNATURE_INVALID: 401,
    RuleId.AU_REPLAY_DETECTED: 401,
    RuleId.AU_CREDENTIAL_REVOKED: 401,
    RuleId.AU_NODE_UNKNOWN: 401,
    RuleId.MX_DOMAIN_FORBIDDEN: 403,
    RuleId.MX_ISSUER_NOT_ALLOWED: 403,
    RuleId.AU_NOT_APPROVED: 403,
    RuleId.WL_TYPE_NOT_REGISTERED: 404,
    RuleId.WL_FIELD_NOT_ALLOWED: 403,
    RuleId.WL_LEVEL_EXCEEDED: 403,
    RuleId.WL_FREETEXT_NOT_ALLOWED: 403,
    RuleId.QT_BACKLOG_FULL: 429,
    RuleId.QT_RATE_LIMITED: 429,
    RuleId.LF_TASK_NOT_FOUND: 404,
    RuleId.QT_CONNECTION_LIMIT: 503,
    RuleId.LF_LEASE_HELD: 409,
    RuleId.LF_TASK_EXPIRED: 410,
    RuleId.LF_INVALID_TRANSITION: 409,
    RuleId.LF_DUPLICATE_IDEMPOTENCY: 409,
}


def status_for(rule_id: RuleId) -> int:
    return _STATUS_BY_RULE.get(rule_id, 400)


def _ops_secret_from_env() -> bytes | None:
    """从环境变量取运维密钥。

    **过短的一律视为未配置**——半个密钥比没有密钥更危险，
    因为它会让人以为运维面是受保护的。
    """
    import os

    raw = os.environ.get(ENV_OPS_SECRET, "")
    if len(raw) < MIN_SECRET_LENGTH:
        if raw:
            log.warning(
                "%s 太短（<%d 字节），运维接口未启用",
                ENV_OPS_SECRET, MIN_SECRET_LENGTH,
            )
        return None
    return raw.encode()


def _require_node_id(payload: Mapping[str, Any]) -> str:
    """从请求体取 ``node_id``，缺失即拒。"""
    node_id = payload.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        raise _Reject(400, RuleId.FM_CARDCARD_INVALID, "missing node_id")
    return node_id


# ---------------------------------------------------------------------------
# 网关
# ---------------------------------------------------------------------------

@dataclass
class GatewayStats:
    """运行计数。**只记数量，不记内容。**"""

    requests: int = 0
    rejected: int = 0
    sse_streams_opened: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)

    def hit(self, rule_id: RuleId) -> None:
        self.rejected += 1
        self.by_rule[rule_id.value] = self.by_rule.get(rule_id.value, 0) + 1


class Gateway:
    """HTTP 网关。

    用法::

        gw = Gateway(app)
        gw.start()            # 非阻塞，随机端口时 gw.port 为实际端口
        ...
        gw.stop()             # 优雅关闭

    :param server_config: 省略时取 ``app.config.server``
    """

    def __init__(self, app: NexusApp, *,
                 server_config: ServerConfig | None = None,
                 authenticator: Authenticator | None = None,
                 transport: InProcessTransport | None = None,
                 event_hub: EventHub | None = None,
                 ops_secret: bytes | None = None) -> None:
        self.app = app
        self.server_config = server_config or app.config.server
        self.transport = transport or app.transport
        self.events = event_hub or EventHub()
        self.authenticator = authenticator or Authenticator(
            {node_id: secret for node_id, secret in app.config.secrets.items()}
        )
        self.rate_limiter = RateLimiter(app.config.policy.rate_limit_per_second)
        self.stats = GatewayStats()
        #: 上一次指标快照，用于判断趋势（见 :meth:`collect_metrics`）。
        self._last_snapshot: Any = None

        # 运维面：**默认关闭**。没配密钥时 /nxs/ops/* 返回 501，
        # 攻击面上就不存在这个端点，而不是"存在但拒绝"。
        if ops_secret is None:
            ops_secret = _ops_secret_from_env()
        self.ops_enabled = bool(ops_secret)
        if ops_secret:
            self.authenticator.set_secret(OPS_NODE_ID, ops_secret)

        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        #: accept 循环已开始。用于避免 stop() 在 serve_forever 尚未启动时死等。
        self._serving = threading.Event()
        #: 常规请求并发上限。**SSE 单独计**，否则少量长连接就能把
        #: 短请求饿死——那会让"推送可用"反而降低可用性。
        self._request_slots = threading.BoundedSemaphore(
            max(1, self.server_config.max_concurrent_requests)
        )
        self._sse_slots = threading.BoundedSemaphore(
            max(1, self.server_config.max_sse_streams)
        )
        #: 已打开的流：node_id → 流计数。同一节点重连时旧流会被要求退出，
        #: 避免"断线重连"在服务端堆积僵尸流。
        self._stream_gen: dict[str, int] = {}
        self._stream_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def port(self) -> int:
        if self._httpd is None:
            return self.server_config.port
        return self._httpd.server_address[1]

    @property
    def host(self) -> str:
        return self.server_config.host

    @property
    def base_url(self) -> str:
        scheme = "https" if self.server_config.tls_cert else "http"
        return f"{scheme}://{self.host}:{self.port}{PREFIX}"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> "Gateway":
        """绑定端口并在后台线程里运行。**非阻塞。**"""
        httpd = _make_httpd(self)
        self._httpd = httpd
        self._thread = threading.Thread(
            target=self._serve_locked, name="nxs-gateway", daemon=True
        )
        self._thread.start()
        # 等 accept 循环真正开始，否则紧接着的 stop() 会在 shutdown() 上空等
        self._serving.wait(timeout=5)
        log.info("网关已启动 %s", self.base_url)
        return self

    def _serve_locked(self) -> None:
        """后台线程入口：先亮灯，再进入 accept 循环。"""
        self._serving.set()
        try:
            self._httpd.serve_forever()   # type: ignore[union-attr]
        finally:
            self._serving.clear()

    def serve_forever(self) -> None:
        """在当前线程阻塞运行（用于 ``nxs serve``）。"""
        httpd = _make_httpd(self)
        self._httpd = httpd
        self._serving.set()
        try:
            httpd.serve_forever()
        finally:
            self._serving.clear()
            httpd.server_close()

    def stop(self) -> None:
        """优雅关闭。

        顺序有意义：**先关门，再唤醒推送流，最后停 accept 循环。**

        先置停止标志并关闭事件中枢——这样已建立的 SSE 流会自己退出循环，
        不会在 ``shutdown()`` 之后还在往套接字写。反过来做的话，
        关闭过程中会有一批 "broken pipe" 噪声日志，
        更糟的是掩盖真正的错误。
        """
        if self._stopping.is_set():
            return
        self._stopping.set()
        self.events.close()

        httpd = self._httpd
        if httpd is not None:
            # 只在 accept 循环确实跑起来时才 shutdown——否则它会一直等
            # 一个永远不会结束的循环，表现为"关不掉"。
            if self._serving.is_set():
                httpd.shutdown()
            httpd.server_close()

        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        log.info("网关已停止")

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    # ------------------------------------------------------------------
    # 并发与流登记
    # ------------------------------------------------------------------

    def acquire_request_slot(self) -> bool:
        return self._request_slots.acquire(blocking=False)

    def release_request_slot(self) -> None:
        try:
            self._request_slots.release()
        except ValueError:  # pragma: no cover - 重复释放是编码错误
            log.debug("请求槽重复释放")

    def acquire_sse_slot(self) -> bool:
        return self._sse_slots.acquire(blocking=False)

    def release_sse_slot(self) -> None:
        try:
            self._sse_slots.release()
        except ValueError:  # pragma: no cover
            log.debug("SSE 槽重复释放")

    def open_stream(self, node_id: str) -> int:
        """登记一条新流，并让该节点**旧流失效**。返回本流代号。"""
        with self._stream_lock:
            gen = self._stream_gen.get(node_id, 0) + 1
            self._stream_gen[node_id] = gen
            self.stats.sse_streams_opened += 1
            return gen

    def stream_alive(self, node_id: str, gen: int) -> bool:
        with self._stream_lock:
            return self._stream_gen.get(node_id, 0) == gen

    # ------------------------------------------------------------------
    # 可观测（M13）
    # ------------------------------------------------------------------

    def collect_metrics(self) -> dict[str, Any]:
        """采集指标 + 系统级关联异常。

        ⭐ **只挂在运维面上，不给节点。**

        指标是拓扑与活跃度信息：
        "某节点每天处理多少任务"、"拒绝集中在哪条规则"、"谁在积压"——
        这些单独看都不敏感，合起来就是一张**网络规模与使用习惯的画像**。
        节点没有理由需要这份画像；它的视角应该只有自己的任务。

        这与候选名单的处理是同一个理由（见 ``InProcessTransport.candidates``），
        与审计"只记元数据"也是同一条线：**能不给的就不给**。

        ⚠ **趋势判定要靠两次调用。**
        ``silent_stall`` 需要"积压是否在增长"，而单次快照看不出趋势。
        所以本方法把上一次快照存在网关实例上，下次调用时用来对比。
        副作用是：**第一次调用拿不到趋势类告警**——那是信息不足，
        不是漏报，比凭空猜一个"可能在增长"要诚实。
        """
        from ..mgmt.observ import collect, detect_anomalies

        snap = collect(
            self.app.metrics,
            registry=self.app.registry,
            queue=self.app.stores.queue,
            gateway_stats=self.stats,
        )
        anomalies = detect_anomalies(snap, previous=self._last_snapshot)
        self._last_snapshot = snap

        payload = snap.as_dict()
        payload["anomalies"] = [
            {
                "rule": a.rule,
                "severity": a.severity,
                "subject": a.subject,
                "message": a.message,
                "rationale": a.rationale,
                "suggestion": a.suggestion,
                "evidence": dict(a.evidence),
            }
            for a in anomalies
        ]
        return payload

    # ------------------------------------------------------------------
    # 注册：域裁定与审计
    # ------------------------------------------------------------------

    def resolve_domain(self, card: RoleCard) -> RoleCard:
        """裁定节点最终归属的信任域。

        ⭐ **配置优先于节点自报。** 这个顺序不能反。

        为什么：``domain`` 决定这个节点适用哪一组「域 × 类型」矩阵规则。
        如果让节点自己决定，它可以声称 ``domain="coordinator"``——
        而 coordinator 域通常拥有发布权（``coordinator → zone-a`` 可以派任务）。
        运维批准时如果没细看，就等于给了一个可发任务的节点。

        **信任域是运维划的边界，不是节点自己挑的房间。**

        配置里没有该节点时（纯自注册的新节点），才采信它自报的域——
        那时它一定处于未批准状态，运维能在 ``nxs status`` 里看到
        它自称属于哪个域，再决定批不批。
        """
        from dataclasses import replace as _replace

        configured = self.app.config.nodes.get(card.node_id)
        if configured is None:
            return card
        if configured.domain == card.domain:
            return card
        log.info(
            "节点 %s 自报域 %r 被配置覆盖为 %r",
            card.node_id, card.domain, configured.domain,
        )
        return _replace(card, domain=configured.domain)

    def record_registration(self, node_id: str, outcome: str, record: Any) -> None:
        """记录一次注册事件。

        注册是安全相关的运维事件（等于"一个节点声明了它能做什么"），
        必须留痕。**但不记录角色卡内容**——能力描述是节点自己写的文本，
        记进审计等于让未验证的输入进入唯一的追溯依据。

        只记：谁、什么结果、第几版、是否已批准。
        """
        try:
            self.app.audit.append(make_record(
                outcome=f"register:{outcome}",
                from_node=node_id, to_node=node_id,
                revision=record.revision,
                approved=str(record.approved),
                domain=record.card.domain,
            ))
        except AuditError as exc:  # pragma: no cover - 磁盘故障
            log.warning("审计写入失败，注册事件未记录: %s", exc)

    def record_ops(self, action: str, target: str) -> None:
        """记录一次运维动作。

        运维动作直接改变权限（批准/吊销），**必须留痕**——
        否则"谁在什么时候给谁放了行"将无法回答。
        """
        try:
            self.app.audit.append(make_record(
                outcome=f"ops:{action}",
                from_node=OPS_NODE_ID, to_node=target,
            ))
        except AuditError as exc:  # pragma: no cover - 磁盘故障
            log.warning("审计写入失败，运维动作未记录: %s", exc)

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    def audit_transport_reject(self, rule_id: RuleId, *, method: str,
                               path: str) -> None:
        """记录一次传输层拒绝。

        **刻意不记录调用方声称的节点 ID。** 这个值在校验失败时是
        **攻击者可控的文本**，记进去等于让未认证输入进入审计。
        同理只记 ``method`` 与 ``path``，且 path 只保留端点而不带查询串。
        """
        path = path.split("?", 1)[0]
        try:
            self.app.audit.append(make_record(
                outcome="denied", rule_id=rule_id.value,
                task_type="", session_id="",
                # 用固定标记代替自称的节点 ID，避免未认证内容入账
                from_node="(unauthenticated)",
                to_node="",
                method=method, endpoint=path,
            ))
        except AuditError as exc:  # pragma: no cover - 磁盘故障
            log.warning("审计写入失败，本次传输层拒绝未记录: %s", exc)


# ---------------------------------------------------------------------------
# 服务器装配
# ---------------------------------------------------------------------------

def _make_httpd(gateway: Gateway) -> ThreadingHTTPServer:
    cfg = gateway.server_config

    class _Server(ThreadingHTTPServer):
        daemon_threads = True      # 关闭时不等待僵尸连接
        allow_reuse_address = True
        gateway_instance = gateway

        def handle_error(self, request, client_address):  # noqa: D102
            # 客户端提前断开是常态（尤其是 SSE 重连），不是错误
            import sys
            exc = sys.exc_info()[1]
            if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                log.debug("客户端断开 %s", client_address)
                return
            log.warning("处理请求出错 %s: %s", client_address, exc)

    httpd = _Server((cfg.host, cfg.port), _Handler)

    if cfg.tls_cert and cfg.tls_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(cfg.tls_cert, cfg.tls_key)
        except (OSError, ssl.SSLError) as exc:
            raise ConfigError(f"TLS 证书加载失败: {exc}") from exc
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        log.info("已启用 TLS")

    return httpd


# ---------------------------------------------------------------------------
# 请求处理
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """单个请求的处理。**HTTP/1.0 语义**：一请求一连接，响应必带长度。

    刻意不用 HTTP/1.1 的 keep-alive：那会让"并发上限"失去意义
    （一条连接可以承载无限请求），也让 SSE 与短请求共用连接时的
    状态管理复杂化。节点侧用短连接，代价可忽略。
    """

    protocol_version = "HTTP/1.0"
    server_version = "nxs-gateway"
    sys_version = ""

    # -- 日志：默认实现会写 stderr，且在日志里带上客户端地址与请求行。
    #    v0.1 用 logging 统一，级别降到 debug 避免噪声。
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102
        log.debug("%s - %s", self.address_string(), fmt % args)

    def setup(self) -> None:
        """连接建立时先设超时，**早于读取请求行与请求头**。

        踩过的坑：最初把 ``settimeout`` 写在 :meth:`_dispatch` 里，
        而那时 ``BaseHTTPRequestHandler`` 已经把请求头读完了 ——
        于是"只发一半请求头就停住"的慢速攻击完全不受保护，
        一个连接就能永久占住一个线程。

        **超时必须设在读之前，否则它保护的是已经过去的阶段。**
        """
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    @property
    def gw(self) -> Gateway:
        return self.server.gateway_instance  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_status(405, "method not allowed")

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_status(405, "method not allowed")

    def do_HEAD(self) -> None:  # noqa: N802
        self._reject_status(405, "method not allowed")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._reject_status(405, "method not allowed")

    def _dispatch(self, method: str) -> None:
        gw = self.gw
        gw.stats.requests += 1

        path, _, query = self.path.partition("?")
        full_path = self.path            # 签名要用含查询串的完整路径

        try:
            if path == f"{PREFIX}/health" and method == "GET":
                self._send_json(200, {"status": "ok"})
                return

            # ★ SSE 在**取请求槽之前**分流。
            #   它是一条长连接，若占用常规请求槽，几条流就能把短请求饿死——
            #   那会让"推送可用"反而降低整体可用性。它用独立的流配额。
            if path == f"{PREFIX}/events" and method == "GET":
                self._handle_events(full_path)
                return

            # ★ 运维面也在请求槽之外，且**不限流**。
            #   理由：节点流量洪水时正是最需要运维介入的时候。
            #   若运维通道与节点共用配额，故障时就会被自己的保护机制锁在门外。
            if path.startswith(f"{PREFIX}/ops/"):
                self._handle_ops(path[len(f"{PREFIX}/ops/"):], method, full_path)
                return

            if not gw.acquire_request_slot():
                raise _Reject(503, RuleId.QT_CONNECTION_LIMIT, "too many concurrent requests")
            try:
                if path == f"{PREFIX}/poll" and method == "GET":
                    self._handle_poll(full_path, query)
                elif path == f"{PREFIX}/publish" and method == "POST":
                    self._handle_publish(full_path)
                elif path == f"{PREFIX}/result" and method == "POST":
                    self._handle_result(full_path)
                elif path == f"{PREFIX}/fail" and method == "POST":
                    self._handle_fail(full_path)
                elif path == f"{PREFIX}/register" and method == "POST":
                    self._handle_register(full_path)
                elif path == f"{PREFIX}/ack" and method == "POST":
                    self._handle_ack(full_path)
                elif path == f"{PREFIX}/whoami" and method == "GET":
                    self._handle_whoami(full_path)
                elif path.startswith(f"{PREFIX}/task/") and method == "GET":
                    self._handle_task(full_path, path[len(f"{PREFIX}/task/"):])
                elif path in _KNOWN_PATHS or path.startswith(_PATH_PREFIXES):
                    self._reject_status(405, "method not allowed")
                else:
                    # 未实现的端点：诚实返回 501，而不是 404
                    self._reject_status(501, "not implemented")
            finally:
                gw.release_request_slot()

        except _Reject as rej:
            gw.stats.hit(rej.rule_id)
            gw.audit_transport_reject(rej.rule_id, method=method, path=path)
            self._send_json(rej.status, rej.to_wire())
        except AuthFailed as exc:
            gw.stats.hit(exc.rule_id)
            gw.audit_transport_reject(exc.rule_id, method=method, path=path)
            log.info("认证失败 %s %s: %s", method, path, exc.detail)
            self._send_json(status_for(exc.rule_id),
                            PolicyDenied(exc.rule_id).to_wire())
        except PolicyDenied as exc:
            self._send_json(status_for(exc.rule_id), exc.to_wire())
        except ConfigError as exc:
            # 格式非法：**不回显解析细节**，只给固定措辞
            log.info("格式校验失败 %s %s: %s", method, path, exc)
            self._send_json(400, PolicyDenied(RuleId.FM_ENVELOPE_INVALID).to_wire())
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            log.debug("客户端在响应前断开")
        except NexusError as exc:  # pragma: no cover - 兜底
            log.warning("请求处理失败 %s %s: %s", method, path, exc)
            self._send_json(400, PolicyDenied(RuleId.FM_ENVELOPE_INVALID).to_wire())

    # ------------------------------------------------------------------
    # 端点：poll
    # ------------------------------------------------------------------

    def _handle_poll(self, full_path: str, query: str) -> None:
        """领取任务。**空队列返回 204**（见协议 §11）。"""
        node_id = self._authenticate(full_path, body=None)
        task = self.gw.transport.poll(node_id)
        if task is None:
            self._send_status(204)
            return
        self._send_json(200, {"task": task_to_wire(task)})

    # ------------------------------------------------------------------
    # 端点：publish / result / fail
    # ------------------------------------------------------------------

    def _handle_publish(self, full_path: str) -> None:
        body = self._read_body()
        sender = self._authenticate_envelope(full_path, body)
        envelope = Envelope.from_dict(self._parse_json(body))
        task = envelope.task

        # ★ ``to_node`` 为空字符串 = 请求按能力自动匹配（M4）。
        #
        #   为什么用空串而不是"字段可省略"：投给谁是一个关键决策，
        #   而"漏写 to_node"和"故意不指定"在报文上长得一样。
        #   保持字段必填，让漏写立刻报"缺少必填字段"，
        #   而写空串是一个明确的、看得出来是故意的选择。
        result = self.gw.transport.publish(
            # ★ 发送方取自**已验签的**信封，而不是信任 body 里的 from_node——
            #   两者本该一致，但只有前者不可伪造。
            from_node=sender,
            to_node=task.to_node or None,
            task_type=task.type,
            params=dict(task.params),
            session_id=task.session_id or envelope.session_id,
            idempotency_key=task.idempotency_key,
        )
        self.gw.events.bump(result.task.to_node)
        self._send_json(200, {
            "task": task_to_wire(result.task),
            "created": result.created,
            "effective_max_level": result.effective_max_level,
        })

    def _handle_result(self, full_path: str) -> None:
        body = self._read_body()
        sender = self._authenticate_envelope(full_path, body)
        envelope = Envelope.from_dict(self._parse_json(body))
        task = self._authoritative_task(sender, envelope.task)

        artifacts = list(envelope.task.artifacts)
        done = self.gw.transport.complete(sender, task, artifacts)
        self._send_json(200, {"task": task_to_wire(done)})

    def _handle_fail(self, full_path: str) -> None:
        body = self._read_body()
        sender = self._authenticate_envelope(full_path, body)
        envelope = Envelope.from_dict(self._parse_json(body))
        task = self._authoritative_task(sender, envelope.task)

        # 失败原因只作为诊断信息，取自 params 的保留键——**不额外加字段**，
        # 因为 Task 的字段集是严格校验的（_reject_unknown）。
        reason = str(dict(envelope.task.params).get("reason", ""))[:200]
        failed = self.gw.transport.fail(sender, task, reason)
        self._send_json(200, {"task": task_to_wire(failed)})

    def _handle_whoami(self, full_path: str) -> None:
        """我是谁 —— 返回调用方自己的角色卡与当前状态。

        ⭐ 为什么需要这个端点
        --------------------
        Agent 需要一种**零成本、不需要猜**的方式确认自己的身份：
        我叫什么、有什么能力、能被谁委托、配额还剩多少、批准了没有。

        没有它的话，Agent 只能靠读文档推断 —— 而文档会过时，
        且推断错了不会有任何报错（它会以错误的假设去发任务）。

        为什么不复用 ``/nxs/ops/nodes``：那需要**运维密钥**。
        Agent 不该有运维密钥 —— 那等于把"批准/吊销节点"的钥匙
        交给被管理者。这里的语义是"看我自己"，只需要自己的凭据。

        ## 只回自己的
        刻意**不**支持查别人 —— ``?node=x`` 这种参数一旦存在，
        就会变成一个轻量的节点发现接口，而节点发现该有它自己的
        权衡（哪些节点可见、能力是否暴露）。要列全网成员，
        应该新增专用的 ``/nxs/peers``，而不是把这个口子放开。
        """
        node_id = self._authenticate(full_path, body=None)

        card = self.gw.app.registry.card(node_id)
        if card is None:
            # 未注册就调 whoami：不是错误，只是告诉它"你还没注册"
            self._send_json(404, {
                "denied": "true",
                "rule_id": RuleId.AU_NODE_UNKNOWN.value,
                "hint": "该身份尚未注册。首次接入请运行桥接进程注册一次。",
            })
            return

        record = self.gw.app.registry.record(node_id)
        # ⚠ 原来是 usage_of()，返回按日任务数与并发数。
        #   配额已删（Step 5b），现在只报一个数：**信箱里还有多少待办**。
        #
        #   为什么只留这个：前两个数是节点自己的成本统计，与"网络怎么对待它"
        #   无关；而队列深度是唯一会让发布方被拒（QT-BACKLOG-07）的因素，
        #   也是节点申报 paused 的直接依据。
        self._send_json(200, {
            "node_id": node_id,
            "approved": bool(record.approved) if record else False,
            "state": str(record.state) if record else "unknown",
            "card": card.to_dict(),
            "usage": {
                "pending": self.gw.app.backlog_of(node_id),
            },
        })

    def _handle_task(self, full_path: str, task_id: str) -> None:
        """查询单个任务的状态与产出。

        ⭐ 为什么需要这个端点
        --------------------
        在此之前**发起方拿不回自己发布任务的结果**：网关只有
        ``/publish``（发）、``/poll``（领）、``/result``（交），
        没有"查"。于是 A2A 协作缺了最后一环 —— 问出去的问题，
        答案存在服务端，但问的人取不回来。

        :meth:`TaskLifecycle.session_view` 也不算：它只汇报状态
        （id/type/status/from/to/attempt/level），**不含产出内容**。

        ## 授权：谁能查哪个任务

        规则只有两条 —— **发布者**与**接收者**：

        * ``task.from_node == caller`` → 可以（我问出去的）
        * ``task.to_node == caller``   → 可以（发给我的）

        其余一律 **404**，而不是 403。理由：403 会告诉调用方
        "这个任务确实存在，只是不给你看" —— 那是一个枚举接口，
        可以据此试探出任务 ID 空间里有哪些是真实的。
        404 把"不存在"与"无权"合并成同一种回答，信息量为零。

        这与网关在策略拒绝时只回 ``rule_id`` 是同一条思路：
        **拒绝的形状本身会泄露信息。**
        """
        node_id = self._authenticate(full_path, body=None)

        # 空 ID 或超长 ID 直接按不存在处理，不去查存储
        if not task_id or len(task_id) > 128 or "/" in task_id:
            raise _Reject(404, RuleId.LF_TASK_NOT_FOUND, "task not found")

        task = self.gw.app.lifecycle.get(task_id)

        # "不存在"与"无权"回同一个拒绝 —— 见方法文档
        if task is None or node_id not in (task.from_node, task.to_node):
            raise _Reject(404, RuleId.LF_TASK_NOT_FOUND, "task not found")

        self._send_json(200, {"task": task_to_wire(task)})

    # ------------------------------------------------------------------
    # 端点：SSE 事件流
    # ------------------------------------------------------------------

    def _handle_events(self, full_path: str) -> None:
        """订阅通知流。**只有通知，没有内容。**

        连接由节点发起（出站），网关沿这条已建立的连接写数据。
        因此 Egress-Only 未被破坏：网关从不主动连接节点。
        """
        cfg = self.gw.server_config
        if not cfg.sse_enabled:
            self._reject_status(501, "event stream disabled")
            return

        node_id = self._authenticate(full_path, body=None)

        if not self.gw.acquire_sse_slot():
            raise _Reject(503, RuleId.QT_CONNECTION_LIMIT, "too many streams")

        gen = self.gw.open_stream(node_id)
        try:
            # ★ 订阅点在写响应头**之前**：此后发生的 bump 都会被这条流看到。
            #   放在之后会漏掉"握手期间发生的事件"。
            seen = self.gw.events.generation(node_id)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            # 明确告诉中间设备不要缓冲——否则 SSE 在很多代理后面会"到齐才到"
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            keepalive = max(1, min(cfg.sse_keepalive_seconds, _MAX_KEEPALIVE))
            self._write_stream(": connected\n\n")
            last_write = time.monotonic()

            while not self.gw.stopping and self.gw.stream_alive(node_id, gen):
                if self.gw.events.wait_change(node_id, seen, timeout=keepalive):
                    seen = self.gw.events.generation(node_id)
                    # 代数变了 = 可能有活。**不带任何内容。**
                    self._write_stream("event: work\ndata: {}\n\n")
                    last_write = time.monotonic()
                elif time.monotonic() - last_write >= keepalive:
                    # 注释行：对客户端无语义，只为让中间设备认为连接还活着
                    self._write_stream(": keepalive\n\n")
                    last_write = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            log.debug("事件流断开 %s", node_id)
        finally:
            self.gw.release_sse_slot()

    def _write_stream(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    # ------------------------------------------------------------------
    # 认证
    # ------------------------------------------------------------------

    def _authenticate(self, full_path: str, *, body: bytes | None) -> str:
        """请求签名校验（用于不带任务的调用）。返回 node_id。

        校验通过后**立刻**过速率限制——限流必须在认证之后，
        因为它需要一个可信的身份。先限流再认证的话，
        攻击者可以伪造任意节点 ID 来互相"借"配额。
        """
        headers = {name: self.headers.get(name, "") for name in httpsig.SIGNATURE_HEADERS}
        node_id = self.gw.authenticator.verify_request(
            method=self.command, path=full_path, headers=headers, body=body,
        )
        if not self.gw.rate_limiter.allow(node_id):
            raise _Reject(429, RuleId.QT_RATE_LIMITED,
                          f"{node_id} exceeded {self.gw.rate_limiter.limit}/s")
        return node_id

    def _authenticate_envelope(self, full_path: str, body: bytes) -> str:
        """信封签名校验（用于带任务的调用）。

        **同时校验请求签名与信封签名**：前者绑定"这次 HTTP 调用"，
        后者绑定"任务内容"。只校验信封的话，一个被截获的信封可以在
        任意端点重放（只要 nonce 未用过）；两层一起才把
        "哪个调用 + 什么内容"整体锁住。
        """
        self._authenticate(full_path, body=body)      # 绑定本次调用
        envelope = Envelope.from_dict(self._parse_json(body))
        sender = self.gw.authenticator.verify(envelope)
        return sender

    # ------------------------------------------------------------------
    # 任务归属校验
    # ------------------------------------------------------------------

    def _authoritative_task(self, sender: str, claimed: Task) -> Task:
        """取**服务端持有的**任务，并校验它确实属于 ``sender``。

        这是本层最重要的一条安全性质：**客户端自报的任务归属一律不信。**

        若直接采信 body 里的 task，任何已认证节点都能构造一个
        ``to_node`` 指向别人的任务并"完成"它，从而把产物写进别人的任务里
        ——那等于绕过了权限矩阵。所以这里按 id 回查，并核对收件人。

        不存在与不属于自己**返回同一个错误**（``MX-RECEIVE-03``），
        避免通过状态码枚举别人有哪些任务在跑。
        """
        actual = self.gw.app.lifecycle.get(claimed.id)
        if actual is None or actual.to_node != sender:
            # ⚠ 用 LF-TASK-404 而不是 MX-RECEIVE-03。
            #
            #   原注释说的是「不存在与不属于自己返回同一个错误，避免枚举」，
            #   而 LF-TASK-404 的定义正好是「任务不存在，或调用方看不到它」
            #   —— 本来就是同一件事，分开只是历史原因。
            raise PolicyDenied(
                RuleId.LF_TASK_NOT_FOUND,
                f"task {claimed.id!r} not owned by {sender}",
            )
        return actual

    # ------------------------------------------------------------------
    # 端点：运维（M14）
    # ------------------------------------------------------------------

    def _role_definition(self, role_name: str) -> dict[str, Any] | None:
        """查某个角色在配置里的定义，供 `ops nodes` 输出。

        ⚠ 为什么要内联展开而不是只给角色名

        运维排查"为什么它发不出任务"时，光看到 `role: worker` 还得去
        翻配置文件才知道 worker 能不能发布。**观测接口应该一次把话说完。**

        查不到返回 None（角色名拼错、或配置里没有 roles: 段）——
        这本身就是重要信息：它解释了"为什么这个节点什么都干不了"。
        """
        decl = self.gw.app.config.roles.get(role_name)
        if decl is None:
            return None
        return {
            "can_issue": decl.can_issue,
            "serves": list(decl.serves),
            "max_response_level": str(decl.max_response_level),
        }

    def _handle_ops(self, action: str, method: str, full_path: str) -> None:
        """运维动作。**必须由运维身份签名**，且不经节点配额。

        四个动作：
        * ``approve`` —— 批准某节点（审批针对的是它当前那张角色卡）
        * ``revoke`` —— 吊销某节点，立即生效
        * ``release`` —— 释放过期租约（断电恢复不及时时的兜底）
        * ``nodes`` —— 列出所有节点及其状态

        **为什么这些动作不做限流、不占并发槽**：节点流量洪水时正是最需要
        运维介入的时候。若运维通道与节点共用配额，故障时运维会被自己的
        保护机制锁在门外——那是最糟的失败模式。
        """
        if not self.gw.ops_enabled:
            self._reject_status(
                501, "ops endpoints disabled (set NXS_OPS_SECRET to enable)")
            return
        if method != "POST":
            self._reject_status(405, "method not allowed")
            return

        body = self._read_body()
        self._authenticate_ops(full_path, body)
        payload = self._parse_json(body) if body else {}

        if action == "approve":
            node_id = _require_node_id(payload)
            record = self.gw.app.registry.approve(node_id)
            self.gw.record_ops("approve", node_id)
            self._send_json(200, {
                "node_id": node_id, "approved": record.approved,
                "revision": record.revision, "state": record.state,
                "domain": record.card.domain,
            })
        elif action == "revoke":
            node_id = _require_node_id(payload)
            record = self.gw.app.registry.revoke(node_id)
            # 吊销必须同时作用于认证层，否则旧凭据还能继续签名通过
            self.gw.authenticator.revoke(node_id)
            self.gw.record_ops("revoke", node_id)
            self._send_json(200, {
                "node_id": node_id, "approved": record.approved,
                "state": record.state,
            })
        elif action == "release":
            result = self.gw.app.sweep()
            self.gw.record_ops("release", "")
            self._send_json(200, result)
        elif action == "nodes":
            self._send_json(200, {"nodes": [
                {
                    "node_id": rec.card.node_id,
                    # 自报展示字段。**已在上游校验过**（长度与控制字符），
                    # 但仍是不可信文本 —— 消费方渲染时必须转义（见 models
                    # 的 _clean_self_reported 说明：转义是渲染层的责任）。
                    "display_name": rec.card.display_name,
                    "description": rec.card.description,
                    "domain": rec.card.domain,
                    "state": rec.state,
                    "approved": rec.approved,
                    "revision": rec.revision,
                    "last_seen": rec.last_seen,
                    # ---- 权限模型改造 Step 5c ----
                    #
                    # ⚠ 原来的 roles 五个布尔 + can_issue_types + can_receive_from
                    #   全部换成了三个字段。这不是"少给信息"，是**给的更准**：
                    #
                    #     旧：5 个布尔（其中 4 个从不参与判定）+ 两串白名单，
                    #         运维要自己在脑子里组合出"它到底能干什么"
                    #     新：角色名 + 该角色在配置里的定义 + 能不能广播
                    #
                    #   排查"为什么 A 发不了任务给 B"时，现在只需看 role ——
                    #   driver 能发、worker 不能，一眼就清楚。
                    "role": rec.card.role,
                    #: 该角色在配置里的定义（能否发布 / 服务哪些类型 / 上限）。
                    #: 直接给出，省得运维再去翻配置 —— 缺字段的观测接口会误导人。
                    "role_definition": self._role_definition(rec.card.role),
                    "may_broadcast": rec.card.may_broadcast,
                    "capabilities": [c.type for c in rec.card.capabilities],
                    #: 节点**自己**愿意给多细（角色上限与实际生效值见 role_definition）
                    "max_response_level": str(rec.card.effective_max_level()),
                    #: 节点自律限额 —— 只记录不强制，但排障时要知道它会不会拒收
                    "self_limits": rec.card.self_limits.to_dict(),
                }
                for rec in self.gw.app.registry.all_records()
            ]})
        elif action == "metrics":
            self._send_json(200, self.gw.collect_metrics())
        else:
            self._reject_status(501, f"unknown ops action: {action}")

    def _authenticate_ops(self, full_path: str, body: bytes) -> str:
        """校验运维身份。

        与节点认证的区别：**不查批准状态、不计速率**，但必须是以
        ``__ops__`` 身份签名的请求。用节点密钥签名调用运维接口会被拒——
        这正是"被管理者不能给自己批条子"。
        """
        headers = {name: self.headers.get(name, "")
                   for name in httpsig.SIGNATURE_HEADERS}
        sender = self.gw.authenticator.verify_request(
            method=self.command, path=full_path, headers=headers, body=body,
        )
        if sender != OPS_NODE_ID:
            # 用 PolicyDenied 的固定措辞，不说明"你身份不对"还是"你没权限"——
            # 两者都不该让调用方区分
            raise _Reject(403, RuleId.MX_ISSUER_NOT_ALLOWED,
                          f"ops action by non-ops identity {sender!r}")
        return sender

    # ------------------------------------------------------------------
    # 端点：register（自注册 + 心跳）
    # ------------------------------------------------------------------

    def _handle_register(self, full_path: str) -> None:
        """节点自注册。兼作心跳。

        三道校验，顺序有意义：

        ① **签名** —— 你是谁（请求签名，因为这里没有 Task 可签）
        ② **身份一致** —— 卡片里的 ``node_id`` 必须等于签名身份。
           否则 node-b 签个名就能提交一张 ``node_id=node-a`` 的卡片，
           直接覆盖 node-a 的注册。
        ③ **域由配置裁定** —— 见 :meth:`Gateway._domain_for`

        之后交给 ``registry.register_from_node``（不可信入口）：
        它保证**自注册永远不带来批准**，且卡片一变就撤销批准。

        为什么 register 兼作心跳而不是单独开一个端点：
        节点上报角色卡本来就是"我还活着，这是我当前的能力"，
        分开会让两者可能不一致。协议 §10 也是这么定的。
        """
        body = self._read_body()
        sender = self._authenticate(full_path, body=body)   # ① 签名
        payload = self._parse_json(body)

        raw_card = payload.get("card")
        if not isinstance(raw_card, Mapping):
            raise _Reject(400, RuleId.FM_CARDCARD_INVALID, "missing card")
        try:
            card = RoleCard.from_dict(raw_card, "register.card")
        except ConfigError as exc:
            # 角色卡非法要报 FM-CARD-03，不能与"信封格式非法"混用同一个码——
            # 运维看错误码就该知道是卡片写错了还是报文写错了
            raise _Reject(400, RuleId.FM_CARDCARD_INVALID, str(exc)) from exc

        if card.node_id != sender:                          # ② 身份一致
            # 报"角色卡非法"而不是"冒充"，与既有拒绝体保持一致：
            # 具体是哪种不符不对外说明
            raise _Reject(403, RuleId.FM_CARDCARD_INVALID,
                          f"card.node_id={card.node_id!r} != signer={sender!r}")

        card = self.gw.resolve_domain(card)                 # ③ 域由配置裁定
        record, outcome = self.gw.app.registry.register_from_node(card)

        self.gw.record_registration(sender, outcome, record)
        self._send_json(200, {
            "outcome": outcome,
            "approved": record.approved,
            "revision": record.revision,
            "state": record.state,
            "domain": record.card.domain,
            # 明确告诉节点"待批准"该怎么做——否则它只能盲目重试
            "hint": ("waiting for operator approval"
                     if not record.approved else "ready"),
        })

    # ------------------------------------------------------------------
    # 端点：ack（续租）
    # ------------------------------------------------------------------

    def _handle_ack(self, full_path: str) -> None:
        """续租。长任务在执行期间定期调用。

        用**请求签名**（不是信封签名）：这是一个不携带任务内容的调用，
        它只需要证明"我是持有这个任务的那个节点"。

        返回里带 ``lease_seconds``，让节点知道该多久续一次——
        把间隔写死在节点代码里，一旦中心侧调整租约就会失配。
        """
        body = self._read_body()
        sender = self._authenticate(full_path, body=body)
        payload = self._parse_json(body)

        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise _Reject(400, RuleId.FM_ENVELOPE_INVALID, "missing task_id")

        # 权限与归属校验都在 lifecycle 里 —— 传输层不重复实现业务判断
        task = self.gw.app.lifecycle.renew(sender, task_id)
        self._send_json(200, {
            "task_id": task.id,
            "status": task.status,
            "attempt": task.attempt,
            "lease_seconds": self.gw.app.lifecycle.lease_seconds,
        })

    # ------------------------------------------------------------------
    # 请求体
    # ------------------------------------------------------------------

    def _read_body(self) -> bytes:
        """读请求体。**先用 Content-Length 判断上限，再读入。**

        顺序很重要：先读后判的话，上限就只是"事后抱怨"——
        攻击者仍然可以让服务端读入任意大的体积直到内存耗尽。
        """
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise _Reject(411, RuleId.FM_BODY_TOO_LARGE, "missing content-length")
        try:
            length = int(raw)
        except (TypeError, ValueError) as exc:
            raise _Reject(400, RuleId.FM_ENVELOPE_INVALID, "bad content-length") from exc
        if length < 0:
            raise _Reject(400, RuleId.FM_ENVELOPE_INVALID, "negative content-length")
        if length > self.gw.server_config.max_body_bytes:
            raise _Reject(413, RuleId.FM_BODY_TOO_LARGE,
                          f"body {length} > {self.gw.server_config.max_body_bytes}")
        return self.rfile.read(length)

    def _parse_json(self, body: bytes) -> Mapping[str, Any]:
        try:
            obj = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(obj, Mapping):
            raise ConfigError("请求体期望一个 JSON 对象")
        return obj

    # ------------------------------------------------------------------
    # 响应
    # ------------------------------------------------------------------

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _send_status(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _reject_status(self, status: int, message: str) -> None:
        self._send_json(status, {"denied": "true", "message": message})


#: 已实现前缀下"路径存在但方法不对"的集合（用于区分 405 与 501）。
_KNOWN_PATHS = frozenset({
    f"{PREFIX}/poll", f"{PREFIX}/events", f"{PREFIX}/publish",
    f"{PREFIX}/result", f"{PREFIX}/fail", f"{PREFIX}/health",
    f"{PREFIX}/register", f"{PREFIX}/ack", f"{PREFIX}/whoami",
})
#: 带路径参数的端点前缀。这些不放进 _KNOWN_PATHS（路径不固定），
#: 所以要单独在分发里处理「方法不对」的情况。
_PATH_PREFIXES = (f"{PREFIX}/task/",)


__all__ = [
    "EventHub",
    "Gateway",
    "GatewayStats",
    "PREFIX",
    "RateLimiter",
    "REQUEST_TIMEOUT_SECONDS",
    "status_for",
]
