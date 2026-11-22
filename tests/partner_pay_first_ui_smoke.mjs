// Enrollment only. Real UI, intercepted I/O, fake clock. No live payments or Telegram.
import assert from 'node:assert/strict';
import {readFile, mkdir} from 'node:fs/promises';
import {setTimeout as delay} from 'node:timers/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const root = process.cwd(), origin = 'http://127.0.0.1:19878';
const runtime = '/Users/khamzat/.cache/codex-runtimes/codex-primary-runtime/dependencies/node';
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || `${runtime}/node_modules/playwright/index.mjs`);
const browser = await chromium.launch({executablePath: process.env.CHROME_EXECUTABLE || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true});
const PID = '8175b112-bc10-4468-a5d0-a0898d620db3', OTHER = '6a6bf206-52f7-4669-bfe4-b5e16ae26047';
const OID = 'f378dc14-c7df-4a34-8be6-8201a163f9a3';
const PAYMENT = 'https://payments.example.test/saved-invoice', CREATE = 'https://t.me/newbot/OfflineManagerBot?name=My%20service';
const out = '/private/tmp/spn-partner-payfirst-preview';
await mkdir(out, {recursive: true});
let assertions = 0;
function check(value, message) { assert.ok(value, message); assertions++; }
async function until(fn, message) { for (let i = 0; i < 200; i++) { if (fn()) return; await delay(10); } throw Error(message); }
const project = (active = false, connected = false) => ({id: PID, owner_tg_id: 42, version: connected ? 2 : 1, slug: 'service-one', title: 'Мой сервис', price_kopecks: 19900, status: active ? 'active' : connected ? 'ready' : 'draft', workspace_available: active, bot: connected ? {id: 123456789, username: 'OfflineServiceBot'} : null});
const order = (status = 'created', fee = 999000) => ({id: OID, price_kopecks: fee, status, created_at: '2026-10-25T10:00:00Z', paid_at: status.startsWith('paid') || status === 'launched' ? '2026-10-25T10:01:00Z' : null, confirmation_url: status === 'created' ? PAYMENT : null});

