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

  .pending { border-left: 3px solid var(--warn); background: var(--card);
             border-radius: 0 8px 8px 0; padding: 13px 16px; margin: 18px 0; }
  .pending .t { font-weight: 600; color: var(--warn); }
  .pending .how { color: var(--dim); font-size: 12.5px; margin-top: 6px; }
  .pending code { background: var(--bg); border: 1px solid var(--line);
                  border-radius: 4px; padding: 1px 5px; font-size: 12px; }
  .row-pending { background: color-mix(in srgb, var(--warn) 9%, transparent); }

  .log { width: 100%; border-collapse: collapse; background: var(--card);
         border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
         font-size: 12.5px; }
  .log th, .log td { padding: 7px 11px; border-bottom: 1px solid var(--line);
                     white-space: nowrap; }
  .log th { font-size: 11px; letter-spacing: .4px; color: var(--dim);
            font-weight: 600;
            text-transform: none; }
  .log tr:last-child td { border-bottom: none; }
  .log .t { font-family: ui-monospace, Menlo, monospace; color: var(--dim); }
  .log .flow { font-family: ui-monospace, Menlo, monospace; }
  .log .num { font-variant-numeric: tabular-nums; text-align: right;
              color: var(--dim); }
  .log .rule { font-family: ui-monospace, Menlo, monospace; font-size: 11.5px; }
  .o-completed { color: var(--ok); font-weight: 600; }
  .o-failed    { color: var(--bad); font-weight: 600; }
  .o-denied    { color: var(--warn); font-weight: 600; }
  .o-register  { color: var(--dim); }
  .o-ops       { color: var(--accent); }
  .log-empty { color: var(--dim); padding: 16px 14px; }
  .trunc { color: var(--warn); font-size: 12px; margin-top: 6px; }
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
<div id="pending"></div>

<div class="bar" id="tiles"></div>

<h2>节点</h2>
<table>
  <thead>
    <tr>
      <th style="width:22%">名称 / 简介</th>
      <th style="width:11%">节点</th>
      <th style="width:8%">域</th>
      <th style="width:8%">状态</th>
      <th style="width:9%">批准</th>
      <th style="width:14%">角色</th>
      <th style="width:13%">吞吐（收/成/败）</th>
      <th>能力</th>
    </tr>
  </thead>
  <tbody id="nodes"></tbody>
</table>

<h2>拒绝原因分布</h2>
<div id="denials"></div>

<h2>按任务类型</h2>
<div id="bytype"></div>

<h2>运行日志 <span class="meta" id="logstamp"></span></h2>
<div id="audit"></div>

<h2>系统级关联异常</h2>
<div id="anomalies"></div>

<footer>
  只读视图 —— 这里没有任何写操作。批准 / 吊销请用
  <code>nxs ops</code>（或 SSH 上服务器执行）。<br>
  数据经 coordinator 的运维端点取得，每 5 秒刷新。
  节点与延迟为内存态，重启归零；运行日志读自审计文件，跨重启保留。
</footer>

<script>
"use strict";

const OUTCOME_LABELS = {
  completed: "完成", failed: "失败", denied: "拒绝",
  register: "注册", ops: "运维"
};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  // ★ 只用 textContent。绝不使用 innerHTML 承载任何数据 ——
  //   节点自报的文本不可信，textContent 从结构上不解析标记。
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function fmtMs(v) {
  if (v === null || v === undefined) return "—";
  return Math.round(v) + "";
}

function fmtBytes(v) {
  if (v === null || v === undefined) return "—";
  if (v < 1024) return v + " B";
  if (v < 1024 * 1024) return (v / 1024).toFixed(1) + " KB";
  return (v / 1024 / 1024).toFixed(1) + " MB";
}

