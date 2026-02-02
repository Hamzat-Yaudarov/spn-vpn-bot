import logging
import aiohttp
from datetime import datetime, timedelta, timezone
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from config import ADMIN_ID, DEFAULT_SQUAD_UUID, REMNAWAVE_BASE_URL, REMNAWAVE_API_TOKEN
import database as db
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_add_to_squad
)

logger = logging.getLogger(__name__)

router = Router()


def is_admin(user_id: int) -> bool:
    """Проверить является ли пользователь администратором"""
    return user_id == ADMIN_ID


@router.message(Command("new_code"))
async def admin_new_code(message: Message):
    """Админ команда: создать новый промокод"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /new_code attempt from user {admin_id}")
        return

    parts = message.text.split()

    # Валидация количества аргументов
    if len(parts) < 4:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /new_code КОД ДНЕЙ ЛИМИТ\n\n"
            "<b>Параметры:</b>\n"
            "• <code>КОД</code> - код промокода (только буквы и цифры)\n"
            "• <code>ДНЕЙ</code> - количество дней (число > 0)\n"
            "• <code>ЛИМИТ</code> - максимум использований (число > 0)\n\n"
            "<b>Пример:</b> /new_code SUMMER30 30 100"
        )
        logger.warning(f"Admin {admin_id} /new_code - wrong number of arguments: {len(parts)-1}")
        return

    try:
        code = parts[1].strip()
        days = int(parts[2])
        limit = int(parts[3])

        # Валидация значений
        if not code or not code.isalnum():
            await message.answer("❌ Код промокода должен содержать только буквы и цифры")
            return

        if len(code) < 3:
            await message.answer("❌ Код промокода должен быть не менее 3 символов")
            return

        if days <= 0:
            await message.answer("❌ Количество дней должно быть больше 0")
            return

        if limit <= 0:
            await message.answer("❌ Лимит использований должен быть больше 0")
            return

        # Создаём промокод
        await db.create_promo_code(code.upper(), days, limit)

        await message.answer(
            f"✅ <b>Промокод создан успешно!</b>\n\n"
            f"<b>Код:</b> <code>{code.upper()}</code>\n"
            f"<b>Дней подписки:</b> {days}\n"
            f"<b>Лимит использований:</b> {limit}\n"
            f"<b>Статус:</b> активен"
        )

        logger.info(f"Admin {admin_id} created promo code: {code.upper()} (days={days}, limit={limit})")

    except ValueError:
        await message.answer(
            "❌ <b>Ошибка валидации</b>\n\n"
            "Убедитесь, что:\n"
            "• ДНЕЙ и ЛИМИТ - целые числа\n"
            "• Оба числа больше 0\n\n"
            "<b>Пример:</b> /new_code SUMMER30 30 100"
        )
        logger.warning(f"Admin {admin_id} /new_code - parsing error for arguments: {parts[1:]}")
    except Exception as e:
        await message.answer(f"❌ Ошибка базы данных: {str(e)[:100]}")
        logger.error(f"Admin {admin_id} /new_code database error: {e}")


@router.message(Command("give_sub"))
async def admin_give_sub(message: Message):
    """Админ команда: выдать/продлить подписку пользователю по ИД"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /give_sub attempt from user {admin_id}")
        return

    parts = message.text.split()

    # Валидация количества аргументов
    if len(parts) < 3:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /give_sub ТГ_ИД ДНЕЙ\n\n"
            "<b>Параметры:</b>\n"
            "• <code>ТГ_ИД</code> - ID пользователя Telegram (число)\n"
            "• <code>ДНЕЙ</code> - количество дней (число > 0)\n\n"
            "<b>Пример:</b> /give_sub 123456789 30"
        )
        logger.warning(f"Admin {admin_id} /give_sub - wrong number of arguments: {len(parts)-1}")
        return

    try:
        tg_id = int(parts[1])
        days = int(parts[2])

        # Валидация значений
        if tg_id <= 0:
            await message.answer("❌ ID пользователя должен быть положительным числом")
            return

        if days <= 0:
            await message.answer("❌ Количество дней должно быть больше 0")
            return

        if tg_id == admin_id:
            await message.answer("❌ Нельзя выдать подписку самому себе")
            logger.warning(f"Admin {admin_id} tried to give subscription to themselves")
            return

    except ValueError:
        await message.answer(
            "❌ <b>Ошибка валидации</b>\n\n"
            "Убедитесь, что:\n"
            "• ТГ_ИД и ДНЕЙ - целые числа\n"
            "• Оба числа больше 0\n\n"
            "<b>Пример:</b> /give_sub 123456789 30"
        )
        logger.warning(f"Admin {admin_id} /give_sub - parsing error for arguments: {parts[1:]}")
        return

    if not await db.acquire_user_lock(tg_id):
        await message.answer(f"❌ Пользователь {tg_id} занят, попробуй позже")
        logger.info(f"Admin {admin_id} /give_sub - could not acquire lock for user {tg_id}")
        return

    try:
        # Убедимся что пользователь существует в БД
        if not await db.user_exists(tg_id):
            await db.create_user(tg_id, f"user_{tg_id}")
            logger.info(f"Created new user {tg_id} in database for admin {admin_id}")

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            # Создаём или получаем пользователя в Remnawave
            uuid, username = await remnawave_get_or_create_user(
                session, tg_id, days=days, extend_if_exists=True
            )

            if not uuid:
                await message.answer(
                    f"❌ <b>Ошибка Remnawave API</b>\n\n"
                    f"Не удалось создать/обновить аккаунт для пользователя {tg_id}\n\n"
                    "Попробуй позже"
                )
                logger.error(f"Failed to get/create Remnawave user for TG {tg_id} by admin {admin_id}")
                return

            # Добавляем в сквад
            squad_added = await remnawave_add_to_squad(session, uuid)
            if not squad_added:
                logger.warning(f"Failed to add user {uuid} to squad by admin {admin_id}, continuing")

            # Обновляем подписку в БД
            new_until = datetime.utcnow() + timedelta(days=days)
            await db.update_subscription(tg_id, uuid, username, new_until, DEFAULT_SQUAD_UUID)

        await message.answer(
            f"✅ <b>Подписка выдана успешно!</b>\n\n"
            f"👤 <b>Пользователь:</b> <code>{tg_id}</code>\n"
            f"📅 <b>Дней:</b> {days}\n"
            f"🔑 <b>UUID:</b> <code>{uuid}</code>"
        )

        # Уведомляем пользователя
        try:
            await message.bot.send_message(
                tg_id,
                f"🎉 <b>Поздравляем!</b>\n\n"
                f"Вам выдана подписка SPN VPN на <b>{days} дней</b>\n\n"
                f"Спасибо за использование нашего сервиса! 🚀"
            )
            logger.info(f"User {tg_id} notified about subscription by admin {admin_id}")
        except Exception as e:
            logger.warning(f"Failed to notify user {tg_id}: {e}")
            await message.answer(
                f"⚠️ Подписка выдана, но не удалось отправить уведомление пользователю\n"
                f"(Ошибка: {str(e)[:50]})"
            )

        logger.info(f"Admin {admin_id} gave {days} days subscription to user {tg_id}")

    except Exception as e:
        logger.error(f"Give subscription error: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")

    finally:
        await db.release_user_lock(tg_id)


