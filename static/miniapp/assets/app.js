const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
}

["gesturestart", "gesturechange", "gestureend"].forEach((eventName) => {
  document.addEventListener(eventName, (event) => event.preventDefault(), { passive: false });
});
document.addEventListener("touchmove", (event) => {
  if (event.touches.length > 1) event.preventDefault();
}, { passive: false });

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
  paymentResult: null,
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
function priceHtml(item) {
  if (Number(item.original_price) > Number(item.price)) {
    return `<span class="offer-price"><s>${rub(item.original_price)}</s><b>${rub(item.price)}</b><em>${escapeHtml(item.discount?.name || "Скидка")}</em></span>`;
  }
  return `<b>${rub(item.price)}</b>`;
}
function happLink(url) { return `happ://add/${encodeURIComponent(url)}`; }
function happBridgeLink(url) { return `${window.location.origin}/app/open-happ?url=${encodeURIComponent(url)}`; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[ch])); }
function multilineHtml(value) { return escapeHtml(value).replace(/\n/g, "<br>"); }
function deviceWord(count) { return count === 1 ? "устройство" : [2, 3, 4].includes(count) ? "устройства" : "устройств"; }
function deviceLimitText(s) { const limit = Number(s.devices?.limit || (s.plan_kind === "bypass" ? 3 : 5)); return `${limit} ${deviceWord(limit)}`; }
function devicePackageText(pkg) { return `+${pkg.count} ${deviceWord(pkg.count)} · ${rub(pkg.price)}${pkg.discount_percent ? ` · скидка ${pkg.discount_percent}%` : ""}`; }

function daysLeft(value) {
  if (!value) return null;
  return Math.ceil((new Date(value).getTime() - Date.now()) / 86400000);
}

function timeLeftText(value) {
  const left = daysLeft(value);
  if (left === null) return "неизвестно";
  if (left <= 0) return "сегодня";
  if (left === 1) return "1 день";
  if (left > 1 && left < 5) return `${left} дня`;
  return `${left} дней`;
}

function trafficRemaining(s) {
  if (!s.traffic?.enabled) return null;
  return Math.max(0, Number(s.traffic.limit_gb || 0) - Number(s.traffic.used_gb || 0));
}

function nearestActiveSub() {
  return activeSubs().sort((a, b) => new Date(a.subscription_until) - new Date(b.subscription_until))[0] || null;
}

function statusTone(s) {
  if (!s || s.status !== "active") return "warn";
  const left = daysLeft(s.subscription_until);
  if (left !== null && left <= 1) return "danger";
  if (left !== null && left <= 3) return "warn";
  return "ok";
}

function trafficTone(s) {
  const remaining = trafficRemaining(s);
  if (remaining === null) return "";
  if (remaining <= 3) return "danger";
  if (remaining <= 10) return "warn";
  return "";
}

function subBadges(s) {
  const badges = [];
  const left = daysLeft(s.subscription_until);
  if (s.status === "active") badges.push({ text: left !== null && left <= 3 ? "Скоро закончится" : "Активна", cls: left !== null && left <= 1 ? "danger" : left !== null && left <= 3 ? "warn" : "ok" });
  else badges.push({ text: "Истекла", cls: "danger" });
  badges.push({ text: s.plan_kind === "bypass" ? "Антиглушилка" : "Обычная", cls: s.plan_kind === "bypass" ? "green" : "blue" });
  if (s.traffic?.enabled) {
    const remaining = Number(s.traffic.limit_gb || 0) - Number(s.traffic.used_gb || 0);
    if (remaining < 10) badges.push({ text: "Мало ГБ", cls: remaining <= 3 ? "danger" : "warn" });
  }
  return badges;
}

function badgesHtml(s) {
  return `<span class="badge-row">${subBadges(s).map((badge) => `<span class="badge ${badge.cls}">${badge.text}</span>`).join("")}</span>`;
}

