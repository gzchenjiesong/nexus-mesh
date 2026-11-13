"""M15 · 观察窗 WebUI —— 只读的系统观察进程。

# ⚠ 改这个模块之前先读这段

**唯一目标（2026-10-30 对齐）：给人查看当前网络的运行情况，以及一些运行日志。**

    不管如何调整与增删功能，这个目标是不能丢弃的。

判断一个改动该不该进这个模块，只需问：**它有助于人查看"网络正在发生什么"吗？**

答不上来的就别加。典型反例：写操作按钮（那是改变、不是查看）、
主题切换（与运行情况无关）、多用户面板（单人运维场景没有"用户"）。

## 目标有两半，别只做前一半

    运行情况 —— 此刻：谁在线、谁被批准、积压多少、延迟多少   ✅ 已做
    运行日志 —— 历史：刚才发生了什么、谁发给谁、成败与原因   ❌ 未做

**"运行日志"不是可选项。** 只显示"此刻"的观察窗，在最需要它的时候
（出故障时）恰恰最没用 —— 2026-10-30 排查 work-macbook 死机就是如此，
最后全靠 journalctl 和手工解析 audit.ndjson。

## 与"不做任务详情"的边界

设计文档说「刻意不做任务详情页（涉及产出内容）」——**那条仍然成立**。
但要区分：

    任务元信息（时间/谁→谁/类型/状态/耗时/字节/错误码）  → 可以显示，这就是运行日志
    任务产出文本（answer / summary 正文）                → 不可显示，属另一安全等级

审计记录天然只有元信息，没有业务内容，所以展示审计流不违反那条约束。

---

设计见 ``docs/design/05-webui-design.md``。四条不可妥协的约束：

**① 只读**
本进程只实现 ``do_GET``。没有 ``do_POST`` / ``do_PUT`` / ``do_DELETE``，
不是"暂时没写"，而是**刻意不存在**。要改变系统状态请用 ``nxs ops``。
理由：写操作会引入 CSRF、二次确认、误操作恢复；而批准/吊销天然低频，
留在 CLI 里反而有 shell 历史作为操作记录。

**② 数据只经运维端点**
不 import 存储后端、不 import ``nxs.app``、不读 sqlite、不碰活的注册表。
只做一件事：用运维密钥调 coordinator 的 ``/nxs/ops/nodes`` 与 ``/nxs/ops/metrics``。

为什么这条不能妥协：直读存储等于**新增一条绕过策略层的数据通道**。
本项目的铁律是"所有安全策略的唯一裁决处"——数据出口也必须唯一。

**③ 只听 127.0.0.1**
不接受 ``0.0.0.0``。访问靠 SSH 隧道把端口搬过来（与 OpenViking 同一模式）。
这样不需要开防火墙端口、不需要证书 —— 传输加密由 SSH 提供。

**④ token 不落盘**
每次启动重新生成，只打印在终端。写进 HttpOnly cookie 后用于后续请求。
不做账号密码：能 SSH 登录这台机器的人本来就能 ``export NXS_OPS_SECRET``
直接 curl，给他一个密码框只是多一道他不需要的门。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import logging
import secrets
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from ..core.errors import ConfigError
from ..edge.client import OpsClient
from .page import SHELL, unauthorized_page
from .probe import probe_coordinator

log = logging.getLogger(__name__)

#: 允许绑定的地址。**刻意不含 ``0.0.0.0``。**
#:
#: 有人会想"我只在内网用，绑 0.0.0.0 方便"。但那样这个进程就把
#: 节点元数据暴露给了整个网段，而它连 HTTPS 都没有（靠 SSH 提供）。
#: 想从别的机器访问，用 SSH 隧道 —— 一行命令，不用改这里。
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _port_from_url(url: str) -> int:
    """从 coordinator URL 里解析端口，供本地探针查 accept 队列用。

    ⚠ **刻意从 URL 解析而不单独开一个配置项。**
    两个端口若分开配置，就会出现「探针查 18800、实际连 19000」
    这种查不到根因的错 —— 而那时所有数据看起来都"正常"，
    只是探针永远说端口没在听。**同源比灵活重要。**

    解析不出来时退回 18800（生产默认值），不报错 ——
    探针是辅助信息，不该因为一个小配置问题让整个 UI 起不来。
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return 18800
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 18800

#: cookie 名。带 token，HttpOnly + SameSite=Strict。
COOKIE_NAME = "nxs_ui"

