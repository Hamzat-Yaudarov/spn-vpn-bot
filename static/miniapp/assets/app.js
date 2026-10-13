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
  paymentResult: null,
  paymentPollTimer: null,
  currentView: "subs",
  devices: {},
  devicesLoading: false,
  devicesError: {},
  deviceStatus: {},
  deviceRequestVersion: {},
  authStatus: "loading",
  dataStatus: { subs: "loading", tariffs: "loading", ref: "loading" },
  authError: null,
  paying: false,
  creatingPayment: null,
  disclosures: {},
  loadVersion: 0,
  refRequestVersion: 0,
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
    if (!res.ok) {
      const error = new Error(data.detail || "Ошибка запроса");
      error.status = res.status;
      throw error;
    }
    return data;
  });
}

function el(id) { return document.getElementById(id); }
function rub(value) { return `${Number(value).toLocaleString("ru-RU")} ₽`; }
function gb(value) { return Number(value).toLocaleString("ru-RU", { maximumFractionDigits: 2 }); }
function date(value) { return value && Number.isFinite(new Date(value).getTime()) ? new Date(value).toLocaleDateString("ru-RU") : "—"; }
function showToast(text) { const t = el("toast"); t.textContent = text; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 2400); }
function openLink(url) { tg?.openLink ? tg.openLink(url) : window.open(url, "_blank"); }
function subTitle(s) { return `${s.plan_kind === "bypass" ? "Антиглушилка" : "Обычная"} №${s.type_index}`; }
function activeSubs() { return state.subs.filter((s) => s.status === "active"); }
function selectedSub() { return state.subs.find((s) => s.id === state.selectedSubId); }
function tariffPeriod(t) { return t.days === 30 ? "1 месяц" : t.days === 90 ? "3 месяца" : `${t.days} дней`; }
function bypassBaseGb() {
  const value = state.tariffs?.bypass?.[0]?.base_gb;
  return value != null && Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : null;
}
function baseTrafficText() { const gb = bypassBaseGb(); return gb === null ? "Лимит указан в подписке" : `${gb} ГБ в месяц`; }
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
  if (s.traffic.limit_gb == null || s.traffic.used_gb == null) return null;
  const limit = Number(s.traffic.limit_gb), used = Number(s.traffic.used_gb);
  return Number.isFinite(limit) && Number.isFinite(used) ? Math.max(0, limit - used) : null;
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
    const img = document.createElement("img");
    img.src = user.photo_url;
    img.alt = name;
    avatar.replaceChildren(img);
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

document.addEventListener("toggle", (event) => {
  if (event.target.isConnected && event.target.matches?.("details[data-disclosure]")) {
    state.disclosures[event.target.dataset.disclosure] = event.target.open;
  }
}, true);

function disclosure(key, title, body) {
  return `<details class="disclosure" data-disclosure="${key}"${state.disclosures[key] ? " open" : ""}><summary>${title}</summary><div class="details-body">${body}</div></details>`;
}
function loadingHtml(label) {
  return `<div class="loading-state" role="status" aria-live="polite" aria-busy="true"><p>${label}</p><div class="loading-dashboard" aria-hidden="true"><span class="skeleton-block"></span><span class="skeleton-block"></span></div></div>`;
}
function errorHtml(title, action, message = "Проверьте интернет и попробуйте ещё раз.") {
  return `<div class="card error-state" role="status"><h2 class="title">${title}</h2><p class="muted">${message}</p><button class="button ghost" onclick="${action}">Повторить</button></div>`;
}
function sectionState(section) {
  if (state.authStatus === "error") return errorHtml("Не удалось открыть кабинет", "load()", state.authError?.status === 401 ? "Откройте MiniApp через Telegram-бота SPN." : "Проверьте интернет и попробуйте ещё раз.");
  if (state.authStatus === "loading" || state.dataStatus[section] === "loading") return loadingHtml("Загружаем данные…");
  if (state.dataStatus[section] === "error") return errorHtml("Не удалось загрузить данные", section === "ref" ? "refreshReferral()" : section === "subs" ? "reloadData()" : "load()");
  return null;
}
function pageHeading(title, subtitle = "") {
  return `<div class="page-heading"><h1>${title}</h1>${subtitle ? `<p class="muted">${subtitle}</p>` : ""}</div>`;
}

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
  if (target.dataset.action === "payment-link") openLink(decodeURIComponent(target.dataset.url || ""));
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
  if (view === "home") view = "subs"; // Compatibility with old return actions.
  const enteringReferral = view === "ref" && state.currentView !== "ref";
  if (options.reset) {
    if (state.currentView === "subs" || view === "subs") resetKeysState();
    if (state.currentView === "buy" || view === "buy") resetPurchaseState();
  }
  document.querySelectorAll(".tab").forEach((b) => {
    const active = b.dataset.view === view;
    b.classList.toggle("active", active);
    if (active) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  });
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  el(`view-${view}`).classList.add("active");
  document.body.className = `view-${view}-active`;
  state.currentView = view;
  render();
  window.scrollTo(0, 0);
  // Startup also fetches referral data; only a real tab entry counts as a view.
  if (enteringReferral) refreshReferral({ recordView: true });
}

