#!/usr/bin/env python3
"""Read-only panel audit; --apply saves UUID -> ID mappings in the BOT database.

Run while the panel is still on 2.8.1. No panel users, keys, traffic, subscriptions
or payments are modified. --check-v3 verifies saved mappings after the upgrade.
"""
import argparse
import asyncio
import json
import os
import ssl
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class PreparationError(RuntimeError):
    pass


def public_identity(user: dict, version: int) -> dict:
    """Deliberately discard subscription URLs, credentials and personal data."""
    user_id = user.get("id")
    username = user.get("username")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or not 0 < user_id <= 2**53 - 1:
        raise PreparationError("В ответе панели отсутствует числовой ID пользователя.")
    if not isinstance(username, str) or not username:
        raise PreparationError("В ответе панели отсутствует имя пользователя.")
    result = {"user_id": user_id, "username": username}
    if version == 2:
        try:
            result["local_uuid"] = str(uuid.UUID(user["uuid"]))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise PreparationError("Нет старого UUID. Подготовку нужно делать ДО обновления панели на 3.") from exc
    elif user.get("uuid"):
        raise PreparationError("Панель ещё возвращает API 2; проверка API 3 пока невозможна.")
    return result


async def fetch_identities(session, panel_url: str, version: int) -> list[dict]:
    users = []
    seen_ids, seen_uuids, seen_names = set(), set(), set()
    expected_total = None
    while True:
        params = {"start": len(users), "size": 500,
                  "sorting": json.dumps([{"id": "username", "desc": False}])}
        async with session.get(panel_url + "/users", params=params, allow_redirects=False) as response:
            if response.status != 200:
                raise PreparationError(f"Панель ответила HTTP {response.status}. Ответ и токен не выводятся.")
            payload = await response.json()
        data = payload.get("response", {}) if isinstance(payload, dict) else {}
        page, total = data.get("users"), data.get("total")
        if not isinstance(page, list) or isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise PreparationError("Неожиданный формат списка пользователей панели.")
        if expected_total is not None and expected_total != total:
            raise PreparationError("Список пользователей изменился во время проверки. Повторите проверку.")
        expected_total = total
        for user in page:
            if not isinstance(user, dict):
                raise PreparationError("Неожиданный формат пользователя панели.")
            entry = public_identity(user, version)
            local_uuid = entry.get("local_uuid")
            if (entry["user_id"] in seen_ids or entry["username"] in seen_names
                    or local_uuid is not None and local_uuid in seen_uuids):
                raise PreparationError("Повторные/противоречивые идентификаторы в выгрузке. Повторите проверку.")
            seen_ids.add(entry["user_id"])
            seen_names.add(entry["username"])
            if local_uuid is not None:
                seen_uuids.add(local_uuid)
            users.append(entry)
        if len(users) == total:
            return users
        if not page or len(users) > total:
            raise PreparationError("Панель вернула неполный список пользователей.")


async def local_references(conn) -> list[dict]:
    # users.subscription_until is only a legacy fallback. If subscription rows
    # exist, those rows are authoritative (including hidden/refunded records).
    return await conn.fetch("""
        WITH refs AS (
            SELECT remnawave_uuid, subscription_until
            FROM subscriptions WHERE remnawave_uuid IS NOT NULL
            UNION ALL
            SELECT u.remnawave_uuid, u.subscription_until FROM users u
            WHERE u.remnawave_uuid IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM subscriptions s WHERE s.tg_id = u.tg_id)
        )
        SELECT remnawave_uuid AS local_uuid,
               bool_or(coalesce(subscription_until > (now() AT TIME ZONE 'UTC'), false)) AS active
        FROM refs GROUP BY remnawave_uuid
    """)


async def stored_identities(conn, panel_url: str) -> list[dict]:
    if not await conn.fetchval("SELECT to_regclass('public.remnawave_user_identities')"):
        return []
    return await conn.fetch(
        "SELECT local_uuid, user_id, username FROM remnawave_user_identities WHERE panel_url = $1",
        panel_url,
    )


def validate_mapping_conflicts(candidates: list[dict], stored: list[dict]) -> None:
    by_uuid = {str(row["local_uuid"]): int(row["user_id"]) for row in stored}
    by_id = {int(row["user_id"]): str(row["local_uuid"]) for row in stored}
    for row in candidates:
        local_uuid, user_id = row["local_uuid"], row["user_id"]
        if by_uuid.get(local_uuid, user_id) != user_id or by_id.get(user_id, local_uuid) != local_uuid:
            raise PreparationError("Сохранённая карта ID противоречит панели. Ничего не перезаписано.")


def coverage(references: list[dict], identities: list[dict]) -> tuple[int, int, int]:
    known = {str(row["local_uuid"]) for row in identities}
    missing = [row for row in references if str(row["local_uuid"]) not in known]
    return len(references) - len(missing), sum(bool(row["active"]) for row in missing), len(missing)


def verify_v3(stored: list[dict], remote: list[dict]) -> list[dict]:
    by_id = {row["user_id"]: row for row in remote}
    matched = []
    for row in stored:
        found = by_id.get(row["user_id"])
        if found is None:
            continue
        if found["username"] != row["username"]:
            raise PreparationError("ID совпал, но имя пользователя отличается. Нужна ручная проверка.")
        matched.append(row)
    return matched


