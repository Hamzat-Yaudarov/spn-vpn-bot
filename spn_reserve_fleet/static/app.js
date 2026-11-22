'use strict';

const $ = (id) => document.getElementById(id);
let csrf = '';
let authenticated = false;
let refreshing = false;
const busy = new Set();
let provisioningOptions = null;
let provisioningJobs = [];
let provisioningServer = null;
let provisioningSubmitting = false;
let provisioningRefreshing = false;
let verifiedSSH = null;
let createRequest = null;
const jobBusy = new Set();
const activeJobStatuses = new Set(['queued', 'running']);
const jobStatusLabels = {queued:'В очереди', running:'Выполняется', needs_credentials:'Нужен SSH-доступ',
  waiting_cdn:'Настройте CDN', failed:'Нужна проверка', succeeded:'Завершено'};
const statusLabels = {healthy:'SSH доступен', up:'SSH доступен', online:'SSH доступен', available:'SSH доступен',
  reachable:'SSH доступен', ok:'SSH доступен', down:'Нет SSH', failed:'Нет SSH', unavailable:'Нет SSH',
  blocked:'Нужна проверка', error:'Ошибка', switching:'Смена IP', switching_failed:'Ошибка смены',
  offline:'Нет SSH', pending:'Незавершённая смена', archived:'В архиве', unknown:'Ожидает проверки', degraded:'Проверяем повторно', no_reserves:'Нет резерва', paused:'На паузе'};
