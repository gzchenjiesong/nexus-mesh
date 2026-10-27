# 部署指南

> 目标读者：要把 Nexus Mesh 部署到一台有公网的服务器上、并让内网机器上的
> 节点接入的人。按顺序做，每一步都有验证方法。
>
> 本文以 **Debian 12 + systemd** 为例（腾讯云 CVM 的默认镜像）。
> 其它发行版只需替换包管理与路径，逻辑不变。

---

## 0. 先想清楚一件事：谁连谁

```
        ┌──────────────────────────────────────────┐
        │  云服务器（有公网 IP）                     │
        │  nxs serve  ← 监听 127.0.0.1:8443        │
        │     ▲                                    │
        │     │ Nginx / Caddy（TLS 终止）           │
        │     │ 监听 0.0.0.0:443                    │
        └─────┼────────────────────────────────────┘
              │  ← 节点**主动连出**
    ┌─────────┴─────────┐
    │                   │
┌───▼────────────┐  ┌───▼────────────┐
│ 公司电脑        │  │ 家里 NAS 机器   │
│ work node      │  │ home node      │
│ 不监听任何端口   │  │ 不监听任何端口   │
└────────────────┘  └────────────────┘
```

**关键：节点侧永不监听端口。** 这是 Egress-Only，也是本系统能穿过
公司内网边界的原因。节点只需要"能出网"，不需要"能被连"。

---

## 1. 服务器准备

```bash
# 建一个专用于这个服务的系统用户（**不要用 root 跑**）
sudo useradd --system --home /opt/nexus-mesh --shell /usr/sbin/nologin nxs

# 目录
sudo mkdir -p /opt/nexus-mesh /etc/nexus-mesh /var/lib/nexus-mesh
sudo chown -R nxs:nxs /opt/nexus-mesh /var/lib/nexus-mesh
sudo chown root:nxs /etc/nexus-mesh
sudo chmod 750 /etc/nexus-mesh

# 依赖（Debian 12 自带 Python 3.11+）
sudo apt update && sudo apt install -y python3 python3-venv
```

---

## 2. 装代码

```bash
sudo -u nxs git clone https://github.com/gzchenjiesong/nexus-mesh.git /opt/nexus-mesh
cd /opt/nexus-mesh
sudo -u nxs python3 -m venv .venv
sudo -u nxs .venv/bin/pip install -e .
```

> 运行时依赖只有 PyYAML 一个。如果你看到装了一大堆，说明用的是旧版本。

---

## 3. 生成凭据（这一步最容易出错）

每个节点一把密钥，**全用 `openssl rand -hex 32` 生成，不要自己编**。

```bash
sudo install -m 600 -o root -g nxs /dev/null /etc/nexus-mesh/nxs.env
sudo tee /etc/nexus-mesh/nxs.env >/dev/null <<EOF
NXS_SECRET_HUB=$(openssl rand -hex 32)
NXS_OPS_SECRET=$(openssl rand -hex 32)
EOF
sudo chmod 600 /etc/nexus-mesh/nxs.env
```

⚠ **`NXS_OPS_SECRET` 必须设置。** 不设置时运维接口整体关闭——
听起来安全，但意味着**出故障时你无法远程批准/吊销节点**，只能改配置重启。
这是"平时无感、出事致命"的一项，`nxs doctor --production` 会因此报失败。

**把这一份 env 文件单独备份**到密码管理器里。丢了等于所有节点都要重新发凭据。

---

## 4. 写生产配置

```bash
sudo install -m 640 -o root -g nxs deploy/local.yaml.example /etc/nexus-mesh/local.yaml
sudo -e /etc/nexus-mesh/local.yaml     # 按需修改
```

必须改的地方：

| 配置项 | 改成什么 |
|---|---|
| `server.host` | 保持 `127.0.0.1`（前面放代理）；没有代理才改 `0.0.0.0` |
| `stores.data_dir` | `/var/lib/nexus-mesh` |
| `domains` | 你的真实信任域（如 `zone-work`） |
| `policy.matrix` | 你的真实任务类型与等级上限 |
| `policy.auto_approve_domains` | **留空**（`[]`） |

