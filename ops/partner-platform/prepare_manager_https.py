"""One-shot, fixed-host HTTPS preparation. Default is a read-only plan.

Run only on the already-cleaned authorized VPS. Reuses an EXISTING Let's Encrypt
account; preserves the renewal timer state and never registers/accepts terms, starts an
application, reads private keys, changes app credentials, or touches SQL/Telegram.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


HOST = "partners.wayspn.online"
TARGET = "92.42.102.103"
SERVER = "https://acme-v02.api.letsencrypt.org/directory"
ACCOUNTS = Path("/etc/letsencrypt/accounts/acme-v02.api.letsencrypt.org/directory")
RENEWAL = Path("/etc/letsencrypt/renewal")
HOOKS = Path("/etc/letsencrypt/renewal-hooks")
HOOK = Path("/etc/letsencrypt/spn-franchise-hooks/reload-manager-nginx")
WEBROOT = Path("/var/lib/spn-franchise-acme")
LIVE = Path("/etc/letsencrypt/live") / HOST
ARCHIVE = Path("/etc/letsencrypt/archive") / HOST
HTTP = Path("/etc/nginx/sites-enabled/20-spn-franchise-manager-http.conf")
HTTPS = Path("/etc/nginx/sites-enabled/21-spn-franchise-manager-https.conf")
CA = "/etc/ssl/certs/ca-certificates.crt"
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8"}


class PreparationError(ValueError):
    pass


def command(args, *, allowed=(0,), timeout=60):
    result = subprocess.run(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=ENV, timeout=timeout, check=False,
    )
    if result.returncode not in allowed:
        raise PreparationError("A required command failed; raw output suppressed. Review the current phase.")
    return result


def absent(path):
    if path.exists() or path.is_symlink():
        raise PreparationError("A scoped destination already exists; refuse overwrite and review manually.")


def trusted_directory(path):
    for item in (path, *path.parents):
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise PreparationError("A deployment parent is not a root-owned, non-writable directory.")


def existing_account():
    if not ACCOUNTS.is_dir():
        raise PreparationError("needs_user: no existing production Let's Encrypt account; nothing changed.")
    trusted_directory(ACCOUNTS)
    candidates = []
    for entry in ACCOUNTS.iterdir():
        if not re.fullmatch(r"[a-f0-9]{32}", entry.name):
            raise PreparationError("needs_user: unexpected account entry; nothing changed.")
        trusted_directory(entry)
        for name in ("meta.json", "regr.json", "private_key.json"):
            # Metadata only, NEVER open or print account keys/registration data.
            info = (entry / name).lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022:
                raise PreparationError("needs_user: unsafe existing account metadata; nothing changed.")
            if name == "private_key.json" and info.st_mode & 0o077:
                raise PreparationError("needs_user: account key permissions are not owner-only; nothing changed.")
        candidates.append(entry.name)
    if len(candidates) != 1:
        raise PreparationError("needs_user: expected exactly one existing production account; nothing changed.")
    return candidates[0]


def nginx_test():
    result = command(["/usr/sbin/nginx", "-t"])
    if b"[warn]" in result.stderr or b"[warn]" in result.stdout:
        raise PreparationError("Nginx validation warned; refuse an ambiguous virtual host configuration.")


def verify_local_target():
    result = command(["/usr/sbin/ip", "-j", "-4", "address", "show"])
    try:
        addresses = {
            address["local"] for interface in json.loads(result.stdout)
            for address in interface.get("addr_info", []) if address.get("family") == "inet"
        }
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PreparationError("Cannot verify the local VPS address; nothing changed.") from None
    if TARGET not in addresses:
        raise PreparationError("This host does not have the approved VPS address; nothing changed.")


def renewal_timer_status():
    status = {}
    for action, label in (("is-enabled", "enabled"), ("is-active", "active")):
        result = command(
            ["/usr/bin/systemctl", action, "--quiet", "certbot.timer"],
            allowed=(0, 1, 3, 4),
        )
        status[label] = result.returncode == 0
    # Cleanup intentionally left the timer disabled. NEVER start/enable it here.
    return status


def preflight(target):
    if target != TARGET or sys.platform != "linux" or os.geteuid() != 0:
        raise PreparationError("Explicitly confirm the authorized Linux VPS and run as root.")
    account = existing_account()  # Must happen BEFORE any directory/config change.
    verify_local_target()
    for path in (HTTP, HTTPS, HOOK.parent, WEBROOT, LIVE, ARCHIVE, RENEWAL / (HOST + ".conf")):
        absent(path)
    for path in (HTTP.parent, RENEWAL, HOOKS, HOOK.parent.parent, WEBROOT.parent, LIVE.parent, ARCHIVE.parent):
        trusted_directory(path)
    if list(RENEWAL.glob("*.conf")):
        raise PreparationError("needs_user: legacy renewal configurations remain; nothing changed.")
    for directory in ("pre", "deploy", "post"):
        folder = HOOKS / directory
        if folder.exists():
            trusted_directory(folder)
            if any(folder.iterdir()):
                raise PreparationError("needs_user: existing renewal hooks need review; nothing changed.")
    for config in (Path("/etc/letsencrypt/cli.ini"), Path("/root/.config/letsencrypt/cli.ini")):
        if config.exists() or config.is_symlink():
            raise PreparationError("needs_user: existing Certbot CLI overrides need review; nothing changed.")
    timer = renewal_timer_status()
    nginx_test()
    current = command(["/usr/sbin/nginx", "-T"])
    if HOST.encode() in current.stdout or re.search(rb"\blisten\s+[^;\n]*\b443\b", current.stdout):
        raise PreparationError("Existing hostname/443 listeners need review; nothing changed.")
    return account, timer


def write_new(path, text, mode):
    fd = None
    created = False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        created = True
        os.fchmod(fd, mode)
        data = text.encode("utf-8")
        while data:
            count = os.write(fd, data)
            if count <= 0:
                raise OSError()
            data = data[count:]
        os.fsync(fd)
    except BaseException:
        if created:
            path.unlink()
        raise
    finally:
        if fd is not None:
            os.close(fd)


def activate_nginx(path, text):
    write_new(path, text, 0o644)
    try:
        nginx_test()
        command(["/usr/bin/systemctl", "reload", "nginx"])
    except BaseException:
        # Only this phase's newly created file; never existing/previous configs.
        path.unlink()
        try:
            nginx_test()
            command(["/usr/bin/systemctl", "reload", "nginx"])
        except Exception:
            pass
        raise


def http_config():
    return f"""# New fixed manager hostname only; existing default HTTP remains untouched.