function fmtUptime(sec) {
  if (sec === null || sec === undefined) return "—";
  const s = Math.floor(sec);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h + "h" + String(m).padStart(2, "0");
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
  host.appendChild(tile("p50", fmtMs(lat.p50), "ms"));
  host.appendChild(tile("p95", fmtMs(lat.p95), "ms"));
  host.appendChild(tile("p99", fmtMs(lat.p99), "ms"));
  host.appendChild(tile("样本", (m && m.latency_samples) ?? 0));
  // 运行时长：判断"指标为什么是小数字"的第一手信息 ——
  // 刚重启时所有累计值都很小，没有这个会误以为网络很空闲。
  if (m && m.uptime_seconds !== undefined) {
    host.appendChild(tile("运行", fmtUptime(m.uptime_seconds)));
  }
}

function renderPending(nodes) {
  // 待审批提示。
  //
  // ⚠ 只展示，**不给批准按钮**。审批是写操作，留在 nxs ops / SSH。
  //   这里的作用是让人知道"有节点在等"以及**怎么去批**，
  //   而不是在这里批。理由见页面顶部与设计文档第五节。
  const host = document.getElementById("pending");
  host.replaceChildren();
  const waiting = (nodes || []).filter(n => !n.approved);
  if (!waiting.length) return;

  const box = el("div", "pending");
  const ids = waiting.map(n => n.node_id).join("、");
  box.appendChild(el("div", "t",
    "有 " + waiting.length + " 个节点等待批准：" + ids));
  const how = el("div", "how");
  how.appendChild(el("span", null, "未批准的节点无法收发任何任务。到服务器执行："));
  const line = el("div");
  line.style.cssText = "margin-top:6px";
  const code = el("code", null,
    "nxs ops approve <节点ID> --url <coordinator> --ca-file <ca.pem>");
  line.appendChild(code);
  how.appendChild(line);
  box.appendChild(how);
  host.appendChild(box);
}

function renderNodes(nodes) {
  const body = document.getElementById("nodes");
  body.replaceChildren();
  if (!nodes.length) {
    const tr = el("tr");
    const td = el("td", "meta", "（注册表为空）");
    td.colSpan = 8;
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const n of nodes) {
    const tr = el("tr");
    // 待审批的行整行高亮 —— 一眼扫到，不用逐行读"批准"列
    if (!n.approved) tr.className = "row-pending";

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

    // ---- 角色 ----
    //
    // ⚠ **这里修过一个"界面在说反话"的 bug（2026-10-30）。**
    //
    // 旧代码读 `n.roles`（旧模型的 issuer/executor/relay/observer 布尔对象），
    // 而 ops nodes 改造后返回的是 `n.role`（字符串）+ `n.role_definition`。
    // 结果：**角色列永远显示 "—"**，而所有测试都是绿的 ——
    // 因为没有任何测试断言过这一列的渲染。
    //
    // 是同一类问题的第三次：whoami 恒显示"无发布权"、注册成功但收不到任务、
    // 角色列恒为 "—"。共同点都是**读了一个已经不存在的字段，
    // 而 .get()/|| 让它静默降级成默认值**。
    const cRole = el("td");
    const rd = n.role_definition || {};
    const roleName = n.role || "（未声明）";
    if (n.role_definition) {
      const can = rd.can_issue ? "可发布" : "不可发布";
      cRole.appendChild(el("span", "tag", roleName));
      const note = el("div", "desc", can);
      if (rd.max_response_level) {
        note.textContent = can + " · 上限 " + rd.max_response_level;
      }
      cRole.appendChild(note);
    } else {
      // 角色查不到定义 = 这个节点发不出也收不到任何任务。
      // 这是"注册成功但静默无效"的状态，必须说出来。
      cRole.appendChild(el("span", "no", roleName + " ⚠ 无定义"));
    }
    tr.appendChild(cRole);

    // ---- 吞吐 ----
    const cTp = el("td");
    cTp.className = "caps";
    const recv = n.received ?? "—";
    const done = n.completed ?? "—";
    const fail = n.failed ?? "—";
    cTp.textContent = recv + " / " + done + " / " + fail;
    // 积压与执行中：能看出"它是不是卡住了"
    if (n.pending || n.in_flight) {
      const busy = el("div", "desc");
      busy.textContent = "待办 " + (n.pending ?? 0) +
                         " · 执行中 " + (n.in_flight ?? 0);
      if (n.pending > 0) busy.className = "desc stale";
      cTp.appendChild(busy);
    }
    tr.appendChild(cTp);

    const caps = (n.capabilities || []).join(", ") || "—";
    tr.appendChild(el("td", "caps", caps));

    body.appendChild(tr);
  }
}

