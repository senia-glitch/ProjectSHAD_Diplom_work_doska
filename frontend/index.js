"use strict";

document.getElementById("header-actions").append(themeButton());

const createForm = document.getElementById("create-form");
const createBtn = document.getElementById("create-btn");
const createError = document.getElementById("create-error");

const fromFileBtn = document.getElementById("from-file-btn");
const fromFile = document.getElementById("from-file");

/* Код возвращается только в ответе на создание: сервер хранит лишь его хеш. */
async function createBoard(title) {
  const code = document.getElementById("new-code").value.trim();
  const response = await fetch("/api/v1/boards", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, author: authorKey(), code: code || null }),
  });
  if (!response.ok) throw new Error(await apiError(response, "Не удалось создать доску"));
  const board = await response.json();
  rememberCode(board.id, board.code);
  rememberBoard(board);
  sessionStorage.setItem("doska.created", board.id);  // на доске сразу откроется приглашение с кодом
  return board;
}

function showCreateError(err) {
  createError.textContent = err.message === "Failed to fetch" ? "Сервер недоступен" : err.message;
  createError.hidden = false;
  createBtn.disabled = fromFileBtn.disabled = false;
}

createForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  createError.hidden = true;
  createBtn.disabled = fromFileBtn.disabled = true;
  try {
    const board = await createBoard(document.getElementById("title").value);
    location.assign("/b/" + encodeURIComponent(board.id));
  } catch (err) {
    showCreateError(err);
  }
});

/* Новая доска из выгруженного файла: название из поля или из файла, затем содержимое. */
fromFileBtn.addEventListener("click", () => fromFile.click());
fromFile.addEventListener("change", async () => {
  const file = fromFile.files[0];
  fromFile.value = "";
  if (!file) return;
  createError.hidden = true;
  createBtn.disabled = fromFileBtn.disabled = true;
  try {
    const data = await readBoardFile(file);
    const title = document.getElementById("title").value.trim() || String(data.title || "").slice(0, 120);
    const board = await createBoard(title);
    await importBoard(board.id, data, "replace", board.code);
    location.assign("/b/" + encodeURIComponent(board.id));
  } catch (err) {
    showCreateError(err);
  }
});

document.getElementById("join-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const raw = document.getElementById("join-link").value.trim();
  const match = raw.match(/(?:\/b\/)?([A-Za-z0-9_-]{16,40})\/?$/);
  const error = document.getElementById("join-error");
  if (!match) {
    error.textContent = "Вставьте ссылку вида …/b/идентификатор";
    error.hidden = false;
    return;
  }
  const code = document.getElementById("join-code").value.trim();
  if (code) rememberCode(match[1], code);  // иначе код спросит страница доски
  location.assign("/b/" + match[1]);
});

function renderRecent() {
  const list = (store.get("recent", []) || []).filter((b) => b && /^[A-Za-z0-9_-]{16,40}$/.test(b.id));
  const box = document.getElementById("recent-list");
  document.getElementById("recent").hidden = list.length === 0;
  box.replaceChildren(...list.map((board) => {
    const href = "/b/" + board.id;
    return el("div", { class: "list-row" },
      el("span", { class: "board-dot", "aria-hidden": "true" }),
      el("div", { class: "grow" },
        el("a", { class: "row-link", href, text: board.title || "Без названия" }),
        el("div", { class: "meta", text: formatDate(board.ts) })),
      el("div", { class: "row-actions" },
        ibtn({ icon: "link", tip: "Копировать ссылку", onclick: async () => {
          const ok = await copyText(location.origin + href);
          toast(ok ? "Ссылка скопирована" : "Не удалось скопировать", ok ? "ok" : "error");
        } }),
        ibtn({ icon: "x", tip: "Убрать из списка", danger: true, onclick: () => {
          store.set("recent", (store.get("recent", []) || []).filter((b) => b.id !== board.id));
          forgetCode(board.id);
          renderRecent();
        } })));
  }));
}
renderRecent();
