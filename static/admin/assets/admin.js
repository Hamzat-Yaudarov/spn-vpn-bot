const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = tg?.initData || "";
const state = { view: "dashboard", me:null, dashboard:null, users:[], selectedUser:null, promos:[], links:[], referrals:[], notifications:null, discounts:[] };

function el(id){ return document.getElementById(id); }
function toast(text){ const t=el("toast"); t.textContent=text; t.classList.add("show"); setTimeout(()=>t.classList.remove("show"),2200); }
function rub(v){ return `${Number(v||0).toLocaleString("ru-RU")} ₽`; }
function date(v){ return v ? new Date(v).toLocaleString("ru-RU") : "-"; }
function api(path, options={}){ return fetch(path,{...options,headers:{"Content-Type":"application/json","Authorization":`tma ${initData}`,...(options.headers||{})}}).then(async r=>{const d=await r.json().catch(()=>({})); if(!r.ok) throw new Error(d.detail||"Ошибка"); return d;}); }

document.querySelectorAll(".tab").forEach(b=>b.addEventListener("click",()=>{ state.view=b.dataset.view; document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("active",x===b)); document.querySelectorAll(".view").forEach(v=>v.classList.remove("active")); el(`view-${state.view}`).classList.add("active"); render(); }));

async function reloadAll(){ await load(); toast("Обновлено"); }
async function load(){
  try{
    const me = await api("/admin/api/me");
    const requests = await Promise.allSettled([
      api("/admin/api/dashboard"), api("/admin/api/users"), api("/admin/api/promos"), api("/admin/api/tracking-links"), api("/admin/api/referrals"), api("/admin/api/notifications"), api("/admin/api/discounts")
    ]);
    const [dashboard,users,promos,links,referrals,notifications,discounts] = requests.map((result, index) => {
      if (result.status === "fulfilled") return result.value;
      console.error("Admin section failed", index, result.reason);
      return null;
    });
    Object.assign(state,{me,dashboard:dashboard||{},users:users?.users||[],promos:promos?.promos||[],links:links?.links||[],referrals:referrals?.referrals||[],notifications:notifications||{rules:[],state:[]},discounts:discounts?.discounts||[]});
    el("adminLine").textContent = me.username ? `@${me.username}` : `ID ${me.id}`;
    render();
    const failed = requests.filter((r) => r.status === "rejected").length;
    if (failed) toast(`Загружено частично. Ошибок: ${failed}`);
  }catch(e){ document.querySelector("main").innerHTML=`<div class="card"><h2>Нет доступа</h2><p class="muted">${e.message}</p><p class="muted">Откройте панель кнопкой в боте, не обычной ссылкой в браузере.</p></div>`; }
}

function render(){ renderDashboard(); renderUsers(); renderPromos(); renderLinks(); renderNotifications(); renderDiscounts(); }
function renderDashboard(){ const d=state.dashboard||{}; el("view-dashboard").innerHTML=`<div class="cards">
  ${stat("Пользователи",d.users)}${stat("Активные подписки",d.active_subscriptions)}${stat("Regular",d.active_regular)}${stat("Bypass",d.active_bypass)}${stat("Без подписки",d.users_without_subscription)}${stat("Выручка",rub(d.revenue))}${stat("Платежи",d.paid_payments)}${stat("Куплено ГБ",d.traffic_gb)}
</div>`; }
function stat(label,value){ return `<div class="card stat"><span class="muted">${label}</span><b>${value??0}</b></div>`; }

