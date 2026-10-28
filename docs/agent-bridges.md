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


---

## 九、A2A 协作：Agent 之间怎么互相派任务

### 完整的接入形态

一个 Agent 要能**主动**参与 mesh，需要四样东西：

| # | 要素 | 谁提供 |
|---|---|---|
| 1 | 身份接入（角色卡、心跳） | 桥接进程 |
| 2 | 收任务、执行、交付 | 桥接进程 |
| 3 | **发布权**（`issuer: true` + `can_issue_types`） | 角色卡 |
| 4 | **可调用的工具**（`nxs-cli` + 凭据 + 上下文） | `nxs-cli` + AGENTS.md |

第 3、4 项是 v0.17 补的 —— 在此之前 Agent 只能被动应答，
Agent 之间没有边，所有协作都得由 coordinator 中转。
**那不是 mesh，是消息队列。**

### 关键：`nxs-cli` 与凭据

```bash
nxs-cli --as taskflow ask --to hermes-working "问题"
```

- `--as` 指定身份 → 从 `/etc/nexus-mesh/agents/taskflow.env` 读凭据
- **密钥不进 Agent 的上下文** —— 它只知道自己的名字
- `ask` = `publish` + 轮询 `get_task` + 取产出（不是新协议动作）

### ⚠ 一个真实的架构不对称

| Agent | exec 在哪个进程 | `NXS_ACTOR` 能注入吗 |
|---|---|---|
| Hermes | 桥接的子进程（`hermes -z`） | ✅ 能 |
| OpenClaw | **常驻 gateway 进程** | ❌ 不能 |

OpenClaw 的 `exec` 工具跑在 gateway 里（PID 3446），不在桥接的子进程里。
所以给桥接的子进程设 `NXS_ACTOR` 对 OpenClaw 无效 —— 实测 `ACTOR=` 为空。

**解法**：在 agent 的 `AGENTS.md` 里写明"命令要带 `--as <你的ID>`"。
那个文件每轮会被注入，是 OpenClaw 侧的上下文入口。

### ⚠⚠ 安全收窄 与 A2A 主动性是冲突的

要给 Agent 主动发任务的能力，它就必须能执行 `nxs-cli` —— **那需要 terminal**。

而 v0.12 我给 Hermes 设的是 `-t safe`（无 terminal），理由是
"通过 mesh 派任务等于让远端触发 LLM 执行"。两件事不能同时成立：

| 选择 | 后果 |
|---|---|
| `-t safe`（无 terminal） | 安全，但 Hermes **只能被动应答**，无法主动 A2A |
| `-t safe,terminal` | 能主动 A2A，但它能执行任意命令（以 root） |

当前选择：**`safe,terminal`**（2026-10-27，用户判断：本机调试、
指令来自运维本人，风险由对话控制）。

改回去只需删掉 `terminal`，但那时它会退化成"只能被问、不能问别人"。

### 实测：双向链路

```
正向  coordinator → taskflow       → hermes-working    ✓
反向  coordinator → hermes-working → taskflow          ✓
```

任务表里能看到 `from_node=taskflow` / `from_node=hermes-working` 的记录 ——
那是 **Agent 自己发起的任务**，不是 coordinator 代发的。

反向那次，taskflow 在回答里还主动做了额外的自检：

> 好用——反向调用同样通：我通过 nxs-cli 向 hermes-working 提问并成功收到
> 其答复，说明链路在双向都能正常工作。

---

## 十、部署顺序（踩过的坑）

**必须先让桥接稳定提交最终卡片，再批准。**

我踩过一次：批准时桥接还在崩溃循环（提交的是旧卡片），
桥接修好起来后提交了新卡片 → 卡片变了 → **批准自动撤销** →
报 `MX-RECEIVE-03`，而那看起来像"域不匹配"，实际是"没批准"。

正确顺序：

```bash
# ① 部署代码 → 重启桥接 → 确认它成功注册且日志稳定
systemctl restart nexus-mesh-hermes
journalctl -u nexus-mesh-hermes -n 5 --no-pager     # 看到"开始常驻"才算好

# ② 然后批准（批准绑定的是此刻服务端持有的那张卡）
nxs ops approve hermes-working --url ... --ca-file ...

# ③ 验证
nxs-cli --as hermes-working whoami    # 批准：是
```

