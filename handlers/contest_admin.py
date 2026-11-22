"""Private Telegram commands for preparing and finalizing the contest."""

from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import ADMIN_ID
from services import contest, contest_delivery

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE, F.from_user.id == ADMIN_ID)

SARATOV = ZoneInfo("Europe/Saratov")
PLACE_REASONS = {
    "subscription_short": "на финише оставалось меньше 45 дней подписки",
    "no_paid_invite": "нет первой оплаты приглашённого друга",
}
PREPARE_USAGE = (
    "<code>/contest_prepare код старт финиш @канал ссылка_на_правила</code>\n"
    "Даты в формате <code>ГГГГ-ММ-ДДTЧЧ:ММ</code> по Саратову. "
    "Канал можно указать числовым ID."
)


@router.message(Command("contest_admin"))
async def contest_help(message: Message):
    await message.answer(
        "<b>Конкурс Way SPN</b>\n\n"
        f"{PREPARE_USAGE}\n\n"
        "<code>/contest_preview код</code> — проверить черновик.\n"
        "<code>/contest_publish код</code> — запланировать пост и рассылку.\n"
        "<code>/contest_draw код</code> — зафиксировать итоги после окончания и показать призы к выдаче."
    )


def _parse_local_time(raw: str) -> datetime:
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M").replace(tzinfo=SARATOV)
    except ValueError:
        raise ValueError("Введите дату и время в формате ГГГГ-ММ-ДДTЧЧ:ММ.") from None


@router.message(Command("contest_prepare"))
async def prepare_contest(message: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) != 5:
        await message.answer(PREPARE_USAGE)
        return
    slug, starts, ends, channel, rules = parts
    try:
        starts_at, ends_at = _parse_local_time(starts), _parse_local_time(ends)
        if starts_at <= datetime.now(SARATOV):
            raise ValueError("Укажите время старта в будущем.")
        await contest.create_campaign(
            slug,
            "Конкурс Way SPN",
            starts_at,
            ends_at,
            channel_chat_id=channel,
            rules_url=rules,
        )
    except ValueError as exc:
        await message.answer(escape(str(exc)))
        return
    await message.answer(f"Черновик сохранён. Проверьте его командой <code>/contest_preview {escape(slug)}</code>.")


@router.message(Command("contest_preview"))
async def preview_contest(message: Message, command: CommandObject):
    slug = (command.args or "").strip()
    campaign = await contest.get_campaign(slug) if slug else None
    if not campaign:
        await message.answer("Конкурс не найден. Укажите его код после /contest_preview.")
        return
    channel = escape(str(campaign["channel_chat_id"]))
    announcement = contest_delivery.build_announcement(campaign, starts_now=True)
    bot_user = await message.bot.me()
    entry_url = escape(contest_delivery.start_link(bot_user.username, campaign["slug"]))
    await message.answer(
        f"<b>Предпросмотр для канала {channel}</b>\n\n{announcement}\n\n"
        f"<b>Ссылка для роликов и постов</b>\n<code>{entry_url}</code>"
    )


@router.message(Command("contest_publish"))
async def publish_contest(message: Message, command: CommandObject):
    slug = (command.args or "").strip()
    campaign = await contest.get_campaign(slug) if slug else None
    if not campaign:
        await message.answer("Конкурс не найден. Укажите его код после /contest_publish.")
        return
    if campaign["published_at"] is not None:
        await message.answer("Этот конкурс уже запланирован.")
        return

    channel = campaign["channel_chat_id"]
    if isinstance(channel, str) and channel.startswith("-100"):
        channel = int(channel)
    if channel is None:
        await message.answer("Для публикации укажите канал в черновике.")
        return
    try:
        chat = await message.bot.get_chat(channel)
        member = await message.bot.get_chat_member(chat.id, message.bot.id)
    except TelegramAPIError:
        await message.answer("Канал недоступен. Добавьте бота в канал администратором с правом публикации.")
        return
    if chat.type != ChatType.CHANNEL or not (
        member.status == ChatMemberStatus.CREATOR
        or (member.status == ChatMemberStatus.ADMINISTRATOR and member.can_post_messages)
    ):
        await message.answer("Укажите канал и дайте боту право публиковать сообщения.")
        return

    try:
        await contest.publish_campaign(slug, resolved_channel_chat_id=chat.id)
    except ValueError as exc:
        await message.answer(escape(str(exc)))
        return
    await message.answer("Конкурс запланирован. В указанное время бот начнёт публикацию и рассылку.")


def _format_draw(result: contest.DrawResult) -> str:
    lines = [
        f"<b>Итоги конкурса · {escape(result.campaign['slug'])}</b>",
        f"Участников — {result.participant_count}",
        f"Оплат от приглашённых — {result.paid_invites}",
        "",
    ]
    for place in result.places:
        amount = place.prize.amount
        prize = f"{amount:,} ₽".replace(",", " ") if place.prize.kind == "cash" else f"{amount} дн."
        label = f"{place.prize.rank}. <b>{prize}</b>"
        if place.status == "unfilled":
            lines.append(f"{label} · место свободно")
            continue
        user = f"<code>{place.tg_id}</code>"
        if place.status == "won":
            lines.append(f"{label} · {user} · к выдаче")
        else:
            reasons = ", ".join(PLACE_REASONS[reason] for reason in place.reasons)
            lines.append(f"{label} · {user} · без приза ({reasons})")
        expiry = place.subscription_until
        if expiry:
            expiry = expiry.replace(tzinfo=timezone.utc).astimezone(SARATOV).strftime("%d.%m.%Y %H:%M")
        lines.append(f"   Подписка до {expiry or '—'} · оплаченных приглашений {place.paid_invites}")

    lines.append("\nВыдача призов вручную. Повтор команды показывает сохранённые итоги.")
    return "\n".join(lines)


@router.message(Command("contest_draw"))
async def draw_contest(message: Message, command: CommandObject):
    slug = (command.args or "").strip()
    if not slug:
        await message.answer("Укажите код конкурса после /contest_draw.")
        return
    try:
        result = await contest.draw(slug)
    except ValueError as exc:
        await message.answer(escape(str(exc)))
        return
    await message.answer(_format_draw(result))
