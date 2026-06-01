const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
}

const initData = tg?.initData || "";
const state = {
  me: null,
  subs: [],
  tariffs: null,
  referral: null,
  keysMode: "list",
  selectedSubId: null,
  buyMode: "plan",
  buyPlan: null,
  buyTariffCode: null,
  pendingPayment: null,
};

function api(path, options = {}) {
  return fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "Authorization": `tma ${initData}`,
      ...(options.headers || {}),
    },
  }).then(async (res) => {
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Ошибка запроса");
    return data;
  });
}

function el(id) { return document.getElementById(id); }
function rub(value) { return `${Number(value).toLocaleString("ru-RU")} ₽`; }
function date(value) { return value ? new Date(value).toLocaleDateString("ru-RU") : "неизвестно"; }
function showToast(text) { const t = el("toast"); t.textContent = text; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 2400); }
function openLink(url) { tg?.openLink ? tg.openLink(url) : window.open(url, "_blank"); }
function subTitle(s) { return `${s.plan_kind === "bypass" ? "С антиглушилкой" : "Обычная"} #${s.type_index}`; }
function activeSubs() { return state.subs.filter((s) => s.status === "active"); }
function selectedSub() { return state.subs.find((s) => s.id === state.selectedSubId); }
function tariffPeriod(t) { return t.days === 30 ? "1 месяц" : t.days === 90 ? "3 месяца" : `${t.days} дней`; }
function planName(plan) { return plan === "bypass" ? "Антиглушилка" : "Обычная"; }
function planClass(plan) { return plan === "bypass" ? "bypass" : "regular"; }
function planTag(plan) { return plan === "bypass" ? "3 устройства · 80 ГБ/мес" : "5 устройств · без лимита ГБ"; }
function planText(plan) { return plan === "bypass" ? "Для связи при блокировках и нестабильной сети." : "Для повседневного VPN на телефоне, ноутбуке и других устройствах."; }
function daysLeft(value) {
  if (!value) return null;
  const diff = new Date(value).getTime() - Date.now();
  return Math.max(0, Math.ceil(diff / 86400000));
}
function nearestActiveUntil() {
  const dates = activeSubs().map((s) => new Date(s.subscription_until).getTime()).filter(Boolean);
  return dates.length ? new Date(Math.min(...dates)).toISOString() : null;
}
function cheapest(plan) {
  const tariffs = state.tariffs?.[plan] || [];
  return tariffs.reduce((best, t) => !best || t.price < best.price ? t : best, null);
}
function trafficPercent(s) {
  return s.traffic.enabled && s.traffic.limit_gb ? Math.min(100, Math.round((s.traffic.used_gb / s.traffic.limit_gb) * 100)) : 0;
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view));
});

function switchView(view) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  el(`view-${view}`).classList.add("active");
  render();
}

async function load() {
  try {
    const [me, tariffs, subscriptions, referral] = await Promise.all([
      api("/miniapp/api/me"),
      api("/miniapp/api/tariffs"),
      api("/miniapp/api/subscriptions"),
      api("/miniapp/api/referral"),
    ]);
    state.me = me;
    state.tariffs = tariffs;
    state.subs = subscriptions.subscriptions || [];
    state.referral = referral;
    el("userLine").textContent = me.first_name ? `Добро пожаловать, ${me.first_name}` : "Управляйте подписками в пару кликов";
    render();
  } catch (e) {
    el("userLine").textContent = "Откройте кабинет внутри Telegram";
    document.querySelector("main").innerHTML = `<section class="view active"><div class="card empty"><h2>Не удалось открыть MiniApp</h2><p class="muted">${e.message}</p></div></section>`;
  }
}

async function reloadData() {
  const subscriptions = await api("/miniapp/api/subscriptions");
  state.subs = subscriptions.subscriptions || [];
  render();
}

function render() {
  renderHome();
  renderKeys();
  renderBuy();
  renderReferral();
  renderHelp();
}