async function load() {
  const version = ++state.loadVersion;
  const refVersion = ++state.refRequestVersion;
  state.authStatus = "loading";
  state.authError = null;
  state.dataStatus = { subs: "loading", tariffs: "loading", ref: "loading" };
  render();
  try {
    const me = await api("/miniapp/api/me");
    if (version !== state.loadVersion) return;
    state.me = me;
    state.authStatus = "ready";
    finishHeaderLoading();
    el("userLine").textContent = me.first_name || "Личный кабинет";
    renderAvatar(me);
    render();
    await Promise.allSettled([
      ["subs", "/miniapp/api/subscriptions", data => {
        if (!Array.isArray(data.subscriptions)) throw new Error("Subscriptions unavailable");
        state.subs = data.subscriptions;
      }],
      ["tariffs", "/miniapp/api/tariffs", data => {
        if (!Array.isArray(data.regular) || !Array.isArray(data.bypass)) throw new Error("Catalog unavailable");
        state.tariffs = data;
      }],
      ["ref", "/miniapp/api/referral", data => { state.referral = validReferral(data); }],
    ].map(async ([section, path, apply]) => {
      try {
        const data = await api(path);
        if (version !== state.loadVersion) return;
        if (section === "ref" && refVersion !== state.refRequestVersion) return;
        apply(data);
        state.dataStatus[section] = "ready";
      } catch (_) {
        if (version !== state.loadVersion) return;
        if (section === "ref" && refVersion !== state.refRequestVersion) return;
        state.dataStatus[section] = "error";
      }
      render();
    }));
  } catch (e) {
    if (version !== state.loadVersion) return;
    state.authStatus = "error";
    state.authError = e;
    finishHeaderLoading();
    renderAvatar(null);
    el("userLine").textContent = "Личный кабинет";
    render();
  }
}

async function reloadData() {
  state.dataStatus.subs = "loading";
  renderKeys();
  try {
    const subscriptions = await api("/miniapp/api/subscriptions");
    if (!Array.isArray(subscriptions.subscriptions)) throw new Error("Subscriptions unavailable");
    state.subs = subscriptions.subscriptions;
    state.dataStatus.subs = "ready";
  } catch (error) {
    state.dataStatus.subs = "error";
    return false;
  } finally { render(); }
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
      : "После оплаты подписка появится автоматически.";
  showToast(waitingText);
  state.paymentPollTimer = setInterval(checkActivePayment, 3000);
}

