# Nexus Mesh 监控面板 · 开发交接文档

> **状态：历史交接稿，已被 [`09-ui-hub-integration.md`](09-ui-hub-integration.md) 消化。**
>
> 本文写于 2026-11-13，是 hub 侧交给 nexus-mesh 侧的**作业说明**。
> 接到它之后的工作成果、方案决策与实施记录全部记在 `09` 里 ——
> **要动手改面板，读 `09`；本文只作背景与追溯。**
>
> ⚠ **本文有两处结论已被 `09` 修正**，别按旧说法做：
>
> | 本文说 | `09` 实测/收敛后 |
> |---|---|
> | 面板要「接入 hub」，需新增子域 | **接入链路早已完整存在**，hub 侧零改动（`09` §0） |
> | 「最需要时最没用」用 C 方案（落盘快照），需用户授权 | 改用 **内存末次成功**（B+D），不落盘、铁律冲突消失（`09` §二·阶段 1） |
>
> 其余结论（数据只走 ops 端点、只报组合异常、`rationale` 必须显示、
> 视觉规范、`whoami` 不加 `?node=`）经 `09` 逐项复验**全部属实**，仍然有效。
>
> **来源**：private-hub 项目（`HANDOFF-nexus-ui.md`），2026-11-17 迁入本仓库
> 以便 `09` 的引用可读、可追溯。

> **这份文档给谁**：负责开发 nexus-mesh web 监控面板的 Agent。
>
> **前置阅读**：本项目的核心设计意图与铁律，都写在
> `/opt/nexus-mesh/nxs/ui/server.py` 与 `page.py` 的 docstring 里。
> 那是**最好的规范来源** —— 本文档做转述与补充，但代码注释里的推理更完整。
>
> **阅读顺序**：本文档 → `server.py` 的模块 docstring（前 60 行）→ `HANDOFF.md`（hub 侧约定）

---

## 零、开始前先读：那次 13 天故障，和我的四处误诊

> **2026-11-17 更新**：故障已修复（代码修复 `f76d7f1`，已部署），
> coordinator 现在**健康**，可以直接开工。
>
> 本节保留下来讲那次故障的**真实根因**，以及我当时的四个错误判断。
> 后者比前者更有用 —— 因为它们正好是监控面板必须避免的四类误报。

### 真故障与真根因（2026-10-31 → 11-13，13 天）

现象：

```
systemctl:  nexus-mesh active（systemd 认为它活着）
18800:      LISTEN，但 Recv-Q=6 ← 6 个连接排队，无人 accept
HTTPS 探测:  HTTP 000，8 秒超时          ← 这是最硬的证据
进程:       CPU 0.0%，NRestarts: 0（13 天从未重启）
```

**根因（已在 `f76d7f1` 修复）**：把监听套接字整体 wrap 成 SSL。

```python
# 原来的写法（错的）
httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
```

看着很自然，但 `SSLSocket.accept()` **内部会同步做 TLS 握手**
（`do_handshake_on_connect` 默认 `True`），而 `accept()` 是从
`serve_forever` 的**单线程循环**里调用的，且继承来的 socket **没有超时**。于是：

```
一个 TCP 连接建立（三次握手完成）
  → 但不发 TLS ClientHello
  → accept() 内部 do_handshake() 永久阻塞
  → serve_forever 循环卡死
  → 后续连接全部堆在 accept 队列（实测 Recv-Q=6，backlog=5）
```

触发它的是一条**来自公网的半开连接**（源 IP `14.212.83.239`，中国电信，
从未通过认证、不扫其他端口）。但修复提交里有一句更重要的判断：

> 这是「默认行为脆弱」，不是「被攻击」。真正的问题是：为什么一个不需要
> 任何凭据、不需要任何协议的静默 TCP 连接，能让协调节点冻结 13 天？
> 换成客户端 bug、机房抖动、某个忘记关的 nc —— 结果完全一样。

**为什么 systemd 完全没发现**：主线程一直在正常跑 5 秒维护循环
（`ticks` 涨到 241834）。卡住的只有 gateway 的 accept 循环 ——
进程不退出、不吃 CPU、没有 traceback。

> 这是最坏的失败模式：**服务看起来健康，实际已经死了。**

### 修复要点（三条缺一不可）

| # | 做法 | 漏了会怎样 |
|---|---|---|
| 1 | 监听套接字**不 wrap**，保持裸 TCP | 半开连接继续冻结 accept 循环 |
| 2 | 逐连接握手（`get_request` 里），**设 10s 超时** | 半开连接永久占用一个工作线程 |
| 3 | 握手失败**只丢该连接** | 一个扫描器发错协议就能让服务永久死亡 |

第 3 点容易被忽略但同样关键：往 TLS 端口发明文 HTTP 的扫描器很常见，
如果那让 `serve_forever` 退出，比阻塞更糟。

### ⚠ 我当时的四处误诊

**误诊一：说「主线程死锁」。**

完全错了。主线程**全程正常**，卡住的是 gateway 的 accept 循环。
我当时只看到 `wchan=futex_wait_queue` 加上「日志不动」，就编出了一个
「主线程锁死」的故事。

**误诊二：把 `futex_wait_queue` 当成死锁证据。**

