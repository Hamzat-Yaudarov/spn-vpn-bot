#!/usr/bin/env python3
"""Recover missing Telegram users from bot logs.

Usage examples:
  sudo journalctl -u spn-bot --since "2026-06-30 00:00:00" --no-pager \
    | python3 scripts/recover_users_from_logs.py --parse-only

  sudo journalctl -u spn-bot --since "2026-06-30 00:00:00" --no-pager \
    | python3 scripts/recover_users_from_logs.py

  sudo journalctl -u spn-bot --since "2026-06-30 00:00:00" --no-pager \
    | python3 scripts/recover_users_from_logs.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


TG_ID_RE = r"(\d{6,12})"


@dataclass
class SeenUser:
    tg_id: int
    username: str | None = None
    hits: int = 0


USERNAME_PATTERNS = [
    re.compile(rf"\bUser\s+{TG_ID_RE}\(@([^)]+)\)"),
    re.compile(rf"\bStart command received:\s+user={TG_ID_RE}\s+username=([^\s]*)"),
    re.compile(rf"\bUnknown or inactive tracking link payload:\s+user={TG_ID_RE}\s+username=([^\s]*)"),
    re.compile(rf"\bUser ensured in database after /start:\s+user={TG_ID_RE}\s+username=([^\s]*)"),
]


ID_PATTERNS = [
    re.compile(rf"\b(?:User|user|TG)\s+{TG_ID_RE}\b"),
    re.compile(rf"\b(?:to|for)\s+user\s+{TG_ID_RE}\b", re.IGNORECASE),
    re.compile(rf"\bdirect message to\s+{TG_ID_RE}\b", re.IGNORECASE),
    re.compile(rf"\bBroadcast message copied to user\s+{TG_ID_RE}\b"),
    re.compile(rf"\btarget_id[=:]\s*{TG_ID_RE}\b"),
]


PASSIVE_LOG_MARKERS = (
    "failed to send notification to user",
    "notification sent",
    "next notification",
    "no subscription for user",
    "broadcast message copied to user",
    "direct message to",
    "sent a direct message",
    "notified about",
    "failed to notify user",
)


def clean_username(value: str | None) -> str | None:
    if not value:
        return None
    username = value.strip()
    if not username or username.lower() in {"none", "null", "-"}:
        return None
    return username.lstrip("@")[:64]


def remember(users: dict[int, SeenUser], tg_id: int, username: str | None = None) -> None:
    if tg_id <= 0:
        return
    user = users.setdefault(tg_id, SeenUser(tg_id=tg_id))
    user.hits += 1
    username = clean_username(username)
    if username and not user.username:
        user.username = username


def is_passive_log_line(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in PASSIVE_LOG_MARKERS)


def parse_log_lines(lines, include_passive_logs: bool = False) -> dict[int, SeenUser]:
    users: dict[int, SeenUser] = {}

    for line in lines:
        if not include_passive_logs and is_passive_log_line(line):
            continue

        for pattern in USERNAME_PATTERNS:
            for match in pattern.finditer(line):
                remember(users, int(match.group(1)), match.group(2))

        for pattern in ID_PATTERNS:
            for match in pattern.finditer(line):
                remember(users, int(match.group(1)))

    return users


def read_input(path: str | None) -> list[str]:
    if path:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    return sys.stdin.read().splitlines()


async def sync_with_database(users: dict[int, SeenUser], apply: bool) -> tuple[list[SeenUser], list[SeenUser]]:
    import database as db

    await db.init_db()
    existing: list[SeenUser] = []
    missing: list[SeenUser] = []
    try:
        for user in sorted(users.values(), key=lambda item: item.tg_id):
            if await db.user_exists(user.tg_id):
                existing.append(user)
            else:
                missing.append(user)

        if apply:
            for user in missing:
                await db.create_user(user.tg_id, user.username or f"user_{user.tg_id}")
    finally:
        await db.close_db()

    return existing, missing


def print_users(title: str, users: list[SeenUser], limit: int) -> None:
    print(f"\n{title}: {len(users)}")
    for user in users[:limit]:
        username = f" @{user.username}" if user.username else ""
        print(f"  {user.tg_id}{username} (hits={user.hits})")
    if len(users) > limit:
        print(f"  ... ещё {len(users) - limit}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Find Telegram IDs in bot logs and add missing users to database.")
    parser.add_argument("path", nargs="?", help="Log file path. If omitted, reads stdin.")
    parser.add_argument("--parse-only", action="store_true", help="Only parse log lines; do not connect to database.")
    parser.add_argument("--apply", action="store_true", help="Actually add missing users. Without this flag it is a dry run.")
    parser.add_argument(
        "--include-passive-logs",
        action="store_true",
        help="Also use passive bot-side lines such as notification/broadcast attempts.",
    )
    parser.add_argument("--limit", type=int, default=80, help="How many users to print in each section.")
    args = parser.parse_args()

    lines = read_input(args.path)
    users = parse_log_lines(lines, include_passive_logs=args.include_passive_logs)
    found = sorted(users.values(), key=lambda item: item.tg_id)

    print(f"Parsed log lines: {len(lines)}")
    print_users("Found Telegram IDs in logs", found, args.limit)

    if args.parse_only:
        return 0

    existing, missing = await sync_with_database(users, apply=args.apply)
    print_users("Already in users", existing, args.limit)
    print_users("Missing in users", missing, args.limit)

    if args.apply:
        print(f"\nAdded missing users: {len(missing)}")
    else:
        print("\nDry run only. Add --apply to insert missing users.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
