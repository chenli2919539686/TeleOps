// TeleOps 前端 · OpenClaw 风格动态界面
// 同源部署（HF Spaces / Caddy 单端口 / 任意域名）一律用相对路径；
// 仅本地「双服务」开发模式（前端静态服务跑在 8001）时才直连 8000
const DEV_FRONTEND = location.port === "8001";
const API = DEV_FRONTEND ? "http://localhost:8000" : "";
let TOKEN = (localStorage.getItem("teleops_token") || "").trim();   // 共享 API Token（服务端设置 TELEOPS_API_TOKEN 时用）
let JWT = (localStorage.getItem("teleops_jwt") || "").trim();       // 每用户 JWT（登录获取，优先使用）
let USER = null;                                                    // 当前登录用户 {username, is_admin}
function setToken(t) { TOKEN = (t || "").trim(); }
function setJwt(t, user) {
  JWT = (t || "").trim();
  if (JWT) localStorage.setItem("teleops_jwt", JWT);
  else localStorage.removeItem("teleops_jwt");
  USER = user || null;
  renderAuthArea();
}
let _suppressLoginPrompt = false;   // 登录/注册请求自身 401 时不弹窗

// 统一请求封装：优先携带每用户 JWT，其次共享 Token；写操作 401 时弹出登录框
function apiFetch(url, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  const cred = JWT || TOKEN;
  if (cred) headers["Authorization"] = "Bearer " + cred;
  const full = API ? API + url : url;
  const p = fetch(full, Object.assign({}, opts, { headers }));
  if (!_suppressLoginPrompt) {
    p.then((r) => { if (r.status === 401) openLogin("登录已过期或未登录，请重新登录"); }).catch(() => {});
  }
  return p;
}

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

marked.setOptions({ breaks: true, gfm: true });
function md(text) {
  try { return DOMPurify.sanitize(marked.parse(text || "")); }
  catch (e) { return `<pre>${text}</pre>`; }
}

// ---------- 连接状态 ----------
async function checkHealth() {
  const dot = $("#connDot"), txt = $("#connText"), badge = $("#modeBadge");
  try {
    const r = await apiFetch(`/health`);
    const d = await r.json();
    dot.className = "dot on";
    txt.textContent = "已连接";
    badge.textContent = "LLM: " + (d.llm_mode === "live" ? "真实模型" : "Mock 兜底");
    badge.style.color = d.llm_mode === "live" ? "var(--ok)" : "var(--warn)";
  } catch (e) {
    dot.className = "dot off";
    txt.textContent = "后端未启动 (需 uvicorn :8000)";
    badge.textContent = "离线";
  }
}

// ---------- 视图切换 ----------
$$(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    const v = btn.dataset.view;
    $$(".nav-item").forEach((b) => b.classList.toggle("active", b === btn));
    $$(".view").forEach((s) => s.classList.toggle("active", s.id === "view-" + v));
    if (v === "topo") renderTopo();
    if (v === "tools") renderTools();
    if (v === "board") renderBoard();
    if (v === "integ") renderIntegrations();
    if (v === "overview") loadOverview();
    if (v === "stream") streamEnter();
  });
});

$("#themeBtn").addEventListener("click", () => {
  const el = document.documentElement;
  el.dataset.theme = el.dataset.theme === "dark" ? "light" : "dark";
});

// ---------- 流式打字渲染 ----------
function streamInto(el, text, speed = 12) {
  return new Promise((resolve) => {
    let i = 0;
    const tick = () => {
      i += Math.max(1, Math.round(speed * 0.6));
      if (i >= text.length) { el.innerHTML = md(text); return resolve(); }
      // 每若干字符重渲染一次 markdown（短文本可接受细微闪烁）
      el.innerHTML = md(text.slice(0, i));
      setTimeout(tick, speed);
    };
    tick();
  });
}

function addMsg(container, role, contentHTML, avatar) {
  const empty = container.querySelector(":scope > .empty");
  if (empty) empty.remove();
  const m = document.createElement("div");
  m.className = "msg " + role;
  m.innerHTML = `<div class="avatar">${avatar}</div><div class="bubble">${contentHTML}</div>`;
  container.appendChild(m);
  container.scrollTop = container.scrollHeight;
  return m.querySelector(".bubble");
}

function thinking(container, avatar) {
  const m = document.createElement("div");
  m.className = "msg bot";
  m.innerHTML = `<div class="avatar">${avatar}</div><div class="bubble"><div class="thinking"><span></span><span></span><span></span></div></div>`;
  container.appendChild(m);
  container.scrollTop = container.scrollHeight;
  return m.querySelector(".bubble");
}

// ---------- 工作台公用预设 / 渲染 ----------
const PRESETS = [
  { label: "🌡️ 温度过热 host-1", alert: { alert_id: "A-TEMP", metric: "temperature", host: "host-1", severity: "critical", value: "88C", message: "物理机 host-1 核心温度过热告警，疑似散热故障", tags: ["compute", "temperature"], is_noise: false } },
  { label: "💡 ONU 光弱 onu-1", alert: { alert_id: "A-ONU", metric: "optical_power", host: "onu-1", severity: "major", value: "-28dBm", message: "ONU 光模块接收光功率低于阈值，疑似光路劣化", tags: ["access", "optical"], is_noise: false } },
  { label: "🔕 噪声告警", alert: { alert_id: "A-NOISE", metric: "cpu", host: "host-2", severity: "info", value: "62%", message: "CPU 短时抖动", tags: ["compute"], is_noise: true } },
  { label: "📦 端口错包 switch-3", alert: { alert_id: "A-PORT", metric: "ifInErrors", host: "switch-3", severity: "major", value: "1200", message: "上联端口入向错包激增，疑似光模块或链路问题", tags: ["switch"], is_noise: false } },
];

// ---- 真实告警样本库（data/alerts.json · 55 条 BlueGene/L 机群事件） ----
const LIB_FILTERS = [["all", "全部"], ["critical", "🚨 严重"], ["noise", "🔕 噪声"]];
let WB_ALERT_ALL = null;        // GET /alerts 全量缓存（首次拉取后复用）
let WB_ALERT_FILTER = "all";    // all | critical | noise

function wbLibChips() {
  return LIB_FILTERS.map(f => `<span class="chip${WB_ALERT_FILTER === f[0] ? " on" : ""}" data-f="${f[0]}">${f[1]}</span>`).join("");
}
async function loadWbAlertLib() {
  if (!document.getElementById("wbLibBox")) return;
  if (!WB_ALERT_ALL) {
    try {
      const r = await fetch("/alerts");
      const d = await r.json();
      WB_ALERT_ALL = d.alerts || [];
    } catch (e) { WB_ALERT_ALL = []; }
  }
  renderWbAlertLib();
}
function renderWbAlertLib() {
  const list = document.getElementById("wbLibList");
  if (!list) return;
  const q = (document.getElementById("wbLibQ")?.value || "").trim().toLowerCase();
  let arr = WB_ALERT_ALL || [];
  if (WB_ALERT_FILTER === "critical") arr = arr.filter(a => a.severity === "critical");
  else if (WB_ALERT_FILTER === "noise") arr = arr.filter(a => a.is_noise);
  if (q) arr = arr.filter(a => (a.alert_id + " " + a.metric + " " + a.host + " " + a.message).toLowerCase().includes(q));
  const cnt = document.getElementById("wbLibCount");
  if (cnt) cnt.textContent = arr.length + " / " + (WB_ALERT_ALL || []).length;
  if (!arr.length) { list.innerHTML = '<div class="empty">无匹配样本</div>'; return; }
  list.innerHTML = arr.slice(0, 60).map(a => {
    const cls = a.severity === "critical" ? "lib-crit" : (a.is_noise ? "lib-noise" : "lib-info");
    const tag = a.is_noise ? "🔕" : (a.severity === "critical" ? "🚨" : "ℹ️");
    return `<button class="lib-item ${cls}" data-aid="${escapeHtml(a.alert_id)}" title="点击填入分析框">` +
      `<span class="lib-id">${escapeHtml(a.alert_id)}</span>` +
      `<span class="lib-tag">${tag} ${escapeHtml(a.metric)}</span>` +
      `<span class="lib-msg">${escapeHtml(String(a.message || "").slice(0, 56))}</span></button>`;
  }).join("");
  list.querySelectorAll(".lib-item").forEach(b => {
    b.onclick = () => {
      const al = (WB_ALERT_ALL || []).find(x => x.alert_id === b.dataset.aid);
      const inp = document.getElementById("wbAlertInput");
      if (al && inp) { inp.value = JSON.stringify(al, null, 2); inp.focus(); }
    };
  });
}

function buildOpsMarkdown(out) {
  const n = out.normalized || {};
  const d = out.diagnosis || {};
  let s = `### 🛰️ 根因分析\n`;
  s += `- **降噪结论**：${n.is_noise ? "⚠️ 判定为噪声/误报，已抑制" : "✅ 真实告警，进入处置"}\n`;
  const hyps = d.hypotheses || [];
  if (hyps.length) {
    s += `\n### 🔍 根因假设\n`;
    hyps.forEach((h, i) => {
      s += `**H${i + 1}**（置信度 ${h.confidence}）：${h.cause}\n`;
      if (h.evidence) s += `- 证据：${h.evidence}\n`;
      if (h.recommended_tool) s += `- 建议工具：\`${h.recommended_tool}\`\n`;
      if (h.recommended_action) s += `- 处置动作：${h.recommended_action}\n`;
    });
  }
  if (d.conclusion) s += `\n**结论**：${d.conclusion}\n`;
  if (out.tool_results && out.tool_results.length) {
    s += `\n### 🔧 工具调用\n`;
    out.tool_results.forEach((t) => {
      const res = typeof t.result === "object" ? JSON.stringify(t.result) : t.result;
      s += `- \`${t.tool}\` → *${t.status}*：${res}\n`;
    });
  }
  const plan = out.plan || {};
  const acts = plan.actions || [];
  if (acts.length) s += `\n### 📋 处置建议\n` + acts.map((a) => `- ${a}`).join("\n") + "\n";
  if (out.missing_tool) s += `\n> 🔴 **缺口**：运维需要工具 \`${out.missing_tool}\`，但工具库缺失 → 已生成反馈工单，转研发 Agent 处理。\n`;
  return s;
}

