const state = {
  overview: null,
  settings: null,
  status: "all",
  offset: 0,
  page: "overview",
  loading: false,
  toastTimer: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function iconPath(type) {
  const paths = {
    calendar: '<path d="M5 5h14v15H5zM8 3v4m8-4v4M5 10h14" stroke="currentColor" stroke-width="1.6"/>',
    clipboard: '<rect x="8" y="8" width="11" height="11" stroke="currentColor" stroke-width="1.6"/><path d="M16 8V5H5v11h3" stroke="currentColor" stroke-width="1.6"/>',
  };
  return paths[type] || paths.calendar;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.method && options.method !== "GET") {
    headers.set("x-admin-request", "1");
  }
  if (options.body) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  const isJson = (response.headers.get("content-type") || "").includes("application/json");
  const data = isJson ? await response.json() : null;
  if (!response.ok) {
    const error = new Error(data?.error?.message || `请求失败，状态 ${response.status}`);
    error.status = response.status;
    error.code = data?.error?.code;
    throw error;
  }
  return data;
}

function toast(message, type = "success") {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", type === "error");
  el.classList.add("visible");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => el.classList.remove("visible"), 2800);
}

function formatNumber(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
}

function compactNumber(value) {
  const number = Number(value || 0);
  if (number >= 1000000) return `${(number / 1000000).toFixed(2)}m`;
  if (number >= 10000) return `${(number / 1000).toFixed(1)}k`;
  return formatNumber(number);
}

function formatDuration(value) {
  if (value === null || value === undefined) return "—";
  const ms = Number(value);
  if (ms >= 1000) return `${(ms / 1000).toFixed(ms >= 10000 ? 1 : 2)}s`;
  return `${Math.round(ms)}ms`;
}

