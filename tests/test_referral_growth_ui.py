"""Offline referral UX contracts: no Telegram, provider or database requests."""
from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import database as db
from handlers import callbacks, referral, start
from services import referral_accounting as accounting
from services.referral_program import (
    ENTRY_INTENTS, SHARE_TEXT, build_rules, current_pricing_examples,
    pricing_examples, referral_link,
)

ROOT = Path(__file__).resolve().parents[1]
DISCOUNT = {
    'id': 1, 'name': '20%', 'discount_type': 'percent', 'value': 20,
    'target_type': 'subscription', 'target_code': None,
}


def plain_length(text):
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_data(self, value):
            self.parts.append(value)

    parser = Parser()
    parser.feed(text)
    return len(''.join(parser.parts).encode('utf-16-le')) // 2


def message(payload='/start'):
    return SimpleNamespace(
        text=payload, from_user=SimpleNamespace(id=123, username='tester'),
        chat=SimpleNamespace(id=123), answer=AsyncMock(), answer_photo=AsyncMock(),
        delete=AsyncMock(), edit_text=AsyncMock(), edit_media=AsyncMock(), photo=None,
    )


class ReferralCopyTests(unittest.TestCase):
    def test_saved_destination_whitelist_matches_database_contract(self):
        self.assertEqual(ENTRY_INTENTS, db.REFERRAL_ENTRY_INTENTS)

    def test_phone_caption_leads_with_money_and_keeps_aggregate_stats(self):
        stats = {
            'current_balance': Decimal('1400'), 'total_earned': Decimal('2500'),
            'registered_referrals': 7, 'active_referrals': 3,
        }
        text, keyboard = referral.earning_screen(stats, referral_link('WaySPN_robot', 123))
        self.assertLessEqual(plain_length(text), 1024)
        self.assertLess(text.index('35%'), text.index('Доступно:'))
        self.assertIn('покупок и продлений', text)
        self.assertIn('Зарегистрировались по ссылке: <b>7</b>', text)
        self.assertIn('Друзей с покупками: <b>3</b>', text)
        self.assertIn('осталось 100.00 ₽', text)
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        self.assertTrue(buttons[0].url.startswith('https://t.me/share/url?'))
        self.assertEqual(buttons[1].copy_text.text, referral_link('WaySPN_robot', 123))
        self.assertEqual(
            [button.callback_data for button in buttons[2:]],
            ['referral_spend', 'referral_withdraw', 'referral_history:0', 'referral_rules', 'back_to_menu'],
        )

    def test_caption_stays_below_limit_with_large_wallet_and_refund(self):
        text, _ = referral.earning_screen({
            'current_balance': 999999999.99, 'total_earned': 999999999.99,
            'registered_referrals': 99999999, 'active_referrals': 99999999,
            'refunded_amount': 999999999.99,
        }, 'https://t.me/WaySPN_robot?start=ref_9223372036854775807')
        self.assertLessEqual(plain_length(text), 1024)
        self.assertIn('Учтено возвратов: −999999999.99 ₽', text)

    def test_examples_share_existing_pricing_and_commission_helpers(self):
        examples = pricing_examples([DISCOUNT])
        by_code = {row['tariff_code']: row for row in examples}
        self.assertEqual(
            [by_code['regular_1m'][key] for key in ('price', 'first_commission', 'repeat_commission')],
            [160, 56, 24],
        )
        self.assertEqual(
            [by_code['bypass_1m'][key] for key in ('price', 'first_commission', 'repeat_commission')],
            [240, 84, 36],
        )
        self.assertEqual(by_code['regular_1m']['original_price'], 200)
        self.assertEqual(by_code['bypass_1m']['original_price'], 300)
        rules = build_rules(examples)
        for phrase in ('160 ₽', '56 ₽', '24 ₽', '240 ₽', '84 ₽', '36 ₽', 'перепривязать нельзя', 'фактически оплаченной'):
            self.assertIn(phrase, rules)
        self.assertNotIn('300 ₽', rules)
        self.assertLessEqual(plain_length('<b>Как работает заработок</b>\n\n' + rules), 1024)

    def test_personal_invitation_is_short_with_link_only_once(self):
        self.assertEqual(SHARE_TEXT, 'Лучший VPN:')
        link = referral_link('WaySPN_robot', 123)
        _, keyboard = referral.earning_screen({
            'current_balance': 0, 'total_earned': 0, 'active_referrals': 0,
        }, link)
        payload = parse_qs(urlparse(keyboard.inline_keyboard[0][0].url).query)
        self.assertEqual(payload, {'url': [link], 'text': ['Лучший VPN:']})
        self.assertNotIn(link, payload['text'][0])

    def test_website_explains_shared_telegram_identity_and_separate_web_account(self):
        source = (ROOT / 'static/site/index.html').read_text()
        self.assertIn('покупки в MiniApp этого же Telegram-аккаунта тоже учитываются', source)
        self.assertIn('Отдельный аккаунт и покупки на сайте не объединяются автоматически', source)
        self.assertNotIn('Пример: с оплаты 300 ₽', source)


class ReferralNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Even unpatched optional code is unable to open a real connection.
        self.pool_guard = patch.object(db, 'get_pool', new=AsyncMock(side_effect=AssertionError('offline only')))
        self.pool_guard.start()
        self.addCleanup(self.pool_guard.stop)

    async def test_prices_fetch_active_discounts_once(self):
        with patch.object(db, 'get_active_discounts', new=AsyncMock(return_value=[DISCOUNT])) as discounts:
            examples = await current_pricing_examples()
        discounts.assert_awaited_once_with()
        self.assertEqual([row['price'] for row in examples], [160, 240])

    async def test_rules_screen_uses_live_examples_without_stale_base_price(self):
        msg = message()
        callback = SimpleNamespace(message=msg, answer=AsyncMock())
        with (
            patch.object(db, 'get_active_discounts', new=AsyncMock(return_value=[DISCOUNT])) as discounts,
            patch.object(referral, 'edit_message_with_photo', new=AsyncMock()) as edit,
        ):
            await referral.rules(callback, AsyncMock())
        discounts.assert_awaited_once_with()
        text = edit.await_args.args[1]
        self.assertIn('Оплата 160 ₽', text)
        self.assertIn('<b>56 ₽</b>', text)
        self.assertIn('Оплата 240 ₽', text)
        self.assertNotIn('300 ₽', text)
        self.assertLessEqual(plain_length(text), 1024)

    async def test_only_main_earning_screen_records_view(self):
        bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username='WaySPN_robot')))
        stats = {'current_balance': 2000, 'total_earned': 2500, 'active_referrals': 2, 'registered_referrals': 5}
        with (
            patch.object(db, 'get_referral_stats', new=AsyncMock(return_value=stats)),
            patch.object(accounting, 'record_view', new=AsyncMock()) as views,
            patch.object(referral, 'send_text_with_photo', new=AsyncMock()) as send,
        ):
            await referral.send_referral_entry_screen(message(), 123, bot, 'earn_spend')
            await referral.send_referral_entry_screen(message(), 123, bot, 'earn_withdraw')
            views.assert_not_awaited()
            await referral.send_referral_entry_screen(message(), 123, bot, 'earn')
        views.assert_awaited_once_with(123, 'telegram')
        self.assertEqual([call.args[3] for call in send.await_args_list], [
            'Оплатить с заработанного баланса', 'Вывести деньги', 'Зарабатывать',
        ])

    async def test_deep_action_survives_agreement_channel_and_plain_start_restart(self):
        for intent in sorted(ENTRY_INTENTS):
            with self.subTest(intent=intent):
                saved = {}

                async def set_intent(user_id, value):
                    saved[user_id] = value

                async def get_intent(user_id):
                    return saved.get(user_id)

                async def pop_intent(user_id):
                    return saved.pop(user_id, None)

                bot = AsyncMock()
                bot.get_chat_member.return_value = SimpleNamespace(status='member')
                msg = message('/start ' + intent)
                callback = SimpleNamespace(from_user=msg.from_user, message=msg, bot=bot, answer=AsyncMock())
                with (
                    patch.object(db, 'user_exists', new=AsyncMock(return_value=True)),
                    patch.object(db, 'create_user', new=AsyncMock(return_value=False)),
                    patch.object(db, 'has_accepted_terms', new=AsyncMock(return_value=False)) as accepted,
                    patch.object(db, 'accept_terms', new=AsyncMock()),
                    patch.object(db, 'needs_news_channel_onboarding', new=AsyncMock(return_value=True)) as needs,
                    patch.object(db, 'complete_news_channel_onboarding', new=AsyncMock(return_value=True)),
                    patch.object(db, 'set_referral_entry_intent', new=AsyncMock(side_effect=set_intent)),
                    patch.object(db, 'get_referral_entry_intent', new=AsyncMock(side_effect=get_intent)),
                    patch.object(db, 'pop_referral_entry_intent', new=AsyncMock(side_effect=pop_intent)) as pop,
                    patch.object(start, 'clear_telegram_delivery_blocked', new=AsyncMock()),
                    patch.object(callbacks, 'pending_challenge_for_user', new=AsyncMock(return_value=None)),
                    patch.object(referral, 'send_referral_entry_screen', new=AsyncMock()) as entry,
                    patch.object(start, 'show_main_menu', new=AsyncMock()) as menu,
                    patch.object(callbacks, 'show_main_menu', new=AsyncMock()) as callback_menu,
                ):
                    await start.cmd_start(msg, AsyncMock(), bot)
                    self.assertEqual(saved[123], intent)
                    entry.assert_not_awaited()
                    await callbacks.process_accept_terms(callback, AsyncMock())
                    self.assertEqual(saved[123], intent)
                    accepted.return_value = True
                    # Fresh FSM context represents a restart; destination remains in DB.
                    msg.text = '/start'
                    await start.cmd_start(msg, AsyncMock(), bot)
                    entry.assert_not_awaited()
                    await callbacks.process_check_news_channel(callback, AsyncMock())
                    entry.assert_awaited_once_with(msg, 123, bot, intent)
                    pop.assert_awaited_once_with(123)
                    self.assertNotIn(123, saved)
                    menu.assert_not_awaited()
                    callback_menu.assert_not_awaited()

    async def test_resume_keeps_destination_if_screen_delivery_fails(self):
        with (
            patch.object(db, 'get_referral_entry_intent', new=AsyncMock(return_value='earn_spend')),
            patch.object(db, 'pop_referral_entry_intent', new=AsyncMock()) as pop,
            patch.object(referral, 'send_referral_entry_screen', new=AsyncMock(side_effect=RuntimeError('delivery failed'))),
        ):
            with self.assertRaises(RuntimeError):
                await start.show_pending_referral_entry(message(), 123, AsyncMock())
        pop.assert_not_awaited()

    async def test_valid_inviter_is_passed_to_atomic_insert_despite_old_snapshot(self):
        msg = message('/start ref_456')
        with (
            patch.object(db, 'user_exists', new=AsyncMock(return_value=True)),
            patch.object(db, 'create_user', new=AsyncMock(return_value=False)) as create,
            patch.object(db, 'has_accepted_terms', new=AsyncMock(return_value=True)),
            patch.object(db, 'needs_news_channel_onboarding', new=AsyncMock(return_value=False)),
            patch.object(db, 'get_referral_entry_intent', new=AsyncMock(return_value=None)),
            patch.object(db, 'update_referral_count', new=AsyncMock()) as count,
            patch.object(start, 'clear_telegram_delivery_blocked', new=AsyncMock()),
            patch.object(start, 'show_main_menu', new=AsyncMock()),
        ):
            await start.cmd_start(msg, AsyncMock(), AsyncMock())
        self.assertEqual(create.await_args.args[2], 456)
        count.assert_not_awaited()

    async def test_invalid_self_and_missing_inviter_are_not_saved(self):
        for payload in ('ref_123', 'ref_-456', 'ref_0', 'ref_unknown', 'ref_456',
                        'ref_9223372036854775808', 'ref_' + '9' * 60):
            with (
                self.subTest(payload=payload),
                patch.object(db, 'user_exists', new=AsyncMock(return_value=False)) as exists,
                patch.object(db, 'create_user', new=AsyncMock(return_value=True)) as create,
                patch.object(db, 'has_accepted_terms', new=AsyncMock(return_value=False)),
                patch.object(db, 'update_referral_count', new=AsyncMock()) as count,
                patch.object(start, 'clear_telegram_delivery_blocked', new=AsyncMock()),
            ):
                await start.cmd_start(message('/start ' + payload), AsyncMock(), AsyncMock())
                self.assertIsNone(create.await_args.args[2])
                count.assert_not_awaited()
                if payload != 'ref_456':
                    exists.assert_awaited_once_with(123)

    async def test_valid_new_registration_updates_count_only_after_atomic_insert(self):
        with (
            patch.object(db, 'user_exists', new=AsyncMock(side_effect=[False, True])),
            patch.object(db, 'create_user', new=AsyncMock(return_value=True)) as create,
            patch.object(db, 'has_accepted_terms', new=AsyncMock(return_value=False)),
            patch.object(db, 'update_referral_count', new=AsyncMock()) as count,
            patch.object(start, 'clear_telegram_delivery_blocked', new=AsyncMock()),
        ):
            await start.cmd_start(message('/start ref_456'), AsyncMock(), AsyncMock())
        self.assertEqual(create.await_args.args[2], 456)
        count.assert_awaited_once_with(456)

    async def test_history_formats_confirmed_refund_as_negative_adjustment(self):
        row = {'kind': 'reversal', 'amount': Decimal('28'), 'detail': 'private-invoice',
               'status': 'confirmed', 'created_at': datetime(2026, 10, 10)}
        msg = message()
        callback = SimpleNamespace(from_user=msg.from_user, message=msg, data='referral_history:0', answer=AsyncMock())
        with (
            patch.object(referral.store, 'history', new=AsyncMock(return_value=[row])),
            patch.object(referral, 'edit_message_with_photo', new=AsyncMock()) as edit,
        ):
            await referral.show_history(callback, AsyncMock())
        self.assertIn('−28.00 ₽', edit.await_args.args[1])
        self.assertIn('Корректировка после возврата', edit.await_args.args[1])
        self.assertNotIn('private-invoice', edit.await_args.args[1])


