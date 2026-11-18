/**
 * CNKI "AI 代行" 独立脚本：复用导出的 storage_state（cookie），
 * 验证关闭原浏览器后能否继续搜索 / 查信息 / 下载 PDF。
 *
 * 用法:  node cnki_agent.js ["关键词"]
 * 依赖:  node playwright（已装，chromium）
 */
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');
const sp = require('./paths');

// 登录态与下载残留都在 data/paper_sessions/cnki/（本机运行时数据，不进仓库）
const STORAGE = sp.storageState('cnki');
const OUTDIR = sp.downloadsDir('cnki');
const QUERY = process.argv[2] || '工业互联网标识解析';

const BASE_URL = (kw) =>
  `https://kns.cnki.net/kns8s/search?classid=WD0FTY92&kw=${encodeURIComponent(kw)}` +
  '&korder=SU&language=CHS&uniplatform=NZKPT';

function log(step, ok, detail = '') {
  console.log(`[${ok ? 'OK ' : 'FAIL'}] ${step}${detail ? ' | ' + detail : ''}`);
}

const { requireEnv } = require('./deployment');

(async () => {
  const institutionName = requireEnv('CNKI_INSTITUTION_NAME');
  if (!fs.existsSync(STORAGE)) {
    console.error(`找不到 storage_state: ${STORAGE}`);
    process.exit(1);
  }
  fs.mkdirSync(OUTDIR, { recursive: true });

  console.log('=== 用导出的 cookie 启动全新 Chromium（headless） ===\n');
  // 关键：知网 kns8s 对 headless 返回 403，必须用有头浏览器复用 cookie
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext({ storageState: STORAGE });
  const page = await context.newPage();

  // 1) 打开知网首页，检查登录态
  try {
    await page.goto('https://kns.cnki.net/', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForTimeout(2500);
    const state = await page.evaluate((name) => {
      const body = document.body ? document.body.innerText : '';
      return {
        title: document.title,
        hasInstitution: body.includes(name),
        hasLoginLink: body.includes('个人登录'),
      };
    }, institutionName);
    log('登录态', state.hasInstitution, `title=${state.title}`);
  } catch (e) {
    log('登录态', false, e.message);
  }

  // 2) 搜索
  let resultLinks = [];
  try {
    await page.goto(BASE_URL(QUERY), { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForTimeout(4000);
    // 等结果链接出现
    await page.waitForSelector('a[href*="kcms2/article"]', { timeout: 20000 }).catch(() => {});
    resultLinks = await page.evaluate(() =>
      [...document.querySelectorAll('a[href*="kcms2/article"]')].map(a => ({
        title: a.innerText.trim().slice(0, 60),
        href: a.href,
      }))
    );
    log('搜索', resultLinks.length > 0, `找到 ${resultLinks.length} 条，首条: ${resultLinks[0]?.title || ''}`);
  } catch (e) {
    log('搜索', false, e.message);
  }

  // 3) 打开第一篇，提取信息
  let abstractUrl = resultLinks[0]?.href;
  let info = {};
  if (abstractUrl) {
    try {
      await page.goto(abstractUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForTimeout(3000);
      info = await page.evaluate(() => {
        const txt = (sel) => {
          const el = document.querySelector(sel);
          return el ? el.innerText.trim().replace(/\s+/g, ' ') : '';
        };
        return {
          title: document.title.replace(/ - 中国知网$/, ''),
          author: txt('.author') || txt('[class*="author"]'),
          abstract: (txt('#ChDivSummary') || '').slice(0, 120),
        };
      });
      log('信息查询', !!info.title, `《${info.title.slice(0, 40)}》 | 作者: ${info.author || '?'}`);
    } catch (e) {
      log('信息查询', false, e.message);
    }
  } else {
    log('信息查询', false, '无详情链接');
  }

  // 4) 下载 PDF：从详情页取 bar.cnki.net 下载链接，用 context.request（共享 cookie）抓取
  let downloadStatus = 'paywall';
  let downloadedPath = '';
  try {
    const dlHref = await page.evaluate(() => {
      const a = [...document.querySelectorAll('a')].find(
        (x) => x.innerText.trim() === 'PDF下载' && (x.href || '').includes('bar.cnki.net')
      );
      return a ? a.href : '';
    });
    if (dlHref) {
      // 关键：bar.cnki.net 下载必须带 Referer: https://kns.cnki.net/，否则返回拦截页
      const resp = await context.request.get(dlHref, { timeout: 60000, headers: { Referer: 'https://kns.cnki.net/' } });
      const buf = await resp.body();
      if (buf.length > 1000 && buf.slice(0, 4).toString() === '%PDF') {
        const safe = (info.title || QUERY).replace(/[^\w一-鿿-]+/g, '_').slice(0, 60);
        downloadedPath = path.join(OUTDIR, safe + '.pdf');
        fs.writeFileSync(downloadedPath, buf);
        downloadStatus = 'downloaded';
        log('下载 PDF', true, `${buf.length} bytes -> ${downloadedPath}`);
      } else {
        downloadStatus = 'oa_link_only';
        log('下载 PDF', false, `响应 ${buf.length}B，非 PDF（可能被拦截/验证码）`);
      }
    } else {
      log('下载 PDF', false, '页面无 bar.cnki.net PDF下载链接（可能需验证码或未授权）');
    }
  } catch (e) {
    log('下载 PDF', false, e.message);
  }

  console.log('\n=== 结果汇总 ===');
  console.log(`登录态: ${info.title || '?'} | 搜索条数: ${resultLinks.length} | 下载: ${downloadStatus}`);
  await browser.close();
})().catch((e) => {
  console.error('脚本异常:', e);
  process.exit(1);
});
