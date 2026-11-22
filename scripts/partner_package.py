"""Build a local, secret-free allowlisted franchise staging archive.

Standard library only. No dotenv, runtime import, subprocess, network, config
generation, migration, service installation or service start is performed.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = "spn-partner-staging"
MANIFEST_NAME = "PACKAGE_MANIFEST.json"
TMP_ROOT = Path("/private/tmp")
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024

# This is the complete shipping contract: no globs, directory recursion, git
# export, local configuration, main-bot adapter or implicit dependency copy.
PACKAGE_FILES = (
    "partner_brand_web.py",
    "partner_client_web.py",
    "partner_finance_web.py",
    "partner_management_web.py",
    "partner_manager_web.py",
    "partner_platform_web.py",
    "partner_public_checkout_web.py",
    "partner_subscription_web.py",
    "partner_support_web.py",
    "partner_web.py",
    "scripts/partner_runtime.py",
    "scripts/partner_enable_owner_pilot.py",
    "scripts/partner_setup.py",
    "services/__init__.py",
    "services/connection_instructions.py",
    "services/partner_bot.py",
    "services/partner_brand_assets.py",
    "services/partner_brand_defaults.py",
    "services/partner_callback_ack.py",
    "services/partner_catalog.py",
    "services/partner_checkout.py",
    "services/partner_commerce_store.py",
    "services/partner_enrollment_store.py",
    "services/partner_entitlements.py",
    "services/partner_entry_checkout.py",
    "services/partner_entry_notifications.py",
    "services/partner_entry_pricing.py",
    "services/partner_finance.py",
    "services/partner_finance_notifications.py",
    "services/partner_launcher.py",
    "services/partner_management.py",
    "services/partner_manager.py",
    "services/partner_manager_access.py",
    "services/partner_managed_bots.py",
    "services/partner_notifications.py",
    "services/partner_panel_identity.py",
    "services/partner_public_checkout.py",
    "services/partner_recovery.py",
    "services/partner_remnawave.py",
    "services/partner_runtime.py",
    "services/partner_runtime_catalog.py",
    "services/partner_security.py",
    "services/partner_store.py",
    "services/partner_support.py",
    "services/partner_subscription_store.py",
    "services/partner_subscriptions.py",
    "services/partner_traffic.py",
    "services/partner_traffic_store.py",
    "services/partner_web_auth.py",
    "services/partner_yookassa.py",
    "services/telegram_auth.py",
    "static/partners/index.html",
    "static/partners/support.html",
    "static/partners/assets/partners.css",
    "static/partners/assets/partners.js",
    "static/partners/assets/workspace.js",
    "static/partners/assets/support.js",
    "static/partner-subscription/assets/subscription.css",
    "static/partner-subscription/assets/subscription.js",
    "ops/partner-platform/BRANDED_LINKS.md",
    "ops/partner-platform/RELEASE_CHECKLIST.md",
    "ops/partner-platform/SECURE_CONFIG.md",
    "ops/partner-platform/STAGING_PACKAGE.md",
    "ops/partner-platform/SUPABASE_INSTALLATION.md",
    "ops/partner-platform/SUPABASE_SETUP.md",
    "ops/partner-platform/branding.sql",
    "ops/partner-platform/commerce.sql",
    "ops/partner-platform/enrollment.sql",
    "ops/partner-platform/entry_launch_notifications.sql",
    "ops/partner-platform/entitlements.sql",
    "ops/partner-platform/finance.sql",
    "ops/partner-platform/management.sql",
    "ops/partner-platform/managed_bots.sql",
    "ops/partner-platform/nginx-subdomains.conf.example",
    "ops/partner-platform/notifications.sql",
    "ops/partner-platform/operations.sql",
    "ops/partner-platform/owner_entry_pilot.sql",
    "ops/partner-platform/panel_identity.sql",
    "ops/partner-platform/public_checkout.sql",
    "ops/partner-platform/public_entry_trial.sql",
    "ops/partner-platform/payment_first_entry.sql",
    "ops/partner-platform/requirements-staging.txt",
    "ops/partner-platform/runtime-nginx.conf.example",
    "ops/partner-platform/runtime.example.json",
    "ops/partner-platform/runtime.supabase.example.json",
    "ops/partner-platform/schema.sql",
    "ops/partner-platform/spn-partner-http.service.example",
    "ops/partner-platform/spn-partner-worker.service.example",
    "ops/partner-platform/subscriptions.sql",
    "ops/partner-platform/supabase_runtime_role.sql",
    "ops/partner-platform/supabase_security.sql",
    "ops/partner-platform/traffic.sql",
)

_SECRET_PATTERNS = (
    rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----",
    rb"\b[0-9]{7,12}:[A-Za-z0-9_-]{30,}\b",
    rb"\bsb_secret_[A-Za-z0-9_-]{16,}\b",
    rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
)


class PackageError(ValueError):
    """Fixed messages must never echo source bytes or submitted paths."""


def member_name(value):
    """Validate POSIX archive names before ever reading or adding a member."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise PackageError("Invalid package manifest path.")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(
            part.startswith(".")
            or part in {"__pycache__", "node_modules", "release", "artifacts"}
            for part in path.parts
        )
        or path.suffix.lower()
        in {".pem", ".key", ".p12", ".pfx", ".pyc", ".db", ".sqlite", ".sqlite3"}
        or path.suffix == ".json" and not path.name.endswith(".example.json")
    ):
        raise PackageError("Unsafe package manifest path.")
    return value


