"""★★ 观察窗 token：从「每次重启随机」改为「派生 + 显式轮换」（2026-11-13）。

## 决策的来龙去脉

用户问：「现在有了安全认证的机制，Token 是否还需要每次都变呢？」

查证后发现两个独立属性被混在一起了：

========================  ==================================
属性                       由什么提供
========================  ==================================
**不可猜测**               256 位熵（与频率无关）
**每次变**                 重启时重新生成
========================  ==================================

「每次变」唯一的价值是**泄露后重启即止血**。而它的代价是实测到的：
一次会话部署 5 次，用户正在看的面板就失效 5 次。

前提也变了 —— 主入口统一到服务台 Authelia 两因素之后：

    路径 A（公网）  Caddy → Authelia 密码+TOTP → 8080
    路径 B（本机）  直接连 127.0.0.1:8080（不过 Authelia）

lighthouse 这类低权限用户能连路径 B。**token 必须存在**（挡住这条横向
移动），但「每次变」对这条路毫无贡献 —— 他无论 token 变不变都拿不到。

所以：**固定 + 显式轮换**。止血没消失，只是从「顺手重启」变成
「改 gen 重启」（`deploy/rotate-ui-token.sh`）。

## 这个文件守什么

1. 派生是确定性的（重启不变）
2. 换 gen / 换密钥都会让旧 token 失效（止血能力不能丢）
3. 不落盘（铁律④）
4. 弱密钥要拒绝（否则派生出一个"看起来安全"的 token）
"""

from __future__ import annotations

import pathlib
import re

import pytest

from conftest import REPO_ROOT, covers
from nxs.core.errors import ConfigError
from nxs.ui.server import (DEFAULT_TOKEN_GEN, TOKEN_BYTES, UiServer,
                           derive_token)

SECRET = b"ui-test-secret-0123456789abcdef"


class TestDerivation:
    """派生函数的三个性质。"""

    @covers("M15.F2")
    def test_deterministic_for_same_secret_and_gen(self):
        """★★★ 同输入同输出 —— 这是「部署不踢人」的全部依据。"""
        assert derive_token(SECRET, "0") == derive_token(SECRET, "0")
        assert derive_token(SECRET, "7") == derive_token(SECRET, "7")

    @covers("M15.F2")
    def test_changing_gen_changes_token(self):
        """★★ 换 gen 必须换 token —— 这是止血手段。"""
        seen = {derive_token(SECRET, g) for g in ("0", "1", "2", "abc")}
        assert len(seen) == 4, f"不同 gen 产生了相同 token：{seen}"

    @covers("M15.F2")
    def test_changing_secret_changes_token(self):
        """★★ 换运维密钥顺带让 token 作废 —— 这符合直觉。"""
        a = derive_token(b"a" * 32, "0")
        b = derive_token(b"b" * 32, "0")
        assert a != b

    @covers("M15.F2")
    def test_length_and_charset_match_url_safety(self):
        """★ 43 字符、URL 安全 —— 与原来的 token_urlsafe(32) 一致。

        ⚠ 这不是审美问题：token 要进 ``?t=`` 查询串和 cookie，含 ``+/=``
        就得额外编码，而编码差异是"看起来一样却对不上"的经典来源。
        """
        token = derive_token(SECRET, "0")
        assert len(token) == 43, (
            f"长度 {len(token)} 与原来不一致（43）—— 下游可能按长度校验"
        )
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token), (
            f"token 含 URL 不安全字符：{token}"
        )
        assert "=" not in token, "残留 padding，进 URL 会需要编码"

    @covers("M15.F2")
    def test_rejects_weak_secret(self):
        """★★ 弱密钥必须拒绝，不能"派生出一个看起来安全的 token"。

        ⚠ 这种失败最难发现：token 输出仍然是 43 个随机字符，肉眼看不出
        任何问题，但实际可暴力枚举。所以必须在派生入口就拦住。
        """
        for weak in (b"", b"short", b"x" * 15):
            with pytest.raises(ConfigError):
                derive_token(weak, "0")

    @covers("M15.F2")
    def test_empty_gen_falls_back_to_default(self):
        """★ gen 为 None/空时用默认值，不能崩。

        配置没设这个环境变量是最常见的情况。
        """
        assert derive_token(SECRET, "") == derive_token(SECRET,
                                                        DEFAULT_TOKEN_GEN)
        assert derive_token(SECRET, None) == derive_token(SECRET,
                                                         DEFAULT_TOKEN_GEN)

    @covers("M15.F2")
    def test_token_is_not_the_secret_itself(self):
        """★ 派生结果不能包含密钥内容 —— 这是个愚蠢但真实的风险。

        如果实现写成 ``secret.decode()`` 之类，token 就成了密钥本身，
        而 token 是要发到浏览器的。
        """
        secret = b"very-secret-value-abcdefghijklmn"
        token = derive_token(secret, "0")
        assert "very-secret" not in token
        assert secret.hex() not in token


