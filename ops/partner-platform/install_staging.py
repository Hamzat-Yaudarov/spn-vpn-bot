"""One-shot installer for the already-cleaned franchise VPS; default is plan.

Run on the authorized Linux VPS, not on the development Mac. --install performs
one official-PyPI wheel installation; it does not SSH, clean old applications,
read/generate runtime credentials, install Nginx, migrate a DB or start services.
"""

from __future__ import annotations

import argparse
import gzip
import grp
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import stat
import subprocess
import sys
import tarfile


TARGET = "92.42.102.103"
EXPECTED_SHA256 = "5fab5be5cb97c367122497c86084c22addf450736cbf39f1e9ad37183b1e1d3c"
ARCHIVE_ROOT = "spn-partner-staging"
RELEASES = Path("/opt/spn-franchise-staging/releases")
RELEASE = RELEASES / "20261021-5fab5be5cb97"
PRIVATE = Path("/etc/spn-partner-private")
STATE = Path("/var/lib/spn-partner")
UNIT_DIRECTORY = Path("/etc/systemd/system")
SERVICE_USER = "spn-partner"
UNITS = ("spn-partner-http.service", "spn-partner-worker.service")
UNIT_SEARCH = tuple(Path(value) for value in (
    "/etc/systemd/system", "/run/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system",
))
REQUIREMENTS = "ops/partner-platform/requirements-staging.txt"
MAX_ARCHIVE = 10 * 1024 * 1024
MAX_UNCOMPRESSED = 24 * 1024 * 1024
MAX_FILE = 5 * 1024 * 1024
SAFE_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "PIP_CONFIG_FILE": "/dev/null",
    "PYTHON_DOTENV_DISABLED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}


class InstallError(ValueError):
    """No captured subprocess output, configuration, or secret data in errors."""


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InstallError("Duplicate manifest JSON key.")
        result[key] = value
    return result


def safe_member(name):
    if not isinstance(name, str) or not name or "\\" in name:
        raise InstallError("Unsafe archive member.")
    path = PurePosixPath(name)
    if (
        path.is_absolute() or str(path) != name
        or any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or path.parts[0] != ARCHIVE_ROOT or len(path.parts) < 2
    ):
        raise InstallError("Unsafe archive member.")
    relative = "/".join(path.parts[1:])
    if (
        relative in {"config.py", "database.py", "main.py"}
        or path.suffix.lower() in {".key", ".pem", ".db", ".sqlite", ".pyc"}
        or path.suffix == ".json"
        and path.name != "PACKAGE_MANIFEST.json"
        and not path.name.endswith(".example.json")
    ):
        raise InstallError("Private or unrelated archive member.")
    return relative


def validate_archive(blob, *, expected_sha256=EXPECTED_SHA256):
    """Authenticate all bytes, then validate paths/types and each file digest."""
    if len(blob) > MAX_ARCHIVE or hashlib.sha256(blob).hexdigest() != expected_sha256:
        raise InstallError("Archive SHA-256 or size does not match the approved release.")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(blob), mode="rb") as stream:
            expanded = stream.read(MAX_UNCOMPRESSED + 1)
        if len(expanded) > MAX_UNCOMPRESSED:
            raise InstallError("Expanded archive exceeds its size limit.")
        files = {}
        with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
            for info in archive:
                name = safe_member(info.name)
                if not info.isfile() or name in files or not 0 <= info.size <= MAX_FILE:
                    raise InstallError("Archive must contain unique bounded regular files.")
                data = archive.extractfile(info).read(MAX_FILE + 1)
                if len(data) != info.size:
                    raise InstallError("Archive member size mismatch.")
                files[name] = data
        manifest = json.loads(files.pop("PACKAGE_MANIFEST.json"), object_pairs_hook=unique_object)
        if (
            manifest["format"] != 1 or manifest["archive_root"] != ARCHIVE_ROOT
            or manifest["runtime_default"] != "disabled"
            or manifest["contains_credentials"] is not False
            or set(manifest["files"]) != set(files)
            or not {"scripts/partner_runtime.py", "scripts/partner_setup.py", REQUIREMENTS} <= set(files)
        ):
            raise InstallError("Archive manifest does not describe the approved runtime.")
        for name, data in files.items():
            item = manifest["files"][name]
            if item["bytes"] != len(data) or item["sha256"] != hashlib.sha256(data).hexdigest():
                raise InstallError("Archive manifest file digest mismatch.")
        files["PACKAGE_MANIFEST.json"] = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
        )
        return files
    except InstallError:
        raise
    except Exception:
        raise InstallError("Archive or manifest is invalid.") from None