⚠ **`stores` 必须是 `sqlite`，不能是 `memory`。**
memory 后端跑起来完全正常，但重启后所有节点注册、所有在途任务全部丢失，
节点要重新注册、重新批准。`nxs doctor --production` 会拦住这个错误。

⚠ **`domains` 段不能省。** 工作电脑上的节点是自注册的——它在协调节点
启动时还不存在。域必须显式声明，否则矩阵里写不出 `hub → zone-work`。

---

## 5. 自检

```bash
cd /opt/nexus-mesh
sudo -u nxs sh -c 'set -a && . /etc/nexus-mesh/nxs.env && set +a && \
  .venv/bin/python -m nxs doctor --config /etc/nexus-mesh/local.yaml --production'
```

**必须全部 ✓ 才能继续。** 特别是这三项：

* `存储后端是持久化的` —— 否则重启即丢
* `运维通道已启用` —— 否则故障时进不来
* `未自动批准任何信任域` —— 否则新节点跳过审批

---

## 6. TLS

**推荐用 Caddy**，它自动申请并续期证书，比手工折腾 certbot 省事。

```bash
sudo apt install -y caddy
```

`/etc/caddy/Caddyfile`：

```
nxs.example.com {
    reverse_proxy 127.0.0.1:8443
}
```

Caddy 会自动处理 TLS、HTTP→HTTPS 跳转、证书续期。

> ⚠ 若节点侧用了 SSE 推送，Caddy 默认会缓冲响应，**必须关掉**：
> 见第 9 节的 `flush_interval -1`。或者干脆关掉推送（`sse_enabled: false`），
> 节点会退回固定周期轮询，功能不受影响。

Nginx 等价配置（若你更熟 Nginx）：

```nginx
server {
    listen 443 ssl http2;
    server_name nxs.example.com;

    ssl_certificate     /etc/letsencrypt/live/nxs.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nxs.example.com/privkey.pem;

    location /nxs/events {
        proxy_pass http://127.0.0.1:8443;
        proxy_http_version 1.1;
        # ★ SSE 必须关缓冲，否则事件会"攒够才发"
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }

    location / {
        proxy_pass http://127.0.0.1:8443;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        # 请求体上限要与 server.max_body_bytes 一致
        client_max_body_size 512k;
    }
}
```

---

## 7. 起服务

```bash
sudo install -m 644 deploy/nexus-mesh.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nexus-mesh
sudo systemctl status nexus-mesh
sudo journalctl -u nexus-mesh -f
```

日志里应当看到：

```
传输层
  监听      : http://127.0.0.1:8443/nxs
  TLS       : 未启用（仅限本地/内网）      ← 前面有代理时这是正常的
  推送(SSE) : 启用 · 心跳 15s
  限额      : 体 262144B · 并发 64 · 流 16
  运维接口  : 启用
```

⚠ 最后一行必须是 **启用**。若显示"未启用"，检查 `EnvironmentFile` 里的
`NXS_OPS_SECRET` 是否真的被读到了（`systemctl show nexus-mesh | grep Environment`）。

---

## 8. 节点接入（在另一台机器上做）

> ⚠ **先读完这三条再动手**——都是实测踩过的，不知道会卡很久。

**① 服务端必须先有该节点的凭据。** HMAC 的固有性质：验签要求服务端
知道对方密钥。所以加节点的最小流程是「在 `/etc/nexus-mesh/nxs.env` 加一行
`NXS_SECRET_<大写下划线节点ID>` → 重启服务」。

好消息是**不用改 yaml、不用写角色卡**（那是自注册解决的）。
坏消息是仍需重启——环境变量在进程启动时固定。

**② hub 自己也要批准。** `auto_approve_domains: []` 意味着默认拒绝对
所有人一视同仁，包括 hub。首次部署后必须先 `nxs ops approve hub`，
否则 hub 发不出任何任务。这是设计使然，不是 bug。

