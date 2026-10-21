// 截取引用截图并输出每个 UI 检查点的真实坐标（供 Python 画编号+引线）
// 用法：node docs/testbook/capture_ui.js
const fs = require('fs');
const path = require('path');
const { chromium } = require('D:/dsh/deepseek-harness/node_modules/playwright');

const BASE = 'http://127.0.0.1:8160';
const OUT = path.join(__dirname, 'shots');
fs.mkdirSync(OUT, { recursive: true });

// 每个检查点：编号、名称、定位方式
const ITEMS = [
  { no: 1,  label: '顶部标题与品牌区',      find: (h) => h.rect('header.app-header') },
  { no: 2,  label: 'HUD 状态条',           find: (h) => h.smallestText(/^SYS/) || h.rect('header.app-header') },
  { no: 3,  label: '侧边导航（页面切换）',  find: (h) => h.rect('.app-sider ul.ant-menu') || h.rect('.app-sider') },
  { no: 4,  label: '摄像头卡片头（选择/刷新/预览）', find: (h) => h.rect('.panel-card .ant-card-head') },
  { no: 5,  label: '实时视频预览区',        find: (h) => h.rect('.preview-frame') },
  { no: 6,  label: '摄像头运行统计',        find: (h) => h.rect('.stat-row') },
  { no: 7,  label: '抓拍训练图区',          find: (h) => h.smallestText(/采集训练图/) },
  { no: 8,  label: '识别结果卡片头（FPS/单帧耗时）', find: (h) => h.rectAll('.panel-card')[1] ? h.rect('.panel-card .ant-card-extra') : null },
  { no: 9,  label: '多目标策略切换',        find: (h) => h.smallestText(/多目标策略/) },
  { no: 10, label: '当前识别主结果',        find: (h) => h.rect('.result-hero') },
  { no: 11, label: '置信度表盘',            find: (h) => h.rect('.hud-ring') },
  { no: 12, label: '类别 → 物理桶明细表',    find: (h) => h.rect('.ant-table') },
  { no: 13, label: '分拣统计图（环形/条形）', find: (h) => h.rect('.donut') },
  { no: 14, label: '近 10 分钟分拣趋势',     find: (h) => h.rect('.sparkline') },
  { no: 15, label: '下发历史列表',          find: (h) => h.rect('.history-box') },
  { no: 16, label: '模型管理卡片',          find: (h) => h.rectAll('.panel-card')[2] },
  { no: 17, label: '串口管理卡片',          find: (h) => h.rectAll('.panel-card')[3] },
  { no: 18, label: '主题切换',              find: (h) => h.smallestText(/深色|浅色/) },
];

