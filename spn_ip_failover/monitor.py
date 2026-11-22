"""Collect an authenticated VPN health snapshot without changing infrastructure."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from .worker import ROUTE_KEY, load_audit_helper


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def outbound_key(outbound):
    value = deepcopy(outbound)
    value.pop("tag", None)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def status(result):
    if result.get("status") == "passed":
        return "pass"
    if result.get("status") == "failed" and result.get("reason") != "unexpected_https_status":
        return "down"
    return "unknown"


def collect(audit, config):
    mode = config.get("control_mode", "peer_vpn")
    if mode not in ("peer_vpn", "direct_https"):
        raise ValueError("Invalid monitor control mode")
    routes = config.get("routes")
    if not isinstance(routes, dict) or not 1 <= len(routes) <= 512:
        raise ValueError("A bounded route inventory is required")
    engine = Path(config["xray"])
    audit.pinned_engine(engine, config["sha256"])
    started = timestamp()
    direct_control = object()
    jobs = {direct_control: None} if mode == "direct_https" else {}
    mapping = {}
    for key, route in routes.items():
        if not isinstance(key, str) or not ROUTE_KEY.fullmatch(key) or not isinstance(route, dict):
            raise ValueError("Invalid monitor route")
        probe, control = route.get("probe"), route.get("control")
        probe_info, probe_error = audit.endpoint(probe)
        probe_key = None
        control_key = direct_control if mode == "direct_https" else None
        if not probe_error:
            probe_key = outbound_key(probe)
            jobs[probe_key] = probe
            if mode == "peer_vpn":
                control_info, control_error = audit.endpoint(control)
                same_method = not control_error and all(
                    probe_info[field] == control_info[field] for field in ("protocol", "network", "security")
                )
                independent = same_method and any(
                    probe_info[field] != control_info[field] for field in ("address", "port")
                )
                if independent:
                    control_key = outbound_key(control)
                    jobs[control_key] = control
        mapping[key] = probe_key, control_key

    def test(item):
        key, outbound = item
        try:
            with tempfile.TemporaryDirectory(prefix="spn-monitor-") as directory:
                if key is direct_control:
                    result = audit.test_one(engine, None, Path(directory), 0, direct_https=True)
                else:
                    result = audit.test_one(engine, outbound, Path(directory), 0)
            return key, status(result)
        except Exception:
            return key, "unknown"

    with ThreadPoolExecutor(max_workers=3) as executor:
        tested = dict(executor.map(test, jobs.items()))
    records = {key: {"status": tested.get(probe, "unknown"), "control": tested.get(control, "unknown")}
               for key, (probe, control) in mapping.items()}
    return {"format": 1, "startedAt": started, "completedAt": timestamp(), "routes": records,
            "uniqueTests": len(jobs),
            "counts": {value: sum(row["status"] == value for row in records.values())
                       for value in ("pass", "down", "unknown")}}


def save_snapshot(output, value):
    output = Path(output)
    if not output.is_absolute() or any(path.is_symlink() for path in (output, *output.parents)):
        raise ValueError("Snapshot needs an absolute private path")
    parent = output.parent.stat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise ValueError("Snapshot directory must be owned and private")
    if output.exists():
        existing = output.stat()
        if (not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.geteuid()
                or stat.S_IMODE(existing.st_mode) != 0o600):
            raise ValueError("Existing snapshot must be an owned private file")
    descriptor, name = tempfile.mkstemp(prefix=".health-", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, output)
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        audit = load_audit_helper()
        config = audit.protected_json(args.config)
        report = collect(audit, config)
        save_snapshot(args.output, report)
        print(json.dumps({"completedAt": report["completedAt"], "counts": report["counts"]}))
        return 0
    except (Exception, KeyboardInterrupt):
        print("VPN monitor did not confirm a new snapshot.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
