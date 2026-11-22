"use strict";

(() => {
  const $ = (selector, node = document) => node.querySelector(selector);
  const app = $("#app");
  const dialog = $("#dialog");
  const state = { csrf: "", authenticated: false, page: "servers", overview: {}, servers: [], operations: [], settings: {}, catalog: null, updated: null, loading: false, error: "", search: "", operationFilter: "all", menuOpen: false };
  const icons = {
    server: ["M4 3h16v7H4z", "M4 14h16v7H4z", "M7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6"],
    activity: ["M3 12h4l3-8 4 16 3-8h4"],
    settings: ["M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8", "m9.5 3-.6 2-2 .9-1.9-.5-2 3.4 1.4 1.6v2.3L3 14.3l2 3.4 2-.4 2 .9.5 2h4l.6-2 2-.9 1.9.4 2-3.4-1.4-1.6v-2.3l1.4-1.6-2-3.4-1.9.5-2-.9-.6-2z"],
    arrow: ["M5 12h14m-5-5 5 5-5 5"], plus: ["M12 5v14M5 12h14"],
    refresh: ["M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5M4 16a8 8 0 0 0 14 3l3-3m0 5v-5h-5"],
    shield: ["m12 3 8 3v6c0 4-5 8-8 9-3-1-8-5-8-9V6z", "m8 12 3 3 5-6"],
    wallet: ["M4 5h14v4H4a2 2 0 0 1 0-4z", "M4 9h16v11H4a2 2 0 0 1-2-2V7", "M15 12h5v5h-5z"],
    chart: ["M4 4v16h17M8 15l4-5 4 3 5-8"], globe: ["M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0", "M3 12h18M12 3a18 18 0 0 1 0 18 18 18 0 0 1 0-18"],
    check: ["m5 12 4 4L19 6"], info: ["M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0", "M12 11v6M12 7h.01"],
    clock: ["M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0", "M12 7v5l3 2"],
    close: ["m6 6 12 12M6 18 18 6"], search: ["M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0", "m15 15 6 6"],
    chevron: ["m9 6 6 6-6 6"], lock: ["M5 10h14v11H5z", "M8 10V7a4 4 0 0 1 8 0v3M12 14v3"],
    logout: ["M9 4H4v16h5M10 12h11m-4-4 4 4-4 4"], menu: ["M4 6h16M4 12h16M4 18h16"],
    eye: ["M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12", "M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0"],
    pause: ["M8 5v14M16 5v14"], play: ["m7 4 14 8-14 8z"], warning: ["m12 3 10 18H2z", "M12 9v5M12 17h.01"],
    key: ["M12 8a5 5 0 1 1-10 0 5 5 0 0 1 10 0", "m10 11 10 10m-4-4 3-3m-6 0 3-3"],
    link: ["m9 15 6-6M7 14l-2 2a3 3 0 0 0 4 4l3-3m0-10 3-3a3 3 0 0 1 4 4l-2 2"]
  };
  function icon(name, className) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const [key, value] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.5", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) svg.setAttribute(key, value);
    if (className) svg.setAttribute("class", className);
    for (const path of icons[name] || icons.info) { const p = document.createElementNS(svg.namespaceURI, "path"); p.setAttribute("d", path); svg.append(p); }
    return svg;
  }
  function h(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = String(value);
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else if (key in node && !key.startsWith("aria") && key !== "role") node[key] = value;
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat(Infinity)) if (child !== null && child !== undefined && child !== false) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    return node;
  }
  const button = (label, name, onClick, className = "", attrs = {}) => h("button", { type: "button", class: `button ${className}`, onclick: onClick, ...attrs }, name ? icon(name) : null, label);
  const iconButton = (label, name, action, attrs = {}) => h("button", { type: "button", class: "icon-button", title: label, "aria-label": label, onclick: action, ...attrs }, icon(name));
  const textValue = (value, fallback = "—") => value === null || value === undefined || value === "" ? fallback : String(value);
  const asArray = value => Array.isArray(value) ? value : [];
  const first = (...values) => values.find(value => value !== undefined && value !== null && value !== "");
  const requestId = () => {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = bytes[6] & 15 | 64; bytes[8] = bytes[8] & 63 | 128;
    const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  };
  const modeLabel = value => ({ observe: "Наблюдение", approval: "С подтверждением", auto: "Автопилот" }[value] || textValue(value, "Наблюдение"));
  function date(value, withTime = true) {
    if (!value) return "Нет данных";
    const parsed = new Date(typeof value === "number" && value < 100000000000 ? value * 1000 : value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    return new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short", ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}) }).format(parsed);
  }
  function money(value, currency) {
    if (value === undefined || value === null || value === "") return "—";
    const number = Number(value);
    if (!Number.isFinite(number)) return String(value);
    try { return new Intl.NumberFormat("ru-RU", { style: "currency", currency: currency || state.settings.currency || "RUB", maximumFractionDigits: 2 }).format(number); }
    catch { return `${number.toLocaleString("ru-RU")} ${currency || ""}`.trim(); }
  }
  function status(value) {
    if (typeof value === "object" && value) value = first(value.status, value.state, value.ok === true ? "healthy" : value.ok === false ? "failed" : undefined);
    const map = {
      healthy: ["Доступен", "good"], online: ["Доступен", "good"], active: ["Активен", "good"], ready: ["Готов", "good"], ok: ["Доступен", "good"], passed: ["Успешно", "good"], success: ["Завершено", "good"], succeeded: ["Завершено", "good"], completed: ["Завершено", "good"], running: ["Выполняется", "info"], pending: ["В очереди", "info"], queued: ["В очереди", "info"], provisioning: ["Создаётся", "info"], checking: ["Проверяется", "info"], replacing: ["Замена", "info"], degraded: ["Есть сбои", "warning"], warning: ["Внимание", "warning"], paused: ["Приостановлен", "warning"], disabled: ["Отключён", "warning"], approval: ["Ждёт решения", "warning"], awaiting_approval: ["Ждёт решения", "warning"], failed: ["Ошибка", "danger"], error: ["Ошибка", "danger"], offline: ["Недоступен", "danger"], unreachable: ["Недоступен", "danger"], blocked: ["Заблокировано", "warning"], cancelled: ["Отменено", ""], unknown: ["Нет данных", ""], not_configured: ["Не настроено", ""], unavailable: ["Нет данных", ""]
    };
    const phases = { complete: ["Завершено", "good"], reserved: ["Бюджет зарезервирован", "info"], ordered: ["Заказ оформлен", "info"], bootstrap: ["Настройка VPN", "info"], "vpn-tested": ["VPN проверен", "info"], cutover: ["Переключение", "info"], committed: ["Узел переключён", "info"], "old-retired": ["Старый узел выведен", "info"], rejected: ["Кандидат отклонён", "warning"], "cleanup-pending": ["Ожидание очистки", "warning"], "outcome-unknown": ["Результат неизвестен", "warning"], "manual-review": ["Нужно решение", "warning"] };
    return map[value] || phases[value] || [textValue(value, "Нет данных"), ""];
  }
  function badge(value, forcedLabel) { const [label, kind] = status(value); return h("span", { class: `badge ${kind}` }, h("span", { class: "status-dot" }), forcedLabel || label); }
  function toast(message, error = false) {
    const node = h("div", { class: `toast ${error ? "error" : ""}`, role: error ? "alert" : "status" }, icon(error ? "warning" : "check"), h("span", {}, message));
    $("#toasts").append(node); window.setTimeout(() => node.remove(), error ? 10000 : 5500);
  }
  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 35000);
    try {
      const response = await fetch(path, { credentials: "same-origin", signal: controller.signal, ...options, headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.method && options.method !== "GET" ? { "X-CSRF-Token": state.csrf, "X-Request-ID": options.requestId || requestId() } : {}), ...options.headers }, ...(options.body && typeof options.body !== "string" ? { body: JSON.stringify(options.body) } : {}) });
      const payload = await response.json().catch(() => ({}));
      if (response.status === 401 && path !== "/api/login") { state.authenticated = false; renderLogin(); throw new Error("Сессия завершилась. Войдите в панель снова."); }
      if (!response.ok) {
        let message = first(payload.message, payload.error, payload.detail);
        if (typeof message === "object") message = Array.isArray(message) ? message.map(item => textValue(item.msg, "Проверьте данные")).join(". ") : first(message.message, message.error, "Не удалось выполнить запрос.");
        throw new Error(textValue(message, `Запрос не выполнен (${response.status}).`));
      }
      return payload;
    } catch (error) { if (error.name === "AbortError") throw new Error("Сервер не ответил вовремя. Проверьте соединение и повторите запрос."); throw error; }
    finally { clearTimeout(timer); }
  }
  async function busy(node, task) {
    if (node) { node.disabled = true; node.classList.add("is-loading"); node.setAttribute("aria-busy", "true"); }
    try { return await task(); }
    catch (error) { toast(error.message || "Не удалось выполнить действие.", true); return null; }
    finally { if (node?.isConnected) { node.disabled = false; node.classList.remove("is-loading"); node.removeAttribute("aria-busy"); } }
  }
  function notice(title, text, type = "", action = null) {
    return h("div", { class: `notice ${type}`, role: type === "error" ? "alert" : "note" }, icon(type === "error" || type === "warning" ? "warning" : "shield"), h("div", { class: "notice-body" }, h("div", { class: "notice-title" }, title), h("div", { class: "notice-text" }, text)), action);
  }
  function empty(title, description, action, iconName = "server") { return h("div", { class: "empty-state" }, h("div", { class: "empty-icon" }, icon(iconName)), h("h3", {}, title), h("p", {}, description), action); }
  function brand() { return h("div", { class: "brand" }, h("span", { class: "brand-symbol", "aria-hidden": "true" }), h("div", {}, h("div", { class: "brand-name" }, "Voxa", h("span", {}, "Fleet")), h("div", { class: "brand-kicker" }, "Infrastructure control"))); }
  function navigate(page) { state.page = page; state.menuOpen = false; window.location.hash = page; render(); }
  async function loadOverview(renderAfter = true) {
    const response = await api("/api/overview");
    state.overview = response || {};
    state.servers = asArray(response.servers).map(server => ({ ...server, ip: first(server.current_ip, server.ip, server.ip_address, server.ipv4) }));
    state.operations = asArray(response.operations).map(operation => ({ ...operation, status: first(operation.status, operation.phase) }));
    state.settings = normalizeSettings(response.settings || state.settings);
    state.updated = new Date(); state.error = "";
    if (renderAfter && state.authenticated && state.page !== "settings" && !dialog.open && !document.activeElement?.matches("input,select,textarea")) render();
    return response;
  }
  async function refresh(node) {
    await busy(node, async () => { await loadOverview(false); if (state.page === "settings") state.settings = normalizeSettings(await api("/api/settings")); render(); toast("Данные обновлены"); });
  }
  function render() {
    if (!state.authenticated) return renderLogin();
    const labels = { servers: "Серверы", operations: "Операции", settings: "Настройки" };
    const runningCount = state.operations.filter(item => !["complete", "completed", "succeeded", "success", "failed", "error", "cancelled"].includes(item.status)).length;
    const nav = h("nav", { class: "nav", "aria-label": "Главное меню" }, ...Object.entries(labels).map(([key, label]) => h("button", { type: "button", class: `nav-button ${state.page === key ? "active" : ""}`, "aria-current": state.page === key ? "page" : null, onclick: () => navigate(key) }, icon(key === "servers" ? "server" : key === "operations" ? "activity" : "settings"), label, key === "operations" && runningCount ? h("span", { class: "nav-count" }, runningCount) : null)));
    const sidebar = h("aside", { class: "sidebar" }, brand(), h("div", { class: "nav-label" }, "Рабочее пространство"), nav, h("div", { class: "sidebar-bottom" }, h("div", { class: "environment" }, h("div", { class: "environment-title" }, h("span", { class: "status-dot" }), "Контур управления"), h("p", {}, "Финансовые действия защищены бюджетом и подтверждениями.")), h("div", { class: "account" }, h("span", { class: "avatar" }, "А"), h("div", { class: "account-copy" }, "Администратор", h("small", {}, "Закрытая панель")), iconButton("Выйти", "logout", logout))));
    const topbar = h("div", { class: "topbar" }, h("div", { class: "breadcrumb" }, iconButton("Открыть меню", "menu", () => { state.menuOpen = !state.menuOpen; $(".shell")?.classList.toggle("menu-open", state.menuOpen); }, { class: "icon-button mobile-menu" }), icon("globe"), h("span", {}, "Панель управления"), icon("chevron"), h("strong", {}, labels[state.page])), h("div", { class: "topbar-right" }, h("span", { class: "topbar-state" }, h("span", { class: "status-dot" }), "Защищённая сессия"), h("span", { class: "topbar-divider" }), h("span", { class: "topbar-date" }, new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric" }).format(new Date())), iconButton("Обновить данные", "refresh", event => refresh(event.currentTarget))));
    const content = state.page === "settings" ? renderSettings() : state.page === "operations" ? renderOperations() : renderServers();
    const main = h("main", { class: "main", id: "main-content" }, topbar, state.error ? notice("Не удалось обновить данные", state.error, "error") : null, content, h("footer", { class: "footer" }, h("span", {}, "VoxaFleet", " / ", "Операционный контур"), h("span", {}, h("span", { class: "status-dot" }), state.updated ? `Обновлено ${state.updated.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}` : "Ожидание данных")));
    app.replaceChildren(h("div", { class: `shell ${state.menuOpen ? "menu-open" : ""}` }, sidebar, main)); app.removeAttribute("aria-busy");
  }
  function pageHeader(kicker, title, description, action) { return h("header", { class: "page-header" }, h("div", {}, h("div", { class: "eyebrow" }, kicker), h("h1", {}, title), h("p", { class: "page-description" }, description)), h("div", { class: "header-actions" }, button("Обновить", "refresh", event => refresh(event.currentTarget), "secondary-refresh"), action)); }
  function renderStats() {
    const { overview, servers, settings } = state;
    const balance = overview.balance;
    const budget = overview.budget || {};
    const active = servers.filter(item => item.enabled !== false && !["disabled", "deleted", "paused"].includes(item.status)).length;
    const ready = overview.readiness || {};
    const financial = Boolean(first(ready.financial_ready, ready.billing_ready, settings.financial_enabled, false));
    const spent = first(budget.spent_today, budget.daily_spent, budget.today_spent, budget.gross_daily, budget.spent);
    const limit = first(budget.daily_budget, budget.daily_limit, budget.limit, settings.daily_budget);
    const readyCount = asArray(ready.checks).filter(item => item.ok === true || item.ready === true || ["ok", "ready", "passed"].includes(item.status)).length;
    const cards = [
      { label: "Серверы во флоте", value: String(servers.length), suffix: "узлов", note: servers.length ? `${active} включено в управление` : "Первый сервер ещё не добавлен", icon: "server", highlight: true },
      { label: "Баланс провайдера", value: money(typeof balance === "object" && balance ? first(balance.amount, balance.balance, balance.available) : balance, balance?.currency), note: balance?.error ? "Не удалось получить баланс" : typeof balance === "object" && balance?.updated_at ? `Проверен ${date(balance.updated_at)}` : balance === null || balance === undefined ? "Подключите токен провайдера" : "По данным API провайдера", icon: "wallet" },
      { label: "Учтено за сутки", value: money(spent, budget.currency), note: limit !== undefined ? `Включая резервы · лимит ${money(limit, budget.currency)}` : "Резервы и затраты · бюджет не задан", icon: "chart" },
      { label: "Финансовый контур", value: financial ? "Разрешён" : "Защищён", note: financial ? "Действуют лимиты и согласия" : readyCount ? `Проверок пройдено: ${readyCount}. Расходы закрыты` : "Расходы по умолчанию закрыты", icon: "shield", compact: true }
    ];
    return h("section", { class: "stats-grid", "aria-label": "Обзор инфраструктуры" }, ...cards.map(card => h("article", { class: `stat-card ${card.highlight ? "highlight" : ""}` }, h("div", { class: "stat-label" }, card.label, h("span", { class: "stat-icon" }, icon(card.icon))), h("div", { class: `stat-value ${card.compact ? "compact" : ""}` }, card.value, card.suffix ? h("span", {}, card.suffix) : null), h("div", { class: `stat-note ${card.highlight ? "good" : ""}` }, card.highlight && servers.length ? icon("check") : null, card.note))));
  }
  function readinessNotice() {
    const readiness = state.overview.readiness || {};
    const missing = asArray(first(readiness.missing, readiness.blockers, readiness.issues)).map(item => typeof item === "string" ? item : first(item.message, item.name, item.label)).filter(Boolean);
    if (missing.length) return notice("Нужно завершить настройку", missing.slice(0, 3).join(" · "), "", button("Открыть настройки", "arrow", () => navigate("settings")));
    if (!state.settings.financial_enabled) return notice("Наблюдаем. Платные действия выключены", "Проверки не создают новые серверы. Для создания и замены настройте бюджет и отдельно разрешите финансовые операции.", "", button("Настроить ограничения", "arrow", () => navigate("settings")));
    return notice("Расходы ограничены вашими правилами", "Создание и замена серверов проходят проверку бюджета, лимитов попыток и согласий. Автопилот включается отдельно для каждого сервера.");
  }
  function renderServers() {
    const fragment = document.createDocumentFragment();
    fragment.append(pageHeader("Инфраструктура", "Серверы под контролем", "Наблюдение, проверка доступности и безопасная замена VPN-узлов.", button("Добавить сервер", "plus", openCreate, "primary")), renderStats(), readinessNotice());
    const search = h("input", { type: "search", value: state.search, placeholder: "Найти сервер…", "aria-label": "Поиск сервера", oninput: event => { state.search = event.target.value; renderServerRows(); } });
    const panel = h("section", { class: "panel" }, h("div", { class: "panel-heading" }, h("div", {}, h("div", { class: "panel-title" }, h("h2", {}, "Ваши серверы"), h("span", { class: "count-pill" }, state.servers.length)), h("p", { class: "panel-subtitle" }, "Реальное состояние инфраструктуры")), h("div", { class: "panel-tools" }, h("label", { class: "search-wrap" }, icon("search"), search))), h("div", { id: "server-table" }));
    fragment.append(panel, h("div", { class: "lower-grid" }, renderProbes(), renderRecentActivity()));
    queueMicrotask(renderServerRows);
    return fragment;
  }
  function renderServerRows() {
    const container = $("#server-table"); if (!container) return;
    const query = state.search.trim().toLocaleLowerCase("ru");
    const servers = state.servers.filter(server => [server.name, server.domain, server.ip, server.ip_address, server.ipv4, server.country, server.location].join(" ").toLocaleLowerCase("ru").includes(query));
    if (!state.servers.length) return container.replaceChildren(empty("Здесь начнётся ваш флот", "Добавьте первый сервер из актуального каталога провайдера. До разрешения финансовых операций создание будет заблокировано.", button("Добавить сервер", "plus", openCreate, "primary")));
    if (!servers.length) return container.replaceChildren(empty("Сервер не найден", "Попробуйте другое имя, IP-адрес или расположение.", button("Сбросить поиск", "close", () => { state.search = ""; render(); }), "search"));
    const table = h("table", {}, h("thead", {}, h("tr", {}, ...["Сервер", "Расположение", "Доступность", "Режим", "Последняя проверка", ""].map((label, index) => h("th", { scope: "col", ...(index === 5 ? { "aria-label": "Действия" } : {}) }, label)))), h("tbody", {}, ...servers.map(server => {
      const serverState = server.enabled === false ? "disabled" : first(server.health, server.status);
      const id = server.id;
      return h("tr", {},
        h("td", {}, h("div", { class: "server-cell" }, h("span", { class: "server-glyph" }, icon("server")), h("div", {}, h("div", { class: "cell-title" }, textValue(server.name, `Сервер ${id}`)), h("div", { class: "cell-subtitle mono" }, textValue(first(server.ip, server.ip_address, server.ipv4, server.domain), "IP ещё не назначен"))))),
        h("td", {}, h("div", { class: "cell-title" }, textValue(typeof server.location === "object" ? first(server.location.name, server.location.id) : first(server.location, server.country), "Не указано")), h("div", { class: "cell-subtitle" }, textValue(first(server.provider, server.provider_name), "Провайдер"))),
        h("td", {}, badge(serverState)),
        h("td", {}, h("span", { class: "mode" }, icon(server.mode === "auto" ? "activity" : server.mode === "approval" ? "shield" : "eye"), modeLabel(server.mode))),
        h("td", {}, h("span", { class: "cell-subtitle" }, date(first(server.last_check_at, server.last_check)))),
        h("td", {}, h("div", { class: "row-actions" }, iconButton("Проверить доступность", "refresh", event => serverAction(server, "check", event.currentTarget)), iconButton("Настроить сервер", "settings", () => openServer(server)))));
    })));
    container.replaceChildren(h("div", { class: "table-wrap" }, table), h("div", { class: "table-footer" }, h("span", {}, `Показано ${servers.length} из ${state.servers.length}`), h("span", {}, "SSH-доступность не подтверждает работу VPN")));
  }
  function probeStatus(kind) {
    const root = state.overview.probes || state.overview.monitoring || state.overview.readiness?.probes || {};
    const found = first(root[kind], kind === "ru" ? first(root.russia, root.ru_probe, state.overview.ru_probe) : first(root.outside, root.external_probe, state.overview.external_probe));
    if (found) return typeof found === "string" ? { status: found } : found;
    const probes = state.servers.map(server => first(server.probes?.[kind], server.checks?.[kind], server[`${kind}_status`])).filter(Boolean);
    if (!probes.length) return { status: "not_configured" };
    const successes = probes.filter(item => ["healthy", "online", "ok", "passed", "success"].includes(typeof item === "object" ? item.status : item) || item.ok === true).length;
    return { status: successes === probes.length ? "healthy" : "degraded", summary: `${successes} из ${probes.length} доступно` };
  }
  function renderProbes() {
    return h("section", { class: "panel" }, h("div", { class: "panel-heading" }, h("div", {}, h("div", { class: "panel-title" }, h("h2", {}, "География проверок")), h("p", { class: "panel-subtitle" }, "Независимые точки наблюдения")), icon("globe")), h("div", { class: "health-grid" }, ...[{ key: "ru", title: "Из России", note: "Проверка с настроенной точки в РФ" }, { key: "external", title: "За пределами РФ", note: "Внешняя доступность узла" }].map(item => { const probe = probeStatus(item.key); return h("article", { class: "probe-card" }, h("div", { class: "probe-top" }, h("strong", {}, item.title), icon("globe")), badge(first(probe.status, probe.ok === true ? "healthy" : "unknown")), h("p", { class: "probe-label" }, first(probe.summary, probe.message, item.note))); })), h("div", { class: "footnote" }, icon("info"), "Результат относится только к конкретной точке проверки. Он не означает доступность из всех сетей России."));
  }
  function operationName(item) { return ({ create: "Создание сервера", create_server: "Создание сервера", replace: "Замена сервера", replace_server: "Замена сервера", check: "Проверка доступности", probe: "Проверка доступности", pause: "Приостановка наблюдения", resume: "Возобновление наблюдения", preflight: "Проверка готовности", catalog_refresh: "Обновление каталога" }[first(item.kind, item.type, item.action)] || first(item.title, item.name, "Операция")); }
  function renderRecentActivity() {
    const recent = state.operations.slice(0, 3);
    return h("section", { class: "panel" }, h("div", { class: "panel-heading" }, h("div", {}, h("div", { class: "panel-title" }, h("h2", {}, "Последние события")), h("p", { class: "panel-subtitle" }, "Прозрачный журнал действий")), button("Все операции", "arrow", () => navigate("operations"), "ghost small")), h("div", { class: "activity-list" }, ...(recent.length ? recent.map(item => h("div", { class: "activity-item" }, h("span", { class: "activity-icon" }, icon(status(item.status)[1] === "danger" ? "warning" : status(item.status)[1] === "good" ? "check" : "clock")), h("div", { class: "activity-text" }, operationName(item), item.server_name ? ` · ${item.server_name}` : "", h("small", {}, `${status(item.status)[0]} · ${date(first(item.created_at, item.started_at))}`)))) : [h("p", { class: "activity-empty" }, "Событий пока нет. Здесь появится история проверок и изменений.")])));
  }
  function renderOperations() {
    const filters = h("select", { class: "filter-select", "aria-label": "Фильтр операций", value: state.operationFilter, onchange: event => { state.operationFilter = event.target.value; render(); } }, h("option", { value: "all" }, "Все операции"), h("option", { value: "running" }, "В работе"), h("option", { value: "failed" }, "С ошибкой"), h("option", { value: "completed" }, "Завершённые"));
    filters.value = state.operationFilter;
    const filtered = state.operations.filter(item => state.operationFilter === "all" || state.operationFilter === "running" && !["complete", "completed", "success", "succeeded", "failed", "error", "cancelled"].includes(item.status) || state.operationFilter === "failed" && ["failed", "error", "blocked", "outcome-unknown", "manual-review"].includes(item.status) || state.operationFilter === "completed" && ["complete", "success", "succeeded", "completed"].includes(item.status));
    const list = h("div", { class: "operation-list" }, ...filtered.map(item => {
      let progress = typeof item.progress === "object" ? first(item.progress.percent, item.progress.percentage) : item.progress;
      progress = Number(progress);
      const hasProgress = Number.isFinite(progress) && item.progress !== null && item.progress !== undefined;
      const bar = hasProgress ? h("div", { class: "operation-progress", role: "progressbar", "aria-valuenow": Math.min(100, Math.max(0, progress)), "aria-valuemin": 0, "aria-valuemax": 100, "aria-label": "Прогресс операции" }, h("span")) : null;
      if (bar) bar.firstChild.style.width = `${Math.min(100, Math.max(0, progress))}%`;
      return h("article", { class: "operation-row" }, h("span", { class: "server-glyph" }, icon(["failed", "error"].includes(item.status) ? "warning" : "activity")), h("div", { class: "operation-main" }, h("div", { class: "operation-head" }, h("h3", {}, operationName(item)), badge(item.status)), h("div", { class: "operation-meta" }, h("span", { class: "mono" }, `#${textValue(item.id)}`), h("span", {}, first(item.server_name, item.server_id) ? `Сервер: ${first(item.server_name, item.server_id)}` : "Управление инфраструктурой"), h("span", {}, date(first(item.created_at, item.started_at)))), first(item.message, item.error, item.current_step) ? h("p", { class: `operation-summary ${item.error ? "danger-text" : ""}` }, typeof first(item.message, item.error, item.current_step) === "object" ? JSON.stringify(first(item.message, item.error, item.current_step)) : first(item.message, item.error, item.current_step)) : null, bar), button("Журнал", "chevron", () => openEvents(item), "small"));
    }));
    return h("div", {}, pageHeader("История действий", "Всё происходит прозрачно", "Этапы, результаты и ошибки каждой операции — в одном журнале.", button("Проверить готовность", "shield", event => preflight(event.currentTarget), "primary")), h("section", { class: "panel" }, h("div", { class: "panel-heading" }, h("div", {}, h("div", { class: "panel-title" }, h("h2", {}, "Журнал операций"), h("span", { class: "count-pill" }, state.operations.length)), h("p", { class: "panel-subtitle" }, "Автоматически обновляется каждые 10 секунд")), filters), filtered.length ? list : empty(state.operations.length ? "Нет подходящих операций" : "Журнал пока пуст", state.operations.length ? "Измените фильтр, чтобы увидеть другие операции." : "Проверки, создание и замена серверов будут записаны здесь вместе с результатом каждого этапа.", state.operations.length ? button("Показать все", "refresh", () => { state.operationFilter = "all"; render(); }) : null, "activity")));
  }
  function field(label, name, value, options = {}) {
    const id = `field-${name}`;
    let control;
    if (options.options) { control = h("select", { id, name, required: options.required, disabled: options.disabled }, ...options.options.map(item => h("option", { value: item.value }, item.label))); control.value = textValue(value, ""); }
    else control = h("input", { id, name, type: options.type || "text", value: textValue(value, ""), placeholder: options.placeholder, required: options.required, disabled: options.disabled, min: options.min, max: options.max, step: options.step, autocomplete: options.autocomplete || "off", inputMode: options.inputMode });
    if (options.help) control.setAttribute("aria-describedby", `${id}-help`);
    return h("div", { class: `field ${options.full ? "full" : ""}` }, h("label", { htmlFor: id }, label), control, options.help ? h("span", { class: "field-help", id: `${id}-help` }, options.help) : null);
  }
  function settingPanel(title, subtitle, content, iconName = null) { return h("section", { class: "panel" }, h("div", { class: "panel-heading" }, h("div", {}, h("div", { class: "panel-title" }, h("h2", {}, title)), h("p", { class: "panel-subtitle" }, subtitle)), iconName ? icon(iconName) : null), h("div", { class: "panel-content" }, content)); }
  function credentialPresent(key) {
    const settings = state.settings;
    const creds = first(settings.credentials, state.overview.credentials, state.overview.readiness?.credentials, {});
    const aliases = { provider: ["provider", "provider_token", "provider_configured", "provider_token_set"], cloudflare: ["cloudflare", "cloudflare_token", "cloudflare_configured", "cloudflare_token_set"], remnawave: ["remnawave", "remnawave_token", "remnawave_configured", "remnawave_token_set"] };
    for (const alias of aliases[key]) {
      const value = first(creds[alias], settings[`${alias}_configured`], settings[`${alias}_present`], settings[alias]);
      if (typeof value === "boolean") return value;
      if (value && typeof value === "object" && typeof first(value.configured, value.present) === "boolean") return first(value.configured, value.present);
    }
    return false;
  }
  function normalizeSettings(value) { const settings = value?.settings || value || {}; return { ...settings, waiver_accepted: first(settings.withdrawal_waiver_accepted, settings.waiver_accepted, false), grace_period: first(settings.grace_seconds, settings.grace_period) }; }
  function renderSettings() {
    const settings = state.settings;
    const credentialForm = h("form", { onsubmit: saveCredentials });
    for (const provider of [{ key: "provider", label: "Облачный провайдер", token: "provider_token", hint: "Токен API для каталога, баланса и управления серверами" }, { key: "cloudflare", label: "Cloudflare", token: "cloudflare_token", hint: "Токен API для DNS-записей домена" }, { key: "remnawave", label: "Remnawave", token: "remnawave_token", hint: "Токен для регистрации VPN-узлов и сквадов" }]) {
      const present = credentialPresent(provider.key);
      credentialForm.append(h("div", { class: "credential-group" }, h("div", { class: "credential-heading" }, h("h3", {}, provider.label), badge(present ? "ready" : "not_configured", present ? "Ключ сохранён" : "Ключ не задан")), field("API-токен", provider.token, "", { type: "password", autocomplete: "new-password", placeholder: present ? "Оставьте пустым, чтобы сохранить текущий" : "Введите токен", help: provider.hint }), provider.key === "provider" ? field("ID проекта", "project_id", first(settings.project_id, settings.provider_project_id, settings.credentials?.project_id, state.overview.credentials?.project_id), { help: "Укажите проект провайдера, в котором будут создаваться узлы.", full: true }) : null, provider.key === "remnawave" ? field("Адрес панели", "remnawave_url", first(settings.remnawave_url, settings.credentials?.remnawave_url, state.overview.credentials?.remnawave_url), { type: "url", placeholder: "https://panel.example.com", help: "Полный HTTPS-адрес вашей панели Remnawave." }) : null));
    }
    credentialForm.append(h("p", { class: "subtle-note" }, "Секреты доступны только для записи. Сохранённые значения не передаются в браузер; пустое поле не меняет текущий токен."), h("div", { class: "form-actions" }, button("Сохранить подключения", "key", null, "primary", { type: "submit" })));
    const financeForm = h("form", { onsubmit: saveFinancial });
    financeForm.append(h("div", { class: "safety-card" }, h("h3", {}, icon("shield"), "Сначала ограничения, затем расходы"), h("p", {}, "Платное создание и замена разрешаются только при заданных лимитах и явных согласиях. Баланс провайдера сам по себе не даёт разрешение на расходы.")), h("div", { class: "form-grid" }, field("Дневной бюджет", "daily_budget", settings.daily_budget, { type: "number", min: "0.01", step: "0.01", placeholder: "Не задан", help: "Максимум новых затрат за сутки.", required: true }), field("Максимальная цена в час", "max_hourly", first(settings.max_hourly, settings.max_hourly_price), { type: "number", min: "0.0001", step: "0.0001", placeholder: "Не задан", help: "Предельная стоимость одного узла.", required: true }), field("Валюта лимитов", "currency", settings.currency || "RUB", { options: [{ value: "RUB", label: "RUB — российский рубль" }, { value: "USD", label: "USD — доллар США" }, { value: "EUR", label: "EUR — евро" }], help: "Должна совпадать с валютой каталога." }), field("Попыток на операцию", "max_attempts", first(settings.max_attempts, 3), { type: "number", min: "1", max: "20", step: "1", required: true }), field("Максимум кандидатов", "max_candidates", first(settings.max_candidates, 3), { type: "number", min: "1", max: "20", step: "1", help: "Ограничение подбора при замене.", required: true })), h("div", { class: "form-divider" }), h("div", { class: "form-stack" }, check("terms_accepted", settings.terms_accepted, "Подтверждаю право управлять этими ресурсами и принимаю условия провайдера."), check("waiver_accepted", settings.waiver_accepted, "Понимаю: создание и замена оплачиваются провайдеру, а доступность из конкретной сети не гарантируется.")), h("label", { class: "toggle-line" }, h("span", {}, h("strong", {}, "Разрешить финансовые операции"), h("small", {}, "Отдельное явное разрешение на платные действия")), h("input", { type: "checkbox", class: "switch", name: "financial_enabled", checked: Boolean(settings.financial_enabled), "aria-label": "Разрешить финансовые операции" })), h("div", { class: "form-actions" }, button("Сохранить ограничения", "shield", null, "primary", { type: "submit" })));
    $(".form-grid", financeForm).append(field("Минимальный остаток", "minimum_balance", settings.minimum_balance, { type: "number", min: "0", step: "0.01", help: "Не расходовать средства ниже этого баланса." }));
    $("[name=max_attempts]", financeForm).max = "10"; $("[name=max_candidates]", financeForm).max = "10";
    $("[name=daily_budget]", financeForm).min = "0"; $("[name=max_hourly]", financeForm).min = "0";
    financeForm.insertBefore(h("div", { class: "form-stack" }, notice("Бесплатная смена IP не автоматизирована", "У Voxa нет документированного API бесплатной смены IP. Замена создаёт новый платный ресурс. Ниже можно отдельно разрешить сценарий без попытки смены IP.", "warning"), check("auto_enabled", settings.auto_enabled, "Разрешаю автоматические платные действия для серверов в режиме «Автопилот» в рамках лимитов."), check("skip_ip_change_approved", settings.skip_ip_change_approved, "Разрешаю сценарий без предварительной смены IP, если смена IP недоступна у провайдера.")), $(".form-actions", financeForm));
    $("[name=terms_accepted]", financeForm).nextSibling.replaceChildren("Подтверждаю право управлять этими ресурсами и принимаю ", h("a", { href: "https://voxa.host/terms", target: "_blank", rel: "noopener noreferrer" }, "условия провайдера"), ".");
    $("[name=waiver_accepted]", financeForm).nextSibling.textContent = "Необязательно: явно соглашаюсь с немедленным началом услуги и отказом от права отзыва (withdrawal waiver), если этого требует провайдер. Включайте только после ознакомления с условиями.";
    const monitorForm = h("form", { onsubmit: saveMonitoring }, h("div", { class: "form-grid" }, field("Интервал проверок, сек.", "probe_interval", first(settings.probe_interval, 60), { type: "number", min: "5", max: "86400", step: "1", required: true }), field("Сбоев подряд до реакции", "consecutive_failures", first(settings.consecutive_failures, 3), { type: "number", min: "1", max: "100", step: "1", required: true }), field("Ожидание после создания, сек.", "grace_period", first(settings.grace_period, 180), { type: "number", min: "0", max: "86400", step: "1", required: true, full: true, help: "Даёт новому узлу время запуститься до первой реакции на сбой." })), h("div", { class: "form-actions" }, button("Сохранить мониторинг", "check", null, "primary", { type: "submit" })));
    return h("div", {}, pageHeader("Параметры контура", "Безопасность по умолчанию", "Подключения, финансовые ограничения и правила наблюдения.", button("Проверить готовность", "shield", event => preflight(event.currentTarget), "primary")), h("div", { class: "settings-grid" }, h("div", {}, settingPanel("Подключения", "Только ваши API. Секреты не отображаются после сохранения.", credentialForm, "key"), settingPanel("Мониторинг", "Правила проверки доступности узлов", monitorForm, "activity")), h("div", {}, settingPanel("Бюджет и разрешения", "Платные действия по умолчанию запрещены", financeForm, "shield"), settingPanel("Готовность к работе", "Предварительная проверка подключений и ограничений", renderReadiness(), "check"), notice("Что подтверждает проверка", "Открытый SSH-порт доказывает только доступность SSH. Для оценки VPN нужны отдельные протокольные проверки из РФ и извне. Одна точка в РФ не представляет все российские сети."))));
  }
  function check(name, checked, label) { return h("label", { class: "check-row" }, h("input", { type: "checkbox", name, checked: Boolean(checked) }), h("span", {}, label)); }
  function renderReadiness() {
    const readiness = state.overview.readiness || {};
    const checks = asArray(readiness.checks);
    if (checks.length) return h("div", { class: "readiness-list" }, ...checks.map(item => { const good = first(item.ok, item.ready, ["ok", "passed", "ready"].includes(item.status)); return h("div", { class: `readiness-item ${good ? "good" : ""}` }, icon(good ? "check" : "info"), h("div", {}, h("strong", {}, first(item.label, item.name, item.key, "Проверка")), textValue(first(item.message, item.detail), good ? "Готово" : "Требуется настройка"))); }));
    const items = [{ label: "Облачный провайдер", good: credentialPresent("provider"), text: "Каталог, баланс и управление серверами" }, { label: "Cloudflare", good: credentialPresent("cloudflare"), text: "DNS-записи для доменов узлов" }, { label: "Remnawave", good: credentialPresent("remnawave"), text: "VPN-конфигурация и регистрация узлов" }, { label: "Финансовое разрешение", good: Boolean(state.settings.financial_enabled), text: state.settings.financial_enabled ? "Разрешение сохранено; лимиты проверяются перед действием" : "Платные действия закрыты" }];
    return h("div", { class: "readiness-list" }, ...items.map(item => h("div", { class: `readiness-item ${item.good ? "good" : ""}` }, icon(item.good ? "check" : "info"), h("div", {}, h("strong", {}, item.label), item.text))));
  }
  async function saveCredentials(event) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); const body = {};
    for (const key of ["provider_token", "cloudflare_token", "remnawave_token", "remnawave_url", "project_id"]) { const value = String(data.get(key) || "").trim(); if (value) body[key] = value; }
    if (!Object.keys(body).length) return toast("Введите новые данные подключения.");
    await busy($("[type=submit]", form), async () => { await api("/api/credentials", { method: "POST", body }); form.querySelectorAll("input[type=password]").forEach(input => input.value = ""); await loadOverview(false); state.settings = normalizeSettings(await api("/api/settings")); state.catalog = null; render(); toast("Подключения сохранены. Проверьте готовность перед запуском."); });
  }
  async function saveFinancial(event) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    const body = { daily_budget: String(data.get("daily_budget") || ""), max_hourly: String(data.get("max_hourly") || ""), minimum_balance: String(data.get("minimum_balance") || "0"), max_attempts: Number(data.get("max_attempts")), max_candidates: Number(data.get("max_candidates")), currency: String(data.get("currency")), terms_accepted: data.has("terms_accepted"), withdrawal_waiver_accepted: data.has("waiver_accepted"), financial_enabled: data.has("financial_enabled"), auto_enabled: data.has("auto_enabled"), skip_ip_change_approved: data.has("skip_ip_change_approved") };
    if (body.financial_enabled && (!body.terms_accepted || Number(body.daily_budget) <= 0 || Number(body.max_hourly) <= 0)) return toast("Для разрешения расходов задайте положительные лимиты и примите условия провайдера.", true);
    if (body.auto_enabled && !body.financial_enabled) return toast("Для автопилота сначала разрешите финансовые операции.", true);
    await saveSettings(form, body, "Ограничения и разрешения сохранены");
  }
  async function saveMonitoring(event) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    await saveSettings(form, { probe_interval: Number(data.get("probe_interval")), consecutive_failures: Number(data.get("consecutive_failures")), grace_seconds: Number(data.get("grace_period")) }, "Правила мониторинга сохранены");
  }
  async function saveSettings(form, body, message) { await busy($("[type=submit]", form), async () => { await api("/api/settings", { method: "PUT", body }); await loadOverview(false); state.settings = normalizeSettings(await api("/api/settings")); render(); toast(message); }); }
  async function preflight(node) { await busy(node, async () => { const result = await api("/api/preflight", { method: "POST", body: {} }); await loadOverview(false); if (!dialog.open) render(); toast(first(result.message, result.ready ? "Контур готов к работе" : "Проверка выполнена. Результат — в настройках.")); }); }
  function openDialog(title, description, content) {
    dialog.replaceChildren(h("div", { class: "dialog-header" }, h("div", {}, h("h2", {}, title), description ? h("p", {}, description) : null), iconButton("Закрыть окно", "close", () => dialog.close())), h("div", { class: "dialog-content" }, content));
    if (!dialog.open) dialog.showModal();
  }
  function productLabel(product) {
    const name = first(product.name, product.title, product.slug, product.id);
    const price = first(product.hourly_price, product.price_hourly, product.pricing?.hourly, product.price?.hourly, product.monthly_price, product.price_monthly, typeof product.price !== "object" ? product.price : undefined, product.pricing?.monthly);
    const period = first(product.hourly_price, product.price_hourly, product.pricing?.hourly, product.price?.hourly) !== undefined ? "/ч" : first(product.monthly_price, product.price_monthly, product.pricing?.monthly) !== undefined ? "/мес" : "";
    return `${name}${price !== undefined ? ` · ${money(typeof price === "object" ? first(price.amount, price.value) : price, first(product.currency, product.pricing?.currency, state.catalog?.currency))}${period}` : ""}`;
  }
  async function openCreate() {
    openDialog("Добавить сервер", "Тарифы и расположения загружаются из API провайдера. Создание является платной операцией.", h("div", { class: "loading-inline" }, "Загружаем актуальный каталог…"));
    try { state.catalog = await api("/api/catalog"); if (dialog.open) renderCreate(); }
    catch (error) { if (dialog.open) openDialog("Каталог пока недоступен", "Для создания нужен актуальный ответ провайдера.", h("div", {}, notice("Не удалось загрузить тарифы", error.message, "error"), h("div", { class: "form-actions" }, button("Открыть настройки", "settings", () => { dialog.close(); navigate("settings"); }), button("Повторить", "refresh", openCreate, "primary")))); }
  }
  function renderCreate() {
    const catalog = state.catalog || {};
    const products = asArray(first(catalog.products, catalog.plans, catalog.items));
    if (!products.length) return openDialog("Каталог пуст", "Сервер нельзя создать без доступного тарифа провайдера.", h("div", {}, notice("Тарифы не получены", first(catalog.message, catalog.error, "Проверьте токен, проект и результат обновления каталога."), "warning"), h("div", { class: "form-actions" }, button("Настройки", "settings", () => { dialog.close(); navigate("settings"); }), button("Обновить каталог", "refresh", event => busy(event.currentTarget, async () => { await api("/api/catalog/refresh", { method: "POST", body: {} }); await openCreate(); }), "primary"))));
    const form = h("form", { onsubmit: createServer });
    const productField = field("Тариф провайдера", "product_id", "", { required: true, options: [{ value: "", label: "Выберите актуальный тариф" }, ...products.map(product => ({ value: String(product.id), label: productLabel(product) }))], full: true });
    const locationField = field("Расположение", "location", "", { required: true, options: [{ value: "", label: "Сначала выберите тариф" }] });
    const summary = h("div", { class: "catalog-summary", hidden: true });
    const locationSelect = $("select", locationField);
    $("select", productField).addEventListener("change", event => {
      const product = products.find(item => String(item.id) === event.target.value);
      const rawLocations = first(product?.locations, product?.available_locations, catalog.locations);
      const locations = asArray(rawLocations);
      locationSelect.replaceChildren(h("option", { value: "" }, locations.length ? "Выберите расположение" : "Нет доступных расположений"), ...locations.map(location => typeof location === "object" ? h("option", { value: String(first(location.id, location.code, location.name)) }, textValue(first(location.name, location.title, location.code, location.id)) + (location.country ? ` · ${location.country}` : "")) : h("option", { value: String(location) }, location)));
      summary.hidden = !product;
      summary.textContent = product ? [productLabel(product), product.cpu ? `${product.cpu} vCPU` : "", first(product.ram, product.memory) ? `RAM: ${first(product.ram, product.memory)}` : "", first(product.disk, product.storage) ? `Диск: ${first(product.disk, product.storage)}` : "", "Итоговую стоимость и лимиты проверяет сервер перед созданием."].filter(Boolean).join(" · ") : "";
    });
    const squads = asArray(first(catalog.squads, state.overview.squads, state.settings.squads));
    let squadField;
    if (squads.length) {
      const select = h("select", { name: "squads", id: "field-squads", multiple: true, size: Math.min(squads.length, 4), required: true }, ...squads.map(squad => h("option", { value: String(first(squad.uuid, squad.id)) }, textValue(first(squad.name, squad.title, squad.uuid, squad.id)))));
      squadField = h("div", { class: "field full" }, h("label", { htmlFor: "field-squads" }, "Сквады Remnawave"), select, h("span", { class: "field-help" }, "Можно выбрать несколько сквадов с Ctrl / ⌘."));
    } else squadField = field("UUID сквадов Remnawave", "squads", "", { full: true, required: true, placeholder: "UUID, через запятую", help: "Список сквадов не получен. Укажите существующие UUID из вашей панели; новые сквады не создаются." });
    form.append(h("div", { class: "form-grid" }, field("Название сервера", "name", "", { required: true, placeholder: "Например, eu-primary" }), field("Домен узла", "domain", "", { required: true, placeholder: "node.example.com", help: "Домен из зоны, доступной токену Cloudflare." }), productField, locationField, field("Режим управления", "mode", "observe", { options: [{ value: "observe", label: "Наблюдение — без автозамены" }, { value: "approval", label: "Замена с подтверждением" }, { value: "auto", label: "Автопилот в пределах бюджета" }] }), squadField), summary, h("div", { class: "safety-card" }, h("h3", {}, icon("wallet"), "Платное создание ресурса"), h("p", {}, "Провайдер начнёт тарификацию после создания. Наблюдение не отменяет плату за сам сервер. Действуют сохранённый бюджет и ограничения.")), h("div", { class: "space-top" }, check("confirm", false, "Подтверждаю создание платного сервера по выбранному тарифу.")), h("div", { class: "form-actions" }, button("Отмена", null, () => dialog.close()), button("Создать сервер", "plus", null, "primary", { type: "submit" })));
    if (!state.settings.financial_enabled) {
      form.prepend(notice("Платные действия выключены", "Сначала задайте бюджет, согласия и разрешение в настройках. Запрос не обходит эту защиту.", "warning"));
      $("[type=submit]", form).disabled = true;
      $(".form-actions", form).prepend(button("Настройки бюджета", "settings", () => { dialog.close(); navigate("settings"); }));
    }
    openDialog("Добавить сервер", "Реальные тарифы провайдера. Бюджет проверяется до создания.", form);
  }
  async function createServer(event) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    if (!data.has("confirm")) return toast("Подтвердите платное создание сервера.", true);
    const rawSquads = data.getAll("squads").flatMap(value => String(value).split(",")).map(value => value.trim()).filter(Boolean);
    const location = String(data.get("location"));
    const locationRecord = asArray(state.catalog?.locations).find(item => typeof item === "object" && String(first(item.id, item.code, item.name)) === location);
    const body = { name: String(data.get("name")).trim(), domain: String(data.get("domain")).trim(), product_id: String(data.get("product_id")), location, mode: String(data.get("mode")), squads: rawSquads, confirm: true, ...(locationRecord?.country ? { country: locationRecord.country } : {}) };
    form.dataset.requestId ||= requestId();
    await busy($("[type=submit]", form), async () => { await api("/api/servers", { method: "POST", body, requestId: form.dataset.requestId }); dialog.close(); await loadOverview(false); navigate("operations"); toast("Создание поставлено в очередь. Этапы появятся в журнале."); });
  }
  async function serverAction(server, action, node, extra = {}) {
    return busy(node, async () => { await api(`/api/servers/${encodeURIComponent(server.id)}/actions/${action}`, { method: "POST", body: extra }); if (dialog.open) dialog.close(); await loadOverview(false); render(); toast(action === "check" ? "Проверка поставлена в очередь" : action === "pause" ? "Наблюдение приостановлено" : action === "resume" ? "Наблюдение возобновлено" : "Замена поставлена в очередь"); });
  }
  function openServer(server) {
    const form = h("form", { onsubmit: async event => { event.preventDefault(); const data = new FormData(form); await busy($("[type=submit]", form), async () => { await api(`/api/servers/${encodeURIComponent(server.id)}`, { method: "PATCH", body: { mode: String(data.get("mode")) } }); dialog.close(); await loadOverview(false); render(); toast("Режим сервера сохранён"); }); } }, h("div", { class: "form-stack" }, field("Режим управления", "mode", server.mode || "observe", { options: [{ value: "observe", label: "Наблюдение" }, { value: "approval", label: "Замена с подтверждением" }, { value: "auto", label: "Автопилот" }], help: "Наблюдение не запускает замену. Автопилот выполняет платные действия только в пределах настроенных ограничений." })), h("div", { class: "form-actions" }, button("Сохранить режим", "check", null, "primary", { type: "submit" })));
    const paused = server.enabled === false || server.status === "paused";
    const content = h("div", {}, notice("Изменения не удаляют ресурсы", "Приостановка останавливает управление, но не выключает тарификацию провайдера. Замена — отдельная платная операция."), form, h("div", { class: "form-divider" }), h("div", { class: "form-actions no-margin" }, button(paused ? "Возобновить" : "Приостановить", paused ? "play" : "pause", event => serverAction(server, paused ? "resume" : "pause", event.currentTarget)), button("Проверить", "refresh", event => serverAction(server, "check", event.currentTarget)), button("Заменить сервер", "server", () => confirmReplace(server), "danger")));
    openDialog(textValue(server.name, "Настройки сервера"), [first(server.ip, server.ip_address, server.ipv4), server.domain].filter(Boolean).join(" · "), content);
  }
  function confirmReplace(server) {
    const confirmation = check("replace-confirm", false, "Подтверждаю платное создание нового ресурса для замены этого сервера.");
    openDialog("Подтвердите замену", `Сервер: ${textValue(server.name, server.id)}. Сначала будет проверен бюджет и создан новый кандидат.`, h("div", {}, notice("Замена может увеличить расходы", "Новый кандидат тарифицируется провайдером. Удаление старого узла и переключение DNS выполняются только согласно правилам серверной операции; следите за журналом.", "warning"), confirmation, h("div", { class: "form-actions" }, button("Назад", null, () => openServer(server)), button("Подтвердить замену", "refresh", event => { if (!$("input", confirmation).checked) return toast("Нужно подтвердить платную замену.", true); serverAction(server, "replace", event.currentTarget, { confirm: true }); }, "primary"))));
  }
  async function openEvents(operation) {
    openDialog(operationName(operation), `Операция #${textValue(operation.id)} · ${status(operation.status)[0]}`, h("div", { class: "loading-inline" }, "Загружаем журнал…"));
    try {
      const response = await api(`/api/events?operation_id=${encodeURIComponent(operation.id)}`);
      const events = asArray(first(response.events, response.items, Array.isArray(response) ? response : undefined));
      if (!dialog.open) return;
      openDialog(operationName(operation), `Операция #${textValue(operation.id)} · ${status(operation.status)[0]}`, h("div", {}, events.length ? h("div", { class: "event-list" }, ...events.map(event => h("div", { class: `event-row ${["error", "failed"].includes(event.level) ? "error" : ""}` }, h("time", {}, date(first(event.created_at, event.timestamp))), h("span", {}, textValue(first(event.message, event.text, event.detail)))))) : h("p", { class: "subtle-note" }, "Для этой операции ещё нет записей."), h("div", { class: "form-actions" }, button("Обновить журнал", "refresh", () => openEvents(operation)), button("Закрыть", null, () => dialog.close(), "primary"))));
    } catch (error) { if (dialog.open) openDialog("Не удалось загрузить журнал", "Операция продолжает выполняться независимо от открытого окна.", notice("Ошибка загрузки", error.message, "error")); }
  }
  function renderLogin(message = "") {
    const error = h("p", { class: "inline-error", role: "alert" }, message);
    const form = h("form", { onsubmit: async event => {
      event.preventDefault(); const submit = $("[type=submit]", form); const password = $("input", form);
      submit.disabled = true; submit.setAttribute("aria-busy", "true"); error.textContent = "";
      try { const response = await api("/api/login", { method: "POST", body: { password: password.value } }); state.csrf = response.csrf || state.csrf; state.authenticated = true; password.value = ""; try { await loadOverview(false); } catch (exception) { state.error = exception.message; } render(); }
      catch (exception) { error.textContent = exception.message || "Не удалось войти."; password.value = ""; password.focus(); }
      finally { submit.disabled = false; submit.removeAttribute("aria-busy"); }
    } }, field("Пароль администратора", "password", "", { type: "password", autocomplete: "current-password", required: true, placeholder: "Введите пароль" }), error, button("Войти в панель", "arrow", null, "primary", { type: "submit" }));
    app.replaceChildren(h("div", { class: "login-shell" }, h("aside", { class: "login-aside" }, brand(), h("div", { class: "login-intro" }, h("h1", {}, "Инфраструктура, на которую можно положиться."), h("p", {}, "Единый контур управления VPN-узлами. Наблюдение, прозрачные операции и расходы под контролем.")), h("div", { class: "login-aside-foot" }, "Наблюдать · Проверять · Управлять")), h("main", { class: "login-main" }, h("div", { class: "login-card" }, h("h2", {}, "С возвращением"), h("p", {}, "Войдите в закрытую панель для управления вашей инфраструктурой."), form, h("div", { class: "login-lock" }, icon("lock"), "Доступ только для администратора")))));
    app.removeAttribute("aria-busy");
  }
  async function logout() {
    await busy(null, async () => { await api("/api/logout", { method: "POST", body: {} }); state.authenticated = false; state.overview = {}; state.servers = []; state.operations = []; state.settings = {}; state.catalog = null; dialog.close(); const response = await api("/api/session"); state.csrf = response.csrf || ""; renderLogin(); });
  }
  async function boot() {
    const page = window.location.hash.slice(1); if (["servers", "operations", "settings"].includes(page)) state.page = page;
    try { const session = await api("/api/session"); state.csrf = session.csrf || ""; state.authenticated = Boolean(session.authenticated); if (state.authenticated) { await loadOverview(false); render(); } else renderLogin(); }
    catch (error) { if (state.authenticated) { state.error = error.message; render(); } else renderLogin(error.message); }
    window.setInterval(async () => { if (!state.authenticated || state.loading || state.page === "settings" || document.hidden || dialog.open || document.activeElement?.matches("input,select,textarea")) return; state.loading = true; try { await loadOverview(); } catch (error) { if (state.authenticated) { state.error = error.message; render(); } } finally { state.loading = false; } }, 10000);
  }
  window.addEventListener("hashchange", () => { const page = window.location.hash.slice(1); if (["servers", "operations", "settings"].includes(page) && page !== state.page) { state.page = page; render(); } });
  document.addEventListener("click", event => { if (state.menuOpen && !event.target.closest(".sidebar,.mobile-menu")) { state.menuOpen = false; $(".shell")?.classList.remove("menu-open"); } });
  dialog.addEventListener("click", event => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
  boot();
})();
