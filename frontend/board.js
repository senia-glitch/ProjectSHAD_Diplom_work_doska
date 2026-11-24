"use strict";
/* Совместная доска: холст, инструменты, панорама/масштаб и синхронизация через WebSocket.
   Координаты штрихов — мировые; экран = мир * scale + offset. */

const BOARD_ID = decodeURIComponent(location.pathname.split("/")[2] || "");
const PAPER = "#fbfcfe";
// 7 готовых цветов + свой цвет = 8 кружков.
const COLORS = ["#1f2430", "#e5484d", "#f76b15", "#30a46c", "#0090ff", "#7177f8", "#d6409f"];
// Единый размер для всех инструментов; ползунок логарифмический: точная подстройка тонких линий.
const SIZE_MIN = 1, SIZE_MAX = 500, SLIDER_STEPS = 1000;
const clampSize = (w) => Math.min(SIZE_MAX, Math.max(SIZE_MIN, Math.round(Number(w)) || SIZE_MIN));
const sizeToSlider = (w) => Math.round(Math.log(w / SIZE_MIN) / Math.log(SIZE_MAX / SIZE_MIN) * SLIDER_STEPS);
const sliderToSize = (v) => clampSize(SIZE_MIN * (SIZE_MAX / SIZE_MIN) ** (v / SLIDER_STEPS));
const TOOLS = [
  { id: "pen", icon: "pen", tip: "Карандаш (P)", key: "p" },
  { id: "line", icon: "line", tip: "Линия (L)", key: "l" },
  { id: "rect", icon: "rect", tip: "Прямоугольник (R)", key: "r" },
  { id: "ellipse", icon: "ellipse", tip: "Эллипс (O)", key: "o" },
  { id: "eraser", icon: "eraser", tip: "Ластик (E)", key: "e" },
  { id: "hand", icon: "hand", tip: "Перемещение (H, пробел)", key: "h" },
];
const MAX_PEN_VALUES = 3998;       // сервер принимает до 8000 чисел; длинный штрих делится на части
const CURSOR_MS = 100;             // курсор не чаще 10 раз в секунду; сервер рассылает пачками

const $ = (id) => document.getElementById(id);
const stage = $("stage");
const canvas = $("canvas");
const ctx = canvas.getContext("2d");
const committed = document.createElement("canvas");
const cctx = committed.getContext("2d");

const state = {
  tool: store.get("tool", "pen"),
  color: store.get("color", COLORS[0]),
  width: clampSize(store.get("width", 4)),
  view: { x: 0, y: 0, scale: 1 },
  dpr: 1,
  strokes: [],                 // подтверждённые и собственные отправленные, в порядке сервера
  index: new Map(),            // id -> штрих
  pending: new Map(),          // свои отправленные без подтверждения: id -> штрих
  mine: null,                  // свой текущий штрих
  history: [],                 // свои id для отмены
  redo: [],
  peers: new Map(),            // id -> {name, color, cursor, node}
  me: null,
  limits: {},
  savedAt: null,
  unsaved: false,
  code: "",
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
let sizeRange, sizeInput, sizeDot;

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

  sizeDot = el("span", { class: "size-dot" });
  sizeRange = el("input", { type: "range", class: "size-range", min: 0, max: SLIDER_STEPS, step: 1,
    "aria-label": "Размер инструмента" });
  sizeRange.addEventListener("input", () => setWidth(sliderToSize(sizeRange.value)));
  sizeInput = el("input", { type: "number", class: "size-input", min: SIZE_MIN, max: SIZE_MAX, step: 1,
    inputmode: "numeric", "aria-label": `Размер инструмента, от ${SIZE_MIN} до ${SIZE_MAX}` });
  const applyInput = () => {
    if (sizeInput.value.trim() === "") { syncToolbar(); return; }
    setWidth(clampSize(sizeInput.value));
  };
  sizeInput.addEventListener("change", applyInput);
  sizeInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); applyInput(); sizeInput.blur(); }
    else if (e.key === "Escape") { syncToolbar(); sizeInput.blur(); }
  });
  const widths = el("div", { class: "tb-group tb-size", role: "group", "aria-label": "Размер" },
    el("span", { class: "size-preview", "data-tip": "Размер: [ и ] на клавиатуре", "data-tip-pos": "right" }, sizeDot),
    sizeRange, sizeInput);
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
  sizeRange.value = sizeToSlider(state.width);
  sizeRange.setAttribute("aria-valuetext", state.width + " пикс.");
  if (document.activeElement !== sizeInput) sizeInput.value = state.width;
  // Превью: до 28 пикселей в реальном размере, крупнее — кружок 28 и число в поле.
  sizeDot.style.setProperty("--d", Math.max(2, Math.min(28, state.width)) + "px");
  sizeDot.classList.toggle("eraser", state.tool === "eraser");
  toolbar.style.setProperty("--current", state.color);
  stage.dataset.tool = state.tool;
  $("undo-btn").disabled = !state.history.some((id) => state.index.has(id));
  $("redo-btn").disabled = state.redo.length === 0;
}

