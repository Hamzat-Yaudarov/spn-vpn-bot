import logging
import aiohttp
import hashlib
import json
import hmac
from typing import Optional, Dict, Any
from config import ONEPLAT_SHOP_ID, ONEPLAT_SHOP_SECRET, ONEPLAT_API_URL, TARIFFS, DEFAULT_SQUAD_UUID
import database as db
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_add_to_squad,
    remnawave_get_subscription_url
)
from datetime import datetime, timedelta, timezone


def generate_signature(shop_id: str, secret: str, amount: int, merchant_order_id: str) -> str:
    """
    Генерирует подпись для запроса к 1Plat API
    
    Формула: MD5(shop_id:secret:amount:merchant_order_id)
    """
    data = f"{shop_id}:{secret}:{amount}:{merchant_order_id}"
    return hashlib.md5(data.encode()).hexdigest()


def verify_callback_signature_v2(merchant_id: str, amount: int, shop_id: str, shop_secret: str, signature_v2: str) -> bool:
    """
    Верифицирует подпись коллбека (signature_v2)
    
    Формула: MD5(merchantId + amount + shopId + shopSecret)
    """
    data = f"{merchant_id}{amount}{shop_id}{shop_secret}"
    expected_sig = hashlib.md5(data.encode()).hexdigest()
    return expected_sig == signature_v2


def verify_callback_signature(payload: Dict[str, Any], shop_secret: str, signature: str) -> bool:
    """
    Верифицирует подпись коллбека (signature)

    Формула: HMAC-SHA256(JSON payload, secret)
    """
    # Создаём копию payload без подписей
    payload_copy = {k: v for k, v in payload.items() if k not in ['signature', 'signature_v2']}

    payload_json = json.dumps(payload_copy, separators=(',', ':'), sort_keys=True)

    expected_sig = hmac.new(
        shop_secret.encode(),
        payload_json.encode(),
        hashlib.sha256
    ).hexdigest()

    return expected_sig == signature


async def create_oneplat_payment(
    merchant_order_id: str,
    tg_id: int,
    amount: int,
    tariff_code: str,
    method: str = "card"
) -> Optional[Dict[str, Any]]:
    """
    Создаёт платёж через 1Plat API
    
    Args:
        merchant_order_id: ID платежа на стороне мерчанта
        tg_id: ID пользователя Telegram
        amount: Сумма в рублях
        tariff_code: Код тарифа (1m, 3m, 6m, 12m)
        method: Метод оплаты (card, sbp, crypto, qr)
    
    Returns:
        Словарь с информацией о платеже или None при ошибке
    """
    
    if not ONEPLAT_SHOP_ID or not ONEPLAT_SHOP_SECRET:
        logging.error("1Plat credentials not configured")
        return None
    
    url = f"{ONEPLAT_API_URL}/api/merchant/order/create/by-api"
    
    headers = {
        "x-shop": ONEPLAT_SHOP_ID,
        "x-secret": ONEPLAT_SHOP_SECRET,
        "Content-Type": "application/json"
    }
    
    payload = {
        "merchant_order_id": merchant_order_id,
        "user_id": tg_id,
        "amount": amount,
        "method": method,
        "email": f"{tg_id}@temp.com"
    }
    
    connector = aiohttp.TCPConnector(ssl=False)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                response_text = await resp.text()
                logging.info(f"1Plat API response status: {resp.status}")
                logging.info(f"1Plat API response body: {response_text}")

                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success"):
                        logging.info(f"1Plat payment created: {merchant_order_id}, guid={data.get('guid')}")
                        return data
                    else:
                        logging.error(f"1Plat error: {data}")
                        return None
                else:
                    logging.error(f"1Plat API error: {resp.status} - {response_text}")
                    return None

    except Exception as e:
        logging.error(f"1Plat request error: {e}")
        return None


