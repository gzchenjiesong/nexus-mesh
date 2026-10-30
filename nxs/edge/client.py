"""节点侧 HTTP 传输 —— 让 ``NodeClient`` 能通过真实网络接入。

为什么放在 edge 而不是 sdk
--------------------------
``sdk/`` 是**节点逻辑**（领任务、执行、回传），它对传输无感知；
本模块是**传输实现**（序列化、签名、发请求），属于"与外界说话"的那一层。
两者分开放，才能保证"换传输不改节点逻辑"这个性质是真的
（``sdk/`` 里没有任何一行 import 本模块）。

Egress-Only
-----------
本模块**只发起出站连接**，自身从不监听端口。SSE 订阅也是先由这里
发起 ``GET /events``，服务端再沿这条已建立的连接写数据——
不存在"服务端主动连节点"的路径。

推送不可用时怎么办
------------------
SSE 可能被中间设备切断或缓冲（这正是原设计对长轮询的顾虑）。
因此：

* **推送只是加速器，不是依赖。** 订阅失败不影响 ``poll`` ——
  :meth:`HttpTransport.poll` 永远是一次独立的短请求，
  与推送是否活着无关。
* :meth:`HttpTransport.subscribe` 在异常或断开时**返回而不是抛出**，
  调用方退回 ``run_forever`` 的固定周期轮询即可，功能不降级。

换句话说：**推送坏掉时，系统退化成纯短轮询，行为与 v0.1 完全相同。**
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from ..core.envelope import Envelope, envelope_for, task_to_wire
from ..core.errors import ConfigError, PolicyDenied, RuleId
from ..data.mailbox import DeliveryResult
from ..core.models import Artifact, RoleCard, Task, TaskStatus
from ..mgmt.config import OPS_NODE_ID
from . import httpsig

log = logging.getLogger("nxs.edge.client")

#: 默认超时。**必须设**：没有超时的客户端在服务端失联时会永久挂住。
DEFAULT_TIMEOUT_SECONDS = 15.0

#: SSE 重连的基础退避与上限（秒）。
RECONNECT_BACKOFF = (0.5, 1.0, 2.0, 5.0, 10.0)


class HttpTransport:
    """实现 :class:`nxs.app.Transport` 协议的 HTTP 客户端。

    用法::

        transport = HttpTransport("http://127.0.0.1:8443/nxs",
                                  node_id="node-a", secret=secret)
        client = NodeClient(NodeConfig("node-a", handlers, levels), transport)
        client.run_forever()

    :param base_url: 含 ``/nxs`` 前缀的基地址
    :param timeout: 单次请求超时（秒）
    """

    def __init__(self, base_url: str, *, node_id: str, secret: bytes,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 ca_file: str | None = None) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError(f"base_url 必须以 http:// 或 https:// 开头: {base_url!r}")
        # 前缀必须显式给全：签名覆盖路径，前缀少了会让每个请求
        # 都以"认证失败"告终，而那种错误最难排查。**在构造时就报错。**
        if not base_url.rstrip("/").endswith("/nxs"):
            raise ConfigError(
                f"base_url 必须以 /nxs 结尾（签名覆盖路径）: {base_url!r}"
            )
        self.base_url = base_url.rstrip("/")
        self.node_id = node_id
        self._secret = secret
        #: 信任的 CA 证书路径。用于 coordinator 使用**自签证书**的场景（无域名时）。
        #:
        #: 为什么不靠 ``SSL_CERT_FILE`` 环境变量：那是**进程全局**的，
        #: 一旦设置就影响该进程的所有 HTTPS 请求。而信任哪个 CA 应该是
        #: **每个连接**的决定 —— 同一进程连两个用不同 CA 的 coordinator 时，
        #: 环境变量根本表达不了。显式参数也更难被意外泄漏的环境变量带偏。
        self.ca_file = ca_file
        self.timeout = timeout

    # ------------------------------------------------------------------
    # 注册与心跳
    # ------------------------------------------------------------------

    def register(self, card: RoleCard) -> dict:
        """提交（或刷新）自己的角色卡。兼作心跳。

        **必须由节点的真实密钥签名**——服务端会校验卡片里的 ``node_id``
        与签名身份一致，所以一个节点无法替另一个节点注册。

        :return: 服务端的响应，含 ``outcome`` / ``approved`` / ``revision``。
            节点应检查 ``approved``：为 ``False`` 说明还在等运维批准。
        """
        if card.node_id != self.node_id:
            raise ConfigError(
                f"角色卡的 node_id={card.node_id!r} 与客户端身份 "
                f"{self.node_id!r} 不符——服务端会拒绝，不必发送"
            )
        payload = json.dumps(
            {"card": card.to_dict()}, ensure_ascii=False
        ).encode("utf-8")
        status, body = self._request("POST", "/register", body=payload)
        if body is None:  # pragma: no cover - 服务端契约保证有体
            raise ConfigError("/register: 响应为空")
        return dict(body)

    # ------------------------------------------------------------------
    # Transport 协议
    # ------------------------------------------------------------------

    def publish(self, *, from_node: str, to_node: str | None = None,
                task_type: str, params: dict, session_id: str = "",
                idempotency_key: str | None = None) -> DeliveryResult:
        """发布任务。

        :param to_node: 目标节点。**省略或传 ``None`` 表示按能力自动匹配**——
            服务端会按 M4 筛出候选、确定性排序、逐个过策略校验，
            取第一个通过的。

        ``from_node`` 参数**保留但会被覆盖**：服务端只认已验签的发送方。
        显式覆盖而不是静默忽略，是为了让"伪造发送方"在服务端被结构性拒绝，
        而不是依赖客户端自律。
        """
        if from_node != self.node_id:
            log.warning("忽略与自身不符的 from_node=%r（将以 %r 发送）",
                        from_node, self.node_id)

        task = Task(
            id=f"t-{httpsig.new_nonce()}",
            idempotency_key=idempotency_key or f"idem-{httpsig.new_nonce()}",
            type=task_type,
            session_id=session_id,
            from_node=self.node_id,
            # 空串 = 自动匹配。协议上 to_node 始终必填（漏写会报"缺少必填字段"），
            # 空串是"故意不指定"的显式表达。
            to_node=to_node or "",
            params=dict(params),
        )
        envelope = envelope_for(self._secret, self.node_id, task,
                                session_id=session_id)
        payload = self._post("/publish", envelope)
        return DeliveryResult(
            task=Task.from_dict(payload["task"], "response.task"),
            created=bool(payload.get("created", True)),
            effective_max_level=str(payload.get("effective_max_level", "")),
        )

    def poll(self, node_id: str) -> Task | None:
        """领取任务。队列为空时服务端返回 204，这里映射为 ``None``。"""
        status, payload = self._request("GET", "/poll")
        if status == 204 or payload is None:
            return None
        return Task.from_dict(payload["task"], "response.task")

    def complete(self, node_id: str, task: Task,
                 artifacts: list[Artifact]) -> Task:
        """回传结果。**回传的是服务端下发的那个任务**（加上产物）。"""
        task.artifacts = list(artifacts)
        task.status = TaskStatus.COMPLETED
        envelope = envelope_for(self._secret, self.node_id, task,
                                session_id=task.session_id)
        payload = self._post("/result", envelope)
        return Task.from_dict(payload["task"], "response.task")

    def fail(self, node_id: str, task: Task, reason: str) -> Task:
        """报告失败。

        失败原因放进 ``params.reason``：``Task`` 的字段集是严格校验的
        （``_reject_unknown``），不为一处诊断信息新增协议字段。
        服务端只把它写进本地日志，不随响应外传。
        """
        task.status = TaskStatus.FAILED
        task.params = {**dict(task.params), "reason": str(reason)[:200]}
        envelope = envelope_for(self._secret, self.node_id, task,
                                session_id=task.session_id)
        payload = self._post("/fail", envelope)
        return Task.from_dict(payload["task"], "response.task")

    def renew(self, task_id: str) -> int:
        """续租。返回服务端建议的下次续租间隔（秒）。

        抛 :class:`PolicyDenied` 表示**你已失去这个任务**：
        租约过期后任务回到队列，可能已被别人领走。此时必须停止执行，
        不能继续算下去再回传——那会产生重复副作用。
        """
        body = json.dumps({"task_id": task_id}).encode("utf-8")
        _, payload = self._request("POST", "/ack", body=body)
        if payload is None:  # pragma: no cover
            raise ConfigError("/ack: 响应为空")
        return int(payload.get("lease_seconds", 15))

    # ------------------------------------------------------------------
    # 事件订阅（推送）
    # ------------------------------------------------------------------

    def subscribe(self, on_work: Callable[[], None] | None = None, *,
                  should_stop: Callable[[], bool] | None = None,
                  max_reconnects: int | None = None) -> str:
        """订阅通知流，收到"有活"时回调 ``on_work``。

        **本方法不抛出网络异常。** 断线会退避重连；达到上限后
        正常返回（返回值说明结束原因），调用方继续用 ``poll`` 循环即可。

        :return: ``"stopped"`` · ``"closed"`` · ``"gave_up"``
        """
        attempts = 0
        while True:
            if should_stop is not None and should_stop():
                return "stopped"
            try:
                self._stream_once(on_work, should_stop)
                attempts = 0
                return "closed"        # 服务端正常结束（例如关闭）
            except (urllib.error.URLError, OSError, socket.timeout) as exc:
                attempts += 1
                log.info("事件流断开（第 %d 次），退回轮询: %s", attempts, exc)
                if max_reconnects is not None and attempts >= max_reconnects:
                    return "gave_up"
                delay = RECONNECT_BACKOFF[min(attempts - 1, len(RECONNECT_BACKOFF) - 1)]
                _sleep(delay, should_stop)

    def _stream_once(self, on_work: Callable[[], None] | None,
                     should_stop: Callable[[], bool] | None) -> None:
        path = "/events"
        headers = httpsig.sign_request(
            self._secret, method="GET", path=self._path_with_prefix(path),
            node_id=self.node_id,
        )
        request = urllib.request.Request(
            self.base_url + path, method="GET", headers=headers,
        )
        # 流式读取：SSE 没有 Content-Length，必须用 readline 逐行
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            if response.status != 200:
                return
            for raw in response:
                if should_stop is not None and should_stop():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("event:") and "work" in line:
                    if on_work is not None:
                        on_work()

    # ------------------------------------------------------------------
    # 内部：HTTP
    # ------------------------------------------------------------------

    def _path_with_prefix(self, path: str) -> str:
        """把端点拼成**签名用的完整路径**（含 ``/nxs`` 前缀）。

        基地址里已含前缀，所以这里从前缀的位置开始截取，
        保证"客户端签的"与"服务端验的"是同一个字符串。
        """
        marker = self.base_url.find("/nxs")
        prefix = self.base_url[marker:] if marker >= 0 else ""
        return f"{prefix}{path}"

    def _ssl_context(self):
        """按需构造 SSL 上下文。

        返回 ``None`` 表示"用 urllib 的默认行为"（系统 CA 库）。
        只有配了 ``ca_file`` 且地址是 https 时才自建上下文。
        """
        if not self.ca_file or not self.base_url.startswith("https://"):
            return None
        import ssl
        try:
            return ssl.create_default_context(cafile=self.ca_file)
        except (OSError, ssl.SSLError) as exc:
            raise ConfigError(
                f"加载 CA 证书失败（ca_file={self.ca_file!r}）：{exc}"
            ) from exc

    def _post(self, path: str, envelope: Envelope) -> Mapping[str, Any]:
        body = json.dumps(envelope.to_dict(), ensure_ascii=False).encode("utf-8")
        status, payload = self._request("POST", path, body=body)
        if payload is None:  # pragma: no cover - 服务端契约保证有体
            raise ConfigError(f"{path}: 响应为空")
        return payload

    def _request(self, method: str, path: str, *,
                 body: bytes | None = None) -> tuple[int, Mapping[str, Any] | None]:
        full = self._path_with_prefix(path)
        headers = httpsig.sign_request(
            self._secret, method=method, path=full, node_id=self.node_id,
            body=body,
        )
        headers["Accept"] = "application/json"
        if body is not None:
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            self.base_url + path, data=body, method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._ssl_context(),
            ) as response:
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as err:
            payload = _decode(err.read())
            # ★ 拒绝不是异常，是**正常运行的一部分**。
            #   把服务端的 rule_id 还原成 PolicyDenied 抛出，
            #   让节点侧看到与同进程传输完全一致的异常类型。
            if payload and "rule_id" in payload:
                try:
                    rule = RuleId(str(payload["rule_id"]))
                except ValueError:  # pragma: no cover - 未知码降级
                    rule = RuleId.FM_ENVELOPE_INVALID
                raise PolicyDenied(rule, f"http {err.code}") from err
            raise

    # ------------------------------------------------------------------
    # 便捷
    # ------------------------------------------------------------------

    def whoami(self) -> dict[str, Any]:
        """取自己的角色卡与当前状态。

        语义是"看我自己"，只需要自己的凭据 —— **不需要运维密钥**。
        返回 ``{node_id, approved, state, card, usage}``。

        :raises PolicyDenied: 该身份尚未在服务端注册（``AU-NODE-04``）
        """
        _, payload = self._request("GET", "/whoami")
        if payload is None:  # pragma: no cover - 服务端契约保证有体
            raise ConfigError("/whoami: 响应为空")
        return dict(payload)

    def get_task(self, task_id: str) -> dict[str, Any]:
        """查一个任务的状态与产出。

        只能查**自己发布的**或**发给自己的**任务；其余一律 404
        （"不存在"与"无权"不区分，避免成为枚举接口）。

        返回线格式的任务字典（含 ``artifacts``），
        形状与信封里、``/result`` 响应里的一致 —— 刻意复用同一实现，
        否则客户端要写两套解析，迟早不一致。

        :raises PolicyDenied: 任务不存在、或调用方看不到它（``LF-TASK-404``）
        """
        # 非 2xx 由 _request 统一抛 PolicyDenied（它把服务端的 rule_id
        # 还原成异常类型）。所以这里**不需要**再判状态码 ——
        # 写了也只是死代码，而"看起来在处理其实处理不到"的分支
        # 比没有更糟：它会让读代码的人以为这里有额外逻辑。
        _, payload = self._request("GET", f"/task/{task_id}")
        if payload is None:  # pragma: no cover - 服务端契约保证有体
            raise ConfigError(f"/task/{task_id}: 响应为空")
        return dict(payload.get("task") or {})

    def health(self) -> bool:
        """探活。**不需要认证，也不返回任何业务信息。**"""
        try:
            with urllib.request.urlopen(
                f"{self.base_url}/health", timeout=self.timeout
            ) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def task_wire(self, task: Task) -> dict[str, Any]:
        """把任务转成线格式。便于节点侧自行落盘或转发。"""
        return task_to_wire(task)


def _decode(raw: bytes) -> Mapping[str, Any] | None:
    if not raw:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"响应不是合法 JSON: {exc}") from exc
    return obj if isinstance(obj, Mapping) else None


def _sleep(seconds: float, should_stop: Callable[[], bool] | None) -> None:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if should_stop is not None and should_stop():
            return
        time.sleep(min(0.1, deadline - time.monotonic()))


# ---------------------------------------------------------------------------
# 运维客户端
# ---------------------------------------------------------------------------

class OpsClient:
    """运维通道客户端（M14）。

    与 :class:`HttpTransport` 的区别：

    * 身份是保留的 ``__ops__``，**不是**任何节点
    * 走的是运维端点，不经节点速率限制与并发配额
    * 做六件事：批准、吊销、释放租约、查节点、查指标、读审计

    为什么运维要单独一个类而不是给 HttpTransport 加参数：
    两者的权限模型完全不同（一个是"被管理者"，一个是"管理者"）。
    合成一个类就必须在每个方法里判"我这次是哪种身份"，
    而那正是最容易判错的地方。
    """

    def __init__(self, base_url: str, *, secret: bytes,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 ca_file: str | None = None) -> None:
        if not base_url.rstrip("/").endswith("/nxs"):
            raise ConfigError(f"base_url 必须以 /nxs 结尾: {base_url!r}")
        self.base_url = base_url.rstrip("/")
        self._secret = secret
        self.timeout = timeout
        #: 信任的 CA（自签场景）。语义同 HttpTransport.ca_file。
        self.ca_file = ca_file

    def _post(self, action: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        # 签名路径必须与网关实际收到的完整路径一致
        marker = self.base_url.find("/nxs")
        prefix = self.base_url[marker:]
        path = f"{prefix}/ops/{action}"
        headers = httpsig.sign_request(
            self._secret, method="POST", path=path,
            node_id=OPS_NODE_ID, body=body,
        )
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}/ops/{action}", data=body, method="POST",
            headers=headers,
        )
        try:
            context = None
            if self.ca_file and self.base_url.startswith("https://"):
                import ssl
                context = ssl.create_default_context(cafile=self.ca_file)
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=context,
            ) as response:
                parsed = _decode(response.read())
                return dict(parsed or {})
        except urllib.error.HTTPError as err:
            parsed = _decode(err.read()) or {}
            raise ConfigError(
                f"运维动作 {action} 失败（HTTP {err.code}）："
                f"{parsed.get('message', '')} {parsed.get('rule_id', '')}".strip()
            ) from err

    def approve(self, node_id: str) -> Mapping[str, Any]:
        """批准节点。**批准的是它当前那张角色卡**，卡片一变即失效。"""
        return self._post("approve", {"node_id": node_id})

    def revoke(self, node_id: str) -> Mapping[str, Any]:
        """吊销节点。立即生效，不等到凭据过期。"""
        return self._post("revoke", {"node_id": node_id})

    def release(self) -> Mapping[str, Any]:
        """主动释放过期租约（断电恢复不及时时的兜底手段）。"""
        return self._post("release", {})

    def nodes(self) -> list[Mapping[str, Any]]:
        """列出所有节点及其状态。"""
        return list(self._post("nodes", {}).get("nodes", []))

    def metrics(self) -> Mapping[str, Any]:
        """读取指标与系统级关联异常。

        ⚠ **连续调用会得到不同结果**：趋势类告警（如 ``silent_stall``）
        需要和"上一次快照"对比才能判断，所以第一次调用可能少了几条。
        这不是漏报，是信息还不够——比凭空猜一个"可能在增长"诚实。
        """
        return self._post("metrics", {})

    def audit(self, limit: int = 100, *,
              include_heartbeat: bool = False) -> Mapping[str, Any]:
        """读取最近的审计记录。**只读，且只含元信息。**

        返回 ``{"records": [...], "limit": 实际条数,
        "truncated": 是否被截断, "max_limit": 服务端上限}``。

        ## 为什么要看 ``limit`` 与 ``truncated``

        服务端有硬上限。请求 5000 而实际只给 1000 时，
        **不告诉调用方就会让人以为"审计只有 1000 条"** ——
        那会导致误判"运行很空闲"。所以这两个字段必须回传。

        ## 默认滤掉心跳（``include_heartbeat``）

        节点每 30 秒心跳一次，3 个节点 ≈ 每 10 秒一条 ``register:unchanged``。
        实机上线后实测：最近 100 条审计 **100% 是心跳**，一条任务事件都没有 ——
        100 条只覆盖约 17 分钟，**半小时前的事故记录已被挤出窗口**。

        所以默认滤掉心跳，并在 ``filtered_heartbeats`` 里报告滤了多少
        （非零不代表出错，而是"这段时间节点很健康"）。

        排查"心跳到底通不通"时传 ``include_heartbeat=True``。

        ## 不含业务内容

        审计记录按设计只写元信息（时间、谁→谁、类型、成败、错误码、
        耗时、字节数）。任务的参数与产出**从来不在审计里** ——
        详见 ``mgmt/audit.py`` 顶部的两条硬约束。

        所以拿这个接口给观察窗看"运行日志"是安全的；
        但**不要**指望它能当"任务详情"用，那需要另一个安全等级的设计。
        """
        return self._post("audit", {
            "limit": int(limit),
            #: 默认滤掉纯心跳（register:unchanged）。
            #: 排查"心跳到底通不通"时才置 True。
            "include_heartbeat": bool(include_heartbeat),
        })


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "HttpTransport", "OpsClient"]
