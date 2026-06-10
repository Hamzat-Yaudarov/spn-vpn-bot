const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }

const initData = tg?.initData || "";
const state = {
  view: "dashboard",
  me: null,
  dashboard: {},
  users: [],
  selectedUser: null,
  promos: [],
  links: [],
  referrals: [],
  notifications: { rules: [], state: [] },
  discounts: [],
};

function el(id) { return document.getElementById(id); }
function rub(v) { return `${Number(v || 0).toLocaleString("ru-RU")} ₽`; }
function date(v) { return v ? new Date(v).toLocaleString("ru-RU") : "не задано"; }
function toast(text) { const t = el("toast"); t.textContent = text; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 2400); }

function api(path, options = {}) {
  return fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "Authorization": `tma ${initData}`, ...(options.headers || {}) },
  }).then(async (res) => {
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    return data;
  });
}

function note(title, text) {
  return `<div class="section-note"><p class="step">${title}</p><p class="muted">${text}</p></div>`;
}

function label(text, inner) {
  return `<label><span>${text}</span>${inner}</label>`;
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    state.view = button.dataset.view;
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === button));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    el(`view-${state.view}`).classList.add("active");
    render();
  });
});

async function reloadAll() { await load(); toast("Обновлено"); }

async function load() {
  try {
    const me = await api("/admin/api/me");
    const requests = await Promise.allSettled([
      api("/admin/api/dashboard"),
      api("/admin/api/users"),
      api("/admin/api/promos"),
      api("/admin/api/tracking-links"),
      api("/admin/api/referrals"),
      api("/admin/api/notifications"),
      api("/admin/api/discounts"),
    ]);
    const [dashboard, users, promos, links, referrals, notifications, discounts] = requests.map((r, index) => {
      if (r.status === "fulfilled") return r.value;
      console.error("Admin section failed", index, r.reason);
      return null;
    });
    Object.assign(state, {
      me,
      dashboard: dashboard || {},
      users: users?.users || [],
      promos: promos?.promos || [],
      links: links?.links || [],
      referrals: referrals?.referrals || [],
      notifications: notifications || { rules: [], state: [] },
      discounts: discounts?.discounts || [],
    });
    el("adminLine").textContent = me.username ? `@${me.username}` : `ID ${me.id}`;
    render();
    const failed = requests.filter((r) => r.status === "rejected").length;
    if (failed) toast(`Загружено частично. Ошибок: ${failed}`);
  } catch (e) {
    document.querySelector("main").innerHTML = `<div class="card"><h2>Панель недоступна</h2><p class="muted">${e.message}</p><p class="muted">Откройте админ-панель именно кнопкой в Telegram-боте. В обычном браузере Telegram не передаёт авторизацию.</p></div>`;
  }
}

function render() {
  renderDashboard();
  renderUsers();
  renderPromos();
  renderLinks();
  renderNotifications();
  renderDiscounts();
}

function renderDashboard() {
  const d = state.dashboard || {};
  el("view-dashboard").innerHTML = `<div class="grid">
    <div class="card">${note("Обзор", "Главные цифры по боту. Нажмите «Обновить», чтобы подтянуть свежие данные.")}</div>
    <div class="cards">
      ${stat("Пользователи", d.users, "Все, кто запускал бота")}
      ${stat("Активные подписки", d.active_subscriptions, "Сейчас работают")}
      ${stat("Regular", d.active_regular, "Обычные ключи")}
      ${stat("Bypass", d.active_bypass, "Антиглушилка")}
      ${stat("Без подписки", d.users_without_subscription, "Кому можно сделать рассылку")}
      ${stat("Выручка", rub(d.revenue), "Оплаченные платежи")}
      ${stat("Платежи", d.paid_payments, "Количество оплат")}
      ${stat("Куплено ГБ", d.traffic_gb, "Доп. трафик bypass")}
    </div>
  </div>`;
}

function stat(title, value, hint) {
  return `<div class="card stat"><span class="muted">${title}</span><b>${value ?? 0}</b><small>${hint}</small></div>`;
}