@router.message(Command("enable_collab"))
async def admin_enable_collab(message: Message):
    """Админ команда: включить партнёрство для пользователя"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /enable_collab attempt from user {admin_id}")
        return

    parts = message.text.split()

    # Валидация количества аргументов
    if len(parts) < 3:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /enable_collab ТГ_ИД ПРОЦЕНТ\n\n"
            "<b>Параметры:</b>\n"
            "• <code>ТГ_ИД</code> - ID пользователя Telegram (число)\n"
            "• <code>ПРОЦЕНТ</code> - % доля партнёра (15, 20, 25, или 30)\n\n"
            "<b>Пример:</b> /enable_collab 123456789 20\n\n"
            "<i>Партнёрство включается на 3 месяца</i>"
        )
        logger.warning(f"Admin {admin_id} /enable_collab - wrong number of arguments: {len(parts)-1}")
        return

    try:
        tg_id = int(parts[1])
        percent = int(parts[2])

        # Валидация значений
        if tg_id <= 0:
            await message.answer("❌ ID пользователя должен быть положительным числом")
            return

        if percent not in [15, 20, 25, 30]:
            await message.answer("❌ Процент должен быть одним из: 15, 20, 25 или 30")
            return

        if tg_id == admin_id:
            await message.answer("❌ Нельзя включить партнёрство самому себе")
            logger.warning(f"Admin {admin_id} tried to enable partnership for themselves")
            return

    except ValueError:
        await message.answer(
            "❌ <b>Ошибка валидации</b>\n\n"
            "Убедитесь, что:\n"
            "• ТГ_ИД и ПРОЦЕНТ - целые числа\n"
            "• ПРОЦЕНТ - одно из: 15, 20, 25, 30\n\n"
            "<b>Пример:</b> /enable_collab 123456789 20"
        )
        logger.warning(f"Admin {admin_id} /enable_collab - parsing error for arguments: {parts[1:]}")
        return

    try:
        # Убедимся что пользователь существует в БД
        if not await db.user_exists(tg_id):
            await db.create_user(tg_id, f"user_{tg_id}")
            logger.info(f"Created new user {tg_id} in database for admin {admin_id}")

        # Включаем партнёрство (на 90 дней = 3 месяца)
        await db.enable_partnership(tg_id, percent, days=90)

        await message.answer(
            f"✅ <b>Партнёрство включено успешно!</b>\n\n"
            f"👤 <b>Пользователь:</b> <code>{tg_id}</code>\n"
            f"💰 <b>% доля:</b> {percent}%\n"
            f"📅 <b>Срок:</b> 3 месяца\n"
            f"🔄 <b>Статус:</b> ожидает принятия соглашения"
        )

        # Уведомляем пользователя
        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🤝 Партнёрство", callback_data="partnership")],
            ])

            await message.bot.send_message(
                tg_id,
                f"🎉 <b>Отлично новость!</b>\n\n"
                f"Вы подключены к программе партнёрства SPN VPN!\n\n"
                f"💰 <b>Ваша доля:</b> {percent}% от каждого платежа приведённого вами пользователя\n\n"
                f"Нажмите кнопку ниже, чтобы начать зарабатывать 🚀",
                reply_markup=kb
            )
            logger.info(f"User {tg_id} notified about partnership by admin {admin_id}")
        except Exception as e:
            logger.warning(f"Failed to notify user {tg_id}: {e}")
            await message.answer(
                f"⚠️ Партнёрство включено, но не удалось отправить уведомление пользователю\n"
                f"(Ошибка: {str(e)[:50]})"
            )

        logger.info(f"Admin {admin_id} enabled partnership for user {tg_id} with {percent}%")

    except Exception as e:
        logger.error(f"Enable partnership error: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")


@router.message(Command("extend_collab"))
async def admin_extend_collab(message: Message):
    """Админ команда: продлить партнёрство для пользователя"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /extend_collab attempt from user {admin_id}")
        return

    parts = message.text.split()

    # Валидация количества аргументов
    if len(parts) < 3:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /extend_collab ТГ_ИД ДНЕЙ\n\n"
            "<b>Параметры:</b>\n"
            "• <code>ТГ_ИД</code> - ID пользователя Telegram (число)\n"
            "• <code>ДНЕЙ</code> - количество дней на продление (число > 0)\n\n"
            "<b>Пример:</b> /extend_collab 123456789 90"
        )
        logger.warning(f"Admin {admin_id} /extend_collab - wrong number of arguments: {len(parts)-1}")
        return

    try:
        tg_id = int(parts[1])
        days = int(parts[2])

        # Валидация значений
        if tg_id <= 0:
            await message.answer("❌ ID пользователя должен быть положительным числом")
            return

        if days <= 0:
            await message.answer("❌ Количество дней должно быть больше 0")
            return

    except ValueError:
        await message.answer(
            "❌ <b>Ошибка валидации</b>\n\n"
            "Убедитесь, что:\n"
            "• ТГ_ИД и ДНЕЙ - целые числа\n"
            "• Оба числа больше 0\n\n"
            "<b>Пример:</b> /extend_collab 123456789 90"
        )
        logger.warning(f"Admin {admin_id} /extend_collab - parsing error for arguments: {parts[1:]}")
        return

    try:
        # Проверяем что пользователь является партнёром
        partner = await db.get_partner_info(tg_id)
        if not partner:
            await message.answer(f"❌ Пользователь {tg_id} не является партнёром")
            logger.warning(f"Admin {admin_id} tried to extend partnership for non-partner {tg_id}")
            return

        # Продлеваем партнёрство
        await db.extend_partnership(tg_id, days)

        await message.answer(
            f"✅ <b>Партнёрство продлено успешно!</b>\n\n"
            f"👤 <b>Пользователь:</b> <code>{tg_id}</code>\n"
            f"📅 <b>Добавлено дней:</b> {days}"
        )

        # Уведомляем пользователя
        try:
            await message.bot.send_message(
                tg_id,
                f"🎉 <b>Партнёрство продлено!</b>\n\n"
                f"Срок вашего партнёрства продлен на <b>{days} дней</b>\n\n"
                f"Спасибо за сотрудничество! 🚀"
            )
            logger.info(f"User {tg_id} notified about partnership extension by admin {admin_id}")
        except Exception as e:
            logger.warning(f"Failed to notify user {tg_id}: {e}")
            await message.answer(
                f"⚠️ Партнёрство продлено, но не удалось отправить уведомление пользователю\n"
                f"(Ошибка: {str(e)[:50]})"
            )

        logger.info(f"Admin {admin_id} extended partnership for user {tg_id} by {days} days")

    except Exception as e:
        logger.error(f"Extend partnership error: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")


