import aiohttp
import logging
import hashlib
import hmac
import json
from typing import Optional, Dict, Any
from config import ONEPLAT_SHOP_ID, ONEPLAT_SHOP_SECRET, ONEPLAT_BASE_URL


async def create_oneplat_payment(
    amount: int,
    tariff_code: str,
    tg_id: int,
    method: str = "card"
) -> Optional[Dict[str, Any]]:
    """
    Создать платеж через 1Plat API
    
    Args:
        amount: Сумма платежа в рублях (целое число)
        tariff_code: Код тарифа (1m, 3m, 6m, 12m)
        tg_id: ID пользователя Telegram
        method: Метод оплаты ("card" или "sbp")
        
    Returns:
        Словарь с информацией о платеже или None
    """
    url = f"{ONEPLAT_BASE_URL}/api/merchant/order/create/by-api"
    
    headers = {
        "x-shop": str(ONEPLAT_SHOP_ID),
        "x-secret": ONEPLAT_SHOP_SECRET,
        "Content-Type": "application/json"
    }
    
    merchant_order_id = f"spn_{tg_id}_{tariff_code}_{int(__import__('time').time())}"
    
    payload = {
        "merchant_order_id": merchant_order_id,
        "user_id": tg_id,
        "amount": amount,
        "method": method,
        "currency": "RUB",
        "email": f"{tg_id}@spn.local"
    }
    
    connector = aiohttp.TCPConnector(ssl=False)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success"):
                        logging.info(f"Created 1Plat payment for user {tg_id}, amount {amount}")
                        return {
                            "guid": data["guid"],
                            "payment": data["payment"],
                            "url": data["url"],
                            "merchant_order_id": merchant_order_id
                        }
                    else:
                        logging.error(f"1Plat API error: {data}")
                else:
                    logging.error(f"1Plat request failed ({resp.status}): {await resp.text()}")
    except Exception as e:
        logging.error(f"1Plat payment creation exception: {e}")
    
    return None


async def get_payment_info(guid: str) -> Optional[Dict[str, Any]]:
    """
    Получить информацию о платеже из 1Plat
    
    Args:
        guid: GUID платежа от 1Plat
        
    Returns:
        Словарь с информацией о платеже или None
    """
    url = f"{ONEPLAT_BASE_URL}/api/merchant/order/info/{guid}/by-api"
    
    headers = {
        "x-shop": str(ONEPLAT_SHOP_ID),
        "x-secret": ONEPLAT_SHOP_SECRET
    }
    
    connector = aiohttp.TCPConnector(ssl=False)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success"):
                        return data.get("payment")
                    else:
                        logging.error(f"1Plat get payment error: {data}")
                else:
                    logging.error(f"1Plat get payment failed ({resp.status}): {await resp.text()}")
    except Exception as e:
        logging.error(f"1Plat get payment exception: {e}")
    
    return None


def verify_callback_signature(
    body: Dict[str, Any],
    signature: str,
    use_v2: bool = False
) -> bool:
    """
    Проверить подпись callback'а от 1Plat
    
    Используются два способа проверки подписи в 1Plat:
    1. signature - HMAC-SHA256(JSON.stringify(payload))
    2. signature_v2 - MD5(merchantId + amount + shopId + shopSecret)
    
    Args:
        body: Body callback'а от 1Plat
        signature: Подпись из callback'а
        use_v2: Использовать signature_v2 вместо signature
        
    Returns:
        True если подпись валидна, False иначе
    """
    try:
        if use_v2:
            # Проверка signature_v2: MD5(merchantId + amount + shopId + shopSecret)
            merchant_id = body.get("merchant_id", "")
            amount = body.get("amount", "")
            shop_id = body.get("shop_id", "")
            
            payload_str = f"{merchant_id}{amount}{shop_id}{ONEPLAT_SHOP_SECRET}"
            expected_sig = hashlib.md5(payload_str.encode()).hexdigest()
            
            return signature == expected_sig
        else:
            # Проверка signature: HMAC-SHA256(JSON.stringify(payload))
            payload = body.get("payload", {})
            
            # Удаляем поля signature и signature_v2 если они есть
            payload_copy = {k: v for k, v in payload.items() if k not in ["signature", "signature_v2"]}
            
            payload_str = json.dumps(payload_copy, separators=(',', ':'), sort_keys=True)
            expected_sig = hmac.new(
                ONEPLAT_SHOP_SECRET.encode(),
                payload_str.encode(),
                hashlib.sha256
            ).hexdigest()
            
            return signature == expected_sig
    except Exception as e:
        logging.error(f"Signature verification error: {e}")
        return False


def verify_callback(body: Dict[str, Any]) -> bool:
    """
    Проверить callback от 1Plat (попробовать оба метода подписи)
    
    Args:
        body: Body callback'а от 1Plat
        
    Returns:
        True если подпись валидна (хотя бы одна из двух)
    """
    signature_v1 = body.get("signature", "")
    signature_v2 = body.get("signature_v2", "")
    
    # Пытаемся проверить обе подписи
    v1_valid = verify_callback_signature(body, signature_v1, use_v2=False) if signature_v1 else False
    v2_valid = verify_callback_signature(body, signature_v2, use_v2=True) if signature_v2 else False
    
    # Валидна если хотя бы одна подпись прошла проверку
    return v1_valid or v2_valid


def get_payment_status_description(status: int) -> str:
    """
    Получить описание статуса платежа
    
    Args:
        status: Код статуса от 1Plat
        
    Returns:
        Описание статуса
    """
    statuses = {
        -2: "Ошибка при выписании счета",
        -1: "Черновик (ожидает выбора метода оплаты)",
        0: "Ожидает оплаты",
        1: "Оплачен (ожидает подтверждения мерчантом)",
        2: "Подтвержден и закрыт"
    }
    return statuses.get(status, f"Неизвестный статус: {status}")
