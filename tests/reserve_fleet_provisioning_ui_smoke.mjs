// Exercise the shipped forms and event handlers without a browser dependency.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {randomUUID} from 'node:crypto';

let document;
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = {};
    this.attributes = {};
    this._text = '';
    this.value = '';
    this.disabled = false;
    this.checked = false;
    this.hidden = false;
    this.className = '';
    this.classList = {add:(value) => {this.className += ' ' + value;}};
  }
  set textContent(value) {this._text = String(value); this.children = [];}
  get textContent() {return this._text + this.children.map((child) => child.textContent).join(' ');}
  append(...items) {items.forEach((child) => {child.parentElement = this; this.children.push(child);});}
  replaceChildren(...items) {this.children = []; this._text = ''; this.append(...items);}
  setAttribute(key, value) {this.attributes[key] = value;}
  addEventListener(type, listener) {this.listeners[type] = listener;}
  contains(other) {return other === this || this.children.some((child) => child.contains(other));}
  querySelectorAll(query) {
    const selectors = query.split(',');
    const matches = (child) => selectors.some((selector) => selector.startsWith('.') ?
      child.className.split(' ').includes(selector.slice(1)) : child.tagName === selector.toUpperCase());
    return this.children.flatMap((child) => [...(matches(child) ? [child] : []), ...child.querySelectorAll(query)]);
  }
  reset() {
    this.querySelectorAll('input,select').forEach((input) => {
      input.value = input.defaultValue || '';
      input.checked = false;
    });
  }
  focus() {document.activeElement = this;}
  scrollIntoView() {}
}

const html = fs.readFileSync(new URL('../spn_reserve_fleet/static/index.html', import.meta.url), 'utf8');
const root = new Element('document');
const stack = [root];
const elements = new Map();
for (const match of html.matchAll(/<\/?([a-z][a-z0-9-]*)([^>]*)>/gi)) {
  const [markup, tag, attrs] = match;
  if (markup.startsWith('</')) {if (stack.length > 1) stack.pop(); continue;}
  const element = new Element(tag);
  for (const attribute of attrs.matchAll(/([a-z][a-z0-9-]*)(?:="([^"]*)")?/gi)) {
    const key = attribute[1], value = attribute[2] ?? true;
    if (key === 'id') {element.id = value; elements.set(value, element);}
    else if (key === 'class') element.className = value;
    else if (key === 'value') element.defaultValue = element.value = value;
    else element[key] = value;
  }
  stack.at(-1).append(element);
  if (!['input', 'link', 'meta', 'br', 'hr', 'img'].includes(tag)) stack.push(element);
}
document = {createElement:(tag) => new Element(tag), getElementById:(id) => elements.get(id) || null,
  activeElement:new Element('body')};
const el = (id) => elements.get(id);
const requests = [];
let createResult = null;
let resumeResult = null;
const options = {enabled:true, capabilities:{create_tls:true, create_reality:true, create_cdn:true,
  replace_tls:true, replace_reality:true}, defaults:{transport:'tls', ssh_port:22, dns_suffix:'wayspn.com'},
  squads:[{id:'squad-1', name:'Все пользователи'}, {id:'squad-2', name:'Тест'}]};
const response = (body, ok = true, status = 200) => ({ok, status, json:async () => body});
const context = vm.createContext({document, location:{protocol:'https:'}, crypto:{randomUUID}, console,
  setInterval:() => {}, fetch:async (path, request = {}) => {
    const payload = request.body ? JSON.parse(request.body) : undefined;
    requests.push({path, request, payload});
    if (path === '/api/session') return response({authenticated:false, configured:true, csrf:'test-csrf'});
    if (path === '/api/provisioning/options') return response(options);
    if (path === '/api/provisioning/ssh-key') return response({fingerprint:'SHA256:' + 'A'.repeat(43)});
    if (path === '/api/provisioning/jobs' && request.method === 'POST') return createResult();
    if (path.endsWith('/resume')) return resumeResult(payload);
    if (path === '/api/overview') return response({servers:[], archived_servers:[], events:[]});
    if (path === '/api/provisioning/jobs') return response({jobs:[]});
    throw new Error('Unexpected API path: ' + path);
  }});
vm.runInContext(fs.readFileSync(new URL('../spn_reserve_fleet/static/app.js', import.meta.url), 'utf8'), context);
await new Promise((resolve) => setImmediate(resolve));
vm.runInContext("authenticated = true; csrf = 'test-csrf';", context);
context.applyProvisioningOptions(options);
assert.equal(el('create-server').disabled, false);
context.openProvisioning();
el('provisioning-ip').value = '8.8.8.8';
el('provisioning-name').value = 'Финляндия';
el('provisioning-country').value = 'fi';
el('provisioning-squads').querySelectorAll('input')[0].checked = true;
await context.readSSHKey();
assert.equal(el('ssh-fingerprint').hidden, false);
assert.equal(el('submit-provisioning').disabled, true, 'The fetched key must be explicitly confirmed');
el('trust-ssh-key').checked = true;
context.updateProvisioningSubmit();
assert.equal(el('submit-provisioning').disabled, false);
el('provisioning-ip').value = '1.1.1.1';
el('provisioning-ip').listeners.input();
assert.equal(el('trust-ssh-key').checked, false, 'IP changes must invalidate trust');
assert.equal(el('submit-provisioning').disabled, true);
await context.readSSHKey();
el('trust-ssh-key').checked = true;
context.updateProvisioningSubmit();

