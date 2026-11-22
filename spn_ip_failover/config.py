"""Explicit inventory of production addresses managed by the controller."""

from dataclasses import dataclass
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from uuid import UUID

from .dns import DNSRecord
from .regru_dns import RegRuRecord
from .probes import Probe, command


def ipv4(value, *, allow_example=False):
    if not isinstance(value, str):
        raise ValueError("Требуется публичный IPv4")
    address = ipaddress.IPv4Address(value)
    if allow_example and address in ipaddress.IPv4Network("192.0.2.0/24"):
        return str(address)
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise ValueError("Требуется публичный IPv4")
    return str(address)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,160}", value):
        raise ValueError("Неверный идентификатор в конфигурации")
    return value


def uuid(value):
    parsed = UUID(str(value))
    if str(parsed) != value:
        raise ValueError("Нужен UUID в каноническом формате")
    return value


def probe_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ValueError("Неверный ключ профиля проверки")
    return value


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Число должно быть от {minimum} до {maximum}")
    return value


def boolean(value):
    if type(value) is not bool:
        raise ValueError("Требуется true или false")
    return value


def dns_name(value):
    value = identifier(value).lower().rstrip(".")
    if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
        raise ValueError("Требуется DNS-имя")
    return value


@dataclass(frozen=True)
class Host:
    uuid: str
    key: str
    port: int
    inbound: str
    name: str | None
    visibility: bool

    def address(self, current_ip):
        return self.name or current_ip


@dataclass(frozen=True)
class Server:
    key: str
    provider: str | None
    server_id: str
    initial_ip: str
    initial_ref: str
    release_initial: bool
    node: str | None
    node_name: str | None
    hosts: tuple[Host, ...]
    dns: tuple[DNSRecord | RegRuRecord, ...]
    fingerprint: str

    def node_address(self, address):
        return self.node_name or address


@dataclass(frozen=True)
class Config:
    panel_url: str
    panel_token_env: str
    dns_token_env: str
    providers: dict
    probes: tuple[Probe, ...]
    servers: tuple[Server, ...]
    max_attempts: int
    max_ips_per_server: int
    fail_rounds: int
    recover_rounds: int
    interval: int
    verify_seconds: int
    retire_seconds: int


def load_config(path, *, allow_example=False):
    data = json.loads(Path(path).read_text())
    probes = tuple(Probe(identifier(p["name"]), p["region"], command(p["command"])) for p in data["probes"])
    if (not 2 <= len(probes) <= 12 or len({p.name for p in probes}) != len(probes)
            or len({p.command for p in probes}) != len(probes)
            or any(p.region not in ("ru", "outside") for p in probes)
            or not any(p.region == "ru" for p in probes) or not any(p.region == "outside" for p in probes)):
        raise ValueError("Нужны разные команды для проверки из РФ и контрольной проверки извне")
    providers = {identifier(name): {op: list(command(argv)) for op, argv in operations.items()}
                 for name, operations in data.get("providers", {}).items()}
    servers = []
    for row in data["servers"]:
        provider = row.get("provider")
        if provider is not None and provider not in providers:
            raise ValueError("Неизвестный адаптер хостинга")
        hosts = tuple(Host(
            uuid(h["uuid"]), probe_key(h["key"]), integer(h["port"], 1, 65535), uuid(h["inbound_uuid"]),
            dns_name(h["name"]) if h.get("name") else None, boolean(h.get("manage_visibility", provider is not None)),
        ) for h in row["hosts"])
        dns_records = []
        for d in row.get("dns", []):
            kind = d.get("provider", "cloudflare")
            record_type = {"cloudflare": DNSRecord, "regru": RegRuRecord}.get(kind)
            if record_type is None:
                raise ValueError("Неизвестный оператор DNS")
            dns_records.append(record_type(identifier(d["zone_id"]), identifier(d["record_id"]), dns_name(d["name"])))
        dns = tuple(dns_records)
        node = uuid(row["node_uuid"]) if row.get("node_uuid") else None
        node_name = dns_name(row["node_name"]) if row.get("node_name") else None
        if not hosts or (node_name and not node):
            raise ValueError("У сервера должны быть хосты, а DNS-имя ноды требует UUID")
        if provider is not None:
            names = {h.name for h in hosts if h.name} | ({node_name} if node_name else set())
            record_names = {d.name for d in dns}
            if not names <= record_names or len(record_names) != len(dns):
                raise ValueError("Для каждого DNS-имени управляемого сервера нужна ровно одна A-запись")
        elif dns:
            raise ValueError("В режиме наблюдения DNS-записи не нужны")
        fingerprint = hashlib.sha256(json.dumps(
            {"server": row, "provider": ({key: argv for key, argv in providers[provider].items()
                                          if key not in ("inventory", "retire")}
                                        if provider else None)}, sort_keys=True,
        ).encode()).hexdigest()
        release_initial = boolean(row.get("release_initial", False))
        initial_ref = row.get("initial_allocation_id")
        if provider is not None and initial_ref is None:
            raise ValueError("Для замены IP нужен идентификатор исходного адреса у адаптера")
        servers.append(Server(identifier(row["id"]), provider, identifier(row.get("server_id", row["id"])),
                              ipv4(row["initial_ip"], allow_example=allow_example),
                              identifier(initial_ref or "unmanaged"), release_initial,
                              node, node_name, hosts, dns, fingerprint))
    if not servers:
        raise ValueError("Добавь хотя бы один сервер")
    for group in (
        [s.key for s in servers], [s.initial_ip for s in servers], [s.node for s in servers if s.node],
        [(s.provider, s.server_id) for s in servers if s.provider],
        [h.uuid for s in servers for h in s.hosts], [h.key for s in servers for h in s.hosts],
        [(d.zone_id, d.record_id) for s in servers for d in s.dns],
    ):
        if len(set(group)) != len(group):
            raise ValueError("В инвентаре повторяются адреса или идентификаторы")
    return Config(data["remnawave"]["url"], identifier(data["remnawave"].get("token_env", "REMNAWAVE_API_TOKEN")),
                  identifier(data.get("dns_token_env", "CLOUDFLARE_API_TOKEN")), providers, probes, tuple(servers),
                  integer(data.get("max_attempts", 10), 1, 10), integer(data.get("max_ips_per_server", 10), 2, 10),
                  integer(data.get("fail_rounds", 3), 2, 60),
                  integer(data.get("recover_rounds", 2), 2, 60), integer(data.get("interval_seconds", 120), 15, 3600),
                  integer(data.get("verify_seconds", 900), 60, 86400),
                  integer(data.get("retire_seconds", 3600), 300, 604800))