function renderUsers() {
  el("view-users").innerHTML = `<div class="grid">
    <div class="card">${note("Пользователи", "Найдите человека по Telegram ID или username. В карточке можно выдать/убрать дни, архивировать или удалить запись подписки.")}
      <div class="form">${label("Поиск", `<input id="userSearch" placeholder="123456789 или username">`)}<button class="button blue" onclick="searchUsers()">Найти</button></div>
    </div>
    <div class="list">${state.users.map(userHtml).join("") || `<p class="muted">Пользователей не найдено</p>`}</div>
    <div id="userDetail"></div>
  </div>`;
  if (state.selectedUser) renderUserDetail();
}

function userHtml(u) {
  return `<div class="item"><div class="row"><div><b>${u.username || "без username"}</b><small>ID ${u.tg_id}</small><small>Активных подписок: ${u.active_subscriptions || 0} · оплат: ${rub(u.revenue)}</small></div><button class="button ghost" onclick="openUser(${u.tg_id})">Открыть</button></div></div>`;
}

async function searchUsers() { const q = el("userSearch").value.trim(); const r = await api(`/admin/api/users${q ? `?q=${encodeURIComponent(q)}` : ""}`); state.users = r.users || []; renderUsers(); }
async function openUser(id) { state.selectedUser = await api(`/admin/api/users/${id}`); renderUserDetail(); }

function renderUserDetail() {
  const d = state.selectedUser;
  const box = el("userDetail");
  if (!box || !d) return;
  box.innerHTML = `<div class="card"><h2>${d.user.username || "Пользователь"}</h2><p class="muted">ID ${d.user.tg_id} · tracking: ${d.user.tracking_code || "нет"}</p>
    <h3>Подписки</h3><div class="list">${d.subscriptions.map(subHtml).join("") || `<p class="muted">Нет подписок</p>`}</div>
    <h3>Последние платежи</h3><div class="list">${d.payments.map(payHtml).join("") || `<p class="muted">Платежей пока нет</p>`}</div></div>`;
}

function subHtml(s) {
  const title = `${s.plan_kind || "regular"} #${s.type_index || s.slot_number}`;
  return `<div class="item"><b>${title}</b><small>Срок: ${date(s.subscription_until)}</small><small>Видимость: ${s.is_visible ? "видна пользователю" : "скрыта"} · Продление: ${s.is_renewable ? "доступно" : "недоступно"}</small><div class="actions"><button class="button green" onclick="changeDays(${s.id},30)">+30 дней</button><button class="button red" onclick="changeDays(${s.id},-30)">-30 дней</button><button class="button ghost" onclick="archiveSub(${s.id})">Скрыть/архив</button><button class="button red" onclick="deleteSub(${s.id})">Удалить из БД</button></div><small>Удаление из БД не удаляет ключ в Remnawave. Для безопасного отключения лучше сначала убрать дни.</small></div>`;
}

function payHtml(p) { return `<div class="item"><b>${p.tariff_code}</b><small>${p.provider} · ${p.status} · ${rub(p.amount)} · ${date(p.created_at)}</small></div>`; }
async function changeDays(id, days) { await api(`/admin/api/subscriptions/${id}/days`, { method: "POST", body: JSON.stringify({ days }) }); toast("Срок изменён"); await openUser(state.selectedUser.user.tg_id); }
async function archiveSub(id) { await api(`/admin/api/subscriptions/${id}/archive`, { method: "POST" }); toast("Подписка скрыта"); await openUser(state.selectedUser.user.tg_id); }
async function deleteSub(id) { if (!confirm("Удалить запись подписки из БД?")) return; await api(`/admin/api/subscriptions/${id}`, { method: "DELETE" }); toast("Удалено из БД"); await openUser(state.selectedUser.user.tg_id); }

function renderPromos() {
  el("view-promos").innerHTML = `<div class="grid"><div class="card">${note("Промокоды", "Промокод даёт бесплатные дни подписки. Пользователь вводит код в боте.")}
    <div class="form">${label("Код", `<input id="promoCode" placeholder="SUMMER30">`)}${label("Дней", `<input id="promoDays" type="number" placeholder="30">`)}${label("Лимит", `<input id="promoLimit" type="number" placeholder="100">`)}<button class="button green" onclick="createPromo()">Создать</button></div></div>
    <div class="list">${state.promos.map(promoHtml).join("") || `<p class="muted">Промокодов пока нет</p>`}</div></div>`;
}