// ---------- 闭环看板 ----------
const STEPS = [
  { ico: "🛰️", title: "运维处理告警", desc: "运维 Agent 降噪 + 根因推理，发现处置所需工具缺失", meta: "" },
  { ico: "📝", title: "生成反馈工单", desc: "自动产出反馈单，转交研发 Agent", meta: "" },
  { ico: "🛠️", title: "研发造工具", desc: "生成工具脚本、写入 tools/、注册进工具库、沉淀 SOP", meta: "" },
  { ico: "🔁", title: "运维复用新工具", desc: "第二轮处置直接调用研发刚注册的工具", meta: "" },
  { ico: "✅", title: "闭环完成", desc: "业务运维一体化闭环达成", meta: "" },
];

function buildPipeline() {
  const box = $("#pipeline");
  box.innerHTML = "";
  STEPS.forEach((s, i) => {
    const el = document.createElement("div");
    el.className = "step";
    el.innerHTML = `<div class="rail"><div class="node">${s.ico}</div><div class="line"></div></div><div class="body"><div class="st-title">${s.title}</div><div class="st-desc">${s.desc}</div><div class="st-meta"></div></div>`;
    box.appendChild(el);
  });
}
function setStep(i, cls, meta) {
  const steps = $$("#pipeline .step");
  if (steps[i]) { steps[i].classList.add(cls); if (meta) steps[i].querySelector(".st-meta").textContent = meta; }
}

$("#loopRun").addEventListener("click", async () => {
  buildPipeline();
  $("#loopResult").innerHTML = "";
  const btn = $("#loopRun"); btn.disabled = true;
  try {
    setStep(0, "active");
    const r = await apiFetch(`/closed-loop/run`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) });
    const out = await r.json();
    setStep(0, "done");
    if (out.loop_closed) {
      setStep(1, "done");
      const dev = out.dev_result || {};
      setStep(2, "active"); await wait(500); setStep(2, "done", `→ 生成 ${dev.tool ? dev.tool.name : "工具"}`);
      setStep(3, "active"); await wait(500); setStep(3, "done");
    } else {
      setStep(1, "done", "工具已存在，直接复用（无需研发介入）");
      setStep(2, "done"); setStep(3, "active"); await wait(400); setStep(3, "done");
    }
    setStep(4, "active"); await wait(400); setStep(4, "done");
    const card = document.createElement("div");
    card.className = "tcard";
    card.innerHTML = `<h3>${out.loop_closed ? "🔴→🟢 闭环已达成" : "♻️ 复用既有工具"}</h3>
      <p>告警 <code>${out.alert.alert_id}</code> · 缺口工具：<b>${out.missing_tool || "无"}</b></p>
      ${out.round1 && out.round1.diagnosis && out.round1.diagnosis.hypotheses && out.round1.diagnosis.hypotheses.length ? `<p>首轮根因：${out.round1.diagnosis.hypotheses[0].cause}</p>` : ""}
      ${out.round2 && out.round2.tool_results && out.round2.tool_results.length ? `<p>次轮工具调用：${out.round2.tool_results.map((t) => t.tool + "(" + t.status + ")").join("、")}</p>` : ""}`;
    $("#loopResult").appendChild(card);
  } catch (e) {
    $("#loopResult").innerHTML = `<div class="empty" style="color:var(--coral)">请求失败：${e.message}</div>`;
  } finally { btn.disabled = false; }
});

$("#loopReset").addEventListener("click", () => {
  $("#loopResult").innerHTML = `<div class="empty">重置说明：删除 <code>tools/optical_power_probe.py</code>、<code>tools/temperature_probe.py</code> 并清理 <code>data/tools.json</code> 中对应条目后，再跑闭环即可重演"造工具"全过程。<br>（也可直接重新克隆仓库首次运行）</div>`;
});

// ================= 实时告警流（持续监控演示面板） =================
// 后端 AlertStream 单例：剧本队列按节拍播放 → 运维逐条降噪/根因/处置 →
// 缺口自动派研发造工具 → 后续同类故障复用沉淀。前端秒级轮询 status + 增量 feed。
const STREAM = { inited: false, seen: 0, started: false, tick: null, ops: "", mode: "auto", profile: "", wsName: "全局" };

function streamSegVal(segId) {
  const el = document.querySelector(`#${segId} .seg-btn.active`);
  return el ? (el.dataset.p || el.dataset.m || el.dataset.t) : null;
}
function streamSegPick(segId) {
  document.querySelectorAll(`#${segId} .seg-btn`).forEach(b => {
    b.onclick = () => {
      document.querySelectorAll(`#${segId} .seg-btn`).forEach(x => x.classList.toggle("active", x === b));
    };
  });
}
["streamProfileSeg", "streamModeSeg", "streamTickSeg"].forEach(streamSegPick);

function fmtUptime(s) {
  if (!(s > 0)) return "00:00";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return (h ? String(h).padStart(2, "0") + ":" : "") + String(m).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
}
function streamProfileLabel(p) {
  return p === "story" ? "story · 聚焦闭环" : "mixed · 样本为主";
}

function ensureStreamStats() {
  const box = $("#streamStats");
  if (box.children.length) return;
  const defs = [
    ["ingested", "已接入", "dim"], ["noise", "噪声抑制", "dim"],
    ["real", "真实处置", "coral"], ["created", "闭环造工具", "prime"],
    ["reused", "复用沉淀", "ok"], ["pending", "待派发", "warn"],
    ["errors", "错误", "coral"], ["uptime", "已运行", "cy"],
  ];
  box.innerHTML = defs.map(d => `<div class="ss-card"><div class="ss-num ${d[2]}" data-k="${d[0]}">0</div><div class="ss-lbl">${d[1]}</div></div>`).join("");
}
function setStreamStat(k, v) {
  const el = document.querySelector(`#streamStats .ss-num[data-k="${k}"]`);
  if (el) el.textContent = v;
}
function renderStreamStats(s) {
  ensureStreamStats();
  const st = s.stats || {};
  setStreamStat("ingested", st.ingested ?? 0);
  setStreamStat("noise", st.noise ?? 0);
  setStreamStat("real", st.real ?? 0);
  setStreamStat("created", st.created ?? 0);
  setStreamStat("reused", st.reused ?? 0);
  setStreamStat("pending", st.pending ?? 0);
  setStreamStat("errors", st.errors ?? 0);
  setStreamStat("uptime", fmtUptime(s.uptime_s || 0));
}

function renderStreamLivebar(s) {
  const pill = document.querySelector("#streamLivebar .live-pill");
  if (pill) {
    pill.className = "live-pill " + (s.running ? "live" : "idle");
    const txt = $("#liveTxt");
    txt.textContent = s.running ? "LIVE 运行中" : (STREAM.started ? "已停止" : "未运行");
  }
  const meta = $("#liveMeta");
  if (meta) {
    const modeTxt = s.mode === "manual" ? "✋ 手动" : (s.running || STREAM.started ? "⚡ 自动" : "—");
    meta.innerHTML = `<b>${escapeHtml(STREAM.profile || streamProfileLabel(s.profile || "story"))}</b>` +
      ` · 域 <b>${escapeHtml(STREAM.wsName)}</b> · 处置 <code>${escapeHtml(STREAM.ops || s.ops_agent_id || "自动路由")}</code>` +
      ` · ${modeTxt} · 间隔 ${s.interval_ms || 1200}ms · 轮次 ${s.rounds || 0}` +
      ` · 剩余 ${s.queue_remaining ?? 0} 条`;
  }
  const cur = $("#liveCurrent");
  if (cur) {
    const c = s.current;
    if (s.running && c) {
      const sev = c.severity === "critical" ? "🚨" : c.severity === "major" ? "⚠️" : "ℹ️";
      cur.innerHTML = `<span class="spin">⟳</span> 正在处置 ${sev} <code>${escapeHtml(c.alert_id)}</code> · ${escapeHtml(c.metric)} @ ${escapeHtml(c.host)}`;
    } else if (s.running) {
      cur.innerHTML = `<span class="spin">⟳</span> 等待下一条告警…`;
    } else {
      cur.innerHTML = "";
    }
  }
  const err = $("#liveErr");
  if (err) {
    if (s.last_error) err.textContent = "⚠ " + s.last_error;
    else err.textContent = "";
  }
}

