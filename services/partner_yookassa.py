"""Explicitly configured YooKassa port for partner orders; never auto-enabled.

No environment/config reads and no calls on construction. The host must review
the merchant/legal setup and inject separate credentials before using this port.
Transport responses are validated, redirects disabled and errors redacted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import json
import re
from uuid import UUID

from services.partner_checkout import CheckoutError, PartnerPayment, https_url


@dataclass(frozen=True)
class PartnerYooKassaSettings:
    shop_id: str
    secret_key: str = field(repr=False)
    return_url: str
    test_mode: bool = True
    owner_entry_pilot: bool = False
    public_entry_trial: bool = False
    public_entry_sales: bool = False

    def __post_init__(self):
        if (not isinstance(self.shop_id, str) or not re.fullmatch(r"[0-9]{1,20}", self.shop_id)
                or not isinstance(self.secret_key, str) or not 10 <= len(self.secret_key) <= 512
                or any(c.isspace() for c in self.secret_key)
                or type(self.test_mode) is not bool or type(self.owner_entry_pilot) is not bool
                or type(self.public_entry_trial) is not bool
                or type(self.public_entry_sales) is not bool
                or sum((self.owner_entry_pilot, self.public_entry_trial,
                        self.public_entry_sales)) > 1
                or (self.owner_entry_pilot or self.public_entry_trial
                    or self.public_entry_sales) and self.test_mode):
            raise ValueError("Платёжный провайдер не настроен.")
        https_url(self.return_url)


def _money(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,7})\.[0-9]{2}", value):
        raise ValueError()
    amount = int(Decimal(value) * 100)
    if not 0 < amount <= 10_000_000:
        raise ValueError()
    return amount


class PartnerYooKassaProvider:
    BASE_URL = "https://api.yookassa.ru/v3"

    def __init__(self, settings: PartnerYooKassaSettings, *, session_factory=None):
        self.settings = settings
        self._session_factory = session_factory

    def __repr__(self):
        return "<PartnerYooKassaProvider>"

    async def _request(self, method, path, *, payload=None, key=None):
        try:
            import aiohttp
            factory = self._session_factory or aiohttp.ClientSession
            headers = {"Accept": "application/json"}
            if key:
                headers["Idempotence-Key"] = str(UUID(key))
            async with factory(auth=aiohttp.BasicAuth(self.settings.shop_id, self.settings.secret_key),
                               timeout=aiohttp.ClientTimeout(total=20, connect=5), trust_env=False) as session:
                async with session.request(method, self.BASE_URL + path, json=payload,
                                           headers=headers, allow_redirects=False) as response:
                    if response.status not in (200, 201):
                        raise ValueError()
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        raw.extend(chunk)
                        if len(raw) > 131_072:
                            raise ValueError()
                    return json.loads(raw)
        except Exception:
            raise CheckoutError("Платёжный сервис временно недоступен. Проверьте этот же заказ позже.") from None

    def _payment(self, body):
        try:
            metadata = body["metadata"]
            if (body["recipient"]["account_id"] != self.settings.shop_id
                    or type(body["test"]) is not bool or body["test"] != self.settings.test_mode
                    or type(body["paid"]) is not bool
                    or body["status"] not in {"pending", "waiting_for_capture", "succeeded", "canceled"}
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", body["id"])):
                raise ValueError()
            tg = metadata["wl_tg_id"]
            purpose = metadata.get("wl_kind", "subscription")
            if not isinstance(tg, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", tg):
                raise ValueError()
            if purpose not in {"subscription", "entry", "traffic", "devices"}:
                raise ValueError()
            amount = _money(body["amount"]["value"])
            if self.settings.owner_entry_pilot:
                from services.partner_entry_pricing import (
                    ENTRY_PILOT_OWNER_ID, ENTRY_PILOT_PRICE_KOPECKS,
                )

                if (purpose != "entry" or int(tg) != ENTRY_PILOT_OWNER_ID
                        or amount != ENTRY_PILOT_PRICE_KOPECKS
                        or body["amount"]["currency"] != "RUB"):
                    raise ValueError()
            elif self.settings.public_entry_trial or self.settings.public_entry_sales:
                # Bound receipts retain their price even when the promotion
                # changes. The checkout compares this with the saved order.
                if (purpose != "entry" or amount not in {1000, 999000}
                        or body["amount"]["currency"] != "RUB"):
                    raise ValueError()
            url = (body.get("confirmation") or {}).get("confirmation_url") or ""
            https_url(url, empty=True)
            return PartnerPayment(
                id=body["id"], order_id=str(UUID(metadata["wl_order_id"])),
                project_id=str(UUID(metadata["wl_project_id"])), tg_id=int(tg),
                amount_kopecks=amount, currency=body["amount"]["currency"],
                status=body["status"], paid=body["paid"], confirmation_url=url, purpose=purpose,
            )
        except Exception:
            raise CheckoutError("Платёжный сервис вернул непроверенные данные. Оплата требует проверки.") from None

    async def create_invoice(self, order, *, idempotence_key):
        price = order["price_kopecks"]
        purpose = order.get("purpose", "subscription")
        if type(price) is not int or not 1 <= price <= 10_000_000:
            raise CheckoutError("Некорректная цена заказа.")
        if self.settings.owner_entry_pilot:
            from services.partner_entry_pricing import (
                ENTRY_PILOT_OWNER_ID, ENTRY_PILOT_PRICE_KOPECKS,
            )

            if (purpose != "entry" or type(order.get("tg_id")) is not int
                    or order["tg_id"] != ENTRY_PILOT_OWNER_ID
                    or price != ENTRY_PILOT_PRICE_KOPECKS):
                raise CheckoutError("Доступна только закрытая проверка вступления.")
        elif self.settings.public_entry_trial:
            if (purpose != "entry" or type(order.get("tg_id")) is not int
                    or price != 1000):
                raise CheckoutError("Доступна только пробная покупка вступления за 10 ₽.")
        elif self.settings.public_entry_sales:
            if (purpose != "entry" or type(order.get("tg_id")) is not int
                    or price != 999000):
                raise CheckoutError("Доступна только покупка подключения проекта за 9 990 ₽.")
        if (purpose not in {"subscription", "entry", "traffic", "devices"}
                or (purpose == "entry" and price != 999_000
                    and not (self.settings.owner_entry_pilot or self.settings.public_entry_trial))
                or (purpose == "subscription" and price < 19_900)):
            raise CheckoutError("Некорректное назначение оплаты.")
        if purpose in {"traffic", "devices"}:
            quantity = order.get("addon_quantity")
            package = order.get("package_id")
            if (type(quantity) is not int or not 1 <= quantity <= (3 if purpose == "devices" else 100_000)
                    or not isinstance(package, str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", package)):
                raise CheckoutError("Не удалось проверить пакет докупки.")
            try:
                UUID(str(order["subscription_id"]))
            except (KeyError, ValueError, TypeError, AttributeError):
                raise CheckoutError("Не удалось проверить подписку для докупки.") from None
        order_id, project_id = str(UUID(str(order["id"]))), str(UUID(str(order["project_id"])))
        if str(UUID(idempotence_key)) != order_id:
            raise CheckoutError("Некорректный ключ оплаты.")
        if type(order["tg_id"]) is not int or not 0 < order["tg_id"] < 2**63:
            raise CheckoutError("Некорректный получатель.")
        body = await self._request("POST", "/payments", key=idempotence_key, payload={
            "amount": {"value": f"{price // 100}.{price % 100:02d}", "currency": "RUB"},
            "capture": True, "confirmation": {"type": "redirect", "return_url": self.settings.return_url},
            "description": {
                "entry": "Подключение партнёрского проекта",
                "subscription": "Подписка VPN на 30 дней",
                "traffic": "Докупка трафика VPN", "devices": "Докупка устройств VPN",
            }[purpose],
            "metadata": {"wl_order_id": order_id, "wl_project_id": project_id,
                         "wl_tg_id": str(order["tg_id"]), "wl_kind": purpose},
        })
        return self._payment(body)

    async def get_payment(self, payment_id):
        if not isinstance(payment_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", payment_id):
            raise CheckoutError("Некорректный идентификатор оплаты.")
        result = self._payment(await self._request("GET", "/payments/" + payment_id))
        if result.id != payment_id:
            raise CheckoutError("Платёжный сервис вернул другую оплату.")
        return result