function formatTime(timestamp) {
  return new Date(timestamp * 1000).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function localDateValue(date = new Date()) {
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return shifted.toISOString().slice(0, 10);
}

function showLogin() {
  $("#app-view").hidden = true;
  $("#login-view").hidden = false;
  setTimeout(() => $("#login-password")?.focus(), 20);
}

async function showApp() {
  $("#login-view").hidden = true;
  $("#app-view").hidden = false;
  if (!$("#report-date").value) $("#report-date").value = localDateValue();
  await Promise.allSettled([loadOverview(), loadSettings()]);
}

async function bootstrap() {
  try {
    const session = await api("/api/admin/session");
    if (session.authenticated) {
      await showApp();
    } else {
      showLogin();
    }
  } catch (error) {
    showLogin();
    $("#login-error").textContent = error.message;
  }
}

async function login(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("button[type=submit]", form);
  const errorEl = $("#login-error");
  errorEl.textContent = "";
  button.disabled = true;
  try {
    await api("/api/admin/login", {
      method: "POST",
      body: JSON.stringify({ password: $("#login-password").value }),
    });
    form.reset();
    await showApp();
  } catch (error) {
    errorEl.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function logout() {
  try {
    await api("/api/admin/logout", { method: "POST" });
  } finally {
    state.overview = null;
    state.settings = null;
    showLogin();
  }
}

function setPage(page) {
  state.page = page;
  $$(".page").forEach((el) => el.classList.toggle("active", el.id === `page-${page}`));
  $$(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.page === page));
  $("#page-kicker").textContent = page === "settings" ? "接口设置" : "运行概览";
  document.title = page === "settings" ? "接口设置 · Mercury" : "运行概览 · Mercury";
  if (page === "settings") loadSettings();
  if (page === "overview") loadOverview();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function loadOverview() {
  if (state.loading) return;
  state.loading = true;
  const refresh = $("#refresh-button");
  refresh.disabled = true;
  refresh.classList.add("loading");
  $("#freshness-text").textContent = "正在读取";
  if (!state.overview) {
    ["input-tokens", "output-tokens", "avg-latency", "request-count"].forEach((id) => $(`#${id}`).classList.add("skeleton"));
  }
  const params = new URLSearchParams({
    date: $("#report-date").value,
    status: state.status,
    offset: String(state.offset),
  });
  try {
    const data = await api(`/api/admin/overview?${params}`);
    state.overview = data;
    renderOverview(data);
    $("#freshness-text").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;
  } catch (error) {
    if (error.status === 401) {
      showLogin();
    } else {
      toast(error.message, "error");
      $("#freshness-text").textContent = "读取失败";
    }
  } finally {
    state.loading = false;
    refresh.disabled = false;
    refresh.classList.remove("loading");
    ["input-tokens", "output-tokens", "avg-latency", "request-count"].forEach((id) => $(`#${id}`).classList.remove("skeleton"));
  }
}

function renderOverview(data) {
  const summary = data.summary;
  $("#input-tokens").textContent = compactNumber(summary.input_tokens);
  $("#input-tokens").title = `${formatNumber(summary.input_tokens)} 个估算 token`;
  $("#output-tokens").textContent = compactNumber(summary.output_tokens);
  $("#output-tokens").title = `${formatNumber(summary.output_tokens)} 个估算 token`;
  $("#avg-latency").textContent = formatDuration(summary.avg_latency_ms);
  $("#avg-latency").title = summary.avg_latency_ms === null ? "今天还没有成功请求" : `平均 ${Math.round(summary.avg_latency_ms)} 毫秒`;
  $("#request-count").textContent = compactNumber(summary.requests);
  const totalTokens = summary.input_tokens + summary.output_tokens;
  $("#input-share").textContent = totalTokens ? `占总量 ${Math.round(summary.input_tokens / totalTokens * 100)}%` : "暂无用量";
  $("#reasoning-tokens").textContent = summary.reasoning_tokens ? `思考 ${compactNumber(summary.reasoning_tokens)}` : "含思考内容";
  $("#first-token-note").textContent = summary.avg_first_token_ms === null ? "首字时间暂无" : `首字 ${formatDuration(summary.avg_first_token_ms)}`;
  $("#request-breakdown").textContent = `${summary.success} 成功 · ${summary.errors} 失败${summary.cancelled ? ` · ${summary.cancelled} 取消` : ""}`;
  const rate = summary.requests ? Math.round(summary.success / summary.requests * 100) : null;
  const successEl = $("#success-rate");
  successEl.textContent = rate === null ? "—" : `成功率 ${rate}%`;
  successEl.classList.toggle("warn", rate !== null && rate < 90);
  $("#timezone-label").textContent = data.timezone === "Asia/Shanghai" ? "UTC+8" : data.timezone;
  $("#timezone-setting").textContent = data.timezone;
  $("#date-line").textContent = data.date === localDateValue() ? `今天 · ${new Date().toLocaleDateString("zh-CN", { month: "long", day: "numeric" })}` : `${data.date} · 历史记录`;
  const dot = $("#sidebar-status-dot");
  dot.classList.toggle("offline", !data.browser_ready);
  $("#sidebar-status").textContent = data.browser_ready ? "服务运行中" : "控制台已连接";
  $("#sidebar-status-note").textContent = data.browser_ready ? "浏览器会话已就绪" : "浏览器将在首次请求时启动";
  renderChart(data.hourly);
  renderRequests(data);
}

function renderChart(hourly) {
  const root = $("#usage-chart");
  const maximum = Math.max(1, ...hourly.flatMap((item) => [item.input_tokens, item.output_tokens]));
  if (!hourly.some((item) => item.requests)) {
    root.innerHTML = '<div class="chart-empty">还没有用量，发送一次对话后会在这里形成曲线。</div>';
    return;
  }
  const width = 900;
  const height = 220;
  const left = 42;
  const right = 8;
  const top = 12;
  const bottom = 24;
  const plotHeight = height - top - bottom;
  const step = (width - left - right) / 24;
  const barWidth = Math.max(4, step * 0.26);
  const grid = [0, 0.25, 0.5, 0.75, 1].map((ratio) => {
    const y = top + plotHeight * (1 - ratio);
    return `<line class="chart-grid-line" x1="${left}" x2="${width - right}" y1="${y}" y2="${y}"/><text class="chart-axis" x="0" y="${y + 3}">${compactNumber(Math.round(maximum * ratio))}</text>`;
  }).join("");
  const bars = hourly.map((item, index) => {
    const x = left + step * index + step / 2;
    const inputHeight = item.input_tokens / maximum * plotHeight;
    const outputHeight = item.output_tokens / maximum * plotHeight;
    const label = index % 4 === 0 ? `<text class="chart-axis" text-anchor="middle" x="${x}" y="${height - 5}">${String(index).padStart(2, "0")}</text>` : "";
    return `<g><title>${String(index).padStart(2, "0")}:00 输入 ${formatNumber(item.input_tokens)}，输出 ${formatNumber(item.output_tokens)}，共 ${item.requests} 次请求</title><rect class="chart-bar" x="${x - barWidth - 1}" y="${top + plotHeight - inputHeight}" width="${barWidth}" height="${Math.max(item.input_tokens ? 1 : 0, inputHeight)}" fill="#173a27"/><rect class="chart-bar" x="${x + 1}" y="${top + plotHeight - outputHeight}" width="${barWidth}" height="${Math.max(item.output_tokens ? 1 : 0, outputHeight)}" fill="#c75e2d"/>${label}</g>`;
  }).join("");
  root.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="按小时统计的输入和输出 token"><title>24 小时输入与输出 token 用量</title>${grid}${bars}</svg>`;
}

function statusLabel(status) {
  return { success: "成功", error: "失败", cancelled: "已取消" }[status] || status;
}

function renderRequests(data) {
  const body = $("#request-rows");
  const empty = $("#requests-empty");
  body.innerHTML = "";
  empty.hidden = data.requests.length > 0;
  data.requests.forEach((row) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="time">${formatTime(row.created_at)}</td>
      <td class="endpoint">${row.endpoint === "/v1/responses" ? "responses" : "chat"}</td>
      <td><span class="status status-${row.status}" title="${row.error_code || ""}">${statusLabel(row.status)}</span></td>
      <td class="numeric">${formatNumber(row.input_tokens)}</td>
      <td class="numeric">${formatNumber(row.output_tokens)}</td>
      <td class="numeric">${formatDuration(row.latency_ms)}</td>
      <td class="numeric">${formatDuration(row.first_token_ms)}</td>
      <td>${row.stream ? "流式" : "普通"}</td>
    `;
    body.appendChild(tr);
  });
  const start = data.total ? data.offset + 1 : 0;
  const end = Math.min(data.offset + 20, data.total);
  $("#page-info").textContent = `${start}–${end} / ${data.total}`;
  $("#prev-page").disabled = data.offset <= 0;
  $("#next-page").disabled = data.offset + 20 >= data.total;
}

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const input = document.createElement("textarea");
  input.value = text;
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.appendChild(input);
  input.select();
  document.execCommand("copy");
  input.remove();
}

