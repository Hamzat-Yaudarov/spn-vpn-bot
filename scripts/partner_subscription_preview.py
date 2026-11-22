"""Offline subscription-page demo. No DB, Telegram, payments, DNS or panel.

Run from the repository: python3 scripts/partner_subscription_preview.py
The separate loopback server never handles real capability/config requests.
"""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from partner_subscription_web import render_subscription_page, security_headers
from services.partner_subscriptions import PartnerLinkSettings, PartnerSubscriptionLinks

STATES = {
    "active": "ready",
    "low": "ready",
    "long": "ready",
    "expired": "expired",
    "updating": "updating",
    "error": "unavailable",
}


def demo_page(name="active", *, short_links=False):
    state = STATES.get(name, "unavailable")
    context = {
        "project_id": "11111111-1111-4111-8111-111111111111",
        "subscription_id": "22222222-2222-4222-8222-222222222222",
        "public_link_generation": 0,
        "slug": "demo-vpn",
        "title": "Север VPN",
        "brand": {"accent_color": "#d8b55b"},
        "support_url": "https://t.me/OfflinePreviewSupport",
        "bot_username": "OfflinePreviewBot",
        "device_limit": 3,
        "expires_at": datetime(2026, 11, 19, tzinfo=timezone.utc),
    }
    if name == "long":
        context["title"] = (
            "Очень длинное название моего собственного VPN для друзей и знакомых"
        )
        context["brand"] = {
            "accent_color": "#000000",
            "subscription_description": "Обычные серверы и антиглушилка в одной подписке. Подключение через Happ и INCY.",
        }
    total = 350 * 1024**3
    remaining = 512 * 1024**2 if name == "low" else 300 * 1024**3
    quota = {
        "remaining_bytes": remaining,
        "total_bytes": total,
        "used_bytes": total - remaining,
    }
    links = PartnerSubscriptionLinks(
        PartnerLinkSettings(
            "partners.example.test",
            b"demo-only-no-real-capabilities-32",
            short_links=short_links,
        )
    )
    response = render_subscription_page(
        context, links, state, quota if state == "ready" else None
    )
    # These buttons cannot launch a real client or a real Telegram/payment flow.
    body = response.body.decode().replace(
        "<body>",
        '<body><p class="demo-banner" role="note">Демонстрация · вымышленные данные · подключение отключено</p>',
    )
    import re

    body = re.sub(r'href="happ:[^"]+"', 'href="#demo" aria-disabled="true"', body)
    body = re.sub(
        r'href="https://t\.me/[^"]+"', 'href="#demo" aria-disabled="true"', body
    )
    response.body = body.encode()
    response.headers["content-length"] = str(len(response.body))
    return response


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Even the demo does not log secret-shaped paths.

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path in {"/subscription-preview", "/subscription-preview/"}:
            query = parse_qs(parsed.query)
            response = demo_page(
                query.get("state", ["active"])[0],
                short_links=query.get("style") == ["short"],
            )
            data, headers = response.body, dict(response.headers)
        elif parsed.path in {
            "/partner-subscription-assets/subscription.css",
            "/partner-subscription-assets/subscription.js",
        }:
            name = parsed.path.rsplit("/", 1)[-1]
            data = (ROOT / "static/partner-subscription/assets" / name).read_bytes()
            headers = {
                **security_headers(),
                "content-type": (
                    "text/css" if name.endswith("css") else "application/javascript"
                ),
            }
        else:
            self.send_error(404, "Offline demo only")
            return
        self.send_response(200)
        for key, value in headers.items():
            if key != "content-length":
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8965)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Choose an unprivileged port")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(
        f"Offline subscription demo: http://127.0.0.1:{args.port}/subscription-preview",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
