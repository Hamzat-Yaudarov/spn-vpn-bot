import aiohttp
import logging
import asyncio
import base64
import uuid
from datetime import datetime, timezone
from config import (
    YOOKASSA_SHOP_ID,
    YOOKASSA_SECRET_KEY,
    YOOKASSA_API_URL,
    PAYMENT_CHECK_INTERVAL,
    CLEANUP_CHECK_INTERVAL,
    API_REQUEST_TIMEOUT,
    WEBHOOK_USE_POLLING
)
import database as db
from utils import safe_api_call
from services.payment_processing import process_paid_payment


async def create_yookassa_payment(
    bot,
    amount: float,
    tariff_code: str,
    tg_id: int
) -> dict | None:
    """
    Создать платёж через Yookassa API с retry логикой

    Args:
        bot: Экземпляр Bot
        amount: Сумма платежа в рублях
        tariff_code: Код тарифа
        tg_id: ID пользователя Telegram

    Returns:
        Словарь с информацией о платеже или None
    """
    async def _create_payment():
        url = f"{YOOKASSA_API_URL}/payments"
        bot_username = (await bot.get_me()).username

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
                "return_url": f"https://t.me/{bot_username}"
            },
            "capture": True,
            "description": f"Подписка SPN VPN — {tariff_code}",
            "metadata": {
                "tg_id": str(tg_id),
                "tariff_code": tariff_code
            }
        }

        connector = aiohttp.TCPConnector(ssl=True)
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    logging.info(f"Created Yookassa payment for user {tg_id}, payment ID: {data.get('id')}")
                    return data
                else:
                    error_text = await resp.text()
                    raise RuntimeError(f"Yookassa HTTP {resp.status}: {error_text}")

    return await safe_api_call(
        _create_payment,
        error_message=f"Failed to create Yookassa payment for user {tg_id}"
    )


async def get_payment_status(payment_id: str) -> dict | None:
    """
    Получить статус платежа в Yookassa с retry логикой

    Args:
        payment_id: ID платежа в Yookassa

    Returns:
        Словарь с информацией о платеже или None
    """
    async def _get_status():
        url = f"{YOOKASSA_API_URL}/payments/{payment_id}"

        credentials = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json"
        }

        connector = aiohttp.TCPConnector(ssl=True)
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data
                else:
                    error_text = await resp.text()
                    raise RuntimeError(f"Yookassa HTTP {resp.status}: {error_text}")

    return await safe_api_call(
        _get_status,
        error_message=f"Failed to get Yookassa payment status {payment_id}"
    )


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
    return await process_paid_payment(bot, tg_id, payment_id, tariff_code, acquire_lock=False)


async def check_yookassa_payments(bot):
    """
    Фоновая задача для проверки статусов платежей в Yookassa

    Примечание: Если настроен WEBHOOK_HOST, платежи будут обработаны
    через webhook'и мгновенно. Polling используется как fallback.

    Args:
        bot: Экземпляр Bot
    """
    if not WEBHOOK_USE_POLLING:
        logging.info("Yookassa polling disabled (webhook mode enabled)")
        return

    logging.info("Yookassa polling mode enabled")

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
    Фоновая задача для удаления истёкших неоплаченных счётов

    Периодичность настраивается в config.CLEANUP_CHECK_INTERVAL
    """
    logging.info(f"Cleanup task started (interval: {CLEANUP_CHECK_INTERVAL}s)")

    try:
        while True:
            await asyncio.sleep(CLEANUP_CHECK_INTERVAL)

            try:
                await db.delete_expired_payments()
                logging.info("Expired payments cleaned up")
            except asyncio.CancelledError:
                logging.info("Cleanup task cancelled")
                raise
            except Exception as e:
                logging.error(f"Cleanup expired payments error: {e}")
    except asyncio.CancelledError:
        logging.info("Cleanup task shut down gracefully")
        raise