nexus-mesh 自己在 `32ee112` 里已经否定了这条路，原因：

```
tid=1080678  (main)  wchan=futex_wait_queue   ← 永久停在这里（正常）
tid=1080692          wchan=do_sys_poll        ← 真正在收连接（正常）
```

主线程在 `Event.wait()` 等退出信号 —— **futex 是这个架构的稳态**。
那个项目为此专门修过一版探针，因为单指标告警必然误报：

> 误报比不报更糟：它训练人忽略 warnings，等真死锁来时那句警告已经没人看了。

**误诊三：把「日志停止」当成卡死证据。**

`cmd_serve` 的维护循环：

```python
result = app.sweep()
if result["marked_stale"] or result["released"]:
    _info(f"[tick {ticks}] 标记 stale ...")   # ← 没动作就什么都不打印
```

没有 stale 要标记时，日志本来就是空的。**我把「安静」读成了「死了」**。
而事实正相反：那次故障里主线程的 tick 一直在涨，只是没动作不打日志。

**误诊四：断言「仓库里没有修复提交」。**

我在 `/opt/nexus-mesh` 里 grep 了 `deadlock|锁|死锁|futex|卡` 这些词，
没命中，就写下「修复是否已落到代码里尚未确认，bug 可能复发」。
**而修复提交 `f76d7f1` 当时就在 HEAD 历史里。**

漏掉的原因很蠢：那个提交的标题是「TLS 握手移入工作线程」，
不含我搜的任何一个关键词。**搜索关键词选错，不能推出「不存在」。**

### 正确的判据

单看任何一个信号都不够，**必须三条组合**（这来自 `32ee112`，也是它探针的实现）：

| 信号 | 看法 |
|---|---|
| 有 I/O 线程在等 | `cat /proc/<pid>/task/*/wchan`，出现 `do_sys_poll` / `do_epoll_wait` / `inet_csk_accept` → accept 循环活着 |
| accept 队列是否在涨 | `ss -tlnp \| grep 18800`，Recv-Q 持续堆积才可疑 |
| ops 端点是否可达 | `curl -sk --max-time 5 https://127.0.0.1:18800/nxs/health` |

**三条全中才是故障**（10-31 那次三条全中）。
只有 futex、其余正常 → **健康，不要告警**。

2026-11-17 的健康基线实测（可以拿来对照）：

```
tid=1192709 (main)  wchan=futex_wait_queue   ← 稳态
tid=1192712        wchan=do_sys_poll         ← accept 活着
Recv-Q=0 · health http=200 time=0.0045s
主线程 16 秒内自愿上下文切换 +3 次 ≈ 每 5 秒唤醒（--interval 默认 5）
```

最后那条是我最推荐的判据：**数主线程的上下文切换**。它不需要 py-spy、
不改动生产，却能直接证明维护循环在活着：

```bash
PID=$(systemctl show -p MainPID --value nexus-mesh)
grep voluntary_ctxt_switches /proc/$PID/task/$PID/status; sleep 16
grep voluntary_ctxt_switches /proc/$PID/task/$PID/status
# 16 秒增长约 3 次 = 每 5 秒正常唤醒
```

### 给面板设计的四条直接启示

1. **不要做 `futex` → 告警的单指标映射**（误诊二）
2. **区分「安静」与「死了」** —— 日志空、计数为 0 都不是故障证据（误诊三）
3. **不要把「搜不到」当成「不存在」** —— 查代码/查提交时，关键词选错就会
   得到错误结论。多试几组词，或直接看时间线（误诊四）
4. 真故障的判据是**组合信号**，而组合信号正是 nexus-mesh 的
   `anomalies` 已经在做的事（见第四章），别自己另发明一套

**这次故障最该教会面板的事**：它完美演示了「服务看起来健康、实际已死」
—— systemd 说 active、主线程在跑、CPU 0%。面板的价值就是在那时候
把 `queue=6` 和 `ops 不可达` 这两个信号摆到人眼前。


### 顺带一个设计启示

即便这次故障已修，**「协调节点静默失去响应」依然是这类系统的常态故障形态**。
而且它最刁钻的地方在于：端口照开、systemd 照报 active、进程照在。

你的面板有一半的价值，恰恰是在这种时候还能告诉人「它到底还活着吗」。
但前提是**判据得对** —— 上面两个错误示范就是反面教材：
用错判据的监控，会在健康时报红、在真故障时报绿，两种都让人不再信任它。

详见第五章「监控面板的根本矛盾」。

---

## 一、数据从哪来：只有三个入口

### 铁律：不读数据库

```
验证：grep -cE "sqlite3|from nxs.*store|import.*registry" nxs/ui/server.py
结果：0 次
```

**现有 UI 一个数据库都不读。** 只调三个运维端点：

| 端点 | 返回 | 说明 |
|---|---|---|
| `GET /nxs/ops/nodes` | 节点列表 | 需运维密钥 |
| `GET /nxs/ops/metrics` | 指标 + 节点统计 + 异常 | 需运维密钥，**核心数据源** |
| `GET /nxs/ops/audit` | 审计流 | 需运维密钥。`OPS_AUDIT_MAX_LIMIT=1000`、`OPS_AUDIT_DEFAULT_LIMIT=100` |

