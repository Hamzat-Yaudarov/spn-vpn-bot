const state = { section: "dashboard", usersOffset: 0, usersSearch: "", usersTotal: 0, pageSize: 50 };
const titles = { dashboard: "Обзор", users: "Пользователи", promos: "Промокоды", links: "Ссылки", discounts: "Скидки", "special-discounts": "Спец. скидки", withdrawals: "Выплаты", referrals: "Рефералы" };
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[ch]));
const money = (value) => `${Number(value || 0).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ₽`;
const date = (value, withTime = false) => value ? new Date(value).toLocaleString("ru-RU", withTime ? { dateStyle: "short", timeStyle: "short" } : { dateStyle: "short" }) : "—";
const isActiveDate = (value) => value && new Date(value).getTime() > Date.now();
function relativeTime(value) {
  if (!value) return "—";
  const diff = new Date(value).getTime() - Date.now();
  if (!Number.isFinite(diff)) return "—";
  if (diff <= 0) return "пора сбросить";
  const days = Math.floor(diff / 86400000);
  const hours = Math.floor((diff % 86400000) / 3600000);
  if (days > 0) return `через ${days} д. ${hours} ч.`;
  if (hours > 0) return `через ${hours} ч.`;
  return `через ${Math.max(1, Math.ceil(diff / 60000))} мин.`;
}
function trafficResetHtml(subscription) {
  if (subscription.plan_kind !== "bypass") {
    return `<div class="subscription-meta-item muted"><span>Сброс трафика</span><b>не используется</b></div>`;
  }
  const resetAt = subscription.traffic_reset_at;
  const isDue = resetAt && new Date(resetAt).getTime() <= Date.now() && isActiveDate(subscription.subscription_until);
  return `<div class="subscription-meta-item ${isDue ? "warning" : ""}"><span>Сброс трафика</span><b>${resetAt ? `${date(resetAt, true)} · ${relativeTime(resetAt)}` : "не назначен"}</b></div>`;
}
const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();
const initData = tg?.initData || "";