async def get_oneplat_payment_info(guid: str) -> Optional[Dict[str, Any]]:
    """
    Получает информацию о платеже по guid
    
    Args:
        guid: GUID платежа
    
    Returns:
        Словарь с информацией о платеже или None при ошибке
    """
    
    if not ONEPLAT_SHOP_ID or not ONEPLAT_SHOP_SECRET:
        logging.error("1Plat credentials not configured")
        return None
    
    url = f"{ONEPLAT_API_URL}/api/merchant/order/info/{guid}/by-api"
    
    headers = {
        "x-shop": ONEPLAT_SHOP_ID,
        "x-secret": ONEPLAT_SHOP_SECRET
    }
    
    connector = aiohttp.TCPConnector(ssl=False)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success"):
                        return data.get("payment")
                    else:
                        logging.error(f"1Plat error: {data}")
                        return None
                else:
                    logging.error(f"1Plat API error: {resp.status}")
                    return None
                    
    except Exception as e:
        logging.error(f"1Plat request error: {e}")
        return None


async def process_oneplat_callback(callback_data: Dict[str, Any]) -> bool:
    """
    Обрабатывает коллбек от 1Plat
    
    Статусы платежа:
    0 - Платеж ожидает оплаты
    1 - Платеж оплачен, ожидает подтверждения мерчантом
    2 - Платеж успешно подтвержден
    
    Args:
        callback_data: Данные коллбека из 1Plat
    
    Returns:
        True если обработано успешно, False иначе
    """
    
    try:
        # Верифицируем подпись
        signature_v2 = callback_data.get("signature_v2")
        merchant_id = callback_data.get("merchant_id")
        amount = callback_data.get("amount")
        shop_id = ONEPLAT_SHOP_ID
        
        # Проверяем подпись
        if not verify_callback_signature_v2(merchant_id, amount, shop_id, ONEPLAT_SHOP_SECRET, signature_v2):
            logging.error(f"1Plat callback signature verification failed")
            return False
        
        # Получаем информацию о платеже из БД
        guid = callback_data.get("guid")
        status = callback_data.get("status")
        payment_id = callback_data.get("payment_id")
        user_id = callback_data.get("user_id")
        
        logging.info(f"1Plat callback: payment_id={payment_id}, guid={guid}, status={status}, user_id={user_id}")
        
        # Получаем платёж из БД
        payment = await db.get_payment_by_guid(guid)
        if not payment:
            logging.error(f"Payment not found in DB: guid={guid}")
            return False
        
        tg_id = payment['tg_id']
        tariff_code = payment['tariff_code']
        
        # Статус 1 или 2 означает успешную оплату
        if status in [1, 2]:
            # Активируем подписку
            success = await activate_oneplat_subscription(tg_id, tariff_code)
            
            if success:
                # Обновляем статус платежа в БД
                await db.update_payment_status_by_guid(guid, "completed")
                logging.info(f"1Plat subscription activated for user {tg_id}")
                return True
            else:
                logging.error(f"Failed to activate subscription for user {tg_id}")
                return False
        
        return True
        
    except Exception as e:
        logging.error(f"1Plat callback processing error: {e}")
        return False


async def activate_oneplat_subscription(tg_id: int, tariff_code: str) -> bool:
    """
    Активирует подписку пользователя после успешной оплаты через 1Plat
    
    Args:
        tg_id: ID пользователя Telegram
        tariff_code: Код тарифа
    
    Returns:
        True если успешно, False иначе
    """
    
    try:
        tariff = TARIFFS.get(tariff_code)
        if not tariff:
            logging.error(f"Invalid tariff code: {tariff_code}")
            return False
        
        days = tariff["days"]
        
        # Создаём или получаем пользователя в Remnawave
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            uuid, username = await remnawave_get_or_create_user(
                session,
                tg_id,
                days=days,
                extend_if_exists=True
            )
            
            if not uuid:
                logging.error(f"Failed to create/get Remnawave user: {tg_id}")
                return False
            
            # Добавляем в сквад
            await remnawave_add_to_squad(session, uuid)
            
            # Обновляем подписку в БД
            new_until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
            await db.update_subscription(tg_id, uuid, username, new_until, DEFAULT_SQUAD_UUID)
            
            logging.info(f"1Plat subscription activated: user={tg_id}, days={days}, uuid={uuid}")
            return True
            
    except Exception as e:
        logging.error(f"1Plat subscription activation error: {e}")
        return False
