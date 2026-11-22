"""Private durable state and transaction boundaries for Voxa Fleet.

No provider requests belong in this module. An intent is committed before any
external effect, and unknown outcomes deliberately retain their reservations.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo


class StoreError(ValueError):
    pass


class ConflictError(StoreError):
    pass


class BudgetError(StoreError):
    pass


DEFAULT_SETTINGS = {
    "auto_enabled": False, "financial_enabled": False,
    "daily_budget": "0", "max_hourly": "0", "minimum_balance": "0",
    "currency": "EUR", "max_attempts": 3, "max_candidates": 1,
    "probe_interval": 30, "consecutive_failures": 3, "grace_seconds": 120,
    "probe_max_age": 90, "timezone": "Europe/Saratov",
    "terms_accepted": False, "withdrawal_waiver_accepted": False,
    "skip_ip_change_approved": False,
}
TERMINAL_PHASES = frozenset({"complete", "failed", "cancelled"})
PHASES = frozenset({
    "queued", "reserved", "ordered", "provisioning", "bootstrap", "vpn-tested",
    "cutover", "committed", "old-retired", "rejected", "cleanup-pending",
    "outcome-unknown", "manual-review", *TERMINAL_PHASES,
})
PHASE_TRANSITIONS = {
    "queued": {"reserved", "cleanup-pending", "complete", "failed", "cancelled", "manual-review"},
    "reserved": {"ordered", "outcome-unknown", "failed", "cancelled", "manual-review"},
    "ordered": {"provisioning", "outcome-unknown", "cleanup-pending", "manual-review"},
    "provisioning": {"bootstrap", "rejected", "cleanup-pending", "outcome-unknown", "manual-review"},
    "bootstrap": {"vpn-tested", "rejected", "cleanup-pending", "outcome-unknown", "manual-review"},
    "vpn-tested": {"cutover", "rejected", "cleanup-pending", "manual-review"},
    "cutover": {"committed", "outcome-unknown", "manual-review"},
    "committed": {"old-retired", "complete", "cleanup-pending", "manual-review"},
    "old-retired": {"complete", "manual-review"},
    "rejected": {"cleanup-pending", "manual-review"},
    "cleanup-pending": {"reserved", "failed", "complete", "outcome-unknown", "manual-review"},
    "outcome-unknown": {"ordered", "provisioning", "cleanup-pending", "reserved", "committed", "failed", "manual-review"},
    "manual-review": {"reserved", "ordered", "provisioning", "cleanup-pending", "committed", "complete", "failed", "cancelled"},
}


def money(value):
    """Exact nonnegative money; floats are rejected at the persistence boundary."""
    if isinstance(value, (float, bool)):
        raise StoreError("money must be a decimal string, integer, or Decimal")
    raw = str(value)
    if len(raw) > 128:
        raise StoreError("money is too large")
    try:
        result = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise StoreError("invalid money") from None
    if not result.is_finite() or result < 0:
        raise StoreError("money must be finite and nonnegative")
    if result.adjusted() > 24 or result.as_tuple().exponent < -24:
        raise StoreError("money exceeds supported exact precision")
    return format(result, "f")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _exact_calculation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        # Money is limited to 49 significant places. Leave ample headroom for
        # aggregation, independently of another library's thread-local context.
        with localcontext() as context:
            context.prec = 80
            return function(*args, **kwargs)
    return wrapped


def _time(value=None):
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise StoreError("timestamps require a timezone")
        return value.timestamp()
    result = float(value)
    if not math.isfinite(result):
        raise StoreError("invalid timestamp")
    return result


def _settings(changes, current, *, user_action):
    if not isinstance(changes, dict) or set(changes) - set(DEFAULT_SETTINGS):
        raise StoreError("unknown settings")
    result = {**current, **changes}
    for key in ("auto_enabled", "financial_enabled", "terms_accepted", "withdrawal_waiver_accepted", "skip_ip_change_approved"):
        if type(result[key]) is not bool:
            raise StoreError(f"{key} must be a boolean")
    for key, low, high in (("max_attempts", 1, 10), ("max_candidates", 1, 10),
                           ("probe_interval", 5, 86400), ("consecutive_failures", 1, 100),
                           ("grace_seconds", 0, 86400), ("probe_max_age", 1, 86400)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise StoreError(f"invalid {key}")
    for key in ("daily_budget", "max_hourly", "minimum_balance"):
        result[key] = money(result[key])
    if not isinstance(result["currency"], str) or len(result["currency"]) != 3 or not result["currency"].isalpha():
        raise StoreError("currency must be a three-letter code")
    result["currency"] = result["currency"].upper()
    try:
        ZoneInfo(result["timezone"])
    except (TypeError, ValueError, KeyError):
        raise StoreError("invalid timezone") from None
    for key in ("terms_accepted", "withdrawal_waiver_accepted", "skip_ip_change_approved", "financial_enabled", "auto_enabled"):
        if result[key] and not current[key] and not user_action:
            raise StoreError(f"{key} requires an explicit user action")
    if result["financial_enabled"]:
        if Decimal(result["daily_budget"]) <= 0 or Decimal(result["max_hourly"]) <= 0:
            raise StoreError("positive financial limits are required")
        if not result["terms_accepted"]:
            raise StoreError("provider terms require explicit acceptance")
    if result["auto_enabled"] and not result["financial_enabled"]:
        raise StoreError("automatic spending requires financial authorization")
    return result


def _server(data, old=None):
    if not isinstance(data, dict):
        raise StoreError("server must be an object")
    defaults = {"name": "", "domain": "", "country": "", "product_id": "", "location": "",
                "mode": "observe", "enabled": True, "original_state_path": "",
                "current_vps_id": None, "current_ip": None, "hourly_price": "0", "currency": "EUR", "metadata": {}}
    allowed = set(defaults) | {"id", "created_at", "updated_at"}
    if set(data) - allowed:
        raise StoreError("unknown server fields; use metadata for nonsecret UUID references")
    result = {**defaults, **(old or {}), **data}
    if old and result["id"] != old["id"]:
        raise StoreError("server id is immutable")
    result["id"] = result.get("id") or str(uuid.uuid4())
    if not isinstance(result["id"], str) or not result["id"] or len(result["id"]) > 200:
        raise StoreError("invalid server id")
    for key in ("name", "domain", "country", "product_id", "location", "original_state_path"):
        if not isinstance(result[key], str) or len(result[key]) > 4096:
            raise StoreError(f"invalid {key}")
    if not result["name"].strip():
        raise StoreError("server name is required")
    if result["mode"] not in {"observe", "approval", "auto"} or type(result["enabled"]) is not bool:
        raise StoreError("invalid server monitoring mode")
    if not isinstance(result["metadata"], dict):
        raise StoreError("metadata must be an object")
    result["hourly_price"] = money(result["hourly_price"])
    if not isinstance(result["currency"], str) or len(result["currency"]) != 3 or not result["currency"].isalpha():
        raise StoreError("invalid currency")
    result["currency"] = result["currency"].upper()
    _json(result)
    return result


class Store:
    def __init__(self, path, clock=None):
        self.path = Path(path).absolute()
        self.clock = clock or time.time
        self._prepare_private_path()
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS servers (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, server_id TEXT REFERENCES servers(id), action TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, phase TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0, worker_id TEXT, lease_until REAL,
                    data TEXT NOT NULL DEFAULT '{}', result TEXT, error TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_operation_per_server ON operations(server_id)
                    WHERE server_id IS NOT NULL AND phase NOT IN ('complete','failed','cancelled');
                CREATE TABLE IF NOT EXISTS leases (name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
                    kind TEXT NOT NULL, message TEXT NOT NULL, server_id TEXT, operation_id TEXT, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS probes (id INTEGER PRIMARY KEY AUTOINCREMENT, server_id TEXT NOT NULL
                    REFERENCES servers(id), created_at REAL NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reservations (operation_id TEXT PRIMARY KEY REFERENCES operations(id),
                    amount TEXT NOT NULL, hourly_price TEXT NOT NULL, currency TEXT NOT NULL, budget_day TEXT NOT NULL,
                    status TEXT NOT NULL, actual_amount TEXT, accounting_kind TEXT NOT NULL DEFAULT 'reservation',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS generations (vps_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL
                    REFERENCES operations(id), server_id TEXT REFERENCES servers(id), order_id TEXT NOT NULL,
                    data TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
            """)
            if "accounting_kind" not in {row[1] for row in db.execute("PRAGMA table_info(reservations)")}:
                db.execute("ALTER TABLE reservations ADD COLUMN accounting_kind TEXT NOT NULL DEFAULT 'reservation'")
            db.execute("INSERT OR IGNORE INTO settings(id,data) VALUES(1,?)", (_json(DEFAULT_SETTINGS),))
        self._check_files()

    def _prepare_private_path(self):
        # Reject symlink ancestors, not just the final DB name. Never chmod an
        # existing shared directory into a private one behind the caller's back.
        for part in (self.path.parent, *self.path.parent.parents):
            if part.is_symlink():
                raise StoreError("state directory must not traverse symlinks")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.path.parent.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise StoreError("state directory must be owned by this user and mode 0700")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise StoreError("database must be an owned regular file without hard links")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self._check_files()

    def _check_files(self):
        parent = self.path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise StoreError("state directory must remain private")
        for filename in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            try:
                info = filename.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise StoreError("unsafe state file")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise StoreError("state files must be private")

    def _connect(self):
        self._check_files()
        db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _transaction(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _now(self, now=None):
        return _time(self.clock() if now is None else now)

    @staticmethod
    def _op(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("payload", "data", "result"):
            if result.get(key) is not None:
                result[key] = json.loads(result[key])
        return result

    @staticmethod
    def _settings_db(db):
        return json.loads(db.execute("SELECT data FROM settings WHERE id=1").fetchone()[0])

    def get_settings(self):
        with closing(self._connect()) as db:
            return self._settings_db(db)

    def update_settings(self, changes, *, user_action=False):
        with self._transaction() as db:
            old = self._settings_db(db)
            result = _settings(changes, old, user_action=user_action)
            # Currency changes cannot reinterpret any existing liability.
            if result["currency"] != old["currency"]:
                if db.execute("SELECT 1 FROM reservations LIMIT 1").fetchone() or db.execute("SELECT 1 FROM generations WHERE status!='deleted' LIMIT 1").fetchone():
                    raise StoreError("currency cannot change with recorded financial liabilities")
            db.execute("UPDATE settings SET data=? WHERE id=1", (_json(result),))
            self._event(db, "settings.updated", "Settings updated", data={"fields": sorted(changes), "user_action": user_action})
            return result

    def list_servers(self):
        with closing(self._connect()) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM servers ORDER BY id")]

    def get_server(self, server_id):
        with closing(self._connect()) as db:
            row = db.execute("SELECT data FROM servers WHERE id=?", (server_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def create_server(self, data):
        result = _server(data)
        now = self._now()
        result.update(created_at=now, updated_at=now)
        with self._transaction() as db:
            try:
                db.execute("INSERT INTO servers(id,data) VALUES(?,?)", (result["id"], _json(result)))
            except sqlite3.IntegrityError:
                raise ConflictError("server already exists") from None
            self._event(db, "server.created", "Logical server added", server_id=result["id"])
        return result

    def update_server(self, server_id, changes):
        with self._transaction() as db:
            row = db.execute("SELECT data FROM servers WHERE id=?", (server_id,)).fetchone()
            if row is None:
                raise StoreError("server not found")
            old = json.loads(row[0])
            result = _server(changes, old)
            if result["current_vps_id"] != old["current_vps_id"]:
                raise StoreError("use commit_generation to change an active generation")
            result["updated_at"] = self._now()
            db.execute("UPDATE servers SET data=? WHERE id=?", (_json(result), server_id))
            self._event(db, "server.updated", "Logical server updated", server_id=server_id, data={"fields": sorted(changes)})
            return result

    def enqueue_operation(self, action, server_id=None, payload=None, *, idempotency_key=None):
        if action not in {"create", "replace", "check", "cleanup"}:
            raise StoreError("invalid operation action")
        if action != "create" and not server_id:
            raise StoreError("server id is required")
        payload = payload or {}
        if not isinstance(payload, dict):
            raise StoreError("operation payload must be an object")
        key = idempotency_key or str(uuid.uuid4())
        if not isinstance(key, str) or not key or len(key) > 200:
            raise StoreError("invalid idempotency key")
        body = _json(payload)
        now = self._now()
        with self._transaction() as db:
            prior = db.execute("SELECT * FROM operations WHERE idempotency_key=?", (key,)).fetchone()
            if prior:
                if prior["action"] != action or prior["server_id"] != server_id or prior["payload"] != body:
                    raise ConflictError("idempotency key cannot be reused with a different request")
                return self._op(prior)
            if server_id and not db.execute("SELECT 1 FROM servers WHERE id=?", (server_id,)).fetchone():
                raise StoreError("server not found")
            if server_id:
                prior = db.execute("SELECT * FROM operations WHERE server_id=? AND phase NOT IN ('complete','failed','cancelled')", (server_id,)).fetchone()
                if prior:
                    if action == "cleanup" and prior["phase"] == "manual-review" and payload.get("approved") is True:
                        generation = self._assert_deletable(db, payload.get("vps_id"), prior["id"])
                        if generation["server_id"] != server_id:
                            raise ConflictError("cleanup target belongs to a different logical server")
                        details = json.loads(prior["data"])
                        details.update(cleanup_requested=True, retry_disabled=True, attention="explicit owner-requested candidate cleanup")
                        db.execute("UPDATE operations SET phase='cleanup-pending',data=?,version=version+1,worker_id=NULL,lease_until=NULL,updated_at=? WHERE id=?", (_json(details), now, prior["id"]))
                        self._event(db, "operation.cleanup-requested", "Owner explicitly requested exact candidate cleanup", server_id=server_id, operation_id=prior["id"], data={"vps_id": payload["vps_id"]})
                        return self._op(db.execute("SELECT * FROM operations WHERE id=?", (prior["id"],)).fetchone())
                    if prior["action"] == action and prior["payload"] == body:
                        return self._op(prior)
                    raise ConflictError("server already has an active operation")
            op_id = str(uuid.uuid4())
            db.execute("INSERT INTO operations(id,server_id,action,idempotency_key,payload,phase,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?)", (op_id, server_id, action, key, body, now, now))
            self._event(db, "operation.queued", "Operation queued", server_id=server_id, operation_id=op_id)
            return self._op(db.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone())

    def get_operation(self, operation_id):
        with closing(self._connect()) as db:
            return self._op(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def list_operations(self, limit=100, server_id=None):
        limit = max(1, min(int(limit), 1000))
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM operations WHERE (? IS NULL OR server_id=?) ORDER BY created_at DESC LIMIT ?", (server_id, server_id, limit))
            return [self._op(row) for row in rows]

    def claim_operation(self, worker_id, lease_seconds=60, operation_id=None, now=None):
        if not worker_id or not 1 <= lease_seconds <= 3600:
            raise StoreError("invalid worker lease")
        now = self._now(now)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE phase NOT IN ('complete','failed','cancelled','manual-review') AND (? IS NULL OR id=?) AND (worker_id IS NULL OR lease_until<=? OR worker_id=?) ORDER BY created_at LIMIT 1", (operation_id, operation_id, now, worker_id)).fetchone()
            if not row:
                return None
            db.execute("UPDATE operations SET worker_id=?,lease_until=?,version=version+1,updated_at=? WHERE id=?", (worker_id, now + lease_seconds, now, row["id"]))
            return self._op(db.execute("SELECT * FROM operations WHERE id=?", (row["id"],)).fetchone())

    def update_operation(self, operation_id, *, expected_version, worker_id, phase=None, data=None, result=None, error=None, lease_seconds=None):
        now = self._now()
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not row:
                raise StoreError("operation not found")
            if row["version"] != expected_version or row["worker_id"] != worker_id or row["lease_until"] is None or row["lease_until"] <= now:
                raise ConflictError("operation version or lease is stale")
            phase = phase or row["phase"]
            if phase not in PHASES or (phase != row["phase"] and phase not in PHASE_TRANSITIONS.get(row["phase"], set())):
                raise ConflictError("invalid operation phase transition")
            if phase in TERMINAL_PHASES:
                pending = db.execute("SELECT status FROM reservations WHERE operation_id=?", (operation_id,)).fetchone()
                if pending and pending["status"] in {"reserved", "unknown"}:
                    raise ConflictError("financial outcome is still pending")
                for generation in db.execute("SELECT vps_id,status FROM generations WHERE operation_id=? AND status!='deleted'", (operation_id,)):
                    if phase != "complete" or generation["status"] != "active":
                        raise ConflictError("candidate outcome is still pending")
                    current = db.execute("SELECT data FROM servers WHERE id=?", (row["server_id"],)).fetchone()
                    if not current or json.loads(current[0])["current_vps_id"] != generation["vps_id"]:
                        raise ConflictError("generation is not the committed active server")
            merged = json.loads(row["data"])
            if data is not None:
                if not isinstance(data, dict):
                    raise StoreError("operation data must be an object")
                merged.update(data)
            until = row["lease_until"]
            if lease_seconds is not None:
                if not 1 <= lease_seconds <= 3600:
                    raise StoreError("invalid lease duration")
                until = now + lease_seconds
            db.execute("UPDATE operations SET phase=?,data=?,result=?,error=?,version=version+1,updated_at=?,lease_until=? WHERE id=?", (phase, _json(merged), _json(result) if result is not None else row["result"], error if error is not None else row["error"], now, until, operation_id))
            self._event(db, "operation.phase", "Operation state recorded", server_id=row["server_id"], operation_id=operation_id, data={"from": row["phase"], "to": phase})
            return self._op(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def acquire_lease(self, name, owner, seconds=60, now=None):
        if not name or not owner or not 1 <= seconds <= 3600:
            raise StoreError("invalid lease")
        now = self._now(now)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM leases WHERE name=?", (name,)).fetchone()
            if row and row["owner"] != owner and row["expires_at"] > now:
                return False
            db.execute("INSERT INTO leases(name,owner,expires_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at", (name, owner, now + seconds))
            return True

    def release_operation(self, operation_id, *, worker_id, expected_version):
        with self._transaction() as db:
            updated = db.execute("UPDATE operations SET worker_id=NULL,lease_until=NULL,version=version+1 WHERE id=? AND worker_id=? AND version=?", (operation_id, worker_id, expected_version))
            if updated.rowcount != 1:
                raise ConflictError("operation version or lease is stale")
            return self._op(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def adopt_initial_identity(self, operation_id, registration, *, worker_id, expected_version):
        """Atomically adopt a first installation's nonsecret logical references.

        The executor verifies the private state file and remote objects. This
        boundary only accepts a fresh create, never swaps an existing identity,
        and permits recovery to replay exactly the same registration evidence.
        """
        if not isinstance(registration, dict) or set(registration) != {"original_state_path", "metadata"}:
            raise StoreError("invalid initial registration evidence")
        path, refs = registration["original_state_path"], registration["metadata"]
        allowed = {"node_id", "host_id", "profile_id", "inbound_id", "cloudflare_record_id", "installation_code"}
        if not isinstance(path, str) or not path or len(path) > 4096 or not Path(path).is_absolute() or ".." in Path(path).parts or any(ord(char) < 32 for char in path):
            raise StoreError("initial state path must be an absolute nonsecret path")
        if not isinstance(refs, dict) or set(refs) != allowed:
            raise StoreError("initial registration requires only the documented public references")
        normalized = {}
        for key in ("node_id", "host_id", "profile_id", "inbound_id"):
            try:
                value = uuid.UUID(refs[key])
            except (ValueError, TypeError, AttributeError):
                raise StoreError("initial registration UUID is invalid") from None
            if value.int == 0 or not isinstance(refs[key], str):
                raise StoreError("initial registration UUID is invalid")
            normalized[key] = str(value)
        if not isinstance(refs["cloudflare_record_id"], str) or not re.fullmatch(r"[a-fA-F0-9]{32}", refs["cloudflare_record_id"]):
            raise StoreError("initial Cloudflare record reference is invalid")
        normalized["cloudflare_record_id"] = refs["cloudflare_record_id"].lower()
        if not isinstance(refs["installation_code"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", refs["installation_code"]):
            raise StoreError("initial installation code is invalid")
        normalized["installation_code"] = refs["installation_code"]
        registration = {"original_state_path": str(Path(path)), "metadata": normalized}
        now = self._now()
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not row or row["worker_id"] != worker_id or row["version"] != expected_version or row["lease_until"] is None or row["lease_until"] <= now:
                raise ConflictError("operation version or lease is stale")
            details = json.loads(row["data"])
            if row["action"] != "create" or row["phase"] != "bootstrap" or details.get("previous_vps_id") is not None:
                raise StoreError("only a fresh initial create may register a logical identity")
            server_row = db.execute("SELECT data FROM servers WHERE id=?", (row["server_id"],)).fetchone()
            if server_row is None:
                raise StoreError("logical server not found")
            server = json.loads(server_row[0])
            prior = details.get("initial_registration")
            if prior is not None:
                if prior != registration or server["original_state_path"] != registration["original_state_path"] or any(server["metadata"].get(key) != value for key, value in normalized.items()):
                    raise ConflictError("initial identity registration is immutable")
                return self._op(row)
            if server["original_state_path"] or server["current_vps_id"]:
                raise StoreError("an existing server identity cannot be replaced by registration")
            if any(key in server["metadata"] and server["metadata"][key] != value for key, value in normalized.items()):
                raise ConflictError("initial registration conflicts with existing references")
            for key in ("node_id", "host_id", "profile_id", "inbound_id"):
                alias = key.removesuffix("_id") + "_uuid"
                if alias in server["metadata"] and str(server["metadata"][alias]).lower() != normalized[key]:
                    raise ConflictError("initial registration conflicts with an existing UUID alias")
            server.update(original_state_path=registration["original_state_path"], metadata={**server["metadata"], **normalized}, updated_at=now)
            details["initial_registration"] = registration
            details["identity_snapshot"] = {"id": server["id"], "name": server["name"], "domain": server["domain"], "original_state_path": server["original_state_path"], "metadata": {key: value for key, value in server["metadata"].items() if key != "last_cutover_at"}}
            db.execute("UPDATE servers SET data=? WHERE id=?", (_json(server), server["id"]))
            db.execute("UPDATE operations SET data=?,version=version+1,updated_at=? WHERE id=?", (_json(details), now, operation_id))
            self._event(db, "identity.registered", "Initial logical identity registered and durably preserved", server_id=server["id"], operation_id=operation_id, data={"reference_fields": sorted(normalized)})
            return self._op(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def finish_rejected_operation(self, operation_id, *, worker_id, expected_version, retry_payload=None, idempotency_key=None):
        """Atomically finish a deleted candidate and optionally enqueue its child."""
        now = self._now()
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not row or row["worker_id"] != worker_id or row["version"] != expected_version or row["lease_until"] <= now:
                raise ConflictError("operation version or lease is stale")
            if row["phase"] != "cleanup-pending" or db.execute("SELECT 1 FROM generations WHERE operation_id=? AND status!='deleted'", (operation_id,)).fetchone():
                raise ConflictError("candidate deletion is not confirmed")
            reservation = db.execute("SELECT status FROM reservations WHERE operation_id=?", (operation_id,)).fetchone()
            if reservation and reservation[0] in {"unknown", "reserved"}:
                raise ConflictError("purchase outcome is still unknown")
            details = json.loads(row["data"])
            details["outcome"] = "candidate_rejected_and_deleted"
            child = None
            db.execute("UPDATE operations SET phase='failed',version=version+1,updated_at=? WHERE id=?", (now, operation_id))
            if retry_payload is not None:
                if not isinstance(retry_payload, dict) or not idempotency_key:
                    raise StoreError("retry identity and payload are required")
                if retry_payload.get("parent_operation_id") != operation_id:
                    raise StoreError("retry must preserve its parent operation")
                child_id = str(uuid.uuid4())
                db.execute("INSERT INTO operations(id,server_id,action,idempotency_key,payload,phase,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?)", (child_id, row["server_id"], row["action"], idempotency_key, _json(retry_payload), now, now))
                details["child_operation_id"] = child_id
                self._event(db, "operation.queued", "Linked candidate retry queued after confirmed deletion", server_id=row["server_id"], operation_id=child_id, data={"parent_operation_id": operation_id})
                child = self._op(db.execute("SELECT * FROM operations WHERE id=?", (child_id,)).fetchone())
            db.execute("UPDATE operations SET data=? WHERE id=?", (_json(details), operation_id))
            self._event(db, "operation.failed", "Rejected candidate deletion confirmed", server_id=row["server_id"], operation_id=operation_id)
            return self._op(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()), child

    def release_lease(self, name, owner):
        with self._transaction() as db:
            return bool(db.execute("DELETE FROM leases WHERE name=? AND owner=?", (name, owner)).rowcount)

    def _event(self, db, kind, message, *, server_id=None, operation_id=None, data=None):
        db.execute("INSERT INTO events(created_at,kind,message,server_id,operation_id,data) VALUES(?,?,?,?,?,?)", (self._now(), kind, message, server_id, operation_id, _json(data or {})))

    def add_event(self, kind, message, *, server_id=None, operation_id=None, data=None):
        with self._transaction() as db:
            self._event(db, kind, message, server_id=server_id, operation_id=operation_id, data=data)

    def list_events(self, limit=100):
        with closing(self._connect()) as db:
            result = []
            for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 1000)),)):
                item = dict(row)
                item["data"] = json.loads(item["data"])
                result.append(item)
            return result

    def record_probe(self, server_id, probe):
        if not isinstance(probe, dict):
            raise StoreError("probe must be an object")
        data = {**probe}
        data.setdefault("observed_at", self._now())
        with self._transaction() as db:
            if not db.execute("SELECT 1 FROM servers WHERE id=?", (server_id,)).fetchone():
                raise StoreError("server not found")
            db.execute("INSERT INTO probes(server_id,created_at,data) VALUES(?,?,?)", (server_id, self._now(), _json(data)))
        return data

    def latest_probes(self, server_id, limit=20):
        with closing(self._connect()) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM probes WHERE server_id=? ORDER BY id DESC LIMIT ?", (server_id, max(1, min(int(limit), 1000))))]

    @_exact_calculation
    def _budget_db(self, db, now):
        cfg = self._settings_db(db)
        day = datetime.fromtimestamp(now, ZoneInfo(cfg["timezone"])).date().isoformat()
        rows = db.execute("SELECT * FROM reservations").fetchall()
        def gross_amount(row):
            actual = Decimal(row["actual_amount"]) if row["actual_amount"] is not None else Decimal(row["amount"])
            return max(actual, Decimal(row["amount"])) if row["accounting_kind"] == "conservative_upper_bound" else actual
        gross = sum((gross_amount(r) for r in rows if r["budget_day"] == day and r["status"] != "released"), Decimal(0))
        active = db.execute("SELECT * FROM generations WHERE status!='deleted'").fetchall()
        hourly = Decimal(0)
        unknown_costs = []
        counted_vps = set()
        for row in db.execute("SELECT data FROM servers"):
            server = json.loads(row[0])
            if server["current_vps_id"]:
                if server["currency"] != cfg["currency"]:
                    unknown_costs.append("current_server_currency_mismatch")
                if Decimal(server["hourly_price"]) <= 0:
                    unknown_costs.append("current_server_price_unknown")
                hourly += Decimal(server["hourly_price"])
                counted_vps.add(server["current_vps_id"])
        candidates = 0
        represented_ops = {row[0] for row in db.execute("SELECT DISTINCT operation_id FROM generations")}
        for row in active:
            gen = json.loads(row["data"])
            represented_ops.add(row["operation_id"])
            if row["vps_id"] not in counted_vps:
                if gen["currency"] != cfg["currency"]:
                    unknown_costs.append("generation_currency_mismatch")
                hourly += Decimal(gen["hourly_price"])
                candidates += 1
        # Imported/current IDs are not automatically owned for deletion. If a
        # cutover replaces one, its billing exposure remains occupied until an
        # explicit provider-correlated retirement/import is recorded.
        all_generation_ids = {row[0] for row in db.execute("SELECT vps_id FROM generations")}
        for row in db.execute("SELECT data FROM operations WHERE action IN ('create','replace')"):
            details = json.loads(row[0])
            old_id = details.get("previous_vps_id")
            if old_id and old_id not in counted_vps and old_id not in all_generation_ids and not details.get("retire_confirmed"):
                price = details.get("previous_hourly_price")
                if price is None or Decimal(price) <= 0 or details.get("previous_currency") != cfg["currency"]:
                    unknown_costs.append("retained_imported_generation_price_unknown")
                else:
                    hourly += Decimal(price)
                candidates += 1
                counted_vps.add(old_id)
        for row in rows:
            if row["status"] in {"reserved", "unknown", "settled", "accounted"} and row["operation_id"] not in represented_ops:
                # A settled order without an identified/deleted generation still
                # represents a possible live, billable machine.
                hourly += Decimal(row["hourly_price"])
                candidates += 1
        return {"day": day, "currency": cfg["currency"], "gross_daily": money(gross), "current_hourly": money(hourly) if not unknown_costs else None, "costs_known": not unknown_costs, "unknown_costs": sorted(set(unknown_costs)), "pending_candidates": candidates, "daily_budget": cfg["daily_budget"], "max_hourly": cfg["max_hourly"]}

    def budget_status(self, now=None):
        with closing(self._connect()) as db:
            return self._budget_db(db, self._now(now))

    @_exact_calculation
    def reserve_budget(self, operation_id, amount, hourly_price, *, currency=None, balance=None, now=None):
        amount, hourly_price = money(amount), money(hourly_price)
        if Decimal(amount) <= 0 or Decimal(hourly_price) <= 0:
            raise BudgetError("a positive conservative reserve and hourly price are required")
        now = self._now(now)
        with self._transaction() as db:
            cfg = self._settings_db(db)
            currency = currency or cfg["currency"]
            if currency != cfg["currency"]:
                raise BudgetError("currency mismatch")
            prior = db.execute("SELECT * FROM reservations WHERE operation_id=?", (operation_id,)).fetchone()
            if prior:
                if prior["amount"] != amount or prior["hourly_price"] != hourly_price or prior["currency"] != currency:
                    raise ConflictError("reservation cannot be changed or duplicated")
                return dict(prior)
            op = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not op or op["action"] not in {"create", "replace"} or op["phase"] in TERMINAL_PHASES:
                raise BudgetError("operation is not a purchase intent")
            if op["server_id"]:
                server = json.loads(db.execute("SELECT data FROM servers WHERE id=?", (op["server_id"],)).fetchone()[0])
                if server["currency"] != currency:
                    raise BudgetError("server currency differs from budget currency")
            if not cfg["financial_enabled"] or not cfg["terms_accepted"]:
                raise BudgetError("financial actions are disabled")
            if balance is None:
                raise BudgetError("confirmed account balance is required")
            balance = Decimal(money(balance))
            totals = self._budget_db(db, now)
            if not totals["costs_known"]:
                raise BudgetError("ongoing billing exposure is unknown or has mixed currencies")
            if Decimal(cfg["daily_budget"]) <= 0 or Decimal(cfg["max_hourly"]) <= 0:
                raise BudgetError("financial limits are not configured")
            if Decimal(totals["gross_daily"]) + Decimal(amount) > Decimal(cfg["daily_budget"]):
                raise BudgetError("daily gross spending limit exceeded")
            if Decimal(totals["current_hourly"]) + Decimal(hourly_price) > Decimal(cfg["max_hourly"]):
                raise BudgetError("ongoing hourly limit exceeded")
            if totals["pending_candidates"] >= cfg["max_candidates"]:
                raise BudgetError("candidate limit exceeded")
            held = sum((Decimal(r[0]) for r in db.execute("SELECT amount FROM reservations WHERE status IN ('reserved','unknown')")), Decimal(0))
            if balance - held - Decimal(amount) < Decimal(cfg["minimum_balance"]):
                raise BudgetError("minimum account balance would be breached")
            db.execute("INSERT INTO reservations(operation_id,amount,hourly_price,currency,budget_day,status,created_at,updated_at) VALUES(?,?,?,?,?,'reserved',?,?)", (operation_id, amount, hourly_price, currency, totals["day"], now, now))
            self._event(db, "budget.reserved", "Purchase budget reserved", operation_id=operation_id, data={"amount": amount, "hourly_price": hourly_price, "currency": currency})
            return dict(db.execute("SELECT * FROM reservations WHERE operation_id=?", (operation_id,)).fetchone())

    def settle_budget(self, operation_id, amount, *, currency=None):
        amount = money(amount)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM reservations WHERE operation_id=?", (operation_id,)).fetchone()
            if not row or (currency is not None and currency != row["currency"]):
                raise BudgetError("reservation or matching currency not found")
            if row["status"] == "released" or (row["status"] == "settled" and row["actual_amount"] != amount):
                raise ConflictError("financial outcome cannot be rewritten")
            db.execute("UPDATE reservations SET status='settled',actual_amount=?,updated_at=? WHERE operation_id=?", (amount, self._now(), operation_id))
            self._event(db, "budget.settled", "Provider charge recorded", operation_id=operation_id, data={"amount": amount, "currency": row["currency"]})
            return dict(db.execute("SELECT * FROM reservations WHERE operation_id=?", (operation_id,)).fetchone())

    def account_budget(self, operation_id, *, order_id, vps_id):
        """Occupy a conservative bound, without pretending it is a charge receipt.

        Only a durably identified order/service can leave the unknown-result
        hold. Later ledger reconciliation never replenishes this gross bound.
        """
        with self._transaction() as db:
            row = db.execute("SELECT * FROM reservations WHERE operation_id=?", (operation_id,)).fetchone()
            generation = db.execute("SELECT * FROM generations WHERE operation_id=? AND order_id=? AND vps_id=?", (operation_id, order_id, vps_id)).fetchone()
            if not row or not generation or row["status"] == "released":
                raise BudgetError("exact recorded order and service are required for conservative accounting")
            if row["status"] == "settled":
                return dict(row)
            db.execute("UPDATE reservations SET status='accounted',accounting_kind='conservative_upper_bound',updated_at=? WHERE operation_id=?", (self._now(), operation_id))
            self._event(db, "budget.accounted", "Conservative cost bound occupied; not an actual-charge receipt", operation_id=operation_id, data={"amount": row["amount"], "currency": row["currency"], "accounting_kind": "conservative_upper_bound", "order_id": order_id, "vps_id": vps_id})
            return dict(db.execute("SELECT * FROM reservations WHERE operation_id=?", (operation_id,)).fetchone())

    def release_budget(self, operation_id, *, confirmed_no_charge=False):
        with self._transaction() as db:
            row = db.execute("SELECT * FROM reservations WHERE operation_id=?", (operation_id,)).fetchone()
            if not row:
                raise BudgetError("reservation not found")
            if row["status"] in {"settled", "accounted"}:
                raise BudgetError("refunds do not replenish gross daily spending")
            status = "released" if confirmed_no_charge else "unknown"
            if row["status"] == "released" and status != "released":
                raise ConflictError("released reservation cannot be reopened")
            if status == "released" and db.execute("SELECT 1 FROM generations WHERE operation_id=? AND status!='deleted'", (operation_id,)).fetchone():
                raise BudgetError("live generation still represents financial liability")
            db.execute("UPDATE reservations SET status=?,updated_at=? WHERE operation_id=?", (status, self._now(), operation_id))
            self._event(db, "budget." + status, "Financial outcome recorded", operation_id=operation_id)
            return dict(db.execute("SELECT * FROM reservations WHERE operation_id=?", (operation_id,)).fetchone())

    def record_generation(self, operation_id, order_id, vps_id, *, ip=None, hourly_price=None, currency=None, status="provisioning"):
        if not isinstance(vps_id, str) or not vps_id or not isinstance(order_id, str) or not order_id:
            raise StoreError("exact provider order and VPS ids are required")
        hourly_price = money(hourly_price) if hourly_price is not None else None
        if status not in {"ordered", "provisioning", "bootstrap", "vpn-tested", "active", "rejected", "cleanup-pending", "outcome-unknown", "deleted"}:
            raise StoreError("invalid generation status")
        now = self._now()
        with self._transaction() as db:
            op = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not op or op["action"] not in {"create", "replace"}:
                raise StoreError("generation needs a recorded purchase operation")
            currency = currency or self._settings_db(db)["currency"]
            reservation = db.execute("SELECT * FROM reservations WHERE operation_id=?", (operation_id,)).fetchone()
            if not reservation or reservation["status"] == "released" or reservation["currency"] != currency:
                raise StoreError("generation needs an unreleased financial reservation")
            hourly_price = hourly_price if hourly_price is not None else reservation["hourly_price"]
            if Decimal(hourly_price) <= 0:
                raise StoreError("generation hourly cost is unknown")
            existing = db.execute("SELECT * FROM generations WHERE vps_id=?", (vps_id,)).fetchone()
            if existing:
                if existing["operation_id"] != operation_id or existing["order_id"] != order_id:
                    raise ConflictError("VPS ownership cannot be reassigned")
                return json.loads(existing["data"])
            if db.execute("SELECT 1 FROM generations WHERE operation_id=? AND status!='deleted'", (operation_id,)).fetchone():
                raise ConflictError("operation already owns a live candidate")
            data = {"vps_id": vps_id, "operation_id": operation_id, "server_id": op["server_id"], "order_id": order_id,
                    "ip": ip, "hourly_price": hourly_price, "currency": currency, "status": status}
            db.execute("INSERT INTO generations(vps_id,operation_id,server_id,order_id,data,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (vps_id, operation_id, op["server_id"], order_id, _json(data), status, now, now))
            self._event(db, "generation.recorded", "Provider generation identified", server_id=op["server_id"], operation_id=operation_id, data={"vps_id": vps_id, "order_id": order_id})
            return data

    def list_generations(self, server_id=None):
        with closing(self._connect()) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM generations WHERE (? IS NULL OR server_id=?) ORDER BY created_at", (server_id, server_id))]

    def update_generation(self, vps_id, changes):
        if not isinstance(changes, dict) or set(changes) - {"status", "ip"}:
            raise StoreError("generation ownership and prices are immutable")
        allowed = {"ordered", "provisioning", "bootstrap", "vpn-tested", "active", "rejected", "cleanup-pending", "outcome-unknown", "deleted"}
        if "status" in changes and changes["status"] not in allowed:
            raise StoreError("invalid generation status")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM generations WHERE vps_id=?", (vps_id,)).fetchone()
            if not row:
                raise StoreError("unowned generation")
            if row["status"] == "deleted" and changes.get("status", "deleted") != "deleted":
                raise ConflictError("deleted generation cannot be revived")
            if changes.get("status") == "deleted":
                self._assert_deletable(db, vps_id)
            data = {**json.loads(row["data"]), **changes}
            db.execute("UPDATE generations SET data=?,status=?,updated_at=? WHERE vps_id=?", (_json(data), data["status"], self._now(), vps_id))
            self._event(db, "generation.updated", "Generation outcome recorded", server_id=row["server_id"], operation_id=row["operation_id"], data={"vps_id": vps_id, "status": data["status"]})
            return data

    def _assert_deletable(self, db, vps_id, operation_id=None):
        row = db.execute("SELECT * FROM generations WHERE vps_id=?", (vps_id,)).fetchone()
        if not row or (operation_id is not None and row["operation_id"] != operation_id):
            raise StoreError("VPS is not owned by this recorded operation")
        for server in db.execute("SELECT data FROM servers"):
            data = json.loads(server[0])
            protected = data.get("metadata", {}).get("protected_vps_ids", [])
            if data["current_vps_id"] == vps_id or vps_id in protected:
                raise StoreError("active or protected VPS cannot be deleted")
        return json.loads(row["data"])

    def assert_deletable(self, vps_id, operation_id=None):
        with closing(self._connect()) as db:
            return self._assert_deletable(db, vps_id, operation_id)

    def commit_generation(self, vps_id):
        with self._transaction() as db:
            row = db.execute("SELECT * FROM generations WHERE vps_id=?", (vps_id,)).fetchone()
            if not row or not row["server_id"] or row["status"] not in {"vpn-tested", "active"}:
                raise StoreError("only a tested owned generation can be committed")
            gen = json.loads(row["data"])
            server = json.loads(db.execute("SELECT data FROM servers WHERE id=?", (row["server_id"],)).fetchone()[0])
            server.update(current_vps_id=vps_id, current_ip=gen["ip"], hourly_price=gen["hourly_price"], currency=gen["currency"], updated_at=self._now())
            gen["status"] = "active"
            db.execute("UPDATE servers SET data=? WHERE id=?", (_json(server), row["server_id"]))
            db.execute("UPDATE generations SET data=?,status='active',updated_at=? WHERE vps_id=?", (_json(gen), self._now(), vps_id))
            self._event(db, "generation.committed", "Active generation committed", server_id=row["server_id"], operation_id=row["operation_id"], data={"vps_id": vps_id})
            return gen