function renderUsers(){ el("view-users").innerHTML=`<div class="grid"><div class="card"><h2>Пользователи</h2><div class="form"><input id="userSearch" placeholder="tg_id или username"><button class="button blue" onclick="searchUsers()">Найти</button></div></div><div class="list">${state.users.map(userHtml).join("")}</div><div id="userDetail"></div></div>`; if(state.selectedUser) renderUserDetail(); }
function userHtml(u){ return `<div class="item"><div class="row"><div><b>${u.username||"без username"}</b><small>ID ${u.tg_id} · активных: ${u.active_subscriptions||0} · ${rub(u.revenue)}</small></div><button class="button ghost" onclick="openUser(${u.tg_id})">Открыть</button></div></div>`; }
async function searchUsers(){ const q=el("userSearch").value.trim(); const r=await api(`/admin/api/users${q?`?q=${encodeURIComponent(q)}`:""}`); state.users=r.users||[]; renderUsers(); }
async function openUser(id){ state.selectedUser=await api(`/admin/api/users/${id}`); renderUserDetail(); }
function renderUserDetail(){ const d=state.selectedUser; const box=el("userDetail"); if(!box||!d)return; box.innerHTML=`<div class="card"><h2>${d.user.username||"Пользователь"}</h2><p class="muted">ID ${d.user.tg_id}</p><h3>Подписки</h3><div class="list">${d.subscriptions.map(subHtml).join("")||"<p class='muted'>Нет подписок</p>"}</div></div>`; }
function subHtml(s){ return `<div class="item"><b>${s.plan_kind||"regular"} #${s.type_index||s.slot_number}</b><small>до ${date(s.subscription_until)} · ${s.is_visible?"видима":"скрыта"} · ${s.is_renewable?"renewable":"non-renewable"}</small><div class="actions"><button class="button green" onclick="changeDays(${s.id},30)">+30 дней</button><button class="button red" onclick="changeDays(${s.id},-30)">-30 дней</button><button class="button ghost" onclick="archiveSub(${s.id})">Архив</button><button class="button red" onclick="deleteSub(${s.id})">Удалить из БД</button></div></div>`; }
async function changeDays(id,days){ await api(`/admin/api/subscriptions/${id}/days`,{method:"POST",body:JSON.stringify({days})}); toast("Готово"); await openUser(state.selectedUser.user.tg_id); }
async function archiveSub(id){ await api(`/admin/api/subscriptions/${id}/archive`,{method:"POST"}); toast("В архиве"); await openUser(state.selectedUser.user.tg_id); }
async function deleteSub(id){ if(!confirm("Удалить подписку из БД?"))return; await api(`/admin/api/subscriptions/${id}`,{method:"DELETE"}); toast("Удалено"); await openUser(state.selectedUser.user.tg_id); }

function renderPromos(){ el("view-promos").innerHTML=`<div class="grid"><div class="card"><h2>Промокоды</h2><div class="form"><input id="promoCode" placeholder="CODE"><input id="promoDays" type="number" placeholder="Дней"><input id="promoLimit" type="number" placeholder="Лимит"><button class="button green" onclick="createPromo()">Создать</button></div></div><div class="list">${state.promos.map(p=>`<div class="item"><div class="row"><div><b>${p.code}</b><small>${p.days} дней · ${p.used_count}/${p.max_uses} · ${p.active?"активен":"выключен"}</small></div><div class="actions"><button class="button ghost" onclick="togglePromo('${p.code}',${!p.active})">${p.active?"Выключить":"Включить"}</button><button class="button red" onclick="deletePromo('${p.code}')">Удалить</button></div></div></div>`).join("")}</div></div>`; }
async function createPromo(){ await api("/admin/api/promos",{method:"POST",body:JSON.stringify({code:el("promoCode").value,days:el("promoDays").value,max_uses:el("promoLimit").value})}); await reloadAll(); }
async function togglePromo(code,active){ await api(`/admin/api/promos/${code}/active`,{method:"POST",body:JSON.stringify({active})}); await reloadAll(); }
async function deletePromo(code){ if(!confirm("Удалить промокод?"))return; await api(`/admin/api/promos/${code}`,{method:"DELETE"}); await reloadAll(); }

function renderLinks(){ el("view-links").innerHTML=`<div class="grid"><div class="card"><h2>Tracking и рефералы</h2><div class="form"><input id="linkCode" placeholder="blogger1"><input id="linkTitle" placeholder="Название"><button class="button green" onclick="createLink()">Создать ссылку</button></div></div><h3>Tracking-ссылки</h3><div class="list">${state.links.map(l=>`<div class="item"><b>${l.link.code}</b><small>${l.link.title||""} · клики ${l.total_clicks} · новые ${l.new_clicks} · оплаты ${l.paid_payments} · ${rub(l.revenue)}</small><div class="actions"><button class="button ghost" onclick="toggleLink('${l.link.code}',${!l.link.is_active})">${l.link.is_active?"Выключить":"Включить"}</button></div></div>`).join("")}</div><h3>Рефералы пользователей</h3><div class="list">${state.referrals.map(r=>`<div class="item"><b>${r.username||r.tg_id}</b><small>ID ${r.tg_id} · клики ${r.clicks} · новые ${r.new_clicks} · рефералы ${r.referred_users} · ${rub(r.referred_revenue)}</small></div>`).join("")||"<p class='muted'>Пока нет данных</p>"}</div></div>`; }
async function createLink(){ await api("/admin/api/tracking-links",{method:"POST",body:JSON.stringify({code:el("linkCode").value,title:el("linkTitle").value})}); await reloadAll(); }
async function toggleLink(code,active){ await api(`/admin/api/tracking-links/${code}/active`,{method:"POST",body:JSON.stringify({active})}); await reloadAll(); }