function renderAvatar(user) {
  const avatar = el("userAvatar");
  avatar.classList.remove("skeleton-avatar");
  avatar.removeAttribute("aria-hidden");
  const name = user?.first_name || user?.username || "Way SPN";
  if (user?.photo_url) {
    avatar.innerHTML = `<img src="${user.photo_url}" alt="${name}">`;
  } else {
    avatar.textContent = name.trim().slice(0, 1).toUpperCase() || "W";
  }
}

function finishHeaderLoading() {
  el("userLine").classList.remove("skeleton-text");
  el("userLine").removeAttribute("aria-label");
  el("userAvatar").classList.remove("skeleton-avatar");
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
  if (target.dataset.action === "device-addons") openDeviceAddons(subId);
  if (target.dataset.action === "delete-subscription") deleteSubscription(subId);
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
    finishHeaderLoading();
    el("userLine").textContent = me.first_name ? `Добро пожаловать, ${me.first_name}` : "Управляйте подписками в пару кликов";
    renderAvatar(me);
    render();
  } catch (e) {
    finishHeaderLoading();
    renderAvatar(null);
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
  state.paymentResult = null;
  const waitingText = payment.type === "traffic"
    ? "После оплаты ГБ добавятся автоматически."
    : payment.type === "devices"
      ? "После оплаты лимит устройств обновится автоматически."
      : "После оплаты ключ появится автоматически.";
  showToast(waitingText);
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
      state.paymentResult = status.summary || {
        title: "Оплата прошла",
        message: "Покупка активирована и уже отображается в подписках.",
        toast: "Оплата прошла. Покупка активирована.",
        subscription_id: payment.subscription_id || null,
      };
      state.selectedSubId = null;
      state.keysMode = "list";
      state.buyMode = "plan";
      switchView("payment-result", { preserve: true });
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
  renderPaymentResult();
}

function renderHome() {
  const active = activeSubs();
  const nearest = nearestActiveSub();
  const hasBypass = active.some((s) => s.plan_kind === "bypass");
  const nearestTone = statusTone(nearest);
  const primary = active.length ? { text: "Мои ключи", action: "openKeysList", hint: "Управление" } : { text: "Выбрать тариф", action: "openNewPurchase", hint: "Оформить доступ" };
  const secondary = active.length ? { text: "Продлить", action: "openRenewList", hint: "Выбрать ключ" } : { text: "Помощь", action: "switchView('help', { preserve: true })", hint: "Инструкции" };
  const primaryAction = primary.action.includes("(") ? primary.action : `${primary.action}()`;
  const secondaryAction = secondary.action.includes("(") ? secondary.action : `${secondary.action}()`;
  el("view-home").innerHTML = `
    <div class="home-dashboard">
      <div class="summary-grid">
        <div class="summary-card blue"><span>Активные ключи</span><b>${active.length}</b></div>
        <div class="summary-card green"><span>Антиглушилка</span><b>${hasBypass ? "Подключена" : "Нет"}</b></div>
      </div>

      ${nearest ? `<button class="status-card ${nearestTone}" onclick="openSubDetail(${nearest.id})">
        <span class="status-orb"></span>
        <div><p class="title">${subTitle(nearest)}</p><p class="muted">до ${date(nearest.subscription_until)} · осталось ${timeLeftText(nearest.subscription_until)}</p></div>
        <b>›</b>
      </button>` : `<div class="card empty soft-empty"><h2>Активных ключей пока нет</h2><p class="muted">Выберите подписку — ключ появится после оплаты.</p></div>`}

      <div class="quick-grid home-actions elevated-actions">
        <button class="quick-card gold primary-action" onclick="${primaryAction}"><span>${primary.text}</span><small>${primary.hint}</small></button>
        <button class="quick-card blue secondary-action" onclick="${secondaryAction}"><span>${secondary.text}</span><small>${secondary.hint}</small></button>
      </div>
      ${active.length || hasBypass ? `<div class="choice-list compact-actions">
        ${active.length ? `<button class="choice-button" onclick="openNewPurchase()"><span>Купить ещё ключ<small>Обычный или с антиглушилкой</small></span><b>›</b></button>` : ""}
        ${hasBypass ? `<button class="choice-button" onclick="openKeysList(); showToast('Выберите ключ с антиглушилкой и нажмите Купить ГБ')"><span>Купить ГБ<small>Для антиглушилки</small></span><b>›</b></button>` : ""}
      </div>` : ""}
    </div>`;
}