// Passwords leave the input before a slow request completes and are absent from persistent UI state.
let resolveCreate;
createResult = () => new Promise((resolve) => {resolveCreate = resolve;});
el('provisioning-password').value = 'private-root-password';
const submission = context.submitProvisioning({preventDefault() {}});
assert.equal(el('provisioning-password').value, '');
const creation = requests.findLast((request) => request.path === '/api/provisioning/jobs' && request.payload);
assert.equal(creation.payload.password, 'private-root-password');
assert.equal(creation.payload.country, 'FI');
assert.equal(creation.payload.transport, 'tls');
assert.deepEqual(creation.payload.squad_ids, ['squad-1']);
assert.equal(creation.payload.host_key_sha256, 'SHA256:' + 'A'.repeat(43));
assert.equal(creation.request.headers['X-CSRF-Token'], 'test-csrf');
assert.equal(creation.request.credentials, 'same-origin');
assert.equal(Object.keys(creation.payload).sort().join(','),
  ['kind','server_id','ip','ssh_port','password','host_key_sha256','domain','name','country','transport','squad_ids','cdn_domain','request_id'].sort().join(','));
resolveCreate(response({id:'job-1', kind:'create', status:'running', domain:'new.wayspn.com', target_ip:'1.1.1.1', stage_label:'Установка VPN', progress:35}));
await submission;
assert.equal(el('provisioning-panel').hidden, true);
assert.ok(el('provisioning-jobs').textContent.includes('Установка VPN'));
assert.ok(!el('dashboard').textContent.includes('private-root-password'));

// A retry after a lost/failed response keeps its request ID, target and SSH pin.
context.openProvisioning();
el('provisioning-ip').value = '8.8.4.4';
el('provisioning-name').value = 'Тест';
el('provisioning-squads').querySelectorAll('input')[1].checked = true;
await context.readSSHKey();
el('trust-ssh-key').checked = true;
createResult = () => response({message:'Повторите ту же установку'}, false, 409);
await context.submitProvisioning({preventDefault() {}});
const firstRetry = requests.findLast((request) => request.path === '/api/provisioning/jobs' && request.payload).payload;
await context.submitProvisioning({preventDefault() {}});
const secondRetry = requests.findLast((request) => request.path === '/api/provisioning/jobs' && request.payload).payload;
assert.equal(firstRetry.request_id, secondRetry.request_id);
assert.equal(firstRetry.ip, secondRetry.ip);
assert.equal(firstRetry.host_key_sha256, secondRetry.host_key_sha256);

// The migration form preserves the existing installation and its immutable SSH port.
const server = {id:'old.wayspn.com', domain:'old.wayspn.com', name:'Рабочий сервер', current_ip:'8.8.8.8',
  ssh_port:2223, transport:'tls', reserves:[]};
context.openProvisioning(server);
assert.equal(el('provisioning-port').value, '2223');
assert.equal(el('provisioning-port').readOnly, true);
assert.equal(el('move-warning').hidden, false);
assert.equal(el('squad-field').hidden, true);
const move = context.provisioningPayload();
assert.equal(move.kind, 'replace');
assert.equal(move.server_id, server.id);
assert.equal(move.domain, null);
assert.equal(move.name, null);
assert.equal(move.transport, null);
assert.equal(move.squad_ids.length, 0);
assert.equal(context.moveReason({...server, pending_ip:'1.1.1.1'}), 'Сначала завершите текущую смену IP.');
assert.ok(context.moveReason({...server, operational_job_id:'in-progress'}));
const locked = context.renderServer({...server, operational_job_id:'in-progress'});
assert.ok(locked.querySelectorAll('button,input').every((input) => input.disabled));
assert.equal(context.renderServer({...server, archived:true}).querySelectorAll('button,input').length, 0);

// CDN cards expose origin SSH and a VPS move, while reserve IP rotation stays unavailable.
const cdnServer = {...server, id:'origin.wayspn.com', domain:'origin.wayspn.com',
  cdn_domain:'client.wayspn.com', transport:'yandex-xhttp-cdn-v1',
  reserves:[{ip:'9.9.9.9', status:'online'}], auto_enabled:true};
