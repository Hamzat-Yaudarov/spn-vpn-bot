"""Controller-side interface for independent VPN probes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class Probe:
    name: str
    region: str
    command: tuple[str, ...]

    def run(self, payload):
        try:
            result = subprocess.run(
                self.command, input=json.dumps(payload).encode(), stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=55, check=False,
                env={key: os.environ[key] for key in ("PATH", "HOME", "USER", "SSH_AUTH_SOCK", "LANG", "CREDENTIALS_DIRECTORY")
                     if key in os.environ},
            )
            if result.returncode or len(result.stdout) > 256:
                return "unknown"
            response = json.loads(result.stdout)
            if isinstance(response, dict) and set(response) == {"status"}:
                if response["status"] in ("pass", "down", "unknown"):
                    return response["status"]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        return "unknown"


def command(value):
    if (not isinstance(value, list) or not value
            or any(not isinstance(item, str) or not item or "\x00" in item for item in value)
            or not Path(value[0]).is_absolute()):
        raise ValueError("Команда должна быть массивом аргументов с абсолютным путём")
    return tuple(value)


class ProbeRound:
    def __init__(self, probes):
        self.probes = probes
        self.controls = {}

    @staticmethod
    def _send(probes, payload):
        if not probes:
            return {}
        with ThreadPoolExecutor(max_workers=len(probes)) as executor:
            statuses = executor.map(lambda probe: probe.run(payload), probes)
            return {probe.name: status for probe, status in zip(probes, statuses)}

    def test(self, host, address, expected_ip=None):
        if host.key not in self.controls:
            self.controls[host.key] = self._send(self.probes, {"action": "control", "key": host.key})
        available = [probe for probe in self.probes if self.controls[host.key][probe.name] == "pass"]
        payload = {
            "action": "probe", "key": host.key, "address": address, "port": host.port,
        }
        if expected_ip is not None and host.name:
            payload["expected_ip"] = expected_ip
        observed = self._send(available, payload)
        ru = [observed.get(probe.name, "unknown") for probe in self.probes if probe.region == "ru"]
        outside = [observed.get(probe.name, "unknown") for probe in self.probes if probe.region == "outside"]
        if all(value == "pass" for value in ru + outside):
            return "healthy"
        if all(value == "down" for value in ru):
            if all(value == "pass" for value in outside):
                return "ru_down"
            if all(value == "down" for value in outside):
                return "down"
        if all(value in ("pass", "down") for value in ru) and all(value == "pass" for value in outside):
            return "partial_ru"
        return "unknown"
