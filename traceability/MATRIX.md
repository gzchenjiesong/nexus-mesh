# 追溯矩阵

> **本文件由 `tests/test_traceability.py` 自动生成，请勿手工编辑。**
> 修改编号请改 `traceability/registry.py`，修改覆盖请给测试加 `@covers`。

共 **283** 个测试方法（已声明覆盖）· **102** 个纵向编号项（81 功能点 + 21 模块需求）

## 设计目标

| 编号 | 目标 | 派生模块 |
|---|---|---|
| **G1** | 让异构 Agent 能协作 | M3, M4, M6, M7, M8, M9 |
| **G2** | 协调节点被攻破也不泄露原始数据 | M1, M5, M10 |
| **G3** | 每次协作可校验可追溯 | M5, M12 |
| **G4** | 与任何具体 Agent 解耦 | M2, M11 |
| **G5** | 节点集合变化不需要改代码 | M3, M11 |

## 功能点与需求

### M1 网关与认证

平面 `接入层` · 实现 `nxs/edge/auth.py · gateway.py · httpsig.py · client.py` · 状态 **已实现**

| 编号 | 名称／约束 | 实现位置 | G | P | S | 覆盖测试 |
|---|---|---|---|---|---|---|
| **M1.F1** | 信封签名生成 | `nxs/core/envelope.py:Envelope.sign/compute_signature` | G2 | P0 | S4 | 1 |
| **M1.F2** | 签名验签（常数时间比较） | `nxs/core/envelope.py:Envelope.verify` | G2 | P0 | S4 | 6 |
| **M1.F3** | 时间窗校验（防重放） | `nxs/core/envelope.py:Envelope.verify（drift 判定）` | G2 | P0 | S4 | 4 |
| **M1.F4** | 规范 JSON 序列化（签名确定性） | `nxs/core/envelope.py:canonical_bytes` | G2 | P0 | — | 1 |
| **M1.F5** | nonce 去重防重放（有界缓存） | `nxs/edge/auth.py:Authenticator._check_nonce` | G2 | P0 | S4 | 4 |
| **M1.F6** | 凭据管理（轮换／吊销即时生效） | `nxs/edge/auth.py:Authenticator.set_secret/revoke` | G2 | P0 | S10 | 1 |
| **M1.F7** | 成员认证编排（四步校验顺序） | `nxs/edge/auth.py:Authenticator.verify` | G2 | P0 | S4 | 2 |
| **M1.F8** | 信封格式校验（版本与结构） | `nxs/core/envelope.py:Envelope.from_dict` | G1,G2 | P0 | — | 1 |
| **M1.F9** | 请求签名生成（方法+路径+节点+时间+nonce+体摘要） | `nxs/edge/httpsig.py:sign_request/compute_signature` | G2 | P0 | — | 4 |
| **M1.F10** | 请求签名校验（与信封共用 nonce 缓存与吊销表） | `nxs/edge/auth.py:Authenticator.verify_request` | G2 | P0 | S4 | 8 |
| **M1.F11** | HTTP 端点分发与规则→状态码映射 | `nxs/edge/gateway.py:_Handler._dispatch/status_for` | G1,G2 | P0 | S11 | 18 |
| **M1.F12** | 请求体上限（读入前按 Content-Length 判定） | `nxs/edge/gateway.py:_Handler._read_body` | G2 | P1 | — | 3 |
| **M1.F13** | 并发与流配额（短请求与 SSE 分别计） | `nxs/edge/gateway.py:Gateway.acquire_request_slot/acquire_sse_slot` | G2 | P1 | — | 3 |
| **M1.F14** | SSE 通知流（仅通知不含数据 · 心跳 · 重连换流） | `nxs/edge/gateway.py:_Handler._handle_events` | G1 | P1 | — | 4 |
| **M1.F15** | 网关优雅关闭（先关中枢再停 accept） | `nxs/edge/gateway.py:Gateway.stop` | G1 | P2 | — | 2 |
| **M1.F16** | 节点侧 HTTP 传输（实现 Transport 协议） | `nxs/edge/client.py:HttpTransport` | G1,G4 | P0 | — | 7 |
| **M1.F17** | 推送不可用时降级为短轮询 | `nxs/edge/client.py:HttpTransport.subscribe` | G1 | P1 | — | 2 |
| **M1.R1** | 认证失败不得区分具体原因（防成员枚举） | （由 M1.F7 实现） | G2 | P0 | S4 | 1 |
| **M1.R2** | Egress-Only：节点侧永不监听端口，入站连接只由总线提供 | （由 M1.F16, M1.F17 实现） | G2 | P1 | — | 1 |
| **M1.R3** | 未认证输入不得进入审计（拒绝时不记录调用方自报的标识） | （由 M1.F11 实现） | G2,G3 | P0 | S4 | 2 |

