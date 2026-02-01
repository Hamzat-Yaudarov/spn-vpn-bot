import logging
import asyncio
from datetime import datetime, timedelta
import database as db


logger = logging.getLogger(__name__)


async def check_and_send_notifications(bot):
    """
    Фоновая задача для проверки и отправки уведомлений о заканчивающихся подписках
    
    Проверяет каждые 30 минут пользователей которым нужно отправить уведомление
    """
    logger.info("✅ Subscription notification service started")
    
    try:
        while True:
            await asyncio.sleep(1800)  # проверяем каждые 30 минут
            
            try:
                await _send_notifications_batch(bot)
            except asyncio.CancelledError:
                logger.info("Subscription notification service cancelled")
                raise
            except Exception as e:
                logger.error(f"Error in notification check: {e}", exc_info=True)
    except asyncio.CancelledError:
        logger.info("Subscription notification service shut down gracefully")
        raise


async def _send_notifications_batch(bot):
    """Отправить уведомления пользователям у которых скоро закончится подписка"""
    try:
        users = await db.get_users_needing_notification()
        
        if not users:
            return
        
        logger.info(f"📤 Found {len(users)} users to notify")
        
        for user in users:
            try:
                tg_id = user['tg_id']
                notification_type = user['notification_type']
                subscription_until = user['subscription_until']
                
                # Рассчитываем оставшееся время
                now = datetime.utcnow()
                time_left = subscription_until - now
                
                if time_left.total_seconds() < 0:
                    # Подписка уже истекла
                    days_left = 0
                    hours_left = 0
                else:
                    days_left = time_left.days
                    hours_left = time_left.seconds // 3600
                
                # Формируем сообщение в зависимости от типа уведомления
                if notification_type == "1day_left":
                    # 1-1.5 дня осталось
                    time_str = f"{days_left} дн. {hours_left} ч." if days_left > 0 else f"{hours_left} ч."
                    text = (
                        "⏰ <b>Ваша подписка скоро закончится!</b>\n\n"
                        f"Осталось: <b>{time_str}</b>\n\n"
                        "Продлите подписку, чтобы не потерять доступ к быстрой и безопасной сети!"
                    )
                elif notification_type == "below1day":
                    # Меньше дня осталось
                    time_str = f"{hours_left} ч." if hours_left > 0 else f"{time_left.seconds // 60} мин."
                    text = (
                        "⚠️ <b>Подписка закончится совсем скоро!</b>\n\n"
                        f"Осталось: <b>{time_str}</b>\n\n"
                        "Срочно продлите подписку, чтобы вернуть доступ!"
                    )
                elif notification_type == "expired":
                    # Подписка истекла
                    text = (
                        "❌ <b>Ваша подписка закончилась!</b>\n\n"
                        "Продлите подписку, чтобы вернуть доступ к быстрой и безопасной сети!"
                    )
                else:
                    logger.warning(f"Unknown notification type: {notification_type} for user {tg_id}")
                    continue
                
                # Создаём кнопку "Продлить подписку"
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Продлить подписку", callback_data="buy_subscription")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
                ])
                
                # Отправляем сообщение
                await bot.send_message(tg_id, text, reply_markup=kb)
                logger.info(f"✅ Notification sent to user {tg_id}, type: {notification_type}")
                
                # Отмечаем что уведомление отправлено и устанавливаем следующее
                await db.mark_notification_sent(tg_id)
                
            except Exception as e:
                logger.error(f"Failed to send notification to user {user.get('tg_id')}: {e}")
    
    except Exception as e:
        logger.error(f"Error in _send_notifications_batch: {e}", exc_info=True)
