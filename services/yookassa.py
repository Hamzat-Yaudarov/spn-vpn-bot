import aiohttp
import logging
import asyncio
import base64
import uuid
from datetime import datetime, timedelta, timezone
from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, YOOKASSA_API_URL, TARIFFS, PAYMENT_CHECK_INTERVAL
import database as db
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_add_to_squad,
    remnawave_get_subscription_url,
    remnawave_extend_subscription
)


async def create_yookassa_payment(
    bot,
    amount: float,
    tariff_code: str,
    tg_id: int
) -> dict | None:
    """
    Создать платёж через Yookassa API
    
    Args:
        bot: Экземпляр Bot
        amount: Сумма платежа в рублях
        tariff_code: Код тарифа
        tg_id: ID пользователя Telegram
        
    Returns:
        Словарь с информацией о платеже или None
    """
    from main import get_global_session
    
    try:
        url = f"{YOOKASSA_API_URL}/payments"
        
        # Базовая авторизация: base64(shop_id:secret_key)
        credentials = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Idempotence-Key": str(uuid.uuid4()),
            "Content-Type": "application/json"
        }
        
        # Генерируем уникальный ID платежа
        payment_id = f"spn_{tg_id}_{int(datetime.now(timezone.utc).timestamp())}_{tariff_code}"
        
        payload = {
            "amount": {
                "value": str(amount),
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": "https://t.me/spn_vpn_bot"  # После оплаты вернёт в бот
            },
            "capture": True,
            "description": f"Подписка SPN VPN — {tariff_code}",
            "metadata": {
                "tg_id": str(tg_id),
                "tariff_code": tariff_code
            }
        }
        
        session = get_global_session()
        
        async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                logging.info(f"[USER:{tg_id}] Created Yookassa payment: {data.get('id')}")
                return data
            else:
                error_text = await resp.text()
                logging.error(f"Yookassa error {resp.status}: {error_text}")
    except asyncio.TimeoutError:
        logging.error(f"[USER:{tg_id}] Yookassa timeout")
    except Exception as e:
        logging.error(f"[USER:{tg_id}] Yookassa payment creation exception: {e}")
    
    return None


async def get_payment_status(payment_id: str) -> dict | None:
    """
    Получить статус платежа в Yookassa
    
    Args:
        payment_id: ID платежа в Yookassa
        
    Returns:
        Словарь с информацией о платеже или None
    """
    from main import get_global_session
    
    try:
        url = f"{YOOKASSA_API_URL}/payments/{payment_id}"
        
        credentials = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json"
        }
        
        session = get_global_session()
        
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data
            else:
                logging.error(f"Get payment status error {resp.status}")
    except asyncio.TimeoutError:
        logging.error(f"Yookassa status check timeout for {payment_id}")
    except Exception as e:
        logging.error(f"Get payment status exception: {e}")
    
    return None


