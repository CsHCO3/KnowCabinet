/**
 * 知网/万方/IEEE(elib)/ACM 机构批量下载（Playwright 脚本，确定性 A1，脱离 agent）。
 *
 * 用法:
 *   node download_institution.js <papers.json> [输出目录] [--limit N] [--proxy=auto|socks5|none]
 *
 * papers.json: run_task 导出的机构版记录数组，每项含 {source:'cnki'|'wanfang'|'ieee'|'acm', title, link/doi/arnumber, ...}
 *
 * 依赖 cookie（都在 data/paper_sessions/<站点>/storage_state.json，本地数据不进仓库）:
 *   cnki    -> data/paper_sessions/cnki/storage_state.json
 *   wanfang -> data/paper_sessions/wanfang/storage_state.json
 *   cookie 失效时需重新登录导出（见 学校认证网站鉴权方法.md）
 *
 * IEEE(elib): 走 elib.ustb.edu.cn 代理（token 由 elib_token.js 动态解析，任意主机名；2026-09-17 重写实测）。
 *   record 可给 {source:'ieee', doi} 或 {source:'ieee', arnumber}；arnumber 优先由 DOI 直接推出（10.1109/….<arnumber>）。
 *   PDF 取 stampPDF/getPDF.jsp（实测直链最稳），stamp.jsp iframe 兜底。**不需要校园网 / wssocks**。
 * ACM: dl.acm.org 有 Cloudflare 反爬，需有头浏览器过 CF + 校园IP（wssocks 在线/校园网）。record 需含 {source:'acm', doi, title}。
 *
 * 注意: 万方每次下载从机构账户扣约 3 元/积分，--limit 控制本次下载篇数。
 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

// 参数解析：兼容两种调用签名（run_task.py 传 3 个位置参数；main.py 多传一个 --proxy=<mode>）
//   node download_institution.js <papers.json> [输出目录] [--limit N] [--proxy=auto|socks5|none]
const _argv = process.argv.slice(2);
const _proxyArg = _argv.find(a => a.startsWith('--proxy='));
const _limitArg = _argv.find(a => a.startsWith('--limit='));
const _pos = _argv.filter(a => !a.startsWith('--'));
const INPUT = _pos[0];
const OUTDIR = _pos[1] || 'downloads';
// 第 3 个位置参数既可能是 limit 数字，也可能是旧式 --proxy 误传成的位置参数（容错）
const _third = _pos[2] || '';
const LIMIT = parseInt(_limitArg ? _limitArg.split('=')[1] : (/^\d+$/.test(_third) ? _third : '10'), 10);
const PROXY_MODE = (_proxyArg ? _proxyArg.replace(/^--proxy=/, '') : (/^(auto|socks5|none)$/.test(_third) ? _third : 'auto'));

// elib 代理 token 解析（动态，支持任意主机名；见 论文检索\elib校外访问代理自动化.md）
const elibTok = require('./elib_token');
const sp = require('./paths');

const CNKI_SS = sp.storageState('cnki');
const WF_SS = sp.storageState('wanfang');
const ACM_SS = sp.storageState('acm');
// elib 强制登录（2026-10-27 起）后的会话，由 `论文检索/elib-proxy/elib-cli.js login` 人工登录一次生成
const { root, requireEnv } = require('./deployment');
/** 走 elib 代理的浏览器上下文统一注入 elib 登录会话 */
function elibCtxOpts(extra = {}) {
  const o = { ...extra };
  o.storageState = path.resolve(root, requireEnv('ELIB_SESSION_PATH'));
  return o;
}

function safe(name) {
  return (name || 'paper').replace(/[^\w一-鿿-]+/g, '_').slice(0, 60);
}

