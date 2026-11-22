"""Contest entry points in bot chats and channel announcements."""

import logging
import re
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message

import database as db
from services import contest, contest_delivery
from services.referral_program import referral_link


logger = logging.getLogger(__name__)
router = Router()
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
_ENTERED = {
    "joined": "Вы участвуете! Выполните условия конкурса, чтобы получить приз, если вам выпадет призовое место.",
    "already_joined": "Вы уже участвуете. Выполните условия конкурса, чтобы получить приз, если вам выпадет призовое место.",
}
_CLOSED = "Приём заявок на этот конкурс сейчас закрыт."
_RETRY = "Не удалось записать вас на конкурс. Попробуйте ещё раз."


def is_start_payload(payload: str | None) -> bool:
    return bool(payload and payload.startswith(contest_delivery.START_PREFIX)
                and _SLUG.fullmatch(payload[len(contest_delivery.START_PREFIX):]))


async def process_start_payload(message: Message, payload: str) -> None:
    """Join from a channel deep link after normal /start user creation."""
    if not is_start_payload(payload) or message.chat.type != ChatType.PRIVATE or message.from_user is None:
        return
    slug = payload[len(contest_delivery.START_PREFIX):]
    try:
        enrollment = await contest.enroll(slug, message.from_user.id)
    except Exception:
        logger.exception("Could not enroll a contest participant from /start")
        await message.answer(_RETRY)
        return
    await message.answer(_ENTERED.get(enrollment.status, _CLOSED))


@router.message(Command("contest"), F.chat.type == ChatType.PRIVATE)
async def show_contest(message: Message) -> None:
    campaign = await contest.get_current_campaign()
    if campaign is None:
        await message.answer("Сейчас нет открытого конкурса.")
        return
    slug = campaign["slug"]
    count = await contest.count_participants(slug)
    text = contest_delivery.build_announcement(campaign)
    if message.from_user and await db.user_exists(message.from_user.id):
        link = referral_link((await message.bot.me()).username, message.from_user.id)
        text += f"\n\n<b>Ваша ссылка для друзей</b>\n<code>{escape(link)}</code>"
    await message.answer(
        text,
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=contest_delivery.participation_keyboard(slug, count),
    )


@router.callback_query(F.data.startswith(contest_delivery.JOIN_PREFIX))
async def join_contest(callback: CallbackQuery) -> None:
    slug = callback.data[len(contest_delivery.JOIN_PREFIX):]
    if not _SLUG.fullmatch(slug):
        await callback.answer(_CLOSED, show_alert=True)
        return

    try:
        enrollment = await contest.enroll(slug, callback.from_user.id)
        if enrollment.status == "unknown_user":
            bot = await callback.bot.me()
            await callback.answer(url=contest_delivery.start_link(bot.username, slug))
            return
    except Exception:
        logger.exception("Could not enroll a contest participant from a callback")
        await callback.answer(_RETRY, show_alert=True)
        return

    await callback.answer(_ENTERED.get(enrollment.status, _CLOSED), show_alert=True)
    message = callback.message
    if isinstance(message, Message) and message.chat.type == ChatType.PRIVATE:
        try:
            await message.edit_reply_markup(reply_markup=contest_delivery.participation_keyboard(
                slug, enrollment.count, closed=enrollment.status not in _ENTERED,
                joined=enrollment.status in _ENTERED,
            ))
        except TelegramAPIError:
            # Old, deleted, and unchanged private messages do not invalidate enrollment.
            logger.debug("Could not refresh a private contest keyboard for %s", slug)
