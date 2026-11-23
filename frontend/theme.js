// Подключается синхронно в <head>: тема применяется до отрисовки, без мигания.
(function () {
  var saved = null;
  try { saved = localStorage.getItem("doska.theme"); } catch (e) { /* приватный режим */ }
  document.documentElement.setAttribute("data-theme", saved === "light" ? "light" : "dark");
})();
