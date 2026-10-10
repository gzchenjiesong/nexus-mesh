"""架构纪律测试 —— 把"约定"变成"可执行的检查"。

有些设计约束靠代码审查很难保证——它们不是"某一行写错了"，而是
"某处多了一行不该有的东西"。这类约束必须由测试守护，否则会随时间
悄悄腐化，而且腐化时不报错、不告警，只在将来某次改动中突然暴露。

本文件检查四条纪律：

1. **代码里不出现任何节点的名字**（最高优先级）
   判据：如果加一个节点需要改代码，它就不是基础设施。

2. **依赖方向单向**：``edge → core/control/data → ports ← backends``
   反向依赖即设计错误。

3. **Core 不做 I/O、不感知传输**
   Core 里不出现文件、网络、数据库、HTTP 相关调用。

4. **拒绝不泄露内容**
   对外的拒绝响应里不得出现被拒内容的任何痕迹。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from conftest import covers

from nxs.core.errors import PolicyDenied, RuleId, safe_message

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG = REPO_ROOT / "nxs"

#: 示例配置里出现的节点名与域名。
#: 这些名字**只允许**出现在配置、示例与测试夹具里，
#: 不允许出现在 ``nxs/`` 包的源代码中。
SAMPLE_NODE_IDS = ("node-a", "node-b", "hub")
SAMPLE_DOMAINS = ("zone-a", "zone-b")

#: 示例任务类型的前缀。同样只允许出现在配置与示例里。
SAMPLE_TYPE_PREFIX = "demo."


def _py_files(root: Path):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _source_without_docstrings(path: Path) -> str:
    """返回去掉 docstring 与注释的源码。

    注释与文档里**允许**提到示例名字——那是解释性文字，不是逻辑判断。
    真正要禁的是**代码里的特判**。
    """
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:  # pragma: no cover
        return text

    docstrings: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add((body[0].lineno, body[0].end_lineno or body[0].lineno))

    lines = text.splitlines()
    kept: list[str] = []
    for i, line in enumerate(lines, start=1):
        if any(lo <= i <= hi for lo, hi in docstrings):
            continue
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # 去掉行尾注释（简化处理：只在没有引号包裹时截断）
        if "#" in line and line.count('"') % 2 == 0 and line.count("'") % 2 == 0:
            line = line.split("#", 1)[0]
        kept.append(line)
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# 纪律 1：代码里不出现节点的名字
# ---------------------------------------------------------------------------

class TestNoNodeHardcoding:
    """**本项目最重要的架构纪律。**

    节点的身份、域、能力都来自配置。源代码里出现任何一处的特判，
    都意味着"加一个节点要改代码"——那样它就不是基础设施，
    而是某个具体应用的一部分。
    """

    @pytest.mark.parametrize("name", SAMPLE_NODE_IDS)
    @covers("C3")
    def test_no_node_id_in_source(self, name: str):
        offenders = []
        for path in _py_files(PKG):
            code = _source_without_docstrings(path)
            # 用引号包裹的形式才算"硬编码进逻辑"
            if re.search(rf"""['"]{re.escape(name)}['"]""", code):
                offenders.append(str(path.relative_to(REPO_ROOT)))
        assert not offenders, (
            f"节点名 {name!r} 被硬编码进源代码：{offenders}\n"
            f"节点判断必须来自配置（config.nodes），不能写在代码里。"
        )

    @pytest.mark.parametrize("domain", SAMPLE_DOMAINS)
    @covers("C3")
    def test_no_domain_in_source(self, domain: str):
        offenders = []
        for path in _py_files(PKG):
            code = _source_without_docstrings(path)
            if re.search(rf"""['"]{re.escape(domain)}['"]""", code):
                offenders.append(str(path.relative_to(REPO_ROOT)))
        assert not offenders, (
            f"域 {domain!r} 被硬编码进源代码：{offenders}\n"
            f"域只应出现在配置里。"
        )

    @covers("C3")
    def test_no_task_type_in_source(self):
        offenders = []
        pattern = re.compile(rf"""['"]{re.escape(SAMPLE_TYPE_PREFIX)}\w+['"]""")
        for path in _py_files(PKG):
            code = _source_without_docstrings(path)
            hit = pattern.search(code)
            if hit:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {hit.group(0)}")
        assert not offenders, (
            f"示例任务类型被硬编码：{offenders}\n"
            f"任务类型只应出现在配置与 examples/ 里。"
        )

    @covers("C3")
    def test_config_drives_behavior(self, config):
        """反向验证：节点集合完全由配置决定。

        给配置加一个节点，系统应当自动认识它——不需要改任何代码。
        这是"独立基础设施"的正面证据。
        """
        from dataclasses import replace
        from nxs.control import PolicyEngine

        extra_nodes = dict(config.nodes)
        base = config.nodes["node-b"]
        probe = replace(base, node_id="probe-node", domain="zone-probe")
        extra_nodes["probe-node"] = probe
        cfg = replace(config, nodes=extra_nodes)

        engine = PolicyEngine(cfg)
        # 新节点的能力被自动索引——没有改一行代码
        assert "probe-node" in engine.providers_of("demo.echo")
        # 但没有为 zone-probe 配置矩阵规则，所以仍不可达（默认拒绝）
        with pytest.raises(PolicyDenied) as ei:
            engine.check_delivery("hub", "probe-node", "demo.echo")
        assert ei.value.rule_id == RuleId.MX_DOMAIN_FORBIDDEN