@router.message(Command("take_sub"))
async def admin_take_sub(message: Message):
    """Админ команда: отозвать подписку пользователя по ИД"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /take_sub attempt from user {admin_id}")
        return

    parts = message.text.split()

    # Валидация количества аргументов
    if len(parts) < 3:
        await message.answer(
            "❌ <b>Неверный формат команды</b>\n\n"
            "<b>Использование:</b> /take_sub ТГ_ИД ДНЕЙ\n\n"
            "<b>Параметры:</b>\n"
            "• <code>ТГ_ИД</code> - ID пользователя Telegram (число)\n"
            "• <code>ДНЕЙ</code> - количество дней для отзыва (число > 0)\n\n"
            "<b>Пример:</b> /take_sub 123456789 30\n\n"
            "<i>Если ДНЕЙ больше чем осталось в подписке, время сбрасывается до 1 минуты</i>"
        )
        logger.warning(f"Admin {admin_id} /take_sub - wrong number of arguments: {len(parts)-1}")
        return

    try:
        tg_id = int(parts[1])
        days = int(parts[2])

        # Валидация значений
        if tg_id <= 0:
            await message.answer("❌ ID пользователя должен быть положительным числом")
            return

        if days <= 0:
            await message.answer("❌ Количество дней должно быть больше 0")
            return

        if tg_id == admin_id:
            await message.answer("❌ Нельзя отозвать подписку у самого себя")
            logger.warning(f"Admin {admin_id} tried to take subscription from themselves")
            return

    except ValueError:
        await message.answer(
            "❌ <b>Ошибка валидации</b>\n\n"
            "Убедитесь, что:\n"
            "• ТГ_ИД и ДНЕЙ - целые числа\n"
            "• Оба числа больше 0\n\n"
            "<b>Пример:</b> /take_sub 123456789 30"
        )
        logger.warning(f"Admin {admin_id} /take_sub - parsing error for arguments: {parts[1:]}")
        return

    if not await db.acquire_user_lock(tg_id):
        await message.answer(f"❌ Пользователь {tg_id} занят, попробуй позже")
        logger.info(f"Admin {admin_id} /take_sub - could not acquire lock for user {tg_id}")
        return

    try:
        # Получаем пользователя
        user = await db.get_user(tg_id)
        if not user:
            await message.answer(f"❌ Пользователь {tg_id} не найден в системе")
            logger.warning(f"Admin {admin_id} tried to take subscription from non-existent user {tg_id}")
            return

        # Проверяем есть ли подписка
        if not user.get('subscription_until'):
            await message.answer(f"❌ У пользователя {tg_id} нет активной подписки")
            logger.warning(f"Admin {admin_id} tried to take subscription from user {tg_id} with no subscription")
            return

        # Рассчитываем новое время подписки
        subscription_until = user['subscription_until']
        now = datetime.utcnow()

        # Время осталось в подписке (рассчитываем в днях более точно)
        time_left = subscription_until - now
        days_left = time_left.total_seconds() / 86400  # Количество дней (с учётом часов/минут/секунд)

        # Если ДНЕЙ больше или равно чем осталось, сбрасываем до 1 минуты
        if days >= days_left:
            new_subscription_until = now + timedelta(minutes=1)
        else:
            new_subscription_until = subscription_until - timedelta(days=days)

        # Обновляем подписку в БД И в Remnawave API
        remnawave_uuid = user.get('remnawave_uuid')
        remnawave_username = user.get('remnawave_username')
        squad_uuid = user.get('squad_uuid')

        # Используем встроенную функцию которая обновляет всё правильно
        await db.update_subscription(tg_id, remnawave_uuid, remnawave_username, new_subscription_until, squad_uuid)

        # Дополнительно обновляем Remnawave API напрямую чтобы убедиться
        if remnawave_uuid:
            try:
                # Обновляем expireAt на новое время через PATCH запрос
                payload = {
                    "uuid": str(remnawave_uuid),
                    "expireAt": new_subscription_until.isoformat()
                }

                headers = {
                    "Authorization": f"Bearer {REMNAWAVE_API_TOKEN}",
                    "Content-Type": "application/json"
                }

                connector = aiohttp.TCPConnector(ssl=False)
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                    async with session.patch(
                        f"{REMNAWAVE_BASE_URL}/users",
                        headers=headers,
                        json=payload
                    ) as resp:
                        if resp.status == 200:
                            logger.info(f"✅ Updated Remnawave subscription for user {tg_id} to {new_subscription_until}")
                        else:
                            error_text = await resp.text()
                            logger.warning(f"❌ Failed to update Remnawave subscription for {tg_id}: {resp.status} - {error_text}")
            except Exception as e:
                logger.error(f"❌ Could not update Remnawave subscription for user {tg_id}: {e}", exc_info=True)
                # Не блокируем процесс если Remnawave недоступен

        new_days_left = max(0, int((new_subscription_until - now).total_seconds() / 86400))

        await message.answer(
            f"✅ <b>Подписка отозвана успешно!</b>\n\n"
            f"👤 <b>Пользователь:</b> <code>{tg_id}</code>\n"
            f"📅 <b>Отозвано дней:</b> {days}\n"
            f"⏰ <b>Осталось дней:</b> {new_days_left}\n"
            f"🔔 <b>Уведомления:</b> очищены"
        )

        # Уведомляем пользователя
        try:
            if new_days_left <= 0:
                await message.bot.send_message(
                    tg_id,
                    f"⚠️ <b>Ваша подписка отозвана!</b>\n\n"
                    f"Администратор отозвал вашу подписку на {days} дней\n\n"
                    f"Чтобы восстановить доступ, продлите подписку в меню 'Купить подписку'"
                )
            else:
                await message.bot.send_message(
                    tg_id,
                    f"⚠️ <b>Подписка сокращена</b>\n\n"
                    f"Администратор отозвал {days} дней вашей подписки\n\n"
                    f"Осталось дней: <b>{new_days_left}</b>"
                )
            logger.info(f"User {tg_id} notified about subscription removal by admin {admin_id}")
        except Exception as e:
            logger.warning(f"Failed to notify user {tg_id}: {e}")
            await message.answer(
                f"⚠️ Подписка отозвана, но не удалось отправить уведомление пользователю\n"
                f"(Ошибка: {str(e)[:50]})"
            )

        logger.info(f"Admin {admin_id} took {days} days subscription from user {tg_id}, {int(days_left)} days were remaining")

    except Exception as e:
        logger.error(f"Take subscription error: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")

    finally:
        await db.release_user_lock(tg_id)


@router.message(Command("stats"))
async def admin_stats(message: Message):
    """Админ команда: получить статистику"""
    admin_id = message.from_user.id

    if not is_admin(admin_id):
        await message.answer("❌ Эта команда доступна только администратору")
        logger.warning(f"Unauthorized /stats attempt from user {admin_id}")
        return

    try:
        # TODO: Реализовать получение полной статистики из БД
        await message.answer(
            "📊 <b>Статистика бота</b>\n\n"
            "Функция в разработке...\n\n"
            "<i>Будут доступны:</i>\n"
            "• 👥 Количество активных пользователей\n"
            "• 💳 Статистика платежей\n"
            "• 🎁 Активированные подарки\n"
            "• 🎟 Использованные промокоды\n"
            "• 👥 Статистика рефералов"
        )
        logger.info(f"Admin {admin_id} requested /stats")
    except Exception as e:
        await message.answer(f"❌ Ошибка при получении статистики: {str(e)[:100]}")
        logger.error(f"Error getting stats for admin {admin_id}: {e}")