function renderDenials(m) {
  const host = document.getElementById("denials");
  host.replaceChildren();
  const d = (m && m.denied_by_rule) || {};
  const keys = Object.keys(d);
  if (!keys.length) {
    host.appendChild(el("div", "none", "无拒绝记录。"));
    return;
  }
  // 说明：这个区块存在的意义是**区分"越权尝试"与"普通失败"**。
  // 只看总数"拒绝 2"看不出原因；按规则分组才知道是
  // AU-APPROVE-05（未批准）还是 MX-DOMAIN-02（域不通）。
  const table = el("table", "log");
  const thead = el("thead");
  const htr = el("tr");
  htr.appendChild(el("th", null, "规则"));
  htr.appendChild(el("th", null, "次数"));
  thead.appendChild(htr);
  table.appendChild(thead);
  const tb = el("tbody");
  for (const k of keys.sort((a, b) => d[b] - d[a])) {
    const tr = el("tr");
    const c1 = el("td");
    c1.appendChild(el("span", "rule o-denied", k));
    tr.appendChild(c1);
    tr.appendChild(el("td", "num", d[k]));
    tb.appendChild(tr);
  }
  table.appendChild(tb);
  host.appendChild(table);
}

function renderByType(m) {
  const host = document.getElementById("bytype");
  host.replaceChildren();
  const bt = (m && m.by_type) || {};
  const keys = Object.keys(bt);
  if (!keys.length) {
    host.appendChild(el("div", "none", "暂无任务。"));
    return;
  }
  const table = el("table", "log");
  const thead = el("thead");
  const htr = el("tr");
  for (const h of ["任务类型", "提交", "完成", "失败", "失败率"]) {
    htr.appendChild(el("th", null, h));
  }
  thead.appendChild(htr);
  table.appendChild(thead);
  const tb = el("tbody");
  for (const k of keys.sort()) {
    const r = bt[k] || {};
    const sub = r.submitted ?? 0;
    const fail = r.failed ?? 0;
    const rate = sub > 0 ? Math.round(fail / sub * 100) : 0;
    const tr = el("tr");
    const c1 = el("td");
    c1.appendChild(el("span", "rule", k));
    tr.appendChild(c1);
    tr.appendChild(el("td", "num", sub));
    tr.appendChild(el("td", "num", r.completed ?? 0));
    const cFail = el("td", "num");
    if (fail > 0) cFail.className = "num o-failed";
    cFail.textContent = String(fail);
    tr.appendChild(cFail);
    const cRate = el("td", "num");
    if (rate > 0) cRate.className = "num o-failed";
    cRate.textContent = rate + "%";
    tr.appendChild(cRate);
    tb.appendChild(tr);
  }
  table.appendChild(tb);
  host.appendChild(table);
}

function outcomeKey(o) {
  // "register:unchanged" → "register"；用于选样式
  return String(o || "").split(":")[0];
}