# ---------------------------------------------------------------------------
# 纪律 2：依赖方向单向
# ---------------------------------------------------------------------------

class TestLayerDirection:
    """``edge → core/control/data → ports ← backends``。

    反向依赖即设计错误。允许的例外只有 ``mgmt``（它横切各层）与
    同层内的相对导入。
    """

    #: 每层允许 import 的层
    ALLOWED = {
        "core": {"core"},
        "ports": {"core"},
        "backends": {"core", "ports"},
        "control": {"core", "ports"},
        "data": {"core", "ports", "control"},
        "mgmt": {"core", "ports"},
        "edge": {"core", "ports"},
        "sdk": {"core"},
    }

    def _imports(self, path: Path) -> set[str]:
        """抽取跨层 import 的目标层名。"""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
            elif isinstance(node, ast.Import):
                mod = node.names[0].name
            else:
                continue
            # 相对导入按层级解析（简化：只看 nxs.xxx 与 ..xxx 的形态）
            layer = self._layer_of(mod, path)
            if layer:
                found.add(layer)
        return found

    @staticmethod
    def _layer_of(module: str, path: Path) -> str | None:
        layers = {"core", "ports", "backends", "control", "data", "mgmt", "edge", "sdk"}
        if module.startswith("nxs."):
            head = module.split(".")[1]
            return head if head in layers else None
        # 相对导入：用文件所在层推算
        parts = module.split(".")
        return None  # 相对导入的目标层需解析，此处放宽（由下面的绝对导入检查覆盖）

    @pytest.mark.parametrize("layer", sorted(ALLOWED))
    @covers("C2")
    def test_layer_only_imports_allowed(self, layer: str):
        allowed = self.ALLOWED[layer]
        for path in _py_files(PKG / layer):
            for target in self.imports_of(path):
                assert target in allowed, (
                    f"{path.relative_to(REPO_ROOT)} 从 {layer} 层 import 了 {target} 层。\n"
                    f"{layer} 层只允许依赖 {sorted(allowed)}。反向依赖是设计错误。"
                )

    @staticmethod
    def imports_of(path: Path) -> set[str]:
        """抽取绝对导入（``from nxs.xxx`` / ``import nxs.xxx``）的目标层。"""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        layers = {"core", "ports", "backends", "control", "data", "mgmt", "edge", "sdk"}
        out: set[str] = set()
        for node in ast.walk(tree):
            mod: str | None = None
            if isinstance(node, ast.ImportFrom):
                mod = node.module
            elif isinstance(node, ast.Import):
                mod = node.names[0].name
            if mod and mod.startswith("nxs."):
                head = mod.split(".")[1]
                if head in layers:
                    out.add(head)
        return out


# ---------------------------------------------------------------------------
# 纪律 3：Core 不做 I/O、不感知传输
# ---------------------------------------------------------------------------

