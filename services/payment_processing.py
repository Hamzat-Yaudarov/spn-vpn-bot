import logging
from datetime import datetime

import aiohttp
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import database as db
from config import (
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
    remnawave_reset_user_traffic,
    remnawave_set_subscription_expiry,
    remnawave_update_user_profile,
)
from services.device_addons import device_count_text, effective_device_limit
from services.traffic_periods import build_traffic_period_state
from services.reactivation_campaigns import cancel_reactivation_offers_after_purchase
from services.custom_emoji import semantic_button
from services import payment_activation as activation


logger = logging.getLogger(__name__)


async def _cancel_reactivation_safely(bot, tg_id: int) -> None:
    try:
        await cancel_reactivation_offers_after_purchase(bot, tg_id)
    except Exception as exc:
        logger.error("Could not cancel reactivation offer after payment by %s: %s", tg_id, exc, exc_info=True)


def _build_remnawave_username(tg_id: int, subscription_id: int) -> str:
    return f"tg_{tg_id}_{subscription_id}"


def _build_v2_remnawave_username(tg_id: int, plan_kind: str, type_index: int) -> str:
    prefix = f"tg_{tg_id}" if tg_id > 0 else f"web_{abs(tg_id)}"
    return f"{prefix}_{plan_kind}_{type_index}"


def _subscription_display_name(subscription) -> str:
    plan_kind = subscription.get("plan_kind") or "regular"
    type_index = subscription.get("type_index") or subscription.get("slot_number")
    title = "Обычная" if plan_kind == "regular" else "С антиглушилкой"
    return f"{title} #{type_index}"


