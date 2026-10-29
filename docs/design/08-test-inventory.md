# 测试用例与权限模型的对应关系

> 配套：`docs/design/06-agent-role-model.md`（目标模型）
> `docs/design/07-permission-model-implementation.md`（实施顺序）
>
> 本文回答一个问题：**哪些测试会在改造中失败，为什么，该怎么处理。**
>
> 日期：2026-10-29

---

## 为什么需要这份清单

用户在动手前提出：

> 「790 多个用例都是基于之前旧版本设置的，如果不清理一下会导致后续修改
> 过程中出现各种用例不通过，但其实是用例已经过时了。如果没注意的话
> 可能会导致把正确的代码或者文档改回旧的版本。」

**这个风险是真实的，而且比"测试多跑几分钟"严重得多。**

它的机制是这样的：

```
你按新模型改对了代码
  → 旧模型绑定的测试报红
  → 你以为自己改错了
  → 把正确代码改回旧行为
```

**测试不是中立的**。当测试本身编码了过时设计时，它会主动把你推向错误方向。
这种失败在日志里看起来和"真 bug"一模一样。

所以本文把每个受影响的测试标注清楚：**它为什么红，以及该改哪边。**

---

## 分类总览

| 类别 | 含义 | 处理 | 数量 |
|---|---|---|---|
| **A · 已删除** | 测的能力整体不存在了 | 本轮已删 | 25 用例 |
| **B · 待重写** | 能力还在，但断言基于旧字段 | 在各 Step 中重写 | 见下 |
| **C · 需改 helper** | 一个 `make_card()` 牵连整个文件 | 各 Step 改一处即可 | 见下 |
| **D · 完全不受影响** | 不碰权限模型 | 不动 | 约 600 |

---

## A · 本轮已删除（25 个用例）

### 整文件删除

| 文件 | 用例 | 删除理由 |
|---|---|---|
| `test_local_node.py` | 9 | `local_node` 概念整体移除（mesh 不再占节点名额） |
| `test_quota_integration.py` | 7 | 配额整体移除（归节点自律） |

**删除前逐条确认过：它们保护的性质在别处仍有覆盖**

| 原测试 | 保护的性质 | 现在由谁保护 |
|---|---|---|
| `test_local_node_cannot_be_ops_identity` | `__ops__` 不能是真实节点 ID | `test_ops.py::test_ops_node_id_cannot_be_taken_by_a_real_node` |
| `test_sweep_marks_genuinely_dead_remote_node` | 死节点必须判 stale | `test_runtime.py::test_stale_then_resurrect` |
| `test_verify_warns_when_local_node_missing` | 缺失配置要警告 | 随字段一起消失，无对应需求 |

### 删除的测试类

| 文件 | 类 | 用例 | 理由 |
|---|---|---|---|
| `test_policy.py` | `TestQuota` | 4 | 配额校验（`check_quota`）整体移除 |
| `test_policy.py` | `TestRoleFieldsAreLabelsNotGates` | 3 | 它专门测"哪个 roles 标志是闸门"—— 标志本身消失了 |

> ⚠ **`TestRoleFieldsAreLabelsNotGates` 值得单独说。**
>
> 这个类做的事是：验证 `roles.executor` **不**构成准入闸门，
> 而 `roles.issuer` **是**闸门。
>
> 一个需要专门写测试去锁定「某个字段不生效」的字段，本身就是设计问题。
> 新模型里没有这个字段，也就不需要这个测试。
>
> **这不是"删掉不方便的测试"，是"删掉为错误设计辩护的测试"。**

### 一处删除后被我恢复的（记录过程，避免重犯）

`TestCapabilityDiscovery`（2 用例）我一开始也删了，但当场收回：

**`providers_of` 不会消失。** 「谁能为某个类型服务」在新模型下依然要回答
（自动匹配靠它），变的只是信息来源：

```
旧：从每个节点各自声明的 capabilities 汇总
新：从节点的 role 推导
```

追溯表立刻抓到了这个错误（`M5.F9` 失去覆盖）。**这是追溯机制的价值**：
删测试不是静默的，丢覆盖会被拦住。

---

## B · 待重写的测试（能力还在，断言过时）

### `test_policy.py` — `TestPublishPermission`（3 用例）

| | |
|---|---|
| 现在测 | `roles.issuer=false` → 不能发布；`can_issue_types` 不含类型 → 不能发布 |
| 新模型 | `check_publish()` 只读 `role.can_issue`，不再逐类型比对 |
| 怎么改 | Step 3：断言改为「role 是否允许发布」 |

### `test_policy.py` — `TestTripleCeiling`（3 用例）

| | |
|---|---|
| 现在测 | 上限 = min(域矩阵, 节点约束, 能力声明) 三者 |
| 新模型 | 上限 = min(role 上限, 节点自限) 两者 |
| 怎么改 | Step 4：去掉矩阵那一路，保留"多方取最小"的性质断言 |

> ⚠ **这条必须改对，不能简单删。** "多重独立来源、各自只能收紧"
> 是防止单点配错越界的唯一保障。从三重变两重，性质本身要留下。

### `test_policy.py` — `TestSecurityAssertion3_DefaultDeny`（7 用例）