**③ 自注册的节点默认未批准。** 注册成功只代表"我来了"，
不代表"我可以干活"。必须运维显式批准。

节点侧只需要**能出网**，不需要任何入站端口。

```python
# work_node.py
from nxs.core.models import Level, RoleCard
from nxs.edge.client import HttpTransport
from nxs.sdk import NodeClient, NodeConfig

# ⚠ 角色卡必须**确定性生成**：不要放时间戳或随机串。
#   卡片一变，中心侧会把批准撤销——每次重启都要重新批准，很快就没人管审批了。
CARD = RoleCard.from_dict({
    "node_id": "work-laptop",
    "domain": "zone-work",
    "display_name": "Company Laptop",
    "heartbeat_interval": 30,
    "roles": {"executor": True, "issuer": False},
    "capabilities": [{
        "type": "work.calendar",
        "level": "summary",           # ← 声明即承诺：实现必须真的只产这个等级
        "mode": "sync",
        "response_fields": ["events", "level"],
    }],
    "constraints": {
        "max_response_level": "summary",
        "can_receive_from": ["hub"],
        "can_issue_types": [],
        "quota": {"tasks_per_day": 200, "concurrent_tasks": 2},
    },
})

def handle_calendar(params):
    # 你的真实业务逻辑
    return {"events": ["10:00 周会", "14:00 面试"], "level": "summary"}

transport = HttpTransport("https://nxs.example.com/nxs",
                          node_id="work-laptop",
                          secret=os.environ["NXS_SECRET_WORK_LAPTOP"].encode())

client = NodeClient(NodeConfig(
    "work-laptop",
    handlers={"work.calendar": handle_calendar},
    levels={"work.calendar": Level.SUMMARY},
    card=CARD,                 # ← 给了 card 才有完整的节点侧边界
    heartbeat_interval=60,     # 心跳（复用注册接口）
    renew_interval=5,          # 长任务续租
), transport)

client.run_forever()
```

首次启动会打印：

```
[work-laptop] 已注册但**尚未批准**（outcome=created, revision=1）
```

这是**正常的**——新节点不能自行加入。去服务器上批准：

```bash
sudo -u nxs sh -c 'set -a && . /etc/nexus-mesh/nxs.env && set +a && \
  .venv/bin/python -m nxs ops approve work-laptop \
    --url https://nxs.example.com/nxs'
```

> ⚠ **批准绑定的是它当前那张角色卡。** 节点之后改了卡片，
> 批准会自动撤销，需要重新批准。所以角色卡要确定性生成。

### 不指定目标也能派活（v0.6）

批准之后，hub 就可以不指定 `to_node` 直接投递，由系统按能力选目标：

```python
hub.publish(from_node="hub", task_type="work.calendar",
            params={"day": "2026-10-27"}, idempotency_key="daily-1")
# to_node 省略 → 自动匹配
```

排序是**确定性**的（同域 → 等级刚好够用 → 负载率低 → 节点 ID），
所以"为什么派给了它"是一个可以复现的问题：

```bash
nxs candidates --type work.calendar --from-node hub \
  -c /etc/nexus-mesh/local.yaml
```

---

## 9. 连通性验证

在节点侧：

```bash
# 探活（不认证，也不返回任何业务信息）
curl -s https://nxs.example.com/nxs/health
# → {"status": "ok"}
```

在服务器侧看节点是否真的在线：

```bash
nxs ops nodes --url https://nxs.example.com/nxs
```

```
节点            域          状态      批准  版本  能力
hub             hub         alive    ✓    1     work.summary
work-laptop     zone-work   alive    ✓    1     work.calendar
```

再确认"谁能干某类活"（自动投递的前提）：

```bash
nxs candidates --type work.calendar --from-node hub \
  -c /etc/nexus-mesh/local.yaml
```

```
候选执行者 · work.calendar  （来源 hub）
  #  节点          域          等级      负载    同域
  1  work-laptop   zone-work   summary   0/2     -
排序依据：① 同域优先 ② 等级刚好够用 ③ 负载率（工作/并发上限）④ 节点 ID 字典序
```

