// Fully intercepted local workspace UI checks. No real Telegram/payment/VPN requests.
import assert from 'node:assert/strict';
import {readFile, mkdir} from 'node:fs/promises';
import path from 'node:path';

const root=process.cwd();
const out=process.env.PARTNER_PREVIEW_DIR||'/private/tmp/spn-partner-workspace-preview';
const runtime='/Users/khamzat/.cache/codex-runtimes/codex-primary-runtime/dependencies/node';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||`${runtime}/node_modules/playwright/index.mjs`);
const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
await mkdir(out,{recursive:true});
let assertions=0;
const check=(condition,message)=>{assert.ok(condition,message);assertions++;};
const PID='8175b112-bc10-4468-a5d0-a0898d620db3';
const WORKSPACE=`/partners/workspace/${PID}`,API=WORKSPACE+'/api';
const CID='8f9505bb-432a-4fd7-b166-7c72b5e2d7c0';
const BID='afcb1117-2290-4e83-8736-68c4c14a99fb';
const PERMISSIONS={owner:['analytics','customers','orders','referrals','marketing','team'],viewer:['analytics','customers','orders','referrals'],support:['customers','orders','referrals'],marketer:['analytics','referrals','marketing']};
const now='2026-10-20T10:00:00Z';
const PNG='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a8H8AAAAASUVORK5CYII=';
const order={id:'597dc8d9-469b-46a3-a12c-7aeb5e7bccbc',tg_id:100001,price_kopecks:24900,purpose:'subscription',status:'active',created_at:now,paid_at:now};
const report={customers:2,active_subscriptions:1,paid_orders:3,revenue_kopecks:74700,partner_kopecks:52290};

