权限模型改造 · 实施顺序

> 配套：`docs/design/06-agent-role-model.md`（目标模型）
> 本文是**开发顺序**，不是迁移计划。
> 日期：2026-10-29

---

## 0. 前提：这是一次整体替换，不是灰度升级

**当前系统尚未正式部署，不需要考虑运行时兼容。**

| 不做什么 | 原因 |
|---|---|
| 双格式兼容（新旧卡片并存） | 没有外部用户在用 |
| `card_version` 版本协商 | 不是线上灰度 |
| 数据库迁移脚本 | 清库重建即可 |
| 分阶段部署观察窗口 | 没有"边跑边改"的场景 |
| 保留旧判定作为回退路径 | 直接改，出问题就改回来 |

**做法**：停掉全部进程 → 改完 → 重启 → 验证。

回退方式就是 `git revert`，不需要运行时开关。

### 唯一保留的东西：代码内部的实现顺序

下面的步骤划分**不是为了部署安全，而是为了开发时不失控**。

理由很实际：如果一次性把数据结构、判定逻辑、配置格式全改完再跑测试，
可能几百个测试同时红，那时**无法判断哪一处是根因**。

所以仍然分步，每步跑一次测试。**测试绿了再进下一步。**

---

## 1. 停服务（开工前）

```bash
# 服务器：停四个服务
sudo systemctl stop nexus-mesh nexus-mesh-ui nexus-mesh-hermes nexus-mesh-taskflow
sudo systemctl disable nexus-mesh nexus-mesh-ui nexus-mesh-hermes nexus-mesh-taskflow
```

```powershell
# Windows：停并禁用计划任务
Stop-ScheduledTask    -TaskName NexusMesh-work-desktop
Disable-ScheduledTask -TaskName NexusMesh-work-desktop
```

**确认全停了**：

```bash
systemctl is-active nexus-mesh nexus-mesh-ui nexus-mesh-hermes nexus-mesh-taskflow
# 应全部输出 inactive
```

### 数据怎么处理

| 文件 | 大小 | 处置 |
|---|---|---|
| `registry.db` | 48K | **删**。节点重新注册 + 重新批准 |
| `queue.db` | 68K | **删**。没有在途任务，无价值 |
| `audit.ndjson` | 4.0M | **留着**。历史证据，与模型改造无关 |
| `/etc/nexus-mesh/nxs.env` | — | **不动**。密钥不重新生成 |

关键：**密钥在 `nxs.env` 里，不在库里**。所以清库后节点用原凭据重连，
只需重新批准，不需要挨个换密钥、改凭据文件、改计划任务参数。

清库命令（开工时执行）：

```bash
sudo rm -f /var/lib/nexus-mesh/registry.db /var/lib/nexus-mesh/registry.db-shm \
             /var/lib/nexus-mesh/registry.db-wal \
             /var/lib/nexus-mesh/queue.db /var/lib/nexus-mesh/queue.db-shm \
             /var/lib/nexus-mesh/queue.db-wal
```

---

## 2. 六个实现步骤

每步结束的可运行状态与验收判据如下。**测试没绿不要进下一步。**

### Step 1 · 类型与角色定义进配置 ✅ 已完成（2026-10-29）

> **完成情况**：`TypeDecl` / `RoleDecl` 已实现，`types:` / `roles:` 两节
> 已接入 `load_config`，示例配置已补齐，新增 23 条测试。
> **旧判定逻辑一行未动** —— `nxs verify` 19 项、`make demo` 九步全部通过。

**改**

- `local.yaml` 新增 `types:` 节（TaskType 全局定义：`delivery` / `params_schema` / `response_fields` / `max_level`）
- `local.yaml` 新增 `roles:` 节（worker / driver / publisher，含 `can_issue` / `may_broadcast` / `serves` / `max_response_level`）
- `config.py` 加 `TaskTypeConfig` / `RoleConfig`，启动时校验：
  - 角色引用的类型必须已定义
  - 类型名符合 `<verb>.<object>` 约定

