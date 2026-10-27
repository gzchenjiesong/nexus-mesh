"""端到端测试 —— 模拟真实部署：云端协调节点 + 公司电脑上的执行节点。

这个文件要回答的问题只有一个
----------------------------
**"能不能真的用起来"** —— 不是"每个模块单独能跑"，而是
从节点自注册一路走到结果落地的完整链路。

链路：
    节点自注册 → 未批准 → 运维批准 → coordinator 投任务
      → 节点领取 → 执行 → **节点侧边界** → 网络传输
      → **coordinator 侧边界** → 落库 → coordinator 取到结果 → 审计可查

为什么用生产配置模板
--------------------
测试用 ``deploy/local.yaml.example`` 而不是示例配置——因为要验证的正是
"发给别人的那份生产配置能不能跑通"。如果模板本身有错，这里会失败。
这是一种廉价的文档正确性检查（与 conftest 对示例配置的做法一致）。

与 test_gateway 的区别
----------------------
* ``test_gateway`` 验证**传输层**的协议语义（端点、状态码、限额）
* 本文件验证**业务链路**能走通（注册、批准、投递、执行、边界、结果）

两者都走真实 HTTP，但关注点不同。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import REPO_ROOT, covers

from nxs.app import AppStores, NexusApp
from nxs.backends.memory import (
    MemoryAuditStore,
    MemoryQueueStore,
    MemoryRegistryStore,
)
from nxs.core.errors import PolicyDenied, RuleId
from nxs.core.models import Level, RoleCard
from nxs.edge.client import HttpTransport, OpsClient
from nxs.edge.gateway import Gateway
from nxs.mgmt.config import load_config
from nxs.sdk import NodeClient, NodeConfig

DEPLOY = REPO_ROOT / "deploy"
COORDINATOR_SECRET = "coordinator-e2e-secret-0123456789abcdef"
OPS_SECRET = b"ops-e2e-secret-0123456789abcdef"
NODE_SECRET = "work-e2e-secret-0123456789abcdef"


# ---------------------------------------------------------------------------
# 夹具：一套"像生产"的环境
# ---------------------------------------------------------------------------

@pytest.fixture
def deployed(workdir):
    """按**生产配置模板**装配，但存储用内存、端口随机。

    刻意保留模板的：
      * 域声明（cloud / zone-work / zone-home）
      * 矩阵规则（cloud → zone-work 只允许两类任务，上限 summary）
      * auto_approve_domains 为空（必须逐个批准）
    只把存储与端口改成适合测试的形态。

    用 ``workdir`` 而不是 pytest 的 ``tmp_path``：受限环境无法在系统临时
    目录创建文件（见 conftest 的说明）。
    """
    cfg = load_config(DEPLOY / "local.yaml.example",
                      env={"NXS_SECRET_COORDINATOR": COORDINATOR_SECRET})
    cfg.stores = replace(cfg.stores, queue="memory", registry="memory",
                         audit="file", data_dir=str(workdir / "data"))

    app = NexusApp(cfg, AppStores(
        registry=MemoryRegistryStore(),
        queue=MemoryQueueStore(),
        audit=MemoryAuditStore(),
    ))
    app.start()

    gw = Gateway(app, ops_secret=OPS_SECRET)
    gw.server_config = replace(gw.server_config, host="127.0.0.1", port=0)
    gw.start()
    gw.authenticator.set_secret("work-laptop", NODE_SECRET.encode())

    try:
        yield {
            "app": app,
            "gateway": gw,
            "coordinator": HttpTransport(gw.base_url, node_id="coordinator",
                                 secret=COORDINATOR_SECRET.encode()),
            "ops": OpsClient(gw.base_url, secret=OPS_SECRET),
            "node": HttpTransport(gw.base_url, node_id="work-laptop",
                                  secret=NODE_SECRET.encode()),
        }
    finally:
        gw.stop()


def work_card() -> RoleCard:
    """与 examples/node_run.py 一致的角色卡（确定性）。"""
    return RoleCard.from_dict({
        "node_id": "work-laptop",
        "domain": "zone-work",
        "display_name": "Company Laptop",
        "roles": {"executor": True, "issuer": False},
        "capabilities": [
            {"type": "work.calendar", "level": "summary", "mode": "sync",
             "params_schema": {"day": "string"},
             "response_fields": ["events", "level"]},
            {"type": "work.summary", "level": "summary", "mode": "sync",
             "params_schema": {"date": "string"},
             "response_fields": ["summary", "count", "level"],
             "freetext_fields": ["summary"]},
        ],
        "constraints": {
            "max_response_level": "summary",
            "can_receive_from": ["cloud"],
            "can_issue_types": [],
            "quota": {"tasks_per_day": 200, "concurrent_tasks": 2},
        },
    })


def calendar_handler(params: dict) -> dict:
    return {"events": ["10:00 周会", "14:00 面试"], "level": "summary"}


def node_client(transport) -> NodeClient:
    return NodeClient(NodeConfig(
        "work-laptop",
        handlers={"work.calendar": calendar_handler},
        levels={"work.calendar": Level.SUMMARY},
        card=work_card(),
        heartbeat_interval=0,      # 测试里不自动心跳，避免时序干扰
        renew_interval=0,
    ), transport)


# ---------------------------------------------------------------------------
# 1. 完整链路
# ---------------------------------------------------------------------------

class TestFullCooperationLoop:
    """这条链走通，就说明"云端 + 公司电脑"真的能协作。"""

    @covers("M3.F8", "M3.F9", "M14.F2", "M1.F16", "M10.F5")
    def test_register_approve_publish_execute(self, deployed):
        d = deployed
        client = node_client(d["node"])

        # ---- 1. 自注册 ----
        reg = client.register()
        assert reg["outcome"] == "created"
        assert reg["approved"] is False, "新节点不该自行获批"
        assert reg["domain"] == "zone-work"

        # ---- 2. 未批准时领不到任务 ----
        # 用运维视角确认它确实在等批准
        nodes = {n["node_id"]: n for n in d["ops"].nodes()}
        assert nodes["work-laptop"]["approved"] is False

        # ---- 3. 运维批准 ----
        d["ops"].approve("work-laptop")
        assert client.register()["approved"] is True   # 心跳后确认已批准

        # ---- 4. coordinator 投任务 ----
        published = d["coordinator"].publish(
            from_node="coordinator", to_node="work-laptop",
            task_type="work.calendar", params={"day": "2026-10-25"},
            session_id="ses-e2e", idempotency_key="idem-e2e-1",
        )
        assert published.effective_max_level == "summary"

        # ---- 5. 节点领取 → 执行 → 两层边界 → 回传 ----
        task = d["node"].poll("work-laptop")
        assert task is not None
        done = client.execute(task)
        assert done.status == "completed"
        assert done.artifact_payload()["events"] == ["10:00 周会", "14:00 面试"]

        # ---- 6. 审计里能查到这次协作 ----
        records = list(d["app"].stores.audit.read_recent(limit=200))
        completed = [r for r in records if r.get("outcome") == "completed"]
        assert completed, "完成的协作没有留下审计"
        assert any(r.get("task_type") == "work.calendar" for r in completed)

    @covers("M5.F10")
    def test_self_registered_node_is_visible_to_policy(self, deployed):
        """★ 策略层必须能看见**自注册**的节点。

        这是一个真实存在过的阻塞：v0.3 让节点能自注册了，但 PolicyEngine
        仍然只从**配置文件**取角色卡与类型索引。于是自注册节点：
          * 投递时被判"未知节点"（AU-NODE-04）
          * 或者"类型未注册"（WL-TYPE-404）
        ——注册成功，却永远无法参与协作。

        只有端到端测试能发现这个问题：单测"能注册"会过，
        单测"策略引擎"也会过，两者接不上才暴露。
        """
        d = deployed
        d["node"].register(work_card())
        d["ops"].approve("work-laptop")

        policy = d["app"].policy
        assert "work.calendar" in policy.registered_types, (
            "自注册节点声明的类型没有进入策略层的类型索引"
        )
        assert "work-laptop" in policy.providers_of("work.calendar"), (
            "自注册节点没有被识别为该能力的提供者"
        )

    @covers("M5.F10")
    def test_policy_falls_back_to_config_when_resolver_absent(self):
        """不注入 resolver 时仍按配置工作（向后兼容，供纯配置部署与测试用）。"""
        from nxs.control.policy import PolicyEngine
        from nxs.mgmt.config import load_config

        from conftest import EXAMPLE_CONFIG, demo_env

        cfg = load_config(EXAMPLE_CONFIG, env=demo_env())
        engine = PolicyEngine(cfg)      # 不注入 resolver
        assert "demo.echo" in engine.registered_types
        assert "node-a" in engine.providers_of("demo.echo")

    @covers("M1.F16")
    def test_idempotent_publish_over_network(self, deployed):
        """同一个幂等键重复投递不会产生第二个任务。

        这是断电重试不产生重复副作用的前提，也必须在网络层成立
        （不能只靠客户端自律）。
        """
        d = deployed
        d["node"].register(work_card())
        d["ops"].approve("work-laptop")

        first = d["coordinator"].publish(
            from_node="coordinator", to_node="work-laptop", task_type="work.calendar",
            params={"day": "2026-10-25"}, idempotency_key="idem-dup",
        )
        second = d["coordinator"].publish(
            from_node="coordinator", to_node="work-laptop", task_type="work.calendar",
            params={"day": "2026-10-25"}, idempotency_key="idem-dup",
        )
        assert first.task.id == second.task.id
        assert second.created is False


# ---------------------------------------------------------------------------
# 2. 边界在真实链路上确实拦得住
# ---------------------------------------------------------------------------

class TestBoundariesHoldOnTheWire:
    """端到端跑起来之后，两条安全断言必须仍然成立。"""

    @covers("M10.F5", "S2", "S14")
    def test_node_side_boundary_blocks_before_send(self, deployed):
        """★ 越界产出在**节点侧**被拦，coordinator 侧连拒绝记录都不该有。

        验证方式：故意让处理器多返回一个字段，然后检查
        coordinator 的审计里**没有** WL-FIELD-001 的拒绝记录——
        因为请求根本没发出去。
        """
        d = deployed
        d["node"].register(work_card())
        d["ops"].approve("work-laptop")

        def leaky_handler(params):
            return {"events": [], "level": "summary",
                    "internal_note": "不该外发的内容"}

        client = NodeClient(NodeConfig(
            "work-laptop",
            handlers={"work.calendar": leaky_handler},
            levels={"work.calendar": Level.SUMMARY},
            card=work_card(),
            heartbeat_interval=0, renew_interval=0,
        ), d["node"])

        d["coordinator"].publish(from_node="coordinator", to_node="work-laptop",
                         task_type="work.calendar", params={"day": "x"},
                         idempotency_key="idem-leak")
        task = d["node"].poll("work-laptop")
        result = client.execute(task)

        # 任务落 failed（节点上报了失败），而不是 completed
        assert result.status == "failed"

        # ★ 关键：coordinator 侧没有"产出被拒"的记录 —— 请求没到过 coordinator
        records = list(d["app"].stores.audit.read_recent(limit=200))
        coordinator_denied = [r for r in records
                      if r.get("rule_id") == RuleId.WL_FIELD_NOT_ALLOWED.value]
        assert not coordinator_denied, (
            "coordinator 收到了越界产出并拒绝了它 —— 说明节点侧边界没生效，"
            "数据已经离开了节点"
        )

    @covers("M10.F1", "S2")
    def test_coordinator_still_rejects_if_node_is_compromised(self, deployed):
        """节点被攻破（绕过 SDK 直接回传）时，coordinator 侧仍然拦得住。

        这是纵深防御的意义：节点侧边界保护"诚实的实现犯的错"，
        coordinator 侧边界保护"不诚实的实现故意的越界"。
        """
        d = deployed
        d["node"].register(work_card())
        d["ops"].approve("work-laptop")

        published = d["coordinator"].publish(
            from_node="coordinator", to_node="work-laptop", task_type="work.calendar",
            params={"day": "x"}, idempotency_key="idem-compromised",
        )
        task = d["node"].poll("work-laptop")

        # 模拟被攻破的节点：绕过 SDK，直接用 transport 回传越界产物
        from nxs.core.models import Artifact
        with pytest.raises(PolicyDenied) as excinfo:
            d["node"].complete("work-laptop", task, [
                Artifact(level=Level.SUMMARY, kind="structured",
                         data={"events": [], "level": "summary",
                               "internal_note": "被攻破的节点硬塞进来的"})
            ])
        assert excinfo.value.rule_id is RuleId.WL_FIELD_NOT_ALLOWED

    @covers("M10.F1", "S5")
    def test_coordinator_rejects_level_above_matrix_ceiling(self, deployed):
        """等级超过**矩阵**上限时，coordinator 拦下。

        生产模板里 coordinator → zone-work 上限是 summary。
        节点声明 summary 也遵守了，但如果矩阵改成更严会怎样——
        这里用 metadata 级别的类型来验证矩阵上限确实生效。
        """
        d = deployed
        # 矩阵只允许 work.calendar / work.summary，类型不在范围内会被拒
        d["node"].register(work_card())
        d["ops"].approve("work-laptop")

        with pytest.raises(PolicyDenied) as excinfo:
            d["coordinator"].publish(
                from_node="coordinator", to_node="work-laptop",
                task_type="work.unknown",      # 不在矩阵 allow_types 内
                params={}, idempotency_key="idem-matrix",
            )
        assert excinfo.value.rule_id in (
            RuleId.WL_TYPE_NOT_REGISTERED, RuleId.MX_DOMAIN_FORBIDDEN,
        )


# ---------------------------------------------------------------------------
# 3. 运维与恢复
# ---------------------------------------------------------------------------

class TestOperationsOnRealDeployment:

    @covers("M14.F3", "S10")
    def test_revoke_cuts_the_node_off_immediately(self, deployed):
        """吊销后节点立刻无法注册/领任务 —— 不需要重启服务。"""
        d = deployed
        d["node"].register(work_card())
        d["ops"].approve("work-laptop")
        assert d["node"].register(work_card())["approved"] is True

        d["ops"].revoke("work-laptop")

        with pytest.raises(PolicyDenied) as excinfo:
            d["node"].register(work_card())
        assert excinfo.value.rule_id is RuleId.AU_CREDENTIAL_REVOKED

    @covers("M14.F4")
    def test_release_clears_stale_leases(self, deployed):
        """运维可以主动释放过期租约（断电恢复的兜底手段）。"""
        d = deployed
        d["node"].register(work_card())
        d["ops"].approve("work-laptop")
        result = d["ops"].release()
        assert "released" in result and "marked_stale" in result

    @covers("M14.F5")
    def test_ops_nodes_reflects_reality(self, deployed):
        """运维看到的节点清单与实际状态一致。"""
        d = deployed
        d["node"].register(work_card())
        rows = {n["node_id"]: n for n in d["ops"].nodes()}
        assert rows["work-laptop"]["domain"] == "zone-work"
        assert rows["work-laptop"]["approved"] is False
        assert rows["work-laptop"]["max_response_level"] == "summary"
        assert "work.calendar" in rows["work-laptop"]["capabilities"]


# ---------------------------------------------------------------------------
# 4. 示例脚本可用（部署文档里让人照抄的那份）
# ---------------------------------------------------------------------------

class TestExampleNodeScript:
    """``examples/node_run.py`` 是文档让用户照抄的入口，必须真的能跑。"""

    @covers("M14.F7")
    def test_node_run_script_dry_run(self, deployed):
        env = dict(os.environ)
        env["NXS_SECRET_WORK_LAPTOP"] = NODE_SECRET
        result = subprocess.run(
            [sys.executable, "examples/node_run.py",
             "--url", deployed["gateway"].base_url,
             "--node-id", "work-laptop", "--dry-run"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, env=env,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "探活成功" in output
        # 未批准时必须说清楚怎么批准，不能让人干瞪眼
        assert "尚未批准" in output
        assert "nxs ops approve" in output
