"use strict";
/* Совместная доска: холст, инструменты, панорама/масштаб и синхронизация через WebSocket.
   Координаты штрихов — мировые; экран = мир * scale + offset. */

const BOARD_ID = decodeURIComponent(location.pathname.split("/")[2] || "");
const PAPER = "#fbfcfe";
const COLORS = ["#1f2430", "#e5484d", "#f76b15", "#e5a000", "#30a46c", "#0090ff", "#7177f8", "#d6409f"];
const WIDTHS = [2, 4, 8, 16];
const TOOLS = [
  { id: "pen", icon: "pen", tip: "Карандаш (P)", key: "p" },
  { id: "line", icon: "line", tip: "Линия (L)", key: "l" },
  { id: "rect", icon: "rect", tip: "Прямоугольник (R)", key: "r" },
  { id: "ellipse", icon: "ellipse", tip: "Эллипс (O)", key: "o" },
  { id: "eraser", icon: "eraser", tip: "Ластик (E)", key: "e" },
  { id: "hand", icon: "hand", tip: "Перемещение (H, пробел)", key: "h" },
];
const MAX_PEN_VALUES = 3998;       // сервер принимает до 8000 чисел; длинный штрих делится на части
const FLUSH_MS = 40;               // как часто отправлять живые точки
const CURSOR_MS = 50;

const $ = (id) => document.getElementById(id);
const stage = $("stage");
const canvas = $("canvas");
const ctx = canvas.getContext("2d");
const committed = document.createElement("canvas");
const cctx = committed.getContext("2d");

const state = {
  tool: store.get("tool", "pen"),
  color: store.get("color", COLORS[0]),
  width: store.get("width", 4),
  view: { x: 0, y: 0, scale: 1 },
  dpr: 1,
  strokes: [],                 // подтверждённые и собственные отправленные, в порядке сервера
  index: new Map(),            // id -> штрих
  pending: new Map(),          // свои отправленные без подтверждения: id -> штрих
  live: new Map(),             // чужие незаконченные: "peer:id" -> штрих
  mine: null,                  // свой текущий штрих
  history: [],                 // свои id для отмены
  redo: [],
  peers: new Map(),            // id -> {name, color, cursor, node}
  me: null,
  chatIds: new Set(),
  unread: 0,
  importing: false,
  ready: false,
  dirty: true,
  committedDirty: true,
};
if (!TOOLS.some((t) => t.id === state.tool)) state.tool = "pen";

/* ------------------------------------------------------------ интерфейс */

const toolbar = $("toolbar");
const toolButtons = new Map();
const colorButtons = new Map();
const widthButtons = new Map();

function buildToolbar() {
  const tools = el("div", { class: "tb-group" });
  for (const t of TOOLS) {
    const b = ibtn({ icon: t.icon, tip: t.tip, tipPos: "right", onclick: () => setTool(t.id) });
    b.setAttribute("aria-pressed", "false");
    toolButtons.set(t.id, b);
    tools.append(b);
  }
  const colors = el("div", { class: "tb-group tb-colors", role: "group", "aria-label": "Цвет" });
  for (const c of COLORS) {
    const b = el("button", { type: "button", class: "swatch", "aria-label": "Цвет " + c, "data-tip": c, "data-tip-pos": "right",
      onclick: () => setColor(c) });
    b.style.setProperty("--swatch", c);
    colorButtons.set(c, b);
    colors.append(b);
  }
  const custom = el("input", { type: "color", class: "swatch-custom", "aria-label": "Свой цвет", value: state.color });
  custom.addEventListener("input", () => setColor(custom.value));
  colors.append(el("label", { class: "swatch swatch-picker", "data-tip": "Свой цвет", "data-tip-pos": "right" }, custom));

  const widths = el("div", { class: "tb-group", role: "group", "aria-label": "Толщина" });
  for (const w of WIDTHS) {
    const b = el("button", { type: "button", class: "ibtn width-btn", "aria-label": "Толщина " + w, "data-tip": "Толщина " + w,
      "data-tip-pos": "right", onclick: () => setWidth(w) }, el("span", { class: "width-dot" }));
    b.firstChild.style.setProperty("--w", Math.min(w, 14) + "px");
    widthButtons.set(w, b);
    widths.append(b);
  }
  const actions = el("div", { class: "tb-group" },
    ibtn({ icon: "undo", tip: "Отменить (Ctrl+Z)", tipPos: "right", id: "undo-btn", onclick: undo }),
    ibtn({ icon: "redo", tip: "Повторить (Ctrl+Shift+Z)", tipPos: "right", id: "redo-btn", onclick: redo }),
    ibtn({ icon: "fit", tip: "Показать всё (F)", tipPos: "right", onclick: fitToContent }),
    ibtn({ icon: "trash", tip: "Очистить доску", tipPos: "right", danger: true, onclick: askClear }));
  toolbar.append(tools, colors, widths, actions);
  syncToolbar();
}

