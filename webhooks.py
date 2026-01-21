import logging
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta, timezone
from config import WEBHOOK_HOST, WEBHOOK_PORT, TARIFFS, DEFAULT_SQUAD_UUID
import database as db
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_add_to_squad,
    remnawave_get_subscription_url,
    remnawave_extend_subscription
)
import aiohttp


logger = logging.getLogger(__name__)

app = FastAPI(title="SPN VPN Bot Webhooks")

# Глобальная переменная для хранения экземпляра бота
_bot = None


def set_bot(bot):
    """Установить экземпляр бота для отправки уведомлений"""
    global _bot
    _bot = bot


async def _process_paid_invoice(bot, tg_id: int, invoice_id: str, tariff_code: str, subscription_type: str = 'regular') -> bool:
    """
    Обработать оплаченный счёт и активировать подписку

    Args:
        bot: Экземпляр Bot
        tg_id: ID пользователя Telegram
        invoice_id: ID счёта в CryptoBot
        tariff_code: Код тарифа
        subscription_type: Тип подписки (regular или anti_jamming)

    Returns:
        True если успешно, False иначе
    """
    from config import TARIFFS_REGULAR, TARIFFS_ANTI_JAMMING
    from services.xui import create_xui_client

    if not await db.acquire_user_lock(tg_id):
        logger.warning(f"Could not acquire lock for user {tg_id}")
        return False

    try:
        # Выбираем правильный словарь тарифов
        tariffs = TARIFFS_ANTI_JAMMING if subscription_type == 'anti_jamming' else TARIFFS_REGULAR

        if tariff_code not in tariffs:
            logger.error(f"Invalid tariff code {tariff_code} for subscription type {subscription_type}")
            return False

        days = tariffs[tariff_code]["days"]
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
                logger.error(f"Failed to create/get Remnawave user for {tg_id}")
                return False

            # Добавляем в сквад
            squad_added = await remnawave_add_to_squad(session, uuid)
            if not squad_added:
                logger.warning(f"Failed to add user {uuid} to squad")

            # Получаем ссылку подписки
            sub_url = await remnawave_get_subscription_url(session, uuid)
            if not sub_url:
                logger.warning(f"Failed to get subscription URL for {uuid}")

            # Если тип подписки anti_jamming, создаём клиента в 3X-UI
            xui_url = None
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
                    logger.info(f"Created 3X-UI client for user {tg_id}")
                except Exception as e:
                    logger.error(f"Failed to create 3X-UI client for {tg_id}: {e}")

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
                                logger.info(f"Referral bonus (+7 days) given to {referrer_id} by user {tg_id}")
                            else:
                                logger.warning(f"Failed to extend subscription for referrer {referrer_id}")
                        except Exception as ref_err:
                            logger.error(f"Error extending referrer subscription for {referrer_id}: {ref_err}")
                    else:
                        logger.warning(f"Referrer {referrer_id} has no active Remnawave account")

                    # Отмечаем что пользователь сделал первый платеж
                    # (независимо от статуса реферала)
                    await db.mark_first_payment(tg_id)
            except Exception as e:
                logger.error(f"Error processing referral for user {tg_id}: {e}")
                # Реферальная ошибка не должна блокировать основной платеж
                # но мы логируем её для анализа

            # Обновляем подписку пользователя
            new_until = datetime.utcnow() + timedelta(days=days)
            await db.update_subscription(tg_id, uuid, username, new_until, None)

            # Отмечаем платеж как paid
            await db.update_payment_status_by_invoice(invoice_id, 'paid')

            # Отправляем сообщение пользователю
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

            if bot:
                try:
                    await bot.send_message(tg_id, text)
                except Exception as e:
                    logger.error(f"Failed to send message to user {tg_id}: {e}")

            return True

    except Exception as e:
        logger.error(f"Process paid invoice exception: {e}")
        return False
    
    finally:
        await db.release_user_lock(tg_id)


