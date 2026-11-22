"""Explicit isolated staging composition. Import/construction performs no I/O.

This is deliberately NOT an SPN startup hook. Configuration is provided by the
caller, never discovered through dotenv, the main bot's settings or environment.
Production is a release gate, not a spelling that silently enables real money.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
import inspect
import re
from urllib.parse import unquote, urlsplit
from uuid import UUID

from services.partner_manager_access import normalize_manager_allowed_user_ids


class RuntimeConfigurationError(ValueError):
    """A deliberately generic error: configuration can contain credentials."""


def _invalid():
    raise RuntimeConfigurationError("Некорректные настройки изолированного стенда.")


def _origin(value):
    from services.partner_catalog import validate_base_domain

    try:
        parsed = urlsplit(value)
        if (
            not isinstance(value, str)
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)
            or "\\" in value
        ):
            _invalid()
        return "https://" + validate_base_domain(parsed.hostname)
    except Exception:
        _invalid()


def _dsn(value):
    try:
        parsed = urlsplit(value)
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 4096
            or parsed.scheme not in {"postgres", "postgresql"}
            or not parsed.hostname
            or parsed.port is not None
            and not 1 <= parsed.port <= 65535
            or not re.fullmatch(r"/[A-Za-z0-9_-]{1,63}", parsed.path)
            or parsed.query
            or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)
            or "\\" in value
        ):
            _invalid()
    except Exception:
        _invalid()


@dataclass(frozen=True)
class PartnerRuntimeSettings:
    mode: str = "disabled"
    staging_isolation_confirmed: bool = False
    allow_staging_network: bool = False
    allow_staging_writes: bool = False
    enable_http: bool = False
    enable_customer_webhooks: bool = False
    enable_manager_webhook: bool = False
    enable_managed_bots: bool = False
    enable_billing: bool = False
    # A deliberately bounded live entry-only check; never public commerce.
    enable_owner_entry_pilot: bool = False
    # Public enrollment only; NEVER permission to charge tenant customers.
    enable_public_entry_trial: bool = False
    # Regular-price enrollment only; customer commerce stays disabled.
    enable_public_entry_sales: bool = False
    enable_payment_first_entry: bool = False
    invoice_intents_enabled: bool = False
    panel_identity_enabled: bool = False
    enable_provisioning: bool = False
    enable_launches: bool = False
    enable_notifications: bool = False
    # Factual central-manager notice only; independent of customer delivery.
    enable_entry_launch_notifications: bool = False
    enable_marketing_delivery: bool = False
    enable_support_delivery: bool = False
    enable_finance_notifications: bool = False
    enable_public_gateway: bool = False
    enable_public_checkout: bool = False
    enable_worker: bool = False
    management_enabled: bool = False
    finance_enabled: bool = False
    branding_enabled: bool = False
    operator_ids: tuple[int, ...] = ()
    admin_recipient_id: int | None = None
    database_dsn: str = field(default="", repr=False)
    database_tls: bool = True
    database_ca_file: str = field(default="", repr=False)
    database_isolation_confirmed: bool = False
    database_api_disabled_confirmed: bool = False
    database_connection_mode: str = "direct"
    database_project_ref: str = ""
    base_domain: str = ""
    manager_origin: str = ""
    webhook_origin: str = ""
    manager_bot_token: str = field(default="", repr=False)
    manager_bot_username: str = ""
    encryption_keys: tuple[str, ...] = field(default=(), repr=False)
    webhook_key_hex: str = field(default="", repr=False)
    subscription_key_hex: str = field(default="", repr=False)
    yookassa_shop_id: str = field(default="", repr=False)
    yookassa_secret_key: str = field(default="", repr=False)
    yookassa_test_mode: bool = True
    remnawave_api_url: str = ""
    remnawave_api_token: str = field(default="", repr=False)
    remnawave_api_version: int = 3
    remnawave_squad_ids: tuple[str, ...] = ()
    subscription_source_origin: str = ""
    addon_catalog_reviewed: bool = False
    staging_no_addons_confirmed: bool = False
    interval_seconds: int = 30
    phase_timeout_seconds: int = 30
    pass_limit: int = 25
    # Central-manager entry/API only: None keeps legacy access; () denies all.
    manager_allowed_user_ids: tuple[int, ...] | None = None

    def __post_init__(self):
        try:
            # There is intentionally no production/live mode in this release.
            if self.mode not in {"disabled", "staging"}:
                _invalid()
            for entry in fields(self):
                if (
                    entry.type in {bool, "bool"}
                    and type(getattr(self, entry.name)) is not bool
                ):
                    _invalid()
            for name, low, high in (
                ("interval_seconds", 5, 300),
                ("phase_timeout_seconds", 1, 120),
                ("pass_limit", 1, 100),
            ):
                value = getattr(self, name)
                if type(value) is not int or not low <= value <= high:
                    _invalid()
            if type(
                self.remnawave_api_version
            ) is not int or self.remnawave_api_version not in {2, 3}:
                _invalid()
            for name in ("operator_ids", "encryption_keys", "remnawave_squad_ids"):
                value = getattr(self, name)
                if not isinstance(value, (tuple, list)) or len(value) > 100:
                    _invalid()
                object.__setattr__(self, name, tuple(value))
            if any(type(v) is not int or not 0 < v < 2**63 for v in self.operator_ids):
                _invalid()
            if len(set(self.operator_ids)) != len(self.operator_ids):
                _invalid()
            object.__setattr__(
                self,
                "manager_allowed_user_ids",
                normalize_manager_allowed_user_ids(self.manager_allowed_user_ids),
            )
            if self.admin_recipient_id is not None and (
                type(self.admin_recipient_id) is not int
                or not 0 < self.admin_recipient_id < 2**63
            ):
                _invalid()
            string_fields = (
                "database_dsn",
                "database_ca_file",
                "database_connection_mode",
                "database_project_ref",
                "base_domain",
                "manager_origin",
                "webhook_origin",
                "manager_bot_token",
                "manager_bot_username",
                "webhook_key_hex",
                "subscription_key_hex",
                "yookassa_shop_id",
                "yookassa_secret_key",
                "remnawave_api_url",
                "remnawave_api_token",
                "subscription_source_origin",
            )
            if any(not isinstance(getattr(self, name), str) for name in string_fields):
                _invalid()
            if self.database_connection_mode not in {"direct", "session"}:
                _invalid()
            if self.database_project_ref and not re.fullmatch(
                r"[a-z0-9]{16,32}", self.database_project_ref
            ):
                _invalid()
            if self.database_ca_file:
                from pathlib import PurePath

                path = PurePath(self.database_ca_file)
                if (
                    not path.is_absolute()
                    or any(
                        part in {"..", ".env"} or part.startswith(".env.")
                        for part in path.parts
                    )
                    or any(ord(c) < 32 for c in self.database_ca_file)
                    or not self.database_tls
                ):
                    _invalid()
            if self.database_dsn:
                _dsn(self.database_dsn)
                parsed = urlsplit(self.database_dsn)
                host = parsed.hostname
                # Durable activation leases use session-level advisory locks.
                # Port 6543 is the Supabase transaction endpoint, not a safe
                # shortcut even when automatic prepared statements are disabled.
                if parsed.port == 6543:
                    _invalid()
                supabase_direct = host.endswith(".supabase.co")
                supabase_pooler = host.endswith(".pooler.supabase.com")
                if supabase_direct or supabase_pooler:
                    if not self.database_project_ref or parsed.port not in {None, 5432}:
                        _invalid()
                    if supabase_direct and (
                        self.database_connection_mode != "direct"
                        or host != f"db.{self.database_project_ref}.supabase.co"
                    ):
                        _invalid()
                    if supabase_pooler and (
                        self.database_connection_mode != "session"
                        or not unquote(parsed.username or "").endswith(
                            "." + self.database_project_ref
                        )
                    ):
                        _invalid()
                elif self.database_project_ref:
                    _invalid()
                if not self.database_tls and host not in {
                    "localhost",
                    "127.0.0.1",
                    "::1",
                }:
                    _invalid()
            for name in (
                "manager_origin",
                "webhook_origin",
                "subscription_source_origin",
            ):
                if getattr(self, name):
                    object.__setattr__(self, name, _origin(getattr(self, name)))
            if self.remnawave_api_url:
                if self.remnawave_api_url.endswith("/api/"):
                    origin = self.remnawave_api_url[:-5]
                elif self.remnawave_api_url.endswith("/api"):
                    origin = self.remnawave_api_url[:-4]
                else:
                    _invalid()
                object.__setattr__(self, "remnawave_api_url", _origin(origin) + "/api")
            if self.remnawave_squad_ids:
                normalized = tuple(
                    str(UUID(str(value))) for value in self.remnawave_squad_ids
                )
                if any(UUID(value).int == 0 for value in normalized) or len(
                    set(normalized)
                ) != len(normalized):
                    _invalid()
                object.__setattr__(self, "remnawave_squad_ids", normalized)
            if self.base_domain:
                from services.partner_catalog import validate_base_domain

                object.__setattr__(
                    self, "base_domain", validate_base_domain(self.base_domain)
                )
            if self.manager_bot_token:
                from services.partner_security import validate_bot_token

                validate_bot_token(self.manager_bot_token)
            if self.manager_bot_username and not re.fullmatch(
                r"[A-Za-z0-9_]{5,32}", self.manager_bot_username
            ):
                _invalid()
            if self.encryption_keys:
                from services.partner_security import TokenVault

                TokenVault(self.encryption_keys)
            for value in (self.webhook_key_hex, self.subscription_key_hex):
                if value and not re.fullmatch(r"[0-9a-f]{64}", value):
                    _invalid()
            keys = [
                bytes.fromhex(v)
                for v in (self.webhook_key_hex, self.subscription_key_hex)
                if v
            ]
            import base64

            keys += [base64.urlsafe_b64decode(v) for v in self.encryption_keys]
            if len(set(keys)) != len(keys):
                _invalid()
            if self.entry_only_live:
                from services.partner_entry_pricing import ENTRY_PILOT_OWNER_ID

                if (
                    self.mode != "staging"
                    or not self.enable_billing
                    or self.yookassa_test_mode
                    or sum((self.enable_owner_entry_pilot, self.enable_public_entry_trial,
                            self.enable_public_entry_sales)) > 1
                    or (self.manager_allowed_user_ids != (ENTRY_PILOT_OWNER_ID,)
                        if self.enable_owner_entry_pilot else self.manager_allowed_user_ids is not None)
                    or self.operator_ids != (ENTRY_PILOT_OWNER_ID,)
                    or self.admin_recipient_id != ENTRY_PILOT_OWNER_ID
                    or not all((
                        self.staging_isolation_confirmed,
                        self.database_isolation_confirmed,
                        self.allow_staging_network,
                        self.allow_staging_writes,
                        self.enable_http,
                        self.enable_manager_webhook,
                        self.enable_customer_webhooks,
                        self.enable_launches,
                        self.enable_worker,
                    ))
                    or any((
                        self.enable_provisioning,
                        self.invoice_intents_enabled,
                        self.enable_public_gateway,
                        self.enable_public_checkout,
                        self.enable_notifications,
                        self.enable_marketing_delivery,
                        self.enable_support_delivery,
                        self.enable_finance_notifications,
                    ))
                ):
                    _invalid()
            elif self.enable_billing and not self.yookassa_test_mode:
                _invalid()
            if self.enable_payment_first_entry and not (
                self.enable_public_entry_sales and self.enable_managed_bots
            ):
                _invalid()
        except Exception:
            _invalid()

    @property
    def entry_only_live(self):
        return (self.enable_owner_entry_pilot or self.enable_public_entry_trial
                or self.enable_public_entry_sales)

    @classmethod
    def from_mapping(cls, values):
        try:
            if not isinstance(values, Mapping) or set(values) - {
                f.name for f in fields(cls)
            }:
                _invalid()
            return cls(**values)
        except Exception:
            _invalid()

    def preflight(self):
        """Pure codes only. This is configuration consistency, not remote proof."""
        if self.mode == "disabled":
            return {
                "mode": "disabled",
                "configured": False,
                "production_ready": False,
                "blockers": ["runtime_disabled"],
                "external_checks_performed": False,
            }
        blockers = []

        def require(ok, code):
            if not ok:
                blockers.append(code)

        require(self.staging_isolation_confirmed, "isolated_staging_not_confirmed")
        require(
            self.database_isolation_confirmed, "separate_partner_database_not_confirmed"
        )
        require(bool(self.database_dsn), "staging_database_missing")
        if self.database_project_ref:
            require(
                self.database_api_disabled_confirmed,
                "supabase_data_api_disabled_not_confirmed",
            )
        require(
            bool(self.base_domain and self.manager_origin and self.manager_bot_token),
            "manager_configuration_missing",
        )
        require(bool(self.encryption_keys), "token_vault_missing")
        writes = any(
            (
                self.enable_http,
                self.enable_managed_bots,
                self.enable_worker,
                self.enable_billing,
                self.enable_provisioning,
                self.enable_launches,
                self.enable_notifications,
                self.enable_entry_launch_notifications,
                self.enable_marketing_delivery,
                self.enable_support_delivery,
                self.enable_finance_notifications,
                self.finance_enabled,
                self.management_enabled,
                self.branding_enabled,
            )
        )
        if writes:
            require(self.allow_staging_writes, "staging_writes_not_authorized")
        network = any(
            (
                self.enable_http,
                self.enable_managed_bots,
                self.enable_billing,
                self.enable_provisioning,
                self.enable_launches,
                self.enable_notifications,
                self.enable_entry_launch_notifications,
                self.enable_marketing_delivery,
                self.enable_support_delivery,
                self.enable_finance_notifications,
                self.enable_public_gateway,
            )
        )
        if network:
            require(self.allow_staging_network, "staging_network_not_authorized")
        if self.enable_billing:
            if not self.entry_only_live:
                require(self.invoice_intents_enabled, "invoice_intent_disabled")
            require(
                self.enable_manager_webhook and bool(self.manager_bot_username),
                "central_manager_onboarding_missing",
            )
            require(
                bool(self.yookassa_shop_id and self.yookassa_secret_key),
                "test_merchant_missing",
            )
            if not self.entry_only_live:
                require(
                    self.enable_provisioning and self.enable_worker,
                    "billing_requires_provisioning_worker",
                )
                require(
                    self.addon_catalog_reviewed or self.staging_no_addons_confirmed,
                    "main_addon_prices_not_reviewed",
                )
        if self.invoice_intents_enabled:
            require(self.enable_provisioning, "invoice_intent_requires_entitlements")
        if self.enable_provisioning or self.enable_public_gateway:
            require(
                bool(
                    self.remnawave_api_url
                    and self.remnawave_api_token
                    and self.remnawave_squad_ids
                    and self.subscription_source_origin
                ),
                "staging_panel_missing",
            )
        if (
            self.enable_customer_webhooks
            or self.enable_launches
            or self.enable_manager_webhook
        ):
            require(
                self.enable_http and bool(self.webhook_origin and self.webhook_key_hex),
                "webhook_configuration_missing",
            )
        if self.enable_manager_webhook:
            require(bool(self.manager_bot_username), "manager_bot_username_missing")
        if self.enable_managed_bots:
            require(
                self.enable_http and self.enable_manager_webhook
                and bool(self.manager_bot_token and self.manager_bot_username and self.encryption_keys),
                "managed_bots_require_http_manager_vault",
            )
        if self.enable_launches:
            require(
                self.enable_customer_webhooks and self.enable_worker,
                "launch_requires_webhook_worker",
            )
        if self.enable_public_gateway:
            require(
                self.enable_http
                and self.enable_provisioning
                and self.enable_worker
                and bool(self.subscription_key_hex),
                "gateway_configuration_missing",
            )
        if self.enable_notifications:
            require(self.enable_worker, "notifications_require_worker")
        if self.enable_entry_launch_notifications:
            require(
                self.enable_billing and self.enable_launches and self.enable_worker
                and self.enable_manager_webhook
                and bool(self.manager_bot_token and self.manager_bot_username),
                "entry_launch_notifications_require_billing_launch_worker_manager",
            )
        if self.enable_public_checkout:
            require(
                self.enable_public_gateway
                and self.enable_billing
                and self.invoice_intents_enabled,
                "public_checkout_requires_gateway_billing_intents",
            )
        if self.enable_marketing_delivery or self.enable_support_delivery:
            require(
                self.management_enabled and self.enable_worker,
                "management_delivery_requires_store_worker",
            )
        if self.enable_finance_notifications:
            require(
                self.finance_enabled and self.enable_worker,
                "finance_delivery_requires_store_worker",
            )
        if self.finance_enabled:
            require(
                self.admin_recipient_id in self.operator_ids,
                "finance_operator_missing",
            )
        return {
            "mode": "staging",
            "configured": not blockers,
            "production_ready": False,
            "blockers": blockers,
            "external_checks_performed": False,
        }


REQUIRED_COLUMNS = {
    "wl_projects": {"id", "status", "encrypted_token"},
    "wl_customers": {"project_id", "tg_id"},
    "wl_subscriptions": {
        "id",
        "traffic_cycle_baseline_bytes",
        "public_link_generation",
    },
    "wl_orders": {"id", "purpose", "activation_issue", "invoice_requested_at"},
    "wl_activation_jobs": {"job_id", "kind", "blocked_reason", "next_attempt_at"},
    "wl_traffic_credits": {"order_id", "state", "next_attempt_at"},
    "wl_entry_orders": {"id", "status"},
    "wl_launch_jobs": {"order_id", "needs_webhook_replace", "next_attempt_at"},
    "wl_customer_outbox": {"id", "delivery_state", "lease_token"},
    "wl_partner_outbox": {"id", "delivery_state", "lease_token"},
}

# Read-only fail-closed guard against an accidentally reused main-SPN DSN.
# These are the exact public table signatures from database.py, not all tables
# named `users` that Supabase itself may keep in its separate auth schema.
MAIN_SPN_SIGNATURES = {
    "users": {"tg_id", "remnawave_uuid", "accepted_terms"},
    "payments": {"tg_id", "tariff_code", "invoice_id", "provider"},
    "subscriptions": {"tg_id", "slot_number", "remnawave_uuid"},
}

ENTRY_LAUNCH_NOTIFICATION_COLUMNS = {
    "wl_partner_outbox": {
        "id", "project_id", "order_id", "owner_tg_id", "kind", "status",
        "delivery_state", "lease_token", "lease_until", "next_attempt_at",
        "attempts", "telegram_message_id",
    },
    "wl_projects": {"owner_tg_id", "bot_id", "bot_username"},
    "wl_entry_orders": {
        "project_id", "owner_tg_id", "bot_id", "price_kopecks", "paid_at", "launched_at",
    },
    "wl_launch_jobs": {"project_id", "owner_tg_id", "bot_id", "status", "completed_at"},
}

MANAGED_BOT_REQUIRED_COLUMNS = {
    "project_id", "owner_tg_id", "project_version", "status", "bot_id",
    "last_update_id", "created_at", "expires_at", "bound_at",
}

# Only this explicit migration permits the new event. Merely having delivery
# columns or a broad/unchecked kind constraint is not sufficient.
ENTRY_LAUNCH_NOTIFICATIONS_SCHEMA_CHECK_SQL = """SELECT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='public.wl_partner_outbox'::regclass
      AND conname='wl_partner_outbox_kind_check' AND contype='c' AND convalidated
      AND lower(regexp_replace(
          replace(replace(pg_get_expr(conbin,conrelid), '::text', ''), '''', ''),
          '[[:space:]]', '', 'g'))='((kind=entry_paid)or(kind=entry_launched))'
) AS kind_constraint_safe"""

SUPABASE_ACCESS_CHECK_SQL = """SELECT
    current_user='spn_partner_runtime' AND EXISTS (
        SELECT 1 FROM pg_roles r WHERE r.rolname=current_user
          AND r.rolcanlogin AND NOT r.rolsuper AND NOT r.rolcreatedb
          AND NOT r.rolcreaterole AND NOT r.rolreplication AND NOT r.rolbypassrls
          AND NOT EXISTS (
              SELECT 1 FROM pg_auth_members m
              WHERE m.member=r.oid OR (m.roleid=r.oid AND NOT (
                  m.member=(SELECT oid FROM pg_roles WHERE rolname='postgres')
                  AND m.grantor IN (SELECT oid FROM pg_roles WHERE rolname IN ('postgres','supabase_admin'))
                  AND m.admin_option
                  AND (to_jsonb(m)->>'inherit_option')::boolean IS FALSE
                  AND (to_jsonb(m)->>'set_option')::boolean IS FALSE
              ))
          )
    ) AS role_safe,
    NOT has_schema_privilege(current_user,'public','CREATE') AS no_schema_create,
    NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_roles owner_role ON owner_role.oid=c.relowner
        WHERE n.nspname='public' AND c.relname LIKE 'wl\\_%' ESCAPE '\\'
          AND c.relkind IN ('r','p') AND (
            owner_role.rolname=current_user OR NOT c.relrowsecurity
            OR NOT has_table_privilege(current_user,c.oid,'SELECT')
            OR NOT has_table_privilege(current_user,c.oid,'INSERT')
            OR NOT has_table_privilege(current_user,c.oid,'UPDATE')
            OR NOT has_table_privilege(current_user,c.oid,'DELETE')
            OR NOT EXISTS (
                SELECT 1 FROM pg_policy p WHERE p.polrelid=c.oid
                  AND p.polname='wl_runtime_server' AND p.polcmd='*'
                  AND p.polroles=ARRAY[(SELECT oid FROM pg_roles WHERE rolname=current_user)]
                  AND pg_get_expr(p.polqual,p.polrelid)='true'
                  AND pg_get_expr(p.polwithcheck,p.polrelid)='true'
            )
          )
    ) AS runtime_tables_safe,
    NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,
            acldefault(CASE WHEN c.relkind='S' THEN 'S'::\"char\" ELSE 'r'::\"char\" END,c.relowner))) a
        LEFT JOIN pg_roles r ON r.oid=a.grantee
        WHERE n.nspname='public' AND c.relname LIKE 'wl\\_%' ESCAPE '\\'
          AND c.relkind IN ('r','p','S','v','m','f')
          AND (a.grantee=0 OR r.rolname IN ('anon','authenticated','service_role'))
    ) AS api_objects_revoked,
    NOT EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
        LEFT JOIN pg_roles r ON r.oid=a.grantee
        WHERE n.nspname='public' AND p.proname LIKE 'wl\\_%' ESCAPE '\\'
          AND (a.grantee=0 OR r.rolname IN ('anon','authenticated','service_role'))
    ) AS api_functions_revoked"""


OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL = """SELECT
    EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='public.wl_entry_orders'::regclass
          AND conname='wl_entry_orders_price_kopecks_check'
          AND contype='c' AND convalidated
          AND lower(regexp_replace(
              replace(replace(pg_get_expr(conbin,conrelid), '::bigint', ''), '''', ''),
              '[[:space:]]', '', 'g')) IN (
              '((price_kopecks=999000)or((price_kopecks=1000)and(owner_tg_id=6910097562)))',
              '((price_kopecks=999000)or(price_kopecks=1000))')
    ) AS price_constraint_safe,
    EXISTS (
        SELECT 1 FROM pg_index i
        JOIN pg_class c ON c.oid=i.indexrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attname='owner_tg_id'
        WHERE n.nspname='public'
          AND c.relname='wl_entry_orders_one_open_owner_pilot'
          AND i.indrelid='public.wl_entry_orders'::regclass
          AND i.indisunique AND i.indisvalid AND i.indisready
          AND i.indkey::text=a.attnum::text
          AND lower(regexp_replace(
              replace(replace(replace(pg_get_expr(i.indpred,i.indrelid),
                  '::text', ''), '::bigint', ''), '''', ''),
              '[[:space:]]', '', 'g'))=
              '((price_kopecks=1000)and(status<>canceled))'
    ) AS owner_pilot_unique_safe,
    EXISTS (
        SELECT 1 FROM pg_trigger t
        JOIN pg_proc p ON p.oid=t.tgfoid
        JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_language l ON l.oid=p.prolang
        WHERE t.tgrelid='public.wl_entry_orders'::regclass
          AND t.tgname='wl_entry_financial_snapshot_guard'
          AND t.tgenabled IN ('O','A') AND t.tgtype=19 AND NOT t.tgisinternal
          AND t.tgattr::text='' AND t.tgqual IS NULL AND t.tgconstraint=0
          AND n.nspname='public' AND p.proname='wl_guard_entry_financial_snapshot'
          AND p.pronargs=0 AND p.prorettype='trigger'::regtype
          AND NOT p.prosecdef AND l.lanname='plpgsql'
          AND cardinality(p.proconfig)=1
          AND regexp_replace(p.proconfig[1], '[[:space:]]', '', 'g')='search_path=pg_catalog,public'
          AND regexp_replace(p.prosrc, '[[:space:]]', '', 'g')=
              regexp_replace($spn_expected_body$
BEGIN
    IF ROW(OLD.price_kopecks, OLD.owner_tg_id, OLD.project_id, OLD.request_key,
           OLD.bot_id, OLD.purpose, OLD.currency)
       IS DISTINCT FROM
       ROW(NEW.price_kopecks, NEW.owner_tg_id, NEW.project_id, NEW.request_key,
           NEW.bot_id, NEW.purpose, NEW.currency) THEN
        RAISE EXCEPTION 'Entry financial snapshot is immutable'
            USING ERRCODE='23514', CONSTRAINT='wl_entry_financial_snapshot_immutable';
    END IF;
    RETURN NEW;
END;
$spn_expected_body$, '[[:space:]]', '', 'g')
    ) AS financial_snapshot_guard_safe"""

# Both exact guard implementations are valid during a rolling release. The
# second allows only a proved paid NULL -> verified Managed Bot attachment;
# money, owner, project, request and currency still cannot change.
_LEGACY_ENTRY_FINANCIAL_GUARD_BODY = OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL.split("$spn_expected_body$")[1]
_PAYMENT_FIRST_FINANCIAL_GUARD_BODY = """
BEGIN
    IF ROW(OLD.price_kopecks, OLD.owner_tg_id, OLD.project_id, OLD.request_key,
           OLD.purpose, OLD.currency)
       IS DISTINCT FROM
       ROW(NEW.price_kopecks, NEW.owner_tg_id, NEW.project_id, NEW.request_key,
           NEW.purpose, NEW.currency) THEN
        RAISE EXCEPTION 'Entry financial snapshot is immutable'
            USING ERRCODE='23514', CONSTRAINT='wl_entry_financial_snapshot_immutable';
    END IF;
    IF OLD.bot_id IS DISTINCT FROM NEW.bot_id AND NOT (
        OLD.bot_id IS NULL AND NEW.bot_id IS NOT NULL
        AND OLD.status='paid_pending_launch' AND NEW.status=OLD.status
        AND OLD.paid_at IS NOT NULL AND NEW.paid_at=OLD.paid_at
        AND OLD.provider_payment_id IS NOT NULL
        AND NEW.provider_payment_id=OLD.provider_payment_id
        AND EXISTS (
            SELECT 1 FROM public.wl_managed_bot_requests r
            JOIN public.wl_projects p ON p.id=r.project_id AND p.owner_tg_id=r.owner_tg_id
            WHERE r.project_id=OLD.project_id AND r.owner_tg_id=OLD.owner_tg_id
              AND r.status='pending' AND r.bot_id=NEW.bot_id
              AND p.bot_id=NEW.bot_id AND p.status IN ('draft','ready')
        )
    ) THEN
        RAISE EXCEPTION 'Entry bot identity is immutable'
            USING ERRCODE='23514', CONSTRAINT='wl_entry_financial_snapshot_immutable';
    END IF;
    RETURN NEW;
END;
"""
_PAYMENT_FIRST_PROJECT_GUARD_BODY = """
BEGIN
    IF OLD.bot_id IS DISTINCT FROM NEW.bot_id AND
       (OLD.status='active' OR EXISTS(SELECT 1 FROM public.wl_entry_orders
            WHERE project_id=OLD.id AND status<>'canceled')) AND NOT (
        OLD.bot_id IS NULL AND NEW.bot_id IS NOT NULL
        AND OLD.status IN ('draft','ready') AND NEW.status=OLD.status
        AND EXISTS (
            SELECT 1 FROM public.wl_entry_orders o
            JOIN public.wl_managed_bot_requests r
              ON r.project_id=o.project_id AND r.owner_tg_id=o.owner_tg_id
            WHERE o.project_id=OLD.id AND o.owner_tg_id=OLD.owner_tg_id
              AND o.status='paid_pending_launch' AND o.bot_id IS NULL
              AND o.paid_at IS NOT NULL AND o.provider_payment_id IS NOT NULL
              AND r.status='pending' AND r.bot_id=NEW.bot_id
        )
    ) THEN
        RAISE EXCEPTION 'Bot identity is locked by an entry order'
            USING ERRCODE='23514', CONSTRAINT='wl_entry_bot_identity_locked';
    END IF;
    RETURN NEW;
END;
"""
OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL = OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL.replace(
    "AND regexp_replace(p.prosrc, '[[:space:]]', '', 'g')=",
    "AND regexp_replace(p.prosrc, '[[:space:]]', '', 'g') IN (",
).replace(
    "$spn_expected_body$, '[[:space:]]', '', 'g')\n    ) AS financial_snapshot_guard_safe",
    "$spn_expected_body$, '[[:space:]]', '', 'g'),\n"
    "              regexp_replace($spn_paid_bot$" + _PAYMENT_FIRST_FINANCIAL_GUARD_BODY
    + "$spn_paid_bot$, '[[:space:]]', '', 'g'))\n    ) AS financial_snapshot_guard_safe",
)


def _exact_entry_trigger_guard(table, trigger, function, body, *, update_column=None):
    # Fixed internal identifiers/bodies only; never interpolates configuration.
    trigger_columns = ("t.tgattr::text=''" if update_column is None else
        "t.tgattr::text=(SELECT a.attnum::text FROM pg_attribute a "
        f"WHERE a.attrelid='public.{table}'::regclass AND a.attname='{update_column}' "
        "AND NOT a.attisdropped)")
    return f"""EXISTS (
        SELECT 1 FROM pg_trigger t
        JOIN pg_proc p ON p.oid=t.tgfoid
        JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_language l ON l.oid=p.prolang
        WHERE t.tgrelid='public.{table}'::regclass
          AND t.tgname='{trigger}' AND t.tgenabled IN ('O','A')
          AND t.tgtype=19 AND NOT t.tgisinternal AND {trigger_columns}
          AND t.tgqual IS NULL AND t.tgconstraint=0
          AND n.nspname='public' AND p.proname='{function}'
          AND p.pronargs=0 AND p.prorettype='trigger'::regtype
          AND NOT p.prosecdef AND l.lanname='plpgsql'
          AND cardinality(p.proconfig)=1
          AND regexp_replace(p.proconfig[1], '[[:space:]]', '', 'g')='search_path=pg_catalog,public'
          AND regexp_replace(p.prosrc, '[[:space:]]', '', 'g')=
              regexp_replace($spn_expected_body${body}$spn_expected_body$, '[[:space:]]', '', 'g')
    )"""


PAYMENT_FIRST_ENTRY_SCHEMA_CHECK_SQL = """SELECT
    EXISTS (SELECT 1 FROM pg_attribute
        WHERE attrelid='public.wl_entry_orders'::regclass AND attname='bot_id'
          AND NOT attnotnull AND NOT attisdropped) AS bot_identity_nullable_safe,
    EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid='public.wl_entry_orders'::regclass
          AND conname='wl_entry_order_launched_bot_required'
          AND contype='c' AND convalidated
          AND lower(regexp_replace(
              replace(replace(pg_get_expr(conbin,conrelid),'::text',''),'''',''),
              '[[:space:]]','','g'))='((status<>launched)or(bot_idisnotnull))'
    ) AS launched_bot_required_safe,
    """ + _exact_entry_trigger_guard(
        "wl_entry_orders", "wl_entry_financial_snapshot_guard", "wl_guard_entry_financial_snapshot",
        _PAYMENT_FIRST_FINANCIAL_GUARD_BODY,
    ) + " AS financial_snapshot_guard_safe,\n" + _exact_entry_trigger_guard(
        "wl_projects", "wl_entry_bot_identity_guard", "wl_guard_entry_bot_identity",
        _PAYMENT_FIRST_PROJECT_GUARD_BODY, update_column="bot_id",
    ) + " AS project_bot_identity_guard_safe"

# Public access requires the explicitly broadened price constraint, not the
# former owner-only migration. All other exact money safeguards are identical.
PUBLIC_ENTRY_TRIAL_SCHEMA_CHECK_SQL = OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL.replace(
    "'((price_kopecks=999000)or((price_kopecks=1000)and(owner_tg_id=6910097562)))',\n              ", ""
)


class _UnavailableVerifier:
    async def verify(self, token):
        from services.partner_security import BotConnectionError

        raise BotConnectionError("Сетевая проверка бота на этом стенде выключена.")


@dataclass(repr=False)
class PartnerRuntime:
    settings: PartnerRuntimeSettings
    pool_getter: Callable | None = field(default=None, repr=False)
    addon_loader: Callable | None = field(default=None, repr=False)
    catalog_loader: Callable | None = field(default=None, repr=False)
    extra_router_builder: Callable | None = field(default=None, repr=False)
    partner_settings: object | None = field(default=None, repr=False)
    registry: object | None = field(default=None, repr=False)
    commerce: object | None = field(default=None, repr=False)
    enrollment: object | None = field(default=None, repr=False)
    checkout: object | None = field(default=None, repr=False)
    entry: object | None = field(default=None, repr=False)
    launcher: object | None = field(default=None, repr=False)
    manager_bootstrap: object | None = field(default=None, repr=False)
    managed_bots: object | None = field(default=None, repr=False)
    subscriptions: object | None = field(default=None, repr=False)
    public_checkout: object | None = field(default=None, repr=False)
    vault: object | None = field(default=None, repr=False)
    recovery: object | None = field(default=None, repr=False)
    notifications: object | None = field(default=None, repr=False)
    entry_launch_notifications: object | None = field(default=None, repr=False)
    _app: object | None = field(default=None, repr=False)
    _extensions: dict | None = field(default=None, repr=False)
    _hooks: list = field(default_factory=list, repr=False)

    def __repr__(self):
        return f"<PartnerRuntime mode={self.settings.mode!r}>"

    def register_worker_hook(self, name, callback, *, enabled=False):
        if (
            type(enabled) is not bool
            or not isinstance(name, str)
            or not re.fullmatch(r"[a-z_]{1,40}", name)
        ):
            _invalid()
        if enabled:
            if not callable(callback) or any(name == row[0] for row in self._hooks):
                _invalid()
            self._hooks.append((name, callback))

    def extensions(self):
        if self._extensions is None:
            if self.extra_router_builder is None and any(
                (
                    self.settings.management_enabled,
                    self.settings.finance_enabled,
                    self.settings.branding_enabled,
                    self.settings.enable_public_checkout,
                )
            ):
                from partner_platform_web import build_platform_extensions

                self.extra_router_builder = build_platform_extensions
            result = (
                self.extra_router_builder(self) if self.extra_router_builder else {}
            )
            if not isinstance(result, dict) or set(result) - {
                "core_kwargs",
                "routers",
                "worker_hooks",
                "public_checkout",
            }:
                _invalid()
            kwargs = result.get("core_kwargs", {})
            if not isinstance(kwargs, dict) or set(kwargs) - {
                "management_store",
                "finance_store",
                "brand_asset_store",
                "operator_ids",
            }:
                _invalid()
            for row in result.get("worker_hooks", ()):
                if not isinstance(row, (tuple, list)) or len(row) != 3:
                    _invalid()
                self.register_worker_hook(*row[:2], enabled=row[2])
            if self.settings.enable_public_checkout:
                self.public_checkout = result.get("public_checkout")
                if (
                    self.public_checkout is None
                    or self.public_checkout.enabled is not True
                ):
                    _invalid()
            self._extensions = result
        return self._extensions

    async def probe(self):
        """Explicit bounded read-only schema/queue diagnostics. No remote APIs."""
        report = self.settings.preflight()
        report = {
            **report,
            "database_checked": False,
            "database_ready": False,
            "manual_review": {},
            "queue_health": {},
        }
        if (
            self.settings.mode != "staging"
            or not report["configured"]
            or self.pool_getter is None
        ):
            return report
        try:
            return await asyncio.wait_for(
                self._probe_database(report),
                timeout=self.settings.phase_timeout_seconds,
            )
        except Exception:
            report["database_checked"] = True
            report["blockers"] = [*report["blockers"], "staging_database_unavailable"]
            report["configured"] = False
            return report

    async def _probe_database(self, report):
        value = self.pool_getter()
        pool = await value if inspect.isawaitable(value) else value
        expected = {table: set(columns) for table, columns in REQUIRED_COLUMNS.items()}
        if self.settings.enable_entry_launch_notifications:
            for table, columns in ENTRY_LAUNCH_NOTIFICATION_COLUMNS.items():
                expected[table].update(columns)
        if self.settings.enable_managed_bots:
            expected["wl_managed_bot_requests"] = set(MANAGED_BOT_REQUIRED_COLUMNS)
        if self.settings.panel_identity_enabled:
            expected["wl_subscriptions"].add("remote_identity")
        if self.settings.finance_enabled:
            expected.update(
                {"wl_withdrawals": {"id", "status"}, "wl_finance_outbox": {"id"}}
            )
            if self.settings.enable_finance_notifications:
                expected["wl_finance_outbox"].update(
                    {"delivery_state", "lease_token", "next_attempt_at"}
                )
        if self.settings.management_enabled:
            expected.update(
                {
                    "wl_project_members": {"project_id", "tg_id"},
                    "wl_campaigns": {"id"},
                    "wl_marketing_broadcasts": {"id"},
                }
            )
        if self.settings.enable_marketing_delivery:
            expected["wl_marketing_recipients"] = {
                "project_id",
                "status",
                "lease_token",
                "next_attempt_at",
            }
        if self.settings.enable_support_delivery:
            expected["wl_support_resend_intents"] = {
                "project_id",
                "status",
                "lease_token",
                "next_attempt_at",
            }
        if self.settings.branding_enabled:
            expected.update(
                {
                    "wl_brand_assets": {"project_id", "asset_id", "png"},
                    "wl_brand_asset_audit": {"project_id", "action"},
                }
            )
        if self.settings.enable_public_checkout:
            expected["wl_public_renewal_requests"] = {
                "project_id",
                "subscription_id",
                "request_key",
                "order_id",
            }
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                rows = await conn.fetch(
                    """SELECT table_name,column_name FROM information_schema.columns
                    WHERE table_schema='public' AND table_name=ANY($1::text[])""",
                    list(expected) + list(MAIN_SPN_SIGNATURES),
                )
                found = {}
                for row in rows:
                    found.setdefault(row["table_name"], set()).add(row["column_name"])
                if any(
                    columns <= found.get(table, set())
                    for table, columns in MAIN_SPN_SIGNATURES.items()
                ):
                    return {
                        **report,
                        "configured": False,
                        "database_checked": True,
                        "blockers": [*report["blockers"], "main_spn_database_detected"],
                    }
                if any(
                    not columns <= found.get(table, set())
                    for table, columns in expected.items()
                ):
                    return {
                        **report,
                        "configured": False,
                        "database_checked": True,
                        "blockers": [
                            *report["blockers"],
                            "explicit_migrations_required",
                        ],
                    }
                if self.settings.database_project_ref:
                    access = await conn.fetchrow(SUPABASE_ACCESS_CHECK_SQL)
                    if not access or not all(
                        value is True for value in dict(access).values()
                    ):
                        return {
                            **report,
                            "configured": False,
                            "database_checked": True,
                            "blockers": [
                                *report["blockers"],
                                "supabase_access_policy_required",
                            ],
                        }
                if self.settings.entry_only_live:
                    pilot_guard = await conn.fetchrow(
                        PUBLIC_ENTRY_TRIAL_SCHEMA_CHECK_SQL if (self.settings.enable_public_entry_trial
                                                               or self.settings.enable_public_entry_sales)
                        else OWNER_ENTRY_PILOT_SCHEMA_CHECK_SQL
                    )
                    expected_guard_keys = {
                        "price_constraint_safe", "owner_pilot_unique_safe",
                        "financial_snapshot_guard_safe",
                    }
                    if (
                        not pilot_guard
                        or set(dict(pilot_guard)) != expected_guard_keys
                        or not all(value is True for value in dict(pilot_guard).values())
                    ):
                        return {
                            **report,
                            "configured": False,
                            "database_checked": True,
                            "blockers": [*report["blockers"],
                                "public_entry_sales_migration_required" if self.settings.enable_public_entry_sales
                                else "public_entry_trial_migration_required" if self.settings.enable_public_entry_trial
                                else "owner_entry_pilot_migration_required"],
                        }
                if self.settings.enable_payment_first_entry:
                    payment_first_guard = await conn.fetchrow(PAYMENT_FIRST_ENTRY_SCHEMA_CHECK_SQL)
                    expected_guard_keys = {
                        "bot_identity_nullable_safe", "launched_bot_required_safe",
                        "financial_snapshot_guard_safe", "project_bot_identity_guard_safe",
                    }
                    if (
                        not payment_first_guard
                        or set(dict(payment_first_guard)) != expected_guard_keys
                        or not all(value is True for value in dict(payment_first_guard).values())
                    ):
                        return {
                            **report, "configured": False, "database_checked": True,
                            "blockers": [*report["blockers"], "payment_first_entry_migration_required"],
                        }
                if self.settings.enable_entry_launch_notifications:
                    notice_guard = await conn.fetchrow(ENTRY_LAUNCH_NOTIFICATIONS_SCHEMA_CHECK_SQL)
                    if (
                        not notice_guard
                        or set(dict(notice_guard)) != {"kind_constraint_safe"}
                        or dict(notice_guard)["kind_constraint_safe"] is not True
                    ):
                        return {
                            **report, "configured": False, "database_checked": True,
                            "blockers": [*report["blockers"], "entry_launch_notifications_migration_required"],
                        }
                if self.settings.panel_identity_enabled:
                    identity_rows = await conn.fetchrow(
                        "SELECT count(*) AS missing FROM wl_subscriptions WHERE remote_identity IS NULL"
                    )
                    if identity_rows is None or identity_rows["missing"] != 0:
                        return {
                            **report,
                            "configured": False,
                            "database_checked": True,
                            "blockers": [
                                *report["blockers"],
                                "panel_identity_backfill_required",
                            ],
                        }
                review = await conn.fetchrow(
                    """SELECT
                    (SELECT count(*) FROM wl_activation_jobs WHERE status<>'complete'
                        AND blocked_reason='activation_expired') AS activation_expired,
                    (SELECT count(*) FROM wl_activation_jobs WHERE status<>'complete'
                        AND expires_at<=now() AND blocked_reason IS NULL) AS expired_unclassified,
                    (SELECT count(*) FROM wl_subscriptions WHERE traffic_cycle_hold IS NOT NULL) AS traffic_holds,
                    (SELECT count(*) FROM wl_launch_jobs WHERE status<>'complete'
                        AND needs_webhook_replace) AS webhook_consent_required,
                    (SELECT count(*) FROM wl_orders WHERE status='created' AND provider_payment_id IS NULL
                        AND invoice_requested_at<=now()-interval '23 hours') AS invoice_recovery_required"""
                )
                queues = await conn.fetchrow(
                    """SELECT
                    (SELECT count(*) FROM wl_customer_outbox WHERE delivery_state='uncertain') AS customer_delivery_uncertain,
                    (SELECT count(*) FROM wl_partner_outbox WHERE delivery_state='uncertain') AS partner_delivery_uncertain,
                    (SELECT count(*) FROM wl_activation_jobs WHERE status<>'complete') AS activation_pending,
                    (SELECT GREATEST(0,EXTRACT(EPOCH FROM (now()-MIN(created_at))))::bigint
                        FROM wl_activation_jobs WHERE status<>'complete') AS activation_oldest_seconds,
                    (SELECT GREATEST(0,EXTRACT(EPOCH FROM (now()-MIN(paid_at))))::bigint
                        FROM wl_orders WHERE status='paid_pending_activation') AS paid_activation_oldest_seconds,
                    (SELECT GREATEST(0,EXTRACT(EPOCH FROM (now()-MIN(available_at))))::bigint
                        FROM wl_traffic_credits WHERE state='waiting' AND available_at<=now()) AS traffic_due_oldest_seconds,
                    (SELECT GREATEST(0,EXTRACT(EPOCH FROM (now()-MIN(paid_at))))::bigint
                        FROM wl_entry_orders WHERE status='paid_pending_launch') AS entry_pending_oldest_seconds"""
                )
                queues = dict(queues)
                if self.settings.finance_enabled:
                    queues.update(
                        await conn.fetchrow(
                            """SELECT
                        (SELECT count(*) FROM wl_withdrawals WHERE status='pending') AS withdrawals_pending,
                        (SELECT count(*) FROM wl_withdrawals WHERE status='processing') AS withdrawals_processing"""
                        )
                    )
                if self.settings.enable_finance_notifications:
                    queues.update(
                        await conn.fetchrow(
                            """SELECT count(*) AS finance_delivery_uncertain
                        FROM wl_finance_outbox WHERE delivery_state='uncertain'"""
                        )
                    )
                if self.settings.enable_marketing_delivery:
                    queues.update(
                        await conn.fetchrow(
                            """SELECT count(*) AS marketing_delivery_uncertain
                        FROM wl_marketing_recipients WHERE status='uncertain'"""
                        )
                    )
                if self.settings.enable_support_delivery:
                    queues.update(
                        await conn.fetchrow(
                            """SELECT count(*) AS support_delivery_uncertain
                        FROM wl_support_resend_intents WHERE status='uncertain'"""
                        )
                    )
        return {
            **report,
            "database_checked": True,
            "database_ready": True,
            "manual_review": _safe_counts(review),
            "queue_health": _safe_counts(queues),
        }

    def app(self):
        """Build routes only. No lifespan migrations, worker or socket startup."""
        if self._app is not None:
            return self._app
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from fastapi.staticfiles import StaticFiles
        from pathlib import Path

        app = FastAPI(
            title="Partner isolated staging",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
            debug=False,
        )

        @app.exception_handler(Exception)
        async def redacted_error(request, error):
            return JSONResponse(
                {"detail": "Стенд временно недоступен."},
                status_code=503,
                headers={"cache-control": "no-store"},
            )

        @app.get("/healthz", include_in_schema=False)
        async def health():
            return JSONResponse(
                {
                    "status": "alive",
                    "mode": self.settings.mode,
                    "production_ready": False,
                },
                headers={"cache-control": "no-store"},
            )

        # Liveness is public and does not query a database. Detailed readiness
        # stays an explicit local CLI probe, not an unauthenticated queue dump.
        if self.settings.mode == "staging" and self.settings.enable_http:
            from partner_web import create_router

            extensions = self.extensions()
            kwargs = extensions.get("core_kwargs", {})
            # Central enrollment and tenant administration are different trust
            # boundaries. Never mount the legacy all-in-one central surface.
            for surface, route_base in (
                ("enrollment", "/partners"),
                ("workspace", "/partners/workspace/{workspace_project_id}"),
            ):
                app.include_router(create_router(
                    self.registry,
                    self.partner_settings,
                    surface=surface,
                    route_base=route_base,
                    verifier=(
                        None
                        if self.settings.allow_staging_network
                        else _UnavailableVerifier()
                    ),
                    addon_loader=self.catalog_loader,
                    commerce_store=self.commerce,
                    enrollment_store=self.enrollment,
                    entry_checkout=self.entry,
                    managed_service=self.managed_bots,
                    manager_allowed_user_ids=self.settings.manager_allowed_user_ids,
                    **(
                        {"customer_checkout": self.checkout}
                        if self.settings.enable_billing
                        else {}
                    ),
                    **kwargs,
                ))
            # Separate central-manager proof: tenant bot owners must never be
            # able to forge a platform operator identity with their bot token.
            from partner_support_web import create_support_router
            from services.partner_support import PartnerSupportStore

            app.include_router(create_support_router(
                PartnerSupportStore(self.pool_getter, operator_ids=self.settings.operator_ids),
                self.partner_settings, operator_ids=self.settings.operator_ids,
            ))
            assets = (
                Path(__file__).resolve().parents[1] / "static" / "partners" / "assets"
            )
            app.mount(
                "/partners/assets", StaticFiles(directory=assets), name="partner-assets"
            )
            for router in extensions.get("routers", ()):
                app.include_router(router)
            if self.settings.enable_manager_webhook:
                from partner_manager_web import create_manager_router
                from services.partner_manager import PartnerManagerHandler

                bot_id = int(self.settings.manager_bot_token.split(":", 1)[0])
                handler = PartnerManagerHandler(
                    bot_id,
                    self.settings.manager_origin,
                    username=self.settings.manager_bot_username,
                    manager_allowed_user_ids=self.settings.manager_allowed_user_ids,
                    managed_service=self.managed_bots,
                    operator_ids=self.settings.operator_ids,
                    entry_pricing=self.enrollment.entry_pricing,
                )
                app.include_router(
                    create_manager_router(
                        handler,
                        bot_id=bot_id,
                        secret_key=bytes.fromhex(self.settings.webhook_key_hex),
                        webhook_origin=self.settings.webhook_origin,
                        enabled=True,
                    )
                )
            if self.settings.enable_customer_webhooks:
                from partner_client_web import create_client_router
                from services.partner_bot import PartnerBotHandler
                from services.partner_callback_ack import PartnerCallbackAcknowledger

                handler = PartnerBotHandler(
                    self.commerce,
                    self.checkout,
                    (
                        "https://t.me/" + self.settings.manager_bot_username
                        if self.settings.manager_bot_username
                        else self.settings.manager_origin + "/partners"
                    ),
                    subscription_pages=self.subscriptions,
                    management=kwargs.get("management_store"),
                    workspace_origin=self.settings.manager_origin,
                    operator_ids=self.settings.operator_ids,
                    manager_username=self.settings.manager_bot_username,
                )
                app.include_router(
                    create_client_router(
                        self.registry.get_runtime_project,
                        handler,
                        context_loader=self.registry.get_bot_update_context,
                        secret_key=bytes.fromhex(self.settings.webhook_key_hex),
                        enabled=True,
                        callback_acknowledger=PartnerCallbackAcknowledger(
                            self.registry.get_workspace_auth_project, self.vault
                        ),
                    )
                )
            if self.settings.enable_public_gateway:
                from partner_subscription_web import create_subscription_router

                app.include_router(
                    create_subscription_router(
                        self.subscriptions,
                        public_checkout=self.public_checkout,
                        **(
                            {"brand_asset_store": kwargs["brand_asset_store"]}
                            if kwargs.get("brand_asset_store") is not None
                            else {}
                        ),
                    )
                )
        self._app = app
        return app

    def worker(self):
        if self.settings.mode == "staging":
            self.extensions()
        return PartnerRuntimeWorker(self)


def _safe_counts(value):
    """No arbitrary adapter strings, IDs, URLs, keys or error descriptions."""
    if not isinstance(value, Mapping):
        return {}
    allowed = {
        "checked",
        "pending",
        "canceled",
        "active",
        "errors",
        "phase_errors",
        "queued",
        "complete",
        "prepared",
        "held",
        "sent",
        "retry",
        "blocked",
        "uncertain",
        "launched",
        "activation_expired",
        "expired_unclassified",
        "traffic_holds",
        "webhook_consent_required",
        "customer_delivery_uncertain",
        "partner_delivery_uncertain",
        "activation_pending",
        "invoice_recovery_required",
        "finance_delivery_uncertain",
        "marketing_delivery_uncertain",
        "support_delivery_uncertain",
        "withdrawals_pending",
        "withdrawals_processing",
        "activation_oldest_seconds",
        "paid_activation_oldest_seconds",
        "traffic_due_oldest_seconds",
        "entry_pending_oldest_seconds",
    }
    return {
        key: item
        for key, item in value.items()
        if key in allowed and type(item) is int and 0 <= item < 2**63
    }


def build_runtime(
    settings,
    pool_getter=None,
    *,
    addon_loader=None,
    catalog_loader=None,
    provider=None,
    provisioner=None,
    telegram=None,
    sender=None,
    entry_notification_sender=None,
    managed_bot_telegram=None,
    extra_router_builder=None,
):
    """Composition only. Explicit test/staging adapters never run on creation."""
    if not isinstance(settings, PartnerRuntimeSettings):
        _invalid()
    runtime = PartnerRuntime(
        settings=settings,
        pool_getter=pool_getter,
        addon_loader=addon_loader,
        catalog_loader=catalog_loader,
        extra_router_builder=extra_router_builder,
    )
    if settings.mode == "disabled":
        return runtime
    if (
        not settings.preflight()["configured"]
        or not callable(pool_getter)
        or not callable(addon_loader)
    ):
        raise RuntimeConfigurationError(
            "Стенд не прошёл проверку настроек. Выполните --check."
        )
    if catalog_loader is None:

        async def catalog_loader():
            return {"traffic_packages": None, "device_options": None}

        runtime.catalog_loader = catalog_loader

    from services.partner_security import PartnerSettings, TokenVault
    from services.partner_store import PartnerStore
    from services.partner_commerce_store import PartnerCommerceStore
    from services.partner_enrollment_store import PartnerEnrollmentStore
    from services.partner_entry_pricing import EntryPricingPolicy
    from services.partner_checkout import PartnerCheckoutService
    from services.partner_entry_checkout import PartnerEntryCheckoutService
    from services.partner_launcher import PartnerLaunchService
    from services.partner_entitlements import PartnerEntitlementService
    from services.partner_traffic import PartnerTrafficService
    from services.partner_recovery import PartnerRecoveryService
    from services.partner_notifications import (
        PartnerNotificationStore,
        PartnerNotificationService,
        TelegramNotificationAdapter,
    )
    from services.partner_entry_notifications import (
        EntryLaunchNotificationStore,
        EntryLaunchNotificationService,
        TelegramEntryLaunchNotificationAdapter,
    )
    from services.partner_managed_bots import ManagedBotStore, PartnerManagedBotService

    runtime.partner_settings = PartnerSettings(
        enabled=settings.enable_http,
        base_domain=settings.base_domain,
        manager_bot_token=settings.manager_bot_token,
        encryption_keys=settings.encryption_keys,
    )
    runtime.vault = TokenVault(settings.encryption_keys)
    runtime.registry = PartnerStore(pool_getter)
    runtime.managed_bots = PartnerManagedBotService(
        ManagedBotStore(pool_getter, payment_first_enabled=settings.enable_payment_first_entry), runtime.vault,
        settings.manager_bot_token, settings.manager_bot_username,
        telegram=managed_bot_telegram, enabled=settings.enable_managed_bots,
        allowed_user_ids=settings.manager_allowed_user_ids,
    )
    runtime.commerce = PartnerCommerceStore(
        pool_getter,
        addon_loader,
        entitlements_enabled=settings.enable_provisioning,
        traffic_cycles_enabled=settings.enable_provisioning,
        invoice_intents_enabled=settings.invoice_intents_enabled,
        panel_identity_enabled=settings.panel_identity_enabled,
    )
    entry_pricing = EntryPricingPolicy(
        owner_pilot_enabled=settings.enable_owner_entry_pilot,
        public_trial_enabled=settings.enable_public_entry_trial,
        public_sales_enabled=settings.enable_public_entry_sales,
    )
    runtime.enrollment = PartnerEnrollmentStore(
        pool_getter, entry_pricing=entry_pricing,
        entry_launch_notifications_enabled=settings.enable_entry_launch_notifications,
        payment_first_enabled=settings.enable_payment_first_entry,
    )
    if settings.enable_billing and provider is None:
        from services.partner_yookassa import (
            PartnerYooKassaProvider,
            PartnerYooKassaSettings,
        )

        provider = PartnerYooKassaProvider(
            PartnerYooKassaSettings(
                settings.yookassa_shop_id,
                settings.yookassa_secret_key,
                settings.manager_origin + "/partners",
                test_mode=settings.yookassa_test_mode,
                owner_entry_pilot=settings.enable_owner_entry_pilot,
                public_entry_trial=settings.enable_public_entry_trial,
                public_entry_sales=settings.enable_public_entry_sales,
            )
        )
    if settings.enable_provisioning and provisioner is None:
        from services.partner_remnawave import (
            PartnerRemnawaveProvisioner,
            PartnerRemnawaveSettings,
        )

        provisioner = PartnerRemnawaveProvisioner(
            PartnerRemnawaveSettings(
                settings.remnawave_api_url,
                settings.remnawave_api_token,
                settings.remnawave_api_version,
                settings.remnawave_squad_ids,
                settings.subscription_source_origin,
            )
        )
    # Injecting a real adapter does not override the installation's capability.
    provider = provider if settings.enable_billing else None
    provisioner = provisioner if settings.enable_provisioning else None
    # The pilot is an entry fee check, not permission to charge subscriptions
    # or add-ons through this live merchant (including injected adapters).
    commerce_provider = None if settings.entry_only_live else provider
    runtime.checkout = PartnerCheckoutService(runtime.commerce, commerce_provider, provisioner)
    runtime.launcher = PartnerLaunchService(
        runtime.enrollment,
        runtime.vault,
        telegram,
        webhook_base_url=settings.webhook_origin,
        webhook_secret_key=bytes.fromhex(settings.webhook_key_hex),
        clients_enabled=settings.enable_launches,
    )
    runtime.entry = PartnerEntryCheckoutService(
        runtime.enrollment, provider, runtime.launcher, entry_pricing=entry_pricing
    )
    from services.partner_manager import PartnerManagerBootstrap

    runtime.manager_bootstrap = PartnerManagerBootstrap(
        token=settings.manager_bot_token,
        webhook_origin=settings.webhook_origin,
        webhook_key=bytes.fromhex(settings.webhook_key_hex),
        enabled=settings.enable_manager_webhook,
        username=settings.manager_bot_username,
        managed_bots_enabled=settings.enable_managed_bots,
    )
    if settings.enable_public_gateway:
        from services.partner_subscriptions import (
            PartnerLinkSettings,
            PartnerSubscriptionLinks,
            PartnerSubscriptionService,
            PartnerSubscriptionSource,
        )

        links = PartnerSubscriptionLinks(
            PartnerLinkSettings(
                settings.base_domain,
                bytes.fromhex(settings.subscription_key_hex),
                short_links=True,
            )
        )
        runtime.subscriptions = PartnerSubscriptionService(
            runtime.commerce,
            links,
            provisioner=provisioner,
            source=PartnerSubscriptionSource(settings.subscription_source_origin),
            enabled=True,
        )
    notifications_store = PartnerNotificationStore(
        pool_getter, enabled=settings.enable_notifications
    )
    runtime.notifications = PartnerNotificationService(
        notifications_store,
        sender
        or (TelegramNotificationAdapter() if settings.enable_notifications else None),
        vault=runtime.vault,
        manager_bot_token=settings.manager_bot_token,
        enabled=settings.enable_notifications,
    )
    runtime.entry_launch_notifications = EntryLaunchNotificationService(
        EntryLaunchNotificationStore(
            pool_getter, enabled=settings.enable_entry_launch_notifications
        ),
        entry_notification_sender or (
            TelegramEntryLaunchNotificationAdapter()
            if settings.enable_entry_launch_notifications else None
        ),
        manager_bot_token=settings.manager_bot_token,
        enabled=settings.enable_entry_launch_notifications,
    )
    runtime.recovery = PartnerRecoveryService(
        runtime.commerce,
        runtime.checkout,
        PartnerEntitlementService(runtime.commerce, provisioner),
        PartnerTrafficService(runtime.commerce, provisioner),
        runtime.notifications,
        enabled=settings.enable_worker,
        phase_timeout_seconds=settings.phase_timeout_seconds,
    )
    return runtime


class PartnerRuntimeWorker:
    """No task is spawned by construction. SIGTERM requests a bounded drain."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.failures = 0

    @property
    def ready(self):
        return (
            self.runtime.settings.mode == "staging"
            and self.runtime.settings.enable_worker
        )

    def delay_seconds(self):
        return min(
            300, self.runtime.settings.interval_seconds * 2 ** min(self.failures, 6)
        )

    async def run_once(self):
        settings, runtime = self.runtime.settings, self.runtime
        result = {"enabled": self.ready, "phase_errors": 0}
        if not self.ready:
            return result
        health = await runtime.probe()
        if not health["database_ready"]:
            return {**result, "phase_errors": 1, "database_ready": False}
        result["database_ready"] = True
        phases = []
        if runtime.recovery is not None and not settings.entry_only_live:
            phases.append(
                (
                    "commerce",
                    lambda: runtime.recovery.recover_once(limit=settings.pass_limit),
                    8 * settings.phase_timeout_seconds,
                )
            )
        if runtime.entry is not None:
            from services.partner_entry_checkout import recover_partner_entries_once

            phases.append(
                (
                    "entries",
                    lambda: recover_partner_entries_once(
                        runtime.enrollment, runtime.entry, limit=settings.pass_limit
                    ),
                    settings.phase_timeout_seconds,
                )
            )
        if runtime.entry_launch_notifications is not None and runtime.entry_launch_notifications.ready:
            phases.append(
                (
                    "entry_launch_notifications",
                    lambda: runtime.entry_launch_notifications.recover_once(limit=settings.pass_limit),
                    settings.phase_timeout_seconds,
                )
            )
        if not settings.entry_only_live:
            phases += [
                (name, callback, settings.phase_timeout_seconds)
                for name, callback in runtime._hooks
            ]
        for name, callback, timeout in phases:
            try:
                counts = await asyncio.wait_for(callback(), timeout=timeout)
                result[name] = _safe_counts(counts)
                result["phase_errors"] += result[name].get("phase_errors", 0) + result[
                    name
                ].get("errors", 0)
            except Exception:
                result[name] = {"errors": 1}
                result["phase_errors"] += 1
        result["manual_review"] = _safe_counts(health.get("manual_review"))
        result["queue_health"] = _safe_counts(health.get("queue_health"))
        return result

    async def run(self, stop_event, *, report=None):
        if not isinstance(stop_event, asyncio.Event):
            raise TypeError("asyncio.Event required")
        if not self.ready:
            return {"enabled": False, "passes": 0}
        passes = 0
        while not stop_event.is_set():
            result = await self.run_once()
            passes += 1
            self.failures = (
                min(7, self.failures + 1) if result.get("phase_errors") else 0
            )
            if report is not None:
                # Only our redacted envelope is sent to the operator's sink.
                report(result)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.delay_seconds())
            except TimeoutError:
                pass
        return {"enabled": True, "passes": passes}
