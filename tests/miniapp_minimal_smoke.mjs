// Fully intercepted local MiniApp checks. No Telegram, payment provider or live API calls.
import assert from 'node:assert/strict';
import { readFile, mkdir } from 'node:fs/promises';
import path from 'node:path';

const root = process.cwd();
const out = process.env.MINIAPP_PREVIEW_DIR || '/private/tmp/spn-miniapp-native';
const playwrightModule = process.env.PLAYWRIGHT_MODULE || '/Users/khamzat/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs';
const { chromium } = await import(playwrightModule);
await mkdir(out, { recursive: true });

const tariffs = {
  regular: [
    {code:'regular_1m', title:'Обычная на месяц', days:30, price:160, original_price:200, discount:{name:'20%'}},
    {code:'regular_3m', title:'Обычная на 3 месяца', days:90, price:540, original_price:540},
  ],
  bypass: [
    {code:'bypass_1m', title:'Антиглушилка на месяц', days:30, price:240, original_price:300, base_gb:300, discount:{name:'20%'}},
    {code:'bypass_3m', title:'Антиглушилка на 3 месяца', days:90, price:810, original_price:810, base_gb:300},
  ],
  traffic_packages: [{code:'traffic_50', gb:50, price:100, original_price:100}],
};
function subscription(id, kind, status = 'active', overrides = {}) {
  return {
    id, plan_kind:kind, type_index:id === 101 ? 1 : 2, status,
    subscription_until:status === 'active' ? '2099-11-15T09:00:00Z' : '2025-10-01T09:00:00Z',
    subscription_url:`https://subscriptions.test/sub/local-${id}`,
    traffic:{enabled:kind === 'bypass', limit_gb:350, used_gb:347.5, reset_at:'2099-10-30T09:00:00Z'},
    devices:{limit:kind === 'bypass' ? 3 : 5, max_limit:10, addon_status:status === 'active' ? 'available' : 'expired', packages:status === 'active' ? [{count:1, price:50, discount_percent:0}] : []},
    ...overrides,
  };
}
const referral = {
  link:'https://t.me/WaySPN_robot?start=ref_123', share_text:'Лучший VPN:',
  bot_earning_url:'https://t.me/WaySPN_robot?start=earn',
  earning_actions:{home:'https://t.me/WaySPN_robot?start=earn', spend:'https://t.me/WaySPN_robot?start=earn_spend', withdraw:'https://t.me/WaySPN_robot?start=earn_withdraw'},
  minimum_withdrawal:1500, current_balance:1400, total_earned:3200,
  registered_referrals:11, active_referrals:5, refunded_amount:56,
  pricing_examples:[{price:160, first_commission:56, repeat_commission:24}, {price:240, first_commission:84, repeat_commission:36}],
  history:[{kind:'earning', id:1, amount:56, status:'credited', detail:'regular_1m', created_at:'2026-10-12T09:00:00Z'}, {kind:'reversal', id:2, amount:56, status:'confirmed', detail:'invoice-local', created_at:'2026-10-12T10:00:00Z'}],
};

const browser = await chromium.launch({
  executablePath:process.env.CHROME_EXECUTABLE || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  headless:true,
});
let assertions = 0;
function check(value, message) { assert.ok(value, message); assertions += 1; }

