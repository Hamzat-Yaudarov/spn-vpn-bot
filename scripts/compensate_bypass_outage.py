#!/usr/bin/env python3
"""Safely compensate bypass subscriptions affected on 16-19 September 2026.

Dry-run is the default. Apply requires a stopped bot, creates a durable journal,
backs up every selected database row, sets a fixed expiry in Remnawave and the
bot database, and sends at most one Telegram message per user.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_CAMPAIGN = "bypass-outage-2026-09-16-19"
# 16.09 00:00 through 19.09 23:59:59 Moscow time, stored as naive UTC.
DEFAULT_WINDOW_START = datetime.fromisoformat("2026-09-15T21:00:00")
DEFAULT_WINDOW_END = datetime.fromisoformat("2026-09-19T21:00:00")
DEFAULT_DAYS = 5


def utcnow() -> datetime:
    """Naive UTC, matching the project's TIMESTAMP columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscription_compensations (
    campaign_key TEXT NOT NULL,
    subscription_id BIGINT NOT NULL,
    tg_id BIGINT NOT NULL,
    original_expires_at TIMESTAMP NOT NULL,
    target_expires_at TIMESTAMP NOT NULL,
    status TEXT NOT NULL DEFAULT 'prepared',
    remote_applied_at TIMESTAMP,
    local_applied_at TIMESTAMP,
    notification_status TEXT,
    notified_at TIMESTAMP,
    last_error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_key, subscription_id)
)
"""


CANDIDATE_QUERY = """
SELECT s.*
FROM subscriptions AS s
WHERE s.tg_id > 0
  AND s.plan_kind = 'bypass'
  AND s.remnawave_uuid IS NOT NULL
  AND s.subscription_until IS NOT NULL
  AND s.subscription_until > $1
  AND s.created_at < $2
  AND (
        (s.generation = 'v2' AND s.is_visible IS TRUE)
        OR s.legacy_readonly IS TRUE
      )
  AND NOT (
        EXISTS (
            SELECT 1
            FROM payment_subscription_activations AS later_activation
            WHERE later_activation.subscription_id = s.id
              AND later_activation.created_at >= $2
        )
        AND NOT EXISTS (
            SELECT 1
            FROM payment_subscription_activations AS earlier_activation
            WHERE earlier_activation.subscription_id = s.id
              AND earlier_activation.created_at < $2
              AND earlier_activation.expires_at > $1
        )
        AND NOT EXISTS (
            SELECT 1
            FROM payments AS earlier_payment
            WHERE earlier_payment.subscription_id = s.id
              AND earlier_payment.status = 'paid'
              AND COALESCE(earlier_payment.payment_kind, 'subscription') = 'subscription'
              AND earlier_payment.updated_at < $2
        )
      )
  AND NOT (
        EXISTS (
            SELECT 1
            FROM payments AS later_payment
            WHERE later_payment.subscription_id = s.id
              AND later_payment.status = 'paid'
              AND COALESCE(later_payment.payment_kind, 'subscription') = 'subscription'
              AND later_payment.updated_at >= $2
        )
        AND NOT EXISTS (
            SELECT 1
            FROM payments AS earlier_payment
            WHERE earlier_payment.subscription_id = s.id
              AND earlier_payment.status = 'paid'
              AND COALESCE(earlier_payment.payment_kind, 'subscription') = 'subscription'
              AND earlier_payment.updated_at < $2
        )
        AND NOT EXISTS (
            SELECT 1
            FROM payment_subscription_activations AS earlier_activation
            WHERE earlier_activation.subscription_id = s.id
              AND earlier_activation.created_at < $2
              AND earlier_activation.expires_at > $1
        )
      )
  -- A non-payment slot may have existed for months and then have been
  -- manually reactivated only after the outage. Exclude it only when all
  -- three pieces of evidence agree: an old expiry notice, a new traffic
  -- period started after the window, and that start reconstructs the whole
  -- current term. This avoids excluding ordinary post-window traffic syncs.
  AND NOT (
        s.last_traffic_sync_at >= $2
        AND COALESCE(s.purchase_days, 0) > 0
        AND abs(
              extract(epoch FROM (s.subscription_until - s.last_traffic_sync_at))
              - (s.purchase_days * 86400)
            ) < 600
        AND EXISTS (
            SELECT 1
            FROM notification_state AS old_expiry_notice
            WHERE old_expiry_notice.subscription_id = s.id
              AND old_expiry_notice.notification_type IN ('expires_today', 'expired')
              AND old_expiry_notice.last_sent_at < $1
        )
        AND NOT EXISTS (
            SELECT 1
            FROM payments AS earlier_payment
            WHERE earlier_payment.subscription_id = s.id
              AND earlier_payment.status = 'paid'
              AND COALESCE(earlier_payment.payment_kind, 'subscription') = 'subscription'
              AND earlier_payment.updated_at < $2
        )
        AND NOT EXISTS (
            SELECT 1
            FROM payment_subscription_activations AS earlier_activation
            WHERE earlier_activation.subscription_id = s.id
              AND earlier_activation.created_at < $2
              AND earlier_activation.expires_at > $1
        )
      )
