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

# CA 落盘位置。**必须在被引用之前定义** —— 脚本开头是 set -u，
# 未定义变量一引用就退出（不是当空串）。
# 这个错误我把 CA 段落前移时犯过一次：原来的定义在被替换掉的那段里，
# 移走后就没有了。
CA_CANDIDATE="$CONF_DIR/ca.pem"

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

# ⚠ 先建目录 —— CA 块要往 $CONF_DIR 里写文件。
#   这个顺序同样是踩过的：CA 段落前移后，建目录还在原处（后面），
#   于是复制时报 ENOENT。**移一个块，要检查它依赖的前提是否也移了。**
mkdir -p "$CONF_DIR" "$AGENT_DIR"
chmod 700 "$CONF_DIR" "$AGENT_DIR" 2>/dev/null || true

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
  bad "找不到 CA 证书"
  say "     期望位置：$REPO_CA"
  say ""
  say "   ⚠ 仓库里应该有但你没有？最常见原因是 .gitignore 拦了它。"
  say "     .gitignore 有 *.pem（防私钥泄露，是对的），"
  say "     deploy/ca.pem 需要 !deploy/ca.pem 例外。"
  say ""
  say "     验证：git ls-files deploy/ca.pem"
  say "       有输出 → 在仓库里，可能 clone 不完整"
  say "       无输出 → 没入库"
  say ""
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

# 2. 网络与证书 —— **必须在 CA 准备好之后**
#
# ⚠⚠ 两点都是实测踩出来的：
#
#   ① 顺序：初版网络检查在前、CA 复制在后。于是检查时本地没有 CA，
#      只能退回系统 CA 库 —— 自签证书必然验不过。
#      **这项检查在修好之前永远失败**，无论环境多正确。
#
#   ② 工具：**用 Python，不用 curl**。桥接用的是 Python 的 ssl 模块。
#      curl 是另一个程序、另一套判断：
#        · macOS/Linux 的 curl 默认不开 VERIFY_X509_STRICT
#        · Python 3.13+ 默认开，要求 CA 有 keyUsage
#      拿 curl 的结论判断"桥接能不能连"没有意义。
say ""
say "→ 网络与证书"

PY_HEALTH=$(mktemp 2>/dev/null || echo "/tmp/nxs-health-script.$$")
cat > "$PY_HEALTH" <<'PYHEALTH_EOF'
import json, os, ssl, urllib.error, urllib.request
url = os.environ.get("NXS_CHK_URL", "")
ca = os.environ.get("NXS_CHK_CA", "")
out = {"ok": False, "url": url}
try:
    ctx = ssl.create_default_context(cafile=ca) if ca else None
    if ctx:
        out["strict"] = bool(ctx.verify_flags & ssl.VERIFY_X509_STRICT)
    with urllib.request.urlopen(url, context=ctx, timeout=10) as r:
        out["ok"] = (r.status == 200)
        out["status"] = r.status
except urllib.error.URLError as e:
    reason = e.reason
    if isinstance(reason, ssl.SSLCertVerificationError):
        out["err"] = "cert: " + str(reason)
        out["verify_code"] = getattr(reason, "verify_code", None)
    else:
        out["err"] = "net: " + str(reason)
except ssl.SSLCertVerificationError as e:
    out["err"] = "cert: " + str(e)
    out["verify_code"] = getattr(e, "verify_code", None)
except Exception as e:
    out["err"] = type(e).__name__ + ": " + str(e)
print(json.dumps(out))
PYHEALTH_EOF

NXS_CHK_URL="https://${SERVER}/nxs/health"
if [ -f "$CA_CANDIDATE" ]; then NXS_CHK_CA="$CA_CANDIDATE"; else NXS_CHK_CA=""; fi
export NXS_CHK_URL NXS_CHK_CA
health=$("$PY" "$PY_HEALTH" 2>/dev/null)
unset NXS_CHK_URL NXS_CHK_CA
rm -f "$PY_HEALTH"

