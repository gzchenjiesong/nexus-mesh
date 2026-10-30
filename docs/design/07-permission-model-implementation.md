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

### Step 2 · AgentCard 换字段（判定仍走旧路径）✅ 已完成（2026-10-29）

> **完成情况**：`RoleCard` 新增 6 个字段，三个桥接的卡片已声明，
> `derived_role()` / `effective_max_level()` / `effective_capacity()`
> 三个派生方法就位。**判定逻辑仍未改** —— `make demo` / `demo-net`
> 九步全过，`verify` 19 项全过。

**改**

- `AgentCard` 新增：`role: str`、`self_limits{max_backlog, max_response_level}`、`tools`、`state`
- 旧的 `roles` / `constraints.can_issue_types` / `can_receive_from` / `quota` **仍在**，标记为待删
- 加推导函数：`roles{issuer,executor} → role`（过渡期辅助，Step 5 一起删）

**这步不动判定逻辑。** 目的是先立数据结构，让后面两步有地方落。

**判据（本计划最有价值的一条测试）**

参数化覆盖四种组合，断言「旧判定」与「role 推导」结论一致：

```
(issuer, executor) →  推导 role
(True,  True )     →  driver
(False, True )     →  worker
(True,  False)     →  publisher
(False, False)     →  ""（无角色，既不能发也不能被执行）
```

这条测试的作用不是"证明迁移安全"（没有迁移了），而是**在你动判定逻辑之前，
确认自己对旧行为的理解是对的**。它是一面镜子。

**实际产出**

| 内容 | 位置 |
|---|---|
| `SelfLimits`（节点自律限额） | `nxs/core/models.py` |
| `RoleCard` 新增 6 字段 | `role` / `may_broadcast` / `self_limits` / `capacity` / `tools` / `state` |
| `derived_role()` | 旧布尔 → 角色推导，**过渡期辅助，Step 5 删** |
| `role_is_consistent()` | 声明的 role 与推导值对账 |
| `effective_max_level()` | min(旧约束, 自限) —— 保住"多方取最小"性质 |
| `effective_capacity()` | 未声明时退回 `quota.concurrent_tasks`，旧卡片零影响 |
| 三个桥接卡片 | `hermes` / `openclaw` / `codebuddy` 均声明新字段 |
| 测试 35 条 | `tests/test_agent_card_step2.py` |

**每个新字段都有真实读取点**（避免重蹈 `can_issue` 的覆辙）

| 字段 | 读取点 |
|---|---|
| `role` | `to_public_dict` 暴露 + `derived_role` 对账 |
| `may_broadcast` | `to_public_dict` |
| `self_limits.max_response_level` | `effective_max_level()` |
| `self_limits.max_backlog` | ⚠ 暂登记 UNIMPLEMENTED（Step 6 接线） |
| `capacity` | `effective_capacity()` → 观测层算负载率 |
| `tools` | `to_public_dict` —— 让其他 Agent 知道它能做什么 |
| `state` | `to_public_dict` |

**⚠⚠ 一个被显式暴露、但**没有**擅自决定的策略问题**

`work-desktop`（CodeBuddy）旧卡片是 `issuer=True`，等价于 `driver`
——**它能主动给云端 Agent 派任务**。

而权限模型 review 时，它正是"被批出发布权却没人回头看过"的典型。

Step 2 **只记录现状**（`role: driver`，与旧行为一致），没有改它。
注释里写清了两个选项：

| 选 | 含义 |
|---|---|
| `driver` | 保留"主动问云端口径"的能力，但能发的范围要收窄 |
| `worker` | 纯执行者，不能主动发起任何任务 |

**这是策略决定，不是技术改造步骤。** 若改为 worker，
`role_is_consistent()` 会立刻报不一致 —— 那正是我们要的信号。

---

### Step 3 · 判定切到 role ✅ 已完成（2026-10-29）

**改**

