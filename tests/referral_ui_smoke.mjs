// Isolated UI checks. All requests are fulfilled locally; no user data or Telegram messages.
import { readFile, mkdir } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';
const modulePath = process.env.PLAYWRIGHT_MODULE || '/Users/khamzat/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs';
const { chromium } = await import(modulePath.startsWith('file:') ? modulePath : pathToFileURL(modulePath).href);
const root = process.cwd();
const out = process.env.SPN_REFERRAL_PREVIEW_DIR || '/private/tmp/spn-referral-growth-preview';
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
const context = await browser.newContext({viewport:{width:390,height:844}});
await context.addInitScript(() => {
  window.openedLinks = [];
  window.Telegram = {WebApp:{initData:'local-test-fixture',ready(){},expand(){},openTelegramLink(u){window.openedLinks.push(u);}}};
});
const payout = {program:'referral',id:1,user_id:123,username:'test_user',amount:1500,withdrawal_type:'sbp',bank_name:'Тестовый банк <test>',phone_number:'+70000000000',status:'pending',created_at:'2026-10-03T09:00:00Z',notification_attempts:1,notification_error:'RuntimeError'};
const fixtures = {
 '/miniapp/api/me': {id:123,first_name:'Тест'},
 '/miniapp/api/tariffs': {regular:[],bypass:[],traffic_packages:[]},
 '/miniapp/api/subscriptions': {subscriptions:[]},
 '/miniapp/api/referral': {link:'https://t.me/WaySPN_robot?start=ref_123',bot_earning_url:'https://t.me/WaySPN_robot?start=earn',share_text:'Лучший VPN:',minimum_withdrawal:1500,current_balance:1400,total_earned:3000,active_referrals:12,history:[{kind:'earning',id:1,amount:105,status:'credited',detail:'regular_1m',created_at:'2026-10-03T09:00:00Z'},{kind:'withdrawal',id:1,amount:1500,status:'pending',detail:'sbp',created_at:'2026-10-02T09:00:00Z'}]},
 '/admin/api/session': {id:999}, '/admin/api/dashboard': {},
 '/admin/api/withdrawals': {items:[payout],has_more:false},
 '/site/api/catalog': {regular:[],bypass:[],traffic_packages:[]},
 '/site/api/config': {}, '/site/api/me': {login:'test_user'},
 '/site/api/subscriptions': {subscriptions:[]}, '/site/api/payments': {payments:[]},
};
await context.route('**/*', async route => {
 const url = new URL(route.request().url());
 if (url.host !== 'preview.test') return route.fulfill({status:200,contentType:'application/javascript',body:''});
 if (fixtures[url.pathname]) return route.fulfill({json:fixtures[url.pathname]});
 const path = url.pathname === '/app' ? '/miniapp/index.html' : url.pathname === '/admin' ? '/admin/index.html' : url.pathname === '/account' ? '/site/index.html' : url.pathname.replace(/^\/app\//,'/miniapp/');
 if (!/^\/(miniapp|admin|site)\//.test(path)) return route.fulfill({status:404,body:''});
 const mime = path.endsWith('.js')?'application/javascript':path.endsWith('.css')?'text/css':'text/html';
 return route.fulfill({contentType:mime,body:await readFile(root+'/static'+path)});
});
const page = await context.newPage();
const errors = [];
page.on('pageerror', e => errors.push(String(e)));
await page.goto('https://preview.test/app');
await page.waitForFunction(() => typeof state !== 'undefined' && state.authStatus === 'ready'
  && ['subs','tariffs','ref'].every(section => state.dataStatus[section] === 'ready'));
assert.equal(await page.locator('.tabbar .tab').count(),4);
assert.equal(await page.locator('.tabbar [data-view="home"]').count(),0);
assert.equal(await page.locator('.tabbar [data-view="subs"]').getAttribute('aria-current'),'page');
await page.locator('[data-view="ref"]').click();
await page.getByText('До вывода осталось 100 ₽').waitFor();
assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
await page.getByRole('button',{name:'Пригласить друга',exact:true}).click();
const invitation = new URL(await page.evaluate(() => window.openedLinks[0]));
assert.equal(invitation.pathname,'/share/url');
assert.equal(invitation.searchParams.get('text'),'Лучший VPN:');
assert.equal(invitation.searchParams.get('url'),fixtures['/miniapp/api/referral'].link);
await page.getByRole('button',{name:'Вывести',exact:true}).click();
await page.getByLabel('Сумма, ₽',{exact:true}).waitFor();
assert.equal((await page.evaluate(() => window.openedLinks)).length,1,'withdrawal form stays in MiniApp');
await page.locator('#view-ref').getByRole('button',{name:/Назад/}).click();
await page.getByRole('button',{name:'Потратить на подписку',exact:true}).click();
await page.getByRole('button',{name:/^Купить новую подписку/}).waitFor();
assert.equal((await page.evaluate(() => window.openedLinks)).length,1,'wallet chooser stays in MiniApp');
await page.locator('#view-ref').getByRole('button',{name:/Назад/}).click();
await page.locator('#view-ref summary').filter({hasText:'Статистика'}).click();
await page.locator('#view-ref').getByText('—',{exact:true}).waitFor();
await page.locator('#view-ref summary').filter({hasText:'История'}).click();
const withdrawal = page.locator('#referral-history .ref-history-row').filter({hasText:'Заявка R-1 · На рассмотрении'});
await withdrawal.waitFor();
assert.match(await withdrawal.locator('b').textContent(),/^1\s500 ₽$/);
await page.screenshot({path:out+'/miniapp.png',fullPage:true});
await page.setViewportSize({width:1280,height:1000});
await page.goto('https://preview.test/admin');
await page.locator('[data-section="withdrawals"]').click();
await page.getByText('Тестовый банк <test>').waitFor();
assert.equal(await page.locator('test').count(),0);
await page.screenshot({path:out+'/admin.png',fullPage:true});
await page.goto('https://preview.test/account');
await page.locator('[data-account-section="earn"]').click();
await page.getByRole('heading',{name:'💰 Зарабатывать'}).waitFor();
assert.match(await page.getByRole('link',{name:'Открыть заработок в Telegram'}).getAttribute('href'), /start=earn/);
await page.screenshot({path:out+'/site.png',fullPage:true});
assert.deepEqual(errors,[]);
await browser.close();
console.log('UI smoke passed: MiniApp (390px), admin, website; no external requests.');