function promoHtml(p) { return `<div class="item"><div class="row"><div><b>${p.code}</b><small>${p.days} дней · использовано ${p.used_count}/${p.max_uses} · ${p.active ? "активен" : "выключен"}</small></div><div class="actions"><button class="button ghost" onclick="togglePromo('${p.code}',${!p.active})">${p.active ? "Выключить" : "Включить"}</button><button class="button red" onclick="deletePromo('${p.code}')">Удалить</button></div></div></div>`; }
async function createPromo() { await api("/admin/api/promos", { method: "POST", body: JSON.stringify({ code: el("promoCode").value, days: el("promoDays").value, max_uses: el("promoLimit").value }) }); await reloadAll(); }
async function togglePromo(code, active) { await api(`/admin/api/promos/${code}/active`, { method: "POST", body: JSON.stringify({ active }) }); await reloadAll(); }
async function deletePromo(code) { if (!confirm("Удалить промокод?")) return; await api(`/admin/api/promos/${code}`, { method: "DELETE" }); await reloadAll(); }

function renderLinks() {
  el("view-links").innerHTML = `<div class="grid"><div class="card">${note("Ссылки и рефералы", "Tracking-ссылки нужны для рекламы/блогеров. Реферальная статистика показывает переходы по ссылкам пользователей.")}
    <div class="form">${label("Код ссылки", `<input id="linkCode" placeholder="blogger1">`)}${label("Название", `<input id="linkTitle" placeholder="Реклама у блогера">`)}<button class="button green" onclick="createLink()">Создать</button></div></div>
    <h3>Tracking-ссылки</h3><div class="list">${state.links.map(linkHtml).join("") || `<p class="muted">Ссылок пока нет</p>`}</div>
    <h3>Рефералы пользователей</h3><div class="list">${state.referrals.map(refHtml).join("") || `<p class="muted">Пока нет данных</p>`}</div></div>`;
}

function linkHtml(l) { return `<div class="item"><b>${l.link.code}</b><small>${l.link.title || "без названия"}</small><small>Клики: ${l.total_clicks} · уникальные: ${l.unique_clicks} · новые: ${l.new_clicks} · оплаты: ${l.paid_payments} · выручка: ${rub(l.revenue)}</small><div class="actions"><button class="button ghost" onclick="toggleLink('${l.link.code}',${!l.link.is_active})">${l.link.is_active ? "Выключить" : "Включить"}</button></div></div>`; }
function refHtml(r) { return `<div class="item"><b>${r.username || r.tg_id}</b><small>ID ${r.tg_id}</small><small>Клики: ${r.clicks} · новые: ${r.new_clicks} · рефералы: ${r.referred_users} · выручка: ${rub(r.referred_revenue)}</small></div>`; }
async function createLink() { await api("/admin/api/tracking-links", { method: "POST", body: JSON.stringify({ code: el("linkCode").value, title: el("linkTitle").value }) }); await reloadAll(); }
async function toggleLink(code, active) { await api(`/admin/api/tracking-links/${code}/active`, { method: "POST", body: JSON.stringify({ active }) }); await reloadAll(); }

function renderNotifications() {
  const n = state.notifications || { rules: [], state: [] };
  el("view-notifications").innerHTML = `<div class="grid"><div class="card">${note("Уведомления", "Здесь меняется расписание и пороги автоматических уведомлений. Изменения применяются без правки кода после перезапуска/следующего цикла проверки.")}</div><h3>Правила</h3><div class="list">${n.rules.map(ruleHtml).join("")}</div><h3>Последние отправки</h3><div class="list">${n.state.map(notifHtml).join("") || `<p class="muted">Отправок пока нет</p>`}</div></div>`;
}