class TestServerWiring:
    """UiServer 接上派生函数。"""

    @covers("M15.F2")
    def test_server_uses_derived_token(self):
        """★ 构造出的 token 必须等于独立算出的派生值。"""
        srv = UiServer("http://127.0.0.1:1/nxs", SECRET, port=0,
                       token_gen="3")
        assert srv.token == derive_token(SECRET, "3")
        assert srv.token_gen == "3"

    @covers("M15.F2")
    def test_default_gen_is_used_when_absent(self):
        srv = UiServer("http://127.0.0.1:1/nxs", SECRET, port=0)
        assert srv.token_gen == DEFAULT_TOKEN_GEN
        assert srv.token == derive_token(SECRET, DEFAULT_TOKEN_GEN)

    @covers("M15.F2")
    def test_gen_from_env_is_honoured(self, monkeypatch):
        """★ 环境变量能配置（部署走这条，不是命令行参数）。"""
        import os

        from nxs.__main__ import build_parser

        monkeypatch.setenv("NXS_UI_TOKEN_GEN", "42")
        assert os.environ["NXS_UI_TOKEN_GEN"] == "42"
        # 解析层面确认参数存在（真正的读取在 cmd_ui 里）
        parser = build_parser()
        args = parser.parse_args(["ui", "--token-gen", "9"])
        assert args.token_gen == "9"


class TestTokenNotOnDisk:
    """铁律④：token 不落盘。

    ⚠ 固定化之后这条更值得守：如果实现偷懒写成「第一次随机生成后存到
    /etc/nexus-mesh/ui-token 复用」，磁盘上就多了一份凭据副本，
    而它比 ui.env 更容易被误备份/误提交（因为它是个新文件，不在
    既有白名单里）。
    """

    @covers("M15.F2")
    def test_no_token_file_created(self):
        srv = UiServer("http://127.0.0.1:1/nxs", SECRET, port=0)
        token = srv.token
        suspects = [pathlib.Path("/tmp") / n
                    for n in ("nxs-ui-token", ".nxs-ui-token", "ui-token")]
        suspects += [pathlib.Path("/etc/nexus-mesh") / n
                     for n in ("ui-token", "ui.token")]
        for path in suspects:
            if path.is_file():
                assert token not in path.read_text(errors="ignore"), (
                    f"token 被写进了 {path} —— 违反「不落盘」"
                )

    @covers("M15.F2")
    def test_source_does_not_write_token(self):
        """★ 源码里不能出现把 token 写文件的操作。"""
        text = (REPO_ROOT / "nxs" / "ui" / "server.py").read_text()
        # 找 write_text / open(..., "w") 且同行或邻近提到 token
        suspicious = []
        for match in re.finditer(r"\.write_text\(|open\([^)]*['\"]w", text):
            start = max(0, match.start() - 200)
            window = text[start:match.start() + 200]
            if "token" in window.lower():
                suspicious.append(window.strip()[-120:])
        assert not suspicious, (
            "疑似把 token 写到文件：\n  " + "\n  ".join(suspicious)
        )


