const { chromium } = require("playwright-core");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const OUT = "docs/shots/settings-pricing.png";

// 截图：设置面板「用量与预算」区块的自定义单价行（v0.8.6）。
// 流程：登录 → 设置单价（让来源显示"自定义"）→ 打开设置 → 截图。
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

  const user = "price_" + Date.now();
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

  // 保存一个自定义单价，让来源提示显示「✏️ 自定义单价」
  await page.evaluate(async () => {
    const token = localStorage.getItem("teleops_token");
    await fetch("/llm/config", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
      body: JSON.stringify({ provider: "deepseek", pricing: { "deepseek.deepseek-chat": [2.0, 0.5, 8.0] } }),
    });
  });

  await page.reload({ waitUntil: "load" });
  await page.waitForTimeout(500);
  await page.click("#openSettings");
  await page.waitForTimeout(1500);   // 等用量接口返回

  const info = await page.evaluate(() => ({
    priceText: document.querySelector("#priceSourceText")?.textContent,
    in: document.querySelector("#setPriceIn")?.value,
    cached: document.querySelector("#setPriceCached")?.value,
    out: document.querySelector("#setPriceOut")?.value,
  }));
  console.log("pricing panel:", JSON.stringify(info, null, 2));

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
  }

  // 清理：清掉刚才保存的自定义单价，恢复内置计价
  await page.evaluate(async () => {
    const token = localStorage.getItem("teleops_token");
    await fetch("/llm/config", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
      body: JSON.stringify({ provider: "deepseek", pricing: {} }),
    });
  });

  if (errors.length) { console.log("JS ERRORS:"); errors.slice(0, 6).forEach(e => console.log(" -", e)); }
  else console.log("no JS errors");
  await browser.close();
})();
