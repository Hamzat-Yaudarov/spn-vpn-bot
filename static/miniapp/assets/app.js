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
  currentView: "home",
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

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view, { reset: true }));
});

function resetKeysState() {
  state.keysMode = "list";
  state.selectedSubId = null;
  state.pendingPayment = null;
}

function resetPurchaseState() {
  state.buyMode = "plan";
  state.buyPlan = null;
  state.buyTariffCode = null;
  state.pendingPayment = null;
}

function switchView(view, options = {}) {
  if (options.reset) {
    if (state.currentView === "subs" || view === "subs") resetKeysState();
    if (state.currentView === "buy" || view === "buy") resetPurchaseState();
  }
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  el(`view-${view}`).classList.add("active");
  document.body.className = `view-${view}-active`;
  state.currentView = view;
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
  el("view-home").innerHTML = `
    <div class="grid">
      <div class="home-panel">
        <p class="step">Быстрые действия</p>
        <p class="title">Что хотите сделать?</p>
        <p class="muted">Покупка, продление, ключи и бонусы собраны в одном кабинете.</p>
      </div>
      <div class="quick-grid">
        <button class="quick-card gold" onclick="openNewPurchase()"><span>Купить</span><small>Новая подписка</small></button>
        <button class="quick-card blue" onclick="openKeysList()"><span>Ключи</span><small>Ссылки и статус</small></button>
        <button class="quick-card bronze" onclick="openRenewList()"><span>Продлить</span><small>Выбрать ключ</small></button>
        ${hasBypass ? `<button class="quick-card green" onclick="openKeysList(); showToast('Выберите ключ с антиглушилкой и нажмите Купить ГБ')"><span>Купить ГБ</span><small>Для антиглушилки</small></button>` : ""}
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
    <div class="section-note"><p class="step">Раздел ключей</p><p class="title">${title}</p><p class="muted">${subtitle}</p></div>
    <div class="choice-list">${state.subs.map((s) => keyButton(s, action)).join("")}</div>
  </div>`;
}

function keyButton(s, action) {
  const handler = action === "renew" ? `openRenew(${s.id})` : `openSubDetail(${s.id})`;
  const planClass = s.plan_kind === "bypass" ? "bypass" : "regular";
  return `<button class="choice-button key-choice ${planClass}" onclick="${handler}"><span><i></i>${subTitle(s)}<small>${s.status === "active" ? `Активна до ${date(s.subscription_until)}` : "Истекла"}</small></span><b>›</b></button>`;
}

function subscriptionDetailHtml(s) {
  const percent = s.traffic.enabled && s.traffic.limit_gb ? Math.min(100, Math.round((s.traffic.used_gb / s.traffic.limit_gb) * 100)) : 0;
  return `<div class="grid">
    <button class="button ghost" onclick="openKeysList()">← Назад к ключам</button>
    <div class="card strong ${s.plan_kind === "bypass" ? "plan-bypass" : "plan-regular"}">
      <div class="row start"><div><p class="title">${subTitle(s)}</p><p class="muted">${s.status === "active" ? `Активна до ${date(s.subscription_until)}` : "Подписка истекла"}</p></div><span class="badge ${s.status === "active" ? "ok" : "warn"}">${s.status === "active" ? "Активна" : "Истекла"}</span></div>
      ${s.traffic.enabled ? `<div class="grid"><div><div class="row"><span class="small">Трафик антиглушилки</span><span class="small">${s.traffic.used_gb} / ${s.traffic.limit_gb} ГБ</span></div><div class="progress"><span style="width:${percent}%"></span></div><p class="small">Сброс: ${date(s.traffic.reset_at)}</p></div></div>` : ""}
    </div>
    <div class="card"><p class="title">Ключ подключения</p><p class="muted">Добавьте ключ в Happ автоматически или скопируйте ссылку вручную.</p>${s.subscription_url ? `<div class="keybox">${s.subscription_url}</div><div class="grid"><button class="button blue" onclick="addKeyToHapp('${encodeURIComponent(s.subscription_url)}')">Добавить ключ в Happ</button><button class="button ghost" onclick="copyText('${encodeURIComponent(s.subscription_url)}')">Скопировать ключ</button></div>` : `<p class="muted">Ключ появится после активации оплаты.</p>`}</div>
    <button class="button ghost" onclick="openRenew(${s.id})">Продлить</button>
    ${s.traffic.enabled ? `<button class="button green" onclick="openTraffic(${s.id})">Купить ГБ</button>` : ""}
  </div>`;
}

function renewHtml(s) {
  const tariffs = state.tariffs[s.plan_kind] || [];
  return `<div class="grid">
    <button class="button ghost" onclick="openSubDetail(${s.id})">← Назад к ключу</button>
    <div class="section-note"><p class="step">Шаг 2 из 3</p><p class="title">${subTitle(s)}</p><p class="muted">Выберите срок продления.</p></div>
    <div class="choice-list">${tariffs.map((t) => `<button class="choice-button" onclick="prepareRenewPayment(${s.id}, '${t.code}')"><span>${tariffPeriod(t)}<small>${subTitle(s)}</small></span><b>${rub(t.price)}</b></button>`).join("")}</div>
  </div>`;
}