- `check_publish()` → 读 `role.can_issue`
- 新增 `PolicyEngine.role_of(card)` —— 卡片 → 角色的解析
- `MX-ISSUER-01` 的 message 改成「role X cannot issue」

**⚠ 一处与原计划的偏离（有意为之）**

原计划写了「删 `providers_of` / `registered_types`」。**没有在 Step 3 做，挪到了 Step 4。**

理由：这两个是**类型路由**的基础设施，而 Step 3 只切「发布权」。
如果在 Step 3 就删掉，「目标节点是否提供该能力」这个检查会失去依据 ——
那会让 `WL-TYPE-404` 失效，属于**权限漏洞**，不是简化。

正确的顺序是：Step 4 让类型真正由配置全局定义、`providers_of`
改成从 `role.serves` 计算，那时才能谈删除。

**核心行为变化（这是设计的必然结果，不是意外）**

| 场景 | Step 2 之前 | Step 3 之后 |
|---|---|---|
| driver 发**已定义**类型 | ✓ | ✓ |
| driver 发**未定义**类型 | ✗ `MX-ISSUER-01` | ✓ 过发布权检查 → ✗ 在 `check_delivery` 报 `WL-TYPE-404` |
| worker 发任何类型 | ✗ `MX-ISSUER-01` | ✗ `MX-ISSUER-01` |

**关键点：未知类型的拦截点从"发布权"移到了"投递检查"。**
保护没丢 —— 未定义的类型依然发不出去，只是理由更准确：
不是「你没权限」，而是「这个类型不存在」。

**实测确认**

```
coordinator (driver)  发 other.thing  → 发布权通过
node-a      (worker)  发 demo.echo    → MX-ISSUER-01
coordinator → node-a  other.thing     → WL-TYPE-404
coordinator → node-a  demo.echo       → 放行，上限=METADATA
```

**判据**

- `work-desktop` 类节点（worker）尝试发布 → 仍被拒，`MX-ISSUER-01`
- 发布权检查与类型无关（新增专项测试）
- 未定义类型仍在投递层被拦（新增专项测试）
- 反向验证：强制 `role_of` 返回 None（退回旧路径）→ 2 条测试 FAILED

**测试改动**

原 `test_coordinator_cannot_publish_outside_its_allowlist` **被改写而非删除** ——
它保护的性质（不能发任意类型）仍然重要，只是检查点变了，所以拆成两条：
一条验"发布权只看角色"，一条验"未知类型在投递层被拦"。

---

### Step 4 · 域矩阵去类型化 ✅ 主体完成（2026-10-29）

**改**

```yaml
# 前
- from_domain: cloud
  to_domain: zone-work
  allow_types: [work.calendar, work.summary, work.query, work.*]
  max_level: summary

# 后
- from_domain: cloud
  to_domain: zone-work
```

- `MatrixRule` 删 `allow_types` / `max_level` —— 只表达可达性
- `providers_of` / `registered_types` 改为从 `role.serves` 与 `types` 计算
- 删 `can_receive_from` 检查（"谁能给我派活"归运维矩阵，不归节点自报）
- 上限从「三重」变「两重」：`min(role, 节点自限, 能力声明)`
- **`MX-RECEIVE-03` 拆成三份并删除**

**这是唯一真正放宽的一步。** 单独做，是为了出问题时归因清晰。

**错误码拆分结果**

| 原语义 | 新归属 |
|---|---|
| 域不可达 | `MX-DOMAIN-02` |
| 未批准 / 不可路由 | **新增** `AU-APPROVE-05` |
| 任务不属于调用方 | 并入已有的 `LF-TASK-404` |

第三项是白捡的简化：`gateway.py` 原注释写「不存在与不属于自己返回同一个
错误，避免枚举」，而 `LF-TASK-404` 的定义正是这个 —— 本来就是一件事。

**⚠ 实际改动中踩到的一个真 bug（值得记录）**