async function copyEndpoint() {
  await copyText(`${location.origin}/v1`);
  toast("接口地址已复制");
}

async function loadSettings() {
  try {
    const data = await api("/api/admin/settings");
    state.settings = data;
    if (!["api-key", "input"].includes(document.activeElement?.id)) {
      $("#api-key").value = data.api_key_preview;
      $("#api-key").type = "password";
    }
    $("#reasoning-effort").value = data.reasoning_effort;
    $("#web-search").checked = data.web_search;
    $("#upstream-timeout").value = data.upstream_timeout;
    $("#base-url").textContent = `${location.origin}/v1`;
    $("#endpoint-address").textContent = location.host;
  } catch (error) {
    if (error.status === 401) showLogin();
    else toast(error.message, "error");
  }
}

async function revealApiKey() {
  const input = $("#api-key");
  if (input.type === "password" && input.value.includes("…")) {
    try {
      const data = await api("/api/admin/api-key");
      input.value = data.api_key;
    } catch (error) {
      toast(error.message, "error");
      return;
    }
  }
  input.type = input.type === "password" ? "text" : "password";
  $$(`[data-toggle-password="api-key"]`).forEach((button) => {
    button.setAttribute("aria-label", input.type === "password" ? "显示接口密钥" : "隐藏接口密钥");
  });
}