async function fixturePage({width = 390, subs = [subscription(303,'regular','expired'), subscription(101,'bypass'), subscription(202,'regular')], failures = {}, holds = {}, missingCatalogBase = false} = {}) {
  const context = await browser.newContext({viewport:{width,height:844}});
  await context.addInitScript(() => {
    window.testLinks = []; window.testCopied = []; window.testConfirms = [];
    Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{writeText:async text => window.testCopied.push(text)}});
    window.Telegram = {WebApp:{initData:'local-fixture', ready(){}, expand(){}, openLink(url){window.testLinks.push(url);}, openTelegramLink(url){window.testLinks.push(url);}, showConfirm(text, callback){window.testConfirms.push(text); callback(window.testConfirmResult !== false);}}};
  });
  const control = {
    ownerId:123, subs:structuredClone(subs), failures:{...failures}, posts:[], deletes:[], referral:structuredClone(referral), catalog:structuredClone(tariffs),
    gate:null, paymentStatus:'pending', errors:[], external:[], walletPosts:[], walletGets:[], walletByKey:new Map(), walletGate:null, walletStatus:'paid', walletLostResponse:false, walletLookupNotFound:false,
    withdrawalPosts:[], withdrawalGets:[], withdrawalsByKey:new Map(), withdrawalGate:null, withdrawalLostResponse:false, withdrawalRejectOnce:false,
    devices:[{platform:'Android',device_model:'Тестовый планшет',hwid:'local-device',user_agent:'Happ/4.0 local-fixture',created_at:'2026-10-01T10:00:00Z'}],
  };
  if (missingCatalogBase) control.catalog.bypass.forEach(t => delete t.base_gb);
  await context.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.host !== 'preview.test') {
      control.external.push(request.url());
      // The SDK is deliberately stubbed; no external request is allowed out.
      return route.fulfill({status:200, contentType:'application/javascript', body:''});
    }
    if (holds[url.pathname]) await holds[url.pathname];
    if (control.failures[url.pathname]) return route.fulfill({status:503,json:{detail:'Временно недоступно. Повторите попытку.'}});
    if (url.pathname === '/miniapp/api/me') return route.fulfill({json:{id:control.ownerId,first_name:'Тест',username:'local_fixture'}});
    if (url.pathname === '/miniapp/api/tariffs') return route.fulfill({json:control.catalog});
    if (url.pathname === '/miniapp/api/referral') return route.fulfill({json:control.referral});
    if (url.pathname === '/miniapp/api/subscriptions') return route.fulfill({json:{subscriptions:control.subs}});
    if (/^\/miniapp\/api\/subscriptions\/\d+\/devices$/.test(url.pathname)) return route.fulfill({json:{devices:control.devices}});
    if (/^\/miniapp\/api\/subscriptions\/\d+\/devices\/delete$/.test(url.pathname)) {
      const {hwid} = JSON.parse(request.postData()); control.deletes.push({path:url.pathname,hwid});
      control.devices = control.devices.filter(device => device.hwid !== hwid);
      return route.fulfill({json:{ok:true}});
    }
    if (/^\/miniapp\/api\/subscriptions\/\d+\/devices\/delete-all$/.test(url.pathname)) {
      control.deletes.push({path:url.pathname}); control.devices = []; return route.fulfill({json:{ok:true}});
    }
    if (url.pathname === '/miniapp/api/referral/purchases' && request.method() === 'POST') {
      const payload = JSON.parse(request.postData()); control.walletPosts.push(payload);
      const product = [...control.catalog.regular,...control.catalog.bypass].find(item => item.code === payload.tariff_code);
      if (!product) return route.fulfill({status:400,json:{detail:{code:'invalid_tariff',message:'Выберите подписку.'}}});
      if (Number(payload.expected_amount) !== Number(product.price)) return route.fulfill({status:409,json:{detail:{code:'price_changed',message:'Цена изменилась. Проверьте новую цену.',amount:Number(product.price).toFixed(2)}}});
      const existing = control.walletByKey.get(payload.request_key);
      if (existing && existing.owner !== control.ownerId) return route.fulfill({status:404,json:{detail:{code:'not_found',message:'Покупка не найдена.'}}});
      if (!existing && Number(control.referral.current_balance) < Number(product.price)) return route.fulfill({status:409,json:{detail:{code:'insufficient_balance',message:'Недостаточно денег на балансе.',available:String(control.referral.current_balance),amount:Number(product.price).toFixed(2)}}});
      let entry = existing;
      if (!entry) {
        entry = {owner:control.ownerId,payload,body:{request_key:payload.request_key,invoice_id:'local-wallet-'+(control.walletByKey.size+1),status:control.walletStatus,amount:Number(product.price).toFixed(2),summary:control.walletStatus === 'paid' ? {title:'Подписка оплачена',message:'Оплачено с реферального баланса.',subscription_id:payload.subscription_id || 404} : null}};
        control.walletByKey.set(payload.request_key,entry);
        control.referral.current_balance = Number(control.referral.current_balance) - Number(product.price);
      }
      if (control.walletGate) await control.walletGate;
      if (control.walletLostResponse) { control.walletLostResponse = false; return route.abort('failed'); }
      return route.fulfill({json:entry.body});
    }
    if (url.pathname.startsWith('/miniapp/api/referral/purchases/') && request.method() === 'GET') {
      const key = decodeURIComponent(url.pathname.split('/').at(-1)); control.walletGets.push(key);
      const entry = control.walletByKey.get(key);
      if (control.walletLookupNotFound || !entry || entry.owner !== control.ownerId) return route.fulfill({status:404,json:{detail:{code:'not_found',message:'Покупка не найдена.'}}});
      return route.fulfill({json:entry.body});
    }
    if (url.pathname === '/miniapp/api/referral/withdrawals' && request.method() === 'POST') {
      const payload = JSON.parse(request.postData()); control.withdrawalPosts.push(payload);
      if (control.withdrawalRejectOnce) {
        control.withdrawalRejectOnce = false;
        return route.fulfill({status:400,json:{detail:{code:'invalid_request',message:'Реквизиты не приняты. Проверьте номер телефона.'}}});
      }
      const amount = Number(payload.amount);
      if (!Number.isFinite(amount) || amount < 1500 || amount > Number(control.referral.current_balance)) return route.fulfill({status:409,json:{detail:{code:'insufficient_balance',message:'Проверьте сумму и доступный баланс.',available:String(control.referral.current_balance),amount:String(payload.amount)}}});
      let entry = control.withdrawalsByKey.get(payload.request_key);
      if (entry && entry.owner !== control.ownerId) return route.fulfill({status:404,json:{detail:{code:'not_found',message:'Заявка не найдена.'}}});
      if (!entry) {
        const id = control.withdrawalsByKey.size + 1;
        entry = {owner:control.ownerId,payload,body:{request_key:payload.request_key,id,number:'R-'+id,status:'pending',amount:amount.toFixed(2),method:payload.method,bank:payload.method === 'sbp' ? payload.bank : null,phone:payload.method === 'sbp' ? '*******4567' : null,address:payload.method === 'usdt' ? String(payload.address).slice(0,5)+'…'+String(payload.address).slice(-4) : null,decision_note:null}};
        control.withdrawalsByKey.set(payload.request_key,entry); control.referral.current_balance -= amount;
      }
      if (control.withdrawalGate) await control.withdrawalGate;
      if (control.withdrawalLostResponse) { control.withdrawalLostResponse = false; return route.abort('failed'); }
      return route.fulfill({json:entry.body});
    }
    if (url.pathname.startsWith('/miniapp/api/referral/withdrawals/') && request.method() === 'GET') {
      const key = decodeURIComponent(url.pathname.split('/').at(-1)); control.withdrawalGets.push(key);
      const entry = control.withdrawalsByKey.get(key);
      if (!entry || entry.owner !== control.ownerId) return route.fulfill({status:404,json:{detail:{code:'not_found',message:'Заявка не найдена.'}}});
      return route.fulfill({json:entry.body});
    }
    if (/^\/miniapp\/api\/subscriptions\/\d+$/.test(url.pathname) && request.method() === 'DELETE') {
      control.deletes.push(url.pathname);
      const id = Number(url.pathname.split('/').at(-1)); control.subs = control.subs.filter(s => s.id !== id);
      return route.fulfill({json:{ok:true}});
    }
    if (/^\/miniapp\/api\/payments\/(subscription|traffic|devices)$/.test(url.pathname) && request.method() === 'POST') {
      const payload = JSON.parse(request.postData()); control.posts.push({path:url.pathname,payload});
      if (control.gate) await control.gate;
      return route.fulfill({json:{invoice_id:'local-invoice-1',pay_url:'https://payments.test/local-only'}});
    }
    if (url.pathname === '/miniapp/api/payments/local-invoice-1') return route.fulfill({json:{status:control.paymentStatus, summary:{title:'Оплата прошла',message:'Подписка продлена.',toast:'Оплата прошла',subscription_id:101}}});
    if (url.pathname.startsWith('/miniapp/api/')) {
      control.errors.push(`Unexpected local API call: ${request.method()} ${url.pathname}`);
      return route.fulfill({status:501,json:{detail:'Unexpected fixture API call'}});
    }
    const relative = url.pathname === '/app' ? 'index.html' : url.pathname.replace(/^\/app\//,'');
    if (!['index.html','assets/app.css','assets/app.js'].includes(relative)) return route.fulfill({status:404,body:''});
    const mime = relative.endsWith('.js') ? 'application/javascript' : relative.endsWith('.css') ? 'text/css' : 'text/html';
    return route.fulfill({contentType:mime,body:await readFile(path.join(root,'static/miniapp',relative))});
  });
  const page = await context.newPage();
  page.on('pageerror', error => control.errors.push(String(error)));
  await page.goto('https://preview.test/app');
  return {context,page,control};
}