**这步只加配置解析，不改任何判定。** 跑测试应该全绿。

**判据**：配置里有非法类型引用 → 启动报错且指明是哪一行。

**实际产出**

| 内容 | 位置 |
|---|---|
| `TypeDecl`（类型定义） | `nxs/mgmt/config.py` |
| `RoleDecl`（角色定义） | 同上 |
| `_parse_types` / `_parse_roles` | 同上，含交叉校验 |
| 示例配置 `types:` / `roles:` | `config/nxs.example.yaml` |
| 测试 23 条 | `tests/test_type_and_role_config.py` |

**实现的静态校验**（每条都有对应测试）

| 校验 | 为什么 |
|---|---|
| 类型名符合 `<verb>.<object>` | 与能力声明的命名约定一致 |
| **同名类型不可重复定义** | ★ 本改造的核心目的：旧模型允许两套契约共存 |
| `delivery` 只接受 unicast / broadcast | multicast 不预留"用不了的值" |
| `freetext_fields ⊆ response_fields` | 声明一个永不出现的字段会让人误以为那里有清洗 |
| 未知字段报错 | 拼错 `delivery` 被静默忽略 → 以为配了实际没配 |
| 角色引用的类型必须已定义 | 提前报错，避免运行时"派了活没反应" |
| 通配模式必须能匹配到类型 | `invest.*` 在只有 `work.*` 时是配置错误 |
| **空角色被拒** | 既不能发又不服务的角色，批准了等于什么都没放 |

**⚠ Step 1 期间新增的 5 个「已知缺口」**

`TypeDecl` / `RoleDecl` 的字段现在**只解析、不参与判定**。这必须如实登记，
不能让它静默存在 —— 已加入 `config.UNIMPLEMENTED`：

| 字段 | 何时接线 |
|---|---|
| `RoleDecl.can_issue` | Step 3 |
| `RoleDecl.serves` | Step 4 |
| `TypeDecl.delivery` | Step 4 |
| `TypeDecl.response_fields` | Step 3 |
| `TypeDecl.max_level` | Step 4 |

> 这个登记是 `test_every_config_field_is_read_somewhere` 逼出来的 ——
> 该守护的历史事故正是 `MatrixRule.allow_issue` 定义了却从未被读取。
> **我没有用豁免绕过它，而是如实登记并写明接线时机。**

---

### Step 2 · AgentCard 换字段（判定仍走旧路径）

**改**

- `AgentCard` 新增：`role: str`、`self_limits{max_backlog, max_response_level}`、`tools`、`state`
- 旧的 `roles` / `constraints.can_issue_types` / `can_receive_from` / `quota` **仍在**，标记为待删
- 加推导函数：`roles{issuer,executor} → role`（过渡期辅助，Step 5 一起删）

**这步不动判定逻辑。** 目的是先立数据结构，让后面两步有地方落。

**判据（本计划最有价值的一条测试）**

参数化覆盖四种组合，断言「旧判定」与「role 推导」结论一致：

```
(issuer, executor) →  能否发布  能否被执行   推导 role
(True,  True )     →  True      True          driver
(False, True )     →  False     True          worker
(True,  False)     →  True      False         publisher
(False, False)     →  False     False         无角色
```

这条测试的作用不是"证明迁移安全"（没有迁移了），而是**在你动判定逻辑之前，
确认自己对旧行为的理解是对的**。它是一面镜子。

---

### Step 3 · 判定切到 role

**改**

- `check_publish()` → 读 `role.can_issue`
- 删 `policy.py` 里按 `can_issue_types` 逐类型比对的逻辑
- 删 `providers_of` / `registered_types`（类型全局定义后天然唯一）
- `MX-ISSUER-01` 的 message 改成「role X 不允许发布」

**这步开始有行为变化，但应当是等价的**（Step 2 的测试已证明）。