### 为什么不能直读 SQLite

这是项目铁律，理由写在 `server.py` 里：

> 不 import 存储后端、不 import `nxs.app`、不读 sqlite、不碰活的注册表。
> 为什么这条不能妥协：直读存储等于**新增一条绕过策略层的数据通道**。
> 本项目的铁律是"所有安全策略的唯一裁决处" —— 数据出口也必须唯一。

**具体后果**：如果你直读 `queue.db` 显示任务，你会看到
**本来不该被这个节点看到的数据** —— 因为策略层（谁能看谁）被绕过了。

> ⚠ 这条我实测确认过：`server.py` 里 sqlite 引用为 0。
> 它不是"建议"，是已经在执行的设计。

### `/nxs/ops/metrics` 的完整结构

这是我逐字段从 `nxs/__main__.py` 的 `_render_metrics()` 里读出来的，
**CLI 与运维端点共用同一份数据形状**：

```python
{
  "uptime_seconds": float,

  "totals": {
    "submitted": int, "completed": int, "failed": int, "denied": int
  },

  "latency_ms": {"p50": float, "p95": float, "p99": float},
  "latency_samples": int,          # < 2 时不应显示分位数

  "by_type": {
    "<task_type>": {"completed": int, "failed": int}
  },

  "denied_by_rule": {"<rule_id>": int},   # 拒绝分布，按次数降序

  "nodes": [{
    "node_id": str,
    "domain": str,
    "state": str,                  # alive / stale / ...
    "received": int,
    "completed": int,
    "failed": int,
    "pending": int,
    "in_flight": int,
    "backlog_ratio": float | None, # None 表示 ∞（除零）
    "bytes_out": int,
    "approved": bool
  }],

  "anomalies": [{
    "severity": str,               # critical / warn / info
    "rule": str,                   # 规则 ID
    "subject": str,                # 出问题的对象
    "message": str,
    "rationale": str,              # ★ 为什么"单看正常"——必须显示
    "suggestion": str              # ★ 建议动作
  }]
}
```

### `/nxs/ops/audit` 的字段

审计流是 NDJSON（`/var/lib/nexus-mesh/audit.ndjson`），单条字段：

```
ts, outcome, rule_id, level,
from_node, from_domain, to_node, to_domain,
task_id, task_type, session_id, trace_id,
attempt, bytes, duration_ms
```

`outcome` 的取值样本：`ops:approve`、`denied`、以及任务流转状态。

> **注意**：审计流**没有走运维通道的 CLI 命令**（`nxs ops` 的子命令只有
> `{approve,revoke,release,nodes}`）。要拿审计数据只有两条路：HTTP 端点，或直接读文件。
>
> **有个陷阱**：`nxs status` 看起来像，但它**没有 `--url` 参数**——
> 它的实现是 `_load_app()` + `app.start()`，**在自己进程里直接读本地 store**，
> 只打印一个摘要（总记录 / 完成 / 拒绝 / `denied_by_rule`）。
> 拿它调试远端 coordinator 会得到「看起来正常但数据是另一份进程的」的错觉，
> 而且它绕过了「数据只经运维端点」这条铁律。
> **面板后端不要调 `nxs status`。**
>
> ⚠ **认证不是 Bearer token，是 HTTP Signature。** 别想着用
> `curl -H "Authorization: Bearer ..."` 调运维端点 —— 会被拒（`AU-SIG-01`）。
>
> 实际机制（`nxs/edge/httpsig.py`）要求四个头：
>
> ```
> X-NXS-Node   节点 ID（运维身份是 __ops__）
> X-NXS-Ts     时间戳
> X-NXS-Nonce  随机串（防重放）
> X-NXS-Sig    HMAC 签名
> ```
>
> **签名要用运维密钥对 `method + path + node_id + ts + nonce` 做 HMAC 计算。**
> 所以调试运维端点**请用 `nxs` CLI**，不要手写 curl：
>
> ```bash
> .venv/bin/nxs metrics --url https://127.0.0.1:18800/nxs --json
> ```
>
> 你的面板后端同理 —— 要么复用 `nxs` 的客户端代码，要么自己实现
> `httpsig.compute_signature()`。**这是本次交接最容易踩的坑。**

规模参考：**27,809 行 / 7.9 MB**（截至 2026-11-13）。
**别一次拉全量** —— 端点有单次上限，且页面上也用不到。

### 一个刻意的设计：`whoami` 不需要运维密钥

```
GET /nxs/whoami    ← 只回自己的身份，用自己的凭据
```

代码注释解释得很清楚：

> 为什么不复用 `/nxs/ops/nodes`：那需要**运维密钥**。
> Agent 不该有运维密钥 —— 那等于把"批准/吊销节点"的钥匙交给被管理者。

**并且刻意不支持 `?node=x`**：一旦存在就会变成轻量的节点发现接口，
而节点发现该有它自己的权衡。要列全网节点应该新增 `/nxs/peers`，不是放开这个口子。

**这条对你的启示**：不要为了"方便"给面板加「查任意节点详情」的功能，
那会绕过既有的权限设计。

---

## 二、四条铁律（现有 UI 已执行，你要继承）