function syncToolbar() {
  for (const [id, b] of toolButtons) { b.classList.toggle("on", id === state.tool); b.setAttribute("aria-pressed", String(id === state.tool)); }
  for (const [c, b] of colorButtons) b.setAttribute("aria-pressed", String(c === state.color));
  for (const [w, b] of widthButtons) { b.classList.toggle("on", w === state.width); b.setAttribute("aria-pressed", String(w === state.width)); }
  toolbar.style.setProperty("--current", state.color);
  stage.dataset.tool = state.tool;
  $("undo-btn").disabled = !state.history.some((id) => state.index.has(id));
  $("redo-btn").disabled = state.redo.length === 0;
}

function setTool(t) { state.tool = t; store.set("tool", t); syncToolbar(); }
function setColor(c) { state.color = c.toLowerCase(); store.set("color", state.color); if (state.tool === "eraser" || state.tool === "hand") state.tool = "pen"; syncToolbar(); }
function setWidth(w) { state.width = w; store.set("width", w); syncToolbar(); }

/* Участники в шапке */
function initials(name) { return (name || "?").trim().slice(0, 1).toUpperCase() || "?"; }

function renderPeers() {
  const box = $("peers");
  const list = [];
  if (state.me) list.push({ ...state.me, self: true });
  for (const [id, p] of state.peers) list.push({ id, ...p });
  const shown = list.slice(0, 6);
  box.replaceChildren(...shown.map((p) => {
    const label = p.self ? `${p.name} (вы) — изменить имя` : p.name;
    const node = el(p.self ? "button" : "span", { class: "avatar" + (p.self ? " self" : ""), "data-tip": label,
      "aria-label": label, type: p.self ? "button" : null, onclick: p.self ? askName : null, text: initials(p.name) });
    node.style.setProperty("--peer", p.color);
    return node;
  }), ...(list.length > shown.length ? [el("span", { class: "avatar more", text: "+" + (list.length - shown.length),
    "data-tip": list.slice(6).map((p) => p.name).join(", ") })] : []));
}

/* Курсоры других участников */
function peerCursorNode(peer) {
  if (!peer.node) {
    peer.node = el("div", { class: "cursor" }, el("span", { class: "cursor-dot" }), el("span", { class: "cursor-name" }));
    $("cursors").append(peer.node);
  }
  peer.node.style.setProperty("--peer", peer.color);
  peer.node.lastChild.textContent = peer.name;
  return peer.node;
}

function placeCursors() {
  for (const peer of state.peers.values()) {
    if (!peer.cursor) { if (peer.node) peer.node.hidden = true; continue; }
    const node = peerCursorNode(peer);
    node.hidden = false;
    const sx = peer.cursor.x * state.view.scale + state.view.x;
    const sy = peer.cursor.y * state.view.scale + state.view.y;
    node.style.transform = `translate(${sx}px, ${sy}px)`;
  }
}

/* ------------------------------------------------------------ отрисовка */

function resize() {
  const rect = stage.getBoundingClientRect();
  state.dpr = Math.min(window.devicePixelRatio || 1, 2.5);
  for (const c of [canvas, committed]) {
    c.width = Math.max(1, Math.round(rect.width * state.dpr));
    c.height = Math.max(1, Math.round(rect.height * state.dpr));
  }
  canvas.style.width = rect.width + "px";
  canvas.style.height = rect.height + "px";
  state.committedDirty = true;
  invalidate();
}

function applyView(c) {
  const { x, y, scale } = state.view;
  c.setTransform(state.dpr * scale, 0, 0, state.dpr * scale, state.dpr * x, state.dpr * y);
}

function drawStroke(c, s) {
  const p = s.pts;
  if (!p || p.length < 2) return;
  c.save();
  c.globalCompositeOperation = s.tool === "eraser" ? "destination-out" : "source-over";
  c.strokeStyle = c.fillStyle = s.tool === "eraser" ? "#000" : s.color;
  c.lineWidth = s.width;
  c.lineCap = "round";
  c.lineJoin = "round";
  c.beginPath();
  if (s.tool === "pen" || s.tool === "eraser") {
    if (p.length === 2) {
      c.arc(p[0], p[1], s.width / 2, 0, Math.PI * 2);
      c.fill();
      c.restore();
      return;
    }
    c.moveTo(p[0], p[1]);
    // Сглаживание: кривые через середины соседних точек.
    for (let i = 2; i < p.length - 2; i += 2) {
      const mx = (p[i] + p[i + 2]) / 2, my = (p[i + 1] + p[i + 3]) / 2;
      c.quadraticCurveTo(p[i], p[i + 1], mx, my);
    }
    c.lineTo(p[p.length - 2], p[p.length - 1]);
  } else if (p.length >= 4) {
    const [x0, y0, x1, y1] = p;
    if (s.tool === "line") { c.moveTo(x0, y0); c.lineTo(x1, y1); }
    else if (s.tool === "rect") c.rect(Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0));
    else if (s.tool === "ellipse") c.ellipse((x0 + x1) / 2, (y0 + y1) / 2, Math.abs(x1 - x0) / 2, Math.abs(y1 - y0) / 2, 0, 0, Math.PI * 2);
  }
  c.stroke();
  c.restore();
}