function setTool(t) { state.tool = t; store.set("tool", t); syncToolbar(); }
function setColor(c) { state.color = c.toLowerCase(); store.set("color", state.color); if (state.tool === "eraser" || state.tool === "hand") state.tool = "pen"; syncToolbar(); }
function setWidth(w) { state.width = clampSize(w); store.set("width", state.width); syncToolbar(); }

/* Участники в шапке */
function initials(name) { return (name || "?").trim().slice(0, 1).toUpperCase() || "?"; }

function renderPeers() {
  if (state.limits.maxPeers) setTimeout(renderMeters);
  const box = $("peers");
  const list = [];
  if (state.me) list.push({ ...state.me, self: true });
  for (const [id, p] of state.peers) list.push({ id, ...p });
  const shown = list.slice(0, 6);
  box.replaceChildren(...shown.map((p) => {
    const kickable = !p.self && isCreator();
    const label = (p.self ? `${p.name} (вы) — изменить имя` : kickable ? `${p.name} — исключить` : p.name) +
      (p.creator ? " · создатель доски" : "");
    const clickable = p.self || kickable;
    const node = el(clickable ? "button" : "span", { class: "avatar" + (p.self ? " self" : "") + (p.creator ? " creator" : "") +
      (kickable ? " kickable" : ""), "data-tip": label, "aria-label": label, type: clickable ? "button" : null,
      onclick: p.self ? askName : kickable ? () => askKick(p.id, p.name) : null, text: initials(p.name) });
    node.style.setProperty("--peer", p.color);
    return node;
  }), ...(list.length > shown.length ? [el("span", { class: "avatar more", text: "+" + (list.length - shown.length),
    "data-tip": list.slice(6).map((p) => p.name).join(", ") })] : []));
}

function isCreator() { return Boolean(state.me && state.me.creator); }

function askKick(id, name) {
  const dlg = $("kick-dialog");
  $("kick-name").textContent = name;
  dlg.returnValue = "";
  dlg.showModal();
  dlg.addEventListener("close", () => { if (dlg.returnValue === "kick") send({ t: "kick", id }); }, { once: true });
}

/* Индикаторы: участники N/15, заполненность доски, сохранение */
function fmt(n) { return n.toLocaleString("ru-RU"); }

function meterLevel(ratio) { return ratio >= 0.9 ? "red" : ratio >= 0.7 ? "amber" : "ok"; }

function boardUsage() {
  let values = 0;
  for (const s of state.strokes) values += s.pts.length;
  return { strokes: state.strokes.length, values };
}

function renderMeters() {
  const lim = state.limits;
  if (!lim.maxStrokes) return;
  const u = boardUsage();
  const ratio = Math.max(u.strokes / lim.maxStrokes, u.values / lim.maxValues);
  const pct = Math.min(100, Math.round(ratio * 100));
  const meter = $("board-meter");
  meter.hidden = false;
  meter.dataset.level = meterLevel(ratio);
  meter.style.setProperty("--fill", Math.max(2, pct) + "%");
  $("board-meter-value").textContent = pct + "%";
  const tip = `Заполнено ${pct}%: штрихов ${fmt(u.strokes)} из ${fmt(lim.maxStrokes)}, ` +
    `объём ${Math.round(u.values / lim.maxValues * 100)}%`;
  meter.dataset.tip = tip;
  meter.setAttribute("aria-label", tip);
  const peers = state.peers.size + (state.me ? 1 : 0);
  const count = $("peer-count");
  count.hidden = false;
  count.textContent = `${peers}/${lim.maxPeers}`;
  count.dataset.level = meterLevel(peers / lim.maxPeers);
  count.dataset.tip = `Участников ${peers} из ${lim.maxPeers}`;
  renderChatMeter();
}

function markChanged() {
  state.unsaved = true;
  renderSaved();
}