> 桥接在未批准时会持续轮询被拒。实测一次部署里累积了 **309 次**
> `MX-RECEIVE-03` —— 数字很大但无害，只是说明客户端重试较密。
> 看监控时别被这个数字吓到。

---

## 十一、密钥不该出现在 Agent 身上（实测漏洞）

**曾经的错误做法**（v0.16–v0.17 早期）：桥接的 systemd unit 用
`EnvironmentFile=/etc/nexus-mesh/nxs.env` —— 那会把**整个文件**加载进
进程环境，包括 `NXS_OPS_SECRET` 和所有其他节点的密钥。
而 `_hermes_env` 用 `dict(os.environ)` 继承 → 全部传给 Agent。

用 `tr '\0' '\n' < /proc/<pid>/environ` 一看就知道：
运维密钥和别人的节点密钥赫然在列。Agent 能 exec，所以它读得到。

**现在的做法**：

1. `agent_bridge_common.scrub_secrets()` 按前缀剔除 `NXS_SECRET*` / `NXS_OPS*`
2. 桥接从 `/etc/nexus-mesh/agents/<id>.env` 读**自己那一把**
3. unit 里**不用** `EnvironmentFile`
4. 验证方式：`tr '\0' '\n' < /proc/<pid>/environ | grep -c '^NXS_SECRET'`
   应为 **0**

> ⚠ 而当时的测试 `test_hermes_env_does_not_carry_secret` 是**假阳性**：
> 它跑在测试进程里，那个环境本来就没有这些变量，断言永远成立。
> **测试绿着，漏洞开着。** 现在改成先真的注入再断言。


---

## 十二、Skill：让 Agent 知道怎么用 mesh

### 装在哪（两个 Agent 位置不同）

| Agent | 位置 | 加载方式 |
|---|---|---|
| Hermes | `/root/.hermes/profiles/<profile>/skills/<分类>/<名>/SKILL.md` | 扫目录 |
| OpenClaw | `/root/.openclaw/workspace-<agent>/skills/<名>/SKILL.md` | 自己的 `skills install` |

**同一份源文件**（仓库里的 `nexus-mesh-skill.md`），只是复制到不同位置。
别各维护一份 —— 那会腐化。

```bash
bash deploy/install-skill.sh            # 装到全部已配置的 Agent
bash deploy/install-skill.sh --verify   # 只检查，不改动
```

> ⚠ OpenClaw 的 `skills install` 覆盖已存在的 skill **必须加 `--force`**。
> 不加时它只打印"已存在"然后退出 —— 脚本看起来成功了，内容还是旧的。

### frontmatter 要两个命名空间

```yaml
metadata:
  hermes:
    tags: [Agent, Collaboration, A2A]
  openclaw:
    emoji: "🕸"
    requires:
      bins: ["nxs-cli"]     # OpenClaw 会据此检查二进制在不在
```

漏掉某一个不会报错，只是那一侧的元信息缺失。OpenClaw 那侧的
`requires.bins` 有用 —— `skills info` 会显示 `Binaries: ✓ nxs-cli`。

### ⚠ skill 是**渐进披露**的，不是全文注入

这是最容易误解的一点。装完不等于 Agent 就"会了"：

1. 系统提示里只有 **name + description**
2. 全文（那个 SKILL.md）要 Agent **自己主动 read**
3. 所以 **description 必须写清"什么时候该用它"**

只写"这是 Agent 之间的任务网络"不够 —— Agent 不知道该在何时想起来。
现在的写法是：

> Nexus Mesh 是 Agent 之间的任务网络。用 nxs-cli 查看自己的身份、
> 问别的 Agent 问题、发任务、取结果。**当需要跟另一个 Agent 协作、
> 或需要确认自己在网络里的身份与能力时使用。**

### ⚠ 改完 skill 文件，Agent 不一定立刻用新内容

OpenClaw 的文档明说：`the node host does not watch the skills directory`。
加上渐进披露的机制，实际表现是：

- 装好之后 Agent "知道有这么个 skill"
- 但那一次回答可能凭上下文里的旧印象，而不是重读文件
- **明确要求它读文件**时，它会读到最新版

实测过：同一天先后问两次，第一次引用了旧内容（"问运维 / 看角色卡"），
第二次明确说"先读 /opt/nexus-mesh/nexus-mesh-skill.md 的全文"之后，
准确列出了新版的三条途径（含刚加的"别指望 whoami"）。

**结论**：文档写得对不对，比装得对不对更容易出问题。
所以有 `tests/test_skill_doc.py` 守着。