@unittest.skipUnless(shutil.which('node'), 'Node is optional for isolated JavaScript tests')
class MiniAppContracts(unittest.TestCase):
    def test_new_fields_routes_legacy_fallback_and_actual_view_entry(self):
        harness = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('static/miniapp/assets/app.js', 'utf8');
const opened = [], requests = [], panels = new Map();
const element = id => {
  if (!panels.has(id)) panels.set(id,{innerHTML:'',textContent:'',dataset:{},
    classList:{add(){},remove(){},toggle(){}},setAttribute(){},removeAttribute(){},
    addEventListener(){},focus(){},scrollIntoView(){}});
  return panels.get(id);
};
const fixture = {link:'https://t.me/test?start=ref_123',
  bot_earning_url:'https://t.me/test?start=earn',share_text:'Лучший VPN:', registered_referrals:7, active_referrals:3,
  current_balance:1400,total_earned:2500,minimum_withdrawal:1500,
  earning_actions:{home:'https://t.me/test?start=earn',spend:'https://t.me/test?start=earn_spend',withdraw:'https://t.me/test?start=earn_withdraw'},
  pricing_examples:[{price:160,first_commission:56,repeat_commission:24}],
  history:[{kind:'reversal',amount:28,status:'confirmed',detail:'private-invoice'}]};
