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
    from main import get_global_session
    
    try:
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

        session = get_global_session()
        
        async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("ok"):
                    logging.info(f"[USER:{tg_id}] Created CryptoBot invoice: {data.get('result', {}).get('invoice_id')}")
                    return data["result"]
            
            error_text = await resp.text()
            logging.error(f"CryptoBot error {resp.status}: {error_text}")
    except asyncio.TimeoutError:
        logging.error(f"[USER:{tg_id}] CryptoBot timeout")
    except Exception as e:
        logging.error(f"[USER:{tg_id}] CryptoBot invoice exception: {e}")
    
    return None


async def get_invoice_status(invoice_id: str) -> dict | None:
    """
    Получить статус счёта в CryptoBot
    
    Args:
        invoice_id: ID счёта в CryptoBot
        
    Returns:
        Словарь с информацией о счёте или None
    """
    from main import get_global_session
    
    try:
        headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
        url = f"{CRYPTOBOT_API_URL}/getInvoices"
        params = {"invoice_ids": invoice_id}

        session = get_global_session()
        
        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("ok"):
                    invoices = data["result"]["items"]
                    if invoices:
                        return invoices[0]
            else:
                logging.error(f"Get invoice status error {resp.status}")
    except asyncio.TimeoutError:
        logging.error(f"CryptoBot status check timeout for {invoice_id}")
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
        await db.update_payment_status_by_invoice(invoice_id, 'paid')
        
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
        logging.error(f"[USER:{tg_id}] Process paid invoice exception: {e}", exc_info=True)
        return False


async def check_cryptobot_invoices(bot):
    """
    Фоновая задача для проверки статусов платежей в CryptoBot
    
    Args:
        bot: Экземпляр Bot
    """
    logger = logging.getLogger(__name__)
    logger.info("CryptoBot payment checker started")
    
    while True:
        try:
            await asyncio.sleep(PAYMENT_CHECK_INTERVAL)

            pending = await db.get_pending_payments_by_provider('cryptobot')

            if not pending:
                continue

            logger.debug(f"Checking {len(pending)} pending CryptoBot payments")

            for payment_record in pending:
                payment_id = payment_record['id']
                tg_id = payment_record['tg_id']
                invoice_id = payment_record['invoice_id']
                tariff_code = payment_record['tariff_code']
                
                async with db.UserLockContext(tg_id) as acquired:
                    if not acquired:
                        continue

                    try:
                        invoice = await get_invoice_status(invoice_id)
                        
                        if invoice and invoice.get("status") == "paid":
                            # Проверяем идемпотентность
                            if not await db.mark_payment_processed(invoice_id):
                                logger.debug(f"[USER:{tg_id}] Invoice {invoice_id} already processed")
                                continue
                            
                            success = await process_paid_invoice(bot, tg_id, invoice_id, tariff_code)
                            if success:
                                logger.info(f"[USER:{tg_id}] Processed CryptoBot payment: {invoice_id}")

                    except Exception as e:
                        logger.error(f"[USER:{tg_id}] Check CryptoBot invoice error: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"CryptoBot checker exception: {e}", exc_info=True)
            await asyncio.sleep(PAYMENT_CHECK_INTERVAL)