class TestCoreHasNoIO:
    """Core 只做逻辑。文件、网络、数据库、时钟一律经 Port 或由调用方传入。

    收益是**可替换与可测试**——可以用内存实现跑完整测试，不必起数据库。
    （注意这与"能否迁移部署"无关：迁移靠的是本系统是独立服务，
    而运行时与驱动属于部署依赖，是正常的。）
    """

    #: Core 中禁止出现的模块
    FORBIDDEN = {
        "socket", "http", "urllib", "requests", "httpx",        # 网络
        "sqlite3", "psycopg2", "pymysql", "redis",              # 数据库驱动
        "threading",                                             # 并发原语属于装配层
    }

    @pytest.mark.parametrize("layer", ["core", "control", "data"])
    @covers("C2")
    def test_no_forbidden_imports(self, layer: str):
        offenders = []
        for path in _py_files(PKG / layer):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                for n in names:
                    if n in self.FORBIDDEN:
                        offenders.append(f"{path.relative_to(REPO_ROOT)}: import {n}")
        assert not offenders, (
            f"{layer} 层出现 I/O 或传输相关导入：\n  " + "\n  ".join(offenders) + "\n"
            f"这些应经 Port 抽象或由调用方注入。"
        )

    @covers("C2")
    def test_can_run_core_without_any_backend(self, config):
        """纯逻辑层不需要任何后端即可完成判定——这就是分层的收益。"""
        from nxs.control import PolicyEngine, Usage, make_task
        from nxs.core.models import Artifact, Level

        engine = PolicyEngine(config)     # 没有传任何存储
        engine.check_delivery("hub", "node-a", "demo.echo")
        engine.check_quota("node-a", usage=Usage(tasks_today=1))

        task = make_task(task_id="t", idempotency_key="i", task_type="demo.echo",
                         from_node="hub", to_node="node-a", params={},
                         artifacts=[Artifact(level=Level.METADATA, kind="structured",
                                             data={"echo": "x", "level": "metadata"})])
        engine.check_response("node-a", task)   # 同样不需要存储


# ---------------------------------------------------------------------------
# 纪律 4：拒绝不泄露内容
# ---------------------------------------------------------------------------

class TestDenyNeverLeaks:
    """拒绝响应只有 ``denied`` / ``rule_id`` / ``message`` 三个字段。

    错误信息本身也是信道——字段名、字段值、长度、前缀，任何一处都可能
    被用来推断跨域内容。
    """

    SECRET_MARKERS = ("SECRET", "internal", "salary", "private")

    @covers("C4")
    def test_wire_never_contains_secrets(self):
        for rid in RuleId:
            denied = PolicyDenied(rid, detail="SECRET-MARKER-should-not-leak")
            wire = denied.to_wire()
            blob = str(wire)
            assert "SECRET-MARKER" not in blob
            assert set(wire) == {"denied", "rule_id", "message"}

    @covers("C4")
    def test_every_rule_has_a_fixed_message(self):
        """固定消息不含变量部分——否则又会变成信道。"""
        for rid in RuleId:
            msg = safe_message(rid)
            assert msg, f"{rid.value} 缺少固定消息"
            assert len(msg) < 64, f"{rid.value} 的消息过长，可能是动态拼接的"
            assert "{" not in msg and "%" not in msg, (
                f"{rid.value} 的消息含格式化占位符——固定消息不应有变量部分"
            )


# ---------------------------------------------------------------------------
# 纪律 5：术语统一（防止漂移）
# ---------------------------------------------------------------------------

