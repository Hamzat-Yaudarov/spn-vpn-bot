import aiohttp
import logging
import asyncio
from config import (
    CRYPTOBOT_TOKEN,
    CRYPTOBOT_API_URL,
    PAYMENT_CHECK_INTERVAL,
    API_REQUEST_TIMEOUT,
    WEBHOOK_USE_POLLING
)
import database as db
from utils import safe_api_call
from services.payment_processing import process_paid_payment


async def create_cryptobot_invoice(
    bot,
    amount: float,
    tariff_code: str,
    tg_id: int
) -> dict | None:
    """
    Создать счёт для оплаты через CryptoBot с retry логикой

    Args:
        bot: Экземпляр Bot
        amount: Сумма платежа в рублях
        tariff_code: Код тарифа
        tg_id: ID пользователя Telegram

    Returns:
        Словарь с информацией о счёте или None
    """
    async def _create_invoice():
        url = f"{CRYPTOBOT_API_URL}/createInvoice"
        headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}

        bot_username = (await bot.get_me()).username

        payload = {
            "currency_type": "fiat",
            "fiat": "RUB",
            "amount": str(amount),
            "description": f"Подписка SPN VPN — {tariff_code}",
            "payload": f"spn_{tg_id}_{tariff_code}",
            "paid_btn_name": "openBot",
            "paid_btn_url": f"https://t.me/{bot_username}",
            "accepted_assets": "USDT,TON,BTC"
        }

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok"):
                        logging.info(f"Created CryptoBot invoice for user {tg_id}")
                        return data["result"]
                    else:
                        raise RuntimeError(f"CryptoBot API error: {data.get('error', 'Unknown')}")
                else:
                    error_text = await resp.text()
                    raise RuntimeError(f"CryptoBot HTTP {resp.status}: {error_text}")

    return await safe_api_call(
        _create_invoice,
        error_message=f"Failed to create CryptoBot invoice for user {tg_id}"
    )


async def get_invoice_status(invoice_id: str) -> dict | None:
    """
    Получить статус счёта в CryptoBot с retry логикой

    Args:
        invoice_id: ID счёта в CryptoBot

    Returns:
        Словарь с информацией о счёте или None
    """
    async def _get_status():
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
            url = f"{CRYPTOBOT_API_URL}/getInvoices"
            params = {"invoice_ids": invoice_id}

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok"):
                        invoices = data["result"]["items"]
                        if invoices:
                            return invoices[0]
                    else:
                        raise RuntimeError(f"CryptoBot API error: {data.get('error', 'Unknown')}")
                else:
                    error_text = await resp.text()
                    raise RuntimeError(f"CryptoBot HTTP {resp.status}: {error_text}")

    return await safe_api_call(
        _get_status,
        error_message=f"Failed to get CryptoBot invoice status {invoice_id}"
    )


async def process_paid_invoice(bot, tg_id: int, invoice_id: str, tariff_code: str) -> bool:
    """
    Обработать оплаченный счёт и активировать подписку

    Args:
        bot: Экземпляр Bot
        tg_id: ID пользователя Telegram
        invoice_id: ID счёта в CryptoBot
        tariff_code: Код тарифа

    Returns:
        True если успешно, False иначе
    """
    return await process_paid_payment(bot, tg_id, invoice_id, tariff_code, acquire_lock=False)


async def check_cryptobot_invoices(bot):
    """
    Фоновая задача для проверки статусов платежей в CryptoBot

    Примечание: Если настроен WEBHOOK_HOST, платежи будут обработаны
    через webhook'и мгновенно. Polling используется как fallback.

    Args:
        bot: Экземпляр Bot
    """
    if not WEBHOOK_USE_POLLING:
        logging.info("CryptoBot polling disabled (webhook mode enabled)")
        return

    logging.info("CryptoBot polling mode enabled")

    try:
        while True:
            await asyncio.sleep(PAYMENT_CHECK_INTERVAL)

            try:
                pending = await db.get_pending_payments()

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
                        invoice = await get_invoice_status(invoice_id)

                        if invoice and invoice.get("status") == "paid":
                            success = await process_paid_invoice(bot, tg_id, invoice_id, tariff_code)
                            if success:
                                logging.info(f"Processed payment for user {tg_id}, invoice {invoice_id}")

                    except Exception as e:
                        logging.error(f"Check invoice error for {tg_id}: {e}")

                    finally:
                        await db.release_user_lock(tg_id)
            except asyncio.CancelledError:
                logging.info("CryptoBot polling task cancelled")
                raise
    except asyncio.CancelledError:
        logging.info("CryptoBot polling task shut down gracefully")
        raise
