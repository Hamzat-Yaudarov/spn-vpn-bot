import logging
from datetime import datetime, timedelta

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


logger = logging.getLogger(__name__)


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
    payment_target = payment_record.get("payment_target") or "new"
    plan_kind = tariff.get("kind", "regular")

    if payment_target == "renew":
        subscription_id = payment_record.get("subscription_id")
        if not subscription_id:
            return None, "Платёж не привязан к подписке"

        subscription = await db.get_subscription_by_id(subscription_id, tg_id)
        if (
            not subscription
            or subscription.get("generation") != "v2"
            or not subscription.get("is_visible")
            or not subscription.get("is_renewable")
        ):
            return None, "Эту подписку нельзя продлить"

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
        traffic_state = build_traffic_period_state(subscription, plan_kind, now)
        active_device_addons = await db.get_active_device_addon_count(subscription["id"])
        device_limit = effective_device_limit(plan_kind, active_device_addons)
        traffic_limit_bytes = traffic_state.limit_bytes
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
                telegram_id=tg_id if tg_id > 0 else None,
            )
            if not uuid:
                logger.error("Failed to create/get Remnawave user for %s", tg_id)
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

            if not await remnawave_set_subscription_expiry(session, uuid, new_until):
                logger.warning("Failed to sync Remnawave expiry for subscription %s", subscription["id"])

            await db.update_subscription_record(
                subscription["id"],
                uuid,
                username,
                new_until,
                squad_uuid,
            )
            await db.link_payment_to_subscription(invoice_id, subscription["id"])
            await db.db_execute(
                """
                UPDATE subscriptions
                SET plan_kind = $1,
                    generation = 'v2',
                    is_visible = TRUE,
                    is_renewable = TRUE,
                    traffic_enabled = $2,
                    base_traffic_bytes = $3,
                    carried_traffic_bytes = $4,
                    current_paid_traffic_bytes = $5,
                    current_period_limit_bytes = $6,
                    traffic_reset_at = $7,
                    hwid_device_limit = $8,
                    last_known_used_traffic_bytes = $9,
                    last_traffic_sync_at = now(),
                    purchase_days = $10
                WHERE id = $11
                """,
                (
                    plan_kind,
                    traffic_state.enabled,
                    traffic_state.base_bytes,
                    traffic_state.carried_bytes,
                    traffic_state.paid_bytes,
                    traffic_limit_bytes,
                    traffic_state.reset_at,
                    device_limit,
                    traffic_state.last_known_used_bytes,
                    days,
                    subscription["id"],
                )
            )
            await db.update_payment_status_by_invoice(invoice_id, "paid")

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
                [InlineKeyboardButton(text="🔐 Мои подписки", callback_data="my_subscriptions", style="primary")],
                [InlineKeyboardButton(text="🔗 Открыть эту подписку", callback_data=f"subscription_view_{subscription['id']}", style="primary")],
                [InlineKeyboardButton(text="📲 Инструкция", callback_data=f"subscription_instruction_{subscription['id']}", style="primary")],
                [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_menu", style="danger")],
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

    connector = aiohttp.TCPConnector(ssl=False)
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
        [InlineKeyboardButton(text="🔐 Открыть подписку", callback_data=f"subscription_view_{subscription_id}", style="primary")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_menu", style="danger")],
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

    connector = aiohttp.TCPConnector(ssl=False)
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
        [InlineKeyboardButton(text="🔐 Открыть подписку", callback_data=f"subscription_view_{subscription_id}", style="primary")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_menu", style="danger")],
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