**判据**

- Step 2 的等价测试仍绿（此时它验新判定与固化下来的旧行为常量表）
- 手工：`work-desktop` 尝试发布 → 仍被拒，错误码仍是 `MX-ISSUER-01`

---

### Step 4 · 域矩阵去类型化

**改**

```yaml
# 前
- from: cloud
  to: zone-work
  allow_types: [work.calendar, work.summary, work.query, work.*]
  max_level: summary

# 后
- from: cloud
  to: zone-work
```

- `MatrixRule` 删 `allow_types` / `max_level`
- `MX-RECEIVE-03` 拆成三份：域不可达 → `MX-DOMAIN-02`；
  未批准 → 新增 `AU-APPROVE-05`；任务归属 → 并入已有的 `LF-TASK-404`
- 广播展开时按域过滤（见模型文档 §4）

**这是唯一真正放宽的一步。** 单独做，是为了出问题时归因清晰。

**判据（明确的前后对比）**

| 场景 | Step 3 之后 | Step 4 之后 |
|---|---|---|
| `hermes-working`（driver）发 `work.query` → work-desktop | ❌ 拒 | ✅ **放行** |
| `work-desktop`（worker）发任何任务 | ❌ 拒 | ❌ 仍拒 |
| 未批准节点发任务 | ❌ 拒 | ❌ 仍拒 |

---

### Step 5 · 删旧字段 + 配额 + mesh 退出节点表

三件清理动作合一步做（都是"删除"，失败模式一致）。

**删**

| 删 | 替代 |
|---|---|
| `roles.{executor,relay,observer,human_interface}` | `role` |
| `constraints.can_issue_types` / `can_receive_from` | role + 域矩阵 |
| `quota.{4 项}` | `self_limits` |
| `capacity` → 降级为可选排程提示 | — |
| `local_node` 配置 + `_pulse_local_node()` | mesh 不再占节点名额 |
| `can_delegate_to` / `MX-DELEGATE-04` | 有转发需求再加 |
| `roles{issuer,executor} → role` 推导函数 | Step 2 的过渡件 |

**保留**（不属于节点配额，是 mesh 自我保护）：

- `QT-RATE-04` 请求速率
- `QT-CONN-06` 连接数
- 新增 `QT-BACKLOG-07` 队列深度

**判据**

- `ops nodes` 只剩 3 个节点（coordinator 消失）
- `nxs serve` 启动不再打印「本进程承载节点」
- 循环打 1000 请求 → `QT-RATE-04` 仍生效（证明自保没被误删）
- `UNIMPLEMENTED` 清单只剩 3 项（`roles.*` 四类已消失）

---

### Step 6 · 新功能：`paused` 状态 + schema 校验

**改**

- `ops` 端点新增 `state` 动作：节点申报 `available` / `paused`
- 投递时跳过 `paused`（广播静默跳过；单播明确报错）
- 网关按 `types[].params_schema` 校验 `params`
- 删 `UNIMPLEMENTED` 的 `params_schema` 条目

**判据**

- 节点申报 `paused` → 广播跳过、单播报错
- 心跳停 → `paused` 转 `stale`（不会卡死）
- 传非法参数 → 被拒，错误码 `FM-PARAM-02`
- **`UNIMPLEMENTED` 归零**

---

## 3. 三条全程不变式

每个 Step 结束都验一遍，任何一条破了立即停手：

| # | 不变式 | 怎么验 |
|---|---|---|
| I1 | 未批准节点无法收发任何任务 | 构造未批准节点投递 → 拒 |
| I2 | **worker 永远无法发布任务** | work-desktop 尝试发布 → `MX-ISSUER-01` |
| I3 | 应答上限 = min(role 上限, 节点自限) | 角色放宽但节点收紧 → 取节点值 |

I2 是 `work-desktop` 的核心边界，也是这轮讨论的起点。**任何一步都不能让它失效。**

