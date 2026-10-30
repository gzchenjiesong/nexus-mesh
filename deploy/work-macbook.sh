#!/usr/bin/env bash
# ==============================================================================
#  work-macbook.sh —— 在本机 Mac 上启停 Nexus Mesh Agent
# ==============================================================================
#
# 为什么需要脚本：让本机 Agent 接入需要 6 个参数，其中 3 个（白名单、
# workdir、profile）每次都得写一样 —— 敲错一个的症状是"注册成功但行为不对"，
# 而那不是一眼能看出来的。
#
# ⚠ **工具白名单刻意不含 Bash。**
#
#   本机持有全量源码、SSH 私钥、云凭据；而 `work.query` 的入参是
#   外部问题文本 —— 提示注入的现成载体。给它 Bash 意味着：
#
#       云端 Agent（或伪装成它的攻击者）发一个问题
#       → CodeBuddy 读它、可能执行它
#       → 本机的 ~/.ssh、~/.aws、env 全部可达
#
#   只读三件（Read/Grep/Glob）把「读到了什么」和「能执行什么」分开：
#   最坏情况是本机文件内容被摘要出去，而不是被任意执行。
#
#   确实需要 Bash 时（比如想让它跑测试），用 `--with-bash` 显式开启，
#   并且**只在你自己盯着的时候开**。
#
# 用法：
#     ./deploy/work-macbook.sh start          # 后台启动（写日志）
#     ./deploy/work-macbook.sh stop           # 停
#     ./deploy/work-macbook.sh status         # 看状态
#     ./deploy/work-macbook.sh log            # 跟踪日志
#     ./deploy/work-macbook.sh start --with-bash   # 含 Bash（危险）
#     ./deploy/work-macbook.sh foreground    # 前台跑（Ctrl-C 停，调试用）
#
# ==============================================================================

set -euo pipefail

NODE_ID="work-macbook"
DOMAIN="zone-work"
PROFILE="macbook"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$REPO_ROOT/.venv/bin/python"
CA_FILE="$HOME/.nexus-mesh/ca.pem"
ACTOR_DIR="$HOME/.nexus-mesh/agents"
PID_FILE="/tmp/nxs-$NODE_ID.pid"

#: CodeBuddy 的项目级 skill 目录。**必须和仓库里的源文件保持一致** ——
#: 否则 Agent 会按过期文档回答（实测把角色说成了旧模型的 agent/agent.ask）。
SKILL_DST="$HOME/.codebuddy/skills/nexus-mesh/SKILL.md"
SKILL_SRC="$REPO_ROOT/nexus-mesh-skill.md"
LOG_FILE="/tmp/nxs-$NODE_ID.log"

#: 只读三件 —— 默认白名单。
READONLY_TOOLS=(Read Grep Glob)
#: 加上 Bash 后能干活，但攻击面大增。见文件头说明。
FULL_TOOLS=(Read Grep Glob Bash WebFetch)

die() { echo "  ✗ $*" >&2; exit 1; }

install_skill() {
    # 把仓库里的 skill 同步到 CodeBuddy 的 skill 目录。
    #
    # ⚠ **这一步漏过一次，代价是节点回答错误的自我认知。**
    #
    #   2026-10-30：本机 skill 还停在 2026-10-28 的旧版（改造前），
    #   里面写着「让运维加 issuer 与 can_issue_types」这种旧模型说法。
    #   Agent 读了它，于是把自己说成了旧模型的角色。
    #
    #   **只在服务器上跑过 install-skill.sh，漏了本机。**
    #   所以现在放进启停脚本 —— 每次 start 都同步，不靠记性。
    if [ ! -f "$SKILL_SRC" ]; then
        echo "  ⚠ 找不到 skill 源文件：$SKILL_SRC（跳过同步）"
        return 0
    fi
    mkdir -p "$(dirname "$SKILL_DST")"
    if [ -f "$SKILL_DST" ] && cmp -s "$SKILL_SRC" "$SKILL_DST"; then
        echo "  skill 已是最新"
        return 0
    fi
    cp "$SKILL_SRC" "$SKILL_DST"
    echo "  skill 已同步 → $SKILL_DST"
}

preflight() {
    [ -x "$VENV_PY" ]   || die "找不到 venv：${VENV_PY}（先在仓库根目录跑 make 或装依赖）"
    [ -f "$CA_FILE" ]   || die "找不到 CA：${CA_FILE}（从服务器拷 /etc/nexus-mesh/tls/ca.pem）"
    local cred="$ACTOR_DIR/$NODE_ID.env"
    [ -f "$cred" ]      || die "找不到凭据：$cred"
    # 凭据必须是 0600，否则密钥可能被同机其它用户读走
    local mode
    mode="$(stat -f '%Lp' "$cred" 2>/dev/null || stat -c '%a' "$cred")"
    [ "$mode" = "600" ] || echo "  ⚠ 凭据权限是 ${mode}，建议 chmod 600 $cred"
}

