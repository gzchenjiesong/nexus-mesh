"""节点 SDK —— 让一个 Agent 接入网络所需的最小客户端。

职责边界
--------
只做五件事：**注册、心跳、轮询、执行回调、回传**。
**不含任何业务逻辑**——它是接入用的骨架，不是框架。

节点侧实现者只需要提供两个东西：
1. 一个 ``handlers`` 映射：任务类型 → 处理函数
2. 一个传输实现（:class:`nxs.app.Transport` 的任意实现）

其余全部由本模块处理：领取、异常兜底、写回产物、失败上报。

设计要点
--------
* **处理器签名统一**：``handler(params: dict) -> Data``，返回的
  ``Data`` 会被包装成 ``Artifact``。处理器不需要关心等级——
  但**必须**在声明能力时给出正确的 ``level``，因为边界会据此校验。
* **异常不吞**：处理器抛异常时上报 ``failed`` 并把原因写入任务，
  而不是静默丢弃——否则任务会一直靠租约超时回收，浪费重试次数。
* **不做重试**：重试由租约机制统一处理。节点侧重试会造成重复执行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..core.errors import PolicyDenied
from ..core.models import Artifact, Level, Task, TaskStatus

log = logging.getLogger("nxs.sdk")

#: 处理函数签名：接收任务参数，返回产出数据。
Handler = Callable[[Mapping[str, Any]], Any]


@dataclass
class NodeConfig:
    """节点接入配置。"""

    node_id: str
    #: 处理函数表：任务类型 → 处理器
    handlers: dict[str, Handler] = field(default_factory=dict)
    #: 产出等级：任务类型 → Level。**必须与角色卡里声明的一致**，
    #: 否则边界会拒绝（这是有意的：声明与实现不符应该被发现）。
    levels: dict[str, Level] = field(default_factory=dict)
    #: 每次轮询之间的间隔（秒）。仅用于 :meth:`run_once` 的便捷循环。
    poll_interval: float = 1.0

    def __post_init__(self) -> None:
        missing = set(self.handlers) - set(self.levels)
        if missing:
            raise ValueError(
                f"这些处理器缺少等级声明: {sorted(missing)}。"
                f"等级必须显式给出，因为它决定边界是否放行产出。"
            )


class NodeClient:
    """节点客户端。

    典型用法::

        client = NodeClient(cfg, transport)
        client.run_once()          # 领一个任务、执行、回传
        client.run_forever()       # 循环（用于常驻节点）

    :param transport: 任意实现 ``Transport`` 协议的对象。
        demo 用同进程传输，真实部署走 HTTP——**节点逻辑对两者无感知**。
    """

    def __init__(self, config: NodeConfig, transport: Any, *,
                 logger: logging.Logger | None = None) -> None:
        self.config = config
        self._transport = transport
        self._log = logger or log
        self.processed = 0
        self.denied = 0
        self.failed = 0

    # ------------------------------------------------------------------
    # 单次轮询
    # ------------------------------------------------------------------

    def run_once(self) -> Task | None:
        """领取并处理一个任务。返回处理后的任务；队列为空返回 None。"""
        try:
            task = self._transport.poll(self.config.node_id)
        except PolicyDenied as denied:
            self.denied += 1
            self._log.warning(
                "[%s] 领取被拒: %s", self.config.node_id, denied.rule_id.value
            )
            return None

        if task is None:
            return None

        return self.execute(task)

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def execute(self, task: Task) -> Task:
        """执行一个已领取的任务并回传。

        三条路径：
        * 有处理器 → 执行 → 包装产物 → 回传（可能被边界拒绝）
        * 无处理器 → 上报失败（**不该发生**：策略层已保证类型可达，
          走到这里说明角色卡声明与实际实现不一致）
        * 处理器抛异常 → 上报失败，原因写入任务
        """
        handler = self.config.handlers.get(task.type)
        if handler is None:
            self.failed += 1
            self._log.error(
                "[%s] 收到无处理器的任务类型 %s——角色卡声明与实现不一致",
                self.config.node_id, task.type,
            )
            return self._transport.fail(
                self.config.node_id, task,
                f"no handler registered for {task.type}",
            )

        try:
            data = handler(dict(task.params))
        except Exception as exc:  # noqa: BLE001 - 处理器是外部代码，必须兜底
            self.failed += 1
            self._log.exception("[%s] 处理器异常: %s", self.config.node_id, task.type)
            return self._transport.fail(
                self.config.node_id, task, f"{type(exc).__name__}: {exc}"
            )

        artifact = self._wrap(task, data)
        try:
            done = self._transport.complete(self.config.node_id, task, [artifact])
            self.processed += 1
            return done
        except PolicyDenied as denied:
            # 边界拒绝了产出——这是**设计要生效**的场景，不是异常情况
            self.denied += 1
            self._log.warning(
                "[%s] 回传被边界拒绝: %s（产出被丢弃）",
                self.config.node_id, denied.rule_id.value,
            )
            raise

    def _wrap(self, task: Task, data: Any) -> Artifact:
        """把处理器的返回值包装成 Artifact。

        等级取自 :class:`NodeConfig` 的声明，而不是让处理器临时决定——
        等级是"这个类型承诺产出什么"的一部分，应该在接入时就固定下来。
        """
        level = self.config.levels[task.type]
        kind = "structured" if isinstance(data, (dict, list)) else "freetext"
        return Artifact(
            level=level,
            kind=kind,
            data=data,
            provenance={"source": f"{self.config.node_id}/{task.type}"},
        )

    # ------------------------------------------------------------------
    # 循环
    # ------------------------------------------------------------------

    def run_forever(self, *, should_stop: Callable[[], bool] | None = None) -> None:
        """常驻循环。用于真实部署里的节点进程。

        :param should_stop: 返回 True 时退出。便于测试与优雅停机。
        """
        import time

        while True:
            if should_stop is not None and should_stop():
                return
            try:
                task = self.run_once()
            except PolicyDenied:
                task = None       # 已被记录，继续下一轮
            if task is None:
                time.sleep(self.config.poll_interval)

    # ------------------------------------------------------------------
    # 上行推送（Issuer 侧）
    # ------------------------------------------------------------------

    def publish(self, *, to_node: str, task_type: str, params: dict,
                session_id: str = "", idempotency_key: str | None = None):
        """发布任务。需要该节点具备 Issuer 角色与相应的类型白名单。"""
        return self._transport.publish(
            from_node=self.config.node_id, to_node=to_node,
            task_type=task_type, params=params, session_id=session_id,
            idempotency_key=idempotency_key,
        )

    def stats(self) -> dict[str, int]:
        return {
            "processed": self.processed,
            "denied": self.denied,
            "failed": self.failed,
        }


__all__ = ["Handler", "NodeClient", "NodeConfig"]