case "$health" in
  *'"ok": true'*)
    if echo "$health" | grep -q '"strict": true'; then
      ok "协调节点可达（HTTP 200，CA 校验通过，严格模式）"
    else
      ok "协调节点可达（HTTP 200，CA 校验通过）"
    fi
    ;;
  *'"err": "cert:'*)
    bad "网络通，但**证书校验失败**"
    say ""
    say "   $health"
    say ""
    if echo "$health" | grep -q "key usage"; then
      say "   → CA 证书**缺少 Key Usage 扩展**。这不是你的文件坏了，"
      say "     是服务端那张 CA 不合规 —— 服务端要跑 deploy/gen-tls.sh 重新生成。"
    else
      say "   → 按顺序查："
      say "     a) 与服务器是否同一份 CA（比对 SHA256 指纹）"
      say "     b) 文件是否被改坏（BOM / 截断）"
      say "     c) 服务器是否换过证书"
    fi
    if [ "$CHECK_ONLY" = "1" ]; then exit 1; fi
    ;;
  *'"err": "net:'*)
    bad "协调节点不可达"
    say ""
    say "   $health"
    say ""
    say "   → 网络层问题："
    say "     1. 防火墙是否拦出站"
    say "     2. 是否有代理：echo \$HTTPS_PROXY"
    if [ "$CHECK_ONLY" = "1" ]; then exit 1; fi
    ;;
  "")
    warn "健康检查没拿到结果 —— 继续做后续检查"
    ;;
  *)
    warn "健康检查异常输出：$health"
    ;;
esac

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

  # ⚠ **必须校验里面的 CLI 真的存在。**
  #
  #   白名单写错（拼错、或工具不在 PATH）时注册照样成功 ——
  #   白名单只是"允许 CodeBuddy 调什么"，协议层不检查程序存不存在。
  #   于是任务能派进来，但 Agent 执行时报"我做不到"——
  #   那看起来像能力不足，很难联想到是名字拼错了。
  tools=$(printf '%s' "$CODEBUDDY_ALLOWED_TOOLS" \
          | grep -oE 'Bash\([[:space:]]*[A-Za-z0-9_.-]+' \
          | sed 's/^Bash([[:space:]]*//' | sort -u)
  missing=""
  found_n=0
  for t in $tools; do
    if command -v "$t" >/dev/null 2>&1; then
      say "       ✓ $t -> $(command -v "$t")"
      found_n=$((found_n + 1))
    else
      missing="$missing $t"
    fi
  done

  if [ -n "$missing" ]; then
    total=$(printf '%s\n' $tools | wc -l | tr -d ' ')
    say ""
    if [ "$found_n" -eq 0 ]; then
      bad "白名单里的 CLI **一个都找不到**:$missing"
      say ""
      say "   → 这不是警告，是配置错误。注册会成功，但任务必然失败。"
      say "     确认：command -v$(echo "$missing" | cut -d' ' -f2)"
      if [ "$CHECK_ONLY" = "1" ]; then exit 1; fi
    else
      warn "这些 CLI 找不到:$missing"
      say "       注册不受影响，但需要它们的任务会失败。"
    fi
  elif [ -n "$tools" ]; then
    say "       （全部可在 PATH 中找到）"
  fi

  if ! printf '%s' "$CODEBUDDY_ALLOWED_TOOLS" | grep -qE '(Read|Grep|Glob|Write)'; then
    warn "白名单里没有 Read/Grep —— Agent 可能读不了文件"
  fi
else
  bad "未配置 CODEBUDDY_ALLOWED_TOOLS"
  say ""
  say "  这一步**必须你自己填** —— 我不知道你公司内网的 CLI 叫什么。"
  say "  形如（尖括号里的换成真实的）："
  say ""
  say "    export CODEBUDDY_ALLOWED_TOOLS='Bash(<内部CLI名>:*),Read,Grep'"
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
