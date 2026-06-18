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
  activePayment: null,
  paymentPollTimer: null,
  currentView: "home",
  devices: {},
  devicesLoading: false,
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
function happLink(url) { return `happ://add/${encodeURIComponent(url)}`; }
function happBridgeLink(url) { return `${window.location.origin}/app/open-happ?url=${encodeURIComponent(url)}`; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[ch])); }

function daysLeft(value) {
  if (!value) return null;
  return Math.ceil((new Date(value).getTime() - Date.now()) / 86400000);
}

function subBadges(s) {
  const badges = [];
  const left = daysLeft(s.subscription_until);
  if (s.status === "active") badges.push({ text: left !== null && left <= 3 ? "Скоро закончится" : "Активна", cls: left !== null && left <= 3 ? "warn" : "ok" });
  else badges.push({ text: "Истекла", cls: "warn" });
  badges.push({ text: s.plan_kind === "bypass" ? "Антиглушилка" : "Обычная", cls: s.plan_kind === "bypass" ? "green" : "blue" });
  if (s.traffic?.enabled) {
    const remaining = Number(s.traffic.limit_gb || 0) - Number(s.traffic.used_gb || 0);
    if (remaining < 10) badges.push({ text: "Мало ГБ", cls: "warn" });
  }
  return badges;
}

function badgesHtml(s) {
  return `<span class="badge-row">${subBadges(s).map((badge) => `<span class="badge ${badge.cls}">${badge.text}</span>`).join("")}</span>`;
}

function renderAvatar(user) {
  const avatar = el("userAvatar");
  const name = user?.first_name || user?.username || "Way SPN";
  if (user?.photo_url) {
    avatar.innerHTML = `<img src="${user.photo_url}" alt="${name}">`;
  } else {
    avatar.textContent = name.trim().slice(0, 1).toUpperCase() || "W";
  }
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view, { reset: true }));
});

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const subId = Number(target.dataset.subId);
  if (target.dataset.action === "renew") openRenew(subId);
  if (target.dataset.action === "traffic") openTraffic(subId);
  if (target.dataset.action === "devices") openDevices(subId);
  if (target.dataset.action === "delete-device") deleteDevice(subId, target.dataset.hwid || "");
  if (target.dataset.action === "delete-all-devices") deleteAllDevices(subId);
  if (target.dataset.action === "happ") {
    event.preventDefault();
    openKeyInHapp(target.dataset.url || "");
  }
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
    renderAvatar(me);
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

function stopPaymentPolling() {
  if (state.paymentPollTimer) clearInterval(state.paymentPollTimer);
  state.paymentPollTimer = null;
}

function startPaymentPolling(payment) {
  stopPaymentPolling();
  state.activePayment = { ...payment, attempts: 0 };
  showToast("После оплаты ключ появится автоматически.");
  state.paymentPollTimer = setInterval(checkActivePayment, 3000);
}

async function checkActivePayment() {
  const payment = state.activePayment;
  if (!payment) return stopPaymentPolling();
  payment.attempts += 1;

  try {
    const status = await api(`/miniapp/api/payments/${encodeURIComponent(payment.invoice_id)}`);
    if (status.status === "paid") {
      stopPaymentPolling();
      state.activePayment = null;
      state.pendingPayment = null;
      await reloadData();
      if (payment.type === "subscription" && payment.payment_target === "renew" && payment.subscription_id) {
        state.selectedSubId = payment.subscription_id;
        state.keysMode = "detail";
      } else if (payment.type === "traffic" && payment.subscription_id) {
        state.selectedSubId = payment.subscription_id;
        state.keysMode = "detail";
      } else {
        state.selectedSubId = null;
        state.keysMode = "list";
      }
      state.buyMode = "plan";
      switchView("subs", { preserve: true });
      showToast("Оплата получена. Ключ обновлён.");
      return;
    }
  } catch (e) {
    if (payment.attempts > 3) console.warn("Payment status check failed", e);
  }

  if (payment.attempts >= 120) {
    stopPaymentPolling();
    state.activePayment = null;
    showToast("Проверка оплаты остановлена. Обновите кабинет позже.");
  }
}

