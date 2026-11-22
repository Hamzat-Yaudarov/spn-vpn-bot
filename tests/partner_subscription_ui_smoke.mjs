// Everything is intercepted. No live Telegram, VPN, clipboard, billing or DNS.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {readFile, mkdir} from 'node:fs/promises';
import path from 'node:path';

const root = process.cwd();
const out = '/private/tmp/spn-partner-subscription-preview';
const runtime = '/Users/khamzat/.cache/codex-runtimes/codex-primary-runtime/dependencies/node';
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || `${runtime}/node_modules/playwright/index.mjs`);
const html = Object.fromEntries(['active', 'low', 'long', 'expired', 'updating', 'error'].map(state => [state,
  execFileSync(process.env.PYTHON || 'python3', ['-c',
    'from scripts.partner_subscription_preview import demo_page; import sys; sys.stdout.buffer.write(demo_page(sys.argv[1]).body)', state],
    {cwd: root, env: {...process.env, PYTHON_DOTENV_DISABLED: '1'}}).toString()]));
const css = await readFile(path.join(root, 'static/partner-subscription/assets/subscription.css'));
const js = await readFile(path.join(root, 'static/partner-subscription/assets/subscription.js'));
const browser = await chromium.launch({executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true});
await mkdir(out, {recursive: true});
let checks = 0;
function check(value, message) { assert.ok(value, message); checks += 1; }
try {
  for (const width of [320, 390, 430]) {
    const context = await browser.newContext({viewport: {width, height: 844}});
    const errors = [], unexpected = [];
    let state = 'active';
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== 'https://demo.partners.example.test') {
        unexpected.push(route.request().url());
        return route.abort();
      }
      if (url.pathname === '/preview') return route.fulfill({contentType: 'text/html', body: html[state]});
      if (url.pathname.endsWith('/subscription.css')) return route.fulfill({contentType: 'text/css', body: css});
      if (url.pathname.endsWith('/subscription.js')) return route.fulfill({contentType: 'application/javascript', body: js});
      unexpected.push(route.request().url());
      return route.abort();
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    for (state of ['active', 'low', 'long', 'expired', 'updating', 'error']) {
      await page.goto('https://demo.partners.example.test/preview');
      const content = await page.locator('main').textContent();
      check(content.length > 100, `${state}/${width}: rendered`);
      check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${state}/${width}: no horizontal overflow`);
      for (const button of await page.locator('a.button, button.button, summary').all()) {
        check((await button.boundingBox()).height >= 48, `${state}/${width}: touch target`);
      }
      if (state === 'active' || width === 320) {
        await page.screenshot({path: `${out}/${state}-${width}.png`, fullPage: true});
      }
      if (['active', 'low', 'long'].includes(state)) {
        check(await page.locator('#copy-link').count() === 1, `${state}/${width}: copy visible`);
        check((await page.locator('#subscription-link').inputValue()).endsWith('/config'), `${state}/${width}: client config URL, not HTML page`);
        check(content.includes(state === 'low' ? '0,5 ГБ' : '300 ГБ'), `${state}/${width}: actual remaining`);
        check(!content.includes('700 ГБ'), `${state}/${width}: no historical cap`);
        await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', {configurable: true,
          value: {writeText: async text => {window.__copiedLink = text;}}}));
        await page.locator('#copy-link').click();
        check((await page.locator('#copy-status').textContent()).includes('скопирована'), `${state}/${width}: spoken copy status`);
        check(await page.evaluate(() => window.__copiedLink === document.querySelector('#subscription-link').value), `${state}/${width}: exact copy without real clipboard`);
        await page.evaluate(() => {navigator.clipboard.writeText = async () => {throw Error('denied');};});
        await page.locator('#copy-link').click();
        check(await page.locator('details').evaluate(node => node.open), `${state}/${width}: manual-copy fallback`);
        check((await page.locator('#copy-status').textContent()).includes('вручную'), `${state}/${width}: fallback instruction`);
      } else {
        check(await page.locator('#copy-link').count() === 0, `${state}/${width}: no misleading import`);
        check(!content.includes('Осталось трафика'), `${state}/${width}: no zero invented`);
      }
      // 200% text expansion, without disabling the viewport's user zoom.
      await page.evaluate(() => {
        const sizes = [...document.querySelectorAll('main *')].map(node => [node, getComputedStyle(node).fontSize]);
        for (const [node, size] of sizes) node.style.fontSize = `${parseFloat(size)*2}px`;
      });
      check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${state}/${width}: expanded text fits`);
      check(!(await page.locator('meta[name=viewport]').getAttribute('content')).includes('user-scalable=no'), `${state}/${width}: zoom allowed`);
    }
    check(errors.length === 0, `${width}: no JS errors ${errors}`);
    check(unexpected.length === 0, `${width}: no external/API requests ${unexpected}`);
    await context.close();
  }
  console.log(`Subscription UI: ${checks} assertions passed. Screenshots: ${out}`);
} finally {
  await browser.close();
}
