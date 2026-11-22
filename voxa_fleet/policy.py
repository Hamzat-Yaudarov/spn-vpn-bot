"""Conservative health policy. SSH is diagnostic, never purchase evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import time


def _timestamp(value):
    if isinstance(value, datetime):
        return value.timestamp() if value.tzinfo is not None else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except ValueError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    return None


def _result(verdict, reason, *, observed_at=None):
    return {"verdict": verdict, "eligible": verdict == "ru_unreachable", "reason": reason, "observed_at": observed_at}


def classify_health(probes, *, now=None, max_age_seconds=90):
    """Classify one round (or the newest round in a history).

    A round contains observed_at, controls_ok, and nonempty rf/outside lists of
    {vpn_ok: bool, controls_ok: bool, observed_at: timestamp}. A probe may inherit
    the round's timestamp and controls_ok. Missing evidence is unknown, not good.
    """
    now = _timestamp(now) if now is not None else time.time()
    if now is None or not isinstance(max_age_seconds, (int, float)) or max_age_seconds <= 0:
        return _result("unknown", "invalid observation clock")
    if isinstance(probes, list):
        valid = [probe for probe in probes if isinstance(probe, dict)]
        if not valid:
            return _result("unknown", "no probe rounds")
        probes = max(valid, key=lambda p: _timestamp(p.get("observed_at")) or float("-inf"))
    if not isinstance(probes, dict):
        return _result("unknown", "no probe round")
    observed = _timestamp(probes.get("observed_at"))
    if observed is None or now - observed > max_age_seconds or observed > now + 5:
        return _result("unknown", "stale or invalid probe round", observed_at=observed)
    if probes.get("controls_ok") is not True:
        return _result("unknown", "control connectivity is not confirmed", observed_at=observed)
    rf, outside = probes.get("rf"), probes.get("outside")
    if not isinstance(rf, list) or not rf or not isinstance(outside, list) or not outside:
        return _result("unknown", "independent RF and outside probes are required", observed_at=observed)
    outcomes = []
    for group in (rf, outside):
        values = []
        for probe in group:
            if not isinstance(probe, dict):
                return _result("unknown", "invalid probe result", observed_at=observed)
            timestamp = _timestamp(probe.get("observed_at", observed))
            if timestamp is None or now - timestamp > max_age_seconds or timestamp > now + 5:
                return _result("unknown", "stale independent probe", observed_at=observed)
            if probe.get("controls_ok", probes["controls_ok"]) is not True:
                return _result("unknown", "independent probe controls failed", observed_at=observed)
            if type(probe.get("vpn_ok")) is not bool:
                return _result("unknown", "real VPN outcome is missing", observed_at=observed)
            values.append(probe["vpn_ok"])
        outcomes.append(values)
    russian, external = outcomes
    if all(russian) and all(external):
        return _result("healthy", "VPN works from RF and outside", observed_at=observed)
    if len(set(russian)) > 1 or len(set(external)) > 1:
        return _result("partial", "probe networks disagree", observed_at=observed)
    if not any(russian) and all(external):
        return _result("ru_unreachable", "VPN is unavailable from the RF control network but works outside", observed_at=observed)
    if not any(russian) and not any(external):
        return _result("service_down", "VPN fails from RF and outside; investigate the service", observed_at=observed)
    return _result("partial", "VPN is reachable from RF but not outside", observed_at=observed)


def evaluate_replacement(server, settings, probes, *, now=None):
    """Authorize only repeated RF-specific failure, never SSH or stale evidence.

    This is a health/mode gate, not a financial reservation or deletion permit.
    The caller must still reserve budget atomically and verify provider ownership.
    """
    now = _timestamp(now) if now is not None else time.time()
    history = probes if isinstance(probes, list) else [probes]
    history = sorted((p for p in history if isinstance(p, dict)), key=lambda p: _timestamp(p.get("observed_at")) or float("-inf"), reverse=True)
    max_age = settings.get("probe_max_age", 90)
    health = classify_health(history, now=now, max_age_seconds=max_age)
    result = {"allowed": False, "reason": health["reason"], "health": health, "consecutive_failures": 0}
    if not server.get("enabled", False):
        result["reason"] = "server monitoring is disabled"
        return result
    if server.get("mode") != "auto":
        result["reason"] = "automatic replacement is not authorized in this mode"
        return result
    if not settings.get("auto_enabled") or not settings.get("financial_enabled"):
        result["reason"] = "automatic or financial actions are disabled"
        return result
    if not settings.get("skip_ip_change_approved", False):
        result["reason"] = "skipping the undocumented IP-change step requires explicit approval"
        return result
    if health["verdict"] != "ru_unreachable":
        return result
    required = settings.get("consecutive_failures", 3)
    interval = settings.get("probe_interval", 30)
    if type(required) is not int or not 1 <= required <= 100 or not isinstance(interval, (int, float)) or interval <= 0:
        result["reason"] = "invalid confirmation policy"
        return result
    # Each historical round is assessed at its original observation time; its
    # internal probes must still be contemporaneous. Continuity is bounded by
    # the monitoring interval, and duplicate timestamps never add confirmation.
    previous = None
    count = 0
    oldest = None
    for probe in history:
        observed = _timestamp(probe.get("observed_at"))
        if observed is None or (previous is not None and (previous - observed > interval * 2 or observed >= previous)):
            break
        if classify_health(probe, now=observed, max_age_seconds=max_age)["verdict"] != "ru_unreachable":
            break
        count += 1
        previous = oldest = observed
    result["consecutive_failures"] = count
    if count < required:
        result["reason"] = "awaiting consecutive independent VPN failure rounds"
        return result
    grace = settings.get("grace_seconds", 120)
    last_change = _timestamp(server.get("metadata", {}).get("last_cutover_at", server.get("created_at")))
    if last_change is not None and now - last_change < grace:
        result["reason"] = "server is in its post-change grace period"
        return result
    result.update(allowed=True, reason="confirmed RF-control-network failure; financial and ownership gates still apply")
    return result
