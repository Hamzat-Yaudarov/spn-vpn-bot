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
  selectedSubId: null,
  trafficSubId: null,
  buyMode: null,
  buyPlan: null,
  buySubscriptionId: null,
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
function showToast(text) { const t = el("toast"); t.textContent = text; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 2200); }
function openLink(url) { tg?.openLink ? tg.openLink(url) : window.open(url, "_blank"); }
function subTitle(s) { return `${s.plan_kind === "bypass" ? "С антиглушилкой" : "Обычная"} #${s.type_index}`; }
function activeSubs() { return state.subs.filter((s) => s.status === "active"); }

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
  renderSubs();
  renderBuy();
  renderReferral();
  renderHelp();
}

function renderHome() {
  const hasBypass = activeSubs().some((s) => s.plan_kind === "bypass");
  el("view-home").innerHTML = `
    <div class="grid">
      <div class="card strong">
        <p class="title">Что хотите сделать?</p>
        <p class="muted">Все основные действия собраны ниже. Выберите нужный раздел.</p>
        <div class="grid">
          <button class="button accent" onclick="startBuy('new')">Купить новую подписку</button>
          <button class="button ghost" onclick="startBuy('renew')">Продлить подписку</button>
          <button class="button ghost" onclick="switchView('subs')">Мои ключи</button>
          ${hasBypass ? `<button class="button green" onclick="switchView('subs'); showToast('Откройте ключ с антиглушилкой и нажмите Купить ГБ')">Купить ГБ</button>` : ""}
        </div>
      </div>
      <div class="card"><p class="title">Тарифы простыми словами</p><p class="muted">Обычная подписка подходит для повседневного VPN. Антиглушилка нужна, если сеть активно ограничивает доступ: 80 ГБ каждый месяц только для антиглушилки.</p></div>
    </div>`;
}

function renderSubs() {
  const container = el("view-subs");
  if (!state.subs.length) {
    container.innerHTML = `<div class="card empty"><h2>Ключей пока нет</h2><p class="muted">Купите первую подписку, и ключ появится здесь.</p><button class="button accent" onclick="startBuy('new')">Купить подписку</button></div>`;
    return;
  }

  const selected = state.subs.find((s) => s.id === state.selectedSubId);
  if (!selected) {
    container.innerHTML = `<div class="grid"><div class="card"><p class="title">Мои ключи</p><p class="muted">Выберите нужный ключ, чтобы открыть информацию, скопировать ссылку или продлить подписку.</p></div><div class="choice-list">${state.subs.map(keyButton).join("")}</div></div>`;
    return;
  }

  container.innerHTML = subscriptionDetail(selected);
}

function keyButton(s) {
  return `<button class="choice-button" onclick="openSub(${s.id})"><span>${subTitle(s)}<small>${s.status === "active" ? `Активна до ${date(s.subscription_until)}` : "Истекла"}</small></span><b>›</b></button>`;
}

function openSub(id) {
  state.selectedSubId = id;
  state.trafficSubId = null;
  switchView("subs");
}

function subscriptionDetail(s) {
  const percent = s.traffic.enabled && s.traffic.limit_gb ? Math.min(100, Math.round((s.traffic.used_gb / s.traffic.limit_gb) * 100)) : 0;
  return `<div class="grid">
    <button class="button ghost" onclick="backToKeys()">← Назад к ключам</button>
    <div class="card strong">
      <div class="row start"><div><p class="title">${subTitle(s)}</p><p class="muted">${s.status === "active" ? `Активна до ${date(s.subscription_until)}` : "Подписка истекла"}</p></div><span class="badge ${s.status === "active" ? "ok" : "warn"}">${s.status === "active" ? "Активна" : "Истекла"}</span></div>
      ${s.traffic.enabled ? `<div class="grid"><div><div class="row"><span class="small">Трафик антиглушилки</span><span class="small">${s.traffic.used_gb} / ${s.traffic.limit_gb} ГБ</span></div><div class="progress"><span style="width:${percent}%"></span></div><p class="small">Сброс: ${date(s.traffic.reset_at)}</p></div></div>` : ""}
    </div>
    <div class="card"><p class="title">Ключ</p>${s.subscription_url ? `<div class="keybox">${s.subscription_url}</div><button class="button accent" onclick="copyText('${encodeURIComponent(s.subscription_url)}')">Скопировать ключ</button>` : `<p class="muted">Ключ появится после активации оплаты.</p>`}</div>
    <button class="button ghost" onclick="startRenewFor(${s.id})">Продлить эту подписку</button>
    ${s.traffic.enabled ? `<button class="button green" onclick="toggleTraffic(${s.id})">Купить ГБ</button>${state.trafficSubId === s.id ? trafficPackages(s) : ""}` : ""}
  </div>`;
}

