const state = {
  catalog: { regular: [], bypass: [], traffic_packages: [] },
  account: null,
  subscriptions: [],
  accountKind: "regular",
  accountSection: "overview",
  renewSubscription: null,
  checkout: null,
};

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[ch]));
const multiline = (value) => esc(value).replace(/\n/g, "<br>");
const rubles = (value) => `${Number(value || 0).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ₽`;
const formatDate = (value, time = false) => value ? new Date(value).toLocaleString("ru-RU", time ? { dateStyle: "medium", timeStyle: "short" } : { dateStyle: "long" }) : "—";
const happBridgeLink = (url) => `${window.location.origin}/open-happ?url=${encodeURIComponent(url)}`;
const deviceWord = (count) => count === 1 ? "устройство" : [2, 3, 4].includes(Number(count)) ? "устройства" : "устройств";
const deviceLimitText = (subscription) => {
  const limit = Number(subscription.devices?.limit || (subscription.plan_kind === "bypass" ? 3 : 5));
  return `${limit} ${deviceWord(limit)}`;
};
const TRACKING_STORAGE_KEY = "wayspnTrackingCode";
const TRACKING_CLIENT_KEY = "wayspnTrackingClientId";

function captureTrackingCode() {
  const params = new URLSearchParams(location.search);
  const code = (params.get("t") || params.get("ref") || params.get("utm_campaign") || "").trim().toLowerCase();
  if (!/^[a-z0-9_-]{3,64}$/.test(code)) return;
  localStorage.setItem(TRACKING_STORAGE_KEY, code);
  let clientId = localStorage.getItem(TRACKING_CLIENT_KEY);
  if (!clientId) {
    clientId = window.crypto?.randomUUID ? window.crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    localStorage.setItem(TRACKING_CLIENT_KEY, clientId);
  }
  api("/track", { method: "POST", body: JSON.stringify({ code, client_id: clientId }) }).catch(() => {});
}

async function api(path, options = {}) {
  const response = await fetch(`/site/api${path}`, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith("/auth/") && path !== "/me") {
      state.account = null;
      showAuth("login", true);
    }
    throw new Error(data.detail || "Не удалось выполнить запрос");
  }
  return data;
}

function toast(message, bad = false) {
  const node = $("toast");
  node.textContent = message;
  node.className = `toast show${bad ? " bad" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.className = "toast", 3000);
}

function setRoute(path, replace = false) {
  if (location.pathname !== path) history[replace ? "replaceState" : "pushState"]({}, "", path);
}

function showAuth(mode = "login", push = true) {
  $("authView").classList.remove("hidden");
  $("accountView").classList.add("hidden");
  $("loginPanel").classList.toggle("hidden", mode !== "login");
  $("registerPanel").classList.toggle("hidden", mode !== "register");
  $("authGuideTitle").textContent = mode === "register" ? "Создайте аккаунт" : "Войдите в аккаунт";
  $("authGuideDescription").textContent = mode === "register"
    ? "Придумайте данные для входа. После регистрации сразу откроется выбор подписки."
    : "Введите логин и пароль. Все ваши подписки и покупки уже сохранены в кабинете.";
  if (push) setRoute(mode === "register" ? "/" : "/login");
}

async function showAccount(section = "overview", push = true) {
  if (!state.account) { showAuth("login", true); return; }
  $("authView").classList.add("hidden");
  $("accountView").classList.remove("hidden");
  if (push) setRoute("/account");
  switchAccountSection(section);
  $("profileLogin").textContent = state.account.login;
  $("profileLetter").textContent = state.account.login.slice(0, 1).toUpperCase();
  $("accountGreeting").textContent = `Здравствуйте, ${state.account.login}`;
  window.scrollTo(0, 0);
  await loadAccountData();
}

function priceMarkup(item) {
  const discounted = Number(item.original_price) > Number(item.price);
  return `<div class="plan-price"><strong>${rubles(item.price)}</strong>${discounted ? `<span class="old-price">${rubles(item.original_price)}</span>` : ""}</div>${discounted ? `<div class="discount-label">Экономия ${rubles(Number(item.original_price) - Number(item.price))}</div>` : ""}`;
}

function planCard(item) {
  const bypass = item.kind === "bypass";
  return `<article class="plan-card ${item.days >= 90 ? "featured" : ""}">
    ${item.days >= 90 ? `<span class="tag">Выгоднее</span>` : ""}
    <h3>${item.days} дней</h3><p class="plan-subtitle">${bypass ? "С антиглушилкой" : "Обычная подписка"}</p>
    ${priceMarkup(item)}
    <ul class="plan-features"><li>${bypass ? "150 ГБ включено" : "Без лимита трафика"}</li><li>${bypass ? "До 3 устройств" : "До 5 устройств"}</li></ul>
    <button class="button ${item.days >= 90 ? "primary" : "glass"}" data-plan="${esc(item.code)}">Купить подписку</button>
  </article>`;
}

function renderAccountPlans() {
  const plans = state.catalog[state.accountKind] || [];
  $("accountPlans").innerHTML = plans.map((item) => planCard(item)).join("");
}

function subscriptionName(subscription) {
  return `${subscription.plan_kind === "bypass" ? "С антиглушилкой" : "Обычная"} #${subscription.type_index}`;
}

