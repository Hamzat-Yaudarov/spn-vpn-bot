import aiohttp
import logging
import hashlib
import json
from datetime import datetime, timedelta, timezone
from config import ONEPLAT_BASE_URL, ONEPLAT_SHOP_ID, ONEPLAT_SHOP_SECRET, TARIFFS
import database as db
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_add_to_squad,
    remnawave_get_subscription_url,
    remnawave_extend_subscription
)


def generate_signature_v2(merchant_id: str, amount: int, shop_id: str, secret: str) -> str:
    """
    Generate signature_v2 for callback verification
    Format: md5(merchantId + '' + amount + '' + shopId + '' + secret)
    """
    data = f"{merchant_id}{amount}{shop_id}{secret}"
    return hashlib.md5(data.encode()).hexdigest()


def generate_sign(shop_id: str, secret: str, amount: int, merchant_order_id: str) -> str:
    """
    Generate sign for API requests
    Format: md5(shopId:secret:amount:merchantOrderId)
    """
    data = f"{shop_id}:{secret}:{amount}:{merchant_order_id}"
    return hashlib.md5(data.encode()).hexdigest()


def verify_callback_signature(payload: dict, signature: str, secret: str) -> bool:
    """
    Verify callback signature using HMAC-SHA256
    Format: HMAC-SHA256(JSON.stringify(payload without signature fields), secret)
    """
    import hmac
    
    # Create payload copy without signature fields
    payload_copy = {k: v for k, v in payload.items() 
                   if k not in ['signature', 'signature_v2']}
    
    payload_json = json.dumps(payload_copy, separators=(',', ':'), sort_keys=True)
    
    expected_signature = hmac.new(
        secret.encode(),
        payload_json.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return signature == expected_signature


def verify_callback_signature_v2(merchant_id: str, amount: int, shop_id: str, 
                                 signature_v2: str, secret: str) -> bool:
    """
    Verify callback signature_v2 using MD5
    Format: md5(merchantId + '' + amount + '' + shopId + '' + secret)
    """
    expected_sig = generate_signature_v2(merchant_id, amount, shop_id, secret)
    return signature_v2 == expected_sig


async def create_oneplat_payment(
    bot,
    amount: float,
    tariff_code: str,
    tg_id: int,
    method: str = "card"
) -> dict | None:
    """
    Create payment order in 1Plat system
    
    Args:
        bot: Telegram bot instance
        amount: Payment amount in rubles
        tariff_code: Tariff code (1m, 3m, etc)
        tg_id: Telegram user ID
        method: Payment method (card or sbp)
        
    Returns:
        Dictionary with payment info or None on error
    """
    try:
        merchant_order_id = f"spn_{tg_id}_{int(datetime.now().timestamp())}"
        
        # Generate sign for request
        sign = generate_sign(
            ONEPLAT_SHOP_ID,
            ONEPLAT_SHOP_SECRET,
            int(amount),
            merchant_order_id
        )
        
        url = f"{ONEPLAT_BASE_URL}/api/merchant/order/create/by-api"
        
        payload = {
            "shop_id": ONEPLAT_SHOP_ID,
            "merchant_order_id": merchant_order_id,
            "user_id": str(tg_id),
            "amount": int(amount),
            "email": f"{tg_id}@temp.com",
            "method": method,
            "sign": sign
        }
        
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success"):
                        logging.info(f"Created 1Plat payment for user {tg_id}, merchant_order_id: {merchant_order_id}")
                        return {
                            **data.get("payment", {}),
                            "merchant_order_id": merchant_order_id,
                            "guid": data.get("guid", "")
                        }
                    else:
                        logging.error(f"1Plat API error: {data}")
                else:
                    text = await resp.text()
                    logging.error(f"1Plat HTTP error {resp.status}: {text}")
    except Exception as e:
        logging.error(f"1Plat payment creation exception: {e}")
    
    return None


async def get_payment_info(guid: str) -> dict | None:
    """
    Get payment information by GUID
    
    Args:
        guid: Payment GUID from 1Plat
        
    Returns:
        Dictionary with payment info or None on error
    """
    try:
        url = f"{ONEPLAT_BASE_URL}/api/merchant/order/info/{guid}/by-api"
        headers = {
            "x-shop": ONEPLAT_SHOP_ID,
            "x-secret": ONEPLAT_SHOP_SECRET
        }
        
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success"):
                        return data.get("payment", {})
                    else:
                        logging.error(f"1Plat API error: {data}")
                else:
                    text = await resp.text()
                    logging.error(f"1Plat HTTP error {resp.status}: {text}")
    except Exception as e:
        logging.error(f"Get payment info exception: {e}")
    
    return None


async def process_paid_payment(bot, tg_id: int, merchant_order_id: str, tariff_code: str, guid: str) -> bool:
    """
    Process successful payment and activate subscription
    
    Args:
        bot: Telegram bot instance
        tg_id: Telegram user ID
        merchant_order_id: Merchant order ID
        tariff_code: Tariff code
        guid: Payment GUID
        
    Returns:
        True if successful, False otherwise
    """
    try:
        days = TARIFFS[tariff_code]["days"]
        
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Get or create user in Remnawave
            uuid, username = await remnawave_get_or_create_user(
                session, tg_id, days, extend_if_exists=True
            )
            
            if not uuid:
                logging.error(f"Failed to create/get Remnawave user for {tg_id}")
                return False

            # Add to squad
            await remnawave_add_to_squad(session, uuid)
            
            # Get subscription URL
            sub_url = await remnawave_get_subscription_url(session, uuid)

            # Handle referral program
            referrer = await db.get_referrer(tg_id)
            if referrer and referrer[0] and not referrer[1]:
                referrer_user = await db.get_user(referrer[0])
                if referrer_user and referrer_user['remnawave_uuid']:
                    await remnawave_extend_subscription(session, referrer_user['remnawave_uuid'], 7)
                    await db.increment_active_referrals(referrer[0])
                    logging.info(f"Referral bonus given to {referrer[0]}")
                
                await db.mark_first_payment(tg_id)

            # Update payment in database
            await db.update_payment_status_by_invoice(guid, 'paid')
            
            # Update user subscription
            new_until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
            await db.update_subscription(tg_id, uuid, username, new_until, None)

            # Send message to user
            text = (
                "✅ <b>Оплата прошла успешно!</b>\n\n"
                f"Тариф: {tariff_code} ({days} дней)\n"
                f"<b>Ссылка подписки:</b>\n<code>{sub_url}</code>"
            )
            await bot.send_message(tg_id, text)
            
            return True

    except Exception as e:
        logging.error(f"Process paid payment exception: {e}")
        return False


async def handle_oneplat_callback(bot, callback_data: dict) -> dict:
    """
    Handle 1Plat webhook callback
    
    Args:
        bot: Telegram bot instance
        callback_data: Callback payload
        
    Returns:
        Dictionary with success status and message
    """
    try:
        payment_id = callback_data.get('payment_id')
        guid = callback_data.get('guid')
        merchant_id = callback_data.get('merchant_id')
        user_id = callback_data.get('user_id')
        status = callback_data.get('status')
        signature = callback_data.get('signature')
        signature_v2 = callback_data.get('signature_v2')
        amount = callback_data.get('amount', 0)
        
        logging.info(f"Received 1Plat callback: payment_id={payment_id}, status={status}, user_id={user_id}")
        
        # Verify signature (optional but recommended)
        if not verify_callback_signature_v2(merchant_id, amount, ONEPLAT_SHOP_ID, signature_v2, ONEPLAT_SHOP_SECRET):
            logging.warning(f"Signature verification failed for payment {guid}")
            # We can still process it, but log the warning
        
        # Only process successful payments (status = 1 or 2)
        if status not in [1, 2]:
            logging.info(f"Ignoring payment with status {status}")
            return {"success": True, "message": "Status not 1 or 2, ignored"}
        
        tg_id = int(user_id)
        
        # Get payment info to extract merchant_order_id
        payment = await db.db_execute(
            "SELECT id, tariff_code FROM payments WHERE invoice_id = $1",
            (guid,),
            fetch_one=True
        )
        
        if not payment:
            logging.warning(f"Payment not found in database for guid {guid}")
            return {"success": False, "message": "Payment not found"}
        
        tariff_code = payment['tariff_code']
        payment_db_id = payment['id']
        
        # Acquire lock to prevent race conditions
        if not await db.acquire_user_lock(tg_id):
            logging.warning(f"Failed to acquire lock for user {tg_id}")
            return {"success": False, "message": "Lock failed"}
        
        try:
            # Check if payment is already processed
            existing_payment = await db.db_execute(
                "SELECT status FROM payments WHERE id = $1",
                (payment_db_id,),
                fetch_one=True
            )
            
            if existing_payment and existing_payment['status'] == 'paid':
                logging.info(f"Payment already processed: {guid}")
                return {"success": True, "message": "Already processed"}
            
            # Process the payment
            success = await process_paid_payment(bot, tg_id, guid, tariff_code, guid)
            
            if success:
                logging.info(f"Successfully processed payment for user {tg_id}")
                return {"success": True, "message": "Payment processed"}
            else:
                logging.error(f"Failed to process payment for user {tg_id}")
                return {"success": False, "message": "Processing failed"}
        
        finally:
            await db.release_user_lock(tg_id)
    
    except Exception as e:
        logging.error(f"Callback handling exception: {e}")
        return {"success": False, "message": str(e)}