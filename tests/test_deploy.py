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
TEMPLATE_ENV = {"NXS_SECRET_COORDINATOR": "template-test-secret-0123456789"}


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
        矩阵里写 ``coordinator → zone-work`` 会被判为"引用了不存在的域"。
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

        # 模板里 allow_types 含 work.calendar，而配置里只有 coordinator 节点、
        # coordinator 并不提供 work.calendar —— 严格核对会失败，自注册模式应放行
        cfg = load_config(DEPLOY / "local.yaml.example", env=TEMPLATE_ENV)
        coordinator_types = {c.type for c in cfg.nodes["coordinator"].capabilities}
        assert "work.calendar" not in coordinator_types, "测试前提：coordinator 不提供 work.calendar"
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
        矩阵里就写不出 ``coordinator → zone-work``。这是"生产配置根本写不出来"的阻塞。
        """
        from nxs.mgmt.config import load_config

        cfg = load_config(DEPLOY / "local.yaml.example", env=TEMPLATE_ENV)
        assert cfg.domains, "生产模板没有声明 domains，矩阵无法引用自注册节点的域"
        # known_domains() 返回的是**域**，不是节点 ID —— 这里要 cloud
        assert {"cloud", "zone-work"} <= cfg.known_domains()

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
                    "NXS_SECRET_COORDINATOR": "doctor-test-secret-0123456789",
                    "NXS_OPS_SECRET": "doctor-ops-secret-0123456789"}
        result = subprocess.run(
            [sys.executable, "-m", "nxs", "doctor", "--config", str(cfg),
             "--production"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
            env=full_env,
        )
        assert result.returncode != 0, "memory 后端在生产模式下没有被拦下"
        assert "memory" in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# 4. 产物大小实测脚本
# ---------------------------------------------------------------------------

class TestArtifactSizeProbe:
    """``examples/probe_artifact_size.py`` 是回答"能传多大"的工具。

    它必须真的能跑 —— 一份跑不起来的测量工具，比没有更糟：
    人会照着它给出的数字做决策。
    """

    @covers("M9.F2")
    def test_probe_script_runs_and_reports_limit(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "examples/probe_artifact_size.py"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        out = result.stdout
        # 必须报出上限
        assert "max_body_bytes" in out
        # 必须给出实测结论（而不是只说"看配置"）
        assert "信封能带约" in out
        # 必须有被拒的证据 —— 否则说明探测没真的加到超限
        assert "FM-BODY-04" in out, "探测没有触及上限，结论不可信"

    @covers("M9.F2")
    def test_reference_payload_does_not_scale_with_file_size(self):
        """"传坐标"的核心性质：信封大小与文件大小**无关**。

        这条用纯计算验证，不依赖脚本：
        10 MB 和 100 MB 的引用，payload 应当一样小。
        """
        import json

        small = {"path": "/d/a.pdf", "sha256": "a" * 64, "size": 10 * 1024 * 1024}
        large = {"path": "/d/b.pdf", "sha256": "b" * 64, "size": 100 * 1024 * 1024}
        assert abs(len(json.dumps(small)) - len(json.dumps(large))) < 10
        assert len(json.dumps(small)) < 200


class TestPublishedTemplatesPassVerify:
    """★ 随仓库发布的每个配置模板，都必须能通过 ``nxs verify``。

    ⚠ **这条测试是在真机部署时才发现该写的。**

    2026-10-27 在云服务器上按文档部署，第一步 ``nxs verify`` 就 FAIL 三项：

    * 矩阵域检查失败 —— ``verify`` 从**节点卡片**推导域，忽略了 ``domains:``
      声明段。而生产配置里 ``nodes`` 只有 coordinator 自己（工作节点靠自注册接入），
      矩阵却引用了 ``zone-work``。**``domains:`` 段的存在意义正是解决这个**，
      verify 却没用它。
    * coordinator 能力等级矛盾 —— 模板声明能力 ``level: summary``，
      应答上限却写 ``max_response_level: metadata``，自相矛盾。

    为什么 480 个测试没抓到？因为 ``TestProductionTemplate`` 测的是
    **库函数** ``cfg.known_domains()``（它是对的），而 ``nxs verify``
    在 CLI 里**自己另写了一遍错的推导**。

    > 覆盖了实现，不等于覆盖了用户走的那条路。CLI 就是用户走的路。

    所以这条测试**跑真实 CLI**，而不是调库函数。
    """

    TEMPLATES = (
        ("deploy/local.yaml.example", "生产部署模板"),
        ("config/nxs.example.yaml", "示例配置（demo 用）"),
    )

    @pytest.mark.parametrize("rel,label", TEMPLATES)
    @covers("M11.F1")
    def test_template_passes_verify(self, rel, label):
        import os
        import subprocess
        import sys

        from nxs.mgmt.config import EXAMPLE_CONFIG_PATH, demo_env_for

        path = REPO_ROOT / rel
        assert path.exists(), f"{label}不见了：{rel}"

        env = dict(os.environ)
        env.update(demo_env_for(EXAMPLE_CONFIG_PATH))

        result = subprocess.run(
            [sys.executable, "-m", "nxs", "verify", "--config", str(path)],
            cwd=REPO_ROOT, capture_output=True, text=True, env=env, timeout=180,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, (
            f"{label}（{rel}）跑不过 nxs verify —— "
            f"照文档部署第一步就会卡住：\n{output[-1500:]}"
        )
        assert "全部通过" in output, (
            f"{label} 的 verify 输出了非预期内容：\n{output[-1500:]}"
        )

    @covers("M11.F1")
    def test_verify_uses_declared_domains(self):
        """★ 矩阵域检查必须认 ``domains:`` 声明，而不只是节点卡片。

        这是上面那个 bug 的最小复现：生产模板里 zone-work 域
        **没有任何节点**，只有声明。verify 必须通过。
        """
        import os
        import subprocess
        import sys

        from nxs.mgmt.config import EXAMPLE_CONFIG_PATH, demo_env_for

        env = dict(os.environ)
        env.update(demo_env_for(EXAMPLE_CONFIG_PATH))
        result = subprocess.run(
            [sys.executable, "-m", "nxs", "verify",
             "--config", str(DEPLOY / "local.yaml.example")],
            cwd=REPO_ROOT, capture_output=True, text=True, env=env, timeout=180,
        )
        output = result.stdout + result.stderr
        # 不能出现"域存在"失败
        assert "✗ 矩阵" not in output, (
            f"verify 仍报矩阵域检查失败 —— 它没有使用 cfg.known_domains()：\n{output}"
        )
        for domain in ("zone-work", "zone-home"):
            assert f"{domain} 域存在" not in output or "✓" in output
        assert "域存在" in output, "verify 应该逐条列出矩阵域检查结果"


class TestDoctorDistinguishesSelfOccupation:
    """★ ``doctor --production`` 要能区分"自己在跑"与"别人占着端口"。

    ⚠ **这条来自真机部署的体验问题。**

    服务装好、systemd 起来了，然后跑 ``nxs doctor --production`` 想确认
    一切就绪 —— 结果 FAIL，理由是"端口已被占用"。

    但端口正是 nexus-mesh 自己在用！**"部署成功后自检"的典型场景，
    得到的却是失败。** 用户只能自己去 ``ps`` 里翻，而两种情况的
    后果完全不同：

    * 自己在跑 → 一切正常，这是**好**消息
    * 别人占着 → 生产环境必然启动失败，要拦住

    判据必须能分开这两件事，否则这个检查在它最该有用的时刻给错答案。
    """

    @covers("M14.F8")
    def test_socket_probe_host_maps_wildcard_to_loopback(self):
        """``0.0.0.0`` 是监听地址，不是可连接目标 —— 探活要换成回环。"""
        from nxs.__main__ import socket_probe_host

        assert socket_probe_host("0.0.0.0") == "127.0.0.1"
        assert socket_probe_host("::") == "127.0.0.1"
        assert socket_probe_host("") == "127.0.0.1"
        # 具体地址原样返回
        assert socket_probe_host("192.168.1.5") == "192.168.1.5"
        assert socket_probe_host("127.0.0.1") == "127.0.0.1"

    @covers("M14.F8")
    def test_probe_recognises_running_nexus(self):
        """★ 真的起一个 nexus-mesh，探测要认出它 —— 端到端，不是 mock。

        用 ``Gateway`` 起真实 HTTP，端口交给系统分配（``port=0``），
        所以不会和别的测试撞车。
        """
        from dataclasses import replace

        from nxs.__main__ import _port_serves_nexus
        from nxs.app import NexusApp
        from nxs.edge.gateway import Gateway
        from nxs.mgmt.config import (
            EXAMPLE_CONFIG_PATH, ServerConfig, demo_env_for,
        )

        env = demo_env_for(EXAMPLE_CONFIG_PATH)
        app = NexusApp.from_config_path(
            str(EXAMPLE_CONFIG_PATH), env=env, force_memory=True)
        app.start()

        gateway = Gateway(
            app, server_config=ServerConfig(host="127.0.0.1", port=0),
            ops_secret=b"",
        ).start()
        try:
            real_port = gateway.port          # 系统分配的真实端口
            # 探测用的配置要指向这个端口、且不带 TLS（Gateway 这里没配 TLS）
            cfg = replace(app.config, server=ServerConfig(
                host="127.0.0.1", port=real_port, tls_cert=None, tls_key=None))
            assert _port_serves_nexus("127.0.0.1", real_port, cfg), (
                "探测没能认出自己起的 nexus-mesh —— 部署后自检会误报失败"
            )
            # 顺带确认 0.0.0.0 也会被正确换成回环
            cfg_wild = replace(cfg, server=replace(cfg.server, host="0.0.0.0"))
            from nxs.__main__ import socket_probe_host
            assert _port_serves_nexus(
                socket_probe_host(cfg_wild.server.host), real_port, cfg_wild)
        finally:
            gateway.stop()

    @covers("M14.F8")
    def test_probe_returns_false_for_foreign_port(self):
        """★ 别人占着端口时，探测必须返回 False（从而判 fail）。

        起一个普通 HTTP 服务（不是 nexus-mesh）占住端口，
        探测应该认不出它 —— 因为 /nxs/health 的响应形状不同。
        """
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading

        from nxs.__main__ import _port_serves_nexus
        from nxs.mgmt.config import load_config, EXAMPLE_CONFIG_PATH, demo_env_for

        class Foreign(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = b'{"hello": "not nexus"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Foreign)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            cfg = load_config(str(EXAMPLE_CONFIG_PATH),
                              env=demo_env_for(EXAMPLE_CONFIG_PATH))
            # 这个配置没有 TLS，探测会用 http，正好打到这个外来服务
            assert cfg.server.tls_cert is None, "测试前提：示例配置不带 TLS"
            assert not _port_serves_nexus("127.0.0.1", port, cfg), (
                "把外来服务误认成了 nexus-mesh —— 生产环境会把"
                "「端口被别人占了」当成「一切正常」"
            )
        finally:
            server.shutdown()
            server.server_close()
