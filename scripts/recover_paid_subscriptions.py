#!/usr/bin/env python3
"""Audit provider-confirmed, unfulfilled subscriptions; apply only explicitly.

No new payments are created. Dry run performs SELECTs and payment-status GETs.
Apply requires a stopped bot, backs up the selected rows, and uses the same
durable/idempotent activation path as the bot. Never sets paid by hand.
"""
import argparse
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def verified_paid(payment, remote):
    if not isinstance(remote, dict):
        return False
    try:
        expected = Decimal(str(payment["amount"]))
        if not expected.is_finite() or expected <= 0:
            return False
        if payment["provider"] == "yookassa":
            amount = remote.get("amount") or {}
            metadata = remote.get("metadata") or {}
            return bool(
                remote.get("status") == "succeeded"
                and remote.get("id") == payment["invoice_id"]
                and amount.get("currency") == "RUB"
                and Decimal(str(amount.get("value"))) == expected
                and str(metadata.get("tg_id")) == str(payment["tg_id"])
                and metadata.get("tariff_code") == payment["tariff_code"]
            )
        if payment["provider"] == "cryptobot":
            return bool(
                remote.get("status") == "paid"
                and str(remote.get("invoice_id")) == payment["invoice_id"]
                and remote.get("currency_type") == "fiat" and remote.get("fiat") == "RUB"
                and Decimal(str(remote.get("amount"))) == expected
                and remote.get("payload") == f"spn_{payment['tg_id']}_{payment['tariff_code']}"
            )
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return False
    return False


