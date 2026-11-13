"""阶段 1：本地探针 + 内存末次成功。

## 为什么要有这两样

观察窗的数据全部来自 coordinator 的 ops 端点，于是有个结构性矛盾：

```
coordinator 卡死 → 端点不响应 → 面板什么都拿不到
        ↓
而「它卡死了」正是面板唯一有意义的时刻
```

2026-10-31 那次 coordinator 冻结了 **13 天**，面板能说的只有「连不上」
—— 和 `curl` 没区别。这两样东西合起来才让它在这种时候仍然有用：

    §1 本地探针       回答「它到底怎么了」（进程在不在、卡在哪、队列多长）
    §2 内存末次成功   回答「它挂之前是什么样」（带明确时间戳的陈旧数据）
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from conftest import covers


# ---------------------------------------------------------------------------
# §1 本地探针
# ---------------------------------------------------------------------------

class TestProbeParsing:
    """探针的解析逻辑 —— 与运行环境无关，可以纯单测。"""

    @covers("M15.F11")
    def test_parse_status(self):
        """从 /proc/<pid>/status 取 State / Threads / VmRSS。"""
        from nxs.ui.probe import _parse_status

        raw = (
            "Name:\tpython\n"
            "State:\tS (sleeping)\n"
            "Threads:\t2\n"
            "VmRSS:\t   29488 kB\n"
        )
        got = _parse_status(raw)
        assert got["state"] == "S"
        assert got["threads"] == 2
        assert got["rss_mb"] == 28.8

    @covers("M15.F11")
    def test_parse_accept_queue(self):
        """★ 从 ss -tln 里取 Recv-Q —— 这是「进程不 accept」的直接证据。

        2026-10-31 那次实测就是 Recv-Q=6：6 个连接排队，无人处理。
        这个数字是判断「卡死」最硬的指标之一。
        """
        from nxs.ui.probe import _parse_accept_queue

        ss = (
            "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port\n"
            "LISTEN 6      5      0.0.0.0:18800       0.0.0.0:*\n"
            "LISTEN 0      5      127.0.0.1:8080      0.0.0.0:*\n"
        )
        assert _parse_accept_queue(ss, 18800) == 6
        assert _parse_accept_queue(ss, 8080) == 0
        # 端口不在监听 → None（不是 0！0 表示"在听但没人排队"）
        assert _parse_accept_queue(ss, 9999) is None

    @covers("M15.F11")
    def test_wchan_hints_cover_common_values(self):
        """常见 wchan 必须有人话翻译。

        ⚠ 这条**修过一次**。原断言是：

            assert "死锁" in _WCHAN_HINTS["futex_wait_queue"]

        那是错的：主线程在 ``Event.wait()`` 上也会落在 futex，
        而这个网关的 accept 循环跑在后台线程，**健康进程的主线程
        永久停在 futex 上**（2026-11-13 实测）。把 futex 翻成"死锁"
        会直接在健康进程上报假警报。

        现在要求翻译**中性**，死锁判断交给组合信号。
        """
        from nxs.ui.probe import _WCHAN_HINTS

        # 要翻成人话（"锁"这个词在，运维看得懂在等什么）
        hint = _WCHAN_HINTS["futex_wait_queue"]
        assert "锁" in hint
        # 但**不能**单独断言死锁 —— 见上面。旧文案是
        # 「阻塞在锁上（若持续如此，疑似死锁）」，会误报。
        assert "疑似死锁" not in hint, (
            "futex 的翻译不能直接说疑似死锁 —— 健康进程的主线程也停在这里"
        )
        assert "常态" in hint, (
            "要说明它可能是正常稳态，否则运维仍会顺着死锁方向排查"
        )
        # 正常空闲状态不该被说成异常
        assert "正常" in _WCHAN_HINTS["do_epoll_wait"]
        assert "正常" in _WCHAN_HINTS["do_sys_poll"], (
            "do_sys_poll 是 I/O 线程的常态，必须翻译（判定死锁要用它）"
        )

    @covers("M15.F11")
    def test_wchan_hint_for_sigstop(self):
        """★ `do_signal_stop` 要翻成「进程被暂停」，不能是「未识别」。

        ⚠ 这条来自 2026-11-13 的实测：用 SIGSTOP 模拟 10-31 卡死时，
        探针正确判出了 `unresponsive`，但 wchan 显示
        「do_signal_stop · 未识别的内核状态」—— 信息给了一半。

        而且这个区分有实际价值：**「被暂停」和「死锁」的处置完全不同**。
        前者是有人按了暂停（或调试器 attach），后者是代码有问题。
        报「未识别」会让人往死锁方向排查。
        """
        from nxs.ui.probe import _WCHAN_HINTS

        hint = _WCHAN_HINTS.get("do_signal_stop")
        assert hint, "do_signal_stop 没有翻译"
        assert "暂停" in hint
        # 要明确说"这不是死锁"，否则照样会被误判
        assert "不是死锁" in hint


class TestProbeVerdict:
    """探针的综合判定 —— 这是面板真正要给出的东西。"""

    @covers("M15.F11")
    def test_unresponsive_when_process_alive_but_ops_dead(self, monkeypatch):
        """★★ 进程活着 + ops 不通 → `unresponsive`（最危险的一种）。

        ⚠ 这正是 2026-10-31 的状态：**systemd 说 active，实际已死**。
        探针必须把这两件事区分开，否则用户会以为「服务好好的」。

        判据顺序也有意义：先说「进程活着」（这是事实），
        再说「但不响应」（这是结论），最后给线索（wchan/队列）。
        """
        from nxs.ui import probe as P

        # ⚠ 只 patch 一次 `_run`。早先写了三遍（后覆盖前）——
        # 能跑但读代码的人会以为三个都在生效。
        monkeypatch.setattr(P, "_read_text", lambda p: {
            "status": "State:\tS (sleeping)\nThreads:\t2\nVmRSS:\t 29488 kB",
            "wchan": "futex_wait_queue",
        }.get(p.name))
        monkeypatch.setattr(P, "_run", lambda cmd: (
            "active" if "is-active" in cmd
            else "1234" if "MainPID" in cmd
            else (
                "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port\n"
                "LISTEN 6      5      0.0.0.0:18800       0.0.0.0:*"
            )
        ))
        monkeypatch.setattr(P.Path, "is_dir", lambda self: True)

        got = P.probe_coordinator(ops_reachable=False, unit="nexus-mesh",
                                  port=18800)

        assert got["verdict"] == "unresponsive"
        assert got["process_running"] is True
        # 说明里必须同时含「进程活着」与「不响应」两个事实
        assert "活着" in got["note"]
        assert "不响应" in got["note"]
        # 线索要给出来
        assert "futex" in got["note"]
        assert "6" in got["note"]

    @covers("M15.F11")
    def test_dead_when_process_gone(self, monkeypatch):
        """进程不存在 → `dead`。这个比 `unresponsive` 更确定，不需要猜。"""
        from nxs.ui import probe as P

        monkeypatch.setattr(P, "_run", lambda cmd: (
            "inactive" if "is-active" in cmd
            else "0" if "MainPID" in cmd
            else ""
        ))

        got = P.probe_coordinator(ops_reachable=False, unit="nexus-mesh",
                                  port=18800)
        assert got["verdict"] == "dead"
        assert got["process_running"] is False

    @covers("M15.F11")
    def test_unknown_when_probe_itself_broken(self, monkeypatch):
        """★★ 探针自己不可用 → `unknown`，**绝不能**报成 `dead`。

        ⚠ 这条最重要。探针在 macOS / 容器 / 非 systemd 环境里会全面失败。
        若那时报 `dead`，用户会以为 coordinator 死了而去重启它 ——
        而真相是"这台机器上没有 systemctl"。

        **探针坏了和被测对象坏了，必须分开。**
        """
        from nxs.ui import probe as P

        monkeypatch.setattr(P, "_run", lambda cmd: None)   # 所有命令都失败

        got = P.probe_coordinator(ops_reachable=False, unit="nexus-mesh",
                                  port=18800)
        assert got["verdict"] == "unknown", (
            f"探针全失败时报成了 {got['verdict']} —— 会被误读成服务死了"
        )
        assert got["unavailable"], "必须列出哪些检测不可用"
        assert "systemctl" in " ".join(got["unavailable"])

    @covers("M15.F11")
    def test_healthy_when_all_good(self, monkeypatch):
        """进程在 + ops 通 → `healthy`。"""
        from nxs.ui import probe as P

        monkeypatch.setattr(P, "_run", lambda cmd: (
            "active" if "is-active" in cmd
            else "1234" if "MainPID" in cmd
            else "LISTEN 0 5 0.0.0.0:18800 0.0.0.0:*"
        ))
        monkeypatch.setattr(P, "_read_text", lambda p: (
            "State:\tS (sleeping)\nThreads:\t8\nVmRSS:\t 40000 kB"
            if p.name == "status" else "do_epoll_wait"
        ))
        monkeypatch.setattr(P.Path, "is_dir", lambda self: True)

        got = P.probe_coordinator(ops_reachable=True, unit="nexus-mesh",
                                  port=18800)
        assert got["verdict"] == "healthy"
        assert got["warnings"] == [], f"健康时不该有告警：{got['warnings']}"

    @covers("M15.F11")
    def test_warns_on_futex_and_queue(self, monkeypatch):
        """★ 即使 ops 通，有可疑信号也要进 warnings。

        `warnings` 与 `verdict` 分开：verdict 答「能不能用」，
        warnings 答「稳不稳」。混在一起会让人以为能用就等于没问题。
        """
        from nxs.ui import probe as P

        monkeypatch.setattr(P, "_run", lambda cmd: (
            "active" if "is-active" in cmd
            else "1234" if "MainPID" in cmd
            else "LISTEN 3 5 0.0.0.0:18800 0.0.0.0:*"
        ))
        monkeypatch.setattr(P, "_read_text", lambda p: (
            "State:\tS (sleeping)\nThreads:\t1\nVmRSS:\t 40000 kB"
            if p.name == "status" else "futex_wait_queue"
        ))
        monkeypatch.setattr(P.Path, "is_dir", lambda self: True)

        got = P.probe_coordinator(ops_reachable=True, unit="nexus-mesh",
                                  port=18800)
        blob = " ".join(got["warnings"])
        assert "futex" in blob, "阻塞在锁上必须进告警"
        assert "队列" in blob, "accept 队列非空必须进告警"
        assert "线程" in blob, "线程数过少必须进告警"


class TestFutexIsNotEvidenceOfDeadlock:
    """★★ 主线程停在 futex 上**不是**死锁证据（2026-11-13 部署后实测）。

    ## 事故

    部署完阶段 1-3 后做生产验收，探针给出这样一份自相矛盾的结果：

        verdict=healthy  unit=True  threads=2  queue=0
        说明: 进程在、ops 端点可达
        告警: ['阻塞在 futex_wait_queue · 阻塞在锁上（若持续如此，疑似死锁）']

    一边说健康，一边说疑似死锁 —— 而实际完全健康。

    ## 真相

    逐线程实测：

        tid=1080678  (main)  wchan=futex_wait_queue   ← 永久停在这里
        tid=1080692          wchan=do_sys_poll        ← 真正在收连接

    这个进程的 accept 循环在**后台线程**，主线程在等退出信号。
    连续 5 次采样（间隔 1 秒）稳定如此，ops 端点每次正常响应。

    **futex 在这个架构里是稳态，不是症状。**

    ## 为什么必须修

    误报比不报更糟：它训练人忽略 warnings。等真死锁来时，
    那句"疑似死锁"已经没人看了 —— 这和 observ 模块
    「单指标阈值告警必然误报」那条是同一个道理。

    判据改成组合信号，见 ``probe._deadlock_suspicion``。
    """

    def _patched(self, monkeypatch, *, threads_wchan, queue, ops_ok,
                 threads=2):
        from nxs.ui import probe as P

        monkeypatch.setattr(P, "_run", lambda cmd: (
            "active" if "is-active" in cmd
            else "1234" if "MainPID" in cmd
            else f"State Recv-Q Send-Q Local Address:Port  Peer Address:Port\n"
                 f"LISTEN {queue} 5 0.0.0.0:18800 0.0.0.0:*"
        ))
        monkeypatch.setattr(P, "_read_text", lambda p: (
            f"State:\tS (sleeping)\nThreads:\t{threads}\nVmRSS:\t 40000 kB"
            if p.name == "status" else "futex_wait_queue"
        ))
        monkeypatch.setattr(P.Path, "is_dir", lambda self: True)
        monkeypatch.setattr(P, "_read_thread_wchans", lambda pid: [
            {"tid": tid, "wchan": w, "wchan_hint": None}
            for tid, w in threads_wchan
        ])
        return P.probe_coordinator(ops_reachable=ops_ok, unit="nexus-mesh",
                                   port=18800)

    @covers("M15.F11")
    def test_healthy_process_with_io_thread_raises_no_alarm(self, monkeypatch):
        """★★★ 主线程 futex + 有 I/O 线程 + 队列空 + ops 通 → **不许告警**。

        这就是生产实测的那个状态。第一版在这里报了「疑似死锁」。
        """
        got = self._patched(
            monkeypatch,
            threads_wchan=[(1080678, "futex_wait_queue"),
                           (1080692, "do_sys_poll")],
            queue=0, ops_ok=True,
        )

        assert got["verdict"] == "healthy"
        assert got["io_thread_present"] is True, "必须识别出 I/O 线程还活着"
        deadlock = [w for w in got["warnings"] if "死锁" in w]
        assert not deadlock, (
            "健康进程被报成死锁 —— 告警必须靠组合信号，不能只看 wchan：\n  "
            + "\n  ".join(got["warnings"])
        )

    @covers("M15.F11")
    def test_deadlock_suspected_when_no_io_thread(self, monkeypatch):
        """★★ 主线程 futex + **没有任何 I/O 线程** → 该报。

        这是组合信号成立的那一半：没人 accept，锁可能真死了。
        """
        got = self._patched(
            monkeypatch,
            threads_wchan=[(1080678, "futex_wait_queue")],
            queue=0, ops_ok=True, threads=1,
        )
        blob = " ".join(got["warnings"])
        assert "死锁" in blob, f"没有 I/O 线程却不怀疑死锁：{got['warnings']}"
        assert "无人 accept" in blob, "要说明为什么可疑"

    @covers("M15.F11")
    def test_deadlock_suspected_on_queue_growth(self, monkeypatch):
        """★★ futex + accept 队列在涨 → 该报（2026-10-31 那次的形态）。"""
        got = self._patched(
            monkeypatch,
            threads_wchan=[(1080678, "futex_wait_queue"),
                           (1080692, "do_sys_poll")],
            queue=6, ops_ok=False,
        )
        blob = " ".join(got["warnings"])
        assert "死锁" in blob, f"队列在涨却不怀疑：{got['warnings']}"
        assert "队列" in blob

    @covers("M15.F11")
    def test_unknown_io_thread_falls_back_to_other_signals(self, monkeypatch):
        """★ 读不到线程级证据时，退回看队列与可达性 —— 不猜。"""
        from nxs.ui import probe as P

        got = self._patched(
            monkeypatch,
            threads_wchan=[], queue=0, ops_ok=True,
        )
        # 空列表 → io_thread_present=False（确实没有 I/O 线程的证据）
        assert got["io_thread_present"] is False
        # 但队列空、ops 通，仍有一条"没有 I/O 线程"的理由 → 该报
        assert any("死锁" in w for w in got["warnings"])

    @covers("M15.F11")
    def test_thread_wchans_are_read_from_proc(self):
        """★ 线程级 wchan 是真实读出来的，不是猜的。

        ⚠ 只读本进程验证「函数能读」这件事。macOS 没有 /proc，跳过。
        """
        import os
        from pathlib import Path as _Path

        from nxs.ui import probe as P

        if not _Path("/proc/self/task").is_dir():
            pytest.skip("没有 /proc（非 Linux）")

        rows = P._read_thread_wchans(os.getpid())
        assert rows, "读不出自己进程的线程 wchan"
        assert all("tid" in r and "wchan" in r for r in rows)
        # 容器/测试环境里 wchan 可能是 0（未阻塞）或具体符号，
        # 不论哪个，只要读到了就算成功
        assert any(isinstance(r["wchan"], str) for r in rows)



# ---------------------------------------------------------------------------
# §2 内存末次成功
# ---------------------------------------------------------------------------

class TestStaleFallback:
    """coordinator 挂掉时，用内存里的末次成功兜底。"""

    def _server(self, **nodes_etc):
        from nxs.ui.server import UiServer

        srv = UiServer("http://127.0.0.1:18800/nxs", b"x" * 32)
        srv._ops = MagicMock()
        return srv

    @covers("M15.F12")
    def test_uses_last_good_when_coordinator_dies(self):
        """★★ 成功过一次之后 coordinator 挂了 → 返回陈旧数据 + 明确标记。"""
        srv = self._server()

        # 第一次：成功
        srv._ops.nodes.return_value = [{"node_id": "a", "state": "alive"}]
        srv._ops.metrics.return_value = {"totals": {"completed": 3}}
        srv._ops.audit.return_value = {"records": [{"outcome": "completed"}],
                                       "truncated": False,
                                       "filtered_heartbeats": 5}
        live = srv.snapshot()
        assert live["stale"] is False
        assert live["nodes"]

        # 第二次：coordinator 挂了
        srv._ops.nodes.side_effect = RuntimeError("timed out")
        stale = srv.snapshot()

        assert stale["stale"] is True, "应标记为陈旧"
        assert stale["nodes"] == [{"node_id": "a", "state": "alive"}], (
            "陈旧态应返回末次成功的节点列表"
        )
        assert stale["metrics"] == {"totals": {"completed": 3}}
        assert stale["audit"] == [{"outcome": "completed"}]
        # ⚠ 必须带时间戳 —— 不带的话人无法判断数据有多旧
        assert stale["stale_since"], "必须带陈旧数据的采集时刻"
        assert stale["stale_age_seconds"] is not None
        assert stale["stale_age_seconds"] >= 0

    @covers("M15.F12")
    def test_no_history_when_never_succeeded(self):
        """★ 面板刚启动就遇上不可达 → `no_history`，不能用空列表冒充。

        ⚠ 这两者含义完全不同：

            空列表 + no_history=False  → "网络里没有节点"（注册表是空的）
            空列表 + no_history=True   → "没读到"（可能有一堆节点）

        混为一谈会让人以为注册表空了，进而去查错方向。
        """
        srv = self._server()
        srv._ops.nodes.side_effect = RuntimeError("timed out")

        snap = srv.snapshot()
        assert snap["no_history"] is True
        assert snap["stale"] is False, "没有历史时不该说成 stale"
        assert snap["nodes"] == []
        assert snap["error"]

    @covers("M15.F12")
    def test_partial_failure_is_not_stale(self):
        """★★ nodes 成功但 metrics 失败 → **不是陈旧**，是部分失败。

        混排的危害：人看到有节点列表+陈旧标记，会以为整份数据都是旧的；
        而实际节点是新的、只有指标缺失。两种情况的处置完全不同 ——
        前者要去看 coordinator，后者只是某个端点坏了。
        """
        srv = self._server()
        srv._ops.nodes.return_value = [{"node_id": "fresh"}]
        srv._ops.metrics.side_effect = RuntimeError("boom")
        srv._ops.audit.return_value = {"records": [], "truncated": False,
                                       "filtered_heartbeats": 0}

        snap = srv.snapshot()
        assert snap["stale"] is False, (
            "nodes 拿到了就不是陈旧态 —— 那是部分失败"
        )
        assert snap["nodes"] == [{"node_id": "fresh"}]
        assert snap["metrics"] is None
        assert "metrics" in (snap["error"] or "")

    @covers("M15.F12")
    def test_last_good_is_isloated_from_returned_snapshot(self):
        """★★ 篡改**返回出去的**快照，不能污染内存里的末次成功。

        ⚠ 这里我第一版测错了对象：我去改「stale 返回的副本」，
        那本来就该隔离，所以即使 `_last_good` 存的是引用，测试也是绿的
        —— 反向验证时暴露（改成存引用，测试没红）。

        真正危险的路径是**反方向**：

            _fill_live 里 out["nodes"] 被存进 _last_good（若存引用）
              → out 返回给调用方
              → 调用方（handler 序列化、中间件、将来的过滤器）改了它
              → _last_good 同步被改
              → 下次兜底给出的是**被改坏的数据**，而且看不出来

        所以断言要打在「改 live 快照」这一侧。
        """
        srv = self._server()
        srv._ops.nodes.return_value = [{"node_id": "a", "deep": {"x": 1}}]
        srv._ops.metrics.return_value = {}
        srv._ops.audit.return_value = {"records": [], "truncated": False,
                                       "filtered_heartbeats": 0}

        live = srv.snapshot()
        # 篡改 live 快照（模拟调用方或后续处理改了它）
        live["nodes"][0]["node_id"] = "TAMPERED"
        live["nodes"][0]["deep"]["x"] = 999

        # 再挂掉 coordinator，看兜底给的是原始值还是被改坏的
        srv._ops.nodes.side_effect = RuntimeError("dead")
        stale = srv.snapshot()

        assert stale["nodes"][0]["node_id"] == "a", (
            "改了 live 快照后，兜底数据也变了 —— `_last_good` 存的是引用。\n"
            "  后果：任何碰到返回值的代码都能悄悄改坏兜底数据，\n"
            "  而下次故障时给出的就是被改坏的旧数据，且看不出来。"
        )
        assert stale["nodes"][0]["deep"]["x"] == 1, (
            "嵌套 dict 没深拷贝 —— 只拷贝了外层 list"
        )

    @covers("M15.F12")
    def test_recovery_clears_stale(self):
        """★ coordinator 恢复后，stale 标记必须消失。

        否则会出现「已经好了但页面还写着陈旧」—— 那会让人不敢信任何数据。
        """
        srv = self._server()
        srv._ops.nodes.return_value = [{"node_id": "a"}]
        srv._ops.metrics.return_value = {}
        srv._ops.audit.return_value = {"records": [], "truncated": False,
                                       "filtered_heartbeats": 0}
        srv.snapshot()

        srv._ops.nodes.side_effect = RuntimeError("dead")
        assert srv.snapshot()["stale"] is True

        # 恢复
        srv._ops.nodes.side_effect = None
        srv._ops.nodes.return_value = [{"node_id": "a"}]
        back = srv.snapshot()
        assert back["stale"] is False, "恢复了却还标陈旧"
        assert back["stale_since"] is None
        assert back["stale_age_seconds"] is None


class TestSnapshotCarriesProbe:
    """探针要一直在快照里 —— 无论 coordinator 通不通。"""

    @covers("M15.F11")
    def test_probe_present_on_success(self):
        """ops 通的时候也要有探针 —— 它能预警（队列在涨、线程数过少）。"""
        from nxs.ui.server import UiServer

        srv = UiServer("http://127.0.0.1:18800/nxs", b"x" * 32)
        srv._ops = MagicMock()
        srv._ops.nodes.return_value = []
        srv._ops.metrics.return_value = {}
        srv._ops.audit.return_value = {"records": [], "truncated": False,
                                       "filtered_heartbeats": 0}

        snap = srv.snapshot()
        assert snap["probe"] is not None
        assert "verdict" in snap["probe"]

    @covers("M15.F11")
    def test_probe_present_on_failure(self):
        """★★ ops 不通时探针是**唯一**的线索，必须存在。"""
        from nxs.ui.server import UiServer

        srv = UiServer("http://127.0.0.1:18800/nxs", b"x" * 32)
        srv._ops = MagicMock()
        srv._ops.nodes.side_effect = RuntimeError("timed out")

        snap = srv.snapshot()
        assert snap["probe"] is not None
        assert "verdict" in snap["probe"], "探针结果里必须有判定"

    @covers("M15.F11")
    def test_port_derived_from_ops_url(self):
        """★ 探针用的端口必须从 ops_url 解析，不能另配。

        两个端口分开配置会造出「探针查 18800、实际连 19000」——
        那时所有数据看起来正常，只是探针永远说端口没在听。
        """
        from nxs.ui.server import UiServer

        a = UiServer("https://127.0.0.1:18800/nxs", b"x" * 32)
        assert a.probe_port == 18800

        b = UiServer("http://127.0.0.1:19000/nxs", b"x" * 32)
        assert b.probe_port == 19000, "端口没跟着 URL 走"

    @covers("M15.F11")
    def test_probe_crash_does_not_break_snapshot(self, monkeypatch):
        """★★ 探针自己崩了，不能让整份快照崩。

        探针是**辅助信息**，不是主体。它挂了还应该有节点列表可看。
        """
        from nxs.ui import server as S

        def boom(**_kw):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(S, "probe_coordinator", boom)

        srv = S.UiServer("http://127.0.0.1:18800/nxs", b"x" * 32)
        srv._ops = MagicMock()
        srv._ops.nodes.return_value = [{"node_id": "a"}]
        srv._ops.metrics.return_value = {}
        srv._ops.audit.return_value = {"records": [], "truncated": False,
                                       "filtered_heartbeats": 0}

        snap = srv.snapshot()
        assert snap["nodes"] == [{"node_id": "a"}], "探针崩了不该影响节点列表"
        assert snap["probe"]["verdict"] == "unknown"
        assert "probe" in (snap["probe"].get("unavailable") or [])
