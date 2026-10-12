// Offline fixture UI checks. Every request is intercepted: no provider, DB or Telegram.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || '/Users/khamzat/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs');
const browser = await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless:true});
const context = await browser.newContext({viewport:{width:1280,height:1000}});
await context.addInitScript(() => {
  window.Telegram = {WebApp:{initData:'offline-admin-test',ready(){},expand(){}}};
});
const requests = [], savedRefunds = [], errors = [];
let alreadyRecorded = false;
const funnel = {views:12,unique_viewers:8,attributed_registrations:7,first_payments:3,repeat_payments:2,
  external_paid_amount:'800.00',credited_earnings:'216.00',reversed_earnings:'7.00',wallet_spend:'160.00',
  cash_payouts:'1500.00',pending_cash_payouts:'1500.00',unlinked_legacy_earnings:1};
await context.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.host !== 'preview.test') return route.fulfill({status:200,contentType:'application/javascript',body:''});
  if (url.pathname.startsWith('/admin/api/')) {
    const data = request.method() === 'POST' ? request.postDataJSON() : null;
    requests.push({path:url.pathname, method:request.method(), data});
    assert.equal(request.headers().authorization, 'tma offline-admin-test');
    if (url.pathname === '/admin/api/session') return route.fulfill({json:{authenticated:true}});
    if (url.pathname === '/admin/api/dashboard') return route.fulfill({json:{}});
    if (url.pathname === '/admin/api/withdrawals') return route.fulfill({json:{items:[
      {program:'referral',id:1,user_id:123,username:'test',amount:'1500.00',withdrawal_type:'sbp',bank_name:'Тестовый банк',phone_number:'+70000000000',status:'pending',current_available_balance:'-120.00',payout_blocked:true},
      {program:'partner',id:2,user_id:123,username:'test',amount:'1500.00',withdrawal_type:'sbp',bank_name:'Тестовый банк',phone_number:'+70000000000',status:'pending',current_available_balance:null,payout_blocked:false},
    ],has_more:false}});
    if (url.pathname === '/admin/api/referrals/funnel') return route.fulfill({json:funnel});
    if (url.pathname === '/admin/api/referrals/review') return route.fulfill({json:{items:url.searchParams.get('offset') === '0' ? [
      {kind:'unlinked_legacy_earning',payment_id:null,invoice_id:null,buyer_id:123,source_id:4,amount:'56.00',detail:'Проверить <img src=x onerror="alert(1)"> вручную'},
    ] : [], has_more:url.searchParams.get('offset') === '0'}});
    if (url.pathname === '/admin/api/referrals/refunds/prepare') {
      if (Number(data.amount) > 140 && !alreadyRecorded) return route.fulfill({status:409,json:{detail:'Сумма превышает остаток фактически оплаченного счёта.'}});
      return route.fulfill({json:{review_token:'offline-review-token',expires_in:300,preview:{
        invoice_id:data.invoice_id,payment_id:4,buyer_id:123,actual_paid_rub:'160.00',refunded_rub:alreadyRecorded?'160.00':'20.00',remaining_rub:alreadyRecorded?'0.00':'140.00',
        linked:false,settled:true,manual_review_required:true,already_recorded:alreadyRecorded,
        recorded_refund:alreadyRecorded?{amount:data.amount,reference:data.reference,note:'Исходная причина <test>'}:null,
      }}});
    }
    if (url.pathname === '/admin/api/referrals/refunds/confirm') {
      assert.equal(data.confirm_transferred, true);
      assert.equal(data.review_token, 'offline-review-token');
      if (data.reference === 'expired-review') return route.fulfill({status:409,json:{detail:'Проверка устарела или данные изменились. Повторите первый шаг.'}});
      savedRefunds.push(data);
      return route.fulfill({json:{id:2,amount:data.amount,legacy_review_required:true}});
    }
    return route.fulfill({status:404,json:{detail:'Unexpected offline API request'}});
  }
  const local = url.pathname === '/admin' ? 'static/admin/index.html' : 'static' + url.pathname;
  assert.match(local, /^static\/admin\//);
  const mime = local.endsWith('.js')?'application/javascript':local.endsWith('.css')?'text/css':'text/html';
  return route.fulfill({contentType:mime,body:await readFile(path.join(root,local))});
});

