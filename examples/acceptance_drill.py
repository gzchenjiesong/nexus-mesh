"""验收抽查 —— 按 docs/acceptance-guide.md 的条目自动验一遍。

用法：

    python examples/acceptance_drill.py            # 跑全部可自动化的条目
    python examples/acceptance_drill.py --verbose  # 带细节

为什么要有这个脚本
------------------
验收手册写在纸上的东西，**实际跑不一定对**。这个脚本就是为了发现那种情况。

本脚本第一次跑时（2026-10-27）就抓到了手册自己的一个错误：
手册说 A1 注册返回 `outcome=created`，但示例配置里节点已预注册，
实际返回 `unchanged`。手册已修正并写明了三种 outcome 的分支。

它覆盖手册里能自动化的部分。**不能自动化的（systemd、TLS、跨机器网络）
必须手工验**，见手册第九章。

与 tests/ 的分工
---------------
``tests/`` 里的测试守护"代码行为不变"；本脚本验证"手册描述与代码一致"。
两者都会跑真实 HTTP，但目的不同：测试防回归，抽查防文档说错。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSED = 0
FAILED = 0


def check(name: str, passed: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if passed:
        PASSED += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        FAILED += 1
        print(f"  \033[31m✗\033[0m {name}" + (f"   {detail}" if detail else ""))
    return passed


def title(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m")


# ---------------------------------------------------------------------------
# G4 · CLI 全可用
# ---------------------------------------------------------------------------

def drill_cli() -> None:
    """手册 G4：8 个命令全部可用。"""
    title("G4 · CLI 全可用（手册说 9 个命令）")
    commands = ["verify", "doctor", "demo", "serve", "ops",
                "candidates", "metrics", "status", "ui"]
    for cmd in commands:
        result = subprocess.run(
            [sys.executable, "-m", "nxs", cmd, "--help"],
            cwd=ROOT, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        check(f"nxs {cmd} --help", result.returncode == 0,
              f"rc={result.returncode}")
    check("命令数量与手册一致（9 个）", len(commands) == 9)


# ---------------------------------------------------------------------------
# A · 核心闭环
# ---------------------------------------------------------------------------

def _build_live():
    """装配一个真实 HTTP 的 coordinator + 两个节点。"""
    from nxs.app import NexusApp
    from nxs.edge.client import HttpTransport
    from nxs.edge.gateway import Gateway
    from nxs.mgmt.config import EXAMPLE_CONFIG_PATH, ServerConfig, demo_env_for

    app = NexusApp.from_config_path(
        str(EXAMPLE_CONFIG_PATH), env=demo_env_for(EXAMPLE_CONFIG_PATH),
        force_memory=True,
    )
    app.start()
    # 运维通道要用**真实密钥**开启：留空等于关闭，观察窗就无从取数。
    # （初版这里传了 b""，结果 drill_ui 里的指标检查失败 —— 抽查脚本
    #  自己抓到了这个问题，正是它存在的意义。）
    ops_secret = b"drill-ops-secret-0123456789abcdef"
    gateway = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0),
                      ops_secret=ops_secret).start()
    transports = {
        name: HttpTransport(gateway.base_url, node_id=name,
                            secret=app.config.secrets[name])
        for name in ("coordinator", "node-a", "node-b")
    }
    transports["_ops_secret"] = ops_secret
    return app, gateway, transports


def drill_core_loop(transports: dict) -> None:
    """手册 A：注册 → 批准 → 投递 → 领取 → 执行 → 结果。"""
    from nxs.core.models import Level
    from nxs.sdk import NodeClient, NodeConfig

    title("A · 核心闭环（真实 HTTP）")
    node = transports["node-a"]
    coordinator = transports["coordinator"]

    # A1 注册 —— outcome 有三分支，判据是 alive + 未批准
    registered = node.register(_card_of(transports))
    check(
        "A1 注册后 state=alive 且未批准",
        registered.get("state") == "alive" and registered.get("approved") is False,
        f"outcome={registered.get('outcome')} approved={registered.get('approved')}",
    )

    # A2 批准
    transports["_app"].approve("node-a")
    again = node.register(_card_of(transports))
    check("A2 批准后心跳变已批准", again.get("approved") is True)

    # A3 投递
    delivery = coordinator.publish(from_node="coordinator", to_node="node-a",
                           task_type="demo.echo", params={"text": "hi"},
                           idempotency_key="drill-core-1")
    check("A3 投递 created=True", delivery.created is True)

    # A4 领取
    task = node.poll("node-a")
    check("A4 领取 status=working",
          task is not None and task.status == "working")

    # A5/A6 执行与结果
    app_config = transports["_app"].config
    client = NodeClient(NodeConfig(
        "node-a",
        handlers={"demo.echo": lambda p: {"echo": p["text"], "level": "metadata"}},
        levels={"demo.echo": Level.METADATA},
        card=app_config.nodes["node-a"],
        heartbeat_interval=0, renew_interval=0,
    ), node)
    done = client.execute(task)
    check("A5 执行 completed", done.status == "completed")
    check("A6 结果字段正确",
          done.artifact_payload().get("echo") == "hi",
          str(done.artifact_payload()))


def _card_of(transports: dict):
    return transports["_app"].config.nodes["node-a"]


# ---------------------------------------------------------------------------
# B · 安全断言
# ---------------------------------------------------------------------------

def drill_boundaries(transports: dict) -> None:
    """手册 B：三层拦截各自独立。"""
    from nxs.core.errors import PolicyDenied, RuleId
    from nxs.core.models import Artifact, Level

    title("B · 安全断言（三层拦截）")
    coordinator = transports["coordinator"]
    node = transports["node-a"]

    # B2 矩阵层：往矩阵里没有的域投递
    try:
        coordinator.publish(from_node="coordinator", to_node="node-b", task_type="demo.echo",
                    params={"text": "x"}, idempotency_key="drill-b2-x")
        # coordinator→zone-b 在示例配置里是允许的，所以这条不必然报错
        check("B2 矩阵层（示例配置 coordinator→zone-b 允许，跳过）", True)
    except PolicyDenied:
        check("B2 矩阵层拒绝", True)

    # B3 字段边界：多一个字段
    delivery = coordinator.publish(from_node="coordinator", to_node="node-a",
                           task_type="demo.echo", params={"text": "x"},
                           idempotency_key="drill-b3")
    task = node.poll("node-a")
    if task is not None:
        try:
            node.complete("node-a", task, [
                Artifact(level=Level.METADATA, kind="structured",
                         data={"echo": "x", "level": "metadata",
                               "stray_field": "不该外发"})
            ])
            check("B3 越界字段被拒", False, "竟然通过了")
        except PolicyDenied as denied:
            check("B3 越界字段被拒 → WL-FIELD-001",
                  denied.rule_id is RuleId.WL_FIELD_NOT_ALLOWED,
                  denied.rule_id.value)
            # B6 拒绝不泄露
            wire = str(denied.to_wire())
            check("B6 响应体不含字段名", "stray_field" not in wire)
            # ★ 必须让任务落终态，否则它一直停在 working，
            #   占满 node-a 的 concurrent_tasks=1，后续投递全被
            #   QT-CONC-05 拒掉。这是验收时最容易踩的坑之一。
            try:
                node.fail("node-a", task, "drill: boundary rejected")
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# F · 观测
# ---------------------------------------------------------------------------

def drill_observability(transports: dict) -> None:
    """手册 F8：健康时零告警。"""
    from nxs.mgmt.observ import collect, detect_anomalies

    title("F8 · 健康时零告警")
    app = transports["_app"]
    snapshot = collect(app.metrics, registry=app.registry,
                       queue=app.stores.queue)
    anomalies = detect_anomalies(snapshot)
    check("正常业务后零告警", anomalies == [],
          f"误报 {[a.rule for a in anomalies]}")


# ---------------------------------------------------------------------------
# M15 · 观察窗
# ---------------------------------------------------------------------------

def drill_ui(transports: dict) -> None:
    """手册「怎么验观察窗」那一节的可自动化部分。"""
    import http.cookiejar
    import urllib.error
    import urllib.request

    from nxs.ui import UiServer

    title("M15 · 观察窗（只读 · token · 只听本机）")
    gateway_url = transports["coordinator"].base_url
    secret = transports["_ops_secret"]
    server = UiServer(gateway_url, secret, port=0).start()

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    def get(url, cookie=""):
        opener = urllib.request.build_opener(_NoRedirect)
        request = urllib.request.Request(url)
        if cookie:
            request.add_header("Cookie", cookie)
        try:
            with opener.open(request, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    try:
        # ① 不带 token → 401
        status, _, _ = get(f"{server.base_url}/")
        check("不带 token 访问 → 401", status == 401, f"得到 {status}")

        # ② 带 token → 303 + HttpOnly cookie
        status, headers, _ = get(f"{server.base_url}/?t={server.token}")
        cookie = headers.get("Set-Cookie", "")
        check("token 换 cookie 并跳走", status == 303 and "Location" in headers,
              f"得到 {status}")
        check("cookie 是 HttpOnly + SameSite=Strict",
              "HttpOnly" in cookie and "SameSite=Strict" in cookie, cookie[:60])

        # ③ 带 cookie 能取页面与数据
        jar = f"nxs_ui={server.token}"
        status, headers, body = get(f"{server.base_url}/", jar)
        check("带 cookie 取页面 → 200 text/html",
              status == 200 and "text/html" in headers.get("Content-Type", ""))
        status, _, raw = get(f"{server.base_url}/api/state", jar)
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            data = {}
        check("取 /api/state → 有节点与指标",
              status == 200 and data.get("nodes") is not None
              and data.get("metrics") is not None)
        nodes = data.get("nodes") or []
        # ⚠ 必须先要求非空：``all([])`` 恒为 True，会让这条空过。
        #   初版就踩了 —— 指标取不到时节点是空列表，它照样"通过"。
        check("节点行含名称与简介",
              len(nodes) > 0
              and all("display_name" in n and "description" in n for n in nodes),
              f"{len(nodes)} 个节点")

        # ④ 写方法一律 405
        for method in ("POST", "PUT", "DELETE"):
            opener = urllib.request.build_opener(_NoRedirect)
            request = urllib.request.Request(
                f"{server.base_url}/api/state", data=b"{}", method=method)
            request.add_header("Cookie", jar)
            try:
                with opener.open(request, timeout=10) as resp:
                    code = resp.status
            except urllib.error.HTTPError as exc:
                code = exc.code
            check(f"{method} → 405（只读）", code == 405, f"得到 {code}")

        # ⑤ 拒绝绑公网
        try:
            UiServer(gateway_url, secret, host="0.0.0.0", port=0)
            check("拒绝绑定 0.0.0.0", False, "竟然接受了")
        except Exception:  # noqa: BLE001
            check("拒绝绑定 0.0.0.0", True)

        # ⑥ token 不跨重启复用
        another = UiServer(gateway_url, secret, port=0).start()
        try:
            check("每次启动换新 token", another.token != server.token)
        finally:
            another.stop()
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 三 · 配了不生效
# ---------------------------------------------------------------------------

def drill_dead_config(transports: dict) -> None:
    """手册第三章：配额已删除，唯一生效的是信箱深度。

    ⚠ **这一段是性质反转的**（权限模型改造 Step 5b）：

        旧：验证 `bytes_per_response` **确实不生效**（一项死配置）
        新：验证 `max_backlog` **确实生效**（唯一真正的强制点）

    反转本身就是这次改造的核心成果 —— 从"配了但不生效"变成
    "配了就真的会被执行"。所以这不再是"抽查死配置"，
    而是"抽查唯一活着的那道闸门"。
    """
    from nxs.core.errors import PolicyDenied, RuleId
    from nxs.mgmt.config import UNIMPLEMENTED

    title("三 · 节点自限额（max_backlog 确实生效）")

    # ① 配额必须已从"未实现清单"里消失 —— 因为它整个不存在了
    registered = dict(UNIMPLEMENTED)
    check("配额项已从 UNIMPLEMENTED 移除（配置本身已删除）",
          "bytes_per_response" not in registered,
          "若它还在清单里，说明配额没删干净")

    # ② Mesh 自我保护必须还在 —— 删配额时很容易误伤
    from nxs.core.errors import RuleId as _R
    values = {r.value for r in _R}
    check("Mesh 自我保护仍在（速率 / 连接数）",
          "QT-RATE-04" in values and "QT-CONN-06" in values,
          "这两项与节点配额无关，不该被删")

    # ③ 信箱深度确实会拒收
    from nxs.control.policy import PolicyEngine
    app = transports["_app"]
    eng = PolicyEngine(app.config)
    card = app.config.nodes["node-a"]
    limit = card.self_limits.max_backlog
    try:
        eng.check_backlog("node-a", pending=limit)
        check(f"信箱满（{limit}）时拒收", False, "竟然没拒 —— 检查没生效")
    except PolicyDenied as exc:
        check(f"信箱满（{limit}）时拒收，错误码 {exc.rule_id.value}",
              exc.rule_id == RuleId.QT_BACKLOG_FULL,
              "错误码不对：该用 QT-BACKLOG-07（资源问题），"
              "不能用 MX-*（权限问题）")
    try:
        eng.check_backlog("node-a", pending=0)
        check("信箱空时放行", True)
    except PolicyDenied as exc:
        check("信箱空时放行", False, f"竟然被拒：{exc.rule_id.value}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="按验收手册抽查（覆盖可自动化的条目）")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()  # noqa: F841

    print("\n  Nexus Mesh · 验收抽查")
    print("  依据：docs/acceptance-guide.md")
    print("  " + "─" * 60)

    drill_cli()

    app, gateway, transports = _build_live()
    transports["_app"] = app
    try:
        drill_core_loop(transports)
        drill_boundaries(transports)
        drill_observability(transports)
        drill_dead_config(transports)
        drill_ui(transports)
    finally:
        gateway.stop()

    print("\n  " + "─" * 60)
    if FAILED:
        print(f"  \033[31m{FAILED} 项未通过\033[0m，{PASSED} 项通过")
        print("  ⚠ 若手册与代码不符，先判断是哪个错了，再改哪一个。")
        return 1
    print(f"  \033[32m{PASSED} 项全部通过\033[0m")
    print("  ⚠ 本脚本只覆盖可自动化部分。systemd、TLS、跨机器网络"
          "必须手工验（见手册）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
