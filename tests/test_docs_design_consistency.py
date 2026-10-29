"""文档与设计一致性守护。

⚠ **本文件的 PENDING 清单在权限模型改造期间是一个进度表。**

每改完一份文档，从 ``PENDING`` 里删掉一行。清单清空 = 文档改完。
在那之前不允许动代码 —— 否则代码会照着一份过时的设计去写。

为什么用测试来管这件事：口头确认会忘，而 CI 会一直提醒。
「这次先凑合」如果没有任何东西拦着，就一定会发生。
"""

from __future__ import annotations

import pathlib
import re

import pytest

from tests.test_traceability import covers

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"


#: 权限模型改造后**不再允许出现**的词汇。
#:
#: 每一个词都对应一处被淘汰的设计，附上替代方案，
#: 这样看到报错的人知道该改成什么，而不是只看到"不许用"。
DEAD_VOCAB: dict[str, str] = {
    "roles.issuer": "改为单一 role 字段",
    "roles.executor": "改为单一 role 字段（该标志从来不参与判定）",
    "roles.relay": "删除（从未实现）",
    "roles.observer": "删除（角色不定义，看板不是节点）",
    "roles.human_interface": "删除（从未实现）",
    "can_issue_types": "由 role.can_issue 决定",
    "can_receive_from": "由域矩阵表达",
    "tasks_per_day": "改为 self_limits（节点自律，非配额）",
    "bytes_per_response": "改为 self_limits",
    "bytes_per_day": "改为 self_limits",
    "concurrent_tasks": "删除（信箱排队，不设并发上限）",
    "local_node": "删除（mesh 不再占用节点名额）",
    "zone-work": "域名改为 work",
    "zone-home": "域名改为 home",
    "MX-RECEIVE-03": "并入 MX-DOMAIN-02",
    "MX-DELEGATE-04": "删除（先不做二次委托）",
    "QT-CONC-05": "删除（并发不设上限）",
    "QT-TASK-01": "删除（按日任务数属节点自律）",
}

#: **尚未改完**的文档。改完一个删一行。
#:
#: 顺序按风险排：可执行示例 → 权威架构 → 用户会照做的 → 其他。
PENDING: set[str] = {
    "config/nxs.example.yaml",              # 可执行，与 config.py 同步改
    "deploy/local.yaml.example",            # 同上
}

#: 历史性文档 —— 描述的是某个时间点的事实，改了反而失真。
HISTORICAL: set[str] = {
    "CHANGELOG.md",                          # 各版本的变更记录
    "docs/docs-audit.md",                    # 一次审计的快照
    "docs/design/03-traceability-review.md",  # 一次追溯评审的快照
}

#: 本次改造的设计文档本身 —— 正文会引用旧模型做对照，属必要内容。
SELF_REFERENTIAL: set[str] = {
    "docs/design/06-agent-role-model.md",
    "docs/design/07-permission-model-implementation.md",
    # 测试清单：它的职责正是"列出哪些测试绑定了旧模型"，必然要写出这些词
    "docs/design/08-test-inventory.md",
}


def _iter_docs() -> list[pathlib.Path]:
    """仓库里的文档文件，排除测试产物与依赖目录。"""
    out = []
    for p in REPO_ROOT.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix not in (".md", ".yaml", ".yml"):
            continue
        s = str(p.relative_to(REPO_ROOT))
        if any(x in s for x in ("egg-info", ".venv", "node_modules",
                                ".nxs/test-tmp", ".git/")):
            continue
        out.append(p)
    return sorted(out)


def _rel(p: pathlib.Path) -> str:
    return str(p.relative_to(REPO_ROOT))