const goodStatuses = new Set(['healthy', 'up', 'online', 'available', 'reachable', 'ok']);
const badStatuses = new Set(['down', 'offline', 'pending', 'failed', 'unavailable', 'blocked', 'error', 'switching_failed']);

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function date(value, full = false) {
  if (!value) return 'ещё не проверен';
  const parsed = new Date(typeof value === 'number' ? (value < 1e12 ? value * 1000 : value) : value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString('ru-RU', full ? {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit'} : {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}

function notice(text, success = false) {
  $('notice').textContent = text;
  $('notice').className = success ? 'notice ok' : 'notice';
  $('notice').hidden = !text;
}

async function api(path, method = 'GET', payload) {
  const options = {method, credentials:'same-origin', headers:{}};
  if (method !== 'GET') {
    options.headers['X-CSRF-Token'] = csrf;
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(payload || {});
  }
  const response = await fetch(path, options);
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/login') showLogin();
    const message = typeof result.message === 'string' ? result.message : typeof result.detail === 'string' ? result.detail : 'Не удалось выполнить запрос';
    throw new Error(message);
  }
  return result;
}

function showLogin() {
  authenticated = false;
  clearProvisioningCredentials();
  $('provisioning-panel').hidden = true;
  $('dashboard').hidden = true;
  $('logout').hidden = true;
  $('login-view').hidden = false;
}

function badge(status) {
  const type = goodStatuses.has(status) ? 'ok' : badStatuses.has(status) ? 'bad' : 'warn';
  return node('span', 'badge ' + type, statusLabels[status] || 'Ожидает проверки');
}

function button(text, className, action) {
  const result = node('button', 'button ' + (className || ''), text);
  result.type = 'button';
  result.addEventListener('click', action);
  return result;
}

function secureProvisioning() {
  return globalThis.location?.protocol === 'https:';
}

function provisioningEnabled() {
  return provisioningOptions?.enabled === true && secureProvisioning();
}

function clearProvisioningCredentials() {
  $('provisioning-password').value = '';
  $('provisioning-jobs').querySelectorAll('input').forEach((input) => {
    if (input.type === 'password') input.value = '';
  });
}

function formError(message) {
  $('provisioning-error').textContent = message;
  $('provisioning-error').hidden = !message;
}

function sshAddress() {
  return $('provisioning-ip').value.trim() + ':' + $('provisioning-port').value;
}

function resetSSHTrust() {
  verifiedSSH = null;
  $('trust-ssh-key').checked = false;
  $('trust-ssh-key').disabled = true;
  $('ssh-fingerprint').textContent = '';
  $('ssh-fingerprint').hidden = true;
  updateProvisioningSubmit();
}

function updateProvisioningSubmit() {
  $('submit-provisioning').disabled = provisioningSubmitting || !provisioningEnabled() ||
    !verifiedSSH || verifiedSSH.address !== sshAddress() || !$('trust-ssh-key').checked;
}

function updateTransportFields() {
  const cdn = !provisioningServer && $('provisioning-transport').value === 'cdn';
  $('cdn-domain-field').hidden = !cdn;
  $('provisioning-cdn-domain').required = false;
  $('domain-help').textContent = cdn ? 'Домен источника. Если оставить пустым, создастся автоматически.' :
    'Если оставить пустым, домен создастся автоматически.';
}

function applyProvisioningOptions(options) {
  provisioningOptions = options;
  const enabled = provisioningEnabled();
  $('create-server').disabled = !enabled;
  const reason = !secureProvisioning() ? 'Создание и перенос серверов доступны через HTTPS.' :
    options?.disabled_reason || 'Создание серверов пока недоступно.';
  $('provisioning-unavailable').hidden = enabled;
  $('provisioning-unavailable').textContent = enabled ? '' : reason;
  const select = $('provisioning-transport');
  select.querySelectorAll('option').forEach((option) => {
    option.disabled = options?.capabilities?.['create_' + option.value] !== true;
  });
  updateProvisioningSubmit();
}

function serverJob(server) {
  return server.operational_job_id || provisioningJobs.find((job) => job.server_id === server.id &&
    (activeJobStatuses.has(job.status) || job.status === 'needs_credentials' || job.status === 'waiting_cdn' ||
      job.status === 'failed' && job.can_resume === true))?.id;
}

function serverTransport(server) {
  const transport = String(server.transport || '').toLowerCase();
  return transport.includes('cdn') ? 'cdn' : transport.includes('tls') ? 'tls' : 'reality';
}

function moveReason(server) {
  if (!provisioningEnabled()) return 'Перенос пока недоступен.';
  if (server.archived) return 'Эта установка в архиве.';
  if (server.pending_ip) return 'Сначала завершите текущую смену IP.';
  if (serverJob(server)) return 'Сначала завершите текущую установку или перенос.';
  const transport = serverTransport(server);
  if (!provisioningOptions.capabilities?.['replace_' + transport]) return 'Перенос этого подключения пока недоступен.';
  return '';
}

function openProvisioning(server = null) {
  if (!provisioningEnabled() || provisioningSubmitting) return;
  if (server && moveReason(server)) return;
  provisioningServer = server;
  createRequest = null;
  $('provisioning-form').reset();
  $('provisioning-password').value = '';
  $('provisioning-ip').value = '';
  $('provisioning-port').value = String(server ? server.ssh_port || 22 : provisioningOptions.defaults?.ssh_port || 22);
  $('provisioning-port').readOnly = Boolean(server);
  $('provisioning-transport').value = provisioningOptions.defaults?.transport || 'tls';
  $('provisioning-panel').querySelectorAll('.create-field').forEach((field) => {field.hidden = Boolean(server);});
  $('provisioning-name').required = !server;
  $('move-warning').hidden = !server;
  $('move-warning').textContent = server && serverTransport(server) === 'cdn' ?
    'Домен источника, CDN-домен и подписка сохранятся. При переносе текущие соединения оборвутся; обновление источника CDN может занять некоторое время.' :
    'Домен и подписка сохранятся. При переносе текущие соединения оборвутся; обновление DNS у клиентов может занять некоторое время.';
  $('move-source').hidden = !server;
  $('move-source').textContent = server ? (server.name || server.domain) + ' · ' +
    (serverTransport(server) === 'cdn' ? 'CDN: ' + (server.cdn_domain || server.domain) + ' · Источник: ' : '') +
    server.domain + ' · ' + server.current_ip : '';
  $('provisioning-title').textContent = server ? 'Перенести на новую VPS' : 'Новый сервер';
  $('submit-provisioning').textContent = server ? 'Начать перенос' : 'Создать сервер';
  const squads = Array.isArray(provisioningOptions.squads) ? provisioningOptions.squads : [];
  $('provisioning-squads').replaceChildren(...squads.map((squad) => {
    const label = node('label', 'check-label');
    const input = node('input');
    input.type = 'checkbox';
    input.value = squad.id;
    input.name = 'squad';
    label.append(input, node('span', '', squad.name));
    return label;
  }));
  if (!squads.length) $('provisioning-squads').append(node('p', 'field-help', 'Нет доступных сквадов. Обновите страницу после их добавления.'));
  updateTransportFields();
  resetSSHTrust();
  formError('');
  $('provisioning-panel').hidden = false;
  $('provisioning-panel').scrollIntoView?.({behavior:'smooth', block:'start'});
  $('provisioning-ip').focus?.();
}

async function readSSHKey() {
  if (!provisioningEnabled() || provisioningSubmitting) return;
  const ip = $('provisioning-ip').value.trim();
  const ssh_port = Number($('provisioning-port').value);
  if (!ip || !Number.isInteger(ssh_port) || ssh_port < 1 || ssh_port > 65535) {
    formError('Укажите IPv4 и SSH-порт новой VPS.');
    return;
  }
  resetSSHTrust();
  const address = sshAddress();
  $('read-ssh-key').disabled = true;
  formError('');
  try {
    const result = await api('/api/provisioning/ssh-key', 'POST', {ip, ssh_port});
    if (address !== sshAddress()) return;
    if (typeof result.fingerprint !== 'string' || !/^SHA256:[A-Za-z0-9+/]{43}$/.test(result.fingerprint)) {
      throw new Error('Сервер не вернул корректный отпечаток SSH.');
    }
    verifiedSSH = {address, fingerprint:result.fingerprint};
    $('ssh-fingerprint').textContent = result.fingerprint;
    $('ssh-fingerprint').hidden = false;
    $('trust-ssh-key').disabled = false;
  } catch (error) {
    formError(error.message);
  } finally {
    $('read-ssh-key').disabled = false;
    updateProvisioningSubmit();
  }
}

function provisioningPayload() {
  const moving = Boolean(provisioningServer);
  return {kind:moving ? 'replace' : 'create', server_id:moving ? provisioningServer.id : null,
    ip:$('provisioning-ip').value.trim(), ssh_port:Number($('provisioning-port').value),
    password:$('provisioning-password').value, host_key_sha256:verifiedSSH?.fingerprint || null,
    domain:moving ? null : $('provisioning-domain').value.trim() || null,
    name:moving ? null : $('provisioning-name').value.trim() || null,
    country:moving ? null : $('provisioning-country').value.trim().toUpperCase() || null,
    transport:moving ? null : $('provisioning-transport').value,
    squad_ids:moving ? [] : [...$('provisioning-squads').querySelectorAll('input')].filter((input) => input.checked).map((input) => input.value),
    cdn_domain:!moving && $('provisioning-transport').value === 'cdn' ? $('provisioning-cdn-domain').value.trim() || null : null};
}

function setProvisioningBusy(value) {
  provisioningSubmitting = value;
  $('provisioning-form').querySelectorAll('input,select,button').forEach((element) => {element.disabled = value;});
  $('close-provisioning').disabled = value;
  if (!value) {
    $('trust-ssh-key').disabled = !verifiedSSH;
    applyProvisioningOptions(provisioningOptions);
  }
}

async function submitProvisioning(event) {
  event.preventDefault();
  if (provisioningSubmitting || !provisioningEnabled()) return;
  if (!verifiedSSH || verifiedSSH.address !== sshAddress() || !$('trust-ssh-key').checked) {
    formError('Получите отпечаток SSH и подтвердите ключ новой VPS.');
    return;
  }
  const payload = provisioningPayload();
  if (!provisioningServer && !payload.squad_ids.length) {
    formError('Выберите хотя бы один сквад.');
    return;
  }
  const key = JSON.stringify({...payload, password:''});
  if (!createRequest || createRequest.key !== key) createRequest = {key, id:globalThis.crypto.randomUUID()};
  payload.request_id = createRequest.id;
  setProvisioningBusy(true);
  formError('');
  try {
    const request = api('/api/provisioning/jobs', 'POST', payload);
    $('provisioning-password').value = '';
    payload.password = '';
    const job = await request;
    provisioningJobs = [job, ...provisioningJobs.filter((item) => item.id !== job.id)];
    $('provisioning-panel').hidden = true;
    resetSSHTrust();
    renderJobs();
    notice(provisioningServer ? 'Перенос запущен. Ход операции появится ниже.' : 'Установка запущена. Ход операции появится ниже.', true);
    await refresh(true);
  } catch (error) {
    formError(error.message);
  } finally {
    payload.password = '';
    $('provisioning-password').value = '';
    setProvisioningBusy(false);
  }
}

function jobField(labelText, type, placeholder = '') {
  const label = node('label', 'form-field', labelText);
  const input = node('input');
  input.type = type;
  input.placeholder = placeholder;
  input.autocomplete = type === 'password' ? 'new-password' : 'off';
  input.maxLength = type === 'password' ? 1024 : 253;
  label.append(input);
  return {label, input};
}

function renderJob(job) {
  const card = node('article', 'job-card' + (job.status === 'succeeded' ? ' job-complete' : ''));
  card.id = 'job-' + job.id;
  const top = node('div', 'job-heading');
  const title = node('div');
  const isCDN = job.transport === 'cdn' || Boolean(job.cdn);
  title.append(node('h3', '', (job.kind === 'replace' ? 'Перенос' : isCDN ? 'Yandex CDN' : 'Установка') + ' · ' + (job.domain || job.target_ip || 'Новый сервер')),
    node('p', 'muted small', (job.target_ip || '') + (job.updated_at ? ' · ' + date(job.updated_at, true) : '')));
  top.append(title, node('span', 'badge ' + (job.status === 'succeeded' ? 'ok' : job.status === 'failed' ? 'bad' : 'warn'), jobStatusLabels[job.status] || 'Ожидает проверки'));
  card.append(top);
  if (job.stage_label || job.stage) card.append(node('p', 'job-stage', job.stage_label || job.stage));
  if (typeof job.progress === 'number' && Number.isFinite(job.progress)) {
    const progress = node('progress', 'job-progress');
    progress.max = 100;
    progress.value = Math.min(100, Math.max(0, job.progress));
    progress.setAttribute('aria-label', 'Ход операции');
    card.append(progress);
  }
  if (job.error) card.append(node('p', 'server-error', job.error));
  if (/^[0-9a-f]{32}$/.test(job.log_id || '')) card.append(node('p', 'error-reference', 'Журнал операции: ' + job.log_id.slice(0, 8)));
  if (job.status === 'succeeded') {
    if (isCDN) card.append(node('p', 'job-note', 'Подключение через CDN создано. Автоматическая смена резервных IP для него пока недоступна.'));
    if (job.result_server_id) {
      const link = node('a', 'replacement-link', 'Открыть сервер');
      link.href = '#server-' + encodeURIComponent(job.result_server_id);
      card.append(link);
    }
    return card;
  }
  if (activeJobStatuses.has(job.status)) {
    card.append(node('p', 'job-note', 'Можно закрыть страницу. Операция продолжится на контроллере.'));
    return card;
  }
  const waitingCDN = job.status === 'waiting_cdn';
  if (waitingCDN && job.cdn) {
    const details = node('div', 'cdn-details');
    const pairs = [['Источник', job.cdn.origin_domain], ['Домен CDN', job.cdn.cdn_domain],
      ['Путь подключения', job.cdn.transport_path], ['Сертификат', job.cdn.certificate_name]];
    pairs.forEach(([label, value]) => {if (value) details.append(node('p', '', label + ': ' + value));});
    if (job.cdn.guide_text) details.append(node('p', 'cdn-guide', job.cdn.guide_text));
    card.append(details);
  }
  if (!job.can_resume && !waitingCDN && job.status !== 'needs_credentials') return card;
  const form = node('form', 'job-resume');
  const fields = node('div', 'provisioning-grid');
  let password, certificate, cdn, finish;
  if (!waitingCDN || job.needs_password) {
    password = jobField('Пароль root', 'password');
    password.input.required = job.needs_password === true || job.status === 'needs_credentials';
    fields.append(password.label);
  }
  if (waitingCDN) {
    certificate = jobField('CNAME подтверждения сертификата', 'text', '…cm.yandexcloud.net');
    cdn = jobField('CNAME ресурса CDN', 'text', '…yccdn.ru');
    fields.append(certificate.label, cdn.label);
    const label = node('label', 'check-label finish-cdn');
    finish = node('input');
    finish.type = 'checkbox';
    label.append(finish, node('span', '', 'CDN настроен, сертификат выпущен. Проверить и завершить установку.'));
    form.append(node('p', 'job-note', 'Сначала можно передать только CNAME сертификата. После выпуска сертификата настройте CDN и передайте CNAME ресурса.'));
    form.append(fields, label);
  } else {
    form.append(fields);
    form.append(node('p', 'job-note', 'Повтор продолжит сохранённую операцию на тот же IP. Пароль потребуется снова, если SSH-ключ ещё не добавлен.'));
  }
  const error = node('p', 'form-error');
  error.hidden = true;
  const submit = node('button', 'button primary', waitingCDN ? 'Продолжить настройку CDN' : 'Продолжить');
  submit.type = 'submit';
  submit.disabled = !provisioningEnabled() || jobBusy.has(job.id);
  form.append(error, submit);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (jobBusy.has(job.id) || !provisioningEnabled()) return;
    const payload = {password:password?.input.value || '', certificate_target:certificate?.input.value.trim() || null,
      cdn_target:cdn?.input.value.trim() || null, finish:finish?.checked === true};
    jobBusy.add(job.id);
    form.querySelectorAll('input,button').forEach((input) => {input.disabled = true;});
    error.hidden = true;
    try {
      const request = api('/api/provisioning/jobs/' + encodeURIComponent(job.id) + '/resume', 'POST', payload);
      if (password) password.input.value = '';
      payload.password = '';
      const updated = await request;
      provisioningJobs = provisioningJobs.map((item) => item.id === updated.id ? updated : item);
      renderJobs(true);
      notice('Операция продолжена.', true);
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
      notice(failure.message);
    } finally {
      payload.password = '';
      if (password) password.input.value = '';
      jobBusy.delete(job.id);
      form.querySelectorAll('input,button').forEach((input) => {input.disabled = false;});
    }
  });
  card.append(form);
  return card;
}

function renderJobs(force = false) {
  const container = $('provisioning-jobs');
  if (!force && container.contains?.(document.activeElement)) return;
  $('jobs-section').hidden = !provisioningJobs.length;
  $('job-count').textContent = String(provisioningJobs.length);
  const previous = new Map([...container.children].map((card) => [card.id, card]));
  const cards = provisioningJobs.map((job) => {
    const signature = JSON.stringify(job);
    const existing = previous.get('job-' + job.id);
    if (!force && existing?.jobSignature === signature) return existing;
    const card = renderJob(job);
    card.jobSignature = signature;
    return card;
  });
  container.replaceChildren(...cards);
}

async function refreshProvisioning(initial = false) {
  if (!authenticated || provisioningRefreshing) return;
  provisioningRefreshing = true;
  try {
    if (initial || !provisioningOptions) applyProvisioningOptions(await api('/api/provisioning/options'));
    const result = await api('/api/provisioning/jobs');
    provisioningJobs = Array.isArray(result.jobs) ? result.jobs : [];
    renderJobs();
  } catch (error) {
    if (initial) {
      applyProvisioningOptions({enabled:false, disabled_reason:error.message});
    }
  } finally {
    provisioningRefreshing = false;
  }
}

async function action(server, endpoint, method, payload, message) {
  if (server.archived || serverJob(server) || busy.has(server.id)) return;
  busy.add(server.id);
  const card = document.getElementById('server-' + server.id);
  if (card) {
    card.classList.add('busy');
    card.querySelectorAll('button,input').forEach((element) => {element.disabled = true;});
  }
  notice(endpoint === 'switch' ? 'Меняем IP. Оставьте программу запущенной до завершения операции.' : 'Выполняем действие…', true);
  try {
    await api('/api/servers/' + encodeURIComponent(server.id) + '/' + endpoint, method, payload);
    notice(message, true);
  } catch (error) {
    notice(error.message);
  } finally {
    busy.delete(server.id);
    await refresh(true);
  }
}

function renderServer(server) {
  const archived = server.archived === true;
  const transport = serverTransport(server);
  const isCDN = transport === 'cdn';
  const pending = server.pending_ip || null;
  const operation = !archived && serverJob(server);
  const card = node('article', 'server-card' + (archived ? ' archived-card' : pending ? ' pending-switch' : ''));
  card.id = 'server-' + server.id;
  const main = node('div', 'server-main');
  const top = node('div', 'server-top');
  const heading = node('div');
  heading.append(node('h3', 'server-name', server.name || server.domain),
    node('div', 'domain', isCDN ? 'CDN: ' + (server.cdn_domain || server.domain) : server.domain));
  if (isCDN) heading.append(node('div', 'domain origin-domain', 'Источник: ' + server.domain));
  top.append(heading, badge(archived ? 'archived' : server.status));
  const current = node('div', 'current-ip');
  const address = node('div');
  address.append(node('span', 'field-title', archived ? 'Последний текущий IP' : isCDN ? 'IP источника' : 'Текущий IP'), node('div', 'ip-value', server.current_ip));
  current.append(address, node('span', 'transport', 'VLESS · ' + (isCDN ? 'CDN' : transport === 'tls' ? 'TLS' : 'REALITY')));
  const meta = node('div', 'server-meta');
  meta.append(node('span', '', 'SSH :' + (server.ssh_port || 22)), node('span', '', 'Проверка: ' + date(server.last_checked)), node('span', '', 'Отказов подряд: ' + (server.failures || 0)));
  main.append(top, current, meta);
  if (operation) {
    const ongoing = node('p', 'pending-target', 'Выполняется установка или перенос. Управление IP временно недоступно. ');
    const link = node('a', 'replacement-link', 'Открыть операцию');
    link.href = '#job-' + encodeURIComponent(operation);
    ongoing.append(link);
    main.append(ongoing);
  }
  if (archived) {
    const archive = node('div', 'archive-reason');
    archive.append(node('p', '', server.archive_reason || 'Эта установка выведена из эксплуатации.'));
    if (server.replacement_id) {
      const replacement = node('a', 'replacement-link', 'Рабочая установка: ' + server.replacement_id);
      replacement.href = '#server-' + encodeURIComponent(server.replacement_id);
      archive.append(replacement);
    }
    main.append(archive);
  }
  if (pending) {
    main.append(node('p', archived ? 'pending-history' : 'pending-target',
      archived ? 'В истории сохранена незавершённая смена на ' + pending : 'Продолжить смену на ' + pending));
  }
  if (server.last_error) main.append(node('p', 'server-error', server.last_error));
  if (server.last_error && !pending && server.last_switch_target) {
    main.append(node('p', 'error-reference', 'Последняя попытка смены: ' + server.last_switch_target));
  }
  if (server.last_error && /^[0-9a-f]{32}$/.test(server.last_error_log_id || '')) {
    const reference = node('p', 'error-reference', 'Журнал операции: ' + server.last_error_log_id.slice(0, 8));
    reference.title = 'Идентификатор журнала: ' + server.last_error_log_id;
    main.append(reference);
  }
  if (isCDN) {
    main.append(node('p', 'cdn-origin-note', 'Доступность origin по SSH. Перенос сохраняет CDN-домен.'));
    const bottom = node('div', 'server-bottom cdn-bottom');
    if (archived) {
      bottom.className = 'server-bottom archive-footer';
      bottom.textContent = 'Мониторинг и управление отключены. Журналы сохранены.';
      card.append(main, bottom);
      return card;
    }
    bottom.append(button('Проверить SSH', 'quiet', () => action(server, 'check', 'POST', {}, 'Проверка SSH источника завершена.')));
    card.append(main, bottom);
    return appendServerMove(card, server);
  }
  const reserves = Array.isArray(server.reserves) ? server.reserves : [];
  main.append(node('h4', 'reserve-heading', 'РЕЗЕРВНЫЕ IP · ' + reserves.length));
  const list = node('div', 'reserve-list');
  if (!reserves.length) list.append(node('p', 'no-reserve', 'Добавьте адреса, уже назначенные этой VPS у хостера.'));
  reserves.forEach((reserve) => {
    const row = node('div', 'reserve-row' + (!archived && reserve.ip === pending ? ' resume-target' : ''));
    const dot = node('span', 'reserve-dot ' + (goodStatuses.has(reserve.status) ? 'ok' : badStatuses.has(reserve.status) ? 'bad' : ''));
    dot.title = (statusLabels[reserve.status] || 'Ожидает проверки') + ' · ' + date(reserve.last_checked);
    row.append(dot, node('span', 'reserve-address', reserve.ip));
    if (!archived) {
      const controls = node('div', 'reserve-actions');
      const isTarget = reserve.ip === pending;
      const assign = button(isTarget ? 'Продолжить' : 'Назначить', isTarget ? 'primary' : '',
        () => action(server, 'switch', 'POST', {ip:reserve.ip}, 'Операция смены IP завершена.'));
      assign.disabled = Boolean(pending && !isTarget);
      if (assign.disabled) assign.title = 'Сначала завершите смену на ' + pending;
      controls.append(assign);
      const remove = node('button', 'reserve-remove', '×');
      remove.type = 'button';
      remove.title = pending ? 'Сначала завершите смену на ' + pending : 'Убрать из резерва панели';
      remove.disabled = Boolean(pending);
      remove.setAttribute('aria-label', 'Убрать ' + reserve.ip + ' из резерва');
      remove.addEventListener('click', () => action(server, 'reserves/' + encodeURIComponent(reserve.ip), 'DELETE', {}, 'Адрес убран из резерва панели.'));
      controls.append(remove);
      row.append(controls);
    }
    list.append(row);
  });
  main.append(list);
  if (archived) {
    const bottom = node('div', 'server-bottom archive-footer', 'Мониторинг и изменение IP отключены. Журналы сохранены.');
    card.append(main, bottom);
    return card;
  }
  const form = node('form', 'reserve-form');
  const input = node('input');
  input.type = 'text';
  input.inputMode = 'decimal';
  input.placeholder = 'Резервный IPv4';
  input.required = true;
  input.maxLength = 15;
  input.autocomplete = 'off';
  input.disabled = Boolean(pending);
  input.setAttribute('aria-label', 'Резервный IPv4 для ' + (server.name || server.domain));
  const add = node('button', 'button quiet', '+ Добавить');
  add.type = 'submit';
  add.disabled = Boolean(pending);
  if (pending) form.title = 'Сначала завершите смену на ' + pending;
  form.append(input, add);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    action(server, 'reserves', 'POST', {ip:input.value.trim()}, 'Резервный IP добавлен.');
  });
  main.append(form);
  const bottom = node('div', 'server-bottom');
  const label = node('label', 'auto-label');
  const toggle = node('input', 'switch');
  toggle.type = 'checkbox';
  toggle.checked = server.auto_enabled === true;
  toggle.disabled = Boolean(pending);
  if (pending) label.title = 'Автосмена недоступна до завершения смены на ' + pending;
  toggle.addEventListener('change', () => action(server, 'auto', 'PUT', {enabled:toggle.checked}, toggle.checked ? 'Автоматическая смена IP включена.' : 'Автоматическая смена IP выключена.'));
  label.append(toggle, node('span', '', 'Автоматическая смена'));
  bottom.append(label, button('Проверить SSH', 'quiet', () => action(server, 'check', 'POST', {}, 'Проверка SSH завершена.')));
  card.append(main, bottom);
  return appendServerMove(card, server);
}

