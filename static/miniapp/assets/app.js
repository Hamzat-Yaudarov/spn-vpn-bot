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
  refMode: "home",
  wallet: null,
  withdrawal: null,
  nativeOperations: { purchase: null, withdrawal: null },
  nativeRestoreUser: null,
  nativePollTimer: null,
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
      const detail = data.detail;
      const error = new Error(typeof detail === "object" && detail ? detail.message || "Ошибка запроса" : detail || "Ошибка запроса");
      error.status = res.status;
      error.code = typeof detail === "object" && detail ? detail.code : null;
      error.detail = detail;
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
  if (target.dataset.action === "delete-device") deleteDevice(subId, state.devices[subId]?.[Number(target.dataset.deviceIndex)]?.hwid || "");
  if (target.dataset.action === "delete-all-devices") deleteAllDevices(subId);
  if (target.dataset.action === "happ") {
    event.preventDefault();
    openKeyInHapp(target.dataset.url || "");
  }
  if (target.dataset.action === "payment-link") openLink(decodeURIComponent(target.dataset.url || ""));
  if (target.dataset.action === "wallet-tariff") reviewWalletTariff(target.dataset.code);
  if (target.dataset.action === "wallet-renew") selectWalletSubscription(subId);
});

document.addEventListener("submit", event => {
  const form = event.target.closest("[data-native-form]");
  if (!form) return;
  event.preventDefault();
  advanceWithdrawal(form);
});

