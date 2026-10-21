const { chromium } = require('D:/dsh/deepseek-harness/node_modules/playwright');
(async () => {
  // 先用 API 打开摄像头，让预览区有真实画面
  try {
    const cams = await (await fetch('http://127.0.0.1:8160/api/cameras')).json();
    const idx = cams.current ?? (cams.cameras?.[0]?.index ?? 0);
    await fetch('http://127.0.0.1:8160/api/camera/select', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ index: idx }),
    });
    const models = await (await fetch('http://127.0.0.1:8160/api/models')).json();
    const name = models.models?.[0]?.name;
    if (name) await fetch('http://127.0.0.1:8160/api/models/select', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
    });
    console.log('  预热: 摄像头=' + idx + ' 模型=' + name);
  } catch (e) { console.log('  预热失败: ' + e.message); }

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 950 }, deviceScaleFactor: 1 });
  await page.addInitScript(() => window.localStorage.setItem('trash-sorter-theme', 'dark'));
  await page.goto('http://127.0.0.1:8160/', { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(4500);

  const dump = await page.evaluate(() => {
    const out = [];
    const seen = new Set();
    document.querySelectorAll('*').forEach((el) => {
      const cls = (el.className && typeof el.className === 'string') ? el.className.trim() : '';
      if (!cls) return;
      const key = cls;
      const box = el.getBoundingClientRect();
      if (box.width < 40 || box.height < 18) return;
      const txt = (el.innerText || '').replace(/\s+/g, ' ').slice(0, 40);
      if (seen.has(key + txt)) return;
      seen.add(key + txt);
      out.push({ tag: el.tagName.toLowerCase(), cls, w: Math.round(box.width), h: Math.round(box.height), x: Math.round(box.left), y: Math.round(box.top), txt });
    });
    return out;
  });
  console.log('  === 主要可见容器（class / 尺寸 / 文本） ===');
  dump.filter((d) => d.w > 120 && d.h > 40).slice(0, 60).forEach((d) => {
    console.log('   ' + d.tag + '.' + d.cls.split(' ').join('.') + '  [' + d.w + 'x' + d.h + ' @' + d.x + ',' + d.y + ']  "' + d.txt + '"');
  });
  console.log('  === 菜单项 ===');
  const menu = await page.evaluate(() => Array.from(document.querySelectorAll('.ant-menu-item')).map((el) => (el.innerText || '').trim()));
  console.log('   ' + JSON.stringify(menu));
  await browser.close();
})();
