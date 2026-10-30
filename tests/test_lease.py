"""续租的测试 —— 长任务不能靠调大租约时间。

为什么不能在测试里真的等 20 秒
------------------------------
用真实时钟会让测试变慢且脆弱（"恰好跨过整秒"这类偶发失败）。
所以这里用**极短租约**（1 秒）+ 短任务（2~3 秒）+ 快续租（0.3 秒），
把同样的时序压缩到可接受的时长。

一个必须说清的取舍
------------------
Python 无法干净地中断正在执行的同步函数。所以续租失败时不是"中断执行"，
而是"执行照常跑完，但**丢弃产出、不回传**"。

这比试图 kill 线程更安全：kill 可能留下半写状态，
而丢弃结果只损失这一次计算。
"""

from __future__ import annotations

import time

import pytest

from conftest import covers

from nxs.app import NexusApp, AppStores
from nxs.backends.memory import (
    MemoryAuditStore,
    MemoryQueueStore,
    MemoryRegistryStore,
)
from nxs.core.errors import PolicyDenied, RuleId
from nxs.core.models import Level, Task, TaskStatus
from nxs.data.task import TaskLifecycle


@pytest.fixture
def make_app():
    """按需构造应用实例，租约长度可指定。

    为什么用工厂而不是固定 fixture：租约长度的选择取决于测什么——
    测"续租延长了没有"需要秒级可辨的间隔（1 秒太粗，
    ``isoformat(timespec="seconds")`` 会把 1.4 秒截断成 1 秒）；
    测"任务跑得比租约久"又需要租约小于任务时长。
    一个固定值无法同时满足。
    """
    from conftest import EXAMPLE_CONFIG, demo_env

    from nxs.app import NexusApp, build_stores
    from nxs.mgmt.config import load_config

    def _make(lease_seconds: int = 1) -> NexusApp:
        config = load_config(EXAMPLE_CONFIG, env=demo_env())
        stores = build_stores(config, force_memory=True)
        app = NexusApp(config, stores, lease_seconds=lease_seconds, max_attempts=3)
        app.start()
        app.approve("node-a")
        app.approve("node-b")
        return app

    return _make


def make_task(task_id: str = "t-renew", to_node: str = "node-a") -> Task:
    return Task(
        id=task_id, idempotency_key=f"idem-{task_id}", type="demo.echo",
        session_id="ses-renew", from_node="coordinator", to_node=to_node,
        status=TaskStatus.WORKING,
    )


# ---------------------------------------------------------------------------
# 1. 生命周期层的续租语义
# ---------------------------------------------------------------------------