async def _get_or_create_target_subscription(tg_id: int, payment_record, tariff: dict):
    try:
        return await activation.reserve(tg_id, payment_record["invoice_id"], tariff), None
    except activation.ActivationError as exc:
        return None, str(exc)


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

    db_lock = None
    try:
        db_lock = await activation.acquire_lock(tg_id)
        if db_lock is None:
            logger.info("Payment activation already running for user %s", tg_id)
            return False
        payment_record = await db.get_payment_by_invoice(invoice_id)
        if not payment_record:
            logger.error("Payment record not found for invoice %s", invoice_id)
            return False

        if payment_record["tg_id"] != tg_id or payment_record.get("refund_requested_at"):
            logger.error("Payment owner mismatch or refund requested for invoice %s", invoice_id)
            return False
        tariff_code = payment_record["tariff_code"]

        if payment_record.get("status") == "paid":
            logger.info("Payment %s is already marked paid, skipping activation", invoice_id)
            await _cancel_reactivation_safely(bot, tg_id)
            return True

        if payment_record.get("status") != "pending":
            logger.error("Payment %s is not pending", invoice_id)
            return False

        if payment_record.get("payment_kind") == "traffic_package":
            return await _process_paid_traffic_package(bot, tg_id, invoice_id, payment_record)

        if payment_record.get("payment_kind") == "device_addon":
            return await _process_paid_device_addon(bot, tg_id, invoice_id, payment_record)

        if tariff_code not in TARIFFS:
            logger.error("Invalid tariff code: %s", tariff_code)
            return False

        tariff = TARIFFS[tariff_code]
        days = tariff["days"]
        amount = float(payment_record.get("amount") or tariff["price"])

        subscription, error = await _get_or_create_target_subscription(tg_id, payment_record, tariff)
        if error:
            logger.error("Payment target resolution failed for %s: %s", invoice_id, error)
            return False

        payment_target = payment_record.get("payment_target") or "new"
        plan_kind = subscription.get("plan_kind") or tariff.get("kind", "regular")
        squad_uuid = REGULAR_SQUAD_UUID if plan_kind == "regular" else BYPASS_SQUAD_UUID
        now = datetime.utcnow()
        new_until = subscription["payment_expires_at"]
        if new_until <= now:
            logger.error("Reserved activation deadline expired for payment %s; manual review required", invoice_id)
            return False
        traffic_state = build_traffic_period_state(subscription, plan_kind, now)
        active_device_addons = await db.get_active_device_addon_count(subscription["id"])
        device_limit = effective_device_limit(plan_kind, active_device_addons)
        traffic_limit_bytes = traffic_state.limit_bytes
        traffic_limit_strategy = "NO_RESET"
        connector = aiohttp.TCPConnector()
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            remna_username = subscription.get("remnawave_username") or _build_v2_remnawave_username(
                tg_id,
                plan_kind,
                subscription.get("type_index") or subscription["id"],
            )
            uuid, username = await remnawave_get_or_create_user(
                session,
                tg_id,
                days,
                # Repeated provider checks must never add days again remotely.
                # Apply the durable absolute expiry below, not an increment.
                extend_if_exists=False,
                remna_username=remna_username,
                traffic_limit_bytes=traffic_limit_bytes if plan_kind == "bypass" else 0,
                traffic_limit_strategy=traffic_limit_strategy,
                active_internal_squads=[squad_uuid],
                hwid_device_limit=device_limit,
                telegram_id=tg_id if tg_id > 0 else None,
            )
            if not uuid:
                logger.error("Failed to create/get Remnawave user for %s", tg_id)
                return False

            if not await remnawave_set_subscription_expiry(session, uuid, new_until):
                logger.error("Expiry sync failed for payment %s; kept pending", invoice_id)
                return False

            sub_url = await remnawave_get_subscription_url(session, uuid)
            if not sub_url:
                logger.error(
                    "Subscription URL is not ready for payment %s; payment will remain pending for retry",
                    invoice_id,
                )
                return False

            should_reset_traffic_now = (
                traffic_state.enabled
                and not traffic_state.was_active
                and bool(uuid)
                and (payment_target == "renew" or bool(subscription.get("remnawave_uuid")))
            )
            if should_reset_traffic_now:
                reset_ok = await remnawave_reset_user_traffic(session, uuid)
                if reset_ok:
                    logger.info(
                        "Traffic reset immediately after reactivating expired bypass subscription %s",
                        subscription["id"],
                    )
                else:
                    logger.warning(
                        "Immediate traffic reset failed for reactivated subscription %s; queued retry",
                        subscription["id"],
                    )
                    traffic_state.reset_at = now
                    traffic_state.last_known_used_bytes = int(subscription.get("last_known_used_traffic_bytes") or 0)

            await activation.complete(
                tg_id, invoice_id, subscription, uuid, username, squad_uuid,
                traffic_state, device_limit, days,
            )
            try:
                await db.sync_primary_subscription_to_user(tg_id)
            except Exception:
                logger.exception("Legacy subscription mirror update failed for user %s", tg_id)
            await _cancel_reactivation_safely(bot, tg_id)

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

            action_text = "активирована" if payment_target == "new" else "продлена"
            traffic_text = (
                f"\nТрафик антиглушилки: <b>{traffic_limit_bytes / GB_BYTES:.1f} ГБ</b>"
                if plan_kind == "bypass"
                else ""
            )
            text = (
                f"✅ <b>{_subscription_display_name(subscription)} {action_text}!</b>\n\n"
                f"Тариф: <b>{tariff.get('title', tariff_code)}</b>\n"
                f"Срок действия: <b>до {new_until.strftime('%d.%m.%Y')}</b>\n"
                f"Устройства: <b>до {device_count_text(device_limit)}</b>"
                f"{traffic_text}\n\n"
                "Ключ уже готов — можно подключаться.\n\n"
                f"<b>Ваш ключ:</b>\n{sub_url}"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [semantic_button(text="🔑 Открыть подписку", callback_data=f"subscription_view_{subscription['id']}", style="primary")],
                [semantic_button(text="📲 Подключить", callback_data=f"subscription_instruction_{subscription['id']}", style="success")],
                [semantic_button(text="🔑 Мои подписки", callback_data="my_subscriptions", style="primary")],
                [semantic_button(text="🏠 Главное меню", callback_data="back_to_menu", style="primary")],
            ])
            if bot is not None and tg_id > 0:
                try:
                    await bot.send_message(tg_id, text, reply_markup=kb)
                except Exception as exc:
                    logger.warning("Could not send payment notification to %s: %s", tg_id, exc)

            logger.info("Payment processing completed successfully for user %s", tg_id)
            return True

    except Exception as e:
        logger.error("Process paid payment exception: %s", e, exc_info=True)
        return False
    finally:
        try:
            if db_lock is not None:
                await activation.release_lock(db_lock, tg_id)
        except Exception:
            logger.exception("Could not release payment activation lock for user %s", tg_id)
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
    if (
        not subscription
        or subscription.get("generation") != "v2"
        or not subscription.get("is_visible")
        or not subscription.get("is_renewable")
        or subscription.get("plan_kind") != "bypass"
    ):
        logger.error("Traffic package target subscription is invalid: %s", subscription_id)
        return False

    if not subscription.get("remnawave_uuid"):
        logger.error("Traffic package target subscription has no Remnawave UUID: %s", subscription_id)
        return False

    traffic_bytes = package["gb"] * GB_BYTES
    new_limit = (subscription.get("current_period_limit_bytes") or subscription.get("base_traffic_bytes") or 0) + traffic_bytes
    active_device_addons = await db.get_active_device_addon_count(subscription_id)
    device_limit = effective_device_limit(subscription.get("plan_kind"), active_device_addons)

    connector = aiohttp.TCPConnector()
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        updated = await remnawave_update_user_profile(
            session,
            subscription["remnawave_uuid"],
            traffic_limit_bytes=new_limit,
            traffic_limit_strategy="NO_RESET",
            active_internal_squads=[BYPASS_SQUAD_UUID],
            hwid_device_limit=device_limit,
            telegram_id=tg_id if tg_id > 0 else None,
        )
        if not updated:
            logger.error("Failed to update traffic limit for subscription %s", subscription_id)
            return False

    await db.add_traffic_to_subscription(subscription_id, traffic_bytes)
    await db.activate_traffic_purchase(invoice_id)
    await db.update_payment_status_by_invoice(invoice_id, "paid")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [semantic_button(text="🔑 Открыть подписку", callback_data=f"subscription_view_{subscription_id}", style="primary")],
        [semantic_button(text="🏠 Главное меню", callback_data="back_to_menu", style="primary")],
    ])
    if bot is not None and tg_id > 0:
        try:
            await bot.send_message(
                tg_id,
                f"✅ <b>Пакет {package['gb']} ГБ активирован!</b>\n\n"
                f"Подписка: <b>{_subscription_display_name(subscription)}</b>\n"
                f"Новый лимит периода: <b>{new_limit / GB_BYTES:.1f} ГБ</b>",
                reply_markup=kb,
            )
        except Exception as exc:
            logger.warning("Could not send traffic notification to %s: %s", tg_id, exc)
    return True


