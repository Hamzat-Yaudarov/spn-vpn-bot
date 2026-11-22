#!/usr/bin/env python3
"""Read private pilot evidence; print structure, public IPv4s and no secrets."""
import base64
import hashlib
from ipaddress import IPv4Address
import json
from pathlib import Path
import re

from pilot_onecent import CAPTURE_LIMIT, PILOT_DIRECTORY, controlled_file
from spn_ip_failover.provider import _unique_object


def schema(value, path="$", *, depth=0, output=None):
    output = [] if output is None else output
    if depth > 10 or len(output) >= 150:
        return output
    item = {"path": path, "type": type(value).__name__}
    output.append(item)
    if isinstance(value, dict):
        item["keys"] = [key for key in list(value)[:40]
                        if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key)]
        for key, child in list(value.items())[:40]:
            if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key):
                schema(child, path + "." + key, depth=depth + 1, output=output)
    elif isinstance(value, list):
        item["length"] = len(value)
        for index, child in enumerate(value[:12]):
            schema(child, path + "[" + str(index) + "]", depth=depth + 1, output=output)
    elif isinstance(value, str):
        item["length"] = len(value)
        try:
            address = IPv4Address(value)
            if address.is_global and str(address) == value:
                item["public_ipv4"] = value
        except ValueError:
            pass
        if path.rsplit(".", 1)[-1] in ("status", "state", "code") and re.fullmatch(r"[A-Za-z0-9_-]{1,60}", value):
            item["value"] = value
        if path == "$.amount" and re.fullmatch(r"[0-9]{1,8}\.[0-9]{2}", value):
            item["value"] = value
    elif type(value) is bool or value is None:
        item["value"] = value
    return output


def inspect():
    report = {"files": sorted(p.name for p in PILOT_DIRECTORY.iterdir()), "read_only": True}
    path = PILOT_DIRECTORY / "response.json"
    if not path.exists():
        report["response_present"] = False
        return report
    raw = controlled_file(path, CAPTURE_LIMIT * 2, own_private=True)
    receipt = json.loads(raw, object_pairs_hook=_unique_object)
    report.update(response_present=True, response_artifact_sha256=hashlib.sha256(raw).hexdigest(),
                  http_status=receipt["http_status"], service_id=receipt["service_id"],
                  request_key=receipt["request_key"])
    if "body_base64" not in receipt:
        report["body_omitted"] = True
        return report
    body = base64.b64decode(receipt["body_base64"], validate=True)
    if len(body) > CAPTURE_LIMIT or hashlib.sha256(body).hexdigest() != receipt["body_sha256"]:
        raise ValueError("Invalid private response body")
    report["body_schema"] = schema(json.loads(body, object_pairs_hook=_unique_object))
    return report


if __name__ == "__main__":
    try:
        print(json.dumps(inspect(), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"read_only": True, "error_type": type(exc).__name__}))
        raise SystemExit(1)