async function ready(page) {
  await page.waitForFunction(() => typeof state !== 'undefined' && state.me && (!state.dataStatus || Object.values(state.dataStatus).every(status => status !== 'loading')));
}
async function tab(page, label) { await page.locator('.tabbar').getByRole('button',{name:label,exact:true}).click(); }
function active(page) { return page.locator('.view.active'); }
async function back(page) { await active(page).getByRole('button',{name:/Назад/}).first().click(); }
async function detail(page, id = 101) { await page.evaluate(id => openSubDetail(id),id); await active(page).locator('[data-action="renew"]').waitFor(); }
async function referralReady(page) { await page.waitForFunction(() => state.currentView === 'ref' && state.dataStatus.ref === 'ready'); }
async function walletReview(page, target = 'new', code = 'regular_1m', subId = 202) {
  await tab(page,'Заработать'); await referralReady(page);
  await active(page).getByRole('button',{name:'Потратить на подписку',exact:true}).click();
  await active(page).getByRole('button',{name:target === 'new' ? /^Купить новую подписку/ : /^Продлить подписку/}).click();
  if (target === 'new') await active(page).getByRole('button',{name:code.startsWith('bypass') ? /^С антиглушилкой/ : /^Обычная/}).click();
  else await active(page).locator(`[data-action="wallet-renew"][data-sub-id="${subId}"]`).click();
  await active(page).locator(`[data-action="wallet-tariff"][data-code="${code}"]`).click();
  await active(page).getByRole('button',{name:'Оплатить с баланса',exact:true}).waitFor();
}
async function withdrawalReview(page, method = 'sbp', amount = '1500', address = 'T'+'A'.repeat(33)) {
  await tab(page,'Заработать'); await referralReady(page);
  await active(page).getByRole('button',{name:'Вывести',exact:true}).click();
  await active(page).getByLabel('Сумма, ₽',{exact:true}).fill(amount);
  await active(page).getByRole('button',{name:'Продолжить',exact:true}).click();
  await active(page).getByRole('button',{name:method === 'sbp' ? /^СБП/ : /^USDT/}).click();
  if (method === 'sbp') {
    await active(page).getByLabel('Банк',{exact:true}).fill('Тестовый банк');
    await active(page).getByLabel('Номер телефона',{exact:true}).fill('+79001234567');
  } else await active(page).getByLabel('Адрес USDT',{exact:true}).fill(address);
  await active(page).getByRole('button',{name:'Продолжить',exact:true}).click();
  await active(page).getByRole('button',{name:'Подать заявку',exact:true}).waitFor();
}
async function layout(page, name) {
  const result = await page.evaluate(() => {
    const visible = node => {
      const closed = node.closest('details:not([open])');
      if (closed && !closed.querySelector(':scope > summary')?.contains(node)) return false;
      return node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden';
    };
    const main = document.querySelector('.view.active');
    const textNodes = [...main.querySelectorAll('p, small, span, b, h1, h2, button, summary')].filter(visible);
    const smallFonts = textNodes.filter(n => parseFloat(getComputedStyle(n).fontSize) < 12).map(n => n.textContent.trim().slice(0,60));
    const nav = document.querySelector('.tabbar');
    return {
      overflow:document.documentElement.scrollWidth > innerWidth + 1,
      smallFonts,
      shortButtons:[...document.querySelectorAll('button')].filter(visible).filter(n => n.getBoundingClientRect().height < 47).map(n => n.textContent.trim()),
      clippedTabs:[...nav.querySelectorAll('span')].filter(visible).filter(n => n.scrollWidth > n.clientWidth + 1).map(n => n.textContent),
      gradients:[document.body,...main.querySelectorAll('.card,.button,.choice-button')].filter(visible).filter(n => /gradient/.test(getComputedStyle(n).backgroundImage)).length,
    };
  });
  check(!result.overflow,`${name}: no horizontal overflow`);
  assert.deepEqual(result.smallFonts,[],`${name}: text at least 12px`); assertions += 1;
  assert.deepEqual(result.shortButtons,[],`${name}: buttons at least 48px`); assertions += 1;
  assert.deepEqual(result.clippedTabs,[],`${name}: navigation labels are not clipped`); assertions += 1;
  check(!result.gradients,`${name}: neutral surfaces without gradients`);
  await page.evaluate(() => window.scrollTo(0,document.body.scrollHeight));
  const overlap = await page.evaluate(() => {
    const main = document.querySelector('.view.active');
    const buttons = [...main.querySelectorAll('button')].filter(n => {
      const closed = n.closest('details:not([open])');
      return n.getClientRects().length > 0 && (!closed || closed.querySelector(':scope > summary')?.contains(n));
    });
    const last = buttons.at(-1); const nav = document.querySelector('.tabbar');
    if (!last || !nav.getClientRects().length) return false;
    return last.getBoundingClientRect().bottom > nav.getBoundingClientRect().top + 1;
  });
  check(!overlap,`${name}: bottom action remains above tabbar at scroll end`);
  await page.evaluate(() => window.scrollTo(0,0));
}

