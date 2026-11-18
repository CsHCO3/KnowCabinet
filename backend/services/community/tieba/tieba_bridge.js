#!/usr/bin/env node
/**
 * 贴吧浏览器桥接 — 用 Edge (CDP) 绕过 WAF 加载真实 HTML
 *
 * 模式:
 *   html <URL>          返回完整页面 HTML (默认)
 *   json <URL> <type>   返回结构化 JSON 数据
 *                        type = forum | search | thread
 *
 * 环境变量: BDUSS, BAIDUID, STOKEN
 */

const { launch } = require("puppeteer-core");
const fs = require("fs");
const path = require("path");

const EDGE_PATHS = [
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
];

function findEdge() {
  for (const p of EDGE_PATHS) {
    if (fs.existsSync(p)) return p;
  }
  const paths = process.env.PATH.split(path.delimiter);
  for (const dir of paths) {
    try {
      for (const e of fs.readdirSync(dir)) {
        if (e.toLowerCase() === "msedge.exe" || e.toLowerCase() === "edge.exe") {
          return path.join(dir, e);
        }
      }
    } catch {}
  }
  return null;
}

async function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// ── 滚动页面收集帖子列表 ──────────────────────────────────
async function scrapeThreadList(page) {
  // 滚动到底部触发虚拟列表渲染
  const ITEMS_PER_PAGE = 50;
  for (let i = 0; i < 5; i++) {
    await page.evaluate(() => {
      window.scrollBy(0, 3000);
    });
    await sleep(800);
  }
  // 滚回顶部
  await page.evaluate(() => window.scrollTo(0, 0));
  await sleep(500);

  // 收集数据
  const data = await page.evaluate(() => {
    const threads = [];
    const seen = new Set();

    // 获取所有 thread-title
    document.querySelectorAll(".thread-title, .top-thread-card .thread-title").forEach((el) => {
      const title = el.textContent.trim();
      if (!title || title.length < 2) return;

      // 找 TID
      let parent = el.parentElement;
      let tid = "";
      for (let i = 0; i < 6 && parent; i++) {
        const a = parent.querySelector("a[href*='/p/']");
        if (a) {
          const m = a.href.match(/\/p\/(\d+)/);
          if (m) tid = m[1];
          break;
        }
        parent = parent.parentElement;
      }
      if (!tid || seen.has(tid)) return;
      seen.add(tid);
      threads.push({ tid, title });
    });

    // 如果上面没找到，从 a.thread-content-link 提取
    if (threads.length === 0) {
      document.querySelectorAll("a.thread-content-link, a[href*='/p/']").forEach((a) => {
        const href = a.href || "";
        const m = href.match(/\/p\/(\d+)/);
        if (!m) return;
        const tid = m[1];
        if (seen.has(tid)) return;
        seen.add(tid);
        const titleEl = a.querySelector(".thread-title");
        const title = titleEl ? titleEl.textContent.trim() : a.textContent.trim();
        threads.push({ tid, title: title.substring(0, 200) });
      });
    }

    return threads;
  });

  return data;
}

// ── 搜索页面抓取 ─────────────────────────────────────────
async function scrapeSearchResults(page) {
  await sleep(2000);

  const data = await page.evaluate(() => {
    const results = [];
    // 从页面文本提取结构化数据
    const text = document.body.innerText || "";
    const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);

    // 找 "xxx吧" + "作者 发布于 日期" 模式
    const entries = [];
    let i = 0;
    while (i < lines.length) {
      if (lines[i].endsWith("吧") && i + 1 < lines.length && lines[i + 1].includes("发布于")) {
        const forum = lines[i];
        const authorLine = lines[i + 1];
        const author = authorLine.replace(/发布于.*/, "").trim();
        const date = authorLine.includes("发布于")
          ? authorLine.substring(authorLine.indexOf("发布于") + 3).trim()
          : "";

        let title = "";
        let brief = "";
        let j = i + 2;
        // 标题（不长于 80 且不含 "发布于" 的行）
        if (
          j < lines.length &&
          !lines[j].includes("发布于") &&
          !lines[j].endsWith("吧") &&
          lines[j].length > 3 &&
          lines[j].length < 80
        ) {
          title = lines[j];
          j++;
        }
        // 收集 brief（直到遇见 "分享" 或纯数字或下个吧名）
        const briefLines = [];
        while (
          j < lines.length &&
          !lines[j].endsWith("吧") &&
          !lines[j].includes("关注") &&
          !lines[j].includes("贴子") &&
          lines[j] !== "分享" &&
          lines[j] !== "赞" &&
          lines[j] !== "评论" &&
          !/^\d+$/.test(lines[j])
        ) {
          briefLines.push(lines[j]);
          j++;
        }
        brief = briefLines.join(" ").substring(0, 300);
        entries.push({
          forum,
          author,
          date,
          title,
          brief,
        });
        i = j;
      } else {
        i++;
      }
    }
    return entries;
  });

  return data;
}

