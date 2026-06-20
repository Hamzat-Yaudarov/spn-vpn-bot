const state = { section: "dashboard", usersOffset: 0, usersSearch: "", usersTotal: 0, pageSize: 50 };
const titles = { dashboard: "Обзор", users: "Пользователи", promos: "Промокоды", links: "Ссылки", discounts: "Скидки" };
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[ch]));
const money = (value) => `${Number(value || 0).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ₽`;
const date = (value, withTime = false) => value ? new Date(value).toLocaleString("ru-RU", withTime ? { dateStyle: "short", timeStyle: "short" } : { dateStyle: "short" }) : "—";
const isActiveDate = (value) => value && new Date(value).getTime() > Date.now();
const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();
const initData = tg?.initData || "";

async function api(path, options = {}) {
  const response = await fetch(`/admin/api${path}`, { headers: { "Content-Type": "application/json", Authorization: `tma ${initData}`, ...(options.headers || {}) }, ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) showAccess(data.detail);
    throw new Error(data.detail || "Ошибка запроса");
  }
  return data;
}

function toast(message, bad = false) { const el = $("toast"); el.textContent = message; el.className = `toast show${bad ? " bad" : ""}`; clearTimeout(toast.timer); toast.timer = setTimeout(() => el.className = "toast", 2600); }
function showAccess(message = "Откройте панель кнопкой «Админ-панель» в боте.") { $("adminApp").classList.add("hidden"); $("accessView").classList.remove("hidden"); $("accessMessage").textContent = message; }
function showApp() { $("accessView").classList.add("hidden"); $("adminApp").classList.remove("hidden"); switchSection(state.section); }

async function boot() { if (!initData) { showAccess(); return; } try { await api("/session"); showApp(); } catch (error) { showAccess(error.message); } }

$("closeButton").addEventListener("click", () => { if (tg) tg.close(); else history.back(); });

document.querySelectorAll(".nav-item[data-section]").forEach((button) => button.addEventListener("click", () => switchSection(button.dataset.section)));
function switchSection(section) {
  state.section = section; $("pageTitle").textContent = titles[section];
  document.querySelectorAll(".section").forEach((el) => el.classList.toggle("active", el.id === `section-${section}`));
  document.querySelectorAll(".nav-item[data-section]").forEach((el) => el.classList.toggle("active", el.dataset.section === section));
  if (section === "dashboard") loadDashboard();
  if (section === "users") loadUsers();
  if (section === "promos") loadPromos();
  if (section === "links") loadLinks();
  if (section === "discounts") loadDiscounts();
}

async function loadDashboard() {
  try {
    const d = await api("/dashboard");
    const cards = [["Пользователей", d.total_users, "blue"], ["Новых за 7 дней", d.new_users_7d, "green"], ["Активных подписок", d.active_subscriptions, "green"], ["Оплаченных счетов", d.paid_payments, "gold"], ["Выручка за 30 дней", money(d.revenue_30d), "gold"], ["Выручка всего", money(d.total_revenue), "blue"]];
    $("statsGrid").innerHTML = cards.map(([label, value, cls]) => `<div class="stat-card ${cls}"><span>${label}</span><b>${value}</b></div>`).join("");
  } catch (error) { toast(error.message, true); }
}

