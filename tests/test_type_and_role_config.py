"""任务类型与角色的配置解析（权限模型改造 Step 1）。

⚠ **本文件只测「解析与静态校验」，不测判定逻辑。**

判定（谁能发、谁该收到）要到 Step 3/4 才切换 —— 那时才会写对应的测试。
现在测判定的唯一结果是把旧行为固化成测试，反而是障碍。
"""

from __future__ import annotations

import pathlib

import pytest

from conftest import REPO_ROOT
from nxs.core.errors import ConfigError
from nxs.mgmt.config import RoleDecl, TypeDecl, load_config
from tests.test_traceability import covers

DEMO = REPO_ROOT / "config" / "nxs.example.yaml"


def _write(tmp: pathlib.Path, body: str, *, name: str = "c.yaml") -> pathlib.Path:
    p = tmp / name
    p.write_text(body, encoding="utf-8")
    return p


def _minimal(extra: str = "") -> str:
    """一份最小的可加载配置（不含 types/roles），用于叠加测试。"""
    return f"""
server:
  host: 127.0.0.1
  port: 0
nodes:
  - node_id: node-a
    domain: cloud
    display_name: "A"
    roles: {{executor: true}}
    capabilities:
      - type: demo.echo
        level: summary
        mode: sync
        response_fields: [echo, level]
    constraints:
      max_response_level: summary
      can_issue_types: ["demo.*"]
      can_receive_from: [cloud]
    secret: {"a" * 40}
policy:
  matrix:
    - from_domain: cloud
      to_domain: cloud
{extra}
"""


class TestTypeDeclParsing:
    """单个类型定义的解析。"""

    @covers("M11.F1")
    def test_minimal_type(self, workdir):
        p = _write(workdir, _minimal("""
types:
  - name: work.query
"""))
        cfg = load_config(str(p))
        t = cfg.types["work.query"]
        assert t.delivery == "unicast"        # 默认单播
        assert t.max_level.name == "SUMMARY"

    @covers("M11.F1")
    def test_full_type(self, workdir):
        p = _write(workdir, _minimal("""
types:
  - name: work.query
    delivery: broadcast
    max_level: metadata
    response_fields: [answer, level]
    freetext_fields: [answer]
    params_schema:
      question: string
    description: "问一个问题"
"""))
        t = load_config(str(p)).types["work.query"]
        assert t.delivery == "broadcast"
        assert t.max_level.name == "METADATA"
        assert t.response_fields == ("answer", "level")
        assert t.freetext_fields == ("answer",)
        assert t.params_schema == {"question": "string"}

    @covers("M11.F1")
    def test_rejects_bad_naming(self, workdir):
        """类型名必须符合 <verb>.<object>。"""
        p = _write(workdir, _minimal("""
types:
  - name: notdotname
"""))
        with pytest.raises(ConfigError, match="命名约定"):
            load_config(str(p))

    @covers("M11.F1")
    def test_rejects_multicast(self, workdir):
        """multicast 现在不支持 —— 且错误信息要说清为什么。

        `group` 还没做，枚举里放一个用不了的值，就是又一个
        "配了但不生效"。所以要拒绝，并说明等 group 落地再加。
        """
        p = _write(workdir, _minimal("""
types:
  - name: work.query
    delivery: multicast
"""))
        with pytest.raises(ConfigError) as ei:
            load_config(str(p))
        msg = str(ei.value)
        assert "multicast" in msg
        assert "group" in msg, "错误信息要说明等 group 能力落地"

    @covers("M11.F1")
    def test_rejects_duplicate_type(self, workdir):
        """★ 同名类型不能定义两次 —— 这是本改造的核心目的之一。

        旧模型允许两个节点各自声明同名类型的两套契约，系统不报错。
        全局定义后，重复必须被拦住。
        """
        p = _write(workdir, _minimal("""
types:
  - name: work.query
    response_fields: [answer]
  - name: work.query
    response_fields: [summary, level]
"""))
        with pytest.raises(ConfigError, match="重复定义"):
            load_config(str(p))

    @covers("M11.F1")
    def test_rejects_freetext_outside_response_fields(self, workdir):
        """自由文本字段必须是应答字段的子集。

        否则声明了一个永远不可能出现的字段 —— 而它正是提示注入的载体，
        声明了却不存在，会让人误以为那里有清洗。
        """
        p = _write(workdir, _minimal("""
types:
  - name: work.query
    response_fields: [answer]
    freetext_fields: [notes]
"""))
        with pytest.raises(ConfigError, match="不在 response_fields"):
            load_config(str(p))

    @covers("M11.F1")
    def test_rejects_unknown_field(self, workdir):
        """拼错字段名要报错，不能静默忽略。

        静默忽略的后果：以为配了 delivery，实际没配上，行为不符预期。
        """
        p = _write(workdir, _minimal("""
types:
  - name: work.query
    delevery: broadcast
"""))
        with pytest.raises(ConfigError, match="未知字段"):
            load_config(str(p))


