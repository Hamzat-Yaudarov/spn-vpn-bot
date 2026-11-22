"""Add one exact DE4 gRPC/TLS location; default is a pure no-I/O plan.

The reviewed --before-sha256 belongs to the original vhost (also on retries and
rollback). Mutations require --apply or --rollback plus the fixed target. No
firewall, certificate, Docker, other Nginx files or panel objects are changed.
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


TARGET, DOMAIN = "194.48.250.102", "deeee1.wayspn.com"
SERVICE = "grpc-de4-tls-test"
SITE = Path("/etc/nginx/sites-available/deeee1.wayspn.com")
ENABLED = Path("/etc/nginx/sites-enabled/deeee1.wayspn.com")
BACKUP = Path("/var/backups/spn-grpc-tls-de4-20261031")
ANCHOR = b"    location = /health {\n"
BLOCK = b"""    location = /grpc-de4-tls-test/Tun {
        grpc_pass grpc://127.0.0.1:10089;
        grpc_set_header Host deeee1.wayspn.com;
        grpc_set_header X-Real-IP $remote_addr;
        grpc_set_header X-Forwarded-For $remote_addr;
        grpc_set_header X-Forwarded-Proto https;
        grpc_set_header Forwarded "";
        grpc_connect_timeout 5s;
        grpc_read_timeout 3600s;
        grpc_send_timeout 3600s;
        grpc_next_upstream off;
        client_max_body_size 0;
        client_body_timeout 3600s;
        access_log off;
    }
