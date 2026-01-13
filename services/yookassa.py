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
    
    connector = aiohttp.TCPConnector(ssl=True)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    logging.info(f"Created Yookassa payment for user {tg_id}, payment ID: {data.get('id')}")
                    return data
                else:
                    error_text = await resp.text()
                    logging.error(f"Yookassa error {resp.status}: {error_text}")
    except Exception as e:
        logging.error(f"Yookassa payment creation exception: {e}")
    
    return None


async def get_payment_status(payment_id: str) -> dict | None:
    """
    Получить статус платежа в Yookassa
    
    Args:
        payment_id: ID платежа в Yookassa
        
    Returns:
        Словарь с информацией о платеже или None
    """
    url = f"{YOOKASSA_API_URL}/payments/{payment_id}"
    
    credentials = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json"
    }
    
    connector = aiohttp.TCPConnector(ssl=True)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data
                else:
                    logging.error(f"Get payment status error {resp.status}")
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
    try:
        days = TARIFFS[tariff_code]["days"]
        
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Создаём или получаем пользователя в Remnawave
            uuid, username = await remnawave_get_or_create_user(
                session, tg_id, days, extend_if_exists=True
            )
            
            if not uuid:
                logging.error(f"Failed to create/get Remnawave user for {tg_id}")
                return False

            # Добавляем в сквад
            await remnawave_add_to_squad(session, uuid)
            
            # Получаем ссылку подписки
            sub_url = await remnawave_get_subscription_url(session, uuid)

            # Обрабатываем реферальную программу
            referrer = await db.get_referrer(tg_id)
            if referrer and referrer[0] and not referrer[1]:  # есть рефералит и это первый платеж
                referrer_uuid_row = await db.get_user(referrer[0])
                if referrer_uuid_row and referrer_uuid_row['remnawave_uuid']:  # remnawave_uuid существует
                    await remnawave_extend_subscription(session, referrer_uuid_row['remnawave_uuid'], 7)
                    await db.increment_active_referrals(referrer[0])
                    logging.info(f"Referral bonus given to {referrer[0]}")
                
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
            
            return True

    except Exception as e:
        logging.error(f"Process Yookassa payment exception: {e}")
        return False


async def check_yookassa_payments(bot):
    """
    Фоновая задача для проверки статусов платежей в Yookassa

    Args:
        bot: Экземпляр Bot
    """
    while True:
        await asyncio.sleep(PAYMENT_CHECK_INTERVAL)

        pending = await db.get_pending_payments_by_provider('yookassa')

        if not pending:
            continue

        for payment_record in pending:
            payment_id = payment_record['id']
            tg_id = payment_record['tg_id']
            invoice_id = payment_record['invoice_id']
            tariff_code = payment_record['tariff_code']

            if not await db.acquire_user_lock(tg_id):
                continue

            try:
                payment = await get_payment_status(invoice_id)

                if payment and payment.get("status") == "succeeded":
                    success = await process_paid_yookassa_payment(bot, tg_id, invoice_id, tariff_code)
                    if success:
                        logging.info(f"Processed Yookassa payment for user {tg_id}, payment {invoice_id}")

            except Exception as e:
                logging.error(f"Check Yookassa payment error for {tg_id}: {e}")

            finally:
                await db.release_user_lock(tg_id)


async def cleanup_expired_payments():
    """
    Фоновая задача для удаления истёкших неоплаченных счётов (старше 10 минут)
    """
    while True:
        await asyncio.sleep(300)  # Проверяем каждые 5 минут

        try:
            await db.delete_expired_payments(minutes=10)
            logging.info("Expired payments cleaned up")
        except Exception as e:
            logging.error(f"Cleanup expired payments error: {e}")
