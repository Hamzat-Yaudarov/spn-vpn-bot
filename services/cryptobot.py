import aiohttp
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from config import CRYPTOBOT_TOKEN, CRYPTOBOT_API_URL, TARIFFS, PAYMENT_CHECK_INTERVAL
import database as db
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_add_to_squad,
    remnawave_get_subscription_url,
    remnawave_extend_subscription
)


async def create_cryptobot_invoice(
    bot,
    amount: float,
    tariff_code: str,
    tg_id: int
) -> dict | None:
    """
    Создать счёт для оплаты через CryptoBot
    
    Args:
        bot: Экземпляр Bot
        amount: Сумма платежа в рублях
        tariff_code: Код тарифа
        tg_id: ID пользователя Telegram
        
    Returns:
        Словарь с информацией о счёте или None
    """
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
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok"):
                        logging.info(f"Created CryptoBot invoice for user {tg_id}")
                        return data["result"]
                else:
                    logging.error(f"CryptoBot error {resp.status}: {await resp.text()}")
    except Exception as e:
        logging.error(f"CryptoBot invoice exception: {e}")
    
    return None


async def get_invoice_status(invoice_id: str) -> dict | None:
    """
    Получить статус счёта в CryptoBot
    
    Args:
        invoice_id: ID счёта в CryptoBot
        
    Returns:
        Словарь с информацией о счёте или None
    """
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
        url = f"{CRYPTOBOT_API_URL}/getInvoices"
        params = {"invoice_ids": invoice_id}

        try:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok"):
                        invoices = data["result"]["items"]
                        if invoices:
                            return invoices[0]
        except Exception as e:
            logging.error(f"Get invoice status exception: {e}")

    return None


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
            referrer = db.get_referrer(tg_id)
            if referrer and referrer[0] and not referrer[1]:  # есть рефералит и это первый платеж
                referrer_uuid_row = db.get_user(referrer[0])
                if referrer_uuid_row and referrer_uuid_row[3]:  # remnawave_uuid существует
                    await remnawave_extend_subscription(session, referrer_uuid_row[3], 7)
                    db.increment_active_referrals(referrer[0])
                    logging.info(f"Referral bonus given to {referrer[0]}")
                
                db.mark_first_payment(tg_id)

            # Обновляем платеж в БД
            db.update_payment_status_by_invoice(invoice_id, 'paid')
            
            # Обновляем подписку пользователя
            new_until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
            db.update_subscription(tg_id, uuid, username, new_until, None)

            # Отправляем сообщение пользователю
            text = (
                "✅ <b>Оплата прошла успешно!</b>\n\n"
                f"Тариф: {tariff_code} ({days} дней)\n"
                f"<b>Ссылка подписки:</b>\n<code>{sub_url}</code>"
            )
            await bot.send_message(tg_id, text)
            
            return True

    except Exception as e:
        logging.error(f"Process paid invoice exception: {e}")
        return False


async def check_cryptobot_invoices(bot):
    """
    Фоновая задача для проверки статусов платежей в CryptoBot
    
    Args:
        bot: Экземпляр Bot
    """
    while True:
        await asyncio.sleep(PAYMENT_CHECK_INTERVAL)

        pending = db.get_pending_payments()

        if not pending:
            continue

        for payment_id, tg_id, invoice_id, tariff_code in pending:
            if not db.acquire_user_lock(tg_id):
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
                db.release_user_lock(tg_id)