if (!INPUT || !fs.existsSync(INPUT)) {
  console.error('用法: node download_institution.js <papers.json> [输出目录] [--limit N] [--proxy=auto|socks5|none]');
  process.exit(1);
}
fs.mkdirSync(OUTDIR, { recursive: true });
const records = JSON.parse(fs.readFileSync(INPUT, 'utf-8'));
const cnki = records.filter(r => r.source === 'cnki').slice(0, LIMIT);
const wf = records.filter(r => r.source === 'wanfang').slice(0, LIMIT);
const ieee = records.filter(r => r.source === 'ieee').slice(0, LIMIT);
const acm = records.filter(r => r.source === 'acm').slice(0, LIMIT);
console.log(`待下载: 知网 ${cnki.length} ｜ 万方 ${wf.length} ｜ IEEE ${ieee.length} ｜ ACM ${acm.length} ｜ 输出 ${OUTDIR}\n`);

(async () => {
  // 网络路由（2026-09-17 修正）：
  //   auto/none = 直连。**auto 不再自动开隧道** —— 原先"隧道在线就走 socks5"是反的：
  //     ① IEEE/ACM 走 elib 反向代理，elib 自带机构身份，把整个浏览器塞进隧道会让 WAF 挑战超时
  //        （实测同一篇 IEEE：直连 21.7s 成功 / 隧道 300s 超时）；
  //     ② 知网/万方靠 cookie 鉴权，也不因隧道受益。
  //   socks5 = 显式强制隧道（仅当某源确实按【源 IP】判权且无代理通道时才用）。
  let proxyOpt = {};
  if (PROXY_MODE === 'socks5') {
    const host = requireEnv('SSH_PROXY_HOST');
    const port = Number(requireEnv('SSH_PROXY_PORT'));
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error('SSH_PROXY_PORT 必须是有效端口');
    }
    proxyOpt = { server: `socks5://${host}:${port}` };
    console.log('[网络] 强制走 wssocks SOCKS5 隧道（注意：对 elib/WAF 站点会超时）\n');
  } else {
    console.log('[网络] 直连');
  }
  const sourceSessions = [
    [cnki.length, CNKI_SS, '知网'],
    [wf.length, WF_SS, '万方'],
  ];
  if (ieee.length || acm.length) {
    requireEnv('ELIB_PROXY_ORIGIN');
    requireEnv('ELIB_KEY_HEX');
    const session = path.resolve(root, requireEnv('ELIB_SESSION_PATH'));
    const ticketName = requireEnv('ELIB_TICKET_COOKIE');
    if (!fs.existsSync(session)) throw new Error('缺少图书馆代理的授权会话文件');
    const state = JSON.parse(fs.readFileSync(session, 'utf8'));
    if (!state.cookies?.some(cookie => cookie.name === ticketName && cookie.value)) {
      throw new Error('图书馆授权会话缺少票据，请重新登录并导出');
    }
  }
  for (const [count, file, label] of sourceSessions) {
    if (count && !fs.existsSync(file)) throw new Error(`缺少${label}的授权会话文件`);
  }
  const launchOpt = { headless: false, channel: 'chrome' };  // channel:chrome 系统Chrome（ACM 过 CF 必需，指纹更真实）
  if (proxyOpt.server) launchOpt.proxy = proxyOpt;
  const browser = await chromium.launch(launchOpt);

  // ── 知网：详情页取 PDF 链接 → context.request 下载 ──
  if (cnki.length) {
    console.log('=== 知网 ===');
    const ctx = await browser.newContext({ storageState: CNKI_SS });
    const page = await ctx.newPage();
    let ok = 0;
    for (const rec of cnki) {
      try {
        await page.goto(rec.link, { waitUntil: 'domcontentloaded', timeout: 40000 });
        await page.waitForTimeout(2500);
        const dlHref = await page.evaluate(() => {
          const a = [...document.querySelectorAll('a')].find(
            x => (x.innerText || '').trim() === 'PDF下载' && (x.href || '').includes('bar.cnki.net')
          );
          return a ? a.href : '';
        });
        if (!dlHref) { console.log('  [无链接] ' + (rec.title || '').slice(0, 30)); continue; }
        const resp = await ctx.request.get(dlHref, { headers: { Referer: 'https://kns.cnki.net/' }, timeout: 60000 });
        const buf = await resp.body();
        if (buf.slice(0, 4).toString() === '%PDF') {
          fs.writeFileSync(path.join(OUTDIR, safe(rec.title) + '.pdf'), buf);
          ok++;
          console.log(`  [下载] ${buf.length} bytes ${(rec.title || '').slice(0, 36)}`);
        } else {
          console.log('  [非PDF] ' + (rec.title || '').slice(0, 30) + ` (${buf.length}B)`);
        }
      } catch (e) {
        console.log('  [失败] ' + (rec.title || '').slice(0, 30) + ' | ' + String(e.message).slice(0, 50));
      }
    }
    console.log(`知网完成: ${ok}/${cnki.length}\n`);
    await ctx.close();
  }

  // ── 万方：点下载按钮 + download 事件 ──
  if (wf.length) {
    console.log('=== 万方 ===');
    const ctx = await browser.newContext({ storageState: WF_SS });
    const page = await ctx.newPage();
    let ok = 0;
    for (const rec of wf) {
      try {
        await page.goto(rec.link, { waitUntil: 'domcontentloaded', timeout: 40000 });
        await page.waitForTimeout(2500);
        const btn = page.locator('a.download.buttonItem').first();
        if ((await btn.count()) === 0) { console.log('  [无按钮] ' + (rec.title || '').slice(0, 30)); continue; }
        // 万方点下载会开新标签页(Fulltext/Download+transaction)再触发下载 → 监听 context 级 download
        const dlPromise = ctx.waitForEvent('download', { timeout: 40000 }).catch(() => null);
        await btn.click();
        const dl = await dlPromise;
        if (dl) {
          const target = path.join(OUTDIR, safe(rec.title) + '.pdf');
          await dl.saveAs(target);
          ok++;
          console.log(`  [下载] ${(rec.title || '').slice(0, 36)}`);
        } else {
          console.log('  [事件未触发] ' + (rec.title || '').slice(0, 30));
        }
      } catch (e) {
        console.log('  [失败] ' + (rec.title || '').slice(0, 30) + ' | ' + String(e.message).slice(0, 50));
      }
    }
    console.log(`万方完成: ${ok}/${wf.length}`);
    await ctx.close();
  }

  // ── IEEE：elib 代理（token 由 elib_token.js 解析；2026-09-17 重写并实测）──
  //   下载优先级：① stampPDF/getPDF.jsp 直链（实测最稳） ② stamp.jsp iframe 兜底
  if (ieee.length) {
    console.log('=== IEEE (elib 代理) ===');
    const ctx = await browser.newContext(elibCtxOpts({
      userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    }));
    const page = await ctx.newPage();
    let ok = 0;
    for (const rec of ieee) {
      try {
        // arnumber 三级解析：① rec.arnumber ② Crossref(直连HTTP) ③ doi.org 经代理解析 ④ 老式尾段
        let arnumber = rec.arnumber || (rec.link && rec.link.match(/document\/(\d+)/)?.[1]) || '';
        if (!arnumber && rec.doi) arnumber = await elibTok.arnumberFromCrossref(rec.doi);
        if (!arnumber && rec.doi) {
          const target = `https://doi.org/${rec.doi}`;
          const resolver = await page.goto(elibTok.proxyUrl(target) || target,
            { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => null);
          await page.waitForTimeout(4000);
          arnumber = ((resolver && resolver.url()) || page.url() || '').match(/document\/(\d+)/)?.[1] || '';
          if (arnumber) console.log(`  [DOI解析] ${rec.doi} -> arnumber ${arnumber}`);
        }
        if (!arnumber && rec.doi) arnumber = elibTok.arnumberFromDoiTail(rec.doi);
        if (!arnumber) { console.log('  [无arnumber] ' + (rec.title || '').slice(0, 30)); continue; }

        const docTarget = `https://ieeexplore.ieee.org/document/${arnumber}`;
        const docUrl = elibTok.proxyUrl(docTarget);
        if (!docUrl) { console.log('  [无elib token] ieeexplore.ieee.org' + ' | ' + (rec.title || '').slice(0, 30)); continue; }

        // 1) 先访问 document 页：过 AWS WAF 挑战 + 建立 wengine 会话
        await page.goto(docUrl, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
        await page.waitForTimeout(5000);

        // 2) 首选：stampPDF/getPDF.jsp 直链（带 Referer 更稳）
        const pdfTarget = elibTok.ieeePdfTarget(arnumber);
        let buf = null;
        const direct = await ctx.request.get(elibTok.proxyUrl(pdfTarget) || pdfTarget,
          { timeout: 120000, headers: { Referer: docUrl, Accept: 'application/pdf,*/*' } }).catch(() => null);
        if (direct) {
          const b = await direct.body();
          if (b.slice(0, 4).toString() === '%PDF') buf = b;
        }
        // 3) 兜底：stamp.jsp → iframe src
        if (!buf) {
          const stampUrl = elibTok.proxyUrl(`https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=${arnumber}`);
          await page.goto(stampUrl, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
          await page.waitForTimeout(5000);
          const frameUrl = await page.$eval('iframe', f => f.src).catch(() => null);
          if (frameUrl) {
            const r2 = await ctx.request.get(frameUrl, { timeout: 120000 }).catch(() => null);
            if (r2) {
              const b2 = await r2.body();
              if (b2.slice(0, 4).toString() === '%PDF') buf = b2;
            }
          }
        }
        if (buf && buf.slice(0, 4).toString() === '%PDF') {
          fs.writeFileSync(path.join(OUTDIR, safe(rec.title) + '.pdf'), buf);
          ok++;
          console.log(`  [下载] ${buf.length} bytes ${(rec.title || '').slice(0, 36)} (arnumber ${arnumber})`);
        } else {
          console.log('  [非PDF] ' + (rec.title || '').slice(0, 30) + ` (arnumber ${arnumber})`);
        }
      } catch (e) {
        console.log('  [失败] ' + (rec.title || '').slice(0, 30) + ' | ' + String(e.message).slice(0, 50));
      }
    }
    console.log(`IEEE完成: ${ok}/${ieee.length}\n`);
    await ctx.close();
  }

  // ── ACM：① 首选 elib 代理（浏览器过 CF，无需 cf_clearance/cookies） ② 兜底 cf_clearance 复用 ──
  if (acm.length) {
    console.log('=== ACM ===');
    let okAcm = 0;
    const acmLeft = [];
    const acmCtx = await browser.newContext(elibCtxOpts({
      userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
      viewport: { width: 1366, height: 768 }, locale: 'en-US', acceptDownloads: true,
    }));
    const acmPage = await acmCtx.newPage();
    for (const rec of acm) {
      try {
        const doi = rec.doi || (rec.link && rec.link.includes('doi.org') ? rec.link.split('doi.org/')[1] : '') || '';
        if (!doi) { console.log('  [无doi] ' + (rec.title || '').slice(0, 30)); continue; }
        const docUrl = elibTok.proxyUrl(`https://dl.acm.org/doi/${doi}`);
        if (!docUrl) { acmLeft.push(rec); continue; }
        await acmPage.goto(docUrl, { waitUntil: 'domcontentloaded', timeout: 90000 }).catch(() => {});
        await acmPage.waitForTimeout(5000);
        // ACM PDF 直链：/doi/pdf/<doi>?download=true
        const pdfUrl = elibTok.proxyUrl(`https://dl.acm.org/doi/pdf/${doi}?download=true`);
        const r = await acmCtx.request.get(pdfUrl, { timeout: 120000, headers: { Referer: docUrl } }).catch(() => null);
        const buf = r ? await r.body() : null;
        if (buf && buf.slice(0, 4).toString() === '%PDF') {
          fs.writeFileSync(path.join(OUTDIR, safe(rec.title) + '.pdf'), buf);
          okAcm++;
          console.log(`  [下载·elib] ${buf.length} bytes ${(rec.title || '').slice(0, 36)}`);
        } else {
          console.log(`  [elib未取到] ${(rec.title || '').slice(0, 30)} status=${r && r.status()}`);
          acmLeft.push(rec);
        }
      } catch (e) {
        console.log('  [elib失败] ' + (rec.title || '').slice(0, 30) + ' | ' + String(e.message).slice(0, 50));
        acmLeft.push(rec);
      }
    }
    await acmCtx.close();
    console.log(`ACM(elib) 完成: ${okAcm}/${acm.length}${acmLeft.length ? `，剩 ${acmLeft.length} 篇转 cf_clearance 兜底` : ''}`);

    if (acmLeft.length) {
    console.log('=== ACM (cf_clearance 兜底复用) ===');
    if (!fs.existsSync(ACM_SS)) {
      console.log(`  [警告] 无 ${ACM_SS}（cf_clearance），且 elib 未取到。先跑 node acm_agent.js <doi> 过一次 CF 导出。`);
      console.log(`  ACM 跳过 ${acmLeft.length} 篇`);
    } else {
      const ctx = await browser.newContext({
        storageState: ACM_SS,
        userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        viewport: { width: 1366, height: 768 },
        locale: 'en-US',
        timezoneId: 'Asia/Shanghai',
        acceptDownloads: true,
      });
      // 隐藏 CF 检测的自动化指纹（否则即使有 cf_clearance 也可能再 challenge）
      await ctx.addInitScript(() => {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = window.chrome || { runtime: {} };
      });
      const page = await ctx.newPage();
      let ok = 0;
      for (const rec of acmLeft) {
        try {
          const doi = rec.doi || (rec.link && rec.link.includes('doi.org') ? rec.link.split('doi.org/')[1] : '') || '';
          if (!doi) { console.log('  [无doi] ' + (rec.title || '').slice(0, 30)); continue; }
          const docUrl = `https://dl.acm.org/doi/${doi}`;
          // 1) 访问论文页（带 cf_clearance，通常直接进；若重新 challenge 则多等）
          await page.goto(docUrl, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
          let isCF = (await page.title().catch(() => '')).includes('Just a moment');
          if (isCF) {
            for (let i = 0; i < 20; i++) {
              await page.waitForTimeout(2000);
              if (!(await page.title().catch(() => '')).includes('Just a moment')) { isCF = false; break; }
            }
          }
          if (isCF) { console.log('  [仍CF] ' + (rec.title || '').slice(0, 30)); continue; }
          await page.waitForTimeout(3000);
          // 2) 找 PDF 链接
          const pdfHref = await page.evaluate(() => {
            const a = [...document.querySelectorAll('a')].find(x => {
              const t = (x.textContent || '').trim();
              const h = x.href || '';
              return (t === 'PDF' || t.includes('Download')) && (h.includes('/pdf/') || h.includes('fulltext'));
            });
            return a ? a.href : '';
          }).catch(() => '');
          if (!pdfHref) { console.log('  [无PDF链接] ' + (rec.title || '').slice(0, 30)); continue; }
          // 3) 页面导航触发下载（download 事件）
          const dlPromise = ctx.waitForEvent('download', { timeout: 30000 }).catch(() => null);
          await page.goto(pdfHref, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
          const dl = await dlPromise;
          if (dl) {
            await dl.saveAs(path.join(OUTDIR, safe(rec.title) + '.pdf'));
            ok++;
            console.log(`  [下载] ${(rec.title || '').slice(0, 36)}`);
          } else {
            // 兜底：页面即 PDF
            await page.waitForTimeout(3000);
            const resp = await page.goto(pdfHref, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => null);
            if (resp && (resp.headers()['content-type'] || '').includes('pdf')) {
              const buf = await resp.body();
              fs.writeFileSync(path.join(OUTDIR, safe(rec.title) + '.pdf'), buf);
              ok++;
              console.log(`  [下载] ${buf.length} bytes ${(rec.title || '').slice(0, 36)}`);
            } else {
              console.log('  [下载失败] ' + (rec.title || '').slice(0, 30));
            }
          }
        } catch (e) {
          console.log('  [失败] ' + (rec.title || '').slice(0, 30) + ' | ' + String(e.message).slice(0, 50));
        }
      }
      console.log(`ACM(cf) 完成: ${ok}/${acmLeft.length}`);
      await ctx.close();
    }
    }  // end if (acmLeft.length)
    console.log(`ACM 合计: ${okAcm + 0} (elib) + 见上 (cf)\n`);
  }

  await browser.close();
})().catch(e => { console.error('异常:', e); process.exit(1); });