function devicePackageButton(subscription, pkg) {
  const discount = pkg.discount_percent ? `<small>Скидка ${pkg.discount_percent}% · до ${formatDate(subscription.subscription_until)}</small>` : `<small>До ${formatDate(subscription.subscription_until)}</small>`;
  return `<button class="button glass" data-buy-devices="${subscription.id}" data-device-count="${pkg.count}">+${pkg.count} ${deviceWord(pkg.count)} · ${rubles(pkg.price)}${discount}</button>`;
}

function renderSubscriptions() {
  const container = $("subscriptionsGrid");
  container.classList.remove("skeleton-grid");
  if (!state.subscriptions.length) {
    container.innerHTML = `<div class="empty-card"><h3>Подписок пока нет</h3><p>Нажмите «Купить подписку» в верхней панели.</p></div>`;
    return;
  }
  container.innerHTML = state.subscriptions.map((sub) => {
    const active = sub.status === "active";
    const used = Number(sub.traffic?.used_gb || 0), limit = Number(sub.traffic?.limit_gb || 0);
    const percent = limit ? Math.min(100, Math.round(used / limit * 100)) : 0;
    const devicePackages = sub.devices?.packages || [];
    return `<article class="subscription-card">
      <div class="sub-head"><div><p class="eyebrow">Подписка</p><h3>${esc(subscriptionName(sub))}</h3></div><span class="badge ${active ? "" : "expired"}">${active ? "Активна" : "Истекла"}</span></div>
      <div class="sub-date"><small>Действует до</small><strong>${formatDate(sub.subscription_until)}</strong></div>
      <div class="sub-date"><small>Устройства</small><strong>до ${deviceLimitText(sub)}</strong></div>
      ${sub.traffic?.enabled ? `<div class="traffic-bar"><div class="traffic-meta"><span>Трафик</span><span>${used} из ${limit} ГБ</span></div><div class="bar"><i style="width:${percent}%"></i></div></div>` : ""}
      ${sub.subscription_url ? `<div class="key-box"><input readonly value="${esc(sub.subscription_url)}" /><button data-copy-key="${esc(sub.subscription_url)}">Копировать</button></div>` : `<p class="muted">Ключ появится после активации платежа.</p>`}
      <div class="sub-actions">${sub.subscription_url ? `<button class="button primary" data-connect="${esc(sub.subscription_url)}">Подключить</button>` : ""}<button class="button glass" data-renew="${sub.id}">Продлить</button>${sub.traffic?.enabled && active ? `<button class="button glass" data-buy-traffic="${sub.id}">Купить ГБ</button>` : ""}<button class="button glass danger" data-delete-subscription="${sub.id}">Удалить</button></div>
      ${active && devicePackages.length ? `<div class="device-addon-box"><small>Докупить устройства до конца текущего периода</small><div class="sub-actions">${devicePackages.map((pkg) => devicePackageButton(sub, pkg)).join("")}</div></div>` : ""}
      ${sub.subscription_url ? `<button class="instruction-link" data-connection-help>Как подключить на телефон или компьютер?</button>` : ""}
    </article>`;
  }).join("");
}

