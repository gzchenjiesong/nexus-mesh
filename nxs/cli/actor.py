"""按身份解析凭据 —— 让 Agent 不必持有密钥。

## 为什么需要这个

Agent（Hermes / OpenClaw）是通过 `exec` 拿 shell 的。如果让它自己传
URL、CA、密钥去调 `nxs-cli`，会有三个问题：

1. **密钥会出现在它的上下文里** —— 也就是会进 LLM 的 prompt、会话历史、
   以及我们看不到的地方。密钥一旦进了模型上下文，就没法当作秘密了。
2. **容易写错** —— 长 hex 串，写错一个字符就是 `AU-SIG-01`，
   而那个错误看起来像"密钥不对"，实际可能是拼错。
3. **无法审计谁做了什么** —— 如果所有 Agent 共用一把密钥，
   审计日志里就看不出是哪个 Agent 发的。

所以：**凭据按身份存在文件里，Agent 只报自己的名字。**

```
Agent 上下文里有：  NXS_ACTOR=taskflow
                    ↓
nxs-cli 读：        /etc/nexus-mesh/agents/taskflow.env
                    ↓
里面才有：          NXS_COORDINATOR_URL / NXS_CA_FILE / NXS_SECRET
```

## 文件格式

```
NXS_NODE_ID=taskflow
NXS_COORDINATOR_URL=https://127.0.0.1:18800/nxs
NXS_CA_FILE=/etc/nexus-mesh/tls/ca.pem
NXS_SECRET=<64 位 hex>
```

**不叫 `NXS_SECRET_TASKFLOW` 而叫 `NXS_SECRET`**：文件本身就是按身份分的，
里面的键不需要再带名字。带名字反而会出现"文件名是 A、变量名是 B"的错配。

## 一条硬校验

文件里的 `NXS_NODE_ID` **必须等于文件名**。否则一个 Agent 可以
把 `--as coordinator` 指向自己的文件，用 coordinator 的身份发任务 ——
那不只是越权，还会让审计日志指向错误的节点。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..core.errors import ConfigError

#: 凭据目录。每个身份一个 `<node_id>.env`。
#:
#: 为什么按文件分而不是按环境变量分：环境变量是**继承**的 ——
#: 从父进程启动的所有子进程都会看到同一批变量。而不同 Agent
#: 用不同身份是常态。文件按名字查，天然隔离。
DEFAULT_ACTOR_DIR = Path("/etc/nexus-mesh/agents")

#: 允许的节点 ID 形状。收紧它有两个作用：
#: ① 防路径穿越（`--as ../../etc/passwd`）
#: ② 与角色卡、环境变量命名规则保持一致
_ACTOR_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

#: 凭据文件里必须有的键
REQUIRED_KEYS = ("NXS_NODE_ID", "NXS_COORDINATOR_URL", "NXS_SECRET")

#: 密钥最小长度（与 nxs.mgmt.config.MIN_SECRET_LENGTH 一致）
MIN_SECRET_LENGTH = 16


@dataclass(frozen=True)
class Actor:
    """一次调用的身份与连接参数。"""

    node_id: str
    url: str
    secret: bytes
    ca_file: str | None
    #: 这份凭据来自哪里（便于排查"为什么用的是这个身份"）
    source: str

    def masked(self) -> str:
        """可打印的形式。**永远不包含密钥**。"""
        return (f"节点 {self.node_id} · 服务端 {self.url}"
                f" · CA {self.ca_file or '(系统库)'} · 凭据来自 {self.source}")


def _parse_env_file(path: Path) -> dict[str, str]:
    """解析 `KEY=VALUE` 形式的文件。

    刻意比 ``os.environ`` 宽松一点点（允许空行与 `#` 注释），
    但**不做变量展开** —— 展开会让凭据文件变成一个小脚本，
    而它应该是纯数据。
    """
    out: dict[str, str] = {}
    try:
        # ⚠ 必须用 ``utf-8-sig``，不能只用 ``utf-8``。
        #
        #   Windows PowerShell 5.1 的 ``Set-Content -Encoding UTF8``
        #   **会写 BOM**（EF BB BF）。用纯 utf-8 读时，BOM 会留在第一个
        #   键名前面 —— 于是 ``NXS_NODE_ID`` 变成 ``\ufeffNXS_NODE_ID``，
        #   键名永远匹配不上。
        #
        #   而症状是"文件明明存在、内容也对，就是读不到" ——
        #   排查时几乎不会想到编码。``utf-8-sig`` 会自动剥掉 BOM，
        #   对没有 BOM 的文件行为完全一致（是安全的兼容写法）。
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"读不到凭据文件 {path}：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"凭据文件 {path} 不是 UTF-8 编码：{exc}\n"
            f"  用记事本另存为 UTF-8，或改用 Set-Content -Encoding ASCII"
        ) from exc

    for lineno, line in enumerate(raw.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ConfigError(f"{path}:{lineno}: 不是 KEY=VALUE 形式")
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


def load_actor(name: str, *, actor_dir: Path | None = None,
               env: dict[str, str] | None = None) -> Actor:
    """按名字加载一个身份的凭据。

    :param name: 节点 ID（同时是文件名，不含 ``.env``）
    :param actor_dir: 凭据目录，默认 :data:`DEFAULT_ACTOR_DIR`
    :param env: 环境变量覆盖（测试用）；省略时用 ``os.environ``
    :raises ConfigError: 名字非法、文件缺失、缺键、文件内容自相矛盾
    """
    if not _ACTOR_RE.match(name):
        # 这一条同时挡掉了 `--as ../x`、`--as /abs`、空串、大写等
        raise ConfigError(
            f"身份名非法：{name!r}（只允许小写字母、数字、连字符，且以字母或数字开头）"
        )

    directory = Path(actor_dir or os.environ.get("NXS_ACTOR_DIR")
                     or DEFAULT_ACTOR_DIR)
    path = directory / f"{name}.env"

    if not path.exists():
        # 把可选项列出来 —— 运维最常见的困惑是"我明明建了文件"
        # 而实际名字对不上（比如建了 a_b.env 而身份叫 a-b）
        available: list[str] = []
        if directory.is_dir():
            available = sorted(p.stem for p in directory.glob("*.env"))
        hint = (f"该目录下现有身份：{available}" if available
                else f"该目录（{directory}）下还没有任何身份文件")
        raise ConfigError(f"找不到身份 {name!r} 的凭据：{path}\n  {hint}")

    values = _parse_env_file(path)
    environ = dict(os.environ if env is None else env)

    # ---- 必填键 ----
    #
    # URL 与 SECRET 必须来自文件或环境（二者之一）。CA 可省（公网证书）。
    # 这里允许环境变量兜底，是为了本地调试时不必建文件 ——
    # 但在 Agent 场景下应该总是走文件，见模块文档。
    node_id = values.get("NXS_NODE_ID") or environ.get("NXS_NODE_ID", "")
    url = values.get("NXS_COORDINATOR_URL") or environ.get("NXS_COORDINATOR_URL", "")
    secret = values.get("NXS_SECRET") or environ.get("NXS_SECRET", "")
    ca_file = values.get("NXS_CA_FILE") or environ.get("NXS_CA_FILE", "")

    # ---- 自相矛盾检查 ----
    #
    # ⚠ 这条是关键。没有它，一个 Agent 可以把 `--as coordinator`
    #   指向自己的文件 —— 如果文件里的 NXS_NODE_ID 不被校验，
    #   它就会以 coordinator 的身份签名，既越权，也让审计指向错的节点。
    if node_id and node_id != name:
        raise ConfigError(
            f"凭据文件 {path} 里的 NXS_NODE_ID={node_id!r} 与身份名 {name!r} 不一致。\n"
            f"  这两个必须相同 —— 否则会以别的节点身份签名。"
        )

    missing = [k for k in ("NXS_COORDINATOR_URL", "NXS_SECRET")
               if not (values.get(k) or environ.get(k))]
    if missing:
        raise ConfigError(
            f"凭据文件 {path} 缺少：{', '.join(missing)}\n"
            f"  需要的键：{', '.join(REQUIRED_KEYS)}"
        )

    if len(secret) < MIN_SECRET_LENGTH:
        # 过短的密钥比没有更危险：它会让人以为配好了。
        # 与网关对运维密钥的处理一致（见 gateway._ops_secret_from_env）。
        raise ConfigError(
            f"凭据文件 {path} 里的 NXS_SECRET 过短"
            f"（{len(secret)} < {MIN_SECRET_LENGTH} 字节）"
        )

    return Actor(
        node_id=name,
        url=url.rstrip("/"),
        secret=secret.encode("utf-8"),
        ca_file=ca_file or None,
        source=str(path),
    )


def resolve_actor(explicit: str | None = None, *,
                  actor_dir: Path | None = None,
                  env: dict[str, str] | None = None) -> Actor:
    """解析"这次调用用哪个身份"。

    优先级：

    1. ``--as NAME``（命令行显式指定）
    2. ``NXS_ACTOR`` 环境变量 —— **桥接进程注入的就是这个**
    3. ``NXS_NODE_ID`` 环境变量（兼容手工调试）

    都不给时报错，并说明该怎么给。**不猜默认值** ——
    猜错身份会让任务以错误的名义发出，而那种错误在审计里
    看起来完全正常。
    """
    environ = dict(os.environ if env is None else env)
    name = explicit or environ.get("NXS_ACTOR") or environ.get("NXS_NODE_ID")

    if not name:
        raise ConfigError(
            "无法确定身份。请用 `--as <节点ID>`，或设置 NXS_ACTOR 环境变量。\n"
            "  桥接进程（hermes_node / openclaw_node）会自动注入 NXS_ACTOR。"
        )
    return load_actor(name, actor_dir=actor_dir, env=env)


def write_actor_file(name: str, *, url: str, secret: str,
                     ca_file: str | None = None,
                     actor_dir: Path | None = None) -> Path:
    """写一份凭据文件（运维/部署用）。

    权限设为 ``0600`` 且不跟随已有文件的权限 —— 凭据文件不该被
    同组或其他用户读到。目录不存在时会创建（``0700``）。

    返回值是写入的路径，便于调用方打印。
    """
    if not _ACTOR_RE.match(name):
        raise ConfigError(f"身份名非法：{name!r}")

    directory = Path(actor_dir or DEFAULT_ACTOR_DIR)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{name}.env"

    lines = [
        "# 由 nxs-cli 生成。这个文件等同于该节点的密钥，不要提交、不要外发。",
        f"NXS_NODE_ID={name}",
        f"NXS_COORDINATOR_URL={url}",
    ]
    if ca_file:
        lines.append(f"NXS_CA_FILE={ca_file}")
    lines.append(f"NXS_SECRET={secret}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


__all__ = [
    "Actor",
    "DEFAULT_ACTOR_DIR",
    "MIN_SECRET_LENGTH",
    "REQUIRED_KEYS",
    "load_actor",
    "resolve_actor",
    "write_actor_file",
]
