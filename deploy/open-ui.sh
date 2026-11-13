#!/usr/bin/env bash
# ==============================================================================
#  open-ui.sh —— 打开 Nexus Mesh 观察窗（统一入口版）
# ==============================================================================
#
#  ## 这个脚本替代了什么
#
#  它替代原来的 ``nxs-view.sh``。旧脚本做四件事：查服务 → **建 SSH 隧道**
#  → 从 journalctl 取 token → 打开浏览器。
#
#  现在前两件都不需要了：观察窗有了统一入口
#  ``https://nexus.<域名>``（经服务台 Caddy + Authelia 两因素认证），
#  从任何地方用浏览器打开即可，不用建隧道、不用手抄 token。
#
#  ## 为什么不干脆删掉
#
#  留一个「一键打开 + 自检」的入口仍然有价值：
#    · 不用记域名
#    · ``--check`` 能在服务器侧查清"打不开是哪一层的问题"
#    · ``--rotate`` 是 token 泄露时的止血动作（token 固定化之后，
#      这是唯一的止血手段，必须好按）
#
#  ## 用法
#
#      ./open-ui.sh              # 打开观察窗（或打印链接）
#      ./open-ui.sh --check      # 自检：服务、token、数据、401 页面
#      ./open-ui.sh --rotate     # 轮换 token（旧链接立刻作废）
#      ./open-ui.sh --no-open    # 只打印链接，不启动浏览器
#
#  ## 安全说明
#
#  token 现在由运维密钥派生、**重启不变**、不落盘。所以：
#    · 链接不再是"一次性钥匙"，而是一把长期有效的钥匙
#    · 别贴到聊天/工单/文档里 —— 但它比原来更值得小心
#    · 怀疑泄露时跑 ``--rotate``，所有旧链接立刻作废
#
# ==============================================================================

set -euo pipefail

HUB_HOST="${NXS_HUB_HOST:-43.138.222.206}"
SSH_KEY="${NXS_SSH_KEY:-$HOME/Downloads/key4workbuddy.pem}"
SSH_USER="${NXS_SSH_USER:-root}"
REMOTE_UI_PORT="${NXS_UI_PORT:-8080}"

#: 观察窗的对外域名。改这里或设 NXS_UI_URL。
UI_URL="${NXS_UI_URL:-https://nexus.i-strategy-compass.cn}"

MODE=open
DO_OPEN=1

while [ $# -gt 0 ]; do
    case "$1" in
        --check)   MODE=check ;;
        --rotate)  MODE=rotate ;;
        --no-open) DO_OPEN=0 ;;
        -h|--help) sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $1（用 --help 看用法）" >&2; exit 2 ;;
    esac
    shift
done

#: 我在服务器上还是在本机？
#:
#: ⚠ **这个判断是补的一个真实缺陷。** 脚本原来无条件走 ``ssh_cmd``，
#: 部署时它被装到服务器的 /usr/local/bin/nxs-open —— 在服务器上敲
#: ``nxs-open --check`` 会去本机路径找 ~/Downloads/key4workbuddy.pem，
#: 报「找不到 SSH 密钥」。而那时人已经在服务器上了，根本不需要 SSH。
on_server() {
    # 有本机 nexus-mesh-ui 单元就是服务器。比 hostname 比对可靠：
    # 改名、换 IP、走内网 DNS 都不影响这个判断。
    command -v systemctl >/dev/null 2>&1 \
        && systemctl list-unit-files 2>/dev/null | grep -q '^nexus-mesh-ui'
}

#: 在服务器上跑一段脚本：本机就直接 bash，否则走 SSH。
run_remote() {
    if on_server; then
        bash -s
    else
        ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
            "$SSH_USER@$HUB_HOST" "bash -s"
    fi
}

need_key() {
    if [ ! -f "$SSH_KEY" ]; then
        echo "  找不到 SSH 密钥：$SSH_KEY" >&2
        echo "  设 NXS_SSH_KEY 指向正确的密钥。" >&2
        exit 1
    fi
    chmod 600 "$SSH_KEY" 2>/dev/null || true
}

# ------------------------------------------------------------------ 轮换
if [ "$MODE" = "rotate" ]; then
    on_server || need_key
    echo "→ 轮换观察窗 token（旧链接会全部作废）…"
    # 脚本在仓库里，服务器上跑的是同一份（deploy/ 已随 git 同步）
    if on_server; then
        bash /opt/nexus-mesh/deploy/rotate-ui-token.sh
    else
        ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
            "$SSH_USER@$HUB_HOST" 'bash /opt/nexus-mesh/deploy/rotate-ui-token.sh'
    fi
    echo
    echo "  完成。旧标签页会自动跳回服务台换新 token，不需要手动处理。"
    echo "  ⚠ 如果你把旧链接贴到过别处，现在它已经无效了 —— 这正是轮换的目的。"
    exit 0
fi

# ------------------------------------------------------------------ 自检
if [ "$MODE" = "check" ]; then
    # 在服务器上不需要密钥；在本机才需要（要 SSH 过去）
    on_server || need_key
    if on_server; then
        echo "→ 本机自检（检测到我在服务器上）…"
    else
        echo "→ 服务器侧自检（经 SSH）…"
    fi
    run_remote <<'REMOTE'
set -u
echo "  ① 服务状态"
for s in nexus-mesh nexus-mesh-ui; do
    printf '     %-18s %s\n' "$s" "$(systemctl is-active "$s" 2>/dev/null || echo unknown)"
