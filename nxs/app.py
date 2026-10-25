"""应用装配 —— 把各层组装成一个可运行的系统。

本模块是**唯一的装配点**：它决定用哪个配置、哪个存储后端、哪些策略。
各层自身不知道彼此的存在（依赖方向单向），装配在这之上完成。

这样设计的好处：换后端、换配置、换认证方式都只改这一处，
而 `control/` 与 `data/` 的代码一行不动。

同时提供两种传输：
* :class:`InProcessTransport` —— 同进程直连，用于 demo 与测试
* 真实部署时由 ``edge/gateway.py`` 提供 HTTP 传输

节点侧逻辑（``nxs/sdk/``）对两者无感知，只依赖传输接口。
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, replace
from typing import Any, Protocol

from .backends import memory
from .control.policy import PolicyEngine, Usage
from .control.registry import Registry
from .core.errors import ConfigError, PolicyDenied, RuleId
from .core.models import Artifact, RoleCard, Task, TaskStatus
from .data.boundary import ResponseBoundary
from .data.mailbox import DeliveryResult, Mailbox
from .data.task import TaskLifecycle
from .mgmt.audit import AuditError, FileAuditStore, make_record
from .mgmt.config import NexusConfig, load_config as _load_config
from .ports.stores import AuditStore, QueueStore, RegistryStore


# ---------------------------------------------------------------------------
# 传输接口
# ---------------------------------------------------------------------------

class Transport(Protocol):
    """节点与网络之间的传输。

    节点侧 SDK 只依赖这个接口，因此同一套节点逻辑既能在 demo 里
    同进程运行，也能在真实部署里走 HTTP。
    """

    def register(self, card: RoleCard) -> dict:
        """注册（或刷新）自己的角色卡。兼作心跳。

        返回至少包含 ``outcome`` 与 ``approved`` 两个键——
        节点需要知道"我到底被批准了没有"，否则它只能盲目重试。
        """

    def publish(self, *, from_node: str, to_node: str, task_type: str,
                params: dict, session_id: str = "",
                idempotency_key: str | None = None) -> DeliveryResult:
        """投递任务。"""

    def poll(self, node_id: str) -> Task | None:
        """领取任务。"""

    def complete(self, node_id: str, task: Task, artifacts: list[Artifact]) -> Task:
        """回传结果（会经过应答边界）。"""

    def fail(self, node_id: str, task: Task, reason: str) -> Task:
        """报告失败。"""


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

@dataclass
class AppStores:
    """存储后端的集合。换后端只改这里。"""

    registry: RegistryStore
    queue: QueueStore
    audit: AuditStore


class NexusApp:
    """装配完成的系统实例。

    用法::

        app = NexusApp.from_config_path("config/local.yaml")
        app.start()                 # 初始化注册表、批准节点
        t = app.transport.publish(...)
    """

    def __init__(self, config: NexusConfig, stores: AppStores, *,
                 approved_domains: tuple[str, ...] | None = None,
                 lease_seconds: int = 15,
                 max_attempts: int = 3) -> None:
        self.config = config
        self.stores = stores

        self.registry = Registry(stores.registry)
        # ★ 策略层从**注册表**取角色卡与类型索引，而不是只看配置文件。
        #   否则通过 HTTP 自注册的节点会被判为"未知节点"（AU-NODE-04）
        #   或"类型未注册"（WL-TYPE-404）——注册成功却永远无法参与协作。
        #   这是自注册能真正生效的前提。
        self.policy = PolicyEngine(
            config,
            card_resolver=self.registry.card,
            cards_source=self.registry.all_cards,
        )
        self.lifecycle = TaskLifecycle(stores.queue, lease_seconds=lease_seconds,
                                       max_attempts=max_attempts)
        self.mailbox = Mailbox(self.policy, self.registry, self.lifecycle)
        self.boundary = ResponseBoundary(self.policy)
        self.audit = stores.audit

        # 省略时取配置里的策略设置（默认为空 = 不自动批准任何域）
        self._approved_domains = (
            config.policy.auto_approve_domains
            if approved_domains is None else approved_domains
        )

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    @classmethod
    def from_config_path(cls, path: str | None = None, *,
                         env: dict[str, str] | None = None,
                         force_memory: bool = False) -> "NexusApp":
        """从配置文件装配。

        :param force_memory: 强制使用内存后端（demo 与测试用）。
        """
        config = _load_config(path, env=env)
        stores = build_stores(config, force_memory=force_memory)
        return cls(config, stores)

    @property
    def transport(self) -> "InProcessTransport":
        return InProcessTransport(self)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """初始化注册表：注册全部节点，按信任域决定是否自动批准。

        **只有 ``approved_domains`` 里的域会被自动批准。**
        其余节点保持未批准，需要显式调用 :meth:`approve`——
        这落实了"新节点不能自行加入"。
        """
        for card in self.config.nodes.values():
            self.registry.register(card, approved=card.domain in self._approved_domains)

    def approve(self, node_id: str) -> None:
        self.registry.approve(node_id)

    def revoke(self, node_id: str) -> None:
        self.registry.revoke(node_id)

    def sweep(self) -> dict[str, int]:
        return self.mailbox.sweep()

    # ------------------------------------------------------------------
    # 用量与审计
    # ------------------------------------------------------------------

    def usage_of(self, node_id: str, *, since: str | None = None) -> Usage:
        """返回节点的当前用量快照。

        三个维度都取自后端的真实统计：
        * ``tasks_today`` —— 自 ``since`` 起该节点名下的任务数
          （``since`` 省略时用"今天零点"，即按自然日统计）
        * ``concurrent_now`` —— 当前处于 ``working`` 状态的任务数
        * ``bytes_today`` —— v0.1 未统计（需要按节点累计应答字节数）

        并发维度是精确的，它才是真正限制"同一节点同时处理多少任务"的那个。
        """
        window = since or _today_start()
        queue = self.stores.queue
        return Usage(
            tasks_today=queue.count_tasks(node_id, since=window),
            concurrent_now=queue.count_tasks(node_id, status="working"),
            bytes_today=0,
        )

    def record(self, **kw: Any) -> None:
        """写审计。**失败会抛 :class:`AuditError`**，调用方应据此熔断。"""
        self.audit.append(make_record(**kw))

    def audit_summary(self) -> dict[str, Any]:
        return self.audit.summary()


# ---------------------------------------------------------------------------
# 同进程传输
# ---------------------------------------------------------------------------

class InProcessTransport:
    """同进程传输：直接调用内部模块，不经网络。

    用于 demo 与测试。它**走的是和 HTTP 完全相同的代码路径**
    （策略 → 投递 → 边界 → 生命周期），只是省掉了序列化与网络。
    因此 demo 验证过的行为，真实部署里同样成立。
    """

    def __init__(self, app: NexusApp) -> None:
        self._app = app

    # -- 注册 --

    def register(self, card: RoleCard) -> dict:
        """同进程注册：走**可信**入口。

        这里没有"节点自己说了算"的问题——调用方与 hub 在同一个进程里，
        中间不存在信任边界。真实部署走 HTTP，那条路径由网关用
        ``registry.register_from_node``（不可信入口）处理。
        """
        app = self._app
        record = app.registry.register(
            card, approved=card.domain in app._approved_domains,
        )
        return {
            "outcome": "registered",
            "approved": record.approved,
            "revision": record.revision,
        }

    # -- 投递 --

    def publish(self, *, from_node: str, to_node: str, task_type: str,
                params: dict, session_id: str = "",
                idempotency_key: str | None = None) -> DeliveryResult:
        app = self._app
        try:
            result = app.mailbox.deliver(
                from_node=from_node, to_node=to_node,
                task_type=task_type, params=params, session_id=session_id,
                idempotency_key=idempotency_key,
                usage=app.usage_of(to_node),
            )
        except PolicyDenied as denied:
            self._audit_denied(denied, from_node=from_node,
                               to_node=to_node, task_type=task_type,
                               session_id=session_id)
            raise
        return result

    # -- 领取 --

    def poll(self, node_id: str) -> Task | None:
        try:
            return self._app.mailbox.poll(node_id)
        except PolicyDenied as denied:
            self._audit_denied(denied, from_node=node_id, to_node=node_id)
            raise

    # -- 回传 --

    def complete(self, node_id: str, task: Task,
                 artifacts: list[Artifact]) -> Task:
        """回传结果。

        流程：**边界校验 → 落终态 → 审计**。

        **审计在最后一步，且失败会向上抛。** 这与拒绝路径的处理刻意不同：
        这里"操作已经发生"（任务已落终态），如果审计写不进去，就产生了
        "不可追溯的完成"——那比中断更糟，必须让调用方知道。
        """
        app = self._app
        probe = replace(task, artifacts=list(artifacts))
        try:
            app.boundary.enforce_on_task(node_id, probe)
        except PolicyDenied as denied:
            self._audit_denied(denied, from_node=node_id,
                               to_node=task.from_node, task_id=task.id,
                               task_type=task.type, session_id=task.session_id)
            raise

        done = app.lifecycle.complete(task, probe.artifacts)
        hint = ResponseBoundary.trust_hint(node_id, done.artifacts)
        app.record(
            outcome="completed", task_id=done.id, task_type=done.type,
            session_id=done.session_id, trace_id=done.trace_id,
            from_node=done.from_node, to_node=done.to_node,
            from_domain=app.config.domain_of(done.from_node) or "",
            to_domain=app.config.domain_of(done.to_node) or "",
            level=hint["max_level"], attempt=done.attempt,
            contains_freetext=hint["contains_freetext"],
            sanitized=hint["sanitized"],
        )
        return done

    def fail(self, node_id: str, task: Task, reason: str) -> Task:
        failed = self._app.lifecycle.fail(task, reason)
        self._app.record(
            outcome="failed", task_id=failed.id, task_type=failed.type,
            session_id=failed.session_id, from_node=failed.from_node,
            to_node=failed.to_node, attempt=failed.attempt,
        )
        return failed

    # -- 内部 --

    def _audit_denied(self, denied: PolicyDenied, **kw: Any) -> None:
        """记录一次拒绝。

        **审计失败在这里被吞掉，但会告警——不会掩盖原始的拒绝异常。**

        为什么与完成路径不同：拒绝是"什么都没发生"，即使没记上审计，
        结果也是安全的（内容没有流出）。而如果因为审计失败就抛新异常，
        调用方会看到一个审计错误而非策略拒绝，反而更难判断发生了什么。
        完成路径则相反——那时数据已经流出去了，必须能追溯。

        真正需要熔断的场景（审计存储整体不可用）由调用方在更高层判断。
        """
        app = self._app
        from_node = kw.get("from_node", "")
        to_node = kw.get("to_node", "")
        try:
            app.record(
                outcome="denied",
                rule_id=denied.rule_id.value,
                task_type=kw.get("task_type", ""),
                task_id=kw.get("task_id", ""),
                session_id=kw.get("session_id", ""),
                from_node=from_node,
                to_node=to_node,
                from_domain=app.config.domain_of(from_node) or "",
                to_domain=app.config.domain_of(to_node) or "",
            )
        except AuditError as exc:
            warnings.warn(
                f"审计写入失败，本次拒绝未被记录: {exc}",
                RuntimeWarning, stacklevel=2,
            )


# ---------------------------------------------------------------------------
# 后端装配
# ---------------------------------------------------------------------------

def build_stores(config: NexusConfig, *, force_memory: bool = False) -> AppStores:
    """按配置装配存储后端。

    ``force_memory=True`` 时全部走内存（demo 与测试），否则尊重配置。
    """
    use_memory = force_memory

    if use_memory or config.stores.queue == "memory":
        queue: QueueStore = memory.MemoryQueueStore()
    else:
        from .backends.sqlite import SqliteQueueStore
        queue = SqliteQueueStore(_data_path(config, "queue.db"))

    if use_memory or config.stores.registry == "memory":
        reg: RegistryStore = memory.MemoryRegistryStore()
    else:
        from .backends.sqlite import SqliteRegistryStore
        reg = SqliteRegistryStore(_data_path(config, "registry.db"))

    if use_memory or config.stores.audit == "file":
        audit: AuditStore = (
            memory.MemoryAuditStore() if use_memory
            else FileAuditStore(_data_path(config, "audit.ndjson"))
        )
    else:  # pragma: no cover - 配置校验已限制取值
        raise ConfigError(f"未知审计后端: {config.stores.audit}")

    return AppStores(registry=reg, queue=queue, audit=audit)


def _today_start() -> str:
    """今天零点的 ISO8601 表示，用于"按自然日"统计配额。"""
    from datetime import datetime as _dt
    now = _dt.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(
        timespec="seconds")


def _data_path(config: NexusConfig, name: str):
    from pathlib import Path
    base = Path(config.stores.data_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base / name


__all__ = [
    "AppStores",
    "InProcessTransport",
    "NexusApp",
    "Transport",
    "build_stores",
]
