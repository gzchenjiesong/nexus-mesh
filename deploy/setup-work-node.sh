#!/usr/bin/env bash
# ==============================================================================
#  setup-work-node.sh —— 在公司电脑上装 Nexus Mesh 执行者节点
# ==============================================================================
#
#  这一步只做本机侧的准备工作：凭据、CA、skill 目录、环境变量。
#  **不启动节点** —— 启动前应该先跑 --check-tools 确认 CodeBuddy 能用。
#
#  用法：
#      bash deploy/setup-work-node.sh              # 装本机依赖
#      bash deploy/setup-work-node.sh --check      # 只检查现状
#
#  与云服务器的区别（都是踩过才知道的）：
#    · macOS 没有 /etc/nexus-mesh 的写权限 → 凭据放 ~/.nexus-mesh
#    · 没有 systemd → 用 launchd（~/.nexus-mesh/*.plist）
#    · codebuddy 装在 ~/.local/bin 或 npm 全局 → 要进 PATH
#
# ==============================================================================

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF_DIR="${NXS_ACTOR_DIR:-$HOME/.nexus-mesh}"
AGENT_DIR="$CONF_DIR/agents"
SERVER="${NXS_SERVER:-43.138.222.206:18800}"
NODE_ID="${NXS_NODE_ID:-work-desktop}"
SERVER_URL="https://$SERVER/nxs"

CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
ok()  { printf '  ✓ %s\n' "$*"; }
bad() { printf '  ✗ %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*"; }

# ------------------------------------------------------------------ 前置检查
say "→ 环境检查"
say "  节点 ID    : $NODE_ID"
say "  协调节点   : $SERVER_URL"
say "  凭据目录   : $AGENT_DIR"

# 1. python 能跑吗
PY=""
for cand in "$REPO_DIR/.venv/bin/python" "$(command -v python3)"; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -n "$PY" ]; then ok "Python: $PY"; else bad "找不到 Python"; exit 1; fi

# 2. 服务器通吗 —— **这是最先要验的**，公司网络可能拦出站
say ""
say "→ 网络（这一步最容易卡住）"
CA_CANDIDATE="$CONF_DIR/ca.pem"
if [ -f "$CA_CANDIDATE" ]; then
  code=$(curl -sS --cacert "$CA_CANDIDATE" --max-time 10 \
    -o /dev/null -w "%{http_code}" "$SERVER_URL/health" 2>/dev/null || echo "000")
else
  code=$(curl -sS -k --max-time 10 \
    -o /dev/null -w "%{http_code}" "$SERVER_URL/health" 2>/dev/null || echo "000")
fi
if [ "$code" = "200" ]; then
  ok "协调节点可达（HTTP ${code}）"
else
  bad "协调节点不可达（HTTP ${code}）"
  say ""
  say "  排查顺序："
  say "    1. 公司网络是否拦了出站 18800：telnet $SERVER 18800"
  say "    2. 是否有 HTTP 代理：echo \$HTTPS_PROXY"
  say "    3. 自签 CA 是否被代理拦截（企业 TLS 中间人会换证书）"
  exit 1
fi

# 3. codebuddy 在吗 —— 执行者节点的核心依赖
say ""
say "→ CodeBuddy CLI（执行者靠它）"
CB=""
for cand in "${CODEBUDDY_BIN:-}" "$HOME/.local/bin/codebuddy" \
            "$(command -v codebuddy 2>/dev/null)"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then CB="$cand"; break; fi
done
if [ -n "$CB" ]; then
  ok "codebuddy: $CB"
  "$CB" --version 2>/dev/null | sed 's/^/      /' || true
else
  bad "没装 codebuddy"
  say ""
  say "  二选一："
  say "    npm install -g @tencent-ai/codebuddy-code"
  say "    curl -fsSL https://www.codebuddy.cn/cli/install.sh | bash"
  say ""
  say "  装完先跑一次交互式 \`codebuddy\` 完成登录，再回来。"
  if [ "$CHECK_ONLY" = "1" ]; then exit 1; fi
  say ""
  say "  （继续做其余准备工作，但节点无法启动）"
fi

# 4. 工具白名单 —— 没有它节点会拒绝启动（故意的）
say ""
say "→ 工具白名单（决定这个节点能做什么）"
if [ -n "${CODEBUDDY_ALLOWED_TOOLS:-}" ]; then
  ok "已配置: $CODEBUDDY_ALLOWED_TOOLS"
else
  bad "未配置 CODEBUDDY_ALLOWED_TOOLS"
  say ""
  say "  这一步**必须你自己填** —— 我不知道你公司内网的 CLI 叫什么。"
  say "  形如（尖括号里的换成真实的）："
  say ""
  say "    export CODEBUDDY_ALLOWED_TOOLS='Bash(projectctl:*),Bash(meetingctl:*),Read,Grep'"
  say ""
  say "  含义：只能调这两个 CLI + 读文件 + 搜文件。"
  say "  **不能执行任意命令、不能写文件。**"
  say ""
  say "  为什么必须给：不给白名单意味着 CodeBuddy 能用全部工具（含任意 Bash），"
  say "  那这个节点就等于把公司电脑的 shell 挂到了任务总线上。"
fi

if [ "$CHECK_ONLY" = "1" ]; then
  say ""
  say "检查完毕（--check 模式，未做改动）"
  exit 0
fi

