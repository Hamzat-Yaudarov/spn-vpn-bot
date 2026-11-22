// Offline renewal-widget checks against the real checkout-enabled renderer.
// Every browser request is intercepted: no real checkout, Telegram, VPN or DNS.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {readFile,mkdir} from 'node:fs/promises';
import path from 'node:path';

const root=process.cwd(),out='/private/tmp/spn-partner-public-checkout-preview';
const runtime='/Users/khamzat/.cache/codex-runtimes/codex-primary-runtime/dependencies/node';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||`${runtime}/node_modules/playwright/index.mjs`);
const html=execFileSync(process.env.PYTHON||'python3',['-c',`
from datetime import datetime,timezone
from partner_subscription_web import render_subscription_page
from services.partner_subscriptions import PartnerLinkSettings,PartnerSubscriptionLinks
import sys
context={'project_id':'11111111-1111-4111-8111-111111111111','subscription_id':'22222222-2222-4222-8222-222222222222','public_link_generation':0,'slug':'demo-vpn','title':'Север VPN','brand':{},'support_url':'https://t.me/OfflineSupport','bot_username':'OfflinePreviewBot','device_limit':3,'expires_at':datetime(2026,11,20,tzinfo=timezone.utc)}
links=PartnerSubscriptionLinks(PartnerLinkSettings('partners.example.test',b'demo-only-no-real-capabilities-32',short_links=True))
quota={'remaining_bytes':300*1024**3,'total_bytes':350*1024**3,'used_bytes':50*1024**3}
sys.stdout.buffer.write(render_subscription_page(context,links,'ready',quota,checkout=True).body)
`],{cwd:root,env:{...process.env,PYTHON_DOTENV_DISABLED:'1'}}).toString();
const css=await readFile(path.join(root,'static/partner-subscription/assets/subscription.css'));
const js=await readFile(path.join(root,'static/partner-subscription/assets/subscription.js'));
const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
await mkdir(out,{recursive:true});
let checks=0;
const check=(value,message)=>{assert.ok(value,message);checks++;};
const ORDER='39ac4520-74f4-4b0f-9687-6cf93bc129eb';
const KEY='4f6ba37f-02ed-4ef9-a0db-3dc9c764845b';
const newOrder=(key=KEY,extra={})=>({id:ORDER,request_key:key,price_kopecks:24900,status:'created',confirmation_url:'https://payments.example.test/existing-invoice',...extra});

async function fixture({width=390,order=null,available=true,price=24900}={}){
  const context=await browser.newContext({viewport:{width,height:844}});
  const control={order:order?structuredClone(order):null,price,available,calls:[],errors:[],external:[],byKey:new Map(order?[[order.request_key,structuredClone(order)]]:[]),created:order?1:0,lostPost:false,failBeforeSave:false,conflict:null};
  await context.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.origin!=='https://demo-vpn.partners.example.test'){
      control.external.push(request.url());return route.abort();
    }
    if(url.pathname==='/preview')return route.fulfill({contentType:'text/html',body:html});
    if(url.pathname.endsWith('/subscription.css'))return route.fulfill({contentType:'text/css',body:css});
    if(url.pathname.endsWith('/subscription.js'))return route.fulfill({contentType:'application/javascript',body:js});
    const body=request.postData()?JSON.parse(request.postData()):null;
    control.calls.push({path:url.pathname,method:request.method(),body});
    check(!request.headers().authorization,'Public capability checkout never sends central-manager initData');
    check(!url.searchParams.has('token')&&!url.searchParams.has('tg_id'),'No extra secret or actor ID in query');
    if(url.pathname.endsWith('/purchase-quote')){
      check(request.method()==='GET','Page opening reads a quote only');
      return route.fulfill({json:{price_kopecks:control.price,days:30,renewal_available:control.available,pending_purchase:control.order}});
    }
    if(url.pathname.endsWith('/purchases')&&request.method()==='POST'){
      check(Object.keys(body).sort().join(',')==='expected_price_kopecks,request_key','Server checkout accepts only stable key and expected price');
      if(control.conflict){const code=control.conflict;control.conflict=null;if(code.includes('price'))control.price=29900;return route.fulfill({status:409,json:{code,detail:'Нужна повторная проверка'}});}
      if(control.failBeforeSave){control.failBeforeSave=false;return route.abort('failed');}
      let existing=control.byKey.get(body.request_key);
      if(!existing){existing=newOrder(body.request_key,{price_kopecks:body.expected_price_kopecks});control.byKey.set(body.request_key,existing);control.created++;}
      existing.confirmation_url='https://payments.example.test/existing-invoice';control.order=existing;
      if(control.lostPost){control.lostPost=false;return route.abort('failed');}
      return route.fulfill({json:existing});
    }
    if(url.pathname.includes('/purchases/')&&request.method()==='GET'){
      const existing=control.byKey.get(url.pathname.split('/').pop());
      return existing?route.fulfill({json:existing}):route.fulfill({status:404,json:{detail:'Заказ не найден'}});
    }
    control.errors.push(`Unexpected request ${request.method()} ${url.pathname}`);return route.abort();
  });
  const page=await context.newPage();page.on('pageerror',error=>control.errors.push(error.message));
  await page.goto('https://demo-vpn.partners.example.test/preview');
  return {context,page,control};
}
const renewal=page=>page.locator('#renewal-content');
const button=(page,name)=>renewal(page).getByRole('button',{name,exact:true});
const posts=control=>control.calls.filter(call=>call.method==='POST');
async function review(page){await button(page,'Продлить на 30 дней').waitFor();await button(page,'Продлить на 30 дней').click();await button(page,'Подтвердить и открыть счёт').waitFor();}
async function layout(page,name){
  check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`${name}: no horizontal overflow`);
  for(const item of await page.locator('#renewal button,#renewal a.button').all())check((await item.boundingBox()).height>=48,`${name}: tap target >=48px`);
  await page.screenshot({path:path.join(out,`${name}.png`),fullPage:true});
}
async function close(run){assert.deepEqual(run.control.errors,[],'No browser errors or unknown requests');checks++;assert.deepEqual(run.control.external,[],'No external request occurs');checks++;check(await run.page.evaluate(()=>localStorage.length===0),'No capability URLs or payment data persisted');await run.context.close();}

