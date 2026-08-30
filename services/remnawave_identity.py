"""Remnawave 2 UUIDs remain local handles after the explicit switch to API 3.

An unmapped handle is an error, never a missing/deleted remote user. The map is
scoped to the API URL so an unrelated panel cannot receive destructive requests.
"""
import uuid
from datetime import datetime, timezone

import database as db
from config import REMNAWAVE_API_VERSION, REMNAWAVE_BASE_URL


PANEL_URL = REMNAWAVE_BASE_URL.rstrip("/")


class IdentityError(RuntimeError):
    pass


def numeric_user_id(data: dict) -> int:
    value = data.get("id")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2**53 - 1:
        raise IdentityError("Remnawave response has no valid numeric user ID")
    return value


async def api_user_id(local_uuid) -> str | int:
    if REMNAWAVE_API_VERSION == 2:
        return str(local_uuid)
    row = await db.db_execute(
        "SELECT user_id FROM remnawave_user_identities WHERE panel_url = $1 AND local_uuid = $2",
        (PANEL_URL, uuid.UUID(str(local_uuid))), fetch_one=True,
    )
    if not row:
        raise IdentityError("Remnawave ID mapping missing; run the preparation script before upgrading")
    return int(row["user_id"])


async def remember_remote_user(data: dict) -> str:
    """Preserve v2 IDs; new v3 users get stable local UUID handles, not VPN keys."""
    legacy_uuid = data.get("uuid")
    if REMNAWAVE_API_VERSION == 2:
        if not legacy_uuid:
            raise IdentityError("API 2 configured, but the panel returned no user UUID")
        legacy_uuid = str(uuid.UUID(str(legacy_uuid)))
        # Also record new/renewed v2 users between preparation and the cutover.
        if data.get("id") is None:
            return legacy_uuid
    elif legacy_uuid:
        raise IdentityError("API 3 configured, but the panel still returned a v2 user")

    user_id = numeric_user_id(data)
    username = data.get("username")
    if not isinstance(username, str) or not username:
        raise IdentityError("Remnawave response has no username")
    known = await db.db_execute(
        "SELECT local_uuid FROM remnawave_user_identities WHERE panel_url = $1 AND user_id = $2",
        (PANEL_URL, user_id), fetch_one=True,
    )
    if known:
        if legacy_uuid and str(known["local_uuid"]) != legacy_uuid:
            raise IdentityError("Conflicting Remnawave UUID/ID mapping")
        return str(known["local_uuid"])

    if legacy_uuid:
        local_uuid = uuid.UUID(legacy_uuid)
    else:
        # Recover a pre-existing local reference by its exact unique username.
        rows = await db.db_execute(
            """SELECT remnawave_uuid FROM subscriptions
               WHERE remnawave_username = $1 AND remnawave_uuid IS NOT NULL
               UNION SELECT remnawave_uuid FROM users
               WHERE remnawave_username = $1 AND remnawave_uuid IS NOT NULL""",
            (username,), fetch_all=True,
        )
        if len(rows) > 1:
            raise IdentityError("Several local subscriptions have the same Remnawave username")
        local_uuid = (uuid.UUID(str(rows[0]["remnawave_uuid"])) if rows else
                      uuid.uuid5(uuid.NAMESPACE_URL, f"{PANEL_URL}/users/{user_id}"))

    row = await db.db_execute(
        """INSERT INTO remnawave_user_identities (panel_url, local_uuid, user_id, username)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (panel_url, user_id) DO UPDATE SET username = EXCLUDED.username
           WHERE remnawave_user_identities.local_uuid = EXCLUDED.local_uuid
           RETURNING local_uuid""",
        (PANEL_URL, local_uuid, user_id, username), fetch_one=True,
    )
    if not row:
        raise IdentityError("Conflicting Remnawave UUID/ID mapping")
    return str(row["local_uuid"])


def expiry_fields(expire_at: datetime) -> dict:
    if REMNAWAVE_API_VERSION == 2:
        return {"expireAt": expire_at.isoformat()}
    utc_expiry = expire_at.replace(tzinfo=timezone.utc) if expire_at.tzinfo is None else expire_at
    if utc_expiry <= datetime.now(timezone.utc):
        # v3 rejects past expireAt. Disable access without changing keys/traffic.
        return {"status": "DISABLED"}
    # EXPIRED is reactivated by the panel on a future date. Do not force ACTIVE
    # for LIMITED users: simply buying more days must not bypass a traffic cap.
    return {"expireAt": utc_expiry.isoformat()}


async def should_reactivate(local_uuid) -> bool:
    """Only explicitly reactivate profiles previously disabled by this bot."""
    if REMNAWAVE_API_VERSION == 2:
        return False
    row = await db.db_execute(
        """SELECT disabled_expire_at FROM remnawave_user_identities
           WHERE panel_url = $1 AND local_uuid = $2""",
        (PANEL_URL, uuid.UUID(str(local_uuid))), fetch_one=True,
    )
    return bool(row and row["disabled_expire_at"] is not None)


async def remember_expiry(local_uuid, expire_at: datetime) -> None:
    if REMNAWAVE_API_VERSION == 2:
        return
    fields = expiry_fields(expire_at)
    disabled_until = None
    if fields.get("status") == "DISABLED":
        disabled_until = (expire_at if expire_at.tzinfo is None else
                          expire_at.astimezone(timezone.utc).replace(tzinfo=None))
    await db.db_execute(
        """UPDATE remnawave_user_identities SET disabled_expire_at = $3
           WHERE panel_url = $1 AND local_uuid = $2""",
        (PANEL_URL, uuid.UUID(str(local_uuid)), disabled_until),
    )


async def normalize_user_info(local_uuid, data: dict) -> dict:
    if REMNAWAVE_API_VERSION == 2:
        return data
    data = dict(data)
    row = await db.db_execute(
        """SELECT user_id, disabled_expire_at FROM remnawave_user_identities
           WHERE panel_url = $1 AND local_uuid = $2""",
        (PANEL_URL, uuid.UUID(str(local_uuid))), fetch_one=True,
    )
    if not row or int(row["user_id"]) != numeric_user_id(data):
        raise IdentityError("Remnawave returned a different user")
    data["uuid"] = str(local_uuid)
    if data.get("status") == "DISABLED" and row["disabled_expire_at"] is not None:
        data["expireAt"] = row["disabled_expire_at"].isoformat() + "Z"
    return data
