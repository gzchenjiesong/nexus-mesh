#!/usr/bin/env bash
# ==============================================================================
#  install-ui-env.sh —— 为观察窗生成最小凭据文件
# ==============================================================================
#
# ⚠ **这个脚本存在的唯一理由：把「给多了」变成不可能。**
#
# 2026-11-13 实测发现 `nexus-mesh-ui` 的 unit 直接
# `EnvironmentFile=/etc/nexus-mesh/nxs.env` —— 那是给常驻进程用的**全量**
# 凭据文件。于是一个只读观察窗的进程环境里有：
#
#     NXS_OPS_SECRET            ← 它只需要这一个
#     NXS_SECRET_COORDINATOR    ← 不需要
#     NXS_SECRET_HERMES_WORKING ← 不需要
#     NXS_SECRET_TASKFLOW       ← 不需要
#     NXS_SECRET_WORK_DESKTOP   ← 不需要
#     NXS_SECRET_WORK_MACBOOK   ← 不需要
#
# 后果不是理论上的：UI 一旦出 RCE 或路径穿越，攻击者拿到的
# **不是只读运维通道，而是能直接冒充 coordinator / hermes / taskflow
# 发任务的完整身份**。
#
# 这与「不做写操作」不在一个量级 —— 前者防误操作，这一条防提权。
#
# ## 为什么用脚本而不是让运维手工写
#
# 手工写会漏。而「漏」在这里的表现是**一个只读页面悄悄握着全部身份**，
# 没人会发现。脚本的做法是**白名单提取**：只从 nxs.env 里挑出 OPS 那一行，
# 其余一概不复制。这样即使将来 nxs.env 新增了密钥，ui.env 也不会跟着变多。
#
# 用法：
#     sudo bash deploy/install-ui-env.sh           # 生成/更新
#     sudo bash deploy/install-ui-env.sh --check   # 只检查，不改
#
# ==============================================================================

set -euo pipefail

SRC_ENV=${SRC_ENV:-/etc/nexus-mesh/nxs.env}
DST_ENV=${DST_ENV:-/etc/nexus-mesh/ui.env}
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

die() { echo "  ✗ $*" >&2; exit 1; }

[ -f "$SRC_ENV" ] || die "找不到 $SRC_ENV（协调节点的凭据文件）"
[ "$(id -u)" -eq 0 ] || die "需要 root（要写 /etc/nexus-mesh 并设 0600）"

#: 观察窗真正需要的变量。**只这一个。**
#:
#: 若要新增，先问：观察窗真的需要它吗？它只是一个只读展示页面。
WANTED=(NXS_OPS_SECRET)

# ---- ① 从源文件白名单提取 ----
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

missing=()
for name in "${WANTED[@]}"; do
    # ⚠ 用 `grep -m1 "^NAME="` 精确匹配行首，避免误取 NXS_OPS_SECRET_OLD 这类前缀相近的
    line=$(grep -m1 "^${name}=" "$SRC_ENV" || true)
    if [ -z "$line" ]; then
        missing+=("$name")
        continue
    fi
    echo "$line" >> "$tmp"
done

if [ ${#missing[@]} -gt 0 ]; then
    die "源文件里缺这些变量：${missing[*]}
    观察窗至少要 NXS_OPS_SECRET 才能读运维端点。"
fi

# ---- ② 只检查模式 ----
if [ "$CHECK_ONLY" = "1" ]; then
    echo "  源文件: $SRC_ENV"
    echo "  目标:   $DST_ENV"
    if [ ! -f "$DST_ENV" ]; then
        echo "  ✗ 目标不存在 —— 需要运行 install-ui-env.sh 生成"
        exit 1
    fi
    mode=$(stat -c '%a' "$DST_ENV" 2>/dev/null || stat -f '%Lp' "$DST_ENV")
    if [ "$mode" != "600" ]; then
        echo "  ✗ 权限是 $mode，应为 600"
        exit 1
    fi
    # 关键检查：目标里不能出现任何 NXS_SECRET_*
    if grep -q '^NXS_SECRET_' "$DST_ENV"; then
        echo "  ✗ 目标里含 NXS_SECRET_* —— 观察窗不该有节点身份密钥："
        grep -o '^NXS_SECRET_[A-Z_]*' "$DST_ENV" | sed 's/^/      /'
        exit 1
    fi
    echo "  ✓ 只含观察窗需要的变量，权限 600"
    echo "    变量清单："
    grep -o '^[A-Z_]*' "$DST_ENV" | sed 's/^/      /'
    exit 0
fi

# ---- ③ 写入 ----
# 头部注释说明来源与为什么只有这几行 —— 后来人看到只有一行不会以为漏了
{
    cat <<'HEADER'
# Nexus Mesh · 观察窗凭据（最小集）
#
# ⚠ **只有 NXS_OPS_SECRET。这是刻意的，不是漏了。**
#
# 观察窗是只读展示页面，它只需要读运维端点的权限。
# 它不需要任何节点的身份密钥（NXS_SECRET_*）—— 那些密钥能直接
# 冒充节点发任务，给一个展示页面等于把提权路径敞开。
#
# 生成方式：deploy/install-ui-env.sh（白名单提取，不手工编辑）
# 检查方式：deploy/install-ui-env.sh --check
#
HEADER
    cat "$tmp"
} > "$DST_ENV"

chmod 600 "$DST_ENV"
chown root:root "$DST_ENV" 2>/dev/null || true

echo "  ✓ 已生成 $DST_ENV"
echo "    权限: $(stat -c '%a' "$DST_ENV" 2>/dev/null || stat -f '%Lp' "$DST_ENV")"
echo "    变量:"
grep -o '^[A-Z_]*' "$DST_ENV" | sed 's/^/      /'
echo
echo "  下一步：确认 unit 的 EnvironmentFile 指向它，然后重启"
echo "    grep EnvironmentFile /etc/systemd/system/nexus-mesh-ui.service"
echo "    systemctl restart nexus-mesh-ui"
echo
echo "  验收（应只看到 NXS_OPS_SECRET）："
echo "    tr '\\0' '\\n' < /proc/\$(systemctl show -p MainPID --value nexus-mesh-ui)/environ \\"
echo "      | grep -oE '^NXS_[A-Z_]+' | sort"