function clearStreamFeed(emptyHtml) {
  const box = $("#streamFeed");
  box.innerHTML = `<div class="empty">${emptyHtml || "流水线未启动。"}</div>`;
}
function appendStreamRow(it) {
  const box = $("#streamFeed");
  const empty = box.querySelector(":scope > .empty");
  if (empty) empty.remove();
  const a = it.alert || {};
  const sev = a.severity === "critical" ? "🚨" : a.severity === "major" ? "⚠️" : "ℹ️";
  const verdict = it.noise
    ? `<span class="sf-badge noise">🔕 噪声</span>`
    : `<span class="sf-badge real">🚨 真实</span>`;
  const tri = `<span class="sf-badge ${it.triage_by === "rule" ? "rule" : "llm"}">${it.triage_by === "rule" ? "规则" : "LLM"}</span>`;
  const loop = it.loop === "created" ? `<span class="sf-badge created">🛠 造工具</span>`
    : it.loop === "reused" ? `<span class="sf-badge reused">♻ 复用</span>`
    : it.loop === "pending" ? `<span class="sf-badge pending">📥 待派发</span>`
    : `<span class="sf-badge loop-none">—</span>`;
  const row = document.createElement("div");
  row.className = "sf-row" + (it.error ? " row-err" : "");
  const at = String(it.at || "").slice(11, 19);
  const sum = it.error ? (it.summary || it.error) : (it.summary || "已处置");
  row.innerHTML =
    `<span class="sf-time">${escapeHtml(at)}</span>` +
    `<span class="sf-seq">#${it.seq ?? ""}</span>` +
    verdict + tri +
    `<span class="sf-alert"><span class="a-id">${sev} ${escapeHtml(a.alert_id || "?")}</span>` +
      `<span class="sf-sev">${escapeHtml(a.severity || "")}</span>` +
      `<div class="a-sub">${escapeHtml(a.metric || "")} @ ${escapeHtml(a.host || "")}</div></span>` +
    `<span class="sf-sum">${escapeHtml(sum)}</span>` +
    loop +
    `<span class="sf-dur">${it.duration_ms != null ? it.duration_ms + "ms" : ""}</span>`;
  box.appendChild(row);
  while (box.children.length > 220) box.removeChild(box.firstChild);   // 只保留最近 220 条，防 DOM 膨胀
  box.scrollTop = box.scrollHeight;
}

async function streamPoll() {
  const view = document.getElementById("view-stream");
  if (!view || !view.classList.contains("active")) return;
  try {
    const s = await (await apiFetch(`/stream/status`)).json();
    const st = s.stats || {};
    renderStreamStats(s);
    renderStreamLivebar(s);
    // 长时间离开页面后回流补齐（环形缓冲 300，最多可重放 250 条差额）
    if (s.running && st.ingested - STREAM.seen > 250) { STREAM.seen = 0; clearStreamFeed("正在追赶最近 300 条处置流水…"); }
    const f = await (await apiFetch(`/stream/feed?after=${STREAM.seen}`)).json();
    const items = f.items || [];
    if (items.length) {
      STREAM.seen = Math.max(...items.map(i => i.seq));
      items.forEach(appendStreamRow);
    }
    if (!s.running && !STREAM.started && !st.ingested) {
      // 从未运行过：保持空态文案（stats 仍可展示）
    }
  } catch (e) { /* 后端暂不可达：静默，健康灯会提示 */ }
}
function streamEnter() {
  if (!STREAM.inited) { STREAM.inited = true; clearStreamFeed(); }
  if (!STREAM.tick) STREAM.tick = setInterval(streamPoll, 1200);
  streamPoll();
}

// ---- 控制：启动 / 停止 / 重置 ----
$("#streamStart").onclick = async () => {
  const btn = $("#streamStart");
  btn.disabled = true;
  try {
    const profile = streamSegVal("streamProfileSeg") || "story";
    const mode = streamSegVal("streamModeSeg") || "auto";
    const interval = parseInt(streamSegVal("streamTickSeg") || "1200", 10);
    const body = { profile, mode, interval_ms: interval, loop: true, workspace_id: CURRENT_WS || null };
    const r = await apiFetch(`/stream/start`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status));
    STREAM.ops = d.ops_agent_id || "";
    STREAM.mode = d.mode || mode;
    STREAM.profile = d.profile || profile;
    const ws = WS_LIST.find(w => w.id === CURRENT_WS);
    STREAM.wsName = ws ? ws.name : "全局";
    STREAM.started = true;
    STREAM.seen = 0;
    clearStreamFeed("🚀 流水线已启动，等待首条告警处置完成…");
    $("#streamStop").disabled = false;
    $("#streamReset").disabled = true;
    streamPoll();
  } catch (e) {
    alert("启动失败：" + e.message);
    $("#streamReset").disabled = false;
  } finally { btn.disabled = false; }
};

$("#streamStop").onclick = async () => {
  try {
    await apiFetch(`/stream/stop`, { method: "POST" });
    $("#streamStop").disabled = true;
    $("#streamReset").disabled = false;
    $("#liveTxt").textContent = "已停止";
  } catch (e) { alert("停止失败：" + e.message); }
};

$("#streamReset").onclick = async () => {
  try {
    const r = await apiFetch(`/stream/reset-demo`, { method: "POST" });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status));
    const names = (d.tools || []).join(", ") || "（空）";
    const errEl = $("#liveErr");
    if (errEl) { errEl.style.color = "var(--ok)"; errEl.textContent = "↺ 已重置：工具库回到 → " + names; setTimeout(() => { errEl.style.color = ""; }, 6000); }
    $("#streamStart").disabled = false;
    $("#streamStop").disabled = true;
  } catch (e) { alert("重置失败：" + e.message); }
};

// ---------- 拓扑 ----------
async function renderTopo() {
  const svg = $("#topoSvg");
  try {
    const r = await apiFetch(`/topology`);
    const d = await r.json();
    const nodes = d.nodes || [];
    const edges = d.edges || [];
    const cx = 400, cy = 260, R = 200;
    const pos = {};
    nodes.forEach((n, i) => {
      const a = (i / nodes.length) * Math.PI * 2 - Math.PI / 2;
      pos[n.id] = { x: cx + R * Math.cos(a), y: cy + R * Math.sin(a) };
    });
    let svgStr = "";
    edges.forEach((e) => {
      const a = pos[e.from], b = pos[e.to];
      if (a && b) svgStr += `<line class="tedge" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"/>`;
    });
    nodes.forEach((n, i) => {
      const p = pos[n.id];
      const cls = n.type === "host" ? "host" : n.type === "device" ? "device" : n.type === "service" ? "service" : "";
      svgStr += `<g style="animation: fade .5s ease ${i * 0.05}s both"><circle class="tnode ${cls}" cx="${p.x}" cy="${p.y}" r="20"/><text class="tlabel" x="${p.x}" y="${p.y + 36}" text-anchor="middle">${n.label || n.id}</text></g>`;
    });
    svg.innerHTML = svgStr;
  } catch (e) { svg.innerHTML = `<text fill="var(--coral)" x="400" y="260" text-anchor="middle">拓扑加载失败：${e.message}</text>`; }
}

// ---------- 工具库 ----------
async function renderTools() {
  const grid = $("#toolsGrid");
  grid.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    const r = await apiFetch(`/tools`);
    const d = await r.json();
    const list = d.tools || [];
    if (!list.length) { grid.innerHTML = `<div class="empty">工具库为空</div>`; return; }
    grid.innerHTML = list.map((t) => `<div class="tcard"><h3>${t.name}</h3><div class="tname">${t.file || ""}</div><p>${t.description || ""}</p><span class="tag">风险: ${t.risk || "low"}</span></div>`).join("");
  } catch (e) { grid.innerHTML = `<div class="empty" style="color:var(--coral)">加载失败：${e.message}</div>`; }
}

// ---------- 知识库 ----------
$("#kbRun").addEventListener("click", async () => {
  const q = $("#kbQuery").value.trim();
  if (!q) { alert("请输入问题"); return; }
  const box = $("#kbResult");
  box.innerHTML = `<div class="empty">检索中…</div>`;
  try {
    const r = await apiFetch(`/knowledge?q=${encodeURIComponent(q)}&top_k=4`);
    const d = await r.json();
    const hits = d.hits || [];
    if (!hits.length) { box.innerHTML = `<div class="empty">无匹配知识</div>`; return; }
    box.innerHTML = hits.map((h) => `<div class="kbitem"><div class="ksrc">📚 ${h.source || ""} · 相关度 ${h.score ? h.score.toFixed(2) : "?"}</div><div class="ktext">${escapeHtml(h.text)}</div></div>`).join("");
  } catch (e) { box.innerHTML = `<div class="empty" style="color:var(--coral)">检索失败：${e.message}</div>`; }
});

// ---------- 消息栏 / 需求看板（多 Agent 人工 / 自动派发闭环） ----------
let AGENTS = { ops: [], dev: [] };

function agentName(id) {
  const all = [...AGENTS.ops, ...AGENTS.dev];
  const a = all.find((x) => x.id === id);
  return a ? a.name : id;
}
function statusLabel(s) {
  return ({ pending: "待派发研发", dev_assigned: "已派研发", building: "研发中",
    tool_ready: "待派运维", ops_assigned: "已派运维", done: "闭环完成",
    rejected: "已驳回" })[s] || s;
}