async function fixture({width = 390, existing = false, savedOrder = null, connected = false, active = false, available = true, auth = true, fee = 999000, actor = 42, initialKey = null, failCheckout = false, badCreation = false, failEntry = false, multiple = false, failManaged = false, launchState = null, projectStatus = null} = {}) {
  const context = await browser.newContext({viewport: {width, height: 844}});
  const state = {calls: [], errors: [], external: [], project: existing || savedOrder || active ? project(active, connected || active) : null, order: savedOrder, checkouts: 0, polls: 0, managed: 0, managedPolls: 0, maxActive: 0, activePolls: 0, hold: null, checkoutHold: null, managedHold: null, managedPollHold: null, managedStatus: null, activeManagedPolls: 0, maxActiveManagedPolls: 0, status: null, returnId: PID, connected, active, failCheckout, failManaged};
  if (projectStatus) state.project = {...(state.project || project()), status: projectStatus};
  await context.addInitScript(({auth, actor, initialKey}) => {
    window.__opened = []; window.__testHidden = false;
    if (auth) window.Telegram = {WebApp: {initData: 'offline-payfirst-initdata', ready() {}, expand() {}, openLink(url) { window.__opened.push(url); }, openTelegramLink(url) { window.__opened.push(url); }}};
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => window.__testHidden});
    if (initialKey) localStorage.setItem(`spn-partner-enrollment:${actor}`, initialKey);
  }, {auth, actor, initialKey});
  const envelope = () => ({entry_fee_kopecks: fee, payments_available: available, launch_available: true, order: structuredClone(state.order), ...(launchState ? {launch_state: launchState} : {})});
  await context.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url()), method = req.method();
    if (url.origin !== origin) { if (auth || req.url() !== 'https://telegram.org/js/telegram-web-app.js') state.external.push(req.url()); return route.abort(); }
    if (!url.pathname.startsWith('/partners/api/')) {
      const file = url.pathname === '/partners' ? 'index.html' : url.pathname.replace('/partners/', '');
      if (!['index.html', 'assets/partners.css', 'assets/partners.js', 'assets/workspace.js'].includes(file)) return route.fulfill({status: 404, body: ''});
      return route.fulfill({contentType: file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html', body: await readFile(path.join(root, 'static/partners', file))});
    }
    const endpoint = url.pathname.replace('/partners/api', ''), body = req.postData() ? JSON.parse(req.postData()) : null;
    state.calls.push({endpoint, method, body});
    check(req.headers().authorization === 'tma offline-payfirst-initdata', 'Only authenticated central initData header');
    check(!url.searchParams.has('token') && !url.searchParams.has('initData'), 'No credentials in URLs');
    const ok = json => route.fulfill({json});
    if (endpoint === '/settings') return ok({enabled: true, surface: 'enrollment', actor_id: actor, entry_fee_kopecks: fee, pay_first_enabled: true, managed_bot_creation_enabled: true, managed_bot_creation_available: available, features: {management: false, finance: false, branding: false}});
    if (endpoint === '/projects' && method === 'GET') return ok({items: state.project ? [state.project, ...(multiple ? [{...project(), id: OTHER}] : [])] : []});
    if (endpoint === `/projects/${PID}/entry` && method === 'GET') {
      if (failEntry) return route.fulfill({status: 503, json: {detail: 'private-provider-secret'}});
      return ok(envelope());
    }
    if (endpoint === `/projects/${OTHER}/entry` && method === 'GET') return ok({...envelope(), order: null});
    if (endpoint === '/enrollment/checkout' && method === 'POST') {
      state.checkouts++;
      check(Object.keys(body).sort().join(',') === 'expected_fee_kopecks,request_key', 'Checkout contains no bot, owner, token or editable price');
      check(body.expected_fee_kopecks === 999000, 'One-click confirms exactly the displayed 9 990 RUB');
      check(/^[0-9a-f-]{36}$/i.test(body.request_key), 'Opaque request UUID');
      if (state.checkoutHold) await state.checkoutHold;
      state.project ||= project();
      if (!state.order || state.order.status === 'canceled') state.order = order();
      if (state.failCheckout) { state.failCheckout = false; return route.abort('failed'); }
      return ok({...envelope(), project: state.project});
    }
    if (endpoint === `/projects/${PID}/entry/${OID}/check` && method === 'POST') {
      check(JSON.stringify(body) === '{}', 'Autocheck only reconciles saved order');
      state.polls++; state.activePolls++; state.maxActive = Math.max(state.maxActive, state.activePolls);
      try {
        if (state.hold) await state.hold;
        if (state.status) return await route.fulfill({status: state.status, json: {detail: 'private-provider-secret'}});
        return await ok(envelope());
      } finally { state.activePolls--; }
    }
    if (endpoint === `/projects/${PID}/managed-bot` && method === 'POST') {
      check(state.order?.status === 'paid_pending_launch', 'Managed bot creation always follows confirmed payment');
      check(JSON.stringify(body) === JSON.stringify({expected_version: state.project.version}), 'Saved project version only');
      state.managed++;
      if (state.managedHold) await state.managedHold;
      if (state.failManaged) { state.failManaged = false; return route.fulfill({status: 503, json: {detail: 'private-provider-secret'}}); }
      return ok({status: 'pending', creation_url: badCreation ? 'https://untrusted.example/newbot/BadBot' : CREATE, expires_at: '2026-10-25T12:30:00Z'});
    }
    if (endpoint === `/projects/${PID}` && method === 'GET') {
      state.managedPolls++; state.activeManagedPolls++; state.maxActiveManagedPolls = Math.max(state.maxActiveManagedPolls, state.activeManagedPolls);
      try {
        if (state.managedPollHold) await state.managedPollHold;
        if (state.managedStatus) return await route.fulfill({status: state.managedStatus, json: {detail: 'private-provider-secret'}});
        const current = {...project(state.active, state.connected), id: state.returnId};
        if (state.returnId === PID) state.project = current;
        return await ok(current);
      } finally { state.activeManagedPolls--; }
    }
    if (endpoint === `/projects/${PID}/entry/${OID}/launch` && method === 'POST') {
      check(state.order?.status === 'paid_pending_launch' && state.project.bot, 'Existing connected paid project may finish creation');
      state.order.status = 'launched'; state.active = true; state.project = project(true, true);
      return ok(envelope());
    }
    throw Error('Unexpected operation: ' + method + ' ' + endpoint);
  });
  const page = await context.newPage(); page.on('pageerror', error => state.errors.push(error.message));
  await page.clock.install({time: new Date('2026-10-25T12:00:00Z')});
  await page.clock.pauseAt(new Date('2026-10-25T12:00:01Z'));
  await page.goto(origin + '/partners');
  await page.getByRole('heading', {name: !auth ? 'Откройте из Telegram' : projectStatus === 'suspended' ? 'Сервис приостановлен' : projectStatus === 'closed' ? 'Сервис закрыт' : active ? 'Ваш сервис создан' : 'Создай свой сервис под своим брендом', exact: true}).waitFor();
  return {context, page, state};
}
async function tick(run, ms = 5001) { await run.page.clock.fastForward(ms); await delay(40); }
async function hidden(run, value) { await run.page.evaluate(value => { window.__testHidden = value; document.dispatchEvent(new Event('visibilitychange')); }, value); }
async function pay(run) { await run.page.getByRole('button', {name: 'Оплатить 9 990 ₽', exact: true}).click(); }
async function pending(run) { await run.page.getByRole('link', {name: 'Продолжить оплату', exact: true}).waitFor(); }
async function layout(run, name) {
  const result = await run.page.evaluate(() => {
    const shown = node => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';
    return {overflow: document.documentElement.scrollWidth > innerWidth + 1, small: [...document.querySelectorAll('button,a.button,summary')].filter(shown).filter(node => node.getBoundingClientRect().height < 47).map(node => node.textContent)};
  });
  check(!result.overflow && !result.small.length, `${name}: mobile layout, 48px controls: ${JSON.stringify(result)}`);
  await run.page.screenshot({path: `${out}/${name}.png`, fullPage: true});
}
async function close(run) {
  check(!run.state.errors.length, 'No browser errors: ' + run.state.errors.join(','));
  check(!run.state.external.length, 'No real external requests');
  check(!run.state.calls.some(call => call.endpoint === '/projects' && call.method === 'POST' || call.endpoint.endsWith('/bot')), 'No pre-payment draft/token flow');
  check(!run.state.calls.some(call => /catalog|overview|finance|logo|management/.test(call.endpoint)), 'Enrollment has no workspace calls');
  await run.context.close();
}