$("userSearchForm").addEventListener("submit", (event) => { event.preventDefault(); state.usersSearch = $("userSearch").value.trim(); state.usersOffset = 0; loadUsers(); });
async function loadUsers() {
  try {
    const d = await api(`/users?q=${encodeURIComponent(state.usersSearch)}&limit=${state.pageSize}&offset=${state.usersOffset}`); state.usersTotal = d.total;
    $("usersTable").innerHTML = d.items.length ? `<table><thead><tr><th>Пользователь</th><th>Создан</th><th>Подписки</th><th>Ближайший срок</th><th>Выручка</th><th></th></tr></thead><tbody>${d.items.map((u) => `<tr><td><b>${esc(u.web_login || u.username || "Без username")}</b> <span class="badge ${u.account_type === "web" ? "" : "off"}">${u.account_type === "web" ? "Сайт" : "Telegram"}</span><br><span class="mono muted">${u.account_type === "web" ? "Внутренний ID" : "Telegram ID"}: ${u.tg_id}</span></td><td>${date(u.created_at)}</td><td><span class="badge ${Number(u.active_subscriptions) ? "" : "off"}">${u.active_subscriptions} активных / ${u.subscriptions_count}</span></td><td>${date(u.latest_expiry)}</td><td>${money(u.revenue)}</td><td><button class="button small blue" data-user="${u.tg_id}">Открыть</button></td></tr>`).join("")}</tbody></table>` : `<div class="empty">Пользователи не найдены</div>`;
    const prev = state.usersOffset > 0, next = state.usersOffset + state.pageSize < state.usersTotal;
    const rangeStart = state.usersTotal ? state.usersOffset + 1 : 0;
    $("usersPagination").innerHTML = `<span class="muted">${rangeStart}–${Math.min(state.usersOffset + state.pageSize, state.usersTotal)} из ${state.usersTotal}</span><button class="button small" data-page="prev" ${prev ? "" : "disabled"}>Назад</button><button class="button small" data-page="next" ${next ? "" : "disabled"}>Дальше</button>`;
  } catch (error) { toast(error.message, true); }
}
$("usersTable").addEventListener("click", (e) => { const button = e.target.closest("[data-user]"); if (button) openUser(button.dataset.user); });
$("usersPagination").addEventListener("click", (e) => { const p = e.target.dataset.page; if (p === "prev") state.usersOffset = Math.max(0, state.usersOffset - state.pageSize); if (p === "next") state.usersOffset += state.pageSize; if (p) loadUsers(); });

async function openUser(tgId) {
  try {
    const d = await api(`/users/${tgId}`); const u = d.user; const web = d.web_account; $("dialogTitle").textContent = web ? web.login : (u.username ? `@${u.username}` : String(u.tg_id));
    const subs = d.subscriptions.map((s) => `<article class="subscription-card"><div><h3>${s.plan_kind === "bypass" ? "С антиглушилкой" : "Обычная"} #${s.type_index || s.slot_number}</h3><small>ID ${s.id} · до ${date(s.subscription_until, true)}</small><div><span class="badge ${isActiveDate(s.subscription_until) ? "" : "off"}">${isActiveDate(s.subscription_until) ? "Активна" : "Истекла"}</span></div></div><div class="actions"><button class="button small" data-adjust="${s.id}" data-days="7">+7 дней</button><button class="button small" data-adjust="${s.id}" data-days="30">+30 дней</button><button class="button small danger" data-adjust="${s.id}" data-days="-7">−7 дней</button><button class="button small danger" data-delete-sub="${s.id}">Удалить</button></div></article>`).join("");
    const payments = d.payments.length ? `<table><thead><tr><th>Дата</th><th>Тариф</th><th>Сумма</th><th>Статус</th></tr></thead><tbody>${d.payments.map((p) => `<tr><td>${date(p.created_at, true)}</td><td>${esc(p.tariff_code)}</td><td>${money(p.amount)}</td><td>${esc(p.status)}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">Платежей пока нет</div>`;
    $("userDetails").innerHTML = `<div class="details-body"><div class="details-grid"><div class="detail-tile"><span>${web ? "Веб-аккаунт" : "Telegram ID"}</span><b class="mono">${web ? esc(web.login) : u.tg_id}</b></div><div class="detail-tile"><span>Регистрация</span><b>${date(u.created_at, true)}</b></div><div class="detail-tile"><span>Тип аккаунта</span><b>${web ? "Сайт" : "Telegram"}</b></div></div><div><h3>Подписки</h3><div class="subscription-list">${subs || `<div class="empty">Подписок нет</div>`}</div></div><div><h3>Последние платежи</h3><div class="table-wrap">${payments}</div></div></div>`;
    $("userDialog").dataset.user = tgId; if (!$("userDialog").open) $("userDialog").showModal();
  } catch (error) { toast(error.message, true); }
}
$("closeDialog").addEventListener("click", () => $("userDialog").close());
$("userDetails").addEventListener("click", async (e) => {
  const adjust = e.target.closest("[data-adjust]"); const del = e.target.closest("[data-delete-sub]");
  try {
    if (adjust) { const days = Number(adjust.dataset.days); if (!confirm(`${days > 0 ? "Добавить" : "Убрать"} ${Math.abs(days)} дней?`)) return; await api(`/subscriptions/${adjust.dataset.adjust}/adjust`, { method: "POST", body: JSON.stringify({ days }) }); toast("Срок подписки обновлён"); await openUser($("userDialog").dataset.user); }
    if (del) { if (!confirm("Удалить подписку и отключить её в Remnawave?")) return; await api(`/subscriptions/${del.dataset.deleteSub}`, { method: "DELETE" }); toast("Подписка удалена"); await openUser($("userDialog").dataset.user); }
  } catch (error) { toast(error.message, true); }
});