async function api(path, options = {}) {
  const response = await fetch(`/admin/api${path}`, { headers: { "Content-Type": "application/json", Authorization: `tma ${initData}`, ...(options.headers || {}) }, ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) showAccess(data.detail);
    throw new Error(Array.isArray(data.detail) ? data.detail.map(item => item.msg).join('; ') : (data.detail || "Ошибка запроса"));
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
  if (section === "special-discounts") loadSpecialDiscounts();
  if (section === "withdrawals") loadWithdrawals();
  if (section === "referrals") loadReferrals();
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
    const subs = d.subscriptions.map((s) => `<article class="subscription-card">
      <div>
        <h3>${s.plan_kind === "bypass" ? "С антиглушилкой" : "Обычная"} #${s.type_index || s.slot_number}</h3>
        <small>ID ${s.id} · до ${date(s.subscription_until, true)}</small>
        <div><span class="badge ${isActiveDate(s.subscription_until) ? "" : "off"}">${isActiveDate(s.subscription_until) ? "Активна" : "Истекла"}</span></div>
        <div class="subscription-meta">
          <div class="subscription-meta-item"><span>Действует до</span><b>${date(s.subscription_until, true)}</b></div>
          ${trafficResetHtml(s)}
        </div>
      </div>
      <div class="subscription-controls">
        <div class="actions"><button type="button" class="button small" data-adjust="${s.id}" data-days="7">+7 дней</button><button type="button" class="button small" data-adjust="${s.id}" data-days="30">+30 дней</button><button type="button" class="button small danger" data-delete-sub="${s.id}">Удалить</button></div>
        <div class="custom-days"><input data-days-input="${s.id}" type="number" min="-3650" max="3650" placeholder="+30 или −15" /><button type="button" class="button small blue" data-custom-adjust="${s.id}">Изменить срок</button></div>
      </div>
    </article>`).join("");
    const payments = d.payments.length ? `<table><thead><tr><th>Дата</th><th>Тариф</th><th>Сумма</th><th>Статус</th></tr></thead><tbody>${d.payments.map((p) => `<tr><td>${date(p.created_at, true)}</td><td>${esc(p.tariff_code)}</td><td>${money(p.amount)}</td><td>${esc(p.status)}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">Платежей пока нет</div>`;
    $("userDetails").innerHTML = `<div class="details-body"><div class="details-grid"><div class="detail-tile"><span>${web ? "Веб-аккаунт" : "Telegram ID"}</span><b class="mono">${web ? esc(web.login) : u.tg_id}</b></div><div class="detail-tile"><span>Регистрация</span><b>${date(u.created_at, true)}</b></div><div class="detail-tile"><span>Тип аккаунта</span><b>${web ? "Сайт" : "Telegram"}</b></div></div><div><h3>Подписки</h3><div class="subscription-list">${subs || `<div class="empty">Подписок нет</div>`}</div></div><div><h3>Последние платежи</h3><div class="table-wrap">${payments}</div></div></div>`;
    $("userDialog").dataset.user = tgId; if (!$("userDialog").open) $("userDialog").showModal();
  } catch (error) { toast(error.message, true); }
}
$("closeDialog").addEventListener("click", () => $("userDialog").close());
$("userDetails").addEventListener("click", async (e) => {
  const adjust = e.target.closest("[data-adjust]"); const custom = e.target.closest("[data-custom-adjust]"); const del = e.target.closest("[data-delete-sub]");
  try {
    if (adjust) { const days = Number(adjust.dataset.days); if (!confirm(`${days > 0 ? "Добавить" : "Убрать"} ${Math.abs(days)} дней?`)) return; await api(`/subscriptions/${adjust.dataset.adjust}/adjust`, { method: "POST", body: JSON.stringify({ days }) }); toast("Срок подписки обновлён"); await openUser($("userDialog").dataset.user); }
    if (custom) {
      const input = $("userDetails").querySelector(`[data-days-input="${custom.dataset.customAdjust}"]`);
      const days = Number(input?.value);
      if (!Number.isInteger(days) || days === 0 || Math.abs(days) > 3650) { toast("Введите целое число от −3650 до 3650, кроме нуля", true); return; }
      if (!confirm(`${days > 0 ? "Добавить" : "Убрать"} ${Math.abs(days)} дней?`)) return;
      await api(`/subscriptions/${custom.dataset.customAdjust}/adjust`, { method: "POST", body: JSON.stringify({ days }) });
      toast("Срок подписки обновлён");
      await openUser($("userDialog").dataset.user);
    }
    if (del) { if (!confirm("Удалить подписку из бота и Remnawave?")) return; del.disabled = true; await api(`/subscriptions/${del.dataset.deleteSub}`, { method: "DELETE" }); toast("Подписка удалена из бота и Remnawave"); await openUser($("userDialog").dataset.user); }
  } catch (error) { toast(error.message, true); }
});

$("promoForm").addEventListener("submit", async (e) => { e.preventDefault(); const data = Object.fromEntries(new FormData(e.target)); data.days = Number(data.days); data.max_uses = Number(data.max_uses); try { await api("/promos", { method: "POST", body: JSON.stringify(data) }); toast("Промокод создан"); e.target.reset(); loadPromos(); } catch (error) { toast(error.message, true); } });
async function loadPromos() { try { const d = await api("/promos"); $("promosTable").innerHTML = d.items.length ? `<table><thead><tr><th>Код</th><th>Дней</th><th>Использовано</th><th>Статус</th><th></th></tr></thead><tbody>${d.items.map((p) => `<tr><td class="mono"><b>${esc(p.code)}</b></td><td>${p.days}</td><td>${p.used_count} / ${p.max_uses}</td><td><span class="badge ${p.active ? "" : "off"}">${p.active ? "Активен" : "Выключен"}</span></td><td><button class="button small" data-promo="${esc(p.code)}" data-active="${!p.active}">${p.active ? "Выключить" : "Включить"}</button></td></tr>`).join("")}</tbody></table>` : `<div class="empty">Промокодов пока нет</div>`; } catch (error) { toast(error.message, true); } }
$("promosTable").addEventListener("click", async (e) => { const b = e.target.closest("[data-promo]"); if (!b) return; try { await api(`/promos/${encodeURIComponent(b.dataset.promo)}/toggle`, { method: "POST", body: JSON.stringify({ active: b.dataset.active === "true" }) }); loadPromos(); } catch (error) { toast(error.message, true); } });

