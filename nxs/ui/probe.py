"""本地探针 —— 在 coordinator 不可达时，回答「它到底怎么了」。

## ⚠ 为什么需要这个（2026-11-13）

观察窗的数据全部来自 coordinator 的 ops 端点。于是有个结构性矛盾：

```
coordinator 卡死 → ops 端点不响应 → 面板什么都拿不到
        ↓
而「它卡死了」正是面板唯一有意义的时刻
```

2026-10-31 那次就是这样：coordinator 冻结了 **13 天**，
面板能说的只有「连不上」——和 `curl` 没区别。

### 探针补的是「为什么」

它不看业务数据，只看**主机层的事实**：

| 问题 | 查什么 |
|---|---|
| 进程还在吗 | `systemctl is-active` + `/proc/<pid>` 是否存在 |
| 卡在什么上 | `/proc/<pid>/wchan` |
| 有多少线程 | `/proc/<pid>/status` 的 Threads |
| 吃多少内存 | `VmRSS` |
| 端口在听吗、有人处理吗 | `ss -tln` 的 LISTEN 状态与 **Recv-Q** |

`Recv-Q` 尤其关键：**队列在涨而进程不处理 = 卡死**。
2026-10-31 那次实测就是 `Recv-Q=6`（6 个连接排队，无人 accept）。

## 三条设计约束

### ① 不发起任何网络请求

看起来「探一下 /health 不就知道了吗」——但那会和 `snapshot()`
已经做的 ops 调用**重复等待**。coordinator 卡住时，ops 调用先等满
超时，探针再等一次，用户要等两倍时间。

所以：**「能否响应」由 ops 调用的成败给出**（那是既有事实），
探针只解释「为什么不能」。它纯读本地文件与内核状态，约 20ms。

### ② 每一项独立容错

`ss` 在受限环境里可能没有；`/proc` 在容器里可能不可读；
systemd 可能不是 init 系统。**一项失败不能拖垮整份探针** ——
每项各自 try，失败就留 `None` 并记进 `unavailable`。

否则「探针本身坏了」会被误读成「coordinator 死了」。

### ③ 给判断，不只给数字

`wchan=futex_wait_queue` 对多数人没意义。探针把它翻成人话：

    futex_wait_queue  → 阻塞在锁上（若持续，疑似死锁）
    do_epoll_wait     → 正常等待事件
    sk_wait_data      → 等待网络数据

最后给一个 ``verdict``：``healthy`` / ``dead`` / ``unresponsive`` /
``unknown``。**那才是人想知道的。**
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 单条本地命令的超时（秒）。
#:
#: 这些都是读本地文件/查内核状态的命令，正常在 10ms 内返回。
#: 给 3 秒是留给系统负载极高时的余量 —— 超过就说明这台机器本身有问题了，
#: 那时探针也不该继续等（它存在的意义是"快速给出线索"）。
PROBE_CMD_TIMEOUT = 3.0

#: wchan 的人话翻译。
#:
#: 只覆盖常见值 —— 未收录的原样显示，不猜。
#: **猜错比不猜更糟**：会让运维顺着错误方向排查。
_WCHAN_HINTS: dict[str, str] = {
    "futex_wait_queue": "阻塞在锁上（若持续如此，疑似死锁）",
    "futex_wait": "阻塞在锁上（若持续如此，疑似死锁）",
    "do_epoll_wait": "正常等待事件（空闲时的常态）",
    "ep_poll": "正常等待事件（空闲时的常态）",
    "sk_wait_data": "等待网络数据（连接已建立、等对端发）",
    "inet_csk_accept": "等待新连接（accept 循环正常）",
    "poll_schedule_timeout": "定时等待（如心跳循环）",
    "hrtimer_nanosleep": "定时休眠（如心跳循环）",
    "0": "正在运行（未阻塞）",
}


def _run(cmd: list[str]) -> str | None:
    """跑一条本地命令，返回 stdout；任何失败都返回 None。

    ⚠ **不让失败冒泡**：探针的职责是「尽量报告」，不是「报告不了就罢工」。
    失败信息由调用方通过 ``unavailable`` 列表体现。
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=PROBE_CMD_TIMEOUT,
            check=False,
            # 不传 shell=True：参数虽来自内部常量，但保持"永不经过 shell"
            # 这个习惯比"这次安全"更可靠
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def _read_text(path: Path) -> str | None:
    """读一个小文件，失败返回 None。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _parse_status(raw: str) -> dict[str, Any]:
    """从 ``/proc/<pid>/status`` 里取 State / Threads / VmRSS。"""
    out: dict[str, Any] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "State":
            # "S (sleeping)" → "S"
            out["state"] = value.split()[0] if value else ""
        elif key == "Threads":
            try:
                out["threads"] = int(value)
            except ValueError:
                pass
        elif key == "VmRSS":
            # "29488 kB" → 28.8 (MB)
            match = re.match(r"(\d+)", value)
            if match:
                out["rss_mb"] = round(int(match.group(1)) / 1024, 1)
    return out


def _parse_accept_queue(ss_output: str, port: int) -> int | None:
    """从 ``ss -tln`` 输出里取该端口的 Recv-Q（accept 队列长度）。

    行格式：

        State  Recv-Q  Send-Q  Local Address:Port  Peer Address:Port
        LISTEN 6       5       0.0.0.0:18800       0.0.0.0:*

    ``Recv-Q`` 是**已建立但还没被 accept 的连接数**。
    它持续大于 0 是「进程不 accept」的直接证据 ——
    2026-10-31 那次实测就是 6。
    """
    for line in ss_output.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        if parts[0] != "LISTEN":
            continue
        local = parts[3]
        if not local.endswith(f":{port}"):
            continue
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None


def probe_coordinator(
    *,
    ops_reachable: bool,
    unit: str = "nexus-mesh",
    port: int = 18800,
) -> dict[str, Any]:
    """探测 coordinator 的主机层状态。

    :param ops_reachable: **ops 端点是否响应**。由调用方（``snapshot``）
        传入 —— 那是既有事实，不需要探针重复探测（见模块说明 ①）。

    返回结构见模块说明。关键字段：

    ``verdict``
        ``healthy``      进程在、ops 通
        ``dead``         进程不在了
        ``unresponsive`` **进程在但 ops 不通** —— 最危险的一种，
                         因为 systemd 会说它 active
        ``unknown``      探针本身不可用，给不出判断
    """
    unavailable: list[str] = []
    out: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds"),
        "unit": unit,
        "port": port,
        "unavailable": unavailable,
    }

    # ---- ① systemd 单元状态 ----
    active_raw = _run(["systemctl", "is-active", unit])
    if active_raw is None:
        # is-active 对 inactive 单元返回非 0，但那**不是**探针失败
        # —— 它确实告诉我们"不 active"。用 show 区分「不 active」与「查不到」。
        show_raw = _run(["systemctl", "show", "-p", "ActiveState", "--value", unit])
        if show_raw is None:
            unavailable.append("systemctl")
            out["unit_active"] = None
        else:
            out["unit_active"] = show_raw == "active"
    else:
        out["unit_active"] = active_raw == "active"

    # ---- ② PID ----
    pid_raw = _run(["systemctl", "show", "-p", "MainPID", "--value", unit])
    pid: int | None = None
    if pid_raw is None:
        unavailable.append("systemctl:MainPID")
    else:
        try:
            pid = int(pid_raw)
        except ValueError:
            pid = None
        # MainPID=0 表示没有主进程
        if pid == 0:
            pid = None
    out["pid"] = pid

    # ---- ③ 进程细节（/proc）----
    out["process_running"] = False
    if pid is not None:
        proc = Path("/proc") / str(pid)
        if proc.is_dir():
            out["process_running"] = True
            status_raw = _read_text(proc / "status")
            if status_raw is None:
                unavailable.append("/proc/<pid>/status")
            else:
                out.update(_parse_status(status_raw))

            wchan = _read_text(proc / "wchan")
            if wchan is None:
                unavailable.append("/proc/<pid>/wchan")
                out["wchan"] = None
            else:
                out["wchan"] = wchan
                out["wchan_hint"] = _WCHAN_HINTS.get(wchan)
        else:
            # MainPID 有值但 /proc 里没有 → 进程刚退出或 PID 被复用
            unavailable.append("/proc/<pid>: 不存在（进程可能刚退出）")

    # ---- ④ 端口与 accept 队列 ----
    ss_raw = _run(["ss", "-tln"])
    if ss_raw is None:
        unavailable.append("ss")
        out["port_listening"] = None
        out["accept_queue"] = None
    else:
        queue = _parse_accept_queue(ss_raw, port)
        out["port_listening"] = queue is not None
        out["accept_queue"] = queue

    # ---- ⑤ 综合判定 ----
    #
    # ⚠ 判定顺序有意义：先看进程在不在，再看能不能响应。
    #   「进程不在」比「不响应」更确定 —— 前者不需要猜。
    if out["unit_active"] is None and not out["process_running"]:
        out["verdict"] = "unknown"
        out["note"] = "探针无法判断 —— 见 unavailable"
    elif not out["process_running"]:
        out["verdict"] = "dead"
        out["note"] = (
            f"coordinator 进程不存在（unit {'active' if out['unit_active'] else 'inactive'}）"
            f" —— 服务没在跑"
        )
    elif not ops_reachable:
        out["verdict"] = "unresponsive"
        # ⚠ 不要用 markdown 加粗：这条文案最终走 textContent，
        #   星号会原样显示出来（前端第一次预览时就看到了 `**进程活着...**`）。
        #   强调靠 CSS 类（前端按 verdict 上色），不靠文本标记。
        parts = ["进程活着但不响应 —— 这是最危险的情况：systemd 会认为它正常"]
        if out.get("wchan"):
            hint = out.get("wchan_hint") or "未识别的内核状态"
            # 不嵌套括号：hint 本身常含括号，套起来会难读
            parts.append(f"阻塞在 {out['wchan']} · {hint}")
        if (out.get("accept_queue") or 0) > 0:
            parts.append(f"accept 队列 {out['accept_queue']} 个连接无人处理")
        out["note"] = "；".join(parts)
    else:
        out["verdict"] = "healthy"
        out["note"] = "进程在、ops 端点可达"

    # ---- ⑥ 额外的怀疑信号 ----
    #
    # 即使 ops 通，也值得提示的风险。放在 warnings 里，
    # 不与 verdict 混在一起（verdict 答"能不能用"，warnings 答"稳不稳"）。
    warnings: list[str] = []
    wchan = out.get("wchan") or ""
    if "futex" in wchan:
        # 同 note：不嵌套括号
        warnings.append(
            f"阻塞在 {wchan} · {out.get('wchan_hint') or '疑似死锁'}"
        )
    queue = out.get("accept_queue")
    if isinstance(queue, int) and queue > 0:
        warnings.append(f"accept 队列有 {queue} 个连接待处理")
    if isinstance(out.get("threads"), int) and out["threads"] <= 1:
        warnings.append(
            f"只有 {out['threads']} 个线程 —— 若主线程被长任务卡住，"
            "整个网关将无人 accept"
        )
    out["warnings"] = warnings

    return out


__all__ = ["probe_coordinator", "PROBE_CMD_TIMEOUT"]