### 文档里踩过的两个坑（都实测暴露）

**① 断言"桥接已为你设好身份" —— 对 OpenClaw 是假的**

原话："省略 `--as` 时用 `NXS_ACTOR` 环境变量 —— 桥接进程已为你设好。"
但 OpenClaw 的 exec 跑在常驻 gateway 里，拿不到桥接注入的环境。
照这段话做会直接失败。

现在改成：先跑 `nxs-cli whoami` 试一次，失败就每条都带 `--as`。

**② 循环依赖 —— 教 Agent 用需要 ID 的命令去查 ID**

原话："若报「无法确定身份」…（先跑 `nxs-cli whoami` 确认自己的节点 ID）"

而 `whoami` **正是因为没 ID 才报错的**。实测 taskflow 复述了这个循环，
说明它真被误导了。

现在给出 whoami 之外的三种途径：

```bash
echo $NXS_ACTOR      # ① 有值就直接用（Hermes 一般有）
```
② 看 `AGENTS.md` / 系统提示里有没有「你的节点身份是 xxx」
③ 问运维（他跑 `nxs ops nodes` 能看到全部）

### 还有一件 skill 做不到的事：告诉每个 Agent **它自己的 ID**

Skill 是通用文档，用的是 `<你的节点ID>` 占位符。所以装完还要：

- **Hermes**：`NXS_ACTOR` 环境变量已注入（桥接的子进程），它能自己 `echo`
- **OpenClaw**：拿不到，要写进它的 `AGENTS.md`（见第九节）

技能文档可以共用，**身份不能共用**。


---

## 十二、Skill：让 Agent 知道怎么用 mesh

### 装在哪（两个 Agent 位置不同）

| Agent | 位置 | 加载方式 |
|---|---|---|
| Hermes | `/root/.hermes/profiles/<profile>/skills/<分类>/<名>/SKILL.md` | 扫目录 |
| OpenClaw | `/root/.openclaw/workspace-<agent>/skills/<名>/SKILL.md` | 自己的 `skills install` |

**同一份源文件**（仓库里的 `nexus-mesh-skill.md`），只是复制到不同位置。
别各维护一份 —— 那会腐化。

```bash
bash deploy/install-skill.sh            # 装到全部已配置的 Agent
bash deploy/install-skill.sh --verify   # 只检查，不改动
```

> ⚠ OpenClaw 的 `skills install` 覆盖已存在的 skill **必须加 `--force`**。
> 不加时它只打印"已存在"然后退出 —— 脚本看起来成功了，内容还是旧的。

### frontmatter 要两个命名空间

```yaml
metadata:
  hermes:
    tags: [Agent, Collaboration, A2A]
  openclaw:
    emoji: "🕸"
    requires:
      bins: ["nxs-cli"]     # OpenClaw 会据此检查二进制在不在
```

漏掉某一个不会报错，只是那一侧的元信息缺失。OpenClaw 那侧的
`requires.bins` 有用 —— `skills info` 会显示 `Binaries: ✓ nxs-cli`。

### ⚠ skill 是**渐进披露**的，不是全文注入

这是最容易误解的一点。装完不等于 Agent 就"会了"：

1. 系统提示里只有 **name + description**
2. 全文（那个 SKILL.md）要 Agent **自己主动 read**
3. 所以 **description 必须写清"什么时候该用它"**

只写"这是 Agent 之间的任务网络"不够 —— Agent 不知道该在何时想起来。
现在的写法是：

> Nexus Mesh 是 Agent 之间的任务网络。用 nxs-cli 查看自己的身份、
> 问别的 Agent 问题、发任务、取结果。**当需要跟另一个 Agent 协作、
> 或需要确认自己在网络里的身份与能力时使用。**

### ⚠ 改完 skill 文件，Agent 不一定立刻用新内容

OpenClaw 的文档明说：`the node host does not watch the skills directory`。
加上渐进披露的机制，实际表现是：

- 装好之后 Agent "知道有这么个 skill"
- 但那一次回答可能凭上下文里的旧印象，而不是重读文件
- **明确要求它读文件**时，它会读到最新版

实测过：同一天先后问两次，第一次引用了旧内容（"问运维 / 看角色卡"），
第二次明确说"先读 /opt/nexus-mesh/nexus-mesh-skill.md 的全文"之后，
准确列出了新版的三条途径（含刚加的"别指望 whoami"）。