class TestRoleDeclParsing:
    """单个角色定义的解析。"""

    @covers("M11.F1")
    def test_worker_defaults(self, workdir):
        p = _write(workdir, _minimal("""
types:
  - name: work.query
roles:
  - name: worker
    serves: [work.query]
"""))
        r = load_config(str(p)).roles["worker"]
        assert r.can_issue is False, "worker 默认不能发布"
        assert r.serves_type("work.query") is True
        assert r.serves_type("work.calendar") is False

    @covers("M11.F1")
    def test_driver_can_issue(self, workdir):
        p = _write(workdir, _minimal("""
types:
  - name: work.query
roles:
  - name: driver
    can_issue: true
    serves: [work.query]
"""))
        assert load_config(str(p)).roles["driver"].can_issue is True

    @covers("M11.F1")
    def test_wildcard_serves(self, workdir):
        p = _write(workdir, _minimal("""
types:
  - name: work.query
  - name: work.calendar
roles:
  - name: worker
    serves: ["work.*"]
"""))
        r = load_config(str(p)).roles["worker"]
        assert r.serves_type("work.query")
        assert r.serves_type("work.calendar")
        # 保留结尾的点，避免 work 前缀误匹配 workX.y
        assert not r.serves_type("workX.y")

    @covers("M11.F1")
    def test_rejects_bare_star(self, workdir):
        """裸通配等于取消默认拒绝，必须显式限定。"""
        p = _write(workdir, _minimal("""
types:
  - name: work.query
roles:
  - name: worker
    serves: ["*"]
"""))
        with pytest.raises(ConfigError, match="裸通配"):
            load_config(str(p))

    @covers("M11.F1")
    def test_rejects_undeclared_type(self, workdir):
        """★ 角色引用了未定义的类型必须报错。

        提前报错比运行时"任务投出去没人接"好排查 ——
        后者的症状是"派了活没反应"，看起来像网络问题。
        """
        p = _write(workdir, _minimal("""
types:
  - name: work.query
roles:
  - name: worker
    serves: [work.calendar]
"""))
        with pytest.raises(ConfigError, match="未在 types 中定义"):
            load_config(str(p))

    @covers("M11.F1")
    def test_rejects_wildcard_matching_nothing(self, workdir):
        """通配模式匹配不到任何已定义类型，同样是配置错误。"""
        p = _write(workdir, _minimal("""
types:
  - name: work.query
roles:
  - name: worker
    serves: ["invest.*"]
"""))
        with pytest.raises(ConfigError, match="匹配不到任何"):
            load_config(str(p))

    @covers("M11.F1")
    def test_rejects_empty_role(self, workdir):
        """★ 既不能发布、又不服务的角色必须被拒。

        ⚠ 这不是理论上的边界情况，而是**配置时很容易写出来的东西**：
        想定义一个 publisher，忘了写 `can_issue: true`，于是得到一个
        既不能发也不能收的角色。

        危险在于：批准这样的角色会让运维以为放行了权限，实际什么都没放。
        """
        p = _write(workdir, _minimal("""
types:
  - name: work.query
roles:
  - name: mistake
    serves: []
"""))
        with pytest.raises(ConfigError) as ei:
            load_config(str(p))
        msg = str(ei.value)
        assert "什么都做不了" in msg
        # 错误信息要给出两条修法，否则读者还得自己想
        assert "serves" in msg and "can_issue" in msg

    @covers("M11.F1")
    def test_publisher_with_empty_serves_is_ok(self, workdir):
        """反向：只发布不执行的角色（publisher）是合法的。"""
        p = _write(workdir, _minimal("""
types:
  - name: work.query
roles:
  - name: publisher
    can_issue: true
    serves: []
"""))
        r = load_config(str(p)).roles["publisher"]
        assert r.can_issue is True
        assert r.serves == ()

    @covers("M11.F1")
    def test_rejects_duplicate_role(self, workdir):
        p = _write(workdir, _minimal("""
types:
  - name: work.query
roles:
  - name: worker
    serves: [work.query]
  - name: worker
    serves: [work.query]
"""))
        with pytest.raises(ConfigError, match="重复定义"):
            load_config(str(p))


