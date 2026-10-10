"""pytest 共享夹具。

刻意使用 ``config/nxs.example.yaml`` 而不是各自的临时配置：
测试要验证的正是"仓库里发给别人的那份示例配置能跑通"，
如果示例配置本身有错，这里就会失败——这是一种廉价的文档正确性检查。
"""

from __future__ import annotations

import sys
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
    "hub": "hub-demo-secret-0123456789abcdef",
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
def workdir():
    """项目内的临时工作目录。

    不使用 pytest 的 ``tmp_path``——部分受限环境下无法在系统临时目录
    创建文件。改用项目内路径，该路径已由 ``.gitignore`` 排除（``.nxs/``）。
    """
    import shutil

    base = REPO_ROOT / ".nxs" / "test-tmp"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


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
