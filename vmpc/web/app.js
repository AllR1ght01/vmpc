const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

let appState = null;
let liveModels = [];
let busy = false;
let stream = null;
let toastTimer = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body && typeof options.body !== "string" ? JSON.stringify(options.body) : options.body,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[ch]);
}

function copyButton(value, className = "copy-button", label = "Копировать") {
  return `<button class="${className}" data-copy="${escapeHtml(encodeURIComponent(value))}">${label}</button>`;
}

function inlineMarkdown(raw) {
  const placeholders = [];
  const keep = html => {
    const token = `\u0000${placeholders.length}\u0000`;
    placeholders.push(html);
    return token;
  };
  let text = String(raw ?? "");
  text = text.replace(/`([^`\n]+)`/g, (_, code) => keep(`<code class="inline-code">${escapeHtml(code)}</code>`));
  text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/gi, (_, label, url) => keep(linkPill(label, url)));
  text = text.replace(/https?:\/\/[^\s<]+/gi, url => keep(linkPill(url.replace(/[.,;!?]+$/, ""), url.replace(/[.,;!?]+$/, ""))));
  text = escapeHtml(text)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
  return text.replace(/\u0000(\d+)\u0000/g, (_, index) => placeholders[Number(index)]);
}

function linkPill(label, url) {
  const safeUrl = /^https?:\/\//i.test(url) ? url : "#";
  return `<span class="link-pill"><a href="${escapeHtml(safeUrl)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>${copyButton(url, "link-copy", "⧉")}</span>`;
}

function renderTextBlocks(text) {
  const lines = text.replace(/\r/g, "").split("\n");
  let html = "";
  let paragraph = [];
  let list = null;
  const flushParagraph = () => {
    if (paragraph.length) html += `<p>${inlineMarkdown(paragraph.join("\n")).replace(/\n/g, "<br>")}</p>`;
    paragraph = [];
  };
  const closeList = () => {
    if (list) html += `</${list}>`;
    list = null;
  };
  for (const line of lines) {
    const heading = line.match(/^(#{1,3})\s+(.+)/);
    const bullet = line.match(/^\s*[-*]\s+(.+)/);
    const number = line.match(/^\s*\d+[.)]\s+(.+)/);
    if (!line.trim()) { flushParagraph(); closeList(); continue; }
    if (heading) { flushParagraph(); closeList(); const level = heading[1].length; html += `<h${level}>${inlineMarkdown(heading[2])}</h${level}>`; continue; }
    if (bullet || number) {
      flushParagraph();
      const wanted = bullet ? "ul" : "ol";
      if (list !== wanted) { closeList(); list = wanted; html += `<${list}>`; }
      html += `<li>${inlineMarkdown((bullet || number)[1])}</li>`;
      continue;
    }
    closeList();
    paragraph.push(line);
  }
  flushParagraph(); closeList();
  return html;
}

function renderMarkdown(markdown) {
  const source = String(markdown ?? "");
  let output = "";
  let cursor = 0;
  const fence = /```([\w.+-]*)[^\S\r\n]*\r?\n?([\s\S]*?)```/g;
  let match;
  while ((match = fence.exec(source))) {
    output += renderTextBlocks(source.slice(cursor, match.index));
    const language = match[1] || "код";
    const code = match[2].replace(/\n$/, "");
    output += `<div class="code-card"><div class="code-head"><span>${escapeHtml(language)}</span>${copyButton(code)}</div><pre><code>${escapeHtml(code)}</code></pre></div>`;
    cursor = fence.lastIndex;
  }
  output += renderTextBlocks(source.slice(cursor));
  return output;
}

function showToast(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", error);
  toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add("hidden"), 3400);
}

function setBusy(value) {
  busy = value;
  $("#sendButton").disabled = value;
  $("#stopButton").classList.toggle("hidden", !value);
  $("#modelButton").disabled = value;
  $("#modeButton").disabled = value;
  $("#headerStatus").textContent = value ? "Модель отвечает…" : "Готов к работе";
}

function scrollBottom(immediate = false) {
  const area = $("#messageScroll");
  const apply = () => { area.scrollTop = area.scrollHeight; };
  if (immediate) apply();
  requestAnimationFrame(() => requestAnimationFrame(apply));
}