现有 UI（`nxs/ui/`，server 423 行 + page 719 行）的四条设计原则。
**这不是建议，是已经落地的约束，改之前先想清楚为什么它存在。**

### ① 不做写操作

面板只读。要改状态（批准/吊销/重试）走 CLI —— 因为
**shell 历史本身就是操作记录**。

### ② 数据只经运维端点

见第一章。不读 sqlite，不 import 存储层。

### ③ 只听 `127.0.0.1`

不接受 `0.0.0.0`。访问靠 **SSH 隧道**把端口搬过来。

理由：不需要开防火墙端口、不需要证书 —— **传输加密由 SSH 提供**。

> ⚠ 现状与此有出入：现在 `hub` 通过 Caddy 把 8080 反代到
> `nexus.i-strategy-compass.cn`，前面有 Authelia 两因素认证。
> 这算**另一种合法的收口方式**（认证由 Authelia 提供、加密由 Caddy TLS 提供）。
> 但服务本身仍必须只听 `127.0.0.1` —— 这点不变。

### ④ token 不落盘

每次启动重新生成，只打印到终端（journald）。
写进 `HttpOnly` cookie 后用于后续请求。

**不做账号密码**。理由（原文）：

> 能 SSH 登录这台机器的人本来就能 `export NXS_OPS_SECRET` 直接 curl，
> 给他一个密码框只是多一道他不需要的门。

### 补充：`X-Frame-Options: DENY`

现有 UI 明确拒绝被 iframe 嵌入。**不要去掉它** —— 且 hub 的
`/api/nexus-enter` 就是靠「整页 303 跳转」而非内嵌来衔接的。

---

## 三、三块数据的失败必须互不牵连

这是现有 UI 的核心可靠性设计，原文照录：

> 观察窗最需要工作的时候，恰恰是 coordinator 出问题的时候。
> 所以原则是：**能拿到多少就显示多少。**

| 情况 | 行为 |
|---|---|
| `nodes` 拿不到 | 整体返回（没有节点就没得看），`error` 记下 |
| `metrics` 拿不到 | 节点照常显示，只标指标的错 |
| `audit` 拿不到 | 节点与指标照常显示，只标审计的错 |

**为什么 `nodes` 失败就整体返回**：它是页面主体，
没有节点列表时「有个指标数字」没有意义。另外两块是补充信息，
缺一块不该让整页变空白。

**面板的 snapshot 顶层结构**（可直接复用）：

```python
{
  "at": iso8601,
  "coordinator_url": str,
  "nodes": [],
  "metrics": None,
  "audit": [],
  "audit_truncated": bool,
  "audit_heartbeats": int,
  "error": None,
}
```

---

## 四、异常展示：只报「组合异常」

### 这条最重要，别推翻它

```python
# 原文
"只报「组合异常」——单指标超阈值不在此列，那种告警会响到没人看"
```

**不要做「CPU > 80% 就标红」这类阈值告警。** 设计者明确否定了这条路：
单指标超阈值会产生大量噪音，最后没人看。

**只显示后端给的 `anomalies` 数组**，那是后端已经做过关联分析的「组合异常」。

### `rationale` 必须显示

```python
# 原文
# rationale 是这类规则存在的资格证明，必须显示出来——
# 否则运维会问"这凭什么算异常"
```

**每条异常必须展示 `rationale` 和 `suggestion`**，不能只显示 `message`。

这是这个面板和普通监控面板的关键区别：它不只告诉你「有问题」，
还告诉你「为什么这算问题」。**丢掉 `rationale` 就等于丢掉了这条规则的可信度。**

---

## 五、监控面板的根本矛盾（需要决策）

这是我审出来的一个**结构性风险**，不是缺陷，但必须让用户知道：

```
监控面板的数据源 = coordinator 的 ops 端点
       ↓
coordinator 失去响应 → ops 端点不响应 → 面板什么都拿不到
       ↓
而「coordinator 失去响应」正是面板最该发挥作用的时候
```

**这不是假想**：2026-10-31 到 11-13，coordinator 真的失去响应 13 天
（`ops` 端点 HTTP 000 超时）。那 13 天里，任何依赖 ops 端点的面板都只会
显示「连不上」—— 恰好在最需要它的时候最没用。

根因已修（`f76d7f1`：TLS 握手移入工作线程，见第零章）。但这个结构性矛盾
**不随具体 bug 消失**：只要面板的唯一数据源是 ops 端点，那个端点失效时
监控就一起失效。任何未来的 bug、任何一次 OOM、任何一次磁盘满，都会重现
这个局面 —— 单一数据源失效时，监控也就一起失效。

### ⚠ 一个比「拿不到数据」更严重的事实

那次故障把 `observability.md` 里那条 `silent_stall`（静默卡死）规则的
**根本局限**暴露出来了：

> **那条规则跑在 `metrics` 端点里，而端点本身被冻住了。**

也就是说，**nexus-mesh 所有应用层自检在那种故障下全部失效** ——
不是规则写得不好，是它们物理上无法执行：

```
accept 循环被冻住
  → /nxs/ops/metrics 不可达
    → silent_stall 等五条规则一条都跑不了
    → 面板读不到数据
    → 人只能看到「连不上」
```

