const { chromium } = require("playwright-core");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const OUT = "docs/shots/settings-budget.png";

// 截图：设置面板的「用量与预算」区块。
// 会先登录（写接口需 JWT），再打开设置并滚动到用量区。
(async () => {
  const browser = await chromium.launch({
    executablePath: EDGE, headless: true,
    args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage({ viewport: { width: 1180, height: 1000 } });
  const errors = [];
  page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", e => errors.push("PAGEERROR: " + e.message));

  await page.goto("http://127.0.0.1:8000", { waitUntil: "load", timeout: 30000 });
  await page.waitForTimeout(500);

  // 注册一个临时账号，拿到 JWT 以便使用写接口（预算保存/用量重置）
  const user = "shot_" + Date.now();
  const reg = await page.evaluate(async (u) => {
    const r = await fetch("/auth/register", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: "Shot123456" }),
    });
    const d = await r.json();
    if (d.token) localStorage.setItem("teleops_token", d.token);
    return { ok: r.ok, hasToken: !!d.token };
  }, user);
  console.log("login:", JSON.stringify(reg));
  await page.reload({ waitUntil: "load" });
  await page.waitForTimeout(500);

  await page.click("#openSettings");
  await page.waitForTimeout(1200);   // 等用量接口返回

  // 点一下「查询余额」，让界面显示真实余额
  const balBtn = await page.$("#setBalanceQuery");
  if (balBtn) {
    await balBtn.click();
    await page.waitForTimeout(2500);
  }

  // 滚动到用量区块并单独截图该区域
  const box = await page.$(".usage-box");
  if (box) {
    await box.scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
    const row = await page.evaluateHandle(
      () => document.querySelector(".usage-box").parentElement);
    await row.asElement().screenshot({ path: OUT });
    console.log("saved", OUT);
  } else {
    await page.screenshot({ path: OUT, fullPage: true });
    console.log("saved (full page fallback)", OUT);
  }

  const usage = await page.evaluate(() => ({
    calls: document.querySelector("#usageCalls")?.textContent,
    tokens: document.querySelector("#usageTokens")?.textContent,
    cost: document.querySelector("#usageCost")?.textContent,
    total: document.querySelector("#usageTotalCost")?.textContent,
    bar: document.querySelector("#usageBarText")?.textContent,
    balance: document.querySelector("#balanceText")?.textContent,
    alert: document.querySelector("#usageAlert")?.textContent,
  }));
  console.log("usage panel:", JSON.stringify(usage, null, 2));

  if (errors.length) { console.log("JS ERRORS:"); errors.slice(0, 6).forEach(e => console.log(" -", e)); }
  else console.log("no JS errors");
  await browser.close();
})();
