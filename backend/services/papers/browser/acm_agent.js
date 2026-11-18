/**
 * ACM DL cf_clearance 代行脚本（A2 定期登入）：
 * 有头浏览器打开 dl.acm.org，等待用户手动过 Cloudflare managed challenge，
 * 过 CF 后导出含 cf_clearance 的 storage_state，供 download_institution.js 的 acm 分支复用。
 *
 * 用法:
 *   node acm_agent.js [doi]         # 打开该论文页，等过 CF 后验证下载 + 导出 storage_state
 *   可选 --out <dir>                 # 导出目录（默认 data/paper_sessions/acm/）
 *   可选 --test-download             # 过 CF 后实测下载该篇 PDF
 *
 * 依赖: node playwright（已装）
 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const sp = require('./paths');

const DOI = process.argv[2] || '10.1145/3576915.3616608';
// 登录态属于本机运行时数据：必须写在 data/paper_sessions/ 下（见 paths.js）
const OUTDIR = sp.siteDir('acm');
const TEST_DL = process.argv.includes('--test-download');

const SS_PATH = path.join(OUTDIR, 'storage_state.json');
const DOC_URL = `https://dl.acm.org/doi/${DOI}`;

function safe(name) {
  return (name || 'paper').replace(/[^\w一-鿿-]+/g, '_').slice(0, 60);
}

function log(step, ok, detail = '') {
  console.log(`[${ok ? 'OK ' : 'FAIL'}] ${step}${detail ? ' | ' + detail : ''}`);
}

(async () => {
  fs.mkdirSync(OUTDIR, { recursive: true });
  console.log('=== ACM cf_clearance 代行 ===');
  console.log(`论文: ${DOC_URL}`);
  console.log(`导出: ${SS_PATH}`);
  console.log(`\n>>> 即将打开浏览器。如出现 Cloudflare 验证，请手动点击通过（验证码/滑块/确认）。\n`);

  // 必须用有头浏览器 + 系统 Chrome（channel:'chrome'）+ 隐藏自动化指纹（CF 检测）
  // 2026-08-19 实测：bundled chromium/未隐藏 webdriver → CF 反复拒绝；channel:'chrome' + 隐藏 navigator.webdriver → 直接通过
  const browser = await chromium.launch({ headless: false, channel: 'chrome' });
  const ctx = await browser.newContext({
    userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    viewport: { width: 1366, height: 768 },
    locale: 'en-US',
    timezoneId: 'Asia/Shanghai',
    acceptDownloads: true,
  });
  // 隐藏 CF 检测的自动化指纹
  await ctx.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = window.chrome || { runtime: {} };
  });
  const page = await ctx.newPage();

  // 1) 打开 ACM 论文页
  try {
    await page.goto(DOC_URL, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
    log('打开论文页', true);
  } catch (e) {
    log('打开论文页', false, e.message);
  }

  // 2) 等待 CF 自动过 / 用户手动过（轮询标题 + body 特征，最多 180s）
  console.log('\n>>> 等待 Cloudflare 验证通过（如出现验证码请手动点击）...\n');
  let passed = false;
  for (let i = 0; i < 90; i++) {
    await page.waitForTimeout(2000);
    const title = await page.title().catch(() => '');
    const body = await page.content().catch(() => '');
    // CF 页特征：标题 Just a moment，body 短且含 challenge；过 CF 后标题变论文标题
    const stillCF = title.includes('Just a moment') || (body.length < 30000 && body.includes('challenge-platform'));
    if (!stillCF && body.length > 1000) {
      passed = true;
      console.log(`[CF 已过] ${i * 2}s | 标题: ${title.slice(0, 50)}`);
      break;
    }
    if (i % 10 === 0) console.log(`  ...仍在验证 ${i * 2}s（标题: ${title.slice(0, 30)}）`);
  }
  if (!passed) {
    log('过 Cloudflare', false, '180s 未通过');
    await browser.close();
    process.exit(1);
  }
  log('过 Cloudflare', true, '已进入论文页');

  // 3) 导出 storage_state（含 cf_clearance cookie）
  await ctx.storageState({ path: SS_PATH });
  log('导出 storage_state', true, SS_PATH);

  // 4) 检查登录态 / 机构身份（页面应显示机构，或能访问全文）
  try {
    await page.waitForTimeout(3000);
    const info = await page.evaluate(() => {
      const body = document.body ? document.body.innerText : '';
      const title = document.title;
      return {
        title,
        hasInstitution: /Beijing|北京|University|institution|Institution|Access provided/i.test(body),
        bodySnippet: body.slice(0, 150).replace(/\s+/g, ' '),
      };
    });
    log('论文页信息', !!info.title, `标题: ${info.title.slice(0, 60)}`);
    if (info.hasInstitution) log('机构身份', true, '页面含机构信息');
    else log('机构身份', false, '未见机构标记');
  } catch (e) {
    log('论文页信息', false, e.message);
  }

  // 5) 可选：实测下载 PDF（验证 cf_clearance 能用于自动下载）
  if (TEST_DL) {
    console.log('\n=== 实测下载 PDF ===');
    try {
      await page.waitForTimeout(3000);
      // 找 PDF 链接（ACM 页面 PDF 按钮/链接）
      const pdfHref = await page.evaluate(() => {
        const a = [...document.querySelectorAll('a')].find(x => {
          const t = (x.textContent || '').trim();
          const h = x.href || '';
          return (t === 'PDF' || t.toLowerCase().includes('download')) && (h.includes('/pdf/') || h.includes('fulltext'));
        });
        return a ? a.href : '';
      }).catch(() => '');
      if (!pdfHref) { log('找 PDF 链接', false, '未找到'); }
      else {
        log('找 PDF 链接', true, pdfHref.slice(-60));
        // 用页面导航下载（API request 无 JS 上下文会再触发 CF challenge）
        // 方案1: 页面导航到 PDF URL，拦截 download（ACM ?download=true 会触发下载）
        let saved = false;
        const dlPromise = ctx.waitForEvent('download', { timeout: 30000 }).catch(() => null);
        await page.goto(pdfHref, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
        const dl = await dlPromise;
        if (dl) {
          const target = path.join(OUTDIR, safe('acm_' + DOI.replace(/[^0-9]/g, '_')) + '.pdf');
          await dl.saveAs(target);
          log('下载 PDF', true, `download 事件 -> ${target}`);
          saved = true;
        }
        if (!saved) {
          // 方案2: 页面本身就是 PDF，读 body
          await page.waitForTimeout(3000);
          const resp = await page.goto(pdfHref, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => null);
          if (resp) {
            const ct = resp.headers()['content-type'] || '';
            if (ct.includes('pdf')) {
              const buf = await resp.body();
              const target = path.join(OUTDIR, safe('acm_' + DOI.replace(/[^0-9]/g, '_')) + '.pdf');
              fs.writeFileSync(target, buf);
              log('下载 PDF', true, `${buf.length} bytes (${ct}) -> ${target}`);
              saved = true;
            } else {
              log('下载 PDF', false, `非 PDF (${ct})`);
            }
          }
        }
      }
    } catch (e) {
      log('下载 PDF', false, e.message);
    }
  }

  console.log('\n=== 完成 ===');
  console.log(`storage_state 已导出: ${SS_PATH}`);
  console.log('download_institution.js 的 acm 分支将复用此 cookie（cf_clearance 有效数小时~数天）。');
  await browser.close();
})().catch((e) => {
  console.error('脚本异常:', e);
  process.exit(1);
});
