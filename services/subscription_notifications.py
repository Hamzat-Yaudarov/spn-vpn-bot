import asyncio
import logging
from datetime import datetime, timezone, timedelta
from aiogram import Bot
import database as db

logger = logging.getLogger(__name__)


async def check_subscription_expiry(bot: Bot):
    """
    Фоновая задача для отправки уведомлений об истечении подписки
    
    Проверяет всех пользователей с активной подпиской и отправляет:
    1. Уведомление за 1 день до истечения
    2. Уведомление когда подписка истекла
    """
    logger.info("Subscription expiry check task started")
    
    try:
        while True:
            await asyncio.sleep(3600)  # Проверяем каждый час
            
            try:
                users = await db.get_users_with_active_subscription()
                
                if not users:
                    continue
                
                now = datetime.now(timezone.utc)
                
                for user in users:
                    tg_id = user['tg_id']
                    subscription_until = user['subscription_until']
                    notified_1_day_before = user.get('notified_1_day_before', False)
                    notified_expired = user.get('notified_expired', False)
                    
                    if not subscription_until:
                        continue
                    
                    # Преобразуем в UTC если нужно
                    if subscription_until.tzinfo is None:
                        subscription_until = subscription_until.replace(tzinfo=timezone.utc)
                    
                    time_until_expiry = subscription_until - now
                    
                    # ═══════════════════════════════════════════════════════════
                    # СЛУЧАЙ 1: Подписка уже истекла
                    # ═══════════════════════════════════════════════════════════
                    if time_until_expiry.total_seconds() <= 0 and not notified_expired:
                        try:
                            await bot.send_message(
                                tg_id,
                                "⏰ <b>Ваша подписка истекла</b>\n\n"
                                "Подписка SPN VPN больше не активна.\n"
                                "Пожалуйста, оформите новую подписку для продолжения работы.\n\n"
                                "<i>Нажмите на кнопку ниже чтобы продлить доступ:</i>\n"
                                "/start"
                            )
                            await db.mark_notified_expired(tg_id)
                            logger.info(f"Sent expiry notification to user {tg_id}")
                        except Exception as e:
                            logger.warning(f"Failed to send expiry notification to {tg_id}: {e}")
                    
                    # ═══════════════════════════════════════════════════════════
                    # СЛУЧАЙ 2: До истечения осталось менее 1 дня
                    # ═══════════════════════════════════════════════════════════
                    elif time_until_expiry.total_seconds() <= 86400 and time_until_expiry.total_seconds() > 0 and not notified_1_day_before:
                        try:
                            hours_remaining = int(time_until_expiry.total_seconds() // 3600)
                            minutes_remaining = int((time_until_expiry.total_seconds() % 3600) // 60)
                            
                            time_str = f"{hours_remaining}ч {minutes_remaining}м"
                            
                            await bot.send_message(
                                tg_id,
                                "⚠️ <b>Подписка заканчивается скоро!</b>\n\n"
                                f"До конца подписки осталось: <b>{time_str}</b>\n\n"
                                "Чтобы не потерять доступ, продлите подписку прямо сейчас:\n\n"
                                "<i>Нажмите на кнопку ниже:</i>\n"
                                "/start"
                            )
                            await db.mark_notified_1_day_before(tg_id)
                            logger.info(f"Sent 1-day warning to user {tg_id}")
                        except Exception as e:
                            logger.warning(f"Failed to send 1-day warning to {tg_id}: {e}")
            
            except asyncio.CancelledError:
                logger.info("Subscription expiry check task cancelled")
                raise
            except Exception as e:
                logger.error(f"Error in subscription expiry check: {e}")
                # Продолжаем работу даже при ошибке
                await asyncio.sleep(10)
    
    except asyncio.CancelledError:
        logger.info("Subscription expiry check task shut down gracefully")
        raise
