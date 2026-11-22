"use strict";
// No analytics, persistent storage, automatic import or external SDK.
const copyButton = document.getElementById("copy-link");
if (copyButton) copyButton.addEventListener("click", async () => {
  const input = document.getElementById("subscription-link");
  const status = document.getElementById("copy-status");
  if (!input || !status) return;
  try {
    await navigator.clipboard.writeText(input.value);
    status.textContent = "Ссылка скопирована. Добавьте её в VPN-приложение.";
  } catch (_) {
    input.closest("details").open = true;
    input.focus();
    input.select();
    status.textContent = "Выделили ссылку. Скопируйте её вручную.";
  }
});

// Only an explicitly enabled isolated gateway renders this element. Opening a
// page can read a quote/reconcile a saved order, never create an invoice.
(() => {
  const root=document.getElementById('renewal'),content=document.getElementById('renewal-content');
  if(!root||!content)return;
  const path=root.dataset.purchasePath;
  if(!/^\/(?:s\/)?[A-Za-z0-9_.-]{20,2048}$/.test(path||''))return;
  let busy=false,pending=null,requestKey=null,expected=null,autoTimer,autoBusy=false,autoFailures=0,autoStopped=false;
  const node=(tag,text)=>{const item=document.createElement(tag);if(text)item.textContent=text;return item;};
  const money=value=>new Intl.NumberFormat('ru-RU',{style:'currency',currency:'RUB'}).format(value/100);
  const validPrice=value=>Number.isSafeInteger(value)&&value>=19900&&value<=99999999;
  function button(text,action,primary=false){const b=node('button',text);b.type='button';b.className='button'+(primary?' primary':'');b.addEventListener('click',()=>{if(!busy)void action();});return b;}
  function status(text){const p=node('p',text);p.role='status';p.setAttribute('aria-live','polite');return p;}
  async function api(suffix,body){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
    try{
      const response=await fetch(path+suffix,{method:body?'POST':'GET',credentials:'omit',cache:'no-store',redirect:'error',signal:controller.signal,headers:body?{'Content-Type':'application/json'}:{},...(body?{body:JSON.stringify(body)}:{})});
      if(!response.ok){let data;try{data=await response.json();}catch{}throw Object.assign(Error('request_failed'),{status:response.status,code:data?.code});}
      return await response.json();
    }finally{clearTimeout(timer);}
  }
  async function run(action){
    if(busy)return;busy=true;content.setAttribute('aria-busy','true');
    content.querySelectorAll('button').forEach(b=>b.disabled=true);
    try{await action();}catch(error){
      if(['price_changed','pending_price_changed'].includes(error.code)){
        content.replaceChildren(status('Стоимость изменилась. Загрузите и подтвердите актуальную цену перед оплатой.'),button('Обновить стоимость',loadQuote));
      }else if(error.code==='invoice_manual_review'){
        content.replaceChildren(status('Сохранённый счёт требует проверки поддержкой. Повторно платить не нужно.'),button('Обновить состояние',loadQuote));
      }else{
        content.replaceChildren(status('Ответ не получен. Не оплачивайте новую покупку: сначала проверьте сохранённый заказ.'));
        content.append(button(requestKey?'Проверить этот заказ':'Повторить загрузку',requestKey?recover:loadQuote));
      }
    }finally{busy=false;content.removeAttribute('aria-busy');content.querySelectorAll('button').forEach(b=>b.disabled=false);scheduleCheck();}
  }
  function scheduleCheck(immediate=false){
    clearTimeout(autoTimer);
    if(autoStopped||document.hidden||!pending||!['created','paid_pending_activation'].includes(pending.status)||pending.activation_issue)return;
    autoTimer=setTimeout(checkSaved,immediate?0:Math.min(60000,5000*2**autoFailures));
  }
  async function checkSaved(){
    if(busy||autoBusy||document.hidden){scheduleCheck();return;}
    const saved=pending,key=requestKey;
    if(!saved||!['created','paid_pending_activation'].includes(saved.status))return;
    autoBusy=true;
    try{
      // Automatic checks are GET-only: never use recovery's explicit POST
      // fallback, even if the original response was lost or the order is 404.
      const data=await api(key?'/purchases/'+encodeURIComponent(key):'/purchase-quote');
      const order=key?data:data.pending_purchase;
      if(pending?.id!==saved.id||requestKey!==key)return;
      if(order){if(order.id!==saved.id)throw Error('invalid_order');showOrder(order);}
      autoFailures=0;
    }catch(error){if([401,403,404].includes(error.status))autoStopped=true;autoFailures=Math.min(4,autoFailures+1);}
    finally{autoBusy=false;scheduleCheck();}
  }
  function showOrder(order){
    if(!order||!validPrice(order.price_kopecks)||!['created','paid_pending_activation','active','canceled'].includes(order.status))throw Error('invalid_order');
    pending=order;requestKey=order.request_key||null;
    scheduleCheck();
    content.replaceChildren(node('p',`Продление на 30 дней · ${money(order.price_kopecks)}`));
    if(order.status==='active'){content.append(status('Подписка продлена. Обновите её в VPN-приложении.'),button('Обновить страницу',()=>location.reload()));return;}
    if(order.status==='paid_pending_activation'){content.append(status(order.activation_issue?'Оплата сохранена. Покупка требует проверки поддержкой. Повторно платить не нужно.':'Оплата сохранена, активация повторяется. Повторно платить не нужно.'));}
    else if(order.status==='canceled'){requestKey=null;pending=null;content.append(status('Платёж отменён. Новая покупка возможна после проверки цены.'),button('Проверить цену',loadQuote));return;}
    else{
      let url;try{url=new URL(order.confirmation_url);if(url.protocol!=='https:'||url.username||url.password||url.hash)url=null;}catch(_){url=null;}
      content.append(status(url?'Счёт сохранён. Оплатите его один раз — оплату и активацию проверяем автоматически.':'Заказ сохранён. Ссылка оплаты ещё уточняется; новую покупку создавать не нужно.'));
      if(url){const link=node('a','Перейти к оплате');link.className='button primary';link.href=url.href;link.rel='noreferrer noopener';content.append(link);}
      else if(requestKey){expected=order.price_kopecks;content.append(button('Получить ссылку оплаты',create));}
    }
    if(order.status==='paid_pending_activation'&&!requestKey){
      // A bot purchase has no public request key. Refresh its saved state;
      // never turn an already paid order into a new checkout confirmation.
      content.append(button('Обновить состояние',loadQuote));
    }else{
      content.append(button(requestKey?'Проверить оплату':'Восстановить этот заказ',requestKey?recover:()=>review(order.price_kopecks)));
    }
  }
  function review(price){
    expected=price;
    content.replaceChildren(node('p',`Продление этой подписки на 30 дней. К оплате: ${money(price)}.`),node('p','Сохранится та же подписка. Доступные оплаченные ГБ не обнуляются.'));
    content.append(button(pending?'Восстановить сохранённый заказ':'Подтвердить и открыть счёт',create,true),button('Назад',loadQuote));
  }
  async function create(){await run(async()=>{
    requestKey ||= crypto.randomUUID();
    showOrder(await api('/purchases',{request_key:requestKey,expected_price_kopecks:expected}));
  });}
  async function recover(){await run(async()=>{
    try{showOrder(await api('/purchases/'+encodeURIComponent(requestKey)));}
    catch(error){if(error.status!==404||!validPrice(expected))throw error;
      // Same immutable request after a lost POST, never a fresh purchase key.
      showOrder(await api('/purchases',{request_key:requestKey,expected_price_kopecks:expected}));}
  });}
  async function loadQuote(){await run(async()=>{
    const quote=await api('/purchase-quote');
    if(!validPrice(quote.price_kopecks)||quote.days!==30||typeof quote.renewal_available!=='boolean')throw Error('invalid_quote');
    if(quote.pending_purchase){showOrder(quote.pending_purchase);return;}
    pending=null;requestKey=null;expected=null;
    content.replaceChildren(node('p',`30 дней · ${money(quote.price_kopecks)}`));
    if(quote.renewal_available)content.append(button('Продлить на 30 дней',()=>review(quote.price_kopecks),true));
    else content.append(status('Сейчас продление недоступно: предыдущая покупка ещё обрабатывается или требуется проверка подписки.'),button('Обновить состояние',loadQuote));
  });}
  document.addEventListener('visibilitychange',()=>scheduleCheck(true));
  window.addEventListener('focus',()=>scheduleCheck(true));
  window.addEventListener('pagehide',()=>clearTimeout(autoTimer));
  void loadQuote();
})();
