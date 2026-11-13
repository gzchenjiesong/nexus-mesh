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
  /* ⚠ 尊重系统的「减少动态效果」设置。
     本页面目前几乎没有动画，这条是为将来加过渡时兜底 —— 与其那时
     忘了写，不如现在就把开关放在这里。 */
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: 0.01ms !important;
      animation-iteration-count: 1 !important;
      transition-duration: 0.01ms !important;
      scroll-behavior: auto !important;
    }
  }

  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14161a; --fg: #e6e7ea; --dim: #9aa1ab; --line: #2a2e35;
      --card: #1b1e23; --ok: #4ade80; --warn: #fbbf24; --bad: #f87171;
      --accent: #7aa2f7;
    }
  }
  * { box-sizing: border-box; }

  /* ---- 浏览器原生表面的主题化 ----
   *
   * ⚠ 这一块是「被做过」与「拼装的」最便宜的区分信号。
   *
   * 它们是浏览器**默认**提供的表面（选中高亮、光标、滚动条、焦点框），
   * 不主题化时会在暗色模式下露出刺眼的系统默认色（比如深蓝选中块、
   * 白色滚动条）。用户说不清哪里不对，但会觉得"不像是给这个站点做的"。
   */
  ::selection { background: color-mix(in srgb, var(--accent) 28%, transparent);
                color: var(--fg); }
  html { caret-color: var(--accent); scrollbar-color: var(--line) transparent;
         scrollbar-width: thin; }
  /* WebKit 系需要单独写伪元素 */
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--line); border-radius: 5px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--dim); }
  /* 键盘焦点必须可见 —— 用 :focus-visible 而不是 :focus，
     否则鼠标点击也会出现焦点框，视觉噪音 */
  :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px;
                   border-radius: 3px; }
  a { text-underline-offset: 2px; }

  /* 表格与卡片的数字用等宽数位 —— 数列才能对齐扫读 */
  .num, .tile .v, table td, .log .num { font-variant-numeric: tabular-nums; }

  body {
    margin: 0; padding: 28px 32px 60px;
    background: var(--bg); color: var(--fg);
    font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI",
          "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  }
  header { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; }
  h1 { font-size: 18px; margin: 0; font-weight: 650; letter-spacing: .2px; }
  .meta { color: var(--dim); font-size: 13.5px; }
  .meta b { color: var(--fg); font-weight: 600; }
  .spacer { flex: 1; }

  .bar { display: flex; gap: 10px; flex-wrap: wrap; margin: 20px 0 24px; }
  .tile {
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 12px 16px; min-width: 104px;
  }
  .tile .k { color: var(--dim); font-size: 12px; letter-spacing: .4px;
             text-transform: uppercase; }
  .tile .v { font-size: 21px; font-weight: 640; font-variant-numeric: tabular-nums; }
  .tile .u { font-size: 12px; color: var(--dim); font-weight: 400; }

  h2 { font-size: 13.5px; letter-spacing: .5px; text-transform: uppercase;
       color: var(--dim); margin: 28px 0 10px; font-weight: 600; }

  table { width: 100%; border-collapse: collapse; background: var(--card);
          border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
  th, td { text-align: left; padding: 11px 14px; border-bottom: 1px solid var(--line);
           vertical-align: top; }
  th { font-size: 12px; letter-spacing: .4px; text-transform: uppercase;
       color: var(--dim); font-weight: 600; background: transparent; }
  tr:last-child td { border-bottom: none; }

  .name { font-weight: 600; }
  .desc { color: var(--dim); font-size: 13.5px; margin-top: 2px;
          max-width: 42ch; word-break: break-word; }
  .id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 13.5px; color: var(--dim); }
  .caps { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
          font-size: 12px; color: var(--dim); }
  .tags { display: flex; gap: 4px; flex-wrap: wrap; }
  .tag { font-size: 12px; padding: 1px 7px; border-radius: 999px;
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
  .anomaly .r { font-family: ui-monospace, Menlo, monospace; font-size: 13.5px;
                color: var(--bad); font-weight: 600; }
  .anomaly .why { color: var(--dim); font-size: 13.5px; margin-top: 3px; }
  .none { color: var(--ok); }
  .err { border: 1px solid var(--bad); color: var(--bad); background: var(--card);
         border-radius: 8px; padding: 13px 16px; margin: 18px 0; }
  footer { margin-top: 34px; color: var(--dim); font-size: 12px; }

  .pending { border-left: 3px solid var(--warn); background: var(--card);
             border-radius: 0 8px 8px 0; padding: 13px 16px; margin: 18px 0; }
  .pending .t { font-weight: 600; color: var(--warn); }
  .pending .how { color: var(--dim); font-size: 13.5px; margin-top: 6px; }
  .pending code { background: var(--bg); border: 1px solid var(--line);
                  border-radius: 4px; padding: 1px 5px; font-size: 12px; }
  .row-pending { background: color-mix(in srgb, var(--warn) 9%, transparent); }

  .log { width: 100%; border-collapse: collapse; background: var(--card);
         border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
         font-size: 13.5px; }
  .log th, .log td { padding: 7px 11px; border-bottom: 1px solid var(--line);
                     white-space: nowrap; }
  .log th { font-size: 12px; letter-spacing: .4px; color: var(--dim);
            font-weight: 600;
            text-transform: none; }
  .log tr:last-child td { border-bottom: none; }
  .log .t { font-family: ui-monospace, Menlo, monospace; color: var(--dim); }
  .log .flow { font-family: ui-monospace, Menlo, monospace; }
  .log .num { font-variant-numeric: tabular-nums; text-align: right;
              color: var(--dim); }
  .log .rule { font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
  .o-completed { color: var(--ok); font-weight: 600; }
  .o-failed    { color: var(--bad); font-weight: 600; }
  .o-denied    { color: var(--warn); font-weight: 600; }
  .o-register  { color: var(--dim); }
  .o-ops       { color: var(--accent); }
  .log-empty { color: var(--dim); padding: 16px 14px; }
  .trunc { color: var(--warn); font-size: 12px; margin-top: 6px; }

  /* ---- 阶段 1：探针与陈旧态 ----
   *
   * 三态用**视觉层级**区分，不只是文案：
   *   实时   → 正常
   *   陈旧   → 整块降透明度 + 虚线边框 + 顶部横幅
   *   无历史 → 只显示探针
   *
   * ⚠ 陈旧态刻意用 opacity 而非变灰：变灰会被误认为 disabled，
   *   而降透明度保留可读性，又一眼看得出"这不是新的"。
   */
  .probe { border: 1px solid var(--line); border-left: 0; border-radius: 10px;
           background: var(--card); padding: 13px 16px; margin: 18px 0; }
  .probe .head { display: flex; align-items: baseline; gap: 10px;
                 flex-wrap: wrap; margin-bottom: 8px; }
  .probe .verdict { font-weight: 600; }
  .probe .verdict.healthy      { color: var(--ok); }
  .probe .verdict.dead         { color: var(--bad); }
  .probe .verdict.unresponsive { color: var(--bad); }
  .probe .verdict.unknown      { color: var(--dim); }
  .probe .note { color: var(--fg); font-size: 13.5px; line-height: 1.6; }
  .probe .facts { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 9px;
                  font-family: ui-monospace, Menlo, monospace; font-size: 12px;
                  color: var(--dim); }
  .probe .facts b { color: var(--fg); font-weight: 600; }
  .probe .warn { color: var(--warn); font-size: 13.5px; margin-top: 8px;
                 padding-left: 14px; }
  .probe .warn li { margin-top: 3px; }
  .probe .unavail { color: var(--dim); font-size: 12px; margin-top: 8px; }

  .banner { border-radius: 10px; padding: 13px 16px; margin: 18px 0;
            border: 1px solid var(--bad); background: var(--card); }
  .banner.stale { border-color: var(--warn); }
  .banner .t { font-weight: 600; }
  .banner.stale .t { color: var(--warn); }
  .banner .why { color: var(--dim); font-size: 13.5px; margin-top: 5px;
                 line-height: 1.6; }
  .banner .age { font-weight: 600; color: var(--warn); }

  /* 首次加载提示。刻意用低调的 dim 色、不加边框：
     它是「稍等」，不是「出事了」—— 后者由 #error / #stale 负责。 */
  #boot { color: var(--dim); font-size: 13.5px; padding: 12px 0; }

  /* 陈旧数据整块降透明度 —— 与实时数据视觉隔离，绝不混排观感 */
  .stale-data { opacity: .62; }
  .stale-data table { border-style: dashed; }
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
<!-- 首次加载提示：交接文档 §8 要求「加载中有骨架或明确提示」。
     空白的数据区与「真的没有数据」长得一模一样，等几秒会让人以为坏了。
     renderAll() 第一件事就是把它清掉。 -->
<div id="boot">正在读取 coordinator 的运维端点……</div>
<div id="stale"></div>
<div id="probe"></div>
<div id="pending"></div>

<div id="live-region">
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
      <th style="width:13%">角色</th>
      <th style="width:12%">吞吐（收/成/败）</th>
      <th style="width:9%">水位</th>
      <th style="width:9%">回传</th>
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
</div><!-- /live-region -->

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
  // ---- 延迟分位数 ----
  //
  // ⚠ `latency_samples < 2` 时**不显示分位数**（交接文档 §6 明确要求）。
  //   原因：1 个样本的 p50/p95/p99 是同一个数，"分位数"没有意义，
  //   画出来会让人以为延迟很稳定 —— 而实际是样本不够。
  //   显示 "—" + 明确写「样本不足」，比给一个假数字诚实。
  const lat = (m && m.latency_ms) || {};
  const samples = (m && m.latency_samples) ?? 0;
  const enough = samples >= 2;
  host.appendChild(tile("p50", enough ? fmtMs(lat.p50) : "—", enough ? "ms" : null));
  host.appendChild(tile("p95", enough ? fmtMs(lat.p95) : "—", enough ? "ms" : null));
  host.appendChild(tile("p99", enough ? fmtMs(lat.p99) : "—", enough ? "ms" : null));
  const sampleTile = tile("样本", samples);
  if (!enough) {
    sampleTile.appendChild(el("div", "desc", "样本不足，不算分位数"));
  }
  host.appendChild(sampleTile);
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

function renderNodes(nodes, emptyReason) {
  const body = document.getElementById("nodes");
  body.replaceChildren();
  if (!nodes.length) {
    const tr = el("tr");
    // ⚠ **空列表有两种含义，不能混成一句。**
    //
    //   注册表真的没有成员   → 「（注册表为空）」
    //   从来没取到过数据     → 「没读到」
    //
    // 混成前者会让人以为网络里一个节点都没有，从而去查注册表 ——
    // 而实际是面板连不上。这是本项目第 N 次同类问题：空值被静默
    // 降级成一个看起来正常的默认值。
    const td = el("td", "meta", emptyReason || "（注册表为空）");
    td.colSpan = 10;
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
    //
    // ⚠ **数据契约：从这一行往下的字段（received/completed/failed/
    //   pending/in_flight/backlog_ratio/bytes_out）不在 `/nxs/ops/nodes` 里。**
    //   它们在 `metrics.nodes[]` 里，由服务端 `_merge_node_metrics()`
    //   按 node_id 合并进来（见 nxs/ui/server.py）。
    //
    //   踩过的坑（2026-11-13）：节点表直接读 `data.nodes` 就渲染，
    //   结果是**整张表 7 个格子全是「—」，页面看起来完全正常**。
    //   而守护测试写的是 `"backlog_ratio" in SHELL` —— 字段名在代码里
    //   出现过就绿，根本不管它读的是哪个数据源。
    //   所以别在这里找数据源，也别改回 `data.nodes` 单独一份。
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

    // ---- 水位（backlog_ratio）----
    //
    // ⚠ `backlog_ratio` 为 `None` 时表示**无穷大**（capacity 为 0，除零）。
    //   不能显示成 0 或空白 —— 那会被读成"没有积压"，与实际正好相反。
    //   交接文档 §6 明确要求显示为 ∞。
    const cLevel = el("td", "num");
    const ratio = n.backlog_ratio;
    if (ratio === null || ratio === undefined) {
      cLevel.appendChild(el("span", "o-denied", "∞"));
      cLevel.title = "容量为 0，积压比无穷大";
    } else {
      cLevel.textContent = (ratio).toFixed(1) + "×";
      // 积压超过容量本身就是异常信号（>1×）
      if (ratio > 1) cLevel.className = "num o-denied";
    }
    tr.appendChild(cLevel);

    // ---- 回传字节（bytes_out）----
    const cBytes = el("td", "num");
    cBytes.textContent = (n.bytes_out === undefined || n.bytes_out === null)
      ? "—" : fmtBytes(n.bytes_out);
    tr.appendChild(cBytes);

    const caps = (n.capabilities || []).join(", ") || "—";
    tr.appendChild(el("td", "caps", caps));

    body.appendChild(tr);
  }
}

function renderDenials(m) {
  const host = document.getElementById("denials");
  host.replaceChildren();
  // ⚠ metrics 为 null = 指标这次没读到，**不是"没有拒绝"**。
  //   说「无拒绝记录」会让人以为一切合规，与事实相反。
  if (!m) {
    host.appendChild(el("div", "none",
      "指标不可用 —— 无法判断是否有拒绝。见上方错误信息。"));
    return;
  }
  const d = m.denied_by_rule || {};
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
  // 同 renderDenials：metrics 缺失不等于「没有任务」
  if (!m) {
    host.appendChild(el("div", "none",
      "指标不可用 —— 无法判断任务分布。见上方错误信息。"));
    return;
  }
  const bt = m.by_type || {};
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

function renderAudit(records, truncated, heartbeats, error) {
  const host = document.getElementById("audit");
  const stamp = document.getElementById("logstamp");
  host.replaceChildren();

  // 标题右边注明条数与"滤掉了多少心跳"。
  //
  // ⚠ 必须说出来。节点每 30 秒心跳一次，若日志里一条心跳都没有
  //   却没解释，人会以为"心跳停了"或"日志坏了"——
  //   而事实恰恰相反：**滤掉得多 = 这段时间没发生任务 = 一切正常**。
  const parts = [];
  if (records.length) parts.push("最近 " + records.length + " 条");
  if (heartbeats > 0) parts.push("已滤 " + heartbeats + " 条心跳");
  stamp.textContent = parts.length ? "（" + parts.join(" · ") + "）" : "";

  if (!records.length) {
    // 空列表有**三种**含义，别混成一句：
    //
    //   读取失败         → 说「暂无」就是在撒谎（最重要的一种）
    //   只有心跳         → 说明「这段时间没发生任务」，那是正常的
    //   真的没有记录     → 「暂无审计记录」
    host.appendChild(el("div", "log-empty",
      error
        ? ("运行日志读取失败（" + error + "）—— 不代表没有记录。")
        : heartbeats > 0
          ? "这段时间只有心跳（" + heartbeats + " 条），没有任务事件。"
          : "暂无审计记录。"));
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

    // 标题行：severity · rule · subject
    const head = el("div", "r");
    head.textContent = (a.severity || "") + " · " + (a.rule || "");
    if (a.subject) head.textContent += " · " + a.subject;
    box.appendChild(head);

    // message：发生了什么
    if (a.message) box.appendChild(el("div", "why", a.message));

    // ⚠ rationale 是这类规则存在的**资格证明**，必须显示。
    //   交接文档 §4 原文：「丢掉 rationale 就等于丢掉了这条规则的可信度」。
    //   它回答的是「凭什么单看每个指标都正常，这却算异常」。
    if (a.rationale) {
      const r = el("div", "why");
      r.appendChild(el("span", "tag", "为什么算异常"));
      r.appendChild(document.createTextNode(" " + a.rationale));
      box.appendChild(r);
    }

    // ⚠ suggestion 同样必须显示：只说"有问题"不给动作，等于把判断
    //   全推给运维。交接文档 §8 交付标准明确列了这一项。
    if (a.suggestion) {
      const s = el("div", "why");
      s.appendChild(el("span", "tag", "建议动作"));
      s.appendChild(document.createTextNode(" " + a.suggestion));
      s.style.marginTop = "6px";
      box.appendChild(s);
    }

    host.appendChild(box);
  }
}

// ---- 阶段 1：本地探针 ----
//
// 探针回答「coordinator 到底怎么了」—— 在 ops 端点拿不到数据时，
// 它是唯一的线索。2026-10-31 那次冻结 13 天，面板只能说"连不上"，
// 和 curl 没区别；有了探针才能说"进程活着、阻塞在锁上、6 个连接没人 accept"。
const VERDICT_LABELS = {
  healthy: "正常",
  dead: "进程已停止",
  unresponsive: "活着但不响应",
  unknown: "无法判断"
};

function renderProbe(p) {
  const host = document.getElementById("probe");
  host.replaceChildren();
  if (!p) return;

  const box = el("div", "probe");
  const head = el("div", "head");

  const verdict = String(p.verdict || "unknown");
  head.appendChild(el("span", "verdict " + verdict,
    VERDICT_LABELS[verdict] || verdict));
  head.appendChild(el("span", "meta", "本地探针 · " + (p.unit || "")));
  box.appendChild(head);

  if (p.note) box.appendChild(el("div", "note", p.note));

  // 事实行：只显示拿到的项，拿不到的留给 unavailable
  const facts = el("div", "facts");
  const add = (label, value) => {
    if (value === null || value === undefined || value === "") return;
    const item = el("span");
    item.appendChild(el("b", null, value));
    item.appendChild(document.createTextNode(" " + label));
    facts.appendChild(item);
  };
  add("PID", p.pid);
  add("线程", p.threads);
  add("MB", p.rss_mb);
  if (p.port_listening === true) {
    add("个连接待 accept", p.accept_queue ?? 0);
  } else if (p.port_listening === false) {
    add("端口未监听", p.port ?? "");
  }
  // wchan 单独处理：它后面要跟一句人话解释，而 add() 是"值 + 单位"的格式，
  // 硬套会出现"（解释） wchan=xxx"这种顺序颠倒的读法，还会把括号套两层。
  if (p.wchan) {
    const item = el("span");
    item.appendChild(document.createTextNode("wchan "));
    item.appendChild(el("b", null, p.wchan));
    if (p.wchan_hint) {
      item.appendChild(document.createTextNode(" · " + p.wchan_hint));
    }
    facts.appendChild(item);
  }
  if (facts.childNodes.length) box.appendChild(facts);

  // 告警：与 verdict 分开 —— verdict 答"能不能用"，这里答"稳不稳"
  if (p.warnings && p.warnings.length) {
    const ul = el("ul", "warn");
    for (const w of p.warnings) ul.appendChild(el("li", null, w));
    box.appendChild(ul);
  }

  // 探针自己的缺口要明说 —— 否则"unknown"看起来像服务出了问题
  if (p.unavailable && p.unavailable.length) {
    box.appendChild(el("div", "unavail",
      "本机取不到的项：" + p.unavailable.join("、")));
  }

  host.appendChild(box);
}

// ---- 阶段 1：陈旧态横幅 ----
//
// ⚠ 这个横幅是「陈旧数据永不与实时数据混排」这条约束的落实处。
//   没有它，页面上有节点列表但数据是十分钟前的 —— 那比空白更危险。
function renderStale(data) {
  const host = document.getElementById("stale");
  const region = document.getElementById("live-region");
  host.replaceChildren();

  if (data.no_history) {
    // 面板刚启动就遇上不可达：没有历史，只有探针
    const box = el("div", "banner");
    box.appendChild(el("div", "t", "coordinator 不可达，且没有历史数据"));
    box.appendChild(el("div", "why",
      "面板刚启动，还没成功取到过数据。下面的探针结果说明它当前的状态；"
      + "数据区暂时为空。"));
    host.appendChild(box);
    if (region) region.classList.add("stale-data");
    return;
  }

  if (!data.stale) {
    if (region) region.classList.remove("stale-data");
    return;
  }

  const age = data.stale_age_seconds;
  const ageText = (age === null || age === undefined)
    ? "时间未知"
    : age < 60 ? Math.round(age) + " 秒前"
    : age < 3600 ? Math.round(age / 60) + " 分钟前"
    : (age / 3600).toFixed(1) + " 小时前";

  const box = el("div", "banner stale");
  const title = el("div", "t");
  title.appendChild(document.createTextNode("coordinator 不可达 —— 以下是"));
  title.appendChild(el("span", "age", ageText));
  // ⚠ 这里不能用 markdown 加粗 —— el() 走 textContent，
  //   星号会原样显示出来。要强调就用 CSS 类。
  title.appendChild(document.createTextNode("的最后已知状态"));
  box.appendChild(title);

  box.appendChild(el("div", "why",
    "数据不是实时的。上面是上次成功取到的快照，"
    + "用于回答「它挂之前是什么样」。要判断它现在怎么了，看下面的本地探针。"));

  host.appendChild(box);
  // 整块降透明度 + 虚线边框：一眼看出"这不是新的"
  if (region) region.classList.add("stale-data");
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

//: 把一份 `/api/state` 画到页面上。**这是唯一的渲染入口。**
//
// ⚠ 抽成独立函数是有原因的（2026-11-13）：
//
//   `deploy/preview-ui-state.py` 要把某一种状态画出来给人看。它原来
//   自己写着一段调用序列（renderStale/renderProbe/renderNodes/…），
//   于是「预览的调用序列」与「真实 refresh 的调用序列」是**两份代码**。
//   加了新参数（比如 renderNodes 的空态原因）时，预览那份不会跟着变
//   —— 预览仍然是绿的，而它跑的是过时的接线。
//
//   预览脚本本身已经踩过两次同类坑（手写假数据 → 验证的是假数据）。
//   现在两边共用这一个函数，预览跑的就是生产接线。
function renderAll(data) {
  const nodes = data.nodes || [];

  // 首次加载提示到此为止 —— 后面无论如何都会画出「数据是什么状态」，
  // 那一行再留着就是重复噪音。
  document.getElementById("boot").replaceChildren();

  // ⚠ 顺序有意义：先渲染「数据是什么状态」（陈旧/无历史），
  //   再渲数据本身。否则用户会先看到一屏节点，然后才被告知那是旧的。
  renderStale(data);

  // 探针在失败时是唯一线索，放显眼位置（紧接横幅）
  renderProbe(data.probe);

  // error 只在**没有陈旧兜底**时才显示成错误条 ——
  // 有兜底时，横幅已经说清「不可达」了，再叠一条错误是重复噪音
  if (!data.stale && !data.no_history) {
    showError(data.error ? ("coordinator 不可达：" + data.error) : "");
  } else {
    showError("");
  }

  renderTiles(data.metrics);
  renderPending(nodes);
  // ⚠ 空列表的原因必须传下去：`no_history` 是「没读到」，不是「注册表为空」。
  //   不传的话，面板连不上时会显示「（注册表为空）」—— 那是在说反话。
  renderNodes(nodes, data.no_history
    ? "没有数据 —— coordinator 不可达，面板从未成功读到过注册表"
      + "（这不代表注册表为空）"
    : "");
  renderDenials(data.metrics);
  renderByType(data.metrics);
  renderAudit(data.audit || [], data.audit_truncated, data.audit_heartbeats,
              data.audit_error);
  renderAnomalies(data.metrics ? data.metrics.anomalies : []);

  // 时间戳要区分三种情况 —— 只写「最后更新」会让人以为数据是新的
  const stamp = document.getElementById("stamp");
  if (data.no_history) {
    stamp.textContent = "无历史数据";
  } else if (data.stale) {
    stamp.textContent = "数据非实时（" + (data.stale_since || "时间未知") + "）";
  } else {
    stamp.textContent = "最后更新 " + new Date().toLocaleTimeString("zh-CN");
  }
}

async function refresh() {
  try {
    const res = await fetch("api/state", { headers: { "Accept": "application/json" } });
    if (res.status === 401) {
      showUnauthorized();
      return;
    }
    if (!res.ok) throw new Error("HTTP " + res.status);
    renderAll(await res.json());
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
  h1 { font-size: 18px; margin: 0 0 6px; font-weight: 600; }
  .sub { color: var(--warn); font-size: 13.5px; margin-bottom: 22px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; padding: 20px 24px;
  }
  ol { margin: 0; padding-left: 22px; }
  li { margin-bottom: 12px; }
  li:last-child { margin-bottom: 0; }
  code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 13.5px; background: var(--bg);
    border: 1px solid var(--line); border-radius: 4px; padding: 1px 5px;
  }
  .note { color: var(--dim); font-size: 13.5px; margin-top: 18px; }
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
