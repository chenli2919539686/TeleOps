// 用系统 Edge 驱动 playwright-core 截取 TeleOps 前端 7 个视图
const { chromium } = require("playwright-core");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:8000";
const OUT = "docs/shots";
const VIEWS = ["overview", "loop", "board", "integ", "topo", "tools", "kb"];

(async () => {
  const browser = await chromium.launch({
    executablePath: EDGE,
    headless: true,
    args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("PAGEERROR: " + e.message));

  await page.goto(BASE, { waitUntil: "load", timeout: 30000 });
  // 等待前端拉取数据（/agents /workspaces /alerts 等）
  await page.waitForTimeout(2500);

  for (const v of VIEWS) {
    try {
      await page.click(`[data-view="${v}"]`, { timeout: 8000 });
    } catch (e) {
      errors.push(`click ${v} failed: ${e.message}`);
    }
    await page.waitForTimeout(1500);
    const file = `${OUT}/${v}.png`;
    await page.screenshot({ path: file, fullPage: false });
    console.log("saved", file);
  }

  // 额外：overview 里点开工作台抽屉（右侧），展示 JSON 分析框
  await page.click(`[data-view="overview"]`).catch(() => {});
  await page.waitForTimeout(800);
  const drawerBtn = await page.$("#toggleDrawer");
  if (drawerBtn) {
    await drawerBtn.click().catch(() => {});
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `${OUT}/board_drawer.png`, fullPage: false });
    console.log("saved", `${OUT}/board_drawer.png`);
  }
  await drawerBtn?.click().catch(() => {}); // 关闭消息栏
  await page.waitForTimeout(500);

  // 额外：点击第一个运维 Agent 卡片，打开真实工作台（含告警样本库）
  const firstOps = await page.$("#warOps .war-card");
  if (firstOps) {
    await firstOps.click().catch((e) => errors.push("click ops card: " + e.message));
    await page.waitForTimeout(1800); // 等告警样本库 /alerts 加载
    await page.screenshot({ path: `${OUT}/workbench.png`, fullPage: false });
    console.log("saved", `${OUT}/workbench.png`);
  }

  console.log("CONSOLE_ERRORS:", JSON.stringify(errors.slice(0, 10)));
  await browser.close();
})().catch((e) => { console.error("FATAL", e); process.exit(1); });
