# Agent 桥接：把一个 LLM Agent 接进 Nexus Mesh

> 实现在 `examples/agent_bridge_common.py`（共用安全逻辑）
> + `examples/hermes_node.py` / `examples/openclaw_node.py`（各自怎么调）
> 首次部署：2026-10-27，云服务器 `43.138.222.206`

---

## 一、为什么需要"桥接"

Hermes 和 OpenClaw 都**不认识 mesh 协议**。所以不改造它们，
而是在旁边跑一个**独立的 Nexus 节点进程**做翻译：

```
协调节点 ──task──> 桥接 ──prompt──> Agent CLI ──text──> 桥接 ──artifact──> 协调节点
```

桥接进程是标准的 mesh 节点：有角色卡、有心跳、受策略约束、受配额限制。
Agent 本身对 mesh 一无所知 —— 它只收到一个 prompt。

---

## 二、两个桥接的差异

| | Hermes | OpenClaw |
|---|---|---|
| 调用 | `hermes -z <prompt>` | `openclaw agent --agent X --json -m <prompt>` |
| 输出 | 纯文本（stdout） | JSON 信封 |
| 取回答 | 整个 stdout | `result.meta.terminalReply.text` |
| 兜底 | — | `result.payloads[0].text` |
| **工具收窄** | ✅ 有（`-t safe`） | ❌ **无**（按 agent 配置，见第四节） |
| 实测延迟 | 9–10 秒 | 5–7 秒 |
| profile/agent | `working` | `taskflow` |

**共同点**（都在 `agent_bridge_common.py`）：校验、截断、prompt 措辞、
失败语义。

> ⚠ 为什么必须共用：这个项目刚因为"同一件事两份实现"出过 bug
> （`nxs verify` 自己另写了一遍错的域推导，而库函数是对的，
> 480 个测试没抓到）。**安全逻辑尤其不能有两份** —— 迟早会不一致，
> 而不一致的那天，就是"这个节点受保护、那个没有"。

---

## 三、共同的安全原则

### ① 失败必须显式失败

非零退出 / 超时 / 空输出 / 找不到回答字段 → 抛异常让任务落 `failed`。

**绝不返回空字符串充数**。空产出比失败更难查：调用方会以为
"它答了，只是没内容"，于是继续往下走。

### ② 产出只走自由文本通道

`answer` 声明为 `freetext`，节点侧边界会剥控制字符、剥指令性前缀、
按 4096 字节截断。服务端还会再校验一次 — **双层**，因为威胁模型是
"协调节点可能被攻破"。

自己在节点侧先截断（额外好处）：可以加省略号、可以记日志；
被边界截是**静默的**。

### ③ 先探活 Agent，再注册节点

顺序刻意。反过来的话 Agent 有问题时会先注册成功，然后每条任务都失败 —
**而失败原因藏在节点日志里，运维在协调节点上完全看不见**。

### ④ 不给 `issuer` 权限

桥接只做 `executor`。给 `issuer` 意味着它能把任务参数当外发通道 —
而参数是从别的节点来的，那是一条不该存在的路。

### ⑤ `can_receive_from` 写**域名**，不是节点 ID

这个参数在 `policy.py` 里匹配的是 `src.domain`。
写节点 ID 会永远匹配不上，而**症状是"任务被拒"而不是配置报错**。
（配置注释也曾把域写成节点，已修。）

---

## 四、⚠ OpenClaw 特有的风险（与 Hermes 不同）

`openclaw sandbox explain --agent taskflow` 的实测结果：

```
mode: off            ← 不沙箱，直接跑在主机上
allow: exec, process, read, ls, write, edit, apply_patch, view_image, ...
```

**taskflow 默认有 `exec`（执行任意命令）和 `write`/`edit`（写任意文件）。**
而桥接进程必须 root 运行（OpenClaw 装在 `/usr/local/lib/node_modules`，
state 在 `/root/.openclaw`）。

于是：

> **任何能向 `taskflow` 节点发任务的节点，都能在这台服务器上以 root
> 执行任意命令。**

这不是推测，是上面那行 allow 列表的直接推论。

对比 Hermes：它的工具集被收窄到 `safe`（`web` + `vision` + `image_gen`，
无终端、无文件）。**但 OpenClaw 的限制是按 agent 配置的**
（`agents.entries.<id>.tools`），改了会同时影响该 agent 在 QQ bot
等所有渠道的行为 —— 不能照抄 Hermes 的做法。

### 当前处置（2026-10-27，用户决定）

**先保持 taskflow 工具面原样**，用于快速验证 Agent 间交互。
风险由下面几层控制：

| 层 | 控制 |
|---|---|
| 谁有发布权 | 只有 `coordinator` 有 `issuer`；其余节点发布需运维批准其角色卡 |
| 矩阵 | `zone-work → cloud` 只放行 `work.*`，**不含 `agent.ask`** — 外部域派不进来 |
| 日配额 | `tasks_per_day: 100`，失控循环烧不了无限次 |
| 并发 | `concurrent_tasks: 1`，同一 agent 不会并行自我干扰 |
| 审计 | 每次投递记元数据，可事后追溯 |
| **工具面** | **未限制** ← 唯一的缺口 |

> 决策依据：先把协议与交互逻辑验通，工具收窄放到确定用法之后。
> 这是一个**有意识的取舍**，不是遗漏。

### 随时复核工具面

```bash
cd /opt/nexus-mesh && set -a && . /etc/nexus-mesh/nxs.env && set +a
.venv/bin/python examples/openclaw_node.py --audit-tools
```