class TestRotationScript:
    """★★ 轮换脚本必须真的能改到 unit 并验证生效。

    ⚠ 这个脚本是「固定 token」的代价承担者。它坏了，止血能力就没了 ——
    而坏的表现会是「脚本说成功、实际没换」，那比没有脚本更糟。
    """

    @staticmethod
    def _script() -> str:
        return (REPO_ROOT / "deploy" / "rotate-ui-token.sh").read_text()

    @covers("M15.F2")
    def test_script_exists_and_executable(self):
        path = REPO_ROOT / "deploy" / "rotate-ui-token.sh"
        assert path.exists()
        assert path.stat().st_mode & 0o111

    @covers("M15.F2")
    def test_script_verifies_after_restart(self):
        """★★ 改完必须**核对进程内实际值**，不能只信 systemctl。

        「改了 unit 但没重启」「重启了但改错文件」都会让脚本看起来成功。
        实测过的同款事故：部署脚本说完成、进程环境里却没有新变量。
        """
        text = self._script()
        assert "/proc/" in text and "environ" in text, (
            "没有核对进程内实际值 —— 会漏掉「改了但没生效」"
        )
        assert "exit 1" in text, "核对失败时必须报错退出"

    @covers("M15.F2")
    def test_script_matches_gen_line_precisely(self):
        """★ 必须精确匹配 ``NXS_UI_TOKEN_GEN=`` 行首。

        ⚠ 用宽松匹配会误改 ``NXS_UI_TOKEN_GEN_OLD`` 这类前缀相近的变量，
        或者改到注释里的示例。这类错误会静默发生。

        ⚠ 目标文件**改过**（2026-11-13）：从 unit 改为 ui.env。
        原来改 unit 会被部署脚本同步覆盖，等于撤销轮换。
        """
        text = self._script()
        assert "grep -oP" in text and "\\K" in text, (
            "没有精确提取当前值 —— 宽松匹配可能取到相邻变量的值"
        )
        # 写入时用锚定行首的正则（现在针对 ui.env 的行格式）
        assert "^NXS_UI_TOKEN_GEN=" in text, (
            "写入用的正则没锚定行首 —— 可能改到前缀相近的变量"
        )

    @covers("M15.F2")
    def test_envfile_is_the_source_of_gen(self):
        """★ gen 的**唯一来源**是 ui.env，不是 unit。

        ⚠ 这条替换了原来的 ``test_unit_declares_token_gen``。
        原断言要求 unit 里必须有 gen —— 那正是导致「轮换被部署撤销」
        的设计。现在反过来：unit 里不该有，ui.env 里必须有。
        """
        text = self._script()
        assert "ENVFILE=/etc/nexus-mesh/ui.env" in text, (
            "轮换脚本没指向 ui.env —— gen 会写回被部署覆盖的 unit"
        )
        # 且读当前值时读的是 ENVFILE
        assert 'grep -oP \'^NXS_UI_TOKEN_GEN=\\K\\S+\' "$ENVFILE"' in text, (
            "读当前 gen 时没读 ui.env"
        )

    @covers("M15.F2")
    def test_no_sed_i_for_unit_edit(self):
        """★ 不用 ``sed -i`` 改 unit。

        ⚠ ``sed -i`` 在 BSD/GNU 上参数不同，且改错会让服务起不来 ——
        而这是启动配置。脚本用 python 改，跨平台且可校验。
        """
        text = self._script()
        code_lines = [ln for ln in text.splitlines()
                      if ln.strip() and not ln.strip().startswith("#")]
        code = "\n".join(code_lines)
        assert "sed -i" not in code, "用 sed -i 改 unit，跨平台会咬人"