async function fixture({width=390,role='owner',fail=null}={}){
  const context=await browser.newContext({viewport:{width,height:844}});
  const project={id:PID,version:1,slug:'sever-vpn',title:'Север VPN',price_kopecks:24900,status:'active',support_url:'https://t.me/local_support',news_url:'',brand:{accent_color:'#e6c77a'},bot:{id:123456789,username:'local_preview_bot'},role,permissions:PERMISSIONS[role]};
  const control={api:[],external:[],errors:[],project,role,fail,withdrawals:[],withdrawalKeys:new Map(),lostWithdrawal:false,campaigns:[],broadcasts:[],confirmationKeys:new Map(),lostConfirmation:false,queuedCount:0,team:[],resendKeys:new Map()};
  await context.addInitScript(()=>{window.__workspaceXss=0;window.Telegram={WebApp:{initData:'local-verified-manager',ready(){},expand(){}}};});
  await context.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.origin!=='http://127.0.0.1:19873'){
      control.external.push(request.url());return route.fulfill({status:404,body:''});
    }
    if(!url.pathname.startsWith(API+'/')){
      const file=url.pathname===WORKSPACE?'index.html':url.pathname.replace('/partners/','');
      if(!['index.html','assets/partners.css','assets/partners.js','assets/workspace.js'].includes(file))return route.fulfill({status:404,body:''});
      return route.fulfill({contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html',body:await readFile(path.join(root,'static/partners',file))});
    }
    const endpoint=url.pathname.replace(API,''),method=request.method(),body=request.postData()?JSON.parse(request.postData()):{};
    control.api.push({endpoint,method,body});
    check(request.headers().authorization==='tma local-verified-manager','Fixture tenant initData is sent only in Authorization');
    check(!url.searchParams.has('token')&&!url.searchParams.has('initData'),'No token/initData in URL');
    if(control.fail===endpoint)return route.fulfill({status:503,json:{detail:'SQL private-credential-must-not-render'}});
    const ok=json=>route.fulfill({json});
    const conflict=()=>route.fulfill({status:409,json:{detail:'Состояние изменилось. Обновите данные.'}});
    if(endpoint==='/settings')return ok({enabled:true,surface:'workspace',workspace_project_id:PID,stage:'staging',actor_id:42,is_operator:false,base_domain:'vpn.example.test',billing_days:30,features:{management:true,finance:true,branding:true}});
    if(endpoint==='/projects')return ok({items:[project]});
    if(endpoint==='/catalog')return ok({billing_days:30,products:[],addons:{traffic_packages:[],devices:{addon_status:'unavailable',packages:[]}}});
    if(endpoint===`/projects/${PID}`){
      if(method==='PATCH'){
        if(body.expected_version!==project.version)return conflict();
        const next={...body,price_kopecks:Math.round(Number(body.price)*100),version:project.version+1};delete next.price;delete next.expected_version;Object.assign(project,next);
      }
      return ok(project);
    }
    if(endpoint===`/projects/${PID}/logo`){
      if(method!=='GET'){
        if(body.expected_version!==project.version)return conflict();
        if(method==='DELETE')delete project.brand.logo_asset_id;else{check(typeof body.image_base64==='string'&&body.image_base64.length>0,'Logo upload contains local encoded PNG only');project.brand.logo_asset_id='a'.repeat(64);}
        project.version++;
      }
      return ok({project,logo_id:project.brand.logo_asset_id||null,png_base64:project.brand.logo_asset_id?PNG:null});
    }
    if(endpoint===`/projects/${PID}/overview`)return ok({customers:2,active_subscriptions:1,pending_activations:0,periods:Object.fromEntries(['today','days7','days30'].map(period=>[period,{sales:3,revenue_kopecks:74700,partner_kopecks:52290,platform_kopecks:22410}])),timezone:'UTC',sales_enabled:false});
    if(endpoint===`/projects/${PID}/orders`)return ok({items:[order],truncated:false});
    const manage=`/projects/${PID}/management`;
    if(endpoint===manage+'/overview')return ok(report);
    if(endpoint===manage+'/customers')return ok({items:[{id:CID,tg_id:100001,created_at:now,referrer_tg_id:null,subscription_id:CID,expires_at:'2026-11-20T10:00:00Z',device_limit:3,paid_orders:3}],next_cursor:null});
    if(endpoint===manage+'/subscriptions')return ok({items:[{id:CID,tg_id:100001,created_at:now,expires_at:'2026-11-20T10:00:00Z',device_limit:3,traffic_period_limit_bytes:350*1024**3,traffic_state:'ready',status:'active'}],next_cursor:null});
    if(endpoint===manage+'/orders'||endpoint===manage+'/customers/100001/orders')return ok({items:[order],next_cursor:null});
    if(endpoint===manage+'/customers/100001/resend'){
      const result=control.resendKeys.get(body.request_key)||{id:CID,status:'queued',delivery_enabled:false};control.resendKeys.set(body.request_key,result);return ok(result);
    }
    if(endpoint===manage+'/referrals')return ok({invited:5,qualified:3,earned_days:21,applied_days:14,pending_days:7});
    if(endpoint===manage+'/team'){
      if(method==='GET')return ok({owner_tg_id:42,items:control.team,truncated:false});
      let item=control.team.find(row=>row.tg_id===body.tg_id);
      if((item?.version||0)!==body.expected_version)return conflict();
      if(!item){item={tg_id:body.tg_id,role:body.role,version:0};control.team.push(item);}
      Object.assign(item,{role:body.role||item.role,version:item.version+1,active:body.role!==null});return ok(item);
    }
    if(endpoint===manage+'/campaigns'){
      if(method==='GET')return ok({items:control.campaigns,next_cursor:null});
      const item={id:CID,title:body.title,code:'0123456789abcdef0123',start_payload:'m_0123456789abcdef0123',link:'https://t.me/local_preview_bot?start=m_0123456789abcdef0123',status:'active',version:1,registrations:0,first_purchases:0,repeat_purchases:0,revenue_kopecks:0};control.campaigns.push(item);return ok(item);
    }
    if(endpoint===manage+'/broadcasts'){
      if(method==='GET')return ok({items:control.broadcasts,next_cursor:null});
      check(body.text.length<=3000,'Broadcast editor respects server maximum');
      const item={id:BID,text:body.text,status:'draft',version:1,recipients:{delivered:0,uncertain:0},delivery_enabled:false};control.broadcasts.push(item);return ok(item);
    }
    if(endpoint.startsWith(manage+'/broadcasts/')){
      const item=control.broadcasts.find(row=>endpoint.includes(row.id));if(!item)return route.fulfill({status:404,json:{detail:'Нет рассылки'}});
      if(endpoint.endsWith('/confirm')){
        const old=control.confirmationKeys.get(body.request_key);if(old)return ok(old);
        if(item.status!=='review'||item.version!==body.expected_version||body.review_token!=='r'.repeat(43))return conflict();
        item.status='queued';item.version++;item.recipients={queued:2,delivered:0,uncertain:0};control.queuedCount++;control.confirmationKeys.set(body.request_key,structuredClone(item));
        if(control.lostConfirmation){control.lostConfirmation=false;return route.abort('failed');}
        return ok(item);
      }
      if(item.version!==body.expected_version)return conflict();
      if(endpoint.endsWith('/review')){item.status='review';item.version++;return ok({...item,review_token:'r'.repeat(43),eligible_recipients:2,requires_confirmation:true});}
      if(endpoint.endsWith('/cancel')){item.status='canceled';item.version++;item.recipients={canceled:2,delivered:0,uncertain:0};return ok(item);}
    }
    const finance=`/projects/${PID}/finance`;
    if(endpoint===finance+'/summary'){
      const reserved=control.withdrawals.filter(item=>['pending','processing'].includes(item.status)).reduce((sum,item)=>sum+item.amount_kopecks,0);
      const paid=control.withdrawals.filter(item=>item.status==='paid').reduce((sum,item)=>sum+item.amount_kopecks,0);
      return ok({earned_kopecks:500000,reversed_kopecks:0,available_kopecks:500000-reserved-paid,reserved_kopecks:reserved,paid_kopecks:paid,debt_kopecks:0,minimum_withdrawal_kopecks:150000});
    }
    if(endpoint===finance+'/withdrawals'){
      if(method==='GET')return ok({items:control.withdrawals,truncated:false});
      let item=control.withdrawalKeys.get(body.request_key);
      if(!item){item={id:CID,project_id:PID,number:'P-00001',request_key:body.request_key,amount_kopecks:body.amount_kopecks,method:body.method,status:'pending',created_at:now,destination_hint:'СБП · Банк · **0000'};control.withdrawals.push(item);control.withdrawalKeys.set(body.request_key,item);}
      if(control.lostWithdrawal){control.lostWithdrawal=false;return route.abort('failed');}
      return ok(item);
    }
    if(endpoint.startsWith(finance+'/withdrawals/')){
      const item=control.withdrawalKeys.get(endpoint.split('/').pop());return item?ok(item):route.fulfill({status:404,json:{detail:'Заявка не найдена'}});
    }
    if(endpoint==='/operator/withdrawals')return ok({items:control.withdrawals,truncated:false});
    if(endpoint.startsWith(`/operator/projects/${PID}/withdrawals/`)){
      const item=control.withdrawals.find(row=>endpoint.includes(row.id));if(!item)return route.fulfill({status:404,json:{detail:'Заявка не найдена'}});
      if(endpoint.endsWith('/claim'))item.status='processing';
      if(endpoint.endsWith('/paid')){check(body.confirmed_transferred===true,'Operator explicitly confirms an already completed manual transfer');check(body.actual_amount_kopecks===item.amount_kopecks,'Manual payout amount remains exact');item.status='paid';}
      if(endpoint.endsWith('/reject')){if(item.status==='processing')check(body.confirmed_no_transfer===true,'Processing rejection requires confirmed absence of transfer');item.status='rejected';}
      return ok({...item,requisites:{bank:'Тестовый банк',phone:'+79990000000'}});
    }
    control.errors.push(`Unexpected API: ${method} ${endpoint}`);return route.fulfill({status:404,json:{detail:'Недоступно'}});
  });
  const page=await context.newPage();page.on('pageerror',error=>control.errors.push(String(error)));
  await page.goto('http://127.0.0.1:19873'+WORKSPACE);await page.getByRole('heading',{name:role==='support'?'Клиенты':'Обзор',exact:true}).waitFor();
  return {context,page,control};
}
const nav=(page,name)=>page.getByRole('navigation',{name:'Разделы'}).getByRole('button',{name,exact:false});
async function settingsPage(page,name){await nav(page,'Настройки').click();await page.getByRole('button',{name,exact:true}).click();}
async function subscriptionsPage(page){await nav(page,'Клиенты').click();await page.getByRole('button',{name:'Подписки',exact:true}).click();}
async function layout(page,name){
  const state=await page.evaluate(()=>{const visible=node=>node.getClientRects().length&&getComputedStyle(node).visibility!=='hidden';return{overflow:document.documentElement.scrollWidth>innerWidth+1,small:[...document.querySelectorAll('button')].filter(visible).filter(node=>node.getBoundingClientRect().height<47).map(node=>node.textContent),tiny:[...document.querySelectorAll('p,span,small,label,button')].filter(visible).filter(node=>parseFloat(getComputedStyle(node).fontSize)<12).map(node=>node.textContent)};});
  check(!state.overflow,`${name}: no horizontal overflow`);assert.deepEqual(state.small,[],`${name}: tap targets >=48px`);assertions++;assert.deepEqual(state.tiny,[],`${name}: text >=12px`);assertions++;
  await page.screenshot({path:path.join(out,`${name}.png`),fullPage:true});
}
async function close(run){assert.deepEqual(run.control.errors,[],'No page errors or unknown endpoints');assertions++;assert.deepEqual(run.control.external,[],'No external request leaves browser fixture');assertions++;await run.context.close();}
async function financeReview(page){
  await nav(page,'Финансы').click();await page.getByRole('button',{name:'Создать заявку на вывод',exact:true}).waitFor();
  await page.getByRole('button',{name:'Создать заявку на вывод',exact:true}).click();
  await page.getByLabel('Сумма, ₽',{exact:true}).fill('1500');await page.getByLabel('Банк',{exact:true}).fill('Тестовый банк');await page.getByLabel('Телефон',{exact:true}).fill('+79990000000');
  await page.getByRole('button',{name:'Проверить заявку',exact:true}).click();await page.getByRole('button',{name:'Подать заявку',exact:true}).waitFor();
}