function renderHome() {
  const hasBypass = activeSubs().some((s) => s.plan_kind === "bypass");
  const activeCount = activeSubs().length;
  const nearest = nearestActiveUntil();
  el("view-home").innerHTML = `
    <div class="grid">
      <div class="card hero-card">
        <p class="step">Главная</p>
        <p class="title">${activeCount ? `Активных ключей: ${activeCount}` : "Подключите Way SPN"}</p>
        <p class="muted">${activeCount ? `Ближайшее окончание: ${date(nearest)}. Все ключи, продления и ГБ доступны здесь.` : "Купите подписку, получите ключ и подключитесь за пару минут."}</p>
        <div class="quick-grid">
          <button class="button accent" onclick="openNewPurchase()">Купить подписку</button>
          <button class="button ghost" onclick="openKeysList()">Мои ключи</button>
          ${state.subs.length ? `<button class="button ghost" onclick="openRenewList()">Продлить</button>` : ""}
          ${hasBypass ? `<button class="button green" onclick="openTrafficFromHome()">Купить ГБ</button>` : ""}
        </div>
      </div>
      <div class="split-cards">
        <div class="info-card">
          <span class="mini-badge regular">Обычная</span>
          <b>5 устройств</b>
          <p>Для ежедневного VPN без лимита трафика.</p>
        </div>
        <div class="info-card">
          <span class="mini-badge bypass">Антиглушилка</span>
          <b>3 устройства</b>
          <p>80 ГБ/мес для работы при блокировках.</p>
        </div>
      </div>
    </div>`;
}

function renderKeys() {
  const container = el("view-subs");
  if (!state.subs.length) {
    container.innerHTML = `<div class="card empty"><h2>Ключей пока нет</h2><p class="muted">Купите первую подписку, и ключ появится здесь.</p><button class="button accent" onclick="openNewPurchase()">Купить подписку</button></div>`;
    return;
  }

  if (state.keysMode === "list") {
    container.innerHTML = keysListHtml("Мои ключи", "Выберите ключ, чтобы открыть информацию, скопировать ссылку или продлить.");
    return;
  }

  if (state.keysMode === "renew-list") {
    container.innerHTML = keysListHtml("Что продлить?", "Выберите ключ, который хотите продлить.", "renew");
    return;
  }

  const sub = selectedSub();
  if (!sub) {
    state.keysMode = "list";
    container.innerHTML = keysListHtml("Мои ключи", "Выберите ключ.");
    return;
  }

  if (state.keysMode === "detail") container.innerHTML = subscriptionDetailHtml(sub);
  if (state.keysMode === "renew") container.innerHTML = renewHtml(sub);
  if (state.keysMode === "traffic") container.innerHTML = trafficHtml(sub);
  if (state.keysMode === "traffic-payment") container.innerHTML = paymentHtml("ГБ для " + subTitle(sub));
  if (state.keysMode === "renew-payment") container.innerHTML = paymentHtml("Продление " + subTitle(sub));
}

function keysListHtml(title, subtitle, action = "detail") {
  return `<div class="grid">
    <div class="card"><p class="step">Ключи</p><p class="title">${title}</p><p class="muted">${subtitle}</p></div>
    <div class="choice-list">${state.subs.map((s) => keyButton(s, action)).join("")}</div>
  </div>`;
}

function keyButton(s, action) {
  const handler = action === "renew" ? `openRenew(${s.id})` : `openSubDetail(${s.id})`;
  const left = daysLeft(s.subscription_until);
  const traffic = s.traffic.enabled ? `<small>ГБ: ${s.traffic.used_gb} / ${s.traffic.limit_gb}</small>` : "";
  return `<button class="choice-button key-choice ${planClass(s.plan_kind)}" onclick="${handler}">
    <span><i class="mini-badge ${planClass(s.plan_kind)}">${planName(s.plan_kind)}</i>${subTitle(s)}<small>${s.status === "active" ? `До ${date(s.subscription_until)}${left !== null ? ` · ${left} дн.` : ""}` : "Истекла · можно продлить"}</small>${traffic}</span>
    <b>${action === "renew" ? "Продлить" : "Открыть"}</b>
  </button>`;
}