async function main() {
  // 预热：打开摄像头 + 加载模型，让截图里有真实画面
  try {
    const cams = await (await fetch(BASE + '/api/cameras')).json();
    const idx = cams.current ?? (cams.cameras?.[0]?.index ?? 0);
    await fetch(BASE + '/api/camera/select', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ index: idx }),
    });
    const models = await (await fetch(BASE + '/api/models')).json();
    const name = models.models?.[0]?.name;
    if (name) {
      await fetch(BASE + '/api/models/select', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
    }
    console.log('预热完成: 摄像头=' + idx + ' 模型=' + name);
  } catch (e) {
    console.log('预热失败: ' + e.message);
  }

  const browser = await chromium.launch({ channel: 'msedge', headless: true });

  const helpers = `
    window.__h = {
      rect(sel) {
        const el = document.querySelector(sel);
        if (!el) return null;
        const b = el.getBoundingClientRect();
        return { x: b.left, y: b.top + window.scrollY, w: b.width, h: b.height };
      },
      rectAll(sel) {
        return Array.from(document.querySelectorAll(sel)).map((el) => {
          const b = el.getBoundingClientRect();
          return { x: b.left, y: b.top + window.scrollY, w: b.width, h: b.height,
                   text: (el.innerText || '').replace(/\\s+/g, ' ').slice(0, 40) };
        });
      },
      smallestText(re) {
        let best = null;
        document.querySelectorAll('div,span,li,button,a,section').forEach((el) => {
          const t = (el.innerText || '').trim();
          if (!t || t.length > 80 || !re.test(t)) return;
          const b = el.getBoundingClientRect();
          if (b.width < 30 || b.height < 14) return;
          const area = b.width * b.height;
          if (!best || area < best.area) best = { area, x: b.left, y: b.top + window.scrollY, w: b.width, h: b.height, text: t.slice(0, 40) };
        });
        return best ? { x: best.x, y: best.y, w: best.w, h: best.h, text: best.text } : null;
      },
    };
  `;

  async function shoot(theme, file) {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
    await page.addInitScript((t) => window.localStorage.setItem('trash-sorter-theme', t), theme);
    await page.goto(BASE + '/', { waitUntil: 'load', timeout: 60000 });
    await page.waitForTimeout(4500);
    await page.mouse.move(1200, 260);
    await page.waitForTimeout(800);
    await page.screenshot({ path: path.join(OUT, file), fullPage: true });
    const size = await page.evaluate(() => ({ w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight }));
    return { page, size };
  }

  // ---- 深色：整页 + 各检查点坐标 ----
  const dark = await shoot('dark', 'ui-dark.png');
  const page = dark.page;
  await page.evaluate(helpers);
  const boxes = await page.evaluate((items) => {
    const h = window.__h;
    const allCards = h.rectAll('.panel-card');
    const out = [];
    for (const it of items) {
      let box = null;
      switch (it.no) {
        case 1: box = h.rect('header.app-header'); break;
        case 2: box = h.smallestText(/^SYS/) || null; break;
        case 3: box = h.rect('.app-sider ul.ant-menu') || h.rect('.app-sider'); break;
        case 4: box = h.rect('.panel-card .ant-card-head'); break;
        case 5: box = h.rect('.preview-frame'); break;
        case 6: box = h.rect('.stat-row'); break;
        case 7: box = h.smallestText(/采集训练图/); break;
        case 8: box = h.rectAll('.ant-card-extra')[1] || h.rect('.ant-card-extra'); break;
        case 9: box = h.smallestText(/多目标策略/); break;
        case 10: box = h.rect('.result-hero'); break;
        case 11: box = h.rect('.hud-ring'); break;
        case 12: box = h.rect('.ant-table'); break;
        case 13: box = h.rect('.donut'); break;
        case 14: box = h.rect('.sparkline'); break;
        case 15: box = h.rect('.history-box'); break;
        case 16: box = allCards[2] || null; break;
        case 17: box = allCards[3] || null; break;
        case 18: box = h.smallestText(/深色|浅色/); break;
        default: box = null;
      }
      out.push({ no: it.no, label: it.label, box });
    }
    return out;
  }, ITEMS.map((i) => ({ no: i.no, label: i.label })));

  console.log('\n=== 检查点坐标（CSS 像素，1:1 对应截图）===');
  for (const b of boxes) {
    const s = b.box ? `@${Math.round(b.box.x)},${Math.round(b.box.y)} ${Math.round(b.box.w)}x${Math.round(b.box.h)}` : '未找到';
    console.log(`  ${String(b.no).padStart(2)} ${b.label}  ${s}`);
  }
  console.log('  整页尺寸:', dark.size.w + 'x' + dark.size.h);

  // ---- 浅色整页 ----
  const light = await shoot('light', 'ui-light.png');
  console.log('浅色尺寸:', light.size.w + 'x' + light.size.h);
  await light.page.close();

  // ---- 数据标注页 ----
  await page.locator('.ant-menu-item', { hasText: '数据标注' }).first().click().catch(() => {});
  await page.waitForTimeout(3500);
  await page.screenshot({ path: path.join(OUT, 'ui-annotate.png'), fullPage: true });
  const annotateSize = await page.evaluate(() => ({ w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight }));
  console.log('标注页尺寸:', annotateSize.w + 'x' + annotateSize.h);
  await page.close();

  // ---- 关键区域细节裁切 ----
  const page2 = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
  await page2.addInitScript(() => window.localStorage.setItem('trash-sorter-theme', 'dark'));
  await page2.goto(BASE + '/', { waitUntil: 'load', timeout: 60000 });
  await page2.waitForTimeout(4000);
  await page2.evaluate(helpers);
  const cardBoxes = await page2.evaluate(() => window.__h.rectAll('.panel-card'));
  const crops = [
    { file: 'crop-camera.png', box: cardBoxes[0] },
    { file: 'crop-result.png', box: cardBoxes[1] },
    { file: 'crop-model.png', box: cardBoxes[2] },
    { file: 'crop-serial.png', box: cardBoxes[3] },
  ];
  for (const c of crops) {
    if (!c.box) continue;
    await page2.screenshot({
      path: path.join(OUT, c.file),
      clip: { x: Math.max(0, c.box.x - 6), y: Math.max(0, c.box.y - 6), width: c.box.w + 12, height: Math.min(c.box.h + 12, 3000) },
    });
    console.log('裁切:', c.file, Math.round(c.box.w) + 'x' + Math.round(c.box.h));
  }
  await page2.close();

  fs.writeFileSync(
    path.join(OUT, 'boxes.json'),
    JSON.stringify({ page: dark.size, annotate: annotateSize, boxes, cards: cardBoxes }, null, 2),
    'utf8',
  );
  console.log('\n已写入 shots/boxes.json 与全部截图');
  await browser.close();
}

main().catch((e) => { console.error(e); process.exit(1); });
