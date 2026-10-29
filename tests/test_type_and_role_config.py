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


#: 默认的角色表 —— **不含任何 serves 模式**。
#:
#: ⚠ 为什么不写 serves：许多用例自己声明 `types:`，而角色里若写死
#:   `serves: [demo.echo]`，一旦测试只声明 `work.query` 就会触发
#:   "通配匹配不到已定义类型"的校验 —— 那是**正确的**校验，只是与
#:   这些用例要测的东西无关。
#:
#: `driver` / `publisher` 都是 `can_issue: true` 且 `serves` 为空，
#: 这是合法组合（只发不收），所以不需要任何类型就能通过校验。
#: 需要 worker（只收不发）的用例自己传 `roles=`。
_BASE_ROLES = """
roles:
  - name: driver
    can_issue: true
  - name: publisher
    can_issue: true
"""


def _minimal(extra: str = "", *, role: str = "driver",
             roles: str | None = None) -> str:
    """一份最小的可加载配置，用于叠加测试。

    :param extra: 追加在文件末尾的 YAML 片段（如自定义 types）
    :param role: 节点 node-a 以什么角色接入（默认 driver —— 它不需要 serves）
    :param roles: 替换默认角色表。用例声明自己的 types 时要一并给出
        与之匹配的 roles，否则 serves 模式会匹配不到类型。
    """
    roles_yaml = roles if roles is not None else _BASE_ROLES
    return f"""
server:
  host: 127.0.0.1
  port: 0
nodes:
  - node_id: node-a
    domain: cloud
    display_name: "A"
    role: {role}
    self_limits:
      max_backlog: 20
      max_response_level: summary
    capabilities:
      - type: demo.echo
        level: summary
        mode: sync
        response_fields: [echo, level]
    secret: {"a" * 40}
policy:
  matrix:
    - from_domain: cloud
      to_domain: cloud
{roles_yaml}
{extra}
"""


#: 声明自定义 types 时配套的 roles —— serves 正好覆盖这些类型。
def _roles_serving(*types: str) -> str:
    items = "\n".join(f"    serves: [{', '.join(types)}]" if i == 0
                      else "" for i in range(1))
    return f"""
roles:
  - name: worker
    can_issue: false
    serves: [{", ".join(types)}]
  - name: driver
    can_issue: true
    serves: [{", ".join(types)}]
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
        p = _write(workdir, _minimal(role="worker", extra="""
roles:
  - name: worker
    serves: [work.query]
types:
  - name: work.query
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
        p = _write(workdir, _minimal(role="worker", extra="""
roles:
  - name: worker
    serves: ["work.*"]
types:
  - name: work.query
  - name: work.calendar
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
        p = _write(workdir, _minimal(role="publisher", extra="""
roles:
  - name: publisher
    can_issue: true
    serves: []
types:
  - name: work.query
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


class TestRoleTableIsRequired:
    """★ 有节点就必须有角色表 —— Step 5c 起这是硬要求。

    ⚠ 这个类原来叫 `TestBackwardCompatibility`，测的是「省略 types/roles
    时旧配置照常工作」。**那个行为已经不存在了**：

        role 现在必填（每个 Agent 必须选一个角色）
        角色表是它的定义来源，没有角色表 role 就无处可查

    所以从"向后兼容"改成了"角色表必需"。保护的性质也变了：

        旧：新代码不干扰旧配置（Step 1 的纪律）
        新：缺角色表要**明确报错**，而不是让节点注册成功却什么都干不了
    """

    @covers("M11.F1")
    def test_nodes_without_roles_section_is_rejected(self, workdir):
        """有节点但没有 roles: 段 → 明确报错。

        为什么不能容忍：`role` 必填，而角色的定义在 roles 段里。
        没有角色表，节点的 role 无处可查 —— 网络只能一律拒绝，
        症状是"注册成功但什么都干不了"，看起来像网络故障。

        **启动时就拦住**，错误信息直接说清该加什么。
        """
        p = _write(workdir, """
server:
  host: 127.0.0.1
  port: 0
nodes:
  - node_id: node-a
    domain: cloud
    display_name: "A"
    role: worker
    capabilities: []
    secret: "%s"
policy:
  matrix: []
""" % ("a" * 40))
        with pytest.raises(ConfigError) as ei:
            load_config(str(p))
        msg = str(ei.value)
        assert "roles:" in msg, "要告诉读者加哪一段"
        assert "角色" in msg

    @covers("M11.F1")
    def test_node_role_not_in_table_is_rejected(self, workdir):
        """节点的 role 拼错 → 报错并列出可用角色。

        静默接受的话，症状同样是"注册成功但无权做任何事"。
        """
        p = _write(workdir, _minimal(role="wroker", extra=""))
        # _minimal 的默认角色表只有 driver / publisher，没有 wroker
        with pytest.raises(ConfigError) as ei:
            load_config(str(p))
        msg = str(ei.value)
        assert "wroker" in msg, "要指出是哪个角色名找不到"
        assert "已定义的角色" in msg, "要列出可选项"

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
        p = _write(workdir, _minimal(role="worker", extra="""
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
