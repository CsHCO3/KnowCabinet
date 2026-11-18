/**
 * 论文站点登录态/下载残留的目录 —— JS 侧唯一定义（与 Python 的
 * backend/services/papers/session_paths.py 保持同一套布局）。
 *
 * 这些文件是本机运行时数据（cookie / storage_state / 下载的 pdf），统一放
 * <项目根>/data/paper_sessions/<站点>/，data/ 已被 .gitignore 整目录排除。
 * 不要写回包目录 papers/<站点>_session/：2026-10-27 就是因为写在包目录，
 * cookie 跟着代码一起被提交到了 GitHub。
 */
const path = require('path');
const fs = require('fs');

const SESSION_ROOT = path.join(__dirname, '..', '..', '..', '..', 'data', 'paper_sessions');

/** 迁移期的老位置（只读兼容） */
const LEGACY = {
  acm: path.join(__dirname, 'acm_session'),
  cnki: path.join(__dirname, '..', 'cnki_session'),
  wanfang: path.join(__dirname, '..', 'wanfang_session'),
};

function siteDir(site) {
  return path.join(SESSION_ROOT, site);
}

/** <站点>/storage_state.json；新位置没有、老位置有时返回老位置 */
function storageState(site) {
  const p = path.join(siteDir(site), 'storage_state.json');
  if (fs.existsSync(p)) return p;
  const legacy = LEGACY[site] ? path.join(LEGACY[site], 'storage_state.json') : null;
  if (legacy && fs.existsSync(legacy)) return legacy;
  return p; // 都不存在时返回新位置，报错信息才是"该放哪儿"
}

function downloadsDir(site) {
  const d = path.join(siteDir(site), 'downloads');
  fs.mkdirSync(d, { recursive: true });
  return d;
}

module.exports = { SESSION_ROOT, siteDir, storageState, downloadsDir };
