/* Extra cabinet sections. No secret/requisites persistence and no automatic sends. */
(() => {
  'use strict';
  const OWNER = ['analytics', 'customers', 'orders', 'referrals', 'marketing', 'team'];
  const labels = {subscriptions:'Подписки',referrals:'Бонусы за приглашения',marketing:'Продвижение',finance:'Финансы',team:'Команда',customers:'Клиенты',orders:'Продажи',overview:'Обзор',settings:'Настройки'};
  const statuses = {pending:'На проверке',processing:'В работе',paid:'Выплачено',rejected:'Отклонено',draft:'Черновик',review:'Проверка перед отправкой',complete:'Завершена',blocked:'Доставка закрыта',leased:'Подготовка доставки',queued:'В очереди',canceled:'Отменено',delivered:'Доставлено',sending:'Отправляется',uncertain:'Нужна сверка доставки',active:'Активна',expired:'Истекла',paid_pending_activation:'Оплачено · активация',created:'Ожидает оплаты',none:'Без подписки',unpaid:'Без оплаченной подписки'};
  const cache = new Map();
  const cursors = new Map();
  const queries = new Map();
  const filters = new Map();
  const uuid = () => crypto.randomUUID();
  const isOwner = project => !project?.role || project.role === 'owner';
  const permission = (project, value) => (project?.permissions || (isOwner(project) ? OWNER : [])).includes(value);
  const primaryViews = new Set(['overview','customers','finance','settings']);
  const groupedView = view => ({subscriptions:'customers',orders:'overview',brand:'settings',bot:'settings',referrals:'settings',marketing:'settings',team:'settings'}[view] || view);
  const workspaceMode = C => typeof C.workspaceMode === 'boolean' ? C.workspaceMode : /^\/partners\/workspace\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/?$/.test(window.location?.pathname || '');
  function canView(project,view,features={}) {
    if(!project || ['projects','launch','operator'].includes(view)) return false;
    if(view==='settings') return isOwner(project) || !!features.management && ['referrals','marketing','team'].some(value=>permission(project,value));
    if(['brand','bot'].includes(view)) return isOwner(project);
    if(view==='finance') return isOwner(project) && !!features.finance;
    const required={overview:'analytics',customers:'customers',subscriptions:'customers',orders:'orders',referrals:'referrals',marketing:'marketing',team:'team'}[view];
    if(!required || !permission(project,required)) return false;
    if(['subscriptions','referrals','marketing','team'].includes(view)) return !!features.management;
    return isOwner(project) || !!features.management;
  }
  const base = C => `/projects/${encodeURIComponent(C.scopeProjectId ?? C.state.selected)}`;
  const manage = C => base(C) + '/management';
  const signedMoney = (C, cents) => Number.isSafeInteger(cents) ? C.money(cents / 100) : 'Не подтверждено';
  const integer = value => Number.isSafeInteger(value) && value >= 0;
  const actor = C => C.scopeActorId ?? C.state.settings?.actor_id ?? 'unverified';
  const operatorKey = C => `${actor(C)}/operator/list`;
  function clear(C) { for (const key of cache.keys()) if (key.startsWith(`${actor(C)}/${C.scopeProjectId ?? C.state.selected}/`)) cache.delete(key); }
  function keyFor(form, body) {
    const fingerprint = JSON.stringify(body);
    if (form.dataset.fingerprint !== fingerprint) { form.dataset.requestKey = uuid(); form.dataset.fingerprint = fingerprint; }
    return form.dataset.requestKey;
  }
  function card(C, title, children=[]) { return C.element('section',{class:'card stack'},[C.element('h2',{text:title}),...children]); }
  function alert(C, text) { return C.element('p',{class:'form-error',role:'alert',text}); }
  function field(C, id, title, value='', options={}) {
    if (!['select','textarea'].includes(options.tag)) return C.field(id,title,value,options);
    const tag = options.tag; delete options.tag;
    const input = C.element(tag,{id,name:id,...options});
    if (tag === 'textarea') input.value = value;
    return {input,wrapper:C.element('div',{class:'field'},[C.element('label',{for:id,text:title}),input])};
  }
  function select(C,id,title,options,value) {
    const result=field(C,id,title,'',{tag:'select'});
    for(const [key,label] of options) result.input.append(C.element('option',{value:key,text:label}));
    result.input.value=value; return result;
  }
  async function action(C, form, caption, operation) {
    if(C.state.busy) return;
    C.loadingForm(form,true,caption);
    try { await operation(); } catch(error) { C.errorBox(form,error,false); }
    finally { C.loadingForm(form,false,caption); }
  }
  function load(C, key, loader, draw) {
    key=`${actor(C)}/${key}`;
    const box=C.element('div',{class:'stack workspace-results'}); C.content.append(box);
    const render=entry=>{
      box.replaceChildren();
      if(entry.status==='loading') box.append(card(C,'Загружаем данные',[C.p('Покажем значения после ответа сервера.','muted'),C.element('span',{class:'spinner',role:'status','aria-label':'Загрузка'})]));
      else if(entry.status==='error') box.append(card(C,'Данные не загрузились',[alert(C,C.errorText(entry.error)),C.button('Повторить',()=>{cache.delete(key);C.render();},'quiet')]));
      else {try{draw(entry.data,box);}catch(error){const failed={status:'error',error};cache.set(key,failed);render(failed);}}
    };
    let entry=cache.get(key);
    if(!entry) {
      entry={status:'loading'}; cache.set(key,entry);
      Promise.resolve().then(loader).then(data=>{if(!data||typeof data!=='object') throw Error('invalid_response');entry={status:'ready',data};cache.set(key,entry);render(entry);})
        .catch(error=>{entry={status:'error',error};cache.set(key,entry);render(entry);});
    }
    render(entry);
  }
  function header(C, view, description) {
    C.content.append(C.heading(labels[view],description,C.selectedProject()?.title||'Платформа'));
    C.content.append(C.element('div',{class:'report-toolbar'},[C.p(view==='operator'?'Заявки платформы. Реальные переводы вручную.':'Только данные выбранного проекта.','muted fine'),C.button('Обновить',()=>{clear(C);if(view==='operator')cache.delete(operatorKey(C));C.render();},'quiet')]));
  }
  function metric(C,label,value) { return C.element('section',{class:'card metric-card'},[C.p(label,'metric-label'),C.element('strong',{class:'metric-value',text:value})]); }
  function pages(C,data,kind,box) {
    const old=cursors.get(`${C.state.selected}/${kind}`)||0;
    const row=C.element('div',{class:'button-row'});
    if(old) row.append(C.button('Сначала',()=>{cursors.set(`${C.state.selected}/${kind}`,0);C.render();},'quiet'));
    if(integer(data.next_cursor)) row.append(C.button('Следующая страница',()=>{cursors.set(`${C.state.selected}/${kind}`,data.next_cursor);C.render();},'quiet'));
    box.append(row);
  }
  function validItems(data) { if(!Array.isArray(data.items)) throw Error('invalid_response');return data.items; }
  function renderPeople(C,view) {
    const project=C.selectedProject();
    header(C,view,view==='customers'?'Поиск по Telegram ID и история покупок.':'Сроки и квоты. Доступы бесплатно здесь не выдаются.');
    C.content.append(C.element('div',{class:'button-row',role:'group','aria-label':'Клиенты и подписки'},[
      C.button('Клиенты',()=>C.setView('customers'),view==='customers'?'primary':'quiet'),
      C.button('Подписки',()=>C.setView('subscriptions'),view==='subscriptions'?'primary':'quiet'),
    ]));
    const id=`${C.state.selected}/${view}`, cursor=cursors.get(id)||0, query=queries.get(id)||'';
    if(view==='customers') {
      const form=C.element('form',{class:'card inline-form'}),input=field(C,'customer-query','Telegram ID',query,{inputmode:'numeric',maxlength:19});
      form.append(input.wrapper,C.submitButton('Найти')); form.onsubmit=event=>{event.preventDefault();if(!/^\d{0,19}$/.test(input.input.value.trim())){input.input.focus();return;}queries.set(id,input.input.value.trim());cursors.set(id,0);C.render();}; C.content.append(form);
    }
    const endpoint=`${manage(C)}/${view}?limit=25&cursor=${cursor}`+(view==='customers'?`&query=${encodeURIComponent(query)}`:'');
    load(C,`${id}/${cursor}/${query}`,()=>C.api(endpoint),(data,box)=>{
      const items=validItems(data);
      if(!items.length) box.append(card(C,view==='customers'?'Клиентов не найдено':'Подписок пока нет',[C.p('Здесь не показываются вымышленные пользователи или доступы.','muted')]));
      for(const item of items) {
        const actions=C.element('div',{class:'button-row'});
        if(permission(project,'orders')) actions.append(C.button('История покупок',()=>renderCustomerOrders(C,item.tg_id),'quiet'));
        if(isOwner(project)||project.role==='support') actions.append(C.button('Отправить подписку клиенту',async()=>{
          const form=actions; await action(C,form,'Отправить',async()=>{
            await C.api(`${manage(C)}/customers/${item.tg_id}/resend`,{method:'POST',body:{request_key:keyFor(form,{tg_id:item.tg_id})}});
            C.notify('Запрос сохранён. Это отправка уже оплаченной подписки, без новой выдачи.');
          });
        },'quiet'));
        const values=[C.p(`Регистрация: ${C.date(item.created_at)}`,'record-meta'),C.p(item.expires_at?`Подписка до ${C.date(item.expires_at)}`:'Оплаченный срок не установлен','record-meta')];
        if(view==='subscriptions') {
          values.push(C.p(`Устройств по подписке: ${integer(item.device_limit)?item.device_limit:'уточняется'}`,'record-meta'));
          values.push(C.p(integer(item.traffic_period_limit_bytes)?`Квота периода: ${(item.traffic_period_limit_bytes/1024**3).toLocaleString('ru-RU',{maximumFractionDigits:1})} ГБ`:'Квота трафика ожидает проверки','record-meta'));
        } else values.push(C.p(`Оплаченных покупок: ${integer(item.paid_orders)?item.paid_orders:'не подтверждено'}`,'record-meta'));
        box.append(card(C,`Клиент ${item.tg_id}`,[...values,actions]));
      }
      pages(C,data,view,box);
    });
  }
  function orderCards(C,data,box) {
    const items=validItems(data);
    if(!items.length) box.append(card(C,'Покупок пока нет'));
    for(const item of items) box.append(card(C,`Заказ ${String(item.id).slice(0,8)}`,[
      C.p(`Клиент ${item.tg_id} · ${C.date(item.created_at)}`,'record-meta'),
      C.p(signedMoney(C,item.price_kopecks),'record-value'),
      C.p(item.activation_issue?'Оплата сохранена · нужна сверка':statuses[item.status]||'Статус уточняется','record-status'),
      C.p(({subscription:'Подписка',traffic:'Докупка ГБ',devices:'Докупка устройств'})[item.purpose]||'Покупка','record-meta')]));
  }
  function renderCustomerOrders(C,tg) {
    C.content.replaceChildren(C.heading(`Покупки клиента ${tg}`,'Личные ссылки, реквизиты и ключи здесь не раскрываются.'),C.button('Назад к клиентам',()=>C.render(),'quiet'));
    load(C,`${C.state.selected}/customer-orders/${tg}`,()=>C.api(`${manage(C)}/customers/${tg}/orders?limit=50&cursor=0`),(data,box)=>orderCards(C,data,box));
  }
  function renderStaffReport(C,view) {
    header(C,view,'Просмотр в пределах вашей роли. Настройки и финансы владельца недоступны.');
    if(view==='overview' && permission(C.selectedProject(),'orders')) C.content.append(C.button('Продажи',()=>C.setView('orders'),'quiet'));
    if(view==='orders') C.content.append(C.button('К обзору проекта',()=>C.setView(defaultView(C.selectedProject())),'quiet'));
    const cursor=cursors.get(`${C.state.selected}/${view}`)||0;
    load(C,`${C.state.selected}/staff/${view}/${cursor}`,()=>C.api(`${manage(C)}/${view}?limit=25&cursor=${cursor}`),(data,box)=>{
      if(view==='orders'){orderCards(C,data,box);pages(C,data,view,box);return;}
      const metrics=[['Клиентов','customers'],['Активных подписок','active_subscriptions'],['Оплаченных покупок','paid_orders'],['Выручка','revenue_kopecks']];
      box.append(C.element('div',{class:'metric-grid'},metrics.map(([label,key])=>metric(C,label,key.endsWith('kopecks')?signedMoney(C,data[key]):integer(data[key])?data[key]:'Не подтверждено'))));
    });
  }
  function renderReferrals(C) {
    header(C,'referrals','+7 дней за первую оплаченную подписку друга.');
    load(C,`${C.state.selected}/referrals`,()=>C.api(`${manage(C)}/referrals`),(data,box)=>{
      box.append(C.element('div',{class:'metric-grid'},[['Пришло по приглашениям','invited'],['Друзей купили подписку','qualified'],['Заработано дней','earned_days'],['Применено дней','applied_days'],['Ожидают применения','pending_days']].map(([label,key])=>metric(C,label,integer(data[key])?data[key]:'Не подтверждено'))));
      box.append(card(C,'Правила бонусов',[C.p('Один друг — один бонус после первой покупки подписки. Регистрация, продления и докупки новых бонусных дней не дают.','muted'),C.p('Бонусы начисляются автоматически. Ручное изменение подписок недоступно.','muted')]));
    });
  }
  function pendingKey(C){const identity=actor(C);return Number.isSafeInteger(identity)?`spn.partner.withdrawal.${identity}.${C.scopeProjectId ?? C.state.selected}`:null;}
  function savePending(C,key){try{const name=pendingKey(C);if(name)localStorage.setItem(name,key);}catch{/* Private browsing: stable in-memory key still survives same view. */}}
  function readPending(C){try{const value=localStorage.getItem(pendingKey(C)||'');return /^[a-f0-9-]{36}$/.test(value||'')?value:null;}catch{return null;}}
  function deletePending(C){try{const name=pendingKey(C);if(name)localStorage.removeItem(name);}catch{}}
  function withdrawalCard(C,item){return card(C,`${item.number||'Заявка'} · ${signedMoney(C,item.amount_kopecks)}`,[C.p(`${item.method==='sbp'?'СБП':'USDT'} · ${statuses[item.status]||'Уточняется'}`,'record-status'),C.p(`Создана ${C.date(item.created_at)}`,'record-meta'),item.destination_hint?C.p(item.destination_hint,'record-meta'):null,item.rejection_reason?C.p(`Причина: ${item.rejection_reason}`,'muted'):null]);}
  function renderFinance(C){
    header(C,'finance','Ваши 70% с покупок. Вывод от 1 500 ₽; выплаты выполняются вручную.');
    load(C,`${C.state.selected}/finance`,async()=>({summary:await C.api(`${base(C)}/finance/summary`),history:await C.api(`${base(C)}/finance/withdrawals`)}),(data,box)=>{
      const s=data.summary;
      if(!['available_kopecks','earned_kopecks','reserved_kopecks','paid_kopecks','debt_kopecks'].every(key=>integer(s?.[key]))) throw Error('invalid_response');
      box.append(C.element('div',{class:'metric-grid'},[['Доступно к выводу',s.available_kopecks],['В заявках',s.reserved_kopecks],['Выплачено',s.paid_kopecks],['Начислено с продаж',s.earned_kopecks]].map(([label,value])=>metric(C,label,signedMoney(C,value)))));
      if(s.debt_kopecks) box.append(alert(C,`Задолженность: ${signedMoney(C,s.debt_kopecks)}. Вывод закрыт до её погашения последующими начислениями.`));
      if(s.reversed_kopecks)box.append(C.p(`Подтверждённые сторно: ${signedMoney(C,s.reversed_kopecks)}. Они учитываются отдельно от продаж.`,'report-note'));
      const saved=readPending(C);
      if(saved){const area=card(C,'Проверяем незавершённую заявку',[C.p('Не подавайте новую заявку, пока проверка не завершена.','muted')]);box.append(area);void restoreWithdrawal(C,saved,area);}
      else box.append(C.button('Создать заявку на вывод',()=>withdrawalForm(C,s),'primary'));
      box.append(C.element('h2',{text:'История заявок'}));
      for(const item of validItems(data.history)) box.append(withdrawalCard(C,item));
      if(!data.history.items.length)box.append(C.p('Заявок пока нет.','muted'));
    });
  }
  async function restoreWithdrawal(C,key,box){
    try{const item=await C.api(`${base(C)}/finance/withdrawals/${encodeURIComponent(key)}`);box.replaceChildren(withdrawalCard(C,item));deletePending(C);clear(C);box.append(C.button('Обновить финансы',()=>C.render(),'quiet'));}
    catch(error){box.replaceChildren(C.element('h2',{text:'Ответ ещё не получен'}),C.p(C.errorText(error),'muted'),C.button('Проверить снова',()=>restoreWithdrawal(C,key,box),'quiet'));
      if(error.status===404)box.append(C.button('Заявка не найдена — вернуться к форме',()=>{deletePending(C);clear(C);C.render();},'quiet'));
    }
  }
  function withdrawalForm(C,summary){
    C.content.replaceChildren(C.heading('Заявка на вывод','Сначала проверьте сумму и реквизиты. Реального перевода после нажатия не происходит.'),C.button('Назад к финансам',()=>C.render(),'quiet'));
    const form=C.element('form',{class:'card stack',novalidate:true});
    const amount=field(C,'withdrawal-amount','Сумма, ₽','',{inputmode:'decimal',maxlength:12,required:true});
    const method=select(C,'withdrawal-method','Способ',[['sbp','СБП'],['usdt','USDT']],'sbp');
    const bank=field(C,'withdrawal-bank','Банк','',{maxlength:120,autocomplete:'off'}),phone=field(C,'withdrawal-phone','Телефон','',{type:'tel',maxlength:32,autocomplete:'off'}),address=field(C,'withdrawal-address','Адрес USDT','',{maxlength:160,autocomplete:'off',spellcheck:'false'});
    const details=C.element('div');
    function change(){details.replaceChildren(...(method.input.value==='sbp'?[bank.wrapper,phone.wrapper]:[address.wrapper,C.p('Сеть, курс и сумма USDT согласуются вручную. Не отправляем автоматически.','hint')]));}
    method.input.onchange=change;change();
    form.append(C.p(`Доступно: ${signedMoney(C,summary.available_kopecks)}. Минимум: 1 500 ₽.`,'muted'),amount.wrapper,method.wrapper,details,C.submitButton('Проверить заявку'));
    form.onsubmit=event=>{
      event.preventDefault();if(C.state.busy)return;
      const value=amount.input.value.trim().replace(',','.');
      if(!/^\d{1,8}(\.\d{1,2})?$/.test(value)){amount.input.focus();return;}
      const cents=Math.round(Number(value)*100);
      if(cents<150000||cents>summary.available_kopecks||summary.debt_kopecks){form.querySelector('.form-error')?.remove();form.append(alert(C,'Проверьте сумму: минимум 1 500 ₽, не больше доступного остатка и без задолженности.'));return;}
      const requisites=method.input.value==='sbp'?{bank:bank.input.value.trim(),phone:phone.input.value.trim()}:{address:address.input.value.trim()};
      if(Object.values(requisites).some(v=>!v)){form.append(alert(C,'Заполните реквизиты.'));return;}
      const body={amount_kopecks:cents,method:method.input.value,requisites};
      const key=keyFor(form,body);
      const review=card(C,'Проверьте перед подачей',[C.p(signedMoney(C,cents),'metric-value'),C.p(body.method==='sbp'?`СБП · ${requisites.bank}\n${requisites.phone}`:`USDT · ${requisites.address}`,'workspace-destination'),C.p('Сумма будет зарезервирована. Администратор проверит заявку и выплатит вручную.','muted')]);
      const confirm=C.button('Подать заявку',()=>action(C,review,'Подать заявку',async()=>{
        savePending(C,key);
        try{const result=await C.api(`${base(C)}/finance/withdrawals`,{method:'POST',body:{request_key:key,...body}});deletePending(C);clear(C);C.state.busy=false;C.content.replaceChildren(C.heading('Заявка сохранена','Повторно подавать её не нужно.'),withdrawalCard(C,result),C.button('К финансам',()=>C.render(),'quiet'));}
        catch(error){if(error.status>=400&&error.status<500&&error.status!==409){deletePending(C);}throw error;}
      }),'primary');
      review.append(C.element('div',{class:'button-row'},[confirm,C.button('Исправить реквизиты',()=>review.remove(),'quiet')]));
      form.querySelector('.workspace-review')?.remove();review.classList.add('workspace-review');form.append(review);review.scrollIntoView({block:'nearest'});
    };
    C.content.append(form);
  }
  function renderTeam(C){
    header(C,'team','Сотрудник сначала открывает кабинет через центрального бота. Владелец сохраняет контроль над деньгами и настройками.');
    load(C,`${C.state.selected}/team`,()=>C.api(`${manage(C)}/team`),(data,box)=>{
      box.append(C.p(`Владелец: ${data.owner_tg_id}`,'muted'));
      const form=C.element('form',{class:'card stack'}),tg=field(C,'member-id','Telegram ID сотрудника','',{inputmode:'numeric',maxlength:16,required:true}),role=select(C,'member-role','Права',[['viewer','Наблюдатель: статистика и клиенты'],['support','Поддержка: клиенты и покупки'],['marketer','Маркетолог: рекламные ссылки и рассылки']],'viewer');
      form.append(tg.wrapper,role.wrapper,C.submitButton('Добавить сотрудника'));
      form.onsubmit=event=>{event.preventDefault();const id=Number(tg.input.value);if(!Number.isSafeInteger(id)||id<=0){tg.input.focus();return;}const previous=data.items.find(item=>item.tg_id===id);const body={tg_id:id,role:role.input.value,expected_version:previous?.version||0};void action(C,form,'Добавить сотрудника',async()=>{await C.api(`${manage(C)}/team`,{method:'POST',body:{request_key:keyFor(form,body),...body}});clear(C);C.render();C.notify('Права сохранены.');});};box.append(form);
      for(const member of validItems(data))box.append(card(C,`Сотрудник ${member.tg_id}`,[C.p(`${({viewer:'Наблюдатель',support:'Поддержка',marketer:'Маркетолог'})[member.role]||member.role} · ${member.active?'Доступ открыт':'Доступ закрыт'}`,'muted'),member.active?C.button('Закрыть доступ',()=>action(C,box,'Закрыть доступ',async()=>{await C.api(`${manage(C)}/team`,{method:'POST',body:{request_key:uuid(),tg_id:member.tg_id,role:null,expected_version:member.version}});clear(C);C.render();}),'quiet'):null]));
      box.append(C.p('Сотрудники не могут менять цены, токен бота, баланс, выдавать подписки или подавать заявки на выплату.','report-note'));
    });
  }
  function renderMarketing(C){
    header(C,'marketing','Рекламные ссылки и рассылки только после явного подтверждения. Нажатие «Поделиться» не считается отправленным приглашением.');
    load(C,`${C.state.selected}/marketing`,async()=>({campaigns:await C.api(`${manage(C)}/campaigns`),broadcasts:await C.api(`${manage(C)}/broadcasts`)}),(data,box)=>{
      const campaignForm=C.element('form',{class:'card stack'}),title=field(C,'campaign-title','Название рекламной ссылки','',{maxlength:80,required:true});
      campaignForm.append(C.element('h2',{text:'Новая рекламная ссылка'}),title.wrapper,C.submitButton('Создать ссылку'));
      campaignForm.onsubmit=e=>{e.preventDefault();if(!title.input.value.trim())return;const body={title:title.input.value.trim()};void action(C,campaignForm,'Создать ссылку',async()=>{await C.api(`${manage(C)}/campaigns`,{method:'POST',body:{request_key:keyFor(campaignForm,body),...body}});clear(C);C.render();});};box.append(campaignForm);
      for(const item of validItems(data.campaigns)){
        const link=field(C,`campaign-${item.id}`,'Личная рекламная ссылка',item.link||'',{readonly:true});
        const node=card(C,item.title,[link.wrapper,C.p(`Регистраций: ${integer(item.registrations)?item.registrations:'—'} · первых покупок: ${integer(item.first_purchases)?item.first_purchases:'—'} · повторных: ${integer(item.repeat_purchases)?item.repeat_purchases:'—'}`,'record-meta'),C.p(`Выручка: ${signedMoney(C,item.revenue_kopecks)}`,'record-meta')]);
        node.append(C.button('Скопировать ссылку',async()=>{try{await navigator.clipboard.writeText(link.input.value);C.notify('Ссылка скопирована.');}catch{link.input.focus();link.input.select();C.notify('Скопируйте выделенную ссылку вручную.');}},'quiet'));box.append(node);
      }
      const form=C.element('form',{class:'card stack'}),text=field(C,'broadcast-text','Текст сообщения','',{tag:'textarea',rows:5,maxlength:3000,required:true});
      form.append(C.element('h2',{text:'Черновик рассылки'}),text.wrapper,C.p('Только подписавшимся на новости в вашем боте. Подготовка черновика ничего не отправляет.','hint'),C.submitButton('Сохранить черновик'));
      form.onsubmit=e=>{e.preventDefault();if(!text.input.value.trim())return;const body={text:text.input.value.trim()};void action(C,form,'Сохранить черновик',async()=>{await C.api(`${manage(C)}/broadcasts`,{method:'POST',body:{request_key:keyFor(form,body),...body}});clear(C);C.render();});};box.append(form);
      for(const item of validItems(data.broadcasts)){
        const row=card(C,`Рассылка · ${statuses[item.status]||item.status}`,[C.p(item.text,'workspace-message'),C.p(`Доставлено: ${integer(item.recipients?.delivered)?item.recipients.delivered:'не подтверждено'} · нужна сверка: ${integer(item.recipients?.uncertain)?item.recipients.uncertain:'не подтверждено'}`,'record-meta')]);
        if(['draft','review'].includes(item.status))row.append(C.button('Проверить и подтвердить',()=>reviewBroadcast(C,item),'primary'));
        if(['draft','review','queued'].includes(item.status))row.append(C.button('Отменить рассылку',()=>action(C,row,'Отменить рассылку',async()=>{await C.api(`${manage(C)}/broadcasts/${item.id}/cancel`,{method:'POST',body:{request_key:uuid(),expected_version:item.version}});clear(C);C.render();}),'quiet'));
        box.append(row);
      }
    });
  }
  async function reviewBroadcast(C,item){
    const review=await C.api(`${manage(C)}/broadcasts/${item.id}/review`,{method:'POST',body:{expected_version:item.version}}).catch(error=>{C.notify(C.errorText(error));return null;});
    if(!review)return;
    clear(C);
    C.content.replaceChildren(C.heading('Проверка рассылки','Проверьте текст и число получателей перед отправкой.'),C.button('Назад без отправки',()=>{clear(C);C.render();},'quiet'));
    const form=C.element('form',{class:'card stack'}),checkbox=C.element('input',{type:'checkbox',id:'broadcast-confirm',required:true});
    form.append(C.p(review.text,'workspace-message'),C.p(`Получателей с согласием: ${review.eligible_recipients}`,'metric-label'),C.element('label',{class:'checkbox-row',for:'broadcast-confirm'},[checkbox,'Подтверждаю отправку именно этого сообщения этим получателям']),C.submitButton('Подтвердить рассылку'));
    const key=uuid();form.onsubmit=e=>{e.preventDefault();if(!checkbox.checked)return;void action(C,form,'Подтвердить рассылку',async()=>{await C.api(`${manage(C)}/broadcasts/${item.id}/confirm`,{method:'POST',body:{request_key:key,expected_version:review.version,review_token:review.review_token}});clear(C);C.state.busy=false;C.render();C.notify('Подтверждение сохранено. Доставка зависит от отдельно включённого обработчика.');});};C.content.append(form);
  }
  function renderOperator(C){
    header(C,'operator','Только оператор платформы. Здесь фиксируются уже выполненные вручную переводы, а не отправляются деньги.');
    load(C,'operator/list',()=>C.api('/operator/withdrawals'),(data,box)=>{
      for(const item of validItems(data)){const row=withdrawalCard(C,item);row.append(C.button('Проверить заявку',()=>operatorDetail(C,item),'quiet'));box.append(row);}
      if(!data.items.length)box.append(card(C,'Заявок нет'));
    });
  }
  async function operatorDetail(C,item){
    const prefix=`/operator/projects/${encodeURIComponent(item.project_id)}/withdrawals/${encodeURIComponent(item.id)}`;
    try{
      const detail=await C.api(prefix);
      C.content.replaceChildren(C.heading(item.number||'Проверка заявки','Не переводите повторно по неопределённой операции.'),C.button('К заявкам',()=>{cache.delete(operatorKey(C));C.render();},'quiet'));
      const row=withdrawalCard(C,detail),destination=detail.requisites||detail.destination;
      if(destination)row.append(C.p(Object.values(destination).join('\n'),'workspace-destination'));
      if(detail.status==='pending')row.append(C.button('Взять в работу',()=>action(C,row,'Взять в работу',async()=>{await C.api(prefix+'/claim',{method:'POST',body:{}});await operatorDetail(C,item);}),'primary'));
      if(detail.status==='processing'){
        const form=C.element('form',{class:'stack'}),reference=field(C,'transfer-reference','Номер / идентификатор выполненного перевода','',{required:true,maxlength:200,autocomplete:'off'}),confirmed=C.element('input',{type:'checkbox',id:'paid-confirm',required:true});
        form.append(reference.wrapper,C.element('label',{for:'paid-confirm',class:'checkbox-row'},[confirmed,'Деньги уже переведены вручную; сумма и реквизиты проверены']),C.submitButton('Зафиксировать выполненную выплату'));
        form.onsubmit=e=>{e.preventDefault();if(!confirmed.checked||!reference.input.value.trim())return;void action(C,form,'Зафиксировать выполненную выплату',async()=>{await C.api(prefix+'/paid',{method:'POST',body:{actual_amount_kopecks:detail.amount_kopecks,transfer_reference:reference.input.value.trim(),confirmed_transferred:true}});cache.delete(operatorKey(C));await operatorDetail(C,item);});};row.append(form);
      }
      if(['pending','processing'].includes(detail.status)){
        const form=C.element('form',{class:'stack'}),reason=field(C,'reject-reason','Причина отклонения','',{required:true,maxlength:500}),notSent=C.element('input',{type:'checkbox',id:'not-sent-confirm',required:true});
        form.append(reason.wrapper);if(detail.status==='processing')form.append(C.element('label',{for:'not-sent-confirm',class:'checkbox-row'},[notSent,'Проверил: перевода точно не было. Разрешаю снять резерв']));form.append(C.submitButton('Отклонить и освободить резерв'));
        form.onsubmit=e=>{e.preventDefault();if(!reason.input.value.trim()||detail.status==='processing'&&!notSent.checked)return;void action(C,form,'Отклонить',async()=>{await C.api(prefix+'/reject',{method:'POST',body:{reason:reason.input.value.trim(),...(detail.status==='processing'?{confirmed_no_transfer:true}:{})}});cache.delete(operatorKey(C));await operatorDetail(C,item);});};row.append(form);
      }
      C.content.append(row);
    }catch(error){C.notify(C.errorText(error));}
  }
  function navigation(C){
    const project=C.selectedProject(), features=C.state.settings?.features||{};
    const tenant=workspaceMode(C), active=groupedView(C.state.view);
    document.querySelector('.navigation')?.classList.remove('extended');
    for(const node of document.querySelectorAll('[data-view]')){
      const view=node.dataset.view, primary=!!node.closest('.navigation');
      node.hidden=!canView(project,view,features) || primary && (!tenant || !primaryViews.has(view));
      if(primary){
        const selected=!node.hidden && view===active;
        node.classList.toggle('active',selected);
        if(selected)node.setAttribute('aria-current','page');else node.removeAttribute('aria-current');
      }
    }
  }
  function render(C){
    C={...C,scopeProjectId:C.state.selected,scopeActorId:C.state.settings?.actor_id};
    const view=C.state.view, features=C.state.settings?.features||{}, project=C.selectedProject();
    if(!project)return false;
    if(!canView(project,view,features)){
      const fallback=['overview','customers','settings'].find(candidate=>canView(project,candidate,features));
      C.content.append(card(C,'Раздел недоступен',[
        C.p('Откройте раздел вашего проекта, разрешённый вашей роли.','muted'),
        fallback?C.button('К проекту',()=>C.setView(fallback),'quiet'):null,
      ]));
      return true;
    }
    if(['customers','subscriptions'].includes(view)&&features.management){renderPeople(C,view);return true;}
    if(['overview','orders'].includes(view)&&!isOwner(project)){renderStaffReport(C,view);return true;}
    if(view==='referrals'&&features.management){renderReferrals(C);return true;}
    if(view==='marketing'&&features.management){renderMarketing(C);return true;}
    if(view==='team'&&features.management){renderTeam(C);return true;}
    if(view==='finance'&&features.finance){renderFinance(C);return true;}
    return false;
  }
  const demo={withdrawals:[],campaigns:[],broadcasts:[],team:[]};
  function previewApi(path,options,projects){
    if(!/\/management\/|\/finance\/|^\/operator\//.test(path))return undefined;
    return(async()=>{
      const body=options.body||{}, project=projects[0], now=new Date().toISOString();
      if(path.endsWith('/finance/summary'))return{earned_kopecks:258930,reversed_kopecks:0,reserved_kopecks:demo.withdrawals.filter(w=>w.status==='pending').reduce((a,w)=>a+w.amount_kopecks,0),paid_kopecks:0,debt_kopecks:0,available_kopecks:258930-demo.withdrawals.filter(w=>w.status==='pending').reduce((a,w)=>a+w.amount_kopecks,0),minimum_withdrawal_kopecks:150000};
      if(path.endsWith('/finance/withdrawals')&&options.method==='POST'){const old=demo.withdrawals.find(w=>w.request_key===body.request_key);if(old)return old;const item={id:uuid(),project_id:project.id,number:`P-DEMO${demo.withdrawals.length+1}`,request_key:body.request_key,amount_kopecks:body.amount_kopecks,method:body.method,status:'pending',created_at:now,destination_hint:'Демонстрация · реквизиты не сохранены'};demo.withdrawals.push(item);return structuredClone(item);}
      if(path.endsWith('/finance/withdrawals')||path==='/operator/withdrawals')return{items:structuredClone(demo.withdrawals),truncated:false};
      if(path.includes('/finance/withdrawals/')){const item=demo.withdrawals.find(w=>w.request_key===path.split('/').pop());if(item)return structuredClone(item);throw Object.assign(Error('not_found'),{status:404});}
      if(path.includes('/operator/projects/')){const parts=path.split('/'),item=demo.withdrawals.find(w=>w.id===parts[5]);if(!item)throw Object.assign(Error('not_found'),{status:404});if(path.endsWith('/claim'))item.status='processing';if(path.endsWith('/paid'))item.status='paid';if(path.endsWith('/reject'))item.status='rejected';return {...item,requisites:{bank:'Демонстрационный банк',phone:'Реквизиты не сохраняются'}};}
      if(path.includes('/management/customers/')&&path.endsWith('/resend'))return{id:uuid(),status:'pending',delivery_enabled:false};
      if(path.includes('/management/customers/')&&path.includes('/orders'))return{items:[{id:uuid(),tg_id:100001,status:'active',purpose:'subscription',price_kopecks:24900,created_at:now,paid_at:now}],next_cursor:null};
      if(path.includes('/management/customers?'))return{items:[{id:uuid(),tg_id:100001,created_at:now,expires_at:'2026-11-20T12:00:00Z',paid_orders:2}],next_cursor:null};
      if(path.includes('/management/subscriptions?'))return{items:[{id:uuid(),tg_id:100001,created_at:now,expires_at:'2026-11-20T12:00:00Z',device_limit:3,traffic_period_limit_bytes:350*1024**3}],next_cursor:null};
      if(path.endsWith('/management/referrals'))return{invited:8,qualified:3,earned_days:21,applied_days:14,pending_days:7};
      if(path.endsWith('/management/team')){if(options.method==='POST'){let member=demo.team.find(m=>m.tg_id===body.tg_id);if(!member){member={tg_id:body.tg_id,version:0};demo.team.push(member);}Object.assign(member,{role:body.role||member.role,active:body.role!==null,version:member.version+1});return member;}return{owner_tg_id:123,items:structuredClone(demo.team),truncated:false};}
      if(path.endsWith('/management/campaigns')){if(options.method==='POST'){const item={id:uuid(),title:body.title,link:'https://t.me/offline_preview_bot?start=m_DEMO',registrations:0,first_purchases:0,repeat_purchases:0,revenue_kopecks:0,version:1};demo.campaigns.push(item);return item;}return{items:structuredClone(demo.campaigns),next_cursor:null};}
      if(path.endsWith('/management/broadcasts')){if(options.method==='POST'){const item={id:uuid(),text:body.text,status:'draft',version:1,delivered_count:0,uncertain_count:0};demo.broadcasts.push(item);return item;}return{items:structuredClone(demo.broadcasts),next_cursor:null};}
      const item=demo.broadcasts.find(b=>path.includes(b.id));
      if(item){if(path.endsWith('/review')){item.status='review';item.version++;return{review_token:'demo-review-not-real',version:item.version,text:item.text,eligible_recipients:2,requires_confirmation:true};}if(path.endsWith('/confirm'))item.status='queued';if(path.endsWith('/cancel'))item.status='canceled';return structuredClone(item);}
      throw Object.assign(Error('not_found'),{status:404});
    })();
  }
  const defaultView = project => permission(project,'analytics')?'overview':permission(project,'customers')?'customers':'settings';
  window.SPNPartnerWorkspace=Object.freeze({render,navigation,previewApi,defaultView,canView});
})();