function renderTraffic() {
  const activeBypass = state.subscriptions.filter((sub) => sub.plan_kind === "bypass" && sub.status === "active");
  $("trafficSubscription").innerHTML = activeBypass.length ? activeBypass.map((sub) => `<option value="${sub.id}">${esc(subscriptionName(sub))} · до ${formatDate(sub.subscription_until)}</option>`).join("") : `<option value="">Нет активной подписки</option>`;
  $("trafficPlans").innerHTML = state.catalog.traffic_packages.map((item) => `<article class="traffic-card"><strong>${item.gb} ГБ</strong><div class="traffic-price">${priceMarkup(item)}</div><button class="button primary" data-traffic-plan="${esc(item.code)}" ${activeBypass.length ? "" : "disabled"}>Купить</button></article>`).join("");
}

function paymentTitle(payment) {
  if (payment.payment_kind === "traffic_package") return `Пакет ${payment.traffic_package_code?.replace("gb_", "") || ""} ГБ`;
  if (payment.payment_kind === "device_addon") return `+${payment.target_slot_number || ""} ${deviceWord(payment.target_slot_number || 0)}`;
  const tariff = [...state.catalog.regular, ...state.catalog.bypass].find((item) => item.code === payment.tariff_code);
  return `${payment.payment_target === "renew" ? "Продление" : "Подписка"} · ${tariff?.days || "—"} дней`;
}

async function loadPayments() {
  try {
    const data = await api("/payments");
    $("paymentsTable").innerHTML = data.payments.length ? data.payments.map((payment) => {
      const status = ({ paid: "Оплачен", pending: "Ожидает", canceled: "Отменён" })[payment.status] || payment.status;
      return `<div class="payment-row"><div><b>${esc(paymentTitle(payment))}</b><small>${formatDate(payment.created_at, true)}</small></div><span>ЮKassa</span><strong>${rubles(payment.amount)}</strong><span class="badge payment-status ${payment.status === "paid" ? "" : "expired"}">${esc(status)}</span></div>`;
    }).join("") : `<div class="empty-card">Покупок пока нет</div>`;
  } catch (error) { toast(error.message, true); }
}

async function loadAccountData() {
  try {
    const data = await api("/subscriptions");
    state.subscriptions = data.subscriptions;
    renderSubscriptions();
    renderTraffic();
    renderAccountPlans();
    if (!state.subscriptions.length && state.accountSection === "overview") {
      state.renewSubscription = null;
      switchAccountSection("plans");
    }
    if (state.accountSection === "history") loadPayments();
  } catch (error) { toast(error.message, true); }
}

function switchAccountSection(section) {
  state.accountSection = section;
  document.querySelectorAll(".account-section").forEach((node) => node.classList.toggle("active", node.id === `account-${section}`));
  document.querySelectorAll("[data-account-section]").forEach((node) => node.classList.toggle("active", node.dataset.accountSection === section));
  $("accountView").querySelector(".account-sidebar").classList.remove("open");
  const navigationButton = $("sectionNavigationButton");
  const isHome = section === "overview";
  navigationButton.textContent = isHome ? "Купить подписку" : "← В главное меню";
  navigationButton.className = isHome ? "button primary section-navigation" : "section-navigation back-home";
  if (section === "history" && state.account) loadPayments();
  if (section === "plans") renderAccountPlans();
}

function findPlan(code) {
  return [...state.catalog.regular, ...state.catalog.bypass].find((item) => item.code === code);
}

function openRenewDialog(subscription) {
  state.renewSubscription = subscription;
  const plans = state.catalog[subscription.plan_kind] || [];
  $("renewTitle").textContent = `Продлить «${subscriptionName(subscription)}»`;
  $("renewPlans").innerHTML = plans.map((plan) => `
    <button class="renew-option" data-renew-plan="${esc(plan.code)}">
      <span><b>${plan.days} дней</b><small>${subscription.plan_kind === "bypass" ? "С антиглушилкой" : "Обычная подписка"}</small></span>
      <strong>${rubles(plan.price)}</strong>
    </button>
  `).join("");
  $("renewDialog").showModal();
}