function renderNotifications(){ const n=state.notifications||{rules:[],state:[]}; el("view-notifications").innerHTML=`<div class="grid"><h2>Правила уведомлений</h2><div class="list">${n.rules.map(ruleHtml).join("")}</div><h2>Последние отправки</h2><div class="list">${n.state.map(s=>`<div class="item"><b>${s.notification_type}</b><small>${s.username||s.tg_id} · ${date(s.last_sent_at)} · sub ${s.subscription_id}</small></div>`).join("")}</div></div>`; }
function ruleHtml(r){ return `<div class="item"><b>${r.notification_type}</b><div class="form"><select id="en_${r.notification_type}"><option value="true" ${r.enabled?"selected":""}>Вкл</option><option value="false" ${!r.enabled?"selected":""}>Выкл</option></select><input id="hour_${r.notification_type}" type="number" placeholder="Час МСК" value="${r.send_hour_msk??""}"><input id="cool_${r.notification_type}" type="number" placeholder="Cooldown" value="${r.cooldown_hours??""}"><input id="days_${r.notification_type}" type="number" placeholder="Дней" value="${r.days_before_expiry??""}"><input id="gb_${r.notification_type}" type="number" placeholder="ГБ" value="${r.low_traffic_gb??""}"><input id="reset_${r.notification_type}" type="number" placeholder="Дней до reset" value="${r.min_days_to_reset??""}"><button class="button green" onclick="saveRule('${r.notification_type}')">Сохранить</button></div></div>`; }
async function saveRule(t){ const val=id=>{const v=el(`${id}_${t}`).value; return v===""?null:Number(v)}; await api(`/admin/api/notifications/${t}`,{method:"POST",body:JSON.stringify({enabled:el(`en_${t}`).value==="true",send_hour_msk:val("hour"),cooldown_hours:val("cool"),days_before_expiry:val("days"),low_traffic_gb:val("gb"),min_days_to_reset:val("reset")})}); await reloadAll(); }

function renderDiscounts(){ el("view-discounts").innerHTML=`<div class="grid"><div class="card"><h2>Скидки</h2><div class="form"><input id="discTitle" placeholder="Название"><input id="discCode" placeholder="Код, пусто = авто"><select id="discType"><option value="percent">%</option><option value="fixed">₽</option></select><input id="discValue" type="number" placeholder="Размер"><select id="discTarget"><option value="all">Все</option><option value="subscription">Подписки</option><option value="traffic_package">ГБ</option><option value="regular">Regular</option><option value="bypass">Bypass</option></select><input id="discMax" type="number" placeholder="Лимит"><button class="button green" onclick="createDiscount()">Создать</button></div></div><div class="list">${state.discounts.map(d=>`<div class="item"><b>${d.title}</b><small>${d.code||"авто"} · ${d.discount_type} ${d.discount_value} · ${d.target_kind} · ${d.used_count}/${d.max_uses||"∞"} · ${d.is_active?"активна":"выкл"}</small><div class="actions"><button class="button ghost" onclick="toggleDiscount(${d.id},${!d.is_active})">${d.is_active?"Выключить":"Включить"}</button></div></div>`).join("")}</div></div>`; }
async function createDiscount(){ await api("/admin/api/discounts",{method:"POST",body:JSON.stringify({title:el("discTitle").value,code:el("discCode").value||null,discount_type:el("discType").value,discount_value:el("discValue").value,target_kind:el("discTarget").value,max_uses:el("discMax").value?Number(el("discMax").value):null,is_active:true})}); await reloadAll(); }
async function toggleDiscount(id,active){ await api(`/admin/api/discounts/${id}/active`,{method:"POST",body:JSON.stringify({active})}); await reloadAll(); }

load();