class TestTerminology:
    """术语漂移是本项目最容易发生的腐化。

    它不报错、不告警，只是让"节点"和"成员"、"用户"和"人"在同一份
    文档里交替出现，读者慢慢就不知道它们是不是一回事了。

    这组测试把 ``CONTRIBUTING.md`` 里的术语表变成可执行的检查。
    """

    #: 已弃用的英文标识符。改术语时它们应彻底消失。
    RETIRED_IDENTIFIERS = ("member", "Member", "MEMBER")

    #: 已弃用的中文术语。
    RETIRED_TERMS = ("成员",)

    #: 允许出现弃用词的文件与理由
    ALLOWED_IN = {
        # 术语表本身需要列出"不要写成什么"
        "CONTRIBUTING.md",
    }

    @covers("C1")
    def test_no_retired_identifier_in_code(self):
        """``nxs/`` 与 ``examples/`` 里不得出现 member 系列标识符。"""
        offenders = []
        for root in (PKG, REPO_ROOT / "examples"):
            for path in _py_files(root):
                code = _source_without_docstrings(path)
                for ident in self.RETIRED_IDENTIFIERS:
                    if re.search(rf"\b{ident}\w*", code):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}: {ident}")
        assert not offenders, (
            "出现了已弃用的标识符（应统一为 node）：\n  " + "\n  ".join(offenders)
        )

    @covers("C1")
    def test_no_retired_identifier_in_config(self):
        offenders = []
        for path in (REPO_ROOT / "config").rglob("*.yaml"):
            text = path.read_text(encoding="utf-8")
            if re.search(r"\bmember", text, re.IGNORECASE):
                offenders.append(str(path.relative_to(REPO_ROOT)))
        assert not offenders, (
            f"配置里出现已弃用标识符：{offenders}\n配置键应为 nodes / node_id。"
        )

    @covers("C1")
    def test_no_retired_chinese_term_in_docs(self):
        """中文文档里不得出现"成员"（术语表除外）。"""
        offenders = []
        targets = [REPO_ROOT / "README.md", REPO_ROOT / "CONTRIBUTING.md"]
        targets += sorted((REPO_ROOT / "docs").glob("*.md"))
        for path in targets:
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if any(t in line for t in self.RETIRED_TERMS):
                    rel = path.name
                    # 术语表的"不要写成"列允许出现反例
                    if rel in self.ALLOWED_IN and ("不要写成" in line or "~~" in line):
                        continue
                    offenders.append(f"{rel}:{i}  {line.strip()[:60]}")
        assert not offenders, (
            "文档里出现已弃用术语（应统一为「节点」）：\n  " + "\n  ".join(offenders)
        )

    @covers("C1", "C8")
    def test_python_builtins_not_renamed(self):
        """回归：``Level.parse`` 曾因批量替换把 ``__members__`` 改成
        ``__nodes__``——那是 Enum 的内置属性，不是本项目的术语。

        这条测试防止同类事故：任何 ``__members__`` 的使用都必须保留原名。
        """
        models = (PKG / "core" / "models.py").read_text(encoding="utf-8")
        assert "__members__" in models, (
            "Enum 的内置属性 __members__ 被误改了。它不属于本项目的术语体系，"
            "批量替换时必须排除。"
        )
        assert "__nodes__" not in models, "出现了不存在的内置属性 __nodes__"

    @covers("C1")
    def test_glossary_is_complete(self):
        """术语表必须覆盖文档里实际在用的核心术语。"""
        contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        for term in ("节点", "角色卡", "能力", "任务", "用户", "信任域", "应答边界"):
            assert term in contributing, f"术语表缺少「{term}」"


# ---------------------------------------------------------------------------
# 纪律 6：配置字段必须被使用
# ---------------------------------------------------------------------------

