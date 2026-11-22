"""Immutable scope of the five ordinary 1CENT VPSes; no bypass/DNS resources."""
from __future__ import annotations

DEPLOYMENT = "276abbcb4f334b58ab25d9b2adc9d55c"
STATE = "/var/lib/spn-ip-failover"
PYTHON = "/opt/wayspn/venv/bin/python"
PANEL = "https://panel.wayspn.online"
ROWS = (
    ("sweden-gemini", "7737ba07-6d54-435c-8c01-aea4fe1369b9", "5c472650-1f93-470a-abdb-0caadef059b7", "f5c0287a-636e-4981-95c7-80b24c9559c0", "srv-mwfa434s", "177.3.250.179", "5c6a10e4-6619-40a1-916f-2865ce833758", 23001),
    ("estonia-gemini", "8a26ad46-2a93-49c0-995d-81014fbe8fce", "6caf57c3-e03d-48ac-80e5-c51ba2fbd62a", "21e508f0-2ef2-47da-aee7-15dc8c66eb15", "vds100269.1cent.network", "185.252.177.203", "94ccbada-dab9-409f-a951-c919fc768c52", 23002),
    ("finland-gemini", "1db3bc12-a131-45da-8100-85344ede076d", "b7db14dd-d587-42d3-862e-ff784b33e375", "9b0316d5-fa83-4bf8-b6dc-8202b66622c9", "vds97813.1cent.network", "87.251.16.245", "4a68b143-45fc-4122-87e7-6ca482d0d856", 23003),
    ("finland-gemini-2", "f07a16c5-5111-49d0-9801-f728135aa528", "9f63b074-8b8a-4655-a2b8-02177a7c3c0f", "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d", "vds56962.1cent.network", "87.251.16.241", "5318ab2e-eb57-4dbf-b563-d7d28d19ca3a", 23004),
    ("germany-gemini", "c8274c47-23a4-47bb-b8a4-f3e5dcbac609", "f87b14cf-438a-4c61-acc1-7522491c05ae", "4d6f358a-e75f-4dd8-a635-c6ad320f2732", "vds104020.1cent.network", "177.3.248.30", "afeca9cb-e481-4777-af34-4dd1ad46ac5b", 23005),
)
FIELDS = ("key", "host_uuid", "node_uuid", "service_id", "service_name", "ip", "inbound_uuid", "reverse_port")
SERVERS = tuple(dict(zip(FIELDS, row)) for row in ROWS)


def guest_ssh(row):
    service = row["service_id"]
    return ["/usr/bin/ssh", "-4", "-F", "/dev/null", "-T", "-o", "HostName=127.0.0.1",
            "-p", str(row["reverse_port"]), "-l", "root", "-o", "HostKeyAlias=" + service,
            "-i", STATE + "/.ssh/guest_" + service + "_ed25519", "-o",
            "UserKnownHostsFile=" + STATE + "/.ssh/guest_known_hosts", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes",
            "-o", "PermitLocalCommand=no", "{address}"]


def configs(panel_url=PANEL):
    """Produce repeatable installation bytes, never read tokens or do I/O."""
    from .remna import Remnawave
    Remnawave(panel_url, "configuration-validation-only")
    gateway = [PYTHON, "-B", "-m", "spn_ip_failover.provider_gateway", "--config", STATE + "/provider-gateway.json"]
    billing = [PYTHON, "-B", "-m", "spn_ip_failover.onecent", "--config", "/etc/spn-ip-failover/onecent.json"]
    rotation = {
        "remnawave": {"url": panel_url, "token_env": "REMNAWAVE_API_TOKEN"},
        "max_attempts": 10, "max_ips_per_server": 10, "fail_rounds": 3, "recover_rounds": 2,
        "interval_seconds": 120, "verify_seconds": 900, "retire_seconds": 3600,
        "probes": [
            {"name": "ru-local", "region": "ru", "command": [PYTHON, "-B", "-m", "spn_ip_failover.worker", "--config", STATE + "/routes.json"]},
            {"name": "outside-eu", "region": "outside", "command": [PYTHON, "-B", "-m", "spn_ip_failover.dynamic_relay_worker", "--config", STATE + "/routes.json", "--relay-config", STATE + "/outside-relays.json"]},
        ],
        "providers": {"onecent-five": {name: gateway for name in ("check", "inventory", "allocate", "prepare", "activate", "release", "retire")}},
        "servers": [{"id": row["key"], "provider": "onecent-five", "server_id": row["service_id"],
                     "initial_ip": row["ip"], "initial_allocation_id": row["ip"], "release_initial": True,
                     "node_uuid": row["node_uuid"], "hosts": [{"uuid": row["host_uuid"], "key": row["host_uuid"],
                      "port": 443, "inbound_uuid": row["inbound_uuid"], "manage_visibility": False}], "dns": []}
                    for row in SERVERS],
    }
    gateway_config = {"version": 1, "remnawave": rotation["remnawave"], "servers": {
        row["service_id"]: {"billing_command": billing, "guest_ssh": guest_ssh(row), "node_uuid": row["node_uuid"],
                            "supports_retire": True} for row in SERVERS}}
    outside = {"version": 1, "remnawave": {"url": panel_url, "credential": "remnawave"},
               "routes": {row["host_uuid"]: {"node_uuid": row["node_uuid"], "service_id": row["service_id"]} for row in SERVERS},
               "relays": [{"node_uuid": row["node_uuid"], "service_id": row["service_id"], "user": "spn-ip-relay",
                           "identity": STATE + "/.ssh/relay_" + row["service_id"] + "_ed25519",
                           "known_hosts": STATE + "/.ssh/relay_known_hosts", "ssh_port": 22,
                           "reverse_service": row["service_id"]} for row in SERVERS if row["reverse_port"] in (23002, 23005)]}
    return {"rotation.json": rotation, "provider-gateway.json": gateway_config, "outside-relays.json": outside}
