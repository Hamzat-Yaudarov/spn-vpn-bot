/* Central-manager support only. No tenant initData fallback or persisted credentials. */
(() => {
  "use strict";
  const root = document.getElementById("support-content");
  const status = document.getElementById("support-status");
  const tg = window.Telegram?.WebApp;
  let selected = null;
  let cursor = 0;
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  async function api(path, options = {}) {
    if (!tg?.initData) throw new Error("Откройте этот раздел через @spn_franchise_bot.");
    const response = await fetch("/partners/support/api" + path, {
      ...options, cache: "no-store", headers: {"authorization": "tma " + tg.initData,
        ...(options.body ? {"content-type": "application/json"} : {})},
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Действие недоступно.");
    return data;
  }
  function notice(text) { status.textContent = text; status.hidden = false; }
  function failure(error) {
    root.innerHTML = `<section class="state-card"><h1>Не удалось открыть</h1><p>${esc(error.message)}</p><button class="button" id="retry">Повторить</button></section>`;
    document.getElementById("retry").onclick = start;
  }
  function bindList() {
    root.querySelectorAll("[data-project]").forEach(button => { button.onclick = () => open(button.dataset.project).catch(failure); });
    const previous = root.querySelector("[data-previous]");
    const next = root.querySelector("[data-next]");
    if (previous) previous.onclick = () => list(Math.max(0, cursor - 50)).catch(failure);
    if (next) next.onclick = () => list(cursor + 50).catch(failure);
  }
  async function list(offset = 0) {
    const data = await api("/projects?cursor=" + offset);
    cursor = offset; selected = null;
    root.innerHTML = `<section class="state-card"><h1>Проекты партнёров</h1><p class="muted">Откройте проект, чтобы помочь с названием, оформлением, поддержкой или ценой. Вы не входите от имени владельца.</p>${data.items.length ? data.items.map(project => `<article class="card"><h2>${esc(project.title)}</h2><p class="muted">${esc(project.slug)} · ${esc(project.status)}<br>Владелец: ${esc(project.owner_tg_id)}${project.bot?.username ? " · @" + esc(project.bot.username) : ""}</p><button class="button secondary" data-project="${esc(project.id)}">Открыть настройки</button></article>`).join("") : '<p class="muted">Проектов пока нет.</p>'}<div class="actions">${offset ? '<button class="button secondary" data-previous>Назад</button>' : ""}${data.truncated ? '<button class="button secondary" data-next>Далее</button>' : ""}</div></section>`;
    bindList();
  }
  function field(name, label, value, {long = false, type = "text"} = {}) {
    return `<label class="field"><span>${esc(label)}</span>${long ? `<textarea name="${name}" rows="4">${esc(value)}</textarea>` : `<input name="${name}" type="${type}" value="${esc(value)}" ${name === "price" ? 'min="199" step="0.01" required' : ""}>`}</label>`;
  }
  async function open(id) {
    selected = await api("/projects/" + encodeURIComponent(id));
    const project = selected, brand = project.brand || {};
    root.innerHTML = `<section class="state-card"><button class="button quiet" id="back">← Все проекты</button><h1>${esc(project.title)}</h1><p class="muted">Режим поддержки · Владелец ${esc(project.owner_tg_id)} · ${esc(project.slug)}<br>Изменения сохраняются от вашего имени. Подписки, оплаты и владение ботом не меняются.</p><form id="support-form">${field("title", "Название сервиса", project.title)}${field("price", "Подписка за 30 дней, ₽ (не ниже 199)", (project.price_kopecks / 100).toFixed(2), {type:"number"})}${field("support_url", "Ссылка поддержки", project.support_url)}${field("news_url", "Ссылка новостей", project.news_url)}${field("welcome_text", "Приветствие", brand.welcome_text, {long:true})}${field("subscription_title", "Заголовок страницы подписки", brand.subscription_title)}${field("subscription_description", "Описание подписки", brand.subscription_description, {long:true})}${field("accent_color", "Основной цвет (#RRGGBB)", brand.accent_color)}<button class="button primary" type="submit">Сохранить настройки</button></form></section>`;
    document.getElementById("back").onclick = () => list(cursor).catch(failure);
    document.getElementById("support-form").onsubmit = save;
  }
  async function save(event) {
    event.preventDefault();
    const form = event.currentTarget;
    if (!form.reportValidity()) return;
    const button = form.querySelector('[type="submit"]');
    if (button.disabled) return;
    const values = new FormData(form);
    const brand = {...selected.brand};
    for (const key of ["welcome_text", "subscription_title", "subscription_description", "accent_color"]) brand[key] = values.get(key);
    const payload = {expected_version: selected.version, title: values.get("title"), price: values.get("price"),
      support_url: values.get("support_url"), news_url: values.get("news_url"), brand};
    button.disabled = true; button.textContent = "Сохраняем…";
    try {
      selected = await api("/projects/" + encodeURIComponent(selected.id), {method:"PATCH", body:JSON.stringify(payload)});
      notice("Настройки сохранены. Изменение записано от вашего имени.");
      await open(selected.id);
    } catch (error) { notice(error.message + " При сомнении откройте проект заново: повтор не перезапишет новую версию."); }
    finally { button.disabled = false; button.textContent = "Сохранить настройки"; }
  }
  async function start() {
    try {
      tg?.ready(); tg?.expand();
      await api("/settings");
      const id = new URLSearchParams(location.search).get("project");
      if (id && /^[0-9a-f-]{36}$/i.test(id)) await open(id); else await list();
    } catch (error) { failure(error); }
  }
  start();
})();