"""
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8"}
FILES = {"nginx.before.conf", "nginx.candidate.conf", "manifest.json"}


class PatchError(ValueError):
    pass


def require(value):
    if not value:
        raise PatchError("Fixed DE4 gRPC prerequisites changed; details suppressed.")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PatchError("Only fixed DE4 gRPC patch options are accepted.")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def valid_sha(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None)
    return value


def validate_original(raw):
    require(isinstance(raw, bytes) and 0 < len(raw) <= 65_536
            and raw.count(ANCHOR) == 1 and b"grpc-de4-tls-test" not in raw
            and b"10089" not in raw and b"grpc_pass" not in raw)
    # Exact-byte anchors plus the caller-reviewed SHA pin the existing vhost;
    # reject missing/moved cover, strict SNI/Host, WS or CDN selectors.
    start = b"server {\n    listen 443 ssl http2;\n"
    require(raw.count(start) == 1 and b"ssl_reject_handshake on;" in raw)
    begin, end = raw.index(start), raw.index(ANCHOR)
    require(begin < end)
    prefix = raw[begin:end]
    required = (
        b"    listen [::]:443 ssl http2;\n",
        b"    server_name deeee1.wayspn.com;\n",
        b"    ssl_certificate /etc/letsencrypt/live/deeee1.wayspn.com/fullchain.pem;\n",
        b"    ssl_certificate_key /etc/letsencrypt/live/deeee1.wayspn.com/privkey.pem;\n",
        b"    if ($host != deeee1.wayspn.com) { return 444; }\n",
        b"    if ($ssl_server_name != deeee1.wayspn.com) { return 444; }\n",
        b"    location ^~ /direct/ws/ {\n",
        b"        proxy_pass http://127.0.0.1:10088;\n",
        b"    location ^~ /assets/app/ {\n",
        b"        proxy_pass http://127.0.0.1:10085;\n",
    )
    for value in required:
        require(prefix.count(value) == 1)
    return raw


def render(original, before_sha256):
    require(sha(validate_original(original)) == valid_sha(before_sha256))
    candidate = original.replace(ANCHOR, BLOCK + ANCHOR, 1)
    require(candidate.count(BLOCK) == 1 and candidate.replace(BLOCK, b"", 1) == original)
    return candidate


def classify(raw, before_sha256):
    valid_sha(before_sha256)
    if sha(raw) == before_sha256:
        return raw, render(raw, before_sha256), False
    require(raw.count(BLOCK) == 1)
    original = raw.replace(BLOCK, b"", 1)
    candidate = render(original, before_sha256)
    require(raw == candidate)
    return original, candidate, True


def no_symlinks(path):
    require(not any(item.is_symlink() for item in (path, *path.parents)))


def directory(path, *, private=False):
    no_symlinks(path)
    for item in (path, *path.parents):
        info = item.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022)
    if private:
        require(stat.S_IMODE(path.stat().st_mode) == 0o700)


def read_regular(path, *, mode):
    no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
                                 item.st_uid, item.st_gid, item.st_mode, item.st_nlink)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                and stat.S_IMODE(before.st_mode) == mode and 0 < before.st_size <= 65_536)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(65_537)
        require(len(raw) == before.st_size and identity(os.fstat(fd)) == identity(before))
        return raw
    finally:
        os.close(fd)


def command(args, *, timeout=30):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=ENV, timeout=timeout, check=False)
    require(result.returncode == 0 and len(result.stdout) <= 65_536)
    return result.stdout.decode("utf-8").strip()


def target_check(target):
    require(target == TARGET and sys.platform == "linux" and os.geteuid() == 0)
    interfaces = json.loads(command(["/usr/sbin/ip", "-j", "-4", "address", "show", "scope", "global"]))
    require(any(row.get("local") == TARGET for item in interfaces for row in item.get("addr_info", [])))


def preflight(target, before_sha256):
    valid_sha(before_sha256)
    target_check(target)
    for path in (SITE.parent, ENABLED.parent, BACKUP.parent):
        directory(path)
    require(ENABLED.is_symlink() and os.readlink(ENABLED) == str(SITE)
            and ENABLED.lstat().st_uid == 0 and ENABLED.lstat().st_nlink == 1)
    require(command(["/usr/bin/systemctl", "is-active", "nginx"]) == "active")
    current = read_regular(SITE, mode=0o644)
    original, candidate, present = classify(current, before_sha256)
    command(["/usr/sbin/nginx", "-t"])
    return current, original, candidate, present


def write_exclusive(path, raw, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def backup_files(original, candidate):
    manifest = (json.dumps({"format": 1, "target": TARGET, "domain": DOMAIN, "service_name": SERVICE,
                            "before_sha256": sha(original), "candidate_sha256": sha(candidate)},
                           sort_keys=True) + "\n").encode()
    return {"nginx.before.conf": original, "nginx.candidate.conf": candidate, "manifest.json": manifest}


def audit_backup(original, candidate):
    directory(BACKUP, private=True)
    files = backup_files(original, candidate)
    require({item.name for item in BACKUP.iterdir()} == FILES)
    for name, value in files.items():
        require(read_regular(BACKUP / name, mode=0o600) == value)


def backup(original, candidate, *, already_present):
    if BACKUP.exists() or BACKUP.is_symlink():
        audit_backup(original, candidate)
        return True
    require(not already_present)
    BACKUP.mkdir(mode=0o700)
    for name, value in backup_files(original, candidate).items():
        write_exclusive(BACKUP / name, value, 0o600)
    sync_directory(BACKUP)
    sync_directory(BACKUP.parent)
    return False


def atomic_site(raw, *, expected):
    require(read_regular(SITE, mode=0o644) == expected)
    temporary = SITE.with_name("." + SITE.name + ".grpc-de4-tls-new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    owned = os.fstat(fd)
    try:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
        # Fresh guard after temp-file work: never replace a detected admin edit.
        require(read_regular(SITE, mode=0o644) == expected)
        os.replace(temporary, SITE)
        sync_directory(SITE.parent)
    finally:
        os.close(fd)
        try:
            remaining = temporary.lstat()
        except FileNotFoundError:
            pass
        else:
            if (remaining.st_dev, remaining.st_ino) == (owned.st_dev, owned.st_ino):
                temporary.unlink()


def activate(expected):
    require(read_regular(SITE, mode=0o644) == expected)
    command(["/usr/sbin/nginx", "-t"])
    command(["/usr/bin/systemctl", "reload", "nginx"])
    require(command(["/usr/bin/systemctl", "is-active", "nginx"]) == "active"
            and read_regular(SITE, mode=0o644) == expected)


def transition(current, desired, *, restore):
    require(read_regular(SITE, mode=0o644) == current)
    attempted = False
    try:
        attempted = True
        if current != desired:
            atomic_site(desired, expected=current)
        activate(desired)
    except BaseException:
        if attempted:
            now = read_regular(SITE, mode=0o644)
            # Unknown bytes belong to the administrator: refuse automatic restore.
            require(now in (current, desired))
            if now != restore:
                atomic_site(restore, expected=now)
            activate(restore)
        raise PatchError("DE4 gRPC transition failed; inspect retained backup/site before retry.") from None


def apply(target, before_sha256):
    current, original, candidate, present = preflight(target, before_sha256)
    reused = backup(original, candidate, already_present=present)
    transition(current, candidate, restore=original)
    return {"applied": True, "already_present": present, "backup_reused": reused,
            "target": TARGET, "domain": DOMAIN, "candidate_sha256": sha(candidate),
            "backup": str(BACKUP), "nginx": "active", "existing_routes": "unchanged"}


def rollback(target, before_sha256):
    current, original, candidate, present = preflight(target, before_sha256)
    audit_backup(original, candidate)
    # Exact candidate-only classification refuses all unrelated later edits;
    # removal is only the owned block, giving byte-for-byte original content.
    require(candidate.replace(BLOCK, b"", 1) == original)
    if present:
        transition(current, original, restore=candidate)
    return {"rolled_back": present, "already_removed": not present, "target": TARGET,
            "domain": DOMAIN, "before_sha256": sha(original), "backup": str(BACKUP), "nginx": "active"}


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        actions = parser.add_mutually_exclusive_group()
        actions.add_argument("--apply", action="store_true")
        actions.add_argument("--rollback", action="store_true")
        parser.add_argument("--confirm-target")
        parser.add_argument("--before-sha256")
        args = parser.parse_args(argv)
        if args.apply:
            result = apply(args.confirm_target, args.before_sha256)
        elif args.rollback:
            result = rollback(args.confirm_target, args.before_sha256)
        else:
            result = {"plan_only": True, "target": TARGET, "domain": DOMAIN,
                      "add": "/grpc-de4-tls-test/Tun -> 127.0.0.1:10089", "multi_mode": False,
                      "reviewed_before_sha256_required": True, "network": False, "nginx_changed": False}
        print(json.dumps(result, sort_keys=True))
        return 0
    except BaseException as error:
        if isinstance(error, SystemExit) and error.code == 0:
            return 0
        print("DE4 gRPC patch stopped; details suppressed. Inspect backup/site/service before retry.", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
