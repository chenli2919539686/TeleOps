// 临时脚本：截取 Agent 作战室（含实时任务队列）
const { chromium } = require("playwright-core");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const OUT = "docs/shots/war-tasks.png";

(async () => {
  const browser = await chromium.launch({
    executablePath: EDGE, headless: true,
    args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage({ viewport: { width: 1560, height: 1180 } });
  const errors = [];
  page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", e => errors.push("PAGEERROR: " + e.message));
  await page.goto("http://127.0.0.1:8001", { waitUntil: "load", timeout: 30000 });
  await page.waitForTimeout(6000);   // 等 renderStreamTasks 轮询到任务数据
  await page.screenshot({ path: OUT, fullPage: true });
  console.log("saved", OUT);
  if (errors.length) { console.log("JS ERRORS:"); errors.slice(0, 6).forEach(e => console.log(" -", e)); }
  else console.log("no JS errors");
  await browser.close();
})();
