"""改造边界守护：哪些测试不该被权限模型改造波及。

⚠ **本文件的存在是为了让「测试红了」这件事可解释。**

用户提出的风险（2026-10-29）：

> 「790 多个用例都是基于之前旧版本设置的。如果没注意的话，
> 可能会导致把正确的代码或者文档改回旧的版本。」

这个风险不是"多跑几分钟"，而是：**旧模型绑定的测试会主动把你推向错误方向**
—— 你按新模型改对了代码，过时的测试报红，你以为自己改错了。

所以把测试分成三类，并**用测试来守住分类本身**：

  · **安全带**（本文件的 `UNAFFECTED`）—— 不该变红，红了就是代码被改坏
  · **待重写**（`docs/design/08-test-inventory.md` 的 B/C 类）—— 会红，改测试
  · **已删除**（A 类）—— 测的能力整体不存在了

本文件只守第一类：**安全带文件里不允许出现旧模型的概念。**
"""

from __future__ import annotations

import pathlib
import re

import pytest

from tests.test_traceability import covers

REPO = pathlib.Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"

#: 旧模型的专属概念。出现即意味着该测试**绑定了**旧设计。
OLD_MODEL_VOCAB = [
    "roles.issuer",
    "roles.executor",
    "roles.relay",
    "roles.observer",
    "roles.human_interface",
    "can_issue_types",
    "can_receive_from",
    "tasks_per_day",
    "bytes_per_response",
    "bytes_per_day",
    "concurrent_tasks",
    "local_node",
    "MX-RECEIVE-03",
    "MX-DELEGATE-04",
    "QT-CONC-05",
    "QT-TASK-01",
]

#: **安全带**：这些测试文件完全不涉及权限模型。
#:
#: 改造不应让它们变红。若它们红了，说明代码被改坏了，而不是测试过时。
#:
#: 挑选标准：截至 2026-10-29，文件内**零**旧模型词汇命中。
UNAFFECTED = {
    "test_cli_actor.py",
    "test_deploy.py",
    "test_deploy_scripts.py",
    "test_gateway.py",
    "test_gateway_attack.py",
    "test_lease.py",
    "test_runtime.py",
    "test_skill_doc.py",
    "test_traceability.py",
    "test_ui.py",
}


class TestSeatbeltFilesStayClean:
    """★★ 安全带文件不允许混入旧模型概念。

    ⚠ **为什么需要这条**

    这 10 个文件当前完全不碰权限模型，是改造期间的**回归基准**：
    它们红了就说明代码坏了。

    但如果有人（包括未来的我）往里面加入 `can_receive_from` 之类的新断言，
    这个基准就被污染了 —— 它会从"安全带"变成"又一个待重写的文件"，
    而分类表里还写着它安全。

    **分类表必须和现实一致**，否则它会误导而不是帮助。
    """

    @covers("M11.F1")
    def test_unaffected_files_have_no_old_model_vocab(self):
        problems: dict[str, list[str]] = {}

        for name in sorted(UNAFFECTED):
            p = TESTS / name
            assert p.is_file(), f"安全带清单里的 {name} 不存在（改名了？）"
            text = p.read_text(encoding="utf-8", errors="replace")
            found = [v for v in OLD_MODEL_VOCAB if v in text]
            if found:
                problems[name] = found

        assert not problems, (
            "这些被列为「不该受改造影响」的测试文件里出现了旧模型概念：\n"
            + "\n".join(f"  {f}: {v}" for f, v in problems.items())
            + "\n\n  两种处理，二选一：\n"
            "    ① 移除该断言（若它确实与权限模型无关）\n"
            "    ② 把文件从 UNAFFECTED 移到待重写清单，并同步 08-test-inventory.md\n"
            "  ⚠ 不要只是为了让它绿就改代码 —— 先判断是哪边过时了"
        )

    @covers("M11.F1")
    def test_unaffected_count_is_substantial(self):
        """★ 安全带必须覆盖足够多的用例，否则它保护不了什么。

        若这个数字骤降，说明有人把文件挪走了却没补新的 ——
        改造期间就失去了回归基准。
        """
        total = 0
        for name in UNAFFECTED:
            p = TESTS / name
            if p.is_file():
                total += len(re.findall(
                    r"^\s+def test_", p.read_text(encoding="utf-8"), re.M))

        assert total >= 250, (
            f"安全带只剩 {total} 个用例（预期 ≥250）。\n"
            "  改造期间需要一批「不该变红」的基准测试来区分"
            "「代码坏了」与「测试过时了」"
        )


