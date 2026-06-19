import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class TelegramAuthError(ValueError):
    """Telegram Mini App initData is missing, invalid or expired."""


def validate_telegram_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 86_400,
) -> dict:
    if not init_data:
        raise TelegramAuthError("Missing Telegram initData")
    if not bot_token:
        raise TelegramAuthError("Bot token is not configured")

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise TelegramAuthError("Missing initData hash")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise TelegramAuthError("Invalid Telegram initData")

    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError as exc:
        raise TelegramAuthError("Invalid Telegram auth date") from exc
    now = int(time.time())
    if not auth_date or auth_date > now + 60 or now - auth_date > max_age_seconds:
        raise TelegramAuthError("Telegram initData has expired")

    try:
        user = json.loads(parsed.get("user", ""))
    except (json.JSONDecodeError, TypeError) as exc:
        raise TelegramAuthError("Invalid Telegram user") from exc
    if not isinstance(user, dict) or not user.get("id"):
        raise TelegramAuthError("Missing Telegram user id")
    return user