@app.post("/webhook/cryptobot")
async def webhook_cryptobot(request: Request):
    """
    Webhook endpoint для CryptoBot платежей
    
    CryptoBot отправляет JSON с информацией об оплате:
    {
        "update_id": 123,
        "invoice_id": "456",
        "status": "paid",
        "paid_at": "2024-01-16T12:00:00Z"
    }
    """
    try:
        payload = await request.json()
        logger.info(f"CryptoBot webhook received: {payload}")
        
        invoice_id = payload.get("invoice_id")
        status = payload.get("status")
        
        if not invoice_id or not status:
            logger.warning(f"Invalid CryptoBot webhook payload: {payload}")
            raise HTTPException(status_code=400, detail="Missing required fields")
        
        if status != "paid":
            logger.info(f"Ignoring CryptoBot webhook with status: {status}")
            return JSONResponse({"ok": True})
        
        # Получаем информацию о платеже из БД
        result = await db.db_execute(
            """
            SELECT tg_id, tariff_code, subscription_type
            FROM payments
            WHERE invoice_id = $1 AND status = 'pending' AND provider = 'cryptobot'
            LIMIT 1
            """,
            (invoice_id,),
            fetch_one=True
        )

        if not result:
            logger.warning(f"Payment not found for invoice {invoice_id}")
            return JSONResponse({"ok": True})

        tg_id = result['tg_id']
        tariff_code = result['tariff_code']
        subscription_type = result['subscription_type']

        # Обрабатываем платеж асинхронно
        if _bot:
            asyncio.create_task(_process_paid_invoice(_bot, tg_id, invoice_id, tariff_code, subscription_type))
        else:
            logger.error("Bot not available for webhook processing")
        
        return JSONResponse({"ok": True})
    
    except Exception as e:
        logger.error(f"CryptoBot webhook error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/webhook/yookassa")
async def webhook_yookassa(request: Request):
    """
    Webhook endpoint для Yookassa платежей
    
    Yookassa отправляет JSON с информацией об оплате:
    {
        "type": "notification",
        "event": "payment.succeeded",
        "object": {
            "id": "123",
            "status": "succeeded",
            "metadata": {
                "tg_id": "456",
                "tariff_code": "1m"
            }
        }
    }
    """
    try:
        payload = await request.json()
        logger.info(f"Yookassa webhook received: {payload.get('type')} / {payload.get('event')}")
        
        event = payload.get("event")
        obj = payload.get("object", {})
        
        if event != "payment.succeeded":
            logger.info(f"Ignoring Yookassa event: {event}")
            return JSONResponse({"ok": True})
        
        payment_id = obj.get("id")
        metadata = obj.get("metadata", {})
        
        tg_id_str = metadata.get("tg_id")
        tariff_code = metadata.get("tariff_code")
        
        if not all([payment_id, tg_id_str, tariff_code]):
            logger.warning(f"Invalid Yookassa webhook payload: {payload}")
            raise HTTPException(status_code=400, detail="Missing required fields")
        
        tg_id = int(tg_id_str)
        
        # Получаем информацию о платеже из БД
        result = await db.db_execute(
            """
            SELECT tg_id, tariff_code, subscription_type
            FROM payments
            WHERE invoice_id = $1 AND status = 'pending' AND provider = 'yookassa'
            LIMIT 1
            """,
            (payment_id,),
            fetch_one=True
        )

        if not result:
            logger.warning(f"Payment not found for payment ID {payment_id}")
            return JSONResponse({"ok": True})

        subscription_type = result['subscription_type']

        # Обрабатываем платеж асинхронно
        if _bot:
            asyncio.create_task(_process_paid_invoice(_bot, tg_id, payment_id, tariff_code, subscription_type))
        else:
            logger.error("Bot not available for webhook processing")
        
        return JSONResponse({"ok": True})
    
    except Exception as e:
        logger.error(f"Yookassa webhook error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return JSONResponse({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


async def run_webhook_server():
    """
    Запустить FastAPI сервер для webhook'ов
    
    Используется uvicorn для асинхронного запуска
    """
    import uvicorn
    
    logger.info(f"Starting webhook server on {WEBHOOK_HOST}:{WEBHOOK_PORT}")
    
    config = uvicorn.Config(
        app,
        host=WEBHOOK_HOST,
        port=WEBHOOK_PORT,
        log_level="info",
        access_log=True
    )
    
    server = uvicorn.Server(config)
    await server.serve()
