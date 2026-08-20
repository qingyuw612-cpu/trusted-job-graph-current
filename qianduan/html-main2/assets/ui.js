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

async function getJSON(path) {
  const response = await fetch(path);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || data.error || data.message || "请求失败");
  }
  return data;
}

async function postJSON(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
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
  const response = await fetch(path, { method: "POST", body: form });
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

function navShell(active, title, subtitle, statusId = "pageStatus") {
  const items = [
    ["index.html", "工作台", "Workbench", "index"],
    ["panorama.html", "岗位图谱", "Graph", "panorama"],
    ["emerging-roles.html", "新岗位发现", "Discovery", "emerging"],
    ["resume-match.html", "简历匹配", "Match", "resume"],
  ];
  const nav = items.map(([href, label, code, key]) => (
    `<a class="${active === key ? "active" : ""}" ${active === key ? 'aria-current="page"' : ""} href="${href}"><span>${label}</span><small>${code}</small></a>`
  )).join("");
  return `
    <a class="skip-link" href="#mainContent">跳到主要内容</a>
    <section class="product-shell">
      <aside class="product-rail">
        <div class="brand-lockup">
          <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
          <div>
            <div class="brand-subtitle">TALENT INTELLIGENCE GRAPH</div>
            <div class="brand-title">信息技术岗位能力知识图谱</div>
          </div>
        </div>
        <nav class="module-tabs" aria-label="主导航">${nav}</nav>
        <div class="rail-note">
          <b>证据中台</b>
          <span>岗位、能力点和 JD 原文保持可追溯。</span>
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