let cdnCard = context.renderServer(cdnServer);
assert.ok(cdnCard.textContent.includes('VLESS · CDN'));
assert.ok(!cdnCard.textContent.includes('VLESS · REALITY'));
assert.ok(cdnCard.textContent.includes('CDN: client.wayspn.com'));
assert.ok(cdnCard.textContent.includes('Источник: origin.wayspn.com'));
assert.ok(cdnCard.textContent.includes('Доступность origin по SSH'));
assert.equal(cdnCard.querySelectorAll('input,form').length, 0);
assert.ok(!cdnCard.textContent.includes('РЕЗЕРВНЫЕ IP'));
assert.ok(!cdnCard.textContent.includes('Автоматическая смена'));
assert.ok(!cdnCard.textContent.includes('9.9.9.9'));
assert.equal(cdnCard.querySelectorAll('button').find((button) => button.textContent === 'Перенести на новую VPS').disabled, true);
context.applyProvisioningOptions({...options, capabilities:{...options.capabilities, replace_cdn:true}});
cdnCard = context.renderServer(cdnServer);
assert.deepEqual(cdnCard.querySelectorAll('button').map((button) => button.textContent), ['Проверить SSH', 'Перенести на новую VPS']);
assert.ok(cdnCard.querySelectorAll('button').every((button) => !button.disabled));
assert.equal(context.renderServer({...cdnServer, archived:true}).querySelectorAll('button,input').length, 0);
assert.ok(context.renderServer({...cdnServer, operational_job_id:'cdn-move'}).querySelectorAll('button').every((button) => button.disabled));
context.openProvisioning(cdnServer);
assert.ok(el('move-source').textContent.includes('CDN: client.wayspn.com'));
assert.ok(el('move-source').textContent.includes('Источник: origin.wayspn.com'));
assert.ok(el('move-warning').textContent.includes('CDN-домен'));
const cdnMove = context.provisioningPayload();
assert.equal(cdnMove.server_id, 'origin.wayspn.com');
assert.equal(cdnMove.domain, null);
assert.equal(cdnMove.cdn_domain, null, 'Both domains must be inherited by the backend');
assert.equal(cdnMove.transport, null);
assert.equal(el('provisioning-port').readOnly, true);
context.render({servers:[cdnServer, {...server, reserves:[{ip:'1.1.1.1'}]}], archived_servers:[], events:[]});
assert.equal(el('total-count').textContent, '2');
assert.equal(el('reserve-count').textContent, '1', 'CDN reserves must not be counted as rotation candidates');

// CDN continuation exposes two separate CNAME steps and never requires root credentials by default.
const cdnJob = {id:'cdn-job', kind:'create', transport:'cdn', status:'waiting_cdn', can_resume:true,
  target_ip:'9.9.9.9', domain:'origin.wayspn.com', cdn:{origin_domain:'origin.wayspn.com',
    cdn_domain:'cdn.wayspn.com', transport_path:'/path', certificate_name:'cdn-cert',
    guide_text:'<script>Must remain plain text</script>\nНастройте HTTPS источник.'}};
let card = context.renderJob(cdnJob);
assert.ok(card.textContent.includes(cdnJob.cdn.guide_text));
assert.equal(card.querySelectorAll('script').length, 0);
assert.equal(card.querySelectorAll('input').filter((input) => input.type === 'password').length, 0);
const inputs = card.querySelectorAll('input');
inputs[0].value = 'fp-example.cm.yandexcloud.net';
resumeResult = (payload) => {
  assert.deepEqual(payload, {password:'', certificate_target:'fp-example.cm.yandexcloud.net', cdn_target:null, finish:false});
  return response(cdnJob);
};
await card.querySelectorAll('form')[0].listeners.submit({preventDefault() {}});
card = context.renderJob(cdnJob);
const finishInputs = card.querySelectorAll('input');
finishInputs[1].value = 'example.topology.gslb.yccdn.ru';
finishInputs[2].checked = true;
resumeResult = (payload) => {
  assert.deepEqual(payload, {password:'', certificate_target:null, cdn_target:'example.topology.gslb.yccdn.ru', finish:true});
  return response({...cdnJob, status:'running'});
};
await card.querySelectorAll('form')[0].listeners.submit({preventDefault() {}});
const completed = context.renderJob({...cdnJob, status:'succeeded'});
assert.ok(completed.textContent.includes('Автоматическая смена резервных IP для него пока недоступна'));
assert.equal(completed.querySelectorAll('button,input').length, 0);
const cdnCompletedLink = context.renderJob({...cdnJob, status:'succeeded', result_server_id:cdnServer.id}).querySelectorAll('a')[0];
assert.equal(cdnCompletedLink.href, '#server-origin.wayspn.com');

// Typed credentials are not discarded by background polling; logging out clears them.
const credentialJob = {id:'credential-job', kind:'replace', status:'needs_credentials', needs_password:true, can_resume:true};
vm.runInContext("provisioningJobs = [{id:'credential-job',kind:'replace',status:'needs_credentials',needs_password:true,can_resume:true}];", context);
context.renderJobs(true);
const password = el('provisioning-jobs').querySelectorAll('input')[0];
password.value = 'unsent-secret';
password.focus();
context.renderJobs();
assert.equal(el('provisioning-jobs').querySelectorAll('input')[0], password);
context.showLogin();
assert.equal(password.value, '');
assert.equal(el('provisioning-password').value, '');
context.location.protocol = 'http:';
context.applyProvisioningOptions(options);
assert.equal(el('create-server').disabled, true);
assert.ok(el('provisioning-unavailable').textContent.includes('HTTPS'));
console.log('Provisioning UI: SSH trust, private credentials, jobs, immutable retries, migration and CDN passed');