| | |
|---|---|
| 现在测 | 跨域拒绝的判定链，含 `can_receive_from` 白名单 |
| 新模型 | 拒绝仍靠域矩阵，但矩阵不再列类型；`can_receive_from` 消失 |
| 怎么改 | Step 4：保留"默认拒绝"的核心断言，去掉白名单那部分 |

**`test_zone_b_to_coordinator_blocked_by_receiver_not_matrix` 这条要删** ——
它测的是"接收方白名单比矩阵更严"，新模型里没有接收方白名单这一层了。

### `test_errorcodes` 相关（散落）

| 旧错误码 | 新归属 | 影响的测试 |
|---|---|---|
| `MX-RECEIVE-03` | 拆三份 | `test_observ.py` 1 处、`test_codebuddy_node.py` 2 处 |
| `QT-CONC-05` / `QT-TASK-01` | 删除 | `test_docs_consistency.py` 的清单核对 |
| `MX-DELEGATE-04` | 删除 | 同上 |

### `test_architecture.py` — `test_unimplemented_list_matches_docs`

已在 `TRANSITION_DOCS_AHEAD` 下 skip，Step 5 完成后改回 `False`。

### `test_docs_consistency.py` — `test_dead_config_list_matches_unimplemented`

已 skip，Step 5 后恢复。

---

## C · 需要先改 helper 的文件

这些文件的**大部分用例不受影响**，但共享的 `make_card()` / `build_card()`
会构造旧字段。改一处即可让整个文件的"卡片构造"跟上新模型。

| 文件 | helper | 用例总数 | 受影响 |
|---|---|---|---|
| `test_ops.py` | `make_card()` | 21 | 2 块（helper + `TestOpsWorks`） |
| `test_hermes_node.py` | `TestRoleCard` 内 | 35 | 仅 `TestRoleCard`（6） |
| `test_openclaw_node.py` | `TestRoleCard` 内 | 25 | 仅 `TestRoleCard`（6） |
| `test_codebuddy_node.py` | `TestRoleCard` 内 | 70 | 仅 `TestRoleCard`（5） |
| `test_e2e.py` | `work_card()` | 11 | 1 块 |
| `test_registration.py` | `make_card()` | 18 | 1 块 |
| `test_matcher.py` | fixture `card` | 31 | 3 块 |
| `test_core.py` | `TestDefaultsAreDeny` | 41 | 仅该类（4） |
| `test_sqlite_backend.py` | — | 12 | 2 处断言 |
| `test_local_boundary.py` | — | 18 | 1 处 |

### 三个桥接的 `TestRoleCard` 都测什么

它们的断言结构几乎一样，都在验证卡片生成的确定性：

| 断言 | 新模型下的对应 |
|---|---|
| `card.node_id == args.node_id` | 保留 |
| `"zone-work" in card.constraints.can_receive_from` | → 改为 `card.domain == "work"` |
| `card.roles.executor is True` | → 改为 `card.role == "worker"` |
| 卡片确定性（两次生成相同） | **保留** —— 这是审批绑定的前提 |

**"卡片必须确定性生成"这条要完整保留**，它在任何模型下都成立。

---

## D · 完全不受影响的（约 600 用例）

这些不碰权限模型，改造**不应**让它们变红。若它们红了，说明改坏了。

| 文件 | 用例 | 覆盖什么 |
|---|---|---|
| `test_deploy_scripts.py` | 89 | shell/PowerShell 脚本静态检查 |
| `test_runtime.py` | 45 | 任务生命周期、租约、stale |
| `test_ui.py` | 43 | 观察窗页面 |
| `test_observ.py` | 42/43 | 指标与异常检测（1 处涉错误码） |
| `test_gateway.py` | 32 | 网关端点 |
| `test_gateway_attack.py` | 23 | 攻击面 |
| `test_cli_actor.py` | 24 | nxs-cli 身份 |
| `test_deploy.py` | 24 | doctor / verify |
| `test_traceability.py` | 26 | 追溯表一致性 |
| `test_core.py` | 37/41 | 信封签名、状态机、自报文本清洗 |
| `test_lease.py` | 11 | 租约 |
| `test_skill_doc.py` | 8 | skill 文档格式 |

> **这一栏是"安全带"**：改造过程中如果这些红了，
> 不要怀疑它们过时 —— **是代码被改坏了。**

---

## 改造各 Step 的测试动作

| Step | 要动的测试 |
|---|---|
| 1 | 无（只加配置解析） |
| 2 | 三个桥接的 `TestRoleCard`、各 `make_card()` helper |
| 3 | `TestPublishPermission`、`test_policy.py` 的 publish 相关 |
| 4 | `TestTripleCeiling`、`TestSecurityAssertion3`、错误码断言 |
| 5 | 删 `QT-CONC-05` 等断言、`TRANSITION_DOCS_AHEAD` 改回 False |
| 6 | 新增 `paused` 与 schema 校验的测试 |

---

## 一条建议：红之前先看这份清单

改造过程中遇到测试失败，**先查这份清单再改代码**。

判断顺序：

1. 它在 D 类吗？→ **代码被改坏了**，去查代码
2. 它在 A/B/C 类吗？→ **测试过时了**，改测试
3. 都不在？→ 可能是真问题，也可能是本清单漏了某处，**补进本文档**

第 3 条很重要：清单漏项说明我们对影响面的认识还不完整，
那本身就是要记下来的信息。
