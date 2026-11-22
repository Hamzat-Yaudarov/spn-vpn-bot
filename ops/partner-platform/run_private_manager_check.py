"""Fixed isolated probe/manager bootstrap through systemd credentials.

Default is plan-only. --run explicitly permits the chosen external operation.
The root runner validates metadata only and NEVER reads staging.json contents.
No HTTP/worker start, webhook replacement, SQL mutation or persistent secret copy.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


TARGET = "92.42.102.103"
PRIVATE = Path("/etc/spn-partner-private")
SOURCE = PRIVATE / "staging.json"
RELEASE = Path("/opt/spn-franchise-staging/releases/20261021-5fab5be5cb97")
PYTHON = RELEASE / ".venv/bin/python"
RUNTIME = RELEASE / "scripts/partner_runtime.py"
ACTIONS = ("probe", "bootstrap-manager")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8"}


class CheckError(ValueError):
    pass


def child_code(action):
    if action not in ACTIONS:
        raise CheckError("Unsupported private check action.")
    destination = f"/run/spn-partner-{action}/staging.json"
    # Only the service UID receives/copies bytes. No shell interpolation, config
    # serialization, credential stdout, or exception text reaches the terminal.
    return f"""import json, os, sys
try:
    source = os.path.join(os.environ['CREDENTIALS_DIRECTORY'], 'staging.json')
    with open(source, 'rb') as handle:
        data = handle.read(65537)
    if not data or len(data) > 65536:
        raise ValueError()
    fd = os.open({destination!r}, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        while data:
            count = os.write(fd, data)
            if count <= 0:
                raise OSError()
            data = data[count:]
    finally:
        os.close(fd)
    os.execv({str(PYTHON)!r}, [{str(PYTHON)!r}, '-I', '-B', {str(RUNTIME)!r}, '--config', {destination!r}, '--{action}'])
except Exception:
    print(json.dumps({{'error': 'private_runtime_credential_delivery_failed'}}), flush=True)
    sys.exit(2)
"""


def build_argv(action):
    code = child_code(action)
    properties = (
        "User=spn-partner", "Group=spn-partner",
        f"WorkingDirectory={RELEASE}",
        f"LoadCredential=staging.json:{SOURCE}",
        f"RuntimeDirectory=spn-partner-{action}",
        "RuntimeDirectoryMode=0700", "RuntimeDirectoryPreserve=no",
        "NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=strict",
        "ProtectHome=yes", "UMask=0077", "LimitCORE=0",
        "StandardError=null", "RestrictSUIDSGID=yes", "LockPersonality=yes",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        # Bound the unit itself too: timing out only the systemd-run client must
        # not leave a detached operation running against DB/Telegram.
        "RuntimeMaxSec=165s", "TimeoutStopSec=10s", "KillMode=control-group",
    )
    return [
        "/usr/bin/systemd-run", "--wait", "--pipe", "--collect", "--quiet",
        f"--unit=spn-partner-private-{action}.service",
        "--setenv=PYTHON_DOTENV_DISABLED=1", "--setenv=PYTHONDONTWRITEBYTECODE=1",
        *(f"--property={value}" for value in properties),
        "--", str(PYTHON), "-I", "-B", "-c", code,
    ]


def trusted_parents(path):
    for parent in (path, *path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise CheckError("Unsafe deployment parent metadata.")


def preflight():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise CheckError("Run only as root on the authorized Linux VPS.")
    trusted_parents(PRIVATE)
    if stat.S_IMODE(PRIVATE.lstat().st_mode) != 0o700:
        raise CheckError("Private source directory must remain root-owned 0700.")
    info = SOURCE.lstat()  # Metadata only: no open/read/parse of credentials.
    if (
        not stat.S_ISREG(info.st_mode) or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
        or not 0 < info.st_size <= 65536
    ):
        raise CheckError("Private config must be a root-owned regular 0600 file.")
    trusted_parents(RELEASE)
    resolved_python = PYTHON.resolve(strict=True)
    for parent in (RUNTIME.parent, PYTHON.parent, resolved_python.parent):
        trusted_parents(parent)
    for path in (RUNTIME, resolved_python):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise CheckError("Installed runtime metadata needs review.")
    result = subprocess.run(
        ["/usr/sbin/ip", "-j", "-4", "address", "show"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=ENV, timeout=10, check=True,
    )
    try:
        addresses = {
            row["local"] for interface in json.loads(result.stdout)
            for row in interface.get("addr_info", []) if row.get("family") == "inet"
        }
    except (ValueError, TypeError, KeyError, AttributeError):
        raise CheckError("Cannot confirm the local VPS address.") from None
    if TARGET not in addresses:
        raise CheckError("The approved VPS address is not present locally.")


def run(action):
    argv = build_argv(action)
    preflight()
    result = subprocess.run(
        argv, shell=False, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=ENV, timeout=180, check=False,
        # stdout is inherited: only the runtime's already-redacted JSON passes
        # through. The runner never captures or opens credential contents.
    )
    if result.returncode:
        print(json.dumps({"error": "private_runtime_check_failed", "action": action}), flush=True)
        return 2
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=ACTIONS, required=True)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    if not args.run:
        print(json.dumps({"plan_only": True, "action": args.action,
            "target": TARGET, "service_user": "spn-partner",
            "credential_contents_read": False, "application_started": False}))
        return 0
    try:
        return run(args.action)
    except Exception:
        print(json.dumps({"error": "private_runtime_metadata_or_execution_failed",
            "action": args.action, "credential_contents_read": False}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