server {{
    listen 80;
    listen [::]:80;
    server_name {HOST};
    server_tokens off;
    access_log off;
    error_log /dev/null emerg;
    if ($http_host !~* ^partners\\.wayspn\\.online(:80)?$) {{ return 444; }}
    location ^~ /.well-known/acme-challenge/ {{
        root {WEBROOT};
        default_type text/plain;
        autoindex off;
        try_files $uri =404;
    }}
    location / {{ return 503; }}
}}
"""


def https_config():
    return f"""# Unknown SNI is rejected at TLS; unknown HTTP Host is never proxied.
server {{
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name _;
    ssl_reject_handshake on;
    ssl_protocols TLSv1.2 TLSv1.3;
    access_log off;
    error_log /dev/null emerg;
    return 444;
}}
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {HOST};
    ssl_certificate {LIVE}/fullchain.pem;
    ssl_certificate_key {LIVE}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    server_tokens off;
    access_log off;
    error_log /dev/null emerg;
    client_max_body_size 768k;
    if ($ssl_server_name != {HOST}) {{ return 444; }}
    if ($http_host !~* ^partners\\.wayspn\\.online(:443)?$) {{ return 444; }}
    if ($host != {HOST}) {{ return 444; }}
    add_header Cache-Control "private, no-store" always;
    add_header Referrer-Policy no-referrer always;
    location / {{
        proxy_pass http://127.0.0.1:8966;
        proxy_http_version 1.1;
        proxy_set_header Host {HOST};
        proxy_set_header Forwarded "";
        proxy_set_header X-Forwarded-Host "";
        proxy_set_header X-Forwarded-For "";
        proxy_set_header X-Forwarded-Proto "";
        proxy_set_header X-Forwarded-Port "";
        proxy_set_header X-Real-IP "";
        proxy_set_header Connection "";
        proxy_connect_timeout 5s;
        proxy_read_timeout 120s;
        proxy_send_timeout 30s;
        proxy_buffering off;
        proxy_cache off;
        proxy_redirect off;
        proxy_intercept_errors on;
        error_page 502 504 =503 @backend_unavailable;
    }}
    location @backend_unavailable {{ return 503; }}
}}
"""


def deploy_hook():
    return f"""#!/bin/sh
