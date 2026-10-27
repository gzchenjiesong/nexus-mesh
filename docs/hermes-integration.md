> 📖 **总览在 [`docs/agent-bridges.md`](agent-bridges.md)** ——
> 那一份讲桥接的共同原理、两个 Agent 的安全对比、以及三跳协作实测。
> 本文件是 Hermes 这一侧的细节。

# 把 Hermes Agent 接入 Nexus Mesh

> 实现：`examples/hermes_node.py` · 测试：`tests/test_hermes_node.py`
> 首次部署：2026-10-27，云服务器 `43.138.222.206`，profile=`working`

---

## 一、它做什么

一个 Nexus 节点，把收到的任务转成**一次 Hermes 单次调用**，把返回的文本作为产出回传。

```
协调节点 ──task──> 桥接 ──prompt──> hermes -z ──text──> 桥接 ──artifact──> 协调节点
```

实测延迟 **9–10 秒**（deepseek-v4-pro，简单问答）。

---

## 二、⚠ 这不是"加一个节点"那么简单

**通过 mesh 派任务，等于让远端触发一次 LLM 执行。** 所以在写业务逻辑前，
三件事必须定死。每一条都是刻意的：

### ① 工具集必须收窄（最重要）

`hermes -z` 默认带上该 profile 的全部工具，**包括终端、文件、代码执行**。
不收窄的话：

> **任何能向这个节点发任务的节点，都能让这台服务器执行任意命令。**

默认只给 `safe` —— Hermes 内置的最小集：

| 工具集 | 含什么 | 给不给 |
|---|---|---|
| `safe` | `web` + `vision` + `image_gen` | ✅ 默认 |
| `terminal` | shell 命令 | ❌ |
| `file` | 读写文件 | ❌ |
| `code_execution` | 跑代码 | ❌ |

放宽要用 `--toolsets` 显式指定。请记住范围一扩大，
**mesh 里每个能发任务的节点都跟着获得那个能力**。

### ② 产出只走自由文本通道

`answer` 声明为 `freetext`，边界会做三件事：剥控制字符、剥指令性前缀、
按 4096 字节截断。这不是完整的注入防护（那不存在），
而是把最低成本的攻击做不成。

### ③ 失败要**显式失败**

非零退出 / 超时 / 空输出 → 抛异常让任务落 `failed`。
**绝不返回空字符串充数** —— 那会让调用方以为"它答了，只是没内容"，
比失败更难排查。

---

## 三、部署（云服务器上）

### 1. 服务端准备

三处配置改动，缺一不可。每一处都对应一个刻意的边界：

```yaml
# /etc/nexus-mesh/local.yaml

policy:
  matrix:
    # 协调节点与 hermes-working 都在 cloud 域（同一台机器、同一信任等级）
    - from_domain: cloud
      to_domain: cloud
      allow_types: ["agent.ask"]      # ← 只放行这一个类型
      max_level: summary

nodes:
  - node_id: coordinator
    constraints:
      can_issue_types: ["work.*", "agent.ask"]   # ← 不加则报 MX-ISSUER-01
```

```bash
# 凭据（服务端与节点侧必须是同一个值）
echo "NXS_SECRET_HERMES_WORKING=<openssl rand -hex 32>" >> /etc/nexus-mesh/nxs.env
chmod 600 /etc/nexus-mesh/nxs.env
```

> ⚠ **改完必须重启协调节点。** 环境变量在进程启动时固定 ——
> 改了 env 文件不重启等于没改。这是实测踩到的坑：
> 不重启会得到 `AU-SIG-01`（签名校验失败），而那看起来像密钥写错了。

### 2. 启动桥接

```bash
cd /opt/nexus-mesh
set -a; . /etc/nexus-mesh/nxs.env; set +a
export NXS_COORDINATOR_URL=https://127.0.0.1:18800/nxs
export NXS_CA_FILE=/etc/nexus-mesh/tls/ca.pem

.venv/bin/python examples/hermes_node.py --dry-run   # 先探活 + 注册
```