function appendServerMove(card, server) {
  const move = button('Перенести на новую VPS', 'quiet', () => openProvisioning(server));
  const reason = moveReason(server);
  move.disabled = Boolean(reason);
  if (reason) move.title = reason;
  const actions = node('div', 'server-move');
  actions.append(move);
  card.append(actions);
  if (busy.has(server.id) || serverJob(server)) {
    card.classList.add('busy');
    card.querySelectorAll('button,input').forEach((element) => {element.disabled = true;});
  }
  return card;
}

function render(state) {
  const all = Array.isArray(state.servers) ? state.servers : [];
  const servers = all.filter((server) => !server.archived);
  const archives = [...(Array.isArray(state.archived_servers) ? state.archived_servers : []),
    ...all.filter((server) => server.archived)];
  $('servers').replaceChildren(...servers.map(renderServer));
  $('archive-section').hidden = archives.length === 0;
  $('archive-count').textContent = String(archives.length);
  $('archived-servers').replaceChildren(...archives.map((server) => renderServer({...server, archived:true})));
  $('total-count').textContent = String(servers.length);
  $('server-count').textContent = String(servers.length);
  $('online-count').textContent = String(servers.filter((server) => goodStatuses.has(server.status)).length);
  $('attention-count').textContent = String(servers.filter((server) => badStatuses.has(server.status)).length);
  $('reserve-count').textContent = String(servers.filter((server) => serverTransport(server) !== 'cdn')
    .reduce((sum, server) => sum + (server.reserves || []).length, 0));
  $('empty').hidden = servers.length !== 0;
  const events = Array.isArray(state.events) ? state.events : [];
  const rows = events.slice(0, 100).map((entry) => {
    const row = node('div', 'event-row');
    const text = node('div', 'event-text');
    text.append(node('span', '', entry.message || entry.kind || 'Событие'));
    if (entry.server_id) text.append(node('span', 'event-server', entry.server_id));
    row.append(node('span', 'event-marker'), text, node('time', 'event-time', date(entry.created_at || entry.time, true)));
    return row;
  });
  $('events').replaceChildren(...(rows.length ? rows : [node('p', 'no-events', 'Событий пока нет. Здесь появятся результаты проверок и смены IP.')]));
  $('updated').textContent = 'Обновлено ' + date(Date.now());
}

