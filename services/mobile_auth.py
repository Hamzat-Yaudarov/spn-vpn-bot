"""Безопасная одноразовая Telegram-авторизация для Android-клиента."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from datetime import datetime, timedelta

import database as db
from config import (
    MOBILE_ACCESS_TOKEN_MINUTES,
    MOBILE_AUTH_CHALLENGE_MINUTES,
    MOBILE_REFRESH_TOKEN_DAYS,
)


CODE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
ACCESS_KEY_RE = re.compile(r"^WAY-(?:[A-Z2-7]{4}-){5}[A-Z2-7]{4}$")


class MobileAuthError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def utcnow() -> datetime:
    return datetime.utcnow()


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_access_key(value: str) -> str:
    """Нормализовать ключ без ослабления его формата."""
    return re.sub(r"\s+", "", (value or "").strip().upper())


def generate_access_key() -> str:
    """120-битный ключ аккаунта, пригодный для ручного ввода и QR."""
    encoded = base64.b32encode(secrets.token_bytes(15)).decode("ascii").rstrip("=")
    return "WAY-" + "-".join(encoded[index:index + 4] for index in range(0, len(encoded), 4))


def code_challenge_for_verifier(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def validate_code_challenge(value: str) -> str:
    value = (value or "").strip()
    if not CODE_CHALLENGE_RE.fullmatch(value):
        raise MobileAuthError("invalid_code_challenge", "Некорректный PKCE challenge")
    return value


async def create_challenge(code_challenge: str, device_name: str | None = None) -> dict:
    challenge_id = uuid.uuid4()
    start_token = secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(minutes=MOBILE_AUTH_CHALLENGE_MINUTES)
    await db.db_execute(
        """
        INSERT INTO mobile_auth_challenges
            (id, start_token_hash, code_challenge, device_name, expires_at)
        VALUES ($1, $2, $3, $4, $5)
        """,
        (
            challenge_id,
            hash_secret(start_token),
            validate_code_challenge(code_challenge),
            (device_name or "")[:120] or None,
            expires_at,
        ),
    )
    return {"id": str(challenge_id), "start_token": start_token, "expires_at": expires_at}


async def claim_challenge(start_token: str, tg_id: int):
    return await db.db_execute(
        """
        UPDATE mobile_auth_challenges
        SET candidate_tg_id = $2
        WHERE start_token_hash = $1
          AND status = 'pending'
          AND consumed_at IS NULL
          AND expires_at > now() AT TIME ZONE 'UTC'
          AND (candidate_tg_id IS NULL OR candidate_tg_id = $2)
        RETURNING id, device_name, expires_at, status
        """,
        (hash_secret(start_token), tg_id),
        fetch_one=True,
    )


async def pending_challenge_for_user(tg_id: int):
    return await db.db_execute(
        """
        SELECT id, device_name, expires_at
        FROM mobile_auth_challenges
        WHERE candidate_tg_id = $1
          AND status = 'pending'
          AND consumed_at IS NULL
          AND expires_at > now() AT TIME ZONE 'UTC'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (tg_id,),
        fetch_one=True,
    )


async def approve_challenge(challenge_id: str, tg_id: int) -> bool:
    try:
        parsed_id = uuid.UUID(challenge_id)
    except (ValueError, TypeError):
        return False
    row = await db.db_execute(
        """
        UPDATE mobile_auth_challenges
        SET approved_tg_id = $2, status = 'approved', approved_at = now()
        WHERE id = $1
          AND candidate_tg_id = $2
          AND status = 'pending'
          AND consumed_at IS NULL
          AND expires_at > now() AT TIME ZONE 'UTC'
        RETURNING id
        """,
        (parsed_id, tg_id),
        fetch_one=True,
    )
    return row is not None


def _token_response(session_id: uuid.UUID, access_token: str, refresh_token: str, access_expires: datetime) -> dict:
    return {
        "session_id": str(session_id),
        "token_type": "Bearer",
        "access_token": access_token,
        "expires_in": max(0, int((access_expires - utcnow()).total_seconds())),
        "refresh_token": refresh_token,
    }