function openCheckout(data) {
  state.checkout = data;
  if (data.type === "subscription") {
    const plan = findPlan(data.code);
    $("checkoutTitle").textContent = data.target === "renew" ? "Продление подписки" : "Новая подписка";
    $("checkoutDetails").innerHTML = `<div class="checkout-summary"><div><span>Тариф</span><strong>${plan.kind === "bypass" ? "С антиглушилкой" : "Обычный"}</strong></div><div><span>Срок</span><strong>${plan.days} дней</strong></div><div><span>К оплате</span><strong>${rubles(plan.price)}</strong></div></div>`;
  } else if (data.type === "traffic") {
    const item = state.catalog.traffic_packages.find((entry) => entry.code === data.code);
    $("checkoutTitle").textContent = `Пакет ${item.gb} ГБ`;
    $("checkoutDetails").innerHTML = `<div class="checkout-summary"><div><span>Подписка</span><strong>#${data.subscriptionId}</strong></div><div><span>Трафик</span><strong>${item.gb} ГБ</strong></div><div><span>К оплате</span><strong>${rubles(item.price)}</strong></div></div>`;
  } else {
    const subscription = state.subscriptions.find((entry) => entry.id === Number(data.subscriptionId));
    const pkg = (subscription?.devices?.packages || []).find((entry) => Number(entry.count) === Number(data.deviceCount));
    $("checkoutTitle").textContent = "Докупить устройства";
    $("checkoutDetails").innerHTML = `<div class="checkout-summary"><div><span>Подписка</span><strong>${esc(subscription ? subscriptionName(subscription) : `#${data.subscriptionId}`)}</strong></div><div><span>Добавить</span><strong>+${data.deviceCount} ${deviceWord(data.deviceCount)}</strong></div><div><span>Действует до</span><strong>${formatDate(subscription?.subscription_until)}</strong></div><div><span>К оплате</span><strong>${rubles(pkg?.price || 0)}</strong></div></div>`;
  }
  $("checkoutDialog").showModal();
}

async function confirmCheckout() {
  const button = $("confirmCheckout");
  button.disabled = true;
  button.textContent = "Создаём платёж…";
  try {
    let result;
    if (state.checkout.type === "subscription") {
      result = await api("/payments/subscription", { method: "POST", body: JSON.stringify({ tariff_code: state.checkout.code, payment_target: state.checkout.target, subscription_id: state.checkout.subscriptionId || null }) });
    } else if (state.checkout.type === "traffic") {
      result = await api("/payments/traffic", { method: "POST", body: JSON.stringify({ package_code: state.checkout.code, subscription_id: state.checkout.subscriptionId }) });
    } else {
      result = await api("/payments/devices", { method: "POST", body: JSON.stringify({ subscription_id: state.checkout.subscriptionId, device_count: state.checkout.deviceCount }) });
    }
    localStorage.setItem("spnPendingPayment", result.invoice_id);
    location.href = result.pay_url;
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
    button.textContent = "Перейти к оплате";
  }
}

async function pollPendingPayment() {
  const invoiceId = localStorage.getItem("spnPendingPayment");
  if (!invoiceId || !state.account) return;
  const banner = $("paymentBanner");
  banner.className = "payment-banner";
  banner.innerHTML = `<span class="loader"></span><div><b>Проверяем оплату</b><p>Это обычно занимает несколько секунд.</p></div>`;
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const result = await api(`/payments/${encodeURIComponent(invoiceId)}`);
      if (result.status === "paid") {
        localStorage.removeItem("spnPendingPayment");
        const summary = result.summary || { title: "Оплата прошла", message: "Покупка активирована и уже отображается в кабинете." };
        banner.className = "payment-banner success";
        banner.innerHTML = `<div><b>✓ ${esc(summary.title)}</b><p>${multiline(summary.message || "Покупка активирована.")}</p></div>`;
        switchAccountSection("overview");
        await loadAccountData();
        banner.scrollIntoView({ behavior: "smooth", block: "center" });
        return;
      }
      if (result.status === "canceled") {
        localStorage.removeItem("spnPendingPayment");
        banner.className = "payment-banner failed";
        banner.innerHTML = `<div><b>Платёж отменён</b><p>Попробуйте создать новый платёж.</p></div>`;
        return;
      }
    } catch (error) {
      if (attempt > 2) { toast(error.message, true); return; }
    }
    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
  banner.innerHTML = `<div><b>Платёж ещё обрабатывается</b><p>Обновите страницу через несколько минут.</p></div>`;
}