> ⚠ 候选名单**只在 hub 本机可见**，没有对应 HTTP 端点——
> "谁能干这个活"是拓扑信息。节点侧拿不到，也不需要拿。

最后看系统健康度：

```bash
export NXS_OPS_SECRET=<与服务端一致>
nxs metrics --url https://nxs.example.com/nxs
```

```
运行时指标  （已运行 3600s）
  提交 42 · 完成 40 · 失败 0 · 拒绝 2
  延迟 p50=12.3ms  p95=45.6ms  p99=89.1ms  （128 个样本）

节点
  节点          域          状态      收到    完成    失败    待办    执行中   水位    字节
  work-laptop   zone-work   alive     42      40      0       0       0        0.0×    3500

系统级关联异常：无
```

> ⚠ **这里没有阈值告警，是设计如此。** 只报"单看正常、合起来可疑"的组合
> （如"字节大 **且** 任务多"= 疑似批量导出；"积压高 **且** 零产出"= 静默卡死）。
> 阈值告警会在正常业务上天天响，响到没人看就失去了意义。
> 设计理由见 [`docs/observability.md`](../docs/observability.md)。
>
> ⚠ **指标是内存态，重启归零。** 长期趋势看审计日志。

---

## 9.5 看一眼系统状态：观察窗（可选）

不想敲命令时，可以起一个**只读**的网页看板。

```bash
# 在服务器上（注意只听 127.0.0.1）
export NXS_OPS_SECRET=...            # 与 hub 启动时同一个值
nxs ui --url https://nxs.example.com/nxs --port 8080
```

它会打印一行带 token 的链接。**在本机**做端口转发：

```bash
ssh -N -L 8080:127.0.0.1:8080 root@你的服务器
```

然后浏览器打开那一行链接（token 会立刻换成 cookie，地址栏恢复干净）。

### 三个要知道的点

**① 它只读。** 页面上没有任何"批准 / 吊销"按钮。看到异常节点，
切回终端敲 `nxs ops revoke <节点>`。这是刻意的：写操作要引入 CSRF、
二次确认、误操作恢复，而审批动作天然低频。

**② 它不碰数据库。** 数据全部经 `/nxs/ops/*` 取得（用你的运维密钥），
所以"能看什么"完全由运维端点决定。它不 import 存储后端、不读 sqlite。
这条有测试守着（`import nxs.ui` 之后 `nxs.app` 与 `nxs.backends` 都不该
出现在 `sys.modules` 里）。

**③ 它开不了公网。** `--host 0.0.0.0` 会被拒绝并提示用 SSH 隧道。
所以你**不需要**为它开防火墙端口，也不需要证书 —— 传输加密由 SSH 提供。

### 想长期开着？

它和 hub 是两个进程，可以各写一个 systemd unit。注意 `nxs ui` 的
进程环境里要有 `NXS_OPS_SECRET`（这就是它能读运维端点的原因），
所以 unit 里用 `EnvironmentFile=` 读与 hub 同一份 env 文件即可。

> ⚠ 别把 token 链接贴到聊天、工单或文档里。它等于一把钥匙。
> 重启 `nxs ui` 就换新的，旧链接自动失效。

---

## 10. 出问题时的排查顺序

