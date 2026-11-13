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
echo "═══ ③b 同步 systemd unit ═══"
# ⚠ **这一步是补的一个真实缺口（2026-11-13）。**
#
# 在此之前，`/etc/systemd/system/nexus-mesh-ui.service` 是**手工放**的，
# 仓库里的 deploy/*.service 只是源码留档 —— 部署脚本从不安装它。
#
# 后果实测：给 UI 加了 `Environment=NXS_UI_RECOVER_URL=...`，改的是仓库
# 那份，跑完部署一看进程环境里根本没有这个变量，401 页面也就没有自动
# 恢复按钮。**代码是新的、unit 是旧的，而部署脚本说"完成"。**
#
# 现在以仓库为唯一源头：内容不同就装过去并 daemon-reload。
# ⚠ 若你在生产上手工改过 unit，请改仓库那份 —— 否则会被这次覆盖。
unit_changed=0
for src in deploy/*.service; do
    [ -e "$src" ] || continue
    name=$(basename "$src")
    dst="/etc/systemd/system/$name"
    if ! cmp -s "$src" "$dst"; then
        cp "$src" "$dst"
        chmod 644 "$dst"
        echo "  更新 $name"
        unit_changed=1
    fi
done
if [ "$unit_changed" = "1" ]; then
    systemctl daemon-reload
    echo "  已 daemon-reload"
else
    echo "  全部与仓库一致"
fi

echo
echo "═══ ③c 同步运维脚本 ═══"
# ⚠ 与 ③b 同源的问题，只是对象是脚本而不是 unit（2026-11-13）。
#
# `/root/nexus-mesh-deploy/` 下曾放着 deploy.sh / nxs-view.sh 的**副本**，
# 于是"改的是仓库那份、执行的是旧副本"。这里把仓库里的运维脚本装到
# 固定位置，让运维敲的路径稳定、而内容始终来自 git。
#
# ⚠ 只装**幂等**的只读/自检类脚本。deploy.sh 自己不在其中 ——
#   它是鸡生蛋，靠 /root 那个薄壳 exec 到仓库这份（见 deploy/entrypoint.sh）。
install -d -m 755 /usr/local/lib/nexus-mesh 2>/dev/null || true
script_changed=0
for src in deploy/open-ui.sh deploy/rotate-ui-token.sh; do
    [ -e "$src" ] || continue
    name=$(basename "$src")
    dst="/usr/local/lib/nexus-mesh/$name"
    if ! cmp -s "$src" "$dst"; then
        install -m 755 "$src" "$dst"
        echo "  更新 $name"
        script_changed=1
    fi
done
# 便利入口：PATH 里能直接敲 open-ui
ln -sfn /usr/local/lib/nexus-mesh/open-ui.sh /usr/local/bin/nxs-open 2>/dev/null || true
if [ "$script_changed" = "1" ]; then
    echo "  已安装到 /usr/local/lib/nexus-mesh（nxs-open 指向它）"
else
    echo "  全部与仓库一致"
fi

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
