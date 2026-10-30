"""`nxs-cli` 的凭据解析与命令测试。

核心关注点：**Agent 的上下文里不该出现密钥**。
所以凭据按身份存在文件里，Agent 只报自己的名字。
"""

from __future__ import annotations

import sys

import pytest

from conftest import REPO_ROOT, covers

sys.path.insert(0, str(REPO_ROOT))

from nxs.cli.actor import (  # noqa: E402
    MIN_SECRET_LENGTH,
    load_actor,
    resolve_actor,
    write_actor_file,
)
from nxs.core.errors import ConfigError  # noqa: E402

SECRET = "a" * 64


def _write(workdir, name: str, *, node_id: str | None = None,
           secret: str = SECRET, url: str = "https://x/nxs",
           ca: str | None = None):
    """在 workdir 下造一份凭据文件。"""
    d = workdir / "agents"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        f"NXS_NODE_ID={node_id if node_id is not None else name}",
        f"NXS_COORDINATOR_URL={url}",
        f"NXS_SECRET={secret}",
    ]
    if ca:
        lines.append(f"NXS_CA_FILE={ca}")
    (d / f"{name}.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return d


class TestLoadActor:
    """按名字加载身份。"""

    @covers("M14.F3")
    def test_loads_all_fields(self, workdir):
        d = _write(workdir, "taskflow", ca="/etc/nexus-mesh/tls/ca.pem")
        a = load_actor("taskflow", actor_dir=d, env={})
        assert a.node_id == "taskflow"
        assert a.url == "https://x/nxs"
        assert a.secret == SECRET.encode()
        assert a.ca_file == "/etc/nexus-mesh/tls/ca.pem"
        assert a.source.endswith("taskflow.env")

    @covers("M14.F3")
    def test_url_trailing_slash_stripped(self, workdir):
        """URL 末尾的斜杠要去掉 —— 签名覆盖路径，多一个斜杠就验不过。"""
        d = _write(workdir, "a1", url="https://x/nxs/")
        assert load_actor("a1", actor_dir=d, env={}).url == "https://x/nxs"

    @covers("M14.F3")
    def test_ca_is_optional(self, workdir):
        """公网证书场景不需要 CA。"""
        d = _write(workdir, "a1")
        assert load_actor("a1", actor_dir=d, env={}).ca_file is None

    @covers("M14.F3")
    def test_masked_never_leaks_secret(self, workdir):
        """★ 可打印形式里**绝不能有密钥**。

        这个方法会被用来打日志、显示错误上下文。
        密钥一旦进了日志或 LLM 上下文，就没法再当秘密了。
        """
        d = _write(workdir, "a1")
        a = load_actor("a1", actor_dir=d, env={})
        text = a.masked()
        assert SECRET not in text
        assert "a1" in text  # 身份要在
        assert "https://x/nxs" in text

    @covers("M14.F3")
    def test_comments_and_blank_lines_tolerated(self, workdir):
        d = workdir / "agents"
        d.mkdir(parents=True, exist_ok=True)
        (d / "a1.env").write_text(
            "# 这是注释\n\nNXS_NODE_ID=a1\n\nNXS_COORDINATOR_URL=https://x/nxs\n"
            f"NXS_SECRET={SECRET}\n",
            encoding="utf-8",
        )
        assert load_actor("a1", actor_dir=d, env={}).node_id == "a1"


class TestActorNameHardening:
    """★ 身份名的形状必须收紧 —— 它同时是文件名。"""

    @covers("M14.F3")
    @pytest.mark.parametrize("bad", [
        "../etc/passwd",      # 路径穿越
        "/abs/path",          # 绝对路径
        "a/b",                # 带斜杠
        "..",                 # 上级目录
        "Taskflow",           # 大写（会与环境变量命名规则不一致）
        "",                   # 空
        "a b",                # 空格
        "-lead",              # 以连字符开头
        "x" * 100,            # 过长
    ])
    def test_rejects_bad_names(self, workdir, bad):
        d = _write(workdir, "ok")
        with pytest.raises(ConfigError):
            load_actor(bad, actor_dir=d, env={})

    @covers("M14.F3")
    def test_accepts_normal_names(self, workdir):
        d = _write(workdir, "hermes-working")
        assert load_actor("hermes-working", actor_dir=d, env={}).node_id == \
            "hermes-working"


class TestSelfConsistency:
    """★ 文件里的 NXS_NODE_ID 必须等于文件名。

    ⚠ 这是最重要的一条。没有它，一个 Agent 可以把 ``--as coordinator``
    指向自己的文件 —— 若文件里的 NXS_NODE_ID 不被校验，
    它就会以 coordinator 的身份签名。

    那不只是越权：审计日志会指向**错误的节点**，
    事后追查时会认为是 coordinator 干的。
    """

    @covers("M14.F3")
    def test_mismatch_is_rejected(self, workdir):
        d = _write(workdir, "taskflow", node_id="coordinator")
        with pytest.raises(ConfigError) as ei:
            load_actor("taskflow", actor_dir=d, env={})
        msg = str(ei.value)
        assert "NXS_NODE_ID" in msg
        assert "coordinator" in msg

    @covers("M14.F3")
    def test_missing_node_id_key_is_tolerated(self, workdir):
        """没写 NXS_NODE_ID 时用文件名 —— 不算矛盾。

        刻意宽松：文件名本身就是身份，多写一遍是冗余。
        但**写了就必须一致**（见上一条）。
        """
        d = workdir / "agents"
        d.mkdir(parents=True, exist_ok=True)
        (d / "a1.env").write_text(
            f"NXS_COORDINATOR_URL=https://x/nxs\nNXS_SECRET={SECRET}\n",
            encoding="utf-8",
        )
        assert load_actor("a1", actor_dir=d, env={}).node_id == "a1"


class TestMissingAndBroken:
    """缺文件、缺键、密钥过短 —— 都要给出可执行的提示。"""

    @covers("M14.F3")
    def test_missing_file_lists_available(self, workdir):
        """★ 找不到时要列出**现有身份**。

        运维最常见的困惑是"我明明建了文件"，而实际名字对不上
        （建了 ``a_b.env`` 但身份叫 ``a-b``）。列出可选项能省掉这一轮。
        """
        d = _write(workdir, "taskflow")
        _write(workdir, "hermes-working")
        with pytest.raises(ConfigError) as ei:
            load_actor("typo", actor_dir=d, env={})
        msg = str(ei.value)
        assert "taskflow" in msg and "hermes-working" in msg

    @covers("M14.F3")
    def test_missing_file_with_empty_dir(self, workdir):
        d = workdir / "agents"
        d.mkdir(parents=True, exist_ok=True)
        with pytest.raises(ConfigError) as ei:
            load_actor("a1", actor_dir=d, env={})
        assert "还没有任何身份文件" in str(ei.value)

    @covers("M14.F3")
    def test_missing_required_keys(self, workdir):
        d = workdir / "agents"
        d.mkdir(parents=True, exist_ok=True)
        (d / "a1.env").write_text("NXS_NODE_ID=a1\n", encoding="utf-8")
        with pytest.raises(ConfigError) as ei:
            load_actor("a1", actor_dir=d, env={})
        assert "NXS_SECRET" in str(ei.value)

    @covers("M14.F3")
    def test_short_secret_rejected(self, workdir):
        """★ 过短的密钥比没有更危险 —— 会让人以为配好了。

        与网关对运维密钥的处理一致。
        """
        d = _write(workdir, "a1", secret="short")
        with pytest.raises(ConfigError) as ei:
            load_actor("a1", actor_dir=d, env={})
        assert "过短" in str(ei.value)
        assert str(MIN_SECRET_LENGTH) in str(ei.value)

    @covers("M14.F3")
    def test_malformed_line_reports_line_number(self, workdir):
        d = workdir / "agents"
        d.mkdir(parents=True, exist_ok=True)
        (d / "a1.env").write_text("NXS_NODE_ID=a1\n这不是键值对\n",
                                  encoding="utf-8")
        with pytest.raises(ConfigError) as ei:
            load_actor("a1", actor_dir=d, env={})
        assert ":2:" in str(ei.value), "要报行号，否则长文件里找不到"


class TestResolveActor:
    """"这次调用用哪个身份"的解析优先级。"""

    @covers("M14.F3")
    def test_explicit_wins(self, workdir):
        d = _write(workdir, "taskflow")
        a = resolve_actor("taskflow", actor_dir=d,
                          env={"NXS_ACTOR": "other"})
        assert a.node_id == "taskflow"

    @covers("M14.F3")
    def test_nxs_actor_env(self, workdir):
        """★ 桥接进程注入的就是这个 —— Agent 只需知道自己的名字。"""
        d = _write(workdir, "taskflow")
        a = resolve_actor(None, actor_dir=d, env={"NXS_ACTOR": "taskflow"})
        assert a.node_id == "taskflow"

    @covers("M14.F3")
    def test_nxs_node_id_env_fallback(self, workdir):
        d = _write(workdir, "taskflow")
        a = resolve_actor(None, actor_dir=d, env={"NXS_NODE_ID": "taskflow"})
        assert a.node_id == "taskflow"

    @covers("M14.F3")
    def test_no_identity_is_an_error_not_a_default(self, workdir):
        """★ 什么都不给时报错，**不猜默认值**。

        猜错身份会让任务以错误的名义发出，而那种错误在审计里
        看起来完全正常 —— 比直接失败难查得多。
        """
        d = _write(workdir, "taskflow")
        with pytest.raises(ConfigError) as ei:
            resolve_actor(None, actor_dir=d, env={})
        msg = str(ei.value)
        assert "--as" in msg
        assert "NXS_ACTOR" in msg


class TestWriteActorFile:
    """写凭据文件时的权限。"""

    @covers("M14.F3")
    def test_writes_with_0600(self, workdir):
        """★ 凭据文件等同密钥，必须只有属主可读。"""
        d = workdir / "agents"
        path = write_actor_file("taskflow", url="https://x/nxs",
                                secret=SECRET, actor_dir=d)
        assert path.exists()
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, f"权限应为 0600，实际 {oct(mode)}"

    @covers("M14.F3")
    def test_roundtrip(self, workdir):
        d = workdir / "agents"
        write_actor_file("taskflow", url="https://x/nxs", secret=SECRET,
                         ca_file="/ca.pem", actor_dir=d)
        a = load_actor("taskflow", actor_dir=d, env={})
        assert a.secret == SECRET.encode()
        assert a.ca_file == "/ca.pem"

    @covers("M14.F3")
    def test_rejects_bad_name(self, workdir):
        d = workdir / "agents"
        with pytest.raises(ConfigError):
            write_actor_file("../evil", url="https://x/nxs",
                             secret=SECRET, actor_dir=d)


class TestCliEntry:
    """入口本身。"""

    @covers("M14.F3")
    def test_help_mentions_as_and_ask(self):
        import subprocess

        r = subprocess.run(
            [sys.executable, "-m", "nxs.cli.main", "--help"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0
        out = r.stdout
        assert "--as" in out
        assert "ask" in out
        # 示例要能照抄
        assert "nxs-cli" in out

    @covers("M14.F3")
    def test_no_identity_exits_2_with_hint(self):
        """没有身份时退出码 2，并在 stderr 说明怎么给。"""
        import os
        import subprocess

        env = {k: v for k, v in os.environ.items()
               if k not in ("NXS_ACTOR", "NXS_NODE_ID")}
        env["NXS_ACTOR_DIR"] = str(REPO_ROOT / ".nxs" / "no-such-dir")
        r = subprocess.run(
            [sys.executable, "-m", "nxs.cli.main", "whoami"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, env=env,
        )
        assert r.returncode == 2
        assert "NXS_ACTOR" in (r.stderr or "")

    @covers("M14.F3")
    def test_does_not_expose_ops_command(self):
        """★ `nxs-cli` 不得有 ops 子命令。

        运维面（批准/吊销节点）不该和 Agent 的日常工具在同一个入口 ——
        那样 `--as <节点>` 的调用就能摸到 ops，需要额外检查，
        而那种检查忘了加就是提权。
        """
        import subprocess

        r = subprocess.run(
            [sys.executable, "-m", "nxs.cli.main", "--help"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        for forbidden in ("ops", "serve", "demo", "doctor"):
            assert forbidden not in r.stdout, (
                f"nxs-cli 暴露了 {forbidden} —— 运维面不该在这里"
            )


class TestWhoamiDisplaysNewModel:
    """★★ `nxs-cli whoami` 必须显示**角色模型**的信息。

    ⚠ **这一节来自一次实机故障（2026-10-30）。**

    改造删掉了 `constraints` 类。但 `cmd_whoami` 仍在读那份已删的
    发布白名单（旧 `constraints` 下的字段）—— 因为用的是 `.get()`，
    **不报错，只是永远显示「（无发布权）」**。

    实测：coordinator 的角色是 `hub`（can_issue=True），whoami 却说它
    没有发布权。**使用者看到的界面在说反话**，而所有测试都是绿的 ——
    因为没有任何测试断言过 whoami 的权限展示。

    教训有两层：

        ① 展示层也要有测试。它是使用者的第一手信息，错了比 API 错更误导
        ② `.get()` 的宽容会掩盖字段被删 —— 静默降级成"永远显示默认值"
    """

    def _fake(self, monkeypatch, *, can_issue: bool, role: str = "hub"):
        """把 _transport 换成假的，whoami 返回构造好的响应。"""
        from nxs.cli import commands as C

        class _T:
            def whoami(self_inner):
                return {
                    "node_id": "coordinator",
                    "approved": True,
                    "state": "alive",
                    "card": {
                        "display_name": "协调中枢",
                        "domain": "cloud",
                        "role": role,
                        "may_broadcast": False,
                        "capabilities": [
                            {"type": "work.summary", "level": "summary",
                             "mode": "sync"},
                        ],
                        "self_limits": {"max_backlog": 20,
                                        "max_response_level": "summary"},
                    },
                    "role_definition": {
                        "can_issue": can_issue,
                        "serves": ["work.summary"],
                        "max_response_level": "summary",
                    },
                    "usage": {"pending": 0},
                }

        monkeypatch.setattr(C, "_transport", lambda actor: _T())

    @covers("M14.F3")
    def test_shows_can_issue_when_allowed(self, monkeypatch, capsys):
        """★ can_issue=True 必须显示「可发布」。

        这条就是那个 bug 的直接回归测试：旧代码恒显示「（无发布权）」。
        """
        from nxs.cli.commands import cmd_whoami

        self._fake(monkeypatch, can_issue=True)
        args = type("A", (), {"json": False})()
        cmd_whoami(actor=None, args=args)

        out = capsys.readouterr().out
        assert "可发布" in out, f"can_issue=True 却没显示可发布：\n{out}"
        assert "（无发布权）" not in out, "还在显示旧文案 —— 说明又读了已删字段"
        assert "hub" in out

    @covers("M14.F3")
    def test_shows_cannot_issue_when_denied(self, monkeypatch, capsys):
        """can_issue=False 显示「不可发布」。"""
        from nxs.cli.commands import cmd_whoami

        self._fake(monkeypatch, can_issue=False, role="worker")
        args = type("A", (), {"json": False})()
        cmd_whoami(actor=None, args=args)

        out = capsys.readouterr().out
        assert "不可发布" in out, f"can_issue=False 却没显示不可发布：\n{out}"

    @covers("M14.F3")
    def test_warns_when_role_not_in_config(self, monkeypatch, capsys):
        """★ 角色不在配置表里 → 必须明确警告。

        这正是实机踩到的状态：桥接写 `role: driver` 而配置里只有
        hub/agent/worker。节点一切"正常"（心跳、批准都对），
        但发不出也收不到任何任务。展示层必须把这个说出来。
        """
        from nxs.cli import commands as C
        from nxs.cli.commands import cmd_whoami

        class _T:
            def whoami(self_inner):
                return {
                    "node_id": "hermes-working",
                    "approved": True,
                    "state": "alive",
                    "card": {
                        "display_name": "H", "domain": "cloud",
                        "role": "driver",
                        "may_broadcast": False,
                        "capabilities": [{"type": "agent.ask",
                                          "level": "summary", "mode": "sync"}],
                        "self_limits": {"max_backlog": 5,
                                        "max_response_level": "summary"},
                    },
                    "role_definition": None,      # ← 角色查不到
                    "usage": {"pending": 0},
                }

        monkeypatch.setattr(C, "_transport", lambda actor: _T())
        args = type("A", (), {"json": False})()
        cmd_whoami(actor=None, args=args)

        out = capsys.readouterr().out
        assert "不在服务端配置" in out, (
            f"角色查不到时必须明确警告，否则用户不知道为什么不工作：\n{out}"
        )

    @covers("M14.F3")
    def test_shows_self_limits_and_backlog(self, monkeypatch, capsys):
        """自限与信箱待办要显示（配额已删，这是替代信息）。"""
        from nxs.cli.commands import cmd_whoami

        self._fake(monkeypatch, can_issue=True)
        args = type("A", (), {"json": False})()
        cmd_whoami(actor=None, args=args)

        out = capsys.readouterr().out
        assert "自限" in out and "20" in out
        assert "信箱" in out
        # 旧的配额说法不该再出现（按日任务数那套已整体删除）
        assert "按日" not in out
        assert "配额" not in out


class TestTimeoutPairingIsCoherent:
    """★★ 调用方等待必须**长于**节点执行超时。

    ⚠ 这两个值住在不同模块里，没有任何机制强制它们配合 ——
    2026-10-30 调整前它们**恰好都是 180**，形成竞态。

    ## 为什么相等是 bug

    ```
    t=0    调用方开始等，节点开始执行
    t=180  两个同时到期 —— 谁先谁后不确定
    ```

    若调用方先到：

        调用方报「超时」→ 用户看到"没回答"
        节点继续跑到自己的上限 → 答案写入任务记录，**但没人读**

    那比干脆失败更糟：**用户以为节点没干活，而它干了。**

    ## 正确的关系

        调用方等待 > 节点执行超时

    这样"节点超时"会先发生 —— 用户收到的是节点给出的**具体失败原因**
    （如"轮次用尽，调大 --max-turns"），而不是一句笼统的"超时"。

    ## 为什么放在这个文件

    `DEFAULT_ASK_TIMEOUT` 在 CLI 侧，`DEFAULT_TIMEOUT` 在桥接侧。
    测试是唯一两边都能看的地方 —— 这种跨模块的不变式只能在这里守。
    """

    @covers("M14.F3")
    def test_caller_waits_longer_than_node_runs(self):
        """★ 调用方等待 > 节点执行超时。"""
        from examples.agent_bridge_common import DEFAULT_TIMEOUT
        from nxs.cli.commands import DEFAULT_ASK_TIMEOUT

        assert DEFAULT_ASK_TIMEOUT > DEFAULT_TIMEOUT, (
            f"调用方等待（{DEFAULT_ASK_TIMEOUT}s）不大于节点执行超时"
            f"（{DEFAULT_TIMEOUT}s）。\n"
            "  相等或更短会形成竞态：调用方先放弃时，节点仍在跑，\n"
            "  结果写进任务记录但没人读 —— 用户以为它没干活。"
        )

    @covers("M14.F3")
    def test_margin_is_enough_for_polling_granularity(self):
        """余量要够跑轮询与收尾，不能只差 1 秒。

        `ask` 是 2 秒一次轮询。余量小于一个轮询周期时，
        节点刚回结果、调用方可能已经错过了那一拍。
        """
        from examples.agent_bridge_common import DEFAULT_TIMEOUT
        from nxs.cli.commands import DEFAULT_ASK_TIMEOUT, POLL_INTERVAL

        margin = DEFAULT_ASK_TIMEOUT - DEFAULT_TIMEOUT
        assert margin >= POLL_INTERVAL * 5, (
            f"余量只有 {margin}s，少于 5 个轮询周期"
            f"（{POLL_INTERVAL * 5}s）。\n"
            "  紧贴着的两个超时仍会抖动 —— 节点刚回结果，调用方可能已错过。"
        )

    @covers("M14.F3")
    def test_node_timeout_is_generous_enough_for_real_tasks(self):
        """★ 节点超时不能太小 —— 实测 p95 已经撞过 180 秒。

        2026-10-30 实测本机 CodeBuddy 的延迟：p50=82.5s，**p95=180.8s**。
        180 秒上限意味着 p95 附近的任务在成功/失败之间抖动，
        而"读仓库回答问题"本来就要 100+ 秒。

        这不是"要求更快"能解决的 —— 是工具轮次本身就多。
        """
        from examples.agent_bridge_common import DEFAULT_TIMEOUT

        assert DEFAULT_TIMEOUT >= 240, (
            f"节点超时只有 {DEFAULT_TIMEOUT}s。实测 p95 已达 180.8s，\n"
            "  240 以下会让 p95 附近的任务在成功/失败之间随机抖动。"
        )
