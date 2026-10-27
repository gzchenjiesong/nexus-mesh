"""`nxs-cli` —— 给 Agent 用的命令行工具。

与 `nxs`（运维/开发命令）的分工：

====================  ==================================================
`nxs`                 本机开发与运维：verify / doctor / serve / demo /
                      ops（用**运维密钥**批准节点）/ ui
`nxs-cli`             **Agent 自己用的工具**：以某个节点身份接入网络、
                      发任务、取结果。用**节点凭据**，不是运维密钥
====================  ==================================================

为什么分成两个入口而不是加子命令：

* 权限面不同。`ops`（批准/吊销节点）只该给运维；把它和 Agent 的
  日常工具放在同一个二进制里，等于让 `--as <节点>` 的调用也能摸到
  `ops` —— 那需要额外的"这个子命令不许节点身份调用"检查，而那种
  检查忘了加就是提权。
* 装的位置不同。`nxs-cli` 要进 PATH 给 Agent 的 shell 用；
  `nxs` 只需要在仓库里跑。

刻意**不做**的事：不留 `ops` 子命令、不留 `serve`、不留 `demo`。
"""

from .actor import Actor, load_actor, resolve_actor, write_actor_file

__all__ = ["Actor", "load_actor", "resolve_actor", "write_actor_file"]
