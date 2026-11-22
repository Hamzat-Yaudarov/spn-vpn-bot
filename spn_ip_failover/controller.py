"""Coordinate monitoring, replacement and recovery using an on-disk journal."""

import re
import time
from uuid import uuid4

from .provider import Allocation, ProviderNotSubmitted
from .state import JournalError


class Conflict(RuntimeError):
    """The live inventory or an external operation needs reconciliation."""


def allocation(value):
    return Allocation(value["id"], value["ip"])


class Controller:
    def __init__(self, config, panel, dns, providers, journal, emit, *, now=time.time):
        self.config = config
        self.panel = panel
        self.dns = dns
        self.providers = providers
        self.journal = journal
        self.emit = emit
        self.now = now

    def state(self, server):
        rows = self.journal.data["servers"]
        if server.key not in rows:
            rows[server.key] = {
                "fingerprint": server.fingerprint, "tries": 0, "regional_by_host": {}, "healthy": 0,
                "current": {"id": server.initial_ref, "ip": server.initial_ip},
                "releasable": server.release_initial, "pending": None, "retired": [], "hosts": {},
            }
        row = rows[server.key]
        if row["fingerprint"] != server.fingerprint:
            raise Conflict("Инвентарь сервера изменился. Сверь его с журналом перед продолжением")
        row.setdefault("rejected_history", [])
        return row

    def inventory(self, server, allowed_ips, *, check_dns=True):
        expected = set(allowed_ips)
        node = None
        if server.node:
            node = self.panel.read_node(server.node)
            if node["address"] not in {server.node_address(ip) for ip in expected}:
                raise Conflict("Адрес ноды отличается от инвентаря")
        records = {}
        for host in server.hosts:
            record = self.panel.read_host(host.uuid)
            if (record["address"] not in {host.address(ip) for ip in expected}
                    or record["port"] != host.port
                    or (record.get("inbound") or {}).get("configProfileInboundUuid") != host.inbound):
                raise Conflict(f"Привязка хоста {host.key} отличается от инвентаря")
            records[host.uuid] = record
        for record in server.dns if check_dns else ():
            if self.dns.read_ipv4(record) not in expected:
                raise Conflict(f"Адрес в DNS для {record.name} отличается от инвентаря")
        return node, records

    def publication_preflight(self, server, before, after, *, expected_allocations=None):
        declared = None
        if server.provider is not None:
            try:
                declared = tuple(expected_allocations) if expected_allocations is not None else ()
            except TypeError:
                raise Conflict("Для публикации нужны два точных назначения IPv4 из журнала") from None
            if (len(declared) != 2 or not all(isinstance(entry, Allocation) for entry in declared)
                    or len({entry.ref for entry in declared}) != 2
                    or len({entry.ip for entry in declared}) != 2
                    or {entry.ip for entry in declared} != {before, after}):
                raise Conflict("Назначения для публикации не совпадают с точной парой IPv4")
        # A journalled REG.RU change can temporarily expose the two owned IPs.
        # Resume only that existing transaction after validating panel references.
        self.inventory(server, (before, after), check_dns=False)
        self.assert_no_unmanaged_refs(server, before)
        self.assert_no_unmanaged_refs(server, after)
        if declared is not None:
            provider = self.providers.get(server.provider)
            if provider is None:
                raise Conflict("Для публикации не подключён адаптер хостинга")
            try:
                assigned = set(provider.inventory(server.server_id))
            except (TypeError, ValueError):
                raise Conflict("Хостинг вернул неоднозначные назначения перед публикацией") from None
            if not set(declared) <= assigned:
                raise Conflict("Хостинг не подтверждает оба точных назначения перед публикацией")
        resume = getattr(self.dns, "resume_ipv4", None)
        if callable(resume):
            for record in server.dns:
                resume(record, {before, after})
        self.inventory(server, (before, after))

    def publication(self, server, before, after, *, expected_allocations=None):
        self.publication_preflight(server, before, after, expected_allocations=expected_allocations)
        if server.node and not server.node_name:
            self.panel.replace_node_address(server.node, before, after)
        for host in server.hosts:
            if not host.name:
                self.panel.replace_host_address(host.uuid, before, after)
        for record in server.dns:
            self.dns.replace_ipv4(record, before, after)
        self.inventory(server, (after,))

    def report(self, server, node, results, row=None):
        self.emit({
            "server": server.key, "node_connected": node.get("isConnected") if node else None,
            "node_disabled": node.get("isDisabled") if node else None,
            "hosts": {host.key: results[host.uuid] for host in server.hosts},
            **({"attempts": row["tries"]} if row is not None else {}),
        })

    def measure(self, server, probe, ip, *, pinned=False, verify_dns=False):
        return {host.uuid: probe.test(host, ip if pinned else host.address(ip),
                                     ip if verify_dns else None) for host in server.hosts}

    def observe(self, server, probe, ip=None):
        current = ip or server.initial_ip
        node, _ = self.inventory(server, (current,))
        self.report(server, node, self.measure(server, probe, current, verify_dns=bool(server.provider)))

    def visibility(self, server, row, records, results, *, may_hide, hide_outages=False):
        for host in server.hosts:
            if not host.visibility:
                continue
            stats = row["hosts"].setdefault(host.uuid, {"bad": 0, "good": 0, "hidden": False})
            status = results[host.uuid]
            stats["bad"] = stats["bad"] + 1 if status in ("down", "ru_down") else 0
            stats["outage_bad"] = stats.get("outage_bad", 0) + 1 if status == "down" else 0
            stats["good"] = stats["good"] + 1 if status == "healthy" else 0
            record = records[host.uuid]
            endpoint = {"expected_address": record["address"], "expected_port": host.port,
                        "expected_inbound": host.inbound}
            if stats.get("hide_pending") and record["isDisabled"]:
                stats.update(hidden=True, hide_pending=False)
                self.journal.save()
            elif stats.get("hide_pending") and stats["bad"] < self.config.fail_rounds:
                stats["hide_pending"] = False
            hide_confirmed = ((may_hide and stats["bad"] >= self.config.fail_rounds)
                              or (hide_outages and stats["outage_bad"] >= self.config.fail_rounds))
            if hide_confirmed and not record["isDisabled"]:
                stats["hide_pending"] = True
                self.journal.save()
                changed = self.panel.set_host_disabled(host.uuid, False, True, **endpoint)
                stats.update(hidden=changed, hide_pending=False)
                self.journal.save()
                if changed:
                    self.emit({"server": server.key, "host": host.key, "event": "host_disabled"})
            elif stats["good"] >= self.config.recover_rounds and stats["hidden"]:
                self.panel.set_host_disabled(host.uuid, True, False, **endpoint)
                stats["hidden"] = False
                self.journal.save()
                self.emit({"server": server.key, "host": host.key, "event": "host_restored"})

    def retire(self, server, row, provider, *, healthy):
        for entry in list(row["retired"]):
            unused = entry.get("rolled_back") or entry.get("rejected")
            if self.now() < entry["after"] or (not healthy and not unused):
                continue
            self.inventory(server, (row["current"]["ip"],))
            self.assert_no_unmanaged_refs(server, entry["ip"], retain_configured=False)
            current = None if unused else allocation(row["current"])
            self.release(server, provider, entry, current=current)
            row["retired"].remove(entry)
            self.journal.save()
            self.emit({"server": server.key, "event": "previous_allocation_released"})

    @staticmethod
    def release(server, provider, entry, *, current=None):
        removed = allocation(entry)
        if current is None:
            provider.release(server.server_id, removed)
        else:
            provider.retire(server.server_id, removed, current)
        assigned = set(provider.inventory(server.server_id))
        if removed in assigned or (current is not None and current not in assigned):
            raise Conflict("Хостинг не подтвердил итоговый список IPv4")

    def assert_no_unmanaged_refs(self, server, address, *, retain_configured=True):
        hosts = {host.uuid for host in server.hosts} if retain_configured else set()
        node = server.node if retain_configured else None
        names = {address}
        if retain_configured:
            names.update(host.name for host in server.hosts if host.name)
            if server.node_name:
                names.add(server.node_name)
        for resource in self.panel.list_hosts() + self.panel.list_nodes():
            if resource["uuid"] in hosts or resource["uuid"] == node:
                continue
            values = {value.rstrip(".") for value in re.split(r"[,;\s]+", resource["address"].lower())}
            if values & names:
                raise Conflict("Тот же публичный адрес указан вне управляемого инвентаря Remnawave")

    def cycle(self, server, probe):
        row = self.state(server)
        provider = self.providers.get(server.provider)
        if row["pending"] is not None:
            return self.advance(server, row, provider, probe)

        current = row["current"]["ip"]
        node, records = self.inventory(server, (current,))
        results = self.measure(server, probe, current, verify_dns=provider is not None)
        self.report(server, node, results, row)
        healthy = all(value == "healthy" for value in results.values())
        row["healthy"] = row["healthy"] + 1 if healthy else 0
        regional = row.setdefault("regional_by_host", {})
        for uid, verdict in results.items():
            regional[uid] = regional.get(uid, 0) + 1 if verdict == "ru_down" else 0
        # Recovery must not replenish purchase permission. Otherwise a flapping
        # address could trigger an unlimited sequence of ten-order incidents.
        # Attempts remain charged across restarts and healthy cycles until an
        # operator explicitly resets this node's limit.
        self.visibility(server, row, records, results, may_hide=provider is None, hide_outages=provider is not None)
        self.journal.save()
        if not provider:
            return
        self.retire(server, row, provider, healthy=row["healthy"] >= self.config.recover_rounds)
        if row.get("exhausted") or (node and node["isDisabled"]):
            return
        affected = [uid for uid, count in regional.items() if count >= self.config.fail_rounds]
        if not affected:
            return
        self.assert_no_unmanaged_refs(server, current)
        provider.check(server.server_id)
        row["pending"] = {"phase": "seek", "affected": affected, "rejected": []}
        self.journal.save()
        return self.advance(server, row, provider, probe)

    def assigned(self, server, row, provider, rejected):
        actual = set(provider.inventory(server.server_id))
        owned = {allocation(row["current"]), *(allocation(item) for item in rejected)}
        if not owned <= actual:
            raise Conflict("Инвентарь хостинга не подтверждает текущий IP и все удерживаемые адреса")
        return actual

    def seek(self, server, row, provider, probe, pending):
        current = row["current"]["ip"]
        self.inventory(server, (current,))
        self.retire(server, row, provider, healthy=False)
        actual = self.assigned(server, row, provider, pending["rejected"])
        for entry in pending["rejected"]:
            if entry["ip"] not in row["rejected_history"]:
                row["rejected_history"].append(entry["ip"])
        if row["tries"] >= self.config.max_attempts:
            self.phase(pending, "exhausted")
            return True
        if len(actual) >= self.config.max_ips_per_server:
            if pending["rejected"]:
                # The provider's ten simultaneous IPv4 slots are separate from
                # the user's ten PURCHASE attempts. Free only a journalled,
                # failed candidate, not the original/current or a foreign IP.
                pending["release_failed"] = {
                    "allocation": pending["rejected"][0].copy(),
                    "before": [entry.as_payload() for entry in sorted(actual, key=lambda item: item.ref)],
                }
                self.phase(pending, "release_failed")
                self.release_failed_slot(server, row, provider, pending)
                return False
            self.phase(pending, "exhausted")
            return True
        if pending["rejected"] or row["rejected_history"] or pending.get("budget_recheck"):
            results = self.measure(server, probe, current, verify_dns=True)
            if all(verdict == "healthy" for verdict in results.values()):
                row["retired"].extend({**entry, "after": self.now(), "rejected": True}
                                      for entry in pending["rejected"])
                row["pending"] = None
                self.journal.save()
                self.retire(server, row, provider, healthy=True)
                return False
            if not any(results[uid] == "ru_down" for uid in pending["affected"]):
                return False
            actual = self.assigned(server, row, provider, pending["rejected"])
            if len(actual) >= self.config.max_ips_per_server:
                self.phase(pending, "exhausted")
                return True
        request_id = str(uuid4())
        pending.update(phase="allocating", request_id=request_id,
                       before=[entry.as_payload() for entry in sorted(actual, key=lambda entry: entry.ref)])
        pending.pop("budget_recheck", None)
        row["tries"] += 1
        self.journal.save()
        self.emit({"server": server.key, "event": "allocation_requested",
                   "attempts": row["tries"], "request_id": request_id})
        try:
            candidate = provider.allocate(server.server_id, request_id)
        except ProviderNotSubmitted:
            # Only this typed, exact provider result proves that NO paid order
            # was dispatched. All other failures remain unknown/allocating.
            row["tries"] -= 1
            pending.update(phase="budget_wait", retry_after=self.now() + 3600, budget_recheck=True)
            pending.pop("request_id")
            pending.pop("before")
            self.journal.save()
            self.emit({"server": server.key, "event": "purchase_budget_blocked", "retry_after": pending["retry_after"]})
            return False
        pending["candidate"] = candidate.as_payload()
        pending["phase"] = "prepare"
        if self.allocation_conflict(server, row, candidate) or any(
            candidate.ip == entry.ip or candidate.ref == entry.ref for entry in actual
        ):
            pending["phase"] = "allocation_conflict"
        elif candidate.ip in row["rejected_history"]:
            # A released address can be reissued. It still consumed an order,
            # but repeating its VPN test is not evidence of a new candidate.
            pending["phase"] = "discard"
        self.journal.save()
        return True

    def release_failed_slot(self, server, row, provider, pending):
        operation = pending["release_failed"]
        target = allocation(operation["allocation"])
        before = {allocation(entry) for entry in operation["before"]}
        after = before - {target}
        if (target == allocation(row["current"]) or target not in before
                or operation["allocation"] not in pending["rejected"]):
            raise Conflict("Освобождаемый слот не принадлежит отклонённому кандидату")
        self.inventory(server, (row["current"]["ip"],))
        self.assert_no_unmanaged_refs(server, target.ip, retain_configured=False)
        actual = set(provider.inventory(server.server_id))
        if actual not in (before, after):
            raise Conflict("Назначения изменились во время освобождения слота IPv4")
        # Repeat the *idempotent adapter operation* even if billing already
        # removed the address: the gateway may still need to finish the guest
        # deprovision step after a lost reply or a guest-side failure. The
        # adapter must not repeat its UI delete when the IP is absent.
        self.release(server, provider, operation["allocation"])
        if set(provider.inventory(server.server_id)) != after:
            raise Conflict("Освобождение слота IPv4 не подтверждено")
        if target.ip not in row["rejected_history"]:
            row["rejected_history"].append(target.ip)
        pending["rejected"].remove(operation["allocation"])
        pending.pop("release_failed")
        resume = pending.pop("release_resume_phase", "seek")
        if resume not in ("seek", "budget_wait"):
            raise Conflict("Неверная фаза после освобождения неудачного кандидата")
        self.phase(pending, resume)
        self.emit({"server": server.key, "event": "failed_candidate_slot_released", "ip": target.ip})

    def allocation_conflict(self, server, row, candidate):
        if candidate.ref == row["current"]["id"]:
            return True
        for key, other in self.journal.data["servers"].items():
            if candidate.ip == other["current"]["ip"]:
                return True
            if any(candidate.ip == entry["ip"] or (key == server.key and candidate.ref == entry["id"])
                   for entry in other["retired"]):
                return True
            pending = other["pending"] or {}
            active_candidate = pending.get("candidate")
            if key != server.key and active_candidate and candidate.ip == active_candidate["ip"]:
                return True
            if any(candidate.ip == entry["ip"] or (key == server.key and candidate.ref == entry["id"])
                   for entry in pending.get("rejected", [])):
                return True
        return any(other.key != server.key and candidate.ip == other.initial_ip
                   for other in self.config.servers)

    def advance(self, server, row, provider, probe):
        pending = row["pending"]
        pending.setdefault("affected", [host.uuid for host in server.hosts])
        pending.setdefault("rejected", [])
        if pending["phase"] == "release_failed":
            self.release_failed_slot(server, row, provider, pending)
            return
        if pending["phase"] == "budget_wait":
            # Failed addresses must not keep accruing renewal cost while the
            # account budget is blocked. Each removal has the same durable
            # before/after inventory guard as slot reclamation.
            while pending["rejected"]:
                actual = self.assigned(server, row, provider, pending["rejected"])
                pending["release_failed"] = {
                    "allocation": pending["rejected"][0].copy(),
                    "before": [entry.as_payload() for entry in sorted(actual, key=lambda item: item.ref)],
                }
                pending["release_resume_phase"] = "budget_wait"
                self.phase(pending, "release_failed")
                self.release_failed_slot(server, row, provider, pending)
            current = row["current"]["ip"]
            node, _ = self.inventory(server, (current,))
            results = self.measure(server, probe, current, verify_dns=True)
            self.report(server, node, results, row)
            if all(verdict == "healthy" for verdict in results.values()):
                row["pending"] = None
                self.journal.save()
                return
            if self.now() < pending["retry_after"] or not any(
                results[uid] == "ru_down" for uid in pending["affected"]
            ):
                return
            pending.pop("retry_after")
            self.phase(pending, "seek")
        if pending["phase"] == "seek" and not self.seek(server, row, provider, probe, pending):
            return
        phase = pending["phase"]
        if phase == "exhausted":
            self.exhaust(server, row, provider, probe, pending)
            return
        if phase == "allocating":
            raise Conflict(f"Покупка не подтверждена. Сверь запрос {pending['request_id']} у хостера")
        if phase == "allocation_conflict":
            raise Conflict("Хостер вернул адрес или идентификатор из действующих назначений")

        current, candidate = allocation(row["current"]), allocation(pending["candidate"])
        if phase == "prepare":
            self.inventory(server, (current.ip,))
            self.assert_no_unmanaged_refs(server, candidate.ip, retain_configured=False)
            provider.prepare(server.server_id, candidate)
            self.assigned(server, row, provider, [*pending["rejected"], candidate.as_payload()])
            pending["deadline"] = self.now() + self.config.verify_seconds
            self.phase(pending, "test")
            phase = "test"
        if phase == "test":
            self.inventory(server, (current.ip,))
            result = self.measure(server, probe, candidate.ip, pinned=True)
            self.emit({"server": server.key, "event": "candidate_test", "hosts": result})
            if all(value == "healthy" for value in result.values()):
                pending["deadline"] = self.now() + self.config.verify_seconds
                self.phase(pending, "promote")
                phase = "promote"
            elif (all(value in ("healthy", "ru_down", "partial_ru") for value in result.values())
                  or (self.now() >= pending["deadline"] and "unknown" not in result.values())):
                self.reject(pending, candidate, row)
                return True
            else:
                return
        if phase == "promote":
            try:
                self.publication_preflight(server, current.ip, candidate.ip,
                                           expected_allocations=(current, candidate))
                self.assert_no_unmanaged_refs(server, current.ip)
                self.assert_no_unmanaged_refs(server, candidate.ip)
                provider.activate(server.server_id, current, candidate)
                self.publication(server, current.ip, candidate.ip, expected_allocations=(current, candidate))
            except JournalError:
                raise
            except (RuntimeError, OSError):
                if self.now() >= pending["deadline"]:
                    self.phase(pending, "undo")
                raise
            pending["deadline"] = self.now() + self.config.verify_seconds
            pending["passes"] = 0
            self.phase(pending, "verify")
            return
        if phase == "verify":
            try:
                node, _ = self.inventory(server, (candidate.ip,))
            except (RuntimeError, OSError):
                if self.now() >= pending["deadline"]:
                    self.phase(pending, "undo")
                raise
            results = self.measure(server, probe, candidate.ip, verify_dns=True)
            self.report(server, node, results, row)
            healthy = all(value == "healthy" for value in results.values())
            if healthy and (not node or (node["isConnected"] and not node["isDisabled"])):
                pending["passes"] += 1
                if pending["passes"] >= self.config.recover_rounds:
                    self.commit(server, row, candidate, pending["rejected"])
                    self.retire(server, row, provider, healthy=True)
                    return
            else:
                pending["passes"] = 0
            self.journal.save()
            if self.now() < pending["deadline"]:
                return
            self.phase(pending, "undo")
            phase = "undo"
        if phase == "undo":
            self.publication(server, candidate.ip, current.ip, expected_allocations=(candidate, current))
            provider.activate(server.server_id, candidate, current)
            row["retired"].append({**candidate.as_payload(), "after": self.now() + self.config.retire_seconds,
                                   "rolled_back": True})
            if candidate.ip not in row["rejected_history"]:
                row["rejected_history"].append(candidate.ip)
            pending.pop("candidate")
            self.phase(pending, "seek")
            self.emit({"server": server.key, "event": "replacement_rolled_back"})
            return
        if phase == "discard":
            self.reject(pending, candidate, row)
            return True

    def reject(self, pending, candidate, row):
        pending["rejected"].append(candidate.as_payload())
        if candidate.ip not in row["rejected_history"]:
            row["rejected_history"].append(candidate.ip)
        pending.pop("candidate")
        self.phase(pending, "seek")

    def exhaust(self, server, row, provider, probe, pending):
        current = row["current"]["ip"]
        node, records = self.inventory(server, (current,))
        for entry in list(pending["rejected"]):
            self.assert_no_unmanaged_refs(server, entry["ip"], retain_configured=False)
            self.release(server, provider, entry)
            pending["rejected"].remove(entry)
            self.journal.save()
            self.emit({"server": server.key, "event": "candidate_released"})
        results = self.measure(server, probe, current, verify_dns=True)
        self.report(server, node, results, row)
        if any(results[uid] == "unknown" for uid in pending["affected"]):
            return
        self.visibility(server, row, records, {
            uid: verdict if uid in pending["affected"] else "unknown" for uid, verdict in results.items()
        }, may_hide=True)
        row["pending"] = None
        row["exhausted"] = True
        self.journal.save()
        self.emit({"server": server.key, "event": "attempt_limit_reached", "attempts": row["tries"]})

    def phase(self, pending, phase):
        pending["phase"] = phase
        self.journal.save()

    def commit(self, server, row, candidate, rejected):
        row["retired"].extend({**entry, "after": self.now(), "rejected": True} for entry in rejected)
        if row["releasable"]:
            row["retired"].append({**row["current"], "after": self.now() + self.config.retire_seconds})
        row.update(current=candidate.as_payload(), pending=None, releasable=True, regional_by_host={}, healthy=0)
        self.journal.save()
        self.emit({"server": server.key, "event": "replacement_verified", "new_ip": candidate.ip})

    def reconcile(self, server, request_id, candidate):
        row = self.state(server)
        pending = row["pending"]
        if not pending or pending["phase"] != "allocating" or pending["request_id"] != request_id:
            raise Conflict("В журнале нет такого незавершённого запроса покупки")
        if candidate is None:
            pending.pop("request_id")
            self.phase(pending, "seek")
        else:
            if self.allocation_conflict(server, row, candidate) or any(
                entry["id"] == candidate.ref or entry["ip"] == candidate.ip for entry in pending.get("before", [])
            ):
                raise Conflict("Адрес или идентификатор уже используются")
            pending.update(candidate=candidate.as_payload(),
                           phase="discard" if candidate.ip in row["rejected_history"] else "prepare")
        self.journal.save()

    def reset_attempts(self, server):
        """Explicit operator action; never called by monitoring or recovery."""
        row = self.state(server)
        if row["pending"] is not None or row["retired"]:
            raise Conflict("Сначала сверить и завершить незавершённые покупки и освобождение адресов сервера")
        row.setdefault("limit_resets", []).append({"at": self.now(), "previous_tries": row["tries"]})
        row.update(tries=0, exhausted=False, regional_by_host={}, healthy=0)
        self.journal.save()
        self.emit({"server": server.key, "event": "purchase_limit_manually_reset"})