async function loadAgents() {
  // A4：按当前业务域过滤，避免手动派发误选其他域的 Agent
  const url = CURRENT_WS ? `${API}/agents?workspace_id=${CURRENT_WS}` : `${API}/agents`;
  const r = await fetch(url);
  const d = await r.json();
  AGENTS.ops = (d.agents || []).filter((a) => a.kind === "ops");
  AGENTS.dev = (d.agents || []).filter((a) => a.kind === "dev");
  $("#raiseOps").innerHTML = AGENTS.ops.map((a) => `<option value="${a.id}">${a.name}</option>`).join("");
  renderAgents();
}
function renderAgents() {
  const o = $("#agentOps"), dv = $("#agentDev");
  const opsLocked = AGENTS.ops.length <= 1;   // 每个域至少保留 1 个运维 Agent
  const devLocked = AGENTS.dev.length <= 1;   // 每个域至少保留 1 个研发 Agent
  o.innerHTML = AGENTS.ops.map((a) => agentCard(a, opsLocked)).join("");
  dv.innerHTML = AGENTS.dev.map((a) => agentCard(a, devLocked)).join("");
}
function agentCard(a, locked) {
  const del = locked ? "" :
    `<button class="ac-del" onclick="event.stopPropagation(); deleteAgent('${a.id}')" title="删除 Agent">✕</button>`;
  return `<div class="agent-card">
    <div class="ac-top">
      <span class="ac-name">${a.name}</span>
      <span class="ac-actions">
        <span class="ac-status ${a.status}">${a.status}</span>
        ${del}
      </span>
    </div>
    <div class="ac-scope">擅长: ${a.scope.join(", ")}</div>
    <div class="ac-desc">${a.description || ""}</div>
  </div>`;
}
async function deleteAgent(id) {
  const all = AGENTS.ops.concat(AGENTS.dev);
  const a = all.find((x) => x.id === id);
  const name = a ? a.name : id;
  if (!confirm(`确认删除 Agent「${name}」？\n删除后不可恢复，且该业务域至少需保留一个运维 / 一个研发 Agent。`)) return;
  try {
    const r = await apiFetch(`/workspaces/${CURRENT_WS}/agents/${id}`, { method: "DELETE" });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      alert("删除失败：" + (e.detail || r.status));
      return;
    }
    await loadAgents();
  } catch (e) { alert("删除失败：" + e.message); }
}

async function loadRequirements() {
  const r = await apiFetch(`/requirements`);
  const d = await r.json();
  renderRequirements(d.requirements || []);
}
function renderRequirements(reqs) {
  const box = $("#reqList");
  if (!reqs.length) { box.innerHTML = `<div class="empty">暂无需求，点上方「发起需求」试试。</div>`; return; }
  box.innerHTML = reqs.map((req) => {
    let actions = "";
    if (req.status === "pending") {
      const opts = AGENTS.dev.map((a) => `<option value="${a.id}">${a.name}</option>`).join("");
      actions = `<div class="req-actions"><select id="dpick-${req.id}">${opts}</select><button class="primary-btn sm" onclick="__dispatchDev('${req.id}')">派发研发</button></div>`;
    } else if (req.status === "tool_ready") {
      const opts = AGENTS.ops.map((a) => `<option value="${a.id}">${a.name}</option>`).join("");
      actions = `<div class="req-actions"><select id="opick-${req.id}">${opts}</select><button class="primary-btn sm" onclick="__dispatchOps('${req.id}')">派发运维</button></div>`;
    } else if (req.status === "done") {
      const tr = ((req.round2 && req.round2.tool_results) || []).map((t) => `${t.tool}(${t.status})`).join("、");
      actions = `<div class="req-done">✅ 闭环完成 · 派回 <b>${agentName(req.target_ops_agent_id)}</b> · 工具结果: ${tr || "—"}</div>`;
    }
    return `<div class="req-card st-${req.status}">
      <div class="req-head"><span class="req-id">${req.id}</span><span class="req-badge ${req.status}">${statusLabel(req.status)}</span></div>
      <div class="req-title">${req.title || ""}</div>
      <div class="req-meta">来源: ${agentName(req.source_ops_agent_id)} · 需要工具: <code>${req.needed_tool || "—"}</code>${req.assigned_dev_agent_id ? ` · 研发: ${agentName(req.assigned_dev_agent_id)}` : ""}${req.created_tool_name ? ` · 产出: <code>${req.created_tool_name}</code>` : ""}</div>
      ${actions}
    </div>`;
  }).join("");
}

async function raiseRequirement() {
  const opsId = $("#raiseOps").value;
  const hint = $("#raiseHint"); hint.textContent = "发起中…";
  try {
    const r = await apiFetch(`/requirements/raise`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ops_agent_id: opsId, workspace_id: CURRENT_WS }) });
    const d = await r.json();
    const res = await pollJob(d.job_id);
    if (res.reusable) { hint.textContent = "♻️ " + (res.error || "工具已存在，直接复用"); await loadRequirements(); return; }
    if (res.error) { hint.textContent = "⚠️ " + res.error; return; }
    hint.textContent = res.mode === "auto" ? "✅ 自动模式：已自动跑完闭环" : "✅ 手动模式：需求已登记，请点「派发研发」";
    await loadRequirements(); await loadAgents();
  } catch (e) { hint.textContent = "⚠️ 发起失败：" + e.message; }
}
window.__dispatchDev = async function (id) {
  const aid = document.getElementById(`dpick-${id}`).value;
  await apiFetch(`/requirements/${id}/dispatch-dev`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ agent_id: aid }) });
  await loadRequirements(); await loadAgents();
};
window.__dispatchOps = async function (id) {
  const aid = document.getElementById(`opick-${id}`).value;
  await apiFetch(`/requirements/${id}/dispatch-ops`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ agent_id: aid }) });
  await loadRequirements(); await loadAgents();
};

function setModeUI(mode) {
  $("#modeAuto").classList.toggle("active", mode === "auto");
  $("#modeManual").classList.toggle("active", mode === "manual");
}
async function setMode(mode) {
  await apiFetch(`/dispatch/mode`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) });
  setModeUI(mode);
}
async function renderBoard() {
  try { const m = await (await apiFetch(`/dispatch/mode`)).json(); setModeUI(m.mode); } catch (e) {}
  await loadAgents();
  await loadRequirements();
}
$("#modeAuto").onclick = () => setMode("auto");
$("#modeManual").onclick = () => setMode("manual");
$("#boardRefresh").onclick = () => renderBoard();
$("#raiseBtn").onclick = () => raiseRequirement();

// ---------- 接入层 / 适配器 ----------
const ADP_ICON = { alert: "🚨", log: "📜", cmdb: "🗺️", ticket: "🎫", exec: "⚙️", knowledge: "📚" };

async function renderIntegrations() {
  const grid = $("#adapterGrid"), stats = $("#integStats");
  grid.innerHTML = '<div class="empty">加载适配器…</div>';
  try {
    const d = await (await apiFetch(`/adapters`)).json();
    const list = d.adapters || [];
    const north = list.filter((a) => a.direction === "north").length;
    const south = list.filter((a) => a.direction === "south").length;
    const sample = list.filter((a) => a.status === "sample").length;
    const reserved = list.filter((a) => a.status === "reserved").length;
    stats.innerHTML = `
      <div class="integ-stat"><div class="num">${list.length}</div><div class="lbl">适配器接口</div></div>
      <div class="integ-stat"><div class="num">${north}</div><div class="lbl">北向·感知</div></div>
      <div class="integ-stat"><div class="num">${south}</div><div class="lbl">南向·执行</div></div>
      <div class="integ-stat"><div class="num">${sample}</div><div class="lbl">样板可运行</div></div>
      <div class="integ-stat"><div class="num">${reserved}</div><div class="lbl">预留待接入</div></div>`;
    grid.innerHTML = list.map((a) => `
      <div class="adp-card">
        <div class="adp-top">
          <span class="adp-ico">${ADP_ICON[a.adapter_type] || "🔌"}</span>
          <span class="adp-name">${a.name}</span>
        </div>
        <div class="adp-badges">
          <span class="adp-badge ${a.direction}">${a.direction_label}</span>
          <span class="adp-badge ${a.status}">${a.status_label}</span>
        </div>
        <div class="adp-sys">对接：${a.system}</div>
        <div class="adp-desc">${a.description}</div>
        <div class="adp-foot">
          <button class="ghost-btn sm" onclick="__testAdapter('${a.id}')">🔍 测试连接</button>
        </div>
      </div>`).join("");
  } catch (e) {
    grid.innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
  }
}

window.__testAdapter = async function (id) {
  const grid = $("#adapterGrid");
  try {
    const r = await apiFetch(`/adapters/${id}/test`, { method: "POST" });
    const d = await r.json();
    const h = d.health || {};
    const msg = h.status === "reserved"
      ? `🔸 预留接口：${h.note}`
      : `✅ ${JSON.stringify(h)}`;
    alert(`${d.adapter.name}\n${msg}`);
  } catch (e) { alert("测试失败：" + e.message); }
};

const SAMPLE_PROM_WEBHOOK = {
  alerts: [{
    labels: { alertname: "HostHighTemp", instance: "host-1:9100", job: "node", severity: "critical" },
    annotations: { description: "核心温度 88C 过热", value: "88C" },
    startsAt: "2026-09-01T10:00:00Z",
  }],
};

$("#ingestRun").onclick = async () => {
  const box = $("#ingestResult");
  box.innerHTML = '<div class="empty">正在经 Prometheus 适配器接入并交给运维 Agent 分析…</div>';
  try {
    const r = await apiFetch(`/adapters/alert/ingest`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ adapter_id: "alert-prometheus", payload: SAMPLE_PROM_WEBHOOK, workspace_id: CURRENT_WS }),
    });
    const d = await r.json();
    const res = (d.results || [])[0] || {};
    const diag = res.diagnosis || {};
    const hyps = (diag.hypotheses || []).map((h) =>
      `<li><b>${h.cause}</b> · 置信度 ${h.confidence} · 工具 <code>${h.recommended_tool || "—"}</code></li>`).join("");
    const tools = (res.tool_results || []).map((t) =>
      `<li><code>${t.tool}</code> → <span class="req-badge ${t.status}">${t.status}</span></li>`).join("");
    box.innerHTML = `
      <h4>① 适配器解析出的统一告警</h4>
      <pre class="code-box">${escapeHtml(JSON.stringify((d.results || [])[0]?.alert || {}, null, 2))}</pre>
      <h4>② 运维 Agent 根因分析</h4>
      <ul>${hyps || "<li>无</li>"}</ul>
      <h4>③ 工具调用结果</h4>
      <ul>${tools || "<li>无</li>"}</ul>
      ${res.missing_tool ? `<p class="muted">🔴 发现工具缺口 <code>${res.missing_tool}</code>，可去「消息栏」派发研发造工具形成闭环。</p>` : ""}`;
  } catch (e) {
    box.innerHTML = `<div class="empty">接入失败：${e.message}</div>`;
  }
};

