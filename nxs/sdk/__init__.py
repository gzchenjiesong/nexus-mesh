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
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..core.errors import PolicyDenied
from ..core.models import Artifact, Level, RoleCard, Task, TaskStatus
from ..data.boundary import LocalBoundary

log = logging.getLogger("nxs.sdk")

#: 处理函数签名：接收任务参数，返回产出数据。
Handler = Callable[[Mapping[str, Any]], Any]


class _LeaseKeeper:
    """执行期间的租约看护者。

    为什么需要它
    ------------
    租约默认 15 秒。公司电脑上跑的检索、整理、读文件类任务很容易超过 ——
    一旦超时，任务自动回到队列，可能被**别的节点领走**，
    而本节点还在慢慢算。算完再回传就是重复副作用。

    为什么不是"调大租约时间"
    ------------------------
    租约有第二个作用：多久没交活就判它死了。调大等于延长断电恢复的等待，
    是拿可靠性换省事。**正确做法是长任务主动续租。**

    ⭐ 一个必须说清的局限
    --------------------
    Python **无法干净地中断**一个正在执行的同步函数。所以本类不做
    "中断执行"，只做"记录是否失去租约"。执行照常跑完，
    但 :attr:`lost` 为真时**调用方必须丢弃产出、不回传**。

    这个取舍是刻意的：中断不了，那就别采纳它的结果。
    比起"试图 kill 线程但可能留下半写状态"，丢弃结果要安全得多。
    """

    def __init__(self, transport: Any, task_id: str, interval: float,
                 logger: logging.Logger) -> None:
        self._transport = transport
        self._task_id = task_id
        self._interval = interval
        self._log = logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lost = False

    @property
    def lost(self) -> bool:
        """是否曾经续租失败（= 已失去该任务）。"""
        return self._lost

    def start(self) -> "_LeaseKeeper":
        if self._interval <= 0:
            return self
        renew = getattr(self._transport, "renew", None)
        if renew is None:
            # 同进程传输与测试替身没有网络租约概念，跳过即可
            self._log.debug("传输不支持 renew，跳过续租")
            return self
        self._thread = threading.Thread(
            target=self._run, args=(renew,),
            name="nxs-lease", daemon=True,
        )
        self._thread.start()
        return self

    def _run(self, renew: Callable[[str], Any]) -> None:
        while not self._stop.wait(self._interval):
            try:
                renew(self._task_id)
            except PolicyDenied as denied:
                # ⚠ 不是"警告"，是"你手上这个任务已经不属于你了"
                self._lost = True
                self._log.error(
                    "[%s] 续租失败（%s）——已失去该任务，产出将被丢弃",
                    self._task_id, denied.rule_id.value,
                )
                return
            except Exception as exc:  # noqa: BLE001 - 网络抖动不该拖垮执行
                # 网络问题不判定为"失去任务"：租约可能仍然有效，
                # 下一轮再试。真正的失去由 PolicyDenied 表达。
                self._log.warning("[%s] 续租请求出错（将重试）：%s",
                                  self._task_id, exc)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


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
    #: 角色卡。给了就能自注册（真实部署的场景）；
    #: 省略时假定中心侧已按配置登记过（demo 与测试的场景）。
    #:
    #: ⚠ **卡片必须是确定性生成的**——不要放时间戳或随机串。
    #: 卡片内容一变，中心侧会把批准状态撤销（审批针对的是那张卡）。
    card: RoleCard | None = None
    #: 心跳/重注册间隔（秒）。``0`` 表示不做心跳。
    #: 心跳复用注册接口，所以它同时也是"我的能力有没有变"的上报。
    heartbeat_interval: float = 60.0
    #: 执行期间的续租间隔（秒）。``0`` 表示不续租（仅适合快任务）。
    #:
    #: 默认 5 秒 —— 比默认 15 秒租约小得多，留足了重试余地。
    #: **不要靠调大租约时间来适配长任务**：租约同时是"多久没交活就判死"
    #: 的判据，调大等于延长断电恢复的等待。
    renew_interval: float = 5.0

    def __post_init__(self) -> None:
        missing = set(self.handlers) - set(self.levels)
        if missing:
            raise ValueError(
                f"这些处理器缺少等级声明: {sorted(missing)}。"
                f"等级必须显式给出，因为它决定边界是否放行产出。"
            )
        if self.card is not None and self.card.node_id != self.node_id:
            raise ValueError(
                f"角色卡的 node_id={self.card.node_id!r} 与配置的 "
                f"{self.node_id!r} 不一致——中心侧会拒绝注册"
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
        #: 最近一次注册的结果（含是否已批准）。None 表示还没注册过。
        self.registration: dict[str, Any] | None = None
        self._last_heartbeat = 0.0
        #: 心跳线程的停止信号。`run_forever` 启动线程后由它驱动心跳。
        self._hb_stop = threading.Event()
        self._hb_thread: threading.Thread | None = None
        #: 节点侧应答边界。**数据在离开节点之前先经过它。**
        self._local_boundary = LocalBoundary(
            config.card, levels=config.levels,
        )
        if self._local_boundary.degraded:
            self._log.warning(
                "[%s] 未提供角色卡，节点侧边界**降级**运行：只能校验产出等级，"
                "无法校验应答字段白名单。若要完整防护，请在 NodeConfig.card "
                "里给出角色卡。",
                config.node_id,
            )

    # ------------------------------------------------------------------
    # 注册与心跳
    # ------------------------------------------------------------------

    def register(self) -> dict[str, Any]:
        """提交角色卡（兼作心跳）。

        未配置 ``card`` 时是**合法的空操作**——那种部署形态下节点由中心侧
        配置登记，不需要自注册。返回一个说明而不是抛异常，
        这样 ``run_forever`` 里不必为两种形态写两个分支。
        """
        if self.config.card is None:
            return {"outcome": "skipped", "approved": True,
                    "hint": "no card configured (registered by coordinator config)"}
        try:
            result = self._transport.register(self.config.card)
        except Exception as exc:  # noqa: BLE001 - 注册失败不该拖垮节点
            self._log.warning("[%s] 注册失败：%s", self.config.node_id, exc)
            return {"outcome": "error", "approved": False, "hint": str(exc)}

        self.registration = result
        approved = result.get("approved")
        if approved is False:
            # 明确说出来。节点在等运维批准时，日志里只有"领取被拒"
            # 会让人以为配置错了，而不是"还没批"
            self._log.warning(
                "[%s] 已注册但**尚未批准**（outcome=%s, revision=%s）——"
                "需运维在中心侧批准后才可接收任务",
                self.config.node_id, result.get("outcome"), result.get("revision"),
            )
        elif result.get("outcome") in ("created", "card_changed"):
            self._log.info(
                "[%s] 注册结果 %s（revision=%s）",
                self.config.node_id, result.get("outcome"), result.get("revision"),
            )
        return result

    def _maybe_heartbeat(self) -> None:
        """到点就重注册。间隔取 ``heartbeat_interval``，0 表示不做。

        ⚠ **保留这个方法只给"没有后台线程"的场景用**（如单次 ``run_once``
        调用、测试）。常驻场景请用 :meth:`_start_heartbeat_thread` ——
        理由见那里。
        """
        interval = self.config.heartbeat_interval
        if interval <= 0:
            return
        now = time.monotonic()
        if now - self._last_heartbeat < interval:
            return
        self._last_heartbeat = now
        # ⚠ 心跳失败不该让整个常驻循环崩掉：网络抖动一次就退出，
        #   而调用方会以为节点"崩了"。吞掉异常，下一轮再试。
        try:
            self.register()
        except Exception as exc:      # noqa: BLE001
            self._log.warning(
                "[%s] 心跳失败（将在 %ss 后重试）：%s",
                self.config.node_id, interval, exc,
            )

    def _start_heartbeat_thread(self) -> None:
        """在**独立线程**里发心跳。

        ## ⚠⚠ 为什么必须是独立线程 —— 这是实机踩出来的

        原来心跳挂在常驻循环里：

            while True:
                task = self.run_once()      # ← 执行 handler，可能几十秒到几分钟
                self._maybe_heartbeat()     # ← 任务返回后才发

        对"快任务"没问题。但**任何超过 ``heartbeat_interval × 3``
        的任务都会让节点被判 stale**，而那期间：

            · 其他节点看到它 stale → 投递被 ``AU-APPROVE-05`` 拒
            · 观测窗显示 stale，看起来像节点挂了
            · 而它其实正在好好干活

        实测（2026-10-30，work-macbook 接 CodeBuddy）：一个
        `work.query` 跑 140 秒，节点 90 秒时变 stale，
        云端 Agent 再发任务直接收到 `AU-APPROVE-05` —— 
        **"我明明在跑，为什么说我没批准"**。

        ## 已有的先例

        `_LeaseKeeper`（租约续期）早就是独立线程了，理由完全相同：
        长任务期间不能让"续命"这件事被阻塞。

        **心跳漏了同样的处理，是不一致导致的疏漏。**
        """
        interval = self.config.heartbeat_interval
        if interval <= 0:
            return

        def _run() -> None:
            while not self._hb_stop.wait(interval):
                self._last_heartbeat = time.monotonic()
                try:
                    self.register()
                except Exception as exc:      # noqa: BLE001
                    # 同理：一次网络抖动不该让线程死掉
                    self._log.warning(
                        "[%s] 心跳失败（线程继续）：%s",
                        self.config.node_id, exc,
                    )

        self._hb_thread = threading.Thread(
            target=_run, name="nxs-heartbeat", daemon=True,
        )
        self._hb_thread.start()

    def stop(self) -> None:
        """停掉后台线程。常驻循环退出前调用（或靠 daemon 自然结束）。"""
        self._hb_stop.set()

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
        * 有处理器 → 执行 → **节点侧边界裁剪** → 包装产物 → 回传
        * 无处理器 → 上报失败（**不该发生**：策略层已保证类型可达，
          走到这里说明角色卡声明与实际实现不一致）
        * 处理器抛异常 → 上报失败，原因写入任务

        ⭐ **节点侧边界在回传之前执行**，这是本系统安全模型的关键位置：
        数据在离开节点之前就已经被裁剪到"自己声明的范围"。
        协调节点侧还有一道权威校验，但那一道在 coordinator 被攻破时不可信——
        真正保护原始数据的是这一道。
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

        # ★ 执行期间自动续租：长任务不能靠调大租约时间
        keeper = _LeaseKeeper(
            self._transport, task.id,
            interval=max(1.0, self.config.renew_interval),
            logger=self._log,
        ).start()
        try:
            data = handler(dict(task.params))
        except Exception as exc:  # noqa: BLE001 - 处理器是外部代码，必须兜底
            keeper.stop()
            self.failed += 1
            self._log.exception("[%s] 处理器异常: %s", self.config.node_id, task.type)
            return self._transport.fail(
                self.config.node_id, task, f"{type(exc).__name__}: {exc}"
            )
        finally:
            keeper.stop()

        if keeper.lost:
            # 执行期间失去了租约：任务已回队列、可能已被别人领走。
            # **不回传、也不上报失败** —— 上报失败会干扰现在持有它的那个节点。
            # 让它自己的租约机制照常运行即可。
            self._log.error(
                "[%s] 任务 %s 执行期间失去租约，产出已丢弃（不占用重试次数）",
                self.config.node_id, task.id,
            )
            return task

        artifact = self._wrap(task, data)

        # ★ 节点侧边界：不通过就**根本不发送**
        try:
            artifact = self._local_boundary.check(task, artifact)
        except PolicyDenied as denied:
            self.denied += 1
            self._log.error(
                "[%s] 产出被**节点侧**边界拦下（未发送）: %s — %s",
                self.config.node_id, denied.rule_id.value, denied.detail,
            )
            # 上报失败而不是完成：任务已经执行了但产出不能给，
            # 让中心侧知道"这个任务不会再有结果"，不必等租约超时
            return self._transport.fail(
                self.config.node_id, task,
                f"local boundary rejected: {denied.rule_id.value}",
            )

        try:
            done = self._transport.complete(self.config.node_id, task, [artifact])
            self.processed += 1
            return done
        except PolicyDenied as denied:
            # 边界拒绝了产出——这是**设计要生效**的场景，不是异常情况
            self.denied += 1
            self._log.warning(
                "[%s] 回传被中心侧边界拒绝: %s（产出被丢弃）",
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
        """常驻循环：注册 → 轮询 → 执行 → 定期心跳。

        :param should_stop: 返回 True 时退出。便于测试与优雅停机。
        """
        self.register()          # 先登记，再开始领任务
        self._last_heartbeat = time.monotonic()

        # ⚠ 心跳放在**独立线程**里，不挂在下面的循环上。
        #
        #   原来是在 `run_once()` 之后调 `_maybe_heartbeat()` —— 而
        #   `run_once()` 会执行 handler（跑 LLM / CLI，几十秒到几分钟）。
        #   于是任何超过 `heartbeat_interval × 3` 的任务都会让节点变 stale，
        #   期间别人投递被 `AU-APPROVE-05` 拒。
        #
        #   详见 `_start_heartbeat_thread` 的说明。
        self._start_heartbeat_thread()

        try:
            while True:
                if should_stop is not None and should_stop():
                    return
                try:
                    task = self.run_once()
                except PolicyDenied:
                    task = None       # 已被记录，继续下一轮
                if task is None:
                    time.sleep(self.config.poll_interval)
        finally:
            self._hb_stop.set()

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
