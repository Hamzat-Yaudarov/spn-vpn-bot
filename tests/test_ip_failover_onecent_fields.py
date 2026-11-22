from decimal import Decimal
import unittest

from spn_ip_failover.onecent_fields import (
    Address, PoolVariant, Quote, normalize_service_id, parse_pool_variant, parse_quote, parse_rows,
)


PRICE_PANEL = (
    "Цена за адрес\n100,00 ₽ /мес\n"
    "Срок до конца аренды сервера, 5 дн.\n"
    "К оплате сейчас\n16,13 ₽\n"
    "Адрес оплачивается сразу за весь оставшийся срок аренды сервера.\n"
    "Дальше продлевается вместе с сервером: +100,00 ₽ в месяц."
)
MAIN = "Тип\nГлавный\nАдрес\n8.8.8.8\nPTR (домен)\n—"
EXTRA = "Тип Дополнительный Адрес 8.8.4.4 PTR (домен) resolver.example."
CURRENT_PANEL = (
    "Дополнительный IPv4-адрес Дополнительный адрес 100,00 ₽ в месяц "
    "IP на выбор +1 000,00 ₽ разово "
    "Адрес в месяц 100,00 ₽ За остаток периода (28 дн.) 93,33 ₽ "
    "К оплате сейчас 93,33 ₽ Далее 100,00 ₽ в месяц при продлении. Отмена Да, добавить"
)


class OneCentFieldsTests(unittest.TestCase):
    def test_current_dialog_reads_only_exact_summary_not_unselected_expensive_variant(self):
        expected = Quote(Decimal("93.33"), Decimal("100.00"))
        self.assertEqual(parse_quote(CURRENT_PANEL), expected)
        self.assertEqual(parse_quote(CURRENT_PANEL.replace(" ", "\u00a0")), expected)

    def test_current_dialog_refuses_missing_or_conflicting_final_amounts(self):
        for text in (
            CURRENT_PANEL.replace("К оплате сейчас 93,33 ₽ ", ""),
            CURRENT_PANEL.replace("За остаток периода (28 дн.) 93,33 ₽", "За остаток текущего периода спишется часть месячной цены."),
            CURRENT_PANEL.replace("К оплате сейчас 93,33", "К оплате сейчас 94,00"),
            CURRENT_PANEL.replace("Далее 100,00", "Далее 200,00"),
            CURRENT_PANEL + " К оплате сейчас 93,33 ₽",
            CURRENT_PANEL.replace("Далее", "Разово за вариант 1 000,00 ₽ Далее"),
            CURRENT_PANEL.replace("93,33 ₽", "93,33 €"),
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_quote(text)

    def test_only_normal_zero_fee_pool_variant_is_accepted(self):
        addresses = {
            "addPrice": "100.00", "orderFlow": {"enabled": True, "order": None},
            "addVariants": [
                {"id": "pool1", "name": "Дополнительный адрес", "source": "pool", "price": "0.00", "chargeRemaining": True},
                {"id": "chosen1", "name": "IP на выбор", "source": "chosen", "price": "1000.00", "chargeRemaining": True},
            ],
        }
        tariff = {"price": "100.00", "setupFee": 0, "canBuy": True, "maxExtra": 15}
        self.assertEqual(parse_pool_variant(addresses, tariff), PoolVariant("pool1", "Дополнительный адрес", Decimal("100.00")))
        import copy
        variants = []
        for key, value in (("source", "chosen"), ("price", "0.01"), ("chargeRemaining", False),
                           ("name", "IPWatch"), ("id", "pool/foreign")):
            changed = copy.deepcopy(addresses)
            changed["addVariants"][0][key] = value
            variants.append((changed, tariff))
        changed = copy.deepcopy(addresses)
        changed["addVariants"].append(changed["addVariants"][0].copy())
        variants += [(changed, tariff), (addresses, dict(tariff, setupFee=10)),
                     (addresses, dict(tariff, canBuy=False)), (addresses, dict(tariff, price="NaN")),
                     (dict(addresses, addPrice="101.00"), tariff),
                     (dict(addresses, orderFlow={"enabled": True, "order": {"id": "pending"}}), tariff)]
        for address_data, tariff_data in variants:
            with self.subTest(data=address_data, tariff=tariff_data), self.assertRaises(ValueError):
                parse_pool_variant(address_data, tariff_data)

    def test_reads_monthly_and_prorated_rubles_from_dialog(self):
        self.assertEqual(
            parse_quote("Добавить IP-адрес\n" + PRICE_PANEL + "\nОплатить"),
            Quote(Decimal("16.13"), Decimal("100.00")),
        )
        localized = PRICE_PANEL.replace("100,00 ₽", "1\u202f100,00\u00a0₽")
        self.assertEqual(parse_quote(localized), Quote(Decimal("16.13"), Decimal("1100.00")))

    def test_refuses_ambiguous_or_mismatched_price(self):
        for quote in (
            PRICE_PANEL + PRICE_PANEL,
            PRICE_PANEL.replace("16,13 ₽", "16,13 €"),
            PRICE_PANEL.replace("/мес", "/год"),
            PRICE_PANEL.replace("+100,00 ₽", "+110,00 ₽"),
            PRICE_PANEL.replace("16,13 ₽", "16,1 ₽"),
            PRICE_PANEL.replace("16,13 ₽", "16,13 ₽ /мес"),
        ):
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                parse_quote(quote)

    def test_reads_primary_and_additional_public_ipv4(self):
        self.assertEqual(
            parse_rows([MAIN, EXTRA]),
            [Address("8.8.8.8", True), Address("8.8.4.4", False)],
        )
        self.assertEqual(
            parse_rows([
                "Тип\nДополнительный\nАдрес\n2.26.148.130\nPTR (домен)\n—",
                "Тип\nГлавный\nАдрес\n144.31.171.189\nPTR (домен)\n—",
            ]),
            [Address("2.26.148.130", False), Address("144.31.171.189", True)],
        )

    def test_refuses_ambiguous_or_non_public_inventory(self):
        for rows in (
            [],
            [EXTRA],
            [MAIN, EXTRA.replace("Дополнительный", "Главный")],
            [MAIN, EXTRA.replace("8.8.4.4", "8.8.8.8")],
            [MAIN, EXTRA.replace("8.8.4.4", "127.0.0.1")],
            [MAIN, EXTRA.replace("8.8.4.4", "224.0.0.1")],
            [MAIN, EXTRA.replace("8.8.4.4", "2001:4860::8888")],
            [MAIN, EXTRA.replace("8.8.4.4", "8.8.4.4 1.1.1.1")],
            [MAIN, EXTRA + " Тип Главный"],
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                parse_rows(rows)

    def test_normalizes_only_canonical_service_ids(self):
        service_id = "1234abcd-1234-1234-1234-123456789abc"
        self.assertEqual(normalize_service_id(service_id), service_id)
        for value in (service_id.upper(), "service", service_id + "/manage", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_service_id(value)
