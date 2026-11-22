"""Serve only the static partner-cabinet demo on loopback; no backend or real API.

Run: python3 scripts/partner_preview.py
Open: http://127.0.0.1:8964/partners?preview=1
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1] / "static" / "partners"
FILES = {
    "/partners": ("index.html", "text/html; charset=utf-8"),
    "/partners/": ("index.html", "text/html; charset=utf-8"),
    "/partners/assets/partners.css": ("assets/partners.css", "text/css; charset=utf-8"),
    "/partners/assets/partners.js": ("assets/partners.js", "application/javascript; charset=utf-8"),
    "/partners/assets/workspace.js": ("assets/workspace.js", "application/javascript; charset=utf-8"),
}


class PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        pathname = urlsplit(self.path).path
        entry = FILES.get(pathname)
        if re.fullmatch(r"/partners/workspace/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/?", pathname):
            entry = FILES["/partners"]
        if not entry:
            self.send_error(404, "Local static demo only")
            return
        filename, content_type = entry
        data = (ROOT / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'none'; img-src 'self' data:; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8964)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Choose an unprivileged port between 1024 and 65535")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler)
    print(f"Offline demonstration: http://127.0.0.1:{args.port}/partners?preview=1", flush=True)
    print("No payments, Telegram calls or backend are connected. Stop with Ctrl+C.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