**唯一还能工作的，是从进程外部观察的东西**：

| 观察方式 | 为什么还能用 |
|---|---|
| `Recv-Q`（`ss -tlnp`） | 内核维护，不经过进程 |
| HTTP 可达性（curl） | 结论来自「连不上」这个事实本身 |
| `/proc/<pid>/task/*/wchan` | 内核维护 |
| 主线程上下文切换计数 | 内核维护 |

这就解释了为什么**方案 B（本地探针）不是锦上添花，而是必需**：
它是那次故障中唯一还能给出信息的一层。nexus-mesh 自己在 `871952d`
（阶段 1：本地探针 + 内存末次成功）走的也是这个思路。

> **判据的分层**（写面板时按这个分层来想）：
>
> | 层 | 数据来源 | 端点挂掉时 |
> |---|---|---|
> | 业务层 | `ops/metrics`、`anomalies` | ❌ 全失效 |
> | 传输层 | 端口、Recv-Q、wchan、ctxt switches | ✅ 仍可用 |
>
> 只有传输层那行在故障时还活着 —— 所以它不能省。

### 三种解法（各有代价，需用户选）

| 方案 | 做法 | 代价 |
|---|---|---|
| **A. 接受现状** | 只显示「coordinator 不可达 + 最后成功时间」 | 简单，但最需要时没有信息 |
| **B. 补充本地探针** | 后端加一个「只看进程存活/端口/线程状态」的轻量检查，不依赖 ops 端点 | 能回答「它卡了没」，但看不到业务数据 |
| **C. 降级直读快照** | 定期把 ops 数据落一份快照文件，coordinator 挂时显示「最后已知状态」 | 违反铁律②（但是只读副本，且明确标注「陈旧」）|

**我的建议是 B + C 组合**：

- B 解决「它现在还活着吗」
- C 解决「它挂之前是什么样」（最后快照 + 明确的时间戳标注）

**但 C 需要用户明确同意**，因为它触及了「数据出口唯一」的铁律。
如果用户选 A，就老老实实只显示「不可达」——**不要偷偷绕过**。

> ⚠ **做 B 的时候，判据必须用组合信号。**
>
> 具体怎么判，nexus-mesh 已经踩过坑并写进了 `32ee112`：单看
> `wchan=futex` 会误报（健康进程被报疑似死锁）；单看日志安静也会误报
> （没动作本来就不打印）。要三条一起看 —— 有无 I/O 线程、队列是否在涨、
> ops 是否可达。详见第零章。
>
> 一个便宜且可靠的补充判据：**数主线程的自愿上下文切换**。每 5 秒唤醒
> 一次的话，16 秒内应增长约 3 次。这个指标不需要 py-spy、不影响生产。

---

## 六、视觉规范

### 现有 UI 的基线：**配色合格，可直接沿用**

现有 UI 有**两套完整 token**（这点很重要，见下方警告）：

```css
/* 亮色（默认） */
--bg:#fbfbfc  --fg:#1c1d21  --dim:#6b7280  --line:#e5e7eb
--card:#ffffff --ok:#15803d --warn:#b45309 --bad:#b91c1c --accent:#1d4ed8

/* 暗色（prefers-color-scheme: dark） */
--bg:#14161a  --fg:#e6e7ea  --dim:#9aa1ab  --line:#2a2e35
--card:#1b1e23 --ok:#4ade80 --warn:#fbbf24 --bad:#f87171 --accent:#7aa2f7
```

**逐项验算结果（在 bg 与 card 两种背景上取最差值）**：

| 变量 | 亮色最差 | 暗色最差 | 判定 |
|---|---|---|---|
| `--fg` | 16.28:1 | 13.52:1 | PASS |
| `--ok` | 4.85:1 | 9.59:1 | PASS |
| `--warn` | 4.86:1 | 10.01:1 | PASS |
| `--bad` | 6.26:1 | 6.04:1 | PASS |
| `--accent` | 6.48:1 | 6.64:1 | PASS |
| `--dim` | 4.67:1 | 6.41:1 | PASS |

**全部 ≥4.5:1，配色这块不用重做。**

> ⚠ **一个真实的踩坑警告（我自己在这上面栽过）**
>
> 这两个 `:root` 块**同名变量、不同值**。如果你用
> `grep -oE "\-\-[a-z]+:\s*#[0-9a-fA-F]{3,6}"` 去抓 token，
> 会**把亮色的 `--bg` 和暗色的 `--ok` 混在一起**，
> 算出 `#4ade80` on `#fbfbfc` = 1.68:1 这种荒唐结论。
>
> 抓 CSS 变量必须**限定在各自的 `:root` 块内**（如 `sed -n "/:root/,/}/p"` 逐块处理），
> 否则跨块混算。

### 一个真实存在的问题：字阶噪音

```
字号使用分布：
  11px    2 处   （.tag）
  11.5px  3 处   （.tile .k、th）
  12px    5 处   （.tile .u、footer 等）
  12.5px  9 处   （.meta、.desc、.anomaly 等，用得最多）
  13px    2 处   （h2）
  17 / 18 / 21px 各 1 处

相邻步进：
  11 → 11.5   1.045×   过密
  11.5 → 12   1.043×   过密
  12 → 12.5   1.042×   过密
  12.5 → 13   1.040×   过密
```

