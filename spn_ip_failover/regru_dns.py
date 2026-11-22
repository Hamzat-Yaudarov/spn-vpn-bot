"""Scoped REG.API 2 A-record updates with a durable, restartable journal.

Only the documented client methods get_resource_records, add_alias and
remove_record are used. The new address is added and read back before the old
address is removed. No live API request is made by importing this module.

Official method parameters:
https://www.reg.ru/reseller/api2doc#zone_get_resource_records
https://www.reg.ru/reseller/api2doc#zone_add_alias
https://www.reg.ru/reseller/api2doc#zone_remove_record
"""

from dataclasses import dataclass
from ipaddress import IPv4Address
import json
import math
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .dns import DNSConflict, DNSUpdateError, _RejectRedirects
from .state import Journal


ENDPOINT = "https://api.reg.ru/api/regru2/"
MAX_RESPONSE = 2_000_000
FUNCTIONS = {"zone/get_resource_records", "zone/add_alias", "zone/remove_record"}


def hostname(value):
    if not isinstance(value, str):
        raise ValueError("REG.RU needs a DNS name")
    try:
        name = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("REG.RU needs a DNS name") from None
    if (not 1 <= len(name) <= 253 or any(not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in name.split("."))):
        raise ValueError("REG.RU needs an explicit DNS name without wildcards")
    return name


@dataclass(frozen=True)
class RegRuRecord:
    zone_id: str
    record_id: str
    name: str

    def __post_init__(self):
        domain, name = hostname(self.zone_id), hostname(self.name)
        if "." not in domain or (name != domain and not name.endswith("." + domain)):
            raise ValueError("The REG.RU record must belong to its configured zone")
        subdomain = "@" if name == domain else name[:-(len(domain) + 1)]
        if not isinstance(self.record_id, str) or self.record_id != subdomain:
            raise ValueError("The REG.RU record ID must identify its exact subdomain")
        object.__setattr__(self, "zone_id", domain)
        object.__setattr__(self, "name", name)

    @property
    def domain(self):
        return self.zone_id

    @property
    def subdomain(self):
        return self.record_id


def configured_record(record):
    if isinstance(record, RegRuRecord):
        return record
    try:
        return RegRuRecord(record.zone_id, record.record_id, record.name)
    except AttributeError:
        raise ValueError("A REG.RU record needs zone_id, record_id and name") from None


class RegRuRejected(DNSUpdateError):
    """REG.API explicitly rejected a request, rather than losing its response."""


