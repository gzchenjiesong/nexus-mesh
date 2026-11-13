#!/usr/bin/env bash
# ==============================================================================
#  entrypoint.sh —— 服务器上的部署入口（薄壳）
# ==============================================================================
#
# 这个文件是给**服务器**用的，不是给本地用的。
#
# 部署方式（在服务器上执行）：
#
#     install -m 755 /opt/nexus-mesh/deploy/entrypoint.sh \
#                    /root/nexus-mesh-deploy/deploy.sh
#
# 之后照旧：
#
#     bash /root/nexus-mesh-deploy/deploy.sh
#     bash /root/nexus-mesh-deploy/deploy.sh --no-restart
#
# ==============================================================================
#  ⚠ 为什么这里必须是薄壳，而不是完整脚本的一份拷贝
# ==============================================================================
#
# **部署脚本无法通过"部署"来更新自己 —— 鸡生蛋。**
#
# 2026-11-13 实测踩到：`/root/nexus-mesh-deploy/deploy.sh` 放的是仓库版的
# 一份**拷贝**，于是两边静默分裂：
#
#     /root/nexus-mesh-deploy/deploy.sh   4664 字节   10-30 的版本
#     /opt/nexus-mesh/deploy/deploy.sh    5846 字节   已加了「同步 systemd unit」
#
# 也就是说：**改的是后者，执行的是前者。**
#
# 于是出现了一串"所有断言都绿但功能没生效"的现象：
#   · 部署脚本说「完成、全部节点在线」          —— 旧脚本打的
#   · 给 UI 加了 NXS_UI_RECOVER_URL             —— 写进了没被执行的那份
#   · 进程环境里根本没有这个变量                —— 因为新脚本从没跑过
#
# 这类缺陷特别难查，因为每一步单独看都正常：代码是新的（cookie 的 Max-Age
# 生效能证明）、服务 active、节点全在线。唯独"你以为改的地方"和"实际执行
# 的地方"是两个文件。
#
# 现在只留这一层薄壳：唯一逻辑在 /opt/nexus-mesh/deploy/deploy.sh，
# 它随 git pull 更新，所以部署脚本**能通过部署来更新自己了**。
#
# ==============================================================================
#  ⚠ 为什么用薄壳而不是符号链接
# ==============================================================================
#
# 符号链接（ln -sf）同样能解决分裂，但仓库目录一旦被删除或 git 操作失败，
# 链接就断了 —— 那时你手里连一个能报错的脚本都没有。
#
# 薄壳多这十几行，换来的是：/opt/nexus-mesh 出问题时它会**明确告诉你去修
# 什么**，而不是神秘地 file not found。
#
# ==============================================================================
#  ⚠ 副产品：这个目录里的其他文件都是**符号链接**，指向仓库
# ==============================================================================
#
#     check-git.sh            → /opt/nexus-mesh/deploy/check-git.sh
#     install-ui-env.sh       → ...
#     nexus-mesh-ui.service   → ...
#     verify-tls-resilience.py → ...
#
# ## 这带来的一个真实踩坑（2026-11-13）
#
# 我改了 `deploy/check-git.sh`，想"快速同步到服务器"，于是：
#
#     scp deploy/check-git.sh nexus-hub:/root/nexus-mesh-deploy/
#
# 看起来只动了 /root 下的文件。但那个路径是**符号链接**，scp 顺着链接
# 写进了 /opt/nexus-mesh/deploy/check-git.sh —— 也就是**仓库的工作区**。
#
# 后果：下次 `git pull` 报
#
#     error: Your local changes to deploy/check-git.sh
#            would be overwritten by merge. Aborting
#
# 而那个"本地改动"其实是我自己刚提交过的内容 —— 一个自造的冲突。
#
# ## 正确做法
#
# **改仓库 → commit → push → 跑部署脚本。** 部署脚本会 git pull，
# 符号链接自然指向新内容。永远不要 scp 到符号链接上。
#
# 需要临时验证某个改动时，拷到 /tmp 跑，别覆盖仓库。
# ==============================================================================

set -euo pipefail

TARGET=/opt/nexus-mesh/deploy/deploy.sh

if [ ! -f "$TARGET" ]; then
    echo "  ✗ 找不到 $TARGET" >&2
    echo "" >&2
    echo "    仓库没拉到？先在 /opt/nexus-mesh 执行：" >&2
    echo "      git clone <repo> /opt/nexus-mesh   # 首次" >&2
    echo "      git -C /opt/nexus-mesh pull        # 已有" >&2
    echo "" >&2
    echo "    恢复方式：本目录可能有 .bak-<时间戳> 备份，见 git 历史。" >&2
    exit 1
fi

exec bash "$TARGET" "$@"