#: cookie 保留多久（秒）。
#:
#: ⚠ 原来**没有** Max-Age，于是它是 session cookie —— 浏览器一关就没了，
#: 下次还得重新取链接。而 token 本身根本没有 TTL，所以这个"短命 cookie"
#: 保护不了任何东西：真正的失效点只有服务重启。
#:
#: 30 天与 hub 侧 Authelia 的 ``remember_me: '1M'`` 对齐，让两处
#: 「多久不用重新登录」的预期一致。
#:
#: ⚠ 注意它**不等于** token 活 30 天：token 一变（重启），cookie 里的旧值
#: 立刻失效。这个属性只解决"关浏览器"那一半。
COOKIE_MAX_AGE = 30 * 24 * 3600

#: token 的字节数（HMAC-SHA256 输出，32 字节 ≈ 256 位）。
TOKEN_BYTES = 32

#: token 派生标签。改它等于让**所有**已发出的 token 立刻作废。
#:
#: ⚠ 带 ``v1`` 是为了将来换算法时能干净切换：改成 ``v2`` 就全量轮换，
#: 不需要动 gen（那个是日常轮换用的）。
TOKEN_DERIVE_LABEL = "ui-token:v1"

#: 默认的 token 代数。运维改它来轮换（见 ``derive_token``）。
DEFAULT_TOKEN_GEN = "0"

#: 环境变量名：token 代数。
ENV_TOKEN_GEN = "NXS_UI_TOKEN_GEN"


