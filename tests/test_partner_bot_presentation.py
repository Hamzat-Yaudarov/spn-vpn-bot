"""Customer bot presentation and main-SPN guide compatibility, entirely offline."""

from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, patch

from services.connection_instructions import (
    ANDROID_PLATFORM, IPHONE_PLATFORM, connection_app_button_text, connection_app_url,
)
from services.partner_bot import PartnerBotHandler, render_connection, render_menu
from services.partner_brand_defaults import DEFAULT_CUSTOMER_SUPPORT_URL, brand_defaults
from services.partner_store import _public
from test_partner_bot import FakeCheckout, FakeStore, buttons, callback, message, payload, project
from test_partner_store import project_row


CUSTOM_SUPPORT = "https://t.me/MyPartnerSupport"
OPERATOR_SUPPORT = "https://t.me/vpnislam"


class ConnectionPresentationTests(unittest.TestCase):
    def test_platform_picker_has_clear_device_subscription_and_menu_navigation(self):
        screen = render_connection(project())
        self.assertEqual([item.get("callback_data") for item in buttons(screen)],
                         ["connect:android", "connect:iphone", "subscriptions", "menu"])
        self.assertIn("Выберите устройство", screen["text"])
        self.assertIn("Купить подписку", screen["text"])
        self.assertFalse(any("url" in item for item in buttons(screen)))

    def test_platform_download_buttons_are_exact_existing_main_spn_store_links(self):
        expected = {
            ANDROID_PLATFORM: ("Happ Plus", "https://play.google.com/store/apps/details?id=com.happproxy"),
            IPHONE_PLATFORM: ("INCY", "https://apps.apple.com/ru/app/incy/id6756943388"),
        }
        for platform, (app, official) in expected.items():
            with self.subTest(platform=platform):
                screen = render_connection(project(), platform)
                self.assertEqual(buttons(screen)[0], {
                    "text": connection_app_button_text(platform), "url": official,
                })
                self.assertEqual(connection_app_url(platform), official)
                self.assertIn(app, screen["text"])
                self.assertIn("«Моя подписка» → «Открыть подписку»", screen["text"])
                self.assertIn("На странице подписки", screen["text"])
                self.assertIn("из буфера", screen["text"])
                self.assertIn("Включите VPN", screen["text"])
                self.assertEqual(screen["parse_mode"], "HTML")
                self.assertTrue(screen["disable_web_page_preview"])
                self.assertEqual([item["callback_data"] for item in buttons(screen) if "callback_data" in item],
                                 ["subscriptions", "connect", "menu"])
                self.assertEqual(buttons(screen)[3]["url"], DEFAULT_CUSTOMER_SUPPORT_URL)
                self.assertNotIn("happ://", json.dumps(screen))
                self.assertNotIn("incy://", json.dumps(screen))

    def test_platform_specific_steps_do_not_mix_clients_or_permissions(self):
        android = render_connection(project(), "android")["text"]
        iphone = render_connection(project(), "iphone")["text"]
        self.assertIn("в правом верхнем углу", android)
        self.assertIn("Вставить из буфера", android)
        self.assertNotIn("INCY", android)
        self.assertIn("VPN-конфигурации", iphone)
        self.assertNotIn("Happ", iphone)

    def test_unknown_platforms_fail_closed_not_to_another_app(self):
        for platform in ("windows", "macos", "", "Android", "../../android", "<script>", True, 1):
            with self.subTest(platform=platform), self.assertRaises(ValueError):
                render_connection(project(), platform)

    def test_pure_render_does_not_read_files_construct_handler_or_mutate_project(self):
        row = project(support_url=CUSTOM_SUPPORT, subscription_url="https://source.test/PRIVATE_VPN",
                      encrypted_token="PRIVATE_TOKEN", secret="PRIVATE_SECRET")
        original = deepcopy(row)
        with patch("builtins.open", side_effect=AssertionError("no filesystem")), \
             patch.object(PartnerBotHandler, "__init__", side_effect=AssertionError("no service construction")):
            left, right = render_connection(row, "android"), render_connection(row, "android")
        self.assertEqual(row, original)
        self.assertEqual(left, right)
        self.assertNotIn("PRIVATE", json.dumps(left))
        self.assertEqual(buttons(left)[3]["url"], CUSTOM_SUPPORT)

    def test_project_html_and_private_foreign_links_cannot_inject_instructions(self):
        attack = '<script>alert("private")</script>'
        row = project(title=attack, brand={"welcome_text": attack},
                      subscription_url="https://foreign.test/SECRET_CAPABILITY", encrypted_token="SECRET_TOKEN")
        for platform in (None, "android", "iphone"):
            serialized = json.dumps(render_connection(row, platform), ensure_ascii=False)
            self.assertNotIn(attack, serialized)
            self.assertNotIn("SECRET", serialized)
            self.assertNotIn("foreign.test", serialized)
        menu = render_menu(row)
        self.assertIn("&lt;script&gt;", menu["text"])
        self.assertNotIn("<script>", menu["text"])

    def test_all_callback_destinations_fit_telegram_64_byte_contract(self):
        for screen in (render_menu(project()), render_menu(project(), True),
                       *(render_connection(project(), platform) for platform in (None, "android", "iphone"))):
            for item in buttons(screen):
                if "callback_data" in item:
                    self.assertTrue(1 <= len(item["callback_data"].encode("utf-8")) <= 64)


