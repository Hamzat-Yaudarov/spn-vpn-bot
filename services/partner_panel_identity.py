"""Immutable, server-only panel identity; never use mutable branding on retries.

Version one deliberately preserves pre-cutover names and ownership markers.
Version two adds support-friendly metadata without weakening the marker check.
"""

from __future__ import annotations

import json
import re
from uuid import UUID

from services.partner_catalog import validate_slug


def _uuid(value):
    result = UUID(str(value))
    if result.int == 0:
        raise ValueError("Invalid panel identity")
    return str(result)


def ownership_marker(project_id, subscription_id):
    return f"SPN-WL:{_uuid(project_id)}:{_uuid(subscription_id)}"


def legacy_identity(project_id, subscription_id):
    return {
        "version": 1,
        "username": "wl_" + UUID(_uuid(subscription_id)).hex,
        "description": ownership_marker(project_id, subscription_id),
    }


def readable_identity(project_id, subscription_id, tg_id, *, slug, title, bot_username):
    project_id, subscription_id = _uuid(project_id), _uuid(subscription_id)
    slug = validate_slug(slug)
    # Remote JSON/JavaScript number fields must round-trip exactly.
    if type(tg_id) is not int or not 0 < tg_id <= 2**53 - 1:
        raise ValueError("Invalid panel identity")
    if not isinstance(title, str) or not title or len(title) > 80:
        raise ValueError("Invalid panel identity")
    if any(ord(c) < 32 or ord(c) == 127 for c in title):
        raise ValueError("Invalid panel identity")
    if not isinstance(bot_username, str) or not re.fullmatch(
        r"[A-Za-z0-9_]{5,32}", bot_username
    ):
        raise ValueError("Invalid panel identity")
    prefix = slug.replace("-", "_")
    username = prefix[:12] + "_" + UUID(subscription_id).hex[:20]
    tag = "P_" + prefix[:5].upper() + "_" + UUID(project_id).hex[:8].upper()
    description = (
        f"{title} · @{bot_username}\n{ownership_marker(project_id, subscription_id)}"
    )
    return {
        "version": 2,
        "slug": slug,
        "title": title,
        "bot_username": bot_username,
        "telegram_id": tg_id,
        "username": username,
        "tag": tag,
        "description": description,
    }


def validate_identity(identity, project_id, subscription_id):
    """Rebuild the whole immutable identity; malformed/foreign snapshots fail closed."""
    if isinstance(identity, str):
        identity = json.loads(identity)
    if not isinstance(identity, dict) or type(identity.get("version")) is not int:
        raise ValueError("Invalid panel identity")
    if identity["version"] == 1:
        expected = legacy_identity(project_id, subscription_id)
    elif identity["version"] == 2:
        expected = readable_identity(
            project_id,
            subscription_id,
            identity.get("telegram_id"),
            slug=identity.get("slug"),
            title=identity.get("title"),
            bot_username=identity.get("bot_username"),
        )
    else:
        raise ValueError("Invalid panel identity")
    if identity != expected:
        raise ValueError("Invalid panel identity")
    return expected


def identity_fields(identity, project_id, subscription_id):
    """Expected Remnawave fields. Telegram metadata never enters config responses."""
    identity = validate_identity(identity, project_id, subscription_id)
    result = {"username": identity["username"], "description": identity["description"]}
    if identity["version"] == 2:
        result.update(tag=identity["tag"], telegramId=identity["telegram_id"])
    return result
