import asyncio
import logging
from datetime import datetime, timedelta, timezone
from aiogram import Bot
import database as db


async def send_subscription_expiry_notifications(bot: Bot):
    """
    Фоновая задача для отправки уведомлений пользователям за день до истечения подписки
    
    Запускается каждый час и проверяет пользователей чья подписка истекает завтра
    """
    logger = logging.getLogger(__name__)
    logger.info("Subscription expiry notification task started")
    
    while True:
        try:
            await asyncio.sleep(3600)  # Проверяем каждый час
            
            # Получаем пользователей чья подписка истекает в течение 24 часов
            now = datetime.now(timezone.utc)
            tomorrow = now + timedelta(days=1)
            
            users = await db.db_execute(
                """
                SELECT tg_id, subscription_until FROM users
                WHERE subscription_until IS NOT NULL
                AND subscription_until > $1
                AND subscription_until <= $2
                ORDER BY subscription_until
                """,
                (now, tomorrow),
                fetch_all=True
            )
            
            if not users:
                logger.debug("No users with expiring subscriptions")
                continue
            
            logger.info(f"Sending expiry notifications to {len(users)} users")
            
            for user in users:
                tg_id = user['tg_id']
                expire_at = user['subscription_until']
                
                # Рассчитываем сколько часов осталось
                expire_dt = datetime.fromisoformat(expire_at.replace('Z', '+00:00'))
                remaining = expire_dt - now
                hours = remaining.total_seconds() / 3600
                
                try:
                    text = (
                        "⏰ <b>Внимание!</b>\n\n"
                        f"Ваша подписка истекает через <b>{hours:.1f} часов</b>\n\n"
                        "Продлите подписку сейчас, чтобы не потерять доступ к SPN VPN.\n\n"
                        "Нажмите на кнопку меню и выберите «💳 Оформить подписку»"
                    )
                    
                    await bot.send_message(tg_id, text)
                    logger.debug(f"[USER:{tg_id}] Expiry notification sent")
                    
                except Exception as e:
                    logger.warning(f"[USER:{tg_id}] Failed to send expiry notification: {e}")
                
                # Задержка между сообщениями чтобы не забанить бота
                await asyncio.sleep(0.1)
        
        except Exception as e:
            logger.error(f"Subscription expiry notification task error: {e}", exc_info=True)
            await asyncio.sleep(3600)


async def send_subscription_expired_notifications(bot: Bot):
    """
    Фоновая задача для отправки уведомлений пользователям когда подписка истекла
    
    Запускается каждый час и уведомляет пользователей с истекшей подпиской
    """
    logger = logging.getLogger(__name__)
    logger.info("Subscription expired notification task started")
    
    while True:
        try:
            await asyncio.sleep(3600)  # Проверяем каждый час
            
            # Получаем пользователей с истекшей подпиской (но только недавно)
            now = datetime.now(timezone.utc)
            an_hour_ago = now - timedelta(hours=1)
            
            users = await db.db_execute(
                """
                SELECT DISTINCT tg_id, subscription_until FROM users
                WHERE subscription_until IS NOT NULL
                AND subscription_until <= $1
                AND subscription_until > $2
                AND remnawave_uuid IS NOT NULL
                ORDER BY subscription_until DESC
                LIMIT 100
                """,
                (now, an_hour_ago),
                fetch_all=True
            )
            
            if not users:
                logger.debug("No users with recently expired subscriptions")
                continue
            
            logger.info(f"Sending expired notifications to {len(users)} users")
            
            for user in users:
                tg_id = user['tg_id']
                
                try:
                    text = (
                        "❌ <b>Подписка истекла</b>\n\n"
                        "Ваш доступ к SPN VPN больше не активен.\n\n"
                        "Оформите новую подписку чтобы продолжить использование сервиса!\n\n"
                        "Нажмите на кнопку меню и выберите «💳 Оформить подписку»"
                    )
                    
                    await bot.send_message(tg_id, text)
                    logger.debug(f"[USER:{tg_id}] Expired notification sent")
                    
                except Exception as e:
                    logger.warning(f"[USER:{tg_id}] Failed to send expired notification: {e}")
                
                # Задержка между сообщениями
                await asyncio.sleep(0.1)
        
        except Exception as e:
            logger.error(f"Subscription expired notification task error: {e}", exc_info=True)
            await asyncio.sleep(3600)


async def send_admin_daily_report(bot: Bot, admin_id: int):
    """
    Фоновая задача для отправки ежедневного отчета администратору
    
    Запускается каждый день в определенное время с статистикой
    """
    logger = logging.getLogger(__name__)
    logger.info("Admin daily report task started")
    
    while True:
        try:
            now = datetime.now(timezone.utc)
            
            # Рассчитываем когда запустить (ежедневно в 9:00 UTC)
            tomorrow = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            delay = (tomorrow - now).total_seconds()
            
            if delay < 0:
                delay += 86400  # Если время уже прошло, добавляем сутки
            
            await asyncio.sleep(delay)
            
            # Получаем статистику за последние 24 часа
            stats = await db.get_overall_stats()
            
            if not stats:
                logger.error("Failed to get stats for daily report")
                continue
            
            # Получаем статистику за последние 24 часа
            twenty_four_hours_ago = datetime.now(timezone.utc) - timedelta(hours=24)
            
            today_stats = await db.db_execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM users WHERE created_at > $1) as new_users,
                    (SELECT COUNT(*) FROM payments WHERE status = 'paid' AND updated_at > $1) as paid_today,
                    (SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'paid' AND updated_at > $1) as revenue_today
                """,
                (twenty_four_hours_ago,),
                fetch_one=True
            )
            
            text = (
                "📊 <b>ЕЖЕДНЕВНЫЙ ОТЧЕТ</b>\n"
                f"Дата: {now.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                
                "<b>👥 Пользователи:</b>\n"
                f"  Всего: {stats['total_users']}\n"
                f"  Новых за 24ч: {today_stats['new_users']}\n"
                f"  Активных подписок: {stats['active_subscriptions']}\n\n"
                
                "<b>💰 Доход:</b>\n"
                f"  За 24 часа: {today_stats['revenue_today']} ₽\n"
                f"  Успешных платежей: {today_stats['paid_today']}\n"
                f"  Ожидающих платежей: {stats['pending_payments']}\n"
                f"  Всего доход: {stats['total_revenue']} ₽\n\n"
                
                "<b>🎁 Подарки:</b>\n"
                f"  Выданных: {stats['gifts_given']}\n\n"
                
                "<b>👥 Рефералы:</b>\n"
                f"  Активных рефералов: {stats['active_referrals']}\n"
                f"  Всего рефералов: {stats['total_referrals']}\n"
            )
            
            try:
                await bot.send_message(admin_id, text)
                logger.info(f"[ADMIN:{admin_id}] Daily report sent")
            except Exception as e:
                logger.warning(f"[ADMIN:{admin_id}] Failed to send daily report: {e}")
        
        except Exception as e:
            logger.error(f"Admin daily report task error: {e}", exc_info=True)
            await asyncio.sleep(3600)