function render() {
  renderHome();
  renderKeys();
  renderBuy();
  renderReferral();
  renderHelp();
}

function renderHome() {
  const active = activeSubs();
  const primary = active.length ? { text: "Мои подписки", action: "openKeysList", hint: "Открыть ключи и статус" } : { text: "Купить / Продлить", action: "openNewPurchase", hint: "Новая подписка" };
  const secondary = active.length ? { text: "Купить / Продлить", action: "openNewPurchase", hint: "Оформить новую подписку" } : { text: "Ключи", action: "openKeysList", hint: "Список ключей" };
  const hasBypass = active.some((s) => s.plan_kind === "bypass");
  el("view-home").innerHTML = `
    <div class="grid">
      <div class="home-panel">
        <p class="step">Быстрые действия</p>
        <p class="title">Что хотите сделать?</p>
        <p class="muted">Покупка, продление, ключи и бонусы собраны в одном кабинете.</p>
      </div>
      <div class="quick-grid home-actions">
        <button class="quick-card gold primary-action" onclick="${primary.action}()"><span>${primary.text}</span><small>${primary.hint}</small></button>
        <button class="quick-card blue secondary-action" onclick="${secondary.action}()"><span>${secondary.text}</span><small>${secondary.hint}</small></button>
      </div>
      <div class="choice-list compact-actions">
        <button class="choice-button" onclick="openRenewList()"><span>Продлить<small>Выбрать ключ вручную</small></span><b>›</b></button>
        ${hasBypass ? `<button class="choice-button" onclick="openKeysList(); showToast('Выберите ключ с антиглушилкой и нажмите Купить ГБ')"><span>Купить ГБ<small>Для антиглушилки</small></span><b>›</b></button>` : ""}
        <button class="choice-button" onclick="switchView('help', { preserve: true })"><span>Помощь<small>Инструкция и поддержка</small></span><b>›</b></button>
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
  if (state.keysMode === "devices") container.innerHTML = devicesHtml(sub);
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
  return `<button class="choice-button key-choice ${planClass}" onclick="${handler}"><span><i></i>${subTitle(s)}<small>${s.status === "active" ? `до ${date(s.subscription_until)}` : "истекла"}</small>${badgesHtml(s)}</span><b>›</b></button>`;
}

function subscriptionDetailHtml(s) {
  const percent = s.traffic.enabled && s.traffic.limit_gb ? Math.min(100, Math.round((s.traffic.used_gb / s.traffic.limit_gb) * 100)) : 0;
  const limitText = s.plan_kind === "bypass" ? "3 устройства" : "5 устройств";
  return `<div class="grid">
    <button class="button ghost" onclick="openKeysList()">← Назад к ключам</button>
    <div class="card strong ${s.plan_kind === "bypass" ? "plan-bypass" : "plan-regular"}">
      <div class="row start"><div><p class="title">${subTitle(s)}</p><p class="muted">${s.status === "active" ? `Срок: до ${date(s.subscription_until)}` : "Срок закончился"}</p></div></div>
      ${badgesHtml(s)}
      <div class="hint-list"><div><b>Тип</b><span>${s.plan_kind === "bypass" ? "С антиглушилкой" : "Обычная"}</span></div><div><b>Лимит</b><span>${limitText}</span></div></div>
      ${s.traffic.enabled ? `<div class="grid"><div><div class="row"><span class="small">Трафик антиглушилки</span><span class="small">${s.traffic.used_gb} / ${s.traffic.limit_gb} ГБ</span></div><div class="progress"><span style="width:${percent}%"></span></div><p class="small">Сброс: ${date(s.traffic.reset_at)}</p></div></div>` : ""}
    </div>
    <div class="card"><p class="title">Ключ подключения</p><p class="muted">Добавьте ключ в Happ автоматически или скопируйте ссылку вручную.</p>${s.subscription_url ? `<div class="keybox">${s.subscription_url}</div><div class="grid"><button class="button blue" data-action="happ" data-url="${encodeURIComponent(s.subscription_url)}">Добавить ключ в Happ</button><button class="button ghost" onclick="copyText('${encodeURIComponent(s.subscription_url)}')">Скопировать ключ</button></div>` : `<p class="muted">Ключ появится после активации оплаты.</p>`}</div>
    <button class="button ghost" data-action="renew" data-sub-id="${s.id}">Продлить</button>
    <button class="button blue" data-action="devices" data-sub-id="${s.id}">Устройства</button>
    ${s.traffic.enabled ? `<button class="button green" data-action="traffic" data-sub-id="${s.id}">Купить ГБ</button>` : ""}
  </div>`;
}

function deviceTitle(device) {
  const platform = device.platform || "Устройство";
  return device.device_model ? `${platform} • ${device.device_model}` : platform;
}

function devicesHtml(s) {
  const devices = state.devices[s.id] || [];
  if (state.devicesLoading) {
    return `<div class="grid"><button class="button ghost" onclick="openSubDetail(${s.id})">← Назад к ключу</button><div class="card empty"><h2>Загружаем устройства...</h2></div></div>`;
  }

  const list = devices.length
    ? devices.map((device, index) => `<div class="card device-card"><div class="row start"><div><p class="title">${escapeHtml(deviceTitle(device))}</p><p class="muted">Подключено: ${date(device.created_at)}</p>${device.hwid ? `<p class="small">HWID: ${escapeHtml(device.hwid)}</p>` : ""}</div><span class="badge blue">${index + 1}</span></div>${device.hwid ? `<button class="button danger" data-action="delete-device" data-sub-id="${s.id}" data-hwid="${escapeHtml(device.hwid)}">Удалить устройство</button>` : ""}</div>`).join("")
    : `<div class="card empty"><h2>Устройств пока нет</h2><p class="muted">Когда ключ подключат на телефоне или компьютере, устройство появится здесь.</p></div>`;

  return `<div class="grid">
    <button class="button ghost" onclick="openSubDetail(${s.id})">← Назад к ключу</button>
    <div class="section-note"><p class="step">Устройства</p><p class="title">${subTitle(s)}</p><p class="muted">Удалите устройство, если нужно освободить слот для нового подключения.</p></div>
    ${list}
    ${devices.length ? `<button class="button danger" data-action="delete-all-devices" data-sub-id="${s.id}">Удалить все устройства</button>` : ""}
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
        <button class="plan-card plan-bypass" onclick="selectBuyPlan('bypass')"><span>С антиглушилкой</span><b>150 ГБ в месяц</b><small>3 устройства, обход ограничений.</small></button>
      </div>
    </div>`;
    return;
  }

  if (state.buyMode === "tariff") {
    const tariffs = state.tariffs[state.buyPlan] || [];
    container.innerHTML = `<div class="grid">
      <button class="button ghost" onclick="resetBuy()">← Назад к типам</button>
      <div class="section-note ${state.buyPlan === "regular" ? "regular-note" : "bypass-note"}"><p class="step">Шаг 2 из 3</p><p class="title">${state.buyPlan === "regular" ? "Обычная подписка" : "С антиглушилкой"}</p><p class="muted">${state.buyPlan === "regular" ? "5 устройств, обычные серверы." : "3 устройства, 150 ГБ в месяц."}</p></div>
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
function openSubDetail(id) { state.selectedSubId = id; state.keysMode = "detail"; switchView("subs", { preserve: true }); }
function openRenew(id) { state.selectedSubId = id; state.keysMode = "renew"; switchView("subs", { preserve: true }); }
function openTraffic(id) { state.selectedSubId = id; state.keysMode = "traffic"; switchView("subs", { preserve: true }); }

async function openDevices(id) {
  state.selectedSubId = id;
  state.keysMode = "devices";
  state.devicesLoading = true;
  renderKeys();
  try {
    const data = await api(`/miniapp/api/subscriptions/${id}/devices`);
    state.devices[id] = data.devices || [];
  } catch (e) {
    showToast(e.message);
    state.devices[id] = [];
  } finally {
    state.devicesLoading = false;
    renderKeys();
  }
}

async function deleteDevice(id, hwid) {
  if (!hwid) return;
  try {
    await api(`/miniapp/api/subscriptions/${id}/devices/delete`, { method: "POST", body: JSON.stringify({ hwid }) });
    showToast("Устройство удалено");
    await openDevices(id);
  } catch (e) {
    showToast(e.message);
  }
}

async function deleteAllDevices(id) {
  try {
    await api(`/miniapp/api/subscriptions/${id}/devices/delete-all`, { method: "POST", body: JSON.stringify({}) });
    showToast("Все устройства удалены");
    await openDevices(id);
  } catch (e) {
    showToast(e.message);
  }
}

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
    startPaymentPolling({ ...state.pendingPayment, invoice_id: data.invoice_id, provider });
    openLink(data.pay_url);
    showToast("Счёт создан. Ждём оплату автоматически.");
  } catch (e) {
    showToast(e.message);
  }
}

