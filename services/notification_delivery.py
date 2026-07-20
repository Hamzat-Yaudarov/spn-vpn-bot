"""Состояние доставки фоновых Telegram-уведомлений."""

import database as db


TELEGRAM_DELIVERY_BLOCKED_NOTIFICATION_TYPE = "telegram_delivery_blocked"


async def is_telegram_delivery_blocked(tg_id: int) -> bool:
    """Не отправлять фоновые уведомления в заведомо недоступный Telegram-чат."""
    row = await db.db_execute(
        """
        SELECT 1
        FROM notification_state
        WHERE tg_id = $1
          AND subscription_id = 0
          AND notification_type = $2
        """,
        (tg_id, TELEGRAM_DELIVERY_BLOCKED_NOTIFICATION_TYPE),
        fetch_one=True,
    )
    return row is not None


async def mark_telegram_delivery_blocked(tg_id: int) -> None:
    """Запомнить постоянную ошибку доставки до следующего /start пользователя."""
    await db.db_execute(
        """
        INSERT INTO notification_state (tg_id, subscription_id, notification_type, last_sent_at, updated_at)
        VALUES ($1, 0, $2, now(), now())
        ON CONFLICT (tg_id, subscription_id, notification_type)
        DO UPDATE SET last_sent_at = now(), updated_at = now()
        """,
        (tg_id, TELEGRAM_DELIVERY_BLOCKED_NOTIFICATION_TYPE),
    )


async def clear_telegram_delivery_blocked(tg_id: int) -> None:
    """Пользователь снова открыл бот — разрешить фоновые уведомления."""
    await db.db_execute(
        """
        DELETE FROM notification_state
        WHERE tg_id = $1
          AND subscription_id = 0
          AND notification_type = $2
        """,
        (tg_id, TELEGRAM_DELIVERY_BLOCKED_NOTIFICATION_TYPE),
    )
