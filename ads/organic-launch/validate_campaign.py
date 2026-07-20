#!/usr/bin/env python3
"""Validate the organic campaign package without external dependencies."""

from __future__ import annotations

import csv
import re
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CALENDAR = ROOT / "content-calendar.csv"
TRACKING_COMMANDS = ROOT / "tracking-commands.txt"
METRICS = ROOT / "metrics.csv"
CODE_PATTERN = re.compile(r"^(ig|tt|yt)_d\d{2}$")


def main() -> None:
    with CALENDAR.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))

    if len(rows) != 30:
        raise SystemExit(f"Expected 30 calendar rows, got {len(rows)}")

    codes: list[str] = []
    expected_start = date(2026, 7, 16)
    for index, row in enumerate(rows, start=1):
        expected_day = f"{index:02d}"
        expected_date = (expected_start + timedelta(days=index - 1)).isoformat()
        if row["day"] != expected_day:
            raise SystemExit(f"Row {index}: expected day {expected_day}, got {row['day']}")
        if row["date"] != expected_date:
            raise SystemExit(f"Day {expected_day}: expected date {expected_date}, got {row['date']}")
        for platform in ("ig", "tt", "yt"):
            code = row[f"{platform}_code"]
            if not CODE_PATTERN.fullmatch(code):
                raise SystemExit(f"Day {expected_day}: invalid tracking code {code!r}")
            if code != f"{platform}_d{expected_day}":
                raise SystemExit(f"Day {expected_day}: unexpected tracking code {code!r}")
            codes.append(code)

    if len(codes) != len(set(codes)):
        raise SystemExit("Tracking codes are not unique")

    command_text = TRACKING_COMMANDS.read_text(encoding="utf-8")
    missing = [code for code in codes if f"/new_link {code} " not in command_text]
    if missing:
        raise SystemExit(f"Missing /new_link commands: {', '.join(missing)}")

    with METRICS.open(encoding="utf-8", newline="") as source:
        metric_rows = list(csv.DictReader(source))
    metric_codes = [row["tracking_code"] for row in metric_rows]
    if len(metric_rows) != 90:
        raise SystemExit(f"Expected 90 metrics rows, got {len(metric_rows)}")
    if metric_codes != codes:
        raise SystemExit("Metrics rows do not match calendar tracking codes")

    ready_asset = (ROOT / rows[0]["asset"]).resolve()
    if not ready_asset.is_file():
        raise SystemExit(f"Day 01 asset does not exist: {ready_asset}")

    print(
        f"OK: {len(rows)} days, {len(codes)} unique tracking codes, "
        "90 metrics rows, day 01 asset exists"
    )


if __name__ == "__main__":
    main()
