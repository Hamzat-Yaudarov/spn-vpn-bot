#!/usr/bin/env python3
"""Pinned setup for the five explicitly approved helpers; never buys or moves IPs."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import sys

import build_sweden_agreement as agreement
import deploy_managed_access as setup
import install_guest_keys as keys
import stage_guest

SWEDEN = agreement.SERVICE


def record(name, value):
    setup.access.private_write(setup.STATE / name, setup.encoded(value))


def sweden(guest):
    output = setup.STATE / "sweden-manual-20261116"
    # Guest.__enter__ already refreshed exact hosting identity + singleton .179.
    proof = json.loads(setup.rf_run(shlex.join(["python3", "-c", agreement.RF_READ]), timeout=100))
    known, _alias = setup.access.trust(guest.row, output)
    lines = {p[2] for line in known.read_text().splitlines()
             if len(p := line.split()) == 3 and p[1] == "ssh-ed25519"}
    assert len(lines) == 1
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(next(iter(lines)), validate=True)).digest()).decode().rstrip("=")
    document = {"format": 1, "service_id": SWEDEN, "old_ip": agreement.OLD, "new_ip": agreement.NEW,
                "observed_at": datetime.now(timezone.utc).isoformat(), "user_confirmed_manual_change": True,
                "provider": {"service_id": SWEDEN, "name": "srv-mwfa434s", "primary_ip": agreement.NEW,
                             "assigned_ipv4": [agreement.NEW]},
                "ssh": {"trusted_alias": "177.3.250.75", "fingerprint": fingerprint}, **proof}
    raw = setup.encoded(document)
    sha = hashlib.sha256(raw).hexdigest()
    name = "manual-agreement-" + sha + ".json"
    setup.access.private_write(output / ("agreement-" + sha[:16] + ".json"), raw)
    guest.stage({"reconcile_sweden_epoch.py", "enroll_netplan.py", "guest_enrollment.py"})
    guest.save(name, raw)
    base = ["python3", "-B", setup.PREFIX + "/reconcile_sweden_epoch.py"]
    common = ["--old-ip", agreement.OLD, "--new-ip", agreement.NEW, "--agreement", setup.PREFIX + "/" + name,
              "--agreement-sha256", sha, "--package-root", setup.PREFIX]
    frozen = output / "sweden-captured-epoch.json"
    if not frozen.exists():
        capture = guest.run(shlex.join(base + ["capture"] + common), timeout=180)
        setup.access.private_write(frozen, capture)
    capture = frozen.read_bytes()
    guest.save("manual-snapshot.json", capture)
    args = common + ["--snapshot", setup.PREFIX + "/manual-snapshot.json", "--snapshot-sha256", hashlib.sha256(capture).hexdigest()]
    plan = json.loads(guest.run(shlex.join(base + ["plan"] + args), timeout=180))
    print(json.dumps({"sweden_plan": plan}), flush=True)
    result = json.loads(guest.run(shlex.join(base + ["apply"] + args), timeout=180))
    record("sweden-gemini.manual-reconciled.json", result)
    print(json.dumps({"sweden_reconciled": result}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sweden-only", action="store_true")
    parser.add_argument("--reverse-only", action="store_true")
    parser.add_argument("--exclude-sweden", action="store_true")
    parser.add_argument("--eu-relays-only", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"plan_only": True, "network_used": False, "paid_orders": 0}))
        return
    assert not (args.sweden_only and args.reverse_only)
    relay_keys = setup.relay_prepare() if args.eu_relays_only else None
    public = json.loads(setup.rf_run(shlex.join(["python3", "-c", keys.PUBLIC_READ])))
    if not args.sweden_only:
        setup.rf_stage()
    with setup.browser() as page:
        page.route("**/*", lambda r: r.continue_() if r.request.method in ("GET", "HEAD", "OPTIONS") else r.abort())
        rows = [row for row in setup.access.scope.SELECTED if (not args.sweden_only or row["service_id"] == SWEDEN)
                and not (args.exclude_sweden and row["service_id"] == SWEDEN)]
        for row in rows:
            if args.eu_relays_only and row["service_id"] not in relay_keys:
                continue
            print(json.dumps({"opening_scoped_guest": row["key"]}), flush=True)
            with setup.Guest(page, row) as guest:
                if args.eu_relays_only:
                    print(json.dumps({"eu_relay": setup.relay_install(guest, relay_keys[row["service_id"]])}), flush=True)
                    continue
                if row["service_id"] == SWEDEN and not args.reverse_only:
                    sweden(guest)
                    request = {"service_id": SWEDEN, "public_key": public[SWEDEN],
                               "rpc_sha256": hashlib.sha256(stage_guest.RPC).hexdigest(), "apply": True}
                    key_result = json.loads(guest.run(shlex.join(["python3", "-c", keys.REMOTE]), setup.encoded(request)))
                    assert key_result.get("ok") is True
                    record("sweden-gemini.key-installed.json", {"ok": True, "service_id": SWEDEN, "network_changed": False})
                    print(json.dumps({"restricted_key": key_result}), flush=True)
                if not args.sweden_only:
                    print(json.dumps({"reverse_management": setup.reverse_install(guest, public[row["service_id"]])}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print("Approved management setup stopped: " + message, file=sys.stderr)
        raise SystemExit(1)
