"use strict";

(() => {
  const storageKey = "wayspn.renewal.v1";
  const $ = (id) => document.getElementById(id);
  let state = {};
  let tariffs = [];
  let selected = null;
  let timer = null;
  let checking = false;
  let creating = false;
  let closed = false;

  function save() {
    try { sessionStorage.setItem(storageKey, JSON.stringify(state)); } catch (_) { /* private browsing */ }
  }

  function showError(message) {
    $("error").textContent = message;
    $("error").hidden = false;
  }

  function clearError() { $("error").hidden = true; }

  async function api(path, options = {}) {
    const response = await fetch(`/site/api/renewal${path}`, {
      ...options,
      credentials: "omit",
      cache: "no-store",
      referrerPolicy: "no-referrer",
      headers: { "Authorization": `Bearer ${state.url}`, "Content-Type": "application/json" },
    });
    let data;
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) {
      const error = new Error(typeof data.detail === "string" ? data.detail : "Не удалось выполнить запрос. Попробуйте позже");
      error.status = response.status;
      throw error;
    }
    return data;
  }

  const money = (value) => `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(value)} ₽`;

  function setLink(id, value) {
    try {
      const url = new URL(value);
      if (url.protocol !== "https:" || url.username || url.password) return;
      $(id).href = url.href;
      $(id).hidden = false;
    } catch (_) { /* optional link */ }
  }

  function selectTariff(tariff) {
    selected = tariff;
    for (const label of $("tariffs").children) {
      const active = label.querySelector("input").value === tariff.code;
      label.classList.toggle("selected", active);
      label.querySelector("input").checked = active;
    }
    $("total").textContent = money(tariff.price);
  }

  function render(data, recoverPending = true) {
    $("subscription-name").textContent = data.subscription.name;
    const until = data.subscription.subscription_until ? new Date(data.subscription.subscription_until) : null;
    const date = until && !Number.isNaN(until.getTime()) ? until.toLocaleDateString("ru-RU") : null;
    $("subscription-until").textContent = date
      ? `${until.getTime() > Date.now() ? "Действует до" : "Закончилась"} ${date}`
      : "Выберите срок продления";
    setLink("support-link", data.support_url);
    setLink("agreement-link", data.agreement_url);
    tariffs = data.tariffs;
    $("tariffs").replaceChildren();
    for (const tariff of tariffs) {
      const label = document.createElement("label");
      label.className = "tariff";
      const radio = document.createElement("input");
      radio.type = "radio"; radio.name = "tariff"; radio.value = tariff.code;
      radio.addEventListener("change", () => selectTariff(tariff));
      const days = document.createElement("span");
      days.textContent = `${tariff.days} дней`;
      const price = document.createElement("b");
      price.textContent = money(tariff.price);
      label.append(radio, days, price);
      if (tariff.original_price > tariff.price) {
        const old = document.createElement("s"); old.textContent = money(tariff.original_price); label.append(old);
      }
      $("tariffs").append(label);
    }
    if (tariffs.length) selectTariff(tariffs.find((t) => t.code === state.tariffCode) || tariffs[0]);
    $("pay-button").disabled = !tariffs.length;
    $("loading").hidden = true;
    $("content").hidden = false;
    if (recoverPending && !state.invoiceId && data.pending_invoice_id) { state.invoiceId = data.pending_invoice_id; save(); }
  }

  function scheduleCheck() {
    clearTimeout(timer);
    if (!closed && !document.hidden && state.invoiceId) timer = setTimeout(checkPayment, 8000);
  }

  async function finishPayment() {
    clearTimeout(timer);
    state = { url: state.url }; save();
    $("payment-status").hidden = true;
    $("renew-form").hidden = true;
    $("retry-button").hidden = true;
    $("success").textContent = "Оплата прошла. Подписка продлена!";
    $("success").hidden = false;
    $("success-note").hidden = false;
    clearError();
    try { render(await api("/details"), false); } catch (_) { /* payment is already durably activated */ }
  }

  async function checkPayment() {
    if (!state.invoiceId || checking || creating || closed) return;
    checking = true;
    try {
      const result = await api(`/payments/${encodeURIComponent(state.invoiceId)}`);
      clearError();
      if (result.status === "paid") {
        await finishPayment();
        return;
      }
      if (result.status === "canceled") {
        delete state.invoiceId; delete state.requestId; save();
        $("payment-status").textContent = "Оплата отменена. Можно выбрать срок и оплатить ещё раз.";
      } else {
        $("payment-status").textContent = "Если вы уже оплатили — продление появится автоматически. Проверяем оплату…";
        scheduleCheck();
      }
      $("payment-status").hidden = false;
    } catch (error) {
      showError(error.message || "Нет связи. Проверка продолжится автоматически.");
      if (![401, 403, 404, 422, 429].includes(error.status)) scheduleCheck();
      $("retry-button").hidden = false;
    } finally { checking = false; }
  }

  async function load() {
    clearError();
    $("retry-button").hidden = true;
    try {
      render(await api("/details"));
      if (state.invoiceId) await checkPayment();
    } catch (error) {
      $("loading").hidden = true;
      showError(error.message || "Нет связи. Попробуйте ещё раз.");
      $("retry-button").hidden = false;
    }
  }

  $("renew-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!selected || creating || checking) return;
    creating = true;
    clearTimeout(timer);
    clearError();
    $("pay-button").disabled = true;
    $("pay-button").textContent = "Открываем оплату…";
    try {
      // Reuse the checkout request after a timeout/reload, never the next tariff's request.
      if (!state.requestId || state.tariffCode !== selected.code || state.price !== selected.price) {
        state.requestId = crypto.randomUUID(); state.tariffCode = selected.code; state.price = selected.price;
      }
      save();
      const result = await api("/payment", {
        method: "POST", body: JSON.stringify({ tariff_code: selected.code, request_id: state.requestId, expected_price: selected.price }),
      });
      state.invoiceId = result.invoice_id; save();
      if (result.status === "paid") { await finishPayment(); return; }
      const url = new URL(result.pay_url);
      if (url.protocol !== "https:" || url.username || url.password) throw new Error("Не удалось открыть оплату");
      window.location.assign(url.href);
    } catch (error) {
      showError(error.message || "Не удалось открыть оплату. Попробуйте ещё раз.");
      $("retry-button").hidden = false;
    } finally {
      creating = false;
      $("pay-button").disabled = false;
      $("pay-button").textContent = "Перейти к оплате";
    }
  });
  $("retry-button").addEventListener("click", load);
  window.addEventListener("pagehide", () => { closed = true; clearTimeout(timer); });
  window.addEventListener("pageshow", (event) => { if (event.persisted) { closed = false; load(); } });
  // An app may reuse the same browser tab and only change the fragment.
  // A fresh document prevents a previous subscription's in-flight response
  // from replacing the newly selected subscription.
  window.addEventListener("hashchange", () => { if (window.location.hash) window.location.reload(); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(timer);
    else if (state.invoiceId) checkPayment();
  });

  try { state = JSON.parse(sessionStorage.getItem(storageKey) || "{}"); } catch (_) { state = {}; }
  if (!state || typeof state !== "object") state = {};
  let fragment = window.location.hash.slice(1);
  // The credential must not stay in browser history or be passed to the payment provider.
  window.history.replaceState(null, "", "/renew");
  if (fragment) {
    try { fragment = decodeURIComponent(fragment); } catch (_) { fragment = ""; }
    if (fragment !== state.url) state = { url: fragment };
    save();
  }
  try {
    const url = new URL(state.url);
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) throw new Error();
  } catch (_) {
    $("loading").hidden = true;
    showError("Откройте подписку: в Happ нажмите ⓘ слева от трафика, в INCY — «🌐 Сайт».");
    return;
  }
  load();
})();
