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
    WEBHOOK_USE_POLLING,
    DEFAULT_SQUAD_UUID
)
import database as db
from utils import retry_with_backoff, safe_api_call
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_add_to_squad,
    remnawave_get_subscription_url,
    remnawave_extend_subscription
)
from services.xui_panel import (
    get_xui_session,
    xui_create_or_extend_client,
    xui_extend_client
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


async def process_paid_invoice(bot, tg_id: int, invoice_id: str, tariff_code: str, subscription_type: str = "normal") -> bool:
    """
    Обработать оплаченный счёт и активировать подписку

    Args:
        bot: Экземпляр Bot
        tg_id: ID пользователя Telegram
        invoice_id: ID счёта в CryptoBot
        tariff_code: Код тарифа
        subscription_type: Тип подписки (normal или vip)

    Returns:
        True если успешно, False иначе
    """
    try:
        days = TARIFFS[tariff_code]["days"]
        uuid = None
        sub_url = None
        price = TARIFFS[tariff_code]["price"]

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            # Создаём или получаем пользователя в Remnawave для обычной подписки
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

            # Обновляем обычную подписку пользователя в БД
            new_until = datetime.utcnow() + timedelta(days=days)
            await db.update_subscription(tg_id, uuid, username, new_until, DEFAULT_SQUAD_UUID)

        # Если выбрана VIP подписка или комбо, создаём её через XUI
        if subscription_type in ("vip", "combo"):
            xui_session = await get_xui_session()
            if xui_session:
                try:
                    vip_uuid, vip_email = await xui_create_or_extend_client(xui_session, tg_id, days)
                    if vip_uuid and vip_email:
                        new_vip_until = datetime.utcnow() + timedelta(days=days)
                        await db.update_vip_subscription(tg_id, vip_uuid, vip_email, new_vip_until)
                except Exception as e:
                    logging.warning(f"Failed to create/extend VIP subscription: {e}")
                finally:
                    await xui_session.close()

        # Обрабатываем реферальную программу (25% кешбэк вместо +7 дней)
        try:
            referrer = await db.get_referrer(tg_id)
            if referrer and referrer[0] and not referrer[1]:  # есть рефералит и это первый платеж
                # Добавляем 25% от цены покупки на баланс рефералита
                cashback = price * 0.25
                await db.add_balance(referrer[0], cashback)
                await db.increment_active_referrals(referrer[0])
                logging.info(f"Referral cashback of {cashback}₽ (25% of {price}₽) given to {referrer[0]}")

                # Уведомляем рефералита о кешбэке
                try:
                    await bot.send_message(
                        referrer[0],
                        f"💰 <b>Кешбэк от реферала!</b>\n\n"
                        f"Ваш реферал совершил покупку на {price} ₽\n"
                        f"Вы получили 25% кешбэк: <b>{cashback:.2f} ₽</b>\n\n"
                        f"Баланс пополнен! Используйте его для покупки подписки."
                    )
                except Exception as e:
                    logging.warning(f"Failed to notify referrer {referrer[0]}: {e}")

                await db.mark_first_payment(tg_id)
        except Exception as e:
            logging.error(f"Error processing referral for user {tg_id}: {e}")
            # Реферальная ошибка не должна блокировать основной платеж

        # Только после успешных операций отмечаем платеж как paid
        await db.update_payment_status_by_invoice(invoice_id, 'paid')

        # Отправляем сообщение пользователю
        if subscription_type == "combo":
            sub_type_text = "Обычная подписка + Обход глушилок"
        elif subscription_type == "vip":
            sub_type_text = "Обход глушилок (VIP)"
        else:
            sub_type_text = "Обычная подписка"

        text = (
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            f"Тариф: {tariff_code} ({days} дней)\n"
            f"Тип: {sub_type_text}\n"
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
                    subscription_type = payment_record.get('subscription_type', 'normal')

                    if not await db.acquire_user_lock(tg_id):
                        continue

                    try:
                        invoice = await get_invoice_status(invoice_id)

                        if invoice and invoice.get("status") == "paid":
                            if subscription_type == "topup":
                                # Пополнение баланса
                                amount = int(tariff_code.split("_")[1])
                                await db.add_balance(tg_id, amount)
                                await db.update_payment_status_by_invoice(invoice_id, 'paid')
                                logging.info(f"Processed topup for user {tg_id}, amount {amount}₽")
                            else:
                                # Покупка подписки
                                success = await process_paid_invoice(bot, tg_id, invoice_id, tariff_code, subscription_type)
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
