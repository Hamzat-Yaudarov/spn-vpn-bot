"""Contest announcement copy and durable Telegram delivery."""

import asyncio
from datetime import timedelta
from html import escape
from itertools import groupby
import logging
from typing import Mapping
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

import database as db
from services import contest


logger = logging.getLogger(__name__)
SARATOV = ZoneInfo("Europe/Saratov")
JOIN_PREFIX = "contest_join:"
START_PREFIX = "contest_"
SEND_INTERVAL = 0.10
POLL_INTERVAL = 10
TELEGRAM_TIMEOUT = 20
BATCH_SIZE = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS contest_announcement_batches (
    contest_slug TEXT PRIMARY KEY REFERENCES contests(slug)
);
CREATE TABLE IF NOT EXISTS contest_announcement_deliveries (
    contest_slug TEXT NOT NULL REFERENCES contest_announcement_batches(contest_slug),
    destination TEXT NOT NULL,
    is_channel BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'unreachable')),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INTEGER NOT NULL DEFAULT 0,
    telegram_chat_id BIGINT,
    message_id BIGINT,
    shown_count BIGINT,
    count_final BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (contest_slug, destination)
);
CREATE INDEX IF NOT EXISTS contest_announcement_due
    ON contest_announcement_deliveries (contest_slug, is_channel DESC, next_attempt_at, destination)
    WHERE status = 'pending';
ALTER TABLE contest_announcement_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE contest_announcement_deliveries ENABLE ROW LEVEL SECURITY;
"""


def _number(value: int) -> str:
    return f"{value:,}".replace(",", "\u00a0")


def build_announcement(campaign: Mapping, *, starts_now: bool = False) -> str:
    """Render the same rules in previews, in private chats, and in the channel."""
    prize_lines = []
    for (kind, amount), prizes in groupby(contest.PRIZES, key=lambda prize: (prize.kind, prize.amount)):
        ranks = [prize.rank for prize in prizes]
        place = f"{ranks[0]}–{ranks[-1]} места" if len(ranks) > 1 else f"{ranks[0]} место"
        unit = "₽" if kind == "cash" else "дней подписки"
        prize_lines.append(f"{place} — <b>{_number(amount)} {unit}</b>")

    starts = campaign["starts_at"].astimezone(SARATOV).strftime("%d.%m.%Y в %H:%M")
    ends = campaign["ends_at"].astimezone(SARATOV).strftime("%d.%m.%Y в %H:%M")
    header = "Конкурс начался!\n" if starts_now else ""
    rules_url = campaign.get("rules_url")
    rules_link = f'\n\n<a href="{escape(rules_url, quote=True)}">Полные правила конкурса</a>' if rules_url else ""
    return (
        f"🏆 <b>{escape(campaign['title'])}</b>\n"
        f"{header}Старт — {starts}.\nОкончание — {ends}.\nВремя указано по Саратову (UTC+4).\n\n"
        "<b>10 призовых мест</b>\n"
        + "\n".join(prize_lines)
        + "\n\nНажмите <b>«Участвовать»</b>, чтобы записаться. Условия можно выполнить после записи.\n\n"
        "<b>Чтобы получить приз, если вам выпадет место</b>\n"
        "1. К завершению конкурса на одной действующей подписке должно остаться не менее "
        f"<b>{contest.MIN_SUBSCRIPTION_DAYS} дней</b>.\n"
        "2. Пригласите хотя бы одного друга по своей ссылке из раздела «Заработать». "
        "За время конкурса он должен впервые зарегистрироваться по этой ссылке "
        "и впервые оплатить любую подписку. Пробные подписки, оплата с реферального баланса "
        "и платежи с возвратом, подтверждённым до окончания конкурса, не засчитываются.\n\n"
        "Места случайно распределяются среди всех записавшихся. Если участник с призовым местом "
        "не выполнил условия, его приз не выдаётся и не разыгрывается повторно."
        f"{rules_link}"
    )


def start_link(bot_username: str, slug: str) -> str:
    return f"https://t.me/{bot_username.removeprefix('@')}?start={START_PREFIX}{slug}"


def participation_keyboard(
    slug: str,
    count: int,
    *,
    closed: bool = False,
    joined: bool = False,
    bot_username: str | None = None,
) -> InlineKeyboardMarkup:
    if closed:
        label = "Приём заявок завершён"
    else:
        label = "Вы участвуете" if joined else "Участвовать"
    rows = [[InlineKeyboardButton(text=f"{label} · {_number(count)}", callback_data=f"{JOIN_PREFIX}{slug}")]]
    if bot_username and not closed:
        rows.append([InlineKeyboardButton(text="Вступить через бота", url=start_link(bot_username, slug))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def initialize() -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('contest-announcement-schema'))")
        await conn.execute(SCHEMA)


async def _prepare_delivery(campaign: Mapping) -> None:
    slug = campaign["slug"]
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        new_batch = await conn.fetchval(
            """
            INSERT INTO contest_announcement_batches(contest_slug)
            SELECT slug FROM contests
            WHERE slug = $1 AND published_at IS NOT NULL
              AND starts_at <= clock_timestamp() AND ends_at > clock_timestamp()
            ON CONFLICT DO NOTHING RETURNING contest_slug
            """, slug,
        )
        if new_batch is None:
            return
        if campaign["channel_chat_id"] is not None:
            await conn.execute(
                """
                INSERT INTO contest_announcement_deliveries(contest_slug, destination, is_channel)
                VALUES($1, $2, TRUE)
                """, slug, str(campaign["channel_chat_id"]),
            )
        await conn.execute(
            """
            INSERT INTO contest_announcement_deliveries(contest_slug, destination)
            SELECT $1, tg_id::text FROM users WHERE tg_id > 0
            """, slug,
        )


def _unreachable(exc: Exception) -> bool:
    if isinstance(exc, TelegramForbiddenError):
        return True
    return isinstance(exc, TelegramBadRequest) and any(
        reason in str(exc).lower()
        for reason in ("chat not found", "user not found", "can't initiate conversation", "user is deactivated")
    )


async def _send_next(bot: Bot, campaign: Mapping, text: str) -> tuple[bool, float]:
    slug = campaign["slug"]
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT d.*, (SELECT count(*) FROM contest_participants WHERE contest_slug = $1) AS participants
            FROM contest_announcement_deliveries d
            JOIN contests c ON c.slug = d.contest_slug
            WHERE d.contest_slug = $1 AND d.status = 'pending' AND d.next_attempt_at <= clock_timestamp()
              AND c.published_at IS NOT NULL AND c.ends_at > clock_timestamp()
            ORDER BY d.is_channel DESC, d.next_attempt_at, d.destination
            LIMIT 1 FOR UPDATE OF d SKIP LOCKED
            """, slug,
        )
        if row is None:
            return False, 0

        destination = row["destination"]
        chat_id = destination if destination.startswith("@") else int(destination)
        try:
            username = (await asyncio.wait_for(bot.me(), TELEGRAM_TIMEOUT)).username if row["is_channel"] else None
            message = await asyncio.wait_for(
                bot.send_message(
                    chat_id, text, parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    reply_markup=participation_keyboard(slug, row["participants"], bot_username=username),
                ),
                TELEGRAM_TIMEOUT,
            )
        except TelegramRetryAfter as exc:
            delay = max(1.0, float(exc.retry_after))
            await _retry_after(conn, slug, destination, delay)
            return True, delay
        except Exception as exc:
            if not row["is_channel"] and _unreachable(exc):
                await conn.execute(
                    "UPDATE contest_announcement_deliveries SET status = 'unreachable' "
                    "WHERE contest_slug = $1 AND destination = $2", slug, destination,
                )
            else:
                logger.warning("Contest %s announcement failed (%s): %s", slug,
                               "channel" if row["is_channel"] else "private chat", type(exc).__name__)
                await _retry_after(conn, slug, destination, 5 * 2 ** min(row["attempts"], 6))
            return True, SEND_INTERVAL

        await conn.execute(
            """
            UPDATE contest_announcement_deliveries
            SET status = 'sent', telegram_chat_id = $3, message_id = $4, shown_count = $5, attempts = 0
            WHERE contest_slug = $1 AND destination = $2
            """, slug, destination, message.chat.id, message.message_id, row["participants"],
        )
        return True, SEND_INTERVAL