`check_response()` 里还有一处 `min(rule.max_level, ...)` 我在第一次改
`check_delivery` 时漏掉了。症状是 `test_can_run_core_without_any_backend`
（安全带类）报 `AttributeError: MatrixRule has no attribute max_level`。

**这正是安全带测试的价值**：它不在权限模型的影响面内，
一旦红了就说明代码真坏了，而不是测试过时。

**⚠ 另一个自己造成的坑**

`SelfLimits.max_response_level` 初版默认写成 `SUMMARY`，结果所有未声明
自限的节点上限都被压到摘要级 —— 包括本来就是 fragment 的协调节点。
症状 `WL-LEVEL-002`，看起来像"role 配错了"。

已改为默认 `FULL_TEXT`：**自限是节点主动收紧，没声明就是没收紧。**

**配置改动**

| 文件 | 改了什么 |
|---|---|
| `config/nxs.example.yaml` | 矩阵去类型化；角色 `serves` 精确化；driver 上限改 fragment |
| `deploy/local.yaml.example` | 矩阵去类型化（两处）|

角色 `serves` 为什么精确化：初版都写 `["demo.*"]`，导致所有节点都能服务
所有 demo 类型，`providers_of` 失去区分度，能力匹配的演示价值就没了。

**测试改动（全部是改写，不是删除）**

| 原测试 | 改成 |
|---|---|
| `test_declaring_domains_relaxes_static_type_check` | `test_template_matrix_has_no_type_lists` |
| `test_wildcard_does_not_over_match` | 对象从矩阵 `allow_types` 换成 `RoleDecl.serves` |
| `test_zone_b_to_coordinator_blocked_by_receiver_not_matrix` | `test_zone_b_to_coordinator_is_reachable` + 一条反面测试 |
| `test_providers_are_config_driven` | 期望值更新（角色边界确实起作用）|
| 5 处错误码断言 | `AU-APPROVE-05` / `LF-TASK-404` |

**反向验证**

- 让 `providers_of` 强制走旧路径 → 对应测试 FAILED
- 让矩阵永远放行 → `TestSecurityAssertion3_DefaultDeny` 5 条 FAILED

**未做：广播展开**

原计划 Step 4 含「广播按域展开」。**已拆成独立一步（Step 4b）**，理由：

1. 它是**新功能**，不是改造 —— 当前没有任何调用方
2. 它需要设计「广播的 N 个结果怎么返回给调用方」（task group），
   那是协议层的设计，不是本轮改动
3. 混在 Step 4 里会让"矩阵去类型化"这个已经放宽的改动难以归因

`TypeDecl.delivery` 仍登记在 `UNIMPLEMENTED`，等 Step 4b 接线。

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

### Step 5b · 删配额 + 信箱深度 ✅ 已完成（2026-10-29）

**删了什么**

| 删除 | 位置 |
|---|---|
| `Quota` 类（4 字段） | `nxs/core/models.py` |
| `Constraints.quota` | 同上 |
| `Usage` 类（用量快照） | `nxs/control/policy.py` |
| `check_quota()` | 同上 —— 换成 `check_backlog()` |
| `app.usage_of()` | 换成 `app.backlog_of()` |
| 4 个配额错误码 | `QT-TASK-01` / `QT-BYTE-02` / `QT-BYTE-03` / `QT-CONC-05` |

**加了一样东西：`QT-BACKLOG-07`**

它是**唯一真正会执行的**限额。语义说清：

```
MX-*           → "你没权限"   → 发布方去求运维改配置
QT-BACKLOG-07  → "它来不及干" → 发布方稍后重试或换节点
```

**错误码：27 → 24**
（删 4 个配额码、删 `MX-RECEIVE-03`、加 `AU-APPROVE-05` 与 `QT-BACKLOG-07`）

**保留两项 Mesh 自我保护**

`QT-RATE-04`（请求速率）与 `QT-CONN-06`（连接数）**刻意不动**。
它们与节点配额是两回事：