def checked_command(command, *, cwd=None, allowed_codes=(0,), timeout=600):
    result = subprocess.run(
        command, cwd=cwd, env=SAFE_ENV, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
    )
    if result.returncode not in allowed_codes:
        raise InstallError("Required deployment command failed; raw output suppressed.")
    return result.stdout.decode("utf-8", errors="replace").strip()


def no_symlinks(path):
    for item in (path, *path.parents):
        if item.is_symlink():
            raise InstallError("Deployment paths must not contain symbolic links.")


def directory(path, *, uid, gid=None, exact_mode=None):
    no_symlinks(path)
    info = path.stat()
    if (
        not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
        or info.st_mode & 0o022
        or gid is not None and info.st_gid != gid
        or exact_mode is not None and stat.S_IMODE(info.st_mode) != exact_mode
    ):
        raise InstallError("Existing deployment directory ownership or mode is unsafe.")


def no_existing_unit_or_override(unit):
    # systemd also inherits service-wide and dash-truncated prefix drop-ins.
    # Refuse all unreviewed overrides instead of allowing one to alter identity,
    # ExecStart, credential sources, logging, or sandbox settings later.
    names = (unit, unit + ".d", "spn-partner-.service.d", "spn-.service.d", "service.d")
    for prefix in UNIT_SEARCH:
        for name in names:
            path = prefix / name
            if path.exists() or path.is_symlink():
                raise InstallError("A partner unit or inherited override already exists; review required.")


def preflight(target):
    if target != TARGET or sys.platform != "linux" or os.geteuid() != 0:
        raise InstallError("Installation requires root on the explicitly confirmed Linux staging VPS.")
    if sys.version_info < (3, 11):
        raise InstallError("Python 3.11 or newer is required.")
    account = pwd.getpwnam(SERVICE_USER)
    group = grp.getgrnam(SERVICE_USER)
    if (
        account.pw_uid == 0 or account.pw_gid != group.gr_gid
        or account.pw_shell not in {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false"}
    ):
        raise InstallError("The existing service account must be unprivileged and no-login.")
    for path in (Path("/opt"), RELEASES.parent, RELEASES, UNIT_DIRECTORY):
        directory(path, uid=0)
    directory(PRIVATE, uid=0, exact_mode=0o700)
    directory(STATE, uid=account.pw_uid, gid=group.gr_gid, exact_mode=0o700)
    if RELEASE.exists() or RELEASE.is_symlink():
        raise InstallError("Fixed release path already exists; no overwrite or cleanup is allowed.")
    config = PRIVATE / "staging.json"
    if config.exists() or config.is_symlink():
        info = config.lstat()  # Metadata only: never read or print configuration.
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65_536
        ):
            raise InstallError("Existing private config metadata is unsafe; it was not read.")
    version = checked_command(["/usr/bin/systemctl", "--version"])
    match = re.match(r"systemd (\d+)", version)
    if not match or int(match[1]) < 247:
        raise InstallError("systemd 247+ is required for private credential delivery.")
    for unit in UNITS:
        no_existing_unit_or_override(unit)
        loaded = checked_command([
            "/usr/bin/systemctl", "show", "--property=LoadState", "--value", unit,
        ])
        if loaded != "not-found":
            raise InstallError("A partner unit is already loaded; installation stopped.")
    return account.pw_uid, group.gr_gid