---

## 4. 改完怎么验

### 4.1 本地先验（不碰生产）

```bash
make test                          # 全部绿
make demo && make demo-net         # 九步全过
make verify                        # 19 项全过
```

再起一个内存 mesh 手工验一遍新链路：

```bash
.venv/bin/python -m nxs serve --memory
# 另一个终端
.venv/bin/python -m nxs.cli.main --as <driver节点> ask --to <worker节点> "2+3"
```

### 4.2 生产重启顺序

**必须按依赖顺序起**：

```bash
# ① 先起 mesh 本体（它是被依赖方）
sudo systemctl start nexus-mesh
systemctl is-active nexus-mesh              # 确认 active

# ② 再起两个云上桥接
sudo systemctl start nexus-mesh-hermes nexus-mesh-taskflow

# ③ 最后起看板（它只是消费者）
sudo systemctl start nexus-mesh-ui
```

Windows 那台最后单独起：

```powershell
Enable-ScheduledTask -TaskName NexusMesh-work-desktop
Start-ScheduledTask  -TaskName NexusMesh-work-desktop
```

### 4.3 重新批准

清库后批准状态丢失，3 个节点需重新批准：

```bash
cd /opt/nexus-mesh
set -a && . /etc/nexus-mesh/nxs.env && set +a
URL=https://127.0.0.1:18800/nxs
CA=/etc/nexus-mesh/tls/ca.pem
.venv/bin/python -m nxs ops nodes --url "$URL" --ca-file "$CA"
# 看哪些 approved=False，逐个批
.venv/bin/python -m nxs ops approve <node-id> --url "$URL" --ca-file "$CA"
```

### 4.4 端到端验证

| # | 验什么 | 期望 |
|---|---|---|
| 1 | `ops nodes` | 3 个节点，全 `alive` |
| 2 | driver 发 worker（单播） | 通过，拿到答案 |
| 3 | worker 发任务 | 拒，`MX-ISSUER-01` |
| 4 | 未批准节点发任务 | 拒 |
| 5 | 跨域被矩阵挡 | 拒，`MX-DOMAIN-02` |
| 6 | 超长应答 | 截断到 `min(role, 自限)` |
| 7 | 节点申报 `paused` | 广播跳过它 |
| 8 | 看板 | 正常显示，且**只能读** |

第 8 条的"只能读"需要 `NXS_VIEW_SECRET`。它属于**纯增量补丁**，
与模型改造无关，建议单独先做掉——这样它能独立验证，不会和模型改动混在一起。

---

## 5. 顺带要清的文档债

模型改造会让这些文档失效：

| 文档 | 要改什么 |
|---|---|
| `docs/architecture.md` §4 角色模型 | 全部重写（5 个布尔 → 单一 role） |
| `docs/architecture.md` §5 三重约束 | 改为「域矩阵 + 角色 + 节点自限」 |
| `docs/acceptance-guide.md` | 「配了但不生效」表格几乎全废，重写 |
| `docs/protocol.md` | 卡片字段表、错误码清单 |
| `deploy/README.md` | 配置示例里的 `nodes:` / `policy:` 段 |
| `nexus-mesh-skill.md` | `whoami` 输出示例里的 `roles` |

---

## 6. 动手顺序

```
① 只读凭据补丁（纯增量，与模型无关）        ← 可立刻做，独立验证
② Step 1  配置层：types / roles
③ Step 2  AgentCard 换字段（判定不动）
④ Step 3  判定切到 role
⑤ Step 4  域矩阵去类型化
⑥ Step 5  删旧字段（含 mesh 退出节点表）
⑦ Step 6  paused + schema 校验
⑧ 文档债
⑨ 生产重启 + 重新批准 + 端到端验证
```

① 独立，先做掉。②–⑦ 连续改造，中间不停服务（本来也没在跑）。
⑧ 在代码定稿后做，避免文档反复改。⑨ 最后一次性上线。