try {
  for (const width of [320,390,430]) {
    const {page,context,control} = await fixturePage({width}); await ready(page);
    assert.deepEqual(await page.locator('.tabbar button').allTextContents(),['Подписки','Купить','Заработать','Помощь']); assertions += 1;
    check(await page.locator('.tab[data-view="subs"]').getAttribute('aria-current') === 'page','My VPN initially current');
    const order = await active(page).locator('.key-choice').allTextContents();
    check(order.length === 3 && /Истекла|истекла/.test(order.at(-1)) && !/Истекла|истекла/.test(order[0]),'all active subscriptions precede expired');
    check(/2[,.]5\s*ГБ/.test(await active(page).innerText()),'low remaining traffic taken from API, purchased GB preserved');
    await layout(page,`${width}-subscriptions`);
    await page.screenshot({path:path.join(out,`subscriptions-${width}.png`),fullPage:true});

    await detail(page);
    const raw = active(page).getByText('https://subscriptions.test/sub/local-101',{exact:true});
    check(!(await raw.isVisible()),'raw subscription URL collapsed');
    check(!/VPN подключён|Соединение установлено/.test(await active(page).innerText()),'active subscription not presented as VPN connection');
    await active(page).getByRole('button',{name:'Открыть в Happ',exact:true}).click();
    const links = await page.evaluate(() => window.testLinks);
    const bridge = new URL(links.at(-1));
    check(bridge.pathname === '/app/open-happ' && bridge.searchParams.get('url') === 'https://subscriptions.test/sub/local-101','existing Happ bridge preserved');
    check((await page.evaluate(() => window.testCopied)).includes('https://subscriptions.test/sub/local-101'),'Happ action copies subscription');
    await active(page).getByRole('button',{name:/Скопировать/}).first().click();
    check((await page.evaluate(() => window.testCopied)).at(-1) === 'https://subscriptions.test/sub/local-101','copy action correct');
    await layout(page,`${width}-detail`);
    await page.screenshot({path:path.join(out,`detail-${width}.png`),fullPage:true});

    for (const action of ['renew','traffic','device-addons']) {
      await active(page).locator(`[data-action="${action}"]`).click();
      await active(page).locator('.choice-button').first().click();
      const providers = await active(page).getByRole('button').allTextContents();
      check(providers.indexOf('Банковская карта') < providers.indexOf('CryptoBot'),`${action}: bank first`);
      await back(page); await back(page);
      check(await active(page).locator('[data-action="happ"]').count() === 1,`${action}: back returns selected subscription`);
    }
    control.devices.push({platform:'iOS',device_model:'iPhone',user_agent:'INCY/2 local-fixture',hwid:'local-device-two',created_at:'2026-10-02T10:00:00Z'});
    await active(page).locator('[data-action="devices"]').click();
    await active(page).getByText('Android • Тестовый планшет',{exact:true}).waitFor();
    check(await active(page).getByText('Добавлено: 2 из 3 устройств.',{exact:true}).count() === 1,'device screen shows actual added count and limit');
    await page.waitForFunction(() => getComputedStyle(document.getElementById('toast')).opacity === '0');
    await layout(page,`${width}-devices`); await page.screenshot({path:path.join(out,`devices-${width}.png`),fullPage:true});
    await back(page); check(await active(page).locator('[data-action="happ"]').count() === 1,'device list back preserves detail');

    await tab(page,'Купить');
    check(/300\s*ГБ/.test(await active(page).innerText()),'catalog base300 displayed');
    await layout(page,`${width}-purchase-types`);
    await page.screenshot({path:path.join(out,`purchase-types-${width}.png`),fullPage:true});
    await active(page).getByRole('button',{name:/Обычная/}).first().click();
    await layout(page,`${width}-periods`);
    await page.screenshot({path:path.join(out,`periods-${width}.png`),fullPage:true});
    await active(page).locator('.choice-button').first().click();
    const recap = await active(page).innerText();
    check(/Обычная/.test(recap) && /1 месяц/.test(recap) && /160\s*₽/.test(recap),'checkout shows type, term and actual discounted price');
    await layout(page,`${width}-checkout`);
    await page.screenshot({path:path.join(out,`checkout-${width}.png`),fullPage:true});
    await back(page); check(await active(page).locator('.choice-button').count() === 2,'new purchase back returns periods');

    await tab(page,'Заработать');
    await active(page).getByRole('button',{name:/Пригласить друга/}).click();
    const share = new URL((await page.evaluate(() => window.testLinks)).at(-1));
    check(share.searchParams.get('text') === 'Лучший VPN:' && share.searchParams.get('url') === referral.link,'short share includes personal referral link once');
    await active(page).getByRole('button',{name:/Скопировать ссылку/}).click();
    check((await page.evaluate(() => window.testCopied)).at(-1) === referral.link,'personal referral copied');
    const linkCount = (await page.evaluate(() => window.testLinks)).length;
    await active(page).getByRole('button',{name:'Потратить на подписку',exact:true}).click();
    await active(page).getByRole('button',{name:/^Купить новую подписку/}).waitFor();
    check((await page.evaluate(() => window.testLinks)).length === linkCount,'wallet flow stays inside MiniApp');
    await back(page);
    await active(page).getByRole('button',{name:'Вывести',exact:true}).click();
    await active(page).getByLabel('Сумма, ₽',{exact:true}).waitFor();
    check((await page.evaluate(() => window.testLinks)).length === linkCount,'withdrawal form stays inside MiniApp');
    await back(page);
    for (const label of ['Статистика','Как это работает','История']) {
      const summary = active(page).locator('summary').filter({hasText:label});
      check(await summary.count() === 1,`${label}: native disclosure present`);
      check(!(await summary.evaluate(n => n.parentElement.open)),`${label}: initially collapsed`);
      await summary.click(); check(await summary.evaluate(n => n.parentElement.open),`${label}: opens`);
    }
    check(/56\s*₽/.test(await active(page).innerText()) && /84\s*₽/.test(await active(page).innerText()),'pricing examples use API');
    await layout(page,`${width}-referral`);
    await page.screenshot({path:path.join(out,`referral-${width}.png`),fullPage:true});
    await tab(page,'Помощь'); await layout(page,`${width}-help`);
    await page.screenshot({path:path.join(out,`help-${width}.png`),fullPage:true});
    const viewport = await page.locator('meta[name="viewport"]').getAttribute('content');
    check(!/user-scalable=no|maximum-scale=1(?:\.0)?(?:,|$)/.test(viewport),'page zoom available');
    check(await page.locator('[role="status"], [aria-live="polite"]').count() > 0,'status announced accessibly');
    await page.keyboard.press('Tab');
    await page.locator('.tab').first().focus();
    check(await page.locator('.tab').first().evaluate(n => getComputedStyle(n).outlineStyle !== 'none'),'keyboard focus visible');
    assert.deepEqual(control.errors,[],'no runtime error'); assertions += 1;
    await context.close();
  }

  // A held create request must not create duplicate invoices, even when UI state changes.
  {
    const {page,context,control} = await fixturePage(); await ready(page);
    await detail(page); await active(page).locator('[data-action="renew"]').click();
    await active(page).locator('.choice-button').first().click();
    let release; control.gate = new Promise(resolve => { release = resolve; });
    await active(page).getByRole('button',{name:'Банковская карта',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('[data-provider="yookassa"]')?.disabled || /Создаём|Создаем|создаётся|создается/.test(document.querySelector('.view.active').innerText));
    await page.evaluate(() => payPrepared('yookassa'));
    check(control.posts.length === 1,'double create blocked while pending');
    await tab(page,'Помощь'); release();
    await page.waitForFunction(() => window.testLinks.some(u => u.includes('payments.test')));
    const payment = await page.evaluate(() => ({type:state.activePayment.type,target:state.activePayment.payment_target,sub:state.activePayment.subscription_id,tariff:state.activePayment.tariff_code}));
    assert.deepEqual(payment,{type:'subscription',target:'renew',sub:101,tariff:'bypass_1m'},'invoice retains original selection after navigation'); assertions += 1;
    control.paymentStatus = 'paid'; await page.evaluate(() => checkActivePayment());
    await active(page).getByText('Оплата прошла',{exact:true}).waitFor();
    check(control.posts.length === 1,'paid polling does not create extra payment');
    await active(page).getByRole('button',{name:/Вернуться|[Пп]одписки/}).click();
    check(await page.locator('.tab[data-view="subs"]').getAttribute('aria-current') === 'page','payment completion returns My VPN');
    await context.close();
  }
  // Paid-result refresh from an old invoice must not replace a newly selected checkout.
  {
    const holds = {};
    const {page,context,control} = await fixturePage({holds}); await ready(page);
    await tab(page,'Купить');
    await active(page).getByRole('button',{name:/Обычная/}).first().click();
    await active(page).locator('.choice-button').first().click();
    await active(page).getByRole('button',{name:'Банковская карта',exact:true}).click();
    await page.waitForFunction(() => window.testLinks.some(u => u.includes('payments.test')));
    await page.evaluate(() => stopPaymentPolling());
    await page.waitForFunction(() => state.activePayment && !state.activePayment.checking);
    let release; holds['/miniapp/api/subscriptions'] = new Promise(resolve => { release = resolve; });
    control.paymentStatus = 'paid';
    await page.evaluate(() => { window.testPendingPaidCheck = checkActivePayment(); });
    await page.waitForFunction(() => state.dataStatus.subs === 'loading');
    await tab(page,'Купить');
    await active(page).getByRole('button',{name:/С антиглушилкой/}).first().click();
    await active(page).locator('.choice-button').first().click();
    check(await page.evaluate(() => state.pendingPayment?.tariff_code === 'bypass_1m' && !state.paying),'new checkout selected but not paid during old refresh');
    release(); await page.evaluate(() => window.testPendingPaidCheck);
    const selection = await page.evaluate(() => ({view:state.currentView,mode:state.buyMode,tariff:state.pendingPayment?.tariff_code,result:state.paymentResult}));
    assert.deepEqual(selection,{view:'buy',mode:'payment',tariff:'bypass_1m',result:null},'old paid refresh preserves new unpaid checkout'); assertions += 1;
    check(/240\s*₽/.test(await active(page).innerText()) && control.posts.length === 1,'new selected purchase still displayed without an extra invoice');
    await context.close();
  }
  // Native wallet and payout reviews remain readable and actionable on narrow phones.
  for (const width of [320,390,430]) {
    const {page,context,control} = await fixturePage({width}); await ready(page);
    control.referral.current_balance = 5000;
    await walletReview(page);
    check(/160\s*₽/.test(await active(page).innerText()),'native wallet review uses actual discount price');
    await layout(page,`${width}-wallet-review`); await page.screenshot({path:path.join(out,`wallet-review-${width}.png`),fullPage:true});
    await active(page).getByRole('button',{name:'Оплатить с баланса',exact:true}).click();
    await active(page).getByText('Подписка оплачена',{exact:true}).waitFor();
    check(control.walletPosts.length === 1 && control.walletPosts[0].expected_amount === '160.00' && control.walletPosts[0].payment_target === 'new','native new purchase posts fixed quote exactly once');
    check(await page.evaluate(() => window.testLinks.length === 0),'native purchase does not open bot or provider');
    await layout(page,`${width}-wallet-paid`); await page.screenshot({path:path.join(out,`wallet-paid-${width}.png`),fullPage:true});
    await withdrawalReview(page);
    check(/вручную|не автоматический перевод/.test(await active(page).innerText()),'payout review describes manual request');
    await layout(page,`${width}-payout-review`); await page.screenshot({path:path.join(out,`payout-review-${width}.png`),fullPage:true});
    await active(page).getByRole('button',{name:'Подать заявку',exact:true}).click();
    await active(page).getByText('Заявка на рассмотрении',{exact:true}).waitFor();
    const receiptText = await active(page).innerText();
    check(control.withdrawalPosts.length === 1 && /R-1/.test(receiptText),'one withdrawal request number shown');
    check(!receiptText.includes('+79001234567') && receiptText.includes('4567'),'withdrawal receipt masks phone');
    const storage = await page.evaluate(() => Object.entries(localStorage));
    check(storage.every(([,value]) => { const keys = Object.keys(JSON.parse(value)); return keys.length === 2 && keys.includes('request_key') && keys.includes('type'); }),'storage contains only opaque recovery key/type');
    await layout(page,`${width}-payout-receipt`); await page.screenshot({path:path.join(out,`payout-receipt-${width}.png`),fullPage:true});
    await context.close();
  }
  {
    const {page,context,control} = await fixturePage(); await ready(page);
    await walletReview(page,'renew','bypass_1m',101);
    await back(page); check(await active(page).locator('[data-action="wallet-tariff"]').count() === 2,'wallet review back returns selected renewal periods');
    await active(page).locator('[data-action="wallet-tariff"][data-code="bypass_1m"]').click();
    let release; control.walletGate = new Promise(resolve => { release = resolve; }); control.walletStatus = 'pending';
    await active(page).getByRole('button',{name:'Оплатить с баланса',exact:true}).click();
    await page.waitForFunction(() => state.nativeOperations.purchase?.busy === true);
    await page.evaluate(() => submitWalletPurchase());
    check(control.walletPosts.length === 1,'wallet double click cannot reserve twice');
    await tab(page,'Купить'); await active(page).getByRole('button',{name:/^Обычная/}).click(); await active(page).locator('.choice-button').first().click();
    release(); await page.waitForFunction(() => !state.nativeOperations.purchase?.busy);
    check(await page.evaluate(() => state.currentView === 'buy' && state.pendingPayment?.tariff_code === 'regular_1m'),'held native reply does not replace a later ordinary checkout');
    const post = control.walletPosts[0]; check(post.payment_target === 'renew' && post.subscription_id === 101 && post.expected_amount === '240.00','wallet renewal targets original subscription');
    await tab(page,'Заработать'); await referralReady(page); await active(page).getByRole('button',{name:'Потратить на подписку',exact:true}).click();
    await active(page).getByText('Подписка оформляется',{exact:true}).waitFor();
    check(/зарезервирована/.test(await active(page).innerText()),'pending activation is explicitly reserved, not reported as paid');
    const key = post.request_key; await page.reload(); await ready(page);
    await page.waitForFunction(() => state.nativeOperations.purchase?.receipt?.status === 'pending');
    check(control.walletPosts.length === 1 && control.walletGets.includes(key),'restart recovers wallet operation by lookup, not POST');
    const entry = control.walletByKey.get(key); entry.body.status = 'paid'; entry.body.summary = {title:'Подписка оплачена',message:'Подписка продлена.',subscription_id:101};
    await tab(page,'Заработать'); await referralReady(page); await active(page).getByRole('button',{name:'Потратить на подписку',exact:true}).click();
    await active(page).getByText('Подписка оплачена',{exact:true}).waitFor();
    check(control.walletPosts.length === 1,'activation completion creates no second reservation'); await context.close();
  }
  {
    const {page,context,control} = await fixturePage(); await ready(page); control.referral.current_balance = 10;
    await walletReview(page); await active(page).getByRole('button',{name:'Оплатить с баланса',exact:true}).click();
    check(/Не хватает/.test(await active(page).innerText()) && control.walletPosts.length === 0,'insufficient wallet shows shortage without reservation');
    await tab(page,'Заработать'); await referralReady(page); await active(page).getByRole('button',{name:'Вывести',exact:true}).click();
    await active(page).getByLabel('Сумма, ₽',{exact:true}).fill('1499'); await active(page).getByRole('button',{name:'Продолжить',exact:true}).click();
    check(await active(page).getByLabel('Сумма, ₽',{exact:true}).count() === 1 && control.withdrawalPosts.length === 0,'below-minimum payout cannot advance'); await context.close();
  }
  {
    const {page,context,control} = await fixturePage(); await ready(page); await walletReview(page);
    control.catalog.regular[0].price = 170;
    await active(page).getByRole('button',{name:'Оплатить с баланса',exact:true}).click();
    await active(page).getByText(/Цена изменилась/).waitFor();
    check(control.walletByKey.size === 0 && /170\s*₽/.test(await active(page).innerText()),'price change requires review of refreshed quote, no reservation');
    await active(page).getByRole('button',{name:'Оплатить с баланса',exact:true}).click(); await active(page).getByText('Подписка оплачена',{exact:true}).waitFor();
    check(control.walletPosts.length === 2 && control.walletByKey.size === 1 && control.walletPosts[1].expected_amount === '170.00','explicit second confirmation reserves new price once'); await context.close();
  }
  {
    const {page,context,control} = await fixturePage(); await ready(page); control.referral.current_balance = 5000;
    await walletReview(page); control.referral.current_balance = 100;
    await active(page).getByRole('button',{name:'Оплатить с баланса',exact:true}).click();
    await active(page).getByText(/Не хватает 60 ₽/).waitFor();
    check(control.walletPosts.length === 1 && control.walletByKey.size === 0,'concurrent balance reduction creates no order');
    check(await page.evaluate(() => Number(state.referral.current_balance) === 100) && !/5\s000\s*₽/.test(await active(page).innerText()),'backend shortage updates cached balance to authoritative available amount');
    await active(page).getByRole('button',{name:'Оплатить с баланса',exact:true}).click();
    check(control.walletPosts.length === 1,'retry against refreshed insufficient balance sends no new POST'); await context.close();
  }
  {
    const {page,context,control} = await fixturePage(); await ready(page); control.walletLostResponse = true; control.walletStatus = 'pending';
    await walletReview(page); await active(page).getByRole('button',{name:'Оплатить с баланса',exact:true}).click();
    await active(page).getByText('Результат уточняется',{exact:true}).waitFor();
    const key = control.walletPosts[0].request_key;
    await active(page).getByRole('button',{name:'Проверить статус',exact:true}).click(); await active(page).getByText('Подписка оформляется',{exact:true}).waitFor();
    check(control.walletPosts.length === 1 && control.walletGets.includes(key),'lost wallet response recovers existing purchase by GET');
    control.ownerId = 456; await page.reload(); await ready(page);
    check(await page.evaluate(() => state.nativeOperations.purchase === null),'another Telegram owner does not restore previous owner key');
    check(control.walletPosts.length === 1,'owner change creates no automatic purchase'); await context.close();
  }
  {
    const {page,context,control} = await fixturePage(); await ready(page); control.referral.current_balance = 5000; control.walletStatus = 'pending';
    await walletReview(page); await active(page).getByRole('button',{name:'Оплатить с баланса',exact:true}).click(); await active(page).getByText('Подписка оформляется',{exact:true}).waitFor();
    await back(page); await active(page).getByRole('button',{name:'Вывести',exact:true}).click();
    await active(page).getByLabel('Сумма, ₽',{exact:true}).fill('1500.50');
    await page.evaluate(async () => { await lookupNativeOperation('purchase',{quiet:true}); await refreshReferral(); });
    check(await active(page).getByLabel('Сумма, ₽',{exact:true}).inputValue() === '1500.50' && await page.evaluate(() => document.activeElement?.name === 'amount'),'background wallet polling preserves amount draft and focus');
    await active(page).getByRole('button',{name:'Продолжить',exact:true}).click(); await active(page).getByRole('button',{name:/^СБП/}).click();
    await active(page).getByLabel('Банк',{exact:true}).fill('Тестовый банк — черновик'); await active(page).getByLabel('Номер телефона',{exact:true}).fill('+79001234567');
    await page.evaluate(async () => { await lookupNativeOperation('purchase',{quiet:true}); await refreshReferral(); });
    check(await active(page).getByLabel('Банк',{exact:true}).inputValue() === 'Тестовый банк — черновик' && await active(page).getByLabel('Номер телефона',{exact:true}).inputValue() === '+79001234567' && await page.evaluate(() => document.activeElement?.name === 'phone'),'background wallet polling preserves requisites draft and focus');
    const storage = await page.evaluate(() => JSON.stringify(Object.entries(localStorage)));
    check(!storage.includes('черновик') && !storage.includes('+79001234567'),'payout typing drafts remain memory-only'); await context.close();
  }
  {
    const {page,context,control} = await fixturePage(); await ready(page); control.walletStatus = 'pending'; control.walletLostResponse = true;
    await walletReview(page); await active(page).getByRole('button',{name:'Оплатить с баланса',exact:true}).click(); await active(page).getByText('Результат уточняется',{exact:true}).waitFor();
    const key = control.walletPosts[0].request_key; control.walletLookupNotFound = true;
    await active(page).getByRole('button',{name:'Проверить статус',exact:true}).click(); await active(page).getByRole('button',{name:'Заполнить повторно',exact:true}).click();
    await active(page).getByRole('button',{name:/^Купить новую подписку/}).click(); await active(page).getByRole('button',{name:/^Обычная/}).click(); await active(page).locator('[data-action="wallet-tariff"][data-code="regular_1m"]').click();
    control.walletLookupNotFound = false; control.walletByKey.get(key).body.status = 'paid'; control.walletByKey.get(key).body.summary = {title:'Подписка оплачена',subscription_id:404};
    await page.evaluate(() => lookupNativeOperation('purchase',{quiet:true}));
    const pay = active(page).getByRole('button',{name:'Оплатить с баланса',exact:true});
    if (await pay.count()) await pay.click();
    await active(page).getByText('Подписка оплачена',{exact:true}).waitFor();
    check(control.walletPosts.length === 1 && control.walletByKey.size === 1,'late paid lookup while retry review cannot mint a second purchase'); await context.close();
  }
  for (const [method,address] of [['sbp',null],['usdt','T'+'A'.repeat(33)],['usdt','0x'+'a'.repeat(40)]]) {
    const {page,context,control} = await fixturePage(); await ready(page); control.referral.current_balance = 5000;
    await withdrawalReview(page,method,'1500,50',address);
    await back(page);
    check(await active(page).getByLabel(method === 'sbp' ? 'Банк' : 'Адрес USDT',{exact:true}).count() === 1,'payout review back returns requisites, not home');
    await active(page).getByRole('button',{name:'Продолжить',exact:true}).click();
    let release; control.withdrawalGate = new Promise(resolve => { release = resolve; });
    await active(page).getByRole('button',{name:'Подать заявку',exact:true}).click();
    await page.waitForFunction(() => state.nativeOperations.withdrawal?.busy);
    await page.evaluate(() => submitNativeWithdrawal()); check(control.withdrawalPosts.length === 1,'double payout submission blocked');
    release(); await active(page).getByText('Заявка на рассмотрении',{exact:true}).waitFor();
    check(control.withdrawalPosts[0].amount === '1500.50' && control.withdrawalPosts[0].method === method,'payout request retains exact cents and method');
    const text = await active(page).innerText(); check(!text.includes('+79001234567') && (!address || !text.includes(address)),'receipt hides full requisites');
    const key = control.withdrawalPosts[0].request_key;
    await page.reload(); await ready(page); await page.waitForFunction(() => state.nativeOperations.withdrawal?.receipt?.status === 'pending');
    check(control.withdrawalPosts.length === 1 && control.withdrawalGets.includes(key),'reopening recovers payout by key without resubmission');
    await context.close();
  }
  {
    const {page,context,control} = await fixturePage(); await ready(page); control.referral.current_balance = 5000; control.withdrawalLostResponse = true;
    await withdrawalReview(page); await active(page).getByRole('button',{name:'Подать заявку',exact:true}).click(); await active(page).getByText('Результат уточняется',{exact:true}).waitFor();
    const key = control.withdrawalPosts[0].request_key; await page.reload(); await ready(page); await page.waitForFunction(() => state.nativeOperations.withdrawal?.receipt?.status === 'pending');
    check(control.withdrawalPosts.length === 1 && control.withdrawalGets.includes(key),'lost payout response recovers one accepted request');
    const persisted = await page.evaluate(() => JSON.stringify(Object.entries(localStorage)));
    check(!persisted.includes('+79001234567') && !persisted.includes('Тестовый банк') && !persisted.includes('1500.00'),'full payout requisites and amount never stored');
    control.ownerId = 456; await page.reload(); await ready(page); check(await page.evaluate(() => state.nativeOperations.withdrawal === null),'another owner cannot show previous payout receipt'); await context.close();
  }
  {
    const {page,context,control} = await fixturePage(); await ready(page); control.referral.current_balance = 5000; control.withdrawalRejectOnce = true;
    await withdrawalReview(page); await active(page).getByRole('button',{name:'Подать заявку',exact:true}).click();
    await active(page).getByText('Реквизиты не приняты. Проверьте номер телефона.',{exact:true}).waitFor();
    check(control.withdrawalsByKey.size === 0 && control.withdrawalPosts.length === 1,'known validation rejection creates no payout and does not auto-retry');
    check(await active(page).getByRole('button',{name:'Подать заявку',exact:true}).count() === 1,'known validation error leaves editable review, not unknown-result screen');
    const key = control.withdrawalPosts[0].request_key;
    await back(page); await active(page).getByLabel('Номер телефона',{exact:true}).fill('+79001234568');
    await active(page).getByRole('button',{name:'Продолжить',exact:true}).click(); await active(page).getByRole('button',{name:'Подать заявку',exact:true}).click(); await active(page).getByText('Заявка на рассмотрении',{exact:true}).waitFor();
    check(control.withdrawalPosts.length === 2 && control.withdrawalPosts[1].request_key === key && control.withdrawalsByKey.size === 1,'explicit corrected payout retry reuses request key exactly once'); await context.close();
  }
  {
    const {page,context,control} = await fixturePage(); await ready(page); control.referral.current_balance = 5000;
    await tab(page,'Заработать'); await referralReady(page); await active(page).getByRole('button',{name:'Вывести',exact:true}).click();
    await active(page).getByLabel('Сумма, ₽',{exact:true}).fill('1500.001'); await active(page).getByRole('button',{name:'Продолжить',exact:true}).click();
    check(await active(page).getByLabel('Сумма, ₽',{exact:true}).count() === 1,'fractional cent payout rejected');
    await active(page).getByLabel('Сумма, ₽',{exact:true}).fill('1500'); await active(page).getByRole('button',{name:'Продолжить',exact:true}).click(); await active(page).getByRole('button',{name:/^СБП/}).click();
    await active(page).getByLabel('Банк',{exact:true}).fill('Банк'); await active(page).getByLabel('Номер телефона',{exact:true}).fill('123'); await active(page).getByRole('button',{name:'Продолжить',exact:true}).click();
    check(await active(page).getByLabel('Номер телефона',{exact:true}).count() === 1 && control.withdrawalPosts.length === 0,'invalid payout phone never submitted');
    await back(page); await active(page).getByRole('button',{name:/^USDT/}).click(); await active(page).getByLabel('Адрес USDT',{exact:true}).fill('not-an-address'); await active(page).getByRole('button',{name:'Продолжить',exact:true}).click();
    check(await active(page).getByLabel('Адрес USDT',{exact:true}).count() === 1 && control.withdrawalPosts.length === 0,'invalid payout wallet never submitted'); await context.close();
  }
  for (const status of ['available','expired','max_limit','unavailable',undefined]) {
    const devices = {limit:status === 'max_limit' ? 10 : 5,max_limit:10,packages:status === 'available' ? [{count:1,price:50}] : [],...(status ? {addon_status:status} : {})};
    const {page,context} = await fixturePage({subs:[subscription(202,'regular',status === 'expired' ? 'expired' : 'active',{devices})]}); await ready(page); await detail(page,202);
    await active(page).locator('[data-action="device-addons"]').click();
    if (status === 'available') check(await active(page).locator('.choice-button').count() === 1,'available device addon package visible');
    else if (status === 'expired') check(/Сначала продлите/.test(await active(page).innerText()),'expired device addon explains renewal');
    else if (status === 'max_limit') check(/максимальный/.test(await active(page).innerText()),'maximum device limit explained');
    else if (status === 'unavailable') check(/Для этой подписки докупка устройств недоступна/.test(await active(page).innerText()) && !/максимальный/.test(await active(page).innerText()),'server unavailable eligibility is explicit, not fake maximum');
    else check(/временно недоступна/.test(await active(page).innerText()) && await active(page).getByRole('button',{name:'Обновить данные'}).count() === 1,'absent eligibility metadata offers retry, not fake maximum');
    await context.close();
  }
  {
    const {page,context,control} = await fixturePage(); await ready(page);
    control.devices = [
      {platform:'Android',device_model:'Планшет',user_agent:'Happ Plus/4 private-token',hwid:'private-hwid-one'},
      {platform:'iOS',device_model:'iPhone',user_agent:'INCY/2 private-token',hwid:'private-hwid-two'},
      {platform:'Android',device_model:'Телефон',user_agent:'Happ/4 private-token',hwid:'private-hwid-three'},
      {platform:'evil<img>',device_model:'<script>secret</script>',user_agent:'unknown<script>private-token</script>',hwid:'private-hwid-four'},
    ];
    await detail(page,202); await active(page).locator('[data-action="devices"]').click();
    await active(page).getByText('Добавлено: 4 из 5 устройств.',{exact:true}).waitFor();
    const html = await active(page).innerHTML(); const text = await active(page).innerText();
    check(text.includes('Happ Plus') && text.includes('INCY') && /\bHapp\b/.test(text) && /[Нн]е определено/.test(text),'app labels recognized conservatively');
    check(!html.includes('private-hwid') && !html.includes('private-token') && !html.includes('<script>'),'raw HWID/user agent and malicious metadata never rendered');
    await active(page).locator('[data-action="delete-device"]').first().click(); await active(page).getByText('Добавлено: 3 из 5 устройств.',{exact:true}).waitFor();
    check(control.devices.length === 3,'device deletion refreshes actual registration count');
    await active(page).locator('[data-action="delete-all-devices"]').click(); await active(page).getByText('Добавлено: 0 из 5 устройств.',{exact:true}).waitFor(); check(control.devices.length === 0,'delete all shows authoritative zero'); await context.close();
  }
  // Empty, single, missing URL and declined deletion do not invent access or make writes.
  for (const [name,subs] of [['empty',[]],['single',[subscription(101,'bypass')]],['missing-url',[subscription(101,'bypass','active',{subscription_url:null})]]]) {
    const {page,context,control} = await fixturePage({width:320,subs}); await ready(page);
    if (!subs.length) {
      check(/Подписок пока нет|Нет подписок/.test(await active(page).innerText()),'empty subscription state explicit');
      await active(page).getByRole('button',{name:/Купить подписку/}).waitFor();
    } else {
      await detail(page);
      if (name === 'missing-url') check(await active(page).locator('[data-action="happ"]').count() === 0,'no nonexistent subscription import');
      else {
        const more = active(page).locator('summary').filter({hasText:'Ещё'}); await more.click();
        await page.evaluate(() => { window.testConfirmResult = false; });
        await active(page).locator('[data-action="delete-subscription"]').click();
        check((await page.evaluate(() => window.testConfirms)).length === 1 && control.deletes.length === 0,'delete requires confirmation');
      }
    }
    await layout(page,`320-${name}`); await page.screenshot({path:path.join(out,`${name}-320.png`),fullPage:true});
    await context.close();
  }
  // Unrelated endpoint failures stay local to their tab and never appear as a zero wallet.
  for (const [endpoint,label] of [['/miniapp/api/subscriptions','Подписки'],['/miniapp/api/tariffs','Купить'],['/miniapp/api/referral','Заработать']]) {
    const {page,context,control} = await fixturePage({width:390,failures:{[endpoint]:true}}); await ready(page);
    await tab(page,label);
    await active(page).getByRole('button',{name:/Повторить|Попробовать|Обновить/}).first().waitFor();
    const text = await active(page).innerText();
    check(/Не удалось|недоступно/.test(text),'API error explicit');
    check(!/Доступно:\s*0\s*₽|Подписок пока нет|Нет подписок/.test(text),'failure not fake empty or zero balance');
    const retry = active(page).getByRole('button',{name:/Повторить|Попробовать|Обновить/});
    check(await retry.count() > 0,'failed tab offers retry');
    if (endpoint === '/miniapp/api/subscriptions') await page.screenshot({path:path.join(out,'error-390.png'),fullPage:true});
    delete control.failures[endpoint]; await retry.first().click();
    await page.waitForFunction(() => Object.values(state.dataStatus).every(status => status !== 'loading') && !/Не удалось|Временно недоступно/.test(document.querySelector('.view.active').innerText));
    await layout(page,`390-error-recovered-${label}`); await context.close();
  }
  {
    let release; const held = new Promise(resolve => { release = resolve; });
    const {page,context} = await fixturePage({holds:{'/miniapp/api/subscriptions':held}});
    const text = await active(page).innerText();
    check(!/Подписок пока нет|Нет подписок/.test(text),'waiting subscriptions do not fake empty state');
    check(await active(page).locator('[aria-busy="true"], [role="status"]').count() > 0,'loading announced');
    await page.screenshot({path:path.join(out,'loading-390.png'),fullPage:true});
    release(); await ready(page); await context.close();
  }
  {
    const endpoint = '/miniapp/api/subscriptions/101/devices';
    const {page,context,control} = await fixturePage({failures:{[endpoint]:true}}); await ready(page); await detail(page);
    await active(page).locator('[data-action="devices"]').click();
    await active(page).getByRole('button',{name:/Повторить|Попробовать|Обновить/}).waitFor();
    check(!/Устройств пока нет/.test(await active(page).innerText()),'device fetch error not fake empty');
    delete control.failures[endpoint]; await active(page).getByRole('button',{name:/Повторить|Попробовать|Обновить/}).click();
    await active(page).getByText('Android • Тестовый планшет',{exact:true}).waitFor();
    check(true,'device error retries current subscription'); await context.close();
  }
  {
    const {page,context,control} = await fixturePage({failures:{'/miniapp/api/me':true}});
    await active(page).getByRole('button',{name:/Повторить/}).waitFor();
    check(/Не удалось/.test(await active(page).innerText()),'authentication fetch error explicit');
    delete control.failures['/miniapp/api/me']; await active(page).getByRole('button',{name:/Повторить/}).click();
    await ready(page); check(await active(page).locator('.key-choice').count() === 3,'authentication retry restores data'); await context.close();
  }
  {
    const {page,context,control} = await fixturePage(); await ready(page); await detail(page,303);
    await active(page).locator('summary').filter({hasText:'Ещё'}).click();
    await active(page).locator('[data-action="delete-subscription"]').click();
    await page.waitForFunction(() => !state.subs.some(s => s.id === 303));
    check(control.deletes.length === 1 && control.deletes[0].endsWith('/303'),'confirmed expired subscription delete keeps existing API');
    check(await active(page).locator('.key-choice').count() === 2,'remaining subscriptions preserved after delete');
    await context.close();
  }
  {
    const {page,context} = await fixturePage({missingCatalogBase:true}); await ready(page); await tab(page,'Купить');
    check(!/200\s*ГБ/.test(await active(page).innerText()),'missing API quota not replaced with stale200GB');
    await context.close();
  }
  console.log(`MiniApp offline smoke passed: ${assertions} assertions; 320/390/430px; previews ${out}.`);
} finally {
  await browser.close();
}