| | 问的问题 | 归谁 |
|---|---|---|
| 节点配额 | 你还能不能干活 | 节点自己 |
| Mesh 自保 | 网络会不会被你拖垮 | 必须由 Mesh 强制 |

删配额时很容易顺手把这两个也删掉 —— 那会让单个节点打爆网关。

**⚠ 改动中漏了三处，都是测试抓到的**

| 漏的地方 | 症状 |
|---|---|
| `matcher.py:184` 读 `card.constraints.quota` | 自动匹配时崩溃 |
| `gateway.py` whoami 端点调 `usage_of()` | HTTP 500（RemoteDisconnected）|
| `observ.py` 注释仍写"退回 quota" | 只是过时注释，不影响行为 |

前两个是**真 bug** —— 说明"删字段"这个动作必须配合全库搜索，
不能只改自己记得的那几处。

**测试：新增 `tests/test_backlog.py`（9 条）**

其中三条是这次改造最有价值的守护：

| 测试 | 保护什么 |
|---|---|
| `test_at_limit_rejected` | 边界是 `>=` 不是 `>` —— 写成 `>` 会多攒一个 |
| `test_uses_qt_backlog_not_mx` | 错误码必须是 `QT-*`，用 `MX-*` 会让发布方以为要审批 |
| `test_mesh_self_protection_survives` | **反向**：删配额别误伤自保两项 |

**双向验证三项**

```
边界改成 > limit        → 4 条 FAILED
错误码改回 MX-*         → 2 条 FAILED
整个检查变空操作        → 5 条 FAILED
```

**⚠ 一条测试我写反了，改的是测试不是代码**

我最初写 `test_message_includes_counts`，要求错误响应里带 `pending=3/3`。
但 `to_wire()` 的注释写明：**只含 rule_id 与固定消息** —— 不泄露内部状态，
否则错误响应变成探测通道。

已改为 `test_wire_message_does_not_leak_counts`：验证对外不带数字，
而 `detail`（仅本地日志）带数字。

**验收演练的性质反转**

`examples/acceptance_drill.py` 第三章原本是「验证 `bytes_per_response`
**确实不生效**」，现在改成「验证 `max_backlog` **确实生效**」。

这个反转就是改造的核心成果：从"配了但不生效"变成"配了就真会执行"。

**`UNIMPLEMENTED` 清单：12 → 6**

删掉已过期的 6 项：`bytes_per_response` / `bytes_per_day`（配置已删）、
`RoleDecl.can_issue`（Step 3 接线）、`RoleDecl.serves`（Step 4 接线）、
`TypeDecl.response_fields` / `max_level`（Step 4 接线）。

---

### Step 5c · 删旧权限字段 ✅ 已完成（2026-10-29）

**删了两个类**

| 删除 | 原因 |
|---|---|
| `Roles`（5 个布尔） | 只有 `issuer` 参与过判定，其余 4 个从未被读取；`issuer` 的语义也被 `role.can_issue` 取代 |
| `Constraints`（4 个字段） | 前三个是"节点自己声明权限"（旧思路），第四个与 `self_limits` 完全重复 |

**四个字段的归宿**

```
can_issue_types     → role.can_issue（角色整体决定能否发布）
can_receive_from    → 域矩阵（运维定义围栏，不归节点自报）
can_delegate_to     → 删除（不做二次委托）
max_response_level  → self_limits.max_response_level（同一语义，合并）
```

**`role` 改为必填**

留一个"不写就当 worker"的默认会让「忘记声明」与「故意选 worker」
看起来一样，而审批时看不出区别。所以必填，错误信息指向 `roles:` 段。

**新增两道启动校验**

| 校验 | 为什么 |
|---|---|
| 有节点就必须有 `roles:` 段 | 没有角色表，`role` 无处可查 → 症状是"注册成功但什么都干不了" |
| 节点的 `role` 必须在角色表里 | 拼错名字不该被静默接受 |

**`ops nodes` 输出改为一句话说完**

