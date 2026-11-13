"""★ 在 node 里**真跑**前端渲染 —— 空态与文案的行为测试。

## 为什么要有这个文件

本项目反复栽在同一件事上：**断言落在「某个词存在于代码文本」上，
守不住「行为对不对」**。三次同类故障：

    角色列永远显示「—」        没人断言过那一列的渲染
    水位/回传列全空            断言是 `"backlog_ratio" in SHELL`
    健康进程报「疑似死锁」      断言是把误报本身写成了期望

这些断言在故障时**全是绿的**。

## 做法

页面的 JS 就在 ``SHELL`` 里，是可以直接执行的普通脚本。这个文件：

1. 用一份极简 DOM 桩（约 40 行）顶掉 ``document``
2. 把 ``SHELL`` 里的 ``<script>`` 原样取出执行
3. 调用页面自己的 ``renderAll(data)``
4. 读回渲染出的**文本**，断言人看到的是什么

数据来自**真实的** ``UiServer.snapshot()``（用 MagicMock 顶掉 ops 客户端），
所以「服务端给什么 → 页面说什么」这条链是完整跑通的。

不是 Playwright、不需要浏览器，只在有 ``node`` 时运行。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

from conftest import covers
from nxs.ui.page import SHELL
from nxs.ui.server import UiServer

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="没有 node —— 前端行为测试需要 node 才能跑",
)

#: 运维密钥（≥ MIN_SECRET_LENGTH）
SECRET = b"ui-test-secret-0123456789abcdef"

#: 极简 DOM 桩 + 执行页面脚本 + 调用 renderAll + 回吐文本。
#:
#: ⚠ 桩只实现页面实际用到的那几个成员。缺一个会抛错而不是静默通过 ——
#: 这是有意的：桩不全时应该红，而不是假装渲染成功。
_HARNESS = r"""
class FakeEl {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this._classes = new Set();
    this._text = "";
    this.style = { cssText: "" };
    this.title = "";
    this.colSpan = 1;
    this._attrs = {};
  }
  // className 与 classList 必须是**同一个集合**的两个视图 ——
  // 页面里两种写法都有（el() 用 className，renderStale 用 classList）
  set className(v) {
    this._classes = new Set(String(v || "").split(/\s+/).filter(Boolean));
  }
  get className() { return [...this._classes].join(" "); }
  get classList() {
    const s = this._classes;
    return {
      add: (...c) => { for (const x of c) s.add(x); },
      remove: (...c) => { for (const x of c) s.delete(x); },
      contains: (c) => s.has(c),
      toggle: (c) => (s.has(c) ? (s.delete(c), false) : (s.add(c), true)),
    };
  }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  prepend(c) { c.parent = this; this.children.unshift(c); return c; }
  replaceChildren() { this.children = []; this._text = ""; }
  set textContent(v) {
    this._text = (v === undefined || v === null) ? "" : String(v);
    this.children = [];
  }
  get textContent() {
    return this._text + this.children.map(c => c.textContent).join("");
  }
  get innerText() { return this.textContent; }
  get childNodes() { return this.children; }
  get firstChild() { return this.children[0] || null; }

  // ⚠ closest 是 renderNodes 里 ``body.closest("table")`` 要用的
  //   （labelCells 需要拿到 table 元素读表头）。桩里不实现它，
  //   渲染会直接抛错 —— 这正是桩该有的行为：缺 API 就红，而不是静默跳过。
  closest(sel) {
    let n = this.parent;
    while (n) {
      if (sel === "table" && n.tagName === "TABLE") return n;
      if (sel === "tbody" && n.tagName === "TBODY") return n;
      n = n.parent;
    }
    return null;
  }

  // querySelector / querySelectorAll 的极简版。
  //
  // ⚠ **必须支持后代选择器**（"thead th"）。第一版只按单个标签名匹配，
  //   于是 ``table.querySelectorAll("thead th")`` 返回空 —— 表头读不到、
  //   labelCells 填不出任何 data-label，而测试全绿。这正是"桩比真实
  //   环境宽松"造成的假绿。
  _walk(out) {
    for (const c of this.children) { out.push(c); c._walk(out); }
    return out;
  }
  querySelectorAll(sel) {
    const parts = String(sel).trim().toUpperCase().split(/\s+/);
    let pool = [this];
    for (const tag of parts) {
      const next = [];
      for (const node of pool) {
        // 从当前节点的子树里找，但**不包含自己**
        for (const d of node._walk([])) {
          if (d.tagName === tag) next.push(d);
        }
      }
      pool = next;
      if (!pool.length) return [];
    }
    return pool;
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
  getAttribute(name) {
    if (name === "data-label") return this._attrs["data-label"] ?? null;
    if (name === "id") return this.id ?? null;
    return this._attrs[name] ?? null;
  }
  setAttribute(name, v) { this._attrs[name] = String(v); }
  hasAttribute(name) {
    if (name === "colspan") return (this.colSpan || 1) > 1;
    return name in this._attrs;
  }
}

