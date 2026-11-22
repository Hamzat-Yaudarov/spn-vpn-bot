"""Parse the extra IPv4 quote and address rows rendered by 1Cent billing."""

from dataclasses import dataclass
from decimal import Decimal
from ipaddress import IPv4Address
import re
from uuid import UUID


@dataclass(frozen=True)
class Quote:
    upfront_rub: Decimal
    monthly_rub: Decimal


@dataclass(frozen=True)
class Address:
    ip: str
    primary: bool


@dataclass(frozen=True)
class PoolVariant:
    id: str
    name: str
    monthly_rub: Decimal


MONEY = r"(?:[0-9]{1,3}(?: [0-9]{3})+|[0-9]+),[0-9]{2}"
QUOTE = re.compile(
    rf"Цена за адрес (?P<monthly>{MONEY}) ?₽ ?/ ?мес\.? "
    rf"Срок до конца аренды сервера, [0-9]+ дн\. "
    rf"К оплате сейчас (?P<upfront>{MONEY}) ?₽ "
    rf"Адрес оплачивается сразу за весь оставшийся срок аренды сервера\. "
    rf"Дальше продлевается вместе с сервером: ?\+ ?(?P<renewal>{MONEY}) ?₽ в месяц\."
)
CURRENT_QUOTE = re.compile(
    rf"Адрес в месяц (?P<monthly>{MONEY}) ?₽ "
    rf"За остаток периода \([0-9]+ дн\.\) (?P<remaining>{MONEY}) ?₽ "
    rf"К оплате сейчас (?P<upfront>{MONEY}) ?₽ "
    rf"Далее (?P<renewal>{MONEY}) ?₽ в месяц при продлении\."
)
ROW = re.compile(r"Тип (?P<type>Главный|Дополнительный) Адрес (?P<ip>\S+) PTR \(домен\) (?P<ptr>\S+)")
DNS_LABEL = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
PTR = re.compile(rf"(?=.{{1,254}}$){DNS_LABEL}(?:\.{DNS_LABEL})*\.?")


def normalize_service_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Нужен UUID услуги 1Cent")
    try:
        canonical = str(UUID(value))
    except ValueError:
        raise ValueError("Нужен UUID услуги 1Cent") from None
    if canonical != value:
        raise ValueError("Нужен канонический UUID услуги 1Cent")
    return canonical


def parse_quote(text: str) -> Quote:
    if not isinstance(text, str):
        raise ValueError("Не удалось прочитать цену дополнительного IPv4 1Cent")
    normalized = " ".join(text.split())
    if "Адрес в месяц" in normalized:
        # The unselected "IP на выбор" card also contains a price. Read only
        # the uniquely labelled final summary, never the first money on screen.
        if (any(normalized.count(label) != 1 for label in
                ("Адрес в месяц", "За остаток периода (", "К оплате сейчас", "Далее "))
                or "Разово за вариант" in normalized):
            raise ValueError("Цена дополнительного IPv4 1Cent неоднозначна")
        quote = CURRENT_QUOTE.search(normalized)
        if quote is None:
            raise ValueError("Не удалось подтвердить окончательную цену дополнительного IPv4")
        monthly, remaining, upfront, renewal = (
            Decimal(value.replace(" ", "").replace(",", "."))
            for value in quote.group("monthly", "remaining", "upfront", "renewal")
        )
        if monthly != renewal or remaining != upfront:
            raise ValueError("Цена, списание и продление дополнительного IPv4 не совпадают")
        return Quote(upfront, monthly)
    if normalized.count("Цена за адрес") != 1 or normalized.count("К оплате сейчас") != 1:
        raise ValueError("Цена дополнительного IPv4 1Cent неоднозначна")
    quote = QUOTE.search(normalized)
    if quote is None:
        raise ValueError("Не удалось подтвердить цену дополнительного IPv4 в рублях")
    monthly, upfront, renewal = (Decimal(value.replace(" ", "").replace(",", ".")) for value in
                                  quote.group("monthly", "upfront", "renewal"))
    if monthly != renewal:
        raise ValueError("Цена и стоимость продления дополнительного IPv4 не совпадают")
    return Quote(upfront, monthly)