---

## 7. 两个已定结论

**结论一 · `nodes:` 段整体取消**

`config.py:336` 原本注释说它还承担「登记远端节点以锁定其信任域」。
取消的理由：审批已绑定 `(agent_id, role, domain)` 三者，
节点自报的域必须是已声明的域之一 —— 审批时看得见，锁在配置里是冗余的。

**副作用（需知晓）**：少了一道"预先钉死某节点必须是某域"的防线。
若将来需要，用审批流程解决，不再回到配置锁。

配置里 `nodes:` 段删除，`config.py` 对应校验与 `nodes` 字段一并删。

**结论二 · `max_backlog` 缺省 `20`**

保护默认开启，放宽需要主动声明。

---

## 8. 文档先行（动手写代码之前必须完成）

> ⚠ **这条是硬要求，不是建议。**
>
> 如果文档没改完就写代码，代码会照着一份过时的设计去写；
> 等文档后来更新，两边就对不上——而那时已经很难判断
> "是代码错了还是文档错了"。

### 必须改完的文档清单

### 实测范围（2026-10-29 扫描）

对全仓库 markdown + yaml 扫描 18 个过时词汇，**排除测试产物**后：

| 文件 | 命中数 |
|---|---|
| `config/nxs.example.yaml` | 22 |
| `docs/acceptance-guide.md` | 17 |
| `docs/architecture.md` | 16 |
| `docs/design/01-system-definition.md` | 13 |
| `deploy/README.md` | 12 |
| `docs/agent-bridges.md` | 11 |
| `README.md` | 9 |
| `docs/protocol.md` | 9 |
| `nexus-mesh-skill.md` | 5 |
| `docs/artifact-size.md` | 4 |
| `docs/hermes-integration.md` | 3 |
| `CONTRIBUTING.md` | 2 |
| `traceability/MATRIX.md` | 1 |
| `deploy/local.yaml.example` | （与 nxs.example 同构） |

**合计约 130 处**（不含 06/07 两份设计文档——它们正文里引用旧模型是做对照，属于必要内容）。

### 三类处置

| 类别 | 文件 | 处置 |
|---|---|---|
| **必改** | 上表全部 | 按新模型重写 |
| **保留原样** | `CHANGELOG.md` | 历史记录，旧字段在当时的版本里是对的 |
| **保留原样** | `docs/docs-audit.md` · `docs/design/03-traceability-review.md` | 时间点报告，改了就失真 |

### 两类文档的更新时机不同

| 类别 | 例子 | 何时改 |
|---|---|---|
| **描述性文档** | `architecture.md` · `protocol.md` · `acceptance-guide.md` | **现在改**，它们只描述设计 |
| **可执行文档** | `config/*.yaml` · `deploy/local.yaml.example` | **Step 1 时改**，与 `config.py` 同步 |

第二类必须跟代码同步：示例配置是唯一能被 `nxs verify` 执行的部分，
先改格式后改解析器，会让 `make verify` 在中间阶段失败，
而这个失败**没有任何信息量**（只是代码还没跟上）。

### 机械化守护：让「文档先行」可验证

不靠自觉。加一条测试 `test_no_dead_vocabulary_in_docs`：

```python
DEAD_VOCAB = ["roles.issuer", "can_issue_types", "can_receive_from",
              "tasks_per_day", "local_node", "zone-work", ...]

# 尚未更新完的文件清单 —— 每改完一个就删掉一行
PENDING = {"docs/architecture.md", "docs/protocol.md", ...}

def test_no_dead_vocabulary_in_docs():
    for doc in ALL_DOCS:
        if doc in PENDING:
            continue                      # 待改，暂时放行
        assert not any(v in doc.text for v in DEAD_VOCAB)
```

**PENDING 清空 = 文档改完**，这比口头确认可靠。
且它在 CI 里会一直提醒，不会因为「这次先凑合」而无声腐烂。

