"""部署物的测试 —— 保证"发出去的部署材料本身是对的"。

为什么部署物也要测
------------------
部署文档与配置模板是**最容易腐烂**的东西：它们不参与编译、不被引用，
改坏了没人知道，直到某天真的去部署才发现写不通。

这个项目已经有前车之鉴：``make demo`` 曾经带着子命令不接受的参数，
三个月没人发现，因为测试都绕过了 CLI。

所以这里对 ``deploy/`` 下的每一件东西做实打实的校验：
配置模板能不能被真正加载、systemd unit 的关键加固项在不在、
部署自检能不能识别出问题。
"""

from __future__ import annotations

import pytest

from conftest import REPO_ROOT, covers

DEPLOY = REPO_ROOT / "deploy"

#: 模板里协调节点的凭据（模板不含真实值，测试用假值）
TEMPLATE_ENV = {"NXS_SECRET_HUB": "template-test-secret-0123456789"}


# ---------------------------------------------------------------------------
# 1. 生产配置模板
# ---------------------------------------------------------------------------

class TestProductionTemplate:
    """模板必须真的能被 ``load_config`` 加载 —— 否则等于发了一份错的说明书。"""

    @covers("M11.F7")
    def test_domains_declared_and_usable_by_matrix(self):
        """★ 声明了域之后，矩阵才能引用**尚未注册**的节点所属的域。

        这是"生产配置根本写不出来"的阻塞：工作节点是自注册的，
        协调节点启动时它还不存在。若域只能从节点卡片推导，
        矩阵里写 ``hub → zone-work`` 会被判为"引用了不存在的域"。
        """
        from nxs.mgmt.config import load_config

        cfg = load_config(DEPLOY / "local.yaml.example", env=TEMPLATE_ENV)
        assert cfg.domains, "生产模板必须声明 domains"
        # 矩阵里引用的 zone-work 在配置里**没有任何节点**，只能是声明来的
        assert "zone-work" in cfg.known_domains()
        assert not cfg.nodes_in_domain("zone-work"), (
            "测试前提：zone-work 域下配置里不该有节点（它靠自注册接入）"
        )
        assert any(r.to_domain == "zone-work" for r in cfg.policy.rules)

    @covers("M11.F8")
    def test_undeclared_domain_is_rejected(self):
        """矩阵引用了未声明的域 → 拒。防配置笔误。"""
        from nxs.core.errors import ConfigError
        from nxs.mgmt.config import load_config

        import tempfile
        from pathlib import Path

        raw = (DEPLOY / "local.yaml.example").read_text(encoding="utf-8")
        tampered = raw.replace("to_domain: zone-work", "to_domain: zone-typo", 1)
        assert tampered != raw
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(tampered)
            path = Path(handle.name)
        try:
            with pytest.raises(ConfigError) as excinfo:
                load_config(path, env=TEMPLATE_ENV)
            assert "zone-typo" in str(excinfo.value)
        finally:
            path.unlink(missing_ok=True)

    @covers("M11.F8")
    def test_declaring_domains_relaxes_static_type_check(self):
        """声明了 domains = 接受自注册节点 ⇒ 不再静态核对类型名。

        理由：那些节点的能力运行时才出现。反过来，没声明 domains 的
        配置（所有节点都在文件里）仍应严格核对——见 test_policy 的用例。
        """
        from nxs.mgmt.config import load_config

        # 模板里 allow_types 含 work.calendar，而配置里只有 hub 节点、
        # hub 并不提供 work.calendar —— 严格核对会失败，自注册模式应放行
        cfg = load_config(DEPLOY / "local.yaml.example", env=TEMPLATE_ENV)
        hub_types = {c.type for c in cfg.nodes["hub"].capabilities}
        assert "work.calendar" not in hub_types, "测试前提：hub 不提供 work.calendar"
        assert any("work.calendar" in r.allow_types for r in cfg.policy.rules)

    @covers("M11.F1")
    def test_template_loads(self):
        from nxs.mgmt.config import load_config

        cfg = load_config(DEPLOY / "local.yaml.example", env=TEMPLATE_ENV)
        assert cfg.nodes, "模板里应至少包含协调节点"

    @covers("M11.F1", "M11.F5")
    def test_template_uses_persistent_stores(self):
        """★ 生产模板**必须**用 sqlite，不能用 memory。

        memory 后端进程退出即丢：重启后所有节点注册、所有在途任务全没，
        等于每次部署都是一次事故。
        """
        from nxs.mgmt.config import load_config

        cfg = load_config(DEPLOY / "local.yaml.example", env=TEMPLATE_ENV)
        assert cfg.stores.queue != "memory", "生产配置不能用 memory 队列"
        assert cfg.stores.registry != "memory", "生产配置不能用 memory 注册表"

    @covers("M11.F1", "S9")
    def test_template_does_not_auto_approve(self):
        """★ 生产模板不应自动批准任何域。

        ``auto_approve_domains`` 非空意味着该域下**所有**节点（含未来新接入的）
        自动获批。生产环境应当是"默认拒绝 + 逐个批准"。
        """
        from nxs.mgmt.config import load_config

        cfg = load_config(DEPLOY / "local.yaml.example", env=TEMPLATE_ENV)
        assert cfg.policy.auto_approve_domains == (), (
            "生产模板自动批准了域，新节点会跳过人工审批"
        )

    @covers("M11.F1", "M3.F10")
    def test_template_declares_domains(self):
        """★ 模板必须声明 ``domains``。

        没声明时域只能从节点卡片推导，而工作节点是自注册的、启动时不存在——
        矩阵里就写不出 ``hub → zone-work``。这是"生产配置根本写不出来"的阻塞。
        """
        from nxs.mgmt.config import load_config

        cfg = load_config(DEPLOY / "local.yaml.example", env=TEMPLATE_ENV)
        assert cfg.domains, "生产模板没有声明 domains，矩阵无法引用自注册节点的域"
        assert {"hub", "zone-work"} <= cfg.known_domains()

    @covers("M11.F1")
    def test_template_has_no_credentials(self):
        """模板里**不得出现任何凭据** —— 凭据走环境变量。

        这条用"模板不带 secret 字段 + 缺 env 时加载失败"来验证。
        """
        from nxs.core.errors import ConfigError
        from nxs.mgmt.config import load_config

        with pytest.raises(ConfigError) as excinfo:
            load_config(DEPLOY / "local.yaml.example", env={})
        assert "凭据" in str(excinfo.value) or "secret" in str(excinfo.value).lower()

    @covers("M11.F1")
    def test_template_has_matrix_rules(self):
        """模板要有可用的矩阵规则，否则部署完什么都跑不了。"""
        from nxs.mgmt.config import load_config

        cfg = load_config(DEPLOY / "local.yaml.example", env=TEMPLATE_ENV)
        assert cfg.policy.rules, "生产模板没有任何矩阵规则"