function trafficPackages(s) {
  return `<div class="card"><p class="title">Пакеты ГБ</p><p class="muted">ГБ тратятся только на антиглушилку.</p><div class="choice-list">${state.tariffs.traffic_packages.map((p) => `<button class="choice-button" onclick="buyTraffic(${s.id}, '${p.code}')"><span>+${p.gb} ГБ<small>${rub(p.price)}</small></span><b>Купить</b></button>`).join("")}</div></div>`;
}

function toggleTraffic(id) {
  state.trafficSubId = state.trafficSubId === id ? null : id;
  renderSubs();
}

function backToKeys() {
  state.selectedSubId = null;
  state.trafficSubId = null;
  renderSubs();
}

function renderBuy() {
  const container = el("view-buy");
  if (!state.buyMode) {
    container.innerHTML = `<div class="grid"><div class="card strong"><p class="step">Шаг 1</p><p class="title">Что хотите сделать?</p><p class="muted">Купить новый ключ или продлить уже существующий.</p><div class="grid"><button class="button accent" onclick="selectBuyMode('new')">Купить новую подписку</button><button class="button ghost" onclick="selectBuyMode('renew')">Продлить существующую</button></div></div></div>`;
    return;
  }

  if (state.buyMode === "new") {
    renderNewPurchase(container);
    return;
  }

  renderRenewPurchase(container);
}

function renderNewPurchase(container) {
  if (!state.buyPlan) {
    container.innerHTML = `<div class="grid"><button class="button ghost" onclick="resetBuy()">← Назад</button><div class="card strong"><p class="step">Шаг 2</p><p class="title">Выберите тип подписки</p><p class="muted">Обычная дешевле. Антиглушилка нужна для сетей с ограничениями.</p><div class="grid"><button class="button accent" onclick="selectBuyPlan('regular')">Обычная подписка</button><button class="button green" onclick="selectBuyPlan('bypass')">С антиглушилкой</button></div></div></div>`;
    return;
  }
  const tariffs = state.tariffs[state.buyPlan] || [];
  container.innerHTML = `<div class="grid"><button class="button ghost" onclick="selectBuyPlan(null)">← Назад к типам</button><div class="card"><p class="step">Шаг 3</p><p class="title">Выберите срок</p><p class="muted">${state.buyPlan === "regular" ? "Обычная подписка, 5 устройств." : "Антиглушилка, 3 устройства и 80 ГБ в месяц."}</p></div>${tariffs.map((t) => tariffChoice(t, "new", null)).join("")}</div>`;
}

function renderRenewPurchase(container) {
  if (!state.buySubscriptionId) {
    if (!state.subs.length) {
      container.innerHTML = `<div class="grid"><button class="button ghost" onclick="resetBuy()">← Назад</button><div class="card empty"><h2>Нет подписок для продления</h2><p class="muted">Сначала купите новую подписку.</p><button class="button accent" onclick="selectBuyMode('new')">Купить новую</button></div></div>`;
      return;
    }
    container.innerHTML = `<div class="grid"><button class="button ghost" onclick="resetBuy()">← Назад</button><div class="card"><p class="step">Шаг 2</p><p class="title">Что продлить?</p><p class="muted">Выберите ключ, который хотите продлить.</p></div><div class="choice-list">${state.subs.map((s) => `<button class="choice-button" onclick="selectRenewSub(${s.id})"><span>${subTitle(s)}<small>${s.status === "active" ? `До ${date(s.subscription_until)}` : "Истекла"}</small></span><b>›</b></button>`).join("")}</div></div>`;
    return;
  }

  const sub = state.subs.find((s) => s.id === state.buySubscriptionId);
  const tariffs = state.tariffs[sub?.plan_kind || "regular"] || [];
  container.innerHTML = `<div class="grid"><button class="button ghost" onclick="selectRenewSub(null)">← Назад к ключам</button><div class="card"><p class="step">Шаг 3</p><p class="title">Продлить ${subTitle(sub)}</p><p class="muted">Выберите срок продления.</p></div>${tariffs.map((t) => tariffChoice(t, "renew", sub.id)).join("")}</div>`;
}

