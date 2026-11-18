// 便捷导出知网/万方登录 cookie（storage_state.json）— cookie 过期/失效时刷新
//
// 用法:
//   node export_cookies.js cnki|wanfang [--output 自定义路径]
//   1) 打开浏览器登录页（学校认证/机构登录），你在浏览器里手动完成登录
//   2) 点页面右上角绿色悬浮按钮「完成登录并导出 cookie」→ 保存到
//      data/paper_sessions/{cnki,wanfang}/storage_state.json（或回终端按回车导出）
//   3) 保存用 context.storageState()（含 httpOnly）；必须 headed（headless 下知网可能 403）
//
// 可选：环境变量 COOKIE_TIMEOUT 秒数（默认 600，超时自动退出）

const { chromium } = require("playwright");
const readline = require("readline");
const path = require("path");
const fs = require("fs");
const sp = require("./paths");

// 登录态写 data/paper_sessions/<站点>/storage_state.json（本机数据，不进仓库）
const SITES = {
  cnki: {
    url: "https://kns.cnki.net/",
    out: path.join(sp.siteDir("cnki"), "storage_state.json"),
    label: "知网 CNKI",
  },
  wanfang: {
    url: "https://www.wanfangdata.com.cn/",
    out: path.join(sp.siteDir("wanfang"), "storage_state.json"),
    label: "万方",
  },
};

(async () => {
  const args = process.argv.slice(2);
  const target = (args[0] || "cnki").toLowerCase();
  const site = SITES[target];
  if (!site) {
    console.error("用法: node export_cookies.js cnki|wanfang");
    process.exit(1);
  }
  const outArg = args.indexOf("--output");
  if (outArg !== -1 && args[outArg + 1]) site.out = args[outArg + 1];

  const save = async () => {
    fs.mkdirSync(path.dirname(site.out), { recursive: true });
    await ctx.storageState({ path: site.out });
    console.log(`✅ 已保存 cookie: ${site.out}`);
    await browser.close();
    process.exit(0);
  };

  console.log(`[${site.label}] 打开 ${site.url}`);
  const browser = await chromium.launch({ headless: false });
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.goto(site.url, { waitUntil: "domcontentloaded" });

  // 页面上注入悬浮按钮（点击即导出，无需回终端）
  await page.exposeFunction("__saveCookie", save);
  await page.addInitScript(() => {
    const mk = () => {
      if (document.getElementById("export-cookie-btn")) return;
      const btn = document.createElement("div");
      btn.id = "export-cookie-btn";
      btn.textContent = "✅ 完成登录并导出 cookie";
      btn.style.cssText =
        "position:fixed;right:20px;top:20px;z-index:2147483647;background:#22c55e;color:#fff;" +
        "padding:10px 16px;border-radius:8px;font:14px/1.4 sans-serif;cursor:pointer;" +
        "box-shadow:0 2px 10px rgba(0,0,0,.35);user-select:none;";
      btn.onclick = () => window.__saveCookie && window.__saveCookie();
      document.body.appendChild(btn);
    };
    mk();
    setInterval(mk, 1500); // 页内跳转后重新注入
  });

  console.log("请在浏览器里完成学校认证/机构登录，然后点右上角绿色按钮「完成登录并导出 cookie」。");
  console.log("（或回终端按回车导出；Ctrl+C 取消）");

  // 终端回车也触发导出（兜底）
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  rl.on("line", save);
  rl.on("SIGINT", async () => { await browser.close(); process.exit(0); });

  // 总超时兜底
  const timeoutSec = parseInt(process.env.COOKIE_TIMEOUT || "600", 10);
  setTimeout(async () => { console.log("⏰ 超时退出（未导出）"); await browser.close(); process.exit(1); }, timeoutSec * 1000);
})();
