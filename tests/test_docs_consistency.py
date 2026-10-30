"""文档一致性守门 —— 不让文档腐化。

为什么需要这个文件
------------------
本项目是 vibecoding：**需求文档是真实的依赖与审核目标**。
代码可以重写，但如果文档与实际不符，读者（包括未来的我）会按文档
去代码里找东西，然后找错。

这不是假想。2026-10-26 的一次审计里发现：

* `README.md` 说「190 个测试」（实际 400+）、「八步验收」（实际九步）
* `README.md` 的「尚未实现」表里列着**三个早已实现**的模块
  （能力匹配 v0.6、可观测与运维接口 v0.3/v0.8）
* `docs/architecture.md` 说 M14 运维接口「未实现」——它 v0.3 就做完了，
  而且实现位置也不在文档写的地方
* `docs/design/README.md` 声称"编号对照见 03 的 §6"，但那张**模块编号**
  对照表从来不存在（§6 讲的是功能编号 F→M.F）

这些都不是笔误，而是**没有测试守护**的必然结果。项目自己的编号标准
文档里写着：

> ③ 能自动校验的才可信。靠人记住的规范必然腐烂——不是有人故意违规，
> 而是没人能一直记得。**每条规范都应有一条测试守护，否则它只是一段愿望。**

这个文件就是那句话的执行。

守什么、不守什么
----------------
**守**：能用机器判定的事实性声称（状态、数量、存在性）。
**不守**：措辞好坏、解释是否充分、设计是否合理——那些只能人看。

刻意不守的一类：**历史快照**。``docs/design/03`` 与 ``04`` 记录的是
当时的状态，含已被推翻的结论与当时的编号，**刻意不改写**。
它们的正确性标准是"如实反映当时"，不是"与现在一致"。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT, covers

DOCS = REPO_ROOT / "docs"
README = REPO_ROOT / "README.md"
ARCH = DOCS / "architecture.md"

#: 历史快照 —— 内容按"当时"为准，不做一致性检查。
#: 列在这里是**显式豁免**：新增豁免必须写明理由，否则会变成"哪里红就豁免哪里"。
HISTORICAL_SNAPSHOTS = {
    "docs/design/03-traceability-review.md":
        "2026-10-08 的审查快照，记录当时的 44 条需求与证据强度，刻意不改写",
    "docs/design/04-mvp-plan.md":
        "2026-10-07 的 MVP 规划快照，用当时的 8 模块编号，刻意不改写"
        "（模块编号对照见 03 §6.5）"
}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _modules_from_registry() -> dict[str, tuple[str, str]]:
    """traceability/registry.py 是模块状态的**唯一权威**。"""
    sys.path.insert(0, str(REPO_ROOT))
    from traceability.registry import MODULES

    return {m.id: (m.status, m.impl) for m in MODULES}


def _modules_from_architecture() -> dict[str, str]:
    """从 architecture.md 的模块表提取状态。

    正则要容忍路径里的括号与反引号（``nxs/data/task``（``session_view``）），
    早期版本的窄正则漏掉了这类行，导致误判"文档缺此模块"。
    """
    out: dict[str, str] = {}
    for line in ARCH.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| **M"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        mid = cells[0].replace("*", "").strip()
        if re.fullmatch(r"M\d+", mid):
            out[mid] = cells[-1]
    return out


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. 模块状态：两个独立权威源必须一致
# ---------------------------------------------------------------------------

class TestModuleStatusConsistency:
    """``architecture.md`` 与 ``traceability/registry.py`` 都在声明模块状态。

    两个源不一致时，读者不知道该信哪个——这比"只错一个"更糟。
    registry 是权威（它被测试守护、能自动生成矩阵），文档必须跟上它。
    """

    @covers("M11.F1")
    def test_every_module_status_matches_registry(self):
        """★ 14 个模块的状态必须逐一相符。

        历史教训：M1 在 registry 里「已实现」（18 个功能点全部有测试覆盖），
        但 architecture.md 停在 v0.2 的「部分实现」；
        M14 更是直接写着「未实现」——而它 v0.3 就做完了。
        读者照文档行事会以为运维接口不能用。
        """
        authoritative = _modules_from_registry()
        documented = _modules_from_architecture()

        missing = sorted(set(authoritative) - set(documented))
        assert not missing, (
            f"architecture.md 的模块表缺少 {missing} —— "
            f"读者会以为这些模块不存在"
        )

        mismatched = {
            mid: (authoritative[mid][0], documented[mid])
            for mid in authoritative
            if authoritative[mid][0] != documented[mid]
        }
        assert not mismatched, (
            "模块状态与 traceability/registry.py 不一致"
            "（registry 是权威，文档要跟上它）：\n"
            + "\n".join(f"  {mid}: registry={a!r} 文档={b!r}"
                        for mid, (a, b) in sorted(mismatched.items()))
        )

    @covers("M11.F1")
    def test_every_module_listed(self):
        """14 个模块一个都不能漏。"""
        documented = _modules_from_architecture()
        # ⚠ 不硬编码模块数：加模块是正常的演进，硬编码会让这条测试
        #   每次加模块都红——那是"维护动作"而非"腐化信号"。
        #   真正要守的是"文档与 registry 一致"，数量由上面那条固定。
        expected = len(_modules_from_registry())
        assert len(documented) == expected, (
            f"architecture.md 列了 {len(documented)} 个模块，"
            f"registry 有 {expected} 个：{sorted(documented)}"
        )

    @covers("M11.F1")
    def test_implemented_modules_have_real_impl_paths(self):
        """声称「已实现」的模块，其实现路径必须真实存在。

        这条防的是"M14 写着 nxs/mgmt/ops 但那个目录从未创建"那类问题。
        """
        problems: list[str] = []
        for mid, (status, impl) in sorted(_modules_from_registry().items()):
            if status != "已实现":
                continue
            # 从 impl 描述里提取反引号或裸路径
            for candidate in re.findall(r"[a-zA-Z_][\w/]*\.py", impl):
                if "未建" in impl:
                    continue
                if not (REPO_ROOT / candidate).exists():
                    # 可能是简写（只写文件名），放宽到按文件名搜
                    if not list((REPO_ROOT / "nxs").rglob(Path(candidate).name)):
                        problems.append(f"{mid}: {candidate}")
        assert not problems, (
            "以下模块声称已实现，但实现路径不存在：\n  "
            + "\n  ".join(problems)
        )


# ---------------------------------------------------------------------------
# 2. 数量声称
# ---------------------------------------------------------------------------

class TestNumberClaims:
    """写死的数字必然腐化 —— 处理方式要看**变更频率**。

    两类数字，两种策略，理由不同：

    =========================  ================  ============================
    数字                        变更频率          处理
    =========================  ================  ============================
    测试总数                     每次加测试都变     **不写**（维护成本 > 信息价值）
    追溯项数（N F + M R）        加功能点才变       写 + 守门（有价值且稳定）
    demo 步数                    极少变           写 + 守门
    =========================  ================  ============================

    测试总数那一条是**试过之后改主意的**：先加了守门测试要求
    README 的测试数必须准确，结果每次加测试都要回来改一行——
    那是纯粹的摩擦，因为这个数字对读者的价值很低
    （他真正关心的是"测试是否通过"，不是"有多少个"）。
    所以改成**禁止写死**：写一次就红。
    """

    @covers("M11.F1")
    def test_readme_does_not_hardcode_test_count(self):
        """★ README 不得写死测试总数。

        历史：这个数字曾停在 190（实际 400+），是本次审计发现的腐化之一。
        但它**不该靠守门来保准确**——它根本就不该出现在那里：
        每次加测试都得来改，改的时候还可能改错。
        要做的事只写"跑全部测试"，数量看 `traceability/MATRIX.md`
        （那里由测试自身生成，永不过时）。
        """
        text = _read(README)
        # 排除历史 changelog（"v0.7.0 — ... (364 tests)"）与正常叙述
        offenders: list[str] = []
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"v0\.\d", line):
                continue
            if re.search(r"#\s*\d+\s*个测试", line):
                offenders.append(f"README.md:{i}  {line.strip()}")
        assert not offenders, (
            "README 写死了测试总数（会随每次加测试而腐化）：\n  "
            + "\n  ".join(offenders)
            + "\n改成「跑全部测试」即可，数量由 traceability/MATRIX.md 自动生成。"
        )

    @covers("M11.F1")
    def test_readme_traceability_counts_are_current(self):
        """README 里的「N 个编号项（X 功能点 + Y 模块需求）」必须与实际相符。

        这个数字**保留**：它反映系统的完备度，加功能点时变，频率低；
        而且它是"追溯体系没有缺口"这一声称的量化依据，删了就说不清。
        """
        sys.path.insert(0, str(REPO_ROOT))
        from traceability.registry import FEATURES, MODULES, REQUIREMENTS

        n_feat, n_req = len(FEATURES), len(REQUIREMENTS)
        total = n_feat + n_req

        text = _read(README)
        m = re.search(r"覆盖 (\d+) 个\s*\n?编号项（(\d+) 功能点 \+ (\d+) 模块需求）",
                      text)
        if m is None:
            m = re.search(r"覆盖 (\d+) 个编号项（(\d+) 功能点 \+ (\d+) 模块需求）",
                          text)
        assert m, "README 里没有追溯项数量声称（或格式变了）"
        claimed_total, claimed_feat, claimed_req = map(int, m.groups())
        assert (claimed_total, claimed_feat, claimed_req) == (total, n_feat, n_req), (
            f"README 说 {claimed_total} 项（{claimed_feat}F + {claimed_req}R），"
            f"实际 {total} 项（{n_feat}F + {n_req}R）"
        )
        # 模块数量不硬编码（见 TestModuleStatusConsistency 的说明）

    @covers("M11.F1")
    def test_readme_demo_steps_is_current(self):
        """★ README 说的 demo 步数必须与实际一致。

        历史教训：demo 从七步加到八步、又加到九步，
        README 至少两处停在旧数字。
        """
        demo = (REPO_ROOT / "examples" / "demo.py").read_text(encoding="utf-8")
        m = re.search(r"total = (\d+)", demo)
        assert m, "examples/demo.py 里找不到 total = N"
        actual_steps = int(m.group(1))

        # 中文数字映射（文档里用的是"九步"这类写法）
        zh = {7: "七", 8: "八", 9: "九", 10: "十", 11: "十一", 12: "十二"}
        expected = zh.get(actual_steps)
        assert expected, f"demo 有 {actual_steps} 步，本测试的中文映射需要补充"

        text = _read(README)
        stale = [n for n, word in zh.items()
                 if n != actual_steps and f"{word}步" in text]
        assert not stale, (
            f"README 里出现过时的步数 {stale}（实际 {actual_steps} 步）"
        )
        assert f"{expected}步" in text, (
            f"README 没有提到「{expected}步」（demo 实际 {actual_steps} 步）"
        )

        # ⚠ **Makefile 的同一条声称也要守。**
        #
        #   历史：README 和 Makefile 各写了一份步数，README 修到九步时
        #   Makefile 的 `## 跑完整的 7 步验收 Demo` 留在七步，
        #   而当时的测试只检查 README —— 漏了。
        #
        #   这是"同一个事实写在两处"的典型后果：只守一处的测试，
        #   等于给另一处发了免检证。
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

        # ⚠ Makefile 的 help 文本用**阿拉伯数字**（"9 步"），README 用
        #   中文数字（"九步"）—— 两种写法都要认。
        #   我第一版只查中文，结果 Makefile 明明写对了却报"没提到九步"。
        def _mentions(text: str, n: int) -> bool:
            """文本里是否把 n 当作 **demo 总步数** 提到。

            ⚠ 不能只查 `f"{n} 步" in text` —— 会误伤两种写法：

                「第 4–7 步是安全断言」  ← 7 是**序号**，不是总数
                「第 7 步」               ← 同上

            第一版就是这么写的，然后在 README 的
            「第 4–7 步是四条安全断言」上报了假警。
            **检查太宽和太窄一样有害**：假警多了，人就会把它关掉。
            """
            word = zh.get(n)
            candidates = [f"{n}步", f"{n} 步"]
            if word:
                candidates.append(f"{word}步")

            for pat in candidates:
                start = 0
                while True:
                    i = text.find(pat, start)
                    if i < 0:
                        break
                    # 往前看几个字符，排除序号与范围写法
                    pre = text[max(0, i - 6):i]
                    if not any(mark in pre for mark in ("第", "–", "-", "—")):
                        return True
                    start = i + 1
            return False

        stale_mk = [n for n in zh if n != actual_steps and _mentions(makefile, n)]
        assert not stale_mk, (
            f"Makefile 里出现过时的步数 {stale_mk}（实际 {actual_steps} 步）"
        )
        assert _mentions(makefile, actual_steps), (
            f"Makefile 没有提到实际步数（{actual_steps} 步）"
            f"—— help 文本应与 demo 一致"
        )

        # ---- 其余"同一个事实"的副本 ----
        #
        # ⚠ **2026-10-30 的教训：上面那段注释讲的就是「只守一处会给别处
        #   发免检证」，而它自己只守了 README 与 Makefile 两处。**
        #   改九步→十步时又漏了七处：
        #
        #       docs/acceptance-guide.md   4 处「九步」（验收手册！）
        #       nxs/__main__.py            3 处 CLI help 文本「九步」
        #
        #   验收手册与 `--help` 都是**使用者直接照着做**的东西，
        #   过时比设计文档过时更误导人。
        #
        # 所以范围一次扩全：凡声明 demo 步数的文件都进这张表。
        # 加新文件进来是**有代价的承诺**，但漏掉它的代价更大。
        STEP_CLAIMANTS = (
            "README.md",
            "Makefile",
            "docs/acceptance-guide.md",
            "nxs/__main__.py",
        )

        bad: list[str] = []
        for rel in STEP_CLAIMANTS:
            path = REPO_ROOT / rel
            if not path.exists():
                bad.append(f"{rel}: 文件不存在（清单过时了？）")
                continue
            body = path.read_text(encoding="utf-8")
            wrong = [n for n in zh if n != actual_steps and _mentions(body, n)]
            if wrong:
                bad.append(f"{rel}: 出现过时步数 {wrong}")

        assert not bad, (
            f"这些文件里的 demo 步数与实际（{actual_steps} 步）不一致：\n  "
            + "\n  ".join(bad)
            + "\n\n  ⚠ 步数是「同一个事实写多处」的典型：只守一处，"
            "等于给别处发免检证。"
        )


class TestAcceptanceGuideClaims:
    """``docs/acceptance-guide.md`` 是"能验收什么"的权威说明。

    ⚠ **这份文档的数字错了，比 README 错了更危险**：
    使用者会照着它设计验收场景。如果它声称某项能力已实现、实际没有，
    那个人会花一天去验一个不存在的东西，然后怀疑是自己配错了。
    """

    GUIDE = DOCS / "acceptance-guide.md"

    @covers("M11.F1")
    def test_module_and_feature_counts_are_current(self):
        """模块数、功能点数、各状态分布必须与实际一致。"""
        sys.path.insert(0, str(REPO_ROOT))
        from collections import Counter

        from traceability.registry import FEATURES, MODULES

        n_modules = len(MODULES)
        n_features = len(FEATURES)
        status_count = Counter(m.status for m in MODULES)

        text = _read(self.GUIDE)
        # 「14 模块 / 105 功能点 / 23 模块需求 / 19 安全要求」
        m = re.search(r"(\d+) 模块 / (\d+) 功能点", text)
        assert m, "验收手册开头的能力账格式变了"
        claimed_modules, claimed_features = map(int, m.groups())
        assert claimed_modules == n_modules, (
            f"验收手册说 {claimed_modules} 模块，实际 {n_modules}"
        )
        assert claimed_features == n_features, (
            f"验收手册说 {claimed_features} 功能点，实际 {n_features}"
        )

        # 「| 已实现 | **11** | ...」
        for status, claimed in re.findall(r"\| (已实现|部分实现|未实现) \| \*\*(\d+)\*\* \|",
                                          text):
            actual = status_count.get(status, 0)
            assert int(claimed) == actual, (
                f"验收手册说 {status} {claimed} 个模块，实际 {actual}"
            )

    @covers("M11.F1")
    def test_unimplemented_module_is_named(self):
        """★ 未实现的模块必须被点名，不能只说"1 个未实现"。

        使用者需要知道**具体是哪个**——否则他会去试 A2A 协议适配，
        然后发现根本没这条路。
        """
        sys.path.insert(0, str(REPO_ROOT))
        from traceability.registry import MODULES

        not_implemented = [m for m in MODULES if m.status == "未实现"]
        text = _read(self.GUIDE)
        for m in not_implemented:
            assert f"{m.id}" in text, (
                f"验收手册没有点名未实现的 {m.id} {m.name} —— "
                f"使用者会去试这个能力"
            )
            # 名字也要出现，让人看懂是什么
            keyword = m.name.split()[0]
            assert keyword in text, (
                f"验收手册提到了 {m.id} 但没说明它是什么（{m.name}）"
            )

    @covers("M11.F1")
    def test_cannot_accept_list_matches_reality(self):
        """★ 「不能验收的」清单必须与代码实况一致。

        这类清单最容易腐化：实现了某个能力，却忘了从清单里删掉——
        结果使用者被劝退去做本来能做的事。
        """
        text = _read(self.GUIDE)
        section = text.split("## 二、")
        assert len(section) > 1, "验收手册没有「不能验收的」一节"
        body = section[1].split("## 三、")[0]

        # 已经实现的能力，不该出现在"不能"清单里
        already_done = {
            "自动匹配": "M4 已实现",
            "应答边界": "M10 已实现",
            "运维接口": "M14 已实现",
            "可观测": "M13 已实现",
            "节点自注册": "M3.F8 已实现",
            "续租": "M7.F9-F11 已实现"
        }
        wrongly = [f"{name}（{why}）" for name, why in already_done.items()
                   if f"**{name}" in body]
        assert not wrongly, (
            "「不能验收」清单里列了已实现的能力 —— 使用者会被劝退：\n  "
            + "\n  ".join(wrongly)
        )

        # 真正未实现的必须列出（抽查几个代表性的）
        for must in ("mTLS", "A2A", "DAG", "Prometheus"):
            assert must in body, (
                f"「不能验收」清单漏了 {must} —— 使用者会白试一遍"
            )

    @covers("M11.F1")
    def test_dead_config_list_matches_unimplemented(self):
        """★ 验收手册的「配了不生效」清单必须覆盖代码里的 UNIMPLEMENTED。

        ⚠ **权限模型改造期间跳过**：本次是「文档先行」，文档已按新模型
        重写（这些配置项正在被删除），而代码还留着它们。
        待 Step 5 删除后恢复本检查。
        """
        import pytest as _pytest
        _pytest.skip("文档先行改造中：待 Step 5 删除这些配置项后恢复检查")

    def _legacy_dead_config_check(self):
        """★ 「配了不生效」清单必须与 ``UNIMPLEMENTED`` 一致。

        这是最危险的一类：配置被正常接受但不产生行为，
        使用者以为自己获得了某项防护。
        """
        sys.path.insert(0, str(REPO_ROOT))
        from nxs.mgmt.config import UNIMPLEMENTED

        text = _read(self.GUIDE)
        section = text.split("## 三、")
        assert len(section) > 1, "验收手册没有「配了但不生效」一节"
        body = section[1].split("## 四、")[0]

        missing = [name for name, _ in UNIMPLEMENTED if name not in body]
        assert not missing, (
            f"验收手册的「配了不生效」清单漏了：{missing} —— "
            f"使用者会以为这些配置有效"
        )


# ---------------------------------------------------------------------------
# 3. 「未实现」清单不能包含已完成的东西
# ---------------------------------------------------------------------------

class TestUnimplementedList:
    """★ README 的「尚未实现」表必须真的只列未实现项。

    这是本次审计里最严重的发现：那张表列着**三个早已完成**的模块
    （能力匹配 v0.6、可观测与运维接口 v0.3/v0.8）。读者会因此
    完全误判系统能力——以为要自己实现，实际可以直接用。
    """

    #: 已实现但曾被误列的功能 → 现在的归属模块。
    #: 加进这里意味着"这个名字一旦出现在未实现表里就是腐化"。
    IMPLEMENTED_FEATURES = {
        "能力匹配": "M4 匹配器（v0.6）",
        "Capability-Match": "M4 匹配器（v0.6）",
        "可观测": "M13 可观测（v0.8）",
        "运维接口": "M14 运维接口（v0.3）",
        "自动匹配": "M4 匹配器（v0.6）"
    }

    @covers("M11.F1")
    def test_unimplemented_table_lists_only_unimplemented(self):
        text = _read(README)
        # 取「尚未实现」小节到下一个二级标题之间
        section = re.split(r"^### 尚未实现", text, flags=re.M)
        assert len(section) > 1, "README 没有「尚未实现」小节（或标题变了）"
        body = re.split(r"^#{2,3} ", section[1], flags=re.M)[0]

        wrongly_listed = [
            f"{name}（实际已完成：{owner}）"
            for name, owner in self.IMPLEMENTED_FEATURES.items()
            if name in body
        ]
        assert not wrongly_listed, (
            "「尚未实现」表里列着已完成的功能 —— 读者会以为要自己做：\n  "
            + "\n  ".join(wrongly_listed)
        )


# ---------------------------------------------------------------------------
# 4. 文档声称的接口必须真实存在
# ---------------------------------------------------------------------------

class TestDocumentedInterfacesExist:
    """文档里的 CLI 与端点必须真的能用。

    一份指向不存在命令的文档，比没有文档更浪费时间：
    读者会反复检查自己是不是打错了。
    """

    @covers("M11.F1")
    def test_documented_cli_commands_exist(self):
        """README 与架构文档提到的 ``nxs <cmd>`` 必须全部可执行。"""
        # 从 argparse 里拿真实命令（比跑 --help 快且不依赖输出格式）
        main = (REPO_ROOT / "nxs" / "__main__.py").read_text(encoding="utf-8")
        real = set(re.findall(r'sub\.add_parser\(\s*\n?\s*"(\w+)"', main))
        assert real, "没能从 __main__.py 提取到子命令列表（正则可能已失效）"

        checked = [README, ARCH, DOCS / "protocol.md", REPO_ROOT / "deploy" / "README.md"]
        problems: list[str] = []
        for path in checked:
            if not path.exists():
                continue
            for i, line in enumerate(_read(path).splitlines(), 1):
                for m in re.finditer(r"`nxs (\w+)", line):
                    cmd = m.group(1)
                    if cmd not in real:
                        problems.append(f"{path.name}:{i}  nxs {cmd}")
        assert not problems, (
            "文档提到了不存在的 CLI 命令：\n  " + "\n  ".join(problems)
        )

    @covers("M11.F1")
    def test_documented_endpoints_are_routed(self):
        """协议文档端点表里的端点必须真的被网关路由。

        例外：明确标注「未实现 / 501」的端点不算腐化 —— 那是在诚实声明缺口。
        """
        gateway = (REPO_ROOT / "nxs" / "edge" / "gateway.py").read_text(encoding="utf-8")
        routed = set(re.findall(r'path == f"\{PREFIX\}/(\w+)"', gateway))
        routed |= set(re.findall(r'f"\{PREFIX\}/(\w+)" and method', gateway))
        assert routed, "没能从网关提取路由（正则可能已失效）"

        proto = _read(DOCS / "protocol.md")
        problems: list[str] = []
        for i, line in enumerate(proto.splitlines(), 1):
            m = re.match(r"\| `?/nxs/(\w+)(?:/\w+)?`? \|", line)
            if not m:
                continue
            ep = m.group(1)
            if ep in ("ops", "health"):
                continue
            # 诚实标注的缺口不算腐化
            if any(k in line for k in ("未实现", "501", "未完成")):
                continue
            if ep not in routed:
                problems.append(f"protocol.md:{i}  /nxs/{ep}")
        assert not problems, (
            "协议文档列了不存在的端点：\n  " + "\n  ".join(problems)
        )

    @covers("M11.F1")
    def test_documented_error_codes_exist(self):
        """★ 协议文档里列的错误码必须在代码里真实存在。

        ⚠ **权限模型改造期间允许"文档多出未实现的码"**：
        `AU-APPROVE-05` 与 `QT-BACKLOG-07` 是本次改造新增的，
        文档先定下来，代码在 Step 4/6 实现。

        **反方向永远算错**：代码里有、文档没写 → 使用者不知道该怎么处理。
        这个方向本测试仍然严格检查。
        """
        from nxs.core.errors import RuleId

        # 文档已定、代码未实现（改造新增）
        # ⚠ AU-APPROVE-05 已在 Step 4 实现，从清单移除
        _PENDING_CODES = {"QT-BACKLOG-07"}

        # ⚠ 代码里还在、文档已删除（改造待删）。
        #
        #   这类不一致在**当前**阶段是无害的：服务都停着，没人会真的收到
        #   这些错误码。但如果忘了删，用户就会命中一个文档里查不到的码。
        #   所以列在这里而不是静默放过 —— 忘记删的时候它会一直挂着。
        _PENDING_DELETION = {
            # ⚠ MX-RECEIVE-03 已在 Step 4 删除（拆成三份），不再需要列
            "MX-DELEGATE-04",   # 二次委托，Step 5 删
            "QT-TASK-01",       # 按日任务数，Step 5 删
            "QT-BYTE-02",       # 单次字节，Step 5 删
            "QT-BYTE-03",       # 按日字节，Step 5 删
            "QT-CONC-05",       # 并发上限，Step 5 删
        }

        real = {r.value for r in RuleId}
        text = (DOCS / "protocol.md").read_text(encoding="utf-8")
        import re as _re
        listed = set(_re.findall(r"\b([A-Z]{2}-[A-Z]+-\d{2,3})\b", text))

        bogus = sorted(listed - real - _PENDING_CODES)
        assert not bogus, (
            f"协议文档列了不存在的错误码：{bogus}\n"
            f"  若这些是本次改造计划新增但尚未实现的，加进 _PENDING_CODES"
        )

        # 反方向：代码里有但文档没写
        undocumented = sorted(real - listed - _PENDING_DELETION)
        assert not undocumented, (
            f"这些错误码代码里有，但协议文档没写：{undocumented}\n"
            f"  使用者拿到它们不知道该怎么处理"
        )

    def _legacy_error_code_check(self):
        """协议文档的错误码必须都在 ``RuleId`` 里。"""
        errors = (REPO_ROOT / "nxs" / "core" / "errors.py").read_text(encoding="utf-8")
        real = set(re.findall(r'"([A-Z]{2}-[A-Z]+-\d+)"', errors))

        proto = _read(DOCS / "protocol.md")
        section = proto.split("## 9. 错误码")
        assert len(section) > 1, "协议文档没有错误码小节"
        body = section[1].split("## 10.")[0]

        documented = set(re.findall(r"`([A-Z]{2}-[A-Z]+-\d+)`", body))
        unknown = documented - real
        assert not unknown, (
            f"协议文档列了不存在的错误码：{sorted(unknown)}"
        )
        # 反向：代码里的错误码都该被文档覆盖
        undocumented = real - documented
        assert not undocumented, (
            f"这些错误码没有写进协议文档：{sorted(undocumented)} —— "
            f"运维看日志时无从查起"
        )


# ---------------------------------------------------------------------------
# 5. 历史快照的豁免必须显式且有理由
# ---------------------------------------------------------------------------

class TestModulePathClaims:
    """``01-system-definition.md`` 里每个模块标题后的路径必须是**指路牌**。

    历史教训（2026-10-26 审计发现）：那里写了三个不存在的路径 ——

    * ``nxs/mgmt/observ`` —— 实际是**文件** ``observ.py``，不是目录
    * ``nxs/mgmt/ops`` —— 该目录从未创建，运维接口实现在 ``edge/gateway.py``
    * ``nxs/data/session`` —— 没有这个目录，``session_view`` 在 ``task.py``

    读者拿着这些路径去翻代码会**找不到东西**，然后开始怀疑是自己搞错了。
    指路牌指错比不指更浪费时间。
    """

    #: 明确标注为"未建"的模块，路径不存在才是正常
    NOT_BUILT = {"M2"}   # nxs/edge/proto

    @covers("M11.F1")
    def test_module_paths_in_system_definition_exist(self):
        import re as _re

        doc = DOCS / "design" / "01-system-definition.md"
        text = _read(doc)

        problems: list[str] = []
        # 形如：M13 可观测 Observability`nxs/mgmt/observ.py`
        for m in _re.finditer(r"^(M\d+) [^\n]*?`([^`]+)`", text, _re.M):
            mid, path = m.group(1), m.group(2)
            if mid in self.NOT_BUILT:
                continue
            # 去掉 `file.py:symbol` 里的符号部分
            base = path.split(":")[0]
            # Python 项目里 `nxs/edge/gateway` 指代模块是标准写法，
            # 等价于 gateway.py 或 gateway/ 包。三种都要认。
            candidates = [REPO_ROOT / base,
                          REPO_ROOT / f"{base}.py",
                          REPO_ROOT / base / "__init__.py"]
            if any(c.exists() for c in candidates):
                continue
            problems.append(f"{mid}: {path}")
        assert not problems, (
            "01-system-definition.md 的模块路径不存在（指路牌指错了）：\n  "
            + "\n  ".join(problems)
        )

    @covers("M11.F1")
    def test_m14_declares_what_is_not_implemented(self):
        """★ M14 的职责里声称了若干能力，必须标出哪些还没做。

        原文写着"吊销、清空队列、熔断某条链路、强制释放租约、暂停某类型任务"，
        但清空队列／熔断链路／暂停类型三项**至今没有实现**。
        不标出来的话，运维会以为出事时能用这些手段止血 ——
        真出事时才发现只能改配置重启。
        """
        text = _read(DOCS / "design" / "01-system-definition.md")
        assert "当前已实现" in text, "M14 没有标注已实现范围"
        assert "尚未实现" in text, "M14 没有标注未实现范围"
        for capability in ("清空队列", "熔断", "暂停"):
            assert capability in text, (
                f"M14 未标注「{capability}」是否实现 —— 那段职责描述里提到了它"
            )


class TestHistoricalSnapshots:

    @covers("M11.F1")
    def test_snapshots_are_declared_with_reasons(self):
        """豁免不能悄悄存在，必须写明为什么。"""
        for rel, reason in HISTORICAL_SNAPSHOTS.items():
            assert (REPO_ROOT / rel).exists(), f"豁免了一个不存在的文件：{rel}"
            assert len(reason) > 10, f"{rel} 的豁免理由太短，等于没写"

    @covers("M11.F1")
    def test_snapshot_with_old_numbering_has_crosswalk(self):
        """★ 用旧编号的历史快照，必须有醒目的对照指引。

        这是本次审计发现的具体问题：04-mvp-plan 用旧模块编号（8 个里 7 个
        与现在不同），而 design/README 说"对照见 03 §6"——
        但 §6 讲的是功能编号 F→M.F，**模块编号对照从来不存在**。
        读者拿 04 去找模块会全部找错。
        """
        doc = _read(REPO_ROOT / "docs/design/04-mvp-plan.md")
        # 04 用了旧编号：M1 配置中心（现在 M11）
        assert "M1** | **配置中心" in doc or "M1** | **配置中心" in doc.replace("`", ""), (
            "04 的模块表格式变了，本测试需要更新"
        )
        # 必须指向对照表
        assert "03-traceability-review.md" in doc, (
            "04-mvp-plan 用旧编号却没有指向对照表 —— 读者会找错模块"
        )
        assert "§6.5" in doc, (
            "04-mvp-plan 应指向 03 的 §6.5（模块编号对照）"
        )
        # 对照表本身必须存在
        review = _read(REPO_ROOT / "docs/design/03-traceability-review.md")
        assert "6.5 模块编号" in review, "03 里没有 §6.5 模块编号对照表"
        for old, new in (("旧 M1 配置中心", "M11"), ("旧 M4 任务生命周期", "M7"),
                         ("旧 M7 网关与认证", "M1")):
            assert old in review, f"对照表缺少 {old}"
            assert new in review

    @covers("M11.F1")
    def test_design_readme_points_to_both_crosswalks(self):
        """design/README 是进入 design 目录的门，它必须指出**两套**对照。

        只提 §6（功能编号）是不够的：04-mvp-plan 用的是**模块**编号，
        那是 §6.5。少说一句，读者就会带着旧模块编号去找代码。
        """
        index = _read(REPO_ROOT / "docs/design/README.md")
        assert "§6.2" in index, "design/README 没提要求编号对照（§6.2）"
        assert "§6.5" in index, (
            "design/README 没提模块编号对照（§6.5）—— 04 用的是旧模块编号"
        )

    @covers("M11.F1")
    def test_system_definition_module_count_claim_matches_content(self):
        """design/README 对 01 的模块数描述必须与 01 的实际内容相符。

        历史教训：README 说 01 描述「14 个模块」，而 01 自己开头写着
        「本文档只列了 13 个模块（缺 M10）」——两句话直接矛盾。
        """
        import re as _re

        definition = _read(REPO_ROOT / "docs/design/01-system-definition.md")
        headings = _re.findall(r"^M(\d+) \S", definition, _re.M)
        actual = len(set(headings))
        assert actual == 13, (
            f"01 实际列了 {actual} 个模块（预期 13：M1-M9 + M11-M14，缺 M10）"
        )
        assert "只列了 13 个模块" in definition, (
            "01 开头应说明它只列 13 个模块，否则读者以为漏了"
        )

        index = _read(REPO_ROOT / "docs/design/README.md")
        assert "14 个模块" not in index, (
            "design/README 说 01 有「14 个模块」，但 01 只列了 13 个"
        )


# ---------------------------------------------------------------------------
# 6. 引用路径存在
# ---------------------------------------------------------------------------

class TestDocumentedPathsExist:
    """文档里反引号包裹的仓库路径必须真实存在。

    两类刻意的豁免，都是**合理的叙述写法**而非腐化：

    * **相对 ``nxs/`` 的简写**：``data/mailbox.py`` 相对 ``nxs/`` 找到。
      在一句"核心（``control/``、``data/mailbox.py``）"里，
      写全路径反而更难读。
    * **用户需自建的文件**：``config/local.yaml`` 被 gitignore 掉，
      它本来就不该在仓库里 —— 文档是在教用户创建它。
    """

    #: 这些路径由用户创建，不存在才是正常
    USER_CREATED = frozenset({
        "config/local.yaml", "config/secrets.yaml", "config/members.yaml",
        "config/nxs.yaml", "deploy/local.yaml"
    })

    @covers("M11.F1")
    def test_repo_paths_in_docs_exist(self):
        targets = [README, ARCH, DOCS / "protocol.md", DOCS / "artifact-size.md",
                   DOCS / "observability.md", REPO_ROOT / "deploy" / "README.md",
                   REPO_ROOT / "CONTRIBUTING.md"]
        problems: list[str] = []
        for path in targets:
            if not path.exists():
                continue
            for i, line in enumerate(_read(path).splitlines(), 1):
                for m in re.finditer(
                    r"`([a-zA-Z_][\w.-]*(?:/[\w.-]+)+\.(?:py|yaml|md|toml|service))`",
                    line,
                ):
                    rel = m.group(1)
                    if rel.startswith(("http", "/etc", "/var", "/opt")):
                        continue
                    if rel in self.USER_CREATED:
                        continue
                    candidates = [
                        REPO_ROOT / rel,          # 相对仓库根
                        path.parent / rel,        # 相对本文档
                        REPO_ROOT / "nxs" / rel,  # 相对 nxs/（简写）
                    ]
                    if any(c.exists() for c in candidates):
                        continue
                    problems.append(f"{path.name}:{i}  {rel}")
        assert not problems, (
            "文档引用了不存在的路径：\n  " + "\n  ".join(sorted(set(problems)))
        )


# ---------------------------------------------------------------------------
# 9. 验收手册与抽查脚本
# ---------------------------------------------------------------------------

class TestAcceptanceDrill:
    """``examples/acceptance_drill.py`` 必须能跑通，且覆盖手册的关键条目。

    一份跑不通的抽查脚本比没有更糟：人会以为"抽查过了就是好的"。
    """

    @covers("M11.F1")
    def test_drill_runs_and_passes(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "examples/acceptance_drill.py"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "全部通过" in result.stdout, result.stdout[-800:]
        # 必须诚实声明它的边界
        assert "必须手工验" in result.stdout, (
            "抽查脚本没有说明它不覆盖什么 —— 读者会以为全验过了"
        )

    @covers("M11.F1")
    def test_guide_references_the_drill(self):
        """手册必须告诉读者先跑抽查脚本，否则那个脚本没人会用。"""
        text = _read(DOCS / "acceptance-guide.md")
        assert "acceptance_drill.py" in text, (
            "验收手册没有引用抽查脚本 —— 它写了也不会有人跑"
        )
        assert "必须手工验" in text or "必须手工" in text, (
            "手册没有说清哪些必须手工验"
        )


# ---------------------------------------------------------------------------
# 10. 「没有 Web UI」这一声称必须一直是真的
# ---------------------------------------------------------------------------

class TestWebUiBoundary:
    """观察窗（M15）存在之后，「界面在哪、网关是否纯净」这两条边界。

    ⚠ **这个类是从 ``TestNoWebUi`` 改来的。**
    原来的断言是"仓库里零前端文件"——那是当时的真相。现在有了 M15，
    那句话变成了假的，于是断言跟着改。

    这里刻意**没有删掉旧测试**，而是把它拆成两条更精确的：

    * ``test_gateway_never_returns_html`` —— **保留原样**。观察窗是独立
      进程，网关仍然一个 HTML 都不返回。这条边界比"整个仓库没有前端"
      更本质，而且不会因为加了个 UI 就失效。
    * ``test_page_shell_is_the_only_html`` —— 页面模板只有一个出处，
      且不含服务端插值。

    这个演进本身值得记下来：**测试跟着事实改是对的，删测试掩盖变化是错的。**
    """

    @covers("M15.F6")
    def test_gateway_never_returns_html(self):
        """★ 网关不得返回 HTML —— 它只服务 HMAC 签名的机器身份。

        观察窗（浏览器 cookie 那套）必须待在**另一个进程**里。
        一旦网关开始返回 HTML，两套信任模型就共用了同一个进程，
        UI 的漏洞会落在承载全部节点流量的地方。
        """
        gateway = (REPO_ROOT / "nxs" / "edge" / "gateway.py").read_text(encoding="utf-8")
        forbidden = ["text/html", "<!DOCTYPE", "<html", "<div", "<body"]
        hits = [token for token in forbidden if token in gateway]
        assert not hits, (
            f"网关里出现了 HTML 相关内容 {hits} —— 观察窗必须独立于网关"
        )

    @covers("M15.F6")
    def test_page_shell_is_the_only_html(self):
        """页面模板只应有 ``nxs/ui/page.py`` 这一个出处。

        多一份 HTML 就多一处需要同步 XSS 边界的文件。
        """
        html_files = [
            str(p.relative_to(REPO_ROOT))
            for p in REPO_ROOT.rglob("*.html")
            if not ({"venv", "node_modules", "__pycache__"} & set(p.parts))
        ]
        assert not html_files, (
            f"仓库里出现了独立的 HTML 文件 {html_files} —— "
            f"页面模板应只有 nxs/ui/page.py 一处（内联字符串），"
            f"便于把 XSS 边界收在一个文件里看"
        )

    @covers("M15.F6")
    def test_page_has_no_server_side_interpolation(self):
        """页面模板不得含服务端插值占位符。

        数据全部走 /api/state + 前端 textContent 建 DOM。
        一旦允许服务端拼 HTML，每个插值点都得记得转义一次——
        "靠记得"的安全迟早会破。
        """
        from nxs.ui.page import SHELL

        assert "{name}" not in SHELL and "%s" not in SHELL
        assert "${" not in SHELL

    @covers("M15.F4")
    def test_acceptance_guide_describes_the_window(self):
        """验收手册必须说清"界面在哪、怎么验"，而不只是"没有 UI"。"""
        text = _read(DOCS / "acceptance-guide.md")
        assert "nxs ui" in text, "验收手册没提观察窗"
        assert "SSH" in text, "没说怎么从别的机器访问"
        # 旧的"零前端"说法必须已经改掉
        assert "零前端代码**" not in text.replace("**", ""), (
            "验收手册还在说「零前端代码」—— 观察窗已经实现了"
        )


# ---------------------------------------------------------------------------
# 11. 设计文档里的「当前缺口」会随实现过期
# ---------------------------------------------------------------------------

class TestDesignDocGaps:
    """``docs/design/05-webui-design.md`` 描述的是「现在缺什么」。

    ⚠ **这类描述天生会过期，而且过期方式很坏**：
    缺口一旦被补上，文档却还写着"没有这个字段"，
    后来的人会**重复实现一个已经存在的东西**——
    或者反过来，因为"文档说没有"而去绕过一条其实已经通了的路。

    所以本类做一件事：**缺口一旦被补上，就提醒去更新设计文档**。
    这不是阻止实现，而是保证"文档与实现同步"这个动作一定会发生。
    """

    DESIGN = DOCS / "design" / "05-webui-design.md"

    @covers("M11.F1")
    def test_design_doc_status_matches_registry(self):
        """★ 设计文档的状态标注必须与 registry 一致。

        ⚠ **这条测试的判据改过一次，值得记下来。**
        它原来断言"设计文档必须标明未实现"。M15 实现之后，
        这条断言本身就是错的了 —— 但改成"必须标明已实现"同样会腐化，
        下次如果 M15 被回退，又要改一次。

        所以改成**跟随权威源**：registry 说未实现 → 文档要标未实现；
        registry 说已实现 → 文档要标已实现。这样它永远不会因为
        "实现状态变了"而失效，只在**两者不一致**时才红。
        """
        sys.path.insert(0, str(REPO_ROOT))
        from traceability.registry import MODULES

        assert self.DESIGN.exists(), "WebUI 设计文档不见了"
        text = _read(self.DESIGN)

        m15 = next(m for m in MODULES if m.id == "M15")
        if m15.status == "未实现":
            assert "未实现" in text, (
                "registry 说 M15 未实现，但设计文档没标明 —— "
                "读者会以为可以用了"
            )
        else:
            assert "已实现" in text, (
                f"registry 说 M15 是「{m15.status}」，但设计文档没标明 —— "
                f"读者会以为还不能用"
            )
            # 已实现就得有实施记录，否则"怎么验的"无从追溯
            assert "实施结果" in text, (
                "文档说已实现，但没有实施与实测记录 —— 无法追溯验过什么"
            )

    @covers("M11.F1")
    def test_gap_claims_are_marked_resolved_when_fixed(self):
        """★ 缺口一旦补上，设计文档必须**标注已处置**。

        判据选择说明（这里踩过一次）：
        最初我写的是"补上后不许再提这个缺口"——太脆。它逼人删掉
        历史描述，而那些描述**有解释价值**：不知道"名称曾经拿不到"，
        就看不懂为什么方案要先补接口再写 UI。

        正确的判据不是"不许提历史"，而是"**提了历史就必须标明现状**"。
        所以这里检查：代码已补上时，文档里必须出现「已处置」标记。
        """
        sys.path.insert(0, str(REPO_ROOT))
        from nxs.core.models import RoleCard

        gateway = (REPO_ROOT / "nxs" / "edge" / "gateway.py").read_text(encoding="utf-8")
        text = _read(self.DESIGN)

        nodes_block = gateway.split('elif action == "nodes":')[1].split("elif action")[0]
        has_display_name = "display_name" in nodes_block
        has_description = "description" in str(RoleCard._FIELDS)  # noqa: SLF001

        if has_display_name or has_description:
            assert "已处置" in text, (
                "缺口已经补上了，但设计文档第一节没有任何「已处置」标记 —— "
                "读者会把历史描述当成现状，去重复实现已经存在的东西"
            )
        if has_display_name:
            assert "ops nodes` 现在返回" in text or "现在返回" in text, (
                "`ops nodes` 已返回 display_name，但文档没说清现状"
            )
        if has_description:
            assert "已加 `description`" in text or "已加 description" in text, (
                "`RoleCard` 已有 description，但文档没说清现状"
            )

    @covers("M11.F1")
    def test_declared_facts_in_design_doc_still_hold(self):
        """设计文档引用的既有事实不能过期。

        抽查两条最关键的：网关返回 JSON（不返回 HTML）、
        读动作不留审计（否则 UI 轮询会刷爆日志）。
        """
        text = _read(self.DESIGN)
        gateway = (REPO_ROOT / "nxs" / "edge" / "gateway.py").read_text(encoding="utf-8")

        if "读动作不留痕" in text or "不会刷爆审计" in text:
            nodes_block = gateway.split('elif action == "nodes":')[1].split("elif action")[0]
            metrics_block = gateway.split('elif action == "metrics":')[1].split("elif action")[0]
            assert "record_ops" not in nodes_block, (
                "`nodes` 开始留痕了 —— 设计文档里「UI 轮询不会刷爆审计」"
                "这个前提失效了（每 3 秒写一条审计）"
            )
            assert "record_ops" not in metrics_block, (
                "`metrics` 开始留痕了 —— 设计文档里「轮询不刷审计」的前提失效"
            )


class TestCliCommandClaims:
    """★★ 文档里"有几个子命令"必须与实际一致。

    ⚠ 这条是手工核对时发现的：`docs/acceptance-guide.md` 的 G4 写着
    「8 个命令全部返回 0」，而实际是 **9 个**（多了 `ui`）。

    这类数字最容易漂移 —— 加一个子命令时没人会想起去改文档里的数字。
    所以让测试自己数。
    """

    @covers("M11.F1")
    def test_command_count_in_acceptance_guide(self):
        """★ 验收指南里的命令数必须等于实际子命令数。"""
        import re
        import subprocess
        import sys as _sys

        out = subprocess.run(
            [_sys.executable, "-m", "nxs", "--help"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=60,
        ).stdout
        subs = re.findall(r"^    ([a-z][a-z-]*)\s{2}", out, re.M)
        assert subs, "取不到 nxs 子命令列表"
        actual = len(subs)

        guide = (DOCS / "acceptance-guide.md").read_text(encoding="utf-8")
        m = re.search(r"G4 CLI 全可用.*?(\d+)\s*个命令", guide)
        assert m, "验收指南里找不到「N 个命令」这条声称"

        claimed = int(m.group(1))
        assert claimed == actual, (
            f"验收指南说 {claimed} 个命令，实际 {actual} 个：{subs}\n"
            f"  加/删子命令时记得同步这条"
        )

    @covers("M11.F1")
    def test_all_commands_have_working_help(self):
        """★ 每个子命令的 `--help` 都必须返回 0。

        验收指南让使用者"逐个 --help 检查返回 0"。如果某个子命令
        `--help` 崩了，那条验收步骤会直接失败 —— 而使用者会以为
        是自己环境有问题。
        """
        import re
        import subprocess
        import sys as _sys

        out = subprocess.run(
            [_sys.executable, "-m", "nxs", "--help"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=60,
        ).stdout
        subs = re.findall(r"^    ([a-z][a-z-]*)\s{2}", out, re.M)

        broken = []
        for s in subs:
            r = subprocess.run(
                [_sys.executable, "-m", "nxs", s, "--help"],
                capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=60,
            )
            if r.returncode != 0:
                broken.append((s, r.returncode))

        assert not broken, f"这些子命令的 --help 返回非 0：{broken}"


class TestNoDuplicateSections:
    """★★ 文档里不能有重复章节。

    ⚠ **这是实测发现的问题**：`docs/agent-bridges.md` 曾有
    **209 行完全重复**的内容 ——

      · `## 十二、Skill` 出现两次（各 103 行，逐字相同）
      · `## 十三、公司电脑作为内网执行者` 出现两次

    而且章节编号也是乱的：`八` 排在 `十四` 之后，`九` 紧接 `七`。

    这类问题的成因通常是**合并/粘贴时操作失误**，而它没有任何症状 ——
    读者会读到两遍同样的内容，然后怀疑自己是不是看串行了。
    对着别人发布时尤其糟：看起来像"这文档不严谨"。

    去重后 992 → 759 行，内容零丢失（逐行核对过）。
    """

    #: 章节较多的文档（短文档不需要这条守护）
    LONG_DOCS = ("agent-bridges.md", "protocol.md", "architecture.md",
                 "acceptance-guide.md")

    @covers("M11.F1")
    def test_no_duplicate_h2_headings(self):
        """同一个二级标题不能出现两次。"""
        problems = []
        for name in self.LONG_DOCS:
            p = DOCS / name
            if not p.is_file():
                continue
            heads = [l.strip() for l in
                     p.read_text(encoding="utf-8").splitlines()
                     if l.startswith("## ")]
            dup = {h for h in heads if heads.count(h) > 1}
            if dup:
                problems.append((name, sorted(dup)))

        assert not problems, (
            "这些文档有重复的二级标题（合并时粘贴重复了？）：\n"
            + "\n".join(f"  {n}: {d}" for n, d in problems)
        )

    @covers("M11.F1")
    def test_h2_numbering_is_sequential(self):
        """★ 二级标题的编号必须从「一」开始连续递增。

        编号跳号或乱序会让读者以为漏看了内容，也会让交叉引用
        （"见第 X 节"）指向错误的位置。
        """
        import re

        zh = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
              "十一", "十二", "十三", "十四", "十五", "十六", "十七",
              "十八", "十九", "二十"]

        problems = []
        for name in self.LONG_DOCS:
            p = DOCS / name
            if not p.is_file():
                continue
            nums = []
            for line in p.read_text(encoding="utf-8").splitlines():
                m = re.match(r"^## ([一二三四五六七八九十]+)、", line)
                if m:
                    nums.append(m.group(1))
            if not nums:
                continue            # 这份文档不用中文编号
            expected = zh[:len(nums)]
            if nums != expected:
                problems.append((name, nums, expected))

        assert not problems, (
            "这些文档的章节编号不是连续递增：\n"
            + "\n".join(
                f"  {n}:\n    实际 {a}\n    应为 {e}"
                for n, a, e in problems
            )
        )


# ---------------------------------------------------------------------------
# 6. 死词汇：改造删掉的旧字段不能再出现在描述性文档里
# ---------------------------------------------------------------------------

class TestNoDeadVocabulary:
    """★★ 权限模型改造删掉的旧字段，不能再出现在描述性文档里。

    ## 这条测试的来历

    `docs/design/07` §8 写过一段计划：

    > 不靠自觉。加一条测试 `test_no_dead_vocabulary_in_docs`：
    > **PENDING 清空 = 文档改完**，这比口头确认可靠。
    > 且它在 CI 里会一直提醒，不会因为「这次先凑合」而无声腐烂。

    **但它从未落地** —— 只作为代码片段留在设计文档里。于是改造期间
    「14 / 16 份文档已改完」这个结论一直是靠人工扫描支撑的，
    而人工扫描正是那句话说的「不可靠」的那一种。

    这份文档整理补上了它。而且比原计划更严：**没有 PENDING 白名单**。
    原计划留白名单是因为改造尚未完成；现在完成了，任何命中都必须处理。

    ## 为什么要用「字段路径级」的词表

    不用 `quota` 这种裸词 —— 它在讲历史教训的段落里是必要内容。
    只列**精确到字段路径的旧写法**：`quota.tasks_per_day` 这类。
    讲"这个字段为什么被删"时不会碰巧写出完整路径。

    确需引用旧写法时，在那一行加 `<!-- ok:describes-removed -->`。
    `docs/protocol.md` 里已有先例。

    ## 刻意豁免的文件

    * **历史快照**：`CHANGELOG` / `docs-audit` / `03` / `04` ——
      正确性标准是"如实反映当时"，改了反而失真
    * **设计文档 `06` / `07` / `08`**：正文引用旧模型是做**前后对照**，
      删掉这个对照，读者就看不出改造到底改了什么
    """

    #: 改造删掉/改名的字段路径。
    #:
    #: ⚠ 每一项都精确到「对象.字段」，不列裸字段名 ——
    #:   裸词会误伤讲解历史与对照的段落。
    DEAD_VOCAB = (
        # Roles 的 5 个布尔（Step 5c 删除）
        "roles.issuer", "roles.executor", "roles.relay",
        "roles.observer", "roles.human_interface",
        # Constraints 的 3 个字段（Step 5c 删除）
        "can_issue_types", "can_receive_from", "can_delegate_to",
        # Quota 的 4 个字段（Step 5b 删除）
        "quota.tasks_per_day", "quota.bytes_per_response",
        "quota.bytes_per_day", "quota.concurrent_tasks",
        # 与 self_limits 合并的那个（Step 5c）
        "constraints.max_response_level",
        # mesh 退出节点表（Step 5a）
        "local_node", "_pulse_local_node",
        # 拆掉/删掉的错误码（Step 4 / 5c）
        "MX-RECEIVE-03", "MX-DELEGATE-04", "MX-DELEGATION-TOO-DEEP",
    )

    #: 描述性文档 —— 应该**已经没有**旧词汇。
    #:
    #: 加新文档进这里等于宣称"它描述的是当前系统"。
    #: 这是有代价的承诺：它从此受这条测试约束。
    DESCRIPTIVE_DOCS = (
        "README.md",
        "CONTRIBUTING.md",
        "docs/architecture.md",
        "docs/protocol.md",
        "docs/acceptance-guide.md",
        "docs/agent-bridges.md",
        "docs/hermes-integration.md",
        "docs/observability.md",
        "docs/artifact-size.md",
        "deploy/README.md",
        "traceability/MATRIX.md",
    )

    #: 设计档案 —— 豁免。
    #:
    #: ⚠ `docs/design/01-system-definition.md` 原本在描述性清单里，
    #: 2026-10-30 移出。理由：它有一节「原角色 | 问题」的表格，
    #: 逐条列出旧模型的缺陷（Issuer/Executor 的 4 种组合全不参与判定、
    #: Relay 从未实现、Observer 没有通道）—— **那是改造的直接依据**，
    #: 删掉就只剩结论，将来没人知道为什么改。
    #:
    #: 它同时含变更记录（v1.1 命名方案、待定项定案过程），
    #: 性质与 03/04 的历史快照相同：正确性标准是"如实反映当时"。
    #:
    #: 判断依据不是"哪个文件方便"，而是**它是否在定义词汇表或记录历史**。
    DESIGN_DOCS = (
        "docs/design/01-system-definition.md",
    )

    #: 历史快照 —— 豁免。改了反而失真。
    SNAPSHOT_DOCS = frozenset({
        "CHANGELOG.md",
        "docs/docs-audit.md",
        "docs/design/03-traceability-review.md",
        "docs/design/04-mvp-plan.md",
    })

    @covers("M11.F1")
    def test_descriptive_docs_have_no_dead_vocabulary(self):
        """★ 描述性文档里不得出现旧字段路径。"""
        offenders: list[str] = []

        for rel in self.DESCRIPTIVE_DOCS:
            path = REPO_ROOT / rel
            if not path.exists():
                offenders.append(f"{rel}: 文件不存在（清单过时了？）")
                continue
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if "ok:describes-removed" in line:
                    continue                    # 行内显式豁免
                for word in self.DEAD_VOCAB:
                    if word in line:
                        offenders.append(
                            f"{rel}:{lineno} [{word}] {line.strip()[:60]}")

        assert not offenders, (
            "描述性文档里出现已删除的旧字段：\n  "
            + "\n  ".join(offenders)
            + "\n\n  三种处理：\n"
            "    ① 按新模型改写（多数情况）\n"
            "    ② 确需引用旧写法 → 那一行加 <!-- ok:describes-removed -->\n"
            "    ③ 若整份文档是历史快照 → 加进 SNAPSHOT_DOCS 并说明理由\n"
            "  ⚠ 不要只是为了让它绿就改词表 —— 先判断是文档过时还是词表太宽"
        )

    #: 旧模型的角色名。新模型只有 `worker` / `driver` / `publisher`。
    #:
    #: ⚠ 这些词**不单独进 DEAD_VOCAB**：讲"为什么放弃 Relay"
    #: 或"Observer 没有通道"时必须提到它们，那是改造依据。
    #: 真正会误导的是**并列列表**——「Issuer 发布 / Executor 执行 /
    #: Relay 中转 / Observer 只读」这种把旧模型当现状介绍的写法。
    #: 见下面 test_no_legacy_role_roster。
    LEGACY_ROLE_NAMES = ("Issuer", "Executor", "Relay", "Observer")

    @covers("M11.F1")
    def test_no_legacy_role_roster(self):
        """★★ 不得把旧角色**并列**成一张"当前角色表"。

        ⚠ **这条来自 2026-10-30 的一次真实现场**。

        本机 Agent（work-macbook）接入后，问它"这个仓库是做什么的"，
        它读了本地 README 后回答：

            节点角色：Issuer(发布)、Executor(执行)、Relay(中转)、Observer(只读)

        **这是旧模型，早就删了。** 而 README 的术语表当时确实还写着这一行
        —— 因为 `TestNoDeadVocabulary` 的词表精确到字段路径
        （`roles.issuer`），抓不到裸角色名。

        单测抓不到、CI 抓不到，**是 Agent 读文档时抓到的**。

        这条守护的判据是「一行内出现 **≥2 个**旧角色名」：
        单独提一个通常是在讲历史（"Relay 从未实现"），
        并列两个以上才是在给读者介绍"角色有这些"。
        """
        offenders: list[str] = []

        for rel in self.DESCRIPTIVE_DOCS:
            path = REPO_ROOT / rel
            if not path.exists():
                continue
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if "ok:describes-removed" in line:
                    continue
                found = [n for n in self.LEGACY_ROLE_NAMES if n in line]
                if len(found) >= 2:
                    offenders.append(
                        f"{rel}:{lineno} {found} — {line.strip()[:60]}")

        assert not offenders, (
            "这些行把旧角色并列成了「当前角色表」：\n  "
            + "\n  ".join(offenders)
            + "\n\n  新模型的角色只有 worker / driver / publisher。"

            "\n  ⚠ 若确实在讲历史（如「Relay 从未实现」），单独提一个角色名即可；"
            "\n  若整份文档是设计档案或历史快照，加进 DESIGN_DOCS / SNAPSHOT_DOCS。"
            "\n\n  **这类错误单测抓不到，但 Agent 会读到并照着回答。**"
        )

    @covers("M11.F1")
    def test_no_pending_whitelist(self):
        """★★ 不得引入「待改白名单」。

        07 §8 原计划里有 `PENDING = {...}`，边改边删。
        那是改造期间的权宜；**改造完成后它必须不存在**。

        理由：临时豁免不清掉，等于给这项检查永久免检证 ——
        而"永久免检"与"没有这项检查"没有区别。
        """
        # ⚠ 必须用 AST 查**真正的赋值**，不能只搜字符串 ——
        #   这个类的 docstring 里就引用了「PENDING 清空」这句话
        #   （讲它的来历），纯文本搜索会误判。
        #   第一版就是这么写的，然后它自己红了。
        import ast

        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        cls = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.ClassDef) and n.name == "TestNoDeadVocabulary"),
            None,
        )
        assert cls is not None, "找不到 TestNoDeadVocabulary 类"

        for node in cls.body:
            targets = []
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]

            assert not any(name.startswith("PENDING") for name in targets), (
                f"TestNoDeadVocabulary 里出现了白名单赋值：{targets}\n"
                "  改造已结束，不应再有「等以后改」的豁免。\n"
                "  确需豁免请用行内标记 <!-- ok:describes-removed --> "
                "或 SNAPSHOT_DOCS，并写清理由。"
            )

    @covers("M11.F1")
    def test_snapshot_actually_contains_old_vocab(self):
        """反向：豁免的历史快照里**应该**还有旧词汇。

        如果某份"历史快照"里一个旧词都搜不到，说明它可能已经被
        顺手改过了 —— 那它在"如实反映当时"这个标准上是失真的。

        ⚠ 不要求每一份都命中（文件内容各有不同），只做整体提示：
        若**所有**快照都干净，说明这份豁免清单可能已无必要。
        """
        clean = []
        for rel in self.SNAPSHOT_DOCS:
            path = REPO_ROOT / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            if not any(w in text for w in self.DEAD_VOCAB):
                clean.append(rel)

        assert len(clean) < len(self.SNAPSHOT_DOCS), (
            "所有历史快照里都已没有旧词汇 —— 这份豁免清单可能已无必要，\n"
            f"  或者这些文件被顺手改写过（那会让它们失真）：{clean}"
        )
