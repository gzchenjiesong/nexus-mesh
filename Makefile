# ============================================================
#  Nexus Mesh · Makefile
# ============================================================

PYTHON ?= python3
VENV   ?= .venv
BIN    := $(VENV)/bin
PORT   ?= 8443
CONFIG ?= config/nxs.example.yaml

.PHONY: help venv install run test verify demo clean demo-clean

help:  ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv:  ## 创建虚拟环境
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip

install: venv  ## 安装（含开发依赖）
	$(BIN)/pip install -e ".[dev]"

run:  ## 启动 Nexus 核心
	$(BIN)/nxs serve --config $(CONFIG) --port $(PORT)

test:  ## 跑测试（含四条安全断言）
	$(BIN)/pytest -v

verify:  ## 环境自检：配置合法性 + 端口可用性
	$(BIN)/nxs verify --config $(CONFIG)

demo:  ## 跑完整的 7 步验收 Demo（同进程传输）
	$(BIN)/nxs demo --config $(CONFIG)

demo-net:  ## 同一套 7 步验收，走真实 HTTP（验证传输层）
	$(BIN)/nxs demo --net --config $(CONFIG)

demo-clean:  ## 清理 demo 产生的运行时数据
	rm -rf .nxs/

clean:  ## 清理构建产物与缓存（含测试临时目录）
	rm -rf build/ dist/ *.egg-info .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	@# 测试的 workdir 退化路径刻意不做清理（避开批量删除配额），此处兜住
	@rm -rf .nxs/test-tmp