try{
  for(const width of [320,390,430]){
    const run=await fixture({width}),{page,control}=run;
    check(!(await page.locator('#subscription-link').inputValue()).includes('/s/'),'Canonical copied link has no legacy /s prefix');
    check(!(await page.locator('#subscription-link').inputValue()).endsWith('/config'),'Same branded link is used for browser and import');
    check((await page.locator('#renewal').getAttribute('data-purchase-path'))===new URL(await page.locator('#subscription-link').inputValue()).pathname,'Renewal endpoints use the canonical subscription path');
    await button(page,'Продлить на 30 дней').waitFor();check(posts(control).length===0,'Quote load does not create an invoice');check((await renewal(page).textContent()).includes('249'),'Quote uses exact RUB price from server');await review(page);check(posts(control).length===0,'Renewal review does not create an invoice');check((await renewal(page).textContent()).includes('ГБ не обнуляются'),'Review preserves previously paid GB');await layout(page,`renewal-review-${width}`);
    await button(page,'Подтвердить и открыть счёт').click();await renewal(page).getByRole('link',{name:'Перейти к оплате',exact:true}).waitFor();check(posts(control).length===1&&control.created===1,'Explicit confirmation creates exactly one invoice');check(posts(control)[0].body.expected_price_kopecks===24900,'Invoice receives server quoted 249 RUB in integer kopecks');check((await renewal(page).getByRole('link',{name:'Перейти к оплате'}).getAttribute('rel')).includes('noreferrer'),'Payment navigation does not leak subscription capability referrer');await layout(page,`renewal-invoice-${width}`);
    await page.reload();await renewal(page).getByRole('link',{name:'Перейти к оплате',exact:true}).waitFor();check(posts(control).length===1,'Reopen discovers saved purchase with no new POST');control.order.status='paid_pending_activation';await button(page,'Проверить оплату').click();await page.getByText('Оплата сохранена, активация повторяется. Повторно платить не нужно.',{exact:true}).waitFor();check(posts(control).length===1,'Payment status check is read-only');check(await renewal(page).getByRole('link',{name:'Перейти к оплате',exact:true}).count()===0,'Saved paid order never asks user to pay twice');control.order.status='active';await button(page,'Проверить оплату').click();await page.getByText('Подписка продлена. Обновите её в VPN-приложении.',{exact:true}).waitFor();await close(run);
  }

  const lost=await fixture();await review(lost.page);lost.control.lostPost=true;await button(lost.page,'Подтвердить и открыть счёт').click();await button(lost.page,'Проверить этот заказ').waitFor();check(lost.control.created===1,'Lost POST reply preserves one created purchase');await button(lost.page,'Проверить этот заказ').click();await renewal(lost.page).getByRole('link',{name:'Перейти к оплате',exact:true}).waitFor();check(posts(lost.control).length===1,'Saved response recovery reads existing key rather than posts again');await close(lost);

  const unsaved=await fixture();await review(unsaved.page);unsaved.control.failBeforeSave=true;await button(unsaved.page,'Подтвердить и открыть счёт').click();await button(unsaved.page,'Проверить этот заказ').waitFor();await button(unsaved.page,'Проверить этот заказ').click();await renewal(unsaved.page).getByRole('link',{name:'Перейти к оплате',exact:true}).waitFor();check(posts(unsaved.control).length===2,'404 after lost unsaved POST permits one safe retry');check(posts(unsaved.control)[0].body.request_key===posts(unsaved.control)[1].body.request_key,'Lost unsaved POST reuses original immutable key');check(unsaved.control.created===1,'Retry creates only one subscription order');await close(unsaved);

  const draft=await fixture({order:newOrder(KEY,{confirmation_url:''})});await button(draft.page,'Получить ссылку оплаты').waitFor();check(posts(draft.control).length===0,'Unbound draft is not automatically invoiced on reopen');await button(draft.page,'Проверить оплату').click();await button(draft.page,'Получить ссылку оплаты').waitFor();check(posts(draft.control).length===0,'Reading unbound draft status still cannot create invoice');await button(draft.page,'Получить ссылку оплаты').click();await renewal(draft.page).getByRole('link',{name:'Перейти к оплате',exact:true}).waitFor();check(posts(draft.control).length===1&&posts(draft.control)[0].body.request_key===KEY,'Explicit unbound-draft continuation uses saved key');check(posts(draft.control)[0].body.expected_price_kopecks===24900,'Explicit draft continuation uses frozen saved price');await close(draft);

  for(const activation_issue of [null,'manual_review']){
    const run=await fixture({order:newOrder(null,{status:'paid_pending_activation',confirmation_url:null,activation_issue})});
    await button(run.page,'Обновить состояние').waitFor();
    check(posts(run.control).length===0,'Paid bot order without public key never creates checkout on page load');
    check(await renewal(run.page).getByRole('link',{name:'Перейти к оплате',exact:true}).count()===0,'Paid bot order offers no payment link');
    check(await button(run.page,'Восстановить этот заказ').count()===0,'Paid bot order never offers new-order confirmation');
    await button(run.page,'Обновить состояние').click();await button(run.page,'Обновить состояние').waitFor();
    check(posts(run.control).length===0,'Paid bot order refresh reads quote only');
    check(run.control.calls.every(call=>call.path.endsWith('/purchase-quote')),'No lookup or adoption with an absent public request key');
    run.control.order.status='active';await button(run.page,'Обновить состояние').click();
    await pageText(run.page,'Подписка продлена. Обновите её в VPN-приложении.');
    check(posts(run.control).length===0,'Activation recovery does not charge again');
    await close(run);
  }

  for(const code of ['price_changed','pending_price_changed']){
    const run=await fixture();await review(run.page);run.control.conflict=code;await button(run.page,'Подтвердить и открыть счёт').click();await button(run.page,'Обновить стоимость').waitFor();check(run.control.created===0,'Price conflict does not invent success or invoice');await button(run.page,'Обновить стоимость').click();await button(run.page,'Продлить на 30 дней').waitFor();check((await renewal(run.page).textContent()).includes('299'),'Changed price is visibly refreshed');check(posts(run.control).length===1,'Refreshing price does not automatically retry payment');await button(run.page,'Продлить на 30 дней').click();await button(run.page,'Подтвердить и открыть счёт').click();await renewal(run.page).getByRole('link',{name:'Перейти к оплате',exact:true}).waitFor();check(posts(run.control)[1].body.expected_price_kopecks===29900,'Changed price requires explicit confirmation');await close(run);
  }

  const manual=await fixture();await review(manual.page);manual.control.conflict='invoice_manual_review';await button(manual.page,'Подтвердить и открыть счёт').click();await pageText(manual.page,'Сохранённый счёт требует проверки поддержкой. Повторно платить не нужно.');check(await button(manual.page,'Подтвердить и открыть счёт').count()===0,'Manual-review invoice has no repeat payment CTA');check(manual.control.created===0,'Manual review never claims order paid or created');await close(manual);
  const unavailable=await fixture({available:false});await pageText(unavailable.page,'Сейчас продление недоступно: предыдущая покупка ещё обрабатывается или требуется проверка подписки.');check(posts(unavailable.control).length===0,'Unavailable renewal remains read-only');check(await button(unavailable.page,'Продлить на 30 дней').count()===0,'Unavailable renewal cannot solicit payment');await close(unavailable);
  console.log(JSON.stringify({ok:true,assertions:checks,screenshots:out}));
}finally{await browser.close();}

async function pageText(page,text){await renewal(page).getByText(text,{exact:true}).waitFor();}