**结论**：文档写得对不对，比装得对不对更容易出问题。
所以有 `tests/test_skill_doc.py` 守着。

### 文档里踩过的两个坑（都实测暴露）

**① 断言"桥接已为你设好身份" —— 对 OpenClaw 是假的**

原话："省略 `--as` 时用 `NXS_ACTOR` 环境变量 —— 桥接进程已为你设好。"
但 OpenClaw 的 exec 跑在常驻 gateway 里，拿不到桥接注入的环境。
照这段话做会直接失败。

现在改成：先跑 `nxs-cli whoami` 试一次，失败就每条都带 `--as`。

**② 循环依赖 —— 教 Agent 用需要 ID 的命令去查 ID**

原话："若报「无法确定身份」…（先跑 `nxs-cli whoami` 确认自己的节点 ID）"

而 `whoami` **正是因为没 ID 才报错的**。实测 taskflow 复述了这个循环，
说明它真被误导了。

现在给出 whoami 之外的三种途径：

```bash
echo $NXS_ACTOR      # ① 有值就直接用（Hermes 一般有）
```
② 看 `AGENTS.md` / 系统提示里有没有「你的节点身份是 xxx」
③ 问运维（他跑 `nxs ops nodes` 能看到全部）

### 还有一件 skill 做不到的事：告诉每个 Agent **它自己的 ID**

Skill 是通用文档，用的是 `<你的节点ID>` 占位符。所以装完还要：

- **Hermes**：`NXS_ACTOR` 环境变量已注入（桥接的子进程），它能自己 `echo`
- **OpenClaw**：拿不到，要写进它的 `AGENTS.md`（见第九节）

技能文档可以共用，**身份不能共用**。


---

## 十三、公司电脑作为内网执行者（v0.19）

### 为什么是「执行者」而不是「云上拿 token 去查」

用户的原话点破了核心：

> 这样的方式能够使得不用把公司内部系统的 token 与鉴权信息暴露给云服务器上的
> Agent，减少了数据泄露风险。

这个论证比"云上存 token 不安全"更准确。真正的区别是：

| 做法 | 云上拥有什么 |
|---|---|
| 云上存 token，自己查 | **查询本身的能力** —— 它能查任意东西，只在 token 权限范围内 |
| 公司电脑当执行者 | 只有**请求查询的能力** —— 它只能说"帮我查 X"，查不查、查多少由内网决定 |

差别不在"token 藏得好不好"，而在**能力边界**。前者的边界是 token 的权限，
后者的边界是"内网那个 CLI 愿意做什么"。

### 但凭证隔离只解决一半：结果要过云

```
云上 Agent ──"查 XX 项目本周进度"──> work-laptop 节点
                                          │ 调公司 CLI（本机凭证）
云上 Agent <──── 摘要（≤1500 字节）───────┘
```

凭证没出内网 ✅，但**回传内容会写进云服务器的 `/var/lib/nexus-mesh/queue.db`** ❌。

所以还有第二道闸门。

### ⚠⚠ 「只回传摘要」必须由代码执行，不能靠 prompt

这是 v0.19 最重要的设计判断。在 prompt 里写"只要摘要"是不够的：

* 被提示注入的 Agent 照样会回传原文
* 而且它自己认为在**配合** —— 不会有任何报错

所以分两层落地：

| 层 | 手段 | 强度 |
|---|---|---|
| 行为引导 | `prompt_note` 里写"只要摘要、不要原文" | 弱（可能被绕过） |
| **硬边界** | `SUMMARY_LIMIT = 1500` 字节，桥接进程截断 | **强** |

截断时末尾留标记（`…（已截断）`），让调用方知道"还有内容没给我"——
而不是以为那就是全部。

有两条测试守着这个：

* 截断后**总长**（含标记字节）不超上限 —— off-by-N 很容易算错，
  标记看着 6 个字符，实际 18 字节
* `SUMMARY_LIMIT` 必须**严格小于** `MAX_ANSWER_BYTES`。
  两者相等的话，这道闸门就形同不存在

### 工具白名单：这里比云端两个 Agent 更干净

云端 `taskflow` 的 allow 列表含 `exec`/`process`/`write`（未收窄，
靠矩阵和配额兜）。公司电脑这个节点不同 —— CodeBuddy 支持细粒度白名单：

```bash
--allowedTools "Bash(projectctl:*)" "Bash(meetingctl:*)" "Read" "Grep"
```

