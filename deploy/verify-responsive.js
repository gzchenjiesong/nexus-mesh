// 响应式验收：扫 20 个宽度 × 4 种状态，确认无横向溢出。
//
// ⚠ **为什么要扫这么多宽度，而不是只看 390px。**
//
// 第一版修 390px 时我把断点拍在 680px，结果 390px 修好了、681–749px
// 区间继续溢出 —— 只是把坏点挪到了别人身上。所以必须扫全区间，
// 特别要覆盖断点两侧（759/760）和常见的平板宽度（768/800）。
//
// 用法（需要 /tmp/uitest 里有 playwright）：
//     cp deploy/verify-responsive.js /tmp/uitest/
//     cd /tmp/uitest && node verify-responsive.js
//
// 前置：先跑 `python3 deploy/preview-ui-state.py` 生成四个状态页。
const { chromium } = require('playwright');

const WIDTHS = [320, 360, 375, 390, 414, 430, 540, 600, 660, 700,
                720, 750, 759, 760, 768, 800, 900, 1024, 1280, 1600];
const STATES = ['live', 'stale', 'no_history', 'sparse'];

(async () => {
  const b = await chromium.launch();
  const fails = [];
  const modes = {};

  for (const w of WIDTHS) {
    for (const st of STATES) {
      const p = await b.newPage({ viewport: { width: w, height: 900 } });
      await p.goto(`file:///tmp/ui-state-${st}.html`);
      await p.waitForTimeout(180);
      const r = await p.evaluate(() => {
        const tbody = document.getElementById('nodes');
        const tbl = tbody ? tbody.closest('table') : null;
        const thead = tbl ? tbl.querySelector('thead') : null;
        return {
          docW: document.documentElement.scrollWidth,
          W: window.innerWidth,
          card: thead ? getComputedStyle(thead).display === 'none' : false,
        };
      });
      if (r.docW > r.W + 1) {
        fails.push({ w, st, over: r.docW - r.W });
      }
      modes[w] = r.card ? '卡片' : '表格';
      await p.close();
    }
  }

  console.log(`扫描 ${WIDTHS.length} 个宽度 × ${STATES.length} 个状态 `
    + `= ${WIDTHS.length * STATES.length} 组`);
  const switchOK = modes[759] === '卡片' && modes[760] === '表格'
    && modes[320] === '卡片' && modes[1600] === '表格';
  console.log(`断点两侧的模式切换正确: ${switchOK ? '是' : '否'}`
    + `（759=${modes[759]} 760=${modes[760]}）`);

  if (fails.length) {
    console.log(`✗ ${fails.length} 组溢出:`);
    fails.forEach(f => console.log(`   ${f.w}px [${f.st}] 溢出 ${f.over}px`));
  } else {
    console.log(`✓ 全部无横向溢出（${WIDTHS[0]}px – ${WIDTHS[WIDTHS.length - 1]}px）`);
  }

  await b.close();
  process.exit(fails.length || !switchOK ? 1 : 0);
})();
