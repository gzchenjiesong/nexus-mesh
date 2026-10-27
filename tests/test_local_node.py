"""本进程承载的节点不该被判 stale。

⚠ **这一节修的是一个真机上暴露的缺陷。**

实测：协调节点启动 3 分钟后必然显示 ``stale``
（``state=stale，最后心跳 344 秒前，阈值 180 秒``）。

原因不是心跳机制错了，而是**没人给它发心跳** ——
存活判定是"超过 heartbeat_interval × 3 没有心跳 → stale"，
这条规则对远端节点是对的，但协调节点是本进程内的节点，
它自己就是服务端，没有任何东西会来敲它。

后果两个，一个误导一个真实：
  * 观察窗里"协调中枢 ● stale"看着像核心挂了，其实完全正常
  * ``capability_index`` 按 routable 过滤 → 它声明的能力在匹配器
    眼里消失，变成"没有节点能提供 work.summary"

修法：配置里显式声明 ``local_node``，服务进程自己给它续心跳。
**刻意不采用"配置里的节点都是本进程"这种推断** —— 因为
``nodes:`` 段也可以登记远端节点（为了锁定它的信任域），
替那种节点续命等于让一台已经死掉的机器永远显示 alive，
那比 stale 更危险：任务会被投给一个不存在的对端。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from conftest import covers
from nxs.app import AppStores, NexusApp
from nxs.backends.memory import (
    MemoryAuditStore,
    MemoryQueueStore,
    MemoryRegistryStore,
)
from nxs.ports.stores import NodeState


def _now_iso(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat(
        timespec="seconds")


class TestLocalNodeStaysAlive:
    """协调节点必须一直 alive。"""

    @covers("M3.F4")
    def test_local_node_declared_in_templates(self):
        """★ 随仓库发布的模板必须声明 local_node。

        不声明的话，照模板部署的人一启动就会看到"协调中枢 stale" ——
        而那看起来像核心挂了。模板不能教人踩这个坑。
        """
        from conftest import REPO_ROOT

        for rel in ("deploy/local.yaml.example", "config/nxs.example.yaml"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "local_node:" in text, (
                f"{rel} 没有 local_node —— 照它部署会让协调节点必然变 stale"
            )

    @covers("M3.F4")
    def test_local_node_must_exist(self, workdir):
        """写错节点 ID 要在加载时报错，不能静默忽略。

        ⚠ 静默忽略的症状是"过一会儿变 stale"，那看起来完全与配置无关 ——
        这种"配错了但不报错"的问题最难查。
        """
        from nxs.core.errors import ConfigError
        from nxs.mgmt.config import load_config

        from conftest import REPO_ROOT

        src = (REPO_ROOT / "config" / "nxs.example.yaml").read_text(encoding="utf-8")
        broken = src.replace("local_node: coordinator", "local_node: typo-node", 1)
        path = workdir / "bad.yaml"
        path.write_text(broken, encoding="utf-8")

        from nxs.mgmt.config import demo_env_for
        with pytest.raises(ConfigError) as ei:
            load_config(str(path), env=demo_env_for(REPO_ROOT / "config" / "nxs.example.yaml"))
        assert "local_node" in str(ei.value)
        assert "typo-node" in str(ei.value)

    @covers("M3.F4")
    def test_local_node_cannot_be_ops_identity(self, workdir):
        """运维身份不能同时是"本进程承载的节点"。

        两者合一等于让被管理者持有管理钥匙。
        """
        from nxs.core.errors import ConfigError
        from nxs.mgmt.config import OPS_NODE_ID, demo_env_for, load_config
        from conftest import REPO_ROOT

        src = (REPO_ROOT / "config" / "nxs.example.yaml").read_text(encoding="utf-8")
        broken = src.replace("local_node: coordinator", f"local_node: {OPS_NODE_ID}", 1)
        path = workdir / "ops.yaml"
        path.write_text(broken, encoding="utf-8")

        env = demo_env_for(REPO_ROOT / "config" / "nxs.example.yaml")
        with pytest.raises(ConfigError):
            load_config(str(path), env=env)


class TestPulseKeepsItAlive:
    """心跳续命的行为：真的能把它从 stale 拉回 alive。"""

    @staticmethod
    def _app(config):
        # 用项目自己的 AppStores，不另造一份 —— 测试里再造一个
        # "store 组合"会掩盖真实的装配方式。
        return NexusApp(config, AppStores(
            registry=MemoryRegistryStore(),
            queue=MemoryQueueStore(),
            audit=MemoryAuditStore(),
        ))

    @covers("M3.F4")
    def test_start_pulses_local_node(self, config):
        """启动后 local_node 立刻是 alive。"""
        app = self._app(config)
        app.start()
        rec = app.registry.record(config.local_node)
        assert rec is not None
        assert rec.state == NodeState.ALIVE

    @covers("M3.F4")
    def test_sweep_rescues_stale_local_node(self, config):
        """★ 核心断言：被手动推成 stale 之后，一次 sweep 就拉回来。

        这是修复的实质 —— 光有 heartbeat 不够，还得 resurrect
        （heartbeat 只刷 last_seen，不改 state）。
        漏掉 resurrect 的症状是"一直卡在 stale"，比原来更难看。
        """
        app = self._app(config)
        app.start()

        # 手动推成 stale，模拟"很久没心跳了"
        node_id = config.local_node
        rec = app.registry.record(node_id)
        from dataclasses import replace as _replace
        app.registry._store.put(_replace(rec, state=NodeState.STALE))

        assert app.registry.record(node_id).state == NodeState.STALE

        app.sweep()   # 一次维护就该把它救回来

        assert app.registry.record(node_id).state == NodeState.ALIVE, (
            "sweep 没能把本地节点从 stale 拉回 alive —— "
            "heartbeat 只刷 last_seen，从 stale 回来还要 resurrect"
        )

    @covers("M3.F4")
    def test_sweep_marks_genuinely_dead_remote_node(self, config):
        """★ 反向断言：真的死了的**远端**节点必须照样判 stale。

        修复不能变成"什么都不会 stale 了"—— 那会让死掉的远端节点
        永远显示 alive，任务被投给一个不存在的对端。
        """
        app = self._app(config)
        app.start()

        # 找一个不是 local_node 的节点
        others = [n for n in config.nodes if n != config.local_node]
        if not others:
            pytest.skip("示例配置只有本地节点，无法验证远端节点被正确判 stale")
        remote = others[0]

        # 把它的 last_seen 推到很远的过去
        rec = app.registry.record(remote)
        from dataclasses import replace as _replace
        stale_at = _now_iso(-(rec.card.heartbeat_interval * 10))
        app.registry._store.put(_replace(rec, last_seen=stale_at))

        marked = app.registry.sweep()

        assert remote in marked, "远端节点心跳过期却没被判 stale"
        assert app.registry.record(remote).state == NodeState.STALE

    @covers("M3.F4")
    def test_pulse_is_noop_when_local_node_empty(self, config):
        """``local_node`` 留空时不该报错，也不该给任何节点续命。

        那种部署里本进程不承载节点，sweep 应照常工作。
        """
        cfg = replace(config, local_node="")
        app = self._app(cfg)
        app.start()
        # 不抛异常即可；sweep 也应正常返回
        result = app.sweep()
        assert "marked_stale" in result

    @covers("M3.F4")
    def test_capability_index_includes_local_node(self, config):
        """★ 本地节点声明的能力必须出现在能力索引里。

        这是 stale 造成的**真实功能损失**，不只是显示问题：
        ``capability_index`` 按 routable 过滤，stale 的节点会被跳过，
        于是它声明的能力在匹配器眼里消失 —— "没有节点能提供 work.summary"。
        """
        app = self._app(config)
        app.start()

        # 推到 stale 再 sweep，验证能力索引仍然包含它
        node_id = config.local_node
        rec = app.registry.record(node_id)
        from dataclasses import replace as _replace
        app.registry._store.put(_replace(rec, state=NodeState.STALE))
        app.sweep()

        index = app.registry.capability_index()
        local_caps = [c.type for c in config.nodes[node_id].capabilities]
        for cap_type in local_caps:
            assert node_id in index.get(cap_type, ()), (
                f"能力 {cap_type} 的提供者里没有本地节点 {node_id} —— "
                f"stale 会让它从匹配器眼里消失"
            )


class TestVerifyWarnsWhenLocalNodeMissing:
    """★ 没配 local_node 时，verify 必须**如实**说"留空"。

    ⚠ 初版这里有个自相矛盾的提示：
    「✓ local_node 已声明（本进程承载的节点）」
    后面跟着「留空 = 本进程不承载节点」。

    一个打勾的"已声明"，配着"留空"的说明 —— 读起来像配好了，
    实际什么都没配。**这比不提示更糟**：它会让人以为已经修好了，
    然后困惑于"为什么还是 stale"。

    现在标签如实写"留空"，状态是 warn 而不是 ok。
    """

    @covers("M14.F8")
    def test_empty_local_node_is_warn_not_ok(self, workdir):
        import os
        import subprocess
        import sys

        from conftest import REPO_ROOT
        from nxs.mgmt.config import demo_env_for

        # 造一份没有 local_node 的配置
        src = (REPO_ROOT / "config" / "nxs.example.yaml").read_text(encoding="utf-8")
        stripped = src.replace("local_node: coordinator\n", "", 1)
        assert "local_node" not in stripped
        path = workdir / "nolocal.yaml"
        path.write_text(stripped, encoding="utf-8")

        env = dict(os.environ)
        env.update(demo_env_for(REPO_ROOT / "config" / "nxs.example.yaml"))
        r = subprocess.run(
            [sys.executable, "-m", "nxs", "verify", "--config", str(path)],
            cwd=REPO_ROOT, capture_output=True, text=True, env=env, timeout=180,
        )
        out = r.stdout + r.stderr
        assert "local_node 留空" in out, (
            "没配 local_node 时应如实说「留空」，不能说「已声明」"
        )
        assert "local_node 已声明" not in out, (
            "值为空却报「已声明」—— 自相矛盾，会让人以为配好了"
        )
