"""Fixed-target additive Nginx origin patch. Default is a pure, no-I/O plan.

Run --apply explicitly on the approved VPS. Only the selected vhost is replaced;
no SSH, DNS, certificates, firewall, Docker, profile or credential changes occur.
Existing complete matching backups can be reused, never overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


TARGET, DOMAIN = "92.42.102.103", "fi103.wayspn.com"
SITE = Path("/etc/nginx/sites-available/fi103.wayspn.com")
ENABLED = Path("/etc/nginx/sites-enabled/fi103.wayspn.com")
BACKUP = Path("/var/backups/wayspn-yandex-fi-origin-20261030")
BEFORE_SHA256 = "d4aada1c6eabe0e6a923686d10f93db24fbb806a3ea512a438699b1c0329298d"
ANCHOR = b"    location = /health {\n"
# Nginx permits these directives only in http/server context. Pin field buffers
# to 8192 bytes; Xray independently limits the complete header set to 8192.
HEADER_ANCHOR = b"    if ($host != fi103.wayspn.com) { return 444; }\n"
HEADERS = b"    client_header_buffer_size 8k;\n    large_client_header_buffers 4 8k;\n"
BLOCK = b"""    location ^~ /assets/app/ {
        proxy_pass http://127.0.0.1:10085;
        proxy_http_version 1.1;
        proxy_set_header Host fi103.wayspn.com;
        proxy_set_header Connection "";
        proxy_set_header Upgrade "";
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Forwarded "";
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        add_header Cache-Control no-store always;
        access_log off;
    }