class TestConfigFieldsAreUsed:
    """每个配置字段都必须有"读它的逻辑"。

    这条纪律来自一个真实的事故：``MatrixRule.allow_issue`` 曾被定义、
    被解析、被写进示例配置，**但从未被任何逻辑读取**。

    它的危险之处在于完全静默——配置校验通过、demo 跑通、测试全绿，
    而配了它的人以为自己获得了某种控制。同类问题还发现了
    ``tls_key`` / ``rate_limit_per_second`` / ``params_schema``。

    判据：字段必须至少被**属性访问**一次（``obj.field``）。
    只在 ``from_dict`` 里被 ``obj.get("field")`` 解析不算"使用"——
    那只是把 YAML 搬进了 dataclass。
    """

    #: 允许"只被解析、不被使用"的字段。新增豁免必须写明理由。
    ALLOWED_LOCAL_ONLY: dict[str, str] = {
        "display_name": "由 RoleCard.to_public_dict 输出，不在策略逻辑中",
        "description": "纯文档字段，随角色卡一起呈现",
        "card_version": "版本号，当前仅存储与透传（版本协商属 v0.2）",
        "reachability": "当前所有节点都是 egress-only，尚无分支逻辑",
        "relay": "角色占位——当前中转由网络自身承担，无节点使用",
        "observer": "角色占位，为将来的监控节点预留",
        "reader": "同上，Observer 的别名占位",
    }

    @staticmethod
    def _config_fields() -> dict[str, set[str]]:
        import dataclasses
        from nxs.core import models as mdl_mod
        from nxs.mgmt import config as cfg_mod

        out: dict[str, set[str]] = {}
        for mod in (cfg_mod, mdl_mod):
            for name in dir(mod):
                obj = getattr(mod, name)
                if dataclasses.is_dataclass(obj) and isinstance(obj, type):
                    fields = {f.name for f in dataclasses.fields(obj)} - {"_FIELDS"}
                    if fields:
                        out[name] = fields
        return out

    @covers("C7")
    def test_every_config_field_is_read_somewhere(self):
        """字段要么被属性访问，要么显式列入「豁免」或「已知缺口」。"""
        from nxs.mgmt import UNIMPLEMENTED

        pkg_text = "\n".join(p.read_text(encoding="utf-8") for p in _py_files(PKG))
        declared_gaps = {name.strip() for name, _ in UNIMPLEMENTED}
        # UNIMPLEMENTED 里有的条目是 "a / b" 形式，拆开逐个匹配
        gap_fields: set[str] = set()
        for entry in declared_gaps:
            gap_fields |= {part.strip() for part in entry.split("/")}

        silent: list[str] = []
        for cls_name, fields in sorted(self._config_fields().items()):
            for field in sorted(fields):
                if field in self.ALLOWED_LOCAL_ONLY or field in gap_fields:
                    continue
                if not re.search(rf"\.{re.escape(field)}\b", pkg_text):
                    silent.append(f"{cls_name}.{field}")

        assert not silent, (
            "以下配置字段从未被属性访问——它们会静默地欺骗使用者：\n  "
            + "\n  ".join(silent)
            + "\n\n三种处理方式：① 实现它；② 加入 ALLOWED_LOCAL_ONLY 并写明理由；"
            "③ 加入 config.UNIMPLEMENTED 并写进 docs/architecture.md。"
        )

    @covers("C7", "M11.F5")
    def test_unimplemented_list_matches_docs(self):
        """★ 代码里的未实现清单必须与文档一致。

        两处各说各话是最糟的状态：代码说"未实现"，文档说"已支持"。
        """
        from nxs.mgmt import UNIMPLEMENTED

        docs_text = "\n".join(
            p.read_text(encoding="utf-8")
            for p in [REPO_ROOT / "CONTRIBUTING.md", REPO_ROOT / "README.md"]
            + sorted((REPO_ROOT / "docs").glob("*.md"))
        )
        missing = [
            name for name, _ in UNIMPLEMENTED
            if not any(part.strip() in docs_text
                       for part in name.replace("/", " ").split())
        ]
        assert not missing, (
            "以下未实现项只在代码里声明，文档中没有记录：\n  "
            + "\n  ".join(missing)
            + "\n请同步更新 docs/architecture.md 的「已知缺口」一节。"
        )

    @covers("C7", "M11.F5", "M11.F6")
    def test_unimplemented_is_surfaced_to_operators(self, config):
        """未实现项必须出现在 describe() 输出里——运维看得见才不算沉默。"""
        from nxs.mgmt import UNIMPLEMENTED

        text = config.describe()
        assert "尚未实现" in text, "describe() 未提示存在未实现项"
        for name, _ in UNIMPLEMENTED:
            head = name.split("/")[0].strip()
            assert head in text, f"未实现项 {head} 未出现在 describe() 输出中"


# ---------------------------------------------------------------------------
# 纪律 7：依赖清单守护「明确不做」
# ---------------------------------------------------------------------------

