"""Application composition and read-only live catalogue/readiness queries."""
from __future__ import annotations

import ipaddress
from pathlib import Path
import re
import time
import uuid

from .auth import Auth
from .credentials import Credentials, private_directory
from .policy import classify_health
from .provider import ProviderError, VoxaClient
from .store import Store


def domain_name(value):
    if not isinstance(value, str):
        raise ValueError("Нужен домен сервера")
    try:
        name = value.strip().rstrip(".").encode("idna").decode().lower()
        ipaddress.ip_address(name)
    except UnicodeError:
        raise ValueError("Неверный домен") from None
    except ValueError:
        pass
    else:
        raise ValueError("Укажите домен, не IP")
    if len(name) > 253 or len(name.split(".")) < 2 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in name.split(".")):
        raise ValueError("Неверный домен")
    return name


class Runtime:
    def __init__(self, directory, *, provider_factory=VoxaClient, executor=None):
        self.directory = private_directory(directory)
        self.store = Store(self.directory / "fleet.sqlite3")
        self.credentials = Credentials(self.directory / "credentials")
        self.auth = Auth(self.credentials.directory / "auth.json")
        self.provider_factory, self.executor = provider_factory, executor
        self.cache = {"products": [], "balance": None, "identity": None, "catalog_at": None,
                      "preflight_at": None, "last_error": None}

    def provider(self):
        config = self.credentials.read()
        if not config.get("provider_token") or not config.get("project_id"):
            raise ValueError("Сначала сохраните API-ключ и отдельный проект Voxa")
        return self.provider_factory(config["provider_token"], project_id=config["project_id"])

    def readiness(self):
        keys, settings = self.credentials.status(), self.store.get_settings()
        missing = []
        for key, label in (("provider_token", "Ключ Voxa"), ("project_id", "Проект Voxa"),
                           ("cloudflare_token", "Ключ Cloudflare"), ("remnawave_token", "Ключ Remnawave"),
                           ("remnawave_url", "Адрес Remnawave")):
            if not keys.get(key):
                missing.append(label)
        if not self.cache["preflight_at"]:
            missing.append("Проверка подключения к API")
        if not self.executor:
            missing.append("Исполнитель установки и независимые VPN-проверки ещё не настроены")
        else:
            evidence = self.executor.ready()
            if evidence is not True and not (isinstance(evidence, dict) and evidence.get("ready") is True):
                missing.extend(evidence.get("reasons", ["Исполнитель не готов"]) if isinstance(evidence, dict)
                               else ["Исполнитель не готов"])
        identity = self.cache.get("identity") or {}
        scopes = set((identity.get("key") or {}).get("scopes", []))
        checks = [{"name": "Ключи", "ok": all(keys.get(k) for k in
                   ("provider_token", "project_id", "cloudflare_token", "remnawave_token", "remnawave_url"))},
                  {"name": "Voxa API", "ok": bool(self.cache["preflight_at"])}]
        reasons = list(dict.fromkeys(missing))
        financial_reasons = reasons[:]
        if not settings["financial_enabled"]:
            financial_reasons.append("Покупки выключены владельцем")
        if not {"read", "orders", "destructive"} <= scopes:
            financial_reasons.append("Нет подтверждённых прав orders/destructive")
        return {"ready": not reasons, "financial_ready": not financial_reasons,
                "missing": reasons, "financial_reasons": financial_reasons, "checks": checks,
                "credentials": keys, "last_checked": self.cache["preflight_at"]}

    def preflight(self):
        client = self.provider()
        identity, balance = client.identity(), client.balance()
        if not isinstance(identity, dict) or not isinstance(balance, dict):
            raise ValueError("Не удалось подтвердить ответы Voxa API")
        configured = self.credentials.read()["project_id"]
        key_project = (identity.get("key") or {}).get("project") or {}
        request_project = identity.get("project") or {}
        if not key_project or configured not in (key_project.get("id"), key_project.get("slug")):
            raise ValueError("Нужен ключ, привязанный к отдельному проекту Voxa")
        if request_project and request_project.get("id") != key_project.get("id"):
            raise ValueError("Проект запроса не совпал с проектом ключа")
        if str(balance.get("currency", "")).upper() != self.store.get_settings()["currency"]:
            raise ValueError("Валюта баланса отличается от валюты лимитов; проверьте настройки")
        self.cache.update(identity=identity, balance=balance, preflight_at=time.time(), last_error=None)
        self.store.add_event("preflight.passed", "Ключ и проект Voxa подтверждены; покупок не было")
        return self.readiness()

    def catalog(self, *, refresh=False):
        if refresh:
            products = self.provider().products()
            if not isinstance(products, list):
                raise ValueError("Не удалось прочитать каталог Voxa")
            # Keep exact catalogue values. Do not fabricate hourly/location/VAT conversions.
            self.cache.update(products=products, catalog_at=time.time())
            self.store.add_event("catalog.refreshed", "Каталог Voxa обновлён")
        return {"products": self.cache["products"], "locations": [], "squads": [],
                "updated_at": self.cache["catalog_at"]}

    def public_servers(self):
        result = []
        for server in self.store.list_servers():
            history = self.store.latest_probes(server["id"], limit=1)
            # Only explicit known display metadata leaves the runtime.
            public = {key: value for key, value in server.items() if key not in ("metadata", "original_state_path")}
            public["metadata"] = {key: value for key, value in server.get("metadata", {}).items()
                                  if key in ("squads", "node_id", "host_id", "profile_id", "last_cutover_at")}
            if history:
                probe = history[0].get("probe", history[0])
                health = classify_health(probe)
                public.update(health=health, status=health["verdict"], last_check=probe.get("observed_at"))
                public["probes"] = {"ru": "pass" if health["verdict"] == "healthy" else "unknown",
                                    "external": "pass" if health["verdict"] in ("healthy", "ru_unreachable") else "unknown"}
            else:
                public.update(status="unconfigured" if not server.get("current_vps_id") else "unknown")
            result.append(public)
        return result

    def public_operations(self):
        servers = {item["id"]: item["name"] for item in self.store.list_servers()}
        # Internal payload/data may contain bootstrap nonces. Never serve them.
        allowed = {"id", "server_id", "action", "phase", "created_at", "updated_at", "error"}
        return [{**{key: value for key, value in row.items() if key in allowed},
                 "server_name": servers.get(row.get("server_id"), "Новый сервер")}
                for row in self.store.list_operations()]

    def overview(self):
        balance = self.cache["balance"]
        return {"servers": self.public_servers(), "operations": self.public_operations(),
                "settings": self.store.get_settings(), "readiness": self.readiness(),
                "budget": self.store.budget_status(),
                "balance": ({"amount": balance.get("balance"), "currency": balance.get("currency")}
                            if isinstance(balance, dict) else None),
                "catalog_status": {"updated_at": self.cache["catalog_at"], "count": len(self.cache["products"]),
                                   "error": self.cache["last_error"]}}

    def create_server(self, body, request_id):
        if not isinstance(body, dict):
            raise ValueError("Неверные данные сервера")
        allowed = {"name", "domain", "country", "product_id", "location", "mode", "squads"}
        if set(body) - allowed:
            raise ValueError("Неизвестные поля сервера")
        domain = domain_name(body.get("domain"))
        if not isinstance(body.get("name"), str) or not 1 <= len(body["name"].strip()) <= 60:
            raise ValueError("Название: от 1 до 60 символов")
        product_id = str(uuid.UUID(body.get("product_id", "")))
        squads = body.get("squads") or []
        if not isinstance(squads, list) or not squads:
            raise ValueError("Укажите хотя бы один сквад Remnawave")
        squads = sorted({str(uuid.UUID(item)) for item in squads})
        country = body.get("country", "").upper()
        if country and not re.fullmatch("[A-Z]{2}", country):
            raise ValueError("Страна: двухбуквенный код")
        previous = [server for server in self.store.list_servers() if server["domain"] == domain]
        if previous:
            server = previous[0]
            if server.get("metadata", {}).get("create_request_id") != request_id:
                raise ValueError("Этот домен уже добавлен; для переноса используйте «Заменить»")
        else:
            server = self.store.create_server({"name": body["name"].strip(), "domain": domain, "country": country,
                "product_id": product_id, "location": body.get("location", ""), "mode": body.get("mode", "observe"),
                "currency": self.store.get_settings()["currency"],
                "metadata": {"squads": squads, "create_request_id": request_id}})
        operation = self.store.enqueue_operation("create", server["id"], {"approved": True}, idempotency_key=request_id)
        return {"server": server, "operation": {k: operation[k] for k in ("id", "phase", "action", "server_id")}}