class TestUnifiedEntryScript:
    """★★ 统一入口脚本（``deploy/open-ui.sh``）替代旧的隧道脚本。

    ## 背景

    原来是 ``nxs-view.sh``：查服务 → **建 SSH 隧道** → 从 journalctl
    取 token → 打开浏览器。它只存在于本机 ``~/nexus-mesh-deploy/``，
    **不在版本管理里** —— 和 deploy.sh 分裂是同一类问题。

    有了服务台的统一入口（``nexus.<域名>``，Authelia 两因素）之后，
    隧道和手抄 token 都不必要了。

    ## 这个文件守什么

        · 不再有隧道逻辑（那是被明确废掉的路）
        · ``--rotate`` 必须存在（token 固定化后它是唯一止血手段）
        · 脚本进版本管理（否则又会漂移）
    """

    @staticmethod
    def _script() -> str:
        return (REPO_ROOT / "deploy" / "open-ui.sh").read_text()

    @covers("M15.F2")
    def test_script_is_versioned(self):
        """★ 必须在仓库里 —— 旧脚本只在某台机器上，改不到也审计不到。"""
        path = REPO_ROOT / "deploy" / "open-ui.sh"
        assert path.exists(), "统一入口脚本没进版本管理"
        assert path.stat().st_mode & 0o111, "没有可执行位"

    @covers("M15.F2")
    def test_no_tunnel_logic(self):
        """★★ 不该再有建隧道的代码。

        ⚠ 判据剥掉注释与字符串后再查 —— 头部注释里会提到"SSH 隧道"
        （说明为什么不需要它），那是解释不是行为。
        """
        raw = self._script()
        code = "\n".join(
            ln for ln in raw.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        )
        code = re.sub(r'"[^"]*"', '""', code)
        code = re.sub(r"'[^']*'", "''", code)

        assert "-L " not in code, "脚本里还有 -L 端口转发（隧道）"
        assert "-f -N" not in code, "脚本里还有 -f -N（后台隧道）"
        assert "tunnel_pids" not in code, "还有隧道 pid 管理逻辑"

    @covers("M15.F2")
    def test_has_rotate_mode(self):
        """★★ `--rotate` 必须存在 —— 这是 token 固定化后的唯一止血手段。"""
        text = self._script()
        assert "--rotate" in text, "没有轮换入口，泄露时无法止血"
        # 必须真的调用轮换脚本，不能只是打印一句话
        code = "\n".join(
            ln for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        )
        assert "rotate-ui-token.sh" in code, (
            "--rotate 没有真的调用轮换脚本 —— 那是个假开关"
        )

    @covers("M15.F2")
    def test_points_at_hub_url(self):
        """★ 默认指向服务台的域名入口，而不是本机回环。"""
        text = self._script()
        assert "nexus." in text and "https://" in text, (
            "默认入口不是 HTTPS 域名 —— 那还是隧道思路"
        )
        # 不应把 127.0.0.1 当默认开放地址
        m = re.search(r'UI_URL="\$\{NXS_UI_URL:-(?P<default>[^}]+)\}"', text)
        assert m, "找不到 UI_URL 默认值"
        assert "127.0.0.1" not in m.group("default"), (
            f"默认入口是回环地址 {m.group('default')!r} —— "
            "统一入口应该是公网域名"
        )


