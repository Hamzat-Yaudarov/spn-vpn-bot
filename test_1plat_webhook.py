#!/usr/bin/env python3
"""
Скрипт для локального тестирования webhook'а от 1Plat

Использование:
    python test_1plat_webhook.py [status] [guid]
    
Примеры:
    python test_1plat_webhook.py 0      # Платеж ожидает оплаты
    python test_1plat_webhook.py 1      # Платеж оплачен
    python test_1plat_webhook.py 2      # Платеж подтвержден
    
Убедитесь, что бот запущен перед тестированием!
"""

import asyncio
import json
import hashlib
import hmac
import sys
import aiohttp
from datetime import datetime, timezone, timedelta

# Конфигурация для тестирования
WEBHOOK_URL = "http://localhost:8080/1plat-webhook"
SHOP_ID = "1234"
SHOP_SECRET = "test_secret_key"
PAYMENT_GUID = "test-guid-12345"
MERCHANT_ID = "1234"
TG_ID = 123456789
AMOUNT = 100


def generate_signature_v2(merchant_id, amount, shop_id, shop_secret):
    """Генерируем signature_v2 (MD5)"""
    payload = f"{merchant_id}{amount}{shop_id}{shop_secret}"
    return hashlib.md5(payload.encode()).hexdigest()


def generate_signature(payload_dict, shop_secret):
    """Генерируем signature (HMAC-SHA256)"""
    payload_str = json.dumps(payload_dict, separators=(',', ':'), sort_keys=True)
    return hmac.new(
        shop_secret.encode(),
        payload_str.encode(),
        hashlib.sha256
    ).hexdigest()


async def test_webhook(status=0, guid=None):
    """
    Отправляем тестовый callback на webhook
    
    Args:
        status: Статус платежа
        guid: GUID платежа
    """
    if guid is None:
        guid = PAYMENT_GUID
    
    # Формируем payload
    payload = {
        "guid": guid,
        "payment_id": 12345,
        "merchant_id": MERCHANT_ID,
        "user_id": TG_ID,
        "status": status,
        "amount": AMOUNT,
        "amount_to_pay": AMOUNT,
        "amount_to_shop": int(AMOUNT * 0.85),
        "expired": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    
    # Генерируем подписи
    signature_v2 = generate_signature_v2(MERCHANT_ID, AMOUNT, SHOP_ID, SHOP_SECRET)
    signature = generate_signature(payload, SHOP_SECRET)
    
    # Формируем body
    body = {
        **payload,
        "signature": signature,
        "signature_v2": signature_v2,
        "payload": payload
    }
    
    print(f"\n{'='*60}")
    print("🔹 Отправляем тестовый webhook")
    print(f"{'='*60}")
    print(f"URL: {WEBHOOK_URL}")
    print(f"Status: {status}")
    print(f"GUID: {guid}")
    print(f"\nBody:\n{json.dumps(body, indent=2)}")
    print(f"{'='*60}\n")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                WEBHOOK_URL,
                json=body,
                headers={"Content-Type": "application/json"}
            ) as resp:
                response_text = await resp.text()
                
                print(f"✅ Response Status: {resp.status}")
                print(f"Response Body: {response_text}")
                
                if resp.status == 200:
                    try:
                        response_json = json.loads(response_text)
                        print(f"✅ Webhook обработан успешно!")
                        print(f"Response JSON: {json.dumps(response_json, indent=2)}")
                    except:
                        pass
                else:
                    print(f"❌ Ошибка! Статус: {resp.status}")
                    
    except Exception as e:
        print(f"❌ Ошибка при отправке webhook'а: {e}")
        print(f"Убедитесь, что сервер запущен на localhost:8080")
        print(f"Запустите бот: python main.py")


async def main():
    """Главная функция"""
    
    print("""
╔════════════════════════════════════════════════════════════╗
║         1Plat Webhook Test Script                         ║
║                                                            ║
║ Тестирование webhook'а для получения callback'ов от 1Plat ║
╚════════════════════════════════════════════════════════════╝
""")
    
    # Получаем статус из аргументов
    status = 0
    guid = None
    
    if len(sys.argv) > 1:
        try:
            status = int(sys.argv[1])
        except ValueError:
            print(f"❌ Ошибка: статус должен быть числом, получено: {sys.argv[1]}")
            sys.exit(1)
    
    if len(sys.argv) > 2:
        guid = sys.argv[2]
    
    # Описания статусов
    status_descriptions = {
        -2: "Ошибка при выписании счета",
        -1: "Черновик (ожидает выбора метода)",
        0: "Ожидает оплаты",
        1: "Оплачен (ожидает подтверждения мерчантом)",
        2: "Подтвержден и закрыт"
    }
    
    if status in status_descriptions:
        print(f"📊 Статус платежа: {status} ({status_descriptions[status]})")
    else:
        print(f"⚠️  Неизвестный статус: {status}")
    
    # Запускаем тест
    await test_webhook(status, guid)


if __name__ == "__main__":
    asyncio.run(main())
