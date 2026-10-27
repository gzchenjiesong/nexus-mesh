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
        "（模块编号对照见 03 §6.5）",
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
        assert len(documented) == 14, (
            f"architecture.md 只列了 {len(documented)} 个模块，应为 14："
            f"{sorted(documented)}"
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
        assert len(MODULES) == 14

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
            "续租": "M7.F9-F11 已实现",
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
        "自动匹配": "M4 匹配器（v0.6）",
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
        "config/nxs.yaml", "deploy/local.yaml",
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
