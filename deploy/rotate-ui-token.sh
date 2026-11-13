#!/usr/bin/env bash
# ==============================================================================
#  rotate-ui-token.sh —— 轮换观察窗 token（怀疑泄露时的止血动作）
# ==============================================================================
#
#  ## 为什么需要这个脚本
#
#  2026-11-13 起，观察窗 token 不再"每次重启随机生成"，而是从运维密钥
#  **派生**的：同 gen 同 token，重启不变。好处是部署不再踢掉在线标签页
#  （此前一次会话里部署 5 次 = 用户失效 5 次）。
#
#  代价是失去了"重启即止血"。所以必须有一个**显式**的轮换入口 ——
#  就是这里。
#
#  ## 什么时候用
#
#    · 怀疑链接泄露：截图、转发到聊天/工单、录屏、共享屏幕
#    · 有人离职或失去信任
#    · 例行轮换（比如每季度）
#
#  ## 做完会发生什么
#
#    · 所有已发出的 token 立刻作废
#    · 旧标签页打开会 401 —— 但**不会卡死**：401 页面会自动跳回服务台
#      换一张新 token（NXS_UI_RECOVER_URL 那套机制），人几乎无感
#
#  ## 用法
#
#      sudo bash deploy/rotate-ui-token.sh            # 代数 +1
#      sudo bash deploy/rotate-ui-token.sh --to 7     # 指定代数
#      sudo bash deploy/rotate-ui-token.sh --check    # 只看当前代数
#
# ==============================================================================

set -euo pipefail

UNIT=/etc/systemd/system/nexus-mesh-ui.service
ENVFILE=/etc/nexus-mesh/ui.env
SERVICE=nexus-mesh-ui
MODE=rotate
TARGET=""

while [ $# -gt 0 ]; do
    case "$1" in
        --check) MODE=check ;;
        --to)    MODE=rotate; TARGET="${2:-}"; shift ;;
        -h|--help) sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $1（用 --help 看用法）" >&2; exit 2 ;;
    esac
    shift
done

die() { echo "  ✗ $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "需要 root（要改 $UNIT 并重启服务）"
[ -f "$UNIT" ] || die "找不到 $UNIT"

# 读当前代数。**必须精确匹配行首**，否则遇到 NXS_UI_TOKEN_GEN_OLD 这类
# 前缀相近的变量会取错 —— 那会导致"以为轮换了其实没变"。
current=$(grep -oP '^NXS_UI_TOKEN_GEN=\K\S+' "$ENVFILE" 2>/dev/null || true)
if [ -z "$current" ]; then
    current="0"
    echo "  ⚠ ui.env 里没有 NXS_UI_TOKEN_GEN，按默认 0 处理"
fi

if [ "$MODE" = "check" ]; then
    echo "  当前 token 代数: $current"
    echo "  服务状态: $(systemctl is-active "$SERVICE" 2>/dev/null || echo unknown)"
    # 进程环境里的实际值 —— unit 改了但没重启时两者会不一致
    pid=$(systemctl show "$SERVICE" -p MainPID --value 2>/dev/null || echo 0)
    if [ "${pid:-0}" != "0" ] && [ -r "/proc/$pid/environ" ]; then
        live=$(tr '\0' '\n' < "/proc/$pid/environ" \
               | grep -oP '^NXS_UI_TOKEN_GEN=\K.*' || true)
        echo "  进程内实际值: ${live:-（未设置，进程用默认 0）}"
        if [ -n "$live" ] && [ "$live" != "$current" ]; then
            echo "  ⚠ unit 与进程不一致 —— 改了 unit 但没重启"
        fi
    fi
    exit 0
fi

# ---- 算新代数 ----
if [ -n "$TARGET" ]; then
    new="$TARGET"
else
    # 不是数字就退回 1，避免 "0" + 1 算出怪值
    case "$current" in
        ''|*[!0-9]*) new=1 ;;
        *) new=$((current + 1)) ;;
    esac
fi

[ "$new" != "$current" ] || die "新代数与当前相同（$new），没有意义"
case "$new" in
    ''|*[!0-9]*) die "代数必须是数字：$new" ;;
esac

echo "  token 代数: $current → $new"

# ---- 改 unit ----
# 用 python 改而不是 sed：sed -i 在不同平台/版本上的行为差异会咬人，
# 而这个文件改错会导致服务起不来。
# ⚠⚠ **写 ui.env，不是 unit。**
#
# unit 是部署脚本从仓库同步的**模板** —— 改它会被下次部署覆盖，
# 于是"轮换"变成"下次部署后自动撤销"。实测踩到过：
# 轮换到 gen=1，部署一次再看又回到 0，而部署脚本只说"全部与仓库一致"。
#
# ui.env 是本机状态，部署不覆盖（install-ui-env.sh 也会保留已有值）。
python3 - "$ENVFILE" "$new" <<'PY'
import os
import re
import sys
import tempfile

path, new = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()

pattern = re.compile(r"^NXS_UI_TOKEN_GEN=\S*$", re.M)
if pattern.search(text):
    text = pattern.sub(f"NXS_UI_TOKEN_GEN={new}", text, count=1)
else:
    text = text.rstrip("\n") + f"\nNXS_UI_TOKEN_GEN={new}\n"

# 原子写：这个文件是凭据文件，写一半断电会留下半截
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write(text)
os.chmod(tmp, 0o600)          # 保持 600（里面是密钥）
os.replace(tmp, path)
print("    已写入 ui.env")
PY

# 不用 daemon-reload（unit 没变，改的是 EnvironmentFile 内容）。
# 但**必须重启** —— systemd 只在启动时读 EnvironmentFile。
systemctl restart "$SERVICE"
sleep 2
printf '  %s: %s\n' "$SERVICE" "$(systemctl is-active "$SERVICE")"

# ---- 验证：进程内确实换了 ----
pid=$(systemctl show "$SERVICE" -p MainPID --value)
if [ "${pid:-0}" != "0" ] && [ -r "/proc/$pid/environ" ]; then
    live=$(tr '\0' '\n' < "/proc/$pid/environ" \
           | grep -oP '^NXS_UI_TOKEN_GEN=\K.*' || true)
    if [ "$live" = "$new" ]; then
        echo "  ✓ 进程内代数已是 $new（旧 token 已作废）"
    else
        echo "  ✗ 进程内代数仍是 '${live:-未设置}' —— 轮换没生效" >&2
        exit 1
    fi
fi

echo
echo "  旧标签页会 401，然后自动跳回服务台换新 token —— 不需要手动处理。"
