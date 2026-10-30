#!/usr/bin/env bash
# ==============================================================================
#  check-git.sh —— 检查服务器能否**独立**拉取 nexus-mesh
# ==============================================================================
#
# ⚠ 关键在「独立」这两个字。判据是：**不依赖任何外部凭据**。
#
#   本地用 `ssh -A` 转发 agent 时，服务器能借用你电脑上的 GitHub 凭据 ——
#   `git fetch` 会成功。但 **systemd / cron / 任何自动化环境里永远没有转发**。
#
#   所以只在带 -A 的会话里测，会得出"配好了"的错误结论。
#
#   ## 为什么光加 BatchMode=yes 不够
#
#   `BatchMode=yes` 只禁止**交互式**输入（密码、口令），
#   **它不会禁用 agent**。所以在 -A 会话里跑，checks 照样会通过 ——
#   因为用了转发的 key。
#
#   要真正模拟生产条件，必须显式**关掉 agent**：
#       -o IdentityAgent=none      （不连 agent socket）
#       -o IdentitiesOnly=yes      （只用 config 里指定的那一个）
#
#   这样测出来的才是「这台机器自己有没有凭据」。
#
# 用法：bash /root/nexus-mesh-deploy/check-git.sh
# ==============================================================================

set -u

cd /opt/nexus-mesh || { echo "  ✗ /opt/nexus-mesh 不存在"; exit 1; }

#: 模拟生产条件：不交互、不用 agent、只用 config 里那把 deploy key
SSH_INDEPENDENT=(ssh
    -o BatchMode=yes
    -o IdentityAgent=none
    -o IdentitiesOnly=yes
    -o ConnectTimeout=10
)

echo "  仓库: $(pwd)"
echo "  remote: $(git remote get-url origin)"
echo "  当前提交: $(git log --oneline -1)"
echo

# ---- ① GitHub 认证（不借助 agent）----
echo -n "  GitHub 认证（不用 agent）: "
if out=$("${SSH_INDEPENDENT[@]}" -T git@github.com 2>&1); then
    echo "$out" | head -1
else
    echo "✗ 失败"
    echo
    cat <<'HINT'
  deploy key 还没加到 GitHub。三步：

    ① 复制这行公钥（整行，从 ssh-ed25519 到结尾）：

HINT
    sed 's/^/         /' /root/.ssh/nexus-mesh-deploy.pub
    cat <<'HINT'

    ② 打开 https://github.com/gzchenjiesong/nexus-mesh/settings/keys
       → 「Add deploy key」
       Title: cvm-43.138.222.206
       Key:   粘贴上面那行
       ⚠ **不要勾 "Allow write access"** —— 部署只需要读，
         给它写权限等于让服务器能改仓库。

    ③ 回来重跑：
         bash /root/nexus-mesh-deploy/check-git.sh

  ⚠ 在此之前，部署仍可用（临时借本机凭据）：
       ssh -A root@<这台机器> 'bash /root/nexus-mesh-deploy/deploy.sh'

HINT
    exit 1
fi

# ---- ② 能否 fetch（同样不借助 agent）----
echo -n "  fetch origin/main: "
if GIT_SSH_COMMAND="${SSH_INDEPENDENT[*]}" \
        git fetch origin main --dry-run >/dev/null 2>&1; then
    echo "✓ 可用"
else
    echo "✗ 失败"
    GIT_SSH_COMMAND="${SSH_INDEPENDENT[*]}" \
        git fetch origin main 2>&1 | tail -4 | sed 's/^/      /'
    exit 1
fi

echo
echo "  ✓ 服务器可独立拉取 —— systemd / cron 环境下不会有问题。"