class TestNoDeadVocabulary:
    """★★ 已淘汰的设计词汇不能出现在文档里。

    ⚠ **这条守护是为了让「文档先行」可验证，而不是靠自觉。**

    改造过程中反复出现的问题是：写代码时顺手改了某处，文档忘了；
    或者文档改了，示例配置忘了。等到要发布时，没人说得清
    「到底哪份文档是准的」。

    所以把它变成测试：

      · ``PENDING`` 里的文件暂时放行（它们还没改）
      · 不在 ``PENDING`` 里的文件，出现淘汰词汇立刻红
      · ``PENDING`` 清空 = 文档改完

    ## 两种豁免

      · ``ok:describes-removed`` 行内标记 —— 该行在描述"删掉了什么"
      · ``HISTORICAL`` / ``SELF_REFERENTIAL`` 整份文件豁免

    ## 为什么带替代方案

    ``DEAD_VOCAB`` 是 dict 而不是 set，因为报错时要告诉改的人
    "该换成什么"。只说"不许用"会让人去找一个同样的东西替代，
    而那正是本改造要避免的。
    """

    @covers("M11.F1")
    def test_no_dead_vocabulary(self):
        problems: dict[str, list[str]] = {}

        for p in _iter_docs():
            rel = _rel(p)
            if rel in HISTORICAL or rel in SELF_REFERENTIAL:
                continue
            if rel in PENDING:
                continue

            text = p.read_text(encoding="utf-8", errors="replace")
            # 允许显式标记「本节在描述已删除的东西」。
            # 设计文档必须能讨论"删掉了什么、为什么"，否则读者无从理解变更。
            # 标记做成行内注释，作用范围是本行；跨行的段落用多条标记。
            lines = text.splitlines()
            live_text = "\n".join(
                ln for ln in lines if "ok:describes-removed" not in ln
            )
            found = [v for v in DEAD_VOCAB if re.search(re.escape(v), live_text)]
            if found:
                problems[rel] = found

        assert not problems, (
            "这些文档还含已淘汰的设计词汇（且不在 PENDING 里）：\n"
            + "\n".join(
                f"  {f}:\n" + "\n".join(
                    f"      {v}  → {DEAD_VOCAB[v]}" for v in vs)
                for f, vs in problems.items()
            )
            + "\n\n  若这份文档确实还没改完，把它加进 PENDING 并注明原因"
        )

    @covers("M11.F1")
    def test_pending_files_still_exist(self):
        """★ PENDING 里的路径必须真实存在。

        否则会出现"清单上写着要改，但文件早就改名/删了"的情况，
        于是清单永远清不空，而没人知道为什么。
        """
        missing = [f for f in PENDING if not (REPO_ROOT / f).exists()]
        assert not missing, (
            f"PENDING 里这些路径不存在：{missing}\n"
            "  改完后请从清单删除，或修正路径"
        )

    @covers("M11.F1")
    def test_pending_is_tracked_elsewhere(self):
        """★ 待改清单必须与实施文档里的记录一致。

        避免两处清单各自漂移：改完一半，一处删了一处没删。
        """
        plan = DOCS_DIR / "design" / "07-permission-model-implementation.md"
        if not plan.is_file():
            pytest.skip("实施文档不存在")

        text = plan.read_text(encoding="utf-8")
        # 文档里应当出现 PENDING 中每个文件名（至少提到过）
        not_mentioned = [
            f for f in PENDING
            if pathlib.Path(f).name not in text
        ]
        assert not not_mentioned, (
            "这些待改文件在实施文档里没有提到：\n"
            + "\n".join(f"  {f}" for f in not_mentioned)
            + "\n  两处清单要同步，否则会各自漂移"
        )


class TestDocsCompletedGate:
    """★★ 文档改完之前不允许动代码。

    ⚠ 用户明确要求：**先完成文档变更，再写代码**。

    理由：如果文档没改完就写代码，代码会照着一份过时的设计去写；
    等文档后来更新，两边就对不上——而那时已经很难判断
    "是代码错了还是文档错了"。

    这条测试本身不阻止写代码（测试无法阻止），但它让状态可见：
    `PENDING` 非空 = 还没到写代码的时候。
    """

    @covers("M11.F1")
    def test_pending_emptiness_is_reported(self):
        """把待改清单打印出来，让进度一目了然。"""
        if not PENDING:
            return  # 全部改完

        # 不算失败，只提示。真正的门禁在 test_no_dead_vocabulary。
        print(f"\n  文档改造进度：还剩 {len(PENDING)} 个文件待改")
        for f in sorted(PENDING):
            print(f"    · {f}")
        print("  → 这些改完才能动代码（见 07-permission-model-implementation.md §8）")
