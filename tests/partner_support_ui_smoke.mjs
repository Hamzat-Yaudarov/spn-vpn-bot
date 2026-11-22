// Offline central-support UI fixture. No real Telegram, database or payments.
import assert from 'node:assert/strict';
import {readFile, mkdir} from 'node:fs/promises';
import path from 'node:path';

const runtime='/Users/khamzat/.cache/codex-runtimes/codex-primary-runtime/dependencies/node';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||`${runtime}/node_modules/playwright/index.mjs`);
const browser=await chromium.launch({executablePath:process.env.CHROME_EXECUTABLE||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
const root=process.cwd(), out=process.env.PARTNER_PREVIEW_DIR||'/private/tmp/spn-partner-support-preview';
const PID='8175b112-bc10-4468-a5d0-a0898d620db3';
await mkdir(out,{recursive:true});
let assertions=0;
const check=(value,message)=>{assert.ok(value,message);assertions++;};
async function fixture(width,{authorized=true}={}){
  const context=await browser.newContext({viewport:{width,height:844}});
  const control={calls:[],errors:[],patches:[],project:{id:PID,title:'Север VPN <script>alert(1)</script>',slug:'sever-vpn',status:'active',version:1,owner_tg_id:123,price_kopecks:19900,support_url:'https://t.me/wayspn_support',news_url:'',bot:{username:'SeverVpnBot'},brand:{welcome_text:'Добро пожаловать!',accent_color:'#e6c77a',subscription_title:'Север VPN',subscription_description:'Подключение VPN'}}};
  await context.addInitScript(({authorized})=>{window.Telegram={WebApp:{initData:authorized?'fixture-central-manager':'',ready(){},expand(){}}};},{authorized});
  await context.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.origin!=='http://127.0.0.1:19874')return route.fulfill({status:200,body:''});
    if(url.pathname.startsWith('/partners/support/api/')){
      check(request.headers().authorization==='tma fixture-central-manager','Central proof is only in authorization');
      check(!url.search.includes('initData')&&!url.search.includes('token'),'No proof in URLs');
      const endpoint=url.pathname.replace('/partners/support/api','');control.calls.push(endpoint);
      if(endpoint==='/settings')return route.fulfill({json:{actor_id:6910097562,context:'platform_support'}});
      if(endpoint==='/projects')return route.fulfill({json:{items:[control.project],truncated:false,cursor:0}});
      if(endpoint===`/projects/${PID}`){
        if(request.method()==='PATCH'){
          const body=request.postDataJSON();control.patches.push(body);
          check(body.expected_version===control.project.version,'Optimistic version preserved');
          check(!('owner_tg_id' in body)&&!('actor_id' in body),'UI cannot assert owner or actor');
          Object.assign(control.project,{...body,price_kopecks:Math.round(Number(body.price)*100),version:control.project.version+1});
        }
        return route.fulfill({json:control.project});
      }
      control.errors.push(endpoint);return route.fulfill({status:404,json:{detail:'Нет маршрута'}});
    }
    const file=url.pathname==='/partners/support'?'support.html':url.pathname.replace('/partners/','');
    if(!['support.html','assets/partners.css','assets/support.js'].includes(file))return route.fulfill({status:404,body:''});
    return route.fulfill({contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html',body:await readFile(path.join(root,'static/partners',file))});
  });
  const page=await context.newPage();page.on('pageerror',error=>control.errors.push(String(error)));
  await page.goto('http://127.0.0.1:19874/partners/support');
  return {context,page,control};
}
async function layout(page,label){
  check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),label+': no overflow');
  check(await page.locator('button').evaluateAll(nodes=>nodes.filter(node=>node.getClientRects().length).every(node=>node.getBoundingClientRect().height>=48)),label+': 48px buttons');
  await page.screenshot({path:path.join(out,label+'.png'),fullPage:true});
}
try{
  for(const width of [320,390,430]){
    const run=await fixture(width),{page,control}=run;
    await page.getByRole('heading',{name:'Проекты партнёров',exact:true}).waitFor();
    check(await page.locator('script').count()===2,'Project title does not inject script');
    await layout(page,`support-list-${width}`);
    await page.getByRole('button',{name:'Открыть настройки'}).click();
    await page.getByLabel('Название сервиса',{exact:true}).waitFor();
    await layout(page,`support-settings-${width}`);
    await page.getByLabel('Название сервиса',{exact:true}).fill('Обновлённый сервис');
    await page.getByLabel('Подписка за 30 дней, ₽ (не ниже 199)',{exact:true}).fill('219');
    await page.getByRole('button',{name:'Сохранить настройки',exact:true}).click();
    await page.getByRole('status').filter({hasText:'Настройки сохранены'}).waitFor();
    check(control.patches.length===1,'One tap produces one settings mutation');
    check(control.project.title==='Обновлённый сервис','Settings saved');
    check(await page.evaluate(()=>localStorage.length+sessionStorage.length)===0,'No credential or session data persisted');
    await page.getByRole('button',{name:'← Все проекты',exact:true}).click();
    await page.getByRole('heading',{name:'Проекты партнёров',exact:true}).waitFor();
    assert.deepEqual(control.errors,[]);await run.context.close();
  }
  const denied=await fixture(390,{authorized:false});
  await denied.page.getByRole('heading',{name:'Не удалось открыть',exact:true}).waitFor();
  check(denied.control.calls.length===0,'No initData means no API request');
  assert.deepEqual(denied.control.errors,[]);await denied.context.close();
  console.log(JSON.stringify({status:'ok',assertions,previews:out}));
}finally{await browser.close();}