$("promoForm").addEventListener("submit", async (e) => { e.preventDefault(); const data = Object.fromEntries(new FormData(e.target)); data.days = Number(data.days); data.max_uses = Number(data.max_uses); try { await api("/promos", { method: "POST", body: JSON.stringify(data) }); toast("Промокод создан"); e.target.reset(); loadPromos(); } catch (error) { toast(error.message, true); } });
async function loadPromos() { try { const d = await api("/promos"); $("promosTable").innerHTML = d.items.length ? `<table><thead><tr><th>Код</th><th>Дней</th><th>Использовано</th><th>Статус</th><th></th></tr></thead><tbody>${d.items.map((p) => `<tr><td class="mono"><b>${esc(p.code)}</b></td><td>${p.days}</td><td>${p.used_count} / ${p.max_uses}</td><td><span class="badge ${p.active ? "" : "off"}">${p.active ? "Активен" : "Выключен"}</span></td><td><button class="button small" data-promo="${esc(p.code)}" data-active="${!p.active}">${p.active ? "Выключить" : "Включить"}</button></td></tr>`).join("")}</tbody></table>` : `<div class="empty">Промокодов пока нет</div>`; } catch (error) { toast(error.message, true); } }
$("promosTable").addEventListener("click", async (e) => { const b = e.target.closest("[data-promo]"); if (!b) return; try { await api(`/promos/${encodeURIComponent(b.dataset.promo)}/toggle`, { method: "POST", body: JSON.stringify({ active: b.dataset.active === "true" }) }); loadPromos(); } catch (error) { toast(error.message, true); } });