$("#ingestJson").onclick = () => {
  const box = $("#ingestResult");
  box.innerHTML = `<h4>Alertmanager webhook → 统一 Alert 映射示例</h4>
    <pre class="code-box">${escapeHtml(JSON.stringify(SAMPLE_PROM_WEBHOOK.alerts[0], null, 2))}</pre>
    <p class="muted">经 <code>to_unified()</code> 后字段：alert_id / ts / source / metric / host / severity / value / message / tags / is_noise。</p>`;
};

// ---------- 工具函数 ----------
function escapeHtml(s) { return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
function wait(ms) { return new Promise((r) => setTimeout(r, ms)); }

checkHealth();
checkAuth();
setInterval(checkAuth, 30000);
setInterval(checkHealth, 15000);
loadWorkspaces();

// ================= 业务域 / Agent 作战室 =================
let CURRENT_WS = null;
let WS_LIST = [];
let ADAPTERS = [];
let WAR_AGENTS = {};   // 当前业务域内所有 Agent 索引：id -> {id,name,kind,scope,status}

function openModal(id) { const m = document.getElementById(id); if (m) m.classList.add("open"); }
function closeModal(id) { const m = document.getElementById(id); if (m) m.classList.remove("open"); }

async function loadWorkspaces() {
  try {
    const d = await (await apiFetch(`/workspaces`)).json();
    WS_LIST = d.workspaces || [];
    if (!CURRENT_WS && WS_LIST.length) CURRENT_WS = WS_LIST[0].id;
    renderWsTabs();
    if (CURRENT_WS) await renderOverview(CURRENT_WS);
  } catch (e) { $("#wsTabs").innerHTML = `<span class="empty-sm">业务域加载失败</span>`; }
}
function renderWsTabs() {
  const box = $("#wsTabs");
  box.innerHTML = (WS_LIST.map(w => `<button class="ws-tab ${w.id === CURRENT_WS ? "active" : ""}" data-ws="${w.id}">${w.name}</button>`).join(""))
    + `<button class="ws-tab ws-tab-add" id="wsAddTab">＋ 接入接口</button>`;
  box.querySelectorAll(".ws-tab[data-ws]").forEach(b => b.onclick = async () => {
    CURRENT_WS = b.dataset.ws; renderWsTabs(); await renderOverview(CURRENT_WS);
  });
  box.querySelectorAll(".ws-tab[data-ws]").forEach(b => {
    b.addEventListener("mouseenter", () => showWsHover(b, b.dataset.ws));
    b.addEventListener("mouseleave", hideWsHover);
  });
  $("#wsAddTab").onclick = openWsModal;
}
async function loadOverview() { if (CURRENT_WS) await renderOverview(CURRENT_WS); else await loadWorkspaces(); }

async function renderOverview(wsId) {
  try {
    const d = await (await apiFetch(`/workspaces/${wsId}`)).json();
    $("#ovWsName").textContent = d.name || wsId;
    setWsModeUI(d.mode);
    WAR_AGENTS = {};
    (d.agents || []).forEach(a => { WAR_AGENTS[a.id] = a; });
    const dev = (d.agents || []).filter(a => a.kind === "dev");
    const ops = (d.agents || []).filter(a => a.kind === "ops");
    const devLocked = dev.length <= 1;   // 每个域至少保留 1 个研发 Agent
    const opsLocked = ops.length <= 1;   // 每个域至少保留 1 个运维 Agent
    $("#warDev").innerHTML = dev.map(a => warCard(a, devLocked)).join("") + `<button class="war-add" data-kind="dev">＋ 创建研发 Agent</button>`;
    $("#warOps").innerHTML = ops.map(a => warCard(a, opsLocked)).join("") + `<button class="war-add" data-kind="ops">＋ 创建运维 Agent</button>`;
    bindWarCards();
    refreshWarStatuses();
    renderWarLoop(wsId);
    if (document.getElementById("msgDrawer").classList.contains("open")) loadDrawer();
  } catch (e) {
    // 错误只显示在画布区，不销毁侧栏与容器，避免切回即空白
    $("#warDev").innerHTML = `<div class="empty">作战室加载失败：${escapeHtml(e.message)}</div>`;
    $("#warOps").innerHTML = "";
    $("#warLoopBody").innerHTML = `<div class="empty">当前闭环加载失败</div>`;
  }
}

async function renderWarLoop(wsId) {
  const box = $("#warLoopBody");
  try {
    const r = await apiFetch(`/requirements?workspace_id=${wsId}`);
    const d = await r.json();
    const reqs = (d.requirements || []).slice(0, 3);
    if (!reqs.length) {
      box.innerHTML = `<div class="empty">暂无闭环记录。点「闭环看板」运行一次完整闭环，或从 Agent 工作台发起需求。</div>`;
      return;
    }
    box.innerHTML = reqs.map((req) => {
      let cls = "pending", ico = "⏳", title = "待处理需求", meta = req.title || "";
      if (req.status === "done") {
        cls = "done"; ico = "✅"; title = "闭环完成"; meta = `产出工具：${req.created_tool_name || "—"}`;
      } else if (req.status === "in_progress") {
        cls = "active"; ico = "🔧"; title = "研发造工具中"; meta = `负责：${agentName(req.assigned_dev_agent_id) || "—"}`;
      } else if (req.status === "tool_ready") {
        cls = "active"; ico = "🔁"; title = "工具已就绪，待派回运维"; meta = `需要：${req.needed_tool || "—"}`;
      } else if (req.status === "pending") {
        title = "待派发研发"; meta = `需要：${req.needed_tool || "—"}`;
      }
      const ts = req.created_at ? new Date(req.created_at).toLocaleString("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
      return `<div class="loop-mini ${cls}">
        <div class="step-ico">${ico}</div>
        <div class="step-body">
          <div class="step-title">${escapeHtml(title)} · <code>${req.id}</code></div>
          <div class="step-meta">${escapeHtml(meta)} · 来源 ${agentName(req.source_ops_agent_id) || "—"}</div>
        </div>
        <div class="step-time">${ts}</div>
      </div>`;
    }).join("");
  } catch (e) {
    box.innerHTML = `<div class="empty">当前闭环加载失败：${escapeHtml(e.message)}</div>`;
  }
}

// 状态灯实时联动：轮询各 Agent 真实 status（idle/busy/error），覆盖到作战室卡片
async function refreshWarStatuses() {
  if (!CURRENT_WS) return;
  try {
    const d = await (await apiFetch(`/agents?workspace_id=${CURRENT_WS}`)).json();
    const map = {};
    (d.agents || []).forEach(a => { map[a.id] = a.status; });
    $$(".war-card").forEach(c => {
      const id = c.dataset.id;
      if (!(id in map)) return;
      const st = map[id] || "idle";
      const dot = c.querySelector(".status-dot");
      if (dot) dot.className = "status-dot " + st;
      const idEl = c.querySelector(".war-id");
      if (idEl) {
        const stTxt = st === "idle" ? "待命" : st === "busy" ? "工作中" : st === "error" ? "异常" : st;
        idEl.textContent = id + " · " + stTxt;
      }
    });
    // 已打开的工作台抽屉，状态灯也要实时刷新
    const wbId = $("#wbId").textContent;
    if (document.getElementById("wbDrawer").classList.contains("open") && wbId && (wbId in map)) {
      const dot = document.getElementById("wbDot");
      if (dot) dot.className = "status-dot " + (map[wbId] || "idle");
    }
  } catch (e) {}
}
function warCard(a, locked) {
  const st = a.status || "idle";
  const stTxt = st === "idle" ? "待命" : st === "busy" ? "工作中" : st === "error" ? "异常" : st;
  const del = locked ? "" :
    `<button class="icon-btn war-del" data-del="${a.id}" title="删除 Agent">🗑</button>`;
  return `<div class="war-card ${a.kind}" data-id="${a.id}">
    <div class="war-top"><span class="war-name">${a.name}</span><span class="status-dot ${st}"></span></div>
    <div class="war-id">${a.id} · ${stTxt}</div>
    <div class="war-scope">${(a.scope || []).map(s => `<span>${escapeHtml(s)}</span>`).join("")}</div>
    <div class="war-actions"><button class="icon-btn" data-edit="${a.id}" title="改名 / 编辑">✎</button>${del}</div>
    <div class="war-foot">⤢ 点击打开工作台</div>
  </div>`;
}
function bindWarCards() {
  $$("#warDev .war-add, #warOps .war-add").forEach(b => b.onclick = () => openAgentModal(b.dataset.kind));
  $$(".war-card").forEach(card => {
    card.onclick = (e) => {
      if (e.target.closest(".icon-btn")) return;   // 编辑按钮单独处理
      openWorkbench(card.dataset.id);
    };
  });
  $$(".war-card .icon-btn[data-edit]").forEach(b => b.onclick = (e) => { e.stopPropagation(); openAgentEdit(b.dataset.edit); });
  $$(".war-card .icon-btn[data-del]").forEach(b => b.onclick = (e) => { e.stopPropagation(); deleteAgent(b.dataset.del); });
}

// ---------------- Agent 工作台（点击作战室卡片进入） ----------------
function pollJob(jobId) {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const r = await apiFetch(`/jobs/${jobId}`);
        const d = await r.json();
        if (d.status === "done") return resolve(d.result);
        if (d.status === "error") return reject(new Error(d.error || "任务执行失败"));
        if (d.status === "not_found") return reject(new Error("任务不存在或已过期"));
        setTimeout(tick, 400);
      } catch (e) { reject(e); }
    };
    tick();
  });
}