function rebuildCommitted() {
  cctx.setTransform(1, 0, 0, 1, 0, 0);
  cctx.clearRect(0, 0, committed.width, committed.height);
  applyView(cctx);
  for (const s of state.strokes) drawStroke(cctx, s);
  state.committedDirty = false;
}

function appendCommitted(s) {
  if (state.committedDirty) return;
  applyView(cctx);
  drawStroke(cctx, s);
}

function invalidate() {
  if (state.dirty === "scheduled") return;
  state.dirty = "scheduled";
  requestAnimationFrame(frame);
}

function frame() {
  state.dirty = false;
  if (state.committedDirty) rebuildCommitted();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(committed, 0, 0);
  applyView(ctx);
  for (const s of state.live.values()) drawStroke(ctx, s);
  if (state.mine) drawStroke(ctx, state.mine);
  updateGrid();
  placeCursors();
}

function updateGrid() {
  const step = 24 * state.view.scale;
  const size = step < 8 ? step * 4 : step;
  stage.style.backgroundSize = `${size}px ${size}px`;
  stage.style.backgroundPosition = `${state.view.x}px ${state.view.y}px`;
  $("zoom-value").textContent = Math.round(state.view.scale * 100) + "%";
}

/* ------------------------------------------------------------ модель штрихов */

function addStroke(s) {
  if (state.index.has(s.id)) return false;
  state.strokes.push(s);
  state.index.set(s.id, s);
  appendCommitted(s);
  invalidate();
  return true;
}

function removeStroke(id) {
  const s = state.index.get(id);
  if (!s) return null;
  state.index.delete(id);
  state.strokes.splice(state.strokes.indexOf(s), 1);
  state.committedDirty = true;
  invalidate();
  return s;
}

function clearStrokes() {
  state.strokes = [];
  state.index.clear();
  state.pending.clear();
  state.history = [];
  state.redo = [];
  state.committedDirty = true;
  invalidate();
  syncToolbar();
}

function commitMine(s) {
  addStroke(s);
  state.pending.set(s.id, s);
  state.history.push(s.id);
  if (state.history.length > 500) state.history.shift();
  send({ t: "stroke", stroke: s });
}

function undo() {
  while (state.history.length) {
    const id = state.history.pop();
    const s = removeStroke(id);
    if (!s) continue;
    state.pending.delete(id);
    state.redo.push(s);
    send({ t: "remove", id });
    break;
  }
  syncToolbar();
}

function redo() {
  const s = state.redo.pop();
  if (!s) return;
  commitMine({ ...s, id: randomKey() });
  syncToolbar();
}

/* ------------------------------------------------------------ ввод */

const pointers = new Map();
let gesture = null;            // {type:'draw'|'pan'|'pinch', ...}
let spaceDown = false;
let lastCursorSent = 0;
let flushTimer = null;

function toWorld(clientX, clientY) {
  const r = canvas.getBoundingClientRect();
  return { x: (clientX - r.left - state.view.x) / state.view.scale, y: (clientY - r.top - state.view.y) / state.view.scale };
}
const round1 = (v) => Math.round(v * 10) / 10;

canvas.addEventListener("pointerdown", (e) => {
  if (!state.ready) return;
  try { canvas.setPointerCapture(e.pointerId); } catch { /* указатель уже недоступен */ }
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (pointers.size === 2) {           // второй палец: отменяем рисование, начинаем жест
    cancelMine();
    const [a, b] = [...pointers.values()];
    gesture = { type: "pinch", dist: Math.hypot(a.x - b.x, a.y - b.y), mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 } };
    return;
  }
  if (pointers.size > 2) return;
  const panning = state.tool === "hand" || spaceDown || e.button === 1 || e.button === 2;
  if (panning) {
    gesture = { type: "pan", x: e.clientX, y: e.clientY };
    stage.classList.add("panning");
    return;
  }
  if (e.button !== 0) return;
  const p = toWorld(e.clientX, e.clientY);
  const tool = state.tool;
  state.mine = { id: randomKey(), tool, color: tool === "eraser" ? "#000000" : state.color,
    width: tool === "eraser" ? Math.max(12, state.width * 3) : state.width,
    pts: tool === "pen" || tool === "eraser" ? [round1(p.x), round1(p.y)] : [round1(p.x), round1(p.y), round1(p.x), round1(p.y)] };
  state.mine.sent = 0;
  gesture = { type: "draw", shift: e.shiftKey };
  flushTimer = setInterval(flushLive, FLUSH_MS);
  invalidate();
});

