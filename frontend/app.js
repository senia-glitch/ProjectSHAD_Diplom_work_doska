const status = document.querySelector('#status');
const list = document.querySelector('#list');
const save = document.querySelector('#save');
let latest = 0;

async function api(path, options = {}) {
  const response = await fetch('/api/v1' + path, options);
  if (!response.ok) {
    let message = 'Запрос не выполнен';
    try { const body = await response.json(); message = body.error?.message || message; } catch {}
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

async function load() {
  const token = ++latest;
  status.textContent = 'Загрузка…';
  try {
    const page = await api('/notes?limit=100');
    if (token !== latest) return;
    const nodes = page.items.map(note => {
      const row = document.createElement('li');
      const text = document.createElement('span'); text.textContent = note.title;
      const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Удалить';
      remove.setAttribute('aria-label', 'Удалить заметку ' + note.title);
      remove.onclick = async () => {
        remove.disabled = true;
        try { await api('/notes/' + note.id, { method: 'DELETE' }); await load(); }
        catch (error) { status.textContent = error.message + '. Проверьте состояние перед повтором.'; remove.disabled = false; }
      };
      row.append(text, remove); return row;
    });
    list.replaceChildren(...nodes);
    status.textContent = page.total ? `Показано ${page.items.length} из ${page.total}` : 'Заметок пока нет';
  } catch (error) { if (token === latest) status.textContent = error.message; }
}

document.querySelector('#form').onsubmit = async event => {
  event.preventDefault();
  if (save.disabled) return;
  save.disabled = true;
  try {
    await api('/notes', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: document.querySelector('#title').value }) });
    event.target.reset(); await load();
  } catch (error) { status.textContent = error.message + '. Проверьте состояние перед повтором.'; }
  finally { save.disabled = false; }
};
load();