const _registry = new Map();

//: 节点表的骨架。**必须真的建出 table > thead + tbody#nodes 的层级** ——
//: labelCells 靠 ``tbody.closest("table")`` 找到表头取列名。桩里不给
//: 父节点的话，closest 返回 null，labelCells 直接 return，
//: 于是"有没有填 data-label"这件事在测试里根本看不到（假绿）。
const _nodesTable = (() => {
  const table = new FakeEl("table");
  const thead = new FakeEl("thead");
  const htr = new FakeEl("tr");
  for (const name of ["名称 / 简介", "节点", "域", "状态", "批准",
                      "角色", "吞吐（收/成/败）", "水位", "回传", "能力"]) {
    const th = new FakeEl("th");
    th.textContent = name;
    htr.appendChild(th);
  }
  thead.appendChild(htr);
  table.appendChild(thead);
  return table;
})();
_nodesTable.appendChild(_nodesTable._nodesBody = (() => {
  const tb = new FakeEl("tbody");
  tb.id = "nodes";
  return tb;
})());
_registry.set("nodes", _nodesTable._nodesBody);

const document = {
  createElement: (tag) => new FakeEl(tag),
  getElementById: (id) => {
    if (!_registry.has(id)) {
      const e = new FakeEl("div");
      e.id = id;
      _registry.set(id, e);
    }
    return _registry.get(id);
  },
};
const window = {};

__PAGE_SCRIPT__

const DATA = __DATA__;
renderAll(DATA);

const out = {};
for (const [id, e] of _registry.entries()) out[id] = e.textContent;

//: 节点表的每个单元格：文本 + data-label + class。
//: ⚠ 不额外回吐这些，就没法验证「窄屏卡片化时列名有没有填上」——
//: 那是纯 CSS 依赖的属性，textContent 里看不到它。
out.__cells = [..._nodesTable.querySelectorAll("tbody")[0].children]
  .map(tr => [...tr.children].map(td => ({
    text: td.textContent,
    label: td.getAttribute ? td.getAttribute("data-label") : null,
    cls: td.className || "",
    colspan: td.colSpan || 0,
  })));