def derive_token(ops_secret: bytes, gen: str = DEFAULT_TOKEN_GEN) -> str:
    """从运维密钥按 ``gen`` 派生观察窗的 token。**确定性、不落盘。**

    ## 为什么不再「每次重启随机生成」（2026-11-13 决策）

    原来用 ``secrets.token_urlsafe(32)``，每次启动换一个。问题实测过：
    一次会话里我做了 5 次部署，用户正在看的面板就失效了 5 次。

    而「每次变」提供的唯一价值是**泄露后重启即止血** —— 因为
    「不可猜测性」来自 256 位熵，跟频率无关。

    现在主入口统一到服务台的 Authelia 两因素认证之后（``nexus`` 子域），
    token 的角色从「唯一的门」降级为「Authelia 配错时的兜底」。
    这时固定化是划算的：部署不再踢人，而止血改为显式动作（改 gen 重启）。

    ## 三个性质

    * **确定性** —— 同 secret + 同 gen 永远得到同一 token。所以重启不变。
    * **不可预测** —— HMAC-SHA256 单向；不知道 ops_secret 就推不出来。
      持有 ops_secret 的人本来就能直接读运维端点，token 对他没有意义。
    * **可轮换** —— 改 gen 或改 ops_secret 都会让旧 token 立刻失效。

    ⚠ **不落盘**：token 每次启动从密钥重算，磁盘上没有它（铁律④）。
    这比「把 token 存文件再复用」干净 —— 后者会在磁盘留下第二份凭据。

    ## 为什么输出 43 个字符

    ``base64.urlsafe_b64encode(32 字节)`` 给 44 字符（含一个 ``=`` padding）。
    去掉 padding 得 43 字符，与原来 ``token_urlsafe(32)`` 的长度和字符集一致
    —— 这样 URL、cookie 的处理代码一行都不用改。
    """
    if len(ops_secret) < 16:
        # 与 nxs.mgmt.config.MIN_SECRET_LENGTH 对齐。弱密钥派生出的 token
        # 看起来一样随机，但实际可暴力枚举 —— 那种"安全假象"最难发现。
        raise ConfigError(
            f"ui: 运维密钥过短（{len(ops_secret)} < 16 字节），"
            f"无法安全派生观察窗 token"
        )
    message = f"{TOKEN_DERIVE_LABEL}:gen{gen or DEFAULT_TOKEN_GEN}".encode("utf-8")
    mac = hmac.new(ops_secret, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


#: 判定「这是本机访问」的主机名。这些不给自动恢复入口。
#:
#: ⚠ 理由是场景不同，不是安全：走 SSH 隧道的人**本来就在终端旁边**，
#: 他需要的是"在服务器上怎么再取一张"，而不是被跳去公网 hub
#: （隧道场景下浏览器未必连得上公网域名）。
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", ""})



def _clean_recover_url(raw: str | None) -> str | None:
    """校验并规整自动恢复入口。非法值返回 ``None``（降级为不提供）。

    ## 为什么必须校验，而不只是拼进页面

    这个 URL 会进两处：``href`` 属性 与 ``<script>`` 里的 JSON 字面量。
    只要它含非法字符，两个注入面都成立：

        href   ← ``"`` 或 ``javascript:``
        script ← ``</script>`` 或裸的引号

    来源是**运维配置**（unit 的 Environment / 命令行参数），不是用户输入，
    所以不是"信任边界"问题。但配置写错一个引号就能让 401 页面变成
    XSS 载体 —— 对一个安全敏感的页面，宁可直接拒绝。

    规则刻意收得很紧：只接受 ``https://`` 或 ``http://`` 开头、
    只含 URL 安全字符的字符串。枢纽的 enter 端点就是这种形态。
    """
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate.startswith(("https://", "http://")):
        return None
    # 只放行 URL 里正常出现的字符；< > " ' 空白 一律拒
    if any(ch in candidate for ch in '<>"\' \t\r\n\\'):
        return None
    if len(candidate) > 300:
        return None
    return candidate


class UiServer:
    """只读观察窗。构造即生成 token，:meth:`start` 才监听。"""

    def __init__(self, ops_url: str, ops_secret: bytes, *,
                 host: str = "127.0.0.1", port: int = 8080,
                 refresh_hint_seconds: int = 5,
                 ca_file: str | None = None,
                 coordinator_unit: str | None = None,
                 recover_url: str | None = None,
                 token_gen: str | None = None) -> None:
        if host not in ALLOWED_HOSTS:
            raise ConfigError(
                f"ui: 拒绝绑定 {host!r}。只允许 {sorted(ALLOWED_HOSTS)} —— "
                f"从别的机器访问请用 SSH 隧道："
                f"ssh -N -L {port}:127.0.0.1:{port} user@server"
            )
        if not ops_url.rstrip("/").endswith("/nxs"):
            raise ConfigError(f"ui: coordinator 地址必须以 /nxs 结尾: {ops_url!r}")

        self.ops_url = ops_url
        self.host = host
        self.port = port
        self.refresh_hint_seconds = refresh_hint_seconds
        # ca_file：coordinator 用自签证书时需要。观察窗自己在 systemd 里也有
        # SSL_CERT_FILE，但显式传参更可控（且不依赖部署环境恰好设了变量）。
        self._ops = OpsClient(ops_url, secret=ops_secret, ca_file=ca_file)

        # ---- 本地探针的配置 ----
        #
        #: coordinator 的 systemd unit 名。本地探针用它查进程状态。
        self.probe_unit = coordinator_unit or "nexus-mesh"
        #: coordinator 的端口 —— 从 ops_url 里解析，不另外配置。
        #:
        #: 为什么从 URL 解析而不单独给参数：**两个端口必须一致**，
        #: 分开配置就会出现"探针查 18800、实际连 19000"这种查不到根因的错。
        self.probe_port = _port_from_url(ops_url)

        # ---- 内存末次成功（阶段 1 的核心）----
        #
        #: 最后一次**成功**的 ops 数据。coordinator 挂掉时用它兜底。
        #:
        #: ⚠ **刻意只在内存里，不落盘。** 落盘的代价是：
        #:   · 磁盘上出现一份业务数据副本 → 触碰「数据出口唯一」铁律
        #:   · 要设计 TTL、清理、陈旧判定
        #: 而面板与 coordinator **同机**，coordinator 挂时面板通常还活着 ——
        #: 内存就够覆盖这个主场景。覆盖不到的是"面板也重启了"，
        #: 但那时本来也无从恢复，明说"无历史"比给一份过期数据更诚实。
        self._last_good: dict[str, Any] | None = None
        #: 保护 _last_good 的锁。ThreadingHTTPServer 会并发调 snapshot。
        self._snapshot_lock = threading.Lock()

        #: 观察窗 token。**从运维密钥派生，重启不变；不落盘。**
        #:
        #: ⚠ 这里改过一次方向（2026-11-13）。原来是
        #: ``secrets.token_urlsafe(32)``，每次启动换一个 —— 一次会话里
        #: 部署 5 次就踢掉用户 5 次。而「每次变」唯一的价值是止血，
        #: 「不可猜测性」来自 256 位熵与频率无关。
        #:
        #: 主入口统一到服务台 Authelia 之后，token 降级为兜底，固定化划算。
        #: 要止血就改 gen（环境变量 ``NXS_UI_TOKEN_GEN``）后重启，
        #: 见 :func:`derive_token`。
        self.token_gen = str(token_gen if token_gen is not None
                             else DEFAULT_TOKEN_GEN)
        self.token = derive_token(ops_secret, self.token_gen)

        #: 凭据失效后的**自动恢复入口**（通常是 hub 的 ``/api/nexus-enter``）。
        #:
        #: ## 为什么需要它
        #:
        #: 2026-11-13 实测确认：token 失效只有两种成因 ——
        #: 运维轮换（改 gen / 改密钥）和浏览器关掉（cookie 曾是 session cookie）。
        #: 而失效之后，旧版页面教用户「去服务器跑 journalctl」，
        #: 可用户站在浏览器里，手边没有终端。
        #:
        #: ## 为什么它不放松安全
        #:
        #: recover 目标在 hub，本身在 Authelia 两因素认证后面，且那条路径
        #: 会自己从 journald 取**当前** token。所以它拿到的是有效凭据，
        #: 而不是"绕过校验"。token 仍然不可预测、仍然不落盘。
        #:
        #: ## 为空时的行为
        #:
        #: 没有配置 → 页面不显示自动恢复，只留手动说明。
        #: 这是刻意留的降级：SSH 隧道场景下浏览器可能连不到公网 hub，
        #: 硬跳过去只会白屏。
        self.recover_url = _clean_recover_url(recover_url)

        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def entry_url(self) -> str:
        """带 token 的入口链接。**只打印到终端，不要贴到别处。**"""
        return f"{self.base_url}/?t={self.token}"

    def start(self) -> "UiServer":
        handler = _make_handler(self)
        httpd = ThreadingHTTPServer((self.host, self.port), handler)
        # 端口传 0 时让系统分配，回填真实端口
        self.port = httpd.server_address[1]
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="nxs-ui", daemon=True,
        )
        self._thread.start()
        log.info("观察窗已启动 %s", self.base_url)
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------------
    # 数据聚合
    # ------------------------------------------------------------------

    #: 观察窗每次拉多少条审计。
    #:
    #: 100 条的取舍：约 30 KB，够看清"最近发生了什么"；
    #: 人眼也不会去读第 500 行 —— 需要更深的历史该去翻文件或用 grep，
    #: 那不是观察窗的用途。
    AUDIT_LIMIT = 100

    #: 从 ``metrics.nodes[]`` 借到节点表上的**性能字段**。
    #:
    #: ⚠ **这两个数据源不是同一份东西**（2026-11-13 踩过）：
    #:
    #: ==========================  ==================================
    #: ``/nxs/ops/nodes``          ``metrics.nodes[]``
    #: ==========================  ==================================
    #: **身份/授权**视角            **性能**视角
    #: display_name / description  received / completed / failed
    #: role / role_definition      in_flight / pending / capacity
    #: domain / approved / state   backlog_ratio / bytes_out
    #: capabilities / self_limits  failure_rate / denials
    #: ==========================  ==================================
    #:
    #: 节点表要**同时**显示两边的东西，所以必须按 ``node_id`` 合并。
    #: 不合并的后果：前端读不到就是空，而 ``?? "—"`` 会把空静默降级成
    #: 一个貌似正常的「—」，**页面看起来没问题，数字全是空的**。
    #: 这正是本项目第四次同类型故障（前三次见 page.py 里角色列的注释）。
    NODE_PERF_FIELDS = (
        "received", "completed", "failed",
        "in_flight", "pending", "capacity",
        "backlog_ratio", "bytes_out", "failure_rate", "denials",
    )

    @classmethod
    def _merge_node_metrics(
        cls,
        nodes: list[Mapping[str, Any]],
        metrics: Mapping[str, Any] | None,
    ) -> list[Mapping[str, Any]]:
        """把 ``metrics.nodes[]`` 的性能字段按 ``node_id`` 并入节点表。

        ## 合并规则

        * 以 ``nodes``（``ops/nodes``）为准 —— 它决定**有哪些节点**。
          只在 metrics 里出现的节点不新增行：注册表是成员的唯一权威。
        * 性能字段**有则覆盖、无则留空**。留空（键不存在）而不是填 0：
          0 会被读成「真的没有流量」，而「不知道」必须长得像「不知道」。
        * ``metrics`` 为 ``None``（部分失败）时原样返回 —— 宁可少字段，
          也不能凭空造数字。

        ## 为什么放在服务端而不是前端合并

        放服务端才能用 Python 写**行为断言**（拿两个真实形状喂进去，
        检查合并结果）。前端合并只能靠 JS 字符串匹配来守护，
        而 ``"backlog_ratio" in SHELL`` 这种断言在字段读错数据源时**照样是绿的**
        —— 它就是这次漏掉问题的原因。
        """
        if not metrics:
            return nodes

        perf: dict[str, Mapping[str, Any]] = {}
        for row in metrics.get("nodes") or []:
            nid = row.get("node_id")
            if nid:
                perf[nid] = row
        if not perf:
            return nodes

        merged: list[Mapping[str, Any]] = []
        for node in nodes:
            row = dict(node)
            source = perf.get(node.get("node_id"))
            if source:
                for field in cls.NODE_PERF_FIELDS:
                    if field in source:
                        row[field] = source[field]
            merged.append(row)
        return merged

    def snapshot(self) -> dict[str, Any]:
        """拉一次节点、指标、审计流，并附带本地探针与陈旧判定。

        ## 三块数据的失败互不牵连（**重要**）

        观察窗最需要工作的时候，恰恰是 coordinator 出问题的时候。
        所以这里的原则是：**能拿到多少就显示多少。**

        | 情况 | 行为 |
        |---|---|
        | ``nodes`` 拿不到 | 用内存末次成功兜底；没有历史则整体返回 |
        | ``metrics`` 拿不到 | 节点照常显示，只标指标的错 |
        | ``audit`` 拿不到 | 节点与指标照常显示，只标审计的错 |

        ## 三种状态，前端必须区分（阶段 1）

        这是「监控面板最需要时最没用」那个结构性问题的解法：

        | 状态 | 数据来源 | 前端行为 |
        |---|---|---|
        | **实时** | 本次成功拉到 | 正常显示 |
        | **陈旧** | 内存里的末次成功 + 明确时间戳 | 整块灰显 + 大字标注「N 分钟前」 |
        | **无历史** | 面板刚启动就遇上不可达 | 只显示探针结果 + 「无历史数据」 |

        ⚠ **陈旧数据永不与实时数据混排。** 若 nodes 成功而 metrics 失败，
        那是「部分失败」（保持现状：实时 nodes + 无 metrics），**不是陈旧**。
        只有 nodes 整体拿不到时才进陈旧态 —— 因为它是页面主体。

        混排的后果很实在：人看到有节点列表，会以为数据是新的，
        而实际是十分钟前的拓扑 —— 那比空白更危险。

        ## 为什么把 get 和 set 都放进锁里

        `ThreadingHTTPServer` 会并发调 snapshot（浏览器多个标签、
        或者轮询与手动刷新撞上）。不加锁时两件事会坏：

          · 一个线程读到 `_last_good` 正被另一个线程替换 → 半新半旧
          · 两个线程同时写 → 后写的覆盖先写的，可能用旧数据覆盖新的

        锁的粒度就是整个 snapshot：它只有几次网络调用与内存赋值，
        串行化的代价远小于数据错乱。
        """
        with self._snapshot_lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        """``snapshot()`` 的实际实现。**调用方必须持锁。**"""
        now = datetime.now(timezone.utc).astimezone()
        out: dict[str, Any] = {
            "at": now.isoformat(timespec="seconds"),
            # 键名用 coordinator_url 而不是裸 coordinator：
            # 前者明确表示"协调节点的地址"，后者会被误读成某个叫 coordinator
            # 的**节点对象**。加 _url 后缀消掉这个歧义。
            "coordinator_url": self.ops_url,
            "nodes": [],
            "metrics": None,
            #: 审计流（运行日志）。与 nodes/metrics 平级。
            "audit": [],
            #: 审计被服务端截断时的标记 —— 不显示出来的话，
            #: 人会以为"只有这么多"，而实际是被上限砍了。
            "audit_truncated": False,
            #: 被滤掉的纯心跳条数。**必须显示出来** ——
            #: 否则人会疑惑"节点明明每 30 秒心跳，怎么日志里一条都没有"。
            #: 非零不代表出错，而是"这段时间没发生任务"。
            "audit_heartbeats": 0,
            #: 审计流**读取失败**的原因。为 ``None`` 表示这次读成功了。
            #:
            #: ⚠ 没有这个字段时，前端无法区分下面两种完全不同的情况：
            #:
            #:     读取成功但确实没有记录 → 「暂无审计记录」是对的
            #:     读取失败             → 说「暂无」就是**在撒谎**
            #:
            #: 空列表本身不携带这个区别 —— 所以必须单独给一个信号。
            "audit_error": None,
            "error": None,
            # ---- 阶段 1 新增 ----
            #: 是否在用陈旧数据（来自内存末次成功）。
            "stale": False,
            #: 陈旧数据的采集时刻。stale=False 时为 None。
            "stale_since": None,
            #: 陈旧数据是多少秒之前的。
            "stale_age_seconds": None,
            #: 面板刚启动、且从未成功拿到过数据。
            "no_history": False,
            #: 本地探针结果（进程/端口/wchan 等主机层事实）。
            "probe": None,
        }

        # ---- ① 拉 nodes（页面主体，决定是否进陈旧态）----
        nodes_ok = False
        try:
            out["nodes"] = list(self._ops.nodes())
            nodes_ok = True
        except Exception as exc:  # noqa: BLE001 —— 见 docstring
            out["error"] = f"{type(exc).__name__}: {exc}"

        if nodes_ok:
            self._fill_live(out)
        else:
            self._fill_stale(out, now)

        # ---- ② 本地探针：无论成败都跑 ----
        #
        # ⚠ 它必须独立于 ops 成败：
        #   · ops 失败时，它就是「为什么失败」的唯一线索
        #   · ops 成功时，它也能预警（比如 accept 队列在涨、线程数过少）
        #
        # 探针纯读本地文件与内核状态，约 20ms，不会拖慢快照。
        # 它自己也做了逐项容错（见 probe 模块），不会因某项失败而整体罢工。
        try:
            out["probe"] = probe_coordinator(
                ops_reachable=nodes_ok,
                unit=self.probe_unit,
                port=self.probe_port,
            )
        except Exception as exc:      # noqa: BLE001
            # 探针崩了不能拖垮快照 —— 它是**辅助信息**，不是主体
            out["probe"] = {
                "verdict": "unknown",
                "note": f"探针本身出错：{type(exc).__name__}: {exc}",
                "unavailable": ["probe"],
            }

        return out

    def _fill_live(self, out: dict[str, Any]) -> None:
        """nodes 已拿到 → 继续拉 metrics 与 audit，并存一份末次成功。"""
        try:
            out["metrics"] = self._ops.metrics()
        except Exception as exc:  # noqa: BLE001
            # 节点拿到了、指标没拿到 —— 仍然把节点显示出来，只标指标的错
            out["error"] = f"metrics: {type(exc).__name__}: {exc}"

        # ---- 合并：节点表要同时显示身份字段与性能字段 ----
        #
        # 位置有讲究：必须在**存末次成功之前**。
        # 否则陈旧兜底拿到的 last_good 是未合并的版本 ——
        # coordinator 一挂，水位/回传/吞吐列就集体变空，
        # 而"挂掉的瞬间"恰是最需要看这些数字的时候。
        out["nodes"] = self._merge_node_metrics(out["nodes"], out["metrics"])

        try:
            audit = self._ops.audit(limit=self.AUDIT_LIMIT)
            out["audit"] = list(audit.get("records") or [])
            out["audit_truncated"] = bool(audit.get("truncated"))
            out["audit_heartbeats"] = int(
                audit.get("filtered_heartbeats") or 0)
        except Exception as exc:  # noqa: BLE001
            # ⚠ 审计拿不到**不能**影响前两块。
            #   常见原因：旧版 coordinator 没有这个端点（501）、
            #   或有端点但读文件失败。两种情况下节点与指标都是好的。
            prev = out["error"]
            marker = f"audit: {type(exc).__name__}: {exc}"
            out["error"] = f"{prev} · {marker}" if prev else marker
            # 单独记一份：前端要用它区分「没有记录」与「读不到」
            out["audit_error"] = f"{type(exc).__name__}: {exc}"

        # ---- 存一份末次成功 ----
        #
        # ⚠ 用 deepcopy 而不是引用：metrics 是嵌套 dict，若只存引用，
        # 下一轮 `out["metrics"] = self._ops.metrics()` 不会污染它（新对象），
        # 但 `out` 本身被调用方拿去序列化后可能被改。
        # 复制一份最省心 —— 这些数据加一起不到 100 KB。
        self._last_good = {
            "at": out["at"],
            "nodes": copy.deepcopy(out["nodes"]),
            "metrics": copy.deepcopy(out["metrics"]),
            "audit": copy.deepcopy(out["audit"]),
            "audit_truncated": out["audit_truncated"],
            "audit_heartbeats": out["audit_heartbeats"],
        }

    def _fill_stale(self, out: dict[str, Any], now: datetime) -> None:
        """nodes 拿不到 → 用内存末次成功兜底，或标记无历史。"""
        last = self._last_good
        if last is None:
            # 面板刚启动就遇上不可达 —— 没有历史可用。
            # ⚠ 明说 no_history，不要用空列表冒充"网络里没有节点"。
            #   后者会让人以为注册表是空的，而实际是没读到。
            out["no_history"] = True
            # 运行日志同理：从来没连上过 coordinator，就没读过审计文件。
            # 让前端说「暂无审计记录」等于撒谎 —— 那是「确实没有」的意思。
            out["audit_error"] = "coordinator 不可达，从未读到过运行日志"
            return

        out["nodes"] = copy.deepcopy(last["nodes"])
        out["metrics"] = copy.deepcopy(last["metrics"])
        out["audit"] = copy.deepcopy(last["audit"])
        out["audit_truncated"] = last["audit_truncated"]
        out["audit_heartbeats"] = last["audit_heartbeats"]
        out["stale"] = True
        out["stale_since"] = last["at"]
        try:
            since = datetime.fromisoformat(last["at"])
            out["stale_age_seconds"] = round((now - since).total_seconds(), 1)
        except (ValueError, TypeError):
            # 时间戳格式意外 —— 不猜年龄，但 stale 标记仍然有效
            out["stale_age_seconds"] = None