class TestTokenNotInLogs:
    """★★ 启动输出不该打印 token —— 这是轮换模式下新增的泄露面收窄。

    ⚠ 原来 ``nxs ui`` 启动时会打印 ``http://127.0.0.1:8080/?t=<token>``，
    而 systemd 把 stdout 收进 journald。于是 token 出现在：

        · journalctl（会被备份、会被 grep 到别处）
        · 任何 ``journalctl -u nexus-mesh-ui | grep t=`` 的人眼里

    在「token 每次变」的时代这无伤大雅 —— 十分钟后就作废了。但改成
    **固定 token** 之后，日志里那一行就是一把长期有效的钥匙。
    所以启动输出一并改口径：只打印服务台入口，不打印 token。

    ⚠ 这条测试守的是「不要改回去」：将来有人为了排障方便把 token
    加回启动输出，会让日志重新变成泄露面，而且**没人会注意到**。
    """

    @staticmethod
    def _main_src() -> str:
        return (REPO_ROOT / "nxs" / "__main__.py").read_text()

    @covers("M15.F2")
    def test_startup_output_does_not_print_entry_url_with_token(self):
        """★★ **有统一入口时**不许打印 ``entry_url``（它含 ?t=token）。

        ⚠ 判据必须区分两个分支（第一版写成「整个函数都不许出现 entry_url」，
        结果误伤了降级分支）：

            配了 recover_url   → 打印服务台入口，**不**打印带 token 的链接
            没配 recover_url   → 打印本机 entry_url 是**唯一出路**，允许

        后者的场景是本地开发/应急排障，那时没有别的入口；而生产配了统一
        入口，日志里就不该出现一把长期有效的钥匙。
        """
        text = self._main_src()
        start = text.find("def cmd_ui(")
        assert start > 0
        nxt = text.find("\ndef ", start + 10)
        body = text[start:nxt if nxt > 0 else len(text)]

        # 找到 `if server.recover_url:` 分支，到它的 else 之前
        i = body.find("if server.recover_url:")
        assert i > 0, "找不到「有统一入口」的分支 —— 结构变了，改这里的解析"
        j = body.find("else:", i)
        assert j > i, "找不到 else 分支"
        hub_branch = body[i:j]

        assert "entry_url" not in hub_branch, (
            "配了统一入口却仍打印 entry_url（含 ?t=token）—— "
            "那行会进 journald，成为长期有效的泄露面"
        )
        assert "hub" in hub_branch or "入口" in hub_branch, (
            "有统一入口却不打印它 —— 用户不知道从哪进"
        )

    @covers("M15.F2")
    def test_startup_output_points_at_hub(self):
        """★ 输出要说清从哪进 —— 只删不补会让人不知道怎么办。"""
        text = self._main_src()
        start = text.find("def cmd_ui(")
        nxt = text.find("\ndef ", start + 10)
        body = text[start:nxt if nxt > 0 else len(text)]

        assert "入口" in body, "启动输出没说从哪进入"
        assert "Authelia" in body or "服务台" in body, (
            "没说清统一入口是经过服务台认证的"
        )

    @covers("M15.F2")
    def test_check_script_derives_instead_of_scraping_logs(self):
        """★★ ``open-ui.sh --check`` 要算 token，不能从日志抓。

        两个理由：
          ① 日志里已经没有 token 了，抓不到
          ② 就算有，那也是把凭据当字符串处理；而它可以从密钥算出来
        """
        text = (REPO_ROOT / "deploy" / "open-ui.sh").read_text()
        assert "ui-token:v1:gen" in text, (
            "--check 没有自己算 token —— 说明还在依赖日志抓取"
        )