function renderState(state, renderConversation = true) {
  appState = state;
  renderSidebar();
  renderEndpoint();
  renderModes();
  renderProviders();
  renderAttachments(state.chat?.context || []);
  if (renderConversation) renderChat(state.chat);
  setBusy(Boolean(state.busy));
}

function renderSidebar() {
  const query = $("#chatSearch").value.trim().toLowerCase();
  const list = $("#chatList");
  list.innerHTML = "";
  for (const chat of appState.chats.filter(item => !query || item.title.toLowerCase().includes(query))) {
    const button = document.createElement("button");
    button.className = `chat-item${chat.id === appState.chat?.id ? " active" : ""}`;
    button.innerHTML = `<strong>${escapeHtml(chat.title)}</strong><small>${chat.turns}</small>`;
    button.addEventListener("click", () => openChat(chat.id));
    list.append(button);
  }
}

function renderEndpoint() {
  const provider = appState.providers.find(item => item.name === appState.activeProvider);
  $("#endpointName").textContent = provider?.name || "Endpoint не настроен";
  $("#endpointModel").textContent = provider?.model || "Открыть настройки";
  $("#endpointDot").classList.toggle("online", Boolean(provider));
  $("#modelButtonText").textContent = provider?.model || "Выберите модель";
  $("#modelStatus").classList.toggle("online", Boolean(provider));
  if (provider && !liveModels.length) loadModels(true);
}

function renderModes() {
  const active = appState.modes.find(item => item.name === appState.activeMode) || appState.modes[0];
  $("#modeBadge").textContent = active.label;
  $("#modeButtonText").textContent = active.label;
  $("#modeMenu").innerHTML = appState.modes.map(mode => `<button class="choice ${mode.name === active.name ? "active" : ""}" data-mode="${escapeHtml(mode.name)}"><span class="checkmark">${mode.name === active.name ? "✓" : ""}</span><span><strong>${escapeHtml(mode.label)}</strong><small>${escapeHtml(mode.description)}</small></span></button>`).join("");
}

function renderChat(chat) {
  $("#chatTitle").textContent = chat?.title || "Новый чат";
  const messages = $("#messages");
  messages.innerHTML = "";
  stream = null;
  const rows = chat?.messages || [];
  $("#welcome").classList.toggle("hidden", rows.length > 0);
  for (const message of rows) appendMessage(message.role, message.content, message.model);
  scrollBottom(true);
  detectQuickReplies(rows.at(-1));
}

function appendMessage(role, content, model = "") {
  const node = document.createElement("article");
  if (role === "user") {
    node.className = "message user";
    node.innerHTML = `<div class="user-bubble">${escapeHtml(content)}</div>`;
  } else {
    node.className = "message assistant";
    node.innerHTML = `<div class="avatar">✦</div><div class="message-content"><div class="message-head"><strong>${escapeHtml(model || "Модель")}</strong><span>ответ</span></div><div class="thinking-slot"></div><div class="message-body">${renderMarkdown(content)}</div><div class="message-actions">${copyButton(content, "text-action", "⧉ Копировать")}</div></div>`;
  }
  $("#messages").append(node);
  return node;
}

function startLocalTurn(text, model) {
  $("#welcome").classList.add("hidden");
  $("#quickReplies").classList.add("hidden");
  appendMessage("user", text);
  const node = appendMessage("assistant", "", model);
  const body = node.querySelector(".message-body");
  body.innerHTML = '<span class="cursor"></span>';
  stream = { node, body, thinking: node.querySelector(".thinking-slot"), answer: "", reasoning: "", model };
  scrollBottom();
}

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onmessage = event => {
    const data = JSON.parse(event.data);
    if (data.type === "turn_start") {
      if (!stream) startLocalTurn(data.message, data.model);
    } else if (data.type === "answer" && stream) {
      stream.answer += data.text;
      stream.body.innerHTML = `${renderMarkdown(stream.answer)}<span class="cursor"></span>`;
      scrollBottom();
    } else if (data.type === "reasoning" && stream) {
      stream.reasoning += data.text;
      stream.thinking.innerHTML = `<details class="thinking" open><summary>Ход рассуждений</summary>${escapeHtml(stream.reasoning)}</details>`;
      scrollBottom();
    } else if (data.type === "turn_done") {
      appState.chat = data.chat;
      appState.chats = data.chats;
      renderSidebar();
      renderChat(data.chat);
      setBusy(false);
      if (data.stopped) showToast("Генерация остановлена");
    } else if (data.type === "turn_error") {
      if (stream) stream.body.innerHTML = `<p style="color:var(--danger)">Не удалось получить ответ: ${escapeHtml(data.message)}</p>${data.hint ? `<p style="color:var(--muted)">${escapeHtml(data.hint)}</p>` : ""}`;
      stream = null;
      setBusy(false);
      showToast(data.message, true);
    }
  };
  source.onerror = () => $("#headerStatus").textContent = "Переподключение…";
}