### M2 协议适配

平面 `接入层` · 实现 `nxs/edge/proto（未建）` · 状态 **未实现**

_（未实现，暂无功能点）_

### M3 注册中心

平面 `控制面` · 实现 `nxs/control/registry.py` · 状态 **已实现**

| 编号 | 名称／约束 | 实现位置 | G | P | S | 覆盖测试 |
|---|---|---|---|---|---|---|
| **M3.F1** | 角色卡注册（默认不批准） | `nxs/control/registry.py:Registry.register` | G1,G5 | P0 | S9 | 3 |
| **M3.F2** | 人工批准 | `nxs/control/registry.py:Registry.approve` | G1,G5 | P0 | S9 | 2 |
| **M3.F3** | 凭据吊销（保留记录） | `nxs/control/registry.py:Registry.revoke` | G2 | P0 | S10 | 2 |
| **M3.F4** | 心跳与三档存活状态 | `nxs/control/registry.py:Registry.heartbeat/sweep/resurrect` | G1 | P1 | — | 1 |
| **M3.F5** | 能力索引（只含可路由节点） | `nxs/control/registry.py:Registry.capability_index` | G1,G5 | P1 | — | 1 |
| **M3.F6** | 可路由性检查 | `nxs/control/registry.py:Registry.require_routable` | G3 | P0 | — | 1 |
| **M3.F7** | 角色卡持久化（重启后注册表仍在） | `nxs/backends/sqlite/SqliteRegistryStore` | G1,G5 | P0 | — | 2 |
| **M3.F8** | 节点自注册（不可信入口，永不带来批准） | `nxs/control/registry.py:Registry.register_from_node` | G1,G5 | P0 | S9,S12 | 10 |
| **M3.F9** | 注册身份一致性（卡片 node_id 必须等于签名身份） | `nxs/edge/gateway.py:_Handler._handle_register` | G2 | P0 | S12 | 3 |
| **M3.F10** | 信任域由配置裁定（自报域不足以改变归属） | `nxs/edge/gateway.py:Gateway.resolve_domain` | G2,G5 | P0 | S13 | 3 |
| **M3.F11** | 注册兼作心跳（卡片不变只刷新存活） | `nxs/control/registry.py:Registry.register_from_node（unchanged 路径）` | G1 | P1 | — | 1 |
| **M3.F12** | 注册事件审计（只记结果与版本，不记卡片内容） | `nxs/edge/gateway.py:Gateway.record_registration` | G3 | P1 | — | 1 |
| **M3.R1** | 新节点不能自行加入——注册后需人工批准才可参与 | （由 M3.F1, M3.F2, M3.F6 实现） | G2,G5 | P0 | S9 | 4 |
| **M3.R2** | 吊销不删除记录（须能回答「当时是否在线、是否被吊销」） | （由 M3.F3, M3.F4 实现） | G3 | P0 | S10 | 1 |
| **M3.R3** | 审批针对「那张角色卡」而非 node_id——卡片一变批准即失效 | （由 M3.F8 实现） | G2 | P0 | S12 | 1 |

### M4 匹配器

平面 `控制面` · 实现 `nxs/control/matcher（未建）` · 状态 **未实现**

_（未实现，暂无功能点）_

### M5 策略执行点

平面 `控制面` · 实现 `nxs/control/policy.py` · 状态 **已实现**

