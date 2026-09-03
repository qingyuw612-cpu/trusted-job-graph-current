const PRODUCT_CONFIG = Object.freeze({
  name: "知途",
  shortName: "知途",
  englishName: "talent graph agent",
  version: "",
  tagline: "看懂岗位变化，找到下一步",
});

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  }[char]));
}

function fmtNumber(value, digits = 0) {
  const num = Number(value || 0);
  return num.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function fmtPercent(value, digits = 1) {
  const num = Number(value || 0);
  return `${fmtNumber(num * 100, digits)}%`;
}

async function request(path, options = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(path, {...options, signal: controller.signal});
  } catch (error) {
    if (error.name === "AbortError") throw new Error("请求超时，请检查服务是否在线");
    if (error.name === "TypeError" && /fetch/i.test(error.message || "")) {
      throw new Error("无法连接简历分析服务，请确认 8000 端口服务已启动");
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

async function getJSON(path, options = {}) {
  const response = await request(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || data.error || data.message || "请求失败");
  }
  return data;
}

async function postJSON(path, payload, timeoutMs = 15000) {
  const response = await request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }, timeoutMs);
  const contentType = response.headers.get("Content-Type") || "";
  const data = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const message = typeof data === "string" ? data : (data.detail || data.error || data.message);
    throw new Error(message || "请求失败");
  }
  return data;
}

async function postFile(path, file) {
  const form = new FormData();
  form.append("file", file);
  const response = await request(path, { method: "POST", body: form }, 45000);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || data.error || data.message || "上传失败");
  }
  return data;
}

function setStatus(id, text, kind = "") {
  const el = $(id);
  if (!el) return;
  el.className = `status-pill ${kind ? `is-${kind}` : ""}`;
  el.innerHTML = `<i class="status-dot"></i><span>${esc(text)}</span>`;
}

function emptyState(text) {
  return `<div class="empty">${esc(text)}</div>`;
}

function tagClass(value) {
  const key = String(value || "").toLowerCase();
  if (key.includes("high") || key.includes("高")) return "high";
  if (key.includes("medium") || key.includes("中")) return "medium";
  if (key.includes("low") || key.includes("低")) return "low";
  return "";
}

function readPrefs(key, fallback = {}) {
  try {
    const raw = localStorage.getItem(`talentgraph:${key}`);
    return raw ? {...fallback, ...JSON.parse(raw)} : {...fallback};
  } catch {
    return {...fallback};
  }
}

function writePrefs(key, value) {
  try {
    localStorage.setItem(`talentgraph:${key}`, JSON.stringify(value));
  } catch {
    // 隐私模式或 file:// 页面可能禁用 localStorage；不影响当前会话。
  }
}

function navShell(active, title, subtitle, statusId = "pageStatus") {
  const items = [
    ["index.html", "工作台", "HOME", "index"],
    ["panorama.html", "岗位图谱", "MAP", "panorama"],
    ["emerging-roles.html", "新岗位涌现", "EMERGING", "emerging"],
    ["emerging-roles.html?view=updates", "岗位能力更新", "EVOLUTION", "updates"],
    ["resume-match.html", "人岗差距分析", "MATCH", "resume"],
  ];
  const nav = items.map(([href, label, , key]) => (
    `<a class="${active === key ? "active" : ""}" ${active === key ? 'aria-current="page"' : ""} href="${href}"><span>${label}</span></a>`
  )).join("");
  return `
    <a class="skip-link" href="#mainContent">跳到主要内容</a>
    <section class="product-shell">
      <aside class="product-rail">
        <div class="brand-lockup">
          <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
          <div>
            <div class="brand-subtitle">${esc(PRODUCT_CONFIG.englishName)}</div>
            <div class="brand-title">${esc(PRODUCT_CONFIG.name)}</div>
          </div>
        </div>
        <nav class="module-tabs" aria-label="主导航">${nav}</nav>
        <div class="rail-bottom">
          <a class="rail-maintenance-entry ${active === "maintenance" ? "active" : ""}" ${active === "maintenance" ? 'aria-current="page"' : ""} href="maintenance.html">
            <span class="maintenance-entry-icon" aria-hidden="true"><i></i></span>
            <span class="maintenance-entry-copy">
              <small>DATA PIPELINE</small>
              <b>数据维护监控</b>
              <em>采集 · 归一化 · 发布</em>
            </span>
            <span class="maintenance-entry-arrow" aria-hidden="true">→</span>
          </a>
          <div class="rail-note">
            <b>${esc(PRODUCT_CONFIG.tagline)}</b>
            <span>从市场变化到个人选择，给你更清楚的下一步。</span>
          </div>
        </div>
      </aside>
      <section class="main">
      <header class="product-header">
        <div class="header-title">
          <h1>${esc(title)}</h1>
          <p>${esc(subtitle)}</p>
        </div>
        <div id="${statusId}" class="status-pill" role="status" aria-live="polite"><i class="status-dot"></i><span>就绪</span></div>
      </header>
  `;
}

function closeShell() {
  return "</section></section>";
}