含义：只能调这两个 CLI、读文件、搜文件。**不能执行任意命令、不能写文件。**

**不给 `--dangerously-skip-permissions`。** 那个参数放开几乎全部权限
（连 HIGH/CRITICAL 命令都只"可能"拦）。既然能用白名单精确表达，
就不该用粗放的开关 —— 有测试守着这条（`test_never_skips_permissions`）。

**没有白名单时拒绝启动**，不静默降级。静默降级是最糟的处理：
节点启动了、能收任务了，但实际拥有全部工具，而运维只看得到"启动成功"。

### `--permission-mode dontAsk`：为什么不是默认

`-p` 是单次执行，没有人会去点权限确认框。默认模式下需要授权的操作会
**挂起**，直到任务租约过期 —— 那比失败更糟（失败至少能重试）。

`dontAsk` 让它**被拒**，快速失败。

### 实现与部署

| 文件 | 作用 |
|---|---|
| `examples/codebuddy_node.py` | 桥接（复用 `agent_bridge_common`） |
| `deploy/setup-work-laptop.sh` | 本机准备：凭据 / CA / skill / PATH 提示 |
| `tests/test_codebuddy_node.py` | 26 条，重点在边界而不是"能不能调通" |

步骤：

```bash
# ① 本机检查（网络 + CodeBuddy + 白名单）
bash deploy/setup-work-laptop.sh --check

# ② 确认 CodeBuddy 能用（会消耗一点额度）
python examples/codebuddy_node.py --check-tools

# ③ 注册（会等批准）
python examples/codebuddy_node.py --dry-run

# ④ 服务器上批准
nxs ops approve work-laptop --url ... --ca-file ...

# ⑤ 常驻（launchd）
```

### 矩阵改动（v0.19）

```yaml
cloud      → zone-work   allow: [..., "work.query", "work.*"]
zone-work  → cloud       allow: ["work.*", "agent.ask"]
```

第二条是新加的：原先只放行 `work.*`，意味着公司电脑**不能主动问云端**
（会被 `MX-DOMAIN-02` 拒）—— 那是单向的。现在双向。

### 一个 shell 坑（中文脚本专属）

`deploy/setup-work-laptop.sh` 里第一版有：

```bash
ok "协调节点可达（HTTP $code）"     # ✗ 崩在 unbound variable
```

**shell 把全角括号 `）` 当成了变量名的一部分** → 变量名变成 `code）`，
未定义，`set -u` 下直接报错。

修法：`${code}`。中文为主的脚本里这个问题很普遍 —— `$var` 后面紧跟
任何非 ASCII 字符都会犯。写这类脚本时**变量一律加花括号**最省事。


---

## 十三、公司电脑作为内网执行者（v0.19）

### 为什么是「执行者」而不是「云上拿 token 去查」

用户的原话点破了核心：

> 这样的方式能够使得不用把公司内部系统的 token 与鉴权信息暴露给云服务器上的
> Agent，减少了数据泄露风险。

这个论证比"云上存 token 不安全"更准确。真正的区别是：

| 做法 | 云上拥有什么 |
|---|---|
| 云上存 token，自己查 | **查询本身的能力** —— 它能查任意东西，只在 token 权限范围内 |
| 公司电脑当执行者 | 只有**请求查询的能力** —— 它只能说"帮我查 X"，查不查、查多少由内网决定 |

差别不在"token 藏得好不好"，而在**能力边界**。前者的边界是 token 的权限，
后者的边界是"内网那个 CLI 愿意做什么"。

### 但凭证隔离只解决一半：结果要过云

```
云上 Agent ──"查 XX 项目本周进度"──> work-laptop 节点
                                          │ 调公司 CLI（本机凭证）
云上 Agent <──── 摘要（≤1500 字节）───────┘
```

凭证没出内网 ✅，但**回传内容会写进云服务器的 `/var/lib/nexus-mesh/queue.db`** ❌。

所以还有第二道闸门。

### ⚠⚠ 「只回传摘要」必须由代码执行，不能靠 prompt

这是 v0.19 最重要的设计判断。在 prompt 里写"只要摘要"是不够的：

* 被提示注入的 Agent 照样会回传原文
* 而且它自己认为在**配合** —— 不会有任何报错

所以分两层落地：