function openWorkbench(id) {
  const a = WAR_AGENTS[id];
  if (!a) return;
  $("#wbName").textContent = a.name;
  $("#wbId").textContent = a.id;
  $("#wbKindIco").textContent = a.kind === "ops" ? "🛰️" : "🛠️";
  $("#wbKindLabel").textContent = a.kind === "ops" ? "运维 Agent · 告警根因" : "研发 Agent · 造工具";
  const st = a.status || "idle";
  $("#wbDot").className = "status-dot " + st;

  const EMPTY_OPS = '<div class="empty">👉 左侧选一条预设告警或粘贴 JSON，点「▶ 分析告警」—— 根因分析与处置建议将显示在这里。</div>';
  const EMPTY_DEV = '<div class="empty">👉 左侧填写反馈单，点「🛠️ 研发造工具」—— 生成过程与工具产物将显示在这里。</div>';
  const side = (title, inner, actions, hint) => `
    <div class="wb-side">
      <div class="side-card">
        <h4>${title}</h4>
        ${inner}
        ${actions}
        <div class="side-hint">${hint}</div>
      </div>
    </div>`;
  const main = (emptyTxt) => `
    <div class="wb-main">
      <div class="chat" id="wbChat">${emptyTxt}</div>
    </div>`;
  const actBtns = (runId, runLabel, clearId) => `
    <div class="composer-actions">
      <button class="primary-btn" id="${runId}">${runLabel}</button>
      <button class="ghost-btn" id="${clearId}">清空结果</button>
    </div>`;

  if (a.kind === "ops") {
    const presetChips = PRESETS.map(p => `<span class="chip" data-preset="${escapeHtml(p.label)}">${p.label}</span>`).join("");
    $("#wbBody").innerHTML = `<div class="wb-layout cols-2">
      ${side("① 告警输入",
        `<div class="wb-preset-grid">${presetChips}</div>
         <textarea id="wbAlertInput" rows="5" placeholder="粘贴告警 JSON，或点上方预设样例…"></textarea>
         <details class="wb-lib" id="wbLibBox">
           <summary>📚 真实机群告警样本 <span class="lib-badge" id="wbLibCount">加载…</span></summary>
           <div class="lib-filters" id="wbLibFilters">${wbLibChips()}</div>
           <input id="wbLibQ" type="text" placeholder="搜 alert_id / metric / host / 内容…" />
           <div class="lib-list" id="wbLibList"><div class="empty">加载中…</div></div>
         </details>`,
        actBtns("wbOpsRun", "▶ 分析告警", "wbOpsClear"),
        "💡 预设 4 条为演示样例；📚 为 data/alerts.json 接入的真实机群告警（55 条，52 噪声）。分析发现工具缺口时，右侧底部会出现「登记并派发研发」，一键发起闭环。")}
      ${main(EMPTY_OPS)}
    </div>`;
    $$(".wb-preset-grid .chip").forEach(c => {
      c.onclick = () => {
        const p = PRESETS.find(x => x.label === c.dataset.preset);
        if (p) { $("#wbAlertInput").value = JSON.stringify(p.alert, null, 2); $("#wbAlertInput").focus(); }
      };
    });
    $("#wbOpsRun").onclick = () => wbOpsRun(id);
    $("#wbOpsClear").onclick = () => clearWbChat(EMPTY_OPS);
    loadWbAlertLib();
    $$("#wbLibFilters .chip").forEach(c => {
      c.onclick = () => {
        WB_ALERT_FILTER = c.dataset.f;
        $$("#wbLibFilters .chip").forEach(x => x.classList.toggle("on", x === c));
        renderWbAlertLib();
      };
    });
    const libQ = $("#wbLibQ");
    if (libQ) libQ.oninput = () => renderWbAlertLib();
  } else {
    $("#wbBody").innerHTML = `<div class="wb-layout cols-2">
      ${side("① 研发反馈单",
        `<input id="wbFbId" type="text" placeholder="反馈单号 (如 F-1024)" value="F-1024" />
         <textarea id="wbFbSummary" rows="6" placeholder="描述运维侧缺什么能力。例如：需要探测 ONU 光功率、判定弱光根因的工具…"></textarea>`,
        actBtns("wbDevRun", "🛠️ 研发造工具", "wbDevClear"),
        "💡 工具生成后自动注册进工具库（右侧消息栏 / 「工具库」页可见），运维 Agent 下一轮诊断即可复用，不产生重复需求。")}
      ${main(EMPTY_DEV)}
    </div>`;
    $("#wbDevRun").onclick = () => wbDevRun(id);
    $("#wbDevClear").onclick = () => clearWbChat(EMPTY_DEV);
  }
  window.applyWbWidth && applyWbWidth();
  document.getElementById("wbDrawer").classList.add("open");
}

function clearWbChat(emptyTxt) {
  const chat = $("#wbChat");
  if (chat) chat.innerHTML = emptyTxt || '<div class="empty">已清空，等待下一次操作…</div>';
}

function wbJsonBlock(jsonText, maxH = "340px") {
  const id = "wbjson-" + Math.random().toString(36).slice(2, 8);
  return `<div class="wb-json" id="${id}">
    <button class="ghost-btn json-copy" onclick="copyWbJson('${id}')">📋 复制</button>
    <pre>${escapeHtml(jsonText)}</pre>
  </div>`;
}
window.copyWbJson = function (id) {
  const pre = document.getElementById(id)?.querySelector("pre");
  if (!pre) return;
  const txt = pre.textContent;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(txt).then(() => {
      const b = document.querySelector("#" + id + " .json-copy");
      if (b) { const o = b.textContent; b.textContent = "✅ 已复制"; setTimeout(() => b.textContent = o, 1200); }
    }).catch(() => {});
  }
};

// 工作台抽屉：拖拽调宽（记忆宽度）
(function initWbResize() {
  const drawer = document.getElementById("wbDrawer");
  const bar = document.getElementById("wbDragbar");
  if (!drawer || !bar) return;
  const minW = 520;
  const defaultW = 780;
  const readSaved = () => {
    try { const v = parseInt(localStorage.getItem("teleops_wbw") || "", 10); if (v >= minW) return v; } catch (e) {}
    return defaultW;
  };
  const maxW = () => Math.max(minW, window.innerWidth - 320);
  const apply = (w) => {
    if (w == null || w === 0) {
      drawer.style.width = "";
      drawer.style.flexBasis = "";
    } else {
      const clamped = Math.min(maxW(), Math.max(minW, w));
      drawer.style.width = clamped + "px";
      drawer.style.flexBasis = clamped + "px";
    }
  };
  // 只在抽屉已打开时应用记忆宽度；关闭时交给 CSS（width: 0），避免一进入页面就出现“加载中...”
  if (drawer.classList.contains("open")) apply(readSaved());
  window.applyWbWidth = () => apply(readSaved());
  window.clearWbWidth = () => apply(null);
  bar.addEventListener("mousedown", (e) => {
    e.preventDefault();
    drawer.classList.add("no-anim");
    bar.classList.add("active");
    const startX = e.clientX, startW = drawer.offsetWidth;
    const onMove = (ev) => apply(Math.min(maxW(), Math.max(minW, startW + (startX - ev.clientX))));
    const onUp = () => {
      drawer.classList.remove("no-anim");
      bar.classList.remove("active");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      try { localStorage.setItem("teleops_wbw", String(drawer.offsetWidth)); } catch (e) {}
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
})();

async function wbOpsRun(aid) {
  const raw = $("#wbAlertInput").value.trim();
  let alert;
  try { alert = raw ? JSON.parse(raw) : PRESETS[0].alert; }
  catch (e) { alert("告警 JSON 格式错误"); return; }
  const chat = $("#wbChat");
  addMsg(chat, "user", wbJsonBlock(JSON.stringify(alert, null, 2)), "🧑");
  const tb = thinking(chat, "🛰️");
  if ($("#wbDot")) $("#wbDot").className = "status-dot busy";   // 工作台状态灯实时联动
  try {
    const r = await apiFetch(`/agents/${aid}/diagnose`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ alert }) });
    const d = await r.json();
    if (!r.ok) { tb.innerHTML = `<span style="color:var(--coral)">${(d.detail) || "请求失败"}</span>`; return; }
    const out = await pollJob(d.job_id);
    tb.innerHTML = "";
    await streamInto(tb, buildOpsMarkdown(out), 6);
    if (out.tool_results) out.tool_results.forEach((t) => {
      const c = document.createElement("div");
      c.className = "toolcard";
      c.innerHTML = `<div class="tc-head">🔧 ${t.tool}<span class="tc-status ${t.status === "blocked" ? "blocked" : "ok"}">${t.status}</span></div><div class="tc-body">${typeof t.result === "object" ? escapeHtml(JSON.stringify(t.result, null, 2)) : escapeHtml(String(t.result))}</div>`;
      chat.appendChild(c);
      chat.scrollTop = chat.scrollHeight;
    });
    // 写回消息栏「操作记录」：工作台产出沉淀为成果记录
    const opsWs = (WAR_AGENTS[aid] && WAR_AGENTS[aid].workspace_id) || CURRENT_WS;
    let sum = "诊断完成";
    if (out.diagnosis && out.diagnosis.conclusion) sum = "根因：" + out.diagnosis.conclusion;
    if (out.missing_tool) sum += ` · 缺口工具 ${out.missing_tool}`;
    postWorkbenchMessage(opsWs, aid, "diagnose", sum, JSON.stringify(out).slice(0, 2000));
    // A2：诊断出工具缺口 → 提供「登记并派发」按钮，回流到当前域消息栏
    if (out.missing_tool) wbShowGapAction(chat, aid, alert, out);
  } catch (e) {
    tb.innerHTML = `<span style="color:var(--coral)">分析失败：${e.message}</span>`;
  }
}