async def process_paid_yookassa_payment(bot, tg_id: int, payment_id: str, tariff_code: str) -> bool:
    """
    Обработать оплаченный платёж Yookassa и активировать подписку
    
    Args:
        bot: Экземпляр Bot
        tg_id: ID пользователя Telegram
        payment_id: ID платежа в Yookassa
        tariff_code: Код тарифа
        
    Returns:
        True если успешно, False иначе
    """
    from main import get_global_session
    
    try:
        if tariff_code not in TARIFFS:
            logging.error(f"[USER:{tg_id}] Invalid tariff code: {tariff_code}")
            return False
        
        days = TARIFFS[tariff_code]["days"]
        
        session = get_global_session()
        
        # Создаём или получаем пользователя в Remnawave
        uuid, username = await remnawave_get_or_create_user(
            session, tg_id, days, extend_if_exists=True
        )
        
        if not uuid:
            logging.error(f"[USER:{tg_id}] Failed to create/get Remnawave user")
            return False

        # Добавляем в сквад
        if not await remnawave_add_to_squad(session, uuid):
            logging.warning(f"[USER:{tg_id}] Failed to add to squad, continuing")
        
        # Получаем ссылку подписки
        sub_url = await remnawave_get_subscription_url(session, uuid)

        # Обрабатываем реферальную программу
        referrer_info = await db.get_referrer(tg_id)
        if referrer_info and referrer_info[0]:
            referrer_id = referrer_info[0]
            is_first_payment = not referrer_info[1]  # first_payment = False → первый платеж
            
            if is_first_payment:
                referrer_user = await db.get_user(referrer_id)
                
                if referrer_user and referrer_user['remnawave_uuid']:
                    # Проверяем что у рефер есть активная подписка
                    expire_date = referrer_user.get('subscription_until')
                    if expire_date:
                        expire_dt = datetime.fromisoformat(expire_date.replace('Z', '+00:00'))
                        
                        if expire_dt > datetime.now(timezone.utc):
                            # Подписка активна - даём бонус
                            success = await remnawave_extend_subscription(session, referrer_user['remnawave_uuid'], 7)
                            if success:
                                await db.increment_active_referrals(referrer_id)
                                logging.info(f"[USER:{tg_id}] Referral bonus given to {referrer_id}: +7 days")
                        else:
                            logging.warning(f"[USER:{tg_id}] Referrer {referrer_id} subscription expired")
                
                # Отмечаем первый платеж
                await db.mark_first_payment(tg_id)

        # Обновляем платеж в БД
        await db.update_payment_status_by_invoice(payment_id, 'paid')
        
        # Обновляем подписку пользователя
        new_until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        await db.update_subscription(tg_id, uuid, username, new_until, None)

        # Отправляем сообщение пользователю
        text = (
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            f"Тариф: {tariff_code} ({days} дней)\n"
            f"<b>Ссылка подписки:</b>\n<code>{sub_url}</code>"
        )
        await bot.send_message(tg_id, text)
        
        logging.info(f"[USER:{tg_id}] Payment processed successfully: {tariff_code} (+{days} days)")
        return True

    except Exception as e:
        logging.error(f"[USER:{tg_id}] Process Yookassa payment exception: {e}", exc_info=True)
        return False


async def check_yookassa_payments(bot):
    """
    Фоновая задача для проверки статусов платежей в Yookassa

    Args:
        bot: Экземпляр Bot
    """
    logger = logging.getLogger(__name__)
    logger.info("Yookassa payment checker started")
    
    while True:
        try:
            await asyncio.sleep(PAYMENT_CHECK_INTERVAL)

            pending = await db.get_pending_payments_by_provider('yookassa')

            if not pending:
                continue

            logger.debug(f"Checking {len(pending)} pending Yookassa payments")

            for payment_record in pending:
                payment_id = payment_record['id']
                tg_id = payment_record['tg_id']
                invoice_id = payment_record['invoice_id']
                tariff_code = payment_record['tariff_code']

                async with db.UserLockContext(tg_id) as acquired:
                    if not acquired:
                        continue

                    try:
                        payment = await get_payment_status(invoice_id)

                        if payment and payment.get("status") == "succeeded":
                            # Проверяем идемпотентность
                            if not await db.mark_payment_processed(invoice_id):
                                logger.debug(f"[USER:{tg_id}] Payment {invoice_id} already processed")
                                continue
                            
                            success = await process_paid_yookassa_payment(bot, tg_id, invoice_id, tariff_code)
                            if success:
                                logger.info(f"[USER:{tg_id}] Processed Yookassa payment: {invoice_id}")

                    except Exception as e:
                        logger.error(f"[USER:{tg_id}] Check Yookassa payment error: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Yookassa checker exception: {e}", exc_info=True)
            await asyncio.sleep(PAYMENT_CHECK_INTERVAL)


async def cleanup_expired_payments():
    """
    Фоновая задача для удаления истёкших неоплаченных счётов (старше 10 минут)
    """
    logger = logging.getLogger(__name__)
    logger.info("Payment cleanup task started")
    
    while True:
        try:
            await asyncio.sleep(300)  # Проверяем каждые 5 минут

            await db.delete_expired_payments(minutes=10)
            logger.debug("Expired payments cleaned up")
        except Exception as e:
            logger.error(f"Cleanup expired payments error: {e}", exc_info=True)