function renderKeys() {
  const container = el("view-subs");
  if (!state.subs.length) {
    container.innerHTML = `<div class="card empty"><h2>Ключей пока нет</h2><p class="muted">Оформите первый тариф, и ключ появится здесь.</p><button class="button accent" onclick="openNewPurchase()">Выбрать тариф</button></div>`;
    return;
  }

  if (state.keysMode === "list") {
    container.innerHTML = keysListHtml("Мои ключи", "Выберите ключ для управления.");
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
  if (state.keysMode === "device-addons") container.innerHTML = deviceAddonsHtml(sub);
  if (state.keysMode === "renew") container.innerHTML = renewHtml(sub);
  if (state.keysMode === "traffic") container.innerHTML = trafficHtml(sub);
  if (state.keysMode === "traffic-payment") container.innerHTML = paymentHtml("ГБ для " + subTitle(sub));
  if (state.keysMode === "devices-payment") container.innerHTML = paymentHtml("Устройства для " + subTitle(sub));
  if (state.keysMode === "renew-payment") container.innerHTML = paymentHtml("Продление " + subTitle(sub));
}

function renderPaymentResult() {
  const summary = state.paymentResult;
  el("view-payment-result").innerHTML = `<div class="payment-result-shell">
    <div class="card payment-result-card">
      <div class="result-icon">✓</div>
      <p class="step">Готово</p>
      <p class="title">${escapeHtml(summary?.title || "Оплата прошла")}</p>
      <p class="muted">${multilineHtml(summary?.message || "Покупка активирована и уже отображается в кабинете.")}</p>
      <button class="button green" onclick="returnToMainMenu()">Вернуться в главное меню</button>
    </div>
  </div>`;
}

function keysListHtml(title, subtitle, action = "detail") {
  return `<div class="grid">
    <div class="section-note"><p class="step">Ключи</p><p class="title">${title}</p><p class="muted">${subtitle}</p></div>
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
  const remaining = trafficRemaining(s);
  const tone = statusTone(s);
  const trafficToneClass = trafficTone(s);
  const devicePackages = s.devices?.packages || [];
  return `<div class="grid">
    <button class="button ghost" onclick="openKeysList()">← Назад к ключам</button>
    <div class="card key-hero ${s.plan_kind === "bypass" ? "plan-bypass" : "plan-regular"}">
      <div class="key-hero-top"><div><p class="step">${s.plan_kind === "bypass" ? "Антиглушилка" : "Обычный ключ"}</p><p class="title">${subTitle(s)}</p><p class="muted">${s.status === "active" ? `до ${date(s.subscription_until)} · ${timeLeftText(s.subscription_until)}` : "Срок закончился"}</p></div><span class="status-dot ${tone}"></span></div>
      ${badgesHtml(s)}
      <div class="metric-grid"><div><span>Статус</span><b>${s.status === "active" ? "Активна" : "Истекла"}</b></div><div><span>Устройства</span><b>${deviceLimitText(s)}</b></div></div>
      ${s.traffic.enabled ? `<div class="traffic-panel ${trafficToneClass}">${trafficToneClass ? `<p class="traffic-alert">${trafficToneClass === "danger" ? "Трафик почти закончился" : "Трафик заканчивается"}</p>` : ""}<div class="row"><span class="small">Осталось трафика</span><b>${remaining.toFixed(1)} ГБ</b></div><div class="progress"><span style="width:${percent}%"></span></div><div class="row"><span class="small">Использовано ${s.traffic.used_gb} / ${s.traffic.limit_gb} ГБ</span><span class="small">Сброс: ${date(s.traffic.reset_at)}</span></div></div>` : ""}
    </div>
    <div class="card key-actions-card"><p class="title">Подключение</p><p class="muted">Откройте ключ в Happ или скопируйте его.</p>${s.subscription_url ? `<div class="keybox compact-key">${s.subscription_url}</div><div class="action-grid"><button class="button blue" data-action="happ" data-url="${encodeURIComponent(s.subscription_url)}">Открыть в Happ</button><button class="button ghost" onclick="copyText('${encodeURIComponent(s.subscription_url)}')">Скопировать</button></div>` : `<p class="muted">Ключ появится после активации оплаты.</p>`}</div>
    <div class="action-grid sticky-actions">
      <button class="button accent" data-action="renew" data-sub-id="${s.id}">Продлить</button>
      <button class="button blue" data-action="devices" data-sub-id="${s.id}">Устройства</button>
      ${s.status === "active" && devicePackages.length ? `<button class="button green wide" data-action="device-addons" data-sub-id="${s.id}">Докупить устройства</button>` : ""}
      ${s.traffic.enabled ? `<button class="button green wide" data-action="traffic" data-sub-id="${s.id}">Купить ГБ</button>` : ""}
      <button class="button danger wide" data-action="delete-subscription" data-sub-id="${s.id}">Удалить подписку</button>
    </div>
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
    <div class="section-note"><p class="step">Устройства</p><p class="title">${subTitle(s)}</p><p class="muted">Лимит: до ${deviceLimitText(s)}. Удалите устройство, если нужно освободить слот для нового подключения.</p></div>
    ${list}
    ${s.status === "active" && (s.devices?.packages || []).length ? `<button class="button green" data-action="device-addons" data-sub-id="${s.id}">Докупить устройства</button>` : ""}
    ${devices.length ? `<button class="button danger" data-action="delete-all-devices" data-sub-id="${s.id}">Удалить все устройства</button>` : ""}
  </div>`;
}

function deviceAddonsHtml(s) {
  const packages = s.devices?.packages || [];
  if (!packages.length) {
    return `<div class="grid"><button class="button ghost" onclick="openSubDetail(${s.id})">← Назад к ключу</button><div class="card empty"><h2>Лимит уже максимальный</h2><p class="muted">Сейчас доступно до ${deviceLimitText(s)}.</p></div></div>`;
  }
  return `<div class="grid">
    <button class="button ghost" onclick="openSubDetail(${s.id})">← Назад к ключу</button>
    <div class="section-note"><p class="step">Устройства</p><p class="title">Докупить устройства</p><p class="muted">Выберите пакет для ${subTitle(s)}. Он действует до ${date(s.subscription_until)}.</p></div>
    <div class="choice-list">${packages.map((pkg) => `<button class="choice-button" onclick="prepareDevicePayment(${s.id}, ${pkg.count})"><span>${devicePackageText(pkg)}<small>После окончания периода лимит вернётся к базовому.</small></span><b>›</b></button>`).join("")}</div>
  </div>`;
}

function renewHtml(s) {
  const tariffs = state.tariffs[s.plan_kind] || [];
  return `<div class="grid">
    <button class="button ghost" onclick="openSubDetail(${s.id})">← Назад к ключу</button>
    <div class="section-note"><p class="step">Шаг 2 из 3</p><p class="title">${subTitle(s)}</p><p class="muted">Выберите срок продления.</p></div>
    <div class="choice-list">${tariffs.map((t) => `<button class="choice-button" onclick="prepareRenewPayment(${s.id}, '${t.code}')"><span>${tariffPeriod(t)}<small>${subTitle(s)}</small></span>${priceHtml(t)}</button>`).join("")}</div>
  </div>`;
}

function trafficHtml(s) {
  if (!s.traffic.enabled) return subscriptionDetailHtml(s);
  return `<div class="grid">
    <button class="button ghost" onclick="openSubDetail(${s.id})">← Назад к ключу</button>
    <div class="section-note"><p class="step">Шаг 2 из 3</p><p class="title">${subTitle(s)}</p><p class="muted">ГБ тратятся только при использовании антиглушилки.</p></div>
    <div class="choice-list">${state.tariffs.traffic_packages.map((p) => `<button class="choice-button" onclick="prepareTrafficPayment(${s.id}, '${p.code}')"><span>+${p.gb} ГБ<small>Дополнительный трафик</small></span>${priceHtml(p)}</button>`).join("")}</div>
  </div>`;
}

function renderBuy() {
  const container = el("view-buy");
  if (state.buyMode === "plan") {
    container.innerHTML = `<div class="grid">
      <div class="section-note"><p class="step">Шаг 1 из 3</p><p class="title">Выберите тип подписки</p></div>
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
      <div class="choice-list">${tariffs.map((t) => `<button class="choice-button" onclick="prepareNewPayment('${t.code}')"><span>${tariffPeriod(t)}<small>${t.title}</small></span>${priceHtml(t)}</button>`).join("")}</div>
    </div>`;
    return;
  }

  container.innerHTML = paymentHtml("Новая подписка");
}

function paymentHtml(title) {
  return `<div class="grid">
    <button class="button ghost" onclick="backFromPayment()">← Назад</button>
    <div class="section-note payment-note"><p class="step">Оплата</p><p class="title">${title}</p><p class="muted">Выберите удобный способ оплаты. После оплаты всё обновится автоматически.</p></div><div class="grid"><button class="button accent" onclick="payPrepared('cryptobot')">CryptoBot</button><button class="button green" onclick="payPrepared('yookassa')">Банковская карта</button></div>
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
function openDeviceAddons(id) { state.selectedSubId = id; state.keysMode = "device-addons"; switchView("subs", { preserve: true }); }
function returnToMainMenu() {
  stopPaymentPolling();
  state.paymentResult = null;
  state.pendingPayment = null;
  state.activePayment = null;
  state.selectedSubId = null;
  state.keysMode = "list";
  state.buyMode = "plan";
  switchView("home", { preserve: true });
}

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

function confirmAction(message) {
  if (tg?.showConfirm) return new Promise((resolve) => tg.showConfirm(message, resolve));
  return Promise.resolve(window.confirm(message));
}

async function deleteSubscription(id) {
  const sub = state.subs.find((item) => item.id === Number(id));
  const name = sub ? subTitle(sub) : "эту подписку";
  const ok = await confirmAction(`Удалить ${name}? Ключ будет удалён из Remnawave и перестанет работать. Деньги автоматически не возвращаются.`);
  if (!ok) return;
  try {
    await api(`/miniapp/api/subscriptions/${id}`, { method: "DELETE" });
    showToast("Подписка удалена");
    state.selectedSubId = null;
    state.keysMode = "list";
    await reloadData();
    switchView("subs", { preserve: true });
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

function prepareDevicePayment(subscriptionId, deviceCount) {
  state.pendingPayment = { type: "devices", subscription_id: subscriptionId, device_count: deviceCount };
  state.keysMode = "devices-payment";
  renderKeys();
}

function backFromPayment() {
  if (!state.pendingPayment) return;
  if (state.pendingPayment.type === "traffic") state.keysMode = "traffic";
  else if (state.pendingPayment.type === "devices") state.keysMode = "device-addons";
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
    } else if (state.pendingPayment.type === "devices") {
      data = await api("/miniapp/api/payments/devices", { method: "POST", body: JSON.stringify({ ...state.pendingPayment, provider }) });
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
    <div class="section-note help-note"><p class="step">Помощь</p><p class="title">Подключение и поддержка</p></div>
    <div class="help-card blue"><span>1</span><div><p class="title">Добавьте ключ в Happ</p><p class="muted">Откройте «Ключи», выберите подписку и нажмите «Открыть в Happ».</p></div></div>
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
  navigator.clipboard.writeText(text).catch(() => {});
  const bridgeUrl = happBridgeLink(text);
  if (tg?.openLink) tg.openLink(bridgeUrl);
  else window.location.href = bridgeUrl;
  showToast("Открываем Happ. Ключ также скопирован.");
}

load();