def parse_pool_variant(addresses: dict, tariff: dict) -> PoolVariant:
    """Accept only the observed ordinary, zero-setup-fee pool order flow."""
    try:
        if not isinstance(addresses, dict) or not isinstance(tariff, dict):
            raise ValueError
        variants = addresses["addVariants"]
        flow = addresses["orderFlow"]
        if (not isinstance(variants, list) or not isinstance(flow, dict)
                or flow.get("enabled") is not True or flow.get("order") is not None
                or tariff.get("canBuy") is not True):
            raise ValueError
        candidates = [v for v in variants if isinstance(v, dict) and v.get("source") == "pool"]
        if len(candidates) != 1:
            raise ValueError
        variant = candidates[0]
        if (variant.get("name") != "Дополнительный адрес" or variant.get("chargeRemaining") is not True
                or not isinstance(variant.get("id"), str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", variant["id"]) is None):
            raise ValueError

        def money(value):
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError
            result = Decimal(str(value))
            if not result.is_finite() or result < 0 or result.as_tuple().exponent < -2:
                raise ValueError
            return result

        monthly = money(tariff["price"])
        if (monthly <= 0 or money(addresses["addPrice"]) != monthly
                or money(variant["price"]) != 0 or money(tariff["setupFee"]) != 0):
            raise ValueError
        return PoolVariant(variant["id"], variant["name"], monthly)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise ValueError("Не подтверждён обычный IPv4 из пула без разовой доплаты 1Cent") from None


def parse_rows(rows: list[str]) -> list[Address]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("Таблица IP-адресов 1Cent пуста или недоступна")
    result = []
    seen = set()
    for row in rows:
        match = ROW.fullmatch(" ".join(row.split())) if isinstance(row, str) else None
        if match is None or (match["ptr"] != "—" and PTR.fullmatch(match["ptr"]) is None):
            raise ValueError("Не удалось однозначно прочитать строку IP-адреса 1Cent")
        try:
            address = IPv4Address(match["ip"])
        except ValueError:
            raise ValueError("В строке 1Cent отсутствует IPv4-адрес") from None
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise ValueError("В таблице 1Cent ожидается публичный IPv4-адрес")
        if address in seen:
            raise ValueError("В таблице 1Cent повторяется IPv4-адрес")
        seen.add(address)
        result.append(Address(str(address), match["type"] == "Главный"))
    if sum(row.primary for row in result) != 1:
        raise ValueError("Таблица 1Cent должна содержать один главный IPv4-адрес")
    return result


def parse_api_addresses(value: dict) -> list[Address]:
    """Observed GET /ipv4/addresses contract; IDs are not order receipts."""
    if (not isinstance(value, dict) or "operation" not in value or value["operation"] is not None
            or not isinstance(value.get("orderFlow"), dict)
            or "order" not in value["orderFlow"] or value["orderFlow"]["order"] is not None):
        raise ValueError("Список IPv4 1Cent занят операцией или не соответствует проверенной схеме")
    rows = value.get("addresses")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError("Не подтверждён ограниченный список IPv4 1Cent")
    result, seen = [], set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"id", "address", "ptr", "main", "releasing", "removable"}
                or type(row["main"]) is not bool or row["releasing"] is not False
                or type(row["removable"]) is not bool
                or not isinstance(row["address"], str)
                or (row["ptr"] is not None and (not isinstance(row["ptr"], str) or PTR.fullmatch(row["ptr"]) is None))):
            raise ValueError("Строка IPv4 1Cent не завершена или изменила проверенную схему")
        # Provider row IDs are deliberately not interpreted as allocation or
        # purchase identity. Ownership still comes only from the order journal.
        try:
            address = IPv4Address(row["address"])
        except ValueError:
            raise ValueError("В списке 1Cent ожидается публичный IPv4") from None
        if (str(address) != row["address"] or not address.is_global
                or address.is_multicast or address.is_reserved or address in seen):
            raise ValueError("IPv4 1Cent повторяется или не является публичным адресом")
        seen.add(address)
        result.append(Address(str(address), row["main"]))
    if sum(row.primary for row in result) != 1:
        raise ValueError("Список 1Cent должен содержать ровно один главный IPv4")
    return sorted(result, key=lambda row: int(IPv4Address(row.ip)))
