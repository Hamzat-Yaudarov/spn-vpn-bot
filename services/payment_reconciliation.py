"""Provider reconciliation for every payment entry point.

The UI is never the source of truth for a successful payment.  A payment is
verified against its provider and then handed to the single idempotent
activation path.  This module is intentionally shared by Telegram, MiniApp,
the website, the Android API, webhooks and background polling.
"""
from dataclasses import dataclass
import logging

import database as db
from services.payment_processing import process_paid_payment


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationResult:
    invoice_id: str
    status: str
    activated: bool = False
    reason: str | None = None


def _amount_matches(expected, received) -> bool:
    try:
        return received is not None and abs(float(received) - float(expected)) <= 0.009
    except (TypeError, ValueError):
        return False


async def _provider_state(payment) -> tuple[str, object | None]:
    """Return normalized provider state and the provider-reported amount."""
    provider = payment.get("provider")
    invoice_id = str(payment["invoice_id"])

    if provider == "yookassa":
        # Local imports avoid a module cycle: provider modules also expose the
        # background loops that call this reconciliation service.
        from services.yookassa import get_payment_status

        remote = await get_payment_status(invoice_id)
        if not remote or str(remote.get("id") or "") != invoice_id:
            return "unavailable", None
        status = remote.get("status")
        if status == "succeeded":
            return "paid", (remote.get("amount") or {}).get("value")
        if status == "canceled":
            return "canceled", None
        return "pending", None

    if provider == "cryptobot":
        from services.cryptobot import get_invoice_status

        remote = await get_invoice_status(invoice_id)
        if not remote or str(remote.get("invoice_id") or "") != invoice_id:
            return "unavailable", None
        status = remote.get("status")
        if status == "paid":
            return "paid", remote.get("amount")
        if status in {"expired", "cancelled", "canceled"}:
            return "canceled", None
        return "pending", None

    return "unsupported", None


async def reconcile_payment(
    bot,
    invoice_id: str,
    *,
    expected_tg_id: int | None = None,
) -> ReconciliationResult:
    """Verify and activate one invoice without ever trusting the client UI."""
    invoice_id = str(invoice_id)
    payment = await db.get_payment_by_invoice(invoice_id)
    if not payment:
        return ReconciliationResult(invoice_id, "not_found", reason="payment_not_found")
    if expected_tg_id is not None and int(payment["tg_id"]) != int(expected_tg_id):
        return ReconciliationResult(invoice_id, "not_found", reason="owner_mismatch")

    local_status = payment.get("status") or "pending"
    if local_status != "pending":
        return ReconciliationResult(invoice_id, local_status)
    if payment.get("refund_requested_at"):
        return ReconciliationResult(invoice_id, "blocked", reason="refund_requested")

    provider_state, provider_amount = await _provider_state(payment)
    if provider_state == "canceled":
        await db.update_payment_status_if_pending(invoice_id, "canceled")
        return ReconciliationResult(invoice_id, "canceled")
    if provider_state != "paid":
        return ReconciliationResult(invoice_id, "pending", reason=provider_state)
    if not _amount_matches(payment.get("amount"), provider_amount):
        logger.error(
            "Provider amount mismatch for payment %s: expected=%s received=%s",
            invoice_id,
            payment.get("amount"),
            provider_amount,
        )
        return ReconciliationResult(invoice_id, "pending", reason="amount_mismatch")

    activated = await process_paid_payment(
        bot,
        int(payment["tg_id"]),
        invoice_id,
        payment["tariff_code"],
        acquire_lock=True,
    )
    # A concurrent checker may have won the lock and completed while this call
    # returned False. Re-read durable state before reporting the outcome.
    current = await db.get_payment_by_invoice(invoice_id)
    current_status = (current or {}).get("status") or "pending"
    if current_status == "paid":
        return ReconciliationResult(invoice_id, "paid", activated=activated)
    return ReconciliationResult(
        invoice_id,
        current_status,
        activated=False,
        reason="activation_pending" if not activated else None,
    )


async def reconcile_user_pending_payments(bot, tg_id: int) -> list[ReconciliationResult]:
    """Reconcile every pending invoice, oldest first.

    This is what makes an older paid invoice survive navigation to a newer
    checkout: the current screen and the latest invoice are irrelevant.
    """
    payments = await db.get_pending_payments_for_user(tg_id)
    results = []
    for payment in payments:
        results.append(
            await reconcile_payment(bot, payment["invoice_id"], expected_tg_id=tg_id)
        )
    return results
