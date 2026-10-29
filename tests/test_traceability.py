"""追溯元测试 —— 让编号体系**自我校验**。

这组测试检验的是"编号体系本身对不对"，而不是系统功能。它回答五个问题：

1. **编号格式合法吗** —— ``M5.F1`` 而不是 ``M5-F1``
2. **引用有效吗** —— 说某个需求由 ``M7.F4`` 实现，那 ``M7.F4`` 真的存在吗
3. **覆盖完整吗** —— 有没有定义了却没人验证的功能点
4. **有无孤儿引用** —— 测试里挂的编号，在清单里真的存在吗
5. **追溯表是最新的吗** —— ``traceability/MATRIX.md`` 由本文件自动生成

为什么要有它
------------
手工维护的映射表必然腐烂。不是有人故意违规，而是没人能一直记得：
改了代码忘了改文档，而**漏改不报错**——于是映射表慢慢变成谎言。

解法是让映射表的"来源"变成可执行的：编号清单是代码（``traceability/registry.py``），
测试用 ``@covers("M5.F1")`` 声明覆盖，本文件校验两边一致，
并**自动生成**给人看的追溯表。

**追溯表是跑出来的，不是抄出来的。**
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

import pytest

from conftest import covers  # noqa: F401  （本文件自身也声明覆盖）
from traceability import registry as reg

THIS_FILE = "test_traceability.py"
REPO = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO / "tests"
MATRIX_PATH = REPO / "traceability" / "MATRIX.md"

#: 各类编号的合法形态
PATTERNS = {
    "G": re.compile(r"^G\d+$"),
    "M": re.compile(r"^M\d+$"),
    "F": re.compile(r"^M\d+\.F\d+$"),
    "R": re.compile(r"^M\d+\.R\d+$"),
    "C": re.compile(r"^C\d+$"),
    "N": re.compile(r"^N\d+$"),
    "S": re.compile(r"^S\d+$")
}


def _covers_args(deco: ast.expr) -> list[str]:
    """从 ``@covers("a", "b")`` 装饰器提取编号。"""
    if not isinstance(deco, ast.Call):
        return []
    fn = deco.func
    name = fn.id if isinstance(fn, ast.Name) else (
        fn.attr if isinstance(fn, ast.Attribute) else None
    )
    if name != "covers":
        return []
    return [
        a.value for a in deco.args
        if isinstance(a, ast.Constant) and isinstance(a.value, str)
    ]


def _scan_sources() -> tuple[dict[str, list[str]], list[str]]:
    """**直接扫描测试源码**提取覆盖声明，不依赖 pytest 的收集结果。

    :returns: ``(编号 → 测试列表, 未声明覆盖的测试列表)``

    为什么不依赖 ``request.session.items``：那样元测试只在"跑全量"时有效。
    开发者常只跑单个文件，此时 session 里没有其他文件的测试，
    元测试会误报"全部未覆盖"——**一个会误报的检查比没有检查更糟，
    因为它会让人不再相信它**。

    源码扫描不受收集范围影响，任何调用方式下结论都一致。
    """
    covers_map: dict[str, list[str]] = defaultdict(list)
    undeclared: list[str] = []

    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name == THIS_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            nodeid = f"{path.name}::{node.name}"
            ids: list[str] = []
            for deco in node.decorator_list:
                ids.extend(_covers_args(deco))
            if ids:
                for i in ids:
                    covers_map[i].append(nodeid)
            else:
                undeclared.append(nodeid)

    return dict(covers_map), undeclared


@pytest.fixture(scope="module")
def covers_map():
    return _scan_sources()[0]


@pytest.fixture(scope="module")
def undeclared():
    return _scan_sources()[1]


# ---------------------------------------------------------------------------
# 1. 编号格式
# ---------------------------------------------------------------------------

class TestIdentifierFormat:
    def test_goals(self):
        for g in reg.GOALS:
            assert PATTERNS["G"].match(g.id), f"目标编号格式非法: {g.id}"

    def test_modules(self):
        for m in reg.MODULES:
            assert PATTERNS["M"].match(m.id), f"模块编号格式非法: {m.id}"

    def test_features(self):
        for f in reg.FEATURES:
            assert PATTERNS["F"].match(f.id), f"功能点编号格式非法: {f.id}"

    def test_requirements(self):
        for r in reg.REQUIREMENTS:
            assert PATTERNS["R"].match(r.id), f"需求编号格式非法: {r.id}"

    def test_no_duplicate_ids(self):
        # 注意 ALL_ITEMS 是 dict（id → 对象），遍历它得到的是 key
        ids = [it.id for it in reg.ALL_ITEMS.values()]
        dupes = {i for i in ids if ids.count(i) > 1}
        assert not dupes, f"编号重复定义: {sorted(dupes)}"

    def test_feature_and_requirement_use_dot_separator(self):
        """分隔符统一用点号 —— 短横线留给错误码（MX-DOMAIN-02）。"""
        for it in (*reg.FEATURES, *reg.REQUIREMENTS):
            assert "-" not in it.id, (
                f"{it.id} 使用了短横线。层级用点号（M7.F2），"
                f"短横线保留给错误码，避免两类标识符在视觉上混淆。"
            )


# ---------------------------------------------------------------------------
# 2. 引用有效性
# ---------------------------------------------------------------------------

class TestReferences:
    def test_module_goals_exist(self):
        for m in reg.MODULES:
            for g in m.goals:
                assert g in reg.ALL_ITEMS, (
                    f"{m.id} 引用了不存在的目标 {g}"
                )

    def test_feature_goals_exist(self):
        for f in reg.FEATURES:
            for g in f.goals:
                assert g in reg.ALL_ITEMS, f"{f.id} 引用了不存在的目标 {g}"

    def test_requirement_goals_exist(self):
        for r in reg.REQUIREMENTS:
            for g in r.goals:
                assert g in reg.ALL_ITEMS, f"{r.id} 引用了不存在的目标 {g}"

    def test_requirement_realized_by_exist(self):
        """R 声明的"由哪些 F 实现"，那些 F 必须真实存在。"""
        for r in reg.REQUIREMENTS:
            for fid in r.realized_by:
                assert fid in reg.VERTICAL_ITEMS, (
                    f"{r.id} 声明由 {fid} 实现，但 {fid} 不存在"
                )
                assert isinstance(reg.VERTICAL_ITEMS[fid], reg.Feature), (
                    f"{r.id} 的 realized_by 引用了 {fid}，但它是需求不是功能点"
                )

    def test_feature_security_refs_exist(self):
        for f in reg.FEATURES:
            for s in f.security:
                assert s in reg.ALL_ITEMS, f"{f.id} 引用了不存在的安全要求 {s}"

    def test_module_prefix_consistent(self):
        """M5.F1 必须属于 M5，不能挂在别的模块下。"""
        for it in reg.VERTICAL_ITEMS.values():
            module_id = it.id.split(".")[0]
            assert module_id in reg.ALL_ITEMS, (
                f"{it.id} 的模块前缀 {module_id} 不存在"
            )

    def test_security_requirements_are_traceable(self):
        """每条安全要求都应能追到实现它的功能点。

        否则它就是一句口号 —— 写在文档里但没有代码承载。
        """
        implemented = {
            s for f in reg.FEATURES for s in f.security
        } | {
            s for r in reg.REQUIREMENTS for s in r.security
        }
        orphans = [s.id for s in reg.SECURITY if s.id not in implemented]
        assert not orphans, (
            f"以下安全要求没有任何功能点或需求声明实现它：{orphans}\n"
            f"它们目前只是口号 —— 要么补实现，要么说明为何不需要。"
        )


# ---------------------------------------------------------------------------
# 3. 覆盖完整性
# ---------------------------------------------------------------------------

class TestCoverage:
    def test_every_feature_is_covered(self, covers_map):
        """★ 每个功能点都必须至少被一个测试覆盖。"""
        missing = [
            f.id for f in reg.FEATURES if f.id not in covers_map
        ]
        assert not missing, (
            "以下功能点没有任何测试覆盖：\n  " + "\n  ".join(missing)
            + "\n\n要么补测试，要么说明为何暂不验证（并在此处登记豁免）。"
        )

    def test_every_requirement_is_covered(self, covers_map):
        """★ 每条模块需求都必须至少被一个测试覆盖。

        这是 R 存在的主要价值 —— 跨 F 的聚合验证必须真的存在。
        """
        missing = [
            r.id for r in reg.REQUIREMENTS if r.id not in covers_map
        ]
        assert not missing, (
            "以下模块需求没有任何测试覆盖：\n  " + "\n  ".join(missing)
        )

    def test_every_convention_is_covered(self, covers_map):
        for c in reg.CONVENTIONS:
            assert c.id in covers_map, f"代码准则 {c.id}（{c.name}）没有守护测试"

    def test_no_orphan_covers_reference(self, covers_map):
        """★ 测试里挂的编号必须真实存在——防止编号改名后留下死引用。"""
        orphans = sorted(k for k in covers_map if k not in reg.ALL_ITEMS)
        assert not orphans, (
            "以下编号被测试引用，但清单里不存在：\n  " + "\n  ".join(orphans)
            + "\n\n可能是编号已改名而测试未同步，或是拼写错误。"
        )

    def test_all_tests_declare_covers(self, undeclared):
        """★ 每个测试都必须声明它验证什么 —— 否则它不受追溯体系约束。"""
        assert not undeclared, (
            f"{len(undeclared)} 个测试没有声明 @covers：\n  "
            + "\n  ".join(undeclared[:12])
            + ("\n  …" if len(undeclared) > 12 else "")
            + "\n\n未声明覆盖的测试无法被追溯 —— 它可能重复验证别处已验的东西，"
              "也可能谁都不知道它在验什么。"
        )

    def test_high_priority_items_have_multiple_evidence(self, covers_map):
        """P0 的功能点应有不止一个测试——单一证据容易被一次误改掩盖。"""
        weak = [
            f.id for f in reg.FEATURES
            if f.priority == "P0" and len(covers_map.get(f.id, [])) < 2
        ]
        # 允许存在，但要显式记录数量，避免它悄悄变大
        assert len(weak) <= 20, (
            f"{len(weak)} 个 P0 功能点只有单一证据：{weak[:10]}\n"
            f"（本测试设了上限 20 以便观察趋势；超过了说明证据强度在退化）"
        )


# ---------------------------------------------------------------------------
# 4. 结构与完整性
# ---------------------------------------------------------------------------

class TestStructure:
    def test_modules_have_planes(self):
        valid = {"接入层", "控制面", "数据面", "管理面"}
        for m in reg.MODULES:
            assert m.plane in valid, f"{m.id} 的平面 {m.plane!r} 非法"

    def test_modules_have_status(self):
        valid = {"已实现", "部分实现", "未实现"}
        for m in reg.MODULES:
            assert m.status in valid, f"{m.id} 的状态 {m.status!r} 非法"

    def test_unimplemented_modules_have_no_features(self):
        """未实现的模块不该有功能点——有就说明状态标错了。"""
        for m in reg.MODULES:
            if m.status == "未实现":
                has = [f.id for f in reg.FEATURES if f.id.startswith(f"{m.id}.")]
                assert not has, (
                    f"{m.id} 标记为未实现，却定义了功能点 {has} —— 状态或编号有误"
                )

    def test_implemented_modules_have_features(self):
        """已实现的模块必须有功能点——否则追溯链断了。"""
        for m in reg.MODULES:
            if m.status in ("已实现", "部分实现"):
                has = [f.id for f in reg.FEATURES if f.id.startswith(f"{m.id}.")]
                assert has, f"{m.id}（{m.status}）没有任何功能点定义"

    def test_every_module_serves_a_goal(self):
        for m in reg.MODULES:
            assert m.goals, f"{m.id} 没有关联任何设计目标 —— 它为什么存在？"

    def test_goals_derive_modules(self):
        for g in reg.GOALS:
            assert g.derived_modules, f"{g.id} 没有派生任何模块"


# ---------------------------------------------------------------------------
# 5. 生成追溯矩阵
# ---------------------------------------------------------------------------

class TestGeneratedMatrix:
    def test_matrix_is_generated(self, covers_map):
        """生成 ``traceability/MATRIX.md``。

        这一个测试有副作用（写文件）——刻意如此。追溯表必须随测试一起更新，
        否则它会与代码脱节。
        """
        lines: list[str] = []
        w = lines.append

        w("# 追溯矩阵")
        w("")
        w("> **本文件由 `tests/test_traceability.py` 自动生成，请勿手工编辑。**")
        w("> 修改编号请改 `traceability/registry.py`，修改覆盖请给测试加 `@covers`。")
        w("")
        total_tests = len({t for v in covers_map.values() for t in v})
        s = reg.summary()
        w(f"共 **{total_tests}** 个测试方法（已声明覆盖）· "
          f"**{s['纵向编号项合计']}** 个纵向编号项"
          f"（{s['功能点']} 功能点 + {s['模块需求']} 模块需求）")
        w("")

        # ---- 目标 ----
        w("## 设计目标")
        w("")
        w("| 编号 | 目标 | 派生模块 |")
        w("|---|---|---|")
        for g in reg.GOALS:
            w(f"| **{g.id}** | {g.name} | {', '.join(g.derived_modules)} |")
        w("")

        # ---- 按模块展开 ----
        w("## 功能点与需求")
        for m in reg.MODULES:
            items = reg.items_of_module(m.id)
            w("")
            w(f"### {m.id} {m.name}")
            w("")
            w(f"平面 `{m.plane}` · 实现 `{m.impl}` · 状态 **{m.status}**")
            if not items:
                w("")
                w("_（未实现，暂无功能点）_")
                continue
            w("")
            w("| 编号 | 名称／约束 | 实现位置 | G | P | S | 覆盖测试 |")
            w("|---|---|---|---|---|---|---|")
            for it in items:
                if isinstance(it, reg.Feature):
                    name, impl = it.name, f"`{it.impl}`"
                else:
                    name = it.statement
                    impl = "（由 " + ", ".join(it.realized_by) + " 实现）"
                tests = covers_map.get(it.id, [])
                tn = f"{len(tests)}" if tests else "**0**"
                w(f"| **{it.id}** | {name} | {impl} | "
                  f"{','.join(it.goals)} | {it.priority} | "
                  f"{','.join(it.security) or '—'} | {tn} |")
        w("")

        # ---- 横切 ----
        w("## 代码准则")
        w("")
        w("| 编号 | 准则 | 守护测试类 | 覆盖 |")
        w("|---|---|---|---|")
        for c in reg.CONVENTIONS:
            n = len(covers_map.get(c.id, []))
            w(f"| **{c.id}** | {c.name} | `{c.test_ref}` | {n} |")
        w("")

        w("## 明确不做")
        w("")
        w("| 编号 | 不做 | 理由 | 守护 |")
        w("|---|---|---|---|")
        for n_ in reg.NOT_DOING:
            g = f"`{n_.guarded_by}`" if n_.guarded_by else "**无**（仅靠人工）"
            w(f"| **{n_.id}** | {n_.what} | {n_.why} | {g} |")
        w("")

        w("## 安全要求")
        w("")
        w("| 编号 | 要求 | 承载它的功能点／需求 |")
        w("|---|---|---|")
        for sec in reg.SECURITY:
            carriers = [it.id for it in reg.VERTICAL_ITEMS.values()
                        if sec.id in it.security]
            w(f"| **{sec.id}** | {sec.statement} | {', '.join(carriers) or '—'} |")
        w("")

        # ---- 未实现模块 ----
        w("## 尚未实现的模块")
        w("")
        for mid in reg.modules_without_features():
            m = next(x for x in reg.MODULES if x.id == mid)
            w(f"- **{m.id}** {m.name} —— {m.impl}")
        w("")
        w("> 这些模块暂无功能点，因此不参与覆盖统计。")
        w("> 实现时应先在 `registry.py` 中登记功能点，再写测试并挂 `@covers`。")
        w("")

        MATRIX_PATH.parent.mkdir(parents=True, exist_ok=True)
        MATRIX_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # 生成结果自校验：文件必须真的写出来了且非空
        assert MATRIX_PATH.exists()
        assert MATRIX_PATH.stat().st_size > 500, "生成的追溯矩阵过小，可能生成失败"