try {
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(String(error)));
  await page.goto('https://preview.test/admin');
  await page.locator('[data-section="withdrawals"]').click();
  await page.locator('#withdrawalsTable').getByText('Выплата заблокирована: отрицательный баланс').waitFor();
  const referralPayout = page.locator('[data-payout="referral:1"][data-decision="completed"]');
  assert.equal(await referralPayout.isDisabled(), true);
  assert.equal(await page.locator('[data-payout="referral:1"][data-decision="rejected"]').isDisabled(), false);
  assert.equal(await page.locator('[data-payout="partner:2"][data-decision="completed"]').isDisabled(), false);
  await referralPayout.evaluate(button => button.click());
  assert.equal(requests.filter(r => r.path.startsWith('/admin/api/withdrawals/')).length, 0);
  await page.locator('#section-withdrawals').getByText(/Перед реальным переводом обновите список/).waitFor();
  await page.locator('[data-section="referrals"]').click();
  await page.locator('#referralStatsGrid').getByText('216 ₽').waitFor();
  await page.locator('#referralReviewTable').getByText('Проверить <img src=x onerror="alert(1)"> вручную').waitFor();
  assert.equal(await page.locator('#referralReviewTable img').count(), 0);
  await page.locator('[data-review-page="next"]').click();
  await page.locator('#referralReviewTable').getByText('Записей для ручной сверки не найдено').waitFor();
  await page.locator('[data-review-page="prev"]').click();

  async function fillRefund(amount='56,00', reference='test-transfer') {
    await page.locator('#refundForm [name="invoice_id"]').fill('invoice-<test>');
    await page.locator('#refundForm [name="amount"]').fill(amount);
    await page.locator('#refundForm [name="reference"]').fill(reference);
    await page.locator('#refundForm [name="note"]').fill('Возврат <b>по обращению</b>');
  }
  await fillRefund('NaN');
  await page.locator('#refundForm button').click();
  assert.equal(requests.filter(r => r.path.endsWith('/prepare')).length, 0);
  await fillRefund();
  await page.locator('#refundForm button').click();
  await page.locator('#refundDialog').waitFor({state:'visible'});
  assert.equal(savedRefunds.length, 0);
  assert.equal(await page.locator('#confirmRefund').isDisabled(), true);
  assert.equal(await page.locator('#refundReviewDetails b').filter({hasText:'по обращению'}).count(), 0);
  await page.locator('#refundReviewDetails').getByText('Причина: Возврат <b>по обращению</b>').waitFor();
  await page.locator('#refundReviewDetails').getByText(/старый счёт без связанного начисления/).waitFor();
  await page.screenshot({path:'/private/tmp/referral-growth-admin-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({path:'/private/tmp/referral-growth-admin-mobile.png',fullPage:true});
  await page.locator('#refundActualTransfer').check();
  await page.locator('#confirmRefund').click();
  await page.locator('#refundDialog').waitFor({state:'hidden'});
  assert.equal(savedRefunds.length, 1);
  assert.equal(savedRefunds[0].amount, '56.00');
  await page.locator('#refundResult').getByText(/Старые непривязанные начисления/).waitFor();

  alreadyRecorded = true;
  await fillRefund();
  await page.locator('#refundForm button').click();
  await page.locator('#refundDialog').waitFor({state:'visible'});
  await page.locator('#refundReviewDetails').getByText(/Этот перевод уже учтён/).waitFor();
  await page.locator('#refundReviewDetails').getByText('Причина: Исходная причина <test>').waitFor();
  await page.locator('#cancelRefund').click();
  assert.equal(savedRefunds.length, 1);

  alreadyRecorded = false;
  await fillRefund('140.01');
  await page.locator('#refundForm button').click();
  await page.locator('#toast').getByText(/Сумма превышает остаток/).waitFor();
  assert.equal(await page.locator('#refundDialog').isVisible(), false);

  await fillRefund('56.00', 'expired-review');
  await page.locator('#refundForm button').click();
  await page.locator('#refundDialog').waitFor({state:'visible'});
  await page.locator('#refundActualTransfer').check();
  await page.locator('#confirmRefund').click();
  await page.locator('#toast').getByText(/Проверка устарела/).waitFor();
  assert.equal(savedRefunds.length, 1);
  await page.locator('#cancelRefund').click();
  assert.deepEqual(errors, []);
  console.log('Referral admin fixture smoke passed: blocked negative-balance referral payout, partner unchanged, desktop/390px, escaped review, pagination, two-step refund, repeat review, cap, expired review; no external requests.');
} finally {
  await browser.close();
}