canvas.addEventListener("pointermove", (e) => {
  if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  sendCursor(e);
  if (!gesture) return;
  if (gesture.type === "pinch" && pointers.size >= 2) {
    const [a, b] = [...pointers.values()];
    const dist = Math.hypot(a.x - b.x, a.y - b.y);
    const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    state.view.x += mid.x - gesture.mid.x;
    state.view.y += mid.y - gesture.mid.y;
    zoomAt(mid.x, mid.y, dist / (gesture.dist || dist));
    gesture.dist = dist; gesture.mid = mid;
    return;
  }
  if (gesture.type === "pan") {
    state.view.x += e.clientX - gesture.x;
    state.view.y += e.clientY - gesture.y;
    gesture.x = e.clientX; gesture.y = e.clientY;
    state.committedDirty = true;
    invalidate();
    return;
  }
  if (gesture.type === "draw" && state.mine) {
    const s = state.mine;
    const events = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
    for (const ev of events.length ? events : [e]) {
      const p = toWorld(ev.clientX, ev.clientY);
      if (s.tool === "pen" || s.tool === "eraser") {
        const lx = s.pts[s.pts.length - 2], ly = s.pts[s.pts.length - 1];
        if (Math.hypot(p.x - lx, p.y - ly) * state.view.scale < 1.5) continue;
        s.pts.push(round1(p.x), round1(p.y));
        if (s.pts.length >= MAX_PEN_VALUES) splitMine();
      } else {
        let x = p.x, y = p.y;
        if (e.shiftKey) {             // Shift: квадрат/круг или линия под углом 45°
          const dx = x - s.pts[0], dy = y - s.pts[1];
          if (s.tool === "line") {
            const angle = Math.round(Math.atan2(dy, dx) / (Math.PI / 4)) * (Math.PI / 4);
            const len = Math.hypot(dx, dy);
            x = s.pts[0] + Math.cos(angle) * len; y = s.pts[1] + Math.sin(angle) * len;
          } else {
            const m = Math.max(Math.abs(dx), Math.abs(dy));
            x = s.pts[0] + Math.sign(dx || 1) * m; y = s.pts[1] + Math.sign(dy || 1) * m;
          }
        }
        s.pts[2] = round1(x); s.pts[3] = round1(y);
        s.changed = true;
      }
    }
    invalidate();
  }
});

function endPointer(e) {
  pointers.delete(e.pointerId);
  if (!gesture) return;
  if (gesture.type === "pinch") {
    if (pointers.size === 0) { gesture = null; state.committedDirty = true; invalidate(); }
    return;
  }
  if (gesture.type === "pan") { gesture = null; stage.classList.remove("panning"); return; }
  if (gesture.type === "draw") {
    gesture = null;
    finishMine(e.type === "pointercancel");
  }
}
canvas.addEventListener("pointerup", endPointer);
canvas.addEventListener("pointercancel", endPointer);
canvas.addEventListener("pointerleave", () => send({ t: "cursor", x: null, y: null }));
canvas.addEventListener("contextmenu", (e) => e.preventDefault());

function strokePayload(s) { return { id: s.id, tool: s.tool, color: s.color, width: s.width, pts: s.pts }; }

function flushLive() {
  const s = state.mine;
  if (!s) return;
  if (s.tool === "pen" || s.tool === "eraser") {
    if (s.pts.length <= s.sent) return;
    // Первый пакет — с начала; дальше только новые точки, у получателя они дописываются.
    send({ t: "draw", ...strokePayload(s), pts: s.pts.slice(s.sent), replace: false });
    s.sent = s.pts.length;
  } else if (s.changed) {
    s.changed = false;
    send({ t: "draw", ...strokePayload(s), replace: true });
  }
}

function finishMine(cancelled) {
  clearInterval(flushTimer);
  const s = state.mine;
  state.mine = null;
  if (!s) return;
  const degenerate = s.pts.length >= 4 && s.tool !== "pen" && s.tool !== "eraser"
    && Math.hypot(s.pts[2] - s.pts[0], s.pts[3] - s.pts[1]) < 1;
  if (cancelled || degenerate) {
    send({ t: "draw", ...strokePayload(s), pts: [], replace: true });
    invalidate();
    return;
  }
  state.redo = [];
  commitMine(strokePayload(s));
  syncToolbar();
}

function splitMine() {
  // Длинная линия делится на последовательные штрихи без разрыва.
  const s = state.mine;
  flushLive();
  const lastX = s.pts[s.pts.length - 2], lastY = s.pts[s.pts.length - 1];
  state.redo = [];
  commitMine(strokePayload(s));
  state.mine = { ...s, id: randomKey(), pts: [lastX, lastY], sent: 0 };
}

function cancelMine() {
  if (!state.mine) return;
  clearInterval(flushTimer);
  send({ t: "draw", ...strokePayload(state.mine), pts: [], replace: true });
  state.mine = null;
  gesture = null;
  invalidate();
}

function sendCursor(e) {
  const now = performance.now();
  if (now - lastCursorSent < CURSOR_MS) return;
  lastCursorSent = now;
  const p = toWorld(e.clientX, e.clientY);
  send({ t: "cursor", x: round1(p.x), y: round1(p.y) });
}

/* Масштаб и панорама */
function zoomAt(sx, sy, factor) {
  const r = canvas.getBoundingClientRect();
  const x = sx - r.left, y = sy - r.top;
  const scale = Math.min(8, Math.max(0.1, state.view.scale * factor));
  const k = scale / state.view.scale;
  state.view.x = x - (x - state.view.x) * k;
  state.view.y = y - (y - state.view.y) * k;
  state.view.scale = scale;
  state.committedDirty = true;
  invalidate();
}