class TestLifecycleRenew:

    @covers("M7.F10")
    def test_renew_extends_lease(self, make_app):
        """续租把到期时间往后推，因此在原本会过期的时刻不再过期。

        **用注入时间验证，不 sleep** —— 靠真实时钟的测试会偶发失败，
        而偶发失败的测试会训练人忽略红灯。
        """
        from datetime import datetime, timedelta, timezone

        app = make_app(lease_seconds=3)
        published = app.transport.publish(
            from_node="coordinator", to_node="node-a", task_type="demo.echo",
            params={"text": "x"}, idempotency_key="idem-r1",
        ).task

        # 领取时刻固定为 T，租约 3 秒 → 到期 T+3
        # 用 lifecycle.claim 而不是 mailbox.poll：后者不接受 now 参数，
        # 而本测试必须有可控时钟（见 docstring）
        t0 = datetime.now(timezone.utc).replace(microsecond=0)
        claimed = app.lifecycle.claim("node-a", now=t0.isoformat())
        assert claimed is not None

        # 在 T+2 处续租 → 新到期 T+5
        later = (t0 + timedelta(seconds=2)).isoformat(timespec="seconds")
        app.lifecycle.renew("node-a", published.id, now=later)

        # 在 T+4 处检查：原本 T+3 就过期了，续租后到 T+5 才过期
        check_at = (t0 + timedelta(seconds=4)).isoformat(timespec="seconds")
        released = app.lifecycle.release_expired(now=check_at)
        assert all(t.id != published.id for t in released), (
            "续租没有延长租约"
        )

        # 到 T+7 则必然过期 —— 证明续租只是延后，不是永久
        far = (t0 + timedelta(seconds=7)).isoformat(timespec="seconds")
        released_later = app.lifecycle.release_expired(now=far)
        assert any(t.id == published.id for t in released_later)

    @covers("M7.F10")
    def test_cannot_renew_someone_elses_task(self, make_app):
        """不能替别人续租。任务不存在 → 同一个错误码（避免枚举）。"""
        app = make_app(lease_seconds=1)
        app.transport.publish(
            from_node="coordinator", to_node="node-a", task_type="demo.echo",
            params={"text": "x"}, idempotency_key="idem-r2",
        )
        claimed = app.mailbox.poll("node-a")

        with pytest.raises(PolicyDenied) as excinfo:
            app.lifecycle.renew("node-b", claimed.id)
        # ⚠ 权限模型改造 Step 4：这里原来用的那个「什么都管」的旧码已经删除，
        #   改成了 LF-TASK-404。
        #
        #   语义没变（"这个任务不属于你"），但码更准确了 ——
        #   LF-TASK-404 的定义正是「任务不存在，或调用方看不到它」。
        assert excinfo.value.rule_id is RuleId.LF_TASK_NOT_FOUND

        # 不存在的任务返回同样的码 —— 否则可用来枚举
        with pytest.raises(PolicyDenied) as excinfo2:
            app.lifecycle.renew("node-b", "no-such-task")
        assert excinfo2.value.rule_id is RuleId.LF_TASK_NOT_FOUND

    @covers("M7.F10")
    def test_renew_after_terminal_rejected(self, make_app):
        """任务已落终态后续租 → 拒（LF-STATE-03）。"""
        app = make_app(lease_seconds=1)
        app.transport.publish(
            from_node="coordinator", to_node="node-a", task_type="demo.echo",
            params={"text": "x"}, idempotency_key="idem-r3",
        )
        claimed = app.mailbox.poll("node-a")
        app.transport.complete("node-a", claimed, [])

        with pytest.raises(PolicyDenied) as excinfo:
            app.lifecycle.renew("node-a", claimed.id)
        assert excinfo.value.rule_id is RuleId.LF_INVALID_TRANSITION

    @covers("M7.F10", "M7.R4")
    def test_renew_after_lease_expired_means_task_lost(self, make_app):
        """★ 租约已过期并释放后续租 → **LF-EXPIRE-02**。

        这是最重要的一条：此时任务已回队列、可能已被别人领走。
        调用方必须据此**停止执行并丢弃产出**，否则会产生重复副作用。
        """
        from datetime import datetime, timedelta, timezone

        app = make_app(lease_seconds=1)
        app.transport.publish(
            from_node="coordinator", to_node="node-a", task_type="demo.echo",
            params={"text": "x"}, idempotency_key="idem-r4",
        )
        claimed = app.mailbox.poll("node-a")

        # 让租约过期并释放
        future = (datetime.now(timezone.utc) + timedelta(seconds=10)
                  ).isoformat(timespec="seconds")
        app.lifecycle.release_expired(now=future)

        with pytest.raises(PolicyDenied) as excinfo:
            app.lifecycle.renew("node-a", claimed.id)
        assert excinfo.value.rule_id is RuleId.LF_TASK_EXPIRED


# ---------------------------------------------------------------------------
# 2. 端点层
# ---------------------------------------------------------------------------