async function runPayFirstSmoke() {
 try {
  for (const width of [320, 390, 430]) {
    const run = await fixture({width});
    await run.page.getByRole('button', {name: 'Оплатить 9 990 ₽', exact: true}).waitFor();
    check(run.state.calls.every(call => call.method === 'GET'), 'Opening page never creates invoice or bot');
    const text = await run.page.locator('body').innerText();
    check(!/VPN|300\s*ГБ|антиглуш|обход|Подготовить оплату|Подтвердить покупку/i.test(text), 'Neutral concise enrollment copy');
    check(await run.page.locator('input,textarea').count() === 0, 'No token/setup form before payment');
    await layout(run, `offer-${width}`);
    await pay(run); await pending(run);
    check(run.state.checkouts === 1 && run.state.managed === 0, 'Single pay click creates invoice, no bot');
    check(JSON.stringify(await run.page.evaluate(() => window.__opened)) === JSON.stringify([PAYMENT]), 'Payment opens directly after the first pay click');
    check(await run.page.getByRole('link', {name: 'Продолжить оплату', exact: true}).getAttribute('href') === PAYMENT, 'Saved invoice is accessible if popup is blocked');
    run.state.order = order('paid_pending_launch'); await tick(run);
    await run.page.getByRole('button', {name: 'Создать сервис', exact: true}).waitFor();
    check(await run.page.getByRole('button', {name: 'Оплатить 9 990 ₽', exact: true}).count() === 0, 'Confirmed payment replaces pay action without reload');
    await layout(run, `paid-${width}`);
    await run.page.getByRole('button', {name: 'Создать сервис', exact: true}).click();
    await run.page.getByRole('link', {name: 'Создать сервис', exact: true}).waitFor();
    check(run.state.managed === 1 && (await run.page.evaluate(() => window.__opened)).at(-1) === CREATE, 'Create service directly opens Telegram after payment');
    run.state.connected = true; await tick(run, 3001);
    await until(() => run.state.managedPolls > 0, 'Managed bot check did not run');
    run.state.order.status = 'launched'; run.state.active = true; await tick(run, 5001);
    await run.page.getByRole('heading', {name: 'Ваш сервис создан', exact: true}).waitFor();
    check(await run.page.getByRole('link', {name: 'Перейти в моего бота', exact: true}).getAttribute('href') === 'https://t.me/OfflineServiceBot', 'Completed service links to own bot');
    check(run.state.checkouts === 1, 'Paid creation never repeats payment');
    await close(run);
  }
  const lost = await fixture({failCheckout: true}); await pay(lost); await lost.page.getByRole('alert').waitFor();
  const saved = await lost.page.evaluate(() => Object.entries(localStorage));
  check(saved.length === 1 && saved[0][0] === 'spn-partner-enrollment:42' && /^[0-9a-f-]{36}$/i.test(saved[0][1]), 'Only actor-scoped operation UUID is persisted before lost response');
  await pay(lost); await pending(lost);
  const requests = lost.state.calls.filter(call => call.endpoint === '/enrollment/checkout');
  check(requests.length === 2 && JSON.stringify(requests[0].body) === JSON.stringify(requests[1].body), 'Lost response retry preserves exact key and price'); await close(lost);

  const canceled = await fixture({savedOrder: order('canceled'), failCheckout: true}); await pay(canceled); await canceled.page.getByRole('alert').waitFor();
  await pay(canceled); await pending(canceled);
  const canceledRetries = canceled.state.calls.filter(call => call.endpoint === '/enrollment/checkout');
  check(canceledRetries.length === 2 && JSON.stringify(canceledRetries[0].body) === JSON.stringify(canceledRetries[1].body), 'Retry after a previous canceled order rotates the key only once, even if the new checkout response is lost'); await close(canceled);

  const reload = await fixture({savedOrder: order(), initialKey: '4f6ba37f-02ed-4ef9-a0db-3dc9c764845b'}); await pending(reload);
  check(reload.state.checkouts === 0 && !(await reload.page.evaluate(() => window.__opened)).length, 'Reopening reads saved invoice without new payment or automatic navigation');
  reload.state.order = order('paid_pending_launch', 1000); await tick(reload);
  await reload.page.getByRole('button', {name: 'Создать сервис', exact: true}).waitFor();
  check((await reload.page.evaluate(() => Object.entries(localStorage))).length === 0, 'Paid result clears only its pending-operation UUID'); await close(reload);

  const oldPaid = await fixture({savedOrder: order('paid_pending_launch', 1000)});
  await oldPaid.page.getByRole('button', {name: 'Создать сервис', exact: true}).waitFor();
  await tick(oldPaid, 120000);
  check(oldPaid.state.checkouts === 0 && oldPaid.state.managed === 0 && oldPaid.state.polls === 0, 'Paid 10 RUB remains paid; no invoice and no bot creation until explicit click'); await close(oldPaid);

  for (const projectStatus of ['suspended', 'closed']) {
    const paused = await fixture({projectStatus, savedOrder: order('launched'), connected: true, active: true}); await tick(paused, 120000);
    check(!await paused.page.getByRole('button', {name: /Оплатить|Создать сервис/}).count() && !await paused.page.getByRole('heading', {name: 'Ваш сервис создан', exact: true}).count(), 'Paused or closed project cannot pay, create, or claim to be active');
    check(paused.state.calls.every(call => call.method === 'GET') && paused.state.polls === 0 && paused.state.managed === 0, 'Paused or closed project never mutates or polls orders');
    check(await paused.page.getByRole('link', {name: 'Поддержка', exact: true}).isVisible(), 'Paused or closed project keeps support available'); await close(paused);
  }

  const busy = await fixture(); let release; busy.state.checkoutHold = new Promise(resolve => release = resolve);
  await pay(busy); await until(() => busy.state.checkouts === 1, 'Held checkout did not start');
  await busy.page.getByRole('button', {name: 'Открываем оплату…', exact: true}).evaluate(node => { node.click(); node.click(); });
  check(busy.state.checkouts === 1, 'Repeated clicks do not duplicate an in-flight invoice'); release(); await pending(busy); await close(busy);

  const paused = await fixture({savedOrder: order()}); await pending(paused); await hidden(paused, true); await tick(paused, 60000);
  check(paused.state.polls === 0, 'Hidden page pauses automatic checks'); await hidden(paused, false); await tick(paused, 1);
  await until(() => paused.state.polls === 1, 'Visible page resumes checks'); await close(paused);

  const slow = await fixture({savedOrder: order()}); await pending(slow); let finish; slow.state.hold = new Promise(resolve => finish = resolve);
  await tick(slow); await until(() => slow.state.polls === 1, 'Held check did not start'); await slow.page.evaluate(() => window.dispatchEvent(new Event('focus'))); await tick(slow, 10001);
  check(slow.state.polls === 1 && slow.state.maxActive === 1, 'Autocheck never overlaps a slow response'); finish(); await until(() => slow.state.activePolls === 0, 'Held check did not finish'); await close(slow);

  for (const status of [401, 403, 404]) {
    const rejected = await fixture({savedOrder: order()}); await pending(rejected); rejected.state.status = status; await tick(rejected); await until(() => rejected.state.polls === 1, 'Rejected check did not start'); await tick(rejected, 120000);
    await rejected.page.evaluate(() => window.dispatchEvent(new Event('focus'))); await tick(rejected, 1);
    check(rejected.state.polls === 1, `${status} stops automatic check retries`); await close(rejected);
  }
  const unavailable = await fixture({existing: true, available: false});
  await unavailable.page.getByText('Оплата временно недоступна.', {exact: true}).waitFor();
  check(await unavailable.page.getByRole('button', {name: 'Оплатить 9 990 ₽', exact: true}).isDisabled(), 'Unavailable payment is fail-closed'); await close(unavailable);
  const stale = await fixture({fee: 1000}); check(await stale.page.getByRole('button', {name: 'Оплатить 9 990 ₽', exact: true}).isDisabled(), 'Stale test-price settings cannot create an invoice'); await close(stale);
  const malformed = await fixture({savedOrder: order('paid_pending_launch'), badCreation: true});
  await malformed.page.getByRole('button', {name: 'Создать сервис', exact: true}).click(); await malformed.page.getByRole('alert').waitFor();
  check((await malformed.page.evaluate(() => window.__opened)).length === 0 && malformed.state.managedPolls === 0, 'Untrusted Telegram creation URL never opens or starts polling'); await close(malformed);
  const entryError = await fixture({existing: true, failEntry: true}); await entryError.page.getByRole('alert').waitFor();
  check(await entryError.page.getByRole('button', {name: 'Оплатить 9 990 ₽', exact: true}).count() === 0 && !/private-provider-secret/.test(await entryError.page.locator('body').innerText()), 'Unknown saved payment state never invites another payment or leaks provider details'); await close(entryError);
  const noAuth = await fixture({auth: false}); check(noAuth.state.calls.length === 0, 'No initData means no API requests'); await close(noAuth);
  console.log(JSON.stringify({ok: true, assertions, real_requests: 0, fake_clock: true, screenshots: out}));
 } finally { await browser.close(); }
}

export {browser, fixture, tick, hidden, pay, pending, layout, close, check, until, order, PID, OTHER, OID, PAYMENT, CREATE};
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) await runPayFirstSmoke();