set -eu
[ "${{RENEWED_LINEAGE:-}}" = "{LIVE}" ] || exit 0
/usr/sbin/nginx -t >/dev/null 2>&1
/usr/bin/systemctl reload nginx >/dev/null 2>&1
"""


def certificate_metadata():
    paths = {}
    for name in ("cert.pem", "chain.pem", "fullchain.pem", "privkey.pem"):
        path = (LIVE / name).resolve(strict=True)
        if not path.is_relative_to(ARCHIVE):
            raise PreparationError("Unexpected certificate lineage path.")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022:
            raise PreparationError("Unsafe certificate metadata.")
        if name == "privkey.pem" and info.st_mode & 0o077:
            raise PreparationError("Private key permissions are not owner-only.")
        paths[name] = path
    sans = command(["/usr/bin/openssl", "x509", "-in", str(paths["cert.pem"]), "-noout", "-ext", "subjectAltName"]).stdout
    if set(re.findall(rb"DNS:([^,\s]+)", sans)) != {HOST.encode()}:
        raise PreparationError("Certificate names are not exactly the approved manager hostname.")
    command(["/usr/bin/openssl", "x509", "-in", str(paths["cert.pem"]), "-noout", "-checkend", "86400"])
    command(["/usr/bin/openssl", "verify", "-CAfile", CA, "-untrusted", str(paths["chain.pem"]), "-verify_hostname", HOST, "-purpose", "sslserver", str(paths["cert.pem"])])
    # Public certificates only. NEVER open the private key/account key.
    for name in ("cert.pem", "chain.pem", "fullchain.pem"):
        if paths[name].stat().st_size > 65536:
            raise PreparationError("Unexpected certificate size.")
    fullchain = paths["fullchain.pem"].read_bytes()
    if fullchain != paths["cert.pem"].read_bytes() + paths["chain.pem"].read_bytes():
        raise PreparationError("Fullchain does not contain the issued certificate and chain.")
    return hashlib.sha256(fullchain).hexdigest()


def apply(target):
    account, timer = preflight(target)
    WEBROOT.mkdir(mode=0o755)
    WEBROOT.chmod(0o755)  # Explicitly readable by Nginx even with root umask 0077.
    HOOK.parent.mkdir(mode=0o700)
    HOOK.parent.chmod(0o700)
    write_new(HOOK, deploy_hook(), 0o700)
    activate_nginx(HTTP, http_config())
    command([
        "/usr/bin/certbot", "certonly", "--config", "/dev/null",
        "--non-interactive", "--server", SERVER, "--account", account,
        "--webroot", "--webroot-path", str(WEBROOT), "--preferred-challenges", "http",
        "--cert-name", HOST, "--domains", HOST, "--no-directory-hooks",
        "--deploy-hook", str(HOOK),
    ], timeout=300)
    fingerprint = certificate_metadata()
    # nginx -t verifies that fullchain/private-key match, without reading/printing
    # private keys in this script. Only then reload the new HTTPS virtual host.
    activate_nginx(HTTPS, https_config())
    return {
        "hostname": HOST, "https_prepared": True, "application_started": False,
        "upstream_absent_status": 503, "certificate_fullchain_sha256": fingerprint,
        "renewal_configured_for": HOST,
        "renewal_timer_enabled": timer["enabled"], "renewal_timer_active": timer["active"],
        "needs_timer_activation": not (timer["enabled"] and timer["active"]),
        "old_configs_overwritten": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-target", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({"plan_only": True, "hostname": HOST, "target": TARGET,
            "existing_account_required": True, "application_started": False}))
        return 0
    try:
        print(json.dumps(apply(args.confirm_target)))
        return 0
    except Exception as error:
        # No Certbot/Nginx raw output, keys, tokens or application settings.
        message = str(error) if isinstance(error, PreparationError) else "Preparation failed; inspect scoped configuration metadata."
        print(json.dumps({"prepared": False, "error": message,
            "application_started": False, "note": "Already validated HTTP challenge/503 config may remain; no existing configuration was overwritten."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
