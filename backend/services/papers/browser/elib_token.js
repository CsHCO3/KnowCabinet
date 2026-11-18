/** 机构图书馆适配。部署参数由授权环境提供。 */
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');


// 共享 token 缓存（论文检索域下的公共资产）
const CACHE_PATHS = [
  path.join(__dirname, '..', '..', '..', '..', '文档以及其它代码', '(5)搜集信息源', '论文检索', 'elib-proxy', 'host_tokens.json'),
  path.join(__dirname, '..', '..', '..', '..', '文档以及其它代码', '论文检索', 'elib-proxy', 'host_tokens.json'),
  path.join(__dirname, 'host_tokens.json'),
];

let _cache = null;

/** 本地伪造：AES-128-CFB(key=iv=KEY) 加密主机名 + 魔数前缀（任意长度均正确） */
const { requireEnv } = require('./deployment');

function forgeToken(host) {
  const keyHex = requireEnv('ELIB_KEY_HEX');
  if (!/^[a-f0-9]{32}$/i.test(keyHex)) {
    throw new Error('ELIB_KEY_HEX 应为 16 字节的十六进制值');
  }
  const key = Buffer.from(keyHex, 'hex');
  const c = crypto.createCipheriv('aes-128-cfb', key, key);
  return Buffer.concat([key, c.update(Buffer.from(host, 'utf8')), c.final()]).toString('hex');
}

function loadCache() {
  if (_cache) return _cache;
  for (const p of CACHE_PATHS) {
    try {
      _cache = JSON.parse(fs.readFileSync(p, 'utf8'));
      return _cache;
    } catch (e) { /* 尝试下一个 */ }
  }
  _cache = {};
  return _cache;
}

/** host → token；返回 null 表示需要浏览器采集 */
function tokenFor(host) {
  const h = String(host || '').trim().replace(/^https?:\/\//i, '').split('/')[0];
  if (!h) return null;
  const cached = loadCache()[h];
  if (cached) return cached;
  return forgeToken(h);
}

/** 目标 URL → elib 代理 URL；token 未知返回 null */
function proxyUrl(target) {
  let u;
  try { u = new URL(target); } catch (e) { return null; }
  const token = tokenFor(u.host);
  if (!token) return null;
  const scheme = u.protocol.replace(':', '');
  const origin = requireEnv('ELIB_PROXY_ORIGIN').replace(/\/+$/, '');
  return `${origin}/${scheme}/${token}${u.pathname}${u.search}${u.hash}`;
}

/**
 * IEEE DOI → arnumber（document number）。
 * 重要（2026-09-17 实测）：**IEEE 现行 DOI 的尾段不是 arnumber**——
 *   10.1109/jiot.2022.3160737 → 真 arnumber 9738800（不是 3160737）。
 * 因此按可靠性排序：
 *   ① Crossref 元数据（最稳、最快、直连）：IEEE 通过 Crossref 注册时会带 document id
 *      （alternate-id 形如 ieee:doc/9738800），item URL 亦为 ieeexplore.ieee.org/document/<n>
 *   ② doi.org 解析（需浏览器，走 elib 代理）→ 看最终 URL 的 /document/<n>
 *   ③ 尾段直取（**仅老式 DOI 成立**，如 10.1109/5.771073 → 771073）——最后兜底
 */
function arnumberFromDoi(doi) {
  if (!doi) return '';
  const m = /^10\.1109\/[^/]*?\.\d{4}\.(\d{3,})$/i.exec(doi.trim());
  return m ? m[1] : '';
}

/** ① Crossref 查 IEEE document number（纯 HTTP，直连，~1s；失败返回 ''） */
async function arnumberFromCrossref(doi, timeoutMs = 12000) {
  if (!doi) return '';
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    const r = await fetch(`https://api.crossref.org/works/${encodeURIComponent(doi.trim())}`, {
      headers: { 'User-Agent': 'infosystem-papers/1.0 (mailto:noreply@example.com)' },
      signal: ctrl.signal,
    });
    clearTimeout(t);
    if (!r.ok) return '';
    const j = await r.json();
    const msg = (j && j.message) || {};
    // (a) resource.primary.URL: https://ieeexplore.ieee.org/document/9738800/
    const m1 = /ieeexplore\.ieee\.org\/document\/(\d+)/.exec(String((msg.resource && msg.resource.primary && msg.resource.primary.URL) || ''));
    if (m1) return m1[1];
    // (b) link[].URL: http://xplorestaging.ieee.org/ielx7/.../09738800.pdf?arnumber=9738800
    for (const l of msg.link || []) {
      const m = /[?&]arnumber=(\d+)/.exec(String(l.URL || ''));
      if (m) return m[1];
      const m2 = /ieeexplore\.ieee\.org\/document\/(\d+)/.exec(String(l.URL || ''));
      if (m2) return m2[1];
    }
    // (c) alternate-id: ["ieee:doc/9738800", ...]
    for (const alt of msg['alternate-id'] || []) {
      const m = /doc\/(\d+)/.exec(String(alt));
      if (m) return m[1];
    }
    // (d) URL 字段（有时直接是 document 页）
    const m2 = /ieeexplore\.ieee\.org\/document\/(\d+)/.exec(String(msg.URL || ''));
    if (m2) return m2[1];
    // (e) article-number
    const m3 = /^(\d{6,8})$/.exec(String(msg['article-number'] || ''));
    if (m3) return m3[1];
  } catch (e) { /* 静默失败，交给下一级 */ }
  return '';
}

/** ③ 老式 DOI 尾段直取（仅当 ① ② 都失败时兜底） */
function arnumberFromDoiTail(doi) {
  if (!doi) return '';
  const t = doi.trim();
  if (!/^10\.1109\//i.test(t)) return '';
  const tail = (t.split('.').pop() || '').trim();
  return /^\d{5,8}$/.test(tail) ? tail : '';
}

/** IEEE 全文 PDF 直链（实测该接口返回 application/pdf） */
function ieeePdfTarget(arnumber) {
  return `https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber=${arnumber}&ref=`;
}

module.exports = {
  tokenFor,
  proxyUrl,
  forgeToken,
  arnumberFromDoi,
  arnumberFromCrossref,
  arnumberFromDoiTail,
  ieeePdfTarget,
  loadCache,
};