function trafficHtml(s) {
  if (!s.traffic.enabled) return subscriptionDetailHtml(s);
  return `<div class="grid">
    <button class="button ghost" onclick="openSubDetail(${s.id})">← Назад к ключу</button>
    <div class="section-note"><p class="step">Шаг 2 из 3</p><p class="title">${subTitle(s)}</p><p class="muted">ГБ тратятся только при использовании антиглушилки.</p></div>
    <div class="choice-list">${state.tariffs.traffic_packages.map((p) => `<button class="choice-button" onclick="prepareTrafficPayment(${s.id}, '${p.code}')"><span>+${p.gb} ГБ<small>Дополнительный трафик</small></span><b>${rub(p.price)}</b></button>`).join("")}</div>
  </div>`;
}

function renderBuy() {
  const container = el("view-buy");
  if (state.buyMode === "plan") {
    container.innerHTML = `<div class="grid">
      <div class="section-note"><p class="step">Шаг 1 из 3</p><p class="title">Выберите тип подписки</p><p class="muted">Новая подписка будет создана отдельным ключом.</p></div>
      <div class="plan-grid">
        <button class="plan-card plan-regular" onclick="selectBuyPlan('regular')"><span>Обычная</span><b>5 устройств</b><small>Для ежедневного подключения.</small></button>
        <button class="plan-card plan-bypass" onclick="selectBuyPlan('bypass')"><span>С антиглушилкой</span><b>80 ГБ в месяц</b><small>3 устройства, обход ограничений.</small></button>
      </div>
    </div>`;
    return;
  }

  if (state.buyMode === "tariff") {
    const tariffs = state.tariffs[state.buyPlan] || [];
    container.innerHTML = `<div class="grid">
      <button class="button ghost" onclick="resetBuy()">← Назад к типам</button>
      <div class="section-note ${state.buyPlan === "regular" ? "regular-note" : "bypass-note"}"><p class="step">Шаг 2 из 3</p><p class="title">${state.buyPlan === "regular" ? "Обычная подписка" : "С антиглушилкой"}</p><p class="muted">${state.buyPlan === "regular" ? "5 устройств, обычные серверы." : "3 устройства, 80 ГБ в месяц."}</p></div>
      <div class="choice-list">${tariffs.map((t) => `<button class="choice-button" onclick="prepareNewPayment('${t.code}')"><span>${tariffPeriod(t)}<small>${t.title}</small></span><b>${rub(t.price)}</b></button>`).join("")}</div>
    </div>`;
    return;
  }

  container.innerHTML = paymentHtml("Новая подписка");
}

function paymentHtml(title) {
  return `<div class="grid">
    <button class="button ghost" onclick="backFromPayment()">← Назад</button>
    <div class="section-note payment-note"><p class="step">Шаг 3 из 3</p><p class="title">${title}</p><p class="muted">Выберите удобный способ оплаты. После оплаты подписка активируется автоматически.</p></div><div class="grid"><button class="button accent" onclick="payPrepared('cryptobot')">CryptoBot</button><button class="button green" onclick="payPrepared('yookassa')">Банковская карта</button></div>
  </div>`;
}

function selectBuyPlan(plan) { state.buyPlan = plan; state.buyMode = "tariff"; switchView("buy", { preserve: true }); }
function resetBuy() { state.buyMode = "plan"; state.buyPlan = null; state.buyTariffCode = null; state.pendingPayment = null; renderBuy(); }
function openNewPurchase() { resetBuy(); switchView("buy", { preserve: true }); }
function openKeysList() { state.keysMode = "list"; state.selectedSubId = null; switchView("subs", { preserve: true }); }
function openRenewList() { state.keysMode = "renew-list"; state.selectedSubId = null; switchView("subs", { preserve: true }); }
function showKeyMode(id, mode) {
  state.selectedSubId = id;
  state.keysMode = mode;
  if (state.currentView === "subs") renderKeys();
  else switchView("subs", { preserve: true });
}
function openSubDetail(id) { showKeyMode(id, "detail"); }
function openRenew(id) { showKeyMode(id, "renew"); }
function openTraffic(id) { showKeyMode(id, "traffic"); }

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
  el("view-help").innerHTML = `<div class="grid"><div class="card"><p class="title">Как подключиться</p><p class="muted">Откройте раздел “Ключи”, выберите ключ и нажмите “Добавить ключ в Happ”. Если приложение не открылось, ключ будет скопирован, его можно вставить вручную.</p></div><div class="card"><p class="title">Поддержка</p><p class="muted">Если что-то не получается, напишите нам в Telegram.</p><button class="button ghost" onclick="openLink('https://t.me/wayspn_support')">Открыть поддержку</button></div></div>`;
}

async function copyText(encoded) {
  const text = decodeURIComponent(encoded);
  await navigator.clipboard.writeText(text).catch(() => {});
  showToast("Скопировано");
}

async function addKeyToHapp(encoded) {
  const text = decodeURIComponent(encoded);
  await navigator.clipboard.writeText(text).catch(() => {});
  showToast("Открываем Happ. Если не добавится, ключ уже скопирован.");
  window.location.assign(`happ://add/${encodeURIComponent(text)}`);
}

load();