// ── 帖子页面抓取 ─────────────────────────────────────────
async function scrapeThreadPage(page) {
  // 等待内容渲染（最长 15 秒）
  for (let attempt = 0; attempt < 5; attempt++) {
    const title = await page.title();
    if (
      title.includes("安全验证") ||
      title.includes("百度安全") ||
      title === "百度贴吧" ||
      title === "百度"
    ) {
      console.error(`[BRIDGE] 遇到验证页 (${title})，等待重试...`);
      await sleep(3000);
      // 重新加载
      await page.reload({ waitUntil: "networkidle2", timeout: 30000 });
      await sleep(3000);
      continue;
    }
    // 检查是否有内容
    const hasContent = await page.evaluate(() => {
      return !!(
        document.querySelector(
          ".pb-content-wrap, .d_post_content, #j_p_post_content, .content-container"
        )
      );
    });
    if (hasContent) {
      const html = await page.content();
      return html;
    }
    console.error(`[BRIDGE] 内容尚未加载，等待...`);
    await sleep(2000);
  }

  // 最终尝试：直接返回当前页面的 HTML
  console.error(`[BRIDGE] 最终返回页面 HTML`);
  return await page.content();
}

// ── 主函数 ───────────────────────────────────────────────
async function main() {
  const mode = process.argv[2] || "html";
  const url = process.argv[3];
  const pageType = process.argv[4] || "forum";

  if (!url && mode !== "html") {
    console.error("用法: node tieba_bridge.js html <URL>");
    console.error("       node tieba_bridge.js json <URL> forum|search|thread");
    process.exit(1);
  }

  const edgePath = findEdge();
  if (!edgePath) {
    console.error("[ERR] 找不到 Edge 浏览器");
    process.exit(1);
  }

  const bduss = process.env.BDUSS || "";
  const baiduid = process.env.BAIDUID || "";

  const browser = await launch({
    executablePath: edgePath,
    headless: "new",
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
    ],
  });

  try {
    const page = await browser.newPage();
    // 大视口确保虚拟列表渲染更多条目
    await page.setViewport({ width: 1920, height: 5000 });
    await page.setUserAgent(
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
        "AppleWebKit/537.36 (KHTML, like Gecko) " +
        "Chrome/125.0.0.0 Safari/537.36"
    );

    // Set cookies
    if (bduss) {
      await page.setCookie({ name: "BDUSS", value: bduss, domain: ".baidu.com" });
    }
    if (baiduid) {
      await page.setCookie({ name: "BAIDUID", value: baiduid, domain: ".baidu.com" });
    }

    console.error(`[BRIDGE] 加载: ${url}`);
    await page.goto(url, { waitUntil: "networkidle2", timeout: 30000 });
    await sleep(2000);

    if (mode === "json") {
      let data;
      if (pageType === "search") {
        data = await scrapeSearchResults(page);
      } else {
        data = await scrapeThreadList(page);
      }
      console.log(JSON.stringify(data, null, 2));
      console.error(`[BRIDGE] 成功, 获取 ${data.length} 条`);
    } else if (pageType === "thread" || url.includes("/p/")) {
      // 帖子页面 — 带验证页重试
      const html = await scrapeThreadPage(page);
      console.log(html);
      console.error(`[BRIDGE] 成功, HTML ${html.length} 字节`);
    } else {
      // HTML 模式
      const html = await page.content();
      console.log(html);
      console.error(`[BRIDGE] 成功, HTML ${html.length} 字节`);
    }
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error("[BRIDGE 错误]", err.message);
  process.exit(1);
});