**五档小字挤在 1.04 倍内，肉眼无法区分。** 建议收敛为 3 档（例如 12 / 13 / 18），
把这五档合并。

注意：这些字号都用于**次要信息**（meta、desc、tag、th、footer），
所以危害是「视觉噪音」而非「读不清」。优先级不必很高，但既然要重写，
顺手收敛比留着好。

### 可直接复用的规范（hub 侧已验证）

这是 hub 页面重做时逐项验算过的，监控面板同样是 Operate 模式，规则通用：

```
文字对比度：正文/小字 ≥4.5:1，大字(≥18.66px bold 或 ≥24px) ≥3:1
           必须在「页面底色 / 卡片 / 次级底」三种背景上都达标
字阶：相邻档步进 ≥1.125；产品 UI 用固定 rem 不做 clamp
阴影：必须有偏移 + 软模糊；零偏移彩色光晕是装饰
阴影与描边只取其一（否则是「幽灵卡片」）
卡片圆角 12–16px（现有 UI 用的 12px，可沿用）

浏览器原生表面必须主题化：
  ::selection / caret-color / scrollbar / focus-visible /
  underline-offset / 表格数字用 tabular-nums
  ← 这是「被做过」而非「拼装的」最便宜的信号

动效：150–250ms，只表达状态变化，不做页面级入场编排
状态齐备：default/hover/focus/active/disabled/loading/error
```

### 禁止事项

| 禁止 | 原因 |
|---|---|
| 图标+标题+文字的同尺寸卡片网格当页面骨架 | 监控面板尤其忌讳：每个指标都变成一张等大卡片，看不出轻重 |
| 标题上方的 kicker / eyebrow | 无例外 |
| 渐变文字、装饰性玻璃模糊 | — |
| `>1px` 的彩色左边框 | 状态用颜色时用徽标或圆点，不用侧条 |
| 硬偏移阴影（`box-shadow: 4px 4px 0`） | 除非整个视觉语言就是 neobrutalist |
| emoji 当图标 | 用真实 SVG 图标，统一描边粗细 |
| 只报单指标超阈值 | 见第四章 |
| 丢掉 `rationale` | 见第四章 |

### 数据可视化的额外要求

监控面板有图表，这几条要额外注意：

- **涨跌/好坏配色**：这是技术指标，不是股市。用 `--ok/--warn/--bad`，
  不要套用红涨绿跌（那是投资页面的事）
- **分位数样本不足时不要画**：`latency_samples < 2` 时明确写「样本不足」，不要画一条假的曲线
- **`backlog_ratio: None` 要显示为 `∞`**，不要显示 0 或空白
- **`approved: false` 的节点要有明显标记**（现有 UI 用 `[未批准]` 后缀）

---

## 七、部署与访问链路

### 现有链路（新面板要么复用、要么替换，不能并存）

```
hub 页面点 Nexus 卡片
  → GET hub/api/nexus-enter          （hub 后端 127.0.0.1:8002）
  → 从 journald 抓 8080 的 token
  → 303 跳 https://nexus.i-strategy-compass.cn/?t=<token>
  → Caddy（443，forward_auth 已过 Authelia 两因素）
  → 反代 127.0.0.1:8080
  → 页面把 ?t= 换成 HttpOnly cookie，地址栏跳回 /
```

**端口占用**：`8080` 已被现有 UI 占用。新面板二选一：

| 方案 | 做法 | 影响 |
|---|---|---|
| **替换** | 新面板起在 8080，停掉旧的 `nexus-mesh-ui.service` | hub 侧无需改（`/api/nexus-enter` 照常工作） |
| **并行** | 新面板起在 8090 等端口，新增子域 + `sites.json` 条目 | 需要改 hub 的 Caddy 与 access_control |

**推荐「替换」**：hub 侧的衔接逻辑（token 从 journald 抓）已经工作，
换掉 8080 后面的实现，上游一行都不用改。

### 系统约束

| 项 | 值 |
|---|---|
| 内存 | 8G 总，可用约 2.1G。**Hermes 的 node 进程占 2.2G，是最大变量** |
| 服务预算 | 现有 hub 三组件共 89MB。新面板建议控制在 **100MB 内** |
| Python | 3.11.2，**无 PyYAML**（用 JSON，或自行装） |
| Node | v26.8.1 可用 |
| OS | Debian 12 |
| 权限 | 现有 UI 以 `nxs` 用户运行，`ProtectSystem=strict` 等加固已开 |

### 运维密钥

新面板需要读 `NXS_OPS_SECRET`（在 `/etc/nexus-mesh/nxs.env`，`0600 root`）。

**持有方式**：由 systemd `EnvironmentFile` 注入到面板进程，
**前端永不接触**。这个方向是对的。

```ini
# systemd unit 参考
EnvironmentFile=/etc/nexus-mesh/nxs.env
Environment=SSL_CERT_FILE=/etc/nexus-mesh/tls/ca.pem
```

注意 `SSL_CERT_FILE` —— coordinator 用**自签 CA**，不指定这个环境变量
Python 会拒绝连接。（CLI 走的是另一套：`--ca-file` 或 `NXS_CA_FILE`。）