class TestAckEndpoint:

    @covers("M7.F9")
    def test_ack_over_http(self, make_app):
        import secrets as _secrets

        from nxs.edge.client import HttpTransport
        from nxs.edge.gateway import Gateway
        from nxs.mgmt.config import ServerConfig

        app = make_app(lease_seconds=1)
        gw = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0),
                     ops_secret=b"").start()
        try:
            secret = _secrets.token_hex(32).encode()
            gw.authenticator.set_secret("node-a", secret)
            tr = HttpTransport(gw.base_url, node_id="node-a", secret=secret)

            coordinator = HttpTransport(gw.base_url, node_id="coordinator",
                                secret=app.config.secrets["coordinator"])
            coordinator.publish(from_node="coordinator", to_node="node-a",
                        task_type="demo.echo", params={"text": "x"},
                        idempotency_key="idem-ack")
            claimed = tr.poll("node-a")

            lease = tr.renew(claimed.id)
            assert lease >= 1, "服务端应告知建议的续租间隔"
        finally:
            gw.stop()

    @covers("M7.F9")
    def test_ack_requires_authentication(self, make_app):
        import json
        import socket

        from nxs.edge.gateway import PREFIX, Gateway
        from nxs.mgmt.config import ServerConfig

        gw = Gateway(make_app(lease_seconds=1),
                     server_config=ServerConfig(host="127.0.0.1", port=0),
                     ops_secret=b"").start()
        try:
            body = json.dumps({"task_id": "t-1"}).encode()
            with socket.create_connection(("127.0.0.1", gw.port), timeout=5) as sock:
                sock.sendall((
                    f"POST {PREFIX}/ack HTTP/1.0\r\nHost: x\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                ).encode() + body)
                sock.shutdown(socket.SHUT_WR)
                raw = sock.recv(65536)
            assert int(raw.split(b"\r\n", 1)[0].split(b" ")[1]) == 401
        finally:
            gw.stop()


# ---------------------------------------------------------------------------
# 3. 时区归一化（修掉一个静默失效的真 bug）
# ---------------------------------------------------------------------------

class TestTimezoneNormalisation:
    """租约时间曾经用 ISO 字符串字典序比较，跨时区表示时会静默判错。

    ```
    "2026-10-25T12:13:26+08:00"  vs  "2026-10-25T04:13:35+00:00"
    ```

    前者是 04:13:26 UTC，比后者早 9 秒，**已经过期**。
    但字典序比较认为 ``"12..." > "04..."`` → 判定"未过期"。

    后果不是崩溃，而是**断电恢复静默失灵**：任务永远不回队列，
    表面上一切正常。这类 bug 能查三天，所以必须有守护测试。

    真实场景并非假设：云服务器通常 UTC，本地机器通常 +08:00。
    """

    @covers("M7.F10", "M7.R1")
    def test_to_utc_iso_normalises_offset(self):
        from nxs.core.envelope import to_utc_iso

        assert to_utc_iso("2026-10-25T12:13:26+08:00") == "2026-10-25T04:13:26+00:00"
        assert to_utc_iso("2026-10-25T04:13:26+00:00") == "2026-10-25T04:13:26+00:00"
        # 无时区信息时按 UTC 解释（既有约定）
        assert to_utc_iso("2026-10-25T04:13:26") == "2026-10-25T04:13:26+00:00"

    @covers("M7.F10", "M7.R1")
    def test_memory_store_compares_across_timezones(self):
        """内存后端：+08:00 的查询时刻能正确判定 UTC 存储的租约已过期。"""
        from datetime import datetime, timedelta, timezone

        from nxs.backends.memory import MemoryQueueStore

        store = MemoryQueueStore()
        task = make_task("t-tz")
        store.enqueue(task)

        # 领取时刻固定，租约 5 秒
        t0 = datetime(2026, 10, 25, 4, 0, 0, tzinfo=timezone.utc)
        store.claim("node-a", lease_seconds=5, now=t0.isoformat())

        # 用 +08:00 表示"T+10 秒"（= UTC 04:00:10），此时租约早该过期
        plus8 = timezone(timedelta(hours=8))
        query = (t0 + timedelta(seconds=10)).astimezone(plus8).isoformat()
        released = store.release_expired(now=query)
        assert len(released) == 1, (
            "跨时区表示下没有被判过期 —— 断电恢复会静默失灵"
        )

    @covers("M7.F10", "M7.R1")
    def test_sqlite_store_compares_across_timezones(self, workdir):
        """SQLite 后端同样正确（它也用字符串比较，同样需要归一化）。"""
        from datetime import datetime, timedelta, timezone

        from nxs.backends.sqlite import SqliteQueueStore

        store = SqliteQueueStore(workdir / "tz-queue.db")
        try:
            store.enqueue(make_task("t-tz-sqlite"))
            t0 = datetime(2026, 10, 25, 4, 0, 0, tzinfo=timezone.utc)
            store.claim("node-a", lease_seconds=5, now=t0.isoformat())

            plus8 = timezone(timedelta(hours=8))
            query = (t0 + timedelta(seconds=10)).astimezone(plus8).isoformat()
            released = store.release_expired(now=query)
            assert len(released) == 1, (
                "SQLite 后端跨时区判定失效 —— 断电恢复会静默失灵"
            )
        finally:
            store.close()


# ---------------------------------------------------------------------------
# 4. SDK 自动续租
# ---------------------------------------------------------------------------

class TestSdkAutoRenew:

    @covers("M7.F11", "M7.R4")
    def test_long_task_survives_beyond_lease(self, make_app):
        """★ 任务执行时间（约 2.5s）远超租约（1s），靠续租活下来。"""
        import secrets as _secrets

        from nxs.edge.client import HttpTransport
        from nxs.edge.gateway import Gateway
        from nxs.mgmt.config import ServerConfig
        from nxs.sdk import NodeClient, NodeConfig

        app = make_app(lease_seconds=1)
        gw = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0),
                     ops_secret=b"").start()
        try:
            secret = _secrets.token_hex(32).encode()
            gw.authenticator.set_secret("node-b", secret)
            tr = HttpTransport(gw.base_url, node_id="node-b", secret=secret)

            def slow_handler(params):
                time.sleep(2.5)
                return {"slept": 2.5, "level": "summary"}

            client = NodeClient(NodeConfig(
                "node-b",
                handlers={"demo.slow": slow_handler},
                levels={"demo.slow": Level.SUMMARY},
                card=app.config.nodes["node-b"],
                heartbeat_interval=0,
                renew_interval=0.3,
            ), tr)

            coordinator = HttpTransport(gw.base_url, node_id="coordinator",
                                secret=app.config.secrets["coordinator"])
            coordinator.publish(from_node="coordinator", to_node="node-b",
                        task_type="demo.slow", params={"seconds": 2.5},
                        idempotency_key="idem-slow")
            claimed = tr.poll("node-b")
            done = client.execute(claimed)

            assert done.status == TaskStatus.COMPLETED, (
                "长任务被误判中断 —— 续租没有生效"
            )
        finally:
            gw.stop()

    @covers("M7.F11")
    def test_lost_lease_discards_output(self):
        """★ 续租失败时**丢弃产出、不回传**。

        用记账 transport 验证 `complete` 从未被调用——
        因为任务可能已被别人领走，回传会造成重复。
        """
        from nxs.sdk import NodeClient, NodeConfig

        class LostLeaseTransport:
            def __init__(self):
                self.completed = []
                self.failed = []

            def register(self, card):
                return {"outcome": "created", "approved": True, "revision": 1}

            def poll(self, node_id):
                return None

            def renew(self, task_id):
                raise PolicyDenied(RuleId.LF_TASK_EXPIRED, "lost")

            def complete(self, node_id, task, artifacts):
                self.completed.append(task.id)
                return task

            def fail(self, node_id, task, reason):
                self.failed.append(task.id)
                return task

        transport = LostLeaseTransport()
        client = NodeClient(NodeConfig(
            "node-x",
            handlers={"demo.echo": lambda p: {
                "echo": p["text"], "level": "metadata"
            }},
            levels={"demo.echo": Level.METADATA},
            card=None,
            heartbeat_interval=0,
            renew_interval=0.05,     # 极快，让续租立刻发生
        ), transport)
        # 等续租线程跑起来并失败
        task = make_task()
        client.execute(task)
        time.sleep(0.3)              # 让续租线程有机会执行

        assert transport.completed == [], (
            "失去租约后仍回传了产出 —— 会造成重复副作用"
        )


