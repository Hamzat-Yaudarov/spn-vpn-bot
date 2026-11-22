"""Pure catalogue and public-host validation for the partner platform.

Importing this module does not read ``.env``, import the application config,
connect to a database, or contact a payment service.  The explicit
``load_main_addon_catalog`` adapter is the only entry point that may import the
existing application's pricing helpers; it does so only when called.

Prices are stored as integer kopecks.  A partner's unified product uses the
existing *bypass* add-on policy (three included devices), never the regular
product's cheaper five-device policy.  Quotes are display data, not permission
to charge, issue a subscription, or award a bonus.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import ipaddress
import re
from typing import Any


MIN_PRICE_KOPECKS = 19_900
MAX_PRICE_KOPECKS = 10_000_000  # A 100,000 RUB safety ceiling, not a new tariff.
BILLING_DAYS = 30
BASE_TRAFFIC_GB = 300
BASE_DEVICE_LIMIT = 3
ENTRY_FEE_KOPECKS = 999_000
PARTNER_PERCENT = 70
PLATFORM_PERCENT = 30
MAX_DEVICE_LIMIT = 10

# These labels share infrastructure with platform services and must not be
# made available to partners.  Matching is case-insensitive via canonical form.
RESERVED_SLUGS = frozenset({
    "admin", "administrator", "api", "app", "assets", "auth", "billing",
    "bot", "bots", "cdn", "cdn-cgi", "dashboard", "dev", "docs", "download",
    "downloads", "help", "internal", "login", "logout", "mail", "manager",
    "metrics", "mobile", "news", "ns1", "ns2", "panel", "partner", "partners",
    "payment", "payments", "preview", "root", "secure", "smtp", "stage",
    "staging", "static", "status", "sub", "subscription", "subscriptions",
    "support", "test", "vpn", "webhook", "webhooks", "www",
})

_PRICE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?\Z", re.ASCII)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z", re.ASCII)
_PACKAGE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z", re.ASCII)


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name}: требуется целое число")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}: значение вне допустимого диапазона")
    return value


def parse_price_kopecks(value: str) -> int:
    """Parse a partner's RUB price without floating-point rounding.

    Input is a decimal string, e.g. ``'199'`` or ``'249.90'``.  Signs,
    exponents, whitespace, comma decimals, non-ASCII digits and excess decimal
    places are deliberately rejected.  Integer-kopek callers should pass their
    value straight to ``catalog`` instead of guessing this function's units.
    """
    if not isinstance(value, str) or len(value) > 16 or not _PRICE.fullmatch(value):
        raise ValueError("Цена должна быть строкой в рублях, например 199 или 249.90")
    try:
        kopecks = int(Decimal(value) * 100)
    except (InvalidOperation, ValueError, OverflowError):
        raise ValueError("Некорректная цена") from None
    return _integer(kopecks, "Цена", minimum=MIN_PRICE_KOPECKS, maximum=MAX_PRICE_KOPECKS)


def split_revenue(kopecks: int) -> dict[str, int]:
    """Round the partner's 70% half-up; the platform gets the exact remainder.

    Accepts add-on sales below the main subscription's 199 RUB minimum.  The
    entry fee is not a subscription sale; a caller must not split it as one.
    """
    amount = _integer(kopecks, "Сумма", minimum=0, maximum=MAX_PRICE_KOPECKS)
    partner = (amount * PARTNER_PERCENT + 50) // 100
    return {"partner_kopecks": partner, "platform_kopecks": amount - partner}


def validate_slug(value: str) -> str:
    """Return one canonical ASCII DNS label, length 3..32, excluding services."""
    if not isinstance(value, str) or not value.isascii():
        raise ValueError("Поддомен: используйте латинские буквы, цифры и дефис")
    slug = value.lower()
    if (not 3 <= len(slug) <= 32 or not _LABEL.fullmatch(slug)
            or slug.startswith("xn--") or slug in RESERVED_SLUGS):
        raise ValueError("Этот поддомен недоступен; нужны 3–32 буквы, цифры или дефис")
    return slug


def validate_base_domain(value: str) -> str:
    """Return a canonical hostname, never a URL, port, wildcard, or IP address.

    ASCII-only input intentionally excludes IDNA/punycode ambiguity from an
    operator-configured base domain.  Domain ownership / public-suffix rules
    are deployment checks, not an outbound DNS lookup in this pure helper.
    """
    if not isinstance(value, str) or not value.isascii() or not 3 <= len(value) <= 253:
        raise ValueError("Нужен домен без протокола, порта и пути")
    domain = value.lower()
    labels = domain.split(".")
    if (len(labels) < 2 or any(not 1 <= len(label) <= 63
                             or not _LABEL.fullmatch(label)
                             or label.startswith("xn--") for label in labels)
            or not re.fullmatch(r"[a-z]{2,63}", labels[-1], re.ASCII)):
        raise ValueError("Нужен корректный ASCII-домен без протокола, порта и пути")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise ValueError("IP-адрес нельзя использовать как базовый домен")


def project_hostname(slug: str, base_domain: str) -> str:
    """Build exactly one partner-controlled label under a configured base."""
    host = f"{validate_slug(slug)}.{validate_base_domain(base_domain)}"
    if len(host) > 253:
        raise ValueError("Слишком длинное доменное имя")
    return host


def _rubles_from_main(value: Any) -> int:
    """Convert server-owned main-bot RUB quotes, including exact discounts.

    Raw catalog prices are integer RUB; the main discount adapter serializes a
    decimal price as a string. Floats are not accepted here: that adapter must
    choose an exact, at-most-two-decimal representation rather than asking the
    partner catalogue to round a binary floating-point value independently.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return _integer(value, "Цена докупки", minimum=1,
                        maximum=MAX_PRICE_KOPECKS // 100) * 100
    if not isinstance(value, str) or len(value) > 16 or not _PRICE.fullmatch(value):
        raise ValueError("Цена докупки должна быть целыми рублями или точной десятичной строкой")
    try:
        kopecks = int(Decimal(value) * 100)
    except (InvalidOperation, ValueError, OverflowError):
        raise ValueError("Некорректная цена докупки") from None
    return _integer(kopecks, "Цена докупки", minimum=1, maximum=MAX_PRICE_KOPECKS)


def _traffic_packages(packages: Mapping | None) -> list[dict]:
    if packages is None:
        return []
    if not isinstance(packages, Mapping):
        raise ValueError("Пакеты ГБ должны быть каталогом основного бота")
    result = []
    for package_id, package in packages.items():
        if (not isinstance(package_id, str) or not _PACKAGE_ID.fullmatch(package_id)
                or not isinstance(package, Mapping)):
            raise ValueError("Некорректный пакет ГБ")
        gb = _integer(package.get("gb"), "Пакет ГБ", minimum=1, maximum=100_000)
        result.append({"id": package_id, "gb": gb,
                       "title": f"+{gb} ГБ", "price_kopecks": _rubles_from_main(package.get("price"))})
    return result


def _device_options(options: Mapping | None) -> dict:
    empty = {"addon_status": "not_loaded", "packages": [],
             "max_limit": MAX_DEVICE_LIMIT, "pricing_plan_kind": "bypass"}
    if options is None:
        return empty
    if not isinstance(options, Mapping):
        raise ValueError("Некорректные варианты докупки устройств")
    status = options.get("addon_status")
    if status not in {"available", "expired", "max_limit", "unavailable", "quote_required", "not_loaded"}:
        raise ValueError("Неизвестный статус докупки устройств")
    raw_packages = options.get("packages", [])
    if not isinstance(raw_packages, (list, tuple)):
        raise ValueError("Некорректные пакеты устройств")
    if status != "available" and raw_packages:
        raise ValueError("Недоступная докупка не должна содержать пакеты")
    packages, counts = [], set()
    for package in raw_packages:
        if not isinstance(package, Mapping):
            raise ValueError("Некорректный пакет устройств")
        count = _integer(package.get("count"), "Устройства", minimum=1, maximum=3)
        if count in counts:
            raise ValueError("Повтор пакета устройств")
        counts.add(count)
        packages.append({
            "count": count, "title": f"+{count} " + ("устройство" if count == 1 else "устройства"),
            "price_kopecks": _rubles_from_main(package.get("price")),
            "original_price_kopecks": _rubles_from_main(package.get("original_price", package.get("price"))),
            "discount_percent": _integer(package.get("discount_percent", 0), "Скидка", minimum=0, maximum=100),
            "days": _integer(package.get("days"), "Срок докупки", minimum=1, maximum=36_500),
        })
    if status == "available" and not packages:
        raise ValueError("Для доступной докупки нужны пакеты")
    return {**empty, "addon_status": status, "packages": packages}


def catalog(price_kopecks: int, traffic_packages: Mapping | None = None,
            device_options: Mapping | None = None) -> dict:
    """Build a serializable quote from an operator-approved subscription price.

    Add-ons accept the existing main-bot RUB catalogue/helper result, explicitly
    supplied by the caller. A discounted price must be a decimal string, not a
    float. Missing data stays ``not_loaded`` rather than inventing a price or
    claiming a user has reached the device maximum.
    """
    price = _integer(price_kopecks, "Цена", minimum=MIN_PRICE_KOPECKS, maximum=MAX_PRICE_KOPECKS)
    return {
        "billing_days": BILLING_DAYS,
        "entry_fee_kopecks": ENTRY_FEE_KOPECKS,
        "revenue_share": {"partner_percent": PARTNER_PERCENT, "platform_percent": PLATFORM_PERCENT},
        "products": [{
            "id": "unified_30d", "title": "Подписка на 30 дней", "days": BILLING_DAYS,
            "price_kopecks": price, "min_price_kopecks": MIN_PRICE_KOPECKS,
            "base_traffic_gb": BASE_TRAFFIC_GB, "base_device_limit": BASE_DEVICE_LIMIT,
            "ordinary_servers": True, "bypass_servers": True,
        }],
        "addons": {
            "traffic_status": "available" if traffic_packages else "not_loaded" if traffic_packages is None else "unavailable",
            "traffic_packages": _traffic_packages(traffic_packages),
            "devices": _device_options(device_options),
        },
    }


def load_main_addon_catalog(config_module=None, *, subscription: Mapping | None = None,
                           now=None, device_option_provider=None) -> dict:
    """Explicit lazy adapter; never runs as a side effect of module import.

    Inject ``config_module`` and ``device_option_provider`` for isolated tests
    or a host application that already loaded its config.  When an actual
    subscription is supplied, call the existing device helper with ``bypass``
    pricing and its real remaining term / limit.  Without a subscription there
    is no fabricated prorated quote: the device status is ``quote_required``.
    """
    if config_module is None:
        from importlib import import_module
        config_module = import_module("config")
    if (getattr(config_module, "BYPASS_HWID_DEVICE_LIMIT", None) != BASE_DEVICE_LIMIT
            or getattr(config_module, "DEVICE_ADDON_MAX_HWID_DEVICE_LIMIT", None) != MAX_DEVICE_LIMIT):
        raise ValueError("Лимиты основного бота изменились; требуется проверка каталога партнёров")
    traffic = getattr(config_module, "BYPASS_TRAFFIC_PACKAGES", None)
    if traffic is None:
        raise ValueError("В основном боте не найден каталог докупки ГБ")
    if subscription is None:
        options = {"addon_status": "quote_required", "packages": []}
    else:
        if not isinstance(subscription, Mapping):
            raise ValueError("Некорректная подписка")
        if device_option_provider is None:
            from services.device_addons import miniapp_device_addon_options
            device_option_provider = miniapp_device_addon_options
        options = device_option_provider({**subscription, "plan_kind": "bypass"}, now=now)
    # Normalize here too, so invalid main pricing fails closed before exposure.
    validated = catalog(MIN_PRICE_KOPECKS, traffic, options)["addons"]
    return {"traffic_packages": traffic, "device_options": options,
            "pricing_plan_kind": "bypass", "validated": validated}