原来给 5 个布尔 + 两串白名单，运维要自己脑内组合"它到底能干什么"。
现在给 `role` + `role_definition`（该角色在配置里的定义）+ `may_broadcast`：

```
旧：role 名要查配置才知道能不能发布
新：role: worker, role_definition: {can_issue: false, serves: [...]}
```

**`matcher` 删掉一个预筛条件**

`can_receive_from` 的通配匹配删了。"能不能到达"由域矩阵判，
而这个模块**刻意不知道矩阵存在**（见 `select()` 的说明）。
代价是自动匹配时可能先试到"能力够但域不可达"的节点、多一次校验；
收益是职责边界干净。

**错误码 24 → 23**（删 `MX-DELEGATE-04`）

**`UNIMPLEMENTED`：6 → 2**

删掉 4 项 `roles.*`（字段本身已不存在）。只剩：
`params_schema`（Step 6 接线）、`TypeDecl.delivery`（广播未做）。

**⚠ 测试改动很大，两类都记下来**

**删除的（测的机制整个不存在了）**

| 文件 | 删了什么 |
|---|---|
| `test_agent_card_step2.py` | **整文件重写为 `test_agent_card.py`** —— 里面两组测试分别测 `derived_role()` 和"新字段不影响旧字段"，两者都不存在了 |
| `test_hermes_node.py` / `test_openclaw_node.py` / `test_codebuddy_node.py` | 各删 1–2 条测 `roles` / `can_receive_from` 的用例 |
| `test_matcher.py` | 删 2 条测 `can_receive_from` 预筛的用例 |

**改写为"防回归"的（性质重要，但对象变了）**

| 原测试 | 改成 |
|---|---|
| `test_roles_default_false` | `test_role_is_mandatory_not_defaulted`（比旧版更严） |
| `test_constraints_default_empty` | `test_role_decl_defaults_deny` |
| `test_second_hop_delegation_denied` | `test_second_hop_delegation_is_gone` |
| `test_constraints_has_no_quota_field` | `test_constraints_class_is_gone` |
| `TestBackwardCompatibility`（整个类） | `TestRoleTableIsRequired` |

**新增 `TestOldFieldsAreGone`（5 条）**

防止有人把 `Roles` / `Constraints` / `derived_role()` 加回来。
它们的共同历史是：**定义了、解析了、写进示例配置、然后从未被读取。**

**双向验证**

- 把 `Roles` 类加回来 → `TestOldFieldsAreGone` 1 条 FAILED
- 让 `role` 变回可选 → `TestRoleIsMandatory` 3 条 FAILED

**顺带恢复文档一致性检查**

`TRANSITION_DOCS_AHEAD` 改回 `False`（文档改造期设的临时豁免），
让「代码的 `UNIMPLEMENTED` 必须与文档一致」重新生效。
它立刻抓到一处遗漏：`TypeDecl.delivery` 没写进 `architecture.md`。

---

### Step 6 · `paused` 状态 + `params_schema` 校验 ✅ 已完成（2026-10-30）

这两项都是**配置里写着、但一直没接线**的功能。它们的共同前提是
类型由 Mesh 定义一次 —— 所以"这个类型的参数长什么样"才有唯一答案。

**① `params_schema` 真正校验**

新增 `PolicyEngine.check_params()`，接在投递链路的 **①b** 位置：

```
① 发布方 role 允许发布        MX-ISSUER-01
①b 参数符合 params_schema     FM-PARAM-02   ← 新增
② 目标节点可路由              AU-*
③ 域间矩阵                   MX-DOMAIN-02
③b 目标没有自报 paused        QT-PAUSED-08  ← 新增
④ 应答上限 = min(三重)
⑤ 信箱还有余量                QT-BACKLOG-07
```

**位置是刻意的**：放在发布权限**之后**、目标确定**之前**。

* 放在权限之前 → 没发布权的人也能靠参数错误探测 schema
* 放在目标之后 → 自动匹配可能先挑到节点再发现参数不对，白跑一趟

