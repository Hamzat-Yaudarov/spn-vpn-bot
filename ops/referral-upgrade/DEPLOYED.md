# Referral upgrade deployment — 2026-10-03

Installed 20 manifest-listed files on the bot VPS, `/root/spn-vpn-bot`.
Base commit: `d18d9ed`. No Git commit or push was performed.

## Backup and scope

- Remote backup: `/root/spn-before-referral-flgv_sp6`.
- `files.tar.gz`: prior versions of replaced files.
- `referral-finances.json`: referral/partner financial snapshot, not a full database or panel backup.
- `.env`, credentials, venv, user keys and unrelated files were not replaced.
- The pre-existing local `schema.sql` change was not included in this deployment.
- No broadcast, test payout or money transfer was made.

## Verification

- Installed file hashes match `manifest.json`.
- Service active, `NRestarts=0`; polling and HTTP application started successfully.
- `/app`, `/admin`, `/account`: HTTP 200 with updated content.
- Unauthenticated referral/admin withdrawal API requests: HTTP 401.
- Runtime withdrawal minimum: 1,500 RUB.
- Financial records exactly equal the pre-install snapshot: 530 referral earnings,
  35 referral withdrawal ledger rows, 6 partner earnings, 0 partner withdrawals.
- No application errors were found in the checked post-start logs.
- Local full suite: 185 tests passed with 10 optional skips.
- New referral tests with isolated PGlite: 13 passed.
- Local fixture-only Playwright UI checks passed for MiniApp, admin and website.
- Staged server tests: 25 passed, 8 optional skips, with custom emoji disabled only
  in the test process; production configuration was not changed.
- A pre-existing optional payment-recovery SQL test failure is documented in
  `REFERRAL_PROGRAM.md` and reproduced on unchanged base code.

The old service exceeded its systemd stop timeout before installation. MainPID
was confirmed zero, its failed state was reset and the updated service started
successfully. No manual kill was performed.

## Existing pending request delivered

Request R-39, 5,000 RUB via SBP, was created at 2026-10-03 10:41:56 UTC,
before this deployment. Startup queued its notification without creating a new
withdrawal. The admin notification was delivered at 12:49:36 UTC in one attempt
to the configured administrator @Youdar2. The request remains pending.

Review it under «Ещё → Админ-панель → Выплаты» and check whether any manual
transfer has already been made before paying. The update did not transfer money.
Notifications with the same request number refer to the same request.

The draft promotional post is in `REFERRAL_PROGRAM.md`; it was not published.