def save_export(path: Path, panel_url: str, identities: list[dict]) -> None:
    # Never overwrite a previous backup or create a publicly readable export.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"format": 1, "panel_url": panel_url,
                   "created_at": datetime.now(timezone.utc).isoformat(),
                   "identities": identities}, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


async def apply_mapping(conn, panel_url: str, candidates: list[dict]) -> None:
    # Importing this constant does not run the general/destructive schema sync.
    from database import REMNAWAVE_IDENTITY_SCHEMA

    async with conn.transaction():
        await conn.execute(REMNAWAVE_IDENTITY_SCHEMA)
        await conn.execute("LOCK TABLE remnawave_user_identities IN SHARE ROW EXCLUSIVE MODE")
        validate_mapping_conflicts(candidates, await stored_identities(conn, panel_url))
        for row in candidates:
            await conn.execute("""
                INSERT INTO remnawave_user_identities (panel_url, local_uuid, user_id, username)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (panel_url, local_uuid) DO UPDATE SET username = EXCLUDED.username
                WHERE remnawave_user_identities.user_id = EXCLUDED.user_id
            """, panel_url, uuid.UUID(row["local_uuid"]), row["user_id"], row["username"])


async def run(args) -> int:
    from dotenv import load_dotenv
    import aiohttp
    import asyncpg

    if not args.env.is_file():
        raise PreparationError("Файл .env не найден; укажите его через --env.")
    load_dotenv(args.env)
    panel_url = os.environ.get("REMNAWAVE_BASE_URL", "").rstrip("/")
    parsed = urlsplit(panel_url)
    token, database_url = os.environ.get("REMNAWAVE_API_TOKEN"), os.environ.get("DATABASE_URL")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
        raise PreparationError("REMNAWAVE_BASE_URL должен быть HTTPS-адресом API без пароля/query/fragment.")
    if not token or not database_url:
        raise PreparationError("В .env нужны REMNAWAVE_API_TOKEN и DATABASE_URL. Не присылайте их в чат.")
    version = 3 if args.check_v3 else 2
    if args.apply and os.environ.get("REMNAWAVE_API_VERSION", "2") != "2":
        raise PreparationError("Сначала подготовьте карту на версии 2; REMNAWAVE_API_VERSION должен быть 2.")
    context = ssl.create_default_context(cafile=os.environ.get("REMNAWAVE_CA_BUNDLE") or None)
    async with aiohttp.ClientSession(
        headers={"Authorization": f"Bearer {token}"},
        timeout=aiohttp.ClientTimeout(total=30), connector=aiohttp.TCPConnector(ssl=context),
    ) as session:
        remote = await fetch_identities(session, panel_url, version)
    conn = await asyncpg.connect(database_url, timeout=20, command_timeout=60)
    try:
        refs = await local_references(conn)
        stored = await stored_identities(conn, panel_url)
        if args.check_v3:
            candidates = verify_v3(stored, remote)
        else:
            validate_mapping_conflicts(remote, stored)
            candidates = remote
        matched, missing_active, missing_total = coverage(refs, candidates)
        print(f"Пользователей в панели: {len(remote)}")
        print(f"Профилей в базе бота: {len(refs)}; сопоставлено: {matched}")
        print(f"Нет соответствия среди действующих подписок: {missing_active}")
        print(f"Нет соответствия среди остальных старых профилей: {missing_total - missing_active}")
        if missing_active:
            raise PreparationError("СТОП: не все действующие подписки сопоставлены. Панель пока не обновляйте.")
        if args.check_v3 and refs and not stored:
            raise PreparationError("Нет сохранённой карты ID. Не запускайте бот в режиме API 3.")
        if args.output:
            save_export(args.output, panel_url, [dict(row, local_uuid=str(row["local_uuid"])) for row in candidates])
            print(f"Отдельная карта ID сохранена: {args.output} (это НЕ резервная копия всей панели)")
        if args.apply:
            await apply_mapping(conn, panel_url, candidates)
            print(f"Карта ID сохранена в базе бота: {len(candidates)} записей. Панель не изменена.")
        elif args.check_v3:
            print("Проверка API 3 пройдена. Сроки, трафик и ключи не изменялись.")
        else:
            print("Только проверка; база не изменялась. Для сохранения нужны --apply и --output.")
        if missing_total:
            print("Старые профили без соответствий не удалены и не пересозданы. Нужна отдельная сверка при их восстановлении.")
    finally:
        await conn.close()
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Сохранить только карту ID в базе бота")
    mode.add_argument("--check-v3", action="store_true", help="Проверить карту после обновления панели")
    parser.add_argument("--output", type=Path, help="Новый защищённый JSON-файл с картой ID, без ключей доступа")
    args = parser.parse_args(argv)
    if args.apply and not args.output:
        parser.error("--apply требует --output: сохраните отдельную копию карты ID")
    return args


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run(parse_args())))
    except PreparationError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        # Exception messages from drivers may include connection strings/data.
        print(f"Проверка не завершена ({type(exc).__name__}). Не переходите к обновлению; секреты не выводятся.", file=sys.stderr)
        raise SystemExit(1)
