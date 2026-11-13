"""★★ 跨仓库：hub 与 nexus-mesh 的 token 派生算法必须逐字一致。

## 背景：一次真实故障（2026-11-13）

用户打开服务台点 Nexus 卡片，得到「暂无法进入：读取不到有效的访问 token」。

真因是两件事叠加：

    ① nexus-mesh 把 token 改为「由密钥派生、重启不变」
    ② 同时删掉了启动时打印 ?t=<token> 的输出（那是长期泄露面）

而 hub 的 `/api/nexus-enter` 靠**正则从 journald 抓** `?t=…`。它抓到了
上一版遗留的作废 token：

    日志最后一条    xHXNhze...  → HTTP 401（已作废）
    当前有效 token  -oXyTq_...  → HTTP 303

校验不过 → 返回 None → 503 → 页面上就是那句「未获取到 Token」。

## 修法与它带来的新风险

hub 改为读 `/etc/nexus-mesh/ui.env` 自己派生（hub 以 root 运行，本来就能
读它，**不增加任何权限面**）。

但这让「派生算法」变成**跨两个仓库的隐式契约**：

    nexus-mesh  nxs/ui/server.py : derive_token()
    private-hub backend/status.py : nexus_token_derive()

两边各一份实现（hub 零第三方依赖、不 import 对方的包）。任何一边改了
label、拼接格式或编码方式，另一边会**静默算出另一个 token** —— 症状还是
「未获取到 Token」，而没有任何报错指向根因。

## 这个文件守什么

用 golden vector：固定 secret 与 gen，断言两边输出完全相同。

⚠ 这条测试**依赖本机的 private-hub 仓库**。找不到就跳过 —— 但它跳过时
应当显式说出来，不能静默通过（静默跳过等于没有守护）。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

from conftest import covers

HUB_BACKEND = pathlib.Path.home() / "private-hub" / "backend"
HUB_SCRIPT = HUB_BACKEND / "check_token_parity.py"

#: 固定向量（secret 是编的，不涉及真实密钥）。
VECTORS = [("a" * 64, "0"), ("a" * 64, "3"),
           ("0123456789abcdef", "1"), ("x" * 16, "0")]


@pytest.mark.skipif(not HUB_SCRIPT.exists(),
                    reason="找不到 private-hub 仓库（跨仓库检查跳过）")
class TestTokenDerivationParity:
    """golden vector：两边算出来必须一模一样。"""

    @covers("M15.F2")
    def test_same_output_for_all_vectors(self):
        """★★★ 4 组向量必须全部一致。"""
        result = subprocess.run([sys.executable, str(HUB_SCRIPT)],
                                capture_output=True, text=True, timeout=120,
                                check=False)
        assert result.returncode == 0, (
            "两侧派生算法已分叉 —— hub 会算出另一个 token，"
            "症状是「未获取到 Token」且无报错指向根因。\n"
            + (result.stdout or "")[-2000:]
            + (result.stderr or "")[-800:]
        )
        assert "全部一致" in result.stdout

    @covers("M15.F2")
    def test_label_matches(self):
        """★★ 派生标签必须逐字相同（`ui-token:v1`）。

        ⚠ 这是最容易悄悄改的一处：改 nexus-mesh 的 ``TOKEN_DERIVE_LABEL``
        会让所有 token 瞬间作废 —— 那本身是个**特性**（全量轮换），
        但必须**同时**改 hub，否则卡片入口就断了。
        """
        from nxs.ui.server import TOKEN_DERIVE_LABEL

        hub_src = (HUB_BACKEND / "status.py").read_text()
        assert f'NEXUS_TOKEN_LABEL = "{TOKEN_DERIVE_LABEL}"' in hub_src, (
            f"hub 侧的 label 与 nexus-mesh 的 {TOKEN_DERIVE_LABEL!r} 不一致"
        )

    @covers("M15.F2")
    def test_hub_derives_before_reading_journal(self):
        """★★ hub 必须**先派生、后抓日志** —— 顺序不能反。

        ⚠ 日志是 append-only，抓到的永远是"历史上出现过的最后一个
        ?t="，可能早已作废。原来的实现只有抓日志这一条路，所以 token
        改成派生之后立刻失效。
        """
        hub_src = (HUB_BACKEND / "status.py").read_text()

        i_derive = hub_src.find("derived = nexus_token_derive()")
        i_journal = hub_src.find("from_journal = nexus_token_from_journal()")
        assert i_derive > 0, "hub 没有派生主路径"
        assert i_journal > 0, "hub 的日志兜底不见了（不是必须，但改了要确认）"
        assert i_derive < i_journal, (
            "hub 先抓日志再派生 —— 会优先用到日志里可能作废的旧 token"
        )


class TestMeshSideDerivationIsStable:
    """★ nexus-mesh 侧：算法不能悄悄漂移。

    ⚠ 上面那组跨仓库测试只在**本机有 private-hub 时**才跑。生产环境
    或 CI 里可能没有它，所以这里再加一组自洽的断言：固定输入 + 固定
    输出。它同时也是一份「算法是什么」的可执行文档。
    """

    @covers("M15.F2")
    def test_golden_vector_is_pinned(self):
        """★★ 给定 secret 与 gen 的输出必须固定。

        这条测试的价值在于：**它不关心算法怎么实现，只关心结果不变**。
        所以它既允许内部重构（比如换成别的 HMAC 调用形式），
        又会在结果变化时立刻喊人 —— 而结果变化就意味着 hub 会分叉。
        """
        from nxs.ui.server import derive_token

        secret = b"a" * 64
        got = derive_token(secret, "0")
        # 这个值是用当前实现算出来的。**改它必须同时改 hub。**
        assert got == "lywHtDvOORfASpuQRbVtwaLMwI_-b6vZ1ZY0BFbWwNM", (
            "派生结果变了 —— 若这是有意的算法变更，"
            "必须同步改 private-hub 的 nexus_token_derive()，"
            "否则服务台卡片入口会返回「未获取到 Token」"
        )

    @covers("M15.F2")
    def test_label_is_pinned(self):
        """★ 标签写死在这里，改动即全量轮换。"""
        from nxs.ui.server import TOKEN_DERIVE_LABEL

        assert TOKEN_DERIVE_LABEL == "ui-token:v1", (
            "派生标签变了 —— 这会让所有已发出的 token 立刻作废（这是"
            "设计上允许的全量轮换手段），但 hub 必须同步"
        )

    @covers("M15.F2")
    def test_message_format_is_pinned(self):
        """★ 拼接格式 `{label}:gen{gen}` 也必须固定。

        ⚠ 分开断言格式，是因为「格式变了但 label 没变」这种情况
        用 label 断言抓不到，而结果同样会分叉。
        """
        from nxs.ui.server import derive_token

        # 手工按规范算：base64url(HMAC-SHA256(secret, "ui-token:v1:gen7"))
        import base64
        import hashlib
        import hmac

        secret = b"b" * 32
        expected = base64.urlsafe_b64encode(
            hmac.new(secret, b"ui-token:v1:gen7", hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        assert derive_token(secret, "7") == expected
