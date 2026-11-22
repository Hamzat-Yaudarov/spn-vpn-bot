"""Bounded, journal-first Voxa lifecycle orchestration.

Provider mutations are never retried on ambiguity. Executor effects require
durable intent and, after interruption, explicit recovery evidence. No network
or SSH connection is opened by importing this module.
"""
from __future__ import annotations

from decimal import Decimal
import uuid

from .policy import classify_health, evaluate_replacement
from .provider import ProviderError, parse_order_service, parse_vps
from .store import BudgetError, ConflictError, StoreError, TERMINAL_PHASES, money


class Controller:
    def __init__(self, store, provider, executor, quote_fn, worker_id="fleet-worker"):
        self.store, self.provider, self.executor = store, provider, executor
        self.quote_fn, self.worker_id = quote_fn, worker_id
        self._instance = str(uuid.uuid4())
        self._op = None

    def readiness(self):
        reasons = []
        for name in ("ready", "prepare_order", "bootstrap", "probe", "cutover", "verify", "observe", "restore"):
            if not callable(getattr(self.executor, name, None)):
                reasons.append("executor_missing_" + name)
        if not callable(self.quote_fn):
            reasons.append("catalogue_quote_unavailable")
        if not getattr(self.provider, "project_id", None):
            reasons.append("dedicated_provider_project_required")
        if not reasons:
            try:
                ready = self.executor.ready()
                if ready is not True and not (isinstance(ready, dict) and ready.get("ready") is True):
                    reasons.append("executor_not_ready")
            except Exception:
                reasons.append("executor_readiness_failed")
        return {"ready": not reasons, "reasons": reasons}

    def _now(self):
        return self.store._now()

    def _save(self, phase=None, **data):
        self._op = self.store.update_operation(self._op["id"], expected_version=self._op["version"], worker_id=self.worker_id, phase=phase, data=data)
        return self._op

    def _manual(self, reason):
        return self._save(None if self._op["phase"] in TERMINAL_PHASES else "manual-review", attention=reason)

    def _server(self):
        server = self.store.get_server(self._op["server_id"])
        if not server:
            raise StoreError("logical server no longer exists")
        return server

    @staticmethod
    def _identity_snapshot(server):
        metadata = {key: value for key, value in server.get("metadata", {}).items() if key not in {"last_cutover_at"}}
        return {"id": server["id"], "name": server["name"], "domain": server["domain"], "original_state_path": server["original_state_path"], "metadata": metadata}

    def _identity(self, expected=None):
        identity = self.provider.identity()
        project = identity.get("project") if isinstance(identity, dict) else None
        scope = getattr(self.provider, "project_id", None)
        if not isinstance(project, dict) or project.get("bound") is not True or not project.get("id"):
            raise StoreError("provider project binding is not confirmed")
        if scope not in {project.get("id"), project.get("slug")} or (expected is not None and expected != project["id"]):
            raise StoreError("provider project identity changed")
        return project["id"]

    def tick(self, operation_id=None):
        """Advance at most one bounded stage. No sleeping or background threads."""
        if not self.store.acquire_lease("fleet-controller", self._instance, seconds=900):
            return self.store.get_operation(operation_id) if operation_id else None
        try:
            self._op = self.store.claim_operation(self.worker_id, lease_seconds=900, operation_id=operation_id)
            if self._op is None:
                return self.store.get_operation(operation_id) if operation_id else None
            try:
                stage = self._op["phase"]
                if stage == "queued":
                    self._queued()
                elif stage == "reserved":
                    self._create()
                elif stage == "outcome-unknown":
                    self._recover_unknown()
                elif stage == "ordered":
                    self._ordered()
                elif stage == "provisioning":
                    self._provision()
                elif stage == "bootstrap":
                    self._bootstrap()
                elif stage == "vpn-tested":
                    self._cutover()
                elif stage == "cutover":
                    self._verify_cutover()
                elif stage in {"rejected", "cleanup-pending"}:
                    self._cleanup()
                elif stage == "committed":
                    self._retire()
                elif stage == "old-retired":
                    self._save("complete", outcome="deployed", accounting_kind="conservative_upper_bound")
                else:
                    self._manual("unsupported journal phase")
            except ConflictError:
                # A stale lease must not write over another worker's decision.
                self._op = self.store.get_operation(self._op["id"])
            except (StoreError, ValueError, KeyError, TypeError):
                self._manual("configuration, identity, budget, or evidence validation failed")
            except ProviderError as error:
                self._save(last_error_code=error.code, attention="provider read failed; no paid retry performed")
            except Exception:
                # Do not put raw transport / subprocess exceptions in the journal.
                self._manual("executor stage failed; inspect the private operation journal")
            result = self._op
            if result and result.get("worker_id") == self.worker_id:
                try:
                    result = self.store.release_operation(result["id"], worker_id=self.worker_id, expected_version=result["version"])
                except ConflictError:
                    result = self.store.get_operation(result["id"])
            return result
        finally:
            self._op = None
            self.store.release_lease("fleet-controller", self._instance)

    def _queued(self):
        server = self._server()
        action, payload = self._op["action"], self._op["payload"]
        if action == "check":
            probe = self.executor.observe(server)
            self.store.record_probe(server["id"], probe)
            self._save("complete", health=classify_health(probe, now=self._now(), max_age_seconds=self.store.get_settings()["probe_max_age"]))
            return
        if action == "cleanup":
            if payload.get("approved") is not True or not payload.get("vps_id"):
                self._manual("cleanup requires an explicit exact owned VPS selection")
                return
            gen = self.store.assert_deletable(payload["vps_id"])
            if gen["server_id"] != server["id"]:
                raise StoreError("cleanup target belongs to another logical server")
            original = self.store.get_operation(gen["operation_id"])
            project = self._identity()
            if not original or original["data"].get("project_id") != project:
                raise StoreError("cleanup target project ownership is not proven")
            self._save("cleanup-pending", vps_id=payload["vps_id"], original_operation_id=gen["operation_id"], project_id=project, identity_snapshot=self._identity_snapshot(server), retry_disabled=True)
            return
        if not self.readiness()["ready"]:
            self._manual("executor, independent probes, catalogue, or dedicated project is not ready")
            return
        settings = self.store.get_settings()
        if not settings["financial_enabled"] or not settings["terms_accepted"]:
            self._manual("financial actions require explicit configured owner authorization")
            return
        if not server["enabled"]:
            self._manual("logical server is disabled")
            return
        attempt = payload.get("attempt", 1)
        if type(attempt) is not int or not 1 <= attempt <= settings["max_attempts"]:
            self._manual("incident attempt limit exceeded")
            return
        if action == "replace":
            if not settings["skip_ip_change_approved"]:
                self._manual("the undocumented primary-IP-change step has not been explicitly waived")
                return
            probe = self.executor.observe(server)
            self.store.record_probe(server["id"], probe)
            health = classify_health(probe, now=self._now(), max_age_seconds=settings["probe_max_age"])
            if health["verdict"] == "unknown":
                self._manual("independent probe/control health is unknown; spending prohibited")
                return
            if payload.get("approved") is not True:
                gate = evaluate_replacement(server, settings, self.store.latest_probes(server["id"]), now=self._now())
                if not gate["allowed"]:
                    self._manual(gate["reason"])
                    return
        elif payload.get("approved") is not True:
            self._manual("new service requires explicit owner approval")
            return
        if not self._op["data"].get("order_body"):
            quote = self.quote_fn(server, payload)
            if not isinstance(quote, dict) or quote.get("availability_confirmed") is not True:
                raise StoreError("catalogue availability is uncertain")
            for field in ("product_id", "location", "currency"):
                if not quote.get(field) or quote[field] != server[field]:
                    raise StoreError("quote does not match the explicitly configured product/location/currency")
            if quote["currency"] != settings["currency"] or not quote.get("os_image_id"):
                raise StoreError("quote currency or OS image is not confirmed")
            quote["hourly_price"], quote["initial_reserve"] = money(quote.get("hourly_price")), money(quote.get("initial_reserve"))
            if Decimal(quote["hourly_price"]) <= 0 or Decimal(quote["initial_reserve"]) <= 0:
                raise StoreError("positive conservative pricing is required")
            if quote.get("price_uncertain") or quote.get("fees_unknown"):
                raise StoreError("all tax and minimum billing exposure must be bounded")
            options = self.executor.prepare_order(server, self._op)
            if not isinstance(options, dict) or options.get("os_image_id") != quote["os_image_id"] or not options.get("ssh_public_key") or not options.get("user_data"):
                raise StoreError("public-key / one-use enrollment configuration is incomplete")
            project_id = self._identity()
            body = {"productId": quote["product_id"], "billingCycle": "hourly", "hostname": "g-" + self._op["id"] + "." + server["domain"], "location": quote["location"], "acceptTerms": True, "configOptions": options}
            if settings["withdrawal_waiver_accepted"]:
                body["withdrawalWaiverAccepted"] = True
            self._save(order_body=body, quote=quote, project_id=project_id, identity_snapshot=self._identity_snapshot(server), previous_vps_id=server["current_vps_id"], previous_ip=server["current_ip"], previous_hourly_price=server["hourly_price"], previous_currency=server["currency"], attempt=attempt)
        balance = self.provider.balance()
        if not isinstance(balance, dict) or balance.get("currency") != settings["currency"]:
            raise BudgetError("confirmed account currency is required")
        quote = self._op["data"]["quote"]
        self.store.reserve_budget(self._op["id"], quote["initial_reserve"], quote["hourly_price"], currency=quote["currency"], balance=balance.get("balance"))
        self._save("reserved", accounting_kind="reservation")

    def _create(self):
        if self._op["data"].get("create_attempted"):
            self._save("outcome-unknown", unknown_stage="create")
            return
        cfg = self.store.get_settings()
        if not cfg["financial_enabled"] or not cfg["terms_accepted"] or not self.readiness()["ready"]:
            self._manual("financial authorization or executor readiness was revoked before submission")
            return
        self._identity(self._op["data"]["project_id"])
        self._save(create_attempted=True, create_intent_at=self._now())
        try:
            data = self.provider.create_order(self._op["data"]["order_body"], self._op["idempotency_key"])
        except ProviderError as error:
            if error.outcome_unknown:
                self.store.release_budget(self._op["id"])
                self._save("outcome-unknown", unknown_stage="create", last_error_code=error.code)
            else:
                self.store.release_budget(self._op["id"], confirmed_no_charge=True)
                self._save("failed", outcome="provider_rejected", last_error_code=error.code)
            return
        except Exception:
            self.store.release_budget(self._op["id"])
            self._save("outcome-unknown", unknown_stage="create", attention="submission outcome is unknown; reconcile the saved key")
            return
        try:
            self._accept_order(data)
        except (StoreError, ProviderError, ValueError, KeyError, TypeError):
            self.store.release_budget(self._op["id"])
            self._save("outcome-unknown", unknown_stage="create", attention="accepted response identity is incomplete; reconcile the saved key")

    def _accept_order(self, data):
        order = data.get("order") if isinstance(data, dict) else None
        if not isinstance(order, dict) or order.get("idempotency_key") != self._op["idempotency_key"]:
            raise StoreError("order does not echo the exact persisted key")
        order_id = str(uuid.UUID(order["id"]))
        if order.get("hostname", self._op["data"]["order_body"]["hostname"]) != self._op["data"]["order_body"]["hostname"]:
            raise StoreError("order hostname conflicts with the durable intent")
        known = self._op["data"].get("order_id")
        if known and order_id != known:
            raise StoreError("order identity changed")
        self._save("ordered", order_id=order_id, order_result_known=True)

    def _recover_unknown(self):
        stage = self._op["data"].get("unknown_stage")
        if stage in {"cleanup", "retire"}:
            self._save("committed" if stage == "retire" else "cleanup-pending")
            return
        if stage != "create":
            self._manual("unknown effect requires explicit executor recovery")
            return
        self._identity(self._op["data"]["project_id"])
        rows = self.provider.orders(idempotencyKey=self._op["idempotency_key"])
        matches = []
        for row in rows:
            if not isinstance(row, dict):
                raise StoreError("invalid orders inventory")
            if row.get("idempotency_key") == self._op["idempotency_key"]:
                matches.append(row)
        if len(matches) > 1:
            self._manual("multiple orders claim the saved idempotency key")
        elif len(matches) == 1:
            self._accept_order(self.provider.order(matches[0]["id"]))
        else:
            self._save(reconciliation_at=self._now(), attention="no positively correlated order yet; POST is not repeated")

    def _ordered(self):
        data = self.provider.order(self._op["data"]["order_id"])
        order = data.get("order") if isinstance(data, dict) else None
        if not isinstance(order, dict) or order.get("id") != self._op["data"]["order_id"] or order.get("idempotency_key") != self._op["idempotency_key"]:
            raise StoreError("order detail identity is not positively correlated")
        try:
            vps_id = parse_order_service(data)
        except ProviderError:
            if order.get("services") == []:
                self._save(attention="accepted order is waiting for its exact service identity")
                return
            raise
        quote = self._op["data"]["quote"]
        self.store.record_generation(self._op["id"], order["id"], vps_id, hourly_price=quote["hourly_price"], currency=quote["currency"])
        self.store.account_budget(self._op["id"], order_id=order["id"], vps_id=vps_id)
        self._save("provisioning", vps_id=vps_id, accounting_kind="conservative_upper_bound")

    def _generation(self):
        owner = self._op["data"].get("original_operation_id", self._op["id"])
        matches = [g for g in self.store.list_generations(self._op["server_id"]) if g["vps_id"] == self._op["data"].get("vps_id") and g["operation_id"] == owner]
        if len(matches) != 1:
            raise StoreError("exact owned operation generation not found")
        return matches[0]

    def _read_vps(self, vps_id):
        self._identity(self._op["data"]["project_id"])
        vps = parse_vps(self.provider.vps(vps_id))
        if vps["service_id"] != vps_id or vps["project_id"] != self._op["data"]["project_id"]:
            raise StoreError("VPS exact service/project identity changed")
        return vps

    def _provision(self):
        gen = self._generation()
        vps = self._read_vps(gen["vps_id"])
        if vps["status"] in {"provisioning_failed", "cancelled", "terminated"}:
            self._save("rejected", rejection_reason="provider provisioning failed")
            return
        if not vps["running"] or not vps["ip"]:
            self._save(last_vps_status=vps["status"], attention="waiting for running guest and assigned IPv4")
            return
        self.store.update_generation(gen["vps_id"], {"ip": vps["ip"], "status": "bootstrap"})
        self._save("bootstrap")

    def _effect(self, name, server, generation, **required):
        data = self._op["data"]
        done, started = name + "_done", name + "_started"
        if data.get(done):
            return True
        if self._identity_snapshot(server) != data.get("identity_snapshot"):
            self._manual("logical identity or external configuration changed during the operation")
            return False
        if data.get(started):
            recover = getattr(self.executor, "recover", None)
            if not callable(recover):
                self._manual("interrupted " + name + " requires executor recovery evidence")
                return False
            evidence = recover(name, server, self._op, generation)
        else:
            self._save(**{started: True, name + "_intent_at": self._now()})
            evidence = getattr(self.executor, name)(server, self._op, generation)
        if not isinstance(evidence, dict) or evidence.get("ok") is not True or any(evidence.get(key) != value for key, value in required.items()):
            self._manual(name + " lacks verified identity-preserving evidence")
            return False
        if name == "bootstrap" and evidence.get("registration") is not None:
            self._op = self.store.adopt_initial_identity(self._op["id"], evidence["registration"], worker_id=self.worker_id, expected_version=self._op["version"])
        # Only whitelisted booleans enter the public operation journal; executor
        # output may contain privileged configuration or one-use credentials.
        self._save(**{done: True, name + "_evidence": {"ok": True, **required}})
        return True

    def _bootstrap(self):
        server, gen = self._server(), self._generation()
        if not self._op["data"].get("bootstrap_done"):
            self._effect("bootstrap", server, gen, ids_preserved=True)
            return
        probe = self.executor.probe(server, gen)
        health = classify_health(probe, now=self._now(), max_age_seconds=self.store.get_settings()["probe_max_age"])
        self._save(candidate_health=health)
        if health["verdict"] == "healthy":
            self.store.update_generation(gen["vps_id"], {"status": "vpn-tested"})
            self._save("vpn-tested")
        elif health["verdict"] == "unknown":
            self._manual("candidate probe/control evidence is unknown; no publication or deletion")
        else:
            self.store.update_generation(gen["vps_id"], {"status": "rejected"})
            self._save("rejected", rejection_reason=health["verdict"])

    def _cutover(self):
        self._save("cutover")
        self._effect("cutover", self._server(), self._generation(), ids_preserved=True, dns_record_preserved=True)

    def _verification(self, server, gen):
        evidence = self.executor.verify(server, self._op, gen)
        if not isinstance(evidence, dict) or any(evidence.get(key) is not True for key in ("ok", "ids_preserved", "dns_converged")):
            return False
        return classify_health(evidence.get("round"), now=self._now(), max_age_seconds=self.store.get_settings()["probe_max_age"])["verdict"] == "healthy"

    def _verify_cutover(self):
        server, gen = self._server(), self._generation()
        if not self._op["data"].get("cutover_done"):
            self._effect("cutover", server, gen, ids_preserved=True, dns_record_preserved=True)
            return
        if not self._verification(server, gen):
            self._manual("post-DNS VPN or subscription verification failed; the only new VPS is retained")
            return
        self.store.commit_generation(gen["vps_id"])
        self.store.update_server(server["id"], {"metadata": {**server["metadata"], "last_cutover_at": self._now()}})
        self._save("committed", committed_at=self._now())

    def _cancel_owned(self, vps_id, purpose):
        self.store.assert_deletable(vps_id)
        vps = self._read_vps(vps_id)
        if vps["status"] in {"cancelled", "terminated"}:
            self.store.update_generation(vps_id, {"status": "deleted"})
            self._save(**{purpose + "_confirmed": True})
            return True
        key = purpose + "_cancel_attempted"
        if self._op["data"].get(key):
            self._save(attention="cancellation not yet confirmed; billable liability retained")
            return False
        self.store.assert_deletable(vps_id)
        self._identity(self._op["data"]["project_id"])
        self._save(**{key: True, purpose + "_target": vps_id, purpose + "_intent_at": self._now()})
        try:
            self.provider.cancel(vps_id, "Voxa Fleet " + purpose + " exact owned generation", immediate=True)
            self._save(**{purpose + "_accepted": True})
        except ProviderError as error:
            if error.outcome_unknown:
                self._save("outcome-unknown", unknown_stage=purpose, last_error_code=error.code)
            else:
                self._manual("provider rejected cancellation; liability retained")
        except Exception:
            self._save("outcome-unknown", unknown_stage=purpose)
        return False

    def _cleanup(self):
        server, gen = self._server(), self._generation()
        if self._op["phase"] == "rejected":
            self._save("cleanup-pending")
            return
        if not self._op["data"].get("restore_done"):
            self._effect("restore", server, gen, ids_preserved=True)
            return
        if not self._cancel_owned(gen["vps_id"], "cleanup"):
            return
        if self._op["action"] == "cleanup":
            self._save("complete", outcome="owned_generation_deleted")
            return
        attempt = self._op["data"].get("attempt", 1)
        cfg = self.store.get_settings()
        payload = key = None
        if not self._op["data"].get("retry_disabled") and type(attempt) is int and attempt < cfg["max_attempts"] and cfg["financial_enabled"]:
            payload = {**self._op["payload"], "attempt": attempt + 1, "parent_operation_id": self._op["id"], "root_operation_id": self._op["payload"].get("root_operation_id", self._op["id"])}
            key = "retry-" + payload["root_operation_id"] + "-" + str(attempt + 1)
        self._op, _ = self.store.finish_rejected_operation(self._op["id"], worker_id=self.worker_id, expected_version=self._op["version"], retry_payload=payload, idempotency_key=key)

    def _retire(self):
        cfg = self.store.get_settings()
        if self._now() - self._op["data"]["committed_at"] < cfg["grace_seconds"]:
            self._save(attention="replacement healthy; waiting for old-generation retirement grace")
            return
        server, gen = self._server(), self._generation()
        if not self._verification(server, gen):
            self._manual("post-grace VPN verification failed; old and new generations retained")
            return
        old = self._op["data"].get("previous_vps_id")
        if old and old != gen["vps_id"]:
            if not self._cancel_owned(old, "retire"):
                return
        self._save("old-retired")

    def observe_once(self):
        result = []
        settings = self.store.get_settings()
        for server in self.store.list_servers():
            if not server["enabled"]:
                continue
            try:
                probe = self.executor.observe(server)
                self.store.record_probe(server["id"], probe)
                health = classify_health(probe, now=self._now(), max_age_seconds=settings["probe_max_age"])
                gate = evaluate_replacement(server, settings, self.store.latest_probes(server["id"]), now=self._now())
                entry = {"server_id": server["id"], "health": health, "replacement_allowed": gate["allowed"]}
                if gate["allowed"]:
                    op = self.store.enqueue_operation("replace", server["id"], {"source": "automatic"})
                    entry["operation_id"] = op["id"]
                result.append(entry)
            except ConflictError:
                result.append({"server_id": server["id"], "replacement_allowed": False, "reason": "operation already active"})
            except Exception:
                result.append({"server_id": server["id"], "replacement_allowed": False, "health": {"verdict": "unknown", "reason": "probe execution failed"}})
        return result
