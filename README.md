# Nexus Mesh

> A self-hosted coordination network for heterogeneous AI agents.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-190%20passing-brightgreen.svg)](#开发)
[![Runtime deps](https://img.shields.io/badge/runtime%20deps-1-brightgreen.svg)](pyproject.toml)

> 徽章为静态自述（本项目未接 CI），数字取自本地测试与 `pyproject.toml`。

**[中文说明](#中文说明) · [English](#english)**

---

## 中文说明

Nexus Mesh 是一个**独立的 Agent 协作基础设施**。

它面向的是**异构 Agent**：不同实现、不同环境、不同信任域的 Agent，以各自的**身份、定位与角色**接入同一个网络，然后互相发现、委托任务、回传结果。

网络本身不关心你用什么框架、跑在哪里、由谁实现——它只关心：**你是谁、能做什么、可以被谁委托什么。** 这三件事全部由配置声明，代码里不出现任何一个具体 Agent 的名字。

落到实现上，它是"把任务送到、把状态记对、把产出管住"。与普通的消息传递相比，多了四条硬约束：

| | 普通消息传递 | Nexus Mesh |
|---|---|---|
| 消息 | 自由文本或裸 payload | **结构化任务**——带类型与参数 schema，可校验、可路由 |
| 生命周期 | 发出即止 | **状态机**——投递 / 执行 / 追问 / 超时 / 重试 / 幂等 |
| 权限 | 基本不管 | **权限矩阵**——默认拒绝，应答有等级上限 |
| 发现 | 你得知道发到哪 | **按能力匹配**——只说需要什么能力，不依赖拓扑 |

### 它解决什么问题

当单一 Agent 因**环境隔离、能力边界、数据归属**而无法覆盖全部工作时，就需要多个 Agent 协作。但让它们直接互联会带来两个硬问题：

1. **异构节点如何统一接入**——不同实现、不同角色、不同信任域的 Agent，凭什么信任彼此的委托？每一跳的权限如何收窄？
2. **结果范围如何控制**——跨域协作时，如何保证回传的只是"够用"的那部分，而不是把原始数据整份交出去？

Nexus Mesh 把这两件事作为核心，而不是附属功能。

### 核心概念

| 抽象 | 是什么 |
|---|---|
| **节点（Node）** | 以某角色接入网络的 Agent（Issuer 发布 / Executor 执行 / Relay 中转 / Observer 只读） |
| **角色卡（Role Card）** | 节点的自我声明——角色、能力清单、**信任约束**（可达等级 / 可发布类型 / 配额） |
| **能力（Capability）** | 一个具名的任务类型，含参数 schema 与**应答字段白名单** |
| **任务（Task）** | 一次协作的载体，有状态机、租约、幂等键 |
| **应答边界（Response Boundary）** | 节点侧强制执行的最后关卡——不该给的数据**在实现层不存在代码路径** |
| **会话（Session）** | 一次用户诉求的上下文，串联其下所有任务 |

### 设计文档

**代码是这个目录的派生物** —— 需求、编号体系与验证标准都在这里：

| 文档 | 内容 |
|---|---|
| [`docs/design/`](docs/design/) | 设计文档索引与「文档驱动开发」的流程 |
| [`docs/design/01-system-definition.md`](docs/design/01-system-definition.md) | 这个系统是什么：定位、边界、模块、角色、协作原语、协议契约 |
| [`docs/design/02-numbering-standard.md`](docs/design/02-numbering-standard.md) | 编号体系：G/M/F/R/T 纵向链 + 横切标签 |
| [`docs/architecture.md`](docs/architecture.md) | 工程视角的架构说明与已知缺口 |
| [`traceability/registry.py`](traceability/registry.py) | **需求编号的唯一真相源**（可执行） |
| [`traceability/MATRIX.md`](traceability/MATRIX.md) | 追溯矩阵（由测试自动生成） |

设计文档与代码的一致性由 `tests/test_traceability.py` **自动校验**，
不是靠人工维护 —— 详见 [`CONTRIBUTING.md`](CONTRIBUTING.md) 的「可执行的追溯」。

### 快速开始

```bash
git clone https://github.com/gzchenjiesong/nexus-mesh.git
cd nexus-mesh
make install
make demo          # 等价于 nxs demo
make demo-net      # 同一套八步断言，走真实 HTTP
```

四个命令：

| 命令 | 作用 |
|---|---|
| `nxs verify` | 配置自检：校验角色卡、矩阵、凭据的一致性 |
| `nxs doctor` | **部署前自检**：持久化、权限、TLS、运维通道（加 `--production` 按生产标准） |
| `nxs demo` | 跑八步验收 demo（**先看这个**）。加 `--net` 改走真实网络 |
| `nxs serve` | 核心常驻：**真的监听 HTTP 端口** + 定期维护 |
| `nxs ops` | 远程运维：`approve` / `revoke` / `release` / `nodes` |
| `nxs candidates` | 查某任务类型的候选执行者**及排序依据**（运维视角，不经 HTTP） |
| `nxs status` | 查看节点状态与审计摘要 |

`nxs demo` 会跑出一段完整的协作流程，包括**三条安全断言**：

```
[4/7] 安全断言① 下发未注册的任务类型
      → REJECTED  rule_id=WL-TYPE-404   （不进入任何执行逻辑）
[5/7] 安全断言② 请求超出字段白名单的内容
      → REJECTED  rule_id=WL-FIELD-001  （协调节点仅收到 rule_id，无内容）
[6/7] 安全断言③ 跨信任域发布任务
      → REJECTED  rule_id=MX-DOMAIN-02  （默认拒绝）
```

**这三条断言是这个项目区别于普通消息传递的地方。** 若它们不能稳定复现地通过，本项目就没有达成设计目标。

`nxs demo --net` 跑的是**同一段流程代码**，只把传输换成了 HTTP。
两条命令的审计摘要应当完全一致——这就是"传输可替换"的证明。

### 接入一个真实节点

> 📖 **完整部署步骤见 [`deploy/README.md`](deploy/README.md)**——
> 含 systemd unit、TLS、权限加固、排查顺序、升级回滚。
> 可运行的节点示例见 [`examples/node_run.py`](examples/node_run.py)。

协调端（有独立 IP 的那台）：

```bash
export NXS_SECRET_HUB=$(openssl rand -hex 32)
export NXS_SECRET_NODE_A=$(openssl rand -hex 32)
export NXS_OPS_SECRET=$(openssl rand -hex 32)   # ⚠ 不设 = 运维接口关闭
nxs doctor --config config/local.yaml --production   # 先自检
nxs serve -c config/local.yaml                       # 监听 :8443
```

节点端（**只出站，不监听任何端口**）：

```python
from nxs.core.models import Level, RoleCard
from nxs.edge.client import HttpTransport
from nxs.sdk import NodeClient, NodeConfig

transport = HttpTransport("https://hub.example.com:8443/nxs",
                          node_id="node-a", secret=secret)
client = NodeClient(
    NodeConfig("node-a",
               handlers={"demo.echo": lambda p: {"echo": p["text"],
                                                 "level": "metadata"}},
               levels={"demo.echo": Level.METADATA},
               card=card,          # 给了 card 才有完整的节点侧边界
               renew_interval=5),  # 长任务自动续租
    transport,
)
client.run_forever()        # 没装 SSE 也能跑：纯短轮询
```

首次启动会打印"已注册但**尚未批准**"——这是正常的。去服务器上批准：

```bash
nxs ops approve node-a --url https://hub.example.com/nxs
```

> ⚠ **角色卡必须确定性生成**（不要放时间戳/随机串）。
> 审批绑定的是"那张卡片"，卡片一变批准就撤销。

### 设计原则

1. **只流转，不决策** —— 网络决定"怎么送"，节点决定"做什么"
2. **默认拒绝** —— 没有显式允许的即拒绝
3. **拒绝到实现层** —— 不该给的数据不是"判断后拦截"，而是根本没有这条代码路径
4. **只出不进** —— 协调节点不主动连任何节点，连接一律由节点发起
5. **不感知宿主与存储实现** —— 核心逻辑可替换、可测试
6. **不含任何 LLM 调用** —— 纯基础设施，不理解任务内容
7. **只记元数据** —— 审计不记业务内容，避免自己成为数据集中点

详见 [`docs/architecture.md`](docs/architecture.md)。

### 已实现（v0.6.0）

| 模块 | 状态 |
|---|---|
| 信封与签名（HMAC-SHA256 + 规范 JSON + 防重放） | ✅ |
| 角色卡与三维约束（角色 / 能力 / 信任约束） | ✅ |
| 配置中心（YAML + 严格校验 + 默认拒绝） | ✅ |
| 策略执行点（矩阵 / 白名单 / 配额，**默认拒绝**） | ✅ |
| 注册中心（人工批准 / 心跳 / 三档存活状态） | ✅ |
| 信箱与生命周期（租约 / 幂等 / **断电自愈**） | ✅ |
| 应答边界（字段白名单 / 等级上限 / 自由文本清洗） | ✅ |
| **节点侧应答边界**（数据离节点前裁剪，双层防御） | ✅ |
| 审计留痕（只记元数据 / 写入失败熔断） | ✅ |
| 存储后端 | memory · sqlite |
| **HTTP 传输层**（网关 + 节点客户端，走真实网络） | ✅ |
| **SSE 通知流**（只推"有活"，不推内容；断线自动退回短轮询） | ✅ |
| **自动匹配**（按能力选目标，确定性排序，不泄漏候选） | ✅ |
| **节点自注册 + 心跳**（不用改云端配置重启） | ✅ |
| **长任务续租**（`/nxs/ack`，不靠调大租约） | ✅ |
| **远程运维**（批准 / 吊销 / 释放租约，走 HTTP + 审计） | ✅ |
| **部署物**（systemd unit · 生产配置模板 · 部署自检 · 指南） | ✅ |
| CLI | verify · **doctor** · demo · serve · **ops** · **candidates** · status |

### 尚未实现

| 项 | 说明 |
|---|---|
| **能力匹配（Capability-Match）** | 当前用 Direct 模式（指定目标节点） |
| **会话任务图与依赖跟踪** | 目前只记录 `session_id` 用于审计聚合 |
| **大产物的产物通道** | 当前应答都走信封（小数据） |
| **mTLS** | 目前用共享密钥 HMAC；接口不变，Core 不感知认证方式 |
| **可观测与运维接口** | 指标、告警、在线吊销等仍走 CLI 与改配置 |

### 开发

```bash
make install     # 建 venv 并安装（仅 PyYAML 一个运行时依赖）
make test        # 190 个测试
make demo        # 八步验收
```

`traceability/MATRIX.md` 是可执行的追溯表 —— 由测试生成，覆盖 66 个
需求编号项，零缺口。

### 许可证

[Apache-2.0](LICENSE) · 版权声明见 [`NOTICE`](NOTICE)

---

## English

Nexus Mesh is a **standalone coordination infrastructure for heterogeneous AI agents**.

Agents built on different stacks, running in different environments and trust domains, join the same network with their own **identity, role, and trust domain** — then discover each other, delegate tasks, and return results.

The network does not care what framework you use, where you run, or who wrote you. It only cares about three things: **who you are, what you can do, and who may delegate what to you.** All three are declared in configuration, never hard-coded.

### What it solves

When a single agent cannot cover all work due to **environment isolation, capability boundaries, or data ownership**, multiple agents must collaborate. Connecting them naively raises two hard problems:

1. **How heterogeneous agents join** — agents built on different stacks, with different roles and trust domains, need a common basis for trusting each other's delegations. How does authority narrow at each hop?
2. **How much comes back** — in a cross-domain delegation, how do you ensure only the *minimum necessary* result is returned, rather than handing over the whole underlying dataset?

Nexus Mesh treats these as core concerns rather than add-ons.

### Core concepts

- **Node** — an agent joining the network under a **role** (Issuer / Executor / Relay / Observer)
- **Role Card** — a node's self-declaration: roles, capabilities, and **trust constraints** (max data level, issuable types, quota)
- **Capability** — a named task type with a parameter schema and a **response field allow-list**
- **Response Boundary** — enforced on the node side: data that must not be disclosed has **no code path at all**, rather than being filtered at runtime
- **Session** — the context of one user request, linking all tasks under it

### Design documents

**The code is derived from this directory** — requirements, the numbering
scheme, and the verification criteria live here:

- [`docs/design/`](docs/design/) — index and the document-driven workflow
- [`traceability/registry.py`](traceability/registry.py) — single source of
  truth for every requirement ID (executable)
- [`traceability/MATRIX.md`](traceability/MATRIX.md) — traceability matrix,
  generated by the test suite

The correspondence between docs, code, and tests is **checked automatically**
by `tests/test_traceability.py`.

### Quick start

```bash
git clone https://github.com/gzchenjiesong/nexus-mesh.git
cd nexus-mesh
make install
make demo
```

### Design principles

1. **Route, don't decide** — the network decides *how* to deliver; nodes decide *what* to do
2. **Deny by default**
3. **Deny at the implementation level** — no code path, not a runtime filter
4. **Egress-only** — the coordinator never initiates connections
5. **No host or storage awareness** — swappable and testable core
6. **No LLM calls** — pure infrastructure; it does not interpret task content
7. **Metadata only** — the audit log never stores business content

See [`docs/architecture.md`](docs/architecture.md).

### Status

**v0.6.0** — it can dispatch on its own. Omit `to_node` and the hub picks a
provider by capability, in a **deterministic** order (same-domain →
least-sufficient level → lowest load ratio → node id), then walks the
candidates through the policy matrix until one is allowed (353 tests).
The candidate list and the reason a match failed never leave the hub —
"who can do X" is topology (`nxs candidates` is operator-only).

* **Transport**: `POST /nxs/register` · `POST /nxs/ack` · `POST /nxs/publish`
  · `GET /nxs/poll` · `POST /nxs/result` · `POST /nxs/fail` · `GET /nxs/events`
  (SSE) · `GET /nxs/health` · `POST /nxs/ops/*`
* **Two signing layers**: envelopes sign task content; a separate request
  signature covers method + path + body digest for calls that carry no task
* **Egress-only**: nodes only dial out and never listen; the gateway never
  initiates a connection — push rides the connection the node opened
* **Two response boundaries**: the node trims its own output *before* sending
  (so a compromised hub gets nothing extra), the hub re-checks authoritatively
* **Auto-matching (M4)**: four-level deterministic ordering, no randomness
  anywhere — "why did it go to A and not B" is a reproducible query, not a
  mystery. Candidates are never exposed over HTTP; failed matches report the
  same code as "no such type" so topology and permission layout don't leak
* **Self-registration with approval semantics**: approval binds to *that role
  card*, not the node id — change the card and approval is revoked; a node can
  never approve itself, and ops endpoints are opt-in and never rate-limited
* **Adversarial evidence**: 20 tests that actively attack the wire — forged
  signatures, path/method/body tampering, replays, cross-node task theft,
  slow-loris, oversized bodies, stream-quota exhaustion
* **Deployable**: systemd unit, production config template, `nxs doctor`
  pre-flight check, and a step-by-step [`deploy/README.md`](deploy/README.md)

Not yet implemented: capability matching, session task graph,
large-artifact channel, mTLS, metrics/observability.
See [`docs/architecture.md`](docs/architecture.md) for the tracked gaps —
including the ones with **weaker evidence** (TLS has no automated test).

### License

[Apache-2.0](LICENSE)
