// Visual and asset checks against the actual origin; bypass stale DNS caches only.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({headless:true, channel:'chrome', args:[
    '--host-resolver-rules=MAP chislo.site 31.76.50.87, MAP www.chislo.site 31.76.50.87',
  ]});
  try {
    for (const [name, viewport] of [['desktop',{width:1440,height:1000}], ['mobile',{width:390,height:844}]]) {
      const context = await browser.newContext({viewport});
      const page = await context.newPage();
      const errors = [];
      const bad = [];
      page.on('pageerror', e => errors.push(e.message));
      page.on('response', r => { if (r.status() >= 400) bad.push(`${r.status()} ${r.url()}`); });
      const response = await page.goto('https://chislo.site/', {waitUntil:'networkidle'});
      assert.equal(response.status(), 200);
      assert.match(await page.title(), /CHISLO/);
      assert.equal(await page.locator('.card').count(), 3);
      assert.ok(await page.locator('img').evaluateAll(images => images.every(i => i.complete && i.naturalWidth > 0)));
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({path:`/private/tmp/chislo-${name}.png`,fullPage:true});
      for (const href of await page.locator('.card').evaluateAll(cards => cards.map(c => c.href))) {
        const asset = await page.goto(href);
        assert.equal(asset.status(), 200);
        assert.match(asset.headers()['content-type'], /image\/svg\+xml/);
      }
      assert.deepEqual(errors, []);
      assert.deepEqual(bad, []);
      await context.close();
      console.log(`OK ${name}: origin TLS, layout, images and all three SVG originals`);
    }
    const context = await browser.newContext();
    const page = await context.newPage();
    assert.equal((await page.goto('https://www.chislo.site/')).status(), 200);
    assert.equal((await page.goto('https://chislo.site/missing-page')).status(), 404);
    await context.close();
    console.log('OK www certificate and unknown-path 404');
  } finally { await browser.close(); }
})().catch(e => {console.error(e); process.exitCode=1;});