async def _retry_after(conn, slug: str, destination: str, seconds: float) -> None:
    await conn.execute(
        """
        UPDATE contest_announcement_deliveries
        SET next_attempt_at = clock_timestamp() + $3::interval, attempts = attempts + 1
        WHERE contest_slug = $1 AND destination = $2
        """, slug, destination, timedelta(seconds=seconds),
    )


async def _refresh_channel_button(bot: Bot) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT d.*, counts.participants, (c.ends_at <= clock_timestamp()) AS closed
            FROM contest_announcement_deliveries d
            JOIN contests c ON c.slug = d.contest_slug
            CROSS JOIN LATERAL (
                SELECT count(*) AS participants FROM contest_participants p WHERE p.contest_slug = d.contest_slug
            ) counts
            WHERE d.is_channel AND d.status = 'sent' AND NOT d.count_final
              AND d.next_attempt_at <= clock_timestamp()
              AND (c.ends_at <= clock_timestamp() OR d.shown_count <> counts.participants)
            ORDER BY c.ends_at
            LIMIT 1 FOR UPDATE OF d SKIP LOCKED
            """,
        )
        if row is None:
            return
        slug, destination, closed = row["contest_slug"], row["destination"], row["closed"]
        try:
            username = (await asyncio.wait_for(bot.me(), TELEGRAM_TIMEOUT)).username if not closed else None
            await asyncio.wait_for(
                bot.edit_message_reply_markup(
                    chat_id=row["telegram_chat_id"], message_id=row["message_id"],
                    reply_markup=participation_keyboard(slug, row["participants"], closed=closed, bot_username=username),
                ), TELEGRAM_TIMEOUT,
            )
        except TelegramRetryAfter as exc:
            await _retry_after(conn, slug, destination, max(1.0, float(exc.retry_after)))
            return
        except Exception as exc:
            # A successful edit followed by a lost response is safe to checkpoint on retry.
            if not (isinstance(exc, TelegramBadRequest) and "message is not modified" in str(exc).lower()):
                logger.warning("Could not refresh contest %s channel count: %s", slug, type(exc).__name__)
                await _retry_after(conn, slug, destination, 60 * 2 ** min(row["attempts"], 5))
                return
        await conn.execute(
            """
            UPDATE contest_announcement_deliveries SET shown_count = $3, count_final = $4, attempts = 0
            WHERE contest_slug = $1 AND destination = $2
            """, slug, destination, row["participants"], closed,
        )


async def run_delivery_loop(bot: Bot) -> None:
    while True:
        try:
            await _refresh_channel_button(bot)
            campaign = await contest.get_current_campaign()
            if campaign is not None:
                await _prepare_delivery(campaign)
                text = build_announcement(campaign, starts_now=True)
                for _ in range(BATCH_SIZE):
                    sent_or_retried, delay = await _send_next(bot, campaign, text)
                    if not sent_or_retried:
                        break
                    await asyncio.sleep(delay)
                else:
                    # Interleave sustained delivery with channel counter updates.
                    continue
        except Exception:
            logger.exception("Contest delivery cycle failed")
        await asyncio.sleep(POLL_INTERVAL)