| 编号 | 名称／约束 | 实现位置 | G | P | S | 覆盖测试 |
|---|---|---|---|---|---|---|
| **M5.F1** | 任务类型白名单 | `nxs/control/policy.py:PolicyEngine.check_delivery（类型判定）` | G2 | P0 | S1 | 3 |
| **M5.F2** | 域间矩阵校验（默认拒绝） | `nxs/control/policy.py:PolicyEngine.check_delivery（rule 查找）` | G2 | P0 | S3 | 3 |
| **M5.F3** | 接收方约束校验 | `nxs/control/policy.py:PolicyEngine.check_delivery（can_receive_from）` | G2 | P0 | S3 | 1 |
| **M5.F4** | 应答字段白名单（含列表递归） | `nxs/control/policy.py:PolicyEngine.check_response` | G2 | P0 | S2 | 5 |
| **M5.F5** | 等级上限计算（三重取最小） | `nxs/control/policy.py:PolicyEngine.check_delivery（min 计算）` | G2 | P0 | S5 | 8 |
| **M5.F6** | 发布权限校验 | `nxs/control/policy.py:PolicyEngine.check_publish` | G2 | P0 | S3 | 4 |
| **M5.F7** | 配额校验（并发／按日／字节） | `nxs/control/policy.py:PolicyEngine.check_quota` | G2 | P1 | — | 10 |
| **M5.F8** | 委托链校验 | `nxs/control/policy.py:PolicyEngine.check_delegation` | G2 | P1 | — | 1 |
| **M5.F9** | 能力发现查询 | `nxs/control/policy.py:PolicyEngine.providers_of` | G1 | P1 | — | 2 |
| **M5.F10** | 角色卡来源可注入（策略层以注册表为准，而非只看配置） | `nxs/control/policy.py:PolicyEngine.__init__（card_resolver/cards_source）` | G1,G5 | P0 | — | 2 |
| **M5.R1** | 默认拒绝——没有显式允许的即拒绝 | （由 M5.F1, M5.F2, M5.F3 实现） | G2 | P0 | S3 | 2 |
| **M5.R2** | 产出等级上限取三重约束最小值（声明即承诺） | （由 M5.F5 实现） | G2 | P0 | S5 | 4 |

### M6 信箱

平面 `数据面` · 实现 `nxs/data/mailbox.py` · 状态 **已实现**

| 编号 | 名称／约束 | 实现位置 | G | P | S | 覆盖测试 |
|---|---|---|---|---|---|---|
| **M6.F1** | 任务投递（先校验后入队） | `nxs/data/mailbox.py:Mailbox.deliver` | G1,G2 | P0 | — | 1 |
| **M6.F2** | 任务领取（轮询即心跳） | `nxs/data/mailbox.py:Mailbox.poll` | G1 | P0 | — | 1 |
| **M6.F3** | 原子领取（并发下唯一赢家） | `nxs/backends/{memory,sqlite}/*.claim` | G1 | P0 | — | 3 |
| **M6.F4** | 周期性维护（标记 stale ＋ 释放租约） | `nxs/data/mailbox.py:Mailbox.sweep` | G1 | P1 | — | 1 |
| **M6.R1** | 被拒的任务不得在队列留下任何痕迹 | （由 M6.F1 实现） | G2 | P0 | — | 2 |
| **M6.R2** | 空队列不做长轮询（受限网络下连接会被切断） | （由 M6.F2 实现） | G1 | P1 | — | 1 |

### M7 任务生命周期

平面 `数据面` · 实现 `nxs/data/task.py` · 状态 **已实现**

| 编号 | 名称／约束 | 实现位置 | G | P | S | 覆盖测试 |
|---|---|---|---|---|---|---|
| **M7.F1** | 幂等提交 | `nxs/data/task.py:TaskLifecycle.submit` | G1 | P0 | — | 4 |
| **M7.F2** | 状态机迁移（含终态不可回退） | `nxs/data/task.py:TaskLifecycle._transition` | G1 | P0 | — | 2 |
| **M7.F3** | 租约领取（含重试计数） | `nxs/data/task.py:TaskLifecycle.claim` | G1 | P0 | — | 1 |
| **M7.F4** | 租约超时自动释放 | `nxs/data/task.py:TaskLifecycle.release_expired` | G1,G2 | P0 | — | 5 |
| **M7.F5** | 重试上限判定 | `nxs/data/task.py:TaskLifecycle.release_expired（max_attempts）` | G1,G2 | P0 | — | 1 |
| **M7.F6** | 追问（input_required 双向） | `nxs/data/task.py:TaskLifecycle.request_input` | G1 | P1 | — | 1 |
| **M7.F7** | 终态落定（完成／失败／取消） | `nxs/data/task.py:TaskLifecycle.complete/fail/cancel` | G1,G3 | P0 | — | 2 |
| **M7.F8** | 持久化（重启后队列仍在） | `nxs/backends/sqlite/*` | G1 | P0 | — | 3 |
| **M7.F9** | 续租端点 /nxs/ack（只允许持有者续自己的租约） | `nxs/edge/gateway.py:_Handler._handle_ack` | G1,G2 | P0 | — | 2 |
| **M7.F10** | 续租的归属与状态校验（失败即『已失去任务』） | `nxs/data/task.py:TaskLifecycle.renew` | G2 | P0 | — | 7 |
| **M7.F11** | SDK 执行期间自动续租（失去租约则丢弃产出） | `nxs/sdk/__init__.py:_LeaseKeeper` | G1 | P0 | — | 2 |
| **M7.R1** | 断电恢复零人工干预 | （由 M7.F3, M7.F4, M7.F5, M7.F8 实现） | G1,G2 | P0 | — | 6 |
| **M7.R2** | 重试不得产生重复副作用 | （由 M7.F1, M7.F5 实现） | G1,G2 | P0 | — | 3 |
| **M7.R3** | 终态不可回退 | （由 M7.F2, M7.F7 实现） | G3 | P0 | — | 1 |
| **M7.R4** | 长任务靠续租而非调大租约——租约同时是断电恢复的判据 | （由 M7.F9, M7.F10, M7.F11 实现） | G1,G2 | P0 | — | 2 |

