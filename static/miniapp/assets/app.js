const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
}

const initData = tg?.initData || "";
const state = { me: null, subs: [], tariffs: null, referral: null, plan: "regular" };

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

function render() {
  renderHome();
  renderSubs();
  renderBuy();
  renderReferral();
  renderHelp();
}

function renderHome() {
  const active = state.subs.filter((s) => s.status === "active");
  const bypass = active.filter((s) => s.plan_kind === "bypass");
  el("view-home").innerHTML = `
    <div class="grid">
      <div class="card strong">
        <div class="row"><div><p class="title">Ваш доступ</p><p class="muted">Активных подписок: ${active.length}</p></div><span class="badge ${active.length ? "ok" : "warn"}">${active.length ? "Активен" : "Нет доступа"}</span></div>
      </div>
      <div class="card"><p class="title">Быстрые действия</p><div class="grid"><button class="button accent" onclick="switchView('buy')">Купить или продлить</button><button class="button ghost" onclick="switchView('subs')">Открыть мои ключи</button>${bypass.length ? `<button class="button green" onclick="switchView('subs')">Купить ГБ для антиглушилки</button>` : ""}</div></div>
      <div class="card"><p class="title">Тарифы</p><p class="muted">Обычная подписка без лимита трафика. Антиглушилка: 80 ГБ в месяц и отдельные серверы для стабильной работы.</p></div>
    </div>`;
}

function renderSubs() {
  if (!state.subs.length) {
    el("view-subs").innerHTML = `<div class="card empty"><h2>Подписок пока нет</h2><p class="muted">Выберите тариф и оплатите первый ключ.</p><button class="button accent" onclick="switchView('buy')">Купить подписку</button></div>`;
    return;
  }
  el("view-subs").innerHTML = `<div class="grid">${state.subs.map(subscriptionCard).join("")}</div>`;
}

function subscriptionCard(s) {
  const title = s.plan_kind === "bypass" ? "С антиглушилкой" : "Обычная";
  const percent = s.traffic.enabled && s.traffic.limit_gb ? Math.min(100, Math.round((s.traffic.used_gb / s.traffic.limit_gb) * 100)) : 0;
  return `<div class="card">
    <div class="row start"><div><p class="title">${title} #${s.type_index}</p><p class="muted">До ${date(s.subscription_until)}</p></div><span class="badge ${s.status === "active" ? "ok" : "warn"}">${s.status === "active" ? "Активна" : "Истекла"}</span></div>
    ${s.traffic.enabled ? `<div class="grid"><div><div class="row"><span class="small">Трафик</span><span class="small">${s.traffic.used_gb} / ${s.traffic.limit_gb} ГБ</span></div><div class="progress"><span style="width:${percent}%"></span></div><p class="small">Сброс: ${date(s.traffic.reset_at)}</p></div>${trafficButtons(s)}</div>` : ""}
    <div class="grid"><button class="button accent" onclick="buyRenew(${s.id}, '${s.plan_kind}')">Продлить</button>${s.subscription_url ? `<button class="button ghost" onclick="copyText('${encodeURIComponent(s.subscription_url)}')">Скопировать ключ</button><div class="keybox">${s.subscription_url}</div>` : `<p class="muted">Ключ будет доступен после активации.</p>`}</div>
  </div>`;
}

function trafficButtons(s) {
  return `<div class="grid">${state.tariffs.traffic_packages.map((p) => `<button class="button ghost" onclick="buyTraffic(${s.id}, '${p.code}', 'cryptobot')">+${p.gb} ГБ — ${rub(p.price)}</button>`).join("")}</div>`;
}

function renderBuy() {
  const tariffs = state.tariffs?.[state.plan] || [];
  el("view-buy").innerHTML = `<div class="grid"><div class="segmented"><button class="${state.plan === "regular" ? "active" : ""}" onclick="setPlan('regular')">Обычная</button><button class="${state.plan === "bypass" ? "active" : ""}" onclick="setPlan('bypass')">Антиглушилка</button></div>${tariffs.map(tariffCard).join("")}</div>`;
}

function tariffCard(t) {
  const activeSame = state.subs.filter((s) => s.plan_kind === t.kind);
  return `<div class="card"><div class="row"><div><p class="title">${t.title}</p><p class="muted">${t.days} дней${t.base_gb ? ` · ${t.base_gb} ГБ/мес` : " · без лимита ГБ"}</p></div><div class="price">${rub(t.price)}</div></div><ul class="features"><li>${t.kind === "regular" ? "5 устройств" : "3 устройства"}</li><li>${t.kind === "regular" ? "Обычные серверы" : "Серверы с антиглушилкой"}</li></ul><div class="grid"><button class="button accent" onclick="buySubscription('${t.code}', 'new', null)">Купить новую</button>${activeSame.map((s) => `<button class="button ghost" onclick="buySubscription('${t.code}', 'renew', ${s.id})">Продлить #${s.type_index}</button>`).join("")}</div></div>`;
}

function renderReferral() {
  const r = state.referral;
  el("view-ref").innerHTML = `<div class="grid"><div class="card strong"><p class="title">Бонус за друга</p><p class="muted">35% с первой покупки и 15% с повторных.</p></div><div class="card"><div class="row"><span>Активных друзей</span><b>${r?.active_referrals || 0}</b></div><div class="row"><span>Всего заработано</span><b>${rub(r?.total_earned || 0)}</b></div><div class="row"><span>Баланс</span><b>${rub(r?.current_balance || 0)}</b></div></div><div class="card"><p class="title">Ваша ссылка</p><div class="keybox">${r?.link || ""}</div><button class="button ghost" onclick="copyText('${encodeURIComponent(r?.link || "")}')">Скопировать ссылку</button></div></div>`;
}

function renderHelp() {
  el("view-help").innerHTML = `<div class="grid"><div class="card"><p class="title">Как подключиться</p><p class="muted">Скопируйте ключ из раздела “Ключи”, откройте Happ Plus, нажмите “+” и вставьте ключ из буфера обмена.</p></div><div class="card"><p class="title">Поддержка</p><p class="muted">Если что-то не получается, напишите нам в Telegram.</p><button class="button ghost" onclick="openLink('https://t.me/wayspn_support')">Открыть поддержку</button></div></div>`;
}

function setPlan(plan) { state.plan = plan; renderBuy(); }
function buyRenew(id, plan) { state.plan = plan; switchView("buy"); showToast(`Выберите срок для продления #${id}`); }

async function buySubscription(tariffCode, target, subscriptionId) {
  const provider = await chooseProvider();
  if (!provider) return;
  const data = await api("/miniapp/api/payments/subscription", { method: "POST", body: JSON.stringify({ tariff_code: tariffCode, provider, payment_target: target, subscription_id: subscriptionId }) });
  openLink(data.pay_url);
  showToast("Счёт создан. После оплаты обновите кабинет.");
}

async function buyTraffic(subscriptionId, packageCode, provider = "cryptobot") {
  const data = await api("/miniapp/api/payments/traffic", { method: "POST", body: JSON.stringify({ subscription_id: subscriptionId, package_code: packageCode, provider }) });
  openLink(data.pay_url);
  showToast("Счёт на ГБ создан");
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
