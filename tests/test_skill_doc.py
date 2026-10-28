"""`nexus-mesh-skill.md` 的质量守门。

这份文档是 Agent 理解 mesh 的**唯一依据**。它写错的时候，
症状是"Agent 照着做但失败"，而错误信息在 Agent 那边看起来像别的问题
（比如"无法确定身份"，看起来像配置错，实际是文档让它少写了 `--as`）。

所以在文档层面就要守住几条。
"""

from __future__ import annotations

import re

import pytest

from conftest import REPO_ROOT, covers

SKILL = REPO_ROOT / "nexus-mesh-skill.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _frontmatter() -> str:
    m = re.match(r"^---\n(.*?)\n---\n", _text(), re.S)
    assert m, "skill 文件必须有一段 YAML frontmatter"
    return m.group(1)


class TestFrontmatter:
    """两个 Agent 都靠 frontmatter 认出这个 skill。"""

    @covers("M14.F4")
    def test_is_parseable_yaml(self):
        """★ frontmatter 必须是合法 YAML —— 否则两边都不会加载。

        Hermes 与 OpenClaw 都从这段里读 name / description。
        写错了它们只是**静默跳过**，不会报错 —— 那最难查。
        """
        yaml = pytest.importorskip("yaml")
        data = yaml.safe_load(_frontmatter())
        assert isinstance(data, dict)
        assert data.get("name") == "nexus-mesh"
        assert data.get("description")

    @covers("M14.F4")
    def test_description_says_when_to_use(self):
        """★ description 决定 Agent 会不会去读全文。

        Skill 是**渐进披露**的：系统提示里只有 name + description，
        全文要 Agent 主动 read SKILL.md。

        所以 description 必须说清"什么时候该用它" ——
        只写"这是一个 mesh 网络"不够，Agent 不知道何时该想起来。
        """
        yaml = pytest.importorskip("yaml")
        desc = yaml.safe_load(_frontmatter()).get("description", "")
        # 必须包含"何时使用"的指引，而不只是定义
        assert any(kw in desc for kw in ("当", "需要", "when")), (
            f"description 只说明了是什么，没说什么时候用：{desc!r}"
        )

    @covers("M14.F4")
    def test_declares_both_namespaces(self):
        """★ metadata 同时给两个 Agent 的命名空间。

        Hermes 读 ``metadata.hermes``，OpenClaw 读 ``metadata.openclaw``。
        共用一份源文件，所以要两个都写 —— 少一个不会报错，
        只是那一侧的元信息缺失（OpenClaw 就不知道要检查 nxs-cli 是否存在）。
        """
        yaml = pytest.importorskip("yaml")
        meta = yaml.safe_load(_frontmatter()).get("metadata") or {}
        assert "hermes" in meta
        assert "openclaw" in meta
        assert "nxs-cli" in str(meta["openclaw"].get("requires", {})), (
            "OpenClaw 侧应声明依赖 nxs-cli —— 它会据此检查二进制在不在"
        )


class TestNoCircularDependency:
    """★ 文档不能给出"用需要 ID 的命令去查 ID"这种循环指引。

    ⚠ **这是实测发现的真问题。** 初版写的是：

        若报「无法确定身份」→ 每条命令都要带 --as <你的节点ID>
        （先跑 `nxs-cli whoami` 确认自己的节点 ID）

    而 ``whoami`` **正是因为没有 ID 才报错的那个命令** ——
    拿它去查 ID 是循环。Agent 照着读会卡住，然后开始自己发挥。

    实测时 taskflow 就复述了这个循环，说明它真的被误导了。
    """

    @covers("M14.F4")
    def test_does_not_tell_agent_to_use_whoami_for_id(self):
        text = _text()
        # 找"whoami 确认自己的 ID"这类说法
        bad = re.search(
            r"whoami[^\n]{0,40}(确认|查看|拿到|知道)[^\n]{0,20}(自己的)?(节点\s*ID|ID)",
            text,
        )
        assert not bad, (
            f"文档在教 Agent 用 whoami 查自己的 ID —— 那是循环：\n  {bad.group(0)}"
        )

    @covers("M14.F4")
    def test_gives_non_whoami_ways_to_find_id(self):
        """必须给出 whoami 之外的途径，至少两条。"""
        text = _text()
        ways = [
            "NXS_ACTOR",        # 环境变量
            "AGENTS.md",        # 系统提示 / 工作区文件
            "ops nodes",        # 问运维
            "nexus-mesh/agents",  # 凭据文件名（有些 Agent 会自己想到）
        ]
        found = [w for w in ways if w in text]
        assert len(found) >= 2, (
            f"只给了 {found} 这几条找 ID 的途径，不够 —— "
            f"whoami 报错时 Agent 会卡住"
        )

    @covers("M14.F4")
    def test_explicitly_warns_against_whoami(self):
        """要明说"别指望 whoami"，否则 Agent 会先试它再困惑。"""
        assert "别指望" in _text() or "不要指望" in _text(), (
            "没明确拦住 Agent 用 whoami 查 ID 的企图"
        )


class TestCommandAccuracy:
    """文档里的命令必须与实际 CLI 一致。"""

    @covers("M14.F4")
    def test_mentions_nxs_cli_not_nxs_for_agent_commands(self):
        """★ Agent 用的是 `nxs-cli`，不是 `nxs`。

        `nxs` 里有 ops（批准/吊销节点），那是运维面。
        文档如果混用，Agent 可能去试 `nxs ops`。
        """
        text = _text()
        assert "nxs-cli" in text
        # 不该出现裸的 "nxs ops"（除了说明"运维用它"的语境）
        for line in text.splitlines():
            if re.search(r"`nxs ops", line):
                assert "运维" in line, (
                    f"文档让 Agent 直接用 nxs ops —— 那是运维面：{line.strip()}"
                )

    @covers("M14.F4")
    def test_explains_as_flag_correctly(self):
        """★ 必须说清 `--as` 什么时候要带。

        初版写着"省略 --as 时用 NXS_ACTOR 环境变量 —— 桥接进程已为你设好"。
        这对 **OpenClaw 是错的**：它的 exec 跑在常驻 gateway 进程里，
        拿不到桥接注入的环境（实测 ACTOR 为空）。

        那句话会让 OpenClaw 的 Agent 照抄不带 --as 的命令，然后失败。
        """
        text = _text()
        assert "--as" in text
        # 不能有一刀切说"已为你设好"
        assert "桥接进程已为你设好" not in text, (
            "文档断言环境变量已设好 —— 这对 OpenClaw 不成立"
        )
        # 应该让 Agent 先试再决定
        assert "先跑" in text or "先试" in text, (
            "应让 Agent 先试一次再决定要不要带 --as"
        )