# ---------------------------------------------------------------------------
# 请求处理
# ---------------------------------------------------------------------------

def _make_handler(server: UiServer) -> type[BaseHTTPRequestHandler]:
    """生成 handler 类。用闭包把 server 传进去，避免全局状态。"""

    class Handler(BaseHTTPRequestHandler):
        #: 版本号不回显 Python 版本（少给一点指纹）
        server_version = "nxs-ui"
        sys_version = ""

        # ---- 输出辅助 --------------------------------------------------

        def _send_bytes(self, status: int, body: bytes, *,
                        content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # 观察窗不该被任何页面内嵌（防点击劫持）
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            # 这里没有外链资源，禁止一切外部加载是零成本的收紧
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'; "
                "img-src 'none'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'none'",
            )
            # token 在 cookie 里，绝不能被 Referer 带到别处
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, body, content_type="application/json; charset=utf-8")

        def _send_text(self, status: int, text: str) -> None:
            self._send_bytes(status, text.encode("utf-8"),
                             content_type="text/plain; charset=utf-8")

        def _send_unauthorized(self) -> None:
            """401 —— 返回一个**说清原因与做法**的页面。

            ⚠ 这里最关键的一点：**两种情况返回完全相同的页面**。
            区分"token 过期"与"没带 token"会泄露信息（攻击者能据此判断
            某个 token 是否曾经有效），与网关拒绝时只回 rule_id 同理。

            为什么必须是 HTML 页面而不是纯文本：用户的真实场景是
            **刷新一个带旧 token 的标签** —— 那一步在换 cookie 阶段就被拒，
            根本加载不到主页面。返回纯文本时用户只看到裸的
            "unauthorized" 一个词，会以为系统坏了。

            ## 自动恢复入口按 Host 决定给不给（2026-11-13）

            经 hub 反代进来时 Host 是真实域名，此时给自动恢复（跳回 hub
            换新 token）；SSH 隧道/本机直连时 Host 是 127.0.0.1，不给 ——
            走隧道的人就在终端旁边，他需要的是手动取链接的说明。
            """
            host = (self.headers.get("Host") or "").split(":")[0].lower()
            recover = (server.recover_url
                       if host and host not in _LOCAL_HOSTS else None)
            self._send_bytes(HTTPStatus.UNAUTHORIZED,
                             unauthorized_page(recover).encode("utf-8"),
                             content_type="text/html; charset=utf-8")

        def _redirect(self, location: str, *, cookie: str | None = None) -> None:
            extra = {"Location": location}
            if cookie:
                extra["Set-Cookie"] = cookie
            # 303：把 POST 语义之外的一切都变成 GET，且**换掉 URL**
            # （URL 里的 token 不能留在地址栏/历史里）
            self._send_bytes(HTTPStatus.SEE_OTHER, b"", content_type="text/plain",
                             extra=extra)

        # ---- 鉴权 ------------------------------------------------------

        def _cookie_token(self) -> str:
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE_NAME:
                    return value.strip()
            return ""

        @staticmethod
        def _same(a: str, b: str) -> bool:
            # 常数时间比较：token 是唯一凭据，不该给计时侧信道
            return secrets.compare_digest(a, b)

        def _authorized(self) -> bool:
            return self._same(self._cookie_token(), server.token)

        # ---- 路由（只有 GET） -------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的约定
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            # ① 带 token 的入口：换 cookie 后跳走，让 URL 恢复干净
            query = parse_qs(parsed.query)
            presented = (query.get("t") or [""])[0]
            if presented:
                if not self._same(presented, server.token):
                    # 不区分"token 错"与"没有 token"，少给一点信息
                    self._send_unauthorized()
                    return
                cookie = (
                    f"{COOKIE_NAME}={server.token}; HttpOnly; SameSite=Strict; "
                    # Max-Age：不加的话是 session cookie，浏览器一关就没了。
                    # token 本身没有 TTL，所以"短命 cookie"并不提供额外保护，
                    # 只让人多跑一趟去重新取链接。
                    f"Max-Age={COOKIE_MAX_AGE}; Path=/"
                )
                self._redirect("/", cookie=cookie)
                return

            # ② 其余一律要求 cookie
            if not self._authorized():
                self._send_unauthorized()
                return

            if path == "/":
                self._send_bytes(HTTPStatus.OK, SHELL.encode("utf-8"),
                                 content_type="text/html; charset=utf-8")
                return
            if path == "/api/state":
                self._send_json(HTTPStatus.OK, server.snapshot())
                return
            self._send_text(HTTPStatus.NOT_FOUND, "not found\n")

        # ---- 显式拒绝写方法 ---------------------------------------------
        #
        # 不写 `do_POST = do_PUT = ... = 405` 那种一行赋值：那样
        # "支持哪些方法"要靠读代码推断。这里逐个显式声明，
        # 并且**每个都把方法名写进响应**，便于对着日志排查。

        def _read_only(self) -> None:
            """405 + ``Allow: GET``。

            带 ``Allow`` 不是形式主义：HTTP 规定 405 必须说明允许哪些方法，
            否则调用方只能猜。而这里"允许什么"正是设计的一部分。
            """
            body = (f"{self.command} not allowed: 观察窗是只读的。"
                    f"要改变状态请用 nxs ops。\n").encode("utf-8")
            self._send_bytes(HTTPStatus.METHOD_NOT_ALLOWED, body,
                             content_type="text/plain; charset=utf-8",
                             extra={"Allow": "GET"})

        def do_POST(self) -> None:      # noqa: N802
            self._read_only()

        def do_PUT(self) -> None:       # noqa: N802
            self._read_only()

        def do_PATCH(self) -> None:     # noqa: N802
            self._read_only()

        def do_DELETE(self) -> None:    # noqa: N802
            self._read_only()

        def do_HEAD(self) -> None:      # noqa: N802
            """HEAD **不是**写方法，但也没实现。

            给它单独一条路径，因为用「观察窗是只读的」来拒绝一个
            语义上只读的方法会让人困惑——那条消息该留给真正的写方法。
            """
            self._send_bytes(HTTPStatus.METHOD_NOT_ALLOWED, b"",
                             content_type="text/plain; charset=utf-8",
                             extra={"Allow": "GET"})

        # ---- 日志 ------------------------------------------------------

        def log_message(self, fmt: str, *args: Any) -> None:
            # 默认实现往 stderr 打每一行请求。5 秒轮询会刷满终端，
            # 反而盖住启动时那行**真正重要**的 token 链接。
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------

def serve_ui(ops_url: str, ops_secret: bytes, *, host: str = "127.0.0.1",
             port: int = 8080, ca_file: str | None = None,
             recover_url: str | None = None
             ) -> tuple[UiServer, str]:
    """启动并返回 ``(server, entry_url)``。调用方负责打印 entry_url。"""
    server = UiServer(ops_url, ops_secret, host=host, port=port,
                      ca_file=ca_file, recover_url=recover_url).start()
    return server, server.entry_url
