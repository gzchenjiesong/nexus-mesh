"""M15 · 观察窗的页面模板。

**这里只有一个静态 HTML 壳，不含任何数据。**

为什么不在服务端渲染数据（这是本模块最重要的安全决定）：

节点自报的 ``display_name`` / ``description`` 是不可信文本。
服务端校验已经拒了控制字符与超长，但**刻意没做 HTML 转义**
（转义是渲染层的责任，见 ``nxs/core/models.py`` 的说明）。

如果服务端拼 HTML，那么每一个插值点都必须记得转义一次。
漏一个就是 XSS —— 而这种"靠记得"的安全是迟早会破的。

所以改成：

    /api/state  →  纯 JSON（原样返回文本）
    浏览器      →  用 textContent 建 DOM（永不解析 HTML）

``textContent`` 与 ``innerHTML`` 的区别是**结构性**的：前者根本没有
"把字符串当标记解析"这一步。不是"小心避开了危险路径"，而是
那条路径不存在。这跟本项目「拒绝到实现层」是同一条思路。

代价：需要 JS。可接受——运维看板不是需要无障碍/无脚本兜底的场景。
"""

#: 静态壳。**任何用户/节点数据都不得拼进这个字符串。**
SHELL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nexus Mesh · 观察窗</title>
<style>
  :root {
    --bg: #fbfbfc; --fg: #1c1d21; --dim: #6b7280; --line: #e5e7eb;
    --card: #ffffff; --ok: #15803d; --warn: #b45309; --bad: #b91c1c;
    --accent: #1d4ed8;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14161a; --fg: #e6e7ea; --dim: #9aa1ab; --line: #2a2e35;
      --card: #1b1e23; --ok: #4ade80; --warn: #fbbf24; --bad: #f87171;
      --accent: #7aa2f7;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 28px 32px 60px;
    background: var(--bg); color: var(--fg);
    font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI",
          "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  }
  header { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; }
  h1 { font-size: 18px; margin: 0; font-weight: 650; letter-spacing: .2px; }
  .meta { color: var(--dim); font-size: 12.5px; }
  .meta b { color: var(--fg); font-weight: 600; }
  .spacer { flex: 1; }

  .bar { display: flex; gap: 10px; flex-wrap: wrap; margin: 20px 0 24px; }
  .tile {
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 12px 16px; min-width: 104px;
  }
  .tile .k { color: var(--dim); font-size: 11.5px; letter-spacing: .4px;
             text-transform: uppercase; }
  .tile .v { font-size: 21px; font-weight: 640; font-variant-numeric: tabular-nums; }
  .tile .u { font-size: 12px; color: var(--dim); font-weight: 400; }

  h2 { font-size: 13px; letter-spacing: .5px; text-transform: uppercase;
       color: var(--dim); margin: 28px 0 10px; font-weight: 600; }

  table { width: 100%; border-collapse: collapse; background: var(--card);
          border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
  th, td { text-align: left; padding: 11px 14px; border-bottom: 1px solid var(--line);
           vertical-align: top; }
  th { font-size: 11.5px; letter-spacing: .4px; text-transform: uppercase;
       color: var(--dim); font-weight: 600; background: transparent; }
  tr:last-child td { border-bottom: none; }

  .name { font-weight: 600; }
  .desc { color: var(--dim); font-size: 12.5px; margin-top: 2px;
          max-width: 42ch; word-break: break-word; }
  .id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 12.5px; color: var(--dim); }
  .caps { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
          font-size: 12px; color: var(--dim); }
  .tags { display: flex; gap: 4px; flex-wrap: wrap; }
  .tag { font-size: 11px; padding: 1px 7px; border-radius: 999px;
         border: 1px solid var(--line); color: var(--dim); }
  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
         margin-right: 6px; vertical-align: 1px; }
  .alive { color: var(--ok); }   .alive .dot { background: var(--ok); }
  .stale { color: var(--warn); } .stale .dot { background: var(--warn); }
  .revoked { color: var(--bad); } .revoked .dot { background: var(--bad); }
  .yes { color: var(--ok); font-weight: 600; }
  .no { color: var(--warn); font-weight: 600; }

  .anomaly { border-left: 3px solid var(--bad); background: var(--card);
             border-radius: 0 8px 8px 0; padding: 11px 15px; margin-bottom: 8px; }
  .anomaly .r { font-family: ui-monospace, Menlo, monospace; font-size: 12.5px;
                color: var(--bad); font-weight: 600; }
  .anomaly .why { color: var(--dim); font-size: 12.5px; margin-top: 3px; }
  .none { color: var(--ok); }
  .err { border: 1px solid var(--bad); color: var(--bad); background: var(--card);
         border-radius: 8px; padding: 13px 16px; margin: 18px 0; }
  footer { margin-top: 34px; color: var(--dim); font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>Nexus Mesh</h1>
  <span class="meta">观察窗 · 只读</span>
  <span class="spacer"></span>
  <span class="meta" id="stamp">连接中…</span>
</header>

<div id="error"></div>

<div class="bar" id="tiles"></div>

<h2>节点</h2>
<table>
  <thead>
    <tr>
      <th style="width:26%">名称 / 简介</th>
      <th style="width:13%">节点</th>
      <th style="width:10%">域</th>
      <th style="width:10%">状态</th>
      <th style="width:8%">批准</th>
      <th style="width:22%">角色</th>
      <th>能力</th>
    </tr>
  </thead>
  <tbody id="nodes"></tbody>
</table>

<h2>系统级关联异常</h2>
<div id="anomalies"></div>

<footer>
  只读视图 —— 这里没有任何写操作。批准 / 吊销请用
  <code>nxs ops</code>。<br>
  数据经 hub 的运维端点取得，每 5 秒刷新。指标为内存态，重启归零。
</footer>

<script>
"use strict";

const ROLE_LABELS = {
  issuer: "发布", executor: "执行", relay: "中转",
  observer: "只读", human_interface: "人机入口"
};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  // ★ 只用 textContent。绝不使用 innerHTML 承载任何数据 ——
  //   节点自报的文本不可信，textContent 从结构上不解析标记。
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function tile(label, value, unit) {
  const box = el("div", "tile");
  box.appendChild(el("div", "k", label));
  const v = el("div", "v", value);
  if (unit) {
    const u = el("span", "u", " " + unit);
    v.appendChild(u);
  }
  box.appendChild(v);
  return box;
}

function renderTiles(m) {
  const host = document.getElementById("tiles");
  host.replaceChildren();
  const t = (m && m.totals) || {};
  host.appendChild(tile("提交", t.submitted ?? 0));
  host.appendChild(tile("完成", t.completed ?? 0));
  host.appendChild(tile("失败", t.failed ?? 0));
  host.appendChild(tile("拒绝", t.denied ?? 0));
  const lat = (m && m.latency_ms) || {};
  const ms = (v) => (v === null || v === undefined) ? "—" : Math.round(v);
  host.appendChild(tile("p50", ms(lat.p50), "ms"));
  host.appendChild(tile("p95", ms(lat.p95), "ms"));
  host.appendChild(tile("p99", ms(lat.p99), "ms"));
  host.appendChild(tile("样本", (m && m.latency_samples) ?? 0));
}

function renderNodes(nodes) {
  const body = document.getElementById("nodes");
  body.replaceChildren();
  if (!nodes.length) {
    const tr = el("tr");
    const td = el("td", "meta", "（注册表为空）");
    td.colSpan = 7;
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const n of nodes) {
    const tr = el("tr");

    const cName = el("td");
    cName.appendChild(el("div", "name", n.display_name || "（未命名）"));
    if (n.description) cName.appendChild(el("div", "desc", n.description));
    tr.appendChild(cName);

    const cId = el("td");
    cId.appendChild(el("span", "id", n.node_id));
    tr.appendChild(cId);

    tr.appendChild(el("td", null, n.domain));

    const cState = el("td");
    const state = String(n.state || "unknown");
    const s = el("span", state, state);
    s.prepend(el("span", "dot"));
    cState.appendChild(s);
    tr.appendChild(cState);

    const cAppr = el("td");
    cAppr.appendChild(n.approved
      ? el("span", "yes", "✓ 已批准")
      : el("span", "no", "· 待批准"));
    tr.appendChild(cAppr);

    const cRoles = el("td");
    const tags = el("div", "tags");
    const roles = n.roles || {};
    for (const [key, label] of Object.entries(ROLE_LABELS)) {
      if (roles[key]) tags.appendChild(el("span", "tag", label));
    }
    if (!tags.childNodes.length) tags.appendChild(el("span", "tag", "—"));
    cRoles.appendChild(tags);
    tr.appendChild(cRoles);

    const caps = (n.capabilities || []).join(", ") || "—";
    tr.appendChild(el("td", "caps", caps));

    body.appendChild(tr);
  }
}

function renderAnomalies(list) {
  const host = document.getElementById("anomalies");
  host.replaceChildren();
  if (!list || !list.length) {
    host.appendChild(el("div", "none", "无。"));
    return;
  }
  for (const a of list) {
    const box = el("div", "anomaly");
    box.appendChild(el("div", "r", (a.severity || "") + " · " + (a.rule || "")));
    if (a.detail) box.appendChild(el("div", "why", a.detail));
    if (a.rationale) box.appendChild(el("div", "why", a.rationale));
    host.appendChild(box);
  }
}

function showError(msg) {
  const host = document.getElementById("error");
  host.replaceChildren();
  if (!msg) return;
  host.appendChild(el("div", "err", msg));
}

async function refresh() {
  try {
    const res = await fetch("api/state", { headers: { "Accept": "application/json" } });
    if (res.status === 401) {
      document.getElementById("stamp").textContent = "未授权 —— 请用带 token 的链接打开";
      return;
    }
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    showError(data.error ? ("hub 不可达：" + data.error) : "");
    renderTiles(data.metrics);
    renderNodes(data.nodes || []);
    renderAnomalies(data.metrics ? data.metrics.anomalies : []);
    document.getElementById("stamp").textContent =
      "最后更新 " + new Date().toLocaleTimeString("zh-CN");
  } catch (e) {
    showError("取数据失败：" + e.message);
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