console.log(JSON.stringify(out));
"""


def _page_script() -> str:
    """取出 ``SHELL`` 里的 ``<script>`` 内容，并剥掉启动代码。

    ⚠ 必须剥掉结尾的 ``refresh(); setInterval(...)``：
    它一执行就会去 fetch，而桩里没有 fetch。/ 另外**不要**改写
    被测试的函数体本身 —— 那样测的就不是真实代码了。
    """
    match = re.search(r"<script>(.*?)</script>", SHELL, re.S)
    assert match, "SHELL 里找不到 <script> 块"
    script = match.group(1)
    script = script.replace("refresh();\n", "")
    script = script.replace(
        "window.__nxsTimer = setInterval(refresh, 5000);\n", "")
    assert "setInterval" not in script, "启动代码没剥干净"
    assert "function renderAll" in script, (
        "页面里没有 renderAll —— 渲染入口被改名了，改这里的测试"
    )
    return script


def _render(data: dict) -> dict[str, str]:
    """把一份 ``/api/state`` 载荷交给真实前端代码渲染，读回各区块文本。"""
    js = (_HARNESS
          .replace("__PAGE_SCRIPT__", _page_script())
          .replace("__DATA__", json.dumps(data, ensure_ascii=False)))
    proc = subprocess.run(
        [NODE, "--input-type=commonjs", "-"],
        input=js, capture_output=True, text=True, timeout=30,
        check=False,
    )
    assert proc.returncode == 0, (
        "前端脚本执行失败（桩不全或代码有错）：\n" + (proc.stderr or "")[-3000:]
    )
    return json.loads(proc.stdout)


def _server(**ops) -> UiServer:
    srv = UiServer("http://127.0.0.1:18800/nxs", SECRET)
    srv._ops = MagicMock()
    return srv


def _healthy_payload(**overrides) -> dict:
    """一份健康快照：一个节点、一条拒绝、一条审计。"""
    srv = _server()
    srv._ops.nodes.return_value = [{
        "node_id": "work-macbook", "display_name": "工作笔记本",
        "description": "处理日历与待办", "domain": "zone-work",
        "state": "alive", "approved": True, "revision": 1,
        "last_seen": "2026-11-13T17:09:41+08:00", "role": "worker",
        "role_definition": {"can_issue": False, "serves": ["work.query"]},
        "may_broadcast": False, "capabilities": ["work.query"],
        "max_response_level": "summary", "self_limits": {},
    }]
    srv._ops.metrics.return_value = {
        "uptime_seconds": 100, "latency_samples": 3,
        "latency_ms": {"p50": 10.0, "p95": 20.0, "p99": 30.0},
        "totals": {"submitted": 2, "completed": 1, "failed": 0, "denied": 1},
        "by_type": {"work.query": {"submitted": 2, "completed": 1,
                                   "failed": 0}},
        "denied_by_rule": {"MX-DOMAIN-02": 1},
        "anomalies": [],
        "nodes": [{"node_id": "work-macbook", "domain": "zone-work",
                   "approved": True, "state": "alive", "received": 2,
                   "completed": 1, "failed": 0, "in_flight": 0, "pending": 0,
                   "capacity": 4, "backlog_ratio": 0.0, "bytes_out": 259,
                   "failure_rate": 0.0, "denials": {}}],
    }
    srv._ops.audit.return_value = {
        "records": [{"ts": "2026-11-13T17:09:00+08:00", "outcome": "completed",
                     "from_node": "taskflow", "to_node": "work-macbook",
                     "task_type": "work.query", "duration_ms": 12,
                     "bytes": 259, "rule_id": ""}],
        "truncated": False, "filtered_heartbeats": 7,
    }
    snap = srv.snapshot()
    # probe 是主机层信息，与本次测试无关；塞个最小结构免得渲染分支跑偏
    snap["probe"] = {"verdict": "healthy", "note": "进程在、ops 端点可达",
                     "warnings": [], "unavailable": []}
    snap.update(overrides)
    return snap


def _no_history_payload() -> dict:
    """真实服务端在「从未连上 coordinator」时产出的载荷。"""
    srv = _server()
    srv._ops.nodes.side_effect = RuntimeError("timed out")
    snap = srv.snapshot()
    snap["probe"] = {"verdict": "unresponsive", "note": "进程活着但不响应",
                     "warnings": [], "unavailable": []}
    return snap


class TestEmptyStateTellsTheTruth:
    """★★ 空列表有多种含义，页面必须说清是哪一种。

    交接文档 §8 质量项原话：「空数据不说「暂无数据」而说清为什么」。

    ⚠ 这条原来是错的（2026-11-13 验收时发现）：``no_history`` —— 也就是
    **从未成功读到过数据** —— 时，节点表显示的是「（注册表为空）」，
    拒绝区显示「无拒绝记录」，日志区显示「暂无审计记录」。

    三句都在声称"确实没有"，而事实是"没读到"。人会据此去查注册表、
    去查策略配置 —— 方向全错。
    """

    @covers("M15.F12")
    def test_no_history_does_not_claim_registry_is_empty(self):
        """★★★ 没读到 ≠ 注册表为空。"""
        text = _render(_no_history_payload())["nodes"]

        assert "注册表为空）" not in text.replace("这不代表注册表为空", ""), (
            "面板没连上却说「注册表为空」—— 人会去查注册表，而问题在连通性"
        )
        assert "没有数据" in text, f"没说清为什么空：{text!r}"
        assert "不可达" in text, f"没给出原因：{text!r}"
        assert "不代表" in text, (
            "要明确说「这不代表注册表为空」，否则读者仍会误判"
        )

    @covers("M15.F12")
    def test_no_history_does_not_claim_zero_denials(self):
        """★★ 指标没读到 ≠ 没有拒绝记录。"""
        text = _render(_no_history_payload())["denials"]
        assert "无拒绝记录" not in text, (
            f"指标不可用却说「无拒绝记录」—— 那读起来像一切合规合规：{text!r}"
        )
        assert "不可用" in text or "无法判断" in text, text

    @covers("M15.F12")
    def test_audit_failure_is_not_reported_as_empty(self):
        """★★ 审计读失败 ≠ 没有审计记录。"""
        text = _render(_no_history_payload())["audit"]
        assert "暂无审计记录" not in text, (
            f"审计没读到却说「暂无审计记录」—— 那是在撒谎：{text!r}"
        )
        assert "读取失败" in text or "不代表" in text, text

    @covers("M15.F10")
    def test_genuinely_empty_registry_still_says_so(self):
        """★★ 反方向：真的空就要说「注册表为空」。

        ⚠ 只把文案改成"永远说没读到"是另一种撒谎 —— 刚部署、还没节点
        接入时，注册表**确实**是空的，这时说「没读到」会让人去查网络。
        """
        payload = _healthy_payload()
        payload["nodes"] = []
        payload["metrics"]["nodes"] = []
        payload["metrics"]["denied_by_rule"] = {}
        text = _render(payload)

        assert "注册表为空" in text["nodes"], (
            f"注册表确实为空时却没这么说：{text['nodes']!r}"
        )
        assert "无拒绝记录" in text["denials"], (
            f"确实没有拒绝时应该照直说：{text['denials']!r}"
        )

    @covers("M15.F10")
    def test_healthy_snapshot_renders_real_values(self):
        """★ 健康快照要画出真实数值（顺带守住合并没有把数据吃掉）。"""
        text = _render(_healthy_payload())

        assert "工作笔记本" in text["nodes"], "节点名没渲染"
        # 性能字段（来自 metrics.nodes，经服务端合并）
        assert "2 / 1 / 0" in text["nodes"], (
            f"吞吐列没画出收/成/败：{text['nodes'][:200]!r}"
        )
        assert "259 B" in text["nodes"], "回传字节没画出来"
        assert "MX-DOMAIN-02" in text["denials"], "拒绝规则没画出来"
        assert "work.query" in text["bytype"], "任务类型分布没画出来"
        assert "已滤 7 条心跳" in text["logstamp"], (
            f"没说明滤掉了多少心跳：{text['logstamp']!r}"
        )


class TestRenderEntryIsSingle:
    """★ 渲染入口必须唯一 —— 预览与真实刷新不能是两份接线。

    ⚠ 原来 ``deploy/preview-ui-state.py`` 自己写着一段
    ``renderStale(REAL); renderProbe(...); renderNodes(...)…`` 的调用序列。
    给 ``renderNodes`` 加了「空态原因」参数后，那一段不会跟着变：
    预览依旧全绿，而它跑的是**过时的接线**。

    现在页面把整条序列收进 ``renderAll(data)``，两边共用。
    """

    @covers("M15.F10")
    def test_refresh_delegates_to_render_all(self):
        """``refresh()`` 里不该再逐个调用各个 render 函数。"""
        match = re.search(r"async function refresh\(\)\s*\{(.*?)\n\}", SHELL, re.S)
        assert match, "找不到 refresh()"
        body = match.group(1)
        assert "renderAll(" in body, "refresh 没有走统一的 renderAll"
        for fn in ("renderStale(", "renderNodes(", "renderAudit(",
                   "renderTiles(", "renderAnomalies("):
            assert fn not in body, (
                f"refresh 里还直接调用 {fn} —— 说明渲染接线有两份，"
                "预览那份迟早和它不一致"
            )

    @covers("M15.F10")
    def test_preview_calls_render_all(self):
        """预览脚本必须调 ``renderAll``，不能自己排调用序列。"""
        import pathlib

        from conftest import REPO_ROOT

        text = (REPO_ROOT / "deploy" / "preview-ui-state.py").read_text()
        assert "renderAll(REAL)" in text, (
            "预览没有走 renderAll —— 它排的是另一份接线"
        )
        for fn in ("renderStale(REAL)", "renderNodes(REAL", "renderAudit(REAL"):
            assert fn not in text, f"预览里还自己调 {fn}"


class TestFirstLoadHint:
    """★ 首次加载要有明确提示（交接文档 §8：「加载中有骨架或明确提示」）。

    ⚠ 空白的数据区与「真的没有数据」长得一模一样。coordinator 慢的时候
    （旧连接会等满超时）用户会盯着几个空白标题看上好几秒，无从判断
    是坏了还是在读。
    """

    @covers("M15.F10")
    def test_boot_hint_exists_in_markup(self):
        assert 'id="boot"' in SHELL, "页面里没有首次加载提示"
        assert "正在读取" in SHELL, "提示文案要说清在做什么"

    @covers("M15.F10")
    def test_boot_hint_cleared_after_render(self):
        """★ 渲染后提示必须消失 —— 否则会与真实状态叠着显示。"""
        text = _render(_healthy_payload())
        assert text.get("boot", "").strip() == "", (
            f"渲染完成后首次加载提示还在：{text.get('boot')!r}"
        )


class TestNarrowScreenLabelsAreFilled:
    """★★ 窄屏卡片化依赖 ``data-label`` —— 必须验证它**真的被填上了**。

    ## 为什么单独一个类

    窄屏（<760px）下 ``thead`` 被隐藏，列名只能靠
    ``td::before { content: attr(data-label) }`` 显示。也就是说：
    **data-label 没填 = 手机上每一行只有值、没有列名**，读起来是一串
    无意义的数字。而这种缺陷不会报错、页面"看起来有内容"。

    ## 桩里的一个假绿（记下来）

    第一版这里有两条坑：

    ① 桩的 ``getElementById("nodes")`` 返回一个**游离**的 tbody，
       没有 table 父节点 → ``closest("table")`` 返回 null →
       ``labelCells`` 直接 return → 什么都没填，测试却全绿。

    ② 桩只回吐 ``textContent``，而 ``data-label`` 是属性、不在文本里，
       所以就算填了也看不见。

    修法：桩里真的建出 ``table > thead + tbody#nodes`` 骨架，
    并把每个单元格的 ``data-label`` / class 一起回吐。
    """

    @covers("M15.F10")
    def test_every_non_title_cell_gets_a_label(self):
        """★★★ 除标题列外，每个单元格都要有 data-label。"""
        cells = _render(_healthy_payload())["__cells"]
        assert cells, "没有回吐节点单元格 —— 桩的骨架没建好"

        missing = []
        for row in cells:
            for c in row:
                if "row-title" in c["cls"]:
                    continue
                if c["colspan"]:
                    continue
                if not c["label"]:
                    missing.append(c["text"][:20])
        assert not missing, (
            "这些单元格没有 data-label —— 手机上会只显示值、没有列名：\n  "
            + "\n  ".join(missing)
        )

    @covers("M15.F10")
    def test_labels_match_table_headers(self):
        """★ 标签内容必须是真实列名（取自表头），不能是占位符。"""
        cells = _render(_healthy_payload())["__cells"]
        labels = [c["label"] for row in cells for c in row if c["label"]]
        assert labels, "一个 data-label 都没有"
        # 关键列名必须在
        for name in ("节点", "域", "状态", "水位", "回传"):
            assert name in labels, f"缺列名 {name!r} —— 那一行在窄屏上没有标签"
        # 不该出现空串或占位符
        assert all(x.strip() for x in labels), f"有空的 data-label：{labels}"

    @covers("M15.F10")
    def test_title_cell_is_not_labelled(self):
        """★ 标题列（节点名/简介）不该有 data-label。

        它是卡片标题，前面加「名称 / 简介:」既占位置又奇怪。
        """
        cells = _render(_healthy_payload())["__cells"]
        first_row = cells[0]
        title = first_row[0]
        assert "row-title" in title["cls"], (
            f"第一个单元格没标 row-title：class={title['cls']!r}"
        )
        assert not title["label"], (
            f"标题列不该有 data-label，但它有：{title['label']!r}"
        )

    @covers("M15.F10")
    def test_empty_state_row_has_no_labels(self):
        """★ 空态行（colspan 占满）不该有 data-label。

        那是一整行一句话（「（注册表为空）」），不是字段。
        有标签会让它显示成「名称 / 简介: （注册表为空）」，读着像数据。
        """
        payload = _healthy_payload()
        payload["nodes"] = []
        payload["metrics"]["nodes"] = []
        cells = _render(payload)["__cells"]
        flat = [c for row in cells for c in row]
        assert flat, "空态行没渲染"
        assert all(not c["label"] for c in flat), (
            f"空态行被填了 data-label：{[c['label'] for c in flat]}"
        )
        assert any(c["colspan"] for c in flat), (
            "空态行没有 colspan —— 桩或渲染逻辑变了，改这里"
        )