### M8 会话与任务图

平面 `数据面` · 实现 `nxs/data/task.py:session_view` · 状态 **部分实现**

| 编号 | 名称／约束 | 实现位置 | G | P | S | 覆盖测试 |
|---|---|---|---|---|---|---|
| **M8.F1** | 会话视图（按 session 聚合状态） | `nxs/data/task.py:TaskLifecycle.session_view` | G1,G3 | P1 | — | 1 |

### M9 产物通道

平面 `数据面` · 实现 `nxs/data/artifact（未建）` · 状态 **未实现**

_（未实现，暂无功能点）_

### M10 应答边界

平面 `数据面` · 实现 `nxs/data/boundary.py` · 状态 **已实现**

| 编号 | 名称／约束 | 实现位置 | G | P | S | 覆盖测试 |
|---|---|---|---|---|---|---|
| **M10.F1** | 字段白名单强制执行 | `nxs/data/boundary.py:ResponseBoundary.enforce` | G2 | P0 | S2 | 3 |
| **M10.F2** | semantics 强制改写为 data | `nxs/data/boundary.py:ResponseBoundary.enforce_on_task` | G2 | P0 | S7 | 2 |
| **M10.F3** | 自由文本清洗（控制字符／指令前缀／UTF-8 安全截断） | `nxs/data/boundary.py:ResponseBoundary.sanitize_freetext` | G2 | P0 | S8 | 4 |
| **M10.F4** | 来源标注（trust_hint ＋ provenance） | `nxs/data/boundary.py:ResponseBoundary.trust_hint` | G2,G3 | P1 | — | 2 |
| **M10.F5** | 节点侧应答边界（数据离节点前裁剪到自身声明范围） | `nxs/data/boundary.py:LocalBoundary.check` | G2 | P0 | S2,S14 | 14 |
| **M10.F6** | 自由文本规范化两层共用同一实现（防逻辑分叉） | `nxs/data/boundary.py:sanitize_artifact` | G2 | P1 | S8 | 1 |
| **M10.R1** | 产出等级由产出方标注，发起方无法指定 | （由 M10.F1, M10.F2 实现） | G2 | P0 | S6 | 4 |
| **M10.R2** | 回传内容不得被当作指令执行（防跨域提示注入） | （由 M10.F2, M10.F3 实现） | G2 | P0 | S7,S8 | 2 |

### M11 配置中心

平面 `管理面` · 实现 `nxs/mgmt/config.py` · 状态 **已实现**

| 编号 | 名称／约束 | 实现位置 | G | P | S | 覆盖测试 |
|---|---|---|---|---|---|---|
| **M11.F1** | 配置加载与严格校验（非法即拒绝启动） | `nxs/mgmt/config.py:load_config` | G4,G5 | P0 | — | 10 |
| **M11.F2** | 矩阵规则解析（拒绝裸通配） | `nxs/mgmt/config.py:MatrixRule.from_dict/matches_type` | G5 | P0 | S3 | 1 |
| **M11.F3** | 凭据来源解析（环境变量优先） | `nxs/mgmt/config.py:load_config（凭据段）` | G2 | P0 | — | 3 |
| **M11.F4** | 示例配置回退与临时凭据 | `nxs/mgmt/config.py:resolve_config_path/demo_env_for` | G4 | P2 | — | 4 |
| **M11.F5** | 未实现项登记（防止静默失效） | `nxs/mgmt/config.py:UNIMPLEMENTED` | G3 | P1 | — | 3 |
| **M11.F6** | 配置摘要输出 | `nxs/mgmt/config.py:NexusConfig.describe` | G3 | P2 | — | 1 |
| **M11.F7** | 信任域显式声明（域是第一类实体，可从矩阵引用） | `nxs/mgmt/config.py:_parse_domains/NexusConfig.known_domains` | G4,G5 | P0 | — | 1 |
| **M11.F8** | 校验分级（声明了域即接受自注册节点，类型不做静态核对） | `nxs/mgmt/config.py:load_config（strict 分支）` | G4 | P1 | — | 2 |
| **M11.R1** | 配置非法即拒绝启动（不降级运行） | （由 M11.F1 实现） | G4,G5 | P0 | — | 1 |