stage.addEventListener("wheel", (e) => {
  e.preventDefault();
  if (e.ctrlKey || e.metaKey) {
    zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0025)));
  } else {
    const k = e.deltaMode === 1 ? 16 : 1;
    state.view.x -= (e.shiftKey ? e.deltaY : e.deltaX) * k;
    state.view.y -= (e.shiftKey ? 0 : e.deltaY) * k;
    state.committedDirty = true;
    invalidate();
  }
}, { passive: false });

function zoomCenter(factor) {
  const r = canvas.getBoundingClientRect();
  zoomAt(r.left + r.width / 2, r.top + r.height / 2, factor);
}

function resetView() {
  const r = canvas.getBoundingClientRect();
  state.view = { x: r.width / 2, y: r.height / 2, scale: 1 };
  state.committedDirty = true;
  invalidate();
}

function bounds(list) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const s of list) {
    if (s.tool === "eraser") continue;
    const pad = s.width / 2;
    for (let i = 0; i < s.pts.length; i += 2) {
      minX = Math.min(minX, s.pts[i] - pad); maxX = Math.max(maxX, s.pts[i] + pad);
      minY = Math.min(minY, s.pts[i + 1] - pad); maxY = Math.max(maxY, s.pts[i + 1] + pad);
    }
  }
  return minX === Infinity ? null : { minX, minY, maxX, maxY };
}

function fitToContent() {
  const b = bounds(state.strokes);
  if (!b) { resetView(); return; }
  const r = canvas.getBoundingClientRect();
  const margin = 48;
  // Вертикальная панель инструментов слева (на телефоне — снизу) не должна закрывать рисунок.
  const t = toolbar.getBoundingClientRect();
  const left = t.height > t.width ? Math.max(margin, t.right - r.left + 16) : margin;
  const bottom = t.width >= t.height ? Math.max(margin, r.bottom - t.top + 16) : margin;
  const w = r.width - left - margin, h = r.height - margin - bottom;
  const scale = Math.min(2, Math.max(0.1, Math.min(w / Math.max(1, b.maxX - b.minX), h / Math.max(1, b.maxY - b.minY))));
  state.view = { scale, x: left + w / 2 - ((b.minX + b.maxX) / 2) * scale,
    y: margin + h / 2 - ((b.minY + b.maxY) / 2) * scale };
  state.committedDirty = true;
  invalidate();
}

$("zoom-value").addEventListener("click", resetView);
$("zoombar").prepend(ibtn({ icon: "zoomout", tip: "Уменьшить (−)", onclick: () => zoomCenter(1 / 1.25) }));
$("zoombar").append(ibtn({ icon: "zoomin", tip: "Увеличить (+)", onclick: () => zoomCenter(1.25) }));

/* Клавиатура */
document.addEventListener("keydown", (e) => {
  const typing = e.target instanceof Element && e.target.closest("input, textarea, dialog[open]");
  if (typing) return;
  const mod = e.ctrlKey || e.metaKey;
  const k = e.key.toLowerCase();
  if (mod && (k === "z" || k === "я")) { e.preventDefault(); (e.shiftKey ? redo : undo)(); return; }
  if (mod && (k === "y" || k === "н")) { e.preventDefault(); redo(); return; }
  if (mod) return;
  if (e.code === "Space") { if (!spaceDown) { spaceDown = true; stage.classList.add("space"); } e.preventDefault(); return; }
  const tool = TOOLS.find((t) => t.key === e.key.toLowerCase() || ("Key" + t.key.toUpperCase()) === e.code);
  if (tool) { setTool(tool.id); return; }
  if (e.key === "+" || e.key === "=") zoomCenter(1.25);
  else if (e.key === "-" || e.key === "_") zoomCenter(1 / 1.25);
  else if (e.key === "0") resetView();
  else if (e.code === "KeyF") fitToContent();
});
document.addEventListener("keyup", (e) => {
  if (e.code === "Space") { spaceDown = false; stage.classList.remove("space"); }
});

/* ------------------------------------------------------------ экспорт и диалоги */

function exportPng() {
  const b = bounds(state.strokes);
  if (!b) { toast("Доска пуста"); return; }
  const pad = 32;
  const w = b.maxX - b.minX + pad * 2, h = b.maxY - b.minY + pad * 2;
  const k = Math.min(2, 8192 / w, 8192 / h);
  const ink = document.createElement("canvas");
  ink.width = Math.ceil(w * k); ink.height = Math.ceil(h * k);
  const ictx = ink.getContext("2d");
  ictx.setTransform(k, 0, 0, k, (pad - b.minX) * k, (pad - b.minY) * k);
  for (const s of state.strokes) drawStroke(ictx, s);
  const out = document.createElement("canvas");
  out.width = ink.width; out.height = ink.height;
  const octx = out.getContext("2d");
  octx.fillStyle = PAPER;
  octx.fillRect(0, 0, out.width, out.height);
  octx.drawImage(ink, 0, 0);
  out.toBlob((blob) => {
    if (!blob) { toast("Не удалось сохранить изображение", "error"); return; }
    saveBlob(blob, safeFileName($("board-title").textContent) + ".png");
  }, "image/png");
}

/* Меню «Файл»: выгрузка и загрузка доски */
const fileBtn = $("file-btn");
const fileMenu = $("file-menu");