"""
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8"}


class OriginError(ValueError): pass


def require(value):
    if not value: raise OriginError("Fixed origin prerequisites changed; details suppressed.")


class Parser(argparse.ArgumentParser):
    def error(self, message): raise OriginError("Only fixed origin-patch options are accepted.")


def sha(raw): return hashlib.sha256(raw).hexdigest()


def render(original):
    require(isinstance(original, bytes) and sha(original) == BEFORE_SHA256
            and original.count(ANCHOR) == 1 and original.count(HEADER_ANCHOR) == 1
            and b"/assets/app/" not in original and HEADERS not in original)
    result = original.replace(ANCHOR, BLOCK + ANCHOR, 1).replace(HEADER_ANCHOR, HEADERS + HEADER_ANCHOR, 1)
    require(result.count(BLOCK) == result.count(HEADERS) == 1
            and result.replace(BLOCK, b"", 1).replace(HEADERS, b"", 1) == original)
    return result


def classify(raw):
    if sha(raw) == BEFORE_SHA256: return raw, render(raw), False
    require(raw.count(BLOCK) == raw.count(HEADERS) == 1)
    original = raw.replace(BLOCK, b"", 1).replace(HEADERS, b"", 1)
    candidate = render(original)
    require(raw == candidate)
    return original, candidate, True


def no_symlinks(path):
    require(not any(item.is_symlink() for item in (path, *path.parents)))


def directory(path, *, private=False):
    no_symlinks(path)
    for item in (path, *path.parents):
        info = item.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022)
    if private: require(stat.S_IMODE(path.stat().st_mode) == 0o700)


def read_regular(path, *, mode):
    no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
                                 item.st_uid, item.st_gid, item.st_mode, item.st_nlink)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                and stat.S_IMODE(before.st_mode) == mode and 0 < before.st_size <= 65_536)
        with os.fdopen(fd, "rb", closefd=False) as stream: raw = stream.read(65_537)
        require(len(raw) == before.st_size and identity(os.fstat(fd)) == identity(before))
        return raw
    finally: os.close(fd)


def command(args, *, timeout=30):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=ENV, timeout=timeout, check=False)
    require(result.returncode == 0 and len(result.stdout) <= 65_536)
    return result.stdout.decode("utf-8").strip()


def target_check(target):
    require(target == TARGET and sys.platform == "linux" and os.geteuid() == 0)
    interfaces = json.loads(command(["/usr/sbin/ip", "-j", "-4", "address", "show", "scope", "global"]))
    require(any(row.get("local") == TARGET for item in interfaces for row in item.get("addr_info", [])))


def preflight(target):
    target_check(target)
    for path in (SITE.parent, ENABLED.parent, BACKUP.parent): directory(path)
    require(ENABLED.is_symlink() and os.readlink(ENABLED) == str(SITE)
            and ENABLED.lstat().st_uid == 0 and ENABLED.lstat().st_nlink == 1)
    require(command(["/usr/bin/systemctl", "is-active", "nginx"]) == "active")
    current = read_regular(SITE, mode=0o644)
    original, candidate, present = classify(current)
    command(["/usr/sbin/nginx", "-t"])
    return current, original, candidate, present


def write_exclusive(path, raw, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchown(fd, 0, 0); os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw); stream.flush(); os.fsync(fd)
    finally: os.close(fd)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def backup(original, candidate, *, already_present):
    manifest = (json.dumps({"format": 1, "target": TARGET, "origin": DOMAIN,
                            "before_sha256": sha(original), "candidate_sha256": sha(candidate)},
                           sort_keys=True) + "\n").encode()
    files = {"nginx.before.conf": original, "nginx.candidate.conf": candidate, "manifest.json": manifest}
    if BACKUP.exists() or BACKUP.is_symlink():
        directory(BACKUP, private=True)
        require({item.name for item in BACKUP.iterdir()} == set(files))
        for name, value in files.items(): require(read_regular(BACKUP / name, mode=0o600) == value)
        return True
    require(not already_present)  # Candidate without our verified backup is not resumable.
    BACKUP.mkdir(mode=0o700)
    for name, value in files.items(): write_exclusive(BACKUP / name, value, 0o600)
    sync_directory(BACKUP); sync_directory(BACKUP.parent)
    return False


def atomic_site(raw):
    temporary = SITE.with_name("." + SITE.name + ".yandex-origin-new")
    # An existing temporary is never removed or overwritten. After exclusive
    # creation, only our own temporary can be cleaned up on a failed replace.
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    owned = os.fstat(fd)
    try:
        os.fchown(fd, 0, 0); os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw); stream.flush(); os.fsync(fd)
        os.replace(temporary, SITE)
        sync_directory(SITE.parent)
    finally:
        os.close(fd)
        try: remaining = temporary.lstat()
        except FileNotFoundError: pass
        else:
            if (remaining.st_dev, remaining.st_ino) == (owned.st_dev, owned.st_ino): temporary.unlink()


def apply(target):
    current, original, candidate, present = preflight(target)
    reused = backup(original, candidate, already_present=present)
    require(read_regular(SITE, mode=0o644) == current)
    attempted = False
    try:
        # A post-replace directory-fsync failure must also restore original.
        attempted = True
        if not present: atomic_site(candidate)
        require(read_regular(SITE, mode=0o644) == candidate)
        command(["/usr/sbin/nginx", "-t"])
        command(["/usr/bin/systemctl", "reload", "nginx"])
        require(command(["/usr/bin/systemctl", "is-active", "nginx"]) == "active"
                and read_regular(SITE, mode=0o644) == candidate)
    except BaseException:
        if attempted:
            # Do not overwrite an unrelated concurrent administrator edit.
            require(read_regular(SITE, mode=0o644) in (original, candidate))
            require(read_regular(BACKUP / "nginx.before.conf", mode=0o600) == original)
            atomic_site(original)
            command(["/usr/sbin/nginx", "-t"])
            command(["/usr/bin/systemctl", "reload", "nginx"])
            require(command(["/usr/bin/systemctl", "is-active", "nginx"]) == "active"
                    and read_regular(SITE, mode=0o644) == original)
        raise OriginError("Origin patch failed; inspect retained backup and Nginx state before retry.") from None
    return {"applied": True, "already_present": present, "backup_reused": reused,
            "target": TARGET, "origin": DOMAIN, "candidate_sha256": sha(candidate),
            "backup": str(BACKUP), "nginx": "active", "ws_grpc": "unchanged"}


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--confirm-target")
        args = parser.parse_args(argv)
        result = (apply(args.confirm_target) if args.apply else
                  {"plan_only": True, "target": TARGET, "origin": DOMAIN, "before_sha256": BEFORE_SHA256,
                   "add": "/assets/app/ -> 127.0.0.1:10085", "network": False, "nginx_changed": False})
        print(json.dumps(result, sort_keys=True)); return 0
    except BaseException as error:
        if isinstance(error, SystemExit) and error.code == 0: return 0
        print("Origin patch stopped; captured details suppressed. Inspect backup/site/service before retry.", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == "__main__": raise SystemExit(main())