async function submitAuth(form, type) {
  const data = Object.fromEntries(new FormData(form));
  if (type === "register") data.terms_accepted = form.elements.terms_accepted.checked;
  if (type === "register") {
    const trackingCode = localStorage.getItem(TRACKING_STORAGE_KEY);
    if (trackingCode) data.tracking_code = trackingCode;
  }
  const errorNode = form.querySelector("[data-error]");
  const button = form.querySelector("button[type=submit]");
  const originalButtonText = button.textContent;
  errorNode.textContent = "";
  if (type === "register" && data.password !== data.password_confirmation) {
    errorNode.textContent = "Пароли не совпадают";
    form.elements.password_confirmation.focus();
    return;
  }
  button.disabled = true;
  button.textContent = type === "register" ? "Создаём аккаунт…" : "Входим…";
  try {
    await api(`/auth/${type}`, { method: "POST", body: JSON.stringify(data) });
    if (type === "register") localStorage.removeItem(TRACKING_STORAGE_KEY);
    state.account = await api("/me");
    form.reset();
    await showAccount(type === "register" ? "plans" : "overview", true);
    toast(type === "register" ? "Аккаунт создан" : "Вы вошли в аккаунт");
  } catch (error) {
    errorNode.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = originalButtonText;
  }
}

document.querySelectorAll("[data-password-toggle]").forEach((button) => button.addEventListener("click", () => {
  const input = button.parentElement.querySelector("input");
  input.type = input.type === "password" ? "text" : "password";
  button.textContent = input.type === "password" ? "Показать" : "Скрыть";
}));
$("toRegister").addEventListener("click", () => showAuth("register"));
$("toLogin").addEventListener("click", () => showAuth("login"));
$("loginForm").addEventListener("submit", (event) => { event.preventDefault(); submitAuth(event.currentTarget, "login"); });
$("registerForm").addEventListener("submit", (event) => { event.preventDefault(); submitAuth(event.currentTarget, "register"); });
$("logoutButton").addEventListener("click", async () => { await api("/auth/logout", { method: "POST" }).catch(() => {}); localStorage.removeItem("spnPendingPayment"); state.account = null; state.subscriptions = []; showAuth("login"); });
$("mobileMenu").addEventListener("click", () => $("accountView").querySelector(".account-sidebar").classList.toggle("open"));
document.querySelectorAll("[data-account-kind]").forEach((button) => button.addEventListener("click", () => { state.accountKind = button.dataset.accountKind; document.querySelectorAll("[data-account-kind]").forEach((node) => node.classList.toggle("active", node === button)); renderAccountPlans(); }));
document.querySelectorAll("[data-account-section]").forEach((button) => button.addEventListener("click", () => { state.renewSubscription = null; switchAccountSection(button.dataset.accountSection); }));
$("closeCheckout").addEventListener("click", () => $("checkoutDialog").close());
$("confirmCheckout").addEventListener("click", confirmCheckout);
$("closeRenew").addEventListener("click", () => { $("renewDialog").close(); state.renewSubscription = null; });
$("closeConnection").addEventListener("click", () => $("connectionDialog").close());
$("understoodConnection").addEventListener("click", () => $("connectionDialog").close());
$("sectionNavigationButton").addEventListener("click", () => {
  state.renewSubscription = null;
  switchAccountSection(state.accountSection === "overview" ? "plans" : "overview");
});

const registerPassword = $("registerForm").elements.password;
const registerConfirmation = $("registerForm").elements.password_confirmation;
function validatePasswordConfirmation() {
  registerConfirmation.setCustomValidity(
    registerConfirmation.value && registerConfirmation.value !== registerPassword.value ? "Пароли не совпадают" : ""
  );
}
registerPassword.addEventListener("input", validatePasswordConfirmation);
registerConfirmation.addEventListener("input", validatePasswordConfirmation);