$("linkForm").addEventListener("submit", async (e) => { e.preventDefault(); const data = Object.fromEntries(new FormData(e.target)); try { const d = await api("/links", { method: "POST", body: JSON.stringify(data) }); await navigator.clipboard.writeText(d.site_url || d.url).catch(() => {}); toast("Ссылка создана и скопирована"); e.target.reset(); loadLinks(); } catch (error) { toast(error.message, true); } });
async function loadLinks() { try { const d = await api("/links"); $("linksTable").innerHTML = d.items.length ? `<table><thead><tr><th>Ссылка</th><th>Переходы</th><th>Пользователи</th><th>Уникальные покупатели</th><th>Покупки</th><th>Выручка</th><th>Статус</th><th></th></tr></thead><tbody>${d.items.map((l) => { const botUrl = l.bot_url || `https://t.me/${d.bot_username}?start=${l.code}`; const siteUrl = l.site_url || `${d.site_url}/?t=${l.code}`; return `<tr><td><b>${esc(l.title || l.code)}</b><br><button class="button small mono" data-copy="${esc(botUrl)}">Bot</button> <button class="button small mono" data-copy="${esc(siteUrl)}">Site</button><br><small>${esc(l.code)}</small></td><td>${l.clicks} / ${l.unique_clicks} уник.</td><td>${l.users_count}</td><td><b>${Number(l.unique_buyers || 0)}</b></td><td><b>${Number(l.purchases_count || 0)}</b></td><td>${money(l.revenue)}</td><td><span class="badge ${l.is_active ? "" : "off"}">${l.is_active ? "Активна" : "Выключена"}</span></td><td><button class="button small" data-link="${esc(l.code)}" data-active="${!l.is_active}">${l.is_active ? "Выключить" : "Включить"}</button></td></tr>`; }).join("")}</tbody></table>` : `<div class="empty">Ссылок пока нет</div>`; } catch (error) { toast(error.message, true); } }
$("linksTable").addEventListener("click", async (e) => { const copy = e.target.closest("[data-copy]"); const b = e.target.closest("[data-link]"); if (copy) { await navigator.clipboard.writeText(copy.dataset.copy).catch(() => {}); toast("Ссылка скопирована"); } if (b) { try { await api(`/links/${encodeURIComponent(b.dataset.link)}/toggle`, { method: "POST", body: JSON.stringify({ active: b.dataset.active === "true" }) }); loadLinks(); } catch (error) { toast(error.message, true); } } });

const start = new Date(), end = new Date(Date.now() + 7 * 86400000); const localInput = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0,16); $("discountForm").elements.starts_at.value = localInput(start); $("discountForm").elements.ends_at.value = localInput(end);
function updateTargetCodes() { const target = $("discountTarget").value; const specific = ["tariff", "traffic_package"].includes(target); $("targetCodeLabel").classList.toggle("hidden", !specific); const options = target === "tariff" ? [["regular_1m","Обычная · 1 месяц"],["regular_3m","Обычная · 3 месяца"],["bypass_1m","Антиглушилка · 1 месяц"],["bypass_3m","Антиглушилка · 3 месяца"]] : [["gb_10","10 ГБ"],["gb_20","20 ГБ"],["gb_40","40 ГБ"],["gb_80","80 ГБ"],["gb_150","150 ГБ"]]; $("targetCode").innerHTML = specific ? options.map(([value,label]) => `<option value="${value}">${label}</option>`).join("") : ""; }
$("discountTarget").addEventListener("change", updateTargetCodes); updateTargetCodes();
$("discountForm").addEventListener("submit", async (e) => { e.preventDefault(); const data = Object.fromEntries(new FormData(e.target)); data.value = Number(data.value); data.starts_at = new Date(data.starts_at).toISOString(); data.ends_at = new Date(data.ends_at).toISOString(); if (!["tariff", "traffic_package"].includes(data.target_type)) data.target_code = null; try { await api("/discounts", { method: "POST", body: JSON.stringify(data) }); toast("Скидка запланирована"); loadDiscounts(); } catch (error) { toast(error.message, true); } });
function discountTarget(d) { return ({ all:"Всё", subscription:"Все подписки", regular:"Обычные", bypass:"Антиглушилка", tariff:`Тариф ${d.target_code || ""}`, traffic:"Все пакеты ГБ", traffic_package:`Пакет ${d.target_code || ""}` })[d.target_type] || d.target_type; }
async function loadDiscounts() { try { const d = await api("/discounts"); $("discountsTable").innerHTML = d.items.length ? `<table><thead><tr><th>Название</th><th>Скидка</th><th>Цель</th><th>Период</th><th>Статус</th><th></th></tr></thead><tbody>${d.items.map((x) => `<tr><td><b>${esc(x.name)}</b></td><td>${x.discount_type === "percent" ? `${Number(x.value)}%` : money(x.value)}</td><td>${esc(discountTarget(x))}</td><td>${date(x.starts_at, true)} — ${date(x.ends_at, true)}</td><td><span class="badge ${x.active ? "" : "off"}">${x.active ? "Включена" : "Выключена"}</span></td><td><div class="actions"><button class="button small" data-discount="${x.id}" data-active="${!x.active}">${x.active ? "Выключить" : "Включить"}</button><button class="button small danger" data-delete-discount="${x.id}">Удалить</button></div></td></tr>`).join("")}</tbody></table>` : `<div class="empty">Скидок пока нет</div>`; } catch (error) { toast(error.message, true); } }
$("discountsTable").addEventListener("click", async (e) => { const toggle = e.target.closest("[data-discount]"); const del = e.target.closest("[data-delete-discount]"); try { if (toggle) await api(`/discounts/${toggle.dataset.discount}/toggle`, { method:"POST", body:JSON.stringify({ active:toggle.dataset.active === "true" }) }); if (del) { if (!confirm("Удалить эту скидку?")) return; await api(`/discounts/${del.dataset.deleteDiscount}`, { method:"DELETE" }); } if (toggle || del) loadDiscounts(); } catch (error) { toast(error.message, true); } });

const specialDiscountForm = $("specialDiscountForm");
const specialDiscountAudiences = { all: "Новые и старые", new: "Новые", existing: "Старые" };
const specialDiscountPayments = { any: "Покупка или продление", new: "Покупка", renew: "Продление" };
let specialDiscountItems = [];
let specialDiscountEditing = null;

function setSpecialDiscountForm(campaign = null) {
  const fields = specialDiscountForm.elements;
  specialDiscountEditing = campaign;
  fields.name.value = campaign?.name ?? "";
  fields.percent.value = campaign?.percent ?? "";
  fields.audience.value = campaign?.audience ?? "all";
  fields.payment_target.value = campaign?.payment_target ?? "any";
  fields.inactive_days.value = campaign?.inactive_days ?? "";
  fields.starts_at.value = campaign ? localInput(new Date(campaign.starts_at)) : "";
  fields.starts_at.required = Boolean(campaign);
  fields.ends_at.value = campaign?.ends_at ? localInput(new Date(campaign.ends_at)) : "";
  fields.active.checked = campaign?.active ?? true;
  specialDiscountForm.querySelectorAll('[name="tariff_codes"]').forEach((input) => {
    input.checked = campaign?.tariff_codes.includes(input.value) ?? false;
  });
  $("specialDiscountStartLabel").textContent = campaign ? "Начало" : "Начало (пусто — при сохранении)";
  $("saveSpecialDiscount").textContent = campaign ? "Сохранить акцию" : "Создать акцию";
  $("cancelSpecialDiscountEdit").classList.toggle("hidden", !campaign);
}

function specialDiscountDate(fields, name) {
  const value = fields.get(name);
  if (!value) return null;
  const previous = specialDiscountEditing?.[name];
  if (previous && value === localInput(new Date(previous))) return previous;
  return new Date(value).toISOString();
}

function specialDiscountStatus(campaign, now) {
  if (!campaign.active) return "Выключена";
  if (new Date(campaign.starts_at).getTime() > now) return "Запланирована";
  if (campaign.ends_at && new Date(campaign.ends_at).getTime() <= now) return "Завершена";
  return "Действует";
}

async function loadSpecialDiscounts() {
  try {
    const { items, tariffs } = await api("/special-discounts");
    specialDiscountItems = items;
    const choices = $("specialDiscountTariffChoices");
    const selectedTariffs = new FormData(specialDiscountForm).getAll("tariff_codes");
    choices.innerHTML = tariffs.map(({ code, title }) => `<label class="campaign-checkbox">
      <input type="checkbox" name="tariff_codes" value="${esc(code)}" ${selectedTariffs.includes(code) ? "checked" : ""} /> ${esc(title)}
    </label>`).join("");
    const tariffTitles = Object.fromEntries(tariffs.map(({ code, title }) => [code, title]));
    const now = Date.now();
    $("specialDiscountsTable").innerHTML = items.length ? `<table>
      <thead><tr><th>Акция</th><th>Условия</th><th>Период</th><th>Статус</th><th>Управление</th></tr></thead>
      <tbody>${items.map((campaign) => {
        const tariffNames = campaign.tariff_codes.map((code) => tariffTitles[code] || code);
        const inactivity = campaign.inactive_days === null ? "" : `<br>Без активной подписки от ${Number(campaign.inactive_days)} дн.`;
        return `<tr>
          <td><b>${esc(campaign.name)}</b><br>${Number(campaign.percent).toLocaleString("ru-RU")}%</td>
          <td>${specialDiscountAudiences[campaign.audience]} · ${specialDiscountPayments[campaign.payment_target]}<br>${esc(tariffNames.join(", ") || "Все тарифы")}${inactivity}</td>
          <td>${date(campaign.starts_at, true)}<br>${campaign.ends_at ? `до ${date(campaign.ends_at, true)}` : "Без окончания"}</td>
          <td><span class="badge ${campaign.active ? "" : "off"}">${specialDiscountStatus(campaign, now)}</span></td>
          <td><div class="actions">
            <button class="button small" data-campaign-edit="${campaign.id}">Изменить</button>
            <button class="button small" data-campaign-toggle="${campaign.id}" data-active="${!campaign.active}">${campaign.active ? "Выключить" : "Включить"}</button>
          </div></td>
        </tr>`;
      }).join("")}</tbody>
    </table>` : '<div class="empty">Акций пока нет</div>';
  } catch (error) { toast(error.message, true); }
}

$("cancelSpecialDiscountEdit").addEventListener("click", () => setSpecialDiscountForm());
setSpecialDiscountForm();

specialDiscountForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = new FormData(specialDiscountForm);
  const payload = {
    name: fields.get("name"),
    percent: fields.get("percent"),
    audience: fields.get("audience"),
    payment_target: fields.get("payment_target"),
    inactive_days: fields.get("inactive_days") ? Number(fields.get("inactive_days")) : null,
    tariff_codes: fields.getAll("tariff_codes"),
    starts_at: specialDiscountDate(fields, "starts_at"),
    ends_at: specialDiscountDate(fields, "ends_at"),
    active: fields.has("active"),
  };
  const editing = specialDiscountEditing;
  const submit = $("saveSpecialDiscount");
  submit.disabled = true;
  try {
    await api(editing ? `/special-discounts/${editing.id}` : "/special-discounts", {
      method: editing ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    toast(editing ? "Акция сохранена" : "Акция создана");
    setSpecialDiscountForm();
    await loadSpecialDiscounts();
  } catch (error) { toast(error.message, true); }
  finally { submit.disabled = false; }
});

$("specialDiscountsTable").addEventListener("click", async (event) => {
  const edit = event.target.closest("[data-campaign-edit]");
  if (edit) {
    const campaign = specialDiscountItems.find((item) => String(item.id) === edit.dataset.campaignEdit);
    if (campaign) {
      setSpecialDiscountForm(campaign);
      specialDiscountForm.elements.name.focus();
    }
    return;
  }
  const toggle = event.target.closest("[data-campaign-toggle]");
  if (!toggle) return;
  const active = toggle.dataset.active === "true";
  toggle.disabled = true;
  try {
    await api(`/special-discounts/${toggle.dataset.campaignToggle}/toggle`, {
      method: "POST",
      body: JSON.stringify({ active }),
    });
    if (String(specialDiscountEditing?.id) === toggle.dataset.campaignToggle) specialDiscountForm.elements.active.checked = active;
    await loadSpecialDiscounts();
  } catch (error) { toast(error.message, true); }
  finally { toggle.disabled = false; }
});

let withdrawalOffset = 0;
const payoutStatus = { pending: "На рассмотрении", completed: "Выплачено", rejected: "Отклонено" };
async function loadWithdrawals() {
  try {
    const d = await api('/withdrawals?status=' + encodeURIComponent($("withdrawalStatus").value) + '&offset=' + withdrawalOffset);
    $("withdrawalsTable").innerHTML = d.items.length ? '<table><thead><tr><th>Заявка</th><th>Пользователь</th><th>Сумма и реквизиты</th><th>Статус</th><th>Уведомление</th><th>Решение</th></tr></thead><tbody>' + d.items.map(w => {
      const number = (w.program === 'referral' ? 'R-' : 'P-') + w.id;
      const details = w.withdrawal_type === 'sbp' ? esc(w.bank_name) + '<br>' + esc(w.phone_number) : 'USDT (' + (String(w.usdt_address).startsWith('T') ? 'TRC-20' : 'ERC-20') + ')<br><span class="mono">' + esc(w.usdt_address) + '</span>';
      const blocked = w.program === 'referral' && w.status === 'pending' && (w.payout_blocked === true || Number(w.current_available_balance) < 0);
      const balanceInfo = w.program === 'referral' && w.status === 'pending' && w.current_available_balance != null ? '<p class="muted">Доступный баланс сейчас: ' + money(w.current_available_balance) + '</p>' : '';
      const warning = blocked ? '<p class="badge off">Выплата заблокирована: отрицательный баланс</p><p class="muted">Не переводите деньги. Отклоните заявку или дождитесь новых начислений.</p>' : '';
      const actions = w.status === 'pending' ? warning + balanceInfo + '<button class="button small" ' + (blocked ? 'disabled ' : '') + 'data-payout="' + esc(w.program) + ':' + w.id + '" data-decision="completed" data-amount="' + esc(w.amount) + '">Перевод выполнен</button> <button class="button small danger" data-payout="' + esc(w.program) + ':' + w.id + '" data-decision="rejected" data-amount="' + esc(w.amount) + '">Отклонить</button>' : esc(w.decision_note || '—');
      return '<tr><td><b>' + number + '</b><br>' + date(w.created_at, true) + '</td><td>' + esc(w.username || 'Без username') + '<br>' + esc(w.user_id) + '</td><td><b>' + money(w.amount) + '</b><br>' + details + '</td><td>' + esc(payoutStatus[w.status] || w.status) + '</td><td>' + (w.notified_at ? 'Доставлено' : 'В очереди · попыток ' + (w.notification_attempts || 0)) + (w.notification_error ? '<br>' + esc(w.notification_error) : '') + '</td><td>' + actions + '</td></tr>';
    }).join('') + '</tbody></table>' : '<div class="empty">Заявок с таким статусом нет</div>';
    $("withdrawalsPagination").innerHTML = '<button class="button small" data-payout-page="prev" ' + (withdrawalOffset ? '' : 'disabled') + '>Назад</button><span>' + (withdrawalOffset + 1) + '</span><button class="button small" data-payout-page="next" ' + (d.has_more ? '' : 'disabled') + '>Дальше</button>';
  } catch (error) { toast(error.message, true); }
}
$("withdrawalStatus").addEventListener("change", () => { withdrawalOffset = 0; loadWithdrawals(); });
$("refreshWithdrawals").addEventListener("click", loadWithdrawals);
$("withdrawalsPagination").addEventListener("click", e => {
  const direction = e.target.dataset.payoutPage;
  if (!direction) return;
  withdrawalOffset = Math.max(0, withdrawalOffset + (direction === 'next' ? 30 : -30));
  loadWithdrawals();
});
$("withdrawalsTable").addEventListener("click", async e => {
  const button = e.target.closest('[data-payout]');
  if (!button || button.disabled) return;
  const [program, id] = button.dataset.payout.split(':');
  const status = button.dataset.decision;
  let note = '';
  if (status === 'completed') {
    if (!confirm('Вы уже перевели ' + money(button.dataset.amount) + ' по реквизитам заявки ' + (program === 'referral' ? 'R-' : 'P-') + id + '? Кнопка НЕ переводит деньги.')) return;
  } else {
    note = prompt('Причина отклонения (будет отправлена пользователю):');
    if (!note?.trim()) return;
  }
  button.disabled = true;
  try {
    await api('/withdrawals/' + program + '/' + id, { method: 'POST', body: JSON.stringify({ status, note, confirm_paid: status === 'completed' }) });
    toast(status === 'completed' ? 'Выплата отмечена' : 'Заявка отклонена, резерв освобождён');
    await loadWithdrawals();
  } catch (error) { toast(error.message, true); button.disabled = false; }
});

let referralReviewOffset = 0;
async function loadReferrals() {
  try {
    const [d, report] = await Promise.all([api('/referrals/funnel'), api('/referrals/review?limit=30&offset=' + referralReviewOffset)]);
    const cards = [
      ['Открытий кабинета (с запуска учёта)', Number(d.views || 0), 'blue'],
      ['Уникальных открывших', Number(d.unique_viewers || 0), 'blue'],
      ['Регистраций по приглашению', Number(d.attributed_registrations || 0), 'green'],
      ['Первых оплат подписки', Number(d.first_payments || 0), 'green'],
      ['Повторных оплат подписки', Number(d.repeat_payments || 0), 'green'],
      ['Внешние оплаты подписок', money(d.external_paid_amount), 'gold'],
      ['Начислено по связанным счетам', money(d.credited_earnings), 'gold'],
      ['Списано за возвраты', money(d.reversed_earnings), 'gold'],
      ['Потрачено на свою подписку', money(d.wallet_spend), 'blue'],
      ['Выплачено деньгами', money(d.cash_payouts), 'blue'],
      ['Резерв заявок на вывод', money(d.pending_cash_payouts), 'blue'],
      ['Старых непривязанных начислений', Number(d.unlinked_legacy_earnings || 0), 'gold'],
    ];
    $('referralStatsGrid').innerHTML = cards.map(([label, value, cls]) => `<div class="stat-card ${cls}"><span>${esc(label)}</span><b>${value}</b></div>`).join('');
    const rows = report.items || [];
    $('referralReviewTable').innerHTML = rows.length ? '<table><thead><tr><th>Проблема</th><th>Счёт / платёж</th><th>Пользователь</th><th>Сумма</th><th>Сверка</th></tr></thead><tbody>' + rows.map(r => `<tr><td>${esc(r.kind || 'Ручная сверка')}</td><td class="mono">${esc(r.invoice_id || 'Нет связи со счётом')}<br>${esc(r.payment_id || r.source_id || '—')}</td><td>${esc(r.buyer_id || r.referrer_id || '—')}</td><td>${money(r.amount)}</td><td>${esc(r.detail || r.reason || 'Проверить вручную; автоматического пересчёта нет.')}</td></tr>`).join('') + '</tbody></table>' : '<div class="empty">Записей для ручной сверки не найдено</div>';
    $('referralReviewPagination').innerHTML = '<button class="button small" data-review-page="prev" ' + (referralReviewOffset ? '' : 'disabled') + '>Назад</button><span>' + (referralReviewOffset + 1) + '</span><button class="button small" data-review-page="next" ' + (report.has_more ? '' : 'disabled') + '>Дальше</button>';
  } catch (error) { toast(error.message, true); }
}
$('refreshReferrals').addEventListener('click', loadReferrals);
$('referralReviewPagination').addEventListener('click', event => {
  const direction = event.target.dataset.reviewPage;
  if (!direction || event.target.disabled) return;
  referralReviewOffset = Math.max(0, referralReviewOffset + (direction === 'next' ? 30 : -30));
  loadReferrals();
});

let preparedRefund = null;
let refundSubmitting = false;
function cancelRefundReview() { preparedRefund = null; $('refundActualTransfer').checked = false; $('confirmRefund').disabled = true; $('refundDialog').close(); }
$('closeRefundDialog').addEventListener('click', cancelRefundReview);
$('cancelRefund').addEventListener('click', cancelRefundReview);
$('refundDialog').addEventListener('cancel', () => { preparedRefund = null; });
$('refundActualTransfer').addEventListener('change', () => { $('confirmRefund').disabled = refundSubmitting || !preparedRefund || !$('refundActualTransfer').checked; });
$('refundForm').addEventListener('submit', async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  for (const key of ['invoice_id', 'reference', 'note']) data[key] = data[key].trim();
  data.amount = data.amount.trim().replace(',', '.');
  if (!/^\d+(?:\.\d{1,2})?$/.test(data.amount) || Number(data.amount) <= 0) { toast('Введите положительную сумму в рублях, не более двух знаков после запятой.', true); return; }
  const button = event.target.querySelector('button[type="submit"]');
  button.disabled = true; preparedRefund = null;
  try {
    const review = await api('/referrals/refunds/prepare', {method:'POST', body:JSON.stringify(data)});
    const p = review.preview;
    preparedRefund = {...data, review_token:review.review_token};
    $('refundReviewDetails').innerHTML = `<div class="details-grid"><div class="detail-tile"><span>Invoice ID</span><b class="mono">${esc(data.invoice_id)}</b></div><div class="detail-tile"><span>Фактически оплачено</span><b>${money(p.actual_paid_rub)}</b></div><div class="detail-tile"><span>Уже возвращено</span><b>${money(p.refunded_rub)}</b></div><div class="detail-tile"><span>Остаток для возврата</span><b>${money(p.remaining_rub)}</b></div><div class="detail-tile"><span>${p.already_recorded ? 'Уже записанный перевод' : 'Сейчас учитываем'}</span><b>${money(data.amount)}</b></div><div class="detail-tile"><span>Платёж / пользователь</span><b>${esc(p.payment_id)} / ${esc(p.buyer_id)}</b></div></div><p>Подтверждение перевода: <span class="mono">${esc(data.reference)}</span></p><p>Причина: ${esc(p.recorded_refund?.note || data.note)}</p>${p.already_recorded ? '<p class="muted">Этот перевод уже учтён. Повторная проверка не создаст второй возврат, не изменит исходную причину и не спишет начисление ещё раз.</p>' : ''}${p.manual_review_required ? '<p class="muted">ВНИМАНИЕ: старый счёт без связанного начисления. Возврат можно учесть, но старые начисления не будут пересчитаны автоматически: требуется отдельная ручная сверка.</p>' : ''}`;
    $('refundActualTransfer').checked = false; $('confirmRefund').disabled = true;
    $('refundDialog').showModal();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});
$('confirmRefund').addEventListener('click', async () => {
  if (refundSubmitting || !preparedRefund || !$('refundActualTransfer').checked || $('confirmRefund').disabled) return;
  refundSubmitting = true; $('confirmRefund').disabled = true;
  try {
    const result = await api('/referrals/refunds/confirm', {method:'POST', body:JSON.stringify({...preparedRefund, confirm_transferred:true})});
    cancelRefundReview(); $('refundForm').reset();
    $('refundResult').textContent = result.legacy_review_required ? 'Возврат учтён. Старые непривязанные начисления требуют отдельной ручной сверки.' : 'Выполненный возврат учтён; пересчёт применяется только к связанным начислениям. Деньги этим действием не отправлялись.';
    toast('Выполненный возврат учтён'); await loadReferrals();
  } catch (error) { toast(error.message, true); }
  finally { refundSubmitting = false; $('confirmRefund').disabled = !preparedRefund || !$('refundActualTransfer').checked; }
});

boot();