### 进度（2026-10-29）

**14 / 16 已完成。** 描述性文档全部改完，只剩两份可执行配置：

| 文件 | 状态 |
|---|---|
| `docs/design/06` · `docs/design/07` | ✅ 本次新增 |
| `docs/architecture.md` | ✅ §4 角色模型重写、§5 三重→两重、"配了不生效"重写 |
| `docs/protocol.md` | ✅ 错误码表、检查顺序、`MX-RECEIVE-03` 拆分说明 |
| `docs/acceptance-guide.md` | ✅ §三重写、加"不要设计的场景" |
| `docs/design/01-system-definition.md` | ✅ 加导航、§7.2/7.3 重写 |
| `deploy/README.md` | ✅ 卡片示例、域名、排查表 |
| `docs/agent-bridges.md` | ✅ 三条控制、发布权、矩阵示例 |
| `README.md` · `nexus-mesh-skill.md` · `CONTRIBUTING.md` · `traceability/MATRIX.md` · `docs/artifact-size.md` · `docs/hermes-integration.md` | ✅ |
| `config/nxs.example.yaml` · `deploy/local.yaml.example` | ⬜ **Step 1 同步改** |

### 改文档过程发现的两个实质问题

**① `MX-RECEIVE-03` 不能简单合并**（重要）

我最初在 06 里写「并入 `MX-DOMAIN-02`」。写 protocol.md 时查了它的
5 个调用点，发现覆盖**三种不同语义**：

| 调用点 | 真实语义 | 正确归宿 |
|---|---|---|
| `policy.py` 接收白名单 | 域不可达 | `MX-DOMAIN-02` |
| `mailbox.py` / `registry.py` 未批准 | **准入问题** | 新增 `AU-APPROVE-05` |
| `gateway.py` / `task.py` 任务不属于你 | **任务归属** | `LF-TASK-404`（已存在，语义相同） |

第三项是白捡的简化：`gateway.py` 原注释说「不存在与不属于自己返回同一个
错误，避免枚举」，而 `LF-TASK-404` 的定义正是这个。

**如果没写文档就直接改代码，这个错误会被实现出来** —— 三个不相干的情况
被塞进一个错误码，排障时又得读代码才知道谁不许谁。

**② `docs/design/01` §7.4 的标题早就说了正确答案**

那节标题是「用角色代替点对点授权」，论证也对：

> 规则绑定在类型与角色上，**与节点数无关**

但紧接着的实现（7.3 卡片示例）却是逐项声明 `can_issue_types` /
`can_receive_from` / `quota`。**结论和实现自相矛盾，而且这个矛盾存在了很久。**

本次改造实际上是**让实现追上那份文档两年前的结论**，而不是引入新思路。

### 过渡期的测试状态

改了 4 个现存守护测试，让它们能区分"预期中的过渡"和"真错"：

| 测试 | 处理 |
|---|---|
| `test_unimplemented_list_matches_docs` | `TRANSITION_DOCS_AHEAD = True` 时 skip |
| `test_dead_config_list_matches_unimplemented` | skip，Step 5 后恢复 |
| `test_documented_error_codes_exist` | 双向检查：新增码加白名单、**待删码也列出来**（忘了删会一直挂着） |
| `test_all_tests_declare_covers` | 新文件补了 `@covers`（我的疏漏） |

当前状态：**792 passed, 2 skipped**。

### 改文档时的自查

每份文档改完，过这三个问题：

1. **还能搜到这些词吗？** `roles.issuer` / `can_issue_types` / `can_receive_from` /
   `quota.tasks_per_day` / `local_node` / `zone-work`（后者改为 `work`）
2. **「配了但不生效」清单**里提到的项，是否已从文档中消失？
3. **示例配置**能不能按新格式通过 `nxs verify`？

第 3 条是关键：**示例配置是文档里唯一可执行的部分**，
它错了比任何描述都更容易误导人。