class TestInventoryDocIsAccurate:
    """★★ 测试清单文档必须与真实文件一致。

    `docs/design/08-test-inventory.md` 是改造时的判据文档：
    遇到测试红，先查它再改代码。**它错了比没有更糟** ——
    会让人按错误的分类去改代码。
    """

    DOC = REPO / "docs" / "design" / "08-test-inventory.md"

    @covers("M11.F1")
    def test_doc_exists(self):
        assert self.DOC.is_file(), (
            "缺少 08-test-inventory.md —— 改造时没有判据文档可查"
        )

    @covers("M11.F1")
    def test_deleted_files_genuinely_gone(self):
        """A 类里声明的「已删除」文件必须真的不存在了。

        否则文档说"已删"、实际还在跑，会让人以为改动没生效。
        """
        gone = ["test_local_node.py", "test_quota_integration.py"]
        still_there = [f for f in gone if (TESTS / f).exists()]
        assert not still_there, (
            f"清单说这些已删除，但文件还在：{still_there}"
        )

    @covers("M11.F1")
    def test_referenced_files_exist(self):
        """文档里提到的测试文件名必须真实存在。"""
        #: A 类「已删除」的文件 —— 文档提到它们是**应该的**，
        #: 它们的不存在由 test_deleted_files_genuinely_gone 断言。
        ALREADY_DELETED = {"test_local_node.py", "test_quota_integration.py"}

        text = self.DOC.read_text(encoding="utf-8")
        mentioned = set(re.findall(r"\b(test_[a-z_]+\.py)\b", text)) - ALREADY_DELETED
        missing = sorted(
            f for f in mentioned if not (TESTS / f).is_file()
        )
        assert not missing, (
            f"清单提到但实际不存在的测试文件：{missing}\n"
            "  文件改名后清单要同步"
        )

    @covers("M11.F1")
    def test_seatbelt_matches_code(self):
        """清单里的安全带列表必须与代码里的 UNAFFECTED 一致。"""
        text = self.DOC.read_text(encoding="utf-8")
        mentioned = set(re.findall(r"\b(test_[a-z_]+\.py)\b", text))

        # 清单 D 类里列出的应是 UNAFFECTED 的超集或相等
        not_in_doc = sorted(UNAFFECTED - mentioned)
        assert not not_in_doc, (
            f"代码里列为安全带、但清单文档没写：{not_in_doc}\n"
            "  两处清单会各自漂移"
        )


@covers("M11.F1")
def test_no_test_targets_removed_concepts():
    """★ 全测试目录不应再测「已明确移除」的概念。

    `local_node` 是最确定的一个 —— mesh 不再占节点名额，
    这个概念在新模型里不存在任何位置。
    """
    #: 这些文件**定义**了旧模型词汇表，必然包含这些词。
    #: 它们不是"在测试旧概念"，而是在描述"哪些概念已淘汰"。
    VOCAB_DEFINERS = {"test_docs_design_consistency.py", "test_model_boundary.py"}

    offenders = []
    for p in sorted(TESTS.glob("test_*.py")):
        if p.name in VOCAB_DEFINERS:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if "local_node" in text:
            offenders.append(p.name)

    assert not offenders, (
        f"这些测试还在引用已移除的 local_node 概念：{offenders}\n"
        "  mesh 不再注册为节点，该配置项整体删除"
    )
