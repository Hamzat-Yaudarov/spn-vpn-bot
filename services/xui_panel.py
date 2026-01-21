import aiohttp
import logging
import secrets
import json
import uuid as uuid_lib
from datetime import datetime
from config import (
    XUI_PANEL_URL,
    XUI_PANEL_PATH,
    XUI_USERNAME,
    XUI_PASSWORD,
    SUB_PORT,
    SUB_EXTERNAL_HOST,
    INBOUND_ID,
    API_REQUEST_TIMEOUT
)
from utils import safe_api_call

logger = logging.getLogger(__name__)

# =========================
# BASE URLS
# =========================
XUI_API_BASE = f"{XUI_PANEL_URL}{XUI_PANEL_PATH}/api"
XUI_LOGIN_URL = f"{XUI_PANEL_URL}{XUI_PANEL_PATH.rsplit('/panel', 1)[0]}/login/"


# =========================
# SESSION / LOGIN
# =========================
async def get_xui_session() -> aiohttp.ClientSession | None:
    async def _login():
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT)
        session = aiohttp.ClientSession(connector=connector, timeout=timeout)

        payload = {
            "username": XUI_USERNAME,
            "password": XUI_PASSWORD
        }

        try:
            async with session.post(XUI_LOGIN_URL, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"XUI HTTP {resp.status}: {text}")

                data = await resp.json()
                if not data.get("success"):
                    raise RuntimeError(f"XUI login failed: {data}")

                return session

        except Exception:
            await session.close()
            raise

    try:
        return await safe_api_call(_login, "Failed to authenticate with XUI panel")
    except Exception as e:
        logger.error(f"Get XUI session error: {e}")
        return None


# =========================
# GET CLIENT EXPIRY
# =========================
async def xui_get_client_expiry(
    session: aiohttp.ClientSession,
    email: str
) -> int | None:

    async def _get():
        url = f"{XUI_API_BASE}/inbounds/getClientTraffics/{email}"

        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"XUI HTTP {resp.status}: {await resp.text()}")

            data = await resp.json()
            if not data.get("success"):
                raise RuntimeError(f"Get client traffic failed: {data}")

            return data["obj"]["expiryTime"]

    try:
        return await safe_api_call(_get, f"Failed to get client expiry for {email}")
    except Exception as e:
        logger.error(f"Get client expiry error: {e}")
        return None


# =========================
# CREATE CLIENT
# =========================
async def xui_create_or_extend_client(
    session: aiohttp.ClientSession,
    tg_id: int,
    days: int
) -> tuple[str | None, str | None]:

    async def _create():
        client_uuid = str(uuid_lib.uuid4())
        client_email = f"tg_{tg_id}_{secrets.token_hex(4)}"

        add_ms = int(days * 30 * 24 * 60 * 60 * 1000)
        expiry = int(datetime.utcnow().timestamp() * 1000) + add_ms

        settings = {
            "clients": [{
                "id": client_uuid,
                "flow": "",
                "email": client_email,
                "limitIp": 0,
                "totalGB": 0,
                "expiryTime": expiry,
                "enable": True,
                "tgId": str(tg_id),
                "subId": secrets.token_hex(8),
                "reset": 0
            }]
        }

        payload = {
            "id": str(INBOUND_ID),
            "settings": json.dumps(settings)
        }

        url = f"{XUI_API_BASE}/inbounds/addClient"

        async with session.post(url, data=payload) as resp:
            if resp.status != 200:
                raise RuntimeError(f"XUI HTTP {resp.status}: {await resp.text()}")

            data = await resp.json()
            if not data.get("success"):
                raise RuntimeError(f"Add client failed: {data}")

            logger.info(f"Created XUI client TG={tg_id} UUID={client_uuid}")
            return client_uuid, client_email

    try:
        return await safe_api_call(_create, f"Failed to create XUI client for TG {tg_id}")
    except Exception as e:
        logger.error(f"Create client error: {e}")
        return None, None


# =========================
# EXTEND CLIENT
# =========================
async def xui_extend_client(
    session: aiohttp.ClientSession,
    client_uuid: str,
    client_email: str,
    days: int
) -> bool:

    async def _extend():
        current_expiry = await xui_get_client_expiry(session, client_email)
        if not current_expiry:
            raise RuntimeError("Cannot get current expiry")

        add_ms = int(days * 30 * 24 * 60 * 60 * 1000)
        new_expiry = current_expiry + add_ms

        settings = {
            "clients": [{
                "id": client_uuid,
                "flow": "",
                "email": client_email,
                "limitIp": 0,
                "totalGB": 0,
                "expiryTime": new_expiry,
                "enable": True,
                "reset": 0
            }]
        }

        payload = {
            "id": str(INBOUND_ID),
            "settings": json.dumps(settings)
        }

        url = f"{XUI_API_BASE}/inbounds/updateClient/{client_uuid}"

        async with session.post(url, data=payload) as resp:
            if resp.status != 200:
                raise RuntimeError(f"XUI HTTP {resp.status}: {await resp.text()}")

            data = await resp.json()
            if not data.get("success"):
                raise RuntimeError(f"Update client failed: {data}")

            logger.info(f"Extended XUI client {client_uuid} by {days} days")
            return True

    try:
        await safe_api_call(_extend, f"Failed to extend XUI client {client_uuid}")
        return True
    except Exception as e:
        logger.error(f"Extend client error: {e}")
        return False


# =========================
# SUBSCRIPTION URL
# =========================
async def xui_get_subscription_url(client_email: str) -> str:
    return f"http://{SUB_EXTERNAL_HOST}:{SUB_PORT}/sub/{client_email}"