def require_stopped(service):
    result = subprocess.run(
        ["systemctl", "show", service, "-p", "ActiveState", "--value"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or result.stdout.strip() not in {"inactive", "failed"}:
        raise RuntimeError(f"Сначала остановите бот: sudo systemctl stop {service}")


def save_backup(directory, payment, subscriptions):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"payment-{payment['id']}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"payment": dict(payment), "subscriptions": [dict(s) for s in subscriptions]},
                  stream, ensure_ascii=False, default=str, indent=2)
    return path


async def run(args):
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    import asyncpg
    import config
    import database as db
    from services import payment_activation as activation
    from services.payment_processing import process_paid_payment
    from services.yookassa import get_payment_status
    from services.cryptobot import get_invoice_status

    if args.apply:
        require_stopped(args.service)
        if config.REMNAWAVE_API_VERSION != 3:
            raise RuntimeError("Для этой миграции в .env бота требуется REMNAWAVE_API_VERSION=3")
    # Deliberately do NOT call init_db(): an audit must never run schema sync.
    db._pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=4, command_timeout=60)
    bot = None
    try:
        async with db._pool.acquire() as conn:
            payments = await conn.fetch("""
                SELECT * FROM payments WHERE status = 'pending' AND refund_requested_at IS NULL
                  AND coalesce(payment_kind, 'subscription') = 'subscription'
                  AND (($1::text[] IS NOT NULL AND invoice_id = ANY($1::text[]))
                       OR ($1::text[] IS NULL AND created_at >= $2))
                ORDER BY id
            """, args.invoice or None, args.since)
        print(f"Не выданных локально платежей за подписку: {len(payments)}", flush=True)
        if args.invoice:
            found = {p["invoice_id"] for p in payments}
            for invoice in args.invoice:
                if invoice not in found:
                    print(f"{invoice}: не найден среди ожидающих (возможно, уже выдан); пропущен")
        confirmed, issued, failed = 0, 0, 0
        for payment in payments:
            invoice = payment["invoice_id"]
            getter = {"yookassa": get_payment_status, "cryptobot": get_invoice_status}.get(payment["provider"])
            remote = await getter(invoice) if getter else None
            if not verified_paid(payment, remote):
                print(f"{invoice}: успешная оплата/реквизиты не подтверждены; без изменений", flush=True)
                continue
            confirmed += 1
            tariff = config.TARIFFS.get(payment["tariff_code"])
            if not tariff:
                failed += 1
                print(f"{invoice}: тариф отсутствует; нужна ручная сверка", flush=True)
                continue
            async with db._pool.acquire() as conn:
                subs = await conn.fetch("SELECT * FROM subscriptions WHERE tg_id = $1 ORDER BY id", payment["tg_id"])
                reusable = await activation.reusable_placeholders(conn, payment)
                has_journal = await conn.fetchval("SELECT to_regclass('public.payment_subscription_activations')")
                reserved = (await conn.fetchrow(
                    "SELECT * FROM payment_subscription_activations WHERE invoice_id = $1", invoice)
                    if has_journal else None)
            if payment.get("payment_target") == "renew" and not reserved:
                # The old processor could update local expiry before marking
                # paid. Without its previous deadline we cannot prove that
                # replaying a legacy renewal will not add days a second time.
                failed += 1
                print(f"{invoice}: ОПЛАЧЕНО, старое продление без журнала активации; "
                      "нужна сверка текущего срока, автоматический повтор пропущен", flush=True)
                continue
            try:
                target, index = activation.choose_target(payment, subs, reusable, tariff["kind"])
            except activation.ActivationError as exc:
                failed += 1
                print(f"{invoice}: ОПЛАЧЕНО, user={payment['tg_id']}; {exc}; без изменений", flush=True)
                continue
            print(f"{invoice}: ОПЛАЧЕНО {payment['amount']} ₽, user={payment['tg_id']}, "
                  f"{payment['tariff_code']}, подписка={target['id'] if target else 'новая'}, "
                  f"номер={index}, пустых записей={len(reusable)}", flush=True)
            if not args.apply:
                continue
            require_stopped(args.service)
            backup = save_backup(args.backup_dir, payment, subs)
            print(f"Резервная копия: {backup}", flush=True)
            async with db._pool.acquire() as conn:
                await conn.execute(db.PAYMENT_ACTIVATION_SCHEMA)
            if payment["tg_id"] > 0 and bot is None:
                from aiogram import Bot
                from aiogram.client.default import DefaultBotProperties
                bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
            success = await process_paid_payment(bot, payment["tg_id"], invoice, payment["tariff_code"])
            if success:
                issued += 1
                print(f"{invoice}: ВЫДАНО", flush=True)
            else:
                failed += 1
                print(f"{invoice}: НЕ ВЫДАНО, платёж сохранён для безопасного повтора", flush=True)
        print(f"Подтверждено оплат: {confirmed}; выдано сейчас: {issued}; требуют внимания: {failed}")
        if not args.apply:
            print("ТОЛЬКО ПРОВЕРКА. База, подписки и сообщения не изменялись.")
        return 1 if failed else 0
    finally:
        if bot is not None:
            await bot.session.close()
        await db.close_db()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--invoice", action="append", help="Конкретный invoice ID, можно повторять")
    parser.add_argument("--all-confirmed", action="store_true", help="Явно разрешить выдачу всех подтверждённых оплат в периоде")
    parser.add_argument("--since", type=datetime.fromisoformat, default=datetime.utcnow() - timedelta(days=7),
                        help="Начало периода UTC, например 2026-08-30")
    parser.add_argument("--service", default="spn-bot")
    parser.add_argument("--backup-dir", type=Path,
                        default=ROOT / ("payment-recovery-backup-" + datetime.utcnow().strftime("%Y%m%d-%H%M%S")))
    args = parser.parse_args(argv)
    if args.apply and not (args.invoice or args.all_confirmed):
        parser.error("Для --apply явно укажите --invoice ID или --all-confirmed")
    if args.since.tzinfo is not None:
        parser.error("Укажите время UTC без часового пояса: 2026-08-30T00:00:00")
    return args


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run(parse_args())))
    except RuntimeError as exc:
        # Only our actionable RuntimeErrors; driver exceptions omit details.
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"Проверка/восстановление остановлено ({type(exc).__name__}); секреты не выводятся.", file=sys.stderr)
        raise SystemExit(1)
