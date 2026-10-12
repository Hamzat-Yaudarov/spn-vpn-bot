// Optional browser regression: NODE_PATH=<playwright modules> node this-file.cjs.
// Everything is local/mocked. No Remnawave, YooKassa or Telegram calls.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..", "static", "site");
const token = "https://sub.wayspn.online/TestSecretAbCd12";
const invoice = "32277abd-000f-5000-b000-1b46db3fc1db";
const server = http.createServer((req, res) => {
  const pathname = new URL(req.url, "http://localhost").pathname;
  const relative = pathname === "/renew" ? "renew.html" : pathname.replace(/^\/site\//, "");
  if (!["renew.html", "assets/renew.css", "assets/renew.js"].includes(relative)) {
    res.writeHead(404); res.end(); return;
  }
  res.setHeader("Content-Type", relative.endsWith("html") ? "text/html; charset=utf-8" : relative.endsWith("css") ? "text/css" : "application/javascript");
  res.setHeader("Referrer-Policy", "no-referrer");
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'");
  res.end(fs.readFileSync(path.join(root, relative)));
});

function details(overrides = {}) {
  return {
    subscription: { name: "Обычная #2", subscription_until: "2030-01-20T00:00:00Z" },
    tariffs: [ { code: "regular_1m", days: 30, price: 160, original_price: 200 },
               { code: "regular_3m", days: 90, price: 400, original_price: 500 } ],
    pending_invoice_id: null, support_url: "https://t.me/wayspn_support", agreement_url: "",
    ...overrides,
  };
}

(async () => {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  let passed = 0;
  async function scenario(name, run, options = {}) {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, ...options });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    try { await run(page, context); assert.deepEqual(errors, []); passed++; console.log(`OK ${name}`); }
    finally { await context.close(); }
  }

  try {
    await scenario("no token never requests subscription data", async (page) => {
      const requests = [];
      await page.route("**/site/api/renewal/**", (route) => { requests.push(route.request().url()); return route.abort(); });
      await page.goto(`${origin}/renew`);
      await page.locator("#error").waitFor({ state: "visible" });
      const help = await page.locator("#error").textContent();
      assert.match(help, /Happ нажмите ⓘ слева от трафика/);
      assert.match(help, /INCY — «🌐 Сайт»/);
      assert.deepEqual(requests, []);
    });

    await scenario("mobile details, safe fragment, discounted prices", async (page) => {
      await page.route("**/site/api/renewal/details", async (route) => {
        assert.equal(route.request().headers().authorization, `Bearer ${token}`);
        assert.equal(route.request().headers().cookie, undefined);
        assert.ok(!route.request().url().includes("TestSecret"));
        await route.fulfill({ json: details() });
      });
      await page.goto(`${origin}/renew#${encodeURIComponent(token)}`);
      await page.locator("#content").waitFor({ state: "visible" });
      assert.equal(page.url(), `${origin}/renew`);
      assert.equal(await page.locator("#subscription-name").textContent(), "Обычная #2");
      assert.equal(await page.locator(".tariff s").count(), 2);
      assert.match(await page.locator("#total").textContent(), /160/);
      await page.locator("input[value=regular_3m]").check();
      assert.match(await page.locator("#total").textContent(), /400/);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: "/private/tmp/wayspn-renewal-mobile.png", fullPage: true });
    });

    await scenario("failed checkout reuses request ID; provider does not see credential", async (page) => {
      const checkouts = [];
      await page.route("**/site/api/renewal/details", (route) => route.fulfill({ json: details() }));
      await page.route("**/site/api/renewal/payment", async (route) => {
        const body = route.request().postDataJSON();
        checkouts.push(body);
        assert.equal(body.expected_price, 160);
        assert.deepEqual(Object.keys(body).sort(), ["expected_price", "request_id", "tariff_code"]);
        if (checkouts.length === 1) await route.fulfill({ status: 503, json: { detail: "Попробуйте ещё раз" } });
        else await route.fulfill({ json: { invoice_id: invoice, pay_url: "https://pay.example/test", status: "pending" } });
      });
      await page.route("https://pay.example/**", async (route) => {
        assert.equal(route.request().headers().referer, undefined);
        assert.ok(!route.request().url().includes(token));
        await route.fulfill({ contentType: "text/html", body: "Mock payment provider" });
      });
      await page.goto(`${origin}/renew#${token}`);
      await page.locator("#content").waitFor({ state: "visible" });
      await page.locator("#pay-button").click();
      await page.locator("#error").waitFor({ state: "visible" });
      await page.locator("#pay-button").click();
      await page.waitForURL("https://pay.example/test");
      assert.equal(checkouts.length, 2);
      assert.equal(checkouts[0].request_id, checkouts[1].request_id);
      await page.route(`**/site/api/renewal/payments/${invoice}`, (route) => route.fulfill({ json: { status: "paid", invoice_id: invoice } }));
      await page.goto(`${origin}/renew?payment=return`);
      await page.locator("#success").waitFor({ state: "visible" });
      assert.match(await page.locator("#success").textContent(), /Подписка продлена/);
      assert.equal(await page.locator("#renew-form").isVisible(), false);
      assert.equal(await page.evaluate(() => JSON.parse(sessionStorage.getItem("wayspn.renewal.v1")).invoiceId), undefined);
      await page.screenshot({ path: "/private/tmp/wayspn-renewal-success.png", fullPage: true });
    });

    await scenario("reopen subscription picks up pending payment", async (page) => {
      await page.route("**/site/api/renewal/details", (route) => route.fulfill({ json: details({ pending_invoice_id: invoice }) }));
      await page.route(`**/site/api/renewal/payments/${invoice}`, (route) => route.fulfill({ json: { status: "pending", invoice_id: invoice } }));
      await page.goto(`${origin}/renew#${token}`);
      await page.locator("#payment-status").waitFor({ state: "visible" });
      assert.match(await page.locator("#payment-status").textContent(), /автоматически/);
    });

    await scenario("new link resets previous subscription state", async (page) => {
      let lastAuthorization;
      await page.route("**/site/api/renewal/details", (route) => {
        lastAuthorization = route.request().headers().authorization;
        return route.fulfill({ json: details() });
      });
      await page.goto(`${origin}/renew#${token}`);
      await page.locator("#content").waitFor({ state: "visible" });
      await page.evaluate((invoiceId) => {
        const value = JSON.parse(sessionStorage.getItem("wayspn.renewal.v1"));
        value.invoiceId = invoiceId;
        sessionStorage.setItem("wayspn.renewal.v1", JSON.stringify(value));
      }, invoice);
      const other = "https://sub.wayspn.online/OtherSecretAbC12";
      await page.goto(`${origin}/renew#${other}`);
      await page.waitForFunction((url) => JSON.parse(sessionStorage.getItem("wayspn.renewal.v1")).url === url, other);
      await page.locator("#content").waitFor({ state: "visible" });
      assert.equal(lastAuthorization, `Bearer ${other}`);
      assert.equal(await page.evaluate(() => JSON.parse(sessionStorage.getItem("wayspn.renewal.v1")).invoiceId), undefined);
    });

    await scenario("desktop fits; untrusted text is never HTML", async (page) => {
      await page.route("**/site/api/renewal/details", (route) => route.fulfill({ json: details({
        subscription: { name: '<img src=x onerror="throw 1">', subscription_until: "2020-01-20T00:00:00Z" },
      }) }));
      await page.goto(`${origin}/renew#${token}`);
      await page.locator("#content").waitFor({ state: "visible" });
      assert.equal(await page.locator("#subscription-name img").count(), 0);
      assert.match(await page.locator("#subscription-until").textContent(), /Закончилась/);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }, { viewport: { width: 1280, height: 900 } });
    console.log(`Passed ${passed} browser scenarios`);
  } finally { await browser.close(); await new Promise((resolve) => server.close(resolve)); }
})().catch((error) => { console.error(error); server.close(); process.exitCode = 1; });
