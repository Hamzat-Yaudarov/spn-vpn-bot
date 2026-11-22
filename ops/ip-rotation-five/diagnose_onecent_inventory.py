#!/usr/bin/env python3
"""Read only selected service API shape/status, never credentials or raw bodies."""
import argparse
from ipaddress import IPv4Address
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, "/opt/wayspn")
from spn_ip_failover.onecent import Settings, browser
from spn_ip_failover.onecent_ui import OneCentPage, ORIGIN


ALLOWED = {"f5c0287a-636e-4981-95c7-80b24c9559c0", "21e508f0-2ef2-47da-aee7-15dc8c66eb15",
           "9b0316d5-fa83-4bf8-b6dc-8202b66622c9", "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d",
           "4d6f358a-e75f-4dd8-a635-c6ad320f2732"}


def shape(value):
    if not isinstance(value, dict):
        return {"json_type": type(value).__name__}
    result = {"root_keys": sorted(value), "operation_is_null": value.get("operation") is None,
              "operation_type": type(value.get("operation")).__name__,
              "order_is_null": isinstance(value.get("orderFlow"), dict)
              and value["orderFlow"].get("order") is None}
    rows = value.get("addresses")
    if isinstance(rows, list) and len(rows) <= 16:
        result["rows"] = []
        for row in rows:
            if not isinstance(row, dict):
                result["rows"].append({"type": type(row).__name__})
                continue
            entry = {"keys": sorted(row), "types": {k: type(v).__name__ for k, v in row.items()}}
            for key in ("main", "releasing", "removable"):
                if type(row.get(key)) is bool:
                    entry[key] = row[key]
            try:
                ip = IPv4Address(row.get("address"))
                if ip.is_global:
                    entry["address"] = str(ip)
            except (ValueError, TypeError):
                pass
            result["rows"].append(entry)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--service", required=True, choices=sorted(ALLOWED))
    args = p.parse_args()
    settings = Settings.load("/etc/spn-ip-failover/onecent.json")
    with browser(settings) as page:
        page.route("**/*", lambda r: r.continue_() if r.request.method in ("GET", "HEAD", "OPTIONS") else r.abort())
        ui = OneCentPage(page, args.service, settings.servers[args.service].name)
        ui.open()  # No automatic login and no credential reads.
        url = ORIGIN + "/api/services/" + args.service + "/ipv4/addresses"
        response = page.context.request.get(url, max_redirects=0, timeout=10000)
        try:
            report = {"service": args.service, "http_status": response.status,
                      "url_matches": response.url == url, "credentials_read": False, "paid_requests": 0}
            raw = response.body()
            if len(raw) <= 1000000:
                try:
                    report.update(shape(json.loads(raw)))
                except (ValueError, TypeError):
                    report["json_parsed"] = False
            print(json.dumps(report, ensure_ascii=False))
        finally:
            response.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "paid_requests": 0}))
        raise SystemExit(1)
