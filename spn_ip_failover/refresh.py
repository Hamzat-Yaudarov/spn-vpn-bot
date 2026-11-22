"""Refresh private monitor routes from the Admin subscription using read-only GETs.

Only the supported, isolated client outbounds are retained. Full account and RAW
responses, subscription identifiers and API tokens are never written or printed.
The configured Xray path and hash are references; workers verify the binary.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
from uuid import UUID
from urllib.parse import urlsplit

from . import monitor
from .remna import Remnawave
from .resolved import xray_outbound
from .worker import ProbeArguments, load_audit_helper


CONFIG_FIELDS = {"panel_url", "token_env", "username", "xray", "sha256", "output", "coverage_output"}
OPTIONAL_FIELDS = {"selected_host_uuids"}
SHORT_UUID = re.compile(r"[A-Za-z0-9_-]{1,128}")
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")


def require(condition):
    if not condition:
        raise ValueError("Private route refresh prerequisites failed")


def absolute_path(value):
    require(isinstance(value, str) and 0 < len(value) <= 4096 and not any(ord(c) < 32 for c in value))
    path = Path(value)
    require(path.is_absolute() and ".." not in path.parts and str(path) == value and not value.startswith("//"))
    return path


def validate_output(path):
    """Preflight both targets before any API request or artifact replacement."""
    require(not any(item.is_symlink() for item in (path, *path.parents)))
    parent = path.parent.stat()
    require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.geteuid()
            and stat.S_IMODE(parent.st_mode) == 0o700)
    if path.exists():
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)


def validate_config(config, config_path=None):
    require(isinstance(config, dict) and CONFIG_FIELDS <= set(config)
            and not set(config) - CONFIG_FIELDS - OPTIONAL_FIELDS)
    if "selected_host_uuids" in config:
        selected = config['selected_host_uuids']
        require(isinstance(selected, list) and 1 <= len(selected) <= 128)
        require(len({canonical_uuid(value) for value in selected}) == len(selected))
    require(config["username"] == "Admin")
    require(isinstance(config["panel_url"], str) and 0 < len(config["panel_url"]) <= 2048)
    require(not any(c.isspace() or ord(c) < 32 for c in config["panel_url"]))
    origin = urlsplit(config["panel_url"])
    require(origin.scheme == "https" and bool(origin.hostname) and origin.username is None and origin.password is None
            and not origin.query and not origin.fragment and origin.path.rstrip("/") in ("", "/api")
            and (origin.port is None or 1 <= origin.port <= 65535))
    require(isinstance(config["token_env"], str) and ENV_NAME.fullmatch(config["token_env"]) is not None)
    require(isinstance(config["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", config["sha256"]) is not None)
    engine = absolute_path(config["xray"])
    output, coverage = absolute_path(config["output"]), absolute_path(config["coverage_output"])
    require(output != coverage and engine not in (output, coverage))
    if config_path is not None:
        require(absolute_path(str(config_path)) not in (output, coverage))
    validate_output(output)
    validate_output(coverage)
    return output, coverage


def canonical_uuid(value):
    require(isinstance(value, str))
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("Private route refresh prerequisites failed") from None
    require(canonical == value)
    return canonical


def account(record, now, *, expected=None):
    require(isinstance(record, dict))
    if expected is None or "id" in record:
        require(type(record.get("id")) is int and record["id"] > 0)
        require(expected is None or record["id"] == expected["id"])
    else:
        require(record.get("shortUuid") == expected["shortUuid"])
    if "uuid" in record:
        canonical_uuid(record["uuid"])
        if expected is not None and "uuid" in expected:
            require(record["uuid"] == expected["uuid"])
    if expected is not None and "shortUuid" in record:
        require(record["shortUuid"] == expected["shortUuid"])
    require(record.get("status") == "ACTIVE")
    if expected is None or "username" in record:
        require(record.get("username") == "Admin")
    if "expireAt" in record:
        expiry = record["expireAt"]
        require(isinstance(expiry, str) and 1 <= len(expiry) <= 64)
        try:
            expires = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Private route refresh prerequisites failed") from None
        require(expires.tzinfo is not None and expires.utcoffset() is not None and expires > now)


def public_name(row, metadata):
    for source, field in ((metadata, "remark"), (row, "finalRemark")):
        if field in source and source[field] is not None:
            value = source[field]
            require(isinstance(value, str) and 1 <= len(value) <= 512 and not any(ord(c) < 32 for c in value))
    value = metadata.get("remark") or row.get("finalRemark")
    require(isinstance(value, str) and bool(value))
    return value


def disabled_flag(row, metadata):
    values = [item["isDisabled"] for item in (row, metadata) if "isDisabled" in item]
    require(all(type(value) is bool for value in values) and len(set(values)) <= 1)
    return values[0] if values else None


def compile_inventory(audit, config, raw, now, admin):
    require(isinstance(raw, dict) and isinstance(raw.get("user"), dict))
    account(raw["user"], now, expected=admin)
    rows = raw.get("resolvedProxyConfigs")
    require(isinstance(rows, list) and 1 <= len(rows) <= 512)
    routes, coverage_rows, seen = {}, [], set()
    selected = set(config.get('selected_host_uuids', []))
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get("metadata"), dict))
        metadata = row["metadata"]
        key = canonical_uuid(metadata.get("uuid"))
        require(key not in seen)
        seen.add(key)
        if selected and key not in selected:
            continue  # No conversion, retention or VPN test of excluded hosts.
        name, disabled = public_name(row, metadata), disabled_flag(row, metadata)
        # These are the only source-row fields retained in the coverage file.
        address, port = row.get("address"), row.get("port")
        require(isinstance(address, str) and 1 <= len(address) <= 253 and not any(c.isspace() for c in address))
        require(type(port) is int and 1 <= port <= 65535)
        require(isinstance(row.get("protocol"), str) and 1 <= len(row["protocol"]) <= 64)
        reason = None
        try:
            outbound = xray_outbound(row)
        except ValueError:
            reason = "unsupported_resolved_outbound"
        else:
            if audit.endpoint(outbound)[1] is not None:
                reason = "unsupported_probe_outbound"
            else:
                routes[key] = {"probe": outbound}
        coverage_rows.append({"key": key, "name": name, "address": address, "port": port,
                              "disabled": disabled, "unsupportedReason": reason})
    require(bool(routes))
    if selected:
        require(set(routes) == selected)  # Missing/unsupported selected hosts fail closed.
    refreshed = now.astimezone(timezone.utc).isoformat()
    counts = {"total": len(coverage_rows), "supported": len(routes), "unsupported": len(coverage_rows) - len(routes),
              "disabled": sum(row["disabled"] is True for row in coverage_rows)}
    routes_document = {"xray": config["xray"], "sha256": config["sha256"], "control_mode": "direct_https",
                       "routes": routes, "refreshedAt": refreshed}
    coverage_document = {"format": 1, "refreshedAt": refreshed, "counts": counts, "hosts": coverage_rows}
    return routes_document, coverage_document


def refresh(audit, config, *, config_path=None, environ=None, now=None):
    output, coverage_output = validate_config(config, config_path)
    environ = os.environ if environ is None else environ
    token = environ.get(config["token_env"])
    require(isinstance(token, str) and bool(token))
    api = Remnawave(config["panel_url"], token)
    now = datetime.now(timezone.utc) if now is None else now
    require(isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None)
    user = api._request("GET", "/users/by-username/Admin")
    account(user, now)
    short = user.get("shortUuid")
    require(isinstance(short, str) and SHORT_UUID.fullmatch(short) is not None)
    raw = api._request("GET", "/subscriptions/by-short-uuid/" + short + "/raw?withDisabledHosts=true")
    routes, coverage = compile_inventory(audit, config, raw, now, user)
    # Complete validation precedes either write. Routes are replaced last so a
    # failed coverage write cannot publish an unaccompanied new route inventory.
    monitor.save_snapshot(coverage_output, coverage)
    monitor.save_snapshot(output, routes)
    return {"refreshedAt": routes["refreshedAt"], "counts": coverage["counts"]}


def main(argv=None):
    parser = ProbeArguments(add_help=False, exit_on_error=False, description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
        audit = load_audit_helper()
        config = audit.protected_json(args.config)
        result = refresh(audit, config, config_path=args.config)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except (Exception, KeyboardInterrupt):
        print("Private route refresh did not confirm a new inventory.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
