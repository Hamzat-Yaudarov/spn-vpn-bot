import asyncio
import logging

import aiohttp

import database as db
from services.device_addons import effective_device_limit
from services.remnawave import remnawave_update_user_profile


logger = logging.getLogger(__name__)
DEVICE_ADDON_EXPIRY_CHECK_INTERVAL = 900


async def process_expired_device_addons_once() -> None:
    subscriptions = await db.get_subscriptions_with_expired_device_addons()
    if not subscriptions:
        return

    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for subscription in subscriptions:
            subscription_id = subscription["id"]
            active_addons = await db.get_active_device_addon_count(subscription_id)
            new_limit = effective_device_limit(subscription.get("plan_kind"), active_addons)
            try:
                updated = await remnawave_update_user_profile(
                    session,
                    subscription["remnawave_uuid"],
                    hwid_device_limit=new_limit,
                    telegram_id=subscription["tg_id"] if subscription["tg_id"] > 0 else None,
                )
                if not updated:
                    logger.warning("Could not sync expired device add-ons for subscription %s", subscription_id)
                    continue
                await db.set_subscription_device_limit(subscription_id, new_limit)
                await db.mark_expired_device_addons_processed(subscription_id)
                logger.info("Expired device add-ons processed for subscription %s, limit=%s", subscription_id, new_limit)
            except Exception as exc:
                logger.error("Device add-on expiry processing failed for subscription %s: %s", subscription_id, exc, exc_info=True)


async def run_device_addon_expiry_loop() -> None:
    while True:
        try:
            await process_expired_device_addons_once()
        except Exception as exc:
            logger.error("Device add-on expiry loop error: %s", exc, exc_info=True)
        await asyncio.sleep(DEVICE_ADDON_EXPIRY_CHECK_INTERVAL)