# ---------------------------------------------------------------------------
# 2. systemd unit
# ---------------------------------------------------------------------------

class TestSystemdUnit:

    @pytest.fixture
    def unit_text(self) -> str:
        return (DEPLOY / "nexus-mesh.service").read_text(encoding="utf-8")

    @covers("M14.F1")
    def test_unit_does_not_run_as_root(self, unit_text):
        """★ 不能用 root 跑。

        这个进程监听公网端口、解析外部输入。用 root 跑意味着一个解析漏洞
        直接等于整机失守。
        """
        assert "User=root" not in unit_text
        assert "User=nxs" in unit_text

    @covers("M14.F1")
    def test_unit_keeps_secrets_out_of_the_file(self, unit_text):
        """凭据不能写进 unit 文件 —— 它在 /etc/systemd/system 下容易被 diff、被备份。"""
        assert "EnvironmentFile=" in unit_text
        assert "NXS_SECRET" not in unit_text, "凭据写进了 unit 文件"
        assert "NXS_OPS_SECRET=" not in unit_text

    @covers("M14.F1", "M7.R1")
    def test_unit_restarts_and_stops_gracefully(self, unit_text):
        """要 always 重启（协调节点是单点），停止要留时间给在途请求。"""
        assert "Restart=always" in unit_text
        assert "TimeoutStopSec=" in unit_text

    @covers("M14.F1")
    def test_unit_hardening_present(self, unit_text):
        """关键加固项必须在。

        这些不是装饰：ProtectSystem=strict + ReadWritePaths 让这个进程
        **只能写自己的数据目录**，即使被拿下也动不了系统其余部分。
        """
        for directive in (
            "NoNewPrivileges=true",
            "ProtectSystem=strict",
            "ReadWritePaths=",
            "ProtectHome=true",
            "PrivateTmp=true",
            "RestrictAddressFamilies=",
        ):
            assert directive in unit_text, f"缺少加固项 {directive}"

    @covers("M14.F1")
    def test_unit_points_at_the_configured_paths(self, unit_text):
        """ExecStart 的配置路径要与 env 文件、数据目录一致 —— 不一致会启动即失败。"""
        assert "--config /etc/nexus-mesh/local.yaml" in unit_text
        assert "EnvironmentFile=/etc/nexus-mesh/nxs.env" in unit_text