class TestBackwardCompatibility:
    """★ 省略 types/roles 时，旧配置必须完全照常工作。

    Step 1 的纪律是「只加解析、不改判定」。如果这里红了，
    说明新代码干扰了旧路径 —— 那会让整个改造的第一步就不安全。
    """

    @covers("M11.F1")
    def test_old_style_config_still_loads(self, workdir):
        """**完全不含 types/roles 的旧配置**必须照常工作。

        ⚠ 不能用 config/nxs.example.yaml 测这条 —— 它已经加了 types/roles。
        必须构造一份真正的"旧格式"配置。
        """
        p = _write(workdir, _minimal())
        cfg = load_config(str(p))
        assert cfg.types == {}, "旧配置不该凭空出现类型定义"
        assert cfg.roles == {}, "旧配置不该凭空出现角色定义"
        assert cfg.nodes, "节点照常加载"
        assert cfg.nodes["node-a"].capabilities, "能力仍从卡片读取"

    @covers("M11.F1")
    def test_example_config_has_both_sections(self):
        """示例配置必须同时展示 types 与 roles —— 它是新用户的模板。

        示例配置是文档里唯一可执行的部分，它错了比任何描述都误导人。
        """
        from nxs.mgmt.config import demo_env_for

        cfg = load_config(str(DEMO), env=demo_env_for(DEMO))
        assert cfg.types, "示例配置缺少 types 段"
        assert cfg.roles, "示例配置缺少 roles 段"
        # 三个角色都要有，否则读者不知道角色集长什么样
        assert set(cfg.roles) >= {"worker", "driver", "publisher"}
        # publisher（只发不收）必须能过校验
        assert cfg.roles["publisher"].can_issue is True
        assert cfg.roles["publisher"].serves == ()

    @covers("M11.F1")
    def test_empty_section_is_rejected(self, workdir):
        """声明了节但内容为空 —— 通常是想写却写错了，报错比静默好。"""
        p = _write(workdir, _minimal("types: []\n"))
        with pytest.raises(ConfigError, match="内容为空"):
            load_config(str(p))

    @covers("M11.F1")
    def test_roles_alone_without_types_is_allowed(self, workdir):
        """只声明 roles、不声明 types 时，不做类型交叉校验。

        因为此时类型仍由节点 capabilities 提供（旧路径），
        静态核对无从下手。等 Step 3 才强制要求 types。
        """
        p = _write(workdir, _minimal("""
roles:
  - name: worker
    serves: [demo.echo]
"""))
        cfg = load_config(str(p))
        assert cfg.roles["worker"].serves == ("demo.echo",)


class TestTypeAndRoleHelpers:
    """两个 dataclass 的行为（不起服务，纯逻辑）。"""

    @covers("M11.F1")
    def test_type_serves_exact(self):
        t = TypeDecl(name="work.query")
        assert t.serves("work.query")
        assert not t.serves("work.querysuffix")

    @covers("M11.F1")
    def test_role_wildcard_keeps_trailing_dot(self):
        """`work.*` 不能匹配 `workX.y` —— 保留下点就避免了前缀误配。"""
        r = RoleDecl(name="w", serves=("work.*",))
        assert r.serves_type("work.query")
        assert not r.serves_type("workX.y")

    @covers("M11.F1")
    def test_role_empty_serves_serves_nothing(self):
        r = RoleDecl(name="publisher", can_issue=True)
        assert not r.serves_type("work.query"), (
            "不声明 serves 的 publisher 只发不收"
        )