$("linkForm").addEventListener("submit", async (e) => { e.preventDefault(); const data = Object.fromEntries(new FormData(e.target)); try { const d = await api("/links", { method: "POST", body: JSON.stringify(data) }); await navigator.clipboard.writeText(d.site_url || d.url).catch(() => {}); toast("Ссылка создана и скопирована"); e.target.reset(); loadLinks(); } catch (error) { toast(error.message, true); } });
async function loadLinks() { try { const d = await api("/links"); $("linksTable").innerHTML = d.items.length ? `<table><thead><tr><th>Ссылка</th><th>Переходы</th><th>Пользователи</th><th>Выручка</th><th>Статус</th><th></th></tr></thead><tbody>${d.items.map((l) => { const botUrl = l.bot_url || `https://t.me/${d.bot_username}?start=${l.code}`; const siteUrl = l.site_url || `${d.site_url}/?t=${l.code}`; return `<tr><td><b>${esc(l.title || l.code)}</b><br><button class="button small mono" data-copy="${esc(botUrl)}">Bot</button> <button class="button small mono" data-copy="${esc(siteUrl)}">Site</button><br><small>${esc(l.code)}</small></td><td>${l.clicks} / ${l.unique_clicks} уник.</td><td>${l.users_count}</td><td>${money(l.revenue)}</td><td><span class="badge ${l.is_active ? "" : "off"}">${l.is_active ? "Активна" : "Выключена"}</span></td><td><button class="button small" data-link="${esc(l.code)}" data-active="${!l.is_active}">${l.is_active ? "Выключить" : "Включить"}</button></td></tr>`; }).join("")}</tbody></table>` : `<div class="empty">Ссылок пока нет</div>`; } catch (error) { toast(error.message, true); } }
$("linksTable").addEventListener("click", async (e) => { const copy = e.target.closest("[data-copy]"); const b = e.target.closest("[data-link]"); if (copy) { await navigator.clipboard.writeText(copy.dataset.copy).catch(() => {}); toast("Ссылка скопирована"); } if (b) { try { await api(`/links/${encodeURIComponent(b.dataset.link)}/toggle`, { method: "POST", body: JSON.stringify({ active: b.dataset.active === "true" }) }); loadLinks(); } catch (error) { toast(error.message, true); } } });

const start = new Date(), end = new Date(Date.now() + 7 * 86400000); const localInput = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0,16); $("discountForm").elements.starts_at.value = localInput(start); $("discountForm").elements.ends_at.value = localInput(end);
function updateTargetCodes() { const target = $("discountTarget").value; const specific = ["tariff", "traffic_package"].includes(target); $("targetCodeLabel").classList.toggle("hidden", !specific); const options = target === "tariff" ? [["regular_1m","Обычная · 1 месяц"],["regular_3m","Обычная · 3 месяца"],["bypass_1m","Антиглушилка · 1 месяц"],["bypass_3m","Антиглушилка · 3 месяца"]] : [["gb_10","10 ГБ"],["gb_20","20 ГБ"],["gb_40","40 ГБ"],["gb_80","80 ГБ"],["gb_150","150 ГБ"]]; $("targetCode").innerHTML = specific ? options.map(([value,label]) => `<option value="${value}">${label}</option>`).join("") : ""; }
$("discountTarget").addEventListener("change", updateTargetCodes); updateTargetCodes();
$("discountForm").addEventListener("submit", async (e) => { e.preventDefault(); const data = Object.fromEntries(new FormData(e.target)); data.value = Number(data.value); data.starts_at = new Date(data.starts_at).toISOString(); data.ends_at = new Date(data.ends_at).toISOString(); if (!["tariff", "traffic_package"].includes(data.target_type)) data.target_code = null; try { await api("/discounts", { method: "POST", body: JSON.stringify(data) }); toast("Скидка запланирована"); loadDiscounts(); } catch (error) { toast(error.message, true); } });
function discountTarget(d) { return ({ all:"Всё", subscription:"Все подписки", regular:"Обычные", bypass:"Антиглушилка", tariff:`Тариф ${d.target_code || ""}`, traffic:"Все пакеты ГБ", traffic_package:`Пакет ${d.target_code || ""}` })[d.target_type] || d.target_type; }
async function loadDiscounts() { try { const d = await api("/discounts"); $("discountsTable").innerHTML = d.items.length ? `<table><thead><tr><th>Название</th><th>Скидка</th><th>Цель</th><th>Период</th><th>Статус</th><th></th></tr></thead><tbody>${d.items.map((x) => `<tr><td><b>${esc(x.name)}</b></td><td>${x.discount_type === "percent" ? `${Number(x.value)}%` : money(x.value)}</td><td>${esc(discountTarget(x))}</td><td>${date(x.starts_at, true)} — ${date(x.ends_at, true)}</td><td><span class="badge ${x.active ? "" : "off"}">${x.active ? "Включена" : "Выключена"}</span></td><td><div class="actions"><button class="button small" data-discount="${x.id}" data-active="${!x.active}">${x.active ? "Выключить" : "Включить"}</button><button class="button small danger" data-delete-discount="${x.id}">Удалить</button></div></td></tr>`).join("")}</tbody></table>` : `<div class="empty">Скидок пока нет</div>`; } catch (error) { toast(error.message, true); } }
$("discountsTable").addEventListener("click", async (e) => { const toggle = e.target.closest("[data-discount]"); const del = e.target.closest("[data-delete-discount]"); try { if (toggle) await api(`/discounts/${toggle.dataset.discount}/toggle`, { method:"POST", body:JSON.stringify({ active:toggle.dataset.active === "true" }) }); if (del) { if (!confirm("Удалить эту скидку?")) return; await api(`/discounts/${del.dataset.deleteDiscount}`, { method:"DELETE" }); } if (toggle || del) loadDiscounts(); } catch (error) { toast(error.message, true); } });

boot();
