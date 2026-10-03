// Isolated UI checks. All requests are fulfilled locally; no user data or Telegram messages.
import { readFile, mkdir } from 'node:fs/promises';
import assert from 'node:assert/strict';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE);
const root = process.cwd();
const out = root + '/artifacts/referral-upgrade';
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
 '/miniapp/api/referral': {link:'https://t.me/WaySPN_robot?start=ref_123',bot_earning_url:'https://t.me/WaySPN_robot?start=earn',share_text:'Тест приглашения',minimum_withdrawal:1500,current_balance:1400,total_earned:3000,active_referrals:12,history:[{kind:'earning',id:1,amount:105,status:'credited',detail:'regular_1m',created_at:'2026-10-03T09:00:00Z'},{kind:'withdrawal',id:1,amount:1500,status:'pending',detail:'sbp',created_at:'2026-10-02T09:00:00Z'}]},
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
await page.locator('[data-view="ref"]').click();
await page.getByText('До вывода осталось 100 ₽').waitFor();
assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
await page.getByRole('button',{name:'📨 Пригласить друга'}).click();
assert.match(await page.evaluate(() => window.openedLinks[0]), /t\.me\/share\/url/);
await page.getByRole('button',{name:'Вывести в боте'}).click();
assert.match(await page.evaluate(() => window.openedLinks[1]), /start=earn/);
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