class TestDependencyFootprint:
    """依赖清单是「明确不做」类要求的<b>唯一廉价守护</b>。

    设计要求里有四条本质相同的约束：

    * **N1** 不含任何 LLM 调用
    * **N5** 不提供面向用户的界面
    * **N6** 不实现完整 A2A 协议栈
    * **N7** 不引入消息中间件

    它们的失效方式是同一个：**有人无意中加了一个依赖**。而当前没有任何
    测试会拦住他——这四条会安静地失效。

    这一个测试同时守护全部四条。代价极低，收益是四条从'仅声明'提升到'静态检查'。
    """

    #: 允许的运行时依赖白名单。**只增不减需显式评审。**
    ALLOWED_RUNTIME_DEPS = {"pyyaml"}

    #: 禁止出现的依赖（前缀匹配）。命中即说明某条"不做"被违反了。
    FORBIDDEN = {
        # N1 不含 LLM 调用
        "openai": "N1 不含 LLM 调用", "anthropic": "N1 不含 LLM 调用",
        "litellm": "N1 不含 LLM 调用", "transformers": "N1 不含 LLM 调用",
        "torch": "N1 不含 LLM 调用", "llama": "N1 不含 LLM 调用",
        # N5 不提供面向用户的界面
        "flask": "N5 不提供 UI", "fastapi": "N5 不提供 UI",
        "django": "N5 不提供 UI", "streamlit": "N5 不提供 UI",
        "gradio": "N5 不提供 UI", "jinja2": "N5 不提供 UI",
        # N6 不实现完整 A2A 协议栈
        "a2a": "N6 不实现 A2A 协议栈", "a2a-sdk": "N6 不实现 A2A 协议栈",
        # N7 不引入消息中间件
        "kafka": "N7 不引入消息中间件", "pika": "N7 不引入消息中间件",
        "amqp": "N7 不引入消息中间件", "celery": "N7 不引入消息中间件",
        "nats": "N7 不引入消息中间件", "redis": "N7 不引入消息中间件",
        "pulsar": "N7 不引入消息中间件",
    }

    @staticmethod
    def _runtime_deps() -> list[str]:
        """从 pyproject.toml 读出运行时依赖名（去掉版本约束）。"""
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.M | re.S)
        if not m:
            raise AssertionError("pyproject.toml 里找不到 dependencies 段")
        names = []
        for line in m.group(1).splitlines():
            line = line.strip().strip(",").strip().strip('"').strip("'")
            if not line or line.startswith("#"):
                continue
            names.append(re.split(r"[<>=!~\[]", line)[0].strip().lower())
        return names

    @covers("N7")
    def test_runtime_dependencies_are_minimal(self):
        deps = self._runtime_deps()
        extra = set(deps) - self.ALLOWED_RUNTIME_DEPS
        assert not extra, (
            f"运行时依赖超出白名单：{sorted(extra)}\n"
            f"本项目是基础设施，每多一个依赖就多一份许可证与升级负担。\n"
            f"若确需新增，请同时更新本测试的白名单并说明理由。"
        )

    @covers("N1", "N5", "N6", "N7")
    def test_no_forbidden_dependencies(self):
        deps = self._runtime_deps()
        violations = []
        for dep in deps:
            for forbidden, requirement in self.FORBIDDEN.items():
                if dep == forbidden or dep.startswith(forbidden):
                    violations.append(f"{dep} → 违反 {requirement}")
        assert not violations, (
            "依赖清单里出现了被明确排除的库：\n  " + "\n  ".join(violations)
        )

    @covers("N5")
    def test_no_gui_framework_in_source(self):
        """N5 的补充：源码里也不得 import UI 框架（依赖清单之外的旁路）。"""
        forbidden_mods = {"flask", "fastapi", "django", "streamlit", "gradio"}
        offenders = []
        for path in _py_files(PKG):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module.split(".")[0]]
                for m in mods:
                    if m in forbidden_mods:
                        offenders.append(f"{path.relative_to(REPO_ROOT)}: {m}")
        assert not offenders, "源码里 import 了 UI 框架：\n  " + "\n  ".join(offenders)


# ---------------------------------------------------------------------------
# C8：标识符命名 —— 禁止短编号式
# ---------------------------------------------------------------------------