function subscriptionDetailHtml(s) {
  const percent = trafficPercent(s);
  const left = daysLeft(s.subscription_until);
  return `<div class="grid">
    <button class="button ghost" onclick="openKeysList()">← Назад к ключам</button>
    <div class="card strong detail-head ${planClass(s.plan_kind)}">
      <div class="row start"><div><p class="step">${planTag(s.plan_kind)}</p><p class="title">${subTitle(s)}</p><p class="muted">${s.status === "active" ? `Активна до ${date(s.subscription_until)}${left !== null ? ` · осталось ${left} дн.` : ""}` : "Подписка истекла, её можно продлить."}</p></div><span class="badge ${s.status === "active" ? "ok" : "warn"}">${s.status === "active" ? "Активна" : "Истекла"}</span></div>
      ${s.traffic.enabled ? `<div class="traffic-card"><div class="row"><span class="small">Трафик антиглушилки</span><b>${s.traffic.used_gb} / ${s.traffic.limit_gb} ГБ</b></div><div class="progress"><span style="width:${percent}%"></span></div><p class="small">Следующий сброс: ${date(s.traffic.reset_at)}</p></div>` : ""}
    </div>
    <div class="card"><p class="step">Подключение</p><p class="title">Ключ</p>${s.subscription_url ? `<div class="keybox">${s.subscription_url}</div><button class="button accent" onclick="copyText('${encodeURIComponent(s.subscription_url)}')">Скопировать ключ</button>` : `<p class="muted">Ключ появится после активации оплаты.</p>`}</div>
    <div class="action-row"><button class="button ghost" onclick="openRenew(${s.id})">Продлить</button>${s.traffic.enabled ? `<button class="button green" onclick="openTraffic(${s.id})">Купить ГБ</button>` : ""}</div>
  </div>`;
}

function renewHtml(s) {
  const tariffs = state.tariffs[s.plan_kind] || [];
  const left = daysLeft(s.subscription_until);
  return `<div class="grid">
    <button class="button ghost" onclick="openSubDetail(${s.id})">← Назад к ключу</button>
    <div class="card"><p class="step">Продление</p><p class="title">${subTitle(s)}</p><p class="muted">${s.status === "active" && left !== null ? `Сейчас осталось ${left} дн. Выберите, на сколько продлить ключ.` : "Выберите срок, чтобы восстановить или продлить ключ."}</p></div>
    <div class="choice-list">${tariffs.map((t) => tariffButton(t, `prepareRenewPayment(${s.id}, '${t.code}')`, subTitle(s))).join("")}</div>
  </div>`;
}

function trafficHtml(s) {
  if (!s.traffic.enabled) return subscriptionDetailHtml(s);
  const percent = trafficPercent(s);
  return `<div class="grid">
    <button class="button ghost" onclick="openSubDetail(${s.id})">← Назад к ключу</button>
    <div class="card strong"><p class="step">Покупка ГБ</p><p class="title">${subTitle(s)}</p><p class="muted">Дополнительные ГБ добавятся к этому ключу с антиглушилкой.</p><div class="traffic-card"><div class="row"><span class="small">Сейчас использовано</span><b>${s.traffic.used_gb} / ${s.traffic.limit_gb} ГБ</b></div><div class="progress"><span style="width:${percent}%"></span></div><p class="small">Сброс: ${date(s.traffic.reset_at)}</p></div></div>
    <div class="choice-list">${state.tariffs.traffic_packages.map((p) => `<button class="choice-button package-choice" onclick="prepareTrafficPayment(${s.id}, '${p.code}')"><span>+${p.gb} ГБ<small>Добавить к выбранному ключу</small></span><b>${rub(p.price)}</b></button>`).join("")}</div>
  </div>`;
}

