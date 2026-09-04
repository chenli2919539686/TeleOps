const { chromium } = require("playwright-core");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const OUT = "docs/shots/settings-budget-over.png";

// 截图：故意把预算设到 ¥0.01（已知已花 ¥0.05 必然超限），
// 让「今日用量已达上限」告警条 + 进度条变红色，展示熔断效果。
(async () => {
  const browser = await chromium.launch({
    executablePath: EDGE, headless: true,
    args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage({ viewport: { width: 1180, height: 1000 } });
  await page.goto("http://127.0.0.1:8000", { waitUntil: "load", timeout: 30000 });
  await page.waitForTimeout(500);

  // 注册临时账号拿 JWT
  const user = "over_" + Date.now();
  await page.evaluate(async (u) => {
    const r = await fetch("/auth/register", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: "Over123456" }),
    });
    const d = await r.json();
    if (d.token) localStorage.setItem("teleops_token", d.token);
  }, user);
  await page.reload({ waitUntil: "load" });
  await page.waitForTimeout(500);

  // 直接把预算压到 ¥0.01（模拟用户怕花超，刻意设很紧的预算）
  await page.evaluate(async () => {
    const token = localStorage.getItem("teleops_token");
    await fetch("/llm/config", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
      body: JSON.stringify({ provider: "deepseek", budget_daily_cny: 0.01, budget_action: "fallback" }),
    });
  });

  await page.click("#openSettings");
  await page.waitForTimeout(1500);

  const box = await page.$(".usage-box");
  await box.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  const row = await page.evaluateHandle(
    () => document.querySelector(".usage-box").parentElement);
  await row.asElement().screenshot({ path: OUT });
  console.log("saved", OUT);

  const usage = await page.evaluate(() => ({
    calls: document.querySelector("#usageCalls")?.textContent,
    bar: document.querySelector("#usageBarText")?.textContent,
    barClass: document.querySelector("#usageBar")?.className,
    alert: document.querySelector("#usageAlert")?.textContent,
    alertClass: document.querySelector("#usageAlert")?.className,
  }));
  console.log("over state:", JSON.stringify(usage, null, 2));
  await browser.close();
})();