function renderSaved() {
  const box = $("saved-state");
  if (!state.limits.autosaveMinutes) return;
  box.hidden = false;
  const at = state.savedAt ? new Date(state.savedAt) : null;
  const time = at ? at.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }) : "";
  box.textContent = state.unsaved ? "Есть несохранённые изменения" : at ? `Сохранено в ${time}` : "Пока не сохранялась";
  box.dataset.tip = `Сервер сохраняет доску каждые ${state.limits.autosaveMinutes} мин и когда все выходят`;
  box.classList.toggle("pending", state.unsaved);
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
  scheduleMeters();
  return true;
}

let metersTimer = null;
function scheduleMeters() {      // пересчёт индикаторов не чаще 4 раз в секунду
  markChanged();
  if (!metersTimer) metersTimer = setTimeout(() => { metersTimer = null; renderMeters(); }, 250);
}

function removeStroke(id) {
  const s = state.index.get(id);
  if (!s) return null;
  state.index.delete(id);
  state.strokes.splice(state.strokes.indexOf(s), 1);
  state.committedDirty = true;
  invalidate();
  scheduleMeters();
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
  scheduleMeters();
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
let cursorTimer = null;
let cursorPending = null;

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
    width: state.width,
    pts: tool === "pen" || tool === "eraser" ? [round1(p.x), round1(p.y)] : [round1(p.x), round1(p.y), round1(p.x), round1(p.y)] };
  gesture = { type: "draw", shift: e.shiftKey };
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
canvas.addEventListener("pointerleave", () => { cursorPending = { t: "cursor", x: null, y: null }; flushCursor(); });
canvas.addEventListener("contextmenu", (e) => e.preventDefault());

function strokePayload(s) { return { id: s.id, tool: s.tool, color: s.color, width: s.width, pts: s.pts }; }