async function sendMessage(text = $("#prompt").value) {
  text = text.trim();
  if (!text || busy) return;
  const provider = appState.providers.find(item => item.name === appState.activeProvider);
  if (!provider) { openSettings(); showToast("Добавьте API endpoint", true); return; }
  $("#prompt").value = "";
  resizePrompt();
  setBusy(true);
  startLocalTurn(text, provider.model);
  try {
    await api("/api/send", { method: "POST", body: { message: text } });
  } catch (error) {
    setBusy(false); stream = null; showToast(error.message, true);
    const state = await api("/api/state"); renderState(state);
  }
}

async function openChat(id) {
  if (busy) return showToast("Сначала остановите текущий ответ", true);
  try {
    const chat = await api("/api/chat/open", { method: "POST", body: { id } });
    appState.chat = chat;
    renderChat(chat);
    renderSidebar();
  } catch (error) { showToast(error.message, true); }
}

async function newChat() {
  if (busy) return showToast("Сначала остановите текущий ответ", true);
  try {
    const chat = await api("/api/chat/new", { method: "POST", body: {} });
    appState.chat = chat; renderChat(chat); renderSidebar(); $("#prompt").focus();
  } catch (error) { showToast(error.message, true); }
}

function toggleMenu(selector) {
  const target = $(selector);
  $$(".popover").forEach(item => { if (item !== target) item.classList.add("hidden"); });
  target.classList.toggle("hidden");
}

async function chooseMode(name) {
  try {
    const state = await api("/api/select", { method: "POST", body: { mode: name } });
    renderState(state, false); $("#modeMenu").classList.add("hidden");
  } catch (error) { showToast(error.message, true); }
}

async function loadModels(silent = false) {
  const provider = appState?.activeProvider;
  if (!provider) return;
  $("#modelStatus").classList.add("loading");
  if (!silent) $("#headerStatus").textContent = "Запрашиваю модели…";
  try {
    const result = await api(`/api/models?provider=${encodeURIComponent(provider)}`);
    liveModels = result.models;
    renderModels();
    if (!silent) showToast(result.source === "endpoint" ? `Получено моделей: ${liveModels.length}` : "Endpoint не отдал каталог — показаны сохранённые модели");
  } catch (error) { showToast(error.message, true); }
  finally {
    $("#modelStatus").classList.remove("loading");
    $("#modelStatus").classList.add("online");
    if (!busy) $("#headerStatus").textContent = "Готов к работе";
  }
}

function renderModels() {
  const query = $("#modelSearch").value.trim().toLowerCase();
  const active = appState?.activeModel;
  $("#modelList").innerHTML = liveModels.filter(model => !query || model.toLowerCase().includes(query)).map(model => `<button class="model-row ${model === active ? "active" : ""}" data-model="${escapeHtml(model)}"><span class="status-dot online"></span><span>${escapeHtml(model)}</span><small>${model === active ? "выбрана" : "доступна"}</small></button>`).join("") || '<div style="padding:14px;color:var(--muted)">Модели не найдены</div>';
}

async function chooseModel(model) {
  try {
    const state = await api("/api/select", { method: "POST", body: { model } });
    renderState(state, false); renderModels(); $("#modelMenu").classList.add("hidden");
  } catch (error) { showToast(error.message, true); }
}

