// Exercise the actual UI renderer using only Node built-ins and an offline DOM.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this._text = '';
    this.disabled = false;
    this.hidden = false;
    this.className = '';
    this.classList = {add: (value) => {this.className += ' ' + value;}};
  }
  set textContent(value) {this._text = String(value); this.children = [];}
  get textContent() {return this._text + this.children.map((child) => child.textContent).join(' ');}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children = children; this._text = '';}
  setAttribute(name, value) {this.attributes[name] = value;}
  addEventListener(name, action) {this.listeners[name] = action;}
  querySelectorAll(selectors) {
    const tags = selectors.split(',').map((tag) => tag.toUpperCase());
    return this.children.flatMap((child) => [
      ...(tags.includes(child.tagName) ? [child] : []), ...child.querySelectorAll(selectors),
    ]);
  }
}

const html = fs.readFileSync(new URL('../spn_reserve_fleet/static/index.html', import.meta.url), 'utf8');
const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map((match) => [match[1], new Element('div')]));
const context = vm.createContext({
  document: {createElement: (tag) => new Element(tag), getElementById: (id) => elements.get(id) || null,
    activeElement: new Element('body')},
  fetch: async () => ({ok: true, json: async () => ({authenticated: false, configured: true, csrf: 'offline-csrf'})}),
  setInterval: () => {}, console,
});
vm.runInContext(fs.readFileSync(new URL('../spn_reserve_fleet/static/app.js', import.meta.url), 'utf8'), context);

const pending = {id: 'current.example.com', domain: 'current.example.com', name: 'Германия',
  current_ip: '8.8.8.8', pending_ip: '1.1.1.1', status: 'pending', auto_enabled: false,
  last_error: 'Настройки отличаются от журнала', last_error_log_id: 'a'.repeat(32),
  reserves: [{ip: '1.1.1.1', status: 'online'}, {ip: '9.9.9.9', status: 'unknown'}]};
const card = context.renderServer(pending);
assert.ok(card.textContent.includes('Продолжить смену на 1.1.1.1'));
assert.ok(card.textContent.includes(pending.last_error));
assert.ok(card.textContent.includes('Журнал операции: aaaaaaaa'));
const buttons = card.querySelectorAll('button');
assert.equal(buttons.find((button) => button.textContent === 'Продолжить').disabled, false);
assert.equal(buttons.find((button) => button.textContent === 'Назначить').disabled, true);
assert.ok(buttons.filter((button) => button.textContent === '×').every((button) => button.disabled));
assert.equal(buttons.find((button) => button.textContent === '+ Добавить').disabled, true);
assert.equal(card.querySelectorAll('input').find((input) => input.type === 'checkbox').disabled, true);
assert.equal(buttons.find((button) => button.textContent === 'Проверить SSH').disabled, false);

const archived = {...pending, id: 'legacy.example.com', domain: 'legacy.example.com', archived: true,
  archive_reason: 'Заменена новой установкой', replacement_id: pending.domain};
const history = context.renderServer(archived);
assert.equal(history.querySelectorAll('button,input').length, 0, 'Archive must have no operational controls');
assert.ok(history.textContent.includes('В архиве'));
assert.ok(history.textContent.includes(archived.archive_reason));
assert.ok(history.textContent.includes('В истории сохранена незавершённая смена на 1.1.1.1'));
assert.equal(history.querySelectorAll('a')[0].href, '#server-' + pending.domain);

elements.get('archive-section').open = true;
context.render({servers: [pending], archived_servers: [archived], events: [
  {server_id: archived.domain, message: 'Прежняя операция сохранена', time: 1234567890},
]});
assert.equal(elements.get('total-count').textContent, '1');
assert.equal(elements.get('attention-count').textContent, '1');
assert.equal(elements.get('reserve-count').textContent, '2');
assert.equal(elements.get('archive-count').textContent, '1');
assert.equal(elements.get('archive-section').hidden, false);
assert.equal(elements.get('archive-section').open, true, 'Refresh must preserve the expanded history');
assert.ok(elements.get('events').textContent.includes(archived.domain));
context.render({servers: [{...pending, pending_ip: null, last_error: null}], archived_servers: [], events: []});
assert.equal(elements.get('archive-section').hidden, true);
const recoveredButtons = elements.get('servers').querySelectorAll('button');
assert.ok(recoveredButtons.filter((button) => button.textContent === 'Назначить').every((button) => !button.disabled));
assert.equal(elements.get('servers').querySelectorAll('input').find((input) => input.type === 'checkbox').disabled, false);
console.log('Reserve UI: pending recovery, read-only archive, counters and history passed');