/* Штрих рисуется локально и уходит на сервер одним сообщением после отпускания. */
function finishMine(cancelled) {
  const s = state.mine;
  state.mine = null;
  if (!s) return;
  const degenerate = s.pts.length >= 4 && s.tool !== "pen" && s.tool !== "eraser"
    && Math.hypot(s.pts[2] - s.pts[0], s.pts[3] - s.pts[1]) < 1;
  if (cancelled || degenerate) {
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
  const lastX = s.pts[s.pts.length - 2], lastY = s.pts[s.pts.length - 1];
  state.redo = [];
  commitMine(strokePayload(s));
  state.mine = { ...s, id: randomKey(), pts: [lastX, lastY] };
}

function cancelMine() {
  if (!state.mine) return;
  state.mine = null;
  gesture = null;
  invalidate();
}

function sendCursor(e) {
  const p = toWorld(e.clientX, e.clientY);
  cursorPending = { t: "cursor", x: round1(p.x), y: round1(p.y) };
  const wait = CURSOR_MS - (performance.now() - lastCursorSent);
  if (wait <= 0) flushCursor();
  else if (!cursorTimer) cursorTimer = setTimeout(flushCursor, wait); // последняя позиция не теряется
}

function flushCursor() {
  clearTimeout(cursorTimer);
  cursorTimer = null;
  if (!cursorPending) return;
  lastCursorSent = performance.now();
  send(cursorPending);
  cursorPending = null;
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
  else if (e.key === "[" || e.code === "BracketLeft") setWidth(Math.min(state.width - 1, state.width / 1.25));
  else if (e.key === "]" || e.code === "BracketRight") setWidth(Math.max(state.width + 1, state.width * 1.25));
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
    if (!state.ready) throw new Error("Нет связи: дождитесь подключения");
    const title = $("board-title").textContent;
    const data = { format: "doska", version: 1, title, exported_at: new Date().toISOString(),
      strokes: state.strokes.map((s) => ({ tool: s.tool, color: s.color, width: s.width, pts: s.pts })) };
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
      const result = await importBoard(BOARD_ID, data, mode, state.code);
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
  scheduleMeters();
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
  if (count) renderChatMeter();
  if (stick) chatList.scrollTop = chatList.scrollHeight;
  if (count && !isMine(m) && chat.hidden) {
    state.unread += 1;
    syncChatBadge();
    toast(`${m.name}: ${m.text.length > 80 ? m.text.slice(0, 80) + "…" : m.text}`);
  }
}

function renderChatMeter() {
  const keep = state.limits.chatKeep;
  if (!keep) return;
  const n = state.chatIds.size;
  const meter = $("chat-meter");
  meter.hidden = false;
  meter.textContent = `${Math.min(n, keep)}/${keep}`;
  meter.dataset.level = n >= keep ? "amber" : "ok";
  meter.dataset.tip = n >= keep ? `История заполнена: хранятся последние ${keep} сообщений, старые удаляются`
    : `В истории хранятся последние ${keep} сообщений`;
}

function renderInputCounter() {
  const max = state.limits.chatMaxChars || 1000;
  const n = chatInput.value.length;
  const counter = $("chat-count");
  counter.hidden = n < max * 0.8;
  counter.textContent = `${n}/${max}`;
  counter.dataset.level = n >= max ? "red" : "amber";
}

function renderChat(list) {
  state.chatIds.clear();
  lastChat = null;
  chatList.replaceChildren();
  for (const m of list) appendChat(m, false);
  $("chat-empty").hidden = list.length > 0;
  renderChatMeter();
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
  renderInputCounter();
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
chatInput.addEventListener("input", () => { growInput(); renderInputCounter(); });
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

/* Диалог входа: имя и код. При переименовании поле кода скрыто. */
function askJoin(error = "", wait = 0, needCode = false) {
  const dlg = $("name-dialog");
  const joining = !state.me;
  askJoin.needCode = joining && needCode;
  $("name-dialog-title").textContent = joining ? "Вход на доску" : "Как вас подписать?";
  $("code-field").hidden = !joining || !needCode;
  $("name-input").value = store.get("name", "") || "";
  $("code-input").value = joining ? (state.code || "") : "";
  $("code-input").required = joining && needCode;
  const err = $("join-error");
  err.textContent = error;
  err.hidden = !error;
  const submit = $("join-submit");
  submit.textContent = joining ? "Войти" : "Сохранить";
  clearInterval(askJoin.timer);
  submit.disabled = false;
  if (wait > 0) {                 // блокировка после неверных кодов: обратный отсчёт
    let left = wait;
    submit.disabled = true;
    const tick = () => {
      err.textContent = `Слишком много неверных кодов. Повторите через ${left} с`;
      if (left-- <= 0) { clearInterval(askJoin.timer); submit.disabled = false; err.textContent = "Можно повторить вход"; }
    };
    tick();
    askJoin.timer = setInterval(tick, 1000);
  }
  if (!dlg.open) dlg.showModal();
  (joining && needCode && store.get("name") ? $("code-input") : $("name-input")).focus();
}
const askName = () => askJoin();

$("name-form").addEventListener("submit", (e) => {
  const name = $("name-input").value.trim().slice(0, 32) || "Гость";
  store.set("name", name);
  if (state.me) { state.me.name = name; renderPeers(); send({ t: "rename", name }); return; }
  const code = $("code-input").value.trim();
  if (askJoin.needCode && !code) { e.preventDefault(); $("code-input").focus(); return; }
  if (askJoin.needCode) state.code = code;
  attempt = 0;
  connect();
});
$("name-dialog").addEventListener("cancel", (e) => { if (!state.me) e.preventDefault(); });

function renderInvite() {
  $("invite-site").value = location.origin;
  $("invite-code").value = state.code || "";
  $("invite-unknown").hidden = Boolean(state.code);
  $("invite-copy").disabled = !state.code;
}
$("invite-btn").addEventListener("click", () => {
  if (!isCreator()) return;
  renderInvite();
  $("new-code-input").value = "";
  $("invite-dialog").showModal();
});
$("invite-copy").addEventListener("click", async () => {
  const title = $("board-title").textContent;
  const ok = await copyText(`Доска «${title}»: откройте ${location.origin} и введите код ${state.code}`);
  toast(ok ? "Приглашение скопировано" : "Скопируйте код вручную", ok ? "ok" : "error");
  if (ok) $("invite-dialog").close();
});
$("change-code").addEventListener("click", () => {
  const code = $("new-code-input").value.trim();
  send({ t: "setcode", code: code || null });
});
$("new-code-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("change-code").click(); }
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
    socket.send(JSON.stringify({ t: "hello", author: authorKey(), name: store.get("name", "Гость") || "Гость", code: state.code }));
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
    renderPeers();
    invalidate();
    if (e.code === 4404) return showMessage("Доска не найдена", "Ссылка неверна, или на доску давно не заходили и она удалена.");
    if (e.code === 4410) {
      forgetCode(BOARD_ID);
      return showMessage("Вы исключены из доски", "Создатель доски исключил вас. Войти снова нельзя.");
    }
    if (e.code === 4401 || e.code === 4408) {
      setStatus("Нужен код", "amber");
      const tried = Boolean(state.code);
      if (e.code === 4401) { forgetCode(BOARD_ID); state.code = ""; }
      const wait = e.code === 4408 ? Number((e.reason || "").split(":")[1]) || 60 : 0;
      state.me = null;
      return askJoin(e.code === 4401 ? (tried ? "Неверный код: возможно, создатель его сменил" : "Введите код доски") : "",
        wait, true);
    }
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
      state.peers.set(msg.peer.id, { name: msg.peer.name, color: msg.peer.color, creator: msg.peer.creator, cursor: null });
      renderPeers();
      toast(`${msg.peer.name} подключился`);
      break;
    case "leave": {
      const peer = state.peers.get(msg.id);
      if (peer && peer.node) peer.node.remove();
      state.peers.delete(msg.id);
      renderPeers();
      invalidate();
      break;
    }
    case "peer": {
      const peer = state.peers.get(msg.peer.id);
      if (peer) { peer.name = msg.peer.name; if (peer.node) peerCursorNode(peer); renderPeers(); }
      break;
    }
    case "cursors":
      for (const [id, x, y] of msg.c) {
        const peer = state.peers.get(id);
        if (peer) peer.cursor = x === null ? null : { x, y };
      }
      invalidate();
      break;
    case "stroke":
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
      if (msg.from !== (state.me && state.me.id)) toast(`${msg.by} очистил доску`);
      break;
    case "chat":
      appendChat(msg.message);
      markChanged();
      break;
    case "code":
      state.code = msg.code;
      rememberCode(BOARD_ID, msg.code);
      $("new-code-input").value = "";
      renderInvite();
      toast(msg.kicked ? `Участник «${msg.kicked}» исключён. Новый код: ${msg.code}` : `Код доски изменён: ${msg.code}`, "ok");
      break;
    case "kicked":
      if (!isCreator()) toast(`Участник «${msg.name}» исключён создателем доски`);  // создателю — своё уведомление
      break;
    case "saved":
      state.savedAt = msg.at;
      state.unsaved = false;
      renderSaved();
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
  if (firstLoad) state.history = msg.strokes.filter((s) => s.by === state.me.tag).map((s) => s.id);
  state.limits = msg.limits || {};
  state.savedAt = msg.savedAt;
  state.unsaved = false;
  rememberCode(BOARD_ID, state.code);
  if ($("name-dialog").open) $("name-dialog").close();
  $("invite-btn").hidden = !state.me.creator;   // приглашает только создатель

  renderChat(msg.chat || []);

  state.peers.clear();
  $("cursors").replaceChildren();
  for (const p of msg.peers) state.peers.set(p.id, { name: p.name, color: p.color, creator: p.creator, cursor: null });
  state.committedDirty = true;
  renderPeers();
  syncToolbar();
  setStatus("В сети", "green");
  renderMeters();
  renderSaved();
  if (firstLoad) fitToContent(); else invalidate();
  if (firstLoad && sessionStorage.getItem("doska.created") === BOARD_ID) {
    sessionStorage.removeItem("doska.created");   // создатель сразу видит ссылку и код для приглашения
    $("invite-btn").click();
  }
  clearInterval(pingTimer);
  pingTimer = setInterval(() => send({ t: "ping" }), 25000); // держит соединение через прокси
}

/* ------------------------------------------------------------ старт */

(function start() {
  $("bar-actions").append(themeButton());
  fileBtn.append(icon("file"));
  $("invite-btn").prepend(icon("users"));
  $("invite-btn").hidden = true;
  setInterval(renderSaved, 30000);
  if (store.get("chatOpen", false)) setChat(true);
  buildToolbar();
  new ResizeObserver(resize).observe(stage);
  resize();
  resetView();
  if (!/^[A-Za-z0-9_-]{16,40}$/.test(BOARD_ID)) { showMessage("Неверная ссылка", "Проверьте ссылку или создайте новую доску."); return; }
  window.addEventListener("online", () => { if (!state.ready) { attempt = 0; connect(); } });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !state.ready && !fatal) { attempt = 0; connect(); } });
  state.code = knownCode(BOARD_ID);
  // Создатель и участник, уже входивший с этого устройства, входят без кода.
  if (store.get("name")) connect(); else askJoin();
})();