async function runProbe() {
  $("#confirmModal").classList.add("hidden");
  $("#modelStatus").classList.add("loading");
  $("#headerStatus").textContent = "Проверяю модели…";
  try {
    const result = await api("/api/models/probe", { method: "POST", body: { provider: appState.activeProvider } });
    liveModels = result.models;
    renderModels();
    showToast(`Проверено ${result.checked}, доступно ${result.available.length}`);
  } catch (error) { showToast(error.message, true); }
  finally { $("#modelStatus").classList.remove("loading"); $("#headerStatus").textContent = "Готов к работе"; }
}

function renderAttachments(paths) {
  $("#attachments").innerHTML = paths.map(path => `<span class="attachment">▱ ${escapeHtml(path.split(/[\\/]/).at(-1))}</span>`).join("");
}

async function pickContext(kind) {
  $("#attachMenu").classList.add("hidden");
  $("#headerStatus").textContent = "Выберите контекст…";
  try {
    const result = await api("/api/context/pick", { method: "POST", body: { kind } });
    appState.chat.context = result.context;
    renderAttachments(result.context);
    if (result.errors?.length) showToast(result.errors[0], true);
  } catch (error) { showToast(error.message, true); }
  finally { $("#headerStatus").textContent = "Готов к работе"; }
}

function detectQuickReplies(message) {
  const text = message?.role === "assistant" ? message.content.trim() : "";
  const asksChoice = /(\by\s*\/\s*n\b|\byes\s*\/\s*no\b|да\s*\/\s*нет|\[y\/n\]|\[yes\/no\])\s*[?.!]*$/i.test(text);
  $("#quickReplies").classList.toggle("hidden", !asksChoice);
}

function renderProviders() {
  if (!appState) return;
  $("#providerList").innerHTML = appState.providers.map(provider => `<button type="button" class="${provider.name === appState.activeProvider ? "active" : ""}" data-provider="${escapeHtml(provider.name)}">${escapeHtml(provider.name)}</button>`).join("");
}

function fillProvider(provider = null) {
  $("#oldName").value = provider?.name || "";
  $("#providerName").value = provider?.name || "";
  $("#providerWire").value = provider?.wire || "openai";
  $("#providerUrl").value = provider?.baseUrl || "";
  $("#providerDefaultModel").value = provider?.model || "";
  $("#providerKeyType").value = provider?.keyType || "static-api-key";
  $("#providerAuth").value = provider?.authScheme || "bearer";
  $("#providerKey").value = "";
  $("#providerReasoning").checked = Boolean(provider?.reasoning);
}

function openSettings() {
  renderProviders();
  fillProvider(appState.providers.find(item => item.name === appState.activeProvider));
  $("#settingsModal").classList.remove("hidden");
}

async function saveProvider(event) {
  event.preventDefault();
  const data = {
    oldName: $("#oldName").value,
    name: $("#providerName").value,
    wire: $("#providerWire").value,
    baseUrl: $("#providerUrl").value,
    model: $("#providerDefaultModel").value,
    keyType: $("#providerKeyType").value,
    keyValue: $("#providerKey").value,
    authScheme: $("#providerAuth").value,
    reasoning: $("#providerReasoning").checked,
  };
  try {
    const state = await api("/api/provider/save", { method: "POST", body: data });
    liveModels = [];
    renderState(state, false);
    $("#settingsModal").classList.add("hidden");
    loadModels(true);
    showToast("Endpoint сохранён");
  } catch (error) { showToast(error.message, true); }
}

function resizePrompt() {
  const prompt = $("#prompt");
  prompt.style.height = "auto";
  prompt.style.height = `${Math.min(prompt.scrollHeight, 190)}px`;
}

function installWheelScroller(element, speed = 1) {
  const scroll = event => {
    const maximum = Math.max(0, element.scrollHeight - element.clientHeight);
    if (!maximum) return;
    let delta = Number(event.deltaY || 0);
    if (!delta && "wheelDelta" in event) delta = -Number(event.wheelDelta || 0);
    if (!delta && "detail" in event) delta = Number(event.detail || 0) * 16;
    if (event.deltaMode === 1) delta *= 18;
    if (event.deltaMode === 2) delta *= element.clientHeight;
    delta *= speed;
    element.scrollTop = Math.max(0, Math.min(maximum, element.scrollTop + delta));
    event.preventDefault();
    event.stopPropagation();
  };
  // Capture mode runs before buttons, code blocks and message cards can
  // consume the event. This fixes wheels that Chromium routes to a child.
  element.addEventListener("wheel", scroll, { passive: false, capture: true });
  element.addEventListener("mousewheel", scroll, { passive: false, capture: true });
}

