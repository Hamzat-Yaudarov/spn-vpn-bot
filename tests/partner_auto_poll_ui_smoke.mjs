// Real entry/renewal JavaScript, fake browser clock, all requests intercepted.
// No real payment, Telegram, subscription capability, database, or DNS calls.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {readFile} from 'node:fs/promises';
import {setTimeout as delay} from 'node:timers/promises';
import path from 'node:path';

const root=process.cwd(),runtime='/Users/khamzat/.cache/codex-runtimes/codex-primary-runtime/dependencies/node';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||`${runtime}/node_modules/playwright/index.mjs`);
const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
const PID='8175b112-bc10-4468-a5d0-a0898d620db3',OID='f378dc14-c7df-4a34-8be6-8201a163f9a3',KEY='4f6ba37f-02ed-4ef9-a0db-3dc9c764845b';
const renewalHtml=execFileSync(process.env.PYTHON||'python3',['-c',`
from datetime import datetime,timezone
from partner_subscription_web import render_subscription_page
from services.partner_subscriptions import PartnerLinkSettings,PartnerSubscriptionLinks
import sys
context={'project_id':'11111111-1111-4111-8111-111111111111','subscription_id':'22222222-2222-4222-8222-222222222222','public_link_generation':0,'slug':'demo-vpn','title':'Север VPN','brand':{},'support_url':'https://t.me/OfflineSupport','bot_username':'OfflinePreviewBot','device_limit':3,'expires_at':datetime(2026,11,20,tzinfo=timezone.utc)}
links=PartnerSubscriptionLinks(PartnerLinkSettings('partners.example.test',b'demo-only-no-real-capabilities-32',short_links=True))
quota={'remaining_bytes':300*1024**3,'total_bytes':350*1024**3,'used_bytes':50*1024**3}
sys.stdout.buffer.write(render_subscription_page(context,links,'ready',quota,checkout=True).body)
`],{cwd:root,env:{...process.env,PYTHON_DOTENV_DISABLED:'1'}}).toString();
let checks=0;
const check=(value,message)=>{assert.ok(value,message);checks++;};
async function until(fn,message){for(let i=0;i<150;i++){if(fn())return;await delay(10);}throw Error(message);}
function project(active=false){return{id:PID,owner_tg_id:42,version:1,slug:'local-vpn',title:'Мой VPN',price_kopecks:19900,status:active?'active':'draft',workspace_available:active,bot:{id:123456789,username:'local_preview_bot'}};}
function order(kind,key=KEY){return{id:OID,request_key:key,price_kopecks:kind==='entry'?999000:24900,status:'created',created_at:'2026-10-22T10:00:00Z',paid_at:null,confirmation_url:'https://payments.example.test/existing'}};