校验三条：必需参数齐全 / 类型正确 / **未声明参数一律拒绝**。

第三条是刻意的严格 —— 未声明参数是提示注入的现成载体。

**② 类型名白名单**

踩到的实例：示例配置一处写 `"int"`、另一处写 `"integer"`，
两种写法并存。**类型名拼错的后果是校验永远失败或永远通过，
两种都是坑，而且从配置上完全看不出来。**

新增 `PARAM_TYPES` 白名单（string/integer/number/boolean/array/object），
加载时校验。示例配置的 `"int"` 一并改成 `"number"`（demo.slow 的 seconds
本来就该是 number）。

**③ `paused` 参与投递判定**

两处接线，语义不同：

| 位置 | 行为 | 理由 |
|---|---|---|
| `check_delivery` | 拒绝，`QT-PAUSED-08` | 显式指定了目标，它不接就该报错 |
| `matcher.candidates` | 跳过 | 自动匹配时"有别的能干"和"只能给它但它不接"是两回事 |

**⚠ 错误码的选择与文档不一致，我改了文档并说明理由**

`protocol.md` 早期版本写的是这一条用 `AU-APPROVE-05`。实现时改成新码
`QT-PAUSED-08`，因为两个码回答的问题不同：

| 码 | 回答的问题 | 节点的处境 |
|---|---|---|
| `AU-APPROVE-05` | 它**有没有资格**在这个网络里 | 未批准 / 已吊销 / 不可路由 |
| `QT-PAUSED-08` | 它**现在接不接单** | 已批准、活着，但主动声明暂停 |

paused 的节点在网络上完全合法，只是暂时不接活。用准入错误码会让
排障者去查批准记录 —— 而那一切正常。

**错误码 23 → 24**（新增 `QT-PAUSED-08`）
**`UNIMPLEMENTED` 2 → 1**（只剩 `TypeDecl.delivery`，广播按你的要求延后）

**⚠ 顺带修了一个我自己留下的 bug**

前面几步用正则删代码时，把 `errors.py` 里两处换行吃掉了：
`WL_FREETEXT_NOT_ALLOWED` 的下一行直接贴在 `QT_BACKLOG_FULL` 的注释上。
语法能过（都是注释），但可读性被破坏。已修复并重排了 QT 组的分组说明。

**新增 `tests/test_params_and_paused.py`（18 条）**

重点几条：

| 测试 | 保护什么 |
|---|---|
| `test_undeclared_param_rejected` | 夹带防护 —— 本步最重要的性质 |
| `test_bool_does_not_pass_integer` | Python 里 `True` 是 `int` 子类，必须显式排除 |
| `test_uses_qt_not_au` | paused 必须用 QT-*，用 AU-* 会让排障方向跑偏 |
| `test_extra_param_rejection_does_not_name_it` | 拒绝时不报参数名（防探测） |
| `test_node_cannot_self_report_stale` | 自报态与观测态必须分开 |

**双向验证四项**

```
check_params 置空        → 5 条 FAILED
去掉 bool 排除           → 1 条 FAILED
paused 检查摘掉          → 2 条 FAILED
matcher 的 paused 过滤摘掉 → 1 条 FAILED
```

**demo 从九步变十步**

新增第 7 步「参数校验 + 暂停状态」，把三条拒绝实跑给出来：

```
[7/10] ★ 安全断言④  参数校验 + 暂停状态
       REJECTED  FM-PARAM-02   demo.count 要 n: integer，给了 string
       REJECTED  FM-PARAM-02   夹带 note 参数 → 未声明参数一律拒绝
       REJECTED  QT-PAUSED-08  node-a 自报 paused → 活着但主动不接单
```

改步数带出四处同步：`demo.py` 模块文档、README、Makefile、`test_gateway.py`。
其中 Makefile 那处是**已有的守护测试主动抓到的**（"同一个事实写在两处"）。

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
