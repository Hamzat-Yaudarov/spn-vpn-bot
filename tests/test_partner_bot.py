"""Offline contract tests: returned commands are never sent to Telegram."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from services.partner_bot import PartnerBotHandler, render_menu, render_purchase
from services.partner_commerce_store import CommerceConflict, CommerceNotFound, InvalidCommerce


PROJECT_ID = "244bc71c-2ce8-4d7a-b72f-420424db2507"
OTHER_PROJECT = "132e380c-e519-4d50-885a-58e4dfb35a49"
SUBSCRIPTION_ID = "c7bbbd4b-17b5-43c4-99e1-d7cad9b372ea"


def project(**changes):
    return {
        "id": PROJECT_ID, "owner_tg_id": 777, "title": "My VPN", "price_kopecks": 19900,
        "brand": {}, "support_url": "", "news_url": "",
        "bot": {"id": 90001, "username": "partner_test_bot"}, **changes,
    }


def message(text="/start", user_id=222):
    return {"update_id": 100, "message": {
        "message_id": 5, "from": {"id": user_id, "is_bot": False},
        "chat": {"id": user_id, "type": "private"}, "text": text,
    }}


def callback(data, user_id=222):
    return {"update_id": 101, "callback_query": {
        "id": "callback-id", "from": {"id": user_id, "is_bot": False}, "data": data,
        "message": {"message_id": 10, "from": {"id": 90001, "is_bot": True},
                    "chat": {"id": user_id, "type": "private"}, "text": "Menu"},
    }}


def payload(commands):
    return commands[-1]["payload"]


def buttons(screen):
    return [button for row in screen["reply_markup"]["inline_keyboard"] for button in row]


def pay_button(screen):
    return next(item["callback_data"] for item in buttons(screen) if item.get("callback_data", "").startswith("pay:"))


class FakeStore:
    def __init__(self):
        self.calls = []
        self.customers = {}
        self.subscriptions = {}
        self.orders = {}
        self.requests = {}
        self.current_price = 19900
        self.register_failure = None

    async def register_customer(self, project_id, tg_id, referrer_tg_id=None):
        self.calls.append(("register", project_id, tg_id, referrer_tg_id))
        if self.register_failure:
            raise self.register_failure
        key = (project_id, tg_id)
        if key not in self.customers:
            if referrer_tg_id is not None and (project_id, referrer_tg_id) not in self.customers:
                raise InvalidCommerce("unknown or foreign referrer")
            self.customers[key] = {"referrer_tg_id": referrer_tg_id}
        return self.customers[key]

    async def list_subscriptions(self, project_id, tg_id):
        self.calls.append(("list", project_id, tg_id))
        return deepcopy(self.subscriptions.get((project_id, tg_id), []))

    async def create_order(self, project_id, tg_id, request_key, expected_price_kopecks, *, subscription_id=None):
        self.calls.append(("create", project_id, tg_id, request_key, expected_price_kopecks, subscription_id))
        key = (project_id, tg_id, request_key)
        fingerprint = (expected_price_kopecks, subscription_id)
        if key in self.requests:
            old_fingerprint, order_id = self.requests[key]
            if old_fingerprint != fingerprint:
                raise CommerceConflict("changed input")
            return deepcopy(self.orders[(project_id, tg_id, order_id)])
        if expected_price_kopecks != self.current_price:
            raise CommerceConflict("changed price")
        if subscription_id is not None and not any(
            item["id"] == subscription_id for item in self.subscriptions.get((project_id, tg_id), [])
        ):
            raise CommerceNotFound("foreign subscription")
        order_id = str(uuid4())
        order = {"id": order_id, "project_id": project_id, "tg_id": tg_id,
                 "subscription_id": subscription_id or SUBSCRIPTION_ID,
                 "status": "created", "price_kopecks": expected_price_kopecks,
                 "confirmation_url": None}
        self.orders[(project_id, tg_id, order_id)] = order
        self.requests[key] = (fingerprint, order_id)
        return deepcopy(order)

    async def get_order(self, project_id, tg_id, order_id):
        self.calls.append(("get", project_id, tg_id, order_id))
        if (project_id, tg_id, order_id) not in self.orders:
            raise CommerceNotFound("missing")
        return deepcopy(self.orders[(project_id, tg_id, order_id)])

    async def get_pending_order(self, project_id, tg_id):
        self.calls.append(("pending", project_id, tg_id))
        return next((deepcopy(order) for (pid, tid, _), order in self.orders.items()
                     if pid == project_id and tid == tg_id and order["status"] in {"created", "paid_pending_activation"}), None)


class FakeCheckout:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.failure = None
        self.status = "created"

    async def create_checkout(self, project_id, tg_id, order_id):
        self.calls.append(("create", project_id, tg_id, order_id))
        if self.failure:
            raise self.failure
        return {"id": order_id, "status": self.status, "confirmation_url": "https://pay.example/invoice"}

    async def reconcile(self, project_id, tg_id, order_id):
        self.calls.append(("check", project_id, tg_id, order_id))
        if self.failure:
            raise self.failure
        return {"id": order_id, "status": self.status}


class RenderingTests(unittest.TestCase):
    def test_menu_exact_customer_items_and_owner_only_admin(self):
        expected = ["🛒 Купить подписку", "🔑 Моя подписка", "🎁 Бонусы", "📲 Как подключиться", "🆘 Помощь", "📢 Новости"]
        self.assertEqual([item["text"] for item in buttons(render_menu(project()))], expected)
        self.assertEqual([item["text"] for item in buttons(render_menu(project(), True))], expected + ["⚙️ Админ"])
        help_button = next(item for item in buttons(render_menu(project())) if item["text"] == "🆘 Помощь")
        self.assertEqual(help_button, {"text": "🆘 Помощь", "url": "https://t.me/wayspn_support"})

    def test_brand_is_escaped_not_executable(self):
        screen = render_menu(project(title='<a href="bad">VPN</a>', brand={"welcome_text": "<b>hello & bye</b>"}))
        self.assertIn("&lt;a href=", screen["text"])
        self.assertIn("&lt;b&gt;hello &amp; bye&lt;/b&gt;", screen["text"])
        self.assertNotIn('<a href="bad">', screen["text"])

    def test_subscription_product_and_arbitrary_higher_price(self):
        for price, label in [(19900, "199 ₽"), (24900, "249 ₽"), (24990, "249,90 ₽")]:
            screen = render_purchase(project(price_kopecks=price))
            self.assertIn(label, screen["text"])
            self.assertIn("300 ГБ · 3 устройства", screen["text"])
            self.assertIn("30 дней", screen["text"])
            self.assertIn("антиглушилкой", screen["text"])

    def test_price_validation_and_callback_limit(self):
        for price in [19899, -1, True, 199.0, "19900", 10000001, None]:
            with self.subTest(price=price), self.assertRaises(ValueError):
                render_purchase(project(price_kopecks=price))
        for price in [19900, 10000000]:
            screen = render_purchase(project(price_kopecks=price), SUBSCRIPTION_ID)
            for item in buttons(screen):
                self.assertLessEqual(len(item.get("callback_data", "").encode()), 64)

    def test_one_confirmation_button_has_stable_request_identity(self):
        request = str(uuid4())
        left = render_purchase(project(), SUBSCRIPTION_ID, request_key=request)
        right = render_purchase(project(), SUBSCRIPTION_ID, request_key=request)
        self.assertEqual(pay_button(left), pay_button(right))
        self.assertNotEqual(pay_button(render_purchase(project())), pay_button(render_purchase(project())))


class BrandedSubscriptionBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_customer_link_uses_gateway_and_failure_never_falls_back_to_source(self):
        store = FakeStore()
        store.subscriptions[(PROJECT_ID, 222)] = [{"id": SUBSCRIPTION_ID,
            "expires_at": datetime(2040, 1, 1, tzinfo=timezone.utc),
            "subscription_url": "https://source.example.test/PRIVATE_UPSTREAM_CREDENTIAL"}]
        pages = AsyncMock()
        pages.link_for_customer.return_value = "https://my-vpn.partners.example.test/s/branded-capability"
        handler = PartnerBotHandler(store, subscription_pages=pages)
        screen = await handler._subscriptions(project(), 222)
        urls = [item.get("url") for item in buttons(screen)]
        self.assertIn(pages.link_for_customer.return_value, urls)
        pages.link_for_customer.assert_awaited_once_with(PROJECT_ID, 222, SUBSCRIPTION_ID)
        pages.link_for_customer.side_effect = RuntimeError("PRIVATE SECRET")
        screen = await handler._subscriptions(project(), 222)
        self.assertNotIn("PRIVATE", json.dumps(screen))
        self.assertIn("Ссылка подключения пока недоступна", screen["text"])


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FakeStore()
        self.checkout = FakeCheckout(self.store)
        self.handler = PartnerBotHandler(self.store, self.checkout, manager_url="https://admin.example/partners")

    async def test_start_owner_and_nonowner_admin_is_checked_again(self):
        owner = payload(await self.handler.handle(project(), message(user_id=777)))
        normal = payload(await self.handler.handle(project(), message()))
        self.assertIn("⚙️ Админ", [item["text"] for item in buttons(owner)])
        self.assertNotIn("⚙️ Админ", [item["text"] for item in buttons(normal)])
        forged = payload(await self.handler.handle(project(), callback("admin")))
        self.assertEqual(forged["text"], "Действие недоступно.")
        owner_admin = payload(await self.handler.handle(project(), callback("admin", 777)))
        self.assertEqual(buttons(owner_admin)[0]["url"], "https://admin.example/partners")

    async def test_returned_commands_are_descriptions_only(self):
        sent = await self.handler.handle(project(), message())
        edited = await self.handler.handle(project(), callback("menu"))
        self.assertEqual(sent[0]["method"], "sendMessage")
        self.assertEqual([cmd["method"] for cmd in edited], ["answerCallbackQuery", "editMessageText"])
        self.assertEqual(payload(edited)["chat_id"], 222)
        self.assertEqual(payload(edited)["message_id"], 10)
        self.assertNotIn("token", json.dumps(edited))

    async def test_rejects_groups_bots_forged_identity_business_inline_and_forwarded(self):
        invalid = [None, {}, {"message": {}}, {"message": {}, "callback_query": {}},
                   {"business_message": message()["message"]}, {"edited_message": message()["message"]}]
        for change in [lambda m: m["chat"].update(type="group"),
                       lambda m: m["chat"].update(id=333),
                       lambda m: m["from"].update(is_bot=True),
                       lambda m: m["from"].update(id=True),
                       lambda m: m.update(forward_origin={"type": "user"}),
                       lambda m: m.update(forward_from={"id": 777}),
                       lambda m: m.update(business_connection_id="x"),
                       lambda m: m.update(via_bot={"id": 777})]:
            body = message()
            change(body["message"])
            invalid.append(body)
        for change in [lambda c: c.update(inline_message_id="x"),
                       lambda c: c["message"]["chat"].update(id=444),
                       lambda c: c["message"]["from"].update(id=99999),
                       lambda c: c["message"].update(message_id=True),
                       lambda c: c.update(data="x" * 65),
                       lambda c: c.update(data={"action": "buy"})]:
            body = callback("menu")
            change(body["callback_query"])
            invalid.append(body)
        for update in invalid:
            with self.subTest(update=update):
                self.assertEqual(await self.handler.handle(project(), update), [])
        self.assertEqual(self.store.calls, [])

    async def test_start_existing_referrer_is_same_project_and_immutable(self):
        await self.handler.handle(project(), message(user_id=100))
        await self.handler.handle(project(), message("/start ref_100"))
        self.assertEqual(self.store.customers[(PROJECT_ID, 222)]["referrer_tg_id"], 100)
        await self.handler.handle(project(), message(user_id=333))
        await self.handler.handle(project(), message("/start ref_333"))
        self.assertEqual(self.store.customers[(PROJECT_ID, 222)]["referrer_tg_id"], 100)

    async def test_invalid_foreign_and_self_referrer_do_not_prevent_start(self):
        for start in ["/start ref_999", "/start ref_222", "/start ref_0", "/start ref_-1", "/start ref_true"]:
            store = FakeStore()
            store.customers[(OTHER_PROJECT, 999)] = {}
            result = await PartnerBotHandler(store).handle(project(), message(start))
            self.assertIn("My VPN", payload(result)["text"])
            self.assertIsNone(store.customers[(PROJECT_ID, 222)]["referrer_tg_id"])

    async def test_other_bots_command_and_nonstart_referral(self):
        self.assertEqual(await self.handler.handle(project(), message("/start@someone_else_bot ref_100")), [])
        self.assertFalse(self.store.calls)
        await self.handler.handle(project(), message("hello ref_100"))
        self.assertIsNone(self.store.customers[(PROJECT_ID, 222)]["referrer_tg_id"])
        self.assertTrue(await self.handler.handle(project(), message("/start@PARTNER_TEST_BOT")))

    async def test_missing_subscription_is_not_loading_error(self):
        result = payload(await self.handler.handle(project(), callback("subscriptions")))
        self.assertIn("пока нет подписки", result["text"])
        self.store.register_failure = RuntimeError("SECRET database password")
        # Runtime failures propagate to the webhook's redacted 503 response,
        # not a fictional empty screen or ACK200 for an unsaved registration.
        with self.assertRaises(RuntimeError):
            await self.handler.handle(project(), callback("subscriptions"))

    async def test_purchase_review_and_confirmation_with_canonical_ids(self):
        review = payload(await self.handler.handle(project(), callback("buy")))
        result = payload(await self.handler.handle(project(), callback(pay_button(review))))
        self.assertIn("199 ₽", result["text"])
        self.assertEqual(buttons(result)[0]["url"], "https://pay.example/invoice")
        self.assertEqual(len(self.store.orders), 1)
        create = next(item for item in self.store.calls if item[0] == "create")
        self.assertEqual(create[1:3], (PROJECT_ID, 222))
        self.assertEqual(str(__import__("uuid").UUID(create[3])), create[3])
        self.assertEqual(create[4:], (19900, None))

    async def test_lost_checkout_response_reuses_same_order_and_price(self):
        review = payload(await self.handler.handle(project(), callback("buy")))
        confirmation = pay_button(review)
        self.checkout.failure = TimeoutError("SECRET provider token")
        failed = payload(await self.handler.handle(project(), callback(confirmation)))
        self.assertNotIn("SECRET", failed["text"])
        self.assertIn("Не удалось получить ссылку", failed["text"])
        self.store.current_price = 24900
        self.checkout.failure = None
        result = payload(await self.handler.handle(project(price_kopecks=24900), callback(confirmation)))
        self.assertIn("199 ₽", result["text"])
        self.assertEqual(len(self.store.orders), 1)
        self.assertEqual(self.checkout.calls[0][3], self.checkout.calls[1][3])

    async def test_changed_price_requires_fresh_confirmation_without_order(self):
        review = render_purchase(project())
        self.store.current_price = 24900
        result = payload(await self.handler.handle(project(price_kopecks=24900), callback(pay_button(review))))
        self.assertIn("изменились", result["text"])
        self.assertIn("249 ₽", result["text"])
        self.assertFalse(self.store.orders)
        self.assertFalse(self.checkout.calls)
        self.assertNotEqual(pay_button(review), pay_button(result))

    async def test_disabled_checkout_never_claims_paid_or_granted(self):
        handler = PartnerBotHandler(self.store)
        result = payload(await handler.handle(project(), callback(pay_button(render_purchase(project())))))
        self.assertIn("Оплата ещё не подключена, деньги не списаны", result["text"])
        self.assertNotIn("активирована", result["text"])
        self.assertNotIn("Оплатить", [item["text"] for item in buttons(result)])
        self.assertFalse(self.checkout.calls)

    async def test_paid_pending_and_activated_are_distinct(self):
        self.checkout.status = "paid_pending_activation"
        result = payload(await self.handler.handle(project(), callback(pay_button(render_purchase(project())))))
        self.assertIn("Оплата сохранена, активация повторяется", result["text"])
        self.assertIn("Повторно платить не нужно", result["text"])
        self.assertNotIn("Оплатить", [item["text"] for item in buttons(result)])
        check = next(item["callback_data"] for item in buttons(result) if item.get("callback_data", "").startswith("check:"))
        self.checkout.status = "active"
        result = payload(await self.handler.handle(project(), callback(check)))
        self.assertIn("Подписка активирована", result["text"])

    async def test_foreign_order_never_reaches_provider(self):
        own = payload(await self.handler.handle(project(), callback(pay_button(render_purchase(project())))))
        check = next(item["callback_data"] for item in buttons(own) if item.get("callback_data", "").startswith("check:"))
        self.checkout.calls.clear()
        foreign = payload(await self.handler.handle(project(id=OTHER_PROJECT), callback(check)))
        self.assertIn("не найдены", foreign["text"])
        other_user = payload(await self.handler.handle(project(), callback(check, 333)))
        self.assertIn("не найдены", other_user["text"])
        self.assertFalse(self.checkout.calls)

    async def test_subscriptions_active_expired_and_pending_actual_dates(self):
        subscription = {"id": SUBSCRIPTION_ID, "project_id": PROJECT_ID, "tg_id": 222,
                        "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc),
                        "traffic_limit_bytes": 340 * 1024**3, "device_limit": 5,
                        "subscription_url": "https://sub.example/private-link"}
        self.store.subscriptions[(PROJECT_ID, 222)] = [subscription]
        result = payload(await self.handler.handle(project(), callback("subscriptions")))
        self.assertIn("Подписка активна", result["text"])
        self.assertIn("Лимиты: 340 ГБ · Устройства: 5", result["text"])
        self.assertEqual(buttons(result)[0]["url"], "https://sub.example/private-link")
        self.assertNotIn("подключён", result["text"])
        subscription["expires_at"] = "2020-01-01T00:00:00Z"
        result = payload(await self.handler.handle(project(), callback("subscriptions")))
        self.assertIn("Срок подписки истёк", result["text"])
        self.assertFalse(any("url" in item for item in buttons(result)))
        subscription["expires_at"] = None
        subscription["desired_expires_at"] = "2099-01-01T00:00:00Z"
        result = payload(await self.handler.handle(project(), callback("subscriptions")))
        self.assertIn("Оплата сохранена", result["text"])
        self.assertNotIn("Продлить", [item["text"] for item in buttons(result)])

    async def test_created_unpaid_slot_is_not_called_paid_and_recovers_order(self):
        result = payload(await self.handler.handle(project(), callback(pay_button(render_purchase(project())))))
        self.store.subscriptions[(PROJECT_ID, 222)] = [{"id": SUBSCRIPTION_ID, "expires_at": None, "desired_expires_at": None}]
        restored = payload(await self.handler.handle(project(), callback("buy")))
        self.assertIn("Оплата подписки", restored["text"])
        self.assertNotIn("Оплата сохранена", restored["text"])
        self.assertIn("199 ₽", restored["text"])
        self.assertEqual(len(self.store.orders), 1)

    async def test_renew_review_preserves_selected_subscription(self):
        self.store.subscriptions[(PROJECT_ID, 222)] = [{"id": SUBSCRIPTION_ID, "expires_at": "2099-01-01T00:00:00Z"}]
        subs = payload(await self.handler.handle(project(), callback("subscriptions")))
        renew = next(item["callback_data"] for item in buttons(subs) if item.get("callback_data", "").startswith("renew:"))
        review = payload(await self.handler.handle(project(), callback(renew)))
        self.assertIn("Продление подписки", review["text"])
        await self.handler.handle(project(), callback(pay_button(review)))
        create = next(item for item in self.store.calls if item[0] == "create")
        self.assertEqual(create[-1], SUBSCRIPTION_ID)
        self.store.current_price = 24900
        changed = payload(await self.handler.handle(project(price_kopecks=24900), callback(pay_button(render_purchase(project(), SUBSCRIPTION_ID)))))
        self.assertIn("Продление подписки", changed["text"])

    async def test_foreign_subscription_renewal_denied(self):
        self.store.subscriptions[(OTHER_PROJECT, 222)] = [{"id": SUBSCRIPTION_ID, "expires_at": "2099-01-01T00:00:00Z"}]
        # Obtain a valid compact callback without relying on private helpers.
        raw = pay_button(render_purchase(project(), SUBSCRIPTION_ID)).split(":")[-1]
        denied = payload(await self.handler.handle(project(), callback("renew:" + raw)))
        self.assertIn("не найдена", denied["text"])
        self.assertFalse(self.store.orders)
        self.assertFalse(self.checkout.calls)

    async def test_help_news_url_allowlist_and_unavailable_features(self):
        for key, action in [("support_url", "help"), ("news_url", "news")]:
            result = payload(await self.handler.handle(project(**{key: "https://t.me/partner_news"}), callback(action)))
            self.assertEqual(buttons(result)[0]["url"], "https://t.me/partner_news")
            for url in ["javascript:alert(1)", "http://example.com", "https://u:p@example.com", "https://example.com:444", "https://example.com\\evil"]:
                result = payload(await self.handler.handle(project(**{key: url}), callback(action)))
                urls = [item["url"] for item in buttons(result) if "url" in item]
                self.assertEqual(urls, ["https://t.me/wayspn_support"] if action == "help" else [])
        bonus = payload(await self.handler.handle(project(), callback("bonuses")))
        self.assertIn("+7 дней", bonus["text"])
        self.assertIn("пока не включена", bonus["text"])
        for action in ["traffic", "devices"]:
            result = payload(await self.handler.handle(project(), callback(action)))
            self.assertIn("Заказ не создан, деньги не списаны", result["text"])
        self.assertFalse(self.store.orders)

    async def test_unrecognized_update_never_echoes_credentials_or_raw_text(self):
        secret = "<secret>fake-token-value</secret>"
        result = payload(await self.handler.handle(project(), callback(secret)))
        self.assertNotIn(secret, result["text"])
        self.assertEqual(result["text"], "Действие недоступно.")


if __name__ == "__main__":
    unittest.main()
