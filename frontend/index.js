"use strict";

document.getElementById("header-actions").append(themeButton());

const createForm = document.getElementById("create-form");
const createBtn = document.getElementById("create-btn");
const createError = document.getElementById("create-error");

createForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  createError.hidden = true;
  createBtn.disabled = true;
  try {
    const response = await fetch("/api/v1/boards", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: document.getElementById("title").value }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || (response.status === 429
      ? "Слишком много новых досок, попробуйте через минуту" : "Не удалось создать доску"));
    rememberBoard(data);
    location.assign("/b/" + encodeURIComponent(data.id));
  } catch (err) {
    createError.textContent = err.message === "Failed to fetch" ? "Сервер недоступен" : err.message;
    createError.hidden = false;
    createBtn.disabled = false;
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
          renderRecent();
        } })));
  }));
}
renderRecent();
