import logging
from datetime import datetime, timedelta, timezone
from aiogram import Router, F
from aiogram.types import CallbackQuery
from config import NEWS_CHANNEL_USERNAME, DEFAULT_SQUAD_UUID
import database as db
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_add_to_squad,
    remnawave_get_subscription_url
)


router = Router()


@router.callback_query(F.data == "get_gift")
async def process_get_gift(callback: CallbackQuery):
    """Обработчик получения подарка"""
    tg_id = callback.from_user.id

    # Проверка anti-spam: не более одной попытки в 2 секунды
    can_request, error_msg = await db.can_request_gift(tg_id)
    if not can_request:
        await callback.answer(error_msg, show_alert=True)
        return

    # Обновляем время последней попытки
    await db.update_last_gift_attempt(tg_id)

    async with db.UserLockContext(tg_id) as acquired:
        if not acquired:
            await callback.answer("Подожди пару секунд ⏳", show_alert=True)
            return

        try:
            # Проверяем подписку на канал новостей
            try:
                member = await callback.bot.get_chat_member(f"@{NEWS_CHANNEL_USERNAME}", tg_id)
                logging.info(f"Channel check: user={tg_id}, status={member.status}")
            except Exception as e:
                logging.error(f"get_chat_member failed: {e}")
                await callback.answer(
                    "Не удалось проверить подписку на канал. Попробуй позже.",
                    show_alert=True
                )
                return

            # Проверяем статус подписки
            if member.status not in ("member", "administrator", "creator"):
                await callback.answer(
                    f"Ты не подписан на новостной канал @{NEWS_CHANNEL_USERNAME}",
                    show_alert=True
                )
                return

            # Атомарно проверяем и отмечаем подарок
            gift_marked = await db.mark_gift_received_atomic(tg_id)
            if not gift_marked:
                await callback.answer("Ты уже получал подарок", show_alert=True)
                return

            # Выдаём подарок (3 дня подписки)
            # Импортируем здесь чтобы избежать циклической зависимости
            from main import get_global_session
            
            session = get_global_session()
            
            uuid, username = await remnawave_get_or_create_user(
                session,
                tg_id,
                days=3,
                extend_if_exists=True
            )

            if not uuid:
                logging.error(f"Failed to create/get Remnawave user for gift {tg_id}")
                await callback.answer(
                    "Ошибка при выдаче подарка. Попробуй позже.",
                    show_alert=True
                )
                return

            if not await remnawave_add_to_squad(session, uuid):
                logging.warning(f"Failed to add user {uuid} to squad, continuing anyway")

            sub_url = await remnawave_get_subscription_url(session, uuid)

            if not sub_url:
                logging.error(f"Failed to get subscription URL for gift user {tg_id}")
                await callback.answer(
                    "Ошибка при получении ссылки подписки.",
                    show_alert=True
                )
                return

            # Обновляем данные пользователя в БД
            new_until = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
            await db.update_subscription(tg_id, uuid, username, new_until, DEFAULT_SQUAD_UUID)

            # Отправляем сообщение пользователю
            text = (
                "🎁 <b>Подарок получен!</b>\n\n"
                "Спасибо за подписку на канал!\n"
                "Тебе выдана подписка на 3 дня.\n\n"
                f"<b>Ссылка подписки:</b>\n<code>{sub_url}</code>"
            )

            await callback.message.edit_text(text)
            logging.info(f"[USER:{tg_id}] Gift successfully given: +3 days")

        except Exception as e:
            logging.error(f"[USER:{tg_id}] Get gift error: {e}", exc_info=True)
            await callback.answer("Ошибка при получении подарка", show_alert=True)
