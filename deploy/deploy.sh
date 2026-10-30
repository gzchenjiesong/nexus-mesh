#!/usr/bin/env bash
# ==============================================================================
#  deploy.sh —— 服务器端一键部署（拉代码 → 重启 → 验证）
# ==============================================================================
#
# 为什么需要脚本：手工部署这一步漏过好几次。
#
#   2026-10-30 那天就漏了两件：
#     · 改了 ops 端点却忘记重启 nexus-mesh（服务还在跑旧代码）
#     · 本机 skill 没同步（Agent 按旧文档回答，自报身份全错）
#
# 都是"记得做"但忘记了。所以固化成顺序，不靠记性。
#
# 用法：
#   bash /root/nexus-mesh-deploy/deploy.sh            # 部署 + 重启 + 验证
#   bash /root/nexus-mesh-deploy/deploy.sh --no-restart  # 只拉代码
# ==============================================================================

set -euo pipefail

REPO=/opt/nexus-mesh
SERVICES=(nexus-mesh nexus-mesh-hermes nexus-mesh-taskflow nexus-mesh-ui)
RESTART=1
[ "${1:-}" = "--no-restart" ] && RESTART=0

cd "$REPO"

echo "═══ ① 拉取代码 ═══"
BEFORE=$(git rev-parse --short HEAD)
if ! git fetch origin main 2>/tmp/nxs-fetch-err; then
    echo "  ✗ fetch 失败"
    sed 's/^/    /' /tmp/nxs-fetch-err
    cat <<'HINT'

  ⚠ 两条路，按情况选：

  ① 临时（借本机的 GitHub 凭据，需要加 -A）：
       ssh -A root@<这台机器> 'bash /root/nexus-mesh-deploy/deploy.sh'

     ⚠ 必须是 `-A`（转发 agent）。不带转发时服务器没有可用凭据。

  ② 持久（推荐，一次配置永久生效，之后不需要 -A）：
       bash /root/nexus-mesh-deploy/check-git.sh
     它会打印公钥和添加步骤。

HINT
    rm -f /tmp/nxs-fetch-err
    exit 1
fi
rm -f /tmp/nxs-fetch-err
git merge --ff-only origin/main
AFTER=$(git rev-parse --short HEAD)
echo "  $BEFORE → $AFTER"
if [ "$BEFORE" = "$AFTER" ]; then
    echo "  （无变化）"
fi

# ⚠ 未提交改动会挡住 --ff-only。这里**不自动丢弃** ——
#   万一是线上热修的改动，丢掉就没了。
if [ -n "$(git status --porcelain | grep -v '^?? ')" ]; then
    echo
    echo "  ⚠ 工作区有未提交改动，--ff-only 已失败。逐项确认后再处理："
    git status --short | grep -v '^?? ' | sed 's/^/    /'
    echo
    echo "  若确认这些改动已在 origin/main 里（用 git diff origin/main -- <file> 核对），"
    echo "  可以：git checkout -- <file>  然后重跑本脚本"
    exit 1
fi

if [ "$RESTART" = "0" ]; then
    echo
    echo "  --no-restart：跳过重启"
    exit 0
fi

echo
echo "═══ ② 语法自检（重启前先确认代码可导入）═══"
set -a; . /etc/nexus-mesh/nxs.env; set +a
.venv/bin/python -c "
import nxs.app, nxs.ui.page, nxs.ui.server, nxs.edge.gateway
import nxs.edge.client, nxs.mgmt.audit, nxs.mgmt.observ
import nxs.backends.memory
" && echo "  ✓ 全部可导入"

echo
echo "═══ ③ 配置自检 ═══"
.venv/bin/python -m nxs verify 2>&1 | tail -1 | sed 's/^/  /'

echo
echo "═══ ④ 重启服务 ═══"
# ⚠ 顺序有意义：mesh 先起（桥接要连它），ui 最后（它要查 mesh）
for s in "${SERVICES[@]}"; do
    systemctl restart "$s"
    sleep 1
    printf '  %-22s %s\n' "$s" "$(systemctl is-active "$s")"
done

echo
echo "═══ ⑤ 等桥接重连 ═══"
sleep 8

echo
echo "═══ ⑥ 验证 ═══"
URL=https://127.0.0.1:18800/nxs
CA=/etc/nexus-mesh/tls/ca.pem
.venv/bin/python - <<'PY'
import os, sys
sys.path.insert(0, "/opt/nexus-mesh")
from nxs.edge.client import OpsClient

ops = OpsClient("https://127.0.0.1:18800/nxs",
                secret=os.environ["NXS_OPS_SECRET"].encode(),
                ca_file="/etc/nexus-mesh/tls/ca.pem")
nodes = ops.nodes()
print(f"  节点 {len(nodes)} 个：")
alive = 0
for n in nodes:
    state = n.get("state")
    if state == "alive":
        alive += 1
    flag = "✓" if state == "alive" and n.get("approved") else "⚠"
    print(f"    {flag} {n['node_id']:<16} {n['domain']:<10} "
          f"{state:<7} approved={n.get('approved')}")

m = ops.metrics()
print(f"  运行时长: {m.get('uptime_seconds', 0):.0f}s")
audit = ops.audit(limit=5)
print(f"  运行日志: {len(audit['records'])} 条"
      f"（已滤 {audit['filtered_heartbeats']} 条心跳）")

if alive == len(nodes) and nodes:
    print("  ✓ 全部节点在线")
else:
    print(f"  ⚠ {len(nodes) - alive} 个节点不在线（可能正在重连，稍等再看）")
PY

echo
echo "═══ 完成 ═══"
echo "  观察窗：journalctl -u nexus-mesh-ui -n 20 --no-pager | grep -o 'http://127.0.0.1:8080/?t=[A-Za-z0-9_-]*' | tail -1"