| 现象 | 先查什么 |
|---|---|
| 节点注册返回 **401 `AU-SIG-01`** | **最常见**：服务端 `/etc/nexus-mesh/nxs.env` 里没有这个节点的 `NXS_SECRET_<ID>`。注意 ID 转大写下划线：`work-laptop` → `NXS_SECRET_WORK_LAPTOP`。加完**必须重启服务** |
| 节点注册返回 401 | 两边的 `NXS_SECRET_<节点ID>` 是否一致；节点 ID 拼写（含大小写与连字符） |
| hub 发不出任务 `MX-RECEIVE-03` | **hub 自己没批准**。`auto_approve_domains: []` 时谁都要批，先 `nxs ops approve hub` |
| 注册成功但一直"未批准" | `nxs ops nodes` 看有没有待批准；批准后节点下一次心跳会变 ✓ |
| 任务投递被拒 `MX-DOMAIN-02` | 矩阵里有没有 `hub → zone-work` 这条规则；`allow_types` 是否覆盖该类型 |
| 任务投递被拒 `WL-TYPE-404` | 节点的角色卡里有没有声明该能力；声明后要重新批准 |
| **自动投递总报 `WL-TYPE-404`** | 用 `nxs candidates --type <类型>` 看候选。若显示"无候选"且提示有节点待批准，先去 `nxs ops approve`。注意：候选全被矩阵拒绝时**也**报这个码（防止泄漏"有人但不给你"），所以要去 hub 的审计日志里看真实的拒绝码 |
| 产出被拒 `WL-FIELD-001` | 产出的字段是否都在 `response_fields` 里（**只报数量不报字段名**，去节点侧日志看） |
| 产出被拒 `WL-LEVEL-002` | 等级超过能力声明或卡片上限。**注意节点侧边界会先拦**，看节点日志而不是中心日志 |
| 长任务反复重试 | 节点 `renew_interval` 是否设置；续租失败会丢产出 |
| **产物被拒 `FM-BODY-04`** | 文件超过约 **186 KB**（信封上限 256 KB，base64 膨胀 33%）。小于它就塞信封；更大就走 `kind=reference` 传坐标。实测数据与判断见 [`docs/artifact-size.md`](../docs/artifact-size.md) |
| 想确认到底能传多大 | `python examples/probe_artifact_size.py` —— 实测，不是读配置推的 |
| **想确认系统现在健康吗** | `nxs metrics --url https://<域名>/nxs` —— 指标 + 系统级关联异常。注意它**没有阈值告警**（设计如此），只报"单看正常、合起来可疑"的组合 |
| 指标显示全 0 | 指标是**内存态**，进程重启即归零（审计日志才是账本）。若重启后一直 0，说明打点没接上，不是正常的 |
| 运维接口 501 | 服务器上 `NXS_OPS_SECRET` 没配或过短（<16 字节） |
| SSE 事件不实时 | 代理缓冲没关；或直接设 `sse_enabled: false` 退回轮询 |
| 重启后节点全没了 | `stores` 用了 memory —— 改成 sqlite |

---

## 11. 升级与回滚

```bash
cd /opt/nexus-mesh
sudo -u nxs git fetch --tags
sudo -u nxs git checkout v0.5.0
sudo -u nxs .venv/bin/pip install -e .
sudo systemctl restart nexus-mesh
sudo -u nxs .venv/bin/python -m nxs doctor --config /etc/nexus-mesh/local.yaml --production
```

回滚就是把 `git checkout` 换成旧 tag 再 restart。

⚠ **升级前备份数据目录**：

```bash
sudo systemctl stop nexus-mesh
sudo tar czf /root/nxs-backup-$(date +%F).tar.gz /var/lib/nexus-mesh /etc/nexus-mesh
sudo systemctl start nexus-mesh
```

数据目录里有 sqlite 库与审计日志（NDJSON）。审计是唯一的追溯依据，别丢。

---

## 12. 这个部署**不**保护什么

诚实声明，避免虚假的安全感：

* **工作电脑本身被攻破** —— 那是公司终端安全策略与 WorkBuddy 自身安全边界的事，
  本系统不覆盖。它只负责 A2A 这一条路径。
* **持有运维密钥的人** —— 运维能批准、能吊销，也就等于能放行。
  密钥要单独保管，不要和节点凭据放一起。
* **TLS 的自动化验证** —— 网关会真的加载证书并包 TLS，但项目里没有自签证书
  的自动化测试（不为此引入 `cryptography` 依赖）。部署后请手工确认：
  `curl -v https://nxs.example.com/nxs/health` 看握手是否正常。
* **拒绝服务** —— 有限流与并发上限，但没有做 DDoS 防护。
  那应该交给云厂商的防护或前置代理。