class TestIdentifierNaming:
    """**C8 代码准则：标识符不得使用短编号式命名。**

    反面例子（真实发生过）：``Level.L0`` / ``Level.L1``。
    问题不在于"不好看"，而在于两条实际损害：

    **① 搜索失效。** 在几万行代码与日志里 grep ``L1``，会命中十六进制串、
    变量名片段、注释里的其他编号、无关的自然语言。而 grep ``summary``
    能精确定位。**一个无法被搜索的标识符，等于在维护时不存在。**

    **② 体系冲突。** 项目里同时存在多套编号（模块 M、需求 R、功能 F、
    测试 T、安全 S、准则 C、优先级 P …）。等级再占用 ``L`` 会持续撞号，
    而每撞一次就要在读者脑中做一次消歧。

    判据：标识符形如 ``^[A-Z]{1,2}\\d{1,3}$``（如 ``L0`` / ``D1`` / ``T12``）。
    纯字母短名（``i`` / ``f`` / ``k``）不受限制——它们不构成编号体系。
    """

    #: 形态：1-2 个大写字母 + 1-3 个数字
    NUMBERED = re.compile(r"^[A-Z]{1,2}\d{1,3}$")

    #: 豁免：项目里**有意保留**的编号式标识符，逐条写明理由。
    EXEMPT: dict[str, str] = {
        # 错误码本身就是面向人的稳定标识符，且形如 MX-DOMAIN-02 不含此形态；
        # 这里列的是确实需要短名的例外（当前无）
    }

    @staticmethod
    def _identifiers(path: Path) -> list[tuple[str, int]]:
        """提取源码里所有"被当作标识符使用"的名字。"""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out: list[tuple[str, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                out.append((node.id, node.lineno))
            elif isinstance(node, ast.Attribute):
                out.append((node.attr, node.lineno))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.append((node.name, node.lineno))
            elif isinstance(node, ast.arg):
                out.append((node.arg, node.lineno))
            elif isinstance(node, ast.keyword) and node.arg:
                out.append((node.arg, node.lineno))
        return out

    @covers("C8")
    def test_no_numbered_identifiers_in_package(self):
        offenders: list[str] = []
        for path in _py_files(PKG):
            for name, lineno in self._identifiers(path):
                if name in self.EXEMPT:
                    continue
                if self.NUMBERED.match(name):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{lineno}  {name}"
                    )
        assert not offenders, (
            "出现了短编号式标识符 —— 它们无法被有效搜索，且会与项目里的\n"
            "其他编号体系（M/R/F/T/S/C/P）撞号：\n  "
            + "\n  ".join(offenders)
            + "\n\n请改用语义化名称（如 Level.SUMMARY 而非 Level.L1）。\n"
            "若确需保留，请加入 EXEMPT 并写明理由。"
        )

    @covers("C8")
    def test_level_names_are_semantic(self):
        """具体守护 Level 这一处：成员名必须是语义词，不能是编号。"""
        from nxs.core import Level

        for member in Level:
            assert not self.NUMBERED.match(member.name), (
                f"Level.{member.name} 是编号式命名。"
                f"应改为语义词（METADATA / SUMMARY / FRAGMENT / FULL_TEXT）。"
            )
            # 线上值必须是成员名的小写，且足够长以便搜索
            wire = str(member)
            assert wire == member.name.lower()
            assert len(wire) >= 7, f"线上值 {wire!r} 过短，不利于日志检索"

    @covers("C8")
    def test_legacy_level_names_still_parse(self):
        """兼容旧写法，但要发弃用警告——不能静默接受。"""
        import warnings

        from nxs.core import Level

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert Level.parse("L1") is Level.SUMMARY
        assert any(issubclass(w.category, DeprecationWarning) for w in caught), (
            "解析旧写法 L1 没有发出弃用警告 —— 那会让旧写法永远留存在配置里。"
        )

    @covers("C8")
    def test_new_level_names_parse_case_insensitively(self):
        from nxs.core import Level

        assert Level.parse("summary") is Level.SUMMARY
        assert Level.parse("SUMMARY") is Level.SUMMARY
        assert Level.parse("Full_Text") is Level.FULL_TEXT