# ---------------------------------------------------------------------------
# 3. 部署自检
# ---------------------------------------------------------------------------

class TestDeployCheck:
    """``nxs doctor`` 必须在真部署前就能发现问题。"""

    @covers("M14.F8")
    def test_doctor_lists_all_critical_checks(self):
        """自检项必须覆盖那些"能启动但会出事"的问题。"""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "nxs", "doctor",
             "--config", "config/nxs.example.yaml"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
        )
        output = result.stdout
        for item in ("存储后端是持久化的", "运维通道已启用",
                     "未自动批准任何信任域", "角色卡是确定性的"):
            assert item in output, f"自检缺少关键项：{item}"

    @covers("M14.F8")
    def test_doctor_passes_on_example_config(self):
        """示例配置在**非生产模式**下应当返回 0 —— 否则新人第一次跑就红灯。

        注意：示例配置会用 memory 后端、会不配 TLS，这些在非生产模式下
        只是"需要关注"（WARN）而不是失败。这是刻意的：本地试跑不该被
        生产标准拦住，但也不能假装没问题。
        """
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "nxs", "doctor",
             "--config", "config/nxs.example.yaml"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "未通过" not in result.stdout, "示例配置不该有失败项"
        # 关键项必须真的被检查了
        assert "存储后端是持久化的" in result.stdout
        assert "角色卡是确定性的" in result.stdout

    @covers("M14.F8")
    def test_doctor_flags_memory_backend_for_production(self, workdir):
        """★ 生产模式下用 memory 后端，自检必须**返回非零**。

        这是最容易犯又最难发现的部署错误：跑起来一切正常，
        重启之后所有节点都得重新注册、重新批准。
        """
        import os
        import subprocess
        import sys

        cfg = workdir / "prod.yaml"
        cfg.write_text(
            (REPO_ROOT / "deploy" / "local.yaml.example").read_text(encoding="utf-8")
            .replace("queue: sqlite", "queue: memory")
            .replace("registry: sqlite", "registry: memory")
            .replace("/var/lib/nexus-mesh", str(workdir / "data")),
            encoding="utf-8",
        )
        full_env = {**os.environ,
                    "NXS_SECRET_HUB": "doctor-test-secret-0123456789",
                    "NXS_OPS_SECRET": "doctor-ops-secret-0123456789"}
        result = subprocess.run(
            [sys.executable, "-m", "nxs", "doctor", "--config", str(cfg),
             "--production"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
            env=full_env,
        )
        assert result.returncode != 0, "memory 后端在生产模式下没有被拦下"
        assert "memory" in (result.stdout + result.stderr)