| 层 | 手段 | 强度 |
|---|---|---|
| 行为引导 | `prompt_note` 里写"只要摘要、不要原文" | 弱（可能被绕过） |
| **硬边界** | `SUMMARY_LIMIT = 1500` 字节，桥接进程截断 | **强** |

截断时末尾留标记（`…（已截断）`），让调用方知道"还有内容没给我"——
而不是以为那就是全部。

有两条测试守着这个：

* 截断后**总长**（含标记字节）不超上限 —— off-by-N 很容易算错，
  标记看着 6 个字符，实际 18 字节
* `SUMMARY_LIMIT` 必须**严格小于** `MAX_ANSWER_BYTES`。
  两者相等的话，这道闸门就形同不存在

### 工具白名单：这里比云端两个 Agent 更干净

云端 `taskflow` 的 allow 列表含 `exec`/`process`/`write`（未收窄，
靠矩阵和配额兜）。公司电脑这个节点不同 —— CodeBuddy 支持细粒度白名单：

```bash
--allowedTools "Bash(projectctl:*)" "Bash(meetingctl:*)" "Read" "Grep"
```

含义：只能调这两个 CLI、读文件、搜文件。**不能执行任意命令、不能写文件。**

**不给 `--dangerously-skip-permissions`。** 那个参数放开几乎全部权限
（连 HIGH/CRITICAL 命令都只"可能"拦）。既然能用白名单精确表达，
就不该用粗放的开关 —— 有测试守着这条（`test_never_skips_permissions`）。

**没有白名单时拒绝启动**，不静默降级。静默降级是最糟的处理：
节点启动了、能收任务了，但实际拥有全部工具，而运维只看得到"启动成功"。

### `--permission-mode dontAsk`：为什么不是默认

`-p` 是单次执行，没有人会去点权限确认框。默认模式下需要授权的操作会
**挂起**，直到任务租约过期 —— 那比失败更糟（失败至少能重试）。

`dontAsk` 让它**被拒**，快速失败。

### 实现与部署

| 文件 | 作用 |
|---|---|
| `examples/codebuddy_node.py` | 桥接（复用 `agent_bridge_common`） |
| `deploy/setup-work-laptop.sh` | 本机准备：凭据 / CA / skill / PATH 提示 |
| `tests/test_codebuddy_node.py` | 26 条，重点在边界而不是"能不能调通" |

步骤：

```bash
# ① 本机检查（网络 + CodeBuddy + 白名单）
bash deploy/setup-work-laptop.sh --check

# ② 确认 CodeBuddy 能用（会消耗一点额度）
python examples/codebuddy_node.py --check-tools

# ③ 注册（会等批准）
python examples/codebuddy_node.py --dry-run

# ④ 服务器上批准
nxs ops approve work-laptop --url ... --ca-file ...

# ⑤ 常驻（launchd）
```

### 矩阵改动（v0.19）

```yaml
cloud      → zone-work   allow: [..., "work.query", "work.*"]
zone-work  → cloud       allow: ["work.*", "agent.ask"]
```

第二条是新加的：原先只放行 `work.*`，意味着公司电脑**不能主动问云端**
（会被 `MX-DOMAIN-02` 拒）—— 那是单向的。现在双向。

### 一个 shell 坑（中文脚本专属）

`deploy/setup-work-laptop.sh` 里第一版有：

```bash
ok "协调节点可达（HTTP $code）"     # ✗ 崩在 unbound variable
```

**shell 把全角括号 `）` 当成了变量名的一部分** → 变量名变成 `code）`，
未定义，`set -u` 下直接报错。

修法：`${code}`。中文为主的脚本里这个问题很普遍 —— `$var` 后面紧跟
任何非 ASCII 字符都会犯。写这类脚本时**变量一律加花括号**最省事。

## 八、同机调试能验什么、不能验什么

**能验**：协议正确性、策略矩阵、边界过滤、失败语义、配额、观察窗数据。

**不能验**（等公司电脑接入才暴露）：

| 盲区 | 后果 |
|---|---|
| 同一台机器、同一 IP | 跨网络延迟、断连重连、TLS 路径都没测 |
| 都在 `cloud` 域 | **信任域的真实边界没被检验** — `zone-work → cloud` 一条规则都没跑过 |
| 共享宿主环境 | 测不出状态目录隔离问题 |

所以：**同机跑通 ≠ 接入没问题**。跨网络那一层必须补验。

---

## 十四、Windows 支持（公司电脑是 Windows）

