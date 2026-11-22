// Managed creation follows an already confirmed payment. All I/O is intercepted.
import {browser, fixture, tick, hidden, close, check, until, order, OTHER, CREATE} from './partner_pay_first_ui_smoke.mjs';

const paid = options => fixture({savedOrder: order('paid_pending_launch'), ...options});
const start = run => run.page.getByRole('button', {name: 'Создать сервис', exact: true}).click();
async function pending(run) {
  await run.page.getByRole('link', {name: 'Создать сервис', exact: true}).waitFor();
  check(await run.page.locator('input,textarea').count() === 0, 'Managed creation never exposes a token form');
}
try {
  const basic = await paid(); await start(basic); await pending(basic);
  check(basic.state.checkouts === 0 && basic.state.managed === 1, 'Paid creation creates one managed request and no invoice');
  check(JSON.stringify(await basic.page.evaluate(() => window.__opened)) === JSON.stringify([CREATE]), 'Validated Telegram link opens directly');
  basic.state.connected = true; await tick(basic, 3001);
  await until(() => basic.state.managedPolls === 1, 'Managed GET did not run');
  await basic.page.getByText('Подключаем сервис…', {exact: true}).waitFor();
  check(!await basic.page.getByRole('button', {name: 'Завершить создание', exact: true}).count(), 'Normal creation does not add another confirmation step');
  const done = basic.state.managedPolls; await tick(basic, 3001);
  check(basic.state.managedPolls === done, 'Connected bot stops managed polling'); await close(basic);

  const retry = await paid({failManaged: true}); await start(retry); await retry.page.getByRole('alert').waitFor();
  check(!(await retry.page.locator('body').innerText()).includes('private-provider-secret'), 'Private managed-provider detail is not rendered');
  await start(retry); await pending(retry);
  const requests = retry.state.calls.filter(call => call.endpoint.endsWith('/managed-bot'));
  check(requests.length === 2 && JSON.stringify(requests[0].body) === JSON.stringify(requests[1].body), 'Retry keeps exact paid project version and creates no draft'); await close(retry);

  const busy = await paid(); let release;
  busy.state.managedHold = new Promise(resolve => release = resolve);
  await start(busy); await until(() => busy.state.managed === 1, 'Managed operation did not start');
  await busy.page.getByRole('button', {name: 'Создать сервис', exact: true}).evaluate(node => { node.click(); node.click(); });
  check(busy.state.managed === 1, 'Repeated taps cannot duplicate an in-flight managed request'); release(); await pending(busy); await close(busy);

  const paused = await paid(); await start(paused); await pending(paused); await hidden(paused, true); await tick(paused, 60000);
  check(paused.state.managedPolls === 0, 'Hidden creation page does not poll'); await hidden(paused, false); await tick(paused, 1);
  await until(() => paused.state.managedPolls === 1, 'Visibility did not resume managed polling'); await close(paused);

  const slow = await paid(); await start(slow); await pending(slow); let finish;
  slow.state.managedPollHold = new Promise(resolve => finish = resolve);
  await tick(slow, 3001); await until(() => slow.state.managedPolls === 1, 'Held managed check did not start');
  await slow.page.evaluate(() => window.dispatchEvent(new Event('focus'))); await tick(slow, 6001);
  check(slow.state.managedPolls === 1 && slow.state.maxActiveManagedPolls === 1, 'Focus and timers cannot overlap a slow managed check');
  finish(); await until(() => slow.state.activeManagedPolls === 0, 'Held managed check did not finish'); await close(slow);

  const wrong = await paid(); await start(wrong); await pending(wrong); wrong.state.connected = true; wrong.state.returnId = OTHER;
  await tick(wrong, 3001); await until(() => wrong.state.managedPolls === 1, 'Foreign-project response did not return');
  check(await wrong.page.getByRole('link', {name: 'Создать сервис', exact: true}).count() === 1, 'A foreign-project response cannot bind or replace the selected project');
  check(wrong.state.polls === 0, 'Foreign response cannot begin paid launch reconciliation'); await close(wrong);

  const switched = await paid({multiple: true}); await start(switched); await pending(switched); let releaseOld;
  switched.state.managedPollHold = new Promise(resolve => releaseOld = resolve);
  await tick(switched, 3001); await until(() => switched.state.managedPolls === 1, 'Old-project poll did not start');
  await switched.page.getByText('Мои сервисы', {exact: true}).click();
  await switched.page.getByRole('button', {name: 'Незавершённое подключение', exact: true}).nth(1).click();
  await switched.page.getByRole('button', {name: 'Оплатить 9 990 ₽', exact: true}).waitFor();
  switched.state.connected = true; releaseOld(); await until(() => switched.state.activeManagedPolls === 0, 'Late old-project response did not return'); await tick(switched, 60000);
  check(await switched.page.getByRole('button', {name: 'Оплатить 9 990 ₽', exact: true}).count() === 1 && switched.state.polls === 0, 'Late old-project response cannot replace selection or start paid launch'); await close(switched);

  for (const status of [401, 403, 404]) {
    const rejected = await paid(); await start(rejected); await pending(rejected); rejected.state.managedStatus = status;
    await tick(rejected, 3001); await until(() => rejected.state.managedPolls === 1, 'Rejected managed poll did not run'); await tick(rejected, 60000);
    await rejected.page.evaluate(() => window.dispatchEvent(new Event('focus'))); await tick(rejected, 1);
    check(rejected.state.managedPolls === 1, `${status} stops managed retries and focus resume`); await close(rejected);
  }
  const unavailable = await paid({available: false});
  check(await unavailable.page.getByRole('button', {name: 'Создать сервис', exact: true}).isDisabled(), 'Unavailable managed service cannot begin creation'); await tick(unavailable, 60000);
  check(unavailable.state.managed === 0 && unavailable.state.checkouts === 0, 'Unavailable paid state creates neither a new bot nor another charge'); await close(unavailable);
  const malicious = await paid({badCreation: true}); await start(malicious); await malicious.page.getByRole('alert').waitFor(); await tick(malicious, 60000);
  check(!(await malicious.page.evaluate(() => window.__opened)).length && malicious.state.managedPolls === 0, 'Untrusted creation URL is neither opened nor polled'); await close(malicious);
  console.log(JSON.stringify({ok: true, suite: 'managed-after-payment', real_requests: 0, fake_clock: true}));
} finally { await browser.close(); }