async function fixture(kind,{key=KEY,blocked=false}={}){
  const context=await browser.newContext({viewport:{width:390,height:844}});
  const state={kind,order:order(kind,key),calls:[],polls:0,active:0,maxActive:0,errors:[],external:[],hold:null,status:null,blocked};
  if(blocked){if(kind==='entry')state.order.status='paid_pending_launch';else{state.order.status='paid_pending_activation';state.order.activation_issue='manual_review';}}
  await context.addInitScript(()=>{
    window.Telegram={WebApp:{initData:'offline-creator-initdata',ready(){},expand(){}}};
    window.__testHidden=false;
    Object.defineProperty(document,'hidden',{configurable:true,get:()=>window.__testHidden});
  });
  const origin=kind==='entry'?'http://127.0.0.1:19875':'https://demo-vpn.partners.example.test';
  const entryBase=`/partners/api/projects/${PID}/entry`;
  const entryEnvelope=()=>({entry_fee_kopecks:999000,payments_available:true,launch_available:true,order:structuredClone(state.order),...(blocked?{launch_state:'needs_webhook_replace'}:{})});
  await context.route('**/*',async route=>{
    const req=route.request(),url=new URL(req.url()),method=req.method();
    if(url.origin!==origin){state.external.push(req.url());return route.abort();}
    if(kind==='entry'&&!url.pathname.startsWith('/partners/api/')){
      const file=url.pathname==='/partners'?'index.html':url.pathname.replace('/partners/','');
      if(!['index.html','assets/partners.css','assets/partners.js','assets/workspace.js'].includes(file))return route.fulfill({status:404,body:''});
      return route.fulfill({contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html',body:await readFile(path.join(root,'static/partners',file))});
    }
    if(kind==='renewal'){
      if(url.pathname==='/preview')return route.fulfill({contentType:'text/html',body:renewalHtml});
      if(url.pathname.endsWith('/subscription.css'))return route.fulfill({contentType:'text/css',body:await readFile(path.join(root,'static/partner-subscription/assets/subscription.css'))});
      if(url.pathname.endsWith('/subscription.js'))return route.fulfill({contentType:'application/javascript',body:await readFile(path.join(root,'static/partner-subscription/assets/subscription.js'))});
    }
    state.calls.push({path:url.pathname,method,body:req.postData()?JSON.parse(req.postData()):null});
    if(kind==='entry'){
      if(url.pathname==='/partners/api/settings')return route.fulfill({json:{enabled:true,surface:'enrollment',actor_id:42,entry_fee_kopecks:999000,pay_first_enabled:true,is_operator:false,features:{management:false,finance:false,branding:false}}});
      if(url.pathname==='/partners/api/projects')return route.fulfill({json:{items:[project()]}});
      if(url.pathname===`/partners/api/projects/${PID}`)return route.fulfill({json:project(true)});
      if(url.pathname===entryBase&&method==='GET')return route.fulfill({json:entryEnvelope()});
      check(url.pathname===`${entryBase}/${OID}/check`&&method==='POST','Entry auto-check targets only the existing saved order');
      check(req.postData()==='{}','Entry check never submits price, bot token, or a new request key');
    }else{
      check(method==='GET','Renewal auto-poll is strictly GET-only');
      check(!req.headers().authorization,'Public renewal sends no manager authorization');
      if(url.pathname.endsWith('/purchase-quote')&&state.calls.length===1)return route.fulfill({json:{price_kopecks:24900,days:30,renewal_available:false,pending_purchase:state.order}});
      check(key?url.pathname.endsWith('/purchases/'+key):url.pathname.endsWith('/purchase-quote'),'Renewal reads saved key or quote without adopting/creating one');
    }
    state.polls++;state.active++;state.maxActive=Math.max(state.maxActive,state.active);
    try{
      if(state.hold)await state.hold;
      if(state.status)return await route.fulfill({status:state.status,json:{detail:'private-error-must-not-render'}});
      return await route.fulfill({json:kind==='entry'?entryEnvelope():key?structuredClone(state.order):{price_kopecks:24900,days:30,renewal_available:false,pending_purchase:state.order}});
    }finally{state.active--;}
  });
  const page=await context.newPage();page.on('pageerror',e=>state.errors.push(e.message));
  await page.clock.install({time:new Date('2026-10-22T12:00:00Z')});
  await page.clock.pauseAt(new Date('2026-10-22T12:00:01Z'));
  await page.goto(origin+(kind==='entry'?'/partners':'/preview'));
  if(blocked){
    await page.getByText(kind==='entry'?'Бот уже подключён к другому обработчику.':'Оплата сохранена. Покупка требует проверки поддержкой. Повторно платить не нужно.',{exact:true}).waitFor();
  }else await page.getByRole('link',{name:kind==='entry'?'Продолжить оплату':'Перейти к оплате',exact:true}).waitFor();
  return{context,page,state};
}
async function tick(run,ms=5001){await run.page.clock.fastForward(ms);await delay(30);}
async function hidden(run,value){await run.page.evaluate(value=>{window.__testHidden=value;document.dispatchEvent(new Event('visibilitychange'));},value);}
async function close(run){check(run.state.errors.length===0,'No browser script errors: '+run.state.errors.join(','));check(run.state.external.length===0,'No external requests');check(run.state.kind==='entry'?run.state.calls.every(x=>x.method==='GET'||x.path.endsWith(`/${OID}/check`)):run.state.calls.every(x=>x.method==='GET'),'No automatic invoice/order creation');await run.context.close();}

try{
  for(const kind of ['entry','renewal']){
    const run=await fixture(kind);
    run.state.order.status=kind==='entry'?'paid_pending_launch':'paid_pending_activation';run.state.order.paid_at='2026-10-22T12:00:01Z';
    await tick(run);await until(()=>run.state.polls===1,'First auto check did not run');
    await run.page.getByText(kind==='entry'?'Подключаем сервис…':'Оплата сохранена, активация повторяется. Повторно платить не нужно.',{exact:true}).waitFor();
    run.state.order.status=kind==='entry'?'launched':'active';
    await tick(run);await until(()=>run.state.polls===2,'Second auto check did not run');
    if(kind==='entry')await run.page.getByRole('heading',{name:'Ваш сервис создан',exact:true}).waitFor();
    else await run.page.getByText('Подписка продлена. Обновите её в VPN-приложении.',{exact:true}).waitFor();
    const done=run.state.polls;await tick(run,120000);check(run.state.polls===done,kind+': completed order stops polling');
    check(run.state.maxActive===1,kind+': never overlapping requests');await close(run);

    const paused=await fixture(kind);await hidden(paused,true);await tick(paused,60000);check(paused.state.polls===0,kind+': hidden page does not poll');await hidden(paused,false);await tick(paused,1);await until(()=>paused.state.polls===1,'Visible page did not resume');await close(paused);

    const slow=await fixture(kind);let release;slow.state.hold=new Promise(resolve=>release=resolve);
    await tick(slow);await until(()=>slow.state.polls===1,'Held check did not start');
    await slow.page.evaluate(()=>window.dispatchEvent(new Event('focus')));await tick(slow,6000);
    check(slow.state.polls===1&&slow.state.maxActive===1,kind+': focus/timers do not overlap a slow response');release();await until(()=>slow.state.active===0,'Held check did not finish');await close(slow);

    for(const status of [401,403,404]){
      const failed=await fixture(kind);failed.state.status=status;await tick(failed);await until(()=>failed.state.polls===1,'Rejected check did not start');await tick(failed,120000);await failed.page.evaluate(()=>window.dispatchEvent(new Event('focus')));await tick(failed,1);check(failed.state.polls===1,kind+': '+status+' stops automatic retry and focus resume');await close(failed);
    }
  }
  const noKey=await fixture('renewal',{key:null});noKey.state.order.status='active';await tick(noKey);await noKey.page.getByText('Подписка продлена. Обновите её в VPN-приложении.',{exact:true}).waitFor();check(noKey.state.calls.every(x=>x.path.endsWith('/purchase-quote')),'Saved bot renewal without public request key only refreshes quote');await close(noKey);
  for(const kind of ['entry','renewal']){const run=await fixture(kind,{blocked:true});await tick(run,120000);check(run.state.polls===0,kind+': manual-consent/review state does not auto-retry');await close(run);}
  console.log(JSON.stringify({ok:true,assertions:checks,real_requests:0,fake_clock:true}));
}finally{await browser.close();}
