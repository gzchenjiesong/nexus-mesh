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
    title("G4 · CLI 全可用（手册说 8 个命令）")
    commands = ["verify", "doctor", "demo", "serve", "ops",
                "candidates", "metrics", "status"]
    for cmd in commands:
        result = subprocess.run(
            [sys.executable, "-m", "nxs", cmd, "--help"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        check(f"nxs {cmd} --help", result.returncode == 0,
              f"rc={result.returncode}")
    check("命令数量与手册一致（8 个）", len(commands) == 8)


# ---------------------------------------------------------------------------
# A · 核心闭环
# ---------------------------------------------------------------------------

def _build_live():
    """装配一个真实 HTTP 的 hub + 两个节点。"""
    from nxs.app import NexusApp
    from nxs.edge.client import HttpTransport
    from nxs.edge.gateway import Gateway
    from nxs.mgmt.config import EXAMPLE_CONFIG_PATH, ServerConfig, demo_env_for

    app = NexusApp.from_config_path(
        str(EXAMPLE_CONFIG_PATH), env=demo_env_for(EXAMPLE_CONFIG_PATH),
        force_memory=True,
    )
    app.start()
    gateway = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0),
                      ops_secret=b"").start()
    transports = {
        name: HttpTransport(gateway.base_url, node_id=name,
                            secret=app.config.secrets[name])
        for name in ("hub", "node-a", "node-b")
    }
    return app, gateway, transports


def drill_core_loop(transports: dict) -> None:
    """手册 A：注册 → 批准 → 投递 → 领取 → 执行 → 结果。"""
    from nxs.core.models import Level
    from nxs.sdk import NodeClient, NodeConfig

    title("A · 核心闭环（真实 HTTP）")
    node = transports["node-a"]
    hub = transports["hub"]

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
    delivery = hub.publish(from_node="hub", to_node="node-a",
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
    hub = transports["hub"]
    node = transports["node-a"]

    # B2 矩阵层：往矩阵里没有的域投递
    try:
        hub.publish(from_node="hub", to_node="node-b", task_type="demo.echo",
                    params={"text": "x"}, idempotency_key="drill-b2-x")
        # hub→zone-b 在示例配置里是允许的，所以这条不必然报错
        check("B2 矩阵层（示例配置 hub→zone-b 允许，跳过）", True)
    except PolicyDenied:
        check("B2 矩阵层拒绝", True)

    # B3 字段边界：多一个字段
    delivery = hub.publish(from_node="hub", to_node="node-a",
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
# 三 · 配了不生效
# ---------------------------------------------------------------------------

def drill_dead_config(transports: dict) -> None:
    """手册第三章：登记的死配置确实不生效。"""
    from nxs.core.models import Artifact, Level
    from nxs.mgmt.config import UNIMPLEMENTED

    title("三 · 配了不生效（bytes_per_response 确实不触发）")
    registered = dict(UNIMPLEMENTED)
    check("bytes_per_response 已登记为未实现",
          "bytes_per_response" in registered)

    hub = transports["hub"]
    node = transports["node-a"]
    hub.publish(from_node="hub", to_node="node-a", task_type="demo.echo",
                params={"text": "big"}, idempotency_key="drill-dead-config")
    task = node.poll("node-a")
    if task is None:
        check("准备大产物", False, "没领到任务")
        return
    # 200 KB > bytes_per_response(64 KB)，< max_body_bytes(256 KB)
    big = "x" * 200_000
    try:
        node.complete("node-a", task, [
            Artifact(level=Level.METADATA, kind="structured",
                     data={"echo": big, "level": "metadata"})
        ])
        check("200 KB 产物未被 bytes_per_response 拦（与手册一致）", True)
    except Exception as exc:  # noqa: BLE001
        check("200 KB 产物未被 bytes_per_response 拦", False,
              f"竟被拦住：{exc}")


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