`--dry-run` 会做两件**有序**的事，顺序是刻意的：

1. **先探活 Hermes**（一次真实调用，会烧少量 token）
2. 再注册节点

反过来（先注册后探活）的话，Hermes 有问题时会先注册成功，
然后每条任务都失败 —— 而失败原因藏在节点日志里，运维看不见。

### 3. 批准

```bash
nxs ops approve hermes-working --url https://127.0.0.1:18800/nxs \
  --ca-file /etc/nexus-mesh/tls/ca.pem
```

### 4. 常驻

`/etc/systemd/system/nexus-mesh-hermes.service`（已在服务器上）：

```ini
[Service]
# ⚠ 必须是 root：Hermes 的 venv、profile、凭据全在 root 名下。
#    这是本项目里唯一一个不以 nxs 用户运行的服务。
User=root
EnvironmentFile=/etc/nexus-mesh/nxs.env
Environment=HERMES_HOME=/root/.hermes/profiles/working
Environment=HERMES_TOOLSETS=safe
ExecStart=/opt/nexus-mesh/.venv/bin/python /opt/nexus-mesh/examples/hermes_node.py
Restart=always
```

---

## 四、怎么用

派一个任务：

```python
from nxs.edge.client import HttpTransport

coord = HttpTransport(url, node_id="coordinator", secret=SECRET, ca_file=CA)
coord.publish(
    from_node="coordinator",
    to_node="hermes-working",
    task_type="agent.ask",
    params={"question": "用两点说明多 Agent 协作的主要风险，每点不超过 30 字。"},
    idempotency_key="unique-key",
)
```

拿回的回答形如：

```
1. 传递失真：信息在多 Agent 间层层转述，易丢失或走样。
2. 失控漂移：目标、职责难统一，易相互冲突或偏离初衷。
```

产出结构：

```json
{
  "level": "summary",
  "kind": "structured",
  "data": {"answer": "...", "level": "summary"},
  "provenance": {"source": "hermes-working/agent.ask", "local_boundary": "passed"}
}
```

`local_boundary: passed` 说明**节点侧边界**已经跑过（数据离节点前裁剪过一次），
服务端还会再校验一次 —— 双层防御。

---

## 五、踩过的四个坑（都修了）

| 坑 | 症状 | 原因 |
|---|---|---|
| `HERMES_HOME` 传成命令行参数 | 回答"风格不对"，**不报错** | 它是**环境变量**。传错时 hermes 用默认 profile |
| `AU-SIG-01` 被报成"待批准" | 对着鉴权失败去点批准 | SDK 把异常吞成 `outcome=error`，桥接只看 `approved` |
| 改了 env 没重启协调节点 | 一直 `AU-SIG-01` | 环境变量在进程启动时固定 |
| 改了角色卡批准被撤销 | 突然"未批准" | 批准绑定的是**那张卡**，卡片一变就撤销 |

---

## 六、已知限制

| 限制 | 说明 |
|---|---|
| **每条任务烧 token** | deepseek-v4-pro。`quota.tasks_per_day: 100` 是刻意的成本闸门 |
| **延迟 9–10 秒起** | 靠 SDK 自动续租（5 秒一次），不靠调大租约 |
| **会写 Hermes 会话历史** | `-z` 每次一个 session。跑多了要清理（`hermes sessions`） |
| **与 gateway 并存** | 短进程 vs 常驻服务，共享 `HERMES_HOME` 的记忆，会话互不干扰 |
| **Agent 不知道自己在一个 mesh 里** | prompt 只说了"来自任务总线"，没给 mesh 全局上下文。要它做跨节点判断时，得在问题里带上背景 |

---

## 七、还没做的

- **反向协作**：目前是"协调节点 → hermes-working"单向。
  要让公司电脑的节点直接问它，需要加 `zone-work → cloud` 的 `agent.ask` 规则。
- **多轮对话**：现在是无状态单次问答。多轮要靠 `--resume`，会引入会话管理。
- **成本观测**：没有记录每条任务的 token 用量。要做的话应该记进审计（元数据，不含内容）。