function setMenu(open) {
  fileMenu.hidden = !open;
  fileBtn.setAttribute("aria-expanded", String(open));
  fileBtn.classList.toggle("on", open);
  if (open) fileMenu.querySelector("button").focus();
}
fileBtn.addEventListener("click", () => setMenu(fileMenu.hidden));
document.addEventListener("pointerdown", (e) => {
  if (!fileMenu.hidden && !(e.target instanceof Element && e.target.closest(".menu-wrap"))) setMenu(false);
});
fileMenu.addEventListener("keydown", (e) => {
  const items = [...fileMenu.querySelectorAll("button")];
  const i = items.indexOf(document.activeElement);
  if (e.key === "Escape") { setMenu(false); fileBtn.focus(); }
  else if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
});

$("menu-png").addEventListener("click", () => { setMenu(false); exportPng(); });

$("menu-export").addEventListener("click", async () => {
  setMenu(false);
  try {
    const response = await fetch(`/api/v1/boards/${encodeURIComponent(BOARD_ID)}/export`, { cache: "no-store" });
    if (!response.ok) throw new Error(await apiError(response, "Не удалось выгрузить доску"));
    const data = await response.json();
    saveBlob(new Blob([boardFileText(data)], { type: "application/json" }), safeFileName(data.title) + ".doska.json");
    toast(`Выгружено: ${data.strokes.length} ${strokesWord(data.strokes.length)}`, "ok");
  } catch (err) {
    toast(err.message === "Failed to fetch" ? "Сервер недоступен" : err.message, "error");
  }
});

$("menu-import").addEventListener("click", () => { setMenu(false); $("import-file").click(); });

$("import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  let data;
  try { data = await readBoardFile(file); } catch (err) { toast(err.message, "error"); return; }
  const n = data.strokes.length;
  $("import-summary").textContent = `${data.title ? "«" + data.title + "»" : file.name}: ${n} ${strokesWord(n)}. ` +
    "Добавить к текущему рисунку или заменить его?";
  const dlg = $("import-dialog");
  dlg.returnValue = "";
  dlg.showModal();
  dlg.addEventListener("close", async () => {
    const mode = dlg.returnValue;
    if (mode !== "append" && mode !== "replace") return;
    state.importing = true;
    try {
      const result = await importBoard(BOARD_ID, data, mode);
      toast(`Загружено: ${result.imported} ${strokesWord(result.imported)}`, "ok");
      if (state.ready) fitToContent();
      else toast("Нет связи: рисунок появится после переподключения");
    } catch (err) {
      toast(err.message === "Failed to fetch" ? "Сервер недоступен" : err.message, "error");
    } finally {
      state.importing = false;
    }
  }, { once: true });
});

function applyImport(msg) {
  if (msg.replace) {
    state.strokes = [];
    state.index.clear();
    state.pending.clear();
    state.history = [];
    state.redo = [];
    state.committedDirty = true;
  }
  for (const s of msg.strokes) addStroke(s);
  invalidate();
  syncToolbar();
  if (!state.importing) toast(`${msg.by} ${msg.replace ? "заменил доску" : "добавил рисунок"} из файла`);
}

/* ------------------------------------------------------------ чат */

const chat = $("chat");
const chatList = $("chat-list");
const chatInput = $("chat-input");
const chatBtn = $("chat-btn");
let lastChat = null;

function timeOf(iso) {
  try { return new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }); } catch { return ""; }
}

function isMine(m) { return Boolean(state.me && m.tag === state.me.tag); }

function chatNode(m, prev) {
  const mine = isMine(m);
  // Подряд идущие сообщения одного автора в пределах 3 минут — без повторной подписи.
  const grouped = prev && prev.tag === m.tag && prev.name === m.name && Date.parse(m.at) - Date.parse(prev.at) < 180000;
  const node = el("div", { class: "msg" + (mine ? " mine" : "") + (grouped ? " grouped" : "") },
    grouped ? null : el("div", { class: "msg-head" },
      el("span", { class: "msg-name", text: mine ? "Вы" : m.name }),
      el("time", { class: "msg-time", datetime: m.at, text: timeOf(m.at) })),
    el("div", { class: "msg-text", text: m.text }));
  node.style.setProperty("--peer", m.color);
  return node;
}

function nearBottom() { return chatList.scrollHeight - chatList.scrollTop - chatList.clientHeight < 48; }

function appendChat(m, count = true) {
  if (state.chatIds.has(m.id)) return;
  state.chatIds.add(m.id);
  const stick = nearBottom() || isMine(m);
  chatList.append(chatNode(m, lastChat));
  lastChat = m;
  $("chat-empty").hidden = true;
  if (stick) chatList.scrollTop = chatList.scrollHeight;
  if (count && !isMine(m) && chat.hidden) {
    state.unread += 1;
    syncChatBadge();
    toast(`${m.name}: ${m.text.length > 80 ? m.text.slice(0, 80) + "…" : m.text}`);
  }
}

function renderChat(list) {
  state.chatIds.clear();
  lastChat = null;
  chatList.replaceChildren();
  for (const m of list) appendChat(m, false);
  $("chat-empty").hidden = list.length > 0;
  chatList.scrollTop = chatList.scrollHeight;
}

