// Current central enrollment contract. No BotFather/token form or pre-payment bot.
import {readFile} from 'node:fs/promises';
import {browser, fixture, pay, pending, layout, close, check, order, PAYMENT} from './partner_pay_first_ui_smoke.mjs';

try {
  const html = await readFile('static/partners/index.html', 'utf8');
  const versions = [...html.matchAll(/(?:partners\.css|partners\.js|workspace\.js)\?v=([^"']+)/g)].map(match => match[1]);
  check(versions.length === 3 && new Set(versions).size === 1, 'CSS and both JS cache versions change together');
  for (const width of [320, 390, 430]) {
    const run = await fixture({width});
    check(await run.page.locator('.navigation:visible,input:visible,textarea:visible').count() === 0, 'Enrollment has no workspace navigation or setup fields');
    check(run.state.calls.length === 2 && run.state.calls.every(row => row.method === 'GET'), 'First screen loads settings and saved projects only');
    const text = await run.page.locator('body').innerText();
    check(!/VPN|300\s*ГБ|антиглуш|обход|Подтвердить покупку|Подготовить оплату/i.test(text), 'Enrollment copy is short and neutral');
    check(await run.page.getByRole('link', {name: 'Поддержка', exact: true}).getAttribute('href') === 'https://t.me/vpnislam', 'Support remains available before payment');
    await layout(run, `simple-offer-${width}`);
    await pay(run); await pending(run);
    check(run.state.checkouts === 1 && run.state.managed === 0, 'One explicit pay action creates only a saved invoice');
    check(JSON.stringify(await run.page.evaluate(() => window.__opened)) === JSON.stringify([PAYMENT]), 'First action opens payment immediately');
    check(!run.state.calls.some(row => row.endpoint === '/projects' && row.method === 'POST' || row.endpoint.endsWith('/bot')), 'Browser cannot choose a project price or send a bot token');
    const saved = await run.page.evaluate(() => Object.entries(localStorage));
    check(saved.length === 1 && saved[0][0] === 'spn-partner-enrollment:42' && /^[0-9a-f-]{36}$/i.test(saved[0][1]), 'Storage contains only the actor-bound pending operation UUID');
    await close(run);
  }
  const oldPaid = await fixture({savedOrder: order('paid_pending_launch', 1000)});
  await oldPaid.page.getByRole('button', {name: 'Создать сервис', exact: true}).waitFor();
  check(oldPaid.state.checkouts === 0 && !await oldPaid.page.getByRole('button', {name: 'Оплатить 9 990 ₽', exact: true}).count(), 'Historical paid 10 RUB order is not charged again');
  await close(oldPaid);

  const oldInvoice = await fixture({savedOrder: order('created', 1000)}); await pending(oldInvoice);
  check((await oldInvoice.page.locator('body').innerText()).includes('Сохранённый счёт: 10 ₽.'), 'Unpaid saved 10 RUB invoice keeps its amount');
  check(oldInvoice.state.checkouts === 0, 'Recovering a saved invoice never silently creates a 9 990 RUB charge'); await close(oldInvoice);

  const active = await fixture({active: true});
  check(await active.page.locator('input:visible,.navigation:visible').count() === 0, 'Completed enrollment contains no settings or configuration');
  check(await active.page.getByRole('link', {name: 'Перейти в моего бота', exact: true}).getAttribute('href') === 'https://t.me/OfflineServiceBot', 'Existing service opens its own bot'); await close(active);

  const stale = await fixture({fee: 1000});
  check(await stale.page.getByRole('button', {name: 'Оплатить 9 990 ₽', exact: true}).isDisabled(), 'Stale test-price configuration fails closed'); await close(stale);
  const error = await fixture({existing: true, failEntry: true}); await error.page.getByRole('alert').waitFor();
  check(!await error.page.getByRole('button', {name: 'Оплатить 9 990 ₽', exact: true}).count(), 'Unknown saved payment state never offers a duplicate payment');
  check(!(await error.page.locator('body').innerText()).includes('private-provider-secret'), 'Provider detail is not rendered'); await close(error);
  const noAuth = await fixture({auth: false}); check(noAuth.state.calls.length === 0, 'No Telegram authorization means no API request'); await close(noAuth);
  console.log(JSON.stringify({ok: true, suite: 'simple-pay-first', real_requests: 0, widths: [320, 390, 430]}));
} finally { await browser.close(); }