const context = {
  window:{Telegram:{WebApp:{ready(){},expand(){},openTelegramLink(url){opened.push(url)}}},
    location:{origin:'https://preview.test'},scrollTo(){},addEventListener(){}},
  navigator:{userAgent:'Android',clipboard:{writeText(){return Promise.resolve()}}},
  URL,URLSearchParams,Date,console,
  setTimeout(){return 0},clearTimeout(){},setInterval(){return 0},clearInterval(){},
  document:{getElementById:element,querySelectorAll(){return []},querySelector(){return element('main')},addEventListener(){},body:{}},
  fetch(path){
    requests.push(path);
    const payload = path === '/miniapp/api/me' ? {id:123,first_name:'Тест'}
      : path === '/miniapp/api/subscriptions' ? {subscriptions:[]}
      : path === '/miniapp/api/tariffs' ? {regular:[],bypass:[],traffic_packages:[]}
      : fixture;
    return Promise.resolve({ok:true,json:async()=>payload});
  },
};
vm.createContext(context);
vm.runInContext(source.replace(/\nload\(\);\s*$/,''),context);
context.fixture = fixture;
vm.runInContext("state.referral=fixture;state.currentView='subs';state.authStatus='ready';state.dataStatus={subs:'ready',tariffs:'ready',ref:'ready'}",context);
context.openWalletPurchase();
assert.match(element('view-ref').innerHTML,/Купить новую подписку/);
assert.equal(opened.length,0);
context.backToEarnings();
context.openNativeWithdrawal();
assert.match(element('view-ref').innerHTML,/Сумма, ₽/);
assert.equal(opened.length,0);
context.backToEarnings();
context.shareReferral();
const shared = new URL(opened.at(-1));
assert.equal(shared.searchParams.get('text'),'Лучший VPN:');
assert.equal(shared.searchParams.get('url'),fixture.link);
assert.equal(shared.searchParams.getAll('url').length,1);
context.renderReferral();
const panel = element('view-ref');
assert.match(panel.innerHTML,/Пришли по ссылке<\/span><b>7<\/b>/);
assert.match(panel.innerHTML,/Друзей с покупками<\/span><b>3<\/b>/);
assert.match(panel.innerHTML,/Всего заработано<\/span><b>2\s500 ₽<\/b>/);
assert.match(panel.innerHTML,/160 ₽ → 56 ₽ вам; следующая — 24 ₽/);
assert.match(panel.innerHTML,/Корректировка после возврата/);
assert.match(panel.innerHTML,/−28 ₽/);
assert.ok(!panel.innerHTML.includes('private-invoice'));
assert.ok(!panel.innerHTML.includes('С оплаты 300 ₽'));
context.render = () => {};
context.switchView('ref');context.switchView('ref');context.switchView('subs');context.switchView('ref');
assert.equal(requests.filter(path => path.endsWith('?view=1')).length,2);
context.refreshReferral();
assert.equal(requests.at(-1),'/miniapp/api/referral');
vm.runInContext("state.referral=null;state.dataStatus.ref='ready'",context);
context.renderReferral();
assert.ok(!panel.innerHTML.includes('0 ₽'));
assert.ok(!panel.innerHTML.includes('Пригласить друга'));
assert.ok(!panel.innerHTML.includes(fixture.link));
assert.match(panel.innerHTML,/refreshReferral/);
vm.runInContext('state.referral={...fixture};delete state.referral.registered_referrals;delete state.referral.active_referrals;delete state.referral.total_earned',context);
context.renderReferral();
assert.ok((panel.innerHTML.match(/—/g)||[]).length >= 3);
const missingStatus = {id:1,plan_kind:'regular',type_index:1,traffic:{enabled:false},devices:{limit:5}};
const unknownCard = context.keyButton(missingStatus);
assert.match(unknownCard,/Статус уточняется/);
assert.ok(!/Срок закончился|Истекла|истекла/.test(unknownCard));
assert.ok(!/Срок закончился|Истекла|истекла/.test(context.subscriptionDetailHtml(missingStatus)));
const connections = [];
context.window.Telegram.WebApp.openLink = url => connections.push(url);
delete context.navigator.clipboard;
context.openKeyInHapp(encodeURIComponent('https://preview.test/key'));
assert.equal(connections.at(-1),'https://preview.test/app/open-happ?url=https%3A%2F%2Fpreview.test%2Fkey');
(async () => {
  requests.length=0;
  await context.load();
  assert.ok(requests.includes('/miniapp/api/referral'));
  assert.equal(requests.filter(path => path.endsWith('?view=1')).length,0);
  assert.ok(vm.runInContext("['subs','tariffs','ref'].every(key=>state.dataStatus[key]==='ready')",context));
})().catch(error => {console.error(error);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', harness], cwd=ROOT, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(os.environ.get('PLAYWRIGHT_MODULE'), 'Optional offline screenshot preview')
class OfflinePreviewTests(unittest.TestCase):
    def test_bot_miniapp_site_preview_and_phone_actions(self):
        """Generate reusable fixture-only PNGs; output defaults to /private/tmp.

        PLAYWRIGHT_MODULE=/local/playwright/index.mjs DATABASE_URL='' \
          python3 -m unittest discover -s tests -p test_referral_growth_ui.py
        SPN_REFERRAL_PREVIEW_DIR can select a different local output directory.
        The bot image is a labelled local HTML simulation, not a Telegram shot.
        """
        stats = {
            'current_balance': 1400, 'total_earned': 3000,
            'registered_referrals': 17, 'active_referrals': 12, 'refunded_amount': 28,
        }
        link = referral_link('WaySPN_robot', 123)
        caption, keyboard = referral.earning_screen(stats, link)
        fixture = {
            **stats, 'link': link, 'share_text': SHARE_TEXT, 'minimum_withdrawal': 1500,
            'bot_earning_url': 'https://t.me/WaySPN_robot?start=earn',
            'earning_actions': {
                'home': 'https://t.me/WaySPN_robot?start=earn',
                'spend': 'https://t.me/WaySPN_robot?start=earn_spend',
                'withdraw': 'https://t.me/WaySPN_robot?start=earn_withdraw',
            },
            'pricing_examples': pricing_examples([DISCOUNT]),
            'history': [
                {'kind': 'earning', 'id': 1, 'amount': 56, 'status': 'credited',
                 'detail': 'regular_1m', 'created_at': '2026-10-10T09:00:00Z'},
                {'kind': 'reversal', 'id': 2, 'amount': 28, 'status': 'confirmed',
                 'detail': 'private-fixture-invoice', 'created_at': '2026-10-11T09:00:00Z'},
            ],
            'preview_caption': caption,
            'preview_rows': [[button.text for button in row] for row in keyboard.inline_keyboard],
        }
        harness = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
(async () => {
  const module = process.env.PLAYWRIGHT_MODULE;
  const {chromium} = await import(module.startsWith('file:') ? module : pathToFileURL(module).href);
  const out = process.env.SPN_REFERRAL_PREVIEW_DIR || '/private/tmp/spn-referral-growth-preview';
  await fs.mkdir(out, {recursive:true});
  const referral = JSON.parse(process.env.SPN_REFERRAL_PREVIEW_FIXTURE);
  const browser = await chromium.launch({
    executablePath:process.env.CHROME_EXECUTABLE || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    headless:true,
  });
  const context = await browser.newContext({viewport:{width:390,height:844}});
  const requests = [], errors = [];
  await context.addInitScript(() => {
    window.openedLinks = [];window.copiedValues = [];
    window.Telegram = {WebApp:{initData:'offline-fixture',ready(){},expand(){},
      openTelegramLink(url){window.openedLinks.push(url)}}};
    Object.defineProperty(navigator,'clipboard',{value:{async writeText(value){window.copiedValues.push(value)}}});
  });
  const fixtures = {
    '/miniapp/api/me':{id:123,first_name:'Тест'},
    '/miniapp/api/tariffs':{regular:[],bypass:[],traffic_packages:[]},
    '/miniapp/api/subscriptions':{subscriptions:[]},'/miniapp/api/referral':referral,
    '/site/api/catalog':{regular:[],bypass:[],traffic_packages:[]},'/site/api/config':{},
    '/site/api/me':{login:'offline_test'},'/site/api/subscriptions':{subscriptions:[]},'/site/api/payments':{payments:[]},
  };
  await context.route('**/*',async route => {
    const url = new URL(route.request().url());
    if (url.host !== 'preview.test') return route.fulfill({status:200,contentType:'application/javascript',body:''});
    if (fixtures[url.pathname]) {
      requests.push(url.pathname+url.search);
      return route.fulfill({json:fixtures[url.pathname]});
    }
    const local = url.pathname === '/app' ? 'static/miniapp/index.html'
      : url.pathname === '/account' ? 'static/site/index.html'
      : 'static'+url.pathname.replace(/^\/app\//,'/miniapp/');
    assert.match(local,/^static\/(miniapp|site)\//);
    return route.fulfill({body:await fs.readFile(path.join(process.cwd(),local)),
      contentType:local.endsWith('.js')?'application/javascript':local.endsWith('.css')?'text/css':'text/html'});
  });
  try {
    const page = await context.newPage();
    async function openDetails(label) {
      const summary = page.locator('#view-ref summary').filter({hasText:label});
      if (!await summary.evaluate(node => node.parentElement.open)) await summary.click();
    }
    page.on('pageerror',error => errors.push(error.message));
    await page.goto('https://preview.test/app');
    await page.waitForFunction(() => typeof state !== 'undefined' && state.authStatus === 'ready'
      && ['subs','tariffs','ref'].every(section => state.dataStatus[section] === 'ready'));
    assert.equal(await page.locator('.tabbar .tab').count(),4);
    assert.equal(await page.locator('.tabbar [data-view="home"]').count(),0);
    assert.equal(await page.locator('.tabbar [data-view="subs"]').getAttribute('aria-current'),'page');
    assert.equal(requests.filter(url => url.endsWith('?view=1')).length,0);
    const viewRecorded = page.waitForResponse(response => response.url().endsWith('/miniapp/api/referral?view=1'));
    await page.locator('[data-view="ref"]').click();await viewRecorded;
    await openDetails('Статистика');
    await openDetails('Как это работает');
    await openDetails('История');
    await page.getByText('Оплата 160 ₽ → 56 ₽ вам; следующая — 24 ₽.').waitFor();
    await page.getByText('Оплата 240 ₽ → 84 ₽ вам; следующая — 36 ₽.').waitFor();
    assert.equal(requests.filter(url => url.endsWith('?view=1')).length,1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
    await page.getByRole('button',{name:'Пригласить друга',exact:true}).click();
    await page.getByRole('button',{name:'Скопировать ссылку'}).click();
    await page.getByRole('button',{name:'Вывести',exact:true}).click();
    await page.getByLabel('Сумма, ₽',{exact:true}).waitFor();
    await page.locator('#view-ref').getByRole('button',{name:/Назад/}).click();
    await page.getByRole('button',{name:'Потратить на подписку',exact:true}).click();
    await page.getByRole('button',{name:/^Купить новую подписку/}).waitFor();
    await page.locator('#view-ref').getByRole('button',{name:/Назад/}).click();
    const actions = await page.evaluate(() => ({links:window.openedLinks,copies:window.copiedValues}));
    assert.equal(new URL(actions.links[0]).pathname,'/share/url');
    assert.equal(new URL(actions.links[0]).searchParams.get('text'),'Лучший VPN:');
    assert.equal(new URL(actions.links[0]).searchParams.get('url'),referral.link);
    assert.equal(actions.links.length,1,'native wallet/payout flows do not open Telegram');
    assert.deepEqual(actions.copies,[referral.link]);
    assert.equal(await page.getByText('private-fixture-invoice').count(),0);
    const reversal = page.locator('#referral-history .ref-history-row').filter({hasText:'Корректировка после возврата'});
    await reversal.waitFor();
    await reversal.locator('b').getByText('−28 ₽',{exact:true}).waitFor();
    await page.evaluate(() => {window.scrollTo(0,0);document.getElementById('toast').classList.remove('show')});
    await page.waitForFunction(() => getComputedStyle(document.getElementById('toast')).opacity === '0');
    await page.screenshot({path:path.join(out,'miniapp-390.png'),fullPage:true});
    await page.evaluate(() => {
      state.referral={...state.referral};
      delete state.referral.registered_referrals;
      delete state.referral.active_referrals;
      delete state.referral.total_earned;
      renderReferral();
    });
    await openDetails('Статистика');
    assert.equal(await page.locator('#view-ref').getByText('—',{exact:true}).count(),3);
    await page.evaluate(() => {state.referral=null;renderReferral()});
    assert.equal(await page.locator('#view-ref').getByText('0 ₽',{exact:true}).count(),0);
    assert.equal(await page.locator('#view-ref').getByText(referral.link,{exact:true}).count(),0);
    const retried = page.waitForResponse(response => response.url().endsWith('/miniapp/api/referral'));
    await page.locator('#view-ref').getByRole('button').click();await retried;
    await page.locator('#view-ref').getByText('1 400 ₽',{exact:true}).waitFor();
    assert.equal(requests.filter(url => url.endsWith('?view=1')).length,1);
    await page.goto('https://preview.test/account');
    await page.locator('#mobileMenu').click();
    await page.locator('[data-account-section="earn"]').click();
    await page.getByRole('heading',{name:'💰 Зарабатывать'}).waitFor();
    await page.waitForFunction(() => document.querySelector('.account-sidebar').getBoundingClientRect().right <= 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
    assert.equal(await page.evaluate(() => {
      const card = document.querySelector('#account-earn .earning-card');
      return card.scrollWidth <= card.clientWidth;
    }),true,'Site earning card must not clip its own content');
    assert.equal(await page.evaluate(() => {
      const box = document.querySelector('#account-earn .account-heading');
      return box.scrollWidth <= box.clientWidth;
    }),true,'Site earning header must not clip its own content');
    await page.screenshot({path:path.join(out,'site-390.png'),fullPage:true});
    const banner = (await fs.readFile(path.join(process.cwd(),'pictures/Earning_v1.png'))).toString('base64');
    const escape = value => String(value).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    const botPage = await context.newPage();
    await botPage.setContent(`<!doctype html><html lang="ru"><meta charset="utf-8"><style>
      *{box-sizing:border-box}body{margin:0;padding:12px;background:#17212b;color:#fff;font:15px/1.4 Arial}
      .note{font-size:12px;color:#9eacba}.message{background:#223448;border-radius:14px;overflow:hidden}
      img{display:block;width:100%}.caption{padding:14px;white-space:pre-wrap}blockquote{margin:8px 0;padding:8px;border-left:3px solid #6dbbed;background:#28465d}
      code{font-size:13px;overflow-wrap:anywhere}.row{display:flex;gap:5px;margin-top:5px}.row div{flex:1;text-align:center;background:#2b5278;padding:10px 4px;border-radius:7px;font-size:14px}
      </style><p class="note">Telegram · локальная имитация экрана, тестовые данные</p><div class="message"><img src="data:image/png;base64,${banner}"><div class="caption">${referral.preview_caption}</div></div>
      ${referral.preview_rows.map(row=>'<div class="row">'+row.map(label=>'<div>'+escape(label)+'</div>').join('')+'</div>').join('')}</html>`);
    assert.equal(await botPage.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
    await botPage.screenshot({path:path.join(out,'bot-simulated-390.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('Offline referral previews: '+out);
  } finally {await browser.close()}
})().catch(error => {console.error(error.stack);process.exitCode=1});
'''
        result = subprocess.run(
            ['node', '-e', harness], cwd=ROOT, text=True, capture_output=True, timeout=60,
            env={**os.environ, 'DATABASE_URL': '', 'SPN_REFERRAL_PREVIEW_FIXTURE': json.dumps(fixture)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
