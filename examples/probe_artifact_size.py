"""实测产物能多大 —— 一条命令复现 docs/artifact-size.md 里的数字。

用法：

    python examples/probe_artifact_size.py            # 跑标准探测
    python examples/probe_artifact_size.py --verbose  # 带每步细节

它做两件事：

1. **探边界**：逐级加大产物，看在哪一层被拒、报什么码
2. **算开销**：给定文件大小，算出完整信封占多少字节

为什么这个脚本值得进仓库
------------------------
"能传多大"是个会被反复问到的问题，而答案依赖三个地方的限制
（``server.max_body_bytes`` / 节点 ``self_limits`` / ``MAX_FREETEXT_BYTES``），
其中**有一个是死配置**（``bytes_per_response`` 从未被传入，
见 docs/artifact-size.md）。靠读配置推答案是错的，必须实测。

把这个脚本留下，下次改配置时能立刻知道真实上限变没变。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nxs.app import AppStores, NexusApp
from nxs.backends.memory import (
    MemoryAuditStore,
    MemoryQueueStore,
    MemoryRegistryStore,
)
from nxs.core import envelope as envelope_mod
from nxs.core.models import Artifact, Level, Task, TaskMode
from nxs.edge.client import HttpTransport
from nxs.edge.gateway import Gateway
from nxs.mgmt.config import (
    EXAMPLE_CONFIG_PATH,
    ServerConfig,
    demo_env_for,
    load_config,
)


def _kb(n: int) -> float:
    return n / 1024


def _build_app(lease_seconds: int = 60):
    """用示例配置装配，存储换内存、租约放宽。

    两处刻意的放宽，都只影响探测本身：

    * ``lease_seconds=60``：被拒的产物会让任务停在 working，
      短租约会打乱节奏
    * **并发配额调大**：示例配置里 node-a 的 ``concurrent_tasks=1``，
      一次探测就会占满，导致后续探测因 ``QT-CONC-05`` 失败
      —— 那是配额在正常工作，不是我们要测的东西
    """
    from dataclasses import replace

    from nxs.core.models import RoleCard

    config = load_config(EXAMPLE_CONFIG_PATH, env=demo_env_for(EXAMPLE_CONFIG_PATH))
    config.stores = replace(config.stores, queue="memory", registry="memory",
                            data_dir="/tmp/nxs-probe")

    # 放宽 node-a 的信箱深度，避免探测之间互相挤占。
    #
    # ⚠ 原来是改 quota（并发 64 / 按日 100000）。配额已在权限模型改造
    #   Step 5b 删除 —— 现在唯一会拦住探测的是 self_limits.max_backlog，
    #   所以放宽它即可。
    original = config.nodes["node-a"]
    config.nodes["node-a"] = RoleCard.from_dict({
        **original.to_dict(),
        "self_limits": {
            **original.to_dict()["self_limits"],
            "max_backlog": 500,
        },
    })

    app = NexusApp(
        config,
        AppStores(registry=MemoryRegistryStore(), queue=MemoryQueueStore(),
                  audit=MemoryAuditStore()),
        lease_seconds=lease_seconds,
    )
    app.start()
    app.approve("node-a")
    app.approve("node-b")
    return app


def envelope_overhead(file_bytes: int) -> dict:
    """算出 N 字节文件塞进信封后的实际大小（不经网络）。"""
    raw = b"y" * file_bytes
    b64 = base64.b64encode(raw).decode()
    art = Artifact(level=Level.METADATA, kind="structured",
                   data={"echo": b64, "level": "metadata"})
    task = Task(id="t-probe", idempotency_key="k", type="demo.echo",
                session_id="probe", from_node="node-a", to_node="coordinator",
                mode=TaskMode.SYNC, artifacts=[art])
    wire = json.dumps(
        envelope_mod.envelope_for(b"x" * 32, "node-a", task, session_id="probe").to_dict(),
        ensure_ascii=False,
    ).encode()
    return {
        "file_bytes": file_bytes,
        "b64_bytes": len(b64.encode()),
        "envelope_bytes": len(wire),
        "ratio": len(wire) / file_bytes if file_bytes else 0,
    }


def probe_network(gateway, transports, sizes: list[int], *, verbose: bool) -> list[dict]:
    """真的走 HTTP，逐级试探在哪被拒。"""
    coordinator = transports["coordinator"]
    node = transports["node-a"]
    results: list[dict] = []

    for n in sizes:
        key = f"probe-{n}"
        coordinator.publish(from_node="coordinator", to_node="node-a", task_type="demo.echo",
                    params={"text": "p"}, idempotency_key=key)
        task = node.poll("node-a")
        payload = base64.b64encode(b"x" * n).decode()
        outcome = "OK"
        try:
            node.complete("node-a", task, [
                Artifact(level=Level.METADATA, kind="structured",
                         data={"echo": payload, "level": "metadata"})
            ])
        except Exception as exc:  # noqa: BLE001 - 探测本来就要捕获一切
            outcome = getattr(getattr(exc, "rule_id", None), "value",
                              type(exc).__name__)
            # 让任务落终态，否则会占满节点并发配额
            try:
                node.fail("node-a", task, "probe")
            except Exception:  # noqa: BLE001
                pass
        results.append({"file_bytes": n, "outcome": outcome})
        if verbose:
            print(f"    文件 {_kb(n):>7.1f} KB → {outcome}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="实测产物大小上限")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    app = _build_app()
    gateway = Gateway(app, server_config=ServerConfig(host="127.0.0.1", port=0),
                      ops_secret=b"").start()
    transports = {
        "node-a": HttpTransport(gateway.base_url, node_id="node-a",
                                secret=app.config.secrets["node-a"]),
        "coordinator": HttpTransport(gateway.base_url, node_id="coordinator",
                             secret=app.config.secrets["coordinator"]),
    }
    limit = gateway.server_config.max_body_bytes
    print(f"\n  Nexus Mesh · 产物大小探测")
    print(f"  server.max_body_bytes = {limit:,} 字节（{_kb(limit):.0f} KB）\n")

    try:
        # ---- 1. 开销换算（不经网络，快）----
        print("  ① 塞进信封的开销（base64 + JSON）")
        print(f"     {'文件':>10}  {'base64':>10}  {'完整信封':>10}  {'倍数':>6}")
        for kb in (1, 5, 10, 50, 100, 190):
            r = envelope_overhead(kb * 1024)
            print(f"     {_kb(r['file_bytes']):>8.0f} KB  {_kb(r['b64_bytes']):>8.0f} KB"
                  f"  {_kb(r['envelope_bytes']):>8.0f} KB  {r['ratio']:>5.2f}×")

        # ---- 2. 真实网络探边界 ----
        print("\n  ② 真实走 HTTP，看在哪一层被拒")
        sizes = [1_000, 10_000, 50_000, 100_000, 180_000, 190_000, 200_000, 300_000]
        rows = probe_network(gateway, transports, sizes, verbose=args.verbose)
        for r in rows:
            mark = "✓" if r["outcome"] == "OK" else "✗"
            print(f"     {mark} 文件 {_kb(r['file_bytes']):>7.1f} KB → {r['outcome']}")

        # ---- 3. 引用形态（坐标）----
        print("\n  ③ 对比：同一个 10 MB 文件，传坐标而不是内容")
        coord = {"path": "/data/report.pdf", "sha256": "9f2c", "size": 10 * 1024 * 1024}
        print(f"     坐标 payload ≈ {len(json.dumps(coord))} 字节"
              f"（vs 内容 {_kb(10 * 1024 * 1024):.0f} KB）")
        print("     ⚠ 但系统不校验坐标——文件是否真的取得到，取决于你的存储约定")

        ok_max = max((r["file_bytes"] for r in rows if r["outcome"] == "OK"),
                     default=0)
        print(f"\n  结论：信封能带约 {_kb(ok_max):.0f} KB 的文件。")
        print(f"        超过 {_kb(limit):.0f} KB 的产物应改走 kind=reference 传坐标。")
        print("        详见 docs/artifact-size.md\n")
        return 0
    finally:
        gateway.stop()


if __name__ == "__main__":
    raise SystemExit(main())
