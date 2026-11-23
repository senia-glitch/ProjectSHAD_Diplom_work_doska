/* Общие помощники интерфейса (паттерны my-design-system): el, icon, ibtn, toast, тема, хранилище. */
"use strict";

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("aria-")) node.setAttribute(k, String(v));
    else if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v; // только константы-значки
    else if (k === "value") node.value = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

const _svg = (inner) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
const ICONS = {
  pen: _svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>'),
  eraser: _svg('<path d="M7 21h13"/><path d="M5.6 15.6 14.8 6.4a2 2 0 0 1 2.8 0l2.4 2.4a2 2 0 0 1 0 2.8L11 20.6H7.4L4.2 17.4a1.3 1.3 0 0 1 0-1.8z"/><path d="M9 12.2l5.2 5.2"/>'),
  line: _svg('<path d="M5 19L19 5"/>'),
  rect: _svg('<rect x="4" y="6" width="16" height="12" rx="1.5"/>'),
  ellipse: _svg('<ellipse cx="12" cy="12" rx="9" ry="6.5"/>'),
  hand: _svg('<path d="M18 11V6a2 2 0 0 0-4 0v5"/><path d="M14 10V4a2 2 0 0 0-4 0v6"/><path d="M10 10.5V6a2 2 0 0 0-4 0v8"/><path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.9-6-2.4l-3.6-3.6a2 2 0 0 1 2.8-2.8L7 15"/>'),
  undo: _svg('<path d="M9 14L4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>'),
  redo: _svg('<path d="M15 14l5-5-5-5"/><path d="M20 9H9a5 5 0 0 0 0 10h3"/>'),
  trash: _svg('<path d="M3 6h18"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'),
  download: _svg('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5M12 15V3"/>'),
  fit: _svg('<path d="M3 8V5a2 2 0 0 1 2-2h3M16 3h3a2 2 0 0 1 2 2v3M21 16v3a2 2 0 0 1-2 2h-3M8 21H5a2 2 0 0 1-2-2v-3"/>'),
  zoomin: _svg('<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3M11 8v6M8 11h6"/>'),
  zoomout: _svg('<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3M8 11h6"/>'),
  link: _svg('<path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1.5 1.5"/><path d="M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1.5-1.5"/>'),
  user: _svg('<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>'),
  users: _svg('<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>'),
  open: _svg('<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6M10 14L21 3"/>'),
  x: _svg('<path d="M18 6L6 18M6 6l12 12"/>'),
  add: _svg('<path d="M12 5v14M5 12h14"/>'),
  home: _svg('<path d="M3 10.5L12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>'),
  sun: _svg('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
  moon: _svg('<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>'),
};

const icon = (name) => el("span", { class: "icon", "data-icon": name, html: ICONS[name] });

function ibtn({ icon: name, tip, onclick, danger = false, on = false, bordered = false, id, tipPos }) {
  return el("button", {
    type: "button", id, class: `ibtn${danger ? " danger" : ""}${on ? " on" : ""}${bordered ? " bordered" : ""}`,
    "data-tip": tip, "data-tip-pos": tipPos, "aria-label": tip, onclick,
  }, icon(name));
}

function toast(message, kind = "") {
  let box = document.getElementById("toasts");
  if (!box) {
    box = el("div", { id: "toasts", class: "toasts", role: "status", "aria-live": "polite" });
    document.body.append(box);
  }
  const t = el("div", { class: `toast ${kind}`, text: message });
  box.append(t);
  setTimeout(() => t.remove(), kind === "error" ? 6000 : 3500);
}

const store = {
  get(key, fallback = null) {
    try { const v = localStorage.getItem("doska." + key); return v === null ? fallback : JSON.parse(v); }
    catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem("doska." + key, JSON.stringify(value)); } catch { /* квота */ } },
};

/* crypto.randomUUID есть только в защищённом контексте (https/localhost); в LAN по http его нет. */
function randomKey(len = 20) {
  const abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-";
  const bytes = crypto.getRandomValues(new Uint8Array(len));
  return Array.from(bytes, (b) => abc[b & 63]).join("");
}

function authorKey() {
  let key = store.get("author");
  if (typeof key !== "string" || !/^[A-Za-z0-9_-]{8,40}$/.test(key)) { key = randomKey(24); store.set("author", key); }
  return key;
}

function rememberBoard(board) {
  const list = (store.get("recent", []) || []).filter((b) => b && b.id !== board.id);
  list.unshift({ id: board.id, title: board.title || "", ts: Date.now() });
  store.set("recent", list.slice(0, 20));
}

function themeButton() {
  const btn = ibtn({ icon: "sun", tip: "Тема", tipPos: "left" });
  const sync = () => {
    const light = document.documentElement.dataset.theme === "light";
    btn.replaceChildren(icon(light ? "moon" : "sun"));
    const label = light ? "Тёмная тема" : "Светлая тема";
    btn.setAttribute("aria-label", label); btn.dataset.tip = label;
  };
  btn.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("doska.theme", next); } catch { /* ignore */ }
    sync();
  });
  sync();
  return btn;
}

/* Копирование ссылки: Clipboard API работает только в защищённом контексте, иначе — execCommand. */
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return true; } catch { /* ниже запасной путь */ }
  }
  const area = el("textarea", { class: "offscreen", readonly: true, "aria-hidden": "true" });
  area.value = text;
  document.body.append(area);
  area.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  area.remove();
  return ok;
}

function formatDate(ts) {
  try { return new Date(ts).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); }
  catch { return ""; }
}