它会打印 OpenClaw 算出的**有效**工具面（合并了 agent / global / default），
并在 allow 含危险工具时直接告警。

### 要收窄时怎么做

```jsonc
// /root/.openclaw/openclaw.json
"agents": { "entries": { "taskflow": { "tools": { "sandbox": { "tools": {
  "deny": ["exec", "process"]        // 只禁执行类，保留 read/write/edit
}}}}}}
```

然后：

```bash
openclaw sandbox explain --agent taskflow   # 复核
systemctl restart <openclaw 的 gateway>      # 配置在进程启动时读入
cd /opt/nexus-mesh && .venv/bin/python examples/openclaw_node.py --audit-tools
```

> ⚠ 这是 **agent 级**改动 —— 会同时影响 taskflow 在 QQ bot 等渠道。
> 如果它在那些渠道里靠 exec 干活，会坏。
> 这也是当初没直接收窄的原因。

---

## 五、实测：三跳协作

2026-10-27 实跑，验证"多个 Agent 通过 mesh 协作"确实成立：

```
第 1 跳  coordinator → hermes-working : 问同一个问题
第 2 跳  coordinator → taskflow       : 问同一个问题
第 3 跳  coordinator → taskflow       : 把 A/B 两份回答交给它，要求指出共识与差异
```

结果（4 个任务，0 失败 0 拒绝，p50 6.3 秒）：

两个 Agent 独立回答观点一致但措辞不同，taskflow 在第 3 跳正确识别：

> 共识：两者都把"分布式系统复杂度/治理成本"认定为最大隐性成本，且都列举了
> 服务通信、数据一致性、分布式事务、链路追踪、故障隔离、部署运维、
> 跨服务调试等相同要素。
>
> 差异：几乎无实质分歧；Agent A 强调"事务与排错从进程内调用变为跨服务网络调用"
> 的机制、以及"持续隐蔽吞噬团队效率"；Agent B 强调"团队未充分预估"与
> "远超拆分前"。仅是措辞与侧重不同，结论一致。

每个产出都带 `semantics=data` 与 `provenance.local_boundary=passed` —
说明节点侧边界真的跑过，不是摆设。

---

## 六、部署（云服务器）

四个 systemd 服务：

| 服务 | 用户 | 说明 |
|---|---|---|
| `nexus-mesh` | `nxs` | 协调节点 |
| `nexus-mesh-ui` | `nxs` | 只读观察窗（127.0.0.1:8080） |
| `nexus-mesh-hermes` | **root** | Hermes 桥接 |
| `nexus-mesh-taskflow` | **root** | OpenClaw 桥接 |

> 两个桥接必须以 root 运行 —— 它们要读 `/root/.hermes` 与 `/root/.openclaw`
> 下的 profile、state 与凭据。这是已知的加固缺口，若要收紧，
> 方向是给这两个 Agent 各建一个专用系统用户并迁移 state 目录。

加一个新节点的完整步骤：

```bash
# ① 生成凭据（服务端与节点侧用同一个值）
echo "NXS_SECRET_<NODE_ID>=<openssl rand -hex 32>" >> /etc/nexus-mesh/nxs.env
chmod 600 /etc/nexus-mesh/nxs.env
systemctl restart nexus-mesh          # ★ 环境变量在进程启动时固定

# ② 审计工具面（OpenClaw 必做）
.venv/bin/python examples/openclaw_node.py --audit-tools

# ③ 探活 + 注册
set -a; . /etc/nexus-mesh/nxs.env; set +a
export NXS_COORDINATOR_URL=https://127.0.0.1:18800/nxs
export NXS_CA_FILE=/etc/nexus-mesh/tls/ca.pem
.venv/bin/python examples/openclaw_node.py --dry-run

# ④ 批准
.venv/bin/python -m nxs ops approve <NODE_ID> \
  --url $NXS_COORDINATOR_URL --ca-file /etc/nexus-mesh/tls/ca.pem

# ⑤ 常驻
# 参照 nexus-mesh-taskflow.service 写一个 unit
```

⚠ **批准绑定的是当前那张角色卡。** 卡片一变（哪怕只改 description），
批准会自动撤销，需要重新批准。所以卡片定稿后再批。

---

## 七、已知限制

| 限制 | 说明 |
|---|---|
| 每条任务烧 token | deepseek-v4-pro。`tasks_per_day: 100` 是刻意的成本闸门 |
| 无状态单次问答 | 多轮要靠 `--resume` / `--session-id`，会引入会话管理 |
| Agent 不知道自己在一个 mesh 里 | prompt 只说"来自任务总线"。要它做跨节点判断时，得在问题里带上背景 |
| 无 token 用量统计 | 要做的话应记进审计（元数据，不含内容） |
| 同机调试的盲区 | 两个桥接与协调节点在同一台机器上 —— 见第八节 |

---

## 八、同机调试能验什么、不能验什么

**能验**：协议正确性、策略矩阵、边界过滤、失败语义、配额、观察窗数据。

**不能验**（等公司电脑接入才暴露）：

| 盲区 | 后果 |
|---|---|
| 同一台机器、同一 IP | 跨网络延迟、断连重连、TLS 路径都没测 |
| 都在 `cloud` 域 | **信任域的真实边界没被检验** — `zone-work → cloud` 一条规则都没跑过 |
| 共享宿主环境 | 测不出状态目录隔离问题 |

所以：**同机跑通 ≠ 接入没问题**。跨网络那一层必须补验。