build_cmd() {
    local with_bash="${1:-no}"
    local tools=("${READONLY_TOOLS[@]}")
    [ "$with_bash" = "yes" ] && tools=("${FULL_TOOLS[@]}")

    local tool_args=()
    for t in "${tools[@]}"; do tool_args+=(--allowed-tool "$t"); done

    # workdir 给仓库根：CodeBuddy 会加载 <workdir>/.codebuddy/skills/ 下的
    # 项目级 skill，相对路径也以此为基准。给错位置的症状是"它找不到文件"。
    echo "$VENV_PY" "$REPO_ROOT/examples/codebuddy_node.py" \
        --node-id "$NODE_ID" --domain "$DOMAIN" --profile "$PROFILE" \
        "${tool_args[@]}" --workdir "$REPO_ROOT" --log-file "$LOG_FILE"
}

running_pid() {
    [ -f "$PID_FILE" ] || return 1
    local pid; pid="$(cat "$PID_FILE")"
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

cmd_start() {
    local with_bash="no"
    [ "${1:-}" = "--with-bash" ] && with_bash="yes"

    preflight
    install_skill

    if pid="$(running_pid)"; then
        echo "  已在运行（PID ${pid}）—— 要重启先 stop"
        return 0
    fi
    if [ "$with_bash" = "yes" ]; then
        echo "  ⚠⚠ 已开启 Bash —— 本机文件与凭据可在提示注入下被读取/执行。"
        echo "     只在你自己盯着的时候这么跑。"
    fi

    export NXS_ACTOR_DIR="$ACTOR_DIR"
    # shellcheck disable=SC2046
    nohup $(build_cmd "$with_bash") >>"$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    echo "  已启动（PID ${pid}）· 日志 $LOG_FILE"
    sleep 6
    if kill -0 "$pid" 2>/dev/null; then
        echo "  进程存活。确认注册结果："
        tail -4 "$LOG_FILE" | sed 's/^/    /'
    else
        echo "  ✗ 启动后立即退出 —— 看日志："
        tail -14 "$LOG_FILE" | sed 's/^/    /'
        rm -f "$PID_FILE"
        return 1
    fi
}

cmd_foreground() {
    preflight
    install_skill
    export NXS_ACTOR_DIR="$ACTOR_DIR"
    echo "  前台运行（Ctrl-C 停）· 日志同时写 $LOG_FILE"
    # shellcheck disable=SC2046
    exec $(build_cmd "no")
}

cmd_stop() {
    if pid="$(running_pid)"; then
        kill "$pid" 2>/dev/null || true
        for _ in 1 2 3 4 5; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$PID_FILE"
        echo "  已停止（PID ${pid}）"
    else
        echo "  没在运行"
        rm -f "$PID_FILE"
    fi
}

cmd_status() {
    if pid="$(running_pid)"; then
        echo "  运行中（PID ${pid}）"
    else
        echo "  未运行"
    fi
    echo
    echo "  ── 服务端看到的它 ──"
    if [ -f "$ACTOR_DIR/$NODE_ID.env" ]; then
        # ⚠ `NXS_ACTOR_DIR` 必须显式传 —— 凭据文件里只有 NODE_ID/URL/CA/SECRET，
        #   不含它自己所在目录。缺了它 nxs-cli 会退回默认
        #   /etc/nexus-mesh/agents（那是服务器上的布局），
        #   于是报"找不到身份" —— 而凭据明明就在那儿。
        #   第一版就是这么写的，status 直接查不到自己。
        # shellcheck disable=SC1090
        ( set -a; . "$ACTOR_DIR/$NODE_ID.env"; set +a
          export NXS_ACTOR_DIR="$ACTOR_DIR"
          "$VENV_PY" -m nxs.cli.main --as "$NODE_ID" whoami 2>&1 | sed 's/^/  /' ) \
            || echo "  （查不到 —— 可能未注册或服务端不可达）"
    fi
}

case "${1:-}" in
    start)      shift; cmd_start "${1:-}" ;;
    stop)       cmd_stop ;;
    status)     cmd_status ;;
    foreground) cmd_foreground ;;
    skill)      install_skill ;;
    log)        tail -f "$LOG_FILE" ;;
    *)
        cat <<USAGE
用法：$0 {start [--with-bash] | stop | status | foreground | skill | log}

  start       后台启动（默认只读工具：Read/Grep/Glob）
  start --with-bash  含 Bash/WebFetch —— 攻击面大，慎用
  foreground  前台运行，Ctrl-C 停（调 CodeBuddy 行为时用这个）
  skill       只同步 skill 文档（start 会自动做）
  status      进程状态 + 服务端视角的 whoami
  log         跟踪日志
USAGE
        exit 2 ;;
esac