try{
  for(const width of [320,390,430]){
    const run=await fixture({width}),{page,control}=run;
    assert.deepEqual(await page.locator('.navigation button:visible').allTextContents(),['Обзор','Клиенты','Финансы','Настройки']);assertions++;
    check(await page.getByRole('button',{name:'Выбрать другой проект',exact:true}).count()===0,'Tenant cabinet has no project selector or creation');
    await page.getByRole('button',{name:'История продаж',exact:true}).click();await page.getByRole('heading',{name:'Заказ 597dc8d9',exact:true}).waitFor();await page.getByRole('button',{name:'Назад к обзору',exact:true}).click();
    check(await nav(page,'Обзор').getAttribute('aria-current')==='page','Nested sales return to overview');
    await layout(page,`workspace-overview-${width}`);
    await nav(page,'Настройки').click();await layout(page,`workspace-settings-${width}`);
    check(await page.getByRole('heading',{name:'Мой сервис',exact:true}).count()===1,'Workspace fixed label uses neutral service wording');
    await page.getByRole('button',{name:'Название, оформление и цена',exact:true}).click();await page.getByText('Логотип пока не добавлен.',{exact:true}).waitFor();
    check(!/300\s*ГБ|антиглуш|обход|Все серверы вместе/i.test(await page.locator('#content').innerText()),'Brand preview has no fixed infrastructure marketing copy');
    check(await page.getByLabel('Приветствие в боте',{exact:true}).isHidden(),'Advanced copy fields start collapsed');await page.getByText('Тексты и страница подписки',{exact:true}).click();
    const typed='Мой изменённый VPN';await page.getByLabel('Название проекта',{exact:true}).fill(typed);await page.getByLabel('Цена за 30 дней, ₽',{exact:true}).fill('299');await page.getByLabel('Приветствие в боте',{exact:true}).fill('Добро пожаловать в мой VPN');await page.getByLabel('Заголовок страницы подписки',{exact:true}).fill('Моя подписка');await page.getByLabel('Описание на странице подписки',{exact:true}).fill('Все серверы в одной подписке.');
    await page.getByLabel('Файл логотипа',{exact:true}).setInputFiles({name:'logo.png',mimeType:'image/png',buffer:Buffer.from(PNG,'base64')});await page.getByRole('button',{name:'Загрузить логотип',exact:true}).click();await page.getByText('Логотип сохранён.',{exact:true}).waitFor();
    check(control.project.version===2,'Logo upload advances optimistic project version');check(await page.getByLabel('Название проекта',{exact:true}).inputValue()===typed,'Logo upload preserves unsaved title');check(await page.getByLabel('Приветствие в боте',{exact:true}).inputValue()==='Добро пожаловать в мой VPN','Logo upload preserves unsaved welcome copy');check(await page.locator('.brand-logo-preview').count()===1,'Sanitized logo preview is visible');
    await page.getByRole('button',{name:'Удалить логотип',exact:true}).click();await page.getByText('Логотип пока не добавлен.',{exact:true}).waitFor();check(control.project.version===3&&!control.project.brand.logo_asset_id,'Logo removal advances version without deleting settings');check(await page.getByLabel('Название проекта',{exact:true}).inputValue()===typed,'Logo removal preserves unsaved title');
    const beforeUploads=control.api.filter(row=>row.endpoint.endsWith('/logo')&&row.method==='POST').length;await page.getByLabel('Файл логотипа',{exact:true}).setInputFiles({name:'oversized.png',mimeType:'image/png',buffer:Buffer.alloc(512*1024+1)});await page.getByRole('button',{name:'Загрузить логотип',exact:true}).click();await page.getByText('Выберите PNG или JPEG до 512 КБ.',{exact:true}).waitFor();check(control.api.filter(row=>row.endpoint.endsWith('/logo')&&row.method==='POST').length===beforeUploads,'Oversized image is rejected before network upload');
    await page.getByLabel('Файл логотипа',{exact:true}).setInputFiles({name:'logo.png',mimeType:'image/png',buffer:Buffer.from(PNG,'base64')});await page.getByRole('button',{name:'Загрузить логотип',exact:true}).click();await page.getByText('Логотип сохранён.',{exact:true}).waitFor();await page.getByRole('button',{name:'Сохранить изменения',exact:true}).click();await page.getByRole('heading',{name:'Бренд и тариф',exact:true}).waitFor();await page.waitForFunction(()=>document.querySelector('#brand-title')?.value==='Мой изменённый VPN'&&!document.querySelector('#brand-title')?.disabled);
    check(control.project.version===5,'Settings save uses latest post-logo version');check(control.project.title===typed&&control.project.brand.welcome_text==='Добро пожаловать в мой VPN'&&control.project.brand.subscription_title==='Моя подписка'&&control.project.brand.subscription_description==='Все серверы в одной подписке.','New public brand text fields persist in exact save payload');check(control.project.price_kopecks===29900,'Price remains exact 299 RUB, not logo endpoint side effect');await layout(page,`workspace-brand-${width}`);
    await nav(page,'Клиенты').click();await page.getByRole('heading',{name:'Клиент 100001',exact:true}).waitFor();await layout(page,`workspace-customers-${width}`);
    await page.getByRole('button',{name:'История покупок',exact:true}).click();await page.getByRole('heading',{name:'Заказ 597dc8d9',exact:true}).waitFor();check((await page.locator('#content').textContent()).includes('249'),'Order price rendered in rubles, not kopecks');
    await subscriptionsPage(page);await page.getByRole('heading',{name:'Клиент 100001',exact:true}).waitFor();check((await page.locator('#content').textContent()).includes('350 ГБ'),'Logical period quota is 350 GB, not cumulative 700 GB');check(!(await page.locator('#content').textContent()).includes('Осталось'),'Quota is not misrepresented as remaining traffic');await layout(page,`workspace-subscriptions-${width}`);
    await settingsPage(page,'Бонусы за приглашения');await page.getByRole('heading',{name:'Правила бонусов',exact:true}).waitFor();check((await page.locator('#content').textContent()).includes('+7 дней за первую оплаченную подписку друга.'),'Referral bonus stays seven days after confirmed first purchase');check((await page.locator('#content').textContent()).includes('Бонусы начисляются автоматически.'),'Bonus rules explain the automatic award');
    await financeReview(page);check(control.api.filter(row=>row.endpoint.endsWith('/finance/withdrawals')&&row.method==='POST').length===0,'Review alone never reserves money');await layout(page,`workspace-withdrawal-review-${width}`);
    const before=await page.evaluate(()=>Object.entries(localStorage));check(!JSON.stringify(before).includes('79990000000')&&!JSON.stringify(before).includes('Тестовый'),'No requisites are persisted during review');
    await page.getByRole('button',{name:'Подать заявку',exact:true}).click();await page.getByRole('heading',{name:'Заявка сохранена',exact:true}).waitFor();check(control.withdrawals.length===1&&control.withdrawals[0].amount_kopecks===150000,'One confirmed withdrawal reserves exact 1500 RUB');
    check(!(await page.evaluate(()=>JSON.stringify(Object.entries(localStorage)))).includes('79990000000'),'No requisites persisted after submission');
    await settingsPage(page,'Сотрудники и права');await page.getByLabel('Telegram ID сотрудника',{exact:true}).fill('555');await page.getByLabel('Права',{exact:true}).selectOption('support');await page.getByRole('button',{name:'Добавить сотрудника',exact:true}).click();await page.getByRole('heading',{name:'Сотрудник 555',exact:true}).waitFor();check(control.team[0].role==='support'&&control.team[0].version===1,'Team mutation keeps exact least-privilege role/version');await layout(page,`workspace-team-${width}`);await page.getByRole('button',{name:'Закрыть доступ',exact:true}).click();await page.getByText('Доступ закрыт',{exact:false}).waitFor();check(control.team[0].active===false,'Team removal keeps owner intact and closes staff membership');
    await settingsPage(page,'Рекламные ссылки и рассылки');await page.getByLabel('Название рекламной ссылки',{exact:true}).fill('Личная реклама');await page.getByRole('button',{name:'Создать ссылку',exact:true}).click();await page.getByLabel('Личная рекламная ссылка',{exact:true}).waitFor();check((await page.getByLabel('Личная рекламная ссылка',{exact:true}).inputValue()).includes('?start=m_'),'Campaign attribution has explicit immutable first-start payload');
    const message='Новости <img src=x onerror="window.__workspaceXss=1">';await page.getByLabel('Текст сообщения',{exact:true}).fill(message);check(await page.getByLabel('Текст сообщения',{exact:true}).getAttribute('maxlength')==='3000','Browser and backend broadcast length limits match');await page.getByRole('button',{name:'Сохранить черновик',exact:true}).click();await page.getByRole('heading',{name:'Рассылка · Черновик',exact:true}).waitFor();check(control.queuedCount===0,'Draft creation never queues delivery');check(await page.evaluate(()=>window.__workspaceXss)===0,'Marketing message is plain text, never executed');
    await page.getByRole('button',{name:'Проверить и подтвердить',exact:true}).click();await page.getByRole('heading',{name:'Проверка рассылки',exact:true}).waitFor();check(control.queuedCount===0,'Audience review does not queue delivery');check((await page.locator('.workspace-message').textContent())===message,'Review shows exact server-snapshotted text');await layout(page,`workspace-broadcast-review-${width}`);
    await page.getByRole('button',{name:'Назад без отправки',exact:true}).click();await page.getByRole('heading',{name:'Рассылка · Проверка перед отправкой',exact:true}).waitFor();await page.getByRole('button',{name:'Проверить и подтвердить',exact:true}).click();await page.getByRole('heading',{name:'Проверка рассылки',exact:true}).waitFor();await page.getByRole('button',{name:'Подтвердить рассылку',exact:true}).click();check(control.queuedCount===0,'Unchecked confirmation cannot queue broadcast');await page.getByLabel('Подтверждаю отправку именно этого сообщения этим получателям',{exact:true}).check();await page.getByRole('button',{name:'Подтвердить рассылку',exact:true}).click();await page.getByRole('heading',{name:'Рассылка · В очереди',exact:true}).waitFor();check(control.queuedCount===1,'Explicit confirmation queues exactly one frozen broadcast');check((await page.locator('#content').textContent()).includes('Доставлено: 0'),'Delivery statistics come from recipient status map');
    check(!control.api.some(row=>/sendMessage|setWebhook|activate|grant|charge/.test(row.endpoint)),'UI never invokes direct sending, free grants, or transfers');await close(run);
  }

  for(const role of ['viewer','support','marketer']){
    const run=await fixture({role}),{page,control}=run;
    for(const name of ['Бренд и тариф','Telegram-бот','Запуск','Финансы','Команда','Оператор'])check(await nav(page,name).isHidden(),`${role}: owner/operator navigation is hidden`);
    await nav(page,'Настройки').click();check(await page.getByRole('button',{name:'Рекламные ссылки и рассылки',exact:true}).isVisible()===(role==='marketer'),`${role}: marketing permission matches role`);
    check(await nav(page,'Клиенты').isVisible()===(role!=='marketer'),`${role}: customer permission matches role`);
    await nav(page,role==='support'?'Клиенты':'Обзор').click();
    if(role==='support')await page.getByRole('heading',{name:'Клиент 100001',exact:true}).waitFor();else await page.getByRole('heading',{name:'Обзор',exact:true}).waitFor();
    check(!control.api.some(row=>row.endpoint==='/catalog'),'Staff project opening does not call owner-only catalog');check(await page.locator('#brand-price').count()===0,'Staff cannot see owner editable pricing form');
    if(role==='viewer'){await nav(page,'Клиенты').click();await page.getByRole('heading',{name:'Клиент 100001',exact:true}).waitFor();check(await page.getByRole('button',{name:'Отправить подписку клиенту',exact:true}).count()===0,'Viewer has no support resend action');}
    await layout(page,`workspace-role-${role}`);await close(run);
  }

  const lost=await fixture();await financeReview(lost.page);lost.control.lostWithdrawal=true;await lost.page.getByRole('button',{name:'Подать заявку',exact:true}).click();await lost.page.locator('.form-error').waitFor();check(lost.control.withdrawals.length===1,'Lost withdrawal response still created exactly one request');const saved=await lost.page.evaluate(()=>Object.entries(localStorage));check(saved.length===1&&/^[a-f0-9-]{36}$/.test(saved[0][1]),'Only pending operation UUID is persisted');await lost.page.reload();await lost.page.getByRole('heading',{name:'Обзор',exact:true}).waitFor();await nav(lost.page,'Финансы').click();await lost.page.getByRole('heading',{name:/P-00001/}).first().waitFor();check(lost.control.withdrawals.length===1,'Reopen restores request before offering duplicate submission');check(lost.control.api.filter(row=>row.endpoint.endsWith('/finance/withdrawals')&&row.method==='POST').length===1,'Recovery uses read state, not new money operation');await close(lost);

  const error=await fixture({fail:`/projects/${PID}/management/subscriptions`});await subscriptionsPage(error.page);await error.page.getByRole('heading',{name:'Данные не загрузились',exact:true}).waitFor();check(!(await error.page.locator('#content').textContent()).includes('private-credential'),'Backend technical failures do not render raw secrets');check(!(await error.page.locator('#content').textContent()).includes('Подписок пока нет'),'Loading error is not replaced with a fake empty list');await layout(error.page,'workspace-error');await close(error);
  console.log(JSON.stringify({ok:true,assertions,screenshots:out}));
}finally{await browser.close();}