function renderBuy() {
  const container = el("view-buy");
  if (state.buyMode === "plan") {
    container.innerHTML = `<div class="grid"><div class="card"><p class="step">Покупка</p><p class="title">Выберите подписку</p><p class="muted">Каждая подписка создаёт отдельный ключ подключения.</p></div><div class="plan-grid">${planCard("regular")}${planCard("bypass")}</div></div>`;
    return;
  }

  if (state.buyMode === "tariff") {
    const tariffs = state.tariffs[state.buyPlan] || [];
    container.innerHTML = `<div class="grid">
      <button class="button ghost" onclick="resetBuy()">← Назад к типам</button>
      <div class="card"><p class="step">Срок</p><p class="title">${planName(state.buyPlan)}</p><p class="muted">${planTag(state.buyPlan)}. Выберите удобный срок оплаты.</p></div>
      <div class="choice-list">${tariffs.map((t) => tariffButton(t, `prepareNewPayment('${t.code}')`, t.title)).join("")}</div>
    </div>`;
    return;
  }

  container.innerHTML = paymentHtml("Новая подписка");
}

function paymentHtml(title) {
  const pending = state.pendingPayment;
  const summary = paymentSummary(pending);
  return `<div class="grid">
    <button class="button ghost" onclick="backFromPayment()">← Назад</button>
    <div class="card strong"><p class="step">Оплата</p><p class="title">${title}</p>${summary}<p class="muted">После оплаты кабинет обновится автоматически в системе. Если не увидите изменения сразу, откройте MiniApp заново.</p><div class="grid"><button class="button accent" onclick="payPrepared('cryptobot')">CryptoBot</button><button class="button green" onclick="payPrepared('yookassa')">Банковская карта</button></div></div>
  </div>`;
}

function planCard(plan) {
  const t = cheapest(plan);
  return `<button class="plan-card ${planClass(plan)}" onclick="selectBuyPlan('${plan}')">
    <span class="mini-badge ${planClass(plan)}">${planName(plan)}</span>
    <b>${plan === "bypass" ? "Работает при блокировках" : "Быстрый VPN на каждый день"}</b>
    <p>${planText(plan)}</p>
    <small>${planTag(plan)}</small>
    ${t ? `<strong>от ${rub(t.price)}</strong>` : ""}
  </button>`;
}

function tariffButton(t, handler, subtitle) {
  const monthPrice = Math.round(t.price / Math.max(1, t.days / 30));
  const badge = t.days >= 90 ? `<i class="save-badge">выгоднее</i>` : "";
  return `<button class="choice-button tariff-choice" onclick="${handler}"><span>${tariffPeriod(t)} ${badge}<small>${subtitle}</small><small>≈ ${rub(monthPrice)} / месяц</small></span><b>${rub(t.price)}</b></button>`;
}

function paymentSummary(pending) {
  if (!pending) return "";
  if (pending.type === "traffic") {
    const sub = state.subs.find((s) => s.id === pending.subscription_id);
    const pack = state.tariffs.traffic_packages.find((p) => p.code === pending.package_code);
    return `<div class="summary"><span>Вы покупаете</span><b>+${pack?.gb || ""} ГБ</b><small>${sub ? subTitle(sub) : "Ключ с антиглушилкой"} · ${pack ? rub(pack.price) : ""}</small></div>`;
  }
  const all = [...(state.tariffs.regular || []), ...(state.tariffs.bypass || [])];
  const tariff = all.find((t) => t.code === pending.tariff_code);
  const sub = pending.subscription_id ? state.subs.find((s) => s.id === pending.subscription_id) : null;
  return `<div class="summary"><span>${pending.payment_target === "renew" ? "Вы продлеваете" : "Вы покупаете"}</span><b>${tariff ? tariff.title : "Подписка"}</b><small>${sub ? subTitle(sub) : "Новый ключ"}${tariff ? ` · ${tariffPeriod(tariff)} · ${rub(tariff.price)}` : ""}</small></div>`;
}