### M12 审计留痕

平面 `管理面` · 实现 `nxs/mgmt/audit.py` · 状态 **已实现**

| 编号 | 名称／约束 | 实现位置 | G | P | S | 覆盖测试 |
|---|---|---|---|---|---|---|
| **M12.F1** | 元数据白名单写入 | `nxs/mgmt/audit.py:_whitelist` | G3 | P0 | S4 | 2 |
| **M12.F2** | 写入失败熔断 | `nxs/mgmt/audit.py:FileAuditStore.append` | G3 | P0 | — | 2 |
| **M12.F3** | NDJSON 容错读取 | `nxs/mgmt/audit.py:FileAuditStore.read_recent` | G3 | P1 | — | 1 |
| **M12.F4** | 汇总统计（按规则分组） | `nxs/mgmt/audit.py:FileAuditStore.summary` | G3 | P1 | — | 1 |
| **M12.R1** | 审计只记元数据，不含业务内容 | （由 M12.F1 实现） | G3 | P0 | — | 1 |
| **M12.R2** | 审计写入失败必须熔断（不可追溯比中断更糟） | （由 M12.F2 实现） | G3 | P0 | — | 1 |

### M13 可观测

平面 `管理面` · 实现 `nxs/mgmt/observ（未建）` · 状态 **未实现**

_（未实现，暂无功能点）_

### M14 运维接口

平面 `管理面` · 实现 `nxs/edge/gateway.py（ops 端点）· nxs/edge/client.py:OpsClient` · 状态 **已实现**

| 编号 | 名称／约束 | 实现位置 | G | P | S | 覆盖测试 |
|---|---|---|---|---|---|---|
| **M14.F1** | 运维身份与端点开关（未配密钥则整体不存在） | `nxs/edge/gateway.py:Gateway.ops_enabled/_ops_secret_from_env` | G3,G2 | P0 | — | 10 |
| **M14.F2** | 远程批准节点（绑定当前那张角色卡） | `nxs/edge/gateway.py:_Handler._handle_ops（approve）` | G3 | P0 | — | 5 |
| **M14.F3** | 远程吊销（注册表与认证层同时生效） | `nxs/edge/gateway.py:_Handler._handle_ops（revoke）` | G3 | P0 | S10 | 2 |
| **M14.F4** | 远程释放过期租约 | `nxs/edge/gateway.py:_Handler._handle_ops（release）` | G3 | P1 | — | 2 |
| **M14.F5** | 节点状态查询（含未批准节点） | `nxs/edge/gateway.py:_Handler._handle_ops（nodes）` | G3 | P1 | — | 3 |
| **M14.F6** | 运维动作审计（谁在何时给谁放了行） | `nxs/edge/gateway.py:Gateway.record_ops` | G3 | P0 | — | 1 |
| **M14.F7** | 运维 CLI（nxs ops） | `nxs/__main__.py:cmd_ops` | G3 | P1 | — | 4 |
| **M14.F8** | 部署自检（nxs doctor：持久化／权限／TLS／运维通道） | `nxs/__main__.py:cmd_doctor` | G3 | P1 | — | 3 |
| **M14.R1** | 运维通道不与节点共享配额——故障时运维必须进得来 | （由 M14.F1, M14.F2, M14.F3 实现） | G3 | P0 | — | 2 |
| **M14.R2** | 被管理者不能给自己批条子：节点密钥无法调用运维接口 | （由 M14.F2 实现） | G2,G3 | P0 | — | 2 |

## 代码准则