def render_unit(kind):
    if kind not in {"http", "worker"}:
        raise InstallError("Unknown staging service kind.")
    runtime = f"spn-partner-{kind}"
    action = "--serve --port 8966" if kind == "http" else "--worker"
    stop_timeout = 45 if kind == "http" else 360
    # Copy as the service user: load_config deliberately requires effective-UID
    # ownership, not just credential readability. The root-only source parent
    # stays 0700. RuntimeDirectory's copy disappears when the unit stops.
    return f"""# Installed disabled; no credential values are embedded here.
[Unit]
Description=Isolated franchise staging {kind}
After=network-online.target
Wants=network-online.target
ConditionPathExists={PRIVATE}/staging.json
StartLimitIntervalSec=120
StartLimitBurst=3

[Service]
Type=simple
User={SERVICE_USER}
Group={SERVICE_USER}
WorkingDirectory={RELEASE}
Environment=PYTHON_DOTENV_DISABLED=1
Environment=PYTHONDONTWRITEBYTECODE=1
LoadCredential=staging.json:{PRIVATE}/staging.json
RuntimeDirectory={runtime}
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=no
ExecStartPre=/usr/bin/install --mode=0600 --no-target-directory ${{CREDENTIALS_DIRECTORY}}/staging.json /run/{runtime}/staging.json
ExecStart={RELEASE}/.venv/bin/python -I scripts/partner_runtime.py --config /run/{runtime}/staging.json {action}
Restart=on-failure
RestartSec=15
TimeoutStopSec={stop_timeout}
KillSignal=SIGTERM
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ReadWritePaths={STATE}
RestrictSUIDSGID=true
LockPersonality=true
LimitCORE=0
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
StandardInput=null
StandardOutput=null
StandardError=null

[Install]
WantedBy=multi-user.target
"""


