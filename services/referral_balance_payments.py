"""Fair, bounded retries of durable referral-wallet holds via common activation.

No transfer, commission, provisioning or notification logic is duplicated here.
Normal reconciliation verifies the trusted hold and owns idempotent activation.
"""
import asyncio
import logging
import math

from services import referral_accounting as accounting
from services.payment_reconciliation import reconcile_payment

logger = logging.getLogger(__name__)
DEFAULT_INTERVAL = 60
MAX_BATCH_SIZE = 30


async def recover_once(bot, *, after_id=0, limit=MAX_BATCH_SIZE):
    """One keyset page, wrapping at the end; return the next in-process cursor.

    Failed old holds cannot permanently occupy the first page. A restart resets
    this scan cursor, not the durable holds or activation/idempotency state.
    """
    cursor = max(int(after_id), 0)
    limit = min(max(int(limit), 1), MAX_BATCH_SIZE)
    rows = await accounting.pending_balance_purchases(limit, after_id=cursor)
    if not rows and cursor:
        rows = await accounting.pending_balance_purchases(limit, after_id=0)
    if not rows:
        return 0
    for row in rows:
        try:
            await reconcile_payment(bot, row['invoice_id'], expected_tg_id=row['tg_id'])
        except Exception as exc:
            # Never emit invoice, user, URL, token or provider exception text.
            logger.warning('Referral wallet recovery deferred: %s', type(exc).__name__)
        cursor = int(row['id'])
    return cursor


async def run_recovery_loop(bot, *, interval=DEFAULT_INTERVAL, batch_size=MAX_BATCH_SIZE):
    """Background scan; cancellations always propagate and failures back off."""
    interval = float(interval)
    if not math.isfinite(interval) or not 30 <= interval <= 60:
        raise ValueError('Интервал восстановления должен быть от 30 до 60 секунд.')
    cursor = 0
    while True:
        try:
            cursor = await recover_once(bot, after_id=cursor, limit=batch_size)
        except Exception as exc:
            logger.warning('Referral wallet scan deferred: %s', type(exc).__name__)
        await asyncio.sleep(interval)