function wbShowGapAction(chat, aid, alert, out) {
  const wrap = document.createElement("div");
  wrap.className = "req-actions";
  wrap.innerHTML = `<span class="gap-hint">⚠️ 诊断发现工具缺口：<code>${escapeHtml(out.missing_tool)}</code></span>
    <button class="primary-btn sm" id="wbGapBtn">登记并派发研发</button>`;
  chat.appendChild(wrap);
  chat.scrollTop = chat.scrollHeight;
  $("#wbGapBtn").onclick = async () => {
    const btn = $("#wbGapBtn");
    btn.disabled = true; btn.textContent = "派发中…";
    try {
      const rr = await apiFetch(`/agents/${aid}/register-gap`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ alert, diagnosis: out.diagnosis, missing_tool: out.missing_tool }),
      });
      const rd = await rr.json();
      if (!rr.ok) { btn.textContent = "失败"; alert(rd.detail || "登记失败"); return; }
      const res = await pollJob(rd.job_id);
      btn.textContent = res.reusable
        ? "♻️ 工具已存在，直接复用"
        : "✅ 已登记" + (res.mode === "auto" ? " · 自动闭环完成" : " · 待派发");
      const gapWs = (WAR_AGENTS[aid] && WAR_AGENTS[aid].workspace_id) || CURRENT_WS;
      postWorkbenchMessage(gapWs, aid, "gap", `登记缺口 ${out.missing_tool} 并派发研发`, null);
      if (document.getElementById("msgDrawer").classList.contains("open")) loadDrawer();
    } catch (e) { btn.textContent = "失败"; alert(e.message); }
  };
}

async function wbDevRun(aid) {
  const fid = $("#wbFbId").value.trim() || "F-1024";
  const summary = $("#wbFbSummary").value.trim();
  if (!summary) { alert("请填写反馈摘要"); return; }
  const chat = $("#wbChat");
  addMsg(chat, "user", `**反馈单 ${fid}**：${summary}`, "🧑");
  const tb = thinking(chat, "🛠️");
  try {
    const r = await apiFetch(`/agents/${aid}/build`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ feedback_id: fid, summary }) });
    const d = await r.json();
    if (!r.ok) { tb.innerHTML = `<span style="color:var(--coral)">${(d.detail) || "请求失败"}</span>`; return; }
    const out = await pollJob(d.job_id);
    const tool = out.created_tool || {};
    const s = `### 🛠️ 研发完成\n- **生成工具**：\`${tool.name}\`\n- **用途**：${tool.description || "—"}\n- **代码位置**：\`${tool.file}\`\n- **风险等级**：${tool.risk || "low"}\n- **SOP 已沉淀**：\`${out.sop}\`\n\n> ✅ 工具已自动注册进工具库，运维 Agent 下一轮即可直接调用。`;
    tb.innerHTML = "";
    await streamInto(tb, s, 6);
    // 写回消息栏「操作记录」
    const devWs = (WAR_AGENTS[aid] && WAR_AGENTS[aid].workspace_id) || CURRENT_WS;
    const toolName = (out.created_tool && out.created_tool.name) || "工具";
    postWorkbenchMessage(devWs, aid, "build", `研发完成：生成工具 ${toolName}`, JSON.stringify(out).slice(0, 2000));
  } catch (e) {
    tb.innerHTML = `<span style="color:var(--coral)">造工具失败：${e.message}</span>`;
  }
}

$("#wbClose").onclick = () => {
  document.getElementById("wbDrawer").classList.remove("open");
  window.clearWbWidth && clearWbWidth();
};

function setWsModeUI(mode) {
  $("#wsModeAuto").classList.toggle("active", mode === "auto");
  $("#wsModeManual").classList.toggle("active", mode === "manual");
  syncSetModeUI(mode);
}
function getWsModeFromUI() { return $("#wsModeAuto").classList.contains("active") ? "auto" : "manual"; }
async function setWsMode(mode) {
  if (!CURRENT_WS) return;
  try {
    await apiFetch(`/workspaces/${CURRENT_WS}/mode`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) });
  } catch (e) {}
  setWsModeUI(mode);
}
$("#wsModeAuto").onclick = () => setWsMode("auto");
$("#wsModeManual").onclick = () => setWsMode("manual");

