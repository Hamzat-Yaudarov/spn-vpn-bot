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
    TARIFFS_REGULAR,
    TARIFFS_ANTI_JAMMING,
    PAYMENT_CHECK_INTERVAL,
    CLEANUP_CHECK_INTERVAL,
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


async def process_paid_yookassa_payment(bot, tg_id: int, payment_id: str, tariff_code: str, subscription_type: str = 'regular') -> bool:
    """
    Обработать оплаченный платёж Yookassa и активировать подписку

    Args:
        bot: Экземпляр Bot
        tg_id: ID пользователя Telegram
        payment_id: ID платежа в Yookassa
        tariff_code: Код тарифа
        subscription_type: Тип подписки ('regular' или 'anti_jamming')

    Returns:
        True если успешно, False иначе
    """
    try:
        from config import TARIFFS_REGULAR, TARIFFS_ANTI_JAMMING
        from services.xui import create_xui_client

        # Выбираем правильный словарь тарифов
        tariffs = TARIFFS_ANTI_JAMMING if subscription_type == 'anti_jamming' else TARIFFS_REGULAR

        if tariff_code not in tariffs:
            logging.error(f"Invalid tariff code {tariff_code} for subscription type {subscription_type}")
            return False

        days = tariffs[tariff_code]["days"]
        uuid = None
        sub_url = None
        xui_url = None

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

            # Если тип подписки anti_jamming, создаём клиента в 3X-UI
            if subscription_type == 'anti_jamming':
                try:
                    xui_data = await create_xui_client(tg_id, days)
                    xui_url = xui_data['subscription_url']

                    # Сохраняем 3X-UI данные в БД
                    await db.update_xui_subscription(
                        tg_id,
                        xui_data['xui_uuid'],
                        xui_data['xui_username'],
                        xui_data['subscription_until']
                    )
                    logging.info(f"Created 3X-UI client for user {tg_id}")
                except Exception as e:
                    logging.error(f"Failed to create 3X-UI client for {tg_id}: {e}")
                    # 3X-UI ошибка не должна блокировать основной платеж, но логируем

            # Обрабатываем реферальную программу
            try:
                referrer = await db.get_referrer(tg_id)
                if referrer and referrer[0] and not referrer[1]:  # есть рефералит и это первый платеж
                    referrer_id = referrer[0]
                    referrer_uuid_row = await db.get_user(referrer_id)

                    if referrer_uuid_row and referrer_uuid_row['remnawave_uuid']:
                        try:
                            ref_extended = await remnawave_extend_subscription(
                                session,
                                referrer_uuid_row['remnawave_uuid'],
                                7
                            )
                            if ref_extended:
                                await db.increment_active_referrals(referrer_id)
                                logging.info(f"Referral bonus (+7 days) given to {referrer_id} by user {tg_id}")
                            else:
                                logging.warning(f"Failed to extend subscription for referrer {referrer_id}")
                        except Exception as ref_err:
                            logging.error(f"Error extending referrer subscription for {referrer_id}: {ref_err}")
                    else:
                        logging.warning(f"Referrer {referrer_id} has no active Remnawave account")

                    # Отмечаем что пользователь сделал первый платеж
                    await db.mark_first_payment(tg_id)
            except Exception as e:
                logging.error(f"Error processing referral for user {tg_id}: {e}")

            # Обновляем подписку пользователя (ПЕРЕД отметкой платежа как paid)
            new_until = datetime.utcnow() + timedelta(days=days)
            await db.update_subscription(tg_id, uuid, username, new_until, None)

            # Только после успешных операций отмечаем платеж как paid
            await db.update_payment_status_by_invoice(payment_id, 'paid')

            # Формируем сообщение в зависимости от типа подписки
            if subscription_type == 'anti_jamming' and xui_url:
                text = (
                    "✅ <b>Оплата прошла успешно!</b>\n\n"
                    f"Тариф: {tariff_code} ({days} дней)\n"
                    "<b>Обычная подписка + Обход глушилок</b>\n\n"
                    "<b>📌 Ссылка для обычного подключения:</b>\n"
                    f"<code>{sub_url or 'Ошибка получения ссылки'}</code>\n\n"
                    "<b>📌 Ссылка для обхода глушилок:</b>\n"
                    f"<code>{xui_url}</code>"
                )
            else:
                text = (
                    "✅ <b>Оплата прошла успешно!</b>\n\n"
                    f"Тариф: {tariff_code} ({days} дней)\n"
                    "<b>Обычная подписка</b>\n\n"
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

            if not await db.acquire_user_lock(tg_id):
                continue

            try:
                payment = await get_payment_status(invoice_id)

                if payment and payment.get("status") == "succeeded":
                    # Получаем тип подписки пользователя
                    sub_type = await db.get_subscription_type(tg_id)
                    success = await process_paid_yookassa_payment(bot, tg_id, invoice_id, tariff_code, sub_type)
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
