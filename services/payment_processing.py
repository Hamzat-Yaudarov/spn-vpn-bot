import logging
from datetime import datetime, timedelta

import aiohttp
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import database as db
from config import DEFAULT_SQUAD_UUID, TARIFFS
from services.remnawave import (
    remnawave_add_to_squad,
    remnawave_get_or_create_user,
    remnawave_get_subscription_url,
)


logger = logging.getLogger(__name__)


def _build_remnawave_username(tg_id: int, subscription_id: int) -> str:
    return f"tg_{tg_id}_{subscription_id}"


async def _get_or_create_target_subscription(tg_id: int, payment_record):
    payment_target = payment_record.get("payment_target") or "new"

    if payment_target == "renew":
        subscription_id = payment_record.get("subscription_id")
        if not subscription_id:
            return None, "Платёж не привязан к подписке"

        subscription = await db.get_subscription_by_id(subscription_id, tg_id)
        if not subscription:
            return None, "Подписка для продления не найдена"

        return subscription, None

    target_slot_number = payment_record.get("target_slot_number")
    if target_slot_number is None:
        target_slot_number = await db.get_next_subscription_slot(tg_id)

    if target_slot_number is None:
        return None, "Достигнут лимит подписок"

    subscription = await db.get_subscription_by_slot(tg_id, target_slot_number)
    if subscription and subscription.get("remnawave_uuid"):
        return None, f"Слот #{target_slot_number} уже занят"

    if subscription is None:
        subscription = await db.create_subscription_record(tg_id, target_slot_number)

    return subscription, None


async def process_paid_payment(
    bot,
    tg_id: int,
    invoice_id: str,
    tariff_code: str,
    *,
    acquire_lock: bool = True,
) -> bool:
    """Обработать успешную оплату и активировать нужную подписку."""
    logger.info(
        "Starting payment processing for user %s, invoice %s, tariff %s",
        tg_id,
        invoice_id,
        tariff_code,
    )

    lock_acquired = False
    if acquire_lock:
        lock_acquired = await db.acquire_user_lock(tg_id)
        if not lock_acquired:
            logger.warning(
                "Could not acquire lock for user %s - payment may be processing by another task",
                tg_id,
            )
            return False

    try:
        if tariff_code not in TARIFFS:
            logger.error("Invalid tariff code: %s", tariff_code)
            return False

        payment_record = await db.get_payment_by_invoice(invoice_id)
        if not payment_record:
            logger.error("Payment record not found for invoice %s", invoice_id)
            return False

        tariff = TARIFFS[tariff_code]
        days = tariff["days"]
        amount = tariff["price"]

        subscription, error = await _get_or_create_target_subscription(tg_id, payment_record)
        if error:
            logger.error("Payment target resolution failed for %s: %s", invoice_id, error)
            return False

        payment_target = payment_record.get("payment_target") or "new"
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            remna_username = subscription.get("remnawave_username") or _build_remnawave_username(tg_id, subscription["id"])
            extend_if_exists = payment_target == "renew" and bool(subscription.get("remnawave_uuid"))

            uuid, username = await remnawave_get_or_create_user(
                session,
                tg_id,
                days,
                extend_if_exists=extend_if_exists,
                remna_username=remna_username,
            )
            if not uuid:
                logger.error("Failed to create/get Remnawave user for %s", tg_id)
                return False

            squad_added = await remnawave_add_to_squad(session, uuid, subscription.get("squad_uuid") or DEFAULT_SQUAD_UUID)
            if not squad_added:
                logger.warning("Failed to add user %s to squad", uuid)

            sub_url = await remnawave_get_subscription_url(session, uuid)
            if not sub_url:
                logger.warning("Failed to get subscription URL for %s", uuid)

            try:
                referrer = await db.get_referrer(tg_id)
                if referrer and referrer[0]:
                    referrer_id = referrer[0]
                    is_first_purchase = await db.check_first_referral_purchase(tg_id, referrer_id)
                    percentage = 35 if is_first_purchase else 15

                    await db.add_referral_earning(
                        referrer_id,
                        tg_id,
                        tariff_code,
                        amount,
                        is_first_purchase=is_first_purchase,
                    )

                    referral_share = amount * percentage / 100
                    purchase_type = "первую покупку" if is_first_purchase else "повторную покупку"
                    logger.info(
                        "Referral earning recorded: %s earned %s₽ from %s (%s: %s₽ × %s%%)",
                        referrer_id,
                        referral_share,
                        tg_id,
                        purchase_type,
                        amount,
                        percentage,
                    )
                    await db.mark_first_payment(tg_id)
            except Exception as e:
                logger.error("Error processing referral for user %s: %s", tg_id, e)

            try:
                partner_result = await db.db_execute(
                    """
                    SELECT DISTINCT partner_id FROM partner_referrals
                    WHERE referred_user_id = $1
                    LIMIT 1
                    """,
                    (tg_id,),
                    fetch_one=True,
                )

                if partner_result:
                    partner_id = partner_result["partner_id"]
                    partnership = await db.get_partnership(partner_id)
                    if partnership:
                        await db.add_partner_earning(
                            partner_id,
                            tg_id,
                            tariff_code,
                            amount,
                            partnership["percentage"],
                        )
                        earned = amount * partnership["percentage"] / 100
                        logger.info(
                            "Partner earning recorded: %s earned %s₽ from %s (%s₽ × %s%%)",
                            partner_id,
                            earned,
                            tg_id,
                            amount,
                            partnership["percentage"],
                        )
            except Exception as e:
                logger.error(
                    "Error processing partner earnings for user %s: %s",
                    tg_id,
                    e,
                    exc_info=True,
                )

            existing_subscription = subscription.get("subscription_until")
            now = datetime.utcnow()

            if existing_subscription and existing_subscription > now:
                new_until = existing_subscription + timedelta(days=days)
                logger.info(
                    "Subscription %s for user %s extends from %s by %s days to %s",
                    subscription["id"],
                    tg_id,
                    existing_subscription,
                    days,
                    new_until,
                )
            else:
                new_until = now + timedelta(days=days)
                logger.info(
                    "Subscription %s for user %s starts with %s days until %s",
                    subscription["id"],
                    tg_id,
                    days,
                    new_until,
                )

            await db.update_subscription_record(
                subscription["id"],
                uuid,
                username,
                new_until,
                subscription.get("squad_uuid") or DEFAULT_SQUAD_UUID,
            )
            await db.update_payment_status_by_invoice(invoice_id, "paid")

            action_text = "активирована" if payment_target == "new" else "продлена"
            text = (
                f"✅ <b>Подписка #{subscription['slot_number']} {action_text}!</b>\n\n"
                f"Тариф: {tariff_code} ({days} дней)\n"
                f"<b>Ваш ключ:</b>\n{sub_url or 'Ошибка получения ссылки'}"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔐 Мои подписки", callback_data="buy_subscription")],
                [InlineKeyboardButton(text="📲 Инструкция", callback_data=f"subscription_instruction_{subscription['id']}")],
                [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_menu")],
            ])
            await bot.send_message(tg_id, text, reply_markup=kb)

            logger.info("Payment processing completed successfully for user %s", tg_id)
            return True

    except Exception as e:
        logger.error("Process paid payment exception: %s", e, exc_info=True)
        return False
    finally:
        if lock_acquired:
            await db.release_user_lock(tg_id)
