/* Partner cabinet: owner-scoped setup, entry onboarding and read-only sales reports. */
(() => {
  'use strict';

  const workspaceMatch = location.pathname.match(/^\/partners\/workspace\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\/?$/i);
  const workspaceMode = !!workspaceMatch;
  const API = workspaceMode ? `/partners/workspace/${workspaceMatch[1]}/api` : '/partners/api';
  const content = document.getElementById('content');
  const localhost = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
  const preview = localhost && new URLSearchParams(location.search).get('preview') === '1';
  const state = {view: workspaceMode ? 'overview' : 'enrollment', creatorStep: 'intro', projects: [], selected: null, settings: null, catalog: null, busy: false, createKey: null, createFingerprint: null, checkoutKey: null, checkoutCanceledOrderId: null, checkoutError: null, initData: '', ready: false, reports: new Map(), period: 'today', entries: new Map(), entryKeys: new Map()};
  const ENTRY_FEE = 999000;
  const PILOT_FEE = 1000;
  const PILOT_OWNER = 6910097562;
  const validEntryFee = value => Number.isInteger(value) && [ENTRY_FEE, PILOT_FEE].includes(value);
  const discountedEntry = value => value?.public_entry_trial === true || value?.owner_entry_pilot === true && state.settings?.actor_id === PILOT_OWNER;
  const currentEntryFee = () => discountedEntry(state.settings) && state.settings?.entry_fee_kopecks === PILOT_FEE ? PILOT_FEE : ENTRY_FEE;
  const feeLabel = value => value === ENTRY_FEE ? '9 990 ₽' : '10 ₽';
  let toastTimer;
  let entryPollTimer, entryPollBusy = false, entryPollFailures = 0, entryPollStopped = false;
  let managedCreation = null, managedPollTimer, managedPollBusy = false, managedPollFailures = 0;

  function element(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
      else if (key === 'value') node.value = value;
      else node.setAttribute(key, value === true ? '' : String(value));
    }
    for (const child of [].concat(children)) if (child != null) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    return node;
  }
  const p = (text, className = '') => element('p', {text, class: className});
  const button = (text, onClick, className = '') => element('button', {type: 'button', text, class: `button ${className}`, onclick: onClick});
  const money = value => new Intl.NumberFormat('ru-RU', {style: 'currency', currency: 'RUB', minimumFractionDigits: 0, maximumFractionDigits: 2}).format(Number(value));
  const date = value => value && Number.isFinite(new Date(value).getTime()) ? new Intl.DateTimeFormat('ru-RU', {timeZone: 'UTC', day: '2-digit', month: '2-digit', year: 'numeric'}).format(new Date(value)) : 'Дата не указана';
  const projectId = project => String(project?.id || project?.uuid || '');
  const version = project => project?.version;
  const projectPrice = project => Number.isInteger(project?.price_kopecks) ? project.price_kopecks / 100 : undefined;
  const selectedProject = () => state.projects.find(project => projectId(project) === state.selected);
  const accent = project => /^#[a-f\d]{6}$/i.test(project?.brand?.accent_color || '') ? project.brand.accent_color : '#e6c77a';
  function paintAccent(target, value) {
    if (!target || !/^#[a-f\d]{6}$/i.test(value)) return;
    const channels = [1, 3, 5].map(offset => parseInt(value.slice(offset, offset + 2), 16) / 255).map(channel => channel <= 0.04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4);
    const luminance = channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
    target.style.setProperty('--project-accent', value);
    target.style.setProperty('--project-accent-ink', 1.05 / (luminance + .05) > (luminance + .05) / .05 ? '#ffffff' : '#000000');
  }
  const domain = project => project?.domain || project?.subscription_domain || (state.settings?.base_domain && project?.slug ? `${project.slug}.${state.settings.base_domain}` : 'Домен платформы пока не настроен');
  function normalize(project) {
    if (!project || !projectId(project)) throw new Error('invalid_response');
    return project;
  }
  function notify(text) {
    const toast = document.getElementById('toast');
    toast.textContent = text; toast.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { toast.hidden = true; }, 5000);
  }
  function heading(title, description, eyebrow = 'Партнёрский кабинет') {
    return element('div', {class: 'page-heading'}, [element('div', {}, [p(eyebrow, 'eyebrow'), element('h1', {text: title}), description ? p(description, 'page-description') : null])]);
  }
  function setView(view, focus = true) {
    if (state.busy) return;
    state.view = view;
    document.querySelectorAll('[data-view]').forEach(item => {
      const active = item.dataset.view === view;
      item.classList.toggle('active', active);
      if (active) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current');
    });
    render();
    if (focus) content.focus({preventScroll: true});
  }
  function updateProject(project) {
    project = normalize(project);
    const index = state.projects.findIndex(item => projectId(item) === projectId(project));
    if (index < 0) state.projects.push(project); else state.projects[index] = project;
    state.selected = projectId(project);
    if (workspaceMode) {
      document.querySelector('.wordmark > span').textContent = project.title || 'Мой сервис';
      document.title = 'Личный кабинет · ' + (project.title || 'Мой сервис');
    }
    return project;
  }
  class ApiError extends Error {
    constructor(status, code, publicDetail) { super(code || 'request_failed'); this.status = status; this.code = code; this.publicDetail=publicDetail; }
  }
  async function api(path, options = {}) {
    if (preview) return previewApi(path, options);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(API + path, {method: options.method || 'GET', credentials: 'same-origin', cache: 'no-store', signal: controller.signal, headers: {Authorization: `tma ${state.initData}`, ...(options.body ? {'Content-Type': 'application/json'} : {})}, ...(options.body ? {body: JSON.stringify(options.body)} : {})});
      let data;
      try { data = await response.json(); } catch { throw new ApiError(response.status, 'invalid_response'); }
      if (!response.ok) {
        const detail = data?.detail;
        throw new ApiError(response.status, typeof detail === 'object' && detail ? detail.code : data?.code,
          [400,409].includes(response.status)&&typeof detail==='string'&&detail.length<=500?detail:null);
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new ApiError(0, 'timeout');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  const messages = {
    invalid_slug: 'Адрес: 3–32 строчные латинские буквы, цифры или дефис. По краям — буква или цифра.',
    slug_taken: 'Этот адрес уже занят. Выберите другой.',
    slug_reserved: 'Этот адрес занят платформой. Выберите другой.',
    price_below_minimum: 'Минимальная цена подписки — 199 ₽.',
    invalid_price: 'Укажите цену от 199 ₽ с точностью до копеек.',
    invalid_token: 'Telegram не принял токен. Проверьте его в BotFather.',
    bot_token_invalid: 'Telegram не принял токен. Проверьте его в BotFather.',
    bot_already_connected: 'Этот бот уже подключён к другому проекту.',
    bot_in_use: 'Этот бот уже подключён к другому проекту.',
    telegram_unavailable: 'Telegram временно не отвечает. Попробуйте подключить бота позже.',
    token_encryption_unavailable: 'Подключение ботов ещё не настроено на платформе. Токен не сохранён.',
    invalid_url: 'Используйте полную ссылку https:// или ссылку Telegram.',
    disabled: 'Партнёрская платформа пока выключена.',
    platform_disabled: 'Партнёрская платформа пока выключена.',
    timeout: 'Ответ не получен вовремя. Обновите проект перед повторной попыткой.',
    invalid_response: 'Не удалось прочитать ответ сервера. Попробуйте обновить кабинет.',
    entry_payments_unavailable: 'Приём вступительного платежа пока не подключён. Деньги не списаны.',
    entry_launch_unavailable: 'Автоматический запуск пока недоступен. Если вступление уже оплачено, повторная оплата не нужна.',
    entry_price_changed: 'Стоимость вступления изменилась. Обновите страницу и проверьте условия до подтверждения.',
    bot_not_connected: 'Сначала подключите Telegram-бота в разделе «Telegram-бот».',
    insufficient_balance: 'Недостаточно доступных денег. Обновите финансы перед новой заявкой.',
    withdrawal_pending: 'Заявка уже рассматривается. Проверьте её в истории, не создавайте вторую.',
    request_conflict: 'Эта операция уже сохранена с другими параметрами. Сначала проверьте её состояние.',
    finance_conflict: 'Состояние финансов изменилось. Обновите данные и проверьте историю.',
  };
  function errorText(error) {
    if (error.status === 401 || error.status === 403) return workspaceMode ? 'Доступ не подтверждён. Откройте «Админ» в своём боте и снова нажмите «Личный кабинет».' : 'Откройте покупку заново через бота.';
    if (error.status === 409) return messages[error.code] || error.publicDetail || 'Проект уже изменился в другом окне. Загрузите актуальную версию и повторите изменения.';
    if (error.status === 400 && error.publicDetail) return error.publicDetail;
    if (error.status === 422) return 'Проверьте поля формы. Цена должна быть не ниже 199 ₽, ссылки — полными.';
    if (error.status === 429) return 'Слишком много запросов. Подождите немного и попробуйте снова.';
    if (messages[error.code || error.message]) return messages[error.code || error.message];
    return 'Не удалось выполнить действие. Данные не подменены нулями — попробуйте ещё раз.';
  }
  function errorBox(form, error, reload) {
    form.querySelector('.form-error')?.remove();
    const box = element('div', {class: 'form-error', role: 'alert'}, [p(errorText(error))]);
    if (reload) box.append(button('Загрузить актуальные данные', async () => {
      try {
        const current = await api(`/projects/${encodeURIComponent(state.selected)}`);
        updateProject(current); render(); notify('Данные обновлены. Внесите изменения ещё раз.');
      } catch (cause) { errorBox(form, cause, false); }
    }, 'quiet'));
    form.append(box);
    box.scrollIntoView({block: 'nearest', behavior: 'instant'});
  }
  function loadingForm(form, busy, label) {
    state.busy = busy;
    form.setAttribute('aria-busy', String(busy));
    form.querySelectorAll('input, button, textarea, select').forEach(item => { item.disabled = busy; });
    const submit = form.querySelector('[type="submit"]');
    if (submit) { submit.textContent = busy ? 'Сохраняем…' : label; }
    document.querySelectorAll('[data-view]').forEach(item => { item.disabled = busy; });
  }
  function field(id, title, value = '', options = {}) {
    const input = element('input', {id, name: id, value, ...options});
    const wrapper = element('div', {class: 'field'}, [element('label', {for: id, text: title}), input]);
    return {wrapper, input};
  }
  function submitButton(text) { return element('button', {type: 'submit', class: 'button primary', text}); }
  function validPrice(value) { return /^\d+(?:[.,]\d{1,2})?$/.test(value.trim()) && Number(value.replace(',', '.')) >= 199; }
  function priceString(value) { return Number(value.replace(',', '.')).toFixed(2); }
  function validUrl(value) {
    if (!value) return true;
    try { const parsed = new URL(value); return parsed.protocol === 'https:' && !parsed.username && !parsed.password; } catch { return false; }
  }
  function priceDetails(price) {
    if (!Number.isFinite(Number(price))) return p('Цена пока не получена.', 'muted fine');
    const cents = Math.round(Number(price) * 100);
    const partner = Math.round(cents * 70 / 100);
    return element('div', {class: 'split'}, [p('Расчёт с одной покупки', 'muted fine'), element('div', {class: 'definition'}, [element('span', {text: 'Партнёру · 70%'}), element('strong', {text: money(partner / 100)})]), element('div', {class: 'definition'}, [element('span', {text: 'Платформе · 30%'}), element('strong', {text: money((cents - partner) / 100)})])]);
  }
  function render() {
    if (!state.ready) return;
    content.replaceChildren();
    if (!workspaceMode) { renderCreator(); scheduleEntryCheck(); scheduleManagedCheck(); return; }
    const workspace = window.SPNPartnerWorkspace;
    const context = {state,content,element,p,button,heading,api,notify,money,date,projectId,selectedProject,setView,
      errorText,errorBox,field,submitButton,loadingForm,render,updateProject,preview,workspaceMode};
    workspace?.navigation(context);
    const current=selectedProject();
    if(current?.role&&current.role!=='owner'&&['brand','bot','launch','finance'].includes(state.view)){
      content.append(heading('Раздел доступен владельцу','Откройте раздел, разрешённый вашей роли.'),button('К данным проекта',()=>setView(workspace.defaultView(current)),'primary'));return;
    }
    if (!selectedProject()) stateScreen('Проект не загрузился', 'Переоткройте личный кабинет из своего бота.');
    else if (workspace?.render(context)) return;
    else if (state.view === 'settings') renderSettings();
    else if (state.view === 'brand') renderBrand();
    else if (state.view === 'bot') renderBot();
    else if (state.view === 'launch') renderLaunch();
    else renderReport(state.view);
  }
  function settingsLink(title, description, view) {
    const link = button(title, () => { setView(view); if (view === 'brand') void loadCatalog(state.selected); }, 'quiet settings-link');
    link.setAttribute('aria-label', title);
    return link;
  }
  function renderSettings() {
    const project = selectedProject(), canView = view => window.SPNPartnerWorkspace?.canView(project, view, state.settings?.features || {});
    content.append(heading('Настройки', 'Всё для вашего бота — в одном месте.', project.title));
    const groups = element('div', {class: 'stack'});
    if (!project.role || project.role === 'owner') groups.append(element('section', {class: 'card stack settings-group'}, [
      element('h2', {text: 'Мой сервис'}),
      settingsLink('Название, оформление и цена', 'Бренд', 'brand'),
      settingsLink('Подключение Telegram-бота', 'Бот', 'bot'),
    ]));
    const tools = [];
    if (canView('referrals')) tools.push(settingsLink('Бонусы за приглашения', '', 'referrals'));
    if (canView('marketing')) tools.push(settingsLink('Рекламные ссылки и рассылки', '', 'marketing'));
    if (tools.length) groups.append(element('section', {class: 'card stack settings-group'}, [element('h2', {text: 'Продвижение'}), ...tools]));
    if (canView('team')) groups.append(element('section', {class: 'card settings-group'}, [element('h2', {text: 'Доступ к кабинету'}), settingsLink('Сотрудники и права', '', 'team')]));
    content.append(groups);
  }
  function openOwnBot(project) {
    const bot = botInfo(project);
    if (!bot || !/^[A-Za-z0-9_]{5,32}$/.test(bot.username)) return null;
    return element('a', {class: 'button primary full', href: `https://t.me/${bot.username}`, target: '_blank', rel: 'noopener noreferrer', text: 'Перейти в моего бота'});
  }
  function renderCreator() {
    const project = selectedProject();
    if (project && ['suspended', 'closed'].includes(project.status)) {
      content.append(heading(project.status === 'closed' ? 'Сервис закрыт' : 'Сервис приостановлен', 'Обратитесь в поддержку.', 'Ваш сервис'));
      renderPreviousProjects();
      return;
    }
    if (project && (project.workspace_available || project.status === 'active')) {
      content.append(heading('Ваш сервис создан', 'Настройки и управление — в вашем боте.', 'Ваш сервис'), element('section', {class: 'card stack'}, [element('h2', {text: botInfo(project) ? `@${botInfo(project).username}` : 'Проект подключён'}), openOwnBot(project)]));
      return;
    }
    content.append(heading('Создай свой сервис под своим брендом', 'Готовый Telegram-бот с личным кабинетом и поддержкой.', 'Партнёрство SPN'));
    const entry = project ? state.entries.get(projectId(project)) : null;
    // Read an existing order before offering another purchase, including when
    // reopening after a lost response or a previously paid 10 RUB trial.
    if (project && !entry) { void loadEntry(projectId(project)); return; }
    if (entry?.status === 'loading') { content.append(element('section', {class: 'card', role: 'status', 'aria-live': 'polite', 'aria-busy': 'true'}, [element('span', {class: 'spinner', 'aria-hidden': true}), p('Проверяем оплату…')])); return; }
    if (entry?.status === 'error') { content.append(element('section', {class: 'card stack', role: 'alert'}, [p('Не удалось проверить оплату.'), p(errorText(entry.error), 'muted fine'), button('Повторить загрузку', () => loadEntry(projectId(project)), 'quiet')])); return; }
    const order = entry?.data?.order;
    if (order?.status === 'launched') {
      content.append(element('section', {class: 'card stack'}, [element('h2', {text: 'Ваш сервис создан'}), p('Управляйте им в своём боте.', 'muted'), openOwnBot(project)])); return;
    }
    if (order?.status === 'paid_pending_launch') {
      renderPaidCreator(project, entry); return;
    }
    const card = element('section', {class: 'card creator-offer'}, [
      p('Вам с каждой продажи', 'muted'), element('strong', {class: 'creator-share', text: '70%'}),
      p('30% — платформе и на обслуживание.', 'muted fine'),
      element('div', {class: 'entry-status'}, [p('Подключение один раз', 'muted fine'), element('h2', {class: 'entry-price', text: feeLabel(ENTRY_FEE)})]),
    ]);
    const savedUrl = order?.status === 'created' && safeCheckoutUrl(order.confirmation_url) ? order.confirmation_url : null;
    if (savedUrl) {
      card.append(element('a', {class: 'button primary full', href: savedUrl, target: '_blank', rel: 'noopener noreferrer', text: 'Продолжить оплату', onclick: event => {
        if (typeof window.Telegram?.WebApp?.openLink === 'function') { event.preventDefault(); openPayment(savedUrl); }
      }}), p('Проверяем оплату автоматически.', 'hint'));
      if (order.price_kopecks !== ENTRY_FEE) card.append(p(`Сохранённый счёт: ${feeLabel(order.price_kopecks)}.`, 'muted fine'));
    } else {
      const pay = button(state.busy ? 'Открываем оплату…' : `Оплатить ${feeLabel(ENTRY_FEE)}`, () => beginCheckout(), 'primary full');
      pay.disabled = state.busy || preview || state.settings?.pay_first_enabled !== true || state.settings?.entry_fee_kopecks !== ENTRY_FEE || entry?.data?.payments_available === false;
      card.append(pay);
      if (preview) card.append(p('Предпросмотр — без оплаты.', 'hint'));
      else if (state.settings?.pay_first_enabled !== true || state.settings?.entry_fee_kopecks !== ENTRY_FEE || entry?.data?.payments_available === false) card.append(p('Оплата временно недоступна.', 'hint'));
    }
    if (order?.status === 'canceled') card.append(p('Предыдущая оплата отменена.', 'hint'));
    if (state.checkoutError) card.append(element('div', {class: 'form-error', role: 'alert'}, [p(errorText(state.checkoutError)), p('Повторная попытка проверит сохранённый заказ.', 'fine')]));
    if (state.busy) card.append(p('Подождите…', 'hint'));
    card.append(element('details', {class: 'creator-details'}, [element('summary', {text: 'Как это работает'}),
      p('Оплатите подключение, создайте бота и настройте его под свой бренд.'),
      p('Вы получаете 70% с оплаченных продаж. Вывод — СБП/USDT от 1 500 ₽.'),
    ]));
    content.append(card);
    renderPreviousProjects();
  }
  function renderPreviousProjects() {
    if (state.projects.length <= 1) return;
    const previous = element('details', {class: 'creator-details'}, [element('summary', {text: 'Мои сервисы'})]);
    for (const item of state.projects) previous.append(button(botInfo(item) ? `@${botInfo(item).username}` : 'Незавершённое подключение', () => { if (state.busy) return; state.selected = projectId(item); managedCreation = null; state.checkoutError = null; render(); }, 'quiet full'));
    content.append(previous);
  }
  function checkoutStorageKey() {
    const actor = state.settings?.actor_id;
    return Number.isSafeInteger(actor) && actor > 0 ? `spn-partner-enrollment:${actor}` : null;
  }
  function loadCheckoutKey() {
    try {
      const saved = localStorage.getItem(checkoutStorageKey());
      return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(saved || '') ? saved : null;
    } catch { return null; }
  }
  function rememberCheckoutKey(value) {
    state.checkoutKey = value;
    try {
      const key = checkoutStorageKey();
      if (!key) return;
      if (value) localStorage.setItem(key, value); else localStorage.removeItem(key);
    } catch { /* The server also recovers the actor's existing saved order. */ }
  }
  function openPayment(url) {
    if (!safeCheckoutUrl(url)) return false;
    try {
      const app = window.Telegram?.WebApp;
      if (typeof app?.openLink === 'function') { app.openLink(url); return true; }
      const opened = window.open(url, '_blank', 'noopener,noreferrer');
      if (opened) { opened.opener = null; return true; }
    } catch { /* The saved invoice remains available through the fallback. */ }
    notify('Нажмите «Продолжить оплату», если страница не открылась.');
    return false;
  }
  async function beginCheckout() {
    if (workspaceMode || preview || state.busy || state.settings?.pay_first_enabled !== true || state.settings?.entry_fee_kopecks !== ENTRY_FEE) return;
    const existing = state.entries.get(state.selected)?.data?.order;
    if (existing && ['paid_pending_launch', 'launched'].includes(existing.status)) { render(); return; }
    // Persist only the opaque UUID, bound to the verified actor, before I/O.
    // Retrying a timeout or reopening never changes this request's payload.
    if (existing?.status === 'canceled' && state.checkoutCanceledOrderId !== existing.id) {
      state.checkoutCanceledOrderId = existing.id;
      rememberCheckoutKey(null);
    }
    if (!state.checkoutKey) rememberCheckoutKey(crypto.randomUUID());
    state.busy = true; state.checkoutError = null; render();
    try {
      const result = await api('/enrollment/checkout', {method: 'POST', body: {request_key: state.checkoutKey, expected_fee_kopecks: ENTRY_FEE}});
      if (!validEntry(result) || !result.project) throw new ApiError(0, 'invalid_response');
      const project = normalize(result.project);
      updateProject(project); state.entries.set(projectId(project), entryRecord(result));
      entryPollStopped = false; entryPollFailures = 0;
      if (['paid_pending_launch', 'launched'].includes(result.order?.status)) rememberCheckoutKey(null);
      if (result.order?.status === 'created' && safeCheckoutUrl(result.order.confirmation_url)) openPayment(result.order.confirmation_url);
    } catch (error) { state.checkoutError = error; }
    finally { state.busy = false; render(); }
  }
  function renderPaidCreator(project, entry) {
    const card = element('section', {class: 'card stack'}, [
      element('h2', {text: 'Оплата прошла', role: 'status', 'aria-live': 'polite'}),
      p('Теперь создайте свой сервис.', 'muted'),
    ]);
    rememberCheckoutKey(null);
    if (!botInfo(project)) renderManagedConnect(card, project);
    else if (entry.needsReplace) {
      const replace = element('input', {type: 'checkbox', id: 'replace-webhook'});
      const launch = button('Подключить сервис', () => entryAction(projectId(project), 'launch', replace.checked), 'primary full');
      launch.disabled = true;
      replace.addEventListener('change', () => { launch.disabled = !replace.checked || state.busy; });
      card.append(p('Бот уже подключён к другому обработчику.', 'hint'), element('label', {class: 'confirmation-check', for: 'replace-webhook'}, [replace, element('span', {text: 'Заменить прежнее подключение'})]), launch);
    } else {
      if (!entry.data.launch_available) card.append(p('Оплата сохранена. Подключение временно недоступно — напишите в поддержку.', 'hint'));
      else if (entry.launchState === 'retry') {
        const retry = button(state.busy ? 'Подключаем…' : 'Повторить подключение', () => entryAction(projectId(project), 'launch'), 'primary full');
        retry.disabled = preview || state.busy;
        card.append(p('Не удалось завершить подключение. Повторная оплата не нужна.', 'hint'), retry);
      } else card.append(element('div', {role: 'status', 'aria-live': 'polite', 'aria-busy': 'true'}, [element('span', {class: 'spinner', 'aria-hidden': true}), p('Подключаем сервис…')]));
    }
    if (entry.error) card.append(element('div', {class: 'form-error', role: 'alert'}, [p(errorText(entry.error))]));
    content.append(card); renderPreviousProjects();
  }
  function safeCreationUrl(value) {
    try {
      const url = new URL(value);
      return url.origin === 'https://t.me' && !url.username && !url.password && !url.hash &&
        /^\/newbot\/[A-Za-z0-9_]{5,32}(?:\/[A-Za-z0-9_]{5,32})?$/.test(url.pathname) &&
        [...url.searchParams.keys()].every(key => key === 'name');
    } catch { return false; }
  }
  function renderManagedConnect(card, project) {
    if (managedCreation?.projectId === projectId(project) && safeCreationUrl(managedCreation.creation_url)) {
      card.append(element('a', {class: 'button primary full', href: managedCreation.creation_url, target: '_blank', rel: 'noopener noreferrer', text: 'Создать сервис'}),
        p('Подтвердите создание в Telegram. Подключение проверим автоматически.', 'hint'));
    } else {
      const start = button(state.busy ? 'Открываем Telegram…' : 'Создать сервис', async () => {
        if (state.busy || preview) return;
        const id = projectId(project);
        if (state.entries.get(id)?.data?.order?.status !== 'paid_pending_launch') return;
        loadingForm(card, true, 'Создать сервис');
        try {
          const current = selectedProject();
          if (projectId(current) !== id) return;
          const result = await api(`/projects/${encodeURIComponent(id)}/managed-bot`, {method: 'POST', body: {expected_version: version(current)}});
          if (!safeCreationUrl(result?.creation_url) || result.status !== 'pending' || !Number.isFinite(new Date(result.expires_at).getTime())) throw new ApiError(0, 'invalid_response');
          if (state.selected !== id) return;
          managedCreation = {...result, projectId: id};
          managedPollFailures = 0;
          state.busy = false; render();
          window.Telegram?.WebApp?.openTelegramLink?.(result.creation_url);
        } catch (error) { errorBox(card, error, error.status === 409); }
        finally { loadingForm(card, false, 'Создать сервис'); }
      }, 'primary full');
      start.disabled = preview || state.busy || state.settings?.managed_bot_creation_available !== true;
      card.append(start);
      if (state.settings?.managed_bot_creation_available !== true) card.append(p('Оплата сохранена. Создание временно недоступно — напишите в поддержку.', 'hint'));
    }
  }
  function scheduleManagedCheck(immediate = false) {
    clearTimeout(managedPollTimer);
    if (workspaceMode || preview || !state.ready || document.hidden ||
        !managedCreation || state.selected !== managedCreation.projectId || botInfo(selectedProject())) return;
    if (Date.now() >= new Date(managedCreation.expires_at).getTime()) { managedCreation = null; render(); return; }
    managedPollTimer = setTimeout(pollManaged, immediate ? 0 : Math.min(60000, 3000 * 2 ** managedPollFailures));
  }
  async function pollManaged() {
    if (managedPollBusy || state.busy || !managedCreation || document.hidden) { scheduleManagedCheck(); return; }
    const id = managedCreation.projectId;
    managedPollBusy = true;
    try {
      const current = normalize(await api(`/projects/${encodeURIComponent(id)}`));
      if (state.selected !== id || projectId(current) !== id) return;
      updateProject(current); managedPollFailures = 0;
      if (botInfo(current)) { managedCreation = null; render(); scheduleEntryCheck(true); }
    } catch (error) {
      managedPollFailures = Math.min(5, managedPollFailures + 1);
      if ([401, 403, 404].includes(error.status)) managedCreation = null;
    } finally { managedPollBusy = false; scheduleManagedCheck(); }
  }
  const reportTitles = {overview: 'Обзор', customers: 'Клиенты', orders: 'Продажи'};
  const customerStatuses = {active: 'Подписка активна', expired: 'Подписка истекла', pending: 'Ожидает активации', none: 'Без подписки'};
  const orderStatuses = {created: 'Ожидает оплаты', paid_pending_activation: 'Оплачено · активация', active: 'Подписка активирована', canceled: 'Платёж отменён'};
  const orderPurposes = {subscription: 'Подписка', traffic: 'Докупка ГБ', devices: 'Докупка устройств'};
  function validReport(kind, data) {
    const integer = value => Number.isSafeInteger(value) && value >= 0;
    const validDate = value => typeof value === 'string' && Number.isFinite(new Date(value).getTime());
    if (!data || typeof data !== 'object') return false;
    if (kind === 'overview') {
      return ['customers', 'active_subscriptions', 'pending_activations'].every(key => integer(data[key])) && data.timezone === 'UTC' && data.sales_enabled === false && ['today', 'days7', 'days30'].every(period => data.periods?.[period] && ['sales', 'revenue_kopecks', 'partner_kopecks', 'platform_kopecks'].every(key => integer(data.periods[period][key])));
    }
    if (!Array.isArray(data.items) || typeof data.truncated !== 'boolean') return false;
    return data.items.every(item => item && integer(item.tg_id) && item.tg_id > 0 && validDate(item.created_at) && (kind === 'customers'
      ? Object.hasOwn(customerStatuses, item.subscription_status) && (item.expires_at === null || validDate(item.expires_at)) && (item.referrer_tg_id === null || integer(item.referrer_tg_id))
      : typeof item.id === 'string' && integer(item.price_kopecks) && Object.hasOwn(orderStatuses, item.status) && (item.purpose === undefined || Object.hasOwn(orderPurposes, item.purpose)) && (item.paid_at === null || validDate(item.paid_at))));
  }
  async function loadReport(kind, id) {
    const key = `${id}/${kind}`;
    if (state.reports.get(key)?.status === 'loading') return;
    state.reports.set(key, {status: 'loading'});
    if (state.selected === id && state.view === kind) render();
    try {
      const data = await api(`/projects/${encodeURIComponent(id)}/${kind}`);
      if (!validReport(kind, data)) throw new ApiError(0, 'invalid_response');
      state.reports.set(key, {status: 'ready', data});
    } catch (error) { state.reports.set(key, {status: 'error', error}); }
    if (state.selected === id && state.view === kind) render();
  }
  function metric(label, value) {
    return element('div', {}, [p(label, 'metric-label'), element('strong', {class: 'metric-value', text: value})]);
  }
  function renderReport(kind) {
    const project = selectedProject();
    const id = projectId(project);
    content.append(heading(reportTitles[kind], kind === 'overview' ? 'Клиенты, покупки и начисления вашего проекта.' : kind === 'customers' ? 'Люди, которые впервые открыли вашего бота.' : 'Заказы и состояние активации подписок.', project.title));
    const toolbar = element('div', {class: 'report-toolbar'}, [preview ? element('span', {class: 'demo-label', text: 'Пример данных · не реальные продажи'}) : p(state.settings?.sales_enabled ? 'Данные вашего проекта' : 'Продажи пока не включены', 'muted fine'), kind === 'orders' ? button('Назад к обзору', () => setView('overview'), 'quiet') : null]);
    content.append(toolbar);
    const report = state.reports.get(`${id}/${kind}`);
    if (!report) { void loadReport(kind, id); return; }
    if (report.status === 'loading') {
      const loading = element('section', {class: 'card report-state', role: 'status', 'aria-live': 'polite', 'aria-busy': 'true'}, [element('span', {class: 'spinner', 'aria-hidden': true}), element('h2', {text: 'Загружаем данные'}), p('Покажем актуальные значения после ответа сервера.', 'muted fine')]);
      content.append(loading); return;
    }
    if (report.status === 'error') {
      content.append(element('section', {class: 'card report-state', role: 'alert'}, [element('h2', {text: 'Данные не загрузились'}), p(errorText(report.error), 'muted fine'), button('Повторить загрузку', () => loadReport(kind, id), 'quiet')])); return;
    }
    if (kind === 'overview') renderOverview(report.data);
    else renderRecords(kind, report.data);
    content.append(element('div', {class: 'button-row'}, [button('Обновить данные', () => loadReport(kind, id), 'quiet')]));
  }
  function renderOverview(data) {
    content.append(element('div', {class: 'metric-grid'}, [
      ['Клиентов', data.customers], ['Активных подписок', data.active_subscriptions],
    ].map(([label, value]) => element('section', {class: 'card metric-card'}, [p(label, 'metric-label'), element('strong', {class: 'metric-value', text: value})]))));
    if (data.pending_activations > 0) content.append(element('p', {class: 'notice', role: 'status', text: `Ожидают активации: ${data.pending_activations}. Оплата сохранена, повторная покупка не нужна.`}));
    const selected = data.periods[state.period];
    const section = element('section', {class: 'card'}, [element('h2', {text: 'Покупки и продления'})]);
    const periods = element('div', {class: 'period-switch', role: 'group', 'aria-label': 'Период отчёта'});
    for (const [key, label] of [['today', 'Сегодня'], ['days7', '7 дней'], ['days30', '30 дней']]) periods.append(element('button', {type: 'button', class: 'button quiet', text: label, 'aria-pressed': state.period === key ? 'true' : 'false', onclick: () => { state.period = key; render(); }}));
    section.append(periods, p(`Оплаченных покупок: ${selected.sales}`, 'muted fine'), element('div', {class: 'sales-metrics overview-sales'}, [metric('Выручка', money(selected.revenue_kopecks / 100)), metric('Ваш доход · 70%', money(selected.partner_kopecks / 100))]), p(state.settings?.features?.finance?'Доступный остаток и вывод — в разделе «Финансы».':'Вывод пока не подключён.', 'report-note'));
    section.append(element('details', {class: 'creator-details'}, [element('summary', {text: 'Как считается доход'}), p(`Платформе и на обслуживание — 30%: ${money(selected.platform_kopecks / 100)}. Начисления с продаж не равны доступному остатку: он учитывает резервы и выплаты.`), p('Границы дней в отчёте — по UTC. Неоплаченные заказы в выручку не входят.')]));
    section.append(button('История продаж', () => setView('orders'), 'quiet full'));
    content.append(element('div', {class: 'stack report-state'}, [section]));
  }
  function renderRecords(kind, data) {
    if (!data.items.length) {
      content.append(element('section', {class: 'card report-state'}, [element('h2', {text: kind === 'customers' ? 'Клиентов пока нет' : 'Заказов пока нет'}), p(kind === 'customers' ? 'После запуска здесь появятся клиенты вашего бота.' : 'После запуска здесь появятся покупки и продления.', 'muted fine')])); return;
    }
    const records = element('div', {class: 'record-list'});
    for (const item of data.items) {
      if (kind === 'customers') records.append(element('article', {class: 'card record-card'}, [element('div', {}, [element('h2', {class: 'record-title', text: `Клиент ${item.tg_id}`}), p(`Регистрация: ${date(item.created_at)}`, 'record-meta')]), element('span', {class: 'record-status', text: customerStatuses[item.subscription_status]}), element('div', {class: 'record-details'}, [p(item.expires_at ? `Срок подписки: до ${date(item.expires_at)}` : 'Срок подписки пока не установлен', 'record-meta'), p(item.referrer_tg_id ? 'Пришёл по приглашению друга' : 'Без приглашения', 'record-meta')])]));
      else {
        const addon = item.purpose === 'traffic' || item.purpose === 'devices';
        const issue = ['addon_expired','device_limit','activation_expired'].includes(item.activation_issue);
        const status = issue ? 'Оплачено · нужна сверка' : addon && item.status === 'active' ? 'Докупка применена' : orderStatuses[item.status];
        records.append(element('article', {class: 'card record-card'}, [element('div', {}, [element('h2', {class: 'record-title', text: `Заказ ${item.id.slice(0, 8)}`}), p(`${orderPurposes[item.purpose || 'subscription']} · Клиент ${item.tg_id} · ${date(item.created_at)}`, 'record-meta')]), element('strong', {class: 'record-value', text: money(item.price_kopecks / 100)}), element('div', {class: 'record-details'}, [element('span', {class: 'record-status', text: status}), p(item.status === 'canceled' ? (addon ? 'Оплата отменена. Докупка не применена.' : 'Оплата отменена. Подписка не выдана.') : item.paid_at ? `Оплата: ${date(item.paid_at)}` : 'Оплата ещё не получена', 'record-meta')])]));
      }
    }
    content.append(records);
    if (data.truncated) content.append(p('Показаны последние 100 записей. Используйте поиск и страницы в разделе клиентов.', 'report-note'));
  }
  function tariffSummary(project) {
    const section = element('aside', {class: 'card summary-card', 'aria-label': 'Предпросмотр тарифа'});
    section.append(p('Одна подписка', 'eyebrow'), element('h2', {class: 'tariff-title', text: 'Ваш сервис'}), element('div', {class: 'price-line'}, [element('span', {class: 'price', id: 'preview-price', text: Number.isFinite(Number(projectPrice(project))) ? money(projectPrice(project)) : 'Цена не получена'}), element('span', {class: 'price-unit', text: 'за 30 дней'})]), element('ul', {class: 'allowances'}, [element('li', {text: 'Telegram-бот под вашим брендом'}), element('li', {text: 'Личный кабинет'}), element('li', {text: 'Поддержка пользователей'})]));
    const split = element('div', {id: 'preview-split'}, [priceDetails(projectPrice(project))]); section.append(split);
    const previewCard = element('div', {class: 'project-preview'});
    paintAccent(previewCard, accent(project));
    previewCard.append(p('Пример оформления, не опубликовано', 'preview-label'), element('div', {id: 'preview-title', class: 'preview-brand', text: project.title}), p('Подписка на ваш сервис.', 'muted fine'), element('div', {class: 'preview-cta', text: 'Купить подписку', 'aria-hidden': true}), p('Это образец, не кнопка оплаты.', 'preview-caption'));
    section.append(previewCard);
    const addons = element('div', {class: 'addons'}, [element('h3', {text: 'Докупки'}), p('Цены совпадают с основным ботом. Менять их в кабинете нельзя.', 'muted fine')]);
    if (!state.catalog) addons.append(p('Каталог дополнений загружается…', 'muted fine'));
    else {
      const traffic = state.catalog.addons?.traffic_packages || [];
      const devices = state.catalog.addons?.devices?.packages || [];
      const list = element('ul');
      for (const item of traffic) {
        const gb = item.gb ?? item.gigabytes;
        const amount = Number.isInteger(item.price_kopecks) ? item.price_kopecks / 100 : undefined;
        if (gb != null && amount != null) list.append(element('li', {text: `+${gb} ГБ — ${money(amount)}`}));
      }
      for (const item of devices) {
        const count = item.count;
        const amount = Number.isInteger(item.price_kopecks) ? item.price_kopecks / 100 : undefined;
        if (count != null && amount != null) list.append(element('li', {text: `+${count} ${count === 1 ? 'устройство' : 'устройства'} — ${money(amount)}${Number.isInteger(item.days) ? ` за ${item.days} дн.` : ''}`}));
      }
      if (list.childNodes.length) addons.append(list); else addons.append(p('Пакеты и цены появятся после подключения каталога.', 'muted fine'));
    }
    section.append(addons); return section;
  }
  function renderBrand() {
    const project = selectedProject();
    content.append(heading('Бренд и тариф', 'Настройте название и цену. Клиенты увидят ваш бренд, а не кабинет платформы.', project.title));
    const layout = element('div', {class: 'layout-two'});
    const card = element('section', {class: 'card'});
    const form = element('form', {novalidate: true});
    const title = field('brand-title', 'Название проекта', project.title || '', {required: true, maxlength: 80, autocomplete: 'organization'});
    const price = field('brand-price', 'Цена за 30 дней, ₽', projectPrice(project) ?? '', {required: true, inputmode: 'decimal', maxlength: 12});
    price.wrapper.append(p('Не ниже 199 ₽. Докупки — по ценам основного бота.', 'hint'));
    const support = field('brand-support', 'Ссылка на поддержку', project.support_url || '', {type: 'url', maxlength: 300, placeholder: 'https://t.me/your_support', autocomplete: 'off'});
    const news = field('brand-news', 'Ссылка на новости', project.news_url || '', {type: 'url', maxlength: 300, placeholder: 'https://t.me/your_channel', autocomplete: 'off'});
    const color = field('brand-accent', 'Акцентный цвет', accent(project), {type: 'color'});
    const copyField=(id,label,key,max)=>{const wrapper=element('div',{class:'field'}),input=element('textarea',{id,rows:3,maxlength:max,value:project.brand?.[key]||''});wrapper.append(element('label',{for:id,text:label}),input);return{wrapper,input};};
    const welcome=copyField('brand-welcome','Приветствие в боте','welcome_text',2000);
    const pageTitle=field('brand-page-title','Заголовок страницы подписки',project.brand?.subscription_title||'',{maxlength:80});
    const description=copyField('brand-page-description','Описание на странице подписки','subscription_description',2000);
    color.wrapper.append(p('Цвет главной кнопки в предпросмотре.', 'hint'));
    const domainBox = element('div', {class: 'domain-box'}, [p('Адрес вашего проекта', 'muted fine'), p(domain(project), 'domain'), p('Поддомен платформы. Отдельная покупка домена не нужна.', 'muted fine')]);
    const extra = element('details', {class: 'creator-details'}, [element('summary', {text: 'Тексты и страница подписки'}), welcome.wrapper, pageTitle.wrapper, description.wrapper, domainBox]);
    form.append(title.wrapper, price.wrapper, support.wrapper, news.wrapper, color.wrapper, extra, element('div', {class: 'button-row'}, [submitButton('Сохранить изменения'), button('Назад к настройкам', () => setView('settings'), 'quiet')]));
    title.input.addEventListener('input', () => { document.getElementById('preview-title').textContent = title.input.value || 'Название проекта'; });
    price.input.addEventListener('input', () => { const value = price.input.value.replace(',', '.'); document.getElementById('preview-price').textContent = validPrice(value) ? money(value) : 'От 199 ₽'; document.getElementById('preview-split').replaceChildren(validPrice(value) ? priceDetails(value) : p('Укажите цену от 199 ₽.', 'muted fine')); });
    color.input.addEventListener('input', () => paintAccent(document.querySelector('.project-preview'), color.input.value));
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (state.busy) return;
      if (!title.input.value.trim()) { title.input.focus(); return; }
      if (!validPrice(price.input.value)) { errorBox(form, new ApiError(400, 'invalid_price')); price.input.focus(); return; }
      if (!validUrl(support.input.value.trim()) || !validUrl(news.input.value.trim())) { errorBox(form, new ApiError(400, 'invalid_url')); return; }
      const body = {expected_version: version(project), title: title.input.value.trim(), price: priceString(price.input.value), support_url: support.input.value.trim(), news_url: news.input.value.trim(), brand: {...project.brand, accent_color: color.input.value,welcome_text:welcome.input.value,subscription_title:pageTitle.input.value,subscription_description:description.input.value}};
      loadingForm(form, true, 'Сохранить изменения');
      try { updateProject(await api(`/projects/${encodeURIComponent(projectId(project))}`, {method: 'PATCH', body})); state.busy = false; render(); notify(preview ? 'Изменения только в демонстрации.' : 'Изменения сохранены.'); }
      catch (error) { errorBox(form, error, error.status === 409 && !messages[error.code]); }
      finally { loadingForm(form, false, 'Сохранить изменения'); }
    });
    card.append(element('h2', {text: 'Настройки проекта'}), form);
    if(state.settings?.features?.branding)card.append(logoEditor(project,form));
    layout.append(card, tariffSummary(project)); content.append(layout);
  }
  function logoEditor(project, settingsForm){
    const section=element('section',{class:'logo-editor'}),picture=element('div'),status=p('Загружаем логотип…','hint');
    const file=element('input',{type:'file',id:'brand-logo',accept:'image/png,image/jpeg'});
    const form=element('form',{novalidate:true}),remove=button('Удалить логотип',()=>mutate('DELETE'),'quiet');
    remove.hidden=true;
    section.append(element('h3',{text:'Логотип'}),p('PNG или JPEG до 512 КБ, не более 1024 × 1024. После загрузки изображение очищается и уменьшается.','hint'),picture,status,form);
    form.append(element('label',{for:'brand-logo',text:'Файл логотипа'}),file,element('div',{class:'button-row'},[submitButton('Загрузить логотип'),remove]));
    const path=`/projects/${encodeURIComponent(projectId(project))}/logo`;
    function draw(data){
      picture.replaceChildren();remove.hidden=!data.logo_id;
      if(typeof data.png_base64==='string'&&data.png_base64.length<=400000&&/^[A-Za-z0-9+/]+={0,2}$/.test(data.png_base64))picture.append(element('img',{class:'brand-logo-preview',src:`data:image/png;base64,${data.png_base64}`,alt:'Логотип проекта'}));
      status.textContent=data.logo_id?'Логотип сохранён.':'Логотип пока не добавлен.';
    }
    async function mutate(method){
      if(state.busy)return;
      let selected=file.files?.[0];
      if(method==='POST'&&(!selected||!['image/png','image/jpeg'].includes(selected.type)||selected.size>512*1024||selected.size===0)){status.textContent='Выберите PNG или JPEG до 512 КБ.';return;}
      loadingForm(form,true,'Загрузить логотип');settingsForm.querySelectorAll('button').forEach(node=>node.disabled=true);
      try{
        const body={expected_version:version(project)};
        if(method==='POST')body.image_base64=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(Error('file_read'));reader.readAsDataURL(selected);});
        const result=await api(path,{method,body});
        Object.assign(project,result.project);updateProject(project);draw(result);file.value='';notify('Логотип обновлён. Остальные поля формы не изменены.');
      }catch(error){status.textContent=errorText(error);}finally{loadingForm(form,false,'Загрузить логотип');settingsForm.querySelectorAll('button').forEach(node=>node.disabled=false);}
    }
    form.addEventListener('submit',event=>{event.preventDefault();void mutate('POST');});
    void api(path).then(draw).catch(error=>{status.textContent=errorText(error);section.append(button('Повторить загрузку',()=>api(path).then(draw).catch(error=>{status.textContent=errorText(error);}),'quiet'));});
    return section;
  }
  function botInfo(project) {
    const bot = project.bot || project.telegram_bot;
    if (!bot || !bot.username) return null;
    return {username: String(bot.username).replace(/^@/, ''), title: bot.title || bot.first_name || bot.username};
  }
  function renderBot() {
    const project = selectedProject();
    const connected = botInfo(project);
    content.append(heading('Telegram-бот', 'Управление подключением вашего бота.', project.title));
    const layout = element('div', {class: 'layout-two'});
    const card = element('section', {class: 'card'});
    card.append(element('h2', {text: connected ? 'Бот подключён' : 'Подключение бота'}));
    if (connected) card.append(element('div', {class: 'bot-identity'}, [element('strong', {text: connected.title}), p(`@${connected.username}`)]));
    else card.append(p('Владелец бота остаётся прежним. Не отправляйте токен в обычный чат.', 'muted fine'));
    card.append(element('ol', {class: 'instruction-list'}, [element('li', {text: 'Откройте BotFather в Telegram и создайте бота командой /newbot.'}), element('li', {text: 'Укажите название и свободный username. Скопируйте выданный токен.'}), element('li', {text: 'Вставьте токен ниже. Платформа проверит, что он действителен.'})]));
    const form = element('form', {novalidate: true});
    const token = field('bot-token', connected ? 'Новый токен бота' : 'Токен бота', '', {type: 'password', autocomplete: 'new-password', autocapitalize: 'none', spellcheck: 'false', required: true, maxlength: 256, placeholder: '123456789:AA…'});
    token.wrapper.append(p('Не сохраняется в браузере, адресе страницы или истории кабинета.', 'hint'));
    form.append(token.wrapper, element('div', {class: 'button-row'}, [submitButton(connected ? 'Проверить и заменить токен' : 'Проверить и подключить')]));
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (state.busy) return;
      const value = token.input.value.trim();
      if (!/^\d{5,20}:[A-Za-z0-9_-]{20,200}$/.test(value)) { token.input.value = ''; errorBox(form, new ApiError(400, 'invalid_token')); token.input.focus(); return; }
      loadingForm(form, true, connected ? 'Проверить и заменить токен' : 'Проверить и подключить');
      try {
        updateProject(await api(`/projects/${encodeURIComponent(projectId(project))}/bot`, {method: 'POST', body: {expected_version: version(project), token: value}}));
        token.input.value = ''; state.busy = false;
        if (preview) { render(); notify('Демонстрация: реальный Telegram не вызывался.'); }
        else stateScreen('Подключение сохранено', 'Закройте кабинет и откройте его заново из своего бота. После замены токена нужна новая авторизация Telegram.');
      }
      catch (error) { errorBox(form, error, error.status === 409 && !messages[error.code]); }
      finally { token.input.value = ''; loadingForm(form, false, connected ? 'Проверить и заменить токен' : 'Проверить и подключить'); }
    });
    card.append(form, p('Если токен стал известен другим людям, отзовите его через BotFather и подключите новый.', 'token-warning'), element('div', {class: 'button-row'}, [button('Назад к настройкам', () => setView('settings'), 'quiet')]));
    const previewCard = element('aside', {class: 'card summary-card client-preview'}, [p('Предпросмотр клиентского бота', 'eyebrow'), element('h2', {class: 'tariff-title', text: project.title}), p('Меню вашего бренда. Это образец, не опубликованный бот.', 'muted fine'), p(`${money(projectPrice(project))} / 30 дней`, 'client-allowance'), element('div', {class: 'menu-preview', 'aria-label': 'Образец меню, кнопки не активны'}, ['Купить подписку', 'Моя подписка', 'Бонусы', 'Как подключиться', 'Помощь', 'Новости', 'Админ'].map(text => element('span', {text}))), element('div', {class: 'notice'}, [p('«Админ» виден только владельцу и сотрудникам. За первую покупку приглашённого друга — +7 дней. Бонус начисляется автоматически.')])]);
    paintAccent(previewCard, accent(project));
    layout.append(card, previewCard); content.append(layout);
  }
  const entryStatuses = {created: 'Ожидает оплаты', paid_pending_launch: 'Оплачено · ожидает запуска', launched: 'Настройка бота завершена', canceled: 'Платёж отменён'};
  function validEntry(data) {
    const validDate = value => typeof value === 'string' && Number.isFinite(new Date(value).getTime());
    if (!data || !validEntryFee(data.entry_fee_kopecks) || typeof data.payments_available !== 'boolean' || typeof data.launch_available !== 'boolean') return false;
    if (data.entry_fee_kopecks === PILOT_FEE && !discountedEntry(data)) return false;
    if (data.order === null) return true;
    const order = data.order;
    // Saved 10 RUB invoices remain usable after the trial flag is turned off;
    // the authenticated server, not a browser price flag, owns their amount.
    return order && /^[0-9a-f-]{36}$/i.test(order.id || '') && validEntryFee(order.price_kopecks) && Object.hasOwn(entryStatuses, order.status) && validDate(order.created_at) && (order.paid_at === null || validDate(order.paid_at)) && (!order.confirmation_url || safeCheckoutUrl(order.confirmation_url));
  }
  function safeCheckoutUrl(value) {
    try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password && !url.hash; } catch { return false; }
  }
  function entryRecord(data) {
    return {status: 'ready', data, needsReplace: data.launch_state === 'needs_webhook_replace' || data.order?.needs_webhook_replace === true, launchState: data.launch_state};
  }
  function scheduleEntryCheck(immediate = false) {
    clearTimeout(entryPollTimer);
    const entry = state.entries.get(state.selected);
    if (workspaceMode || preview || entryPollStopped || !state.ready || document.hidden ||
        entry?.status !== 'ready' || entry.needsReplace ||
        !['created', 'paid_pending_launch'].includes(entry.data.order?.status)) return;
    if (entry.data.order.status === 'paid_pending_launch' && !botInfo(selectedProject())) return;
    entryPollTimer = setTimeout(pollEntry, immediate ? 0 : Math.min(60000, 5000 * 2 ** entryPollFailures));
  }
  async function pollEntry() {
    if (entryPollBusy || state.busy || document.hidden) { scheduleEntryCheck(); return; }
    const id = state.selected, saved = state.entries.get(id), order = saved?.data?.order;
    if (!order || !['created', 'paid_pending_launch'].includes(order.status) || saved.needsReplace) return;
    entryPollBusy = true;
    try {
      // Reconcile the EXISTING order only. Never create an invoice or a new
      // request key during polling, including after a timeout/404.
      const data = await api(`/projects/${encodeURIComponent(id)}/entry/${encodeURIComponent(order.id)}/check`, {method: 'POST', body: {}});
      if (!validEntry(data)) throw new ApiError(0, 'invalid_response');
      if (state.selected !== id || state.entries.get(id)?.data?.order?.id !== order.id) return;
      const changed = data.order?.status !== order.status || data.launch_state !== saved.launchState || data.order?.needs_webhook_replace !== order.needs_webhook_replace;
      state.entries.set(id, entryRecord(data)); entryPollFailures = 0;
      if (['paid_pending_launch', 'launched'].includes(data.order?.status)) rememberCheckoutKey(null);
      if (data.order?.status === 'launched') {
        try {
          const current = normalize(await api(`/projects/${encodeURIComponent(id)}`));
          if (projectId(current) !== id) throw new ApiError(0, 'invalid_response');
          updateProject(current);
        } catch { state.entries.get(id).refreshWarning = true; }
      }
      if (changed || data.order?.status === 'launched') render();
    } catch (error) {
      if ([401, 403, 404].includes(error.status)) entryPollStopped = true;
      entryPollFailures = Math.min(entryPollFailures + 1, 4);
    } finally { entryPollBusy = false; scheduleEntryCheck(); }
  }
  async function loadEntry(id) {
    if (state.entries.get(id)?.status === 'loading' || state.busy) return;
    state.entries.set(id, {status: 'loading'});
    if (state.selected === id && (!workspaceMode || state.view === 'launch')) render();
    try {
      const data = await api(`/projects/${encodeURIComponent(id)}/entry`);
      if (!validEntry(data)) throw new ApiError(0, 'invalid_response');
      if (data.order) state.entryKeys.delete(id);
      state.entries.set(id, entryRecord(data));
    } catch (error) { state.entries.set(id, {status: 'error', error}); }
    if (state.selected === id && (!workspaceMode || state.view === 'launch')) render();
  }
  async function entryAction(id, action, replaceExisting = false) {
    if (state.busy || preview || state.selected !== id) return;
    const entry = state.entries.get(id);
    if (entry?.status !== 'ready') return;
    const data = entry.data;
    const order = data.order;
    const connected = botInfo(selectedProject());
    if (action === 'create' && (!entry.confirm || !data.payments_available || !connected || order && order.status !== 'canceled')) return;
    if (action === 'checkout' && (!data.payments_available || order?.status !== 'created')) return;
    if (action === 'check' && !['created', 'paid_pending_launch'].includes(order?.status)) return;
    if (action === 'launch' && (!data.launch_available || order?.status !== 'paid_pending_launch' || !connected || entry.needsReplace && !replaceExisting)) return;
    if (action === 'create') state.entryKeys.set(id, state.entryKeys.get(id) || crypto.randomUUID());
    state.busy = true;
    entry.busy = true; entry.error = null;
    document.querySelectorAll('[data-view]').forEach(item => { item.disabled = true; });
    render();
    const base = `/projects/${encodeURIComponent(id)}/entry`;
    const path = action === 'create' ? base : `${base}/${encodeURIComponent(order.id)}/${action}`;
    const body = action === 'create' ? {request_key: state.entryKeys.get(id), expected_fee_kopecks: data.entry_fee_kopecks} : action === 'launch' ? {replace_existing: replaceExisting} : {};
    try {
      const result = await api(path, {method: 'POST', body});
      if (!validEntry(result)) throw new ApiError(0, 'invalid_response');
      if (action === 'create') state.entryKeys.delete(id);
      const saved = entryRecord(result);
      state.entries.set(id, saved);
      if (result.order?.status === 'launched') {
        try {
          const current = normalize(await api(`/projects/${encodeURIComponent(id)}`));
          if (projectId(current) !== id) throw new ApiError(0, 'invalid_response');
          updateProject(current);
        } catch { saved.refreshWarning = true; }
      }
    } catch (error) { entry.error = error; entry.busy = false; }
    finally {
      state.busy = false;
      document.querySelectorAll('[data-view]').forEach(item => { item.disabled = false; });
      if (state.selected === id && (!workspaceMode || state.view === 'launch')) render();
    }
  }
  function renderLaunch() {
    const project = selectedProject();
    const id = projectId(project);
    const bot = botInfo(project);
    content.append(heading('Подключение сервиса', bot ? `Бот @${bot.username} подключён. Управление — в нём.` : 'Состояние подключения проекта.', 'Ваш сервис'));
    const card = element('section', {class: 'card entry-card', 'aria-label': 'Вступление и запуск'});
    content.append(card);
    const entry = state.entries.get(id);
    if (!entry) { void loadEntry(id); return; }
    if (entry.status === 'loading') { card.append(element('div', {role: 'status', 'aria-live': 'polite', 'aria-busy': 'true'}, [element('span', {class: 'spinner', 'aria-hidden': true}), element('h2', {text: 'Проверяем вступление'}), p('Загружаем состояние оплаты. Повторный платёж не создаём.', 'muted fine')])); return; }
    if (entry.status === 'error') { card.append(element('div', {role: 'alert'}, [element('h2', {text: 'Статус не загрузился'}), p(errorText(entry.error), 'muted fine'), button('Повторить загрузку', () => loadEntry(id), 'quiet')])); return; }
    const data = entry.data;
    const order = data.order;
    card.setAttribute('aria-busy', String(!!entry.busy));
    const shownFee = order?.price_kopecks ?? data.entry_fee_kopecks;
    card.append(p(shownFee === PILOT_FEE ? (data.owner_entry_pilot === true ? 'Закрытый тест владельца' : 'Пробное подключение проекта') : 'Подключение проекта', 'eyebrow'), element('h2', {class: 'entry-price', text: money(shownFee / 100)}), p('Один раз. Не ежемесячная плата.', 'muted fine'));
    if (shownFee === PILOT_FEE) card.append(p('Тестовая покупка за 10 ₽ — это настоящее списание. Обычная стоимость вступления остаётся 9 990 ₽.', 'entry-note'));
    if (preview) card.append(element('div', {class: 'notice'}, [p('Демонстрация запуска. Реальная оплата и подключение бота здесь недоступны.')]));
    if (order) card.append(element('div', {class: 'entry-status', role: 'status', 'aria-live': 'polite'}, [element('strong', {text: entryStatuses[order.status]}), p(`Заказ ${order.id.slice(0, 8)} · ${date(order.created_at)}`, 'muted fine'), order.paid_at ? p(`Оплата: ${date(order.paid_at)}`, 'muted fine') : null]));
    if (order?.status === 'launched') {
      card.append(p('В своём боте откройте «Админ» → «Личный кабинет».', 'entry-note'), openOwnBot(project));
      if (entry.refreshWarning) card.append(element('div', {class: 'notice'}, [p('Запуск подтверждён. Настройки проекта пока не обновились — перед редактированием заново откройте кабинет. Повторная оплата не нужна.')]));
    } else if (order?.status === 'paid_pending_launch') {
      card.append(p('Вступление оплачено. Платить повторно не нужно — при сбое можно повторить только подключение бота.', 'entry-note'));
      if (entry.launchState === 'retry') card.append(p('Подключение пока не завершено. Сохранённая оплата не потеряна; попробуйте запуск ещё раз.', 'entry-note'));
      let replace;
      if (entry.needsReplace) {
        replace = element('input', {type: 'checkbox', id: 'replace-webhook'});
        const replaceButton = button('Заменить подключение и запустить', () => entryAction(id, 'launch', replace.checked), 'primary full');
        replaceButton.disabled = true;
        replace.addEventListener('change', () => { replaceButton.disabled = !replace.checked || !!entry.busy; });
        card.append(element('div', {class: 'notice'}, [p('Этот бот уже подключён к другому обработчику сообщений. Замена отключит прежнюю обработку.'), element('label', {class: 'confirmation-check', for: 'replace-webhook'}, [replace, element('span', {text: 'Заменить текущее подключение бота'})])]), replaceButton);
      } else {
        const launch = button('Завершить подключение бота', () => entryAction(id, 'launch'), 'primary full');
        launch.disabled = preview || !bot || !data.launch_available || entry.launchState === 'disabled';
        card.append(launch);
      }
      if (!data.launch_available || entry.launchState === 'disabled') card.append(p('Автоматический запуск пока не подключён на платформе. Оплата сохранена.', 'entry-note'));
    } else if (order?.status === 'created') {
      if (order.confirmation_url) card.append(element('a', {class: 'button primary full', href: order.confirmation_url, target: '_blank', rel: 'noopener noreferrer', text: 'Открыть оплату'}));
      else {
        const checkout = button('Подготовить оплату', () => entryAction(id, 'checkout'), 'primary full');
        checkout.disabled = preview || !data.payments_available;
        card.append(checkout);
      }
      const check = button('Проверить оплату', () => entryAction(id, 'check'), 'quiet full'); check.disabled = preview;
      card.append(check, p('Оплату проверяем автоматически. После запуска пришлём сообщение со ссылкой на вашего бота. Повторно платить не нужно.', 'entry-note'));
      if (!data.payments_available) card.append(p('Приём оплаты сейчас недоступен. Новый платёж не создаётся.', 'entry-note'));
    } else {
      if (order?.status === 'canceled') card.append(p('Предыдущий платёж отменён. Для новой попытки подтвердите вступление ещё раз.', 'entry-note'));
      if (entry.confirm) {
        card.append(element('div', {class: 'entry-confirm'}, [element('h3', {text: 'Подтвердите вступление'}), p(`Стоимость — ${money(data.entry_fee_kopecks / 100)}, один раз за этот проект. После подтверждения будет создан заказ, а не автоматическое списание.`, 'entry-note')]));
        const confirm = button(`Подтвердить покупку за ${feeLabel(data.entry_fee_kopecks)}`, () => entryAction(id, 'create'), 'primary full');
        confirm.disabled = preview || !bot || !data.payments_available;
        card.append(confirm, button('Пока не создавать заказ', () => { entry.confirm = false; render(); }, 'quiet full'));
      } else {
        const start = button(`Купить за ${feeLabel(data.entry_fee_kopecks)}`, () => { entry.confirm = true; render(); }, 'primary full');
        start.disabled = preview || !bot || !data.payments_available;
        card.append(start);
      }
      if (!bot) card.append(p('Сначала подключите своего Telegram-бота. Заказ вступления пока не создаётся.', 'entry-note'));
      if (!data.payments_available) card.append(p('Покупки пока отключены. Деньги не списаны.', 'entry-note'));
    }
    if (entry.error) card.append(element('div', {class: 'form-error', role: 'alert'}, [p(errorText(entry.error)), p('Состояние не подменено успешным. При потерянном ответе повтор использует тот же заказ.', 'fine')]));
    const refresh = button('Обновить статус', () => loadEntry(id), 'quiet full'); card.append(refresh);
    card.append(button('Назад', () => { state.creatorStep = 'intro'; state.view = 'enrollment'; render(); }, 'quiet full'));
    if (entry.busy) {
      card.querySelectorAll('button, input').forEach(item => { item.disabled = true; });
      card.querySelectorAll('a').forEach(item => { item.removeAttribute('href'); item.setAttribute('aria-disabled', 'true'); });
      card.append(p('Выполняем действие… Не закрывайте кабинет.', 'entry-note'), element('span', {class: 'spinner', role: 'status', 'aria-label': 'Ожидаем ответ сервера'}));
    }
  }
  async function loadCatalog(id) {
    try {
      const data = await api(`/catalog?project_id=${encodeURIComponent(id)}`);
      if (state.selected !== id) return;
      state.catalog = data;
      // Avoid re-rendering a form while a partner is typing.
      const old = content.querySelector('.summary-card');
      if (state.view === 'brand' && old) {
        const project = selectedProject();
        const typedPrice = document.getElementById('brand-price')?.value;
        const typedTitle = document.getElementById('brand-title')?.value;
        const typedAccent = document.getElementById('brand-accent')?.value;
        old.replaceWith(tariffSummary({...project, title: typedTitle || project.title, price_kopecks: validPrice(typedPrice || '') ? Math.round(Number(typedPrice.replace(',', '.')) * 100) : project.price_kopecks, brand: {...project.brand, accent_color: typedAccent || accent(project)}}));
      }
    } catch {
      if (state.selected !== id) return;
      const old = content.querySelector('.addons');
      if (old && state.view === 'brand') old.replaceChildren(element('h3', {text: 'Докупки'}), p('Не удалось загрузить каталог. Цены не подставлены из примера.', 'muted fine'), button('Повторить загрузку', () => loadCatalog(id), 'quiet full'));
    }
  }
  function stateScreen(title, text, action) {
    content.replaceChildren(element('section', {class: 'state-card'}, [element('h1', {text: title}), p(text, 'muted'), action || null]));
  }
  async function boot() {
    state.ready = false;
    clearTimeout(entryPollTimer); clearTimeout(managedPollTimer); entryPollStopped = false; entryPollFailures = 0; managedCreation = null;
    document.body.classList.toggle('creator-surface', !workspaceMode);
    document.querySelector('.sidebar').hidden = !workspaceMode;
    document.querySelector('.stage-badge').textContent = workspaceMode ? 'Личный кабинет' : 'Свой сервис';
    document.querySelector('.wordmark').href = workspaceMode ? location.pathname : '/partners';
    document.querySelector('.wordmark').setAttribute('aria-label', workspaceMode ? 'Личный кабинет' : 'Создать свой сервис');
    if (preview) {
      document.getElementById('demo-banner').hidden = false;
      document.querySelector('.wordmark').href = location.pathname + '?preview=1';
    } else {
      if (!window.Telegram?.WebApp) {
        await new Promise(resolve => {
          const script = document.createElement('script'); script.src = 'https://telegram.org/js/telegram-web-app.js'; script.onload = resolve; script.onerror = resolve;
          const timer = setTimeout(resolve, 4000); script.addEventListener('load', () => clearTimeout(timer)); script.addEventListener('error', () => clearTimeout(timer)); document.head.append(script);
        });
      }
      const app = window.Telegram?.WebApp;
      state.initData = app?.initData || '';
      app?.ready?.(); app?.expand?.();
      if (!state.initData) {
        stateScreen('Откройте из Telegram', workspaceMode ? 'В своём боте нажмите «Админ» → «Личный кабинет».' : 'Откройте кабинет через бота.', button('Проверить доступ снова', boot, 'primary'));
        return;
      }
    }
    try {
      const [settings, projects] = await Promise.all([api('/settings'), api('/projects')]);
      if (!settings || !Array.isArray(projects?.items)) throw new ApiError(0, 'invalid_response');
      if (settings.surface !== (workspaceMode ? 'workspace' : 'enrollment')) throw new ApiError(0, 'invalid_response');
      if (workspaceMode && (settings.workspace_project_id !== workspaceMatch[1] || projects.items.length !== 1 || projectId(projects.items[0]) !== workspaceMatch[1])) throw new ApiError(0, 'invalid_response');
      if (!settings.enabled) { stateScreen('Платформа пока не открыта', 'Подключение партнёрских проектов ещё не включено администратором. Изменений и списаний не было.', button('Проверить снова', boot, 'primary')); return; }
      state.settings = settings; state.projects = projects.items.map(normalize); state.selected = state.projects[0] ? projectId(state.projects[0]) : null; state.reports.clear(); state.entries.clear(); state.entryKeys.clear(); state.ready = true;
      state.checkoutKey = workspaceMode ? null : loadCheckoutKey(); state.checkoutCanceledOrderId = null; state.checkoutError = null;
      state.creatorStep = 'intro';
      if (workspaceMode) {
        const title = selectedProject()?.title || 'Мой сервис';
        document.querySelector('.wordmark > span').textContent = title;
        document.title = 'Личный кабинет · ' + title;
      }
      setView(workspaceMode ? window.SPNPartnerWorkspace.defaultView(selectedProject()) : 'enrollment', false);
    } catch (error) { stateScreen('Не удалось открыть кабинет', errorText(error), button('Повторить загрузку', boot, 'primary')); }
  }
  document.querySelectorAll('[data-view]').forEach(item => item.addEventListener('click', () => setView(item.dataset.view)));
  document.addEventListener('visibilitychange', () => { scheduleEntryCheck(true); scheduleManagedCheck(true); });
  window.addEventListener('focus', () => { scheduleEntryCheck(true); scheduleManagedCheck(true); });
  window.addEventListener('pagehide', () => { clearTimeout(entryPollTimer); clearTimeout(managedPollTimer); });

  // Explicit, loopback-only demonstration: no network, persistent storage or payment calls.
  const demoProjects = workspaceMode ? [{id: workspaceMatch[1], version: 1, slug: 'sever-service', title: 'Север', price_kopecks: 24900, status: 'active', role: 'owner', support_url: 'https://t.me/example_support', news_url: '', brand: {accent_color: '#e6c77a'}, bot: {id: 123456789, username: 'sever_demo_bot'}}] : [];
  const demoReports = {
    overview: {customers: 3, active_subscriptions: 1, pending_activations: 1, periods: {today: {sales: 1, revenue_kopecks: 24900, partner_kopecks: 17430, platform_kopecks: 7470}, days7: {sales: 2, revenue_kopecks: 49800, partner_kopecks: 34860, platform_kopecks: 14940}, days30: {sales: 4, revenue_kopecks: 99600, partner_kopecks: 69720, platform_kopecks: 29880}}, timezone: 'UTC', sales_enabled: false},
    customers: {items: [{tg_id: 100001, created_at: '2026-10-17T12:00:00Z', referrer_tg_id: null, subscription_status: 'active', expires_at: '2026-11-17T12:00:00Z'}, {tg_id: 100002, created_at: '2026-10-18T09:00:00Z', referrer_tg_id: 100001, subscription_status: 'pending', expires_at: null}, {tg_id: 100003, created_at: '2026-10-18T10:00:00Z', referrer_tg_id: null, subscription_status: 'none', expires_at: null}], truncated: false},
    orders: {items: [{id: '597dc8d9-469b-46a3-a12c-7aeb5e7bccbc', tg_id: 100001, price_kopecks: 24900, status: 'active', created_at: '2026-10-17T12:00:00Z', paid_at: '2026-10-17T12:01:00Z'}, {id: '6e8f9ed9-2c16-4740-ac92-ea9a64f4f1fd', tg_id: 100002, price_kopecks: 24900, status: 'paid_pending_activation', created_at: '2026-10-18T09:00:00Z', paid_at: '2026-10-18T09:01:00Z'}, {id: 'e95c1af2-75d8-43a7-a1dc-04118d02a68e', tg_id: 100003, price_kopecks: 24900, status: 'created', created_at: '2026-10-18T10:00:00Z', paid_at: null}, {id: '59d7f59e-6b2c-4c32-bbf6-af822f7afbf6', tg_id: 100003, price_kopecks: 24900, status: 'canceled', created_at: '2026-10-18T09:50:00Z', paid_at: null}], truncated: false},
  };
  async function previewApi(path, options) {
    await new Promise(resolve => setTimeout(resolve, 120));
    if (path === '/settings') return {enabled: true, surface: workspaceMode ? 'workspace' : 'enrollment', workspace_project_id: workspaceMode ? workspaceMatch[1] : null, stage: 'setup', base_domain: 'partners.example.test', billing_days: 30, actor_id:123, entry_fee_kopecks: ENTRY_FEE, pay_first_enabled: true, is_operator:false,features:{management:workspaceMode,finance:workspaceMode,branding:workspaceMode}};
    const extra = window.SPNPartnerWorkspace?.previewApi(path, options, demoProjects);
    if (extra !== undefined) return await extra;
    if (path.startsWith('/catalog')) return {addons: {traffic_packages: [{id: 'traffic10', gb: 10, price_kopecks: 2400}], devices: {addon_status: 'available', packages: [{count: 1, price_kopecks: 10000, days: 30}], max_limit: 10}}};
    const logoPath=path.match(/^\/projects\/([^/]+)\/logo$/);
    if(logoPath){const project=demoProjects.find(p=>p.id===logoPath[1]);if(!project)throw new ApiError(404,'not_found');if(options.method){if(project.version!==options.body.expected_version)throw new ApiError(409,'version_conflict');if(options.method==='DELETE')delete project.brand.logo_asset_id;else project.brand.logo_asset_id='a'.repeat(64);project.version++;}return{project:structuredClone(project),logo_id:project.brand.logo_asset_id||null,png_base64:project.brand.logo_asset_id?'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a8H8AAAAASUVORK5CYII=':null};}
    if (path === '/projects' && !options.body) return {items: structuredClone(demoProjects)};
    if (/^\/projects\/[^/]+\/entry$/.test(path) && !options.body) return {order: null, entry_fee_kopecks: ENTRY_FEE, payments_available: false, launch_available: false};
    if (path === '/projects' && options.method === 'POST') {
      if (demoProjects.some(item => item.slug === options.body.slug)) throw new ApiError(409, 'slug_taken');
      const project = {...options.body, price_kopecks: Math.round(Number(options.body.price) * 100), id: crypto.randomUUID(), version: 1, support_url: '', news_url: '', brand: {accent_color: '#e6c77a'}, bot: null}; delete project.request_key; delete project.price; demoProjects.push(project); return structuredClone(project);
    }
    const reportPath = path.match(/^\/projects\/([^/]+)\/(overview|customers|orders)$/);
    if (reportPath) {
      if (!demoProjects.some(item => projectId(item) === reportPath[1])) throw new ApiError(404, 'not_found');
      if (reportPath[1] === demoProjects[0]?.id) return structuredClone(demoReports[reportPath[2]]);
      return reportPath[2] === 'overview' ? {customers: 0, active_subscriptions: 0, pending_activations: 0, periods: Object.fromEntries(['today', 'days7', 'days30'].map(key => [key, {sales: 0, revenue_kopecks: 0, partner_kopecks: 0, platform_kopecks: 0}])), timezone: 'UTC', sales_enabled: false} : {items: [], truncated: false};
    }
    const project = demoProjects.find(item => path === `/projects/${projectId(item)}` || path === `/projects/${projectId(item)}/bot`);
    if (!project) throw new ApiError(404, 'not_found');
    if (!options.body) return structuredClone(project);
    if (options.body.expected_version !== project.version) throw new ApiError(409, 'version_conflict');
    if (path.endsWith('/bot')) project.bot = {username: `${project.slug.replaceAll('-', '_')}_demo_bot`, title: project.title};
    else { Object.assign(project, options.body); project.price_kopecks = Math.round(Number(options.body.price) * 100); delete project.price; }
    delete project.expected_version; project.version += 1; return structuredClone(project);
  }
  boot();
})();
