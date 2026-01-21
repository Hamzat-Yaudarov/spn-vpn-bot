import aiohttp
import logging
import asyncio
import base64
import uuid
from datetime import datetime, timedelta, timezone
from config import (
    YOOKASSA_SHOP_ID,
    YOOKASSA_SECRET_KEY,
    YOOKASSA_API_URL,
    TARIFFS,
    PAYMENT_CHECK_INTERVAL,
    CLEANUP_CHECK_INTERVAL,
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
                "return_url": "https://t.me/WaySPN_robot"  # После оплаты вернёт в бот
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


async def process_paid_yookassa_payment(bot, tg_id: int, payment_id: str, tariff_code: str, subscription_type: str = "normal") -> bool:
    """
    Обработать оплаченный платёж Yookassa и активировать подписку

    Args:
        bot: Экземпляр Bot
        tg_id: ID пользователя Telegram
        payment_id: ID платежа в Yookassa
        tariff_code: Код тарифа
        subscription_type: Тип подписки (normal или vip)

    Returns:
        True если успешно, False иначе
    """
    try:
        days = TARIFFS[tariff_code]["days"]
        price = TARIFFS[tariff_code]["price"]
        uuid = None
        sub_url = None

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

        # Если выбрана VIP подписка, создаём её через XUI
        if subscription_type == "vip":
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
        await db.update_payment_status_by_invoice(payment_id, 'paid')

        # Отправляем сообщение пользователю
        sub_type_text = "Обычная подписка + Обход глушилок (VIP)" if subscription_type == "vip" else "Обычная подписка"
        text = (
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            f"Тариф: {tariff_code} ({days} дней)\n"
            f"Тип: {sub_type_text}\n"
            f"<b>Ссылка подписки:</b>\n<code>{sub_url or 'Ошибка получения ссылки'}</code>"
        )
        await bot.send_message(tg_id, text)

        return True

    except Exception as e:
        logging.error(f"Process Yookassa payment exception: {e}")
        # Откат: платеж остаётся в pending статусе для повторной попытки
        return False


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
            subscription_type = payment_record.get('subscription_type', 'normal')

            if not await db.acquire_user_lock(tg_id):
                continue

            try:
                payment = await get_payment_status(invoice_id)

                if payment and payment.get("status") == "succeeded":
                    success = await process_paid_yookassa_payment(bot, tg_id, invoice_id, tariff_code, subscription_type)
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
