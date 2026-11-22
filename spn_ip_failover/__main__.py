"""Run a single round, run continuously, or inspect the failover plan."""

import argparse
import json
import os
from pathlib import Path
import sys
import time

from .config import load_config
from .controller import Controller
from .dns import CloudflareDNS
from .dns_router import DNSRouter
from .regru_dns import RegRuDNS, RegRuRecord
from .probes import ProbeRound
from .provider import Allocation, CommandProvider
from .remna import Remnawave
from .state import Journal, JournalError


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def state_path(args):
    return Path(args.state).expanduser()


def require_token(key):
    value = os.getenv(key)
    if not value:
        raise ValueError(f"Не задана переменная {key}")
    return value


def plan(config, providers):
    emit({
        "mode": "plan", "max_attempts_per_server": config.max_attempts,
        "purchase_limit_reset": "explicit-operator-only",
        "max_assigned_ipv4_per_server": config.max_ips_per_server,
        "fail_rounds": config.fail_rounds, "recover_rounds": config.recover_rounds,
        "probes": {probe.name: probe.region for probe in config.probes},
        "provider_adapters": sorted(providers),
        "servers": [{"server": server.key, "provider": server.provider,
                     "node": server.node, "hosts": [host.key for host in server.hosts],
                     "auto_visibility": [host.key for host in server.hosts if host.visibility],
                     "dns": [record.name for record in server.dns]} for server in config.servers],
    })


def execute(args, config, providers):
    if args.command == "plan":
        plan(config, providers)
        return 0
    if args.command == "status":
        emit(Journal(state_path(args)).snapshot())
        return 0
    if args.command == "reset-limit":
        if not args.apply:
            raise ValueError("Новое разрешение на покупки требует reset-limit --apply")
        server = next((server for server in config.servers if server.key == args.server), None)
        if server is None or not server.provider:
            raise ValueError("Неизвестный управляемый сервер")
        with Journal(state_path(args)) as journal:
            if server.key not in journal.data["servers"]:
                raise ValueError("Для этого сервера ещё нет журнала покупок")
            Controller(config, None, None, providers, journal, emit).reset_attempts(server)
        return 0
    if args.command == "reconcile":
        with Journal(state_path(args)) as journal:
            server = next((server for server in config.servers if server.key == args.server), None)
            if server is None or not server.provider:
                raise ValueError("Неизвестный управляемый сервер")
            if args.no_purchase:
                if args.ip or args.allocation_id:
                    raise ValueError("Выбери либо существующее назначение, либо --no-purchase")
                candidate = None
            else:
                candidate = Allocation(args.allocation_id, args.ip)
            Controller(config, None, None, providers, journal, emit).reconcile(server, args.request, candidate)
            emit({"event": "allocation_reconciled", "server": server.key})
        return 0

    if args.command == "run" and not args.apply:
        raise ValueError("Для изменений нужен run --apply. Для проверки используй check")
    panel = Remnawave(config.panel_url, require_token(config.panel_token_env))
    if args.command == "inventory":
        emit({
            "nodes": [{field: node.get(field) for field in ("uuid", "name", "address", "port", "isConnected", "isDisabled")}
                      for node in panel.list_nodes()],
            "hosts": [{field: host.get(field) for field in ("uuid", "remark", "address", "port", "isDisabled")}
                      | {"inbound_uuid": (host.get("inbound") or {}).get("configProfileInboundUuid")}
                      for host in panel.list_hosts()],
        })
        return 0
    records = [record for server in config.servers for record in server.dns]
    dns = DNSRouter(
        cloudflare=(CloudflareDNS(require_token(config.dns_token_env))
                    if any(not isinstance(record, RegRuRecord) for record in records) else None),
        regru=(RegRuDNS(require_token("REG_RU_USERNAME"), require_token("REG_RU_PASSWORD"),
                        journal_path=state_path(args).resolve() / "regru-dns")
               if any(isinstance(record, RegRuRecord) for record in records) else None),
    ) if records else None
    if args.command == "check":
        journal = Journal(state_path(args))
        journal.data = journal.snapshot()
        return round_check(Controller(config, panel, dns, providers, journal, emit), config)
    with Journal(state_path(args)) as journal:
        controller = Controller(config, panel, dns, providers, journal, emit)
        due = dict.fromkeys((server.key for server in config.servers), 0)
        while True:
            failures = False
            probe = ProbeRound(config.probes)
            for server in config.servers:
                if args.watch and time.monotonic() < due[server.key]:
                    continue
                immediate = False
                try:
                    immediate = controller.cycle(server, probe)
                except JournalError:
                    raise
                except (ValueError, RuntimeError, OSError) as exc:
                    emit({"server": server.key, "event": "error", "message": str(exc)})
                    failures = True
                due[server.key] = time.monotonic() + (1 if immediate else config.interval)
            if not args.watch:
                return int(failures)
            time.sleep(max(0, min(due.values()) - time.monotonic()))


def round_check(controller, config):
    probe = ProbeRound(config.probes)
    failures = False
    for server in config.servers:
        try:
            row = controller.state(server)
            pending = row["pending"] or {}
            current = pending["candidate"]["ip"] if pending.get("phase") == "verify" else row["current"]["ip"]
            controller.observe(server, probe, current)
        except (ValueError, RuntimeError, OSError) as exc:
            emit({"server": server.key, "event": "error", "message": str(exc)})
            failures = True
    return int(failures)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Путь к JSON-инвентарю")
    parser.add_argument("--state", default="~/.spn-ip-failover", help="Приватный каталог журнала")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "inventory", "check", "status"):
        subcommands.add_parser(name)
    run = subcommands.add_parser("run")
    run.add_argument("--apply", action="store_true")
    run.add_argument("--watch", action="store_true")
    reset = subcommands.add_parser("reset-limit", help="Явно разрешить новую серию покупок одному серверу")
    reset.add_argument("--server", required=True)
    reset.add_argument("--apply", action="store_true")
    reconcile = subcommands.add_parser("reconcile")
    reconcile.add_argument("--server", required=True)
    reconcile.add_argument("--request", required=True)
    reconcile.add_argument("--allocation-id")
    reconcile.add_argument("--ip")
    reconcile.add_argument("--no-purchase", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, allow_example=args.command in ("plan", "inventory"))
        providers = {name: CommandProvider(commands) for name, commands in config.providers.items()}
        return execute(args, config, providers)
    except (KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
