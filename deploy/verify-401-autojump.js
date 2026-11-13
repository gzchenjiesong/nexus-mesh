// 端到端验证 401 页面的自动跳转 —— **必须在同一个浏览器上下文里跑**。
//
// ⚠ 第一版这里用两个 newPage() 测「第二次是否不再跳」，结果是假阴性：
//    Playwright 的 newPage() 会新建一个 BrowserContext，sessionStorage
//    自然隔离，第二条当然读不到第一条写的标记。
//
//    而真实的循环发生在**同一个标签页**里：
//        401 → 自动跳 hub → 303 回 /?t=… → 若又失效 → 401（同一 tab）
//    所以必须复用同一个 page 才是正确测法。
//
// 这条教训值得记：测「跨请求的状态保持」时，隔离环境的建法本身就会
// 决定结论 —— 隔离了就等于每次都是"第一次"。
const { chromium } = require('playwright');
const fs = require('fs');
const http = require('http');

const DIR = '/tmp/nxs-401';

function startServer() {
  return new Promise(resolve => {
    const srv = http.createServer((req, res) => {
      if (req.url.startsWith('/enter')) {
        res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
        res.end('RECOVERED');
        return;
      }
      res.writeHead(401, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(fs.readFileSync(`${DIR}/page-local.html`));
    });
    srv.listen(0, '127.0.0.1', () => resolve(srv));
  });
}

(async () => {
  // 把恢复目标换成本地 /enter，其他内容保持与真实页面完全一致
  const raw = fs.readFileSync(`${DIR}/page.html`, 'utf8');
  fs.writeFileSync(
    `${DIR}/page-local.html`,
    raw.replace(/https:\/\/[^"']*\/api\/nexus-enter/g, '__LOCAL_ENTER__'),
  );

  const srv = await startServer();
  const port = srv.address().port;
  const BASE = `http://127.0.0.1:${port}`;
  // 服务器启动后才知道端口，所以再写一次（把占位符替换掉）
  fs.writeFileSync(
    `${DIR}/page-local.html`,
    raw.replace(/https:\/\/[^"']*\/api\/nexus-enter/g, `${BASE}/enter`),
  );

  const browser = await chromium.launch();
  const page = await browser.newPage();       // ← 全程只用这一个 page

  console.log('① 首次进入（模拟服务重启后的旧标签）');
  await page.goto(`${BASE}/stale`);
  console.log('   按钮文案:', JSON.stringify(await page.textContent('#recover')));
  console.log('   倒计时:', JSON.stringify(await page.textContent('#count')));
  await page.waitForURL(`${BASE}/enter`, { timeout: 8000 }).catch(() => {});
  console.log('   跳转到:', page.url());
  console.log('   → 自动跳转生效:', page.url().includes('/enter'));

  console.log('\n② 同一标签页内再次撞 401（这就是循环的形态）');
  await page.goto(`${BASE}/stale`);
  const count2 = await page.textContent('#count');
  console.log('   提示:', JSON.stringify(count2));
  await page.waitForTimeout(5000);
  console.log('   等 5 秒后 URL:', page.url());
  const stayed = page.url().includes('/stale');
  console.log('   → 防循环生效（没再跳）:', stayed);
  console.log('   → 有解释为什么不动:', (count2 || '').includes('未成功'));

  console.log('\n③ 手动按钮随时可用');
  const href = await page.getAttribute('#recover', 'href');
  console.log('   href:', href);
  await page.click('#recover');
  await page.waitForURL(`${BASE}/enter`, { timeout: 5000 }).catch(() => {});
  console.log('   点击后:', page.url());
  console.log('   → 手动可用:', page.url().includes('/enter'));

  console.log('\n④ 时间窗过去后（模拟 60 秒后）应重新允许自动跳');
  await page.goto(`${BASE}/stale`);
  await page.evaluate(() => {
    // 把标记改成 61 秒前 —— 代表上一次尝试已过窗口
    sessionStorage.setItem('nxs_ui_recover_at', String(Date.now() - 61000));
  });
  await page.goto(`${BASE}/stale`);
  await page.waitForURL(`${BASE}/enter`, { timeout: 8000 }).catch(() => {});
  console.log('   跳转到:', page.url());
  console.log('   → 窗口过后恢复自动跳:', page.url().includes('/enter'));

  await browser.close();
  srv.close();
})();
