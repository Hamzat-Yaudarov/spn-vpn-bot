#!/usr/bin/env python3
"""One approved service: sign in, read IPv4 inventory/quote, never submit order."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path('/opt/wayspn')
sys.path.insert(0, str(ROOT))

from spn_ip_failover.onecent import Settings, OneCentBilling, browser, execute
from spn_ip_failover.onecent_auth import OneCentLogin, OneCentLoginError
from spn_ip_failover.onecent_ui import OneCentPage, OneCentUIError
from spn_ip_failover.provider import ProviderError


SERVICE = 'f5c0287a-636e-4981-95c7-80b24c9559c0'
AUTH_PATH = '/api/auth/sign-in/email'  # Observed blocked form request, not guessed.


def main():
    assert os.geteuid() != 0
    directory = Path(os.environ['CREDENTIALS_DIRECTORY'])
    email = (directory / 'onecent_email').read_text().rstrip('\n')
    password = (directory / 'onecent_password').read_text().rstrip('\n')
    settings = Settings.load('/etc/spn-ip-failover/onecent.json')
    assert settings.browser_channel is None
    assert settings.profile == Path('/var/lib/spn-ip-failover/onecent-browser')
    assert SERVICE in settings.servers
    report = {'service_id': SERVICE, 'ok': False, 'operation': 'inventory_and_quote',
              'paid_operations_enabled': False, 'paid_post_attempts': 0}
    stage = 'launch'
    counts = {'auth_posts': 0, 'blocked_nonread': 0, 'paid_post_attempts': 0}
    blocked = []
    login = None
    try:
        with browser(settings) as page:
            def guard(route):
                request = route.request
                location = urlsplit(request.url)
                same_origin = (location.scheme == 'https' and location.hostname == 'bill.1cent.host'
                               and location.port in (None, 443) and not location.query
                               and not location.fragment and location.username is None)
                if request.method in ('GET', 'HEAD', 'OPTIONS'):
                    route.continue_()
                    return
                if same_origin and request.method == 'POST' and location.path == AUTH_PATH:
                    try:
                        expected = request.post_data_json == {'email': email, 'password': password}
                    except Exception:
                        expected = False
                    if expected and counts['auth_posts'] == 0:
                        counts['auth_posts'] += 1
                        route.continue_()
                        return
                counts['blocked_nonread'] += 1
                if same_origin and len(blocked) < 8:
                    try:
                        data = request.post_data_json
                    except Exception:
                        data = None
                    blocked.append({'path': location.path, 'method': request.method,
                                    'json_keys': sorted(data) if isinstance(data, dict) else None,
                                    'email_matches': isinstance(data, dict) and data.get('email') == email,
                                    'password_matches': isinstance(data, dict) and data.get('password') == password})
                route.abort()

            # Until OneCentPage installs its stricter quote guard, only one
            # exact observed sign-in POST is permitted. No final order method
            # is ever invoked by this audit.
            page.route('**/*', guard)

            def observed(request):
                location = urlsplit(request.url)
                if request.method == 'POST' and location.hostname == 'bill.1cent.host' and (
                        location.path.endswith('/ipv4/add') or location.path.endswith('/ipv4/release')
                        or '/ipv4/orders/' in location.path):
                    counts['paid_post_attempts'] += 1
            page.on('request', observed)
            login = OneCentLogin(page, email, password, settings.journal / 'authentication')
            ui = OneCentPage(page, SERVICE, settings.servers[SERVICE].name, login=login)
            stage = 'open_service_and_login'
            ui.open()
            billing = OneCentBilling(settings, SERVICE, ui)
            stage = 'read_inventory'
            inventory = execute(billing, {'operation': 'inventory', 'server_id': SERVICE})
            stage = 'read_quote'
            quote = execute(billing, {'operation': 'quote', 'server_id': SERVICE})
            assert counts['paid_post_attempts'] == 0
            assert ui._payment_sent is False and ui._allow_payment is False
            report.update(ok=True, inventory=inventory, quote=quote, stage='complete', **counts)
    except (OneCentLoginError, OneCentUIError, ProviderError) as exc:
        # These exception classes deliberately carry only fixed safe messages.
        report.update(stage=stage, error_type=type(exc).__name__, message=str(exc), **counts)
    except Exception as exc:
        # Browser errors can contain arguments. Never include their text.
        report.update(stage=stage, error_type=type(exc).__name__, **counts)
    report.update(login_phase=getattr(login, 'phase', None), blocked_same_origin=blocked)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
