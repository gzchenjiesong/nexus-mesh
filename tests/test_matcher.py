"""匹配器（M4）的测试 —— 重点不是"能匹配"，而是"每次匹配到同一个"。

为什么这个文件的重心在确定性
----------------------------
匹配器本身很简单（筛 + 排）。它真正难的地方在于：**结果必须可复现**。

分布式系统里"昨天能跑今天不能"是最难查的一类问题。如果匹配器带任何
随机性（随机挑一个、按 dict 插入顺序、按线程完成顺序），这类问题就变成
不可复现的——你甚至无法确认改了一个东西之后是否真的修好了。

所以本文件用大量"同样输入跑多次、断言结果完全相同"的用例钉住它。

⭐ 另一个重点：不泄漏拓扑
-------------------------
"能执行 work.calendar 的节点有 3 个"这句话本身是信息。所以：
* 候选集合只在运维视角可见，不经 HTTP 暴露
* 匹配失败对外统一报 WL-TYPE-404，不区分"没人有这能力"和"有人但不给你"
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from conftest import covers

from nxs.control.matcher import Candidate, Matcher, Requirement
from nxs.core.errors import PolicyDenied, RuleId
from nxs.core.models import Level, RoleCard
from nxs.mgmt.config import load_config


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def card(node_id: str, *, domain: str = "zone-a", task_type: str = "job.run",
         level: str = "summary", max_level: str = "full_text",
         capacity: int = 1) -> RoleCard:
    """造一张最小角色卡。

    ``level``（能力声明的等级）与 ``max_level``（节点自限）分开给，
    因为匹配器要取两者的**较小值** —— 这是最容易写错的地方之一。

    ⚠ 两处参数变更：
      · ``concurrent`` → ``capacity``（Step 5b，配额删除，改为排程提示）
      · ``max_level`` 现在写进 ``self_limits``，不再是
        ``constraints.max_response_level``（Step 5c，constraints 已删）
      · ``can_receive_from`` 已删除 —— "能不能到达"由域矩阵判，
        匹配器不再做这项预筛
    """
    return RoleCard.from_dict({
        "node_id": node_id,
        "domain": domain,
        "capacity": capacity,
        "role": "worker",
        "self_limits": {"max_response_level": max_level},
        "capabilities": [{
            "type": task_type, "level": level, "mode": "sync",
            "response_fields": ["result", "level"],
        }],
    })


@pytest.fixture
def make_matcher(workdir):
    """构造一个隔离的 (registry, queue, matcher) 三件套。

    :param unapproved: 这些 node_id 注册后**不批准**（默认全部批准）。
        需要逐节点控制，不能用一个全局开关——否则没法测"有的批准、
        有的没批准"这种真实混布场景。
    """
    from nxs.backends.memory import MemoryQueueStore, MemoryRegistryStore
    from nxs.control.registry import Registry

    def _make(*cards: RoleCard, unapproved: tuple[str, ...] = (),
              working: dict[str, int] | None = None) -> dict:
        registry = Registry(MemoryRegistryStore())
        queue = MemoryQueueStore()
        for c in cards:
            registry.register(c, approved=c.node_id not in unapproved)
        # 造负载：直接塞 working 状态的任务
        for node_id, count in (working or {}).items():
            for i in range(count):
                from nxs.core.models import Task, TaskStatus
                queue.enqueue(Task(
                    id=f"t-{node_id}-{i}", idempotency_key=f"k-{node_id}-{i}",
                    type="job.run", session_id="s", from_node="coordinator",
                    to_node=node_id, status=TaskStatus.WORKING,
                ))
        return {
            "registry": registry,
            "queue": queue,
            "matcher": Matcher(registry, queue)
        }

    return _make


REQ = Requirement(task_type="job.run")


# ---------------------------------------------------------------------------
# 1. 筛选
# ---------------------------------------------------------------------------

class TestFiltering:

    @covers("M4.F1")
    def test_only_nodes_declaring_the_capability(self, make_matcher):
        s = make_matcher(
            card("has-it"),
            card("no-it", task_type="other.job"),
        )
        ids = [c.node_id for c in s["matcher"].candidates(REQ)]
        assert ids == ["has-it"]

    @covers("M4.F1", "M4.F3", "S9")
    def test_unapproved_and_revoked_are_excluded(self, make_matcher):
        """★ 未批准 / 已吊销的节点不能出现在候选里。

        "我能不能干"的第一层前提是"我在不在"。让一个未批准节点进入候选，
        后面即使策略拦住，也已经把"网络里有这么个节点"暴露给了排序环节。
        """
        s = make_matcher(card("ok"), card("pending"), unapproved=("pending",))
        assert [c.node_id for c in s["matcher"].candidates(REQ)] == ["ok"]

        # 吊销后同样应消失
        s["registry"].register(card("revoked-node"), approved=True)
        assert "revoked-node" in [c.node_id for c in s["matcher"].candidates(REQ)]
        s["registry"].revoke("revoked-node")
        assert "revoked-node" not in [c.node_id for c in s["matcher"].candidates(REQ)]

    @covers("M5.F4")
    def test_provided_level_is_min_of_selflimit_and_capability(self, make_matcher):
        """★ 节点能提供的等级 = min(节点自限, 能力声明)。

        ⚠ 这条替换了 `test_provided_level_is_min_of_card_and_capability`。

        原来取 min(卡片上限, 能力等级)，其中"卡片上限"是
        `constraints.max_response_level` —— 那个字段已删（Step 5c），
        节点侧只剩 `self_limits.max_response_level`。

        **保护的性质没变**：两个来源取小，任一方收紧都生效。
        角色上限不在这里算 —— 那是 check_delivery 的事，
        匹配器只做"这个节点能给出多细"的粗筛。
        """
        s = make_matcher(
            card("tight", task_type="job.run", level="summary", max_level="metadata"),
            card("loose", task_type="job.run", level="summary", max_level="fragment"),
        )
        cands = {c.node_id: c for c in s["matcher"].candidates(REQ)}
        # 能力声明是 summary：tight 的自限（metadata）更紧 → 取 metadata
        #                    loose 的自限（fragment）更松 → 取能力的 summary
        assert cands["tight"].level is Level.METADATA, "自限更紧，取自限"
        assert cands["loose"].level is Level.SUMMARY, "能力更紧，取能力"


    @covers("M4.F1")
    def test_min_level_filters_insufficient_nodes(self, make_matcher):
        s = make_matcher(
            card("rich", level="fragment"),
            card("poor", level="metadata"),
        )
        rich = [c.node_id for c in s["matcher"].candidates(
            Requirement(task_type="job.run", min_level=Level.SUMMARY))]
        assert rich == ["rich"], "等级不足的节点不该出现在候选里"


class TestDeterministicOrdering:

    @covers("M4.F2", "M4.R1")
    def test_same_input_same_output_many_times(self, make_matcher):
        """★ 同样的输入跑 20 次，结果必须逐字相同。

        这是"确定性"最直接的表达。任何一处引入了字典序、集合序、
        或者"当前时间"作为排序依据，这条都会红。
        """
        s = make_matcher(*[card(f"n{i}", capacity=2) for i in range(6)])
        first = [c.node_id for c in s["matcher"].candidates(REQ)]
        for _ in range(20):
            assert [c.node_id for c in s["matcher"].candidates(REQ)] == first

    @covers("M4.F2")
    def test_same_domain_first(self, make_matcher):
        """① 同域优先。跨域数据流动是成本，能力相当时没理由舍近求远。"""
        s = make_matcher(
            card("far", domain="zone-b"),
            card("near", domain="zone-a"),
        )
        req = Requirement(task_type="job.run", from_domain="zone-a")
        assert [c.node_id for c in s["matcher"].candidates(req)] == ["near", "far"]

        # 换来源域，顺序应当翻转 —— 证明这不是写死的，而是真的在算
        req2 = Requirement(task_type="job.run", from_domain="zone-b")
        assert [c.node_id for c in s["matcher"].candidates(req2)] == ["far", "near"]

    @covers("M4.F2")
    def test_least_sufficient_level_first(self, make_matcher):
        """② 等级"刚好够用"的优先。

        都满足需求时，选**承诺更少**的节点：它能给的上限更低，
        意味着被攻破时能泄漏的东西更少。这是最小权限原则的延伸。
        """
        s = make_matcher(
            card("can-give-all", level="full_text"),
            card("gives-just-enough", level="summary"),
            card("gives-least", level="metadata"),
        )
        req = Requirement(task_type="job.run", min_level=Level.SUMMARY)
        ids = [c.node_id for c in s["matcher"].candidates(req)]
        # summary 刚好够用 → 排第一；metadata 不够 → 不在候选里
        assert ids == ["gives-just-enough", "can-give-all"]

    @covers("M4.F2")
    def test_lower_load_ratio_first(self, make_matcher):
        """③ 负载率低的优先。用比率而非绝对数。"""
        s = make_matcher(
            # busy: 1/1 满了；idle: 0/1 空闲
            card("busy", capacity=1), card("idle", capacity=1),
            working={"busy": 1},
        )
        assert [c.node_id for c in s["matcher"].candidates(REQ)] == ["idle", "busy"]

    @covers("M4.F2")
    def test_load_ratio_not_absolute_count(self, make_matcher):
        """★ 用比率而不是绝对数，避免冤枉高并发的大节点。

        big 有 3 个在跑（上限 8，负载率 3/8），small 有 1 个在跑（上限 1，满了）。
        按绝对数会选 small，按比率才是 big —— 后者才反映"谁真的还有余力"。
        """
        s = make_matcher(
            card("big", capacity=8), card("small", capacity=1),
            working={"big": 3, "small": 1},
        )
        ratios = {(c.node_id): c.load_ratio for c in s["matcher"].candidates(REQ)}
        assert ratios["big"] < ratios["small"]
        assert [c.node_id for c in s["matcher"].candidates(REQ)] == ["big", "small"]

    @covers("M4.F2")
    def test_node_id_breaks_ties(self, make_matcher):
        """④ 四级全相同时，节点 ID 字典序兜底。**这一级只为消灭不确定性。**"""
        s = make_matcher(card("zeta"), card("alpha"), card("mike"))
        assert [c.node_id for c in s["matcher"].candidates(REQ)] == [
            "alpha", "mike", "zeta",
        ]

    @covers("M4.F2")
    def test_ordering_is_independent_of_registration_order(self, make_matcher):
        """注册顺序不同，排序结果必须相同。

        这条能抓住"用了 dict/set 插入顺序"这类隐蔽的非确定性——
        那种实现单次跑看起来是对的，换个注册顺序就变了。
        """
        forward = make_matcher(card("a"), card("b"), card("c"))
        backward = make_matcher(card("c"), card("b"), card("a"))
        assert ([c.node_id for c in forward["matcher"].candidates(REQ)]
                == [c.node_id for c in backward["matcher"].candidates(REQ)])

    @covers("M4.F2")
    def test_sort_key_reveals_why(self):
        """排序依据必须可解释 —— "为什么投给了它"要能回答。"""
        win = Candidate("a", "d", Level.METADATA, 0, 4, True)
        lose = Candidate("b", "d", Level.FULL_TEXT, 5, 8, False)
        assert win.sort_key() < lose.sort_key()


# ---------------------------------------------------------------------------
# 3. 依次尝试（匹配 + 策略）
# ---------------------------------------------------------------------------

class TestSelect:

    @covers("M4.F4")
    def test_returns_first_accepted(self, make_matcher):
        s = make_matcher(card("a"), card("b"))
        seen: list[str] = []

        def accept(cand):
            seen.append(cand.node_id)
            return True

        chosen = s["matcher"].select(REQ, accept)
        assert chosen is not None and chosen.node_id == "a"
        assert seen == ["a"], "第一个就通过了，不该继续试"

    @covers("M4.F4")
    def test_skips_denied_and_takes_next(self, make_matcher):
        """第一个被策略拒绝 → 试下一个。这是"依次尝试"的意义。"""
        s = make_matcher(card("a"), card("b"), card("c"))
        seen: list[str] = []

        def accept(cand):
            seen.append(cand.node_id)
            return cand.node_id == "b"

        chosen = s["matcher"].select(REQ, accept)
        assert chosen is not None and chosen.node_id == "b"
        assert seen == ["a", "b"], "b 通过后就该停，不该再试 c"

    @covers("M4.F4")
    def test_returns_none_when_all_denied(self, make_matcher):
        s = make_matcher(card("a"), card("b"))
        assert s["matcher"].select(REQ, lambda c: False) is None

    @covers("M4.F4")
    def test_returns_none_when_no_candidates(self, make_matcher):
        s = make_matcher()
        assert s["matcher"].select(REQ, lambda c: True) is None

    @covers("M4.R2")
    def test_matcher_does_not_make_policy_judgements(self):
        """★ 匹配器**不做策略判定**，只接一个"这个候选行不行"的回调。

        这条约束保证了"加一条矩阵规则不会改变候选集合"——两个变化源被隔开。

        判据用 AST 扫**实际调用**而不是扫源码文本：第一版扫文本时，
        被我写在 docstring 里的"不依赖 PolicyEngine"这句话本身触发了红线——
        说明按文本判断既会误报也会漏报（改个变量名就绕过了）。
        这里改为检查是否存在 ``check_*`` 方法的调用。
        """
        import ast
        import inspect

        from nxs.control import matcher as mod

        tree = ast.parse(inspect.getsource(mod))
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)

        forbidden = {"check_delivery", "check_publish", "check_backlog",
                     "check_response", "find"}
        # `find` 是 MatrixRule 查找矩阵用的（policy.find），也在禁止之列
        overlap = called & forbidden
        assert not overlap, (
            f"匹配器直接调用了策略判定 {sorted(overlap)} —— "
            f"矩阵规则一改就会影响候选集合"
        )


# ---------------------------------------------------------------------------
# 4. 自动投递与不泄漏拓扑
# ---------------------------------------------------------------------------

@pytest.fixture
def live(workdir):
    """一个真实装配的应用 + 网关 + 若干已批准节点。"""
    from nxs.app import NexusApp
    from nxs.edge.gateway import Gateway
    from nxs.mgmt.config import EXAMPLE_CONFIG_PATH, ServerConfig, demo_env_for

    env = demo_env_for(EXAMPLE_CONFIG_PATH)
    app = NexusApp.from_config_path(str(EXAMPLE_CONFIG_PATH), env=env,
                                   force_memory=True)
    app.start()
    app.approve("node-a")
    app.approve("node-b")
    gw = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0),
                 ops_secret=b"").start()
    try:
        yield {"app": app, "gateway": gw}
    finally:
        gw.stop()


class TestAutoDelivery:

    @covers("M4.F5")
    def test_publish_without_to_node_picks_a_provider(self, live):
        from nxs.edge.client import HttpTransport

        coordinator = HttpTransport(live["gateway"].base_url, node_id="coordinator",
                            secret=live["app"].config.secrets["coordinator"])
        result = coordinator.publish(from_node="coordinator", task_type="demo.echo",
                             params={"text": "x"}, idempotency_key="auto-1")
        assert result.task.to_node in ("node-a", "node-b")

    @covers("M4.F5")
    def test_auto_picks_the_only_provider(self, live):
        """只有一个节点能干的类型，自动模式必须选中它。"""
        from nxs.edge.client import HttpTransport

        coordinator = HttpTransport(live["gateway"].base_url, node_id="coordinator",
                            secret=live["app"].config.secrets["coordinator"])
        # ⚠ params 必须符合 demo.count 的 schema（n: integer）——
        #   空 params 会被 FM-PARAM-02 拒掉（参数校验已接线，Step 6）
        result = coordinator.publish(from_node="coordinator", task_type="demo.count",
                             params={"n": 3}, idempotency_key="auto-2")
        assert result.task.to_node == "node-a"

    @covers("M4.F5", "M4.F2")
    def test_auto_avoids_busy_node(self, live):
        """★ 自动模式会把活派给负载轻的节点。

        node-a 并发上限 1，占满之后应自动改投 node-b。
        """
        from nxs.edge.client import HttpTransport

        app = live["app"]
        coordinator = HttpTransport(live["gateway"].base_url, node_id="coordinator",
                            secret=app.config.secrets["coordinator"])
        node_a = HttpTransport(live["gateway"].base_url, node_id="node-a",
                               secret=app.config.secrets["node-a"])

        first = coordinator.publish(from_node="coordinator", task_type="demo.echo",
                            params={"text": "1"}, idempotency_key="auto-3")
        assert first.task.to_node == "node-a"

        node_a.poll("node-a")      # node-a 现在 working，1/1 满
        second = coordinator.publish(from_node="coordinator", task_type="demo.echo",
                             params={"text": "2"}, idempotency_key="auto-4")
        assert second.task.to_node == "node-b", "忙的节点没有被避开"

    @covers("M4.F6", "S15")
    def test_no_provider_reports_type_not_registered(self, live):
        from nxs.edge.client import HttpTransport

        coordinator = HttpTransport(live["gateway"].base_url, node_id="coordinator",
                            secret=live["app"].config.secrets["coordinator"])
        with pytest.raises(PolicyDenied) as excinfo:
            coordinator.publish(from_node="coordinator", task_type="demo.nonexistent",
                        params={}, idempotency_key="auto-5")
        assert excinfo.value.rule_id is RuleId.WL_TYPE_NOT_REGISTERED

    @covers("M4.F6", "S15")
    def test_all_denied_reports_same_code_as_no_provider(self, live):
        """★ 全部候选都被策略拒绝时，对外错误码与"没人能干"**完全相同**。

        若两者可区分，攻击者就能学到"确实有人能做，只是不给我做"——
        那是权限布局的泄漏。真实原因只进本地审计。

        构造方式刻意用**真实场景**而不是篡改配置：注册一个位于
        ``zone-z`` 的节点提供专属任务类型，但矩阵里没有 ``coordinator → zone-z``。
        于是"有候选、但全部被拒"——正是要测的那条路径。
        """
        from nxs.core.models import RoleCard
        from nxs.edge.client import HttpTransport

        app = live["app"]
        coordinator = HttpTransport(live["gateway"].base_url, node_id="coordinator",
                            secret=app.config.secrets["coordinator"])

        # 一个只在 zone-z 的节点，能干活，但没有 coordinator→zone-z 的矩阵规则
        app.registry.register(RoleCard.from_dict({
            "node_id": "node-z", "domain": "zone-z",
            "role": "worker","capabilities": [{
                "type": "demo.only-z", "level": "metadata", "mode": "sync",
                "response_fields": ["result", "level"]
            }],
            "self_limits": {"max_backlog": 20, "max_response_level": "metadata"}}), approved=True)

        # 前提确认：候选确实存在（否则这条测试测的是另一个分支）
        cands = app.transport.candidates(task_type="demo.only-z", from_node="coordinator")
        assert [c["node_id"] for c in cands] == ["node-z"], (
            "测试前提不成立：候选集为空，测不到「全被拒」这条路径"
        )

        with pytest.raises(PolicyDenied) as excinfo:
            coordinator.publish(from_node="coordinator", task_type="demo.only-z",
                        params={}, idempotency_key="deny-all-1")
        assert excinfo.value.rule_id is RuleId.WL_TYPE_NOT_REGISTERED, (
            "候选全被拒时报了别的码 —— 可用来区分「有人但不给」与「没人」"
        )

    @covers("M4.F7", "S16")
    def test_idempotency_wins_over_matching(self, live):
        """★ 幂等命中先于匹配：重试不会因候选变化而误报失败。

        场景：第一次投递成功（投给 node-a），之后 node-a 被吊销、
        候选全部消失。此时用同一幂等键重试，正确行为是**返回原任务**，
        而不是"类型不可用"——否则调用方会以为失败并做业务补偿，
        那才是真正的重复副作用。
        """
        from nxs.edge.client import HttpTransport

        app = live["app"]
        coordinator = HttpTransport(live["gateway"].base_url, node_id="coordinator",
                            secret=app.config.secrets["coordinator"])

        first = coordinator.publish(from_node="coordinator", task_type="demo.count",
                            params={"n": 3}, idempotency_key="same-key")
        assert first.created is True

        # 让候选彻底消失
        app.registry.revoke("node-a")
        app.registry.revoke("node-b")

        again = coordinator.publish(from_node="coordinator", task_type="demo.count",
                            params={}, idempotency_key="same-key")
        assert again.created is False
        assert again.task.id == first.task.id, "幂等重试返回了不同的任务"

    @covers("M4.F7", "S16")
    def test_idempotency_key_cannot_probe_others_tasks(self, live):
        """★ 幂等键必须归属一致，否则可用来探测他人任务。

        若只按 key 命中就返回，node-b 猜到 node-a 的幂等键
        就能拿到 node-a 任务的 params（返回体里含内容）。
        """
        from nxs.edge.client import HttpTransport

        app = live["app"]
        coordinator = HttpTransport(live["gateway"].base_url, node_id="coordinator",
                            secret=app.config.secrets["coordinator"])
        node_b = HttpTransport(live["gateway"].base_url, node_id="node-b",
                               secret=app.config.secrets["node-b"])

        coordinator.publish(from_node="coordinator", task_type="demo.echo",
                    params={"text": "机密"}, idempotency_key="coordinator-secret-key")

        # node-b 用自己的身份 + 别人的幂等键
        with pytest.raises(PolicyDenied):
            node_b.publish(from_node="node-b", task_type="demo.echo",
                           params={"text": "x"},
                           idempotency_key="coordinator-secret-key")

    @covers("M4.F8", "S15")
    def test_candidates_not_exposed_over_http(self, live):
        """★ 候选查询**不经 HTTP 暴露**。

        "谁能干这个活"是拓扑信息。一旦有了端点，它就成了
        "枚举网络规模与分布"的探测接口。
        """
        import json
        import socket

        from nxs.edge.gateway import PREFIX

        body = json.dumps({"task_type": "demo.echo"}).encode()
        with socket.create_connection(("127.0.0.1", live["gateway"].port),
                                      timeout=5) as sock:
            sock.sendall((
                f"POST {PREFIX}/candidates HTTP/1.0\r\nHost: x\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode() + body)
            sock.shutdown(socket.SHUT_WR)
            raw = sock.recv(65536)
        status = int(raw.split(b"\r\n", 1)[0].split(b" ")[1])
        assert status == 501, f"候选端点意外可用（HTTP {status}）"

    @covers("M4.F8")
    def test_candidates_available_to_operator(self, live):
        """运维视角要看得到（否则排查时无从下手）。"""
        rows = live["app"].transport.candidates(
            task_type="demo.echo", from_node="coordinator")
        assert {r["node_id"] for r in rows} == {"node-a", "node-b"}
        assert all("load" in r and "capacity" in r for r in rows)

    @covers("M4.F8")
    def test_candidates_cli_shows_ordering(self):
        """CLI 必须能列出候选并说明排序依据。

        运维真正会问的问题是"为什么投给了 A 而不是 B"，
        这条命令就是为了让那个问题可以被直接查询。
        """
        import subprocess
        import sys

        from conftest import REPO_ROOT

        result = subprocess.run(
            [sys.executable, "-m", "nxs", "candidates",
             "--config", "config/nxs.example.yaml",
             "--type", "demo.echo", "--from-node", "coordinator"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # 示例配置里节点默认未批准 —— 必须提示"该去批准谁"
        assert "尚未批准" in result.stdout
        assert "node-a" in result.stdout

    @covers("M4.F8")
    def test_candidates_cli_lists_approved_nodes(self, live, workdir):
        """批准之后 CLI 应当列出候选并给出排序依据。"""
        import os
        import subprocess
        import sys
        from pathlib import Path

        from conftest import REPO_ROOT

        # 用一份包含 node-a / node-b 且已批准的配置走 CLI
        config = workdir / "cli.yaml"
        original = (REPO_ROOT / "config" / "nxs.example.yaml").read_text(encoding="utf-8")
        assert "auto_approve_domains: [cloud]" in original, (
            "示例配置的自动批准写法变了，本测试的替换需要跟着改"
        )
        config.write_text(
            original.replace("auto_approve_domains: [cloud]",
                             "auto_approve_domains: [cloud, zone-a, zone-b]"),
            encoding="utf-8",
        )
        env = {**os.environ}
        for name, secret in live["app"].config.secrets.items():
            env["NXS_SECRET_" + name.upper().replace("-", "_")] = secret.decode()

        result = subprocess.run(
            [sys.executable, "-m", "nxs", "candidates",
             "--config", str(config),
             "--type", "demo.echo", "--from-node", "coordinator"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "node-a" in result.stdout and "node-b" in result.stdout
        assert "排序依据" in result.stdout, "没有解释排序规则，运维无法理解结果"

    @covers("M4.F5")
    def test_direct_delivery_still_works(self, live):
        """显式指定 to_node 时完全不经过匹配器（Direct 模式保留）。"""
        from nxs.edge.client import HttpTransport

        coordinator = HttpTransport(live["gateway"].base_url, node_id="coordinator",
                            secret=live["app"].config.secrets["coordinator"])
        result = coordinator.publish(from_node="coordinator", to_node="node-b",
                             task_type="demo.echo", params={"text": "x"},
                             idempotency_key="direct-1")
        assert result.task.to_node == "node-b"