class TestHeartbeatIsNotBlockedByLongTask:
    """★★ 心跳必须在**长任务执行期间**照常发。

    ⚠ **这一节来自 2026-10-30 的一次真实故障。**

    接入本机 Mac（work-macbook）后，云端 Agent 发来的 `work.query`
    要跑 CodeBuddy，耗时 140 秒。期间出现：

        云端再发任务 → AU-APPROVE-05「尚未批准或不可路由」

    而本机进程明明在好好干活。

    ## 根因

    原来心跳挂在常驻循环上：

        while True:
            task = self.run_once()      # ← 执行 handler，几十秒到几分钟
            self._maybe_heartbeat()     # ← 任务返回后才发

    服务端判 stale 的阈值是 `heartbeat_interval × 3`（30×3=90 秒）。
    所以**任何超过 90 秒的任务都会让节点变 stale**，而这期间别人
    投递会被 `AU-APPROVE-05` 拒 —— 错误信息说"未批准"，
    完全指错方向。

    ## 为什么之前没暴露

    work-desktop 之前的任务都很快（< 30 秒），心跳赶得上。
    本机 CodeBuddy 要读仓库、多轮工具调用，把这个问题顶出来了。

    ## 已有的先例

    `_LeaseKeeper`（租约续期）早就是独立线程了 —— 同一个理由。
    心跳漏了同样的处理，是**不一致导致的疏漏**。
    """

    @covers("M7.F11")
    def test_heartbeat_fires_while_handler_is_blocking(self):
        """handler 阻塞 1.2 秒期间，心跳（间隔 0.2s）应发出多次。

        用记账 transport，不打网络 —— 这条测的是**调度性质**，
        与传输无关。
        """
        import threading as _threading

        from nxs.control import make_task
        from nxs.sdk import NodeClient, NodeConfig

        beats = []
        release = _threading.Event()

        class BeatingTransport:
            """记账 transport：register 记一笔，poll 只回一次任务。"""

            def __init__(self):
                self._task = None
                self._handed_out = False

            def register(self, card):
                beats.append(time.monotonic())
                return {"outcome": "unchanged", "approved": True}

            def poll(self, node_id):
                if self._task is None and not self._handed_out:
                    self._handed_out = True
                    self._task = make_task(
                        task_id="t-hb", idempotency_key="i-hb",
                        task_type="demo.echo", from_node="coordinator",
                        to_node="node-b", params={"text": "x"},
                    )
                    return self._task
                return None

            def complete(self, *a, **k):
                return None

            def fail(self, *a, **k):
                return None

            def renew(self, task_id):
                return None

        def blocking_handler(params):
            # 模拟"跑 CodeBuddy 140 秒"—— 这里缩到 1.2 秒，
            # 但比例相同：远大于心跳间隔。
            release.wait(1.2)
            return {"echo": "x", "level": "metadata"}

        # ⚠ **必须给 card** —— `NodeClient.register()` 在 `card=None` 时
        #   直接返回 `{"outcome": "skipped"}`（那是"节点由中心侧配置登记"
        #   的合法形态），压根不调 transport.register。
        #   于是心跳一次都不会发生，而测试会误判成"心跳被阻塞"。
        #   第一版就是这么写的，beats 是空的。
        from nxs.core.models import RoleCard

        card = RoleCard.from_dict({
            "node_id": "node-b", "domain": "zone-a", "display_name": "B",
            "role": "worker",
            "self_limits": {"max_response_level": "metadata"},
            # ⚠ response_fields 必须含 handler 实际返回的所有字段。
            #   漏了 `level` 会被节点侧边界拦下（WL-FIELD-001），
            #   而那次拦截发生在 handler 之后 —— 心跳次数照样对，
            #   但 processed 计数会不对，测试在其他地方红。
            "capabilities": [{"type": "demo.echo", "level": "metadata",
                              "mode": "sync",
                              "response_fields": ["echo", "level"]}],
        })

        client = NodeClient(
            NodeConfig(
                "node-b",
                handlers={"demo.echo": blocking_handler},
                levels={"demo.echo": Level.METADATA},
                card=card,
                heartbeat_interval=0.2,   # 心跳很密，便于观察
                renew_interval=0,         # 不测续租，只测心跳
                poll_interval=0.05,
            ),
            BeatingTransport(),
        )

        # 只跑一个任务，然后停
        stop_after_one = _threading.Event()

        def should_stop() -> bool:
            return stop_after_one.is_set()

        def _drive():
            try:
                client.run_forever(should_stop=should_stop)
            except Exception:      # noqa: BLE001
                pass

        t = _threading.Thread(target=_drive, daemon=True)
        t.start()
        time.sleep(1.4)            # 覆盖 handler 的 1.2 秒
        stop_after_one.set()
        client._hb_stop.set()
        release.set()
        t.join(timeout=3)

        assert client.processed == 1, "任务应当被执行一次"

        # 关键断言：handler 阻塞期间心跳发生了多次。
        #
        # ⚠ 判据用「次数 >= 3」而不是「间隔精确」——
        #   定时器有抖动，断言精确间隔会 flaky。
        #
        # 1.2 秒 / 0.2 秒间隔 ≈ 6 次；留余量到 3 次。
        # 旧实现下这里是 0 或 1（只在任务前后各一次）。
        assert len(beats) >= 3, (
            f"handler 阻塞 1.2 秒期间只发了 {len(beats)} 次心跳"
            f"（间隔 0.2s，应 >= 3 次）。\\n"
            "  心跳被长任务阻塞了 —— 那会让节点在执行期间变 stale，\\n"
            "  而别人投递会收到 AU-APPROVE-05「未批准」，方向全错。"
        )

    @covers("M7.F11")
    def test_heartbeat_thread_is_daemon(self):
        """心跳线程必须是 daemon —— 否则进程退不掉。

        常驻桥接被 SIGTERM 时，非 daemon 线程会让进程挂住，
        运维看到"停了但 systemd 还在等"。
        """
        from nxs.sdk import NodeClient, NodeConfig

        class NullTransport:
            def register(self, card):
                return {"outcome": "unchanged", "approved": True}

            def poll(self, node_id):
                return None

        from nxs.core.models import RoleCard

        card = RoleCard.from_dict({
            "node_id": "n-1", "domain": "d", "display_name": "N",
            "role": "worker", "capabilities": [],
        })
        client = NodeClient(
            NodeConfig("n-1", handlers={}, levels={}, card=card,
                       heartbeat_interval=0.1),
            NullTransport(),
        )
        client._start_heartbeat_thread()
        try:
            assert client._hb_thread is not None
            assert client._hb_thread.daemon, (
                "心跳线程不是 daemon —— 进程会退不掉"
            )
            assert client._hb_thread.is_alive()
        finally:
            client.stop()