class TestCheckWorksOnServer:
    """★★ `nxs-open --check` 在**服务器上**也必须能用。

    ⚠⚠ **这来自一次实测失败（2026-11-13）。**

    脚本原来无条件走 ``ssh_cmd``，而部署时它被装到服务器的
    ``/usr/local/bin/nxs-open``。于是运维在服务器上敲：

        $ nxs-open --check
          找不到 SSH 密钥：/root/Downloads/key4workbuddy.pem
          设 NXS_SSH_KEY 指向正确的密钥。

    —— 人已经在服务器上了，根本不需要 SSH，却被要求提供本机密钥路径。
    ``--check`` 恰好是「打不开时该先跑」的那个命令，它不可用最伤。

    修法：先判断本机/远端。判断依据是「本机有 nexus-mesh-ui 单元」，
    比比对 hostname 可靠（改名、换 IP、内网 DNS 都不影响）。
    """

    @staticmethod
    def _script() -> str:
        return (REPO_ROOT / "deploy" / "open-ui.sh").read_text()

    @covers("M15.F2")
    def test_detects_server_locally(self):
        text = self._script()
        assert "on_server()" in text, "没有本机/远端判断"
        assert "nexus-mesh-ui" in text, "判断依据缺失"
        # 走 systemctl 而不是 hostname 比对
        assert "list-unit-files" in text, (
            "用 hostname 之类判断会很脆（改名/换 IP 就失效）"
        )

    @covers("M15.F2")
    def test_check_does_not_require_key_on_server(self):
        """★★ 在服务器上不该要求 SSH 密钥。"""
        text = self._script()
        # 找 --check 分支
        i = text.find('if [ "$MODE" = "check" ]')
        assert i > 0
        j = text.find('if [ "$MODE" = "open" ]', i)
        if j < 0:
            j = text.find("# ------------------------------------------------------------------ 打开", i)
        branch = text[i:j if j > i else i + 1200]

        # 必须是条件式：on_server 时不要求
        assert "on_server || need_key" in branch or "on_server &&" in branch, (
            "check 分支无条件要密钥 —— 在服务器上跑会直接失败"
        )

    @covers("M15.F2")
    def test_warns_when_unit_and_process_gen_differ(self):
        """★★ gen 不一致必须报出来 —— 那是「改了没重启」。

        这正是本次部署里我踩过的坑：rotate 脚本改了 unit 并重启，
        我另写的验收脚本却从 ui.env 读 gen（那里没有），算出旧 token，
        一度以为"轮换没生效"。**配置有三处可能不一致时，必须显式比对。**
        """
        text = self._script()
        assert "unit_gen" in text, "没有读 unit 里的 gen 用来比对"
        assert "改了没重启" in text, "不一致时没有明确诊断"