v0.19 的桥接最初只在 macOS/Linux 上验过。用户告知公司电脑是 **Windows** 后，
补了三处平台差异。**这三处不是"顺便兼容"，是必然会碰到的**。

### ① npm 装的 `codebuddy` 是 `.cmd`，Python 跑不了

```python
subprocess.run(["C:\\npm\\codebuddy.cmd", "-p", "x"])   # ✗ WinError 193
```

Python 在 Windows 上**不能直接执行 `.cmd`/`.bat`** —— 报
`%1 is not a valid Win32 application`。而 npm 全局安装生成的正是 `.cmd`：

```
npm i -g @tencent-ai/codebuddy-code
→ %APPDATA%\npm\codebuddy.cmd        ← 不是 .exe
```

那个错误信息完全不会让人想到"后缀名"。所以加了一层处理：

```python
if sys.platform == "win32" and cmd[0].lower().endswith((".cmd", ".bat")):
    return ["cmd", "/c", *cmd]
```

**不用 `shell=True`** —— 那会把 prompt 交给 shell 解析，而 prompt 里可能有
引号、`&`、`|`，在 shell 下都变成注入面。`cmd /c` + 列表参数保留了逐个传参。

非 Windows 平台**原样返回**（有测试守着）。

### ② 可执行文件路径三平台三个位置

写死 `~/.local/bin/codebuddy` 的话，Windows 上永远找不到，
而症状是"没装 codebuddy"——用户会去重装一遍。

现在按平台探测，**PATH 优先**：

| 平台 | 候选 |
|---|---|
| 通用 | `shutil.which("codebuddy")` ← 先看这个 |
| macOS | `~/.local/bin`、`/usr/local/bin`、`/opt/homebrew/bin` |
| Windows | `%LOCALAPPDATA%\codebuddy\bin\codebuddy.exe`、`%APPDATA%\npm\codebuddy.cmd` |

### ③ 常驻：计划任务，且要关掉 3 天上限

macOS 用 launchd，Windows 用计划任务（`deploy/setup-work-laptop.ps1`）。

两个必须做对的细节：

**用 `pythonw.exe` 而不是 `python.exe`**
否则会有一个黑色控制台窗口，用户会关掉它 —— 然后节点就死了。

**`-ExecutionTimeLimit ([TimeSpan]::Zero)`**
计划任务默认有 **3 天执行上限**。不关的话，节点跑满 72 小时被系统杀掉，
而症状是"莫名其妙不干活了"，很难联想到计划任务设置。

**`-AtLogOn` 而不是 `-AtStartup`**
`AtStartup` 要"无论用户是否登录都运行"，那需要存储账户密码或用 SYSTEM 账户 ——
公司电脑上两者都不合适。代价是**用户注销后进程会停**。
对"7x24 开机且保持登录"的机器没问题；如果公司策略会自动注销，要改用别的方式。

### ⚠ 一个比 bash 更危险的坑：`$var` 后面跟中文

bash 里 `"$code）"` 会把全角括号算进变量名（报 `unbound variable`）。

**PowerShell 里更糟** —— 它的变量名**允许 Unicode**，所以 `"$ver（"`
会解析出一个叫 `ver（` 的变量，值是**空**，而且**不报错**。
脚本继续往下跑，行为诡异但不明确失败。

修法一样：`${var}`。两个脚本都有测试守着这条。

### 无法验证的部分（诚实说明）

**我没有在 Windows 上跑过 `setup-work-laptop.ps1`。**
沙箱里装不了 PowerShell（`sandbox_apply: Operation not permitted`）。

所以做了静态检查：括号配对与嵌套顺序、`$var` 污染、关键逻辑存在性
（有 `tests/test_deploy_scripts.py` 守着）。但**这不等于能跑**。

第一次在公司电脑上执行时，如果报语法错，直接看报错行号 —— 大概率是
我某处引号或转义写错了。

### 一个可能的阻塞：公司电脑没装 Python

桥接是 Python 程序（`requires-python = ">=3.11"`，依赖只有 PyYAML）。
公司电脑上如果没装，整个方案跑不了。脚本第一件事就是查这个：

```powershell
winget install Python.Python.3.12
```

装的时候**要勾上 Add python.exe to PATH**，否则脚本找不到。

如果公司禁止装 Python，替代方向是把桥接用 PyInstaller 打成单个 exe ——
但那会让"改一行代码看效果"变得很麻烦。先用 Python，卡住了再考虑。