function renderAudit(records, truncated) {
  const host = document.getElementById("audit");
  const stamp = document.getElementById("logstamp");
  host.replaceChildren();
  stamp.textContent = records.length ? "（最近 " + records.length + " 条）" : "";

  if (!records.length) {
    host.appendChild(el("div", "log-empty", "暂无审计记录。"));
    return;
  }

  const table = el("table", "log");
  const thead = el("thead");
  const htr = el("tr");
  for (const h of ["时间", "来源 → 目标", "类型", "状态", "耗时 ms", "字节", "规则"]) {
    htr.appendChild(el("th", null, h));
  }
  thead.appendChild(htr);
  table.appendChild(thead);

  const tb = el("tbody");
  // 最新在最上面 —— 排障时人关心"刚才"，不想滚到底
  for (const r of records.slice().reverse()) {
    const tr = el("tr");

    const ts = String(r.ts || "");
    const cTs = el("td", "t");
    // 只留 HH:MM:SS，日期放 title 里（同一天的日志不需要重复日期）
    cTs.textContent = ts.length >= 19 ? ts.slice(11, 19) : ts;
    cTs.title = ts;
    tr.appendChild(cTs);

    const cFlow = el("td", "flow");
    const from = r.from_node || "—";
    const to = r.to_node || "—";
    cFlow.textContent = from + " → " + to;
    tr.appendChild(cFlow);

    tr.appendChild(el("td", "rule", r.task_type || "—"));

    // 状态：按 outcome 着色。这是整张表最需要扫的列。
    const out = String(r.outcome || "—");
    const ok = outcomeKey(out);
    const cOut = el("td");
    const span = el("span", "o-" + ok, OUTCOME_LABELS[ok] || out);
    span.title = out;
    cOut.appendChild(span);
    tr.appendChild(cOut);

    tr.appendChild(el("td", "num", fmtMs(r.duration_ms)));
    // 字节数：0 表示"这次事件没有产出内容"（拒绝、注册、运维都如此），
    // 显示 "0 B" 会让人以为"产出了空内容"。用 "—" 区分这两件事。
    const bytes = (r.bytes === undefined || r.bytes === null) ? null : r.bytes;
    tr.appendChild(el("td", "num",
      (bytes === null || bytes === 0) ? "—" : fmtBytes(bytes)));

    // 规则码：失败/拒绝的关键线索。成功时留空，避免一片噪音。
    const rid = r.rule_id ? String(r.rule_id) : "";
    const cRule = el("td");
    if (rid) {
      cRule.appendChild(el("span", "rule o-denied", rid));
    } else {
      cRule.appendChild(el("span", "rule", ""));
    }
    tr.appendChild(cRule);

    tb.appendChild(tr);
  }
  table.appendChild(tb);
  host.appendChild(table);

  if (truncated) {
    host.appendChild(el("div", "trunc",
      "⚠ 服务端只返回了上限条数，更早的记录未显示。"));
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

function showUnauthorized() {
  // ★ 401 不等于"坏了"。它最常见的原因是**服务重启过**：
  //   token 每次启动重新生成（不落盘、不跨重启），旧链接随之失效。
  //   只写"未授权"会让人以为系统出故障，所以这里说清三件事：
  //   为什么、不是故障、怎么办。
  const host = document.getElementById("error");
  host.replaceChildren();
  const box = el("div", "err");
  box.appendChild(el("div", null, "登录凭据已失效 —— 这不代表系统出了问题"));
  // 统一用 el() 建节点（页面里只有这一个 DOM 构造入口，
  // 便于一眼审查"有没有哪里用了会解析标记的 API"）
  const ul = el("ul");
  ul.style.cssText = "margin:10px 0 0; padding-left:20px; line-height:1.9";
  for (const line of [
    "观察窗的 token 每次启动重新生成（不落盘、不跨重启），所以服务一重启，旧链接就失效。",
    "重新打开时用访问服务器的那个脚本重新取链接（例如 nxs-view.sh），它会自动带上当前 token。",
    "如果你只是刷新了这个标签页，那个 token 已经过期了 —— 换新链接即可。",
  ]) {
    ul.appendChild(el("li", null, line));
  }
  box.appendChild(ul);
  host.appendChild(box);

  document.getElementById("stamp").textContent = "凭据已失效";
  // 别继续轮询：每 5 秒再撞一次 401 只会刷日志，也不会自己变好
  if (window.__nxsTimer) clearInterval(window.__nxsTimer);
}

async function refresh() {
  try {
    const res = await fetch("api/state", { headers: { "Accept": "application/json" } });
    if (res.status === 401) {
      showUnauthorized();
      return;
    }
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    showError(data.error ? ("coordinator 不可达：" + data.error) : "");
    const nodes = data.nodes || [];
    renderTiles(data.metrics);
    renderPending(nodes);
    renderNodes(nodes);
    renderDenials(data.metrics);
    renderByType(data.metrics);
    renderAudit(data.audit || [], data.audit_truncated);
    renderAnomalies(data.metrics ? data.metrics.anomalies : []);
    document.getElementById("stamp").textContent =
      "最后更新 " + new Date().toLocaleTimeString("zh-CN");
  } catch (e) {
    showError("取数据失败：" + e.message);
  }
}

refresh();
window.__nxsTimer = setInterval(refresh, 5000);
</script>
</body>
</html>
"""

#: 凭据无效时返回的页面。
#:
#: ⚠ **这个页面是补的一个真实缺口。** 最初的实现里，token 无效时返回的是
#: 纯文本 ``unauthorized`` —— 而用户的真实场景是"刷新一个带旧 token 的标签"，
#: 那一步在**换 cookie 阶段**就被拒，根本加载不到主页面。
#: 于是用户在浏览器里看到的就是裸的 "unauthorized" 一个词，
#: 自然会以为系统坏了。
#:
#: 主页面里的那些说明（"token 每次启动重新生成…"）永远显示不出来，
#: 因为那条路径走不到。
#:
#: 现在：**任何 401 都返回这一个页面**，不管是"没带 token"还是
#: "token 过期/错误"。两种情况返回完全相同的内容，所以不泄露任何信息
#: —— 与网关拒绝时只回 rule_id 是同一条思路。
UNAUTHORIZED_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nexus Mesh · 凭据已失效</title>
<style>
  :root {
    --bg: #fbfbfc; --fg: #1c1d21; --dim: #6b7280;
    --card: #ffffff; --line: #e5e7eb; --warn: #b45309;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14161a; --fg: #e6e7ea; --dim: #9aa1ab;
      --card: #1b1e23; --line: #2a2e35; --warn: #fbbf24;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 56px 32px; background: var(--bg); color: var(--fg);
    font: 14px/1.75 -apple-system, BlinkMacSystemFont, "Segoe UI",
          "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  }
  .wrap { max-width: 620px; margin: 0 auto; }
  h1 { font-size: 17px; margin: 0 0 6px; font-weight: 600; }
  .sub { color: var(--warn); font-size: 13px; margin-bottom: 22px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; padding: 20px 24px;
  }
  ol { margin: 0; padding-left: 22px; }
  li { margin-bottom: 12px; }
  li:last-child { margin-bottom: 0; }
  code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12.5px; background: var(--bg);
    border: 1px solid var(--line); border-radius: 4px; padding: 1px 5px;
  }
  .note { color: var(--dim); font-size: 12.5px; margin-top: 18px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>凭据已失效</h1>
  <div class="sub">这不代表系统出了问题</div>

  <div class="card">
    <ol>
      <li>
        观察窗的 <code>token</code> <strong>每次启动重新生成</strong>
        （不落盘、不跨重启），所以服务一重启，旧链接就失效。
      </li>
      <li>
        重新打开时，用访问服务器的那个脚本重新取一次链接
        （例如 <code>nxs-view.sh</code>），它会自动带上当前 token
        并验证链路。
      </li>
      <li>
        如果你只是刷新了旧标签页，那个 token 已经过期 ——
        换用新链接即可，不需要重启任何服务。
      </li>
    </ol>
  </div>

  <div class="note">
    需要<b>手动</b>取链接时，在服务器上执行：<br>
    <code>journalctl -u nexus-mesh-ui -n 40 | grep -oE 'http://127\.0\.0\.1:8080/\?t=\S+' | tail -1</code>
  </div>
</div>
</body>
</html>
"""
