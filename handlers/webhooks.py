import logging
import json
from quart import Blueprint, request, jsonify, current_app
from services.oneplat import verify_callback, get_payment_status_description
import database as db
from services.cryptobot import process_paid_invoice


webhook_bp = Blueprint('webhooks', __name__)


def set_bot(bot):
    """Установить экземпляр бота для webhook'а"""
    webhook_bp.bot = bot


@webhook_bp.route('/1plat-webhook', methods=['POST'])
async def handle_oneplat_webhook():
    """
    Обработчик webhook'а от 1Plat
    
    1Plat отправляет callback'и при изменении статуса платежа
    Подписка активируется ТОЛЬКО после успешной оплаты (статус 1 или 2)
    """
    try:
        # Получаем тело запроса
        data = await request.get_json()
        
        if not data:
            logging.warning("Empty webhook data from 1Plat")
            return jsonify({"success": False, "error": "Empty data"}), 400
        
        logging.info(f"Received 1Plat webhook: {json.dumps(data, indent=2)}")
        
        # Проверяем подпись callback'а
        if not verify_callback(data):
            logging.warning(f"Invalid callback signature from 1Plat: {data.get('payment_id')}")
            return jsonify({"success": False, "error": "Invalid signature"}), 403
        
        # Извлекаем данные платежа
        payment_guid = data.get("guid")
        payment_id = data.get("payment_id")
        merchant_id = data.get("merchant_id")
        user_id = data.get("user_id")
        status = data.get("status")
        amount = data.get("amount")
        
        if not payment_guid or status is None:
            logging.warning(f"Missing required fields in 1Plat webhook")
            return jsonify({"success": False, "error": "Missing fields"}), 400
        
        logging.info(
            f"Processing 1Plat payment: guid={payment_guid}, user_id={user_id}, "
            f"status={status} ({get_payment_status_description(status)}), amount={amount}"
        )
        
        # Получаем платеж из БД
        payment = await db.get_payment_by_guid(payment_guid)
        
        if not payment:
            logging.warning(f"Payment not found in DB: {payment_guid}")
            return jsonify({"success": False, "error": "Payment not found"}), 404
        
        tg_id = payment['tg_id']
        tariff_code = payment['tariff_code']
        
        # Статус -2: ошибка при выписании счета
        if status == -2:
            logging.info(f"Payment error for user {tg_id}: {payment_guid}")
            await db.update_payment_status_by_guid(payment_guid, "failed")
            return jsonify({"success": True, "message": "Payment failed recorded"}), 200
        
        # Статус -1: черновик, ещё не выбран метод оплаты - ничего не делаем
        if status == -1:
            logging.info(f"Payment draft for user {tg_id}: {payment_guid}")
            return jsonify({"success": True, "message": "Payment still draft"}), 200
        
        # Статус 0: ожидает оплаты - ничего не делаем, ждём оплаты
        if status == 0:
            logging.info(f"Payment pending for user {tg_id}: {payment_guid}")
            return jsonify({"success": True, "message": "Payment pending"}), 200
        
        # Статус 1: оплачен, но ещё ожидает подтверждения мерчантом
        # Статус 2: подтвержден и закрыт - это то, что нам нужно!
        if status in (1, 2):
            if not await db.acquire_user_lock(tg_id):
                logging.warning(f"Could not acquire lock for user {tg_id}")
                return jsonify({"success": True, "message": "Processing..."}), 200
            
            try:
                # Проверяем, не была ли подписка уже активирована
                current_status = await db.db_execute(
                    "SELECT status FROM payments WHERE payment_guid = $1",
                    (payment_guid,),
                    fetch_one=True
                )
                
                if current_status and current_status['status'] == 'paid':
                    logging.info(f"Payment already processed: {payment_guid}")
                    return jsonify({"success": True, "message": "Already processed"}), 200
                
                # Активируем подписку
                bot = webhook_bp.bot
                success = await process_paid_invoice(bot, tg_id, payment_guid, tariff_code)
                
                if success:
                    await db.update_payment_status_by_guid(payment_guid, "paid")
                    logging.info(f"Payment processed successfully for user {tg_id}: {payment_guid}")
                    return jsonify({"success": True, "message": "Payment processed"}), 200
                else:
                    logging.error(f"Failed to process payment for user {tg_id}: {payment_guid}")
                    return jsonify({"success": True, "message": "Processing error, will retry"}), 200
            
            finally:
                await db.release_user_lock(tg_id)
        
        # Неизвестный статус
        logging.warning(f"Unknown payment status: {status}")
        return jsonify({"success": True, "message": "Unknown status"}), 200
    
    except Exception as e:
        logging.exception(f"Webhook processing error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
