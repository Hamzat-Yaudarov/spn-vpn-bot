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
    username = callback.from_user.username
    logging.info(f"User {tg_id}(@{username}) requested gift")

    # Проверка anti-spam: не более одной попытки в 2 секунды
    can_request, error_msg = await db.can_request_gift(tg_id)
    if not can_request:
        await callback.answer(error_msg, show_alert=True)
        return

    # Обновляем время последней попытки
    await db.update_last_gift_attempt(tg_id)

    if not await db.acquire_user_lock(tg_id):
        await callback.answer("Подожди пару секунд ⏳", show_alert=True)
        return

    try:
        # Проверяем права бота в канале
        try:
            bot_info = await callback.bot.get_me()
            bot_member = await callback.bot.get_chat_member(f"@{NEWS_CHANNEL_USERNAME}", bot_info.id)

            # Проверяем, может ли бот читать членство в канале
            if bot_member.status == "restricted" and not bot_member.can_read_all_group_messages:
                logging.error(f"Bot doesn't have permission to read members in channel @{NEWS_CHANNEL_USERNAME}")
                await callback.answer(
                    "❌ Бот не имеет прав для проверки подписки на канал.\n"
                    "Пожалуйста, дайте боту права на чтение членства.",
                    show_alert=True
                )
                return
        except Exception as e:
            logging.warning(f"Failed to check bot permissions in channel: {e}")
            # Продолжаем, может быть бот всё же может сделать проверку

        # Проверяем подписку пользователя на канал новостей
        try:
            member = await callback.bot.get_chat_member(f"@{NEWS_CHANNEL_USERNAME}", tg_id)
            logging.info(f"Channel check: user={tg_id}, status={member.status}")
        except Exception as e:
            logging.error(f"get_chat_member failed: {e}")
            await callback.answer(
                "❌ Не удалось проверить подписку на канал. Попробуй позже.",
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
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
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
        new_until = datetime.utcnow() + timedelta(days=3)
        await db.update_subscription(tg_id, uuid, username, new_until, DEFAULT_SQUAD_UUID)

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
        await db.release_user_lock(tg_id)