function renderReferral() {
  const r = state.referral;
  el("view-ref").innerHTML = `<div class="grid">
    <div class="section-note bonus-note"><p class="step">Партнёрская программа</p><p class="title">Бонус за друга</p><p class="muted">Получайте 35% с первой покупки друга и 15% с повторных.</p></div>
    <div class="stats-grid">
      <div class="stat-card blue"><span>Активных друзей</span><b>${r?.active_referrals || 0}</b></div>
      <div class="stat-card bronze"><span>Всего заработано</span><b>${rub(r?.total_earned || 0)}</b></div>
      <div class="stat-card green"><span>Баланс</span><b>${rub(r?.current_balance || 0)}</b></div>
    </div>
    <div class="card link-card"><p class="title">Ваша ссылка</p><p class="muted">Отправьте её другу. Бонус появится после оплаты.</p><div class="keybox">${r?.link || ""}</div><button class="button blue" onclick="copyText('${encodeURIComponent(r?.link || "")}')">Скопировать ссылку</button></div>
  </div>`;
}

function renderHelp() {
  el("view-help").innerHTML = `<div class="grid">
    <div class="section-note help-note"><p class="step">Помощь</p><p class="title">Подключение и поддержка</p><p class="muted">Быстрые подсказки, если нужно добавить ключ или написать нам.</p></div>
    <div class="help-card blue"><span>1</span><div><p class="title">Добавьте ключ в Happ</p><p class="muted">Откройте “Ключи”, выберите подписку и нажмите “Добавить ключ в Happ”.</p></div></div>
    <div class="help-card green"><span>2</span><div><p class="title">Если Happ не открылся</p><p class="muted">Ключ уже будет скопирован. Откройте Happ вручную и вставьте его через “+”.</p></div></div>
    <div class="card support-card"><p class="title">Поддержка</p><p class="muted">Если что-то не получается, напишите нам в Telegram.</p><button class="button blue" onclick="openLink('https://t.me/wayspn_support')">Открыть поддержку</button></div>
  </div>`;
}

async function copyText(encoded) {
  const text = decodeURIComponent(encoded);
  await navigator.clipboard.writeText(text).catch(() => {});
  showToast("Скопировано");
}

function openKeyInHapp(encoded) {
  const text = decodeURIComponent(encoded);
  const url = happBridgeLink(text);
  if (tg?.openLink) {
    tg.openLink(url);
  } else {
    window.open(url, "_blank");
  }
  navigator.clipboard.writeText(text).catch(() => {});
  showToast("Открываем Happ. Если не добавится, ключ уже скопирован.");
}

load();
