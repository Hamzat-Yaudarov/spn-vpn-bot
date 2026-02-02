import aiohttp
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from config import (
    CRYPTOBOT_TOKEN,
    CRYPTOBOT_API_URL,
    TARIFFS,
    PAYMENT_CHECK_INTERVAL,
    API_REQUEST_TIMEOUT,
    WEBHOOK_USE_POLLING
)
import database as db
from utils import retry_with_backoff, safe_api_call
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
    try:
        days = TARIFFS[tariff_code]["days"]
        uuid = None
        sub_url = None

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            # Создаём или получаем пользователя в Remnawave
            uuid, username = await remnawave_get_or_create_user(
                session, tg_id, days, extend_if_exists=True
            )

            if not uuid:
                logging.error(f"Failed to create/get Remnawave user for {tg_id}")
                # Откат: оставляем платеж в pending статусе для повторной попытки
                return False

            # Добавляем в сквад
            squad_added = await remnawave_add_to_squad(session, uuid)
            if not squad_added:
                logging.warning(f"Failed to add user {uuid} to squad")

            # Получаем ссылку подписки
            sub_url = await remnawave_get_subscription_url(session, uuid)
            if not sub_url:
                logging.warning(f"Failed to get subscription URL for {uuid}")

            # Обрабатываем реферальную программу
            try:
                referrer = await db.get_referrer(tg_id)
                if referrer and referrer[0] and not referrer[1]:  # есть рефералит и это первый платеж
                    referrer_uuid_row = await db.get_user(referrer[0])
                    if referrer_uuid_row and referrer_uuid_row['remnawave_uuid']:  # remnawave_uuid существует
                        ref_extended = await remnawave_extend_subscription(session, referrer_uuid_row['remnawave_uuid'], 7)
                        if ref_extended:
                            await db.increment_active_referrals(referrer[0])
                            logging.info(f"Referral bonus given to {referrer[0]}")

                    await db.mark_first_payment(tg_id)
            except Exception as e:
                logging.error(f"Error processing referral for user {tg_id}: {e}")
                # Реферальная ошибка не должна блокировать основной платеж

            # Обновляем подписку пользователя (ПЕРЕД отметкой платежа как paid)
            new_until = datetime.utcnow() + timedelta(days=days)
            await db.update_subscription(tg_id, uuid, username, new_until, None)

            # Обрабатываем партнёрскую программу
            try:
                from datetime import datetime as dt
                # Проверяем есть ли партнёрская ссылка для этого пользователя
                partnership_link = await db.db_execute(
                    "SELECT partner_tg_id FROM partnership_links WHERE referred_tg_id = $1",
                    (tg_id,),
                    fetch_one=True
                )

                if partnership_link:
                    partner_tg_id = partnership_link['partner_tg_id']
                    partner_info = await db.get_partner_info(partner_tg_id)

                    # Проверяем активно ли партнёрство
                    if partner_info and partner_info['partnership_until'] > dt.utcnow():
                        commission_percent = partner_info['partnership_percent']
                        commission_amount = (amount * commission_percent) / 100

                        # Начисляем комиссию партнёру
                        await db.add_partnership_earnings(
                            partner_tg_id, tg_id, tariff_code, amount, commission_amount, invoice_id
                        )
                        logging.info(f"Partnership commission added to {partner_tg_id}: {commission_amount:.2f} ₽ ({commission_percent}%)")
                    else:
                        logging.info(f"Partnership for user {partner_tg_id} is not active")
            except Exception as e:
                logging.error(f"Error processing partnership for user {tg_id}: {e}")
                # Партнёрская ошибка не должна блокировать основной платеж

            # Только после успешных операций отмечаем платеж как paid
            await db.update_payment_status_by_invoice(invoice_id, 'paid')

            # Отправляем сообщение пользователю
            text = (
                "✅ <b>Оплата прошла успешно!</b>\n\n"
                f"Тариф: {tariff_code} ({days} дней)\n"
                f"<b>Ссылка подписки:</b>\n<code>{sub_url or 'Ошибка получения ссылки'}</code>"
            )
            await bot.send_message(tg_id, text)

            return True

    except Exception as e:
        logging.error(f"Process paid invoice exception: {e}")
        # Откат: платеж остаётся в pending статусе для повторной попытки
        return False


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