#### ⚠ 别照抄现有的那一行：它给多了

实测现有 `nexus-mesh-ui` 进程的环境（`/proc/<pid>/environ`）：

```
NXS_OPS_SECRET           ← 它唯一需要的
NXS_SECRET_COORDINATOR   ← 不需要
NXS_SECRET_WORK_DESKTOP  ← 不需要
NXS_SECRET_WORK_MACBOOK  ← 不需要
NXS_SECRET_HERMES_WORKING← 不需要
NXS_SECRET_TASKFLOW      ← 不需要
```

**一个只读观察窗，手里握着网络里全部 5 个节点的身份密钥。**
`nxs.env` 是给常驻进程用的全量凭据文件，UI 直接 `EnvironmentFile` 它，
等于把整个网络的密钥一股脑授给了一个展示页面。

后果不是理论上的：UI 一旦出 RCE 或路径穿越，攻击者拿到的**不是只读运维通道，
而是可以直接冒充 coordinator / hermes / taskflow 发任务的完整身份**。
这与第四章「不做写操作」的努力完全不在一个量级上——
**前者防的是误操作，这一条防的是提权。**

**新面板请单独建一个最小化凭据文件**：

```bash
# /etc/nexus-mesh/ui.env —— 只放这个面板真正要用的
NXS_OPS_SECRET=...
```

```bash
chmod 600 /etc/nexus-mesh/ui.env && chown root:root /etc/nexus-mesh/ui.env
```

```ini
# 然后 unit 里指这个，而不是 nxs.env
EnvironmentFile=/etc/nexus-mesh/ui.env
```

存量服务改不改由用户决定（属于 nexus-mesh 项目的事），
但**新代码没有理由继承这个缺陷**。

---

## 八、交付标准

### 功能

- [ ] 显示节点列表：ID、域、状态、收/完/败/待办/执行中、水位、字节
- [ ] 显示 totals 与延迟分位数（样本不足时不画）
- [ ] 显示 `by_type` 与 `denied_by_rule` 分布
- [ ] 显示 `anomalies`，**含 `rationale` 与 `suggestion`**
- [ ] 三块数据独立失败，互不牵连
- [ ] 显示「最后成功更新时间」，让人知道数据新不新

### 安全

- [ ] 服务只听 `127.0.0.1`
- [ ] 不读 sqlite / 不 import 存储层（用审计脚本或人工 review 确认）
- [ ] token 不落盘、`HttpOnly` cookie
- [ ] 保留 `X-Frame-Options: DENY`
- [ ] 前端代码里没有 `NXS_OPS_SECRET`
- [ ] **进程环境里只有 `NXS_OPS_SECRET`，没有 `NXS_SECRET_*`**（用 `/proc/<pid>/environ` 核对）

### 质量

- [ ] 文字对比度 ≥4.5:1（浅/深两模式 × 三种背景都验）
- [ ] 字阶相邻档步进 ≥1.125
- [ ] `prefers-reduced-motion` 下无动画
- [ ] 键盘可操作，`focus-visible` 可见
- [ ] 390px 宽不横向溢出（**表格多，这条最容易挂**）
- [ ] 加载中有骨架或明确提示，空数据不说「暂无数据」而说清为什么

### 运维

- [ ] systemd unit 含 `Restart=always` + `StartLimitBurst`
- [ ] `systemctl is-enabled` 为 enabled（开机自启）
- [ ] 复用现有 hub 的部署管线（`deploy.sh`）或说明为何不适用

---

## 九、排查速查

```bash
# coordinator 是否活着（不要只看 systemctl，它是假绿灯的来源）
curl -sk --max-time 5 https://127.0.0.1:18800/nxs/health
#   返回 {"status": "ok"} 才是真活着；HTTP 000 超时才是真故障

# 运维通道是否可用（注意：metrics 是「顶级命令」，不在 ops 下）
cd /opt/nexus-mesh
set -a && . /etc/nexus-mesh/nxs.env && set +a
.venv/bin/nxs metrics --url https://127.0.0.1:18800/nxs --ca-file /etc/nexus-mesh/tls/ca.pem

# 加 --json 拿结构化输出，开发和调试时最有用
.venv/bin/nxs metrics --url https://127.0.0.1:18800/nxs --json \
  --ca-file /etc/nexus-mesh/tls/ca.pem | head -40

# 节点是否注册（这个才在 ops 下）
.venv/bin/nxs ops nodes --url https://127.0.0.1:18800/nxs --ca-file /etc/nexus-mesh/tls/ca.pem

# ── 判断是否真死锁：三条组合，缺一不可 ────────────────────────
PID=$(systemctl show -p MainPID --value nexus-mesh)

# ① 有没有 I/O 线程？有 → accept 循环还活着，主线程 futex 是稳态
for t in /proc/$PID/task/*; do
    printf "  tid=%s wchan=%s\n" "$(basename $t)" "$(cat $t/wchan 2>/dev/null)"
done
#   ⚠ futex_wait_queue 单独出现**不是**死锁证据（见第零章的错误一）
#   出现 do_sys_poll / do_epoll_wait / inet_csk_accept = 收连接正常

# ② accept 队列是否在涨？持续堆积才可疑
ss -tlnp | grep 18800

# ③ 主线程在唤醒吗？16 秒内应增长约 3 次（--interval 默认 5 秒）
grep voluntary_ctxt_switches /proc/$PID/task/$PID/status; sleep 16
grep voluntary_ctxt_switches /proc/$PID/task/$PID/status

# ── 别用的判据 ──────────────────────────────────────────────
# ⚠ 日志安静 ≠ 卡死。维护循环只在「有 stale 要标记 / 有租约要释放」时
#   打印，没动作时本来就是空的（见第零章的错误二）。
# journalctl -u nexus-mesh -n 3 --no-pager     ← 只能当参考，不能当判据

# 现有 UI 的入口 token（每次启动重新生成）
journalctl -u nexus-mesh-ui -n 50 --no-pager | grep -oE "t=[A-Za-z0-9_-]{20,}" | tail -1
```