# ------------------------------------------------------------------ 建目录
say ""
say "→ 准备本机文件"
mkdir -p "$AGENT_DIR"
chmod 700 "$CONF_DIR" "$AGENT_DIR"
ok "凭据目录: $AGENT_DIR"

# CA —— 优先从仓库复制（与 Windows 版同一策略）
#
# 手工粘贴 PEM 很容易坏：记事本存成 UTF-8 BOM 或内容被截断，
# curl 都只报 (77) error setting certificate verify locations ——
# 而那句话里看不出"编码问题"，常被误判成网络故障。
# CA 是公开证书，本来就该随代码走。
REPO_CA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ca.pem"
if [ -f "$CA_CANDIDATE" ]; then
  ok "CA 已存在: $CA_CANDIDATE"
  # 坏文件自动修复：太小 或 有 BOM
  size=$(wc -c < "$CA_CANDIDATE" | tr -d ' ')
  if [ "$size" -lt 800 ]; then
    warn "但只有 ${size} 字节（正常约 1200），可能粘贴不完整"
    [ -f "$REPO_CA" ] && cp "$REPO_CA" "$CA_CANDIDATE" && ok "已用仓库版本覆盖"
  elif [ "$(head -c 3 "$CA_CANDIDATE" | od -An -tx1 | tr -d ' ')" = "efbbbf" ]; then
    warn "检测到 UTF-8 BOM —— curl 会报 (77)"
    [ -f "$REPO_CA" ] && cp "$REPO_CA" "$CA_CANDIDATE" && ok "已用仓库版本覆盖（去掉 BOM）"
  fi
elif [ -f "$REPO_CA" ]; then
  cp "$REPO_CA" "$CA_CANDIDATE"
  chmod 644 "$CA_CANDIDATE"
  ok "CA 已从仓库复制: $CA_CANDIDATE"
else
  bad "找不到 ca.pem（应在仓库 deploy/ca.pem）"
  say "     或从服务器取：scp root@${SERVER}:/etc/nexus-mesh/tls/ca.pem $CA_CANDIDATE"
fi

# 凭据文件 —— 密钥必须**从服务器取**，不能在这里生成
CRED="$AGENT_DIR/$NODE_ID.env"
if [ -f "$CRED" ]; then
  ok "凭据已存在: ${CRED}（不覆盖）"
else
  bad "缺少凭据: $CRED"
  say ""
  say "  密钥在服务器上（env 里叫 NXS_SECRET_$(echo "$NODE_ID" | tr 'a-z-' 'A-Z_')）。"
  say "  取法 —— 在服务器上跑："
  say ""
  say "    grep NXS_SECRET_$(echo "$NODE_ID" | tr 'a-z-' 'A-Z_') /etc/nexus-mesh/nxs.env"
  say ""
  say "  然后把这一段写到本机 ${CRED}，内容形如："
  say ""
  say "    NXS_NODE_ID=$NODE_ID"
  say "    NXS_COORDINATOR_URL=$SERVER_URL"
  say "    NXS_CA_FILE=$CA_CANDIDATE"
  say "    NXS_SECRET=<从服务器取到的值>"
  say ""
  say "  权限必须是 600：chmod 600 $CRED"
  say ""
  say "  ⚠ 不在这里生成新密钥 —— 两边不一致的话症状是 AU-SIG-01，"
  say "    而那看起来像「密钥写错」，实际是「两边不是同一把」。"
fi

# skill —— 用统一的安装脚本，避免两份内容腐化
say ""
say "→ 安装 skill"
if [ -f "$REPO_DIR/deploy/install-skill.sh" ]; then
  # 本机没有 /root/... 路径，这里只放 WorkBuddy 的 skill 目录
  WB_SKILLS="$HOME/.workbuddy/skills"
  mkdir -p "$WB_SKILLS/agent-network/nexus-mesh"
  cp "$REPO_DIR/nexus-mesh-skill.md" "$WB_SKILLS/agent-network/nexus-mesh/SKILL.md"
  ok "已放 WorkBuddy skill: $WB_SKILLS/agent-network/nexus-mesh/SKILL.md"
else
  bad "找不到 install-skill.sh"
fi

# ------------------------------------------------------------------ 收尾
say ""
say "→ 接下来"
say ""
say "  ① 把环境变量写进 ~/.zshrc："
say ""
say "     export NXS_ACTOR_DIR=\"$AGENT_DIR\""
say "     export NXS_COORDINATOR_URL=\"$SERVER_URL\""
say "     export NXS_CA_FILE=\"$CA_CANDIDATE\""
say "     export CODEBUDDY_BIN=\"${CB:-$HOME/.local/bin/codebuddy}\""
say "     export CODEBUDDY_ALLOWED_TOOLS='<你填的>'"
say ""
say "  ② 确认 CodeBuddy 能用（会消耗一点额度）："
say ""
say "     cd $REPO_DIR && $PY examples/codebuddy_node.py --check-tools"
say ""
say "  ③ 注册节点（会等批准）："
say ""
say "     $PY examples/codebuddy_node.py --dry-run"
say ""
say "  ④ 在**服务器**上批准："
say ""
say "     nxs ops approve $NODE_ID --url $SERVER_URL --ca-file <服务器上的 ca.pem>"
say ""
say "  ⑤ 本机常驻（launchd，见 docs/agent-bridges.md）"
say ""
