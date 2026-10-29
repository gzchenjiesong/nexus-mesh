"""编号清单 —— **追溯体系的唯一真相源**。

本模块定义项目里所有编号及其元数据。它不参与运行时，只服务于：

* ``tests/test_traceability.py`` —— 校验覆盖完整性与引用有效性
* 生成的追溯报告
* 人类查阅（``python -m traceability``）

为什么不写在文档里
------------------
文档里的映射表会腐烂：改了代码没人记得回来改文档，而漏改不报错。
**把清单写成代码，然后让测试去校验它**，才能保证它始终与实现一致。

关于 core/ 与 ports/ 的归属
---------------------------
它们不属于任何 M。理由是：

* ``core/`` 是**契约层**（数据结构 + 纯函数），被所有模块依赖，自身不提供功能
* ``ports/`` 是**接口层**，定义能力而不实现

因此它们的正确性由**使用方**的功能点与全局准则共同保证：

* ``core/envelope.py`` 的签名能力 → 归 ``M1``（网关与认证）
* ``core/errors.py`` 的拒绝语义 → 归 ``C4``（拒绝不泄露内容）
* ``core/models.py`` 的严格校验 → 归 ``C6``（严格校验）
* ``ports/stores.py`` 的接口契约 → 由 ``M6``/``M7`` 的后端功能点保证

这条处理方式写进 ``CONTRIBUTING.md`` 的「编号体系总览」，避免读者以为遗漏了。
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ===========================================================================
# 数据结构
# ===========================================================================

@dataclass(frozen=True)
class Goal:
    """设计目标。系统存在的理由——不轻易变。"""

    id: str
    name: str
    statement: str          # 可判定的形式
    derived_modules: tuple[str, ...]   # 由它派生的模块


@dataclass(frozen=True)
class Module:
    """架构模块。"""

    id: str
    name: str
    plane: str              # 接入层 / 控制面 / 数据面 / 管理面
    impl: str               # 实现位置
    status: str             # 已实现 / 部分 / 未实现
    goals: tuple[str, ...]


@dataclass(frozen=True)
class Feature:
    """功能点（F）。

    判据：**有对应的代码逻辑**，能用一句话说清"它做了什么"。
    F 是最末级拆分——它要为具体代码提供编码需求支撑，所以粒度对齐实现单元。
    """

    id: str                 # "M5.F1"
    name: str
    impl: str               # "nxs/control/policy.py:check_delivery"
    goals: tuple[str, ...]
    priority: str           # P0-P3
    security: tuple[str, ...] = ()


@dataclass(frozen=True)
class Requirement:
    """模块需求（R）。

    判据：**是约束或质量属性**，可能没有独立代码，由若干 F 共同实现。
    与 F 平行，不是层级——T 可以挂 R（跨 F 的聚合验证）。
    """

    id: str                 # "M7.R1"
    statement: str
    realized_by: tuple[str, ...]    # 由哪些 F 实现
    goals: tuple[str, ...]
    priority: str
    security: tuple[str, ...] = ()


@dataclass(frozen=True)
class Convention:
    """代码准则（C）。跨模块的工程纪律。"""

    id: str
    name: str
    test_ref: str           # 守护它的测试类


@dataclass(frozen=True)
class NotDoing:
    """明确不做（N）。反向要求。"""

    id: str
    what: str
    why: str
    guarded_by: str = ""    # 由什么守护（可能为空——那就是只靠人工）


@dataclass(frozen=True)
class SecurityReq:
    """安全要求（S）。违反即系统存在意义不成立。"""

    id: str
    statement: str
    priority: str = "P0"


# ===========================================================================
# G · 设计目标（5 条，已确认）
# ===========================================================================

GOALS: tuple[Goal, ...] = (
    Goal(
        id="G1", name="让异构 Agent 能协作",
        statement="不同框架或厂商构建的 Agent，可以在不共享内部状态的前提下"
                  "发现彼此、委托任务、回传结果",
        derived_modules=("M3", "M4", "M6", "M7", "M8", "M9"),
    ),
    Goal(
        id="G2", name="协调节点被攻破也不泄露原始数据",
        statement="攻击者完全控制协调节点后，经本网络仍无法获取任何节点的原始数据",
        derived_modules=("M1", "M5", "M10"),
    ),
    Goal(
        id="G3", name="每次协作可校验可追溯",
        statement="任何一次委托都能回答：谁、何时、向谁、发了什么类型的任务、"
                  "命中了哪条规则、结果是什么等级",
        derived_modules=("M5", "M12"),
    ),
    Goal(
        id="G4", name="与任何具体 Agent 解耦",
        statement="替换任何一个节点（含人机入口）的实现，网络侧不需要改动。"
                  "→ 派生要求：通过适配器接入不同 Agent，新增 Agent 只需新增适配器",
        derived_modules=("M2", "M11"),
    ),
    Goal(
        id="G5", name="节点集合变化不需要改代码",
        statement="新增／移除节点、新增任务类型，只改配置，代码零改动",
        derived_modules=("M3", "M11"),
    ),
)


# ===========================================================================
# M · 模块（14 个）
# ===========================================================================

MODULES: tuple[Module, ...] = (
    # ---- 接入层 ----
    Module("M1", "网关与认证", "接入层",
           "nxs/edge/auth.py · gateway.py · httpsig.py · client.py",
           "已实现", ("G2", "G4")),
    Module("M2", "协议适配", "接入层", "nxs/edge/proto（未建）", "未实现", ("G4",)),
    # ---- 控制面 ----
    Module("M3", "注册中心", "控制面", "nxs/control/registry.py", "已实现", ("G1", "G5")),
    Module("M4", "匹配器", "控制面",
           "nxs/control/matcher.py", "已实现", ("G1",)),
    Module("M5", "策略执行点", "控制面", "nxs/control/policy.py", "已实现", ("G2", "G3")),
    # ---- 数据面 ----
    Module("M6", "信箱", "数据面", "nxs/data/mailbox.py", "已实现", ("G1",)),
    Module("M7", "任务生命周期", "数据面", "nxs/data/task.py", "已实现", ("G1", "G2")),
    Module("M8", "会话与任务图", "数据面", "nxs/data/task.py:session_view", "部分实现", ("G1", "G3")),
    Module("M9", "产物通道", "数据面",
           "nxs/core/models.py（kind=reference，坐标形态可用）· "
           "一次性令牌与 ArtifactStore 未做 · docs/artifact-size.md",
           "部分实现", ("G1",)),
    Module("M10", "应答边界", "数据面", "nxs/data/boundary.py", "已实现", ("G2",)),
    # ---- 管理面 ----
    Module("M11", "配置中心", "管理面", "nxs/mgmt/config.py", "已实现", ("G4", "G5")),
    Module("M12", "审计留痕", "管理面", "nxs/mgmt/audit.py", "已实现", ("G3",)),
    Module("M13", "可观测", "管理面", "nxs/mgmt/observ.py", "已实现", ("G3",)),
    Module("M14", "运维接口", "管理面",
           "nxs/edge/gateway.py（ops 端点）· nxs/edge/client.py:OpsClient",
           "已实现", ("G3",)),
    Module("M15", "观察窗 WebUI", "管理面",
           "nxs/ui/server.py · nxs/ui/page.py · nxs/__main__.py:cmd_ui",
           "已实现", ("G3",)),
)


# ===========================================================================
# F · 功能点（49 条，对齐代码实现单元）
# ===========================================================================

FEATURES: tuple[Feature, ...] = (
    # ---------------- M1 网关与认证 ----------------
    Feature("M1.F1", "信封签名生成",
            "nxs/core/envelope.py:Envelope.sign/compute_signature",
            ("G2",), "P0", ("S4",)),
    Feature("M1.F2", "签名验签（常数时间比较）",
            "nxs/core/envelope.py:Envelope.verify",
            ("G2",), "P0", ("S4",)),
    Feature("M1.F3", "时间窗校验（防重放）",
            "nxs/core/envelope.py:Envelope.verify（drift 判定）",
            ("G2",), "P0", ("S4",)),
    Feature("M1.F4", "规范 JSON 序列化（签名确定性）",
            "nxs/core/envelope.py:canonical_bytes",
            ("G2",), "P0"),
    Feature("M1.F5", "nonce 去重防重放（有界缓存）",
            "nxs/edge/auth.py:Authenticator._check_nonce",
            ("G2",), "P0", ("S4",)),
    Feature("M1.F6", "凭据管理（轮换／吊销即时生效）",
            "nxs/edge/auth.py:Authenticator.set_secret/revoke",
            ("G2",), "P0", ("S10",)),
    Feature("M1.F7", "成员认证编排（四步校验顺序）",
            "nxs/edge/auth.py:Authenticator.verify",
            ("G2",), "P0", ("S4",)),
    Feature("M1.F8", "信封格式校验（版本与结构）",
            "nxs/core/envelope.py:Envelope.from_dict",
            ("G1", "G2"), "P0"),

    # ---- v0.2 传输层（M1 网关）新增 ----
    Feature("M1.F9", "请求签名生成（方法+路径+节点+时间+nonce+体摘要）",
            "nxs/edge/httpsig.py:sign_request/compute_signature",
            ("G2",), "P0"),
    Feature("M1.F10", "请求签名校验（与信封共用 nonce 缓存与吊销表）",
            "nxs/edge/auth.py:Authenticator.verify_request",
            ("G2",), "P0", ("S4",)),
    Feature("M1.F11", "HTTP 端点分发与规则→状态码映射",
            "nxs/edge/gateway.py:_Handler._dispatch/status_for",
            ("G1", "G2"), "P0", ("S11",)),
    Feature("M1.F12", "请求体上限（读入前按 Content-Length 判定）",
            "nxs/edge/gateway.py:_Handler._read_body",
            ("G2",), "P1"),
    Feature("M1.F13", "并发与流配额（短请求与 SSE 分别计）",
            "nxs/edge/gateway.py:Gateway.acquire_request_slot/acquire_sse_slot",
            ("G2",), "P1"),
    Feature("M1.F14", "SSE 通知流（仅通知不含数据 · 心跳 · 重连换流）",
            "nxs/edge/gateway.py:_Handler._handle_events",
            ("G1",), "P1"),
    Feature("M1.F15", "网关优雅关闭（先关中枢再停 accept）",
            "nxs/edge/gateway.py:Gateway.stop",
            ("G1",), "P2"),
    Feature("M1.F16", "节点侧 HTTP 传输（实现 Transport 协议）",
            "nxs/edge/client.py:HttpTransport",
            ("G1", "G4"), "P0"),
    Feature("M1.F17", "推送不可用时降级为短轮询",
            "nxs/edge/client.py:HttpTransport.subscribe",
            ("G1",), "P1"),
    Feature("M1.F18", "凭据可只放在环境变量（自注册节点无需写进配置节点列表）",
            "nxs/edge/auth.py:Authenticator._secret_for",
            ("G4", "G1"), "P0"),

    # ---------------- M3 注册中心 ----------------
    Feature("M3.F1", "角色卡注册（默认不批准）",
            "nxs/control/registry.py:Registry.register",
            ("G1", "G5"), "P0", ("S9",)),
    Feature("M3.F2", "人工批准",
            "nxs/control/registry.py:Registry.approve",
            ("G1", "G5"), "P0", ("S9",)),
    Feature("M3.F3", "凭据吊销（保留记录）",
            "nxs/control/registry.py:Registry.revoke",
            ("G2",), "P0", ("S10",)),
    Feature("M3.F4", "心跳与三档存活状态",
            "nxs/control/registry.py:Registry.heartbeat/sweep/resurrect",
            ("G1",), "P1"),
    Feature("M3.F5", "能力索引（只含可路由节点）",
            "nxs/control/registry.py:Registry.capability_index",
            ("G1", "G5"), "P1"),
    Feature("M3.F6", "可路由性检查",
            "nxs/control/registry.py:Registry.require_routable",
            ("G3",), "P0"),
    Feature("M3.F7", "角色卡持久化（重启后注册表仍在）",
            "nxs/backends/sqlite/SqliteRegistryStore",
            ("G1", "G5"), "P0"),

    # ---- v0.3 自注册（部署必需）新增 ----
    Feature("M3.F8", "节点自注册（不可信入口，永不带来批准）",
            "nxs/control/registry.py:Registry.register_from_node",
            ("G1", "G5"), "P0", ("S9", "S12")),
    Feature("M3.F9", "注册身份一致性（卡片 node_id 必须等于签名身份）",
            "nxs/edge/gateway.py:_Handler._handle_register",
            ("G2",), "P0", ("S12",)),
    Feature("M3.F10", "信任域由配置裁定（自报域不足以改变归属）",
            "nxs/edge/gateway.py:Gateway.resolve_domain",
            ("G2", "G5"), "P0", ("S13",)),
    Feature("M3.F11", "注册兼作心跳（卡片不变只刷新存活）",
            "nxs/control/registry.py:Registry.register_from_node（unchanged 路径）",
            ("G1",), "P1"),
    Feature("M3.F12", "注册事件审计（只记结果与版本，不记卡片内容）",
            "nxs/edge/gateway.py:Gateway.record_registration",
            ("G3",), "P1"),

    # ---------------- M4 匹配器 ----------------
    Feature("M4.F1", "按能力筛选候选（能力声明 + 等级下限 + 接不接受该域）",
            "nxs/control/matcher.py:Matcher.candidates",
            ("G1",), "P0", ("S15",)),
    Feature("M4.F2", "确定性排序（同域 → 等级刚好够用 → 负载率 → 节点 ID）",
            "nxs/control/matcher.py:Candidate.sort_key",
            ("G1",), "P0"),
    Feature("M4.F3", "只把可路由节点列为候选（未批准/已吊销不参与）",
            "nxs/control/matcher.py:Matcher.candidates（rec.routable）",
            ("G1", "G2"), "P0", ("S9",)),
    Feature("M4.F4", "依次尝试直到通过（匹配 + 策略校验的编排）",
            "nxs/control/matcher.py:Matcher.select",
            ("G1",), "P0"),
    Feature("M4.F5", "自动投递（deliver 省略 to_node 时按能力选目标）",
            "nxs/data/mailbox.py:Mailbox._select_target",
            ("G1",), "P0"),
    Feature("M4.F6", "匹配失败统一错误码（候选数量与拒绝原因不外泄）",
            "nxs/data/mailbox.py:Mailbox._select_target（WL-TYPE-404）",
            ("G2",), "P0", ("S15",)),
    Feature("M4.F7", "幂等检查先于匹配（重试不会因候选变化而误报失败）",
            "nxs/data/mailbox.py:Mailbox.deliver（步骤 ⓪）",
            ("G1", "G2"), "P0", ("S16",)),
    Feature("M4.F8", "候选查询仅供运维（不经 HTTP 暴露）",
            "nxs/app.py:InProcessTransport.candidates",
            ("G3",), "P1", ("S15",)),

    # ---------------- M5 策略执行点 ----------------
    Feature("M5.F1", "任务类型白名单",
            "nxs/control/policy.py:PolicyEngine.check_delivery（类型判定）",
            ("G2",), "P0", ("S1",)),
    Feature("M5.F2", "域间矩阵校验（默认拒绝）",
            "nxs/control/policy.py:PolicyEngine.check_delivery（rule 查找）",
            ("G2",), "P0", ("S3",)),
    Feature("M5.F3", "接收方约束校验",
            "nxs/control/policy.py:PolicyEngine.check_delivery（域矩阵可达性）",
            ("G2",), "P0", ("S3",)),
    Feature("M5.F4", "应答字段白名单（含列表递归）",
            "nxs/control/policy.py:PolicyEngine.check_response",
            ("G2",), "P0", ("S2",)),
    Feature("M5.F5", "等级上限计算（三重取最小）",
            "nxs/control/policy.py:PolicyEngine.check_delivery（min 计算）",
            ("G2",), "P0", ("S5",)),
    Feature("M5.F6", "发布权限校验",
            "nxs/control/policy.py:PolicyEngine.check_publish",
            ("G2",), "P0", ("S3",)),
    Feature("M5.F7", "配额校验（并发／按日／字节）",
            "nxs/control/policy.py:PolicyEngine.check_quota",
            ("G2",), "P1"),
    Feature("M5.F8", "委托链校验",
            "nxs/control/policy.py:PolicyEngine.check_delegation",
            ("G2",), "P1"),
    Feature("M5.F9", "能力发现查询",
            "nxs/control/policy.py:PolicyEngine.providers_of",
            ("G1",), "P1"),
    Feature("M5.F10", "角色卡来源可注入（策略层以注册表为准，而非只看配置）",
            "nxs/control/policy.py:PolicyEngine.__init__（card_resolver/cards_source）",
            ("G1", "G5"), "P0"),

    # ---------------- M6 信箱 ----------------
    Feature("M6.F1", "任务投递（先校验后入队）",
            "nxs/data/mailbox.py:Mailbox.deliver",
            ("G1", "G2"), "P0"),
    Feature("M6.F2", "任务领取（轮询即心跳）",
            "nxs/data/mailbox.py:Mailbox.poll",
            ("G1",), "P0"),
    Feature("M6.F3", "原子领取（并发下唯一赢家）",
            "nxs/backends/{memory,sqlite}/*.claim",
            ("G1",), "P0"),
    Feature("M6.F4", "周期性维护（标记 stale ＋ 释放租约）",
            "nxs/data/mailbox.py:Mailbox.sweep",
            ("G1",), "P1"),

    # ---------------- M7 任务生命周期 ----------------
    Feature("M7.F1", "幂等提交",
            "nxs/data/task.py:TaskLifecycle.submit",
            ("G1",), "P0"),
    Feature("M7.F2", "状态机迁移（含终态不可回退）",
            "nxs/data/task.py:TaskLifecycle._transition",
            ("G1",), "P0"),
    Feature("M7.F3", "租约领取（含重试计数）",
            "nxs/data/task.py:TaskLifecycle.claim",
            ("G1",), "P0"),
    Feature("M7.F4", "租约超时自动释放",
            "nxs/data/task.py:TaskLifecycle.release_expired",
            ("G1", "G2"), "P0"),
    Feature("M7.F5", "重试上限判定",
            "nxs/data/task.py:TaskLifecycle.release_expired（max_attempts）",
            ("G1", "G2"), "P0"),
    Feature("M7.F6", "追问（input_required 双向）",
            "nxs/data/task.py:TaskLifecycle.request_input",
            ("G1",), "P1"),
    Feature("M7.F7", "终态落定（完成／失败／取消）",
            "nxs/data/task.py:TaskLifecycle.complete/fail/cancel",
            ("G1", "G3"), "P0"),
    Feature("M7.F8", "持久化（重启后队列仍在）",
            "nxs/backends/sqlite/*",
            ("G1",), "P0"),
    Feature("M7.F9", "续租端点 /nxs/ack（只允许持有者续自己的租约）",
            "nxs/edge/gateway.py:_Handler._handle_ack",
            ("G1", "G2"), "P0"),
    Feature("M7.F10", "续租的归属与状态校验（失败即『已失去任务』）",
            "nxs/data/task.py:TaskLifecycle.renew",
            ("G2",), "P0"),
    Feature("M7.F11", "SDK 执行期间自动续租（失去租约则丢弃产出）",
            "nxs/sdk/__init__.py:_LeaseKeeper",
            ("G1",), "P0"),

    # ---------------- M8 会话 ----------------
    Feature("M8.F1", "会话视图（按 session 聚合状态）",
            "nxs/data/task.py:TaskLifecycle.session_view",
            ("G1", "G3"), "P1"),

    # ---------------- M9 产物通道 ----------------
    # 实测后重新界定：**不扩容，走引用**（见 docs/artifact-size.md）
    Feature("M9.F1", "引用传递形态（kind=reference 传坐标，不传内容）",
            "nxs/core/models.py:Artifact（kind）+ nxs/data/boundary.py（白名单）",
            ("G1",), "P0"),
    Feature("M9.F2", "产物体积实测脚本（信封上限可复现）",
            "examples/probe_artifact_size.py",
            ("G1", "G3"), "P1"),

    # ---------------- M10 应答边界 ----------------
    Feature("M10.F1", "字段白名单强制执行",
            "nxs/data/boundary.py:ResponseBoundary.enforce",
            ("G2",), "P0", ("S2",)),
    Feature("M10.F2", "semantics 强制改写为 data",
            "nxs/data/boundary.py:ResponseBoundary.enforce_on_task",
            ("G2",), "P0", ("S7",)),
    Feature("M10.F3", "自由文本清洗（控制字符／指令前缀／UTF-8 安全截断）",
            "nxs/data/boundary.py:ResponseBoundary.sanitize_freetext",
            ("G2",), "P0", ("S8",)),
    Feature("M10.F4", "来源标注（trust_hint ＋ provenance）",
            "nxs/data/boundary.py:ResponseBoundary.trust_hint",
            ("G2", "G3"), "P1"),
    Feature("M10.F5", "节点侧应答边界（数据离节点前裁剪到自身声明范围）",
            "nxs/data/boundary.py:LocalBoundary.check",
            ("G2",), "P0", ("S2", "S14")),
    Feature("M10.F6", "自由文本规范化两层共用同一实现（防逻辑分叉）",
            "nxs/data/boundary.py:sanitize_artifact",
            ("G2",), "P1", ("S8",)),
    Feature("M10.F7", "kind 与声明自洽（禁止自称 freetext 绕过清洗）",
            "nxs/data/boundary.py:check_kind_matches_declaration",
            ("G2",), "P0", ("S8", "S17")),

    # ---------------- M11 配置中心 ----------------
    Feature("M11.F1", "配置加载与严格校验（非法即拒绝启动）",
            "nxs/mgmt/config.py:load_config",
            ("G4", "G5"), "P0"),
    Feature("M11.F2", "矩阵规则解析（拒绝裸通配）",
            "nxs/mgmt/config.py:MatrixRule.from_dict/matches_type",
            ("G5",), "P0", ("S3",)),
    Feature("M11.F3", "凭据来源解析（环境变量优先）",
            "nxs/mgmt/config.py:load_config（凭据段）",
            ("G2",), "P0"),
    Feature("M11.F4", "示例配置回退与临时凭据",
            "nxs/mgmt/config.py:resolve_config_path/demo_env_for",
            ("G4",), "P2"),
    Feature("M11.F5", "未实现项登记（防止静默失效）",
            "nxs/mgmt/config.py:UNIMPLEMENTED",
            ("G3",), "P1"),
    Feature("M11.F6", "配置摘要输出",
            "nxs/mgmt/config.py:NexusConfig.describe",
            ("G3",), "P2"),
    Feature("M11.F7", "信任域显式声明（域是第一类实体，可从矩阵引用）",
            "nxs/mgmt/config.py:_parse_domains/NexusConfig.known_domains",
            ("G4", "G5"), "P0"),
    Feature("M11.F8", "校验分级（声明了域即接受自注册节点，类型不做静态核对）",
            "nxs/mgmt/config.py:load_config（strict 分支）",
            ("G4",), "P1"),

    # ---------------- M12 审计 ----------------
    Feature("M12.F1", "元数据白名单写入",
            "nxs/mgmt/audit.py:_whitelist",
            ("G3",), "P0", ("S4",)),
    Feature("M12.F2", "写入失败熔断",
            "nxs/mgmt/audit.py:FileAuditStore.append",
            ("G3",), "P0"),
    Feature("M12.F3", "NDJSON 容错读取",
            "nxs/mgmt/audit.py:FileAuditStore.read_recent",
            ("G3",), "P1"),
    Feature("M12.F4", "汇总统计（按规则分组）",
            "nxs/mgmt/audit.py:FileAuditStore.summary",
            ("G3",), "P1"),

    # ---------------- M13 可观测 ----------------
    Feature("M13.F1", "运行时指标计数器（提交／完成／失败／拒绝／字节，线程安全）",
            "nxs/mgmt/observ.py:Metrics",
            ("G3",), "P0"),
    Feature("M13.F2", "延迟分位数 p50/p95/p99（有界样本窗口）",
            "nxs/mgmt/observ.py:_percentile",
            ("G3",), "P0"),
    Feature("M13.F3", "快照组装（计数用计数器，状态用队列真源）",
            "nxs/mgmt/observ.py:collect",
            ("G3",), "P0"),
    Feature("M13.F4", "积压水位（待办／并发上限，按比率而非绝对数）",
            "nxs/mgmt/observ.py:NodeMetrics.backlog_ratio",
            ("G3",), "P1"),
    Feature("M13.F5", "系统级关联：批量搬运（字节与任务量同时离群）",
            "nxs/mgmt/observ.py:_check_bulk_movement",
            ("G3", "G2"), "P0", ("S18",)),
    Feature("M13.F6", "系统级关联：权限试探（撞多种不同拒绝规则）",
            "nxs/mgmt/observ.py:_check_probe_pattern",
            ("G3", "G2"), "P0", ("S18",)),
    Feature("M13.F7", "系统级关联：给离线节点派活（stale/revoked 名下仍有任务）",
            "nxs/mgmt/observ.py:_check_dead_route",
            ("G3",), "P0"),
    Feature("M13.F8", "系统级关联：静默卡死（积压高且零产出）",
            "nxs/mgmt/observ.py:_check_stall",
            ("G3",), "P0"),
    Feature("M13.F9", "系统级关联：拒绝结构（集中于单条规则 = 配错）",
            "nxs/mgmt/observ.py:_check_denial_surge",
            ("G3",), "P1"),
    Feature("M13.F10", "指标只走运维面（节点拿不到拓扑画像）",
            "nxs/edge/gateway.py:Gateway.collect_metrics（ops/metrics）",
            ("G2",), "P0", ("S19",)),
    Feature("M13.F11", "指标 CLI（nxs metrics，支持本机与 --url 远端）",
            "nxs/__main__.py:cmd_metrics",
            ("G3",), "P1"),
    Feature("M13.F12", "指标不记录业务内容（只有数量、类型名、规则码、字节数）",
            "nxs/mgmt/observ.py:Metrics（打点签名）",
            ("G2", "G3"), "P0", ("S19",)),

    # ---------------- M14 运维接口（v0.3） ----------------
    Feature("M14.F1", "运维身份与端点开关（未配密钥则整体不存在）",
            "nxs/edge/gateway.py:Gateway.ops_enabled/_ops_secret_from_env",
            ("G3", "G2"), "P0"),
    Feature("M14.F2", "远程批准节点（绑定当前那张角色卡）",
            "nxs/edge/gateway.py:_Handler._handle_ops（approve）",
            ("G3",), "P0"),
    Feature("M14.F3", "远程吊销（注册表与认证层同时生效）",
            "nxs/edge/gateway.py:_Handler._handle_ops（revoke）",
            ("G3",), "P0", ("S10",)),
    Feature("M14.F4", "远程释放过期租约",
            "nxs/edge/gateway.py:_Handler._handle_ops（release）",
            ("G3",), "P1"),
    Feature("M14.F5", "节点状态查询（含未批准节点）",
            "nxs/edge/gateway.py:_Handler._handle_ops（nodes）",
            ("G3",), "P1"),
    Feature("M14.F6", "运维动作审计（谁在何时给谁放了行）",
            "nxs/edge/gateway.py:Gateway.record_ops",
            ("G3",), "P0"),
    Feature("M14.F7", "运维 CLI（nxs ops）",
            "nxs/__main__.py:cmd_ops",
            ("G3",), "P1"),
    Feature("M14.F8", "部署自检（nxs doctor：持久化／权限／TLS／运维通道）",
            "nxs/__main__.py:cmd_doctor",
            ("G3",), "P1"),
    Feature("M14.F9", "TLS 证书生成脚本化（CA 扩展完整、文件权限正确）",
            "deploy/gen-tls.sh",
            ("G3",), "P1"),

    # ---- M15 观察窗 WebUI -------------------------------------------
    Feature("M15.F1", "只读 UI 进程（只听 127.0.0.1，拒绝公网绑定）",
            "nxs/ui/server.py:UiServer",
            ("G3",), "P1"),
    Feature("M15.F2", "一次性 token 鉴权（启动生成 · HttpOnly cookie · 不落盘）",
            "nxs/ui/server.py:_make_handler",
            ("G3",), "P1"),
    Feature("M15.F3", "数据只经运维端点（不 import app／存储后端）",
            "nxs/ui/server.py:UiServer.snapshot",
            ("G3",), "P1"),
    Feature("M15.F4", "节点列表视图（名称／简介／角色／状态／能力）",
            "nxs/ui/page.py:SHELL · nxs/ui/server.py:snapshot",
            ("G3",), "P1"),
    Feature("M15.F5", "系统汇总视图（计数／延迟分位数／积压）",
            "nxs/ui/page.py:renderTiles",
            ("G3",), "P1"),
    Feature("M15.F6", "自报字段安全渲染（textContent 建 DOM · 不用 innerHTML）",
            "nxs/ui/page.py:el",
            ("G3",), "P1"),
    Feature("M15.F7", "自报字段服务端校验（长度上限 · 拒控制字符）",
            "nxs/core/models.py:_clean_self_reported",
            ("G3",), "P1"),
    Feature("M15.F8", "只读保证（无写方法 · 405 带 Allow）",
            "nxs/ui/server.py:Handler.do_POST",
            ("G3",), "P1"),
)


# ===========================================================================
# R · 模块需求（15 条，约束视角，与 F 平行）
# ===========================================================================

REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        "M1.R1", "认证失败不得区分具体原因（防成员枚举）",
        realized_by=("M1.F7",), goals=("G2",), priority="P0", security=("S4",),
    ),
    Requirement(
        "M1.R2", "Egress-Only：节点侧永不监听端口，入站连接只由总线提供",
        realized_by=("M1.F16", "M1.F17"), goals=("G2",), priority="P1",
    ),
    Requirement(
        "M1.R3", "未认证输入不得进入审计（拒绝时不记录调用方自报的标识）",
        realized_by=("M1.F11",), goals=("G2", "G3"), priority="P0",
        security=("S4",),
    ),
    Requirement(
        "M3.R1", "新节点不能自行加入——注册后需人工批准才可参与",
        realized_by=("M3.F1", "M3.F2", "M3.F6"), goals=("G2", "G5"),
        priority="P0", security=("S9",),
    ),
    Requirement(
        "M3.R2", "吊销不删除记录（须能回答「当时是否在线、是否被吊销」）",
        realized_by=("M3.F3", "M3.F4"), goals=("G3",), priority="P0", security=("S10",),
    ),
    Requirement(
        "M3.R3", "审批针对「那张角色卡」而非 node_id——卡片一变批准即失效",
        realized_by=("M3.F8",), goals=("G2",), priority="P0", security=("S12",),
    ),
    Requirement(
        "M14.R1", "运维通道不与节点共享配额——故障时运维必须进得来",
        realized_by=("M14.F1", "M14.F2", "M14.F3"), goals=("G3",), priority="P0",
    ),
    Requirement(
        "M14.R2", "被管理者不能给自己批条子：节点密钥无法调用运维接口",
        realized_by=("M14.F2",), goals=("G2", "G3"), priority="P0",
    ),
    Requirement(
        "M4.R1", "排序必须确定性——同一输入两次必须选出同一个候选",
        realized_by=("M4.F2",), goals=("G1",), priority="P0",
    ),
    Requirement(
        "M4.R2", "匹配只看「能力 + 约束」，不看「关系」（不读域间矩阵）",
        realized_by=("M4.F1",), goals=("G1", "G2"), priority="P0", security=("S15",),
    ),
    Requirement(
        "M5.R1", "默认拒绝——没有显式允许的即拒绝",
        realized_by=("M5.F1", "M5.F2", "M5.F3"), goals=("G2",),
        priority="P0", security=("S3",),
    ),
    Requirement(
        "M5.R2", "产出等级上限取三重约束最小值（声明即承诺）",
        realized_by=("M5.F5",), goals=("G2",), priority="P0", security=("S5",),
    ),
    Requirement(
        "M6.R1", "被拒的任务不得在队列留下任何痕迹",
        realized_by=("M6.F1",), goals=("G2",), priority="P0",
    ),
    Requirement(
        "M6.R2", "空队列不做长轮询（受限网络下连接会被切断）",
        realized_by=("M6.F2",), goals=("G1",), priority="P1",
    ),
    Requirement(
        "M7.R1", "断电恢复零人工干预",
        realized_by=("M7.F3", "M7.F4", "M7.F5", "M7.F8"), goals=("G1", "G2"),
        priority="P0",
    ),
    Requirement(
        "M7.R2", "重试不得产生重复副作用",
        realized_by=("M7.F1", "M7.F5"), goals=("G1", "G2"), priority="P0",
    ),
    Requirement(
        "M7.R3", "终态不可回退",
        realized_by=("M7.F2", "M7.F7"), goals=("G3",), priority="P0",
    ),
    Requirement(
        "M7.R4", "长任务靠续租而非调大租约——租约同时是断电恢复的判据",
        realized_by=("M7.F9", "M7.F10", "M7.F11"), goals=("G1", "G2"),
        priority="P0",
    ),
    Requirement(
        "M10.R1", "产出等级由产出方标注，发起方无法指定",
        realized_by=("M10.F1", "M10.F2"), goals=("G2",),
        priority="P0", security=("S6",),
    ),
    Requirement(
        "M10.R2", "回传内容不得被当作指令执行（防跨域提示注入）",
        realized_by=("M10.F2", "M10.F3"), goals=("G2",),
        priority="P0", security=("S7", "S8"),
    ),
    Requirement(
        "M11.R1", "配置非法即拒绝启动（不降级运行）",
        realized_by=("M11.F1",), goals=("G4", "G5"), priority="P0",
    ),
    Requirement(
        "M12.R1", "审计只记元数据，不含业务内容",
        realized_by=("M12.F1",), goals=("G3",), priority="P0",
    ),
    Requirement(
        "M12.R2", "审计写入失败必须熔断（不可追溯比中断更糟）",
        realized_by=("M12.F2",), goals=("G3",), priority="P0",
    ),
)


# ===========================================================================
# C · 代码准则（8 条）
# ===========================================================================

CONVENTIONS: tuple[Convention, ...] = (
    Convention("C1", "术语统一", "TestTerminology"),
    Convention("C2", "分层纪律（依赖单向、Core 不做 I/O）", "TestLayerDirection"),
    Convention("C3", "不得出现任何节点的名字", "TestNoNodeHardcoding"),
    Convention("C4", "拒绝时不得泄露内容", "TestDenyNeverLeaks"),
    Convention("C5", "默认值落在「拒绝」一侧", "TestDefaultsAreDeny"),
    Convention("C6", "严格校验，拒绝未知字段", "TestStrictValidation"),
    Convention("C7", "配置字段必须被使用", "TestConfigFieldsAreUsed"),
    Convention("C8", "标识符命名：禁止短编号式", "TestIdentifierNaming"),
)


# ===========================================================================
# N · 明确不做（9 条）
# ===========================================================================

NOT_DOING: tuple[NotDoing, ...] = (
    NotDoing("N1", "不含任何 LLM 调用",
             "一旦它开始理解任务内容，就需要访问内容",
             "TestDependencyFootprint"),
    NotDoing("N2", "不存业务数据与会话正文",
             "保证自身被攻破时损失可控", "TestAudit"),
    NotDoing("N3", "核心不做内容层面的安全判断",
             "那属于节点侧的应答边界", ""),
    NotDoing("N4", "不决定任务拆解",
             "那是 Decomposer 的职责", ""),
    NotDoing("N5", "不提供面向用户的界面",
             "加 UI 会让它变成应用而非基础设施", "TestDependencyFootprint"),
    NotDoing("N6", "不实现完整 A2A 协议栈",
             "只需信封语义对齐，真正对接交给协议适配器",
             "TestDependencyFootprint"),
    NotDoing("N7", "不引入消息中间件",
             "当前规模用持久化队列足够", "TestDependencyFootprint"),
    NotDoing("N8", "不做节点间物理直连",
             "当前物理条件不满足（是现状，非架构禁止）", ""),
    NotDoing("N9", "不保证业务正确性",
             "只保证投递到、不重复、可追溯", ""),
)


# ===========================================================================
# S · 安全要求（10 条）
# ===========================================================================

SECURITY: tuple[SecurityReq, ...] = (
    SecurityReq("S1", "下发未注册的任务类型时，投递被拒绝，且不进入任何执行逻辑"),
    SecurityReq("S2", "产出含白名单外字段时被拒绝，且发起方收不到该内容的任何痕迹"),
    SecurityReq("S3", "跨信任域的委托默认被拒绝"),
    SecurityReq("S4", "拒绝响应只含 rule_id 与固定消息，不含字段名/值/长度/前缀"),
    SecurityReq("S5", "产出等级上限取三重约束的最小值"),
    SecurityReq("S6", "产出等级由产出方标注，发起方无法指定"),
    SecurityReq("S7", "回传内容的 semantics 被强制改写为 data"),
    SecurityReq("S8", "自由文本字段受长度上限约束，并做指令前缀剥离与控制字符清理"),
    SecurityReq("S9", "新节点不能自行加入——注册后需人工批准"),
    SecurityReq("S10", "凭据吊销立即生效，不等过期"),
    SecurityReq("S11", "回传时以服务端持有的任务为准，节点无法冒领他人的任务"),
    SecurityReq("S12", "节点自注册不能带来批准，角色卡一经变更批准立即撤销"),
    SecurityReq("S13", "信任域由运维配置裁定，节点无法通过自报域获得更多权限"),
    SecurityReq("S14", "节点侧边界先于传输执行——数据离开节点时已裁剪到自身声明范围"),
    SecurityReq("S15", "候选集合与匹配失败原因不外泄——「谁能干这个活」本身是拓扑信息"),
    SecurityReq("S16", "幂等命中须归属一致——不得凭幂等键探测他人任务"),
    SecurityReq("S17", "产物 kind 必须与能力声明自洽——不得自称 freetext 绕过清洗"),
    SecurityReq("S18", "告警须为组合判断——单指标阈值不算异常，避免告警疲劳淹没真信号"),
    SecurityReq("S19", "指标属拓扑画像，只在运维面暴露；不含业务内容"),
)


# ===========================================================================
# 查询辅助
# ===========================================================================

#: 全部纵向编号项（F 与 R）
VERTICAL_ITEMS: dict[str, Feature | Requirement] = {
    it.id: it for it in (*FEATURES, *REQUIREMENTS)
}

#: 全部编号项（含横切）
ALL_ITEMS: dict[str, object] = {
    **{g.id: g for g in GOALS},
    **{m.id: m for m in MODULES},
    **VERTICAL_ITEMS,
    **{c.id: c for c in CONVENTIONS},
    **{n.id: n for n in NOT_DOING},
    **{s.id: s for s in SECURITY},
}


def items_of_module(module_id: str) -> tuple[Feature | Requirement, ...]:
    """列出某模块下的全部 F 与 R。"""
    return tuple(
        it for it in VERTICAL_ITEMS.values()
        if it.id.startswith(f"{module_id}.")
    )


def modules_without_features() -> tuple[str, ...]:
    """尚未实现（无功能点）的模块。"""
    have = {it.id.split(".")[0] for it in VERTICAL_ITEMS.values()}
    return tuple(m.id for m in MODULES if m.id not in have)


def summary() -> dict[str, int]:
    return {
        "目标": len(GOALS),
        "模块": len(MODULES),
        "功能点": len(FEATURES),
        "模块需求": len(REQUIREMENTS),
        "代码准则": len(CONVENTIONS),
        "明确不做": len(NOT_DOING),
        "安全要求": len(SECURITY),
        "纵向编号项合计": len(VERTICAL_ITEMS),
    }


__all__ = [
    "ALL_ITEMS",
    "CONVENTIONS",
    "FEATURES",
    "GOALS",
    "MODULES",
    "NOT_DOING",
    "REQUIREMENTS",
    "SECURITY",
    "VERTICAL_ITEMS",
    "Convention",
    "Feature",
    "Goal",
    "Module",
    "NotDoing",
    "Requirement",
    "SecurityReq",
    "items_of_module",
    "modules_without_features",
    "summary",
]