function exportChat() {
  const chat = appState?.chat;
  if (!chat?.messages?.length) return showToast("В чате пока нечего экспортировать");
  const markdown = [`# ${chat.title}`, ""];
  for (const message of chat.messages) {
    markdown.push(`## ${message.role === "user" ? "Вы" : (message.model || "Модель")}`, "", message.content, "");
  }
  const blob = new Blob([markdown.join("\n")], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${chat.title.replace(/[\\/:*?"<>|]+/g, "-") || "vmpc-chat"}.md`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

document.addEventListener("click", event => {
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    navigator.clipboard.writeText(decodeURIComponent(copy.dataset.copy)).then(() => { const old = copy.textContent; copy.textContent = "Готово"; setTimeout(() => copy.textContent = old, 1200); });
    return;
  }
  const model = event.target.closest("[data-model]"); if (model) return chooseModel(model.dataset.model);
  const mode = event.target.closest("[data-mode]"); if (mode) return chooseMode(mode.dataset.mode);
  const provider = event.target.closest("[data-provider]"); if (provider) return fillProvider(appState.providers.find(item => item.name === provider.dataset.provider));
  const context = event.target.closest("[data-kind]"); if (context) return pickContext(context.dataset.kind);
  const suggestion = event.target.closest("[data-prompt]"); if (suggestion) { $("#prompt").value = suggestion.dataset.prompt; resizePrompt(); $("#prompt").focus(); }
  const reply = event.target.closest("[data-reply]"); if (reply) sendMessage(reply.dataset.reply);
  if (!event.target.closest(".popover") && !event.target.closest("#attachButton,#modeButton,#modelButton")) $$(".popover").forEach(item => item.classList.add("hidden"));
});

$("#newChat").addEventListener("click", newChat);
$("#sendButton").addEventListener("click", () => sendMessage());
$("#stopButton").addEventListener("click", () => api("/api/stop", { method: "POST", body: {} }));
$("#prompt").addEventListener("input", resizePrompt);
$("#prompt").addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendMessage(); } });
$("#chatSearch").addEventListener("input", renderSidebar);
installWheelScroller($("#chatList"));
installWheelScroller($("#messageScroll"), 2.5);
$("#attachButton").addEventListener("click", () => toggleMenu("#attachMenu"));
$("#modeButton").addEventListener("click", () => toggleMenu("#modeMenu"));
$("#modelButton").addEventListener("click", () => { toggleMenu("#modelMenu"); renderModels(); });
$("#refreshModels").addEventListener("click", () => loadModels());
$("#modelSearch").addEventListener("input", renderModels);
$("#probeModels").addEventListener("click", () => $("#confirmModal").classList.remove("hidden"));
$("#probeNo").addEventListener("click", () => $("#confirmModal").classList.add("hidden"));
$("#probeYes").addEventListener("click", runProbe);
$("#endpointButton").addEventListener("click", openSettings);
$("#settingsButton").addEventListener("click", openSettings);
$("#exportChat").addEventListener("click", exportChat);
$("#addProvider").addEventListener("click", () => fillProvider());
$("#providerForm").addEventListener("submit", saveProvider);
$$('.modal-close').forEach(button => button.addEventListener("click", () => button.closest(".modal").classList.add("hidden")));
$("#sidebarToggle").addEventListener("click", () => $(".sidebar").classList.toggle("open"));
window.addEventListener("pagehide", () => navigator.sendBeacon("/api/close", "{}"));

(async function init() {
  try {
    const state = await api("/api/state");
    renderState(state);
    connectEvents();
    $("#prompt").focus();
  } catch (error) {
    showToast(`Не удалось запустить интерфейс: ${error.message}`, true);
  }
})();