document.addEventListener("click", async (event) => {
  const planButton = event.target.closest("[data-plan]");
  const openPlans = event.target.closest("[data-open-plans]");
  const renew = event.target.closest("[data-renew]");
  const deleteSubscriptionButton = event.target.closest("[data-delete-subscription]");
  const buyTraffic = event.target.closest("[data-buy-traffic]");
  const buyDevices = event.target.closest("[data-buy-devices]");
  const trafficPlan = event.target.closest("[data-traffic-plan]");
  const copy = event.target.closest("[data-copy-key]");
  const connect = event.target.closest("[data-connect]");
  const connectionHelp = event.target.closest("[data-connection-help]");
  const renewPlan = event.target.closest("[data-renew-plan]");
  if (openPlans) { state.renewSubscription = null; switchAccountSection("plans"); }
  if (planButton) {
    if (!state.account) { showAuth("register"); return; }
    const plan = findPlan(planButton.dataset.plan);
    openCheckout({ type: "subscription", code: plan.code, target: "new", subscriptionId: null });
  }
  if (renew) {
    const subscription = state.subscriptions.find((item) => item.id === Number(renew.dataset.renew));
    if (subscription) openRenewDialog(subscription);
  }
  if (deleteSubscriptionButton) {
    const subscriptionId = Number(deleteSubscriptionButton.dataset.deleteSubscription);
    const subscription = state.subscriptions.find((item) => item.id === subscriptionId);
    const name = subscription ? subscriptionName(subscription) : "эту подписку";
    if (!confirm(`Удалить ${name}? Ключ будет удалён из Remnawave и перестанет работать. Деньги автоматически не возвращаются.`)) return;
    deleteSubscriptionButton.disabled = true;
    try {
      await api(`/subscriptions/${subscriptionId}`, { method: "DELETE" });
      toast("Подписка удалена");
      await loadAccountData();
    } catch (error) {
      toast(error.message, true);
      deleteSubscriptionButton.disabled = false;
    }
  }
  if (renewPlan && state.renewSubscription) {
    const subscription = state.renewSubscription;
    const plan = findPlan(renewPlan.dataset.renewPlan);
    if (plan && plan.kind === subscription.plan_kind) {
      $("renewDialog").close();
      openCheckout({ type: "subscription", code: plan.code, target: "renew", subscriptionId: subscription.id });
      state.renewSubscription = null;
    }
  }
  if (buyTraffic) { switchAccountSection("traffic"); $("trafficSubscription").value = buyTraffic.dataset.buyTraffic; }
  if (buyDevices) {
    openCheckout({ type: "devices", subscriptionId: Number(buyDevices.dataset.buyDevices), deviceCount: Number(buyDevices.dataset.deviceCount) });
  }
  if (trafficPlan) {
    const subscriptionId = Number($("trafficSubscription").value);
    if (!subscriptionId) { toast("Сначала нужна активная подписка с антиглушилкой", true); return; }
    openCheckout({ type: "traffic", code: trafficPlan.dataset.trafficPlan, subscriptionId });
  }
  if (copy) {
    await navigator.clipboard.writeText(copy.dataset.copyKey).catch(() => {});
    toast("Ключ скопирован");
  }
  if (connect) {
    const key = connect.dataset.connect;
    await navigator.clipboard.writeText(key).catch(() => {});
    toast("Ключ скопирован — открываем Happ");
    window.location.href = happBridgeLink(key);
  }
  if (connectionHelp) $("connectionDialog").showModal();
});

window.addEventListener("popstate", () => routeFromLocation(false));

async function routeFromLocation(push = false) {
  if (location.pathname === "/account") {
    if (state.account) await showAccount("overview", push); else showAuth("login", push);
  } else if (location.pathname === "/register") {
    if (state.account) await showAccount("overview", push); else showAuth("register", push);
  }
  else if (location.pathname === "/login") showAuth("login", push);
  else if (state.account) await showAccount("overview", push);
  else showAuth("register", push);
}

async function boot() {
  captureTrackingCode();
  try {
    const [catalog, config] = await Promise.all([api("/catalog"), api("/config")]);
    state.catalog = catalog;
    if (config.agreement_url) $("agreementLink").href = config.agreement_url;
    renderAccountPlans(); renderTraffic();
  } catch (error) { toast("Не удалось загрузить тарифы", true); }
  try { state.account = await api("/me"); } catch { state.account = null; }
  await routeFromLocation(false);
  if (state.account) pollPendingPayment();
}

boot();
