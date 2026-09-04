// 临时脚本：截取「实时告警流」面板（流水线已在后端运行）
const { chromium } = require("playwright-core");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const OUT = "docs/shots/stream.png";

(async () => {
  const browser = await chromium.launch({
    executablePath: EDGE, headless: true,
    args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage({ viewport: { width: 1500, height: 940 } });
  const errors = [];
  page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", e => errors.push("PAGEERROR: " + e.message));
  await page.goto("http://127.0.0.1:8000", { waitUntil: "load", timeout: 30000 });
  await page.waitForTimeout(1800);
  await page.click('[data-view="stream"]', { timeout: 8000 });
  await page.waitForTimeout(4500);          // 等两轮增量 feed 渲染
  await page.screenshot({ path: OUT, fullPage: false });
  console.log("saved", OUT);
  if (errors.length) { console.log("JS ERRORS:"); errors.slice(0, 6).forEach(e => console.log(" -", e)); }
  await browser.close();
})();