def write_exclusive(path, data, *, mode, gid):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchown(stream.fileno(), 0, gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # os.open succeeded exclusively; this is never a pre-existing file.
        path.unlink()
        raise


def extract_release(files, gid):
    RELEASE.mkdir(mode=0o750)
    os.chown(RELEASE, 0, gid)
    for name, data in sorted(files.items()):
        destination = RELEASE.joinpath(*PurePosixPath(name).parts)
        destination.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        write_exclusive(destination, data, mode=0o640, gid=gid)
    for current, folders, _ in os.walk(RELEASE):
        os.chown(current, 0, gid)
        os.chmod(current, 0o750)


def protect_release(gid):
    """Keep code/venv immutable to the account that will execute them."""
    for current, folders, files in os.walk(RELEASE, followlinks=False):
        os.chown(current, 0, gid)
        os.chmod(current, 0o750)
        for name in folders + files:
            path = Path(current) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                # venv may create lib64 -> lib; never follow/chmod its target.
                if not path.resolve().is_relative_to(RELEASE):
                    raise InstallError("Unexpected external virtual-environment symlink.")
                os.lchown(path, 0, gid)
            elif stat.S_ISDIR(info.st_mode):
                continue
            elif stat.S_ISREG(info.st_mode):
                os.chown(path, 0, gid)
                os.chmod(path, 0o750 if info.st_mode & 0o111 else 0o640)
            else:
                raise InstallError("Unexpected non-regular release entry.")


def install(archive_path, target):
    _, gid = preflight(target)
    no_symlinks(archive_path)
    fd = os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
            or info.st_mode & 0o022 or info.st_size > MAX_ARCHIVE
        ):
            raise InstallError("Archive must be a root-owned, non-writable regular file.")
        files = validate_archive(stream.read(MAX_ARCHIVE + 1))
    os.umask(0o077)
    installed_units = []
    complete = False
    try:
        print("Approved archive verified; creating one immutable staging release.", flush=True)
        extract_release(files, gid)
        print("Installing staging wheels from official PyPI; no services will start.", flush=True)
        checked_command([sys.executable, "-I", "-m", "venv", "--copies", str(RELEASE / ".venv")])
        python = str(RELEASE / ".venv/bin/python")
        checked_command([
            python, "-I", "-m", "pip", "--disable-pip-version-check", "install",
            "--index-url", "https://pypi.org/simple", "--only-binary=:all:",
            "--no-cache-dir", "--no-compile", "--require-virtualenv",
            "-r", str(RELEASE / REQUIREMENTS),
        ])
        checked_command([python, "-I", "-m", "pip", "--disable-pip-version-check", "check"])
        protect_release(gid)
        check = json.loads(checked_command([
            "/usr/sbin/runuser", "--user", SERVICE_USER, "--", python, "-I",
            "scripts/partner_runtime.py", "--check",
        ], cwd=RELEASE))
        if (
            check.get("mode") != "disabled" or check.get("production_ready") is not False
            or "runtime_disabled" not in check.get("blockers", [])
        ):
            raise InstallError("Unprivileged offline runtime check did not remain disabled.")
        checked_command([
            "/usr/sbin/runuser", "--user", SERVICE_USER, "--", python, "-I",
            "scripts/partner_setup.py", "--help",
        ], cwd=RELEASE)
        staging = RELEASE / "deployment-units"
        staging.mkdir(mode=0o750)
        os.chown(staging, 0, gid)
        for kind in ("http", "worker"):
            write_exclusive(staging / f"spn-partner-{kind}.service",
                            render_unit(kind).encode(), mode=0o640, gid=gid)
        checked_command(["/usr/bin/systemd-analyze", "verify", *(str(staging / name) for name in UNITS)])
        for name in UNITS:
            destination = UNIT_DIRECTORY / name
            write_exclusive(destination, (staging / name).read_bytes(), mode=0o644, gid=0)
            installed_units.append(destination)
        checked_command(["/usr/bin/systemctl", "daemon-reload"])
        for name in UNITS:
            overrides = checked_command([
                "/usr/bin/systemctl", "show", "--property=DropInPaths", "--value", name,
            ])
            enabled = checked_command(
                ["/usr/bin/systemctl", "is-enabled", name], allowed_codes=(1, 4)
            )
            active = checked_command(
                ["/usr/bin/systemctl", "is-active", name], allowed_codes=(3, 4)
            )
            if overrides or enabled != "disabled" or active != "inactive":
                raise InstallError("Installed units did not remain disabled and inactive.")
        complete = True
        print(json.dumps({
            "release": str(RELEASE), "archive_sha256": EXPECTED_SHA256,
            "offline_check": "runtime_disabled", "units": "disabled_and_inactive",
            "configuration": "not_read_or_created", "nginx": "not_changed",
        }, sort_keys=True))
    finally:
        if not complete and installed_units:
            # Roll back only files this invocation exclusively created. Never
            # delete an old app, an old release, credentials, or service data.
            for path in installed_units:
                path.unlink()
            checked_command(["/usr/bin/systemctl", "daemon-reload"])
        # A failed new release is deliberately retained, never auto-deleted.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("/root/spn-partner-staging-20261021.tar.gz"))
    parser.add_argument("--confirm-target", help="Explicitly confirm 92.42.102.103 for --install")
    parser.add_argument("--install", action="store_true", help="Install wheels and disabled units; never start them")
    args = parser.parse_args(argv)
    if not args.install:
        print(json.dumps({
            "plan_only": True, "target_confirmation_required": TARGET,
            "release": str(RELEASE), "approved_archive_sha256": EXPECTED_SHA256,
            "pip_index": "https://pypi.org/simple", "services_after_install": "disabled_and_inactive",
            "no_cleanup": True, "no_config_read_or_generation": True,
            "no_nginx_dns_tls_database_changes": True,
        }, sort_keys=True))
        return 0
    try:
        install(args.archive.absolute(), args.confirm_target)
        return 0
    except KeyboardInterrupt:
        print("Installation interrupted; no service was started. New release may remain.", file=sys.stderr)
        return 130
    except Exception:
        print("Installation stopped safely. Raw command output and secrets suppressed; new release may remain.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
