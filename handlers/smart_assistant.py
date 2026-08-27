import logging
import re
import unicodedata

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import SUPPORT_URL


logger = logging.getLogger(__name__)

router = Router()


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9\s]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _has_keyword_like(text: str, words: tuple[str, ...]) -> bool:
    """Проверка с небольшим запасом на частые опечатки без AI/API."""
    tokens = text.split()
    for word in words:
        if word in text:
            return True
        if len(word) < 5:
            continue
        prefix = word[:4]
        if any(token.startswith(prefix) for token in tokens):
            return True
    return False


def _button(text: str, callback_data: str | None = None, url: str | None = None, style: str = "primary") -> InlineKeyboardButton:
    if url:
        return InlineKeyboardButton(text=text, url=url, style=style)
    return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)


def _default_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [_button("🛒 Купить подписку", "buy_subscription", style="success")],
        [_button("🔑 Мои подписки", "my_subscriptions")],
        [_button("✅ Проверить оплату", "check_payment")],
        [_button("📲 Как подключить", "how_to_connect")],
    ]
    if SUPPORT_URL:
        rows.append([_button("🆘 Поддержка", url=SUPPORT_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _detect_intent(text: str) -> str:
    if _has_keyword_like(text, ("возврат", "вернуть", "верните", "отмена", "отменить")) and _has_any(text, ("деньги", "покуп", "оплат", "платеж", "подпис", "ключ", "возврат")):
        return "refund"

    if _has_any(text, ("промокод", "промо код", "промо", "купон", "скидочн", "скидка", "код скид")):
        return "promo"

    if (
        _has_any(text, ("оплатил", "оплатила", "оплатилa", "оплачено", "заплатил", "заплатила", "аплатил", "аплатила"))
        or _has_any(text, ("деньги спис", "платеж прош", "платежка прош"))
        or ("провер" in text and _has_any(text, ("оплат", "платеж", "счет")))
    ):
        return "payment_check"

    if (
        _has_any(text, ("где", "пришл", "пришел", "пришла", "получ", "покаж", "найти", "не вижу", "нету", "нет"))
        and _has_any(text, ("ключ", "кюч", "клч", "подпис", "доступ", "ссылк", "vpn", "впн"))
    ) or _has_any(text, ("ключ не приш", "кюч не приш", "нет ключ", "нет кюч", "мой ключ", "моя подпис", "мои подпис")):
        return "my_key"

    if (
        _has_keyword_like(text, ("подключ", "падключ", "подкл", "настро", "инструкц", "инструкцыя", "установ", "устанав", "happ", "хапп", "прилож"))
        or _has_any(text, ("добавить ключ", "вставить ключ", "добавить кюч", "вставить кюч"))
        or ("как" in text and _has_any(text, ("включ", "польз", "запустить", "vpn", "впн")))
    ):
        return "connect"

    if (
        _has_keyword_like(text, ("куп", "купить", "купит", "оплат", "аплат", "продл", "продлить", "заказ", "оформ", "хочу", "нужен", "тариф", "стоим", "цена", "прайс"))
        and _has_any(text, ("ключ", "кюч", "клч", "подпис", "vpn", "впн", "доступ", "интернет", "сервис", "месяц", "дней", "оплата"))
    ) or _has_any(text, ("как оплатить", "где оплатить", "хочу vpn", "хочу впн", "купить ключ", "купить кюч", "купить впн", "купить vpn")):
        return "buy"

    if _has_any(text, ("поддерж", "оператор", "админ", "помог", "помощ", "человек", "саппорт", "проблем")):
        return "support"

    return "unknown"


def _response_for_intent(intent: str) -> tuple[str, InlineKeyboardMarkup]:
    if intent == "buy":
        return (
            "Нажмите кнопку ниже. Бот покажет виды подписки, сроки и способы оплаты.",
            InlineKeyboardMarkup(inline_keyboard=[
                [_button("🛒 Купить подписку", "buy_subscription", style="success")],
                [_button("📲 Как подключить", "how_to_connect")],
            ]),
        )

    if intent == "my_key":
        return (
            "Ключ находится в разделе «Мои подписки». Нажмите кнопку ниже.",
            InlineKeyboardMarkup(inline_keyboard=[
                [_button("🔑 Мои подписки", "my_subscriptions")],
                [_button("🛒 Купить подписку", "buy_subscription", style="success")],
            ]),
        )

    if intent == "payment_check":
        return (
            "Если вы уже оплатили, нажмите «Проверить оплату». Бот проверит платёж и активирует подписку.",
            InlineKeyboardMarkup(inline_keyboard=[
                [_button("✅ Проверить оплату", "check_payment", style="success")],
                [_button("🔑 Мои подписки", "my_subscriptions")],
            ]),
        )

    if intent == "connect":
        return (
            "Откройте инструкцию и выберите Android или iPhone.",
            InlineKeyboardMarkup(inline_keyboard=[
                [_button("📲 Как подключить", "how_to_connect", style="success")],
                [_button("🔑 Мои подписки", "my_subscriptions")],
            ]),
        )

    if intent == "promo":
        return (
            "Промокод находится в разделе «Мои подписки».",
            InlineKeyboardMarkup(inline_keyboard=[
                [_button("🔑 Мои подписки", "my_subscriptions", style="success")],
                [_button("🎟 Ввести промокод", "enter_promo")],
            ]),
        )

    if intent == "refund":
        return (
            "Возврат доступен в течение 3 суток после покупки или продления.",
            InlineKeyboardMarkup(inline_keyboard=[
                [_button("↩️ Оформить возврат", "refund_start", style="success")],
                [_button("🆘 Поддержка", url=SUPPORT_URL)] if SUPPORT_URL else [_button("🏠 Главное меню", "back_to_menu")],
            ]),
        )

    if intent == "support":
        rows = [[_button("🆘 Написать в поддержку", url=SUPPORT_URL, style="success")]] if SUPPORT_URL else []
        rows.append([_button("🏠 Главное меню", "back_to_menu")])
        return (
            "Если что-то не получается, напишите в поддержку.",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    return (
        "Я могу помочь с покупкой, оплатой, ключом или подключением. Выберите действие.",
        _default_keyboard(),
    )


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def process_free_text_help(message: Message):
    raw_text = message.text or ""
    normalized = _normalize_text(raw_text)
    intent = _detect_intent(normalized)
    logger.info(
        "Smart assistant intent: user=%s username=%s intent=%s text=%s",
        message.from_user.id,
        message.from_user.username or "",
        intent,
        raw_text[:200],
    )

    text, keyboard = _response_for_intent(intent)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "refund_hint")
async def process_refund_hint(callback: CallbackQuery, state: FSMContext):
    """Старая кнопка из уже отправленных сообщений открывает новый возврат."""
    from handlers.subscription import process_refund_start
    await process_refund_start(callback, state)
