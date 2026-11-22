"""Creator purchase entry and tenant-local cabinet navigation; no real ports."""

from copy import deepcopy
import unittest
from unittest.mock import AsyncMock

from services.partner_bot import PartnerBotHandler
from services.partner_manager import PartnerManagerHandler


PROJECT_ID = "00000000-0000-4000-8000-000000000001"
ORIGIN = "https://manager.example.test"
OWNER = 777
BOT = 123456789


def project():
    return {"id": PROJECT_ID, "owner_tg_id": OWNER, "title": "Личный VPN",
            "price_kopecks": 19900, "brand": {}, "bot": {"id": BOT, "username": "TenantBot"}}


def update(user_id, *, callback=None):
    message = {"message_id": 1, "chat": {"id": user_id, "type": "private"},
               "from": {"id": user_id, "is_bot": False}, "text": "/start"}
    if callback is None:
        return {"update_id": 1, "message": message}
    message["from"] = {"id": BOT, "is_bot": True}
    return {"update_id": 1, "callback_query": {
        "id": "offline-callback", "data": callback,
        "from": {"id": user_id, "is_bot": False}, "message": message}}


def screen(commands):
    return commands[-1]["payload"]


def buttons(payload):
    return [button for row in payload["reply_markup"]["inline_keyboard"] for button in row]


class CreatorTenantEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_creator_has_short_neutral_explanation_and_one_enrollment_entry(self):
        handler = PartnerManagerHandler(BOT, ORIGIN, manager_allowed_user_ids=[OWNER])
        response = await handler.handle(update(OWNER))
        self.assertEqual([button for button in buttons(response) if "web_app" in button], [
            {"text": "Создать свой сервис", "web_app": {"url": ORIGIN + "/partners"}}
        ])
        self.assertEqual([button for button in buttons(response) if button["text"] == "Поддержка"], [
            {"text": "Поддержка", "url": "https://t.me/vpnislam"}
        ])
        self.assertEqual(len(buttons(response)), 2)
        self.assertIn("сервис под своим брендом", response["text"])
        self.assertIn("Оплатите подключение, затем создайте бота одной кнопкой", response["text"])
        self.assertIn("Дальнейшее управление — в вашем боте", response["text"])
        for unchanged_term in ("9 990 ₽", "70%", "30%"):
            self.assertIn(unchanged_term, response["text"])
        for removed_term in ("VPN", "199 ₽", "300 ГБ", "3 устройства", "+7 дней", "антиглуш", "обход"):
            self.assertNotIn(removed_term, response["text"])
        self.assertLess(len(response["text"]), 400)
        self.assertIsNone(await handler.handle(update(222)))

    async def test_owner_admin_opens_exact_tenant_webapp_without_central_redirect(self):
        store = AsyncMock()
        handler = PartnerBotHandler(store, manager_url="https://t.me/CentralManagerBot",
                                    workspace_origin=ORIGIN + "/")
        row = project()
        row["id"] = row["id"].upper()
        row["workspace_origin"] = "https://untrusted.example"
        response = screen(await handler.handle(row, update(OWNER, callback="admin")))
        self.assertEqual([button for button in buttons(response) if "web_app" in button], [{
            "text": "Личный кабинет",
            "web_app": {"url": ORIGIN + "/partners/workspace/" + PROJECT_ID},
        }])
        self.assertEqual([button for button in buttons(response) if button["text"] == "Поддержка платформы"], [
            {"text": "Поддержка платформы", "url": "https://t.me/vpnislam"}
        ])
        self.assertEqual([button["callback_data"] for button in buttons(response) if "callback_data" in button], ["menu"])
        self.assertNotIn("CentralManagerBot", str(response))
        self.assertIn("Обзор, клиенты, заказы и настройки", response["text"])

    async def test_customer_menu_features_are_unchanged_and_admin_stays_hidden(self):
        handler = PartnerBotHandler(AsyncMock(), workspace_origin=ORIGIN)
        expected = ["🛒 Купить подписку", "🔑 Моя подписка", "🎁 Бонусы", "📲 Как подключиться", "🆘 Помощь", "📢 Новости"]
        normal = screen(await handler.handle(project(), update(222)))
        owner = screen(await handler.handle(project(), update(OWNER)))
        self.assertEqual([button["text"] for button in buttons(normal)], expected)
        self.assertEqual([button["text"] for button in buttons(owner)], expected + ["⚙️ Админ"])
        forged = screen(await handler.handle(project(), update(222, callback="admin")))
        self.assertFalse(any("web_app" in button for button in buttons(forged)))
        self.assertIn("недоступно", forged["text"])

    async def test_authorized_staff_uses_same_scoped_cabinet_entry(self):
        management = AsyncMock()
        management.authorize.return_value = {"role": "support"}
        handler = PartnerBotHandler(AsyncMock(), management=management, workspace_origin=ORIGIN)
        response = screen(await handler.handle(project(), update(222, callback="admin")))
        self.assertEqual(buttons(response)[0]["web_app"]["url"],
                         ORIGIN + "/partners/workspace/" + PROJECT_ID)
        management.authorize.assert_awaited_once_with(PROJECT_ID, 222, "referrals")

    async def test_foreign_bot_callback_cannot_receive_cabinet_entry(self):
        handler = PartnerBotHandler(AsyncMock(), workspace_origin=ORIGIN)
        forged = deepcopy(update(OWNER, callback="admin"))
        forged["callback_query"]["message"]["from"]["id"] = BOT + 1
        self.assertEqual(await handler.handle(project(), forged), [])

    async def test_workspace_origin_rejects_untrusted_paths_and_credentials(self):
        for origin in (
            "http://manager.example.test", "https://name:secret@manager.example.test",
            "https://manager.example.test/partners", "https://manager.example.test/?project=other",
            "https://manager.example.test/#private", "https://manager.example.test:",
            "https://manager.example.test:444", "https://127.0.0.1",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                PartnerBotHandler(AsyncMock(), workspace_origin=origin)


if __name__ == "__main__":
    unittest.main()