function ruleHtml(r) {
  return `<div class="item"><b>${ruleTitle(r.notification_type)}</b><small>${ruleHint(r.notification_type)}</small><div class="form">${label("Статус", `<select id="en_${r.notification_type}"><option value="true" ${r.enabled ? "selected" : ""}>Включено</option><option value="false" ${!r.enabled ? "selected" : ""}>Выключено</option></select>`)}${label("Час МСК", `<input id="hour_${r.notification_type}" type="number" placeholder="19" value="${r.send_hour_msk ?? ""}">`)}${label("Cooldown, часов", `<input id="cool_${r.notification_type}" type="number" value="${r.cooldown_hours ?? ""}">`)}${label("Дней до конца", `<input id="days_${r.notification_type}" type="number" value="${r.days_before_expiry ?? ""}">`)}${label("Порог ГБ", `<input id="gb_${r.notification_type}" type="number" value="${r.low_traffic_gb ?? ""}">`)}${label("Дней до reset", `<input id="reset_${r.notification_type}" type="number" value="${r.min_days_to_reset ?? ""}">`)}<button class="button green" onclick="saveRule('${r.notification_type}')">Сохранить</button></div></div>`;
}

function ruleTitle(type) { return ({ subscription_expiring: "Подписка скоро закончится", expired_or_no_subscription: "Нет активной подписки", low_bypass_traffic: "Мало ГБ anti-block" })[type] || type; }
function ruleHint(type) { return ({ subscription_expiring: "Когда до окончания осталось N дней", expired_or_no_subscription: "Для пользователей без активной подписки", low_bypass_traffic: "Когда у bypass осталось мало трафика" })[type] || "Автоматическое уведомление"; }
function notifHtml(s) { return `<div class="item"><b>${ruleTitle(s.notification_type)}</b><small>${s.username || s.tg_id} · отправлено: ${date(s.last_sent_at)} · subscription_id: ${s.subscription_id}</small></div>`; }
async function saveRule(t) { const val = id => { const v = el(`${id}_${t}`).value; return v === "" ? null : Number(v); }; await api(`/admin/api/notifications/${t}`, { method: "POST", body: JSON.stringify({ enabled: el(`en_${t}`).value === "true", send_hour_msk: val("hour"), cooldown_hours: val("cool"), days_before_expiry: val("days"), low_traffic_gb: val("gb"), min_days_to_reset: val("reset") }) }); await reloadAll(); }

function renderDiscounts() {
  el("view-discounts").innerHTML = `<div class="grid"><div class="card">${note("Скидки", "Скидка без кода применяется автоматически. Если указан код, пользователь вводит его в MiniApp перед оплатой.")}
    <div class="form">${label("Название", `<input id="discTitle" placeholder="Летняя акция">`)}${label("Код", `<input id="discCode" placeholder="Пусто = авто">`)}${label("Тип", `<select id="discType"><option value="percent">Проценты</option><option value="fixed">Рубли</option></select>`)}${label("Размер", `<input id="discValue" type="number" placeholder="20">`)}${label("На что", `<select id="discTarget"><option value="all">На всё</option><option value="subscription">Все подписки</option><option value="traffic_package">Покупка ГБ</option><option value="regular">Regular</option><option value="bypass">Bypass</option></select>`)}${label("Лимит", `<input id="discMax" type="number" placeholder="Пусто = без лимита">`)}<button class="button green" onclick="createDiscount()">Создать</button></div></div>
    <div class="list">${state.discounts.map(discountHtml).join("") || `<p class="muted">Скидок пока нет</p>`}</div></div>`;
}

function discountHtml(d) { return `<div class="item"><b>${d.title}</b><small>${d.code || "автоматическая"} · ${d.discount_type === "percent" ? `${d.discount_value}%` : rub(d.discount_value)} · цель: ${d.target_kind}</small><small>Использовано: ${d.used_count}/${d.max_uses || "∞"} · ${d.is_active ? "активна" : "выключена"}</small><div class="actions"><button class="button ghost" onclick="toggleDiscount(${d.id},${!d.is_active})">${d.is_active ? "Выключить" : "Включить"}</button></div></div>`; }
async function createDiscount() { await api("/admin/api/discounts", { method: "POST", body: JSON.stringify({ title: el("discTitle").value, code: el("discCode").value || null, discount_type: el("discType").value, discount_value: el("discValue").value, target_kind: el("discTarget").value, max_uses: el("discMax").value ? Number(el("discMax").value) : null, is_active: true }) }); await reloadAll(); }
async function toggleDiscount(id, active) { await api(`/admin/api/discounts/${id}/active`, { method: "POST", body: JSON.stringify({ active }) }); await reloadAll(); }

load();