document.addEventListener("input", event => {
  const input = event.target.closest("[data-native-form] input[name]");
  if (!input || !state.withdrawal || !["amount", "bank", "phone", "address"].includes(input.name)) return;
  // Keep unfinished form edits in this page only. Never persist payment details.
  state.withdrawal[input.name] = input.value;
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
    if (view === "ref") state.refMode = "home";
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
    restoreNativeOperations();
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
  renderReferral({ background: true });
  renderHelp();
  renderPaymentResult();
}

function renderKeys() {
  const container = el("view-subs");
  const unavailable = sectionState("subs");
  if (unavailable) { container.innerHTML = pageHeading("Мои подписки") + unavailable; return; }
  if (!state.subs.length) {
    container.innerHTML = pageHeading("Мои подписки") + `<div class="card empty"><h2 class="title">Подписок пока нет</h2><p class="muted">Купите подписку, чтобы подключиться.</p><button class="button accent" onclick="openNewPurchase()">Купить подписку</button></div>`;
    return;
  }

  if (state.keysMode === "list") {
    container.innerHTML = keysListHtml("Мои подписки", "Выберите подписку.");
    return;
  }

  if (state.keysMode === "renew-list") {
    container.innerHTML = keysListHtml("Что продлить?", "Выберите подписку.", "renew");
    return;
  }

  const sub = selectedSub();
  if (!sub) {
    state.keysMode = "list";
    container.innerHTML = keysListHtml("Мои подписки", "Выберите подписку.");
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
        <button class="button accent" onclick="returnToMainMenu()">Мои подписки</button>
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
  const active = s.status === "active";
  const status = active ? "Подписка активна" : s.status === "expired" ? "Подписка истекла" : "Статус уточняется";
  const expiry = active ? `До ${date(s.subscription_until)}` : s.status === "expired" ? "Срок закончился" : "—";
  const encodedUrl = s.subscription_url ? escapeHtml(encodeURIComponent(s.subscription_url).replace(/'/g, "%27")) : "";
  return `<div class="grid">
    <button class="button back-button ghost" onclick="openKeysList()">← Мои подписки</button>
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
      <button class="choice-button" data-action="device-addons" data-sub-id="${s.id}"><span>Докупить устройства</span><span class="chevron" aria-hidden="true">›</span></button>
      ${s.traffic?.enabled ? `<button class="choice-button" data-action="traffic" data-sub-id="${s.id}"><span>Докупить ГБ</span><span class="chevron" aria-hidden="true">›</span></button>` : ""}
    </div>
    ${disclosure(`more-${s.id}`, "Ещё", `<button class="button danger" data-action="delete-subscription" data-sub-id="${s.id}">Удалить подписку</button>`)}
  </div>`;
}

function deviceTitle(device) {
  const platform = /^(Android|iOS|Windows|Linux|macOS|Mac OS|iPhone|iPad)$/i.test(device.platform || "") ? device.platform : "Устройство";
  const model = String(device.device_model || "").trim();
  return model && model.length <= 100 && !/[<>\r\n]/.test(model) ? `${platform} • ${model}` : platform;
}

function deviceClientName(device) {
  const userAgent = String(device.user_agent || device.userAgent || "");
  if (/\bhapp[\s_-]*plus\b|\bhapp\+/i.test(userAgent)) return "Happ Plus";
  if (/\bhapp\b/i.test(userAgent)) return "Happ";
  if (/\bincy\b/i.test(userAgent)) return "INCY";
  return "Не определено";
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
    ? devices.map((device, index) => `<div class="card device-card"><p class="title">${escapeHtml(deviceTitle(device))}</p><p class="small">Добавлено: ${date(device.created_at)} · Приложение: ${deviceClientName(device)}</p>${device.hwid ? `<button class="button danger" data-action="delete-device" data-sub-id="${s.id}" data-device-index="${index}">Удалить устройство</button>` : ""}</div>`).join("")
    : `<div class="card empty"><h2 class="title">Устройств пока нет</h2><p class="muted">Они появятся после подключения.</p></div>`;

  return `<div class="grid">
    ${back}${pageHeading("Устройства", escapeHtml(subTitle(s)))}
    <p class="muted">Добавлено: ${devices.length} из ${Number(s.devices?.limit || (s.plan_kind === "bypass" ? 3 : 5))} устройств.</p>
    ${list}
    <button class="button ghost" data-action="device-addons" data-sub-id="${s.id}">Докупить устройства</button>
    ${devices.length ? `<button class="button danger" data-action="delete-all-devices" data-sub-id="${s.id}">Удалить все устройства</button>` : ""}
  </div>`;
}

function deviceAddonsHtml(s) {
  const packages = s.devices?.packages || [];
  const status = s.devices?.addon_status;
  if (status !== "available" || !packages.length) {
    const title = status === "expired" ? "Сначала продлите подписку" : status === "max_limit" ? "Лимит уже максимальный" : status === "unavailable" ? "Докупка недоступна" : "Докупка временно недоступна";
    const message = status === "expired" ? "Дополнительные устройства доступны для активной подписки." : status === "max_limit" ? `Доступно: ${deviceLimitText(s)}.` : status === "unavailable" ? "Для этой подписки докупка устройств недоступна." : "Не удалось уточнить доступные пакеты. Обновите данные.";
    return `<div class="grid"><button class="button back-button ghost" onclick="openSubDetail(${s.id})">← Назад к подписке</button>${pageHeading("Докупить устройства")}<div class="card empty"><h2 class="title">${title}</h2><p class="muted">${message}</p>${status === "expired" ? `<button class="button accent" onclick="openRenew(${s.id})">Продлить подписку</button>` : status === "max_limit" ? "" : `<button class="button ghost" onclick="reloadData()">Обновить данные</button>`}</div></div>`;
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


function nativeUserId() {
  const id = state.me?.tg_id ?? state.me?.id;
  return Number.isSafeInteger(Number(id)) && Number(id) > 0 ? String(id) : null;
}
function nativeStorageKey(type) { return `spn:miniapp:referral:${nativeUserId()}:${type}`; }
function saveNativeOperation(operation) {
  if (!nativeUserId()) throw new Error("Откройте MiniApp через Telegram.");
  try {
    localStorage.setItem(nativeStorageKey(operation.type), JSON.stringify({ request_key: operation.request_key, type: operation.type }));
  } catch (_) { throw new Error("Не удалось сохранить проверку операции. Попробуйте открыть MiniApp заново."); }
}
function clearNativeOperationStorage(type) {
  try { localStorage.removeItem(nativeStorageKey(type)); } catch (_) { /* Status remains available in memory. */ }
}
function nativeRequestKey() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  if (!window.crypto?.getRandomValues) throw new Error("Не удалось подготовить безопасную оплату. Откройте MiniApp заново.");
  const bytes = window.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
function nativeEndpoint(type) { return type === "purchase" ? "/miniapp/api/referral/purchases" : "/miniapp/api/referral/withdrawals"; }
function nativeTerminal(operation) {
  return operation?.type === "purchase" ? ["paid", "cancelled"].includes(operation.receipt?.status) : ["completed", "rejected"].includes(operation?.receipt?.status);
}
function nativeBusy(type) { const operation = state.nativeOperations[type]; return Boolean(operation?.busy || operation?.checking); }
function moneyString(value) {
  const text = String(value ?? "").trim().replace(/[\s\u00a0]/g, "").replace(",", ".");
  if (!/^\d{1,10}(?:\.\d{1,2})?$/.test(text) || Number(text) > 1000000000) return null;
  return Number(text).toFixed(2);
}
function nativeBalance() { return Number(state.referral?.current_balance); }
function nativeBalanceError(amount, available = nativeBalance()) {
  const missing = Math.max(0, Number(amount) - Number(available));
  return `Не хватает ${rub(missing)}. Доступно ${rub(available)}.`;
}
function maskedReceiptDetails(receipt) {
  const value = String(receipt.phone || receipt.address || "");
  if (!value || /[*•…]/.test(value)) return value;
  return receipt.method === "sbp" ? `••• ${value.slice(-4)}` : `${value.slice(0, 5)}…${value.slice(-4)}`;
}
function operationNotice(operation) {
  if (!operation || nativeTerminal(operation)) return "";
  const title = operation.type === "purchase" ? "Проверка оплаты с баланса" : "Заявка на вывод";
  const note = operation.receipt?.status === "pending" ? operation.type === "purchase" ? "Оплата сохранена. Проверяем выдачу подписки." : "Заявка на рассмотрении. Выплата выполняется вручную." : "Проверьте сохранённую операцию перед повторной попыткой.";
  return `<div class="operation-note"><p class="title">${title}</p><p class="small">${note}</p><button class="button ghost" onclick="resumeNativeOperation('${operation.type}')">Посмотреть статус</button></div>`;
}
function acceptNativeReceipt(operation, receipt) {
  const statuses = operation.type === "purchase" ? ["pending", "paid", "cancelled"] : ["pending", "completed", "rejected"];
  if (!receipt || receipt.request_key !== operation.request_key || !statuses.includes(receipt.status) || moneyString(receipt.amount) === null) {
    throw new Error("Не удалось подтвердить результат. Проверьте статус операции.");
  }
  operation.receipt = receipt;
  operation.lookup = "ready";
  operation.error = null;
  if (nativeTerminal(operation)) clearNativeOperationStorage(operation.type);
  if (operation.type === "purchase" && receipt.status === "pending") startNativePolling(operation);
  if (operation.type === "purchase" && nativeTerminal(operation)) stopNativePolling();
}
function stopNativePolling() { if (state.nativePollTimer) clearInterval(state.nativePollTimer); state.nativePollTimer = null; }
function startNativePolling(operation) {
  if (state.nativePollTimer) return;
  operation.attempts = 0;
  state.nativePollTimer = setInterval(() => {
    if (state.nativeOperations.purchase !== operation || nativeTerminal(operation) || ++operation.attempts > 100) return stopNativePolling();
    lookupNativeOperation("purchase", { quiet: true });
  }, 3000);
}
async function restoreNativeOperations() {
  const id = nativeUserId();
  if (!id || state.nativeRestoreUser === id) return;
  if (state.nativeRestoreUser && state.nativeRestoreUser !== id) {
    state.wallet = null; state.withdrawal = null; state.refMode = "home";
  }
  state.nativeRestoreUser = id;
  stopNativePolling();
  state.nativeOperations = { purchase: null, withdrawal: null };
  for (const type of ["purchase", "withdrawal"]) {
    try {
      const stored = JSON.parse(localStorage.getItem(nativeStorageKey(type)) || "null");
      if (!stored || stored.type !== type || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(stored.request_key || "")) continue;
      state.nativeOperations[type] = { request_key: stored.request_key, type, lookup: "loading", receipt: null, busy: false };
      lookupNativeOperation(type, { quiet: true });
    } catch (_) { /* Missing or unavailable browser storage must not create an order. */ }
  }
}
async function lookupNativeOperation(type, { quiet = false } = {}) {
  const operation = state.nativeOperations[type];
  if (!operation || operation.busy || operation.checking) return false;
  operation.checking = true;
  if (!operation.receipt) operation.lookup = "loading";
  if (!quiet) renderReferral({ background: true });
  try {
    const receipt = await api(`${nativeEndpoint(type)}/${encodeURIComponent(operation.request_key)}`);
    if (state.nativeOperations[type] !== operation) return false;
    acceptNativeReceipt(operation, receipt);
    if (nativeTerminal(operation)) {
      refreshReferral();
      if (type === "purchase" && receipt.status === "paid") reloadData();
    }
    return true;
  } catch (error) {
    if (state.nativeOperations[type] !== operation) return false;
    operation.lookup = error.status === 404 ? "not_found" : "error";
    operation.error = error.status === 404 ? null : "Не удалось проверить результат. Новую операцию не создаём. Попробуйте проверить ещё раз.";
    return false;
  } finally {
    operation.checking = false;
    if (state.nativeOperations[type] === operation) renderReferral({ background: true });
  }
}
function ensureNativeOperation(type) {
  let operation = state.nativeOperations[type];
  if (!operation || nativeTerminal(operation)) {
    operation = { request_key: nativeRequestKey(), type, lookup: "not_found", receipt: null, busy: false };
    state.nativeOperations[type] = operation;
  }
  saveNativeOperation(operation);
  return operation;
}
function resumeNativeOperation(type) {
  state.refMode = type === "purchase" ? "wallet-status" : "withdraw-status";
  switchView("ref", { preserve: true });
  lookupNativeOperation(type);
}
function backToEarnings() { state.refMode = "home"; state.withdrawal = null; renderReferral(); window.scrollTo(0, 0); }
function nativeBackButton(action = "backToEarnings()") { return `<button class="button back-button ghost" onclick="${action}">← Назад</button>`; }
function nativeStatusHtml(type) {
  const operation = state.nativeOperations[type];
  const title = type === "purchase" ? "Оплата с баланса" : "Вывод денег";
  if (!operation) return `<div class="grid">${nativeBackButton()}${pageHeading(title)}<p class="muted">Сохранённой операции нет.</p></div>`;
  let content;
  if (operation.busy || operation.lookup === "loading") {
    content = loadingHtml(operation.busy ? type === "purchase" ? "Оформляем подписку…" : "Сохраняем заявку…" : "Проверяем статус…");
  } else if (!operation.receipt) {
    const notFound = operation.lookup === "not_found";
    content = `<div class="card grid" role="status"><h2 class="title">${notFound ? "Операция пока не найдена" : "Результат уточняется"}</h2><p class="muted">${notFound ? "Автоматически ничего не отправляем. Проверьте ещё раз или заполните форму повторно — сохранённый номер проверки останется тем же." : escapeHtml(operation.error || "Не удалось проверить результат. Новую операцию не создаём.")}</p><button class="button ghost" onclick="lookupNativeOperation('${type}')">Проверить статус</button>${notFound ? `<button class="button ghost" onclick="retryNativeForm('${type}')">Заполнить повторно</button>` : ""}</div>`;
  } else {
    const receipt = operation.receipt;
    if (type === "purchase") {
      const paid = receipt.status === "paid";
      content = `<div class="card grid" role="status"><h2 class="title">${paid ? "Подписка оплачена" : receipt.status === "cancelled" ? "Оплата отменена" : "Подписка оформляется"}</h2><p class="muted">${paid ? "Подписка появится в разделе «Подписки»." : receipt.status === "cancelled" ? "Эта операция завершена. Можно выбрать подписку заново." : "Сумма зарезервирована. Повторно оплачивать не нужно."}</p><div class="summary-line"><span>Сумма</span><b>${rub(receipt.amount)}</b></div>${paid ? `<button class="button accent" onclick="openKeysList()">Мои подписки</button>` : receipt.status === "cancelled" ? `<button class="button accent" onclick="openWalletPurchase()">Выбрать подписку</button>` : `<button class="button ghost" onclick="lookupNativeOperation('purchase')">Проверить статус</button>`}</div>`;
    } else {
      const label = { pending: "Заявка на рассмотрении", completed: "Выплачено", rejected: "Заявка отклонена" }[receipt.status];
      content = `<div class="card grid" role="status"><h2 class="title">${label}</h2><p class="receipt-number">${escapeHtml(receipt.number)}</p><p class="muted">${receipt.status === "pending" ? "Выплату отправим вручную после проверки заявки." : receipt.status === "rejected" ? "Резерв снят. Деньги снова доступны на балансе." : "Выплата отмечена как выполненная."}</p><div class="summary-line"><span>Сумма</span><b>${rub(receipt.amount)}</b></div><div class="summary-line"><span>Способ</span><b>${receipt.method === "sbp" ? "СБП" : "USDT"}</b></div>${receipt.bank ? `<p class="small">Банк: ${escapeHtml(receipt.bank)}</p>` : ""}${receipt.phone || receipt.address ? `<p class="small">${escapeHtml(maskedReceiptDetails(receipt))}</p>` : ""}${receipt.decision_note ? `<p class="muted">${escapeHtml(receipt.decision_note)}</p>` : ""}<button class="button ghost" onclick="lookupNativeOperation('withdrawal')">Обновить статус</button></div>`;
    }
  }
  return `<div class="grid">${nativeBackButton()}${pageHeading(title)}${content}</div>`;
}
function retryNativeForm(type) {
  const operation = state.nativeOperations[type];
  if (!operation || operation.lookup !== "not_found" || nativeBusy(type)) return;
  // A delayed original POST may still finish. Reuse its key, never mint another here.
  if (type === "purchase") {
    state.wallet = { step: "target", payment_target: null, subscription_id: null, plan: null, tariff_code: null, expected_amount: null, request_key: operation.request_key, error: null };
    state.refMode = "wallet";
  } else {
    state.withdrawal = { step: "amount", amount: "", method: "", bank: "", phone: "", address: "", request_key: operation.request_key, error: null };
    state.refMode = "withdraw";
  }
  renderReferral();
}

function openWalletPurchase() {
  const operation = state.nativeOperations.purchase;
  if (operation && !nativeTerminal(operation)) return resumeNativeOperation("purchase");
  state.wallet = { step: "target", payment_target: null, subscription_id: null, plan: null, tariff_code: null, expected_amount: null, error: null };
  state.refMode = "wallet";
  renderReferral(); window.scrollTo(0, 0);
}
function selectWalletTarget(target) {
  if (!state.wallet || !["new", "renew"].includes(target) || nativeBusy("purchase")) return;
  Object.assign(state.wallet, { payment_target: target, subscription_id: null, plan: null, tariff_code: null, error: null, step: target === "new" ? "plan" : "subs" });
  renderReferral();
}
function selectWalletSubscription(id) {
  const sub = state.subs.find(item => item.id === Number(id));
  if (!sub || !state.wallet || nativeBusy("purchase")) return;
  Object.assign(state.wallet, { payment_target: "renew", subscription_id: sub.id, plan: sub.plan_kind, step: "term", error: null });
  renderReferral();
}
function selectWalletPlan(plan) { if (!state.wallet || !["regular", "bypass"].includes(plan) || nativeBusy("purchase")) return; state.wallet.plan = plan; state.wallet.step = "term"; renderReferral(); }
function walletProduct() { return state.tariffs?.[state.wallet?.plan]?.find(item => item.code === state.wallet?.tariff_code); }
function reviewWalletTariff(code) {
  const product = state.tariffs?.[state.wallet?.plan]?.find(item => item.code === code);
  if (!product || !state.wallet || nativeBusy("purchase")) return;
  Object.assign(state.wallet, { tariff_code: code, expected_amount: moneyString(product.price), step: "review", error: null });
  renderReferral();
}
function backWallet() {
  const wallet = state.wallet;
  if (!wallet || nativeBusy("purchase")) return;
  if (wallet.step === "target") return backToEarnings();
  wallet.error = null;
  wallet.step = wallet.step === "review" ? "term" : wallet.step === "term" ? wallet.payment_target === "renew" ? "subs" : "plan" : "target";
  renderReferral();
}
function walletHtml() {
  const wallet = state.wallet;
  if (!wallet) return "";
  const unavailable = sectionState(wallet.step === "subs" ? "subs" : "tariffs");
  let body;
  if (unavailable) body = unavailable;
  else if (wallet.step === "target") body = `<div class="choice-list"><button class="choice-button" onclick="selectWalletTarget('new')">Купить новую подписку<span class="chevron">›</span></button><button class="choice-button" onclick="selectWalletTarget('renew')">Продлить подписку<span class="chevron">›</span></button></div>`;
  else if (wallet.step === "subs") body = state.subs.length ? `<p class="muted">Выберите подписку.</p><div class="choice-list">${state.subs.map(sub => `<button class="choice-button" data-action="wallet-renew" data-sub-id="${sub.id}"><span>${escapeHtml(subTitle(sub))}<small>${sub.status === "active" ? `До ${date(sub.subscription_until)}` : "Истекла"}</small></span><span class="chevron">›</span></button>`).join("")}</div>` : `<div class="card empty"><h2 class="title">Подписок пока нет</h2><button class="button accent" onclick="selectWalletTarget('new')">Купить новую подписку</button></div>`;
  else if (wallet.step === "plan") body = `<p class="muted">Выберите подписку.</p><div class="choice-list"><button class="choice-button" onclick="selectWalletPlan('regular')"${state.tariffs.regular.length ? "" : " disabled"}><span>Обычная<small>5 устройств · без лимита ГБ</small></span><span class="chevron">›</span></button><button class="choice-button" onclick="selectWalletPlan('bypass')"${state.tariffs.bypass.length ? "" : " disabled"}><span>С антиглушилкой<small>3 устройства · ${baseTrafficText()}</small></span><span class="chevron">›</span></button></div>`;
  else if (wallet.step === "term") body = `<p class="muted">Выберите срок.</p><div class="choice-list">${(state.tariffs[wallet.plan] || []).map(product => `<button class="choice-button" data-action="wallet-tariff" data-code="${escapeHtml(product.code)}"><span>${tariffPeriod(product)}</span>${priceHtml(product)}</button>`).join("")}</div>`;
  else {
    const product = walletProduct();
    const sub = state.subs.find(item => item.id === wallet.subscription_id);
    body = product ? `<div class="card checkout-summary"><p class="title">${wallet.payment_target === "renew" ? "Продление" : "Новая подписка"}</p>${sub ? `<p class="small">${escapeHtml(subTitle(sub))}</p>` : ""}<p class="muted">${wallet.plan === "bypass" ? "С антиглушилкой" : "Обычная"} · ${tariffPeriod(product)}</p><div class="summary-line"><span>К оплате</span><b>${rub(wallet.expected_amount)}</b></div><div class="summary-line"><span>Ваш баланс</span><b>${rub(nativeBalance())}</b></div></div>${wallet.error ? `<p class="form-error" role="status">${escapeHtml(wallet.error)}</p>` : ""}<button class="button accent" onclick="submitWalletPurchase()"${nativeBusy("purchase") ? " disabled" : ""}>Оплатить с баланса</button>` : `<p class="muted">Подписка недоступна. Вернитесь к выбору срока.</p>`;
  }
  return `<div class="grid">${nativeBackButton("backWallet()")}${pageHeading("Оплатить подписку")}${body}</div>`;
}
async function submitWalletPurchase() {
  const wallet = state.wallet;
  if (state.currentView !== "ref" || state.refMode !== "wallet" || !wallet || wallet.step !== "review" || !walletProduct() || nativeBusy("purchase") || !wallet.expected_amount) return;
  const previous = state.nativeOperations.purchase;
  if (wallet.request_key && previous?.request_key === wallet.request_key && previous.receipt) return resumeNativeOperation("purchase");
  if (Number(wallet.expected_amount) > nativeBalance()) { wallet.error = nativeBalanceError(wallet.expected_amount); showToast(wallet.error); renderReferral(); return; }
  let operation;
  try { operation = ensureNativeOperation("purchase"); } catch (error) { showToast(error.message); return; }
  wallet.request_key = operation.request_key;
  const payload = { request_key: operation.request_key, tariff_code: wallet.tariff_code, payment_target: wallet.payment_target, subscription_id: wallet.subscription_id, expected_amount: wallet.expected_amount };
  await submitNativeOperation(operation, payload);
}
async function refreshNativeCatalog() {
  try {
    const catalog = await api("/miniapp/api/tariffs");
    if (!Array.isArray(catalog.regular) || !Array.isArray(catalog.bypass)) throw new Error();
    state.tariffs = catalog; state.dataStatus.tariffs = "ready";
  } catch (_) { state.dataStatus.tariffs = "error"; }
}

function openNativeWithdrawal() {
  const operation = state.nativeOperations.withdrawal;
  if (operation && !nativeTerminal(operation)) return resumeNativeOperation("withdrawal");
  state.withdrawal = { step: "amount", amount: "", method: "", bank: "", phone: "", address: "", error: null };
  state.refMode = "withdraw"; renderReferral(); window.scrollTo(0, 0);
}
function selectWithdrawalMethod(method) {
  if (!state.withdrawal || !["sbp", "usdt"].includes(method) || nativeBusy("withdrawal")) return;
  Object.assign(state.withdrawal, { method, step: "details", error: null, bank: "", phone: "", address: "" }); renderReferral();
}
function backWithdrawal() {
  const withdrawal = state.withdrawal;
  if (!withdrawal || nativeBusy("withdrawal")) return;
  if (withdrawal.step === "amount") return backToEarnings();
  withdrawal.error = null;
  withdrawal.step = { method: "amount", details: "method", review: "details" }[withdrawal.step] || "amount";
  renderReferral();
}
function advanceWithdrawal(form) {
  const withdrawal = state.withdrawal;
  if (!withdrawal || nativeBusy("withdrawal")) return;
  const data = new FormData(form);
  withdrawal.error = null;
  if (form.dataset.nativeForm === "withdraw-amount") {
    withdrawal.amount = String(data.get("amount") || "").trim();
    const amount = moneyString(withdrawal.amount);
    const minimum = Number(state.referral?.minimum_withdrawal || 1500);
    if (!amount || Number(amount) < minimum) withdrawal.error = `Введите сумму от ${rub(minimum)}, не больше двух знаков после запятой.`;
    else if (Number(amount) > nativeBalance()) withdrawal.error = nativeBalanceError(amount);
    else Object.assign(withdrawal, { amount, step: "method" });
  } else if (form.dataset.nativeForm === "withdraw-details") {
    const bank = String(data.get("bank") || "").trim(), phone = String(data.get("phone") || "").replace(/[\s()\-]/g, ""), address = String(data.get("address") || "").trim();
    Object.assign(withdrawal, { bank, phone, address });
    if (withdrawal.method === "sbp" && (bank.length < 2 || bank.length > 120)) withdrawal.error = "Укажите название банка.";
    else if (withdrawal.method === "sbp" && !/^\+[1-9]\d{9,14}$/.test(phone)) withdrawal.error = "Введите телефон с кодом страны, например +79991234567.";
    else if (withdrawal.method === "usdt" && !/^(?:T[1-9A-HJ-NP-Za-km-z]{33}|0x[0-9a-fA-F]{40})$/.test(address)) withdrawal.error = "Укажите адрес USDT в сети TRC-20 или ERC-20.";
    else Object.assign(withdrawal, { bank: withdrawal.method === "sbp" ? bank : "", phone: withdrawal.method === "sbp" ? phone : "", address: withdrawal.method === "usdt" ? address : "", step: "review" });
  }
  renderReferral();
}
function withdrawalHtml() {
  const withdrawal = state.withdrawal;
  if (!withdrawal) return "";
  const error = withdrawal.error ? `<p class="form-error" role="status">${escapeHtml(withdrawal.error)}</p>` : "";
  let body;
  if (withdrawal.step === "amount") body = `<p class="muted">Доступно ${rub(nativeBalance())}. Минимальный вывод — ${rub(state.referral.minimum_withdrawal || 1500)}.</p><form class="stack" data-native-form="withdraw-amount"><label class="field">Сумма, ₽<input name="amount" inputmode="decimal" autocomplete="off" maxlength="13" value="${escapeHtml(withdrawal.amount)}" placeholder="1500" required></label>${error}<button class="button accent" type="submit">Продолжить</button></form>`;
  else if (withdrawal.step === "method") body = `<p class="muted">К выводу ${rub(withdrawal.amount)}. Выберите способ.</p><div class="choice-list"><button class="choice-button" onclick="selectWithdrawalMethod('sbp')">СБП<span class="chevron">›</span></button><button class="choice-button" onclick="selectWithdrawalMethod('usdt')">USDT<span class="chevron">›</span></button></div>`;
  else if (withdrawal.step === "details") body = `<form class="stack" data-native-form="withdraw-details">${withdrawal.method === "sbp" ? `<label class="field">Банк<input name="bank" autocomplete="off" maxlength="120" value="${escapeHtml(withdrawal.bank)}" placeholder="Название банка" required></label><label class="field">Номер телефона<input name="phone" type="tel" autocomplete="off" maxlength="24" value="${escapeHtml(withdrawal.phone)}" placeholder="+79991234567" required></label>` : `<label class="field">Адрес USDT<input name="address" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="42" value="${escapeHtml(withdrawal.address)}" required></label><p class="small">TRC-20 (T…) или ERC-20 (0x…). Проверьте сеть и адрес.</p>`}${error}<button class="button accent" type="submit">Продолжить</button></form>`;
  else body = `<div class="card checkout-summary"><p class="title">Проверьте заявку</p><div class="summary-line"><span>Сумма</span><b>${rub(withdrawal.amount)}</b></div><div class="summary-line"><span>Способ</span><b>${withdrawal.method === "sbp" ? "СБП" : "USDT"}</b></div>${withdrawal.method === "sbp" ? `<div class="summary-line"><span>Банк</span><b>${escapeHtml(withdrawal.bank)}</b></div><div class="summary-line"><span>Телефон</span><b>${escapeHtml(withdrawal.phone)}</b></div>` : `<p class="keybox">${escapeHtml(withdrawal.address)}</p><p class="small">Сеть, курс и сумму USDT согласуем перед переводом.</p>`}<p class="small">Выплата — вручную после проверки заявки.</p></div>${error}<button class="button accent" onclick="submitNativeWithdrawal()"${nativeBusy("withdrawal") ? " disabled" : ""}>Подать заявку</button>`;
  return `<div class="grid">${nativeBackButton("backWithdrawal()")}${pageHeading("Вывести деньги")}${body}</div>`;
}
async function submitNativeWithdrawal() {
  const withdrawal = state.withdrawal;
  if (state.currentView !== "ref" || state.refMode !== "withdraw" || !withdrawal || withdrawal.step !== "review" || nativeBusy("withdrawal")) return;
  const previous = state.nativeOperations.withdrawal;
  if (withdrawal.request_key && previous?.request_key === withdrawal.request_key && previous.receipt) return resumeNativeOperation("withdrawal");
  if (Number(withdrawal.amount) > nativeBalance()) { withdrawal.error = nativeBalanceError(withdrawal.amount); renderReferral(); return; }
  let operation;
  try { operation = ensureNativeOperation("withdrawal"); } catch (error) { showToast(error.message); return; }
  withdrawal.request_key = operation.request_key;
  const payload = { request_key: operation.request_key, amount: withdrawal.amount, method: withdrawal.method, bank: withdrawal.bank, phone: withdrawal.phone, address: withdrawal.address };
  await submitNativeOperation(operation, payload);
}
async function submitNativeOperation(operation, payload) {
  if (operation.busy || operation.checking || operation.receipt || !["not_found"].includes(operation.lookup)) return;
  const type = operation.type;
  operation.busy = true;
  operation.error = null;
  const currentMode = type === "purchase" ? "wallet" : "withdraw";
  if (state.refMode === currentMode) state.refMode = type === "purchase" ? "wallet-status" : "withdraw-status";
  renderReferral();
  try {
    const receipt = await api(nativeEndpoint(type), { method: "POST", body: JSON.stringify(payload) });
    if (state.nativeOperations[type] !== operation) return;
    acceptNativeReceipt(operation, receipt);
    if (type === "withdrawal") state.withdrawal = null; // Do not retain full requisites after acceptance.
    refreshReferral();
    if (type === "purchase" && receipt.status === "paid") reloadData();
  } catch (error) {
    if (state.nativeOperations[type] !== operation) return;
    if (error.code === "price_changed" && type === "purchase") {
      await refreshNativeCatalog();
      const product = walletProduct();
      if (state.wallet) {
        state.wallet.expected_amount = product ? moneyString(product.price) : null;
        state.wallet.error = "Цена изменилась. Проверьте новую сумму и подтвердите оплату ещё раз.";
        state.wallet.step = product ? "review" : "term";
      }
      operation.lookup = "not_found";
      if (state.currentView === "ref" && state.refMode === "wallet-status") state.refMode = "wallet";
    } else if (["insufficient_balance", "invalid_request", "invalid_amount", "invalid_details"].includes(error.code)) {
      operation.lookup = "not_found";
      if (error.code === "insufficient_balance" && state.referral && error.detail?.available != null && Number.isFinite(Number(error.detail.available))) {
        state.referral.current_balance = Number(error.detail.available);
      }
      const message = error.code === "insufficient_balance" ? nativeBalanceError(error.detail?.amount ?? payload.expected_amount ?? payload.amount, error.detail?.available ?? nativeBalance()) : error.message;
      if (type === "purchase" && state.wallet) state.wallet.error = message;
      if (type === "withdrawal" && state.withdrawal) state.withdrawal.error = message;
      if (state.currentView === "ref" && state.refMode === `${type === "purchase" ? "wallet" : "withdraw"}-status`) state.refMode = currentMode;
      showToast(message);
    } else {
      operation.lookup = "error";
      operation.error = "Ответ не получен. Операция могла сохраниться. Проверьте статус — повторно ничего не отправляем.";
    }
  } finally {
    operation.busy = false;
    if (state.nativeOperations[type] === operation) {
      const relevant = state.currentView === "ref" && [currentMode, `${currentMode}-status`].includes(state.refMode);
      renderReferral({ background: !relevant });
    }
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
  renderReferral({ background: true });
  try {
    const data = validReferral(await api("/miniapp/api/referral" + (recordView ? "?view=1" : "")));
    if (version !== state.refRequestVersion) return;
    state.referral = data;
    state.dataStatus.ref = "ready";
  }
  catch (_) { if (version === state.refRequestVersion) state.dataStatus.ref = "error"; }
  finally { if (version === state.refRequestVersion) renderReferral({ background: true }); }
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
function renderReferral({ background = false } = {}) {
  const r = state.referral;
  if (background && state.authStatus === "ready" && state.currentView === "ref" && ["wallet", "withdraw"].includes(state.refMode)) return;
  if (state.authStatus !== "ready") { el("view-ref").innerHTML = pageHeading("Заработать") + sectionState("ref"); return; }
  if (state.refMode === "wallet-status") { el("view-ref").innerHTML = nativeStatusHtml("purchase"); return; }
  if (state.refMode === "withdraw-status") { el("view-ref").innerHTML = nativeStatusHtml("withdrawal"); return; }
  if (r && r.current_balance != null && Number.isFinite(Number(r.current_balance))) {
    if (state.refMode === "wallet") { el("view-ref").innerHTML = walletHtml(); return; }
    if (state.refMode === "withdraw") { el("view-ref").innerHTML = withdrawalHtml(); return; }
  }
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
    '<div class="stack"><button class="button ghost" onclick="openWalletPurchase()">Потратить на подписку</button><button class="button ghost" onclick="openNativeWithdrawal()">Вывести</button></div>' +
    operationNotice(state.nativeOperations.purchase) + operationNotice(state.nativeOperations.withdrawal) +
    '<p class="small">' + (remaining ? 'До вывода осталось ' + rub(remaining) : 'Можно оформить вывод') + '. Вывод через СБП / USDT от ' + rub(minimum) + ' после проверки заявки.</p>' +
    disclosure('ref-stats', 'Статистика', stats) + disclosure('ref-rules', 'Как это работает', rules) +
    disclosure('ref-history', 'История', '<div id="referral-history">' + (history || '<p class="muted">Операций пока нет.</p>') + '<p class="small">Полная история доступна в боте.</p><button class="button ghost" onclick="openEarningBot(\'home\')">Открыть историю в боте</button></div>') +
    '<button class="button ghost" onclick="refreshReferral()">Обновить</button></div>';
}

function renderHelp() {
  el("view-help").innerHTML = `<div class="grid">
    ${pageHeading("Помощь")}
    <div class="card"><h2 class="title">Как подключиться</h2><ol><li>Откройте «Подписки» и выберите подписку.</li><li>Нажмите «Открыть в Happ».</li></ol></div>
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
