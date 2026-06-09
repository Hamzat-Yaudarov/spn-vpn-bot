import logging
from datetime import datetime, timedelta

import aiohttp
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import database as db
from config import (
    BYPASS_BASE_TRAFFIC_GB,
    BYPASS_HWID_DEVICE_LIMIT,
    BYPASS_TRAFFIC_PACKAGES,
    BYPASS_SQUAD_UUID,
    GB_BYTES,
    REGULAR_HWID_DEVICE_LIMIT,
    REGULAR_SQUAD_UUID,
    TARIFFS,
)
from services.remnawave import (
    remnawave_get_or_create_user,
    remnawave_get_subscription_url,
    remnawave_update_user_profile,
)


logger = logging.getLogger(__name__)


def _build_remnawave_username(tg_id: int, subscription_id: int) -> str:
    return f"tg_{tg_id}_{subscription_id}"


def _build_v2_remnawave_username(tg_id: int, plan_kind: str, type_index: int) -> str:
    return f"tg_{tg_id}_{plan_kind}_{type_index}"


def _subscription_display_name(subscription) -> str:
    plan_kind = subscription.get("plan_kind") or "regular"
    type_index = subscription.get("type_index") or subscription.get("slot_number")
    title = "Обычная" if plan_kind == "regular" else "С антиглушилкой"
    return f"{title} #{type_index}"