| 编号 | 准则 | 守护测试类 | 覆盖 |
|---|---|---|---|
| **C1** | 术语统一 | `TestTerminology` | 5 |
| **C2** | 分层纪律（依赖单向、Core 不做 I/O） | `TestLayerDirection` | 3 |
| **C3** | 不得出现任何节点的名字 | `TestNoNodeHardcoding` | 4 |
| **C4** | 拒绝时不得泄露内容 | `TestDenyNeverLeaks` | 6 |
| **C5** | 默认值落在「拒绝」一侧 | `TestDefaultsAreDeny` | 3 |
| **C6** | 严格校验，拒绝未知字段 | `TestStrictValidation` | 6 |
| **C7** | 配置字段必须被使用 | `TestConfigFieldsAreUsed` | 3 |
| **C8** | 标识符命名：禁止短编号式 | `TestIdentifierNaming` | 6 |

## 明确不做

| 编号 | 不做 | 理由 | 守护 |
|---|---|---|---|
| **N1** | 不含任何 LLM 调用 | 一旦它开始理解任务内容，就需要访问内容 | `TestDependencyFootprint` |
| **N2** | 不存业务数据与会话正文 | 保证自身被攻破时损失可控 | `TestAudit` |
| **N3** | 核心不做内容层面的安全判断 | 那属于节点侧的应答边界 | **无**（仅靠人工） |
| **N4** | 不决定任务拆解 | 那是 Decomposer 的职责 | **无**（仅靠人工） |
| **N5** | 不提供面向用户的界面 | 加 UI 会让它变成应用而非基础设施 | `TestDependencyFootprint` |
| **N6** | 不实现完整 A2A 协议栈 | 只需信封语义对齐，真正对接交给协议适配器 | `TestDependencyFootprint` |
| **N7** | 不引入消息中间件 | 当前规模用持久化队列足够 | `TestDependencyFootprint` |
| **N8** | 不做节点间物理直连 | 当前物理条件不满足（是现状，非架构禁止） | **无**（仅靠人工） |
| **N9** | 不保证业务正确性 | 只保证投递到、不重复、可追溯 | **无**（仅靠人工） |

## 安全要求

| 编号 | 要求 | 承载它的功能点／需求 |
|---|---|---|
| **S1** | 下发未注册的任务类型时，投递被拒绝，且不进入任何执行逻辑 | M5.F1 |
| **S2** | 产出含白名单外字段时被拒绝，且发起方收不到该内容的任何痕迹 | M5.F4, M10.F1, M10.F5 |
| **S3** | 跨信任域的委托默认被拒绝 | M5.F2, M5.F3, M5.F6, M11.F2, M5.R1 |
| **S4** | 拒绝响应只含 rule_id 与固定消息，不含字段名/值/长度/前缀 | M1.F1, M1.F2, M1.F3, M1.F5, M1.F7, M1.F10, M12.F1, M1.R1, M1.R3 |
| **S5** | 产出等级上限取三重约束的最小值 | M5.F5, M5.R2 |
| **S6** | 产出等级由产出方标注，发起方无法指定 | M10.R1 |
| **S7** | 回传内容的 semantics 被强制改写为 data | M10.F2, M10.R2 |
| **S8** | 自由文本字段受长度上限约束，并做指令前缀剥离与控制字符清理 | M10.F3, M10.F6, M10.R2 |
| **S9** | 新节点不能自行加入——注册后需人工批准 | M3.F1, M3.F2, M3.F8, M3.R1 |
| **S10** | 凭据吊销立即生效，不等过期 | M1.F6, M3.F3, M14.F3, M3.R2 |
| **S11** | 回传时以服务端持有的任务为准，节点无法冒领他人的任务 | M1.F11 |
| **S12** | 节点自注册不能带来批准，角色卡一经变更批准立即撤销 | M3.F8, M3.F9, M3.R3 |
| **S13** | 信任域由运维配置裁定，节点无法通过自报域获得更多权限 | M3.F10 |
| **S14** | 节点侧边界先于传输执行——数据离开节点时已裁剪到自身声明范围 | M10.F5 |

## 尚未实现的模块

- **M2** 协议适配 —— nxs/edge/proto（未建）
- **M4** 匹配器 —— nxs/control/matcher（未建）
- **M9** 产物通道 —— nxs/data/artifact（未建）
- **M13** 可观测 —— nxs/mgmt/observ（未建）

> 这些模块暂无功能点，因此不参与覆盖统计。
> 实现时应先在 `registry.py` 中登记功能点，再写测试并挂 `@covers`。