class SupportAndBrandPresentationTests(unittest.TestCase):
    def test_default_customer_help_is_direct_url_not_callback_and_never_operator_contact(self):
        self.assertEqual(DEFAULT_CUSTOMER_SUPPORT_URL, "https://t.me/wayspn_support")
        for row in (project(), project(support_url=None), project(support_url="")):
            help_button = next(item for item in buttons(render_menu(row)) if "Помощь" in item["text"])
            self.assertEqual(help_button["url"], DEFAULT_CUSTOMER_SUPPORT_URL)
            self.assertNotIn("callback_data", help_button)
            self.assertNotEqual(help_button["url"], OPERATOR_SUPPORT)

    def test_serialized_old_blank_support_gets_safe_default_without_mutating_database_row(self):
        raw = project_row(support_url="", bot_id=90001, bot_username="partner_test_bot")
        saved = deepcopy(raw)
        serialized = _public(raw)
        self.assertEqual(raw, saved)
        self.assertEqual(serialized["support_url"], DEFAULT_CUSTOMER_SUPPORT_URL)
        for row in (raw, serialized):
            help_button = next(item for item in buttons(render_menu(row)) if "Помощь" in item["text"])
            self.assertEqual(help_button["url"], DEFAULT_CUSTOMER_SUPPORT_URL)
            self.assertEqual(buttons(render_connection(row, "iphone"))[3]["url"], DEFAULT_CUSTOMER_SUPPORT_URL)

    def test_existing_custom_support_welcome_and_brand_are_not_replaced(self):
        row = project(title="Свой VPN", support_url=CUSTOM_SUPPORT,
                      brand={"welcome_text": "Наше приветствие & условия", "accent_color": "#abcdef"})
        before = deepcopy(row)
        screen = render_menu(row)
        self.assertIn("Свой VPN", screen["text"])
        self.assertIn("Наше приветствие &amp; условия", screen["text"])
        self.assertNotIn(brand_defaults("VPN")["welcome_text"], screen["text"])
        self.assertEqual(next(item["url"] for item in buttons(screen) if "Помощь" in item["text"]), CUSTOM_SUPPORT)
        self.assertEqual(buttons(render_connection(row, "android"))[3]["url"], CUSTOM_SUPPORT)
        self.assertEqual(row, before)
        self.assertEqual(_public(project_row(support_url=CUSTOM_SUPPORT))["support_url"], CUSTOM_SUPPORT)

    def test_unsafe_support_is_replaced_by_safe_customer_default_not_rendered(self):
        for unsafe in ("javascript:alert(1)", "http://insecure.test", "https://user:secret@private.test", "https://private.test:444", "https://private.test\\evil"):
            row = project(support_url=unsafe)
            self.assertEqual(buttons(render_connection(row, "android"))[3]["url"], DEFAULT_CUSTOMER_SUPPORT_URL)
            self.assertNotIn(unsafe, json.dumps(render_menu(row)))


class ConnectionHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FakeStore()
        self.checkout = FakeCheckout(self.store)
        self.pages = AsyncMock()
        self.handler = PartnerBotHandler(self.store, self.checkout, subscription_pages=self.pages,
                                         workspace_origin="https://partners.example.test")

    async def test_instructions_only_register_current_customer_never_load_links_or_invoice(self):
        for action in ("connect", "connect:android", "connect:iphone"):
            self.store.calls.clear()
            row = project(subscription_url="https://foreign.test/PRIVATE_LINK", encrypted_token="PRIVATE_TOKEN")
            result = await self.handler.handle(row, callback(action))
            self.assertEqual([command["method"] for command in result], ["answerCallbackQuery", "editMessageText"])
            self.assertEqual(self.store.calls, [("register", row["id"], 222, None)])
            self.assertEqual(self.checkout.calls, [])
            self.assertEqual(self.pages.mock_calls, [])
            self.assertNotIn("PRIVATE", json.dumps(result))

    async def test_platform_back_returns_picker_and_menu_without_creating_purchase(self):
        selected = payload(await self.handler.handle(project(), callback("connect:iphone")))
        back = next(item["callback_data"] for item in buttons(selected) if "Другое устройство" in item["text"])
        self.assertEqual(back, "connect")
        picker = payload(await self.handler.handle(project(), callback(back)))
        self.assertEqual([item.get("callback_data") for item in buttons(picker)][:2], ["connect:android", "connect:iphone"])
        home = next(item["callback_data"] for item in buttons(picker) if "В меню" in item["text"])
        menu = payload(await self.handler.handle(project(), callback(home)))
        self.assertIn("Выберите нужный раздел", menu["text"])
        self.assertEqual(self.checkout.calls, [])
        self.assertEqual(self.store.orders, {})

    async def test_legacy_help_callback_still_works_and_platform_support_remains_separate(self):
        for support in ("", CUSTOM_SUPPORT):
            row = project(support_url=support)
            help_screen = payload(await self.handler.handle(row, callback("help")))
            help_urls = [item["url"] for item in buttons(help_screen) if "url" in item]
            self.assertEqual(help_urls, [support or DEFAULT_CUSTOMER_SUPPORT_URL])
            admin = payload(await self.handler.handle(row, callback("admin", user_id=777)))
            operator = next(item for item in buttons(admin) if item["text"] == "Поддержка платформы")
            self.assertEqual(operator["url"], OPERATOR_SUPPORT)
            self.assertNotIn(OPERATOR_SUPPORT, help_urls)

    async def test_platform_callback_from_another_bot_has_no_effect(self):
        forged = callback("connect:android")
        forged["callback_query"]["message"]["from"]["id"] += 1
        self.assertEqual(await self.handler.handle(project(), forged), [])
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.checkout.calls, [])
        self.assertEqual(self.pages.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
