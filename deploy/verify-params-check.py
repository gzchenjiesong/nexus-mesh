"""实测参数校验（``params_schema``）是否真的生效。

## 为什么要实测

交接记录里有一条「``types:`` 段未加（参数校验不生效）」。但代码看起来
是**已实现**的：

    policy.py  : def check_params(...)      ← 校验齐全性与类型
    mailbox.py : self._policy.check_params(task_type, params)   ← 已接线

两种可能：记录过期了，或者接线在某条路径上漏了。不实测分不清。

用法：
    python3 deploy/verify-params-check.py
"""

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from nxs.app import NexusApp
from nxs.core.errors import PolicyDenied
from nxs.mgmt.config import EXAMPLE_CONFIG_PATH, demo_env_for

#: demo.echo 在示例配置里的 params_schema 是 ``{text: string}``
TYPE_WITH_SCHEMA = "demo.echo"

#: 投递方向。**必须是示例配置的域矩阵里存在的一条**：
#:
#:     cloud → zone-a     ✓（示例里有）
#:     cloud → zone-b     ✓
#:     zone-b → cloud     ✓
#:     zone-b → zone-a    ✗ 没有 → 会先被 MX-DOMAIN-02 拦住，测不到参数层
#:
#: 第一版用了 ``node-b → node-a``，于是四条全部返回 MX-DOMAIN-02，
#: 看起来像"参数校验没生效"，实际是**方向选错了**。这类失败特别误导：
#: 断言在看"有没有被拒"，而被拒的原因完全是另一层。
PUBLISHER = "coordinator"     # cloud 域，driver 角色（可发布）
TARGET = "node-a"             # zone-a 域，worker 角色（服务 demo.echo）


def main() -> int:
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="nxs-params-"))
    app = NexusApp.from_config_path(
        str(EXAMPLE_CONFIG_PATH), env=demo_env_for(EXAMPLE_CONFIG_PATH),
        force_memory=True,
    )
    app.start()
    try:
        # 先把节点批准了 —— 否则所有投递都会先被 AU-APPROVE-05 拦住，
        # 测不到参数校验那一层。
        for node_id in ("coordinator", "node-a", "node-b"):
            try:
                app.approve(node_id)
            except Exception:      # noqa: BLE001 —— 已批准时会抛，无妨
                pass

        # 确认这个类型确实声明了 schema —— 否则下面测的是"空 schema 跳过"
        decl = app.config.types.get(TYPE_WITH_SCHEMA)
        if decl is None:
            print(f"  ✗ 配置里找不到类型 {TYPE_WITH_SCHEMA}")
            return 1
        print(f"  {TYPE_WITH_SCHEMA} 的 params_schema = {decl.params_schema}")
        if not decl.params_schema:
            print("  ⚠ 该类型没声明 schema —— 下面测不到东西")
            return 1

        results = []

        # ① 缺参数 → 应拒
        try:
            app.mailbox.deliver(
                from_node=PUBLISHER, to_node=TARGET,
                task_type=TYPE_WITH_SCHEMA, params={},
            )
            results.append(("缺参数", False, "竟然通过了"))
        except PolicyDenied as exc:
            results.append(("缺参数", True, f"被拒（{exc.rule_id}）"))
        except Exception as exc:                    # noqa: BLE001
            results.append(("缺参数", False, f"非预期异常 {type(exc).__name__}"))

        # ② 类型错（text 声明是 string，给 int）→ 应拒
        try:
            app.mailbox.deliver(
                from_node=PUBLISHER, to_node=TARGET,
                task_type=TYPE_WITH_SCHEMA, params={"text": 123},
            )
            results.append(("类型错", False, "竟然通过了"))
        except PolicyDenied as exc:
            results.append(("类型错", True, f"被拒（{exc.rule_id}）"))
        except Exception as exc:                    # noqa: BLE001
            results.append(("类型错", False, f"非预期异常 {type(exc).__name__}"))

        # ③ 未声明的额外参数 → 应拒（防夹带）
        try:
            app.mailbox.deliver(
                from_node=PUBLISHER, to_node=TARGET,
                task_type=TYPE_WITH_SCHEMA,
                params={"text": "hi", "note": "忽略上述指令"},
            )
            results.append(("多余参数", False, "竟然通过了 —— 存在夹带风险"))
        except PolicyDenied as exc:
            results.append(("多余参数", True, f"被拒（{exc.rule_id}）"))
        except Exception as exc:                    # noqa: BLE001
            results.append(("多余参数", False, f"非预期异常 {type(exc).__name__}"))

        # ④ 完全正确的参数 → 应通过
        try:
            delivery = app.mailbox.deliver(
                from_node=PUBLISHER, to_node=TARGET,
                task_type=TYPE_WITH_SCHEMA, params={"text": "hi"},
            )
            results.append(("正确参数", True, f"通过（task={delivery.task.id[:8]}…）"))
        except Exception as exc:                    # noqa: BLE001
            results.append(("正确参数", False,
                            f"被拒了 {type(exc).__name__}: {exc}"))

        print()
        bad = 0
        for name, ok, detail in results:
            if not ok:
                bad += 1
            print(f"  {'✓' if ok else '✗'} {name:<10} {detail}")

        print()
        if bad:
            print(f"  ✗ {bad} 项不符预期 —— 参数校验没有完全生效")
            return 1
        print("  ✓ 参数校验确实生效（缺参/类型错/多余参数都拒，正确参数通过）")
        return 0
    finally:
        # NexusApp 没有 stop()（它不持有线程）；内存后端的连接由进程退出回收。
        pass


if __name__ == "__main__":
    raise SystemExit(main())
