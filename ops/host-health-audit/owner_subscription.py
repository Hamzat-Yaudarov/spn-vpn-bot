"""Explicit owner-only subscription collection; default is a pure no-I/O plan.

The owner approved using an existing test subscription and its small traffic /
request-device metadata effects. This helper creates no users or payments and
never changes account settings. A saved deployed OpenAPI document must prove
the exact GET routes before any credentials are read or requests are made.
Normalized RAW output is private and is NOT advertised as executable Xray JSON.
The separately authorized --discover-owner diagnostic needs no OpenAPI: it uses
the exact official legacy owner route, falling back to the exact filtered
stream route ONLY on HTTP 404. It never fetches subscription configuration.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import ssl
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[2]
OWNER_ID = 6910097562
PANEL_ORIGIN = "https://panel.wayspn.online/api"
STATE_PARENT = Path("/private/tmp")
MAX_BYTES = 8_000_000
MAX_USERS = 100


class CollectionError(ValueError):
    pass


class PanelHTTPError(CollectionError):
    def __init__(self, status):
        self.status = status
        super().__init__("Panel HTTP " + str(status) + "; body suppressed.")


def require(value):
    if not value:
        raise CollectionError("Owner subscription prerequisites failed; details suppressed.")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CollectionError("Only documented owner-only collection options are accepted.")


def canonical_uuid(value):
    require(isinstance(value, str) and str(uuid.UUID(value)) == value)
    return value


def owner_id(value):
    return type(value) is int and value == OWNER_ID or value == str(OWNER_ID)


def private_json(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                and 0 < info.st_size <= MAX_BYTES)
        raw = stream.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES)
    result = json.loads(raw)
    require(isinstance(result, dict))
    return result


def parameters(spec, path, operation):
    result = {}
    for item in path.get("parameters", []) + operation.get("parameters", []):
        if "$ref" in item:
            ref = item["$ref"]
            require(isinstance(ref, str) and ref.startswith("#/components/parameters/"))
            item = spec.get("components", {}).get("parameters", {}).get(ref.rsplit("/", 1)[-1])
        require(isinstance(item, dict) and isinstance(item.get("name"), str))
        result[(item.get("in"), item["name"])] = item
    return result


def resolve_schema(spec, value, depth=0):
    require(isinstance(value, dict) and depth < 20)
    if "$ref" in value:
        ref = value["$ref"]
        require(isinstance(ref, str) and ref.startswith("#/components/schemas/"))
        return resolve_schema(spec, spec.get("components", {}).get("schemas", {}).get(ref.rsplit("/", 1)[-1]), depth + 1)
    if "allOf" in value:
        merged = {"type": "object", "properties": {}}
        require(isinstance(value["allOf"], list))
        for child in value["allOf"]:
            child = resolve_schema(spec, child, depth + 1)
            merged["properties"].update(child.get("properties", {}))
        merged["properties"].update(value.get("properties", {}))
        return merged
    return value


def properties(spec, value):
    result = resolve_schema(spec, value).get("properties")
    require(isinstance(result, dict))
    return result


def response_schema(spec, route):
    paths = spec["paths"]
    matches = [paths[name] for name in (route, "/api" + route) if name in paths]
    require(len(matches) == 1)
    response = matches[0]["get"].get("responses", {}).get("200")
    require(isinstance(response, dict))
    body = response.get("content", {}).get("application/json", {}).get("schema")
    return properties(spec, body)["response"]


def uuid_user_schema(spec, value):
    fields = properties(spec, value)
    require(resolve_schema(spec, fields.get("uuid", {})).get("type") == "string"
            and "telegramId" in fields)


def documented_get(spec, route):
    paths = spec.get("paths")
    require(isinstance(paths, dict) and isinstance(spec.get("info"), dict)
            and isinstance(spec["info"].get("version"), str))
    matches = [paths[name] for name in (route, "/api" + route) if name in paths]
    require(len(matches) <= 1)
    if not matches:
        return None
    path = matches[0]
    require(isinstance(path, dict) and isinstance(path.get("get"), dict))
    return parameters(spec, path, path["get"])


def discover(spec):
    stream = documented_get(spec, "/users/stream")
    legacy = documented_get(spec, "/users/by-telegram-id/{telegramId}")
    raw = documented_get(spec, "/subscriptions/by-short-uuid/{shortUuid}/raw")
    require(raw is not None and ("path", "shortUuid") in raw
            and ("query", "withDisabledHosts") in raw)
    if stream is not None and {("query", "telegramId"), ("query", "size")} <= set(stream):
        lookup = "stream"
        lookup_fields = properties(spec, response_schema(spec, "/users/stream"))
        require(resolve_schema(spec, lookup_fields.get("hasMore", {})).get("type") == "boolean")
        users = resolve_schema(spec, lookup_fields.get("users", {}))
    else:
        require(legacy is not None and ("path", "telegramId") in legacy)
        lookup = "legacy"
        users = resolve_schema(spec, response_schema(spec, "/users/by-telegram-id/{telegramId}"))
    require(users.get("type") == "array")
    uuid_user_schema(spec, users.get("items"))
    raw_fields = properties(spec, response_schema(spec, "/subscriptions/by-short-uuid/{shortUuid}/raw"))
    # This collector intentionally supports only the deployed UUID contract.
    # Main/v3 numeric IDs require a separately reviewed adaptation, not guesses.
    require(resolve_schema(spec, properties(spec, raw_fields.get("user")).get("uuid", {})).get("type") == "string"
            and resolve_schema(spec, raw_fields.get("resolvedProxyConfigs", {})).get("type") == "array")
    return {"lookup": lookup, "cursor": stream is not None and ("query", "cursor") in stream,
            "version": spec["info"]["version"]}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise CollectionError("Panel redirect refused.")


def credentials():
    # The same two-key parser as the existing inventory adapter; never import
    # application settings, and never print any .env values or response body.
    from dotenv import dotenv_values
    lines = [line for line in (ROOT / ".env").read_text().splitlines()
             if line.split("=", 1)[0].strip() in {"REMNAWAVE_BASE_URL", "REMNAWAVE_API_TOKEN"}]
    values = dotenv_values(stream=io.StringIO("\n".join(lines)), interpolate=False)
    base, token = values.get("REMNAWAVE_BASE_URL"), values.get("REMNAWAVE_API_TOKEN")
    require(isinstance(base, str) and base.rstrip("/") == PANEL_ORIGIN)
    require(isinstance(token, str) and 0 < len(token) <= 8192 and not any(c.isspace() for c in token))
    return base.rstrip("/"), token


class OwnerAPI:
    def __init__(self, contract):
        import certifi
        self.contract = contract
        self.base, self.token = credentials()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
        self.selected_short = None

    def get(self, path):
        parsed = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
        require(not parsed.scheme and not parsed.netloc and not parsed.fragment)
        if self.contract["lookup"] in {"stream", "owner-diagnostic"} and parsed.path == "/users/stream":
            require(set(query) <= {"telegramId", "size", "cursor"}
                    and query.get("telegramId") == [str(OWNER_ID)] and query.get("size") == ["25"]
                    and ("cursor" not in query or self.contract["lookup"] == "stream"
                         and self.contract["cursor"] and len(query["cursor"]) == 1))
        elif self.contract["lookup"] in {"legacy", "owner-diagnostic"} and path == "/users/by-telegram-id/" + str(OWNER_ID):
            pass
        else:
            require(self.contract["lookup"] != "owner-diagnostic" and self.selected_short is not None
                    and path == "/subscriptions/by-short-uuid/" + self.selected_short + "/raw?withDisabledHosts=false")
        request = urllib.request.Request(self.base + path, method="GET", headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=25) as response:
                require(response.status == 200)
                body = response.read(MAX_BYTES + 1)
                require(len(body) <= MAX_BYTES)
                value = json.loads(body)
                require(isinstance(value, dict) and set(value) == {"response"})
                return value["response"]
        except urllib.error.HTTPError as error:
            raise PanelHTTPError(error.code) from None


def lookup(api, contract):
    rows, cursors, cursor = [], set(), None
    for _ in range(5):
        if contract["lookup"] == "legacy":
            page = api.get("/users/by-telegram-id/" + str(OWNER_ID))
            require(isinstance(page, list))
            batch, has_more = page, False
        else:
            query = {"telegramId": str(OWNER_ID), "size": "25"}
            if cursor is not None:
                query["cursor"] = cursor
            page = api.get("/users/stream?" + urllib.parse.urlencode(query))
            require(isinstance(page, dict) and isinstance(page.get("users"), list)
                    and type(page.get("hasMore")) is bool)
            batch, has_more = page["users"], page["hasMore"]
        for row in batch:
            require(isinstance(row, dict) and owner_id(row.get("telegramId")))
            canonical_uuid(row.get("uuid"))
            require(not any(existing["uuid"] == row["uuid"] for existing in rows))
            rows.append(row)
        require(len(rows) <= MAX_USERS)
        if not has_more:
            return rows
        cursor = page.get("nextCursor")
        require(contract["cursor"] and isinstance(cursor, str) and 0 < len(cursor) <= 512 and cursor not in cursors)
        cursors.add(cursor)
    raise CollectionError("Owner lookup pagination bound exceeded.")


def preview(row):
    name = row.get("username")
    return {"uuid": row["uuid"], "status": row.get("status") if row.get("status") in {
        "ACTIVE", "DISABLED", "LIMITED", "EXPIRED"} else "UNKNOWN",
        "name": "".join(c for c in name if c.isprintable())[:80] if isinstance(name, str) else None}


def save_private(value, name="normalized.private.json"):
    require(name in {"normalized.private.json", "owner-lookup.private.json"})
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    require(len(raw) <= MAX_BYTES and STATE_PARENT == Path("/private/tmp") and not STATE_PARENT.is_symlink())
    directory = Path(tempfile.mkdtemp(prefix="wayspn-owner-subscription-", dir=STATE_PARENT))
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return path, hashlib.sha256(raw).hexdigest()


def diagnostic_account(row):
    require(isinstance(row, dict) and owner_id(row.get("telegramId")))
    if "uuid" in row:
        identity = canonical_uuid(row["uuid"])
    else:
        identity = row.get("id")
        require(type(identity) is int and identity > 0)
    username, status, expiry = row.get("username"), row.get("status"), row.get("expireAt")
    require(isinstance(username, str) and 0 < len(username) <= 256 and all(c.isprintable() for c in username)
            and status in {"ACTIVE", "DISABLED", "LIMITED", "EXPIRED"}
            and isinstance(expiry, str) and 0 < len(expiry) <= 64)
    date = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    require(date.tzinfo is not None)
    return {"accountID": identity, "username": username, "status": status, "expiry": expiry}


def discover_owner():
    api = OwnerAPI({"lookup": "owner-diagnostic", "cursor": False})
    route = "/users/by-telegram-id/" + str(OWNER_ID)
    try:
        rows = api.get(route)
        require(isinstance(rows, list))
    except PanelHTTPError as error:
        if error.status != 404:
            raise
        route = "/users/stream?telegramId=" + str(OWNER_ID) + "&size=25"
        page = api.get(route)
        # Diagnostic discovery deliberately does not paginate or guess another
        # wrapper. More than one page needs a separately reviewed next step.
        require(isinstance(page, dict) and set(page) == {"users", "nextCursor", "hasMore"}
                and page["hasMore"] is False and page["nextCursor"] is None)
        rows = page["users"]
        require(isinstance(rows, list))
    require(len(rows) <= 25)
    accounts = [diagnostic_account(row) for row in rows]
    require(len({str(row["accountID"]) for row in accounts}) == len(accounts))
    path, _ = save_private({"format": 1, "kind": "owner-only-lookup-not-subscription-config",
        "ownerTelegramId": OWNER_ID, "collectedAt": datetime.now(timezone.utc).isoformat(),
        "lookupRoute": route, "users": rows}, "owner-lookup.private.json")
    return {"count": len(accounts), "accounts": accounts, "private_file": str(path)}


def collect(spec_path, selected_uuid=None):
    contract = discover(private_json(spec_path))
    if selected_uuid is not None:
        canonical_uuid(selected_uuid)
    api = OwnerAPI(contract)
    rows = lookup(api, contract)
    active = [row for row in rows if row.get("status") == "ACTIVE"]
    if selected_uuid is None and len(active) != 1:
        return {"collected": False, "selection_required": True, "accounts": [preview(row) for row in rows]}
    selected = [row for row in active if row["uuid"] == selected_uuid] if selected_uuid else active
    require(len(selected) == 1)
    selected = selected[0]
    short = selected.get("shortUuid")
    require(isinstance(short, str) and 1 <= len(short) <= 128
            and all(c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in short))
    api.selected_short = short
    result = api.get("/subscriptions/by-short-uuid/" + short + "/raw?withDisabledHosts=false")
    require(isinstance(result, dict) and isinstance(result.get("user"), dict)
            and result["user"].get("uuid") == selected["uuid"]
            and ("telegramId" not in result["user"] or owner_id(result["user"]["telegramId"]))
            and isinstance(result.get("resolvedProxyConfigs"), list))
    path, digest = save_private({"format": 1, "kind": "remnawave-resolved-proxy-configs-not-executable-xray",
        "ownerTelegramId": OWNER_ID, "userUuid": selected["uuid"], "apiVersion": contract["version"],
        "collectedAt": datetime.now(timezone.utc).isoformat(), "response": result})
    return {"collected": True, "user_uuid": selected["uuid"], "private_file": str(path), "sha256": digest,
            "resolved_proxy_count": len(result["resolvedProxyConfigs"]), "account_changes": False,
            "request_device_metadata_may_change": True, "executable_xray_json": False}


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        action = parser.add_mutually_exclusive_group()
        action.add_argument("--collect", action="store_true")
        action.add_argument("--discover-owner", action="store_true")
        parser.add_argument("--openapi", type=Path)
        parser.add_argument("--select-user")
        args = parser.parse_args(argv)
        if args.discover_owner:
            require(args.openapi is None and args.select_user is None)
            result = discover_owner()
        elif args.collect:
            require(args.openapi is not None)
            result = collect(args.openapi, args.select_user)
        else:
            require(args.openapi is None and args.select_user is None)
            result = {"plan_only": True, "network": False, "owner_telegram_id": OWNER_ID,
                      "new_users": False, "payments": False}
        print(json.dumps(result, sort_keys=True))
        return 0
    except BaseException as error:
        if isinstance(error, SystemExit) and error.code == 0:
            return 0
        print("Owner-only collection stopped; credentials, subscription links and raw responses suppressed.", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
