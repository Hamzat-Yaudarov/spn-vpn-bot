import asyncio
import logging
from datetime import timedelta

import aiohttp

import database as db
from config import BYPASS_BASE_TRAFFIC_GB, BYPASS_HWID_DEVICE_LIMIT, BYPASS_SQUAD_UUID, GB_BYTES
from services.remnawave import (
    remnawave_get_user_usage,
    remnawave_reset_user_traffic,
    remnawave_update_user_profile,
)


logger = logging.getLogger(__name__)

TRAFFIC_RESET_CHECK_INTERVAL = 900


async def process_due_traffic_resets():
    subscriptions = await db.get_bypass_subscriptions_for_traffic_reset()
    if not subscriptions:
        return

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for subscription in subscriptions:
            try:
                base_bytes = subscription.get('base_traffic_bytes') or BYPASS_BASE_TRAFFIC_GB * GB_BYTES
                carried_bytes = subscription.get('carried_traffic_bytes') or 0
                paid_bytes = subscription.get('current_paid_traffic_bytes') or 0

                usage = await remnawave_get_user_usage(session, subscription['remnawave_uuid'])
                used_bytes = (usage or {}).get('usedTrafficBytes') or 0

                paid_consumed = max(0, used_bytes - base_bytes)
                remaining_paid = max(0, carried_bytes + paid_bytes - paid_consumed)
                new_limit = base_bytes + remaining_paid
                next_reset_at = subscription['traffic_reset_at'] + timedelta(days=30)
                period_start = subscription['traffic_reset_at'] - timedelta(days=30)

                reset_ok = await remnawave_reset_user_traffic(session, subscription['remnawave_uuid'])
                if not reset_ok:
                    logger.warning("Traffic reset failed for subscription %s", subscription['id'])
                    continue

                updated = await remnawave_update_user_profile(
                    session,
                    subscription['remnawave_uuid'],
                    traffic_limit_bytes=new_limit,
                    traffic_limit_strategy="NO_RESET",
                    active_internal_squads=[BYPASS_SQUAD_UUID],
                    hwid_device_limit=BYPASS_HWID_DEVICE_LIMIT,
                    telegram_id=subscription['tg_id'],
                )
                if not updated:
                    logger.warning("Traffic limit update failed for subscription %s", subscription['id'])
                    continue

                await db.record_traffic_cycle(
                    subscription['id'],
                    period_start,
                    subscription['traffic_reset_at'],
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
            except Exception as e:
                logger.error("Traffic reset error for subscription %s: %s", subscription.get('id'), e, exc_info=True)


async def run_traffic_reset_loop():
    while True:
        try:
            await process_due_traffic_resets()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Traffic reset loop error: %s", e, exc_info=True)

        await asyncio.sleep(TRAFFIC_RESET_CHECK_INTERVAL)