function newKey() {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-";
  const bytes = crypto.getRandomValues(new Uint8Array(42));
  const value = "sk-" + [...bytes].map((byte) => alphabet[byte % alphabet.length]).join("");
  const input = $("#api-key");
  input.type = "text";
  input.value = value;
  input.focus();
  toast("新密钥已生成，点击保存后生效");
}

async function saveKey() {
  const input = $("#api-key");
  const value = input.value.trim();
  if (value.includes("…")) {
    toast("密钥没有变化。需要修改时，请先重新生成或粘贴新密钥。", "error");
    return;
  }
  if (value.length < 16) {
    input.setAttribute("aria-invalid", "true");
    toast("密钥至少需要 16 个字符", "error");
    return;
  }
  input.removeAttribute("aria-invalid");
  const button = $("#save-key");
  button.disabled = true;
  try {
    await api("/api/admin/settings", {
      method: "PUT",
      body: JSON.stringify({
        api_key: value,
        reasoning_effort: $("#reasoning-effort").value,
        web_search: $("#web-search").checked,
        upstream_timeout: Number($("#upstream-timeout").value),
      }),
    });
    await loadSettings();
    toast("接口密钥已更新，旧密钥已经失效");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function saveSettings() {
  const button = $("#save-settings");
  button.disabled = true;
  try {
    await api("/api/admin/settings", {
      method: "PUT",
      body: JSON.stringify({
        reasoning_effort: $("#reasoning-effort").value,
        web_search: $("#web-search").checked,
        upstream_timeout: Number($("#upstream-timeout").value),
      }),
    });
    $("#settings-save-note").textContent = "已保存";
    setTimeout(() => { $("#settings-save-note").textContent = ""; }, 2200);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function copyApiKey() {
  try {
    const data = await api("/api/admin/api-key");
    await copyText(data.api_key);
    toast("接口密钥已复制");
  } catch (error) {
    toast(error.message, "error");
  }
}

function initializeEvents() {
  $("#login-form").addEventListener("submit", login);
  $("#logout-button").addEventListener("click", logout);
  $("#refresh-button").addEventListener("click", loadOverview);
  $("#report-date").addEventListener("change", () => {
    state.offset = 0;
    state.overview = null;
    loadOverview();
  });
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => setPage(button.dataset.page)));
  $$("[data-page-jump]").forEach((button) => button.addEventListener("click", () => setPage(button.dataset.pageJump)));
  $$("[data-status]").forEach((button) => button.addEventListener("click", () => {
    state.status = button.dataset.status;
    state.offset = 0;
    $$("[data-status]").forEach((item) => item.classList.toggle("active", item === button));
    loadOverview();
  }));
  $("#prev-page").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - 20);
    loadOverview();
  });
  $("#next-page").addEventListener("click", () => {
    state.offset += 20;
    loadOverview();
  });
  $("#save-key").addEventListener("click", saveKey);
  $("#generate-key").addEventListener("click", newKey);
  $("#copy-key").addEventListener("click", copyApiKey);
  $("#save-settings").addEventListener("click", saveSettings);
  $("#copy-base-url").addEventListener("click", copyEndpoint);
  $$("[data-copy-endpoint]").forEach((button) => button.addEventListener("click", copyEndpoint));
  $$("[data-copy-path]").forEach((button) => button.addEventListener("click", async () => {
    await copyText(`${location.origin}${button.dataset.copyPath}`);
    toast("接口地址已复制");
  }));
  $$("[data-toggle-password]").forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.togglePassword === "api-key") {
      revealApiKey();
    } else {
      const input = $(`#${button.dataset.togglePassword}`);
      input.type = input.type === "password" ? "text" : "password";
    }
  }));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.page === "overview" && $("#app-view").hidden === false) loadOverview();
  });
  setInterval(() => {
    if (!document.hidden && state.page === "overview" && $("#app-view").hidden === false) loadOverview();
  }, 30000);
}

initializeEvents();
bootstrap();
