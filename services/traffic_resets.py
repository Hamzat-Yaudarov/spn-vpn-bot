import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp

import database as db
from config import BYPASS_BASE_TRAFFIC_GB, BYPASS_SQUAD_UUID, GB_BYTES
from services.device_addons import effective_device_limit
from services.remnawave import (
    remnawave_get_user_usage,
    remnawave_reset_user_traffic,
    remnawave_update_user_profile,
)


logger = logging.getLogger(__name__)

TRAFFIC_RESET_CHECK_INTERVAL = 900


async def _reset_bypass_subscription_traffic(
    session: aiohttp.ClientSession,
    subscription,
    *,
    period_start,
    period_end,
    next_reset_at,
) -> tuple[bool, str | None]:
    """Сбросить traffic-cycle одной bypass-подписки в Remnawave и локальной БД."""
    try:
        base_bytes = subscription.get('base_traffic_bytes') or BYPASS_BASE_TRAFFIC_GB * GB_BYTES
        carried_bytes = subscription.get('carried_traffic_bytes') or 0
        paid_bytes = subscription.get('current_paid_traffic_bytes') or 0

        usage = await remnawave_get_user_usage(session, subscription['remnawave_uuid'])
        used_bytes = (usage or {}).get('usedTrafficBytes') or 0

        paid_consumed = max(0, used_bytes - base_bytes)
        remaining_paid = max(0, carried_bytes + paid_bytes - paid_consumed)
        new_limit = base_bytes + remaining_paid
        active_device_addons = await db.get_active_device_addon_count(subscription['id'])
        device_limit = effective_device_limit(subscription.get('plan_kind'), active_device_addons)

        reset_ok = await remnawave_reset_user_traffic(session, subscription['remnawave_uuid'])
        if not reset_ok:
            return False, "Remnawave не сбросил трафик"

        updated = await remnawave_update_user_profile(
            session,
            subscription['remnawave_uuid'],
            traffic_limit_bytes=new_limit,
            traffic_limit_strategy="NO_RESET",
            active_internal_squads=[BYPASS_SQUAD_UUID],
            hwid_device_limit=device_limit,
            telegram_id=subscription['tg_id'],
        )
        if not updated:
            return False, "Remnawave не обновил лимит после сброса"

        await db.record_traffic_cycle(
            subscription['id'],
            period_start,
            period_end,
            base_bytes,
            carried_bytes,
            paid_bytes,
            used_bytes,
            remaining_paid,
        )
        await db.apply_traffic_reset(subscription['id'], remaining_paid, next_reset_at, new_limit)
        logger.info(
            "Traffic reset applied for subscription %s: used=%s, remaining_paid=%s, new_limit=%s",
            subscription['id'],
            used_bytes,
            remaining_paid,
            new_limit,
        )
        return True, None
    except Exception as e:
        logger.error("Traffic reset error for subscription %s: %s", subscription.get('id'), e, exc_info=True)
        return False, str(e)


async def process_due_traffic_resets():
    await process_pending_legacy_limit_removals()
    await process_pending_traffic_limit_sync()

    subscriptions = await db.get_bypass_subscriptions_for_traffic_reset()
    if not subscriptions:
        return

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for subscription in subscriptions:
            ok, error = await _reset_bypass_subscription_traffic(
                session,
                subscription,
                period_start=subscription['traffic_reset_at'] - timedelta(days=30),
                period_end=subscription['traffic_reset_at'],
                next_reset_at=subscription['traffic_reset_at'] + timedelta(days=30),
            )
            if not ok:
                logger.warning("Traffic reset failed for subscription %s: %s", subscription['id'], error)


async def reset_all_active_bypass_traffic() -> dict:
    """Принудительно сбросить трафик всех активных bypass-подписок."""
    subscriptions = await db.get_active_bypass_subscriptions_for_manual_traffic_reset()
    result = {
        "total": len(subscriptions or []),
        "success": 0,
        "failed": 0,
        "failed_ids": [],
    }
    if not subscriptions:
        return result

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for subscription in subscriptions:
            now = datetime.utcnow()
            period_start = (
                subscription['traffic_reset_at'] - timedelta(days=30)
                if subscription.get('traffic_reset_at')
                else now - timedelta(days=30)
            )
            ok, error = await _reset_bypass_subscription_traffic(
                session,
                subscription,
                period_start=period_start,
                period_end=now,
                next_reset_at=now + timedelta(days=30),
            )
            if ok:
                result["success"] += 1
            else:
                result["failed"] += 1
                result["failed_ids"].append(subscription["id"])
                logger.warning("Manual traffic reset failed for subscription %s: %s", subscription["id"], error)
            await asyncio.sleep(0.1)

    return result


async def process_pending_legacy_limit_removals():
    """Убрать лимит со старых подписок, не сбрасывая использованный трафик."""
    subscriptions = await db.get_legacy_subscriptions_pending_limit_removal()
    if not subscriptions:
        return

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for subscription in subscriptions:
            try:
                updated = await remnawave_update_user_profile(
                    session,
                    subscription["remnawave_uuid"],
                    traffic_limit_bytes=0,
                    traffic_limit_strategy="NO_RESET",
                )
                if not updated:
                    logger.warning(
                        "Legacy traffic limit removal failed for subscription %s; will retry",
                        subscription["id"],
                    )
                    continue

                await db.mark_legacy_subscription_limit_removed(subscription["id"])
                logger.info(
                    "Traffic limit removed from legacy subscription %s without traffic reset",
                    subscription["id"],
                )
            except Exception as e:
                logger.error(
                    "Legacy traffic limit removal failed for subscription %s; will retry: %s",
                    subscription.get("id"),
                    e,
                    exc_info=True,
                )


async def process_pending_traffic_limit_sync():
    subscriptions = await db.get_bypass_subscriptions_for_limit_sync()
    if not subscriptions:
        return

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for subscription in subscriptions:
            try:
                limit_bytes = subscription.get('current_period_limit_bytes') or subscription.get('base_traffic_bytes') or BYPASS_BASE_TRAFFIC_GB * GB_BYTES
                active_device_addons = await db.get_active_device_addon_count(subscription['id'])
                device_limit = effective_device_limit(subscription.get('plan_kind'), active_device_addons)
                updated = await remnawave_update_user_profile(
                    session,
                    subscription['remnawave_uuid'],
                    traffic_limit_bytes=limit_bytes,
                    traffic_limit_strategy="NO_RESET",
                    active_internal_squads=[BYPASS_SQUAD_UUID],
                    hwid_device_limit=device_limit,
                    telegram_id=subscription['tg_id'],
                )
                if not updated:
                    logger.warning("Traffic limit sync failed for subscription %s", subscription['id'])
                    continue

                await db.mark_traffic_limit_synced(subscription['id'])
                logger.info(
                    "Traffic limit synced for subscription %s: limit=%s",
                    subscription['id'],
                    limit_bytes,
                )
            except Exception as e:
                logger.error("Traffic limit sync error for subscription %s: %s", subscription.get('id'), e, exc_info=True)


async def run_traffic_reset_loop():
    while True:
        try:
            await process_due_traffic_resets()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Traffic reset loop error: %s", e, exc_info=True)

        await asyncio.sleep(TRAFFIC_RESET_CHECK_INTERVAL)
