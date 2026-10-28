#!/usr/bin/env bash
# ==============================================================================
#  install-skill.sh —— 把 nexus-mesh skill 装到各 Agent 的 skill 目录
# ==============================================================================
#
#  为什么需要脚本：两个 Agent 的 skill 机制不同，位置也不同。
#
#    Hermes    /root/.hermes/profiles/<profile>/skills/<分类>/<名>/SKILL.md
#              扫目录加载，装完 `hermes skills list` 里能看到
#
#    OpenClaw  /root/.openclaw/workspace-<agent>/skills/<名>/SKILL.md
#              有自己的 `skills install`，且会检查 frontmatter 里的
#              metadata.openclaw.requires（比如 nxs-cli 在不在 PATH）
#
#  **两份用同一个源文件**（仓库里的 nexus-mesh-skill.md）——
#  只是复制到不同位置。不要各维护一份内容，那会腐化。
#
#  用法：
#      bash deploy/install-skill.sh              # 装到全部已配置的 Agent
#      bash deploy/install-skill.sh --verify     # 只检查现状，不改动
#
# ==============================================================================

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_DIR/nexus-mesh-skill.md"

HERMES_PROFILE="${HERMES_PROFILE:-working}"
HERMES_SKILLS="/root/.hermes/profiles/$HERMES_PROFILE/skills/agent-network/nexus-mesh"
OPENCLAW_AGENT="${OPENCLAW_AGENT:-taskflow}"
OPENCLAW_SKILLS="/root/.openclaw/workspace-$OPENCLAW_AGENT/skills/nexus-mesh"

HERMES_PY="${HERMES_PY:-/usr/local/lib/hermes-agent/venv/bin/python}"
OPENCLAW_NODE="${OPENCLAW_NODE:-/root/.hermes/node/bin/node}"
OPENCLAW_CLI="${OPENCLAW_CLI:-/usr/local/lib/node_modules/openclaw/dist/index.js}"

VERIFY_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --verify) VERIFY_ONLY=1 ;;
    -h|--help) sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$SRC" ]]; then
  echo "找不到 skill 源文件：$SRC" >&2
  exit 1
fi

# ------------------------------------------------------------------ 前置检查
# frontmatter 必须是合法 YAML —— 两个 Agent 都靠它读 name/description。
# 这里用一个极小的检查：开头是 ---、第二个 --- 之前有 name: 和 description:
head -1 "$SRC" | grep -qx -- '---' || { echo "skill 文件缺 frontmatter" >&2; exit 1; }
python3 - "$SRC" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
if not m:
    sys.exit("frontmatter 格式不对")
fm = m.group(1)
for key in ("name:", "description:"):
    if key not in fm:
        sys.exit(f"frontmatter 缺 {key}")
PY

# ------------------------------------------------------------------ 安装
install_hermes() {
  echo "→ Hermes（profile=$HERMES_PROFILE）"
  if [[ "$VERIFY_ONLY" == "1" ]]; then
    [[ -f "$HERMES_SKILLS/SKILL.md" ]] && echo "   已安装" || echo "   未安装"
    return 0
  fi
  mkdir -p "$HERMES_SKILLS"
  cp "$SRC" "$HERMES_SKILLS/SKILL.md"
  chmod 644 "$HERMES_SKILLS/SKILL.md"
  echo "   写入 $HERMES_SKILLS/SKILL.md"
}

install_openclaw() {
  echo "→ OpenClaw（agent=$OPENCLAW_AGENT）"
  if [[ ! -x "$OPENCLAW_NODE" ]]; then
    echo "   跳过：找不到 node（$OPENCLAW_NODE）"
    return 0
  fi

  if [[ "$VERIFY_ONLY" == "1" ]]; then
    HERMES_HOME="/root/.hermes/profiles/$HERMES_PROFILE" \
      "$OPENCLAW_NODE" "$OPENCLAW_CLI" skills --agent "$OPENCLAW_AGENT" \
      info nexus-mesh 2>&1 | grep -E "Ready|Visible to model" || echo "   未安装"
    return 0
  fi

  # 用 OpenClaw 自己的 install —— 它会校验 frontmatter 并检查 requires
  local pkg
  pkg="$(mktemp -d)/nexus-mesh"
  mkdir -p "$pkg"
  cp "$SRC" "$pkg/SKILL.md"
  # --force：已装过时要覆盖。不加的话 update 会静默失败，
  # 而"脚本说成功了、其实还是旧内容"是最难发现的那类问题。
  "$OPENCLAW_NODE" "$OPENCLAW_CLI" skills --agent "$OPENCLAW_AGENT" \
    install "$pkg" --acknowledge-install-policy-warning --force 2>&1 | tail -2
  rm -rf "$(dirname "$pkg")"
}

install_hermes
install_openclaw

# ------------------------------------------------------------------ 验证
echo
echo "→ 验证"
echo "   Hermes:"
HERMES_HOME="/root/.hermes/profiles/$HERMES_PROFILE" \
  "$HERMES_PY" -m hermes_cli.main skills list 2>/dev/null \
  | grep -i nexus | sed 's/^/     /' || echo "     ✗ 列表里没有 nexus-mesh"

echo "   OpenClaw:"
"$OPENCLAW_NODE" "$OPENCLAW_CLI" skills --agent "$OPENCLAW_AGENT" \
  info nexus-mesh 2>/dev/null | head -1 | sed 's/^/     /' || echo "     ✗ 查不到"

echo
echo "装完还要做一件事：**告诉每个 Agent 它自己的节点 ID**。"
echo "  skill 是通用文档，用的是 <你的节点ID> 占位符。"
echo "  Hermes 能从 echo \$NXS_ACTOR 拿到；OpenClaw 拿不到，"
echo "  要写进它的 AGENTS.md（见 docs/agent-bridges.md 第九节）。"