ORDER BY s.tg_id, s.id
"""


def require_stopped(service: str) -> None:
    result = subprocess.run(
        ["systemctl", "show", service, "-p", "ActiveState", "--value"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() not in {"inactive", "failed"}:
        raise RuntimeError(f"Сначала остановите бот: sudo systemctl stop {service}")


def compensation_expiry(current_expiry: datetime, now: datetime, days: int) -> datetime:
    """Give active subscriptions +days and expired subscriptions days usable now."""
    return max(current_expiry, now) + timedelta(days=days)


def compensation_message(subscription_count: int, days: int) -> str:
    if subscription_count == 1:
        subject = "Ваша подписка с антиглушилкой продлена"
    else:
        subject = f"Ваши подписки с антиглушилкой ({subscription_count}) продлены"
    return (
        "🎁 <b>Компенсация начислена</b>\n\n"
        "За период, когда не работали обходы, "
        f"{subject.lower()} на <b>{days} дней</b>."
    )


def write_backup(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream, ensure_ascii=False, default=str, indent=2)


async def table_exists(conn, table_name: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1)", f"public.{table_name}"))


async def select_candidates(conn, start: datetime, end: datetime):
    if not await table_exists(conn, "payment_subscription_activations"):
        raise RuntimeError("Нет журнала payment_subscription_activations; безопасный отбор невозможен")
    return await conn.fetch(CANDIDATE_QUERY, start, end)


async def prepare_journal(conn, args, candidates, now: datetime) -> int:
    await conn.execute(JOURNAL_SCHEMA)
    inserted = 0
    async with conn.transaction():
        for subscription in candidates:
            target = compensation_expiry(subscription["subscription_until"], now, args.days)
            result = await conn.execute(
                """
                INSERT INTO subscription_compensations (
                    campaign_key, subscription_id, tg_id,
                    original_expires_at, target_expires_at
                )
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (campaign_key, subscription_id) DO NOTHING
                """,
                args.campaign,
                subscription["id"],
                subscription["tg_id"],
                subscription["subscription_until"],
                target,
            )
            inserted += int(result.endswith(" 1"))
    return inserted


async def journal_rows(conn, campaign: str):
    return await conn.fetch(
        """
        SELECT journal.*, subscription.remnawave_uuid, subscription.plan_kind,
               subscription.type_index, subscription.slot_number,
               subscription.subscription_until AS current_expires_at
        FROM subscription_compensations AS journal
        JOIN subscriptions AS subscription ON subscription.id = journal.subscription_id
        WHERE journal.campaign_key = $1
        ORDER BY journal.tg_id, journal.subscription_id
        """,
        campaign,
    )


async def record_error(conn, campaign: str, subscription_id: int, error: str) -> None:
    await conn.execute(
        """
        UPDATE subscription_compensations
        SET status = 'failed', last_error = $3, updated_at = now()
        WHERE campaign_key = $1 AND subscription_id = $2
        """,
        campaign,
        subscription_id,
        error[:1000],
    )


async def apply_expiries(conn, args, rows) -> tuple[int, int]:
    import database as db
    from services.remnawave import remnawave_set_subscription_expiry

    applied = failed = 0
    for row in rows:
        if row["status"] == "applied":
            continue
        subscription_id = row["subscription_id"]
        try:
            if row["plan_kind"] != "bypass" or not row["remnawave_uuid"]:
                raise RuntimeError("Подписка больше не является выданной антиглушилкой")
            # The target date was fixed before the API call. Repeating this PATCH
            # after a timeout cannot add another five days.
            remote_ok = await remnawave_set_subscription_expiry(
                None,
                row["remnawave_uuid"],
                row["target_expires_at"],
            )
            if not remote_ok:
                raise RuntimeError("Remnawave не подтвердил новый срок")
            await conn.execute(
                """
                UPDATE subscription_compensations
                SET remote_applied_at = COALESCE(remote_applied_at, now()),
                    last_error = NULL, updated_at = now()
                WHERE campaign_key = $1 AND subscription_id = $2
                """,
                args.campaign,
                subscription_id,
            )
            await db.sync_subscription_expiry(subscription_id, row["target_expires_at"])
            await conn.execute(
                """
                UPDATE subscription_compensations
                SET status = 'applied', local_applied_at = COALESCE(local_applied_at, now()),
                    last_error = NULL, updated_at = now()
                WHERE campaign_key = $1 AND subscription_id = $2
                """,
                args.campaign,
                subscription_id,
            )
            applied += 1
            print(f"subscription={subscription_id}: +{args.days} дней применено", flush=True)
        except Exception as exc:
            failed += 1
            await record_error(conn, args.campaign, subscription_id, str(exc))
            print(f"subscription={subscription_id}: ОШИБКА: {str(exc)[:200]}", flush=True)
    return applied, failed


async def send_notifications(conn, args) -> tuple[int, int, int]:
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
    import config

    groups = await conn.fetch(
        """
        SELECT tg_id, count(*) AS subscription_count
        FROM subscription_compensations
        WHERE campaign_key = $1
        GROUP BY tg_id
        HAVING bool_and(status = 'applied')
           AND bool_and(notification_status IS NULL)
        ORDER BY tg_id
        """,
        args.campaign,
    )
    sent = unreachable = failed = 0
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        for index, group in enumerate(groups):
            tg_id = group["tg_id"]
            text = compensation_message(group["subscription_count"], args.days)
            try:
                for attempt in range(3):
                    try:
                        await bot.send_message(tg_id, text)
                        break
                    except TelegramRetryAfter as exc:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(float(exc.retry_after) + 0.25)
                await conn.execute(
                    """
                    UPDATE subscription_compensations
                    SET notification_status = 'sent', notified_at = now(),
                        last_error = NULL, updated_at = now()
                    WHERE campaign_key = $1 AND tg_id = $2
                    """,
                    args.campaign,
                    tg_id,
                )
                sent += 1
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                await conn.execute(
                    """
                    UPDATE subscription_compensations
                    SET notification_status = 'unreachable', last_error = $3, updated_at = now()
                    WHERE campaign_key = $1 AND tg_id = $2
                    """,
                    args.campaign,
                    tg_id,
                    f"Telegram: {str(exc)[:900]}",
                )
                unreachable += 1
            except Exception as exc:
                await conn.execute(
                    """
                    UPDATE subscription_compensations
                    SET last_error = $3, updated_at = now()
                    WHERE campaign_key = $1 AND tg_id = $2
                    """,
                    args.campaign,
                    tg_id,
                    f"Telegram: {str(exc)[:900]}",
                )
                failed += 1
            if index + 1 < len(groups):
                await asyncio.sleep(0.06)
    finally:
        await bot.session.close()
    return sent, unreachable, failed


async def run(args) -> int:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    import asyncpg
    import config
    import database as db

    if args.days <= 0:
        raise RuntimeError("Количество дней должно быть положительным")
    if args.window_start >= args.window_end:
        raise RuntimeError("Начало периода должно быть раньше окончания")
    if args.apply:
        require_stopped(args.service)
        if config.REMNAWAVE_API_VERSION != 3:
            raise RuntimeError("В .env бота требуется REMNAWAVE_API_VERSION=3")

    # Deliberately do not call init_db(): dry-run must not alter the schema.
    db._pool = await asyncpg.create_pool(
        config.DATABASE_URL,
        min_size=1,
        max_size=4,
        command_timeout=60,
    )
    try:
        async with db._pool.acquire() as conn:
            candidates = await select_candidates(conn, args.window_start, args.window_end)
            now = utcnow()
            active = sum(row["subscription_until"] > now for row in candidates)
            expired = len(candidates) - active
            users = len({row["tg_id"] for row in candidates})
            print(
                f"Подходящих антиглушилок: {len(candidates)}; пользователей: {users}; "
                f"сейчас активны: {active}; сейчас истекли: {expired}",
                flush=True,
            )
            if not args.apply:
                if await table_exists(conn, "subscription_compensations"):
                    existing = await conn.fetchval(
                        "SELECT count(*) FROM subscription_compensations WHERE campaign_key = $1",
                        args.campaign,
                    )
                    print(f"Уже есть в журнале кампании: {existing}")
                print("ТОЛЬКО ПРОВЕРКА. Сроки, база и сообщения не изменялись.")
                return 0

            require_stopped(args.service)
            payment_rows = await conn.fetch(
                """
                SELECT payment.*
                FROM payments AS payment
                WHERE payment.subscription_id = ANY($1::bigint[])
                ORDER BY payment.id
                """,
                [row["id"] for row in candidates],
            ) if candidates else []
            activation_rows = await conn.fetch(
                """
                SELECT activation.*
                FROM payment_subscription_activations AS activation
                WHERE activation.subscription_id = ANY($1::bigint[])
                ORDER BY activation.created_at
                """,
                [row["id"] for row in candidates],
            ) if candidates else []
            backup_path = args.backup_dir / "selected-rows.json"
            write_backup(
                backup_path,
                {
                    "campaign": args.campaign,
                    "window_start_utc": args.window_start,
                    "window_end_utc": args.window_end,
                    "days": args.days,
                    "created_at_utc": now,
                    "subscriptions": [dict(row) for row in candidates],
                    "payments": [dict(row) for row in payment_rows],
                    "payment_subscription_activations": [dict(row) for row in activation_rows],
                },
            )
            print(f"Резервная копия выбранных строк: {backup_path}", flush=True)
            prepared = await prepare_journal(conn, args, candidates, now)
            print(f"Новых записей в журнале компенсации: {prepared}", flush=True)
            rows = await journal_rows(conn, args.campaign)

        async with db._pool.acquire() as conn:
            applied, apply_failed = await apply_expiries(conn, args, rows)
            sent, unreachable, notify_failed = await send_notifications(conn, args)
            summary = await conn.fetchrow(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE status = 'applied') AS applied,
                       count(*) FILTER (WHERE status <> 'applied') AS failed,
                       count(DISTINCT tg_id) FILTER (WHERE notification_status = 'sent') AS notified,
                       count(DISTINCT tg_id) FILTER (WHERE notification_status = 'unreachable') AS unreachable
                FROM subscription_compensations
                WHERE campaign_key = $1
                """,
                args.campaign,
            )
        print(
            "ИТОГ: "
            f"подписок={summary['total']}, успешно={summary['applied']}, ошибок={summary['failed']}; "
            f"уведомлено пользователей={summary['notified']}, недоступны={summary['unreachable']}; "
            f"за этот запуск применено={applied}, отправлено={sent}",
            flush=True,
        )
        return 1 if apply_failed or notify_failed or summary["failed"] else 0
    finally:
        await db.close_db()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Применить сроки и отправить сообщения")
    parser.add_argument("--campaign", default=DEFAULT_CAMPAIGN)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--window-start", type=datetime.fromisoformat, default=DEFAULT_WINDOW_START,
                        help="Начало UTC без часового пояса")
    parser.add_argument("--window-end", type=datetime.fromisoformat, default=DEFAULT_WINDOW_END,
                        help="Конец UTC без часового пояса")
    parser.add_argument("--service", default="spn-bot")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=ROOT / ("compensation-backup-" + utcnow().strftime("%Y%m%d-%H%M%S")),
    )
    args = parser.parse_args(argv)
    if args.window_start.tzinfo is not None or args.window_end.tzinfo is not None:
        parser.error("Время задаётся в UTC без суффикса часового пояса")
    return args


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        raise SystemExit(1)
