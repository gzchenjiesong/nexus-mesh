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

from ..app import DeliveryResult
from ..core.envelope import Envelope, envelope_for, task_to_wire
from ..core.errors import ConfigError, PolicyDenied, RuleId
from ..core.models import Artifact, Task, TaskStatus
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
                 timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
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
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Transport 协议
    # ------------------------------------------------------------------

    def publish(self, *, from_node: str, to_node: str, task_type: str,
                params: dict, session_id: str = "",
                idempotency_key: str | None = None) -> DeliveryResult:
        """发布任务。

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
            to_node=to_node,
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
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
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


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "HttpTransport"]