function syncChatBadge() {
  const badge = $("chat-badge");
  badge.hidden = state.unread === 0;
  badge.textContent = state.unread > 99 ? "99+" : String(state.unread);
  chatBtn.setAttribute("aria-label", state.unread ? `Чат, непрочитанных: ${state.unread}` : "Чат");
}

function setChat(open) {
  chat.hidden = !open;
  document.body.classList.toggle("chat-open", open);
  chatBtn.classList.toggle("on", open);
  chatBtn.setAttribute("aria-expanded", String(open));
  store.set("chatOpen", open);
  if (open) {
    state.unread = 0;
    syncChatBadge();
    chatList.scrollTop = chatList.scrollHeight;
    if (window.matchMedia("(hover: hover)").matches) chatInput.focus();
  }
}

function growInput() {
  chatInput.style.height = "auto";
  chatInput.style.height = Math.min(chatInput.scrollHeight, 132) + "px";
}

function sendChat() {
  const text = chatInput.value.trim();
  if (!text) return;
  if (!state.ready) { toast("Нет связи: сообщение не отправлено", "error"); return; }
  send({ t: "chat", text });
  chatInput.value = "";
  growInput();
}

chatBtn.prepend(icon("chat"));
chatBtn.addEventListener("click", () => setChat(chat.hidden));
$("chat-close-slot").replaceWith(ibtn({ icon: "x", tip: "Закрыть чат", tipPos: "left",
  onclick: () => { setChat(false); chatBtn.focus(); } }));
const sendBtn = ibtn({ icon: "send", tip: "Отправить", tipPos: "left" });
sendBtn.type = "submit";
sendBtn.classList.add("chat-send");
$("chat-send-slot").replaceWith(sendBtn);
$("chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendChat(); chatInput.focus(); });
chatInput.addEventListener("input", growInput);
chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat(); }
  else if (e.key === "Escape") { setChat(false); chatBtn.focus(); }
});

function askClear() {
  if (!state.strokes.length) { toast("Доска уже пуста"); return; }
  const dlg = $("clear-dialog");
  dlg.returnValue = "";
  dlg.showModal();
  dlg.addEventListener("close", () => { if (dlg.returnValue === "clear") send({ t: "clear" }); }, { once: true });
}

function askName() {
  const dlg = $("name-dialog");
  const input = $("name-input");
  input.value = store.get("name", "") || "";
  dlg.showModal();
  input.select();
}
$("name-form").addEventListener("submit", () => {
  const name = $("name-input").value.trim().slice(0, 32) || "Гость";
  store.set("name", name);
  if (state.me) { state.me.name = name; renderPeers(); send({ t: "rename", name }); }
  else connect();
});
$("name-dialog").addEventListener("cancel", (e) => { if (!store.get("name")) e.preventDefault(); });

$("invite-btn").addEventListener("click", () => {
  const link = location.origin + "/b/" + BOARD_ID;
  $("invite-link").value = link;
  $("invite-dialog").showModal();
  $("invite-link").select();
});
$("invite-copy").addEventListener("click", async () => {
  const ok = await copyText($("invite-link").value);
  toast(ok ? "Ссылка скопирована" : "Скопируйте ссылку вручную", ok ? "ok" : "error");
  if (ok) $("invite-dialog").close();
});

/* ------------------------------------------------------------ сеть */

let ws = null;
let attempt = 0;
let pingTimer = null;
let reconnectTimer = null;
let fatal = false;

function setStatus(text, kind) {
  const s = $("status");
  s.textContent = text;
  s.className = "pill pill-" + kind;
}

function send(message) {
  if (ws && ws.readyState === WebSocket.OPEN && state.ready) ws.send(JSON.stringify(message));
}

function connect() {
  if (fatal || (ws && ws.readyState <= WebSocket.OPEN)) return;
  clearTimeout(reconnectTimer);
  setStatus(attempt ? "Переподключение…" : "Подключение…", "amber");
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${proto}//${location.host}/api/v1/boards/${encodeURIComponent(BOARD_ID)}/ws`);
  ws = socket;
  socket.addEventListener("open", () => {
    socket.send(JSON.stringify({ t: "hello", author: authorKey(), name: store.get("name", "Гость") || "Гость" }));
  });
  socket.addEventListener("message", (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    handle(msg);
  });
  socket.addEventListener("close", (e) => {
    if (ws !== socket) return;
    state.ready = false;
    clearInterval(pingTimer);
    cancelMine();
    for (const peer of state.peers.values()) peer.node && peer.node.remove();
    state.peers.clear();
    state.live.clear();
    renderPeers();
    invalidate();
    if (e.code === 4404) return showMessage("Доска не найдена", "Проверьте ссылку или создайте новую доску.");
    if (e.code === 4403) return showMessage("Доступ запрещён", "Сервер отклонил подключение с этого адреса.");
    const delay = e.code === 4429 ? 10000 : Math.min(10000, 500 * 2 ** attempt) + Math.random() * 400;
    if (e.code === 4429) toast("На доске слишком много участников, повторим позже", "error");
    if (e.code === 1008 && e.reason === "rate limit") toast("Слишком частые сообщения, соединение сброшено", "error");
    attempt += 1;
    setStatus("Нет связи", "red");
    reconnectTimer = setTimeout(connect, delay);
  });
}

