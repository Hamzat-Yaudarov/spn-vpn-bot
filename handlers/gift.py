import logging
import aiohttp
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

    if not db.acquire_user_lock(tg_id):
        await callback.answer("Подожди пару секунд ⏳", show_alert=True)
        return

    try:
        # Проверяем получал ли пользователь уже подарок
        if db.is_gift_received(tg_id):
            await callback.answer("Ты уже получал подарок", show_alert=True)
            return

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

        # Выдаём подарок (3 дня подписки)
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            uuid, username = await remnawave_get_or_create_user(
                session,
                tg_id,
                days=3,
                extend_if_exists=True
            )

            if not uuid:
                await callback.answer(
                    "Ошибка при выдаче подарка. Попробуй позже.",
                    show_alert=True
                )
                return

            await remnawave_add_to_squad(session, uuid)
            sub_url = await remnawave_get_subscription_url(session, uuid)

        # Обновляем данные пользователя в БД
        new_until = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        db.update_subscription(tg_id, uuid, username, new_until, DEFAULT_SQUAD_UUID)
        db.mark_gift_received(tg_id)

        # Отправляем сообщение пользователю
        text = (
            "🎁 <b>Подарок получен!</b>\n\n"
            "Спасибо за подписку на канал!\n"
            "Тебе выдана подписка на 3 дня.\n\n"
            f"<b>Ссылка подписки:</b>\n<code>{sub_url}</code>"
        )

        await callback.message.edit_text(text)
        logging.info(f"Gift given to user {tg_id}")

    except Exception as e:
        logging.error(f"Get gift error: {e}")
        await callback.answer("Ошибка при получении подарка", show_alert=True)
    
    finally:
        db.release_user_lock(tg_id)
