"""New-project defaults and initial Telegram profile setup, without external I/O."""
from copy import deepcopy
import os
import unittest
from unittest.mock import AsyncMock

from services.partner_brand_defaults import brand_defaults, telegram_profile
from services.partner_launcher import PartnerLaunchService, TelegramLaunchAdapter, TelegramLaunchError
from services.partner_security import TokenVault
from services.partner_store import PartnerStore
from test_partner_launcher import Store, Telegram, PROJECT, ORDER, OWNER, BOT, TOKEN, KEY
from test_partner_store_postgres import RegistryBridge


class BrandDefaultsTests(unittest.TestCase):
    def test_defaults_are_neutral_independent_and_ready_for_new_project(self):
        first = brand_defaults("Север VPN")
        self.assertEqual(first["subscription_title"], "Север VPN")
        self.assertEqual(first["accent_color"], "#e6c77a")
        self.assertTrue(first["welcome_text"])
        self.assertIn("Happ", first["subscription_description"])
        self.assertIn("INCY", first["subscription_description"])
        self.assertNotIn("SPN", str(first))
        first["welcome_text"] = "changed locally"
        self.assertNotEqual(brand_defaults("Север VPN")["welcome_text"], "changed locally")

    def test_telegram_profile_fits_limits_and_commands_are_not_shared(self):
        title = "Личный VPN " + "Я" * 70
        profile = telegram_profile(title[:80])
        self.assertLessEqual(len(profile["name"]), 64)
        self.assertLessEqual(len(profile["description"]), 512)
        self.assertLessEqual(len(profile["short_description"]), 120)
        self.assertIn("300 ГБ", profile["description"])
        self.assertIn("3 устройства", profile["description"])
        self.assertEqual(profile["commands"], [{"command": "start", "description": "Главное меню"}])
        profile["commands"][0]["description"] = "mutated"
        self.assertEqual(telegram_profile("Другой VPN")["commands"][0]["description"], "Главное меню")


class InitialTelegramProfileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from cryptography.fernet import Fernet
        self.vault = TokenVault((Fernet.generate_key().decode(),))
        self.store = Store(self.vault.encrypt(PROJECT, BOT, TOKEN))
        self.store.job["project_title"] = "Мой личный VPN"
        store = self.store

        class ProfileTelegram(Telegram):
            def __init__(self):
                super().__init__()
                self.profile_failure = False
                self.profiles = []

            async def configure_basics(self, token, *, title):
                self.calls.append(("configure_basics", token))
                self.profiles.append(title)
                # Profile setup happens before the durable launch completes.
                if any(call[0] == "complete" for call in store.calls):
                    raise AssertionError("Must configure before complete_launch")
                if self.profile_failure:
                    raise TimeoutError("private-token-must-not-render")

        self.telegram = ProfileTelegram()
        self.service = PartnerLaunchService(
            self.store, self.vault, self.telegram,
            webhook_base_url="https://bots.example.test", webhook_secret_key=KEY, clients_enabled=True,
        )

    async def test_new_paid_launch_configures_title_before_complete_without_an_invoice(self):
        result = await self.service.launch(PROJECT, OWNER, ORDER)
        self.assertEqual(result, {"launch_state": "launched"})
        self.assertEqual(self.telegram.profiles, ["Мой личный VPN"])
        self.assertEqual([call[0] for call in self.telegram.calls],
                         ["getMe", "getWebhookInfo", "setWebhook", "getWebhookInfo", "configure_basics"])
        self.assertEqual(self.store.order["price_kopecks"], 999000)
        self.assertEqual(sum(call[0] == "complete" for call in self.store.calls), 1)
        self.assertFalse(any("invoice" in call[0].lower() or "payment" in call[0].lower()
                             for call in self.store.calls + self.telegram.calls))

    async def test_profile_failure_keeps_paid_order_for_retry_never_marks_it_launched(self):
        self.telegram.profile_failure = True
        result = await self.service.launch(PROJECT, OWNER, ORDER)
        self.assertEqual(result["launch_state"], "retry")
        self.assertEqual(self.store.order["status"], "paid_pending_launch")
        self.assertEqual(self.store.status, "draft")
        self.assertFalse(any(call[0] == "complete" for call in self.store.calls))
        self.assertNotIn("private-token", repr(result))
        self.telegram.profile_failure = False
        result = await self.service.launch(PROJECT, OWNER, ORDER)
        self.assertEqual(result["launch_state"], "launched")
        self.assertEqual(self.telegram.profiles, ["Мой личный VPN", "Мой личный VPN"])
        self.assertEqual(sum(call[0] == "complete" for call in self.store.calls), 1)

    async def test_already_launched_never_overwrites_partner_profile_after_customization(self):
        self.store.order["status"] = "launched"
        self.store.status = "active"
        self.store.job["project_title"] = "Новое личное название"
        self.assertEqual(await self.service.launch(PROJECT, OWNER, ORDER), {"launch_state": "launched"})
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(self.telegram.profiles, [])
        self.assertFalse(any(call[0] == "lock" for call in self.store.calls))

    async def test_adapter_profile_uses_exact_four_methods_and_stops_on_failed_confirmation(self):
        adapter = TelegramLaunchAdapter()
        adapter._call = AsyncMock(return_value=True)
        title = "Мой личный VPN"
        await adapter.configure_basics(TOKEN, title=title)
        profile = telegram_profile(title)
        self.assertEqual([call.args for call in adapter._call.await_args_list], [
            (TOKEN, "setMyName", {"name": profile["name"]}),
            (TOKEN, "setMyDescription", {"description": profile["description"]}),
            (TOKEN, "setMyShortDescription", {"short_description": profile["short_description"]}),
            (TOKEN, "setMyCommands", {"commands": profile["commands"]}),
        ])
        adapter._call = AsyncMock(side_effect=[True, False])
        with self.assertRaises(TelegramLaunchError):
            await adapter.configure_basics(TOKEN, title=title)
        self.assertEqual(adapter._call.await_count, 2)

    async def test_profile_validation_never_calls_telegram_for_bad_title(self):
        adapter = TelegramLaunchAdapter()
        adapter._call = AsyncMock()
        for title in ("", None, "a" * 81, "bad\nname"):
            with self.subTest(title=title), self.assertRaises(TelegramLaunchError):
                await adapter.configure_basics(TOKEN, title=title)
        adapter._call.assert_not_awaited()


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM is not configured")
class NewProjectBrandPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_only_for_new_project_and_retry_preserves_customized_brand(self):
        sql = await RegistryBridge().start()
        self.addAsyncCleanup(sql.close)
        store = PartnerStore(lambda: sql)
        first = await store.create_project(42, "defaults-create-123", "brand-defaults", "Север VPN")
        self.assertEqual(first["brand"], brand_defaults("Север VPN"))
        custom = deepcopy(first["brand"])
        custom.update(welcome_text="Наш собственный текст", subscription_title="Другой заголовок",
                      accent_color="#334455")
        updated = await store.update_project(first["id"], 42, first["version"], {"brand": custom})
        replay = await store.create_project(42, "defaults-create-123", "brand-defaults", "Север VPN")
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(replay["brand"], custom)
        self.assertEqual(replay["version"], updated["version"])
        self.assertEqual(await sql.fetchval("SELECT count(*) FROM wl_projects"), 1)


if __name__ == "__main__":
    unittest.main()