function tariffChoice(t, mode, subscriptionId) {
  const period = t.days === 30 ? "1 месяц" : t.days === 90 ? "3 месяца" : `${t.days} дней`;
  return `<button class="choice-button" onclick="buySubscription('${t.code}', '${mode}', ${subscriptionId || null})"><span>${period}<small>${t.kind === "regular" ? "Обычная подписка" : "С антиглушилкой"}</small></span><b>${rub(t.price)}</b></button>`;
}

function selectBuyMode(mode) { state.buyMode = mode; state.buyPlan = null; state.buySubscriptionId = null; switchView("buy"); }
function selectBuyPlan(plan) { state.buyPlan = plan; renderBuy(); }
function selectRenewSub(id) { state.buySubscriptionId = id; renderBuy(); }
function resetBuy() { state.buyMode = null; state.buyPlan = null; state.buySubscriptionId = null; renderBuy(); }
function startBuy(mode) { selectBuyMode(mode); }
function startRenewFor(id) { state.buyMode = "renew"; state.buySubscriptionId = id; state.buyPlan = null; switchView("buy"); }

function renderReferral() {
  const r = state.referral;
  el("view-ref").innerHTML = `<div class="grid"><div class="card strong"><p class="title">Бонус за друга</p><p class="muted">35% с первой покупки и 15% с повторных.</p></div><div class="card"><div class="row"><span>Активных друзей</span><b>${r?.active_referrals || 0}</b></div><div class="row"><span>Всего заработано</span><b>${rub(r?.total_earned || 0)}</b></div><div class="row"><span>Баланс</span><b>${rub(r?.current_balance || 0)}</b></div></div><div class="card"><p class="title">Ваша ссылка</p><div class="keybox">${r?.link || ""}</div><button class="button ghost" onclick="copyText('${encodeURIComponent(r?.link || "")}')">Скопировать ссылку</button></div></div>`;
}

function renderHelp() {
  el("view-help").innerHTML = `<div class="grid"><div class="card"><p class="title">Как подключиться</p><p class="muted">Скопируйте ключ из раздела “Ключи”, откройте Happ Plus, нажмите “+” и вставьте ключ из буфера обмена.</p></div><div class="card"><p class="title">Поддержка</p><p class="muted">Если что-то не получается, напишите нам в Telegram.</p><button class="button ghost" onclick="openLink('https://t.me/wayspn_support')">Открыть поддержку</button></div></div>`;
}

async function buySubscription(tariffCode, target, subscriptionId) {
  try {
    const provider = await chooseProvider();
    if (!provider) return;
    const data = await api("/miniapp/api/payments/subscription", { method: "POST", body: JSON.stringify({ tariff_code: tariffCode, provider, payment_target: target, subscription_id: subscriptionId }) });
    openLink(data.pay_url);
    showToast("Счёт создан. После оплаты обновите кабинет.");
  } catch (e) {
    showToast(e.message);
  }
}

async function buyTraffic(subscriptionId, packageCode) {
  try {
    const provider = await chooseProvider();
    if (!provider) return;
    const data = await api("/miniapp/api/payments/traffic", { method: "POST", body: JSON.stringify({ subscription_id: subscriptionId, package_code: packageCode, provider }) });
    openLink(data.pay_url);
    showToast("Счёт на ГБ создан");
  } catch (e) {
    showToast(e.message);
  }
}

async function chooseProvider() {
  return new Promise((resolve) => {
    const modal = el("providerModal");
    modal.classList.remove("hidden");
    const handler = (event) => {
      const provider = event.target?.dataset?.provider;
      if (provider === undefined) return;
      modal.classList.add("hidden");
      modal.removeEventListener("click", handler);
      resolve(provider || null);
    };
    modal.addEventListener("click", handler);
  });
}

async function copyText(encoded) {
  const text = decodeURIComponent(encoded);
  await navigator.clipboard.writeText(text).catch(() => {});
  showToast("Скопировано");
}

load();