async def _process_paid_device_addon(bot, tg_id: int, invoice_id: str, payment_record) -> bool:
    purchase = await db.get_device_addon_purchase_by_invoice(invoice_id)
    if not purchase:
        logger.error("Device add-on purchase not found for invoice %s", invoice_id)
        return False

    subscription_id = payment_record.get("subscription_id") or purchase.get("subscription_id")
    subscription = await db.get_subscription_by_id(subscription_id, tg_id) if subscription_id else None
    now = datetime.utcnow()
    if (
        not subscription
        or subscription.get("generation") != "v2"
        or not subscription.get("is_visible")
        or not subscription.get("is_renewable")
        or not subscription.get("subscription_until")
        or subscription["subscription_until"] <= now
    ):
        logger.error("Device add-on target subscription is invalid: %s", subscription_id)
        return False

    if not subscription.get("remnawave_uuid"):
        logger.error("Device add-on target subscription has no Remnawave UUID: %s", subscription_id)
        return False

    if purchase.get("valid_until") <= now:
        logger.error("Device add-on purchase already expired: %s", invoice_id)
        return False

    active_device_addons = await db.get_active_device_addon_count(subscription_id)
    new_limit = effective_device_limit(
        subscription.get("plan_kind"),
        active_device_addons + int(purchase.get("device_count") or 0),
    )

    connector = aiohttp.TCPConnector()
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        updated = await remnawave_update_user_profile(
            session,
            subscription["remnawave_uuid"],
            hwid_device_limit=new_limit,
            telegram_id=tg_id if tg_id > 0 else None,
        )
        if not updated:
            logger.error("Failed to update device limit for subscription %s", subscription_id)
            return False

    await db.activate_device_addon_purchase(invoice_id)
    await db.set_subscription_device_limit(subscription_id, new_limit)
    await db.update_payment_status_by_invoice(invoice_id, "paid")

    count = int(purchase.get("device_count") or 0)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [semantic_button(text="🔑 Открыть подписку", callback_data=f"subscription_view_{subscription_id}", style="primary")],
        [semantic_button(text="🏠 Главное меню", callback_data="back_to_menu", style="primary")],
    ])
    if bot is not None and tg_id > 0:
        try:
            await bot.send_message(
                tg_id,
                f"✅ <b>Дополнительные устройства подключены!</b>\n\n"
                f"Подписка: <b>{_subscription_display_name(subscription)}</b>\n"
                f"Добавлено: <b>+{device_count_text(count)}</b>\n"
                f"Новый лимит: <b>{device_count_text(new_limit)}</b>\n"
                f"Действует до: <b>{purchase['valid_until'].strftime('%d.%m.%Y')}</b>",
                reply_markup=kb,
            )
        except Exception as exc:
            logger.warning("Could not send device add-on notification to %s: %s", tg_id, exc)

    logger.info("Device add-on activated for subscription %s, new limit %s", subscription_id, new_limit)
    return True
