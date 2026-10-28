"""pytest 共享夹具。

刻意使用 ``config/nxs.example.yaml`` 而不是各自的临时配置：
测试要验证的正是"仓库里发给别人的那份示例配置能跑通"，
如果示例配置本身有错，这里就会失败——这是一种廉价的文档正确性检查。
"""

from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# 让 ``from traceability import registry`` 可用（traceability/ 在仓库根，
# 而 pytest 默认只把 tests/ 加入 sys.path）
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nxs.control import PolicyEngine  # noqa: E402
from nxs.mgmt import NexusConfig, load_config  # noqa: E402

EXAMPLE_CONFIG = REPO_ROOT / "config" / "nxs.example.yaml"

#: demo 用的确定性凭据。
#: 真实部署请用环境变量（NXS_SECRET_*），不要像这里一样写死。
DEMO_SECRETS = {
    # 协调节点的 ID 是 coordinator（术语表规定的叫法）。
    # 域叫 cloud —— 域是**位置**，节点是**角色**，两者刻意不同名。
    "coordinator": "coordinator-demo-secret-0123456789abcdef",
    "node-a": "node-a-demo-secret-0123456789abcdef",
    "node-b": "node-b-demo-secret-0123456789abcdef",
}


def demo_env() -> dict[str, str]:
    return {
        f"NXS_SECRET_{mid.upper().replace('-', '_')}": secret
        for mid, secret in DEMO_SECRETS.items()
    }


@pytest.fixture
def config() -> NexusConfig:
    return load_config(EXAMPLE_CONFIG, env=demo_env())


@pytest.fixture
def policy(config: NexusConfig) -> PolicyEngine:
    return PolicyEngine(config)


@pytest.fixture
def workdir(request):
    """每个测试一个**隔离**的临时工作目录。

    ## 优先用 pytest 的标准 ``tmp_path``

    它是 per-test 隔离的、会话结束自动清理、位于系统临时目录 ——
    这些都是正确行为，没有理由自己实现一套。

    ## 但确实存在它不可用的环境

    本项目作者的开发环境里实测过两种失败：

    ① **历史残留目录阻塞**：``FileExistsError: [Errno 17] EEXIST``
       ``mkdir '.../T/pytest-of-root'``
       —— 一个月前残留的 ``pytest-of-<user>`` 目录让 pytest 无法建新的。
       清掉残留即恢复。

    ② **受限环境的 mkdir 拦截**：某些沙箱/代理层会拦截 ``mkdir``，
       连 ``exist_ok=True`` 也走不通。

    所以保留一条退化路径：真的拿不到 ``tmp_path`` 就落在项目内
    （``.nxs/`` 已被 ``.gitignore`` 排除）。

    ## ⚠ 退化路径为什么不共用固定目录

    **初版是所有测试共用 ``.nxs/test-tmp``，每次 ``rmtree`` 整体重来。**
    这曾经造成过很难排查的现象：

    =============================================  ==========================
    问题                                            症状
    =============================================  ==========================
    测试之间不隔离                                   残留影响下一个测试
    一次 ``rmtree`` 删几十上百个文件                 撞上层工具链的批量删除保护
                                                    ``SAFE_DELETE_BULK_CONFIRM_REQUIRED``
                                                    → **setup 阶段随机 ERROR**
    =============================================  ==========================

    最恶劣的是第二个：报错文件**每次都不同**（取决于累计文件数何时越过
    阈值），看起来像"测试不稳定"，实际是删除配额被拦。实测出现在
    Windows 与 macOS 两套环境下，排查花了很多时间。

    所以退化路径也用 ``nodeid`` 派生的唯一子目录，且**不做清理** ——
    删除配额留给真正需要删东西的地方。
    """
    # ⚠ **不要在这个函数里同时用 ``return`` 和 ``yield``。**
    #
    #   一旦出现 ``yield``，整个函数就是 generator function ——
    #   ``return X`` 的语义变成 ``StopIteration(X)``，而不是"fixture 值"。
    #   pytest 拿到的会是生成器本身，测试里所有 ``workdir / "x"`` 都炸。
    #
    #   实测：这个写法让错误数从 38 涨到 119，报错还全是
    #   "previous item was not torn down properly" —— 完全看不出根因。
    #
    #   清理钩子改用 ``request.addfinalizer``，这样函数可以正常 ``return``。

    # ---- 首选：标准 tmp_path ----
    try:
        return request.getfixturevalue("tmp_path")
    except Exception:  # noqa: BLE001 - 拿不到就走退化路径
        pass

    # ---- 退化：项目内唯一子目录 ----
    #
    # ⚠ **此路径不做清理，这是刻意的。**
    #
    #   曾经在 finalizer 里 rmtree 自己的目录。问题是有些测试
    #   （sqlite / lease / probe）单个就能产生 50+ 个文件，
    #   一次 rmtree 就撞上外层工具链的批量删除保护：
    #
    #       [safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]
    #       {"count":50,"threshold":50,...}  →  SystemExit: 1
    #
    #   症状是 **teardown 阶段随机 ERROR**，报错文件每次都不同 ——
    #   极难定位。既然删不干净，索性不删：
    #     · 目录名带 uuid，重复跑也不会复用旧目录（隔离性不靠删除保证）
    #     · ``.nxs/`` 已在 .gitignore 里，不影响仓库
    #     · 清理交给 ``make clean`` 或手工，不占用测试的删除配额
    base = REPO_ROOT / ".nxs" / "test-tmp"
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", request.node.nodeid)[:100]
    directory = base / f"{safe}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# ---------------------------------------------------------------------------
# 追溯标记
# ---------------------------------------------------------------------------

def pytest_configure(config):
    """注册自研 mark，避免 pytest 报 unknown mark 警告。"""
    config.addinivalue_line(
        "markers",
        "covers(*ids): 标记该测试覆盖的编号（M#.F# / M#.R# / C# / N#）",
    )


#: 给测试挂编号的标记。
#:
#: 用法::
#:
#:     @covers("M5.F1")
#:     def test_unregistered_type_is_denied(...): ...
#:
#: 一个测试可挂多个编号（它可能同时验证功能与约束）。
#: **参数化测试只需挂一次** —— pytest 展开成多个 item 时标记会随之复制。
covers = pytest.mark.covers