---

## 十、与 hub 项目的边界

**这个面板不是 hub 的一部分。** 边界划分（来自 `HANDOFF.md` 第八章）：

| 项 | 归属 |
|---|---|
| nexus-mesh 的代码与 UI | **nexus-mesh 项目**（`/opt/nexus-mesh/`） |
| `nexus.i-strategy-compass.cn` 的 Caddy 反代与认证 | hub 项目 |
| hub 主页上的 Nexus 卡片 | hub 项目（`config/services.json`） |
| 本面板的实现 | **nexus-mesh 项目**（改 `nxs/ui/` 或新增目录） |

**判断标准**：要改的文件不在 `~/private-hub` 的 git 仓库里 → 不属于 hub 项目。

**这意味着**：

- 不要在 hub 仓库里加面板的代码
- 面板需要改 `sites.json` 或 `Caddyfile.tmpl` 时，**通知 hub 侧**，不要自己改
- 反过来，hub 侧也不会来改 `nxs/ui/`

---

## 附：需要用户决策的三件事

| # | 问题 | 选项 |
|---|---|---|
| 1 | 面板如何应对 coordinator 失去响应 | A 只显示不可达 / B 加本地探针 / C 降级读最后快照（建议 B+C） |
| 2 | 替换现有 8080 UI 还是并行新端口 | 替换（hub 侧零改动）/ 并行（需改 hub 配置） |
| 3 | 是否复用 hub 的设计 token | 复用（视觉统一）/ 独立（各项目管理自己） |

这三项定了才好动手。**尤其第 1 项**——它决定了面板的基本架构，
改起来不是小工程。

> **第 1 项如果选 B，有一条硬要求**：本地探针的判据必须是**组合信号**，
> 不能用单指标。这不是理论洁癖 —— nexus-mesh 自己就因为
> `if "futex" in wchan: warn("疑似死锁")` 这条单指标规则误报过健康进程，
> 后来在 `32ee112` 里修掉了。误报的代价是「人不再看告警」，
> 那比不告警更糟。详见第零章。

---

## 附二：这次交接我自己犯的错，供后来者引以为戒

写这份文档前后我下过多个错误结论，事后核实**全部推翻**。列在这里不是自谦，
是因为**这些错误的形态会重复出现在监控面板的开发里**：

| 我的错误结论 | 事实 | 教训 |
|---|---|---|
| 「主线程死锁」 | 主线程全程正常（ticks 涨到 241834）；坏的是 gateway 的 accept 循环卡在 TLS 握手 | 先定位到**哪个线程**，别笼统说「进程卡了」 |
| `wchan=futex` → 八成死锁 | 主线程在 `Event.wait()`，futex 是稳态 | 单指标不能判定状态，要看线程全景 |
| 日志停止 → 卡死 | 维护循环「没动作就不打印」，安静是正常态 | 区分「安静」与「死亡」 |
| grep 没命中 → 「仓库里没有修复提交，bug 可能复发」 | 修复 `f76d7f1` 当时就在 HEAD 里，标题是「TLS 握手移入工作线程」，不含我搜的任何关键词 | **搜索关键词选错，不能推出「不存在」** |
| 按变量名 grep CSS token 算对比度 | 亮/暗两套 `:root` 同名不同值，混算得出 1.68:1 | 抓 token 必须限定在各自块内 |
| `nxs status --url` 能查远端 | 它没有 `--url`，是本地直读 store | 别假设 CLI 参数存在，先看 `--help` |
| 运维端点用 `Authorization: Bearer` | 是 HTTP Signature（`X-NXS-Node/Ts/Nonce/Sig` + HMAC） | 认证机制先读源码 |

前四条是同一类：**「看起来像」就下结论，没去验证**。
后三条是另一类：**没有实际测过，就照常识写了**。

两类错误的代价不同，但结果一样 —— 都会让读文档的人建立错误的前提。
尤其第四条（断言修复不存在）最危险：它会让人以为「这 bug 还会复发」，
从而做出错误的运维决策。

这个项目的设计者在 `32ee112` 里写了同一句话 ——

> 误报比不报更糟：它训练人忽略 warnings，等真死锁来时那句警告已经没人看了。

**做监控面板，第一件事是把自己的判据验证一遍。**
**写交接文档，第一件事是把自己的结论验证一遍。**