function selectBuyPlan(plan) { state.buyPlan = plan; state.buyMode = "tariff"; switchView("buy"); }
function resetBuy() { state.buyMode = "plan"; state.buyPlan = null; state.buyTariffCode = null; state.pendingPayment = null; renderBuy(); }
function openNewPurchase() { resetBuy(); switchView("buy"); }
function openKeysList() { state.keysMode = "list"; state.selectedSubId = null; switchView("subs"); }
function openRenewList() { state.keysMode = "renew-list"; state.selectedSubId = null; switchView("subs"); }
function openTrafficFromHome() {
  const sub = activeSubs().find((s) => s.plan_kind === "bypass");
  if (sub) openTraffic(sub.id);
  else openKeysList();
}
function openSubDetail(id) { state.selectedSubId = id; state.keysMode = "detail"; switchView("subs"); }
function openRenew(id) { state.selectedSubId = id; state.keysMode = "renew"; switchView("subs"); }
function openTraffic(id) { state.selectedSubId = id; state.keysMode = "traffic"; switchView("subs"); }

function prepareNewPayment(tariffCode) {
  state.buyTariffCode = tariffCode;
  state.pendingPayment = { type: "subscription", tariff_code: tariffCode, payment_target: "new", subscription_id: null };
  state.buyMode = "payment";
  renderBuy();
}

function prepareRenewPayment(subscriptionId, tariffCode) {
  state.pendingPayment = { type: "subscription", tariff_code: tariffCode, payment_target: "renew", subscription_id: subscriptionId };
  state.keysMode = "renew-payment";
  renderKeys();
}

function prepareTrafficPayment(subscriptionId, packageCode) {
  state.pendingPayment = { type: "traffic", subscription_id: subscriptionId, package_code: packageCode };
  state.keysMode = "traffic-payment";
  renderKeys();
}

function backFromPayment() {
  if (!state.pendingPayment) return;
  if (state.pendingPayment.type === "traffic") state.keysMode = "traffic";
  else if (state.pendingPayment.payment_target === "renew") state.keysMode = "renew";
  else state.buyMode = "tariff";
  state.pendingPayment = null;
  render();
}

async function payPrepared(provider) {
  if (!state.pendingPayment) return;
  try {
    let data;
    if (state.pendingPayment.type === "traffic") {
      data = await api("/miniapp/api/payments/traffic", { method: "POST", body: JSON.stringify({ ...state.pendingPayment, provider }) });
    } else {
      data = await api("/miniapp/api/payments/subscription", { method: "POST", body: JSON.stringify({ ...state.pendingPayment, provider }) });
    }
    openLink(data.pay_url);
    showToast("Счёт создан. После оплаты обновите кабинет.");
  } catch (e) {
    showToast(e.message);
  }
}

function renderReferral() {
  const r = state.referral;
  el("view-ref").innerHTML = `<div class="grid"><div class="card strong"><p class="title">Бонус за друга</p><p class="muted">35% с первой покупки и 15% с повторных.</p></div><div class="card"><div class="row"><span>Активных друзей</span><b>${r?.active_referrals || 0}</b></div><div class="row"><span>Всего заработано</span><b>${rub(r?.total_earned || 0)}</b></div><div class="row"><span>Баланс</span><b>${rub(r?.current_balance || 0)}</b></div></div><div class="card"><p class="title">Ваша ссылка</p><div class="keybox">${r?.link || ""}</div><button class="button ghost" onclick="copyText('${encodeURIComponent(r?.link || "")}')">Скопировать ссылку</button></div></div>`;
}

function renderHelp() {
  el("view-help").innerHTML = `<div class="grid"><div class="card"><p class="title">Как подключиться</p><p class="muted">Скопируйте ключ из раздела “Ключи”, откройте Happ Plus, нажмите “+” и вставьте ключ из буфера обмена.</p></div><div class="card"><p class="title">Поддержка</p><p class="muted">Если что-то не получается, напишите нам в Telegram.</p><button class="button ghost" onclick="openLink('https://t.me/wayspn_support')">Открыть поддержку</button></div></div>`;
}

async function copyText(encoded) {
  const text = decodeURIComponent(encoded);
  await navigator.clipboard.writeText(text).catch(() => {});
  showToast("Скопировано");
}

load();