async function refresh(force = false) {
  if (!authenticated || refreshing) return;
  if (!force && (busy.size || document.activeElement.tagName === 'INPUT')) return;
  refreshing = true;
  try {
    render(await api('/api/overview'));
  } catch (error) {
    notice(error.message);
  } finally {
    refreshing = false;
  }
}

async function openDashboard() {
  authenticated = true;
  $('login-view').hidden = true;
  $('dashboard').hidden = false;
  $('logout').hidden = false;
  await refreshProvisioning(true);
  await refresh(true);
}

$('create-server').addEventListener('click', () => openProvisioning());
$('close-provisioning').addEventListener('click', () => {
  if (provisioningSubmitting) return;
  $('provisioning-password').value = '';
  resetSSHTrust();
  $('provisioning-panel').hidden = true;
});
$('provisioning-ip').addEventListener('input', resetSSHTrust);
$('provisioning-port').addEventListener('input', resetSSHTrust);
$('provisioning-transport').addEventListener('change', updateTransportFields);
$('trust-ssh-key').addEventListener('change', updateProvisioningSubmit);
$('read-ssh-key').addEventListener('click', readSSHKey);
$('provisioning-form').addEventListener('submit', submitProvisioning);

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('login-button').disabled = true;
  notice('');
  try {
    const guest = await api('/api/session');
    csrf = guest.csrf;
    const result = await api('/api/login', 'POST', {password:$('password').value});
    csrf = result.csrf;
    $('password').value = '';
    await openDashboard();
  } catch (error) {
    notice(error.message);
  } finally {
    $('login-button').disabled = false;
  }
});

$('logout').addEventListener('click', async () => {
  try {
    await api('/api/logout', 'POST', {});
    notice('');
    showLogin();
    const guest = await api('/api/session');
    csrf = guest.csrf;
  } catch (error) {
    notice(error.message);
  }
});

(async () => {
  try {
    const session = await api('/api/session');
    csrf = session.csrf;
    $('setup-note').hidden = session.configured;
    if (session.authenticated) await openDashboard();
    else showLogin();
  } catch (error) {
    notice(error.message);
  } finally {
    $('loading').hidden = true;
  }
})();
setInterval(() => refresh(), 10000);
setInterval(() => refreshProvisioning(), 3000);