class TestOnServerActuallyWorks:
    """★★★ 用**真 bash 跑** ``on_server``，而不是查文本里有没有它。

    ⚠⚠ **这个类是被一次真实失败逼出来的（2026-11-13）。**

    第一版守护写的是：

        assert "on_server()" in text
        assert "nexus-mesh-ui" in text

    —— 两条都绿，然后生产实测仍然失败：

        $ nxs-open --check
          找不到 SSH 密钥：/root/Downloads/key4workbuddy.pem

    真因是 ``set -euo pipefail`` + ``grep -q`` 的 SIGPIPE 陷阱：

        grep -q 找到匹配立即退出 → 上游 systemctl 被杀（141）
        → pipefail 让整条管道返回非零 → on_server 判成"不在服务器上"

    单看代码完全正常，grep 明明能匹配到那一行。**文本存在性判据守不住
    这种东西** —— 这已经是本项目第 5 次栽在同一个模式上。

    所以这里改成：造一个假的 ``systemctl``，用**真 bash**（带
    ``set -euo pipefail``）执行页面里的那段函数，看它的退出码。
    """

    @staticmethod
    def _extract_on_server() -> str:
        """从脚本里抠出 on_server 函数体。"""
        text = (REPO_ROOT / "deploy" / "open-ui.sh").read_text()
        start = text.find("on_server() {")
        assert start > 0, "找不到 on_server 函数"
        # 找到配对的收尾 }（第 0 列）
        end = text.find("\n}", start)
        assert end > start, "找不到函数结尾"
        return text[start:end + 2]

    def _run_with_fake_systemctl(self, units_output: str,
                                 *, expect: bool) -> None:
        import os
        import subprocess
        import tempfile

        func = self._extract_on_server()
        # 假 systemctl：只回 list-unit-files 的内容。输出量大一些，
        # 才容易触发 SIGPIPE（如果实现里还有 grep -q 的话）。
        bindir = tempfile.mkdtemp(prefix="nxs-fakebin-")
        fake = pathlib.Path(bindir) / "systemctl"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' {' '.join(repr(units_output).split())}\n",
            encoding="utf-8")
        # 上面那行太绕，直接写清楚
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "cat <<'OUT'\n" + units_output + "\nOUT\n",
            encoding="utf-8")
        fake.chmod(0o755)

        script = (
            "set -euo pipefail\n"
            + func + "\n"
            + "if on_server; then exit 0; else exit 9; fi\n"
        )
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        result = subprocess.run(["bash", "-c", script], capture_output=True,
                                text=True, env=env, timeout=30,
                                check=False)
        expected = 0 if expect else 9
        assert result.returncode == expected, (
            f"on_server 判断错误（期望退出码 {expected}，实际 "
            f"{result.returncode}）。\n"
            f"stderr: {result.stderr[-500:]}\n"
            f"假 systemctl 输出前 200 字: {units_output[:200]!r}"
        )

    @covers("M15.F2")
    def test_detects_present_unit(self):
        """★★★ 有 nexus-mesh-ui 单元 → 必须判成「在服务器上」。

        ⚠⚠ **造数条件必须真的能触发 SIGPIPE，否则这条测试是假绿。**

        第一版这里塞了 80 行填充（约 4 KB），结果：把实现改回
        ``grep -q`` 之后，**文本检查红了、这条行为测试却是绿的** ——
        因为 4 KB 远小于管道缓冲区（Linux 默认 64 KB），
        ``cat`` 一次就写完了，``grep -q`` 提前退出也不会让上游收到 SIGPIPE。

        真实条件需要两条同时成立：

            ① 输出量 > 管道缓冲区（否则上游在 grep 退出前就写完了）
            ② 匹配行靠近**开头**（否则上游快写完才匹配上）

        所以这里是 3000 行 ≈ 150 KB，目标行在第 10 行。
        实测这个组合能把 ``grep -q`` 的实现打红。
        """
        head = "UNIT FILE    STATE    PRESET"
        before = "\n".join(
            f"aaa-early-unit-{i:03d}.service    static    -" for i in range(9))
        after = "\n".join(
            f"zzz-late-unit-{i:04d}.service    static    -" for i in range(3000))
        units = "\n".join([
            head, before,
            "nexus-mesh-ui.service    enabled    enabled",
            after,
        ])
        assert len(units) > 64 * 1024, (
            f"假输出只有 {len(units)} 字节，没超过管道缓冲区 —— "
            "触发不了 SIGPIPE，这条测试会假绿"
        )
        self._run_with_fake_systemctl(units, expect=True)

    @covers("M15.F2")
    def test_detects_absent_unit(self):
        """★ 没有该单元 → 判成「不在服务器上」（本机场景）。"""
        units = ("UNIT FILE    STATE    PRESET\n"
                 "other.service    static    -\n"
                 "another.service  disabled  enabled\n")
        self._run_with_fake_systemctl(units, expect=False)

    @covers("M15.F2")
    def test_no_grep_q_in_pipe(self):
        """★★ 源码层面也不许出现 ``| grep -q`` —— 防有人改回去。

        ⚠ 这条是**辅助**：上面两条行为测试才是主判据。两者并存是因为
        行为测试依赖"假 systemctl 输出足够长"，而源码检查不依赖环境。

        ⚠⚠ 判据必须**剥掉注释**再查（第一版直接 ``in func`` 就误伤了）：
        函数里的注释正在解释这个陷阱，而解释里当然会写出那个写法。
        这是本项目第 6 次栽在「文本存在性判据」上 —— 同一个模式：
        只要判据是"这段文本里有没有某个词"，就一定会误伤或被绕过。
        """
        func = self._extract_on_server()
        # 只留非注释行
        code_lines = [
            ln for ln in func.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        code = "\n".join(code_lines)
        assert "grep -q" not in code, (
            "on_server 的**代码**里又出现了 grep -q —— 在 set -o pipefail "
            "下会因 SIGPIPE 误判。用 case 匹配或先落变量。"
        )


class TestRotationSurvivesDeploy:
    """★★★ 轮换结果必须**活过下一次部署**。

    ⚠⚠ **这来自一次实测（2026-11-13）。**

    轮换脚本第一版改的是 ``/etc/systemd/system/nexus-mesh-ui.service``
    里的 ``Environment=NXS_UI_TOKEN_GEN``。而我刚给部署脚本加了
    「③b 从仓库同步 unit」—— 于是：

        ① 运维跑 rotate-ui-token.sh        gen 0 → 1，旧 token 作废 ✓
        ② 下次部署（③b 同步 unit）          gen 被写回 0
        ③ 结果：**轮换被静默撤销，旧 token 重新有效**

    而部署脚本只会说「全部与仓库一致」。我在验收时看到 `gen=0` 才发现的
    —— 那一刻我以为是轮换失败了，其实是被部署覆盖了。

    ## 设计原则

        模板 → 仓库（unit 由部署同步）
        运行时状态 → 本机（ui.env 由 install 生成，部署不覆盖）

    ``gen`` 是运维动作改出来的运行时状态，所以它属于 ui.env。

    ⚠ 这也说明「部署以仓库为唯一源头」不能无条件套用：**模板可以**，
    状态不行。分不清这两者，就会做出"每次部署都重置运维操作"的系统。
    """

    @covers("M15.F2")
    def test_unit_does_not_set_gen(self):
        """★★ unit 里不许写死 gen —— 否则部署会覆盖轮换。"""
        unit = (REPO_ROOT / "deploy" / "nexus-mesh-ui.service").read_text()
        # 剥注释：注释里会解释为什么不该写
        code = "\n".join(
            ln for ln in unit.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        )
        assert "Environment=NXS_UI_TOKEN_GEN=" not in code, (
            "unit 里写死了 gen —— 部署同步 unit 时会把轮换结果写回默认值，"
            "止血动作在下一次部署后失效"
        )

    @covers("M15.F2")
    def test_rotate_writes_envfile_not_unit(self):
        """★★ 轮换必须写 ui.env，不能写 unit。"""
        text = (REPO_ROOT / "deploy" / "rotate-ui-token.sh").read_text()
        code = "\n".join(
            ln for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        )
        assert "ENVFILE=" in code and "ui.env" in code, (
            "轮换没写 ui.env —— 写 unit 会被部署覆盖"
        )
        # 写入目标必须是 ENVFILE
        assert '"$ENVFILE"' in code, (
            "刷写目标不是 $ENVFILE —— 检查是不是又改回 unit 了"
        )

    @covers("M15.F2")
    def test_install_env_preserves_existing_gen(self):
        """★★ 重新生成 ui.env 时必须保留已有 gen。

        ``install-ui-env.sh`` 是白名单重写 —— 天真的实现会从头生成，
        从而把 gen 抹掉（同样撤销轮换）。所以它必须先读旧值。
        """
        text = (REPO_ROOT / "deploy" / "install-ui-env.sh").read_text()
        assert "prev_gen" in text, (
            "install-ui-env 没有保留旧 gen —— 重跑它就会撤销轮换"
        )
        code_lines = [
            ln for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        code = "\n".join(code_lines)
        assert "prev_gen" in code, "只在注释里提到，代码里没真的保留"

    @covers("M15.F2")
    def test_deploy_fills_missing_gen_without_overwriting(self):
        """★★★ 部署要补缺失的 gen，但**绝不覆盖**已有值。

        ⚠ 这两个要求是矛盾的：补缺失 = 写；不覆盖 = 别写。
        判据必须同时验证两半，否则实现成"每次都写成 0"也过。
        """
        text = (REPO_ROOT / "deploy" / "deploy.sh").read_text()
        i = text.find("③b2")
        assert i > 0, "部署脚本里没有补 gen 的步骤"
        j = text.find("③c", i)
        block = text[i:j if j > i else i + 1200]

        # 必须**先判断存在性**再决定写
        assert "grep -q '^NXS_UI_TOKEN_GEN='" in block, (
            "没有判断 gen 是否已存在 —— 会无条件覆盖"
        )
        # 写入只能发生在 else 分支（不存在时）
        assert ">> /etc/nexus-mesh/ui.env" in block, "缺失时没有真的写入"
        # 「保留」分支必须在写入分支之外
        assert "保留" in block, "没有明确的「保留已有值」分支"