class TestTransientNetworkErrorDoesNotKillNode:
    """★★★ 一次网络抖动**不能**让常驻节点退出。

    ⚠⚠ **这是实机最严重的一个 bug（2026-10-30）。**

    本机 work-macbook 跑了 43 分钟后自己死了，死因：

        urllib.error.URLError:
          <urlopen error _ssl.c:1015: The handshake operation timed out>

    而 `run_forever` 只 catch `PolicyDenied` —— **网络层异常一路上冒到
    main，进程 SystemExit(1)**。

    ## 为什么这类比前两个 bug 严重得多

    | bug | 后果 |
    |---|---|
    | 心跳被长任务阻塞 | 某次投递被拒，任务照常完成 |
    | 退出码 0 掩盖真因 | 报错误导，但节点还在 |
    | **网络抖动杀死进程** | **节点消失** |

    桥接通常由 systemd / launchd 拉起。进程一死，**要等人发现才重启** ——
    期间它既不在线、也无人知晓，服务端只留一条 stale。

    ## 为什么必须当成常态处理

    网络抖动不是异常情况：Wi-Fi 切换、笔记本合盖唤醒、云主机偶发抖动、
    TLS 会话重协商 —— 任何一项都会产生一次 `URLError`。
    **用"抖动即退出"应对常态，等于让节点大部分时间离线。**
    """

    @covers("M7.F11")
    def test_poll_error_is_survived_and_retried(self):
        """poll 抛 URLError 后，循环继续，后续能正常领任务。"""
        import threading as _threading
        import urllib.error

        from nxs.control import make_task
        from nxs.core.models import RoleCard
        from nxs.sdk import NodeClient, NodeConfig

        class FlakyTransport:
            """前 2 次 poll 抛网络错误，第 3 次正常返回一个任务。"""

            def __init__(self):
                self.poll_calls = 0
                self.heartbeats = 0

            def register(self, card):
                self.heartbeats += 1
                return {"outcome": "unchanged", "approved": True}

            def poll(self, node_id):
                self.poll_calls += 1
                if self.poll_calls <= 2:
                    raise urllib.error.URLError(
                        "<urlopen error _ssl.c:1015: The handshake "
                        "operation timed out>"
                    )
                if self.poll_calls == 3:
                    return make_task(
                        task_id="t-net", idempotency_key="i-net",
                        task_type="demo.echo", from_node="coordinator",
                        to_node="node-b", params={"text": "x"},
                    )
                return None

            def complete(self, *a, **k):
                return None

            def fail(self, *a, **k):
                return None

            def renew(self, task_id):
                return None

        card = RoleCard.from_dict({
            "node_id": "node-b", "domain": "zone-a", "display_name": "B",
            "role": "worker",
            "self_limits": {"max_response_level": "metadata"},
            "capabilities": [{"type": "demo.echo", "level": "metadata",
                              "mode": "sync",
                              "response_fields": ["echo", "level"]}],
        })

        tr = FlakyTransport()
        client = NodeClient(
            NodeConfig(
                "node-b",
                handlers={"demo.echo": lambda p: {"echo": "ok",
                                                  "level": "metadata"}},
                levels={"demo.echo": Level.METADATA},
                card=card,
                heartbeat_interval=0,     # 本测试不关心心跳
                renew_interval=0,
                poll_interval=0.05,
            ),
            tr,
        )

        stop = _threading.Event()
        crashed: list[BaseException] = []

        def _drive():
            try:
                client.run_forever(should_stop=stop.is_set)
            except BaseException as exc:      # noqa: BLE001
                crashed.append(exc)

        t = _threading.Thread(target=_drive, daemon=True)
        t.start()

        # 等它跨过两次失败并处理掉一个任务。
        # ⚠ 退避是 1s + 2s，所以至少要 3s 才过得去。
        time.sleep(5.0)
        stop.set()
        client._hb_stop.set()
        t.join(timeout=5)

        assert not crashed, (
            f"常驻循环死于网络异常：{crashed[0]!r}\n"
            "  ⚠ 这正是 2026-10-30 那个 bug：一次 TLS 握手超时\n"
            "     就把节点进程带走，而桥接要等人发现才重启。"
        )
        assert tr.poll_calls >= 3, (
            f"只轮询了 {tr.poll_calls} 次 —— 说明前两次失败后没继续重试"
        )
        assert client.processed == 1, (
            f"网络恢复后没处理到任务（processed={client.processed}）"
        )

    @covers("M7.F11")
    def test_repeated_errors_back_off_instead_of_hot_looping(self):
        """持续失败时必须退避 —— 不能紧循环重试。

        服务端挂掉时，紧循环等于每秒几十次无用连接，
        且会把日志瞬间灌满、把真正的错误淹掉。
        """
        import threading as _threading
        import urllib.error

        from nxs.core.models import RoleCard
        from nxs.sdk import NodeClient, NodeConfig

        class DeadTransport:
            def __init__(self):
                self.poll_calls = 0

            def register(self, card):
                return {"outcome": "unchanged", "approved": True}

            def poll(self, node_id):
                self.poll_calls += 1
                raise urllib.error.URLError("connection refused")

            def complete(self, *a, **k):
                return None

            def fail(self, *a, **k):
                return None

            def renew(self, task_id):
                return None

        card = RoleCard.from_dict({
            "node_id": "n-1", "domain": "d", "display_name": "N",
            "role": "worker", "capabilities": [],
        })
        tr = DeadTransport()
        client = NodeClient(
            NodeConfig("n-1", handlers={}, levels={}, card=card,
                       heartbeat_interval=0, renew_interval=0,
                       poll_interval=0.01),
            tr,
        )

        stop = _threading.Event()
        t = _threading.Thread(
            target=lambda: client.run_forever(should_stop=stop.is_set),
            daemon=True,
        )
        t.start()
        time.sleep(4.0)          # 退避 1+2+4=7s，4 秒内只应有 3 次左右
        stop.set()
        client._hb_stop.set()
        t.join(timeout=5)

        # 若紧循环，4 秒内会打几百次（poll_interval=0.01 → 400 次）
        assert tr.poll_calls <= 6, (
            f"4 秒内轮询了 {tr.poll_calls} 次 —— 没有退避，在紧循环重试。\n"
            "  服务端故障时这会制造连接风暴并淹没日志。"
        )
        assert tr.poll_calls >= 2, "至少要重试几次，不能一次失败就放弃"