async function checkActivePayment() {
  const payment = state.activePayment;
  if (!payment) return stopPaymentPolling();
  if (payment.checking) return;
  payment.checking = true;
  payment.attempts += 1;

  try {
    const status = await api(`/miniapp/api/payments/${encodeURIComponent(payment.invoice_id)}`);
    if (state.activePayment !== payment) return;
    if (status.status === "paid") {
      stopPaymentPolling();
      state.activePayment = null;
      state.pendingPayment = null;
      await reloadData();
      // A later checkout must not be replaced by an older status response.
      if (state.activePayment || state.paying || state.pendingPayment) return;
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
  } finally { payment.checking = false; }

  if (state.activePayment !== payment) return;
  if (payment.attempts >= 120) {
    stopPaymentPolling();
    payment.autoCheckPaused = true;
    showToast("Счёт сохранён. Можно проверить оплату вручную.");
    render();
  }
}

function render() {
  document.querySelectorAll("details[data-disclosure]").forEach(node => {
    state.disclosures[node.dataset.disclosure] = node.open;
  });
  renderKeys();
  renderBuy();
  renderReferral();
  renderHelp();
  renderPaymentResult();
}

function renderKeys() {
  const container = el("view-subs");
  const unavailable = sectionState("subs");
  if (unavailable) { container.innerHTML = pageHeading("Мой VPN") + unavailable; return; }
  if (!state.subs.length) {
    container.innerHTML = pageHeading("Мой VPN") + `<div class="card empty"><h2 class="title">Подписок пока нет</h2><p class="muted">Купите подписку, чтобы подключиться.</p><button class="button accent" onclick="openNewPurchase()">Купить подписку</button></div>`;
    return;
  }

  if (state.keysMode === "list") {
    container.innerHTML = keysListHtml("Мой VPN", "Выберите подписку.");
    return;
  }

  if (state.keysMode === "renew-list") {
    container.innerHTML = keysListHtml("Что продлить?", "Выберите подписку.", "renew");
    return;
  }

  const sub = selectedSub();
  if (!sub) {
    state.keysMode = "list";
    container.innerHTML = keysListHtml("Мой VPN", "Выберите подписку.");
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
        <p class="title">${escapeHtml(summary?.title || "Оплата прошла")}</p>
        <p class="muted">${multilineHtml(summary?.message || "Покупка активирована и уже отображается в кабинете.")}</p>
        <button class="button accent" onclick="returnToMainMenu()">Мой VPN</button>
    </div>
  </div>`;
}

function keysListHtml(title, subtitle, action = "detail") {
  const ordered = state.subs.filter(s => s.status === "active").concat(state.subs.filter(s => s.status !== "active"));
  return `<div class="grid">
    ${pageHeading(title, subtitle)}
    <div class="subscription-list">${ordered.map((s) => keyButton(s, action)).join("")}</div>
  </div>`;
}

function keyButton(s, action) {
  const handler = action === "renew" ? `openRenew(${s.id})` : `openSubDetail(${s.id})`;
  const remaining = trafficRemaining(s);
  const active = s.status === "active";
  const status = active ? "Активна" : s.status === "expired" ? "Истекла" : "Статус уточняется";
  const expiry = active ? `До ${date(s.subscription_until)}` : s.status === "expired" ? "Срок закончился" : "Срок уточняется";
  return `<button class="subscription-card key-choice" data-sub-id="${s.id}" onclick="${handler}"><span><span class="subscription-name">${escapeHtml(subTitle(s))}</span><span class="subscription-meta">${expiry}</span>${s.traffic?.enabled ? `<span class="subscription-meta ${trafficTone(s)}">${remaining === null ? "Трафик уточняется" : `Осталось ${gb(remaining)} ГБ`}</span>` : ""}<span class="status-label ${active ? statusTone(s) : "expired"}">${status}</span></span><span class="chevron" aria-hidden="true">›</span></button>`;
}

function subscriptionDetailHtml(s) {
  const percent = s.traffic?.enabled && s.traffic.limit_gb ? Math.min(100, Math.max(0, Math.round((s.traffic.used_gb / s.traffic.limit_gb) * 100))) : 0;
  const remaining = trafficRemaining(s);
  const tone = statusTone(s);
  const trafficToneClass = trafficTone(s);
  const devicePackages = s.devices?.packages || [];
  const active = s.status === "active";
  const status = active ? "Подписка активна" : s.status === "expired" ? "Подписка истекла" : "Статус уточняется";
  const expiry = active ? `До ${date(s.subscription_until)}` : s.status === "expired" ? "Срок закончился" : "—";
  const encodedUrl = s.subscription_url ? escapeHtml(encodeURIComponent(s.subscription_url).replace(/'/g, "%27")) : "";
  return `<div class="grid">
    <button class="button back-button ghost" onclick="openKeysList()">← Мой VPN</button>
    ${pageHeading(escapeHtml(subTitle(s)))}
    <div class="card subscription-summary">
      <span class="status-label ${active ? tone : "expired"}">${status}</span>
      <div class="summary-line"><span>Срок</span><b>${expiry}</b></div>
      <div class="summary-line"><span>Устройства</span><b>${deviceLimitText(s)}</b></div>
      ${s.traffic?.enabled ? `<div class="traffic-panel ${trafficToneClass}"><div class="summary-line"><span>Осталось</span><b>${remaining === null ? "—" : `${gb(remaining)} ГБ`}</b></div>${remaining !== null ? `<div class="progress" role="progressbar" aria-label="Использованный трафик" aria-valuenow="${percent}" aria-valuemin="0" aria-valuemax="100"><span style="width:${percent}%"></span></div><p class="small">Использовано ${gb(s.traffic.used_gb)} из ${gb(s.traffic.limit_gb)} ГБ</p>` : `<p class="small">Не удалось уточнить трафик.</p>`}${trafficToneClass ? `<p class="small ${trafficToneClass}">Трафик ${remaining === 0 ? "закончился" : "заканчивается"}</p>` : ""}<p class="small">Обновление периода: ${date(s.traffic.reset_at)}</p></div>` : ""}
    </div>
    <div class="action-grid">
      ${active && encodedUrl ? `<button class="button accent" data-action="happ" data-url="${encodedUrl}">Открыть в Happ</button>` : ""}
      <button class="button ${active && encodedUrl ? "ghost" : "accent wide"}" data-action="renew" data-sub-id="${s.id}">Продлить</button>
    </div>
    ${active && !encodedUrl ? `<p class="muted">Ссылка подключения пока недоступна. Обновите данные или напишите в поддержку.</p><button class="button ghost" onclick="reloadData()">Обновить данные</button>` : ""}
    ${encodedUrl ? `<button class="button ghost" onclick="copyText('${encodedUrl}')">Скопировать ссылку</button>${disclosure(`link-${s.id}`, "Ссылка подписки", `<div class="keybox">${escapeHtml(s.subscription_url)}</div>`)}` : ""}
    <div class="details-list">
      <button class="choice-button" data-action="devices" data-sub-id="${s.id}"><span>Устройства</span><span class="chevron" aria-hidden="true">›</span></button>
      ${active && devicePackages.length ? `<button class="choice-button" data-action="device-addons" data-sub-id="${s.id}"><span>Докупить устройства</span><span class="chevron" aria-hidden="true">›</span></button>` : ""}
      ${s.traffic?.enabled ? `<button class="choice-button" data-action="traffic" data-sub-id="${s.id}"><span>Докупить ГБ</span><span class="chevron" aria-hidden="true">›</span></button>` : ""}
    </div>
    ${disclosure(`more-${s.id}`, "Ещё", `<button class="button danger" data-action="delete-subscription" data-sub-id="${s.id}">Удалить подписку</button>`)}
  </div>`;
}

function deviceTitle(device) {
  const platform = device.platform || "Устройство";
  return device.device_model ? `${platform} • ${device.device_model}` : platform;
}

function devicesHtml(s) {
  const devices = state.devices[s.id] || [];
  const back = `<button class="button back-button ghost" onclick="openSubDetail(${s.id})">← Назад к подписке</button>`;
  if (state.deviceStatus[s.id] === "loading") {
    return `<div class="grid">${back}${pageHeading("Устройства")}${loadingHtml("Загружаем устройства…")}</div>`;
  }
  if (state.deviceStatus[s.id] === "error") {
    return `<div class="grid">${back}${pageHeading("Устройства")}${errorHtml("Не удалось загрузить устройства", `openDevices(${s.id})`)}</div>`;
  }

  const list = devices.length
    ? devices.map((device, index) => `<div class="card device-card"><p class="title">${escapeHtml(deviceTitle(device))}</p><p class="small">Добавлено: ${date(device.created_at)}</p>${device.hwid ? disclosure(`device-${s.id}-${index}`, "Данные устройства", `<p class="keybox">${escapeHtml(device.hwid)}</p>`) : ""}${device.hwid ? `<button class="button danger" data-action="delete-device" data-sub-id="${s.id}" data-hwid="${escapeHtml(device.hwid)}">Удалить устройство</button>` : ""}</div>`).join("")
    : `<div class="card empty"><h2 class="title">Устройств пока нет</h2><p class="muted">Они появятся после подключения.</p></div>`;

  return `<div class="grid">
    ${back}${pageHeading("Устройства", escapeHtml(subTitle(s)))}
    <p class="muted">Лимит: ${deviceLimitText(s)}.</p>
    ${list}
    ${s.status === "active" && (s.devices?.packages || []).length ? `<button class="button ghost" data-action="device-addons" data-sub-id="${s.id}">Докупить устройства</button>` : ""}
    ${devices.length ? `<button class="button danger" data-action="delete-all-devices" data-sub-id="${s.id}">Удалить все устройства</button>` : ""}
  </div>`;
}

function deviceAddonsHtml(s) {
  const packages = s.devices?.packages || [];
  if (!packages.length) {
    return `<div class="grid"><button class="button back-button ghost" onclick="openSubDetail(${s.id})">← Назад к подписке</button><div class="card empty"><h2 class="title">Лимит уже максимальный</h2><p class="muted">Доступно: ${deviceLimitText(s)}.</p></div></div>`;
  }
  return `<div class="grid">
    <button class="button back-button ghost" onclick="openSubDetail(${s.id})">← Назад к подписке</button>
    ${pageHeading("Докупить устройства", escapeHtml(subTitle(s)))}
    <p class="muted">Дополнительные устройства действуют до ${date(s.subscription_until)}.</p>
    <div class="choice-list">${packages.map((pkg) => `<button class="choice-button" onclick="prepareDevicePayment(${s.id}, ${pkg.count})"><span>${devicePackageText(pkg)}<small>После окончания периода лимит вернётся к базовому.</small></span><b>›</b></button>`).join("")}</div>
  </div>`;
}

function renewHtml(s) {
  const unavailable = sectionState("tariffs");
  const tariffs = state.tariffs?.[s.plan_kind] || [];
  return `<div class="grid">
    <button class="button back-button ghost" onclick="openSubDetail(${s.id})">← Назад к подписке</button>
    ${pageHeading("Продлить подписку", escapeHtml(subTitle(s)))}
    ${unavailable || `<p class="muted">Выберите срок.</p><div class="choice-list">${tariffs.map((t) => `<button class="choice-button" onclick="prepareRenewPayment(${s.id}, '${t.code}')"><span>${tariffPeriod(t)}</span>${priceHtml(t)}</button>`).join("")}</div>`}
  </div>`;
}

function trafficHtml(s) {
  if (!s.traffic?.enabled) return subscriptionDetailHtml(s);
  const unavailable = sectionState("tariffs");
  return `<div class="grid">
    <button class="button back-button ghost" onclick="openSubDetail(${s.id})">← Назад к подписке</button>
    ${pageHeading("Докупить ГБ", escapeHtml(subTitle(s)))}
    ${unavailable || `<p class="muted">Для подключения через антиглушилку.</p><div class="choice-list">${(state.tariffs.traffic_packages || []).map((p) => `<button class="choice-button" onclick="prepareTrafficPayment(${s.id}, '${p.code}')"><span>+${p.gb} ГБ</span>${priceHtml(p)}</button>`).join("")}</div>`}
  </div>`;
}

function renderBuy() {
  const container = el("view-buy");
  const unavailable = sectionState("tariffs");
  if (unavailable) { container.innerHTML = pageHeading("Купить подписку") + unavailable; return; }
  if (state.buyMode === "plan") {
    container.innerHTML = `<div class="grid">
      ${pageHeading("Купить подписку", "Выберите вариант.")}
      <div class="plan-grid">
        <button class="plan-card" onclick="selectBuyPlan('regular')"${state.tariffs.regular.length ? "" : " disabled"}><span class="title">Обычная</span><small>5 устройств · без лимита ГБ</small><small>Для ежедневного подключения</small></button>
        <button class="plan-card" onclick="selectBuyPlan('bypass')"${state.tariffs.bypass.length ? "" : " disabled"}><span class="title">С антиглушилкой</span><small>3 устройства · ${baseTrafficText()}</small><small>Для обхода ограничений</small></button>
      </div>
      ${!state.tariffs.regular.length && !state.tariffs.bypass.length ? `<p class="muted">Покупка временно недоступна.</p>` : ""}
    </div>`;
    return;
  }

  if (state.buyMode === "tariff") {
    const tariffs = state.tariffs[state.buyPlan] || [];
    container.innerHTML = `<div class="grid">
      <button class="button back-button ghost" onclick="resetBuy()">← Назад</button>
      ${pageHeading(state.buyPlan === "regular" ? "Обычная подписка" : "С антиглушилкой", "Выберите срок.")}
      <div class="choice-list">${tariffs.map((t) => `<button class="choice-button" onclick="prepareNewPayment('${t.code}')"><span>${tariffPeriod(t)}</span>${priceHtml(t)}</button>`).join("")}</div>
    </div>`;
    return;
  }

  container.innerHTML = paymentHtml("Новая подписка");
}

function checkoutSummary(payment) {
  if (!payment || !state.tariffs) return "";
  const sub = state.subs.find(s => s.id === payment.subscription_id);
  let product, description;
  if (payment.type === "traffic") {
    product = state.tariffs.traffic_packages?.find(p => p.code === payment.package_code);
    description = product ? `+${product.gb} ГБ` : "Дополнительный трафик";
  } else if (payment.type === "devices") {
    product = sub?.devices?.packages?.find(p => p.count === payment.device_count);
    description = product ? `+${product.count} ${deviceWord(product.count)}` : "Дополнительные устройства";
  } else {
    product = [...state.tariffs.regular, ...state.tariffs.bypass].find(t => t.code === payment.tariff_code);
    const kind = product?.kind || (state.tariffs.bypass.some(t => t.code === payment.tariff_code) ? "bypass" : "regular");
    description = product ? `${kind === "bypass" ? "С антиглушилкой" : "Обычная"} · ${tariffPeriod(product)}` : "Подписка";
  }
  return `<div class="card checkout-summary"><p class="title">${description}</p>${sub ? `<p class="small">${escapeHtml(subTitle(sub))}</p>` : ""}${product ? `<div class="summary-line"><span>К оплате</span>${priceHtml(product)}</div>` : `<p class="muted">Цена недоступна. Вернитесь к выбору покупки.</p>`}</div>`;
}
function sameCheckout(a, b) {
  return a && b && ["type", "tariff_code", "payment_target", "subscription_id", "package_code", "device_count"].every(key => a[key] === b[key]);
}
function paymentHtml(title) {
  const busy = state.paying;
  const waiting = sameCheckout(state.activePayment, state.pendingPayment) ? state.activePayment : null;
  return `<div class="grid">
    <button class="button back-button ghost" onclick="backFromPayment()">← Назад</button>
    ${pageHeading(title)}${checkoutSummary(state.pendingPayment)}
    ${waiting ? `<div class="card payment-wait" role="status"><h2 class="title">Счёт создан</h2><p class="muted">${waiting.autoCheckPaused ? "Счёт сохранён. Проверьте оплату или обновите подписки." : "После оплаты покупка появится автоматически."}</p><button class="button accent" data-action="payment-link" data-url="${escapeHtml(encodeURIComponent(waiting.pay_url || ""))}">Открыть оплату</button><button class="button ghost" onclick="checkActivePayment()">Проверить оплату</button></div>` : `<p class="muted">Выберите способ оплаты.</p><div class="grid"><button class="button accent" data-provider="yookassa" onclick="payPrepared('yookassa')"${busy ? " disabled aria-busy=\"true\"" : ""}>${busy ? "Создаём счёт…" : "Банковская карта"}</button><button class="button ghost" data-provider="cryptobot" onclick="payPrepared('cryptobot')"${busy ? " disabled" : ""}>CryptoBot</button></div>`}
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
  state.deviceStatus[id] = "loading";
  const version = (state.deviceRequestVersion[id] || 0) + 1;
  state.deviceRequestVersion[id] = version;
  renderKeys();
  try {
    const data = await api(`/miniapp/api/subscriptions/${id}/devices`);
    if (state.deviceRequestVersion[id] !== version) return;
    if (!Array.isArray(data.devices)) throw new Error("Devices unavailable");
    state.devices[id] = data.devices;
    state.deviceStatus[id] = "ready";
  } catch (e) {
    showToast(e.message);
    if (state.deviceRequestVersion[id] === version) state.deviceStatus[id] = "error";
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
  const ok = await confirmAction(`Удалить подписку «${name}»? Подключение перестанет работать. Деньги автоматически не возвращаются.`);
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
  if (!state.pendingPayment || state.paying || !["yookassa", "cryptobot"].includes(provider)) return;
  const payment = { ...state.pendingPayment };
  state.paying = true;
  state.creatingPayment = payment;
  render();
  try {
    let data;
    if (payment.type === "traffic") {
      data = await api("/miniapp/api/payments/traffic", { method: "POST", body: JSON.stringify({ ...payment, provider }) });
    } else if (payment.type === "devices") {
      data = await api("/miniapp/api/payments/devices", { method: "POST", body: JSON.stringify({ ...payment, provider }) });
    } else {
      data = await api("/miniapp/api/payments/subscription", { method: "POST", body: JSON.stringify({ ...payment, provider }) });
    }
    startPaymentPolling({ ...payment, invoice_id: data.invoice_id, pay_url: data.pay_url, provider });
    openLink(data.pay_url);
    showToast("Счёт создан. Ждём оплату автоматически.");
  } catch (e) {
    showToast(e.message);
  } finally {
    state.paying = false;
    state.creatingPayment = null;
    render();
  }
}


function openEarningBot(action = "home") {
  const r = state.referral;
  let url = r?.earning_actions?.[action] || r?.bot_earning_url;
  if (!url) { showToast("Не удалось загрузить ссылку. Обновите кабинет."); return; }
  // Keep action-specific entry working with an older server response too.
  if (!r?.earning_actions?.[action] && ["spend", "withdraw"].includes(action)) {
    const fallback = new URL(url);
    fallback.searchParams.set("start", action === "spend" ? "earn_spend" : "earn_withdraw");
    url = fallback.toString();
  }
  if (tg?.openTelegramLink) tg.openTelegramLink(url); else window.open(url, "_blank", "noopener");
}
function shareReferral() {
  const r = state.referral;
  if (!r?.link) return;
  const url = 'https://t.me/share/url?' + new URLSearchParams({url: r.link, text: r.share_text || ''});
  if (tg?.openTelegramLink) tg.openTelegramLink(url); else window.open(url, "_blank", "noopener");
}
function copyReferral() {
  if (state.referral?.link) copyText(encodeURIComponent(state.referral.link));
}
async function refreshReferral({ recordView = false } = {}) {
  const version = ++state.refRequestVersion;
  state.dataStatus.ref = "loading";
  renderReferral();
  try {
    const data = validReferral(await api("/miniapp/api/referral" + (recordView ? "?view=1" : "")));
    if (version !== state.refRequestVersion) return;
    state.referral = data;
    state.dataStatus.ref = "ready";
  }
  catch (_) { if (version === state.refRequestVersion) state.dataStatus.ref = "error"; }
  finally { if (version === state.refRequestVersion) renderReferral(); }
}
function validReferral(data) {
  if (!data || data.current_balance == null || !Number.isFinite(Number(data.current_balance))) {
    throw new Error("Referral data unavailable");
  }
  return data;
}
function referralMetric(value, currency = false) {
  return value == null || !Number.isFinite(Number(value)) ? "—" : currency ? rub(value) : Number(value).toLocaleString("ru-RU");
}
function renderReferral() {
  const r = state.referral;
  const unavailable = sectionState("ref");
  if (unavailable || !r || r.current_balance == null || !Number.isFinite(Number(r.current_balance))) {
    el("view-ref").innerHTML = pageHeading("Заработать") + (unavailable || errorHtml("Не удалось загрузить баланс", "refreshReferral()"));
    return;
  }
  const minimum = r?.minimum_withdrawal || 1500;
  const remaining = Math.max(0, minimum - Number(r.current_balance));
  const statuses = {pending: "На рассмотрении", completed: "Выплачено", rejected: "Отклонено — резерв возвращён"};
  const history = (r?.history || []).map(row => {
    let title = 'Начисление за покупку друга', amount = '+' + rub(row.amount);
    if (row.kind === 'reversal') {
      title = 'Корректировка после возврата';
      amount = '−' + rub(row.amount);
    } else if (row.kind === 'withdrawal') {
      title = String(row.detail || '').startsWith('subscription_') ? 'Оплата своей подписки' : 'Заявка R-' + row.id + ' · ' + (statuses[row.status] || row.status);
      amount = rub(row.amount);
    }
    return '<div class="ref-history-row"><span>' + escapeHtml(title) + '<small>' + date(row.created_at) + '</small></span><b>' + escapeHtml(amount) + '</b></div>';
  }).join('');
  const examples = (r?.pricing_examples || []).map(example =>
    '<p class="muted">Оплата ' + rub(example.price) + ' → ' + rub(example.first_commission) + ' вам; следующая — ' + rub(example.repeat_commission) + '.</p>'
  ).join('');
  const registered = referralMetric(r.registered_referrals);
  const refundNote = Number(r?.refunded_amount || 0) > 0
    ? '<p class="muted">Учтено возвратов: −' + rub(r.refunded_amount) + '. Доступный баланс уже скорректирован.</p>' : '';
  const stats = '<div class="summary-line"><span>Пришли по ссылке</span><b>' + registered + '</b></div>' +
    '<div class="summary-line"><span>Друзей с покупками</span><b>' + referralMetric(r.active_referrals) + '</b></div>' +
    '<div class="summary-line"><span>Всего заработано</span><b>' + referralMetric(r.total_earned, true) + '</b></div>' + refundNote;
  const rules = '<ol><li>Отправьте другу свою ссылку.</li><li>Друг впервые открывает бота по ней и покупает подписку.</li><li>Получайте деньги с покупок и продлений.</li></ol>' +
    (examples ? '<p class="small">Примеры с текущих месячных цен</p>' + examples : '') +
    '<p class="muted">Процент — от оплаченной суммы, включая скидки. Покупки за реферальный баланс не участвуют. Без покупки начислений нет. Уже зарегистрированного пользователя перепривязать нельзя. Самоприглашения не учитываются.</p>' +
    (r.link ? '<p class="small">Ваша ссылка</p><div class="keybox">' + escapeHtml(r.link) + '</div>' : '');
  el("view-ref").innerHTML = '<div class="grid">' + pageHeading('Заработать') +
    '<div class="card earning-balance"><p class="small">Доступно</p><p class="balance-value">' + rub(r.current_balance) + '</p><p class="muted"><b>35%</b> — первая покупка друга<br><b>15%</b> — следующие покупки</p></div>' +
    '<button class="button accent" onclick="shareReferral()"' + (r.link ? '' : ' disabled') + '>Пригласить друга</button>' +
    '<button class="button ghost" onclick="copyReferral()"' + (r.link ? '' : ' disabled') + '>Скопировать ссылку</button>' +
    '<div class="action-grid"><button class="button ghost" onclick="openEarningBot(\'spend\')">Потратить в боте</button><button class="button ghost" onclick="openEarningBot(\'withdraw\')">Вывести в боте</button></div>' +
    '<p class="small">' + (remaining ? 'До вывода осталось ' + rub(remaining) : 'Можно оформить вывод') + '. Вывод через СБП / USDT от ' + rub(minimum) + ' после проверки заявки.</p>' +
    disclosure('ref-stats', 'Статистика', stats) + disclosure('ref-rules', 'Как это работает', rules) +
    disclosure('ref-history', 'История', '<div id="referral-history">' + (history || '<p class="muted">Операций пока нет.</p>') + '<p class="small">Полная история доступна в боте.</p><button class="button ghost" onclick="openEarningBot(\'home\')">Открыть историю в боте</button></div>') +
    '<button class="button ghost" onclick="refreshReferral()">Обновить</button></div>';
}

function renderHelp() {
  el("view-help").innerHTML = `<div class="grid">
    ${pageHeading("Помощь")}
    <div class="card"><h2 class="title">Как подключиться</h2><ol><li>Откройте «Мой VPN» и выберите подписку.</li><li>Нажмите «Открыть в Happ».</li></ol></div>
    <div class="card"><h2 class="title">Другое приложение?</h2><p class="muted">Нажмите «Скопировать ссылку» в подписке. Откройте VPN-приложение и добавьте её через «+».</p></div>
    <button class="button accent" onclick="openLink('https://t.me/wayspn_support')">Написать в поддержку</button>
  </div>`;
}

async function copyText(encoded) {
  const text = decodeURIComponent(encoded);
  try { await navigator.clipboard.writeText(text); showToast("Скопировано"); }
  catch (_) { showToast("Не удалось скопировать. Выделите ссылку и скопируйте вручную."); }
}

function openKeyInHapp(encoded) {
  const text = decodeURIComponent(encoded);
  navigator.clipboard?.writeText?.(text)?.catch(() => {});
  const bridgeUrl = happBridgeLink(text);
  if (tg?.openLink) tg.openLink(bridgeUrl);
  else window.location.href = bridgeUrl;
  showToast("Открываем Happ.");
}

load();