// ---- 创建 / 编辑 Agent 模态 ----
let agentModalKind = "ops";
let agentEditId = null;
function openAgentModal(kind) {
  agentEditId = null; agentModalKind = kind;
  $("#agentModalTitle").textContent = "创建 Agent";
  $("#agentName").value = ""; $("#agentScope").value = ""; $("#agentDesc").value = ""; $("#agentPrimary").checked = false;
  $$("#agentKind .seg-btn").forEach(b => b.classList.toggle("active", b.dataset.kind === kind));
  openModal("agentModal");
}
async function openAgentEdit(id) {
  const d = await (await apiFetch(`/workspaces/${CURRENT_WS}`)).json();
  const a = (d.agents || []).find(x => x.id === id);
  if (!a) return;
  agentEditId = id; agentModalKind = a.kind;
  $("#agentModalTitle").textContent = "编辑 Agent · " + a.name;
  $("#agentName").value = a.name;
  $("#agentScope").value = (a.scope || []).join(", ");
  $("#agentDesc").value = a.description || "";
  $("#agentPrimary").checked = !!a.primary;
  openModal("agentModal");
}
$$("#agentKind .seg-btn").forEach(b => b.onclick = () => {
  $$("#agentKind .seg-btn").forEach(x => x.classList.toggle("active", x === b));
  agentModalKind = b.dataset.kind;
});
$("#agentSave").onclick = async () => {
  const name = $("#agentName").value.trim();
  const scope = $("#agentScope").value.split(",").map(s => s.trim()).filter(Boolean);
  const desc = $("#agentDesc").value.trim();
  if (!name) { alert("请填写名称"); return; }
  try {
    if (agentEditId) {
      await apiFetch(`/workspaces/${CURRENT_WS}/agents/${agentEditId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, scope, description: desc }) });
    } else {
      await apiFetch(`/workspaces/${CURRENT_WS}/agents`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: agentModalKind, name, scope, description: desc }) });
    }
    closeModal("agentModal");
    await renderOverview(CURRENT_WS);
  } catch (e) { alert("保存失败：" + e.message); }
};

// ---- 创建业务域 模态 ----
let wsModalMode = "auto";
async function openWsModal() {
  try {
    const d = await (await apiFetch(`/adapters`)).json();
    ADAPTERS = d.adapters || [];
    $("#wsAdapter").innerHTML = ADAPTERS.length
      ? ADAPTERS.map(a => `<option value="${a.id}">${a.name}</option>`).join("")
      : `<option value="">（暂无可用适配器）</option>`;
  } catch (e) { $("#wsAdapter").innerHTML = `<option value="">（加载失败）</option>`; }
  $("#wsName").value = "";
  $$("#wsMode .seg-btn").forEach(b => b.classList.toggle("active", b.dataset.mode === "auto"));
  wsModalMode = "auto";
  openModal("wsModal");
}
$$("#wsMode .seg-btn").forEach(b => b.onclick = () => {
  $$("#wsMode .seg-btn").forEach(x => x.classList.toggle("active", x === b));
  wsModalMode = b.dataset.mode;
});
$("#wsSave").onclick = async () => {
  const name = $("#wsName").value.trim();
  if (!name) { alert("请填写业务域名称"); return; }
  const adapter = $("#wsAdapter").value || null;
  try {
    const r = await apiFetch(`/workspaces`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, adapter_id: adapter, mode: wsModalMode }) });
    if (!r.ok) { alert("创建失败：" + (await r.json()).detail); return; }
    closeModal("wsModal");
    await loadWorkspaces();
  } catch (e) { alert("创建失败：" + e.message); }
};

// ---- 设置模态 ----
$("#openSettings").onclick = () => {
  syncSetModeUI(getWsModeFromUI());
  $("#setToken").value = TOKEN;
  openModal("settingsModal");
};
$("#setTokenSave").onclick = () => {
  const t = $("#setToken").value.trim();
  setToken(t);
  localStorage.setItem("teleops_token", t);
  checkAuth();
  alert(t ? "Token 已保存，写操作将自动携带。" : "已清除 Token（当前未启用鉴权或服务端未要求）。");
};
function syncSetModeUI(mode) {
  $$("#setMode .seg-btn").forEach(b => b.classList.toggle("active", b.dataset.mode === mode));
}
$$("#setMode .seg-btn").forEach(b => b.onclick = () => { syncSetModeUI(b.dataset.mode); setWsMode(b.dataset.mode); });
$("#setTheme").onclick = () => { const el = document.documentElement; el.dataset.theme = el.dataset.theme === "dark" ? "light" : "dark"; };
$("#setReset").onclick = () => { if ($("#loopReset")) $("#loopReset").click(); };

// ---- 通用模态关闭 ----
$$(".modal-mask").forEach(m => m.addEventListener("click", e => { if (e.target === m) m.classList.remove("open"); }));
$$("[data-close]").forEach(b => b.onclick = () => closeModal(b.dataset.close));

// ---- 右侧消息抽屉（按当前业务域筛选） ----
async function loadDrawer() {
  const ws = WS_LIST.find(w => w.id === CURRENT_WS);
  $("#drawerWs").textContent = ws ? `· ${ws.name}` : "";
  try {
    const path = CURRENT_WS ? `/requirements?workspace_id=${CURRENT_WS}` : `/requirements`;
    const d = await (await apiFetch(path)).json();
    renderDrawer(d.requirements || []);
  } catch (e) { $("#drawerReqs").innerHTML = `<div class="empty">加载失败</div>`; }
  // 「操作记录」tab 同步刷新
  try {
    if (CURRENT_WS) {
      const md = await (await apiFetch(`/workspaces/${CURRENT_WS}/messages`)).json();
      renderMsgLog(md.messages || []);
    }
  } catch (e) {}
}
function renderDrawer(reqs) {
  const box = $("#drawerReqs");
  if (!reqs.length) { box.innerHTML = `<div class="empty">暂无需求。在作战室点开任意运维 Agent 工作台，分析一条告警即可登记到这里。</div>`; return; }
  box.innerHTML = reqs.map(req => `
    <div class="req-card st-${req.status}">
      <div class="req-head"><span class="req-id">${req.id}</span><span class="req-badge ${req.status}">${statusLabel(req.status)}</span></div>
      <div class="req-meta">需要工具: <code>${req.needed_tool || "—"}</code></div>
      <div class="req-title">${req.title || ""}</div>
    </div>`).join("");
}

function renderMsgLog(msgs) {
  const box = $("#drawerLog");
  if (!msgs.length) { box.innerHTML = `<div class="empty">暂无操作记录。在作战室点开 Agent 工作台，运行分析 / 造工具后，产出会自动写回这里。</div>`; return; }
  const ICO = { diagnose: "🛰️", build: "🛠️", gap: "🔴", info: "ℹ️" };
  box.innerHTML = msgs.map(m => `
    <div class="msglog-item">
      <div class="ml-head"><span class="ml-ico">${ICO[m.kind] || "ℹ️"}</span><span class="ml-kind">${m.kind}</span><span class="ml-time">${m.ts}</span></div>
      <div class="ml-sum">${escapeHtml(m.summary)}</div>
      <div class="ml-meta">${escapeHtml(m.agent_name || m.agent_id || "")}</div>
    </div>`).join("");
}

// 消息栏双 tab 切换
$$(".drawer-tab").forEach(b => b.onclick = () => {
  $$(".drawer-tab").forEach(x => x.classList.toggle("active", x === b));
  const t = b.dataset.tab;
  $("#drawerReqs").style.display = t === "req" ? "flex" : "none";
  $("#drawerLog").style.display = t === "log" ? "flex" : "none";
});

// 服务端鉴权状态探测：若要求 Token 但本地未填，弹横幅提示
async function checkAuth() {
  try {
    const d = await apiFetch("/auth/status").then(r => r.json());
    const banner = document.getElementById("authBanner");
    if (d.auth_required && !JWT && !TOKEN) banner.style.display = "flex";
    else banner.style.display = "none";
    // 服务端支持 JWT 且本地有凭据 → 校验并恢复会话
    if (d.jwt_enabled && JWT) refreshMe();
  } catch (e) {}
}

// ================= 登录 / 注册 / 登出（JWT） =================
let loginMode = "login";   // login | register

function renderAuthArea() {
  const box = document.getElementById("authArea");
  if (!box) return;
  if (USER) {
    box.innerHTML = `<span class="user-chip" title="已登录">👤 ${USER.username}${USER.is_admin ? " · admin" : ""}</span>`
      + `<button class="ghost-btn sm" id="logoutBtn">登出</button>`;
    box.querySelector("#logoutBtn").onclick = logout;
  } else {
    box.innerHTML = `<button class="ghost-btn" id="loginBtn">🔐 登录</button>`;
    box.querySelector("#loginBtn").onclick = () => openLogin("");
  }
}

function openLogin(msg) {
  const m = document.getElementById("loginModal");
  if (!m) return;
  document.getElementById("loginMsg").textContent = msg || "";
  m.classList.add("open");
  setTimeout(() => { const i = document.getElementById("loginUser"); if (i) i.focus(); }, 50);
}

async function refreshMe() {
  try {
    const r = await fetch(API + "/auth/me", { headers: { Authorization: "Bearer " + JWT } });
    if (r.ok) {
      USER = await r.json();
      renderAuthArea();
    } else if (r.status === 401) {
      setJwt("", null);   // JWT 过期/无效，清除
    }
  } catch (e) {}
}

async function submitLogin() {
  const u = (document.getElementById("loginUser").value || "").trim();
  const p = document.getElementById("loginPass").value || "";
  const msgEl = document.getElementById("loginMsg");
  if (!u || !p) { msgEl.textContent = "请输入用户名和密码"; return; }
  if (loginMode === "register" && p.length < 8) { msgEl.textContent = "注册密码至少 8 位"; return; }
  msgEl.textContent = "请求中…";
  _suppressLoginPrompt = true;
  try {
    const path = loginMode === "login" ? "/auth/login" : "/auth/register";
    const r = await fetch(API + path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: p }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.token) {
      setJwt(d.token, d.user || { username: u, is_admin: false });
      document.getElementById("loginModal").classList.remove("open");
      document.getElementById("loginPass").value = "";
      msgEl.textContent = "";
      checkAuth();
    } else {
      msgEl.textContent = d.detail || (loginMode === "login" ? "用户名或密码错误" : "注册失败（用户名可能已存在）");
    }
  } catch (e) {
    msgEl.textContent = "网络错误，请确认后端已启动";
  } finally {
    _suppressLoginPrompt = false;
  }
}

function logout() {
  setJwt("", null);
  checkAuth();
}

function syncLoginTabs() {
  document.querySelectorAll("#loginTabs .seg-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === loginMode));
  document.getElementById("loginTitle").textContent = loginMode === "login" ? "🔐 登录" : "🆕 注册账号";
  document.getElementById("loginSubmit").textContent = loginMode === "login" ? "登录" : "注册并登录";
  document.getElementById("loginMsg").textContent = "";
}
document.querySelectorAll("#loginTabs .seg-btn").forEach(b =>
  b.onclick = () => { loginMode = b.dataset.tab; syncLoginTabs(); });
document.getElementById("loginSubmit").onclick = submitLogin;
document.getElementById("loginPass").addEventListener("keydown", (e) => { if (e.key === "Enter") submitLogin(); });
document.getElementById("loginGuest").onclick = () => {
  document.getElementById("loginModal").classList.remove("open");
};
renderAuthArea();

// 工作台产出写回消息栏「操作记录」
async function postWorkbenchMessage(wsId, agentId, kind, summary, detail) {
  if (!wsId) return;
  try {
    await apiFetch(`/workspaces/${wsId}/messages`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_id: agentId, kind, summary, detail: detail || null }),
    });
    if (document.getElementById("msgDrawer").classList.contains("open")) loadDrawer();
  } catch (e) {}
}

// 业务域悬浮卡（hover 显示 Agent 数 / 待处理需求数）
function showWsHover(el, wsId) {
  const ws = WS_LIST.find(w => w.id === wsId);
  if (!ws) return;
  const card = document.getElementById("wsHover");
  const ops = ws.ops || [], dev = ws.dev || [];
  const pending = ws.pending || 0;
  card.innerHTML = `<div class="wsh-title">${escapeHtml(ws.name)}</div>
    <div class="wsh-row"><span>派发模式</span><b>${ws.mode === "auto" ? "⚡ 自动" : "✋ 手动"}</b></div>
    <div class="wsh-row"><span>Agent 总数</span><b>${ws.agent_count || (ops.length + dev.length)}</b></div>
    <div class="wsh-row"><span>运维 / 研发</span><b>${ops.length} / ${dev.length}</b></div>
    <div class="wsh-row"><span>待处理需求</span><b style="color:${pending ? "var(--warn)" : "var(--ok)"}">${pending}</b></div>`;
  const rect = el.getBoundingClientRect();
  card.style.left = rect.left + "px";
  card.style.top = (rect.bottom + 8) + "px";
  card.style.display = "block";
}
function hideWsHover() { const c = document.getElementById("wsHover"); if (c) c.style.display = "none"; }

$("#toggleDrawer").onclick = () => {
  const d = document.getElementById("msgDrawer");
  d.classList.toggle("open");
  if (d.classList.contains("open")) loadDrawer();
};
$("#drawerClose").onclick = () => document.getElementById("msgDrawer").classList.remove("open");

// ---- 删除业务域 ----
async function deleteWorkspace() {
  if (!CURRENT_WS) return;
  const ws = WS_LIST.find(w => w.id === CURRENT_WS);
  const name = ws ? ws.name : CURRENT_WS;
  if (!confirm(`确认删除业务域「${name}」？\n该域下的所有运维 / 研发 Agent 将一并删除，且不可恢复。`)) return;
  try {
    const r = await apiFetch(`/workspaces/${CURRENT_WS}`, { method: "DELETE" });
    if (!r.ok) { const e = await r.json().catch(() => ({})); alert("删除失败：" + (e.detail || r.status)); return; }
    alert(`已删除业务域「${name}」`);
    CURRENT_WS = null;
    await loadWorkspaces();
  } catch (e) { alert("删除失败：" + e.message); }
}
$("#wsDelete").onclick = deleteWorkspace;

// 状态灯定时刷新（与后端 registry 实时 status 同步）
setInterval(refreshWarStatuses, 2500);