async def _get_or_create_target_subscription(tg_id: int, payment_record, tariff: dict):
    payment_target = payment_record.get("payment_target") or "new"
    plan_kind = tariff.get("kind", "regular")

    if payment_target == "renew":
        subscription_id = payment_record.get("subscription_id")
        if not subscription_id:
            return None, "Платёж не привязан к подписке"

        subscription = await db.get_subscription_by_id(subscription_id, tg_id)
        if not subscription:
            return None, "Подписка для продления не найдена"

        return subscription, None

    type_index = payment_record.get("target_slot_number")
    if type_index is None:
        type_index = await db.get_next_type_index(tg_id, plan_kind)

    if type_index is None:
        return None, f"Достигнут лимит подписок типа {plan_kind}"

    storage_slot = await db.get_next_subscription_slot(tg_id)
    if storage_slot is None:
        return None, "Нет свободного внутреннего слота подписки"

    subscription = await db.create_subscription_record(
        tg_id,
        storage_slot,
        plan_kind=plan_kind,
        type_index=type_index,
        generation="v2",
        is_visible=True,
        is_renewable=True,
        purchase_days=tariff["days"],
    )

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
        payment_record = await db.get_payment_by_invoice(invoice_id)
        if not payment_record:
            logger.error("Payment record not found for invoice %s", invoice_id)
            return False

        if payment_record.get("status") == "paid":
            logger.info("Payment %s is already marked paid, skipping activation", invoice_id)
            return True

        if payment_record.get("payment_kind") == "traffic_package":
            return await _process_paid_traffic_package(bot, tg_id, invoice_id, payment_record)

        if tariff_code not in TARIFFS:
            logger.error("Invalid tariff code: %s", tariff_code)
            return False

        tariff = TARIFFS[tariff_code]
        days = tariff["days"]
        amount = tariff["price"]

        subscription, error = await _get_or_create_target_subscription(tg_id, payment_record, tariff)
        if error:
            logger.error("Payment target resolution failed for %s: %s", invoice_id, error)
            return False

        payment_target = payment_record.get("payment_target") or "new"
        plan_kind = subscription.get("plan_kind") or tariff.get("kind", "regular")
        squad_uuid = REGULAR_SQUAD_UUID if plan_kind == "regular" else BYPASS_SQUAD_UUID
        device_limit = REGULAR_HWID_DEVICE_LIMIT if plan_kind == "regular" else BYPASS_HWID_DEVICE_LIMIT
        base_traffic_bytes = BYPASS_BASE_TRAFFIC_GB * GB_BYTES if plan_kind == "bypass" else 0
        traffic_limit_bytes = subscription.get("current_period_limit_bytes") or base_traffic_bytes if plan_kind == "bypass" else 0
        traffic_limit_strategy = "NO_RESET"
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            remna_username = subscription.get("remnawave_username") or _build_v2_remnawave_username(
                tg_id,
                plan_kind,
                subscription.get("type_index") or subscription["id"],
            )
            extend_if_exists = payment_target == "renew" and bool(subscription.get("remnawave_uuid"))

            uuid, username = await remnawave_get_or_create_user(
                session,
                tg_id,
                days,
                extend_if_exists=extend_if_exists,
                remna_username=remna_username,
                traffic_limit_bytes=traffic_limit_bytes if plan_kind == "bypass" else 0,
                traffic_limit_strategy=traffic_limit_strategy,
                active_internal_squads=[squad_uuid],
                hwid_device_limit=device_limit,
                telegram_id=tg_id,
            )
            if not uuid:
                logger.error("Failed to create/get Remnawave user for %s", tg_id)
                return False

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
                squad_uuid,
            )
            await db.db_execute(
                """
                UPDATE subscriptions
                SET plan_kind = $1,
                    generation = 'v2',
                    is_visible = TRUE,
                    is_renewable = TRUE,
                    traffic_enabled = $2,
                    base_traffic_bytes = $3,
                    current_period_limit_bytes = $4,
                    traffic_reset_at = COALESCE(traffic_reset_at, $5),
                    hwid_device_limit = $6,
                    purchase_days = $7
                WHERE id = $8
                """,
                (
                    plan_kind,
                    plan_kind == "bypass",
                    base_traffic_bytes,
                    traffic_limit_bytes,
                    now + timedelta(days=30) if plan_kind == "bypass" else None,
                    device_limit,
                    days,
                    subscription["id"],
                )
            )
            await db.update_payment_status_by_invoice(invoice_id, "paid")

            action_text = "активирована" if payment_target == "new" else "продлена"
            text = (
                f"✅ <b>{_subscription_display_name(subscription)} {action_text}!</b>\n\n"
                f"Тариф: {tariff.get('title', tariff_code)} ({days} дней)\n"
                f"<b>Ваш ключ:</b>\n{sub_url or 'Ошибка получения ссылки'}"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔐 Мои подписки", callback_data="my_subscriptions", style="primary")],
                [InlineKeyboardButton(text="📲 Инструкция", callback_data=f"subscription_instruction_{subscription['id']}", style="primary")],
                [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_menu", style="danger")],
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


async def _process_paid_traffic_package(bot, tg_id: int, invoice_id: str, payment_record) -> bool:
    package_code = payment_record.get("traffic_package_code") or payment_record.get("tariff_code")
    package = BYPASS_TRAFFIC_PACKAGES.get(package_code)
    if not package:
        logger.error("Invalid traffic package code: %s", package_code)
        return False

    subscription_id = payment_record.get("subscription_id")
    subscription = await db.get_subscription_by_id(subscription_id, tg_id) if subscription_id else None
    if not subscription or subscription.get("plan_kind") != "bypass":
        logger.error("Traffic package target subscription is invalid: %s", subscription_id)
        return False

    if not subscription.get("remnawave_uuid"):
        logger.error("Traffic package target subscription has no Remnawave UUID: %s", subscription_id)
        return False

    traffic_bytes = package["gb"] * GB_BYTES
    new_limit = (subscription.get("current_period_limit_bytes") or subscription.get("base_traffic_bytes") or 0) + traffic_bytes

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        updated = await remnawave_update_user_profile(
            session,
            subscription["remnawave_uuid"],
            traffic_limit_bytes=new_limit,
            traffic_limit_strategy="NO_RESET",
            active_internal_squads=[BYPASS_SQUAD_UUID],
            hwid_device_limit=BYPASS_HWID_DEVICE_LIMIT,
            telegram_id=tg_id,
        )
        if not updated:
            logger.error("Failed to update traffic limit for subscription %s", subscription_id)
            return False

    await db.add_traffic_to_subscription(subscription_id, traffic_bytes)
    await db.activate_traffic_purchase(invoice_id)
    await db.update_payment_status_by_invoice(invoice_id, "paid")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Открыть подписку", callback_data=f"subscription_view_{subscription_id}", style="primary")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_menu", style="danger")],
    ])
    await bot.send_message(
        tg_id,
        f"✅ <b>Пакет {package['gb']} ГБ активирован!</b>\n\n"
        f"Подписка: <b>{_subscription_display_name(subscription)}</b>\n"
        f"Новый лимит периода: <b>{new_limit / GB_BYTES:.1f} ГБ</b>",
        reply_markup=kb,
    )
    return True