function showMessage(title, text) {
  fatal = true;
  setStatus("Отключено", "gray");
  $("board-message-title").textContent = title;
  $("board-message-text").textContent = text;
  $("board-message").hidden = false;
  toolbar.hidden = true;
}

function handle(msg) {
  if (msg.t === "init") return onInit(msg);
  if (!state.ready) return;   // до снимка доски события уже учтены в нём
  switch (msg.t) {
    case "join":
      state.peers.set(msg.peer.id, { name: msg.peer.name, color: msg.peer.color, cursor: null });
      renderPeers();
      toast(`${msg.peer.name} подключился`);
      break;
    case "leave": {
      const peer = state.peers.get(msg.id);
      if (peer && peer.node) peer.node.remove();
      state.peers.delete(msg.id);
      for (const key of [...state.live.keys()]) if (key.startsWith(msg.id + ":")) state.live.delete(key);
      renderPeers();
      invalidate();
      break;
    }
    case "peer": {
      const peer = state.peers.get(msg.peer.id);
      if (peer) { peer.name = msg.peer.name; if (peer.node) peerCursorNode(peer); renderPeers(); }
      break;
    }
    case "cursor": {
      const peer = state.peers.get(msg.id);
      if (peer) { peer.cursor = msg.x === null ? null : { x: msg.x, y: msg.y }; invalidate(); }
      break;
    }
    case "draw": {
      const key = msg.from + ":" + msg.id;
      if (msg.replace) {
        if (!msg.pts.length) state.live.delete(key);
        else state.live.set(key, { id: msg.id, tool: msg.tool, color: msg.color, width: msg.width, pts: msg.pts });
      } else {
        const cur = state.live.get(key);
        if (cur) cur.pts.push(...msg.pts);
        else state.live.set(key, { id: msg.id, tool: msg.tool, color: msg.color, width: msg.width, pts: msg.pts.slice() });
      }
      invalidate();
      break;
    }
    case "stroke":
      state.live.delete(msg.from + ":" + msg.stroke.id);
      state.pending.delete(msg.stroke.id);
      addStroke(msg.stroke);
      invalidate();
      break;
    case "remove":
      removeStroke(msg.id);
      syncToolbar();
      break;
    case "reject":
      state.pending.delete(msg.id);
      removeStroke(msg.id);
      toast(msg.message || "Штрих не сохранён", "error");
      syncToolbar();
      break;
    case "clear":
      clearStrokes();
      state.live.clear();
      if (msg.from !== (state.me && state.me.id)) toast(`${msg.by} очистил доску`);
      break;
    case "chat":
      appendChat(msg.message);
      break;
    case "import":
      applyImport(msg);
      break;
    case "error":
      toast(msg.message || "Ошибка", "error");
      break;
  }
}

function onInit(msg) {
  const firstLoad = !state.me;
  state.me = msg.you;
  state.ready = true;
  attempt = 0;
  document.title = (msg.board.title || "Без названия") + " — Доска";
  $("board-title").textContent = msg.board.title || "Без названия";
  rememberBoard(msg.board);

  // Снимок сервера — источник истины; неподтверждённые свои штрихи отправляем повторно.
  const unsent = [...state.pending.values()];
  state.strokes = [];
  state.index.clear();
  state.pending.clear();
  for (const s of msg.strokes) { state.strokes.push(s); state.index.set(s.id, s); }
  for (const s of unsent) if (!state.index.has(s.id)) { addStroke(s); state.pending.set(s.id, s); send({ t: "stroke", stroke: s }); }
  if (firstLoad) state.history = msg.strokes.filter((s) => s.author === authorKey()).map((s) => s.id);

  renderChat(msg.chat || []);

  state.peers.clear();
  $("cursors").replaceChildren();
  for (const p of msg.peers) state.peers.set(p.id, { name: p.name, color: p.color, cursor: null });
  state.committedDirty = true;
  renderPeers();
  syncToolbar();
  setStatus("В сети", "green");
  if (firstLoad) fitToContent(); else invalidate();
  clearInterval(pingTimer);
  pingTimer = setInterval(() => send({ t: "ping" }), 25000); // держит соединение через прокси
}

/* ------------------------------------------------------------ старт */

(function start() {
  $("bar-actions").append(themeButton());
  fileBtn.append(icon("file"));
  $("invite-btn").prepend(icon("users"));
  if (store.get("chatOpen", false)) setChat(true);
  buildToolbar();
  new ResizeObserver(resize).observe(stage);
  resize();
  resetView();
  if (!/^[A-Za-z0-9_-]{16,40}$/.test(BOARD_ID)) { showMessage("Неверная ссылка", "Проверьте ссылку или создайте новую доску."); return; }
  window.addEventListener("online", () => { if (!state.ready) { attempt = 0; connect(); } });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !state.ready && !fatal) { attempt = 0; connect(); } });
  if (store.get("name")) connect(); else askName();
})();