async def _create_session(conn, tg_id: int, device_name: str | None) -> dict:
    session_id = uuid.uuid4()
    access_token = secrets.token_urlsafe(48)
    refresh_token = secrets.token_urlsafe(48)
    access_expires = utcnow() + timedelta(minutes=MOBILE_ACCESS_TOKEN_MINUTES)
    refresh_expires = utcnow() + timedelta(days=MOBILE_REFRESH_TOKEN_DAYS)
    await conn.execute(
        """
        INSERT INTO mobile_sessions
            (id, tg_id, device_name, access_token_hash, access_expires_at,
             refresh_token_hash, refresh_expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        session_id,
        int(tg_id),
        (device_name or "")[:120] or None,
        hash_secret(access_token),
        access_expires,
        hash_secret(refresh_token),
        refresh_expires,
    )
    return _token_response(session_id, access_token, refresh_token, access_expires)


async def issue_access_key(tg_id: int) -> str:
    """Выпустить/повернуть переносимый ключ аккаунта; в БД хранится только SHA-256."""
    access_key = generate_access_key()
    await db.db_execute(
        """
        INSERT INTO mobile_access_keys (id, tg_id, key_hash)
        VALUES ($1, $2, $3)
        ON CONFLICT (tg_id) DO UPDATE SET
            key_hash = EXCLUDED.key_hash,
            created_at = now(),
            last_used_at = NULL,
            revoked_at = NULL
        """,
        (uuid.uuid4(), int(tg_id), hash_secret(access_key)),
    )
    return access_key


async def exchange_access_key(access_key: str, device_name: str | None = None) -> dict:
    """Создать отдельную мобильную сессию для установки по ключу аккаунта."""
    normalized = normalize_access_key(access_key)
    if not ACCESS_KEY_RE.fullmatch(normalized):
        raise MobileAuthError("invalid_access_key", "Некорректный ключ доступа", 401)

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT tg_id
                FROM mobile_access_keys
                WHERE key_hash = $1 AND revoked_at IS NULL
                FOR UPDATE
                """,
                hash_secret(normalized),
            )
            if not row:
                raise MobileAuthError("invalid_access_key", "Ключ доступа не найден или был заменён", 401)
            await conn.execute(
                "UPDATE mobile_access_keys SET last_used_at = now() WHERE tg_id = $1",
                int(row["tg_id"]),
            )
            return await _create_session(conn, int(row["tg_id"]), device_name)


async def exchange_challenge(challenge_id: str, verifier: str) -> dict:
    try:
        parsed_id = uuid.UUID(challenge_id)
    except (ValueError, TypeError) as exc:
        raise MobileAuthError("invalid_challenge", "Challenge не найден", 404) from exc
    verifier = (verifier or "").strip()
    if not 43 <= len(verifier) <= 128 or not CODE_CHALLENGE_RE.fullmatch(verifier):
        raise MobileAuthError("invalid_verifier", "Некорректный verifier")

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM mobile_auth_challenges WHERE id = $1 FOR UPDATE",
                parsed_id,
            )
            if not row or row["consumed_at"] is not None:
                raise MobileAuthError("invalid_challenge", "Challenge уже использован или не найден", 404)
            if row["expires_at"] <= utcnow():
                raise MobileAuthError("challenge_expired", "Срок challenge истёк", 410)
            if not hmac.compare_digest(row["code_challenge"], code_challenge_for_verifier(verifier)):
                raise MobileAuthError("invalid_verifier", "Verifier не совпадает", 401)
            if row["status"] != "approved" or not row["approved_tg_id"]:
                raise MobileAuthError("authorization_pending", "Подтвердите вход в Telegram", 202)

            tokens = await _create_session(conn, int(row["approved_tg_id"]), row["device_name"])
            await conn.execute(
                """
                UPDATE mobile_auth_challenges
                SET status = 'consumed', consumed_at = now()
                WHERE id = $1
                """,
                parsed_id,
            )
    return tokens


async def rotate_refresh_token(refresh_token: str) -> dict:
    token_hash = hash_secret((refresh_token or "").strip())
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM mobile_sessions WHERE refresh_token_hash = $1 FOR UPDATE",
                token_hash,
            )
            if not row or row["revoked_at"] is not None:
                raise MobileAuthError("invalid_refresh_token", "Refresh token недействителен", 401)
            if row["refresh_expires_at"] <= utcnow():
                await conn.execute("UPDATE mobile_sessions SET revoked_at = now() WHERE id = $1", row["id"])
                raise MobileAuthError("refresh_token_expired", "Срок refresh token истёк", 401)

            access_token = secrets.token_urlsafe(48)
            new_refresh_token = secrets.token_urlsafe(48)
            access_expires = utcnow() + timedelta(minutes=MOBILE_ACCESS_TOKEN_MINUTES)
            refresh_expires = utcnow() + timedelta(days=MOBILE_REFRESH_TOKEN_DAYS)
            await conn.execute(
                """
                UPDATE mobile_sessions
                SET access_token_hash = $2, access_expires_at = $3,
                    refresh_token_hash = $4, refresh_expires_at = $5,
                    updated_at = now(), last_seen_at = now()
                WHERE id = $1
                """,
                row["id"],
                hash_secret(access_token),
                access_expires,
                hash_secret(new_refresh_token),
                refresh_expires,
            )
    return _token_response(row["id"], access_token, new_refresh_token, access_expires)


async def authenticate_access_token(access_token: str):
    return await db.db_execute(
        """
        UPDATE mobile_sessions
        SET last_seen_at = now()
        WHERE access_token_hash = $1
          AND access_expires_at > now() AT TIME ZONE 'UTC'
          AND revoked_at IS NULL
        RETURNING id, tg_id, device_name, access_expires_at
        """,
        (hash_secret((access_token or "").strip()),),
        fetch_one=True,
    )


async def revoke_session(session_id) -> None:
    await db.db_execute(
        "UPDATE mobile_sessions SET revoked_at = now(), updated_at = now() WHERE id = $1 AND revoked_at IS NULL",
        (session_id,),
    )
