#!/usr/bin/env python3
"""One owner-authorized cleanup, not an automatic allocation-ownership claim."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from spn_ip_failover.onecent import Settings, browser
from spn_ip_failover.onecent_fields import parse_api_addresses

SID = "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d"
SERVICE_NAME = "vds56962.1cent.network"
TARGET = "191.217.252.174"
ORIGIN = "https://bill.1cent.host"
EXPECTED = {"87.251.16.241": True, "191.217.252.35": False, TARGET: False}
REMAINING = {ip: primary for ip, primary in EXPECTED.items() if ip != TARGET}
STATE = ROOT / ".local-state/ip-rotation-20261115"
DISPATCH = STATE / "cleanup-191.217.252.174-dispatch.json"
RECEIPT = STATE / "cleanup-191.217.252.174-receipt.json"
RETRY_DISPATCH = STATE / "cleanup-191.217.252.174-idempotent-dispatch.json"
RETRY_RECEIPT = STATE / "cleanup-191.217.252.174-idempotent-receipt.json"


def create(path, value):
    meta = STATE.lstat()
    if STATE.is_symlink() or meta.st_uid != os.getuid() or meta.st_mode & 0o777 != 0o700:
        raise ValueError("Private operator journal is not ready")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    fd = os.open(STATE, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_before(data):
    rows = parse_api_addresses(data)
    if {row.ip: row.primary for row in rows} != EXPECTED:
        raise ValueError("Protected existing addresses changed")
    mapping = {row["address"]: row["id"] for row in data["addresses"]}
    if (mapping != {"87.251.16.241": 0, "191.217.252.35": 1, TARGET: 2}
            or any(type(row["id"]) is not int for row in data["addresses"])
            or next(row for row in data["addresses"] if row["address"] == TARGET)["removable"] is not True):
        raise ValueError("Observed DELETE index no longer identifies the authorized extra IP")


def retry_allowed(dispatch, receipt):
    expected_url = ORIGIN + "/api/services/" + SID + "/ipv4/2"
    if (dispatch.get("service_id") != SID or dispatch.get("target") != TARGET
            or dispatch.get("method") != "DELETE" or dispatch.get("url") != expected_url
            or dispatch.get("before") != EXPECTED or dispatch.get("preserve") != REMAINING
            or receipt.get("target") != TARGET or receipt.get("http_status") != 400
            or receipt.get("url_matches") is not True):
        raise ValueError("The original cleanup outcome is not a definitive pre-execution rejection")
    error = json.loads(base64.b64decode(receipt["body_base64"], validate=True))
    if error != {"message": "Заголовок Idempotency-Key обязателен для этой операции",
                 "error": "Bad Request", "statusCode": 400}:
        raise ValueError("No correction retry for an unknown or different provider failure")


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(stream.fileno())
        if meta.st_uid != os.getuid() or meta.st_mode & 0o777 != 0o600 or meta.st_nlink != 1:
            raise ValueError("Private cleanup evidence changed")
        raw = stream.read(131073)
        if not 0 < len(raw) <= 131072:
            raise ValueError("Cleanup evidence is oversized")
        return json.loads(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-owner-authorized-cleanup", action="store_true")
    parser.add_argument("--retry-known-missing-idempotency-key", action="store_true")
    args = parser.parse_args()
    if not args.execute_owner_authorized_cleanup:
        print(json.dumps({"plan_only": True, "target": TARGET, "preserve": list(REMAINING), "network_used": False}))
        return 0
    dispatch_path, receipt_path = DISPATCH, RECEIPT
    if args.retry_known_missing_idempotency_key:
        retry_allowed(private_json(DISPATCH), private_json(RECEIPT))
        dispatch_path, receipt_path = RETRY_DISPATCH, RETRY_RECEIPT
    if dispatch_path.exists() or dispatch_path.is_symlink() or receipt_path.exists() or receipt_path.is_symlink():
        raise ValueError("Cleanup already dispatched; read state, never repeat DELETE")
    settings = Settings.load(ROOT / ".local-state/ip-failover-ru-20261113/onecent-check.private.json")
    report = {"target": TARGET, "removed": False, "delete_attempts": 0, "automatic_purchase_enabled": False}
    with browser(settings) as page:
        # No incidental page writes/authentication. The one explicit API DELETE
        # below uses the already authenticated cookie jar and observed URL.
        page.context.route("**/*", lambda route: route.continue_()
                           if route.request.method in ("GET", "HEAD", "OPTIONS") else route.abort())
        api = page.context.request
        service = api.get(ORIGIN + "/api/services/" + SID, max_redirects=0, timeout=20000)
        try:
            value = service.json()
            if (service.status != 200 or value.get("id") != SID
                    or value.get("name") != SERVICE_NAME
                    or value.get("pendingOperation") is not None or value.get("migrating") is not False):
                raise ValueError("Authorized hosting service identity changed")
        finally:
            service.dispose()
        url = ORIGIN + "/api/services/" + SID + "/ipv4/addresses"
        def inventory():
            response = api.get(url, max_redirects=0, timeout=20000)
            try:
                if response.status != 200 or response.url != url:
                    raise ValueError("Inventory unavailable")
                return response.json()
            finally:
                response.dispose()
        validate_before(inventory()); time.sleep(0.5); validate_before(inventory())
        delete_url = ORIGIN + "/api/services/" + SID + "/ipv4/2"
        key = str(uuid.uuid4())
        create(dispatch_path, {"service_id": SID, "target": TARGET, "method": "DELETE", "url": delete_url,
                          "request_body": None, "before": EXPECTED, "preserve": REMAINING,
                          "idempotency_key": key,
                          "known_rejection_correction": args.retry_known_missing_idempotency_key,
                          "authorization": "owner explicitly authorized removal of exactly 191.217.252.174",
                          "at": datetime.now(timezone.utc).isoformat()})
        report["delete_attempts"] = 1
        try:
            response = api.delete(delete_url, max_redirects=0, max_retries=0, timeout=20000,
                                  headers={"Origin": ORIGIN, "Referer": ORIGIN + "/dashboard/services/" + SID,
                                           "Idempotency-Key": key})
            try:
                raw = response.body()
                record = {"target": TARGET, "http_status": response.status, "url_matches": response.url == delete_url}
                if len(raw) <= 65536:
                    record["body_base64"] = base64.b64encode(raw).decode()
                create(receipt_path, record)
                report["http_status"] = response.status
            finally:
                response.dispose()
        except Exception as exc:
            report["response_error_type"] = type(exc).__name__
        # Reads may reconcile an uncertain response; they never repeat DELETE.
        matches = 0
        for _ in range(6):
            try:
                current = {row.ip: row.primary for row in parse_api_addresses(inventory())}
                if current == REMAINING:
                    matches += 1
                    if matches == 2:
                        report["removed"] = True
                        break
                elif current == EXPECTED:
                    matches = 0
                else:
                    report["unexpected_inventory"] = True
                    break
            except Exception:
                matches = 0
            time.sleep(2)
    print(json.dumps(report))
    return 0 if report["removed"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"stopped": True, "error_type": type(exc).__name__, "repeat_delete": False,
                          "error_location": [Path(frame.filename).name + ":" + str(frame.lineno)
                                             for frame in traceback.extract_tb(exc.__traceback__)[-3:]]}))
        raise SystemExit(1)