done

echo "  ② token 代数与是否落盘"
PID=$(systemctl show nexus-mesh-ui -p MainPID --value 2>/dev/null || echo 0)
if [ "${PID:-0}" != "0" ] && [ -r "/proc/$PID/environ" ]; then
    gen=$(tr '\0' '\n' < "/proc/$PID/environ" | grep -oP '^NXS_UI_TOKEN_GEN=\K.*' || true)
    unit_gen=$(grep -oP '^Environment=NXS_UI_TOKEN_GEN=\K.*' \
                 /etc/systemd/system/nexus-mesh-ui.service 2>/dev/null || true)
    echo "     代数 gen=${gen:-（未设置，默认 0）}"
    # 进程内与 unit 里的值可能不一致 —— 那是"改了 unit 但没重启"
    if [ -n "${unit_gen:-}" ] && [ "${gen:-0}" != "$unit_gen" ]; then
        echo "     ⚠ unit 写的是 $unit_gen，进程用的是 ${gen:-0} —— 改了没重启" >&2
    fi
    # 铁律④：token 不该出现在任何文件里
    found=$(grep -rl "nxs_ui=" /etc/nexus-mesh/ 2>/dev/null | head -3 || true)
    if [ -n "$found" ]; then
        echo "     ⚠ 发现可能的 token 落盘：$found" >&2
    else
        echo "     ✓ /etc/nexus-mesh 下无 token 落盘"
    fi
else
    echo "     ⚠ 读不到进程环境（进程没起？）"
fi

echo "  ③ 数据面"
# ⚠ 这里**不再从 journalctl 抓 token**（2026-11-13 改）。
#
# 两个原因：
#   ① 新版启动输出根本不打印 token（只打印服务台入口）—— 日志里抓不到
#   ② 就算抓得到，那也是把凭据当字符串处理；token 现在可以从密钥**算**出来
#
# 顺带一个安全改善：日志里不再出现 token，少了一个泄露面
# （journal 会被备份、会被 grep 到别的地方去）。
cd /opt/nexus-mesh
TOKEN=$(set -a; . /etc/nexus-mesh/ui.env 2>/dev/null; set +a; \
  .venv/bin/python - <<'PY' 2>/dev/null
import base64, hashlib, hmac, os, subprocess
secret = os.environ.get("NXS_OPS_SECRET", "").encode()
if len(secret) < 16:
    raise SystemExit(0)
# gen 取**进程内**的实际值，而不是 unit 里写的 —— 两者可能不一致
gen = "0"
try:
    pid = subprocess.run(["systemctl", "show", "nexus-mesh-ui",
                          "-p", "MainPID", "--value"],
                         capture_output=True, text=True).stdout.strip()
    env = open(f"/proc/{pid}/environ", "rb").read().decode("utf-8", "replace")
    for item in env.split("\0"):
        if item.startswith("NXS_UI_TOKEN_GEN="):
            gen = item.split("=", 1)[1] or "0"
except Exception:
    pass
msg = f"ui-token:v1:gen{gen}".encode()
print(base64.urlsafe_b64encode(
    hmac.new(secret, msg, hashlib.sha256).digest()).decode().rstrip("="))
PY
)

if [ -z "${TOKEN:-}" ]; then
    echo "     ⚠ 算不出 token（ui.env 缺失或密钥过短）"
else
    code=$(curl -s -o /tmp/oui-state.json -w '%{http_code}' \
            -H "Cookie: nxs_ui=$TOKEN" "http://127.0.0.1:8080/api/state" || echo 000)
    echo "     /api/state → HTTP $code"
    if [ "$code" = "200" ]; then
        python3 - <<'PY'
import json
try:
    s = json.load(open("/tmp/oui-state.json"))
except Exception as exc:
    print(f"     ⚠ 解析失败：{exc}")
else:
    print(f"     节点 {len(s.get('nodes') or [])} 个"
          f"  stale={s.get('stale')}  no_history={s.get('no_history')}")
    if s.get("error"):
        print(f"     ⚠ error: {s['error']}")
PY
    fi
    rm -f /tmp/oui-state.json
fi

echo "  ④ 统一入口的两种 Host 行为"
for h in nexus.i-strategy-compass.cn 127.0.0.1:18080; do
    body=$(curl -s -H "Host: $h" -H "Cookie: nxs_ui=stale" \
                http://127.0.0.1:8080/ 2>/dev/null || true)
    if printf '%s' "$body" | grep -q '立即重新进入'; then
        rec="有自动恢复（应该）"
    else
        rec="无自动恢复"
    fi
    printf '     %-32s %s\n' "$h" "$rec"
done
REMOTE
    echo
    echo "  自检完成。观察窗入口：$UI_URL"
    exit 0
fi

# ------------------------------------------------------------------ 打开
echo "→ 观察窗入口：$UI_URL"
echo
echo "  经服务台的 Authelia 两因素认证进入（密码 + TOTP）。"
echo "  不需要 SSH 隧道，也不用手抄 token。"
echo
if [ "$DO_OPEN" = "1" ] && command -v open >/dev/null 2>&1; then
    open "$UI_URL"
    echo "  已在浏览器打开。"
else
    echo "  手动复制上面那行到浏览器打开。"
fi
echo
echo "  打不开时先自检：$0 --check"
echo "  怀疑链接泄露：$0 --rotate"