class RegRuDNS:
    def __init__(self, username, password, *, journal_path=None, timeout=15, opener=None):
        if any(not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n")
               for value in (username, password)):
            raise ValueError("REG.RU API credentials are required")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("REG.RU timeout must be positive")
        if journal_path is not None and not Path(journal_path).is_absolute():
            raise ValueError("REG.RU changes need an absolute private journal path")
        self._username, self._password = username, password
        self._journal = Path(journal_path) if journal_path is not None else None
        self._timeout = timeout
        self._open = opener if opener is not None else build_opener(_RejectRedirects(), ProxyHandler({})).open

    def _request(self, function, record, **fields):
        if function not in FUNCTIONS:
            raise ValueError("Unsupported REG.RU DNS operation")
        payload = {"username": self._username, "password": self._password,
                   "domains": [{"dname": record.domain}], **fields}
        request = Request(ENDPOINT + function, method="POST", data=urlencode({
            "input_format": "json", "output_format": "json", "input_data": json.dumps(payload),
        }).encode(), headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self._open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("Response exceeds the size limit")
            value = json.loads(raw)
        except HTTPError as exc:
            exc.close()
            raise DNSUpdateError(f"REG.RU DNS request failed (HTTP {exc.code})") from None
        except (OSError, ValueError):
            raise DNSUpdateError("REG.RU DNS response was not confirmed") from None
        if not isinstance(value, dict):
            raise DNSUpdateError("REG.RU returned an unexpected DNS response")
        if value.get("result") == "error":
            raise RegRuRejected("REG.RU rejected the DNS request")
        domains = value.get("answer", {}).get("domains") if isinstance(value.get("answer"), dict) else None
        if value.get("result") != "success" or not isinstance(domains, list) or len(domains) != 1:
            raise DNSUpdateError("REG.RU did not identify the requested DNS zone")
        result = domains[0]
        if (not isinstance(result, dict) or not isinstance(result.get("dname"), str)
                or result["dname"].rstrip(".").lower() != record.domain):
            raise DNSConflict("REG.RU returned a different DNS zone")
        if result.get("result") == "error":
            raise RegRuRejected("REG.RU rejected the DNS zone request")
        if result.get("result") != "success":
            raise DNSUpdateError("REG.RU did not confirm the DNS zone request")
        return result

    def _addresses(self, record):
        rows = self._request("zone/get_resource_records", record).get("rrs")
        if not isinstance(rows, list):
            raise DNSUpdateError("REG.RU did not return DNS resource records")
        addresses = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("subname"), str):
                raise DNSUpdateError("REG.RU returned an invalid DNS record")
            if row["subname"].rstrip(".").lower() != record.subdomain:
                continue
            kind = row.get("rectype")
            if kind in ("AAAA", "CNAME"):
                raise DNSConflict(f"REG.RU {record.name} has an alternate address or alias")
            if kind == "A":
                try:
                    addresses.append(str(IPv4Address(row["content"])))
                except (ValueError, KeyError, TypeError):
                    raise DNSUpdateError("REG.RU returned invalid IPv4 content") from None
        if not addresses or len(set(addresses)) != len(addresses):
            raise DNSConflict(f"REG.RU {record.name} has no unique A-record inventory")
        return frozenset(addresses)

    def read_ipv4(self, record):
        record = configured_record(record)
        addresses = self._addresses(record)
        if len(addresses) != 1:
            raise DNSConflict(f"REG.RU {record.name} must have one A record")
        return next(iter(addresses))

    def resume_ipv4(self, record, allowed_ips):
        """Finish only this record's saved operation using the two owned IPv4s."""
        record = configured_record(record)
        if self._journal is None:
            return False
        with Journal(self._journal) as journal:
            entry = journal.data["servers"].get(record.name)
            if entry is None:
                return False
            if not isinstance(entry, dict):
                raise DNSConflict("REG.RU DNS journal has an invalid change entry")
            if entry.get("phase") == "complete":
                return False
            try:
                allowed = tuple(allowed_ips)
                pending = (entry["expected"], entry["replacement"])
                if (len(allowed) != 2 or any(not isinstance(value, str) for value in (*allowed, *pending))):
                    raise ValueError("Two IPv4 strings are required")
                allowed = {str(IPv4Address(value)) for value in allowed}
                expected, replacement = (str(IPv4Address(value)) for value in pending)
                if len(allowed) != 2 or expected == replacement:
                    raise ValueError("The IPv4 pair must be distinct")
            except (KeyError, TypeError, ValueError):
                raise DNSConflict("REG.RU pending DNS change has an invalid IPv4 pair") from None
            if entry.get("domain") != record.domain or {expected, replacement} != allowed:
                raise DNSConflict("REG.RU pending DNS change is outside the owned IPv4 pair or zone")
            # Keep this lock while resuming, so another writer cannot finish a
            # rollback and cause resume to start a new forward transaction.
            self._replace_locked(record, expected, replacement, journal, resume_only=True)
            return True

    def replace_ipv4(self, record, expected, replacement):
        record = configured_record(record)
        expected, replacement = str(IPv4Address(expected)), str(IPv4Address(replacement))
        if self._journal is None:
            if self.read_ipv4(record) == replacement:
                return False
            raise DNSUpdateError("REG.RU changes require a configured private journal")
        with Journal(self._journal) as journal:
            return self._replace_locked(record, expected, replacement, journal)

    def _replace_locked(self, record, expected, replacement, journal, *, resume_only=False):
        addresses = self._addresses(record)
        entry = journal.data["servers"].get(record.name)
        if entry is not None and not isinstance(entry, dict):
            raise DNSConflict("REG.RU DNS journal has an invalid change entry")
        identity = {"domain": record.domain, "expected": expected, "replacement": replacement}
        matches = isinstance(entry, dict) and all(entry.get(key) == value for key, value in identity.items())
        if resume_only and (not matches or entry.get("phase") == "complete"):
            raise DNSConflict("REG.RU resume cannot create a new DNS transaction")
        if matches and entry.get("phase") not in ("prepared", "adding", "added", "removing", "complete"):
            raise DNSConflict("REG.RU DNS journal has an unexpected change state")
        if addresses == {replacement}:
            if matches and entry.get("phase") != "complete":
                entry["phase"] = "complete"
                journal.save()
            return False
        if entry and entry.get("phase") != "complete" and not matches:
            raise DNSConflict("REG.RU already has a different unfinished DNS change")
        if not matches or entry.get("phase") == "complete":
            if addresses != {expected}:
                raise DNSConflict(f"REG.RU {record.name} differs from the expected A record")
            entry = {**identity, "phase": "prepared"}
            journal.data["servers"][record.name] = entry
            journal.save()
        phase = entry.get("phase")
        if phase == "prepared":
            if addresses != {expected}:
                raise DNSConflict("REG.RU changed before the new A record was added")
            entry["phase"] = "adding"
            journal.save()
            try:
                self._request("zone/add_alias", record, subdomain=record.subdomain, ipaddr=replacement)
            except RegRuRejected:
                entry["phase"] = "prepared"
                journal.save()
                raise
            entry["phase"] = "added"
            journal.save()
            addresses = self._addresses(record)
        elif phase == "adding":
            if addresses == {expected}:
                raise DNSUpdateError("REG.RU has not confirmed the previous A-record addition")
            if addresses != {expected, replacement}:
                raise DNSConflict("REG.RU did not confirm the previously added A record")
            entry["phase"] = "added"
            journal.save()
        elif phase not in ("added", "removing"):
            raise DNSConflict("REG.RU DNS journal has an unexpected change state")
        if addresses != {expected, replacement}:
            raise DNSConflict("REG.RU did not confirm exactly the old and new A records")
        entry["phase"] = "removing"
        journal.save()
        try:
            self._request("zone/remove_record", record, subdomain=record.subdomain,
                          record_type="A", content=expected)
        except RegRuRejected:
            entry["phase"] = "added"
            journal.save()
            raise
        if self._addresses(record) != {replacement}:
            raise DNSConflict("REG.RU did not confirm the replacement A record")
        entry["phase"] = "complete"
        journal.save()
        return True