def read_source(root, name):
    """Open every directory without following links, then a bounded real file."""
    directory = file_fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        directory = os.open(root, flags | getattr(os, "O_DIRECTORY", 0))
        parts = PurePosixPath(member_name(name)).parts
        for part in parts[:-1]:
            child = os.open(
                part, flags | getattr(os, "O_DIRECTORY", 0), dir_fd=directory
            )
            os.close(directory)
            directory = child
        file_fd = os.open(
            parts[-1], flags | getattr(os, "O_NONBLOCK", 0), dir_fd=directory
        )
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > MAX_FILE_BYTES
        ):
            raise PackageError("Package source must be a bounded regular file.")
        chunks, size = [], 0
        while True:
            chunk = os.read(file_fd, min(65_536, MAX_FILE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise PackageError("Package source exceeded its size limit.")
        after = os.fstat(file_fd)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise PackageError("Package source changed during reading; retry.")
        data = b"".join(chunks)
        if len(data) != before.st_size:
            raise PackageError("Package source changed during reading; retry.")
        if any(re.search(pattern, data) for pattern in _SECRET_PATTERNS):
            raise PackageError("Possible credential in allowlisted source; package refused.")
        # The only concrete password-bearing DSN permitted is the public setup
        # placeholder. Source f-strings are not concrete password-bearing DSNs.
        for match in re.finditer(rb"postgres(?:ql)?://[^\s/@:]+:([^\s/@]+)@", data):
            if match[1] != b"SET_PASSWORD_LOCALLY":
                raise PackageError("Possible database credential; package refused.")
        if name.endswith(".example.json"):
            validate_public_template(data)
        return data
    except OSError:
        raise PackageError("Package source missing, unsafe, or unreadable.") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory is not None:
            os.close(directory)


def validate_public_template(data):
    """Do not ship an example that has been repurposed as an enabled config."""
    try:
        config = json.loads(data)
        runtime = config["runtime"]
        if (
            set(config) != {"runtime", "staging_addon_catalog"}
            or config["staging_addon_catalog"] is not None
            or runtime["mode"] != "disabled"
            or runtime["allow_staging_network"] is not False
            or runtime["allow_staging_writes"] is not False
            or any(
                value is not False
                for key, value in runtime.items()
                if key.startswith("enable_") or key.endswith("_enabled")
            )
            or any(
                runtime.get(key) not in (None, "", [])
                for key in (
                    "manager_bot_token", "encryption_keys", "webhook_key_hex",
                    "subscription_key_hex", "yookassa_shop_id", "yookassa_secret_key",
                    "remnawave_api_token", "database_ca_file",
                )
            )
        ):
            raise ValueError()
    except Exception:
        raise PackageError("JSON example must remain disabled and credential-free.") from None


def output_path(value):
    path = Path(value).expanduser().absolute()
    if path.suffixes[-2:] != [".tar", ".gz"] or ".." in path.parts:
        raise PackageError("Choose a new .tar.gz output under /private/tmp.")
    if not path.is_relative_to(TMP_ROOT) or path == TMP_ROOT:
        raise PackageError("Package output must be under /private/tmp.")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise PackageError("Symbolic links are not allowed in the output path.")
    if path.exists() or not path.parent.is_dir():
        raise PackageError("Output already exists or its directory is missing.")
    return path


def build_package(output, *, source_root=ROOT):
    """Read exactly the allowlist; exclusively create a private tar.gz locally."""
    output = output_path(output)
    names = tuple(sorted(member_name(name) for name in PACKAGE_FILES))
    if len(names) != len(set(names)):
        raise PackageError("Duplicate package manifest path.")
    payload = {name: read_source(source_root, name) for name in names}
    if sum(map(len, payload.values())) > MAX_TOTAL_BYTES:
        raise PackageError("Package exceeded its total size limit.")
    manifest = {
        "format": 1,
        "archive_root": ARCHIVE_ROOT,
        "runtime_default": "disabled",
        "contains_credentials": False,
        "files": {
            name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in payload.items()
        },
    }
    payload[MANIFEST_NAME] = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode()
        + b"\n"
    )
    created = False
    try:
        fd = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            with gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                    for name, data in sorted(payload.items()):
                        info = tarfile.TarInfo(f"{ARCHIVE_ROOT}/{name}")
                        info.size = len(data)
                        info.mode = 0o644
                        info.uid = info.gid = info.mtime = 0
                        info.uname = info.gname = ""
                        archive.addfile(info, io.BytesIO(data))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if created:
            output.unlink()
        raise
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", default="/private/tmp/spn-partner-staging.tar.gz",
        help="New archive under /private/tmp; never overwrites an existing file",
    )
    args = parser.parse_args(argv)
    try:
        path = build_package(args.output)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        print(json.dumps({"archive": str(path), "sha256": digest,
                          "source_files": len(PACKAGE_FILES)}, sort_keys=True))
        return 0
    except PackageError as error:
        print(str(error))
        return 2
    except Exception:
        print("Package creation failed; no source data or credentials were printed.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
