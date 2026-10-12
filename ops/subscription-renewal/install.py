"""Install only the reviewed renewal files, with a backup and automatic rollback."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import urlopen


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def service_status():
    return subprocess.run(
        ["systemctl", "is-active", "spn-bot"], capture_output=True, text=True,
    ).stdout.strip()


def check_health():
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            with urlopen("http://127.0.0.1:8000/health", timeout=2) as response:
                data = json.load(response)
            with urlopen("http://127.0.0.1:8000/renew", timeout=2) as response:
                page = response.read().decode()
            if (service_status() == "active" and data.get("status") == "ok"
                    and "Yes" in data.get("bot_available", "")
                    and "Продлить подписку" in page):
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("Bot or renewal page did not become healthy")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    target = args.target.resolve()
    source = Path(__file__).resolve().parents[2]
    manifest_path = source / "ops/subscription-renewal/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entries = manifest["files"]
    if len({entry["path"] for entry in entries}) != len(entries):
        raise RuntimeError("Duplicate deployment paths")
    for entry in entries:
        name = entry["path"]
        parts = Path(name).parts
        if name.startswith("/") or ".." in parts or any(part.startswith(".") for part in parts):
            raise RuntimeError("Unsafe deployment path")
        destination = target / name
        if destination.is_symlink() or not destination.resolve().is_relative_to(target):
            raise RuntimeError("Deployment path leaves the bot directory")
        if digest(source / name) != entry["new_sha256"]:
            raise RuntimeError("Package checksum mismatch: " + name)
        if digest(destination) != entry["base_sha256"]:
            raise RuntimeError("Production differs from the reviewed baseline: " + name)
        if name.endswith(".py"):
            compile((source / name).read_bytes(), name, "exec")
    for name, expected in manifest["dependencies"].items():
        if digest(target / name) != expected:
            raise RuntimeError("Production dependency differs: " + name)
    print(f"CHECKED {len(entries)} renewal files and {len(manifest['dependencies'])} dependencies", flush=True)
    if not args.apply:
        return
    if service_status() != "active":
        raise RuntimeError("Expected spn-bot to be active before deployment")
    python = target / "venv/bin/python"
    env_hash = digest(target / ".env")
    backup = Path(tempfile.mkdtemp(prefix="spn-before-renewal-", dir="/root"))
    originals = backup / "files"
    originals.mkdir()
    for entry in entries:
        if entry["base_sha256"] is not None:
            saved = originals / entry["path"]
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target / entry["path"], saved)
            if digest(saved) != entry["base_sha256"]:
                raise RuntimeError("Production changed while creating the backup")
    shutil.copy2(manifest_path, backup / "manifest.json")
    with tarfile.open(backup / "files.tar.gz", "w:gz") as archive:
        archive.add(originals, arcname="files")
    print("BACKUP " + str(backup), flush=True)
    try:
        # Allow systemd's graceful shutdown window to finish before installing.
        subprocess.run(["systemctl", "stop", "spn-bot"], check=True, timeout=180)
    except BaseException:
        subprocess.run(["systemctl", "start", "--no-block", "spn-bot"], check=False)
        raise
    if service_status() not in {"inactive", "failed"}:
        subprocess.run(["systemctl", "start", "spn-bot"], check=False)
        raise RuntimeError("Bot did not stop cleanly; no application files changed")
    installed = []
    try:
        # Recheck after stopping: another deployment must not be overwritten.
        for entry in entries:
            if digest(target / entry["path"]) != entry["base_sha256"]:
                raise RuntimeError("Production changed during deployment")
        for entry in entries:
            destination = target / entry["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".renewal-new")
            shutil.copy2(source / entry["path"], temporary)
            os.chmod(temporary, (originals / entry["path"]).stat().st_mode & 0o777
                     if entry["base_sha256"] is not None else 0o644)
            os.replace(temporary, destination)
            installed.append(entry)
        subprocess.run([str(python), "-c", "import webhooks; import subscription_renewal"],
                       cwd=target, check=True, timeout=30)
        if digest(target / ".env") != env_hash:
            raise RuntimeError("Environment changed during deployment")
        subprocess.run(["systemctl", "start", "spn-bot"], check=True, timeout=60)
        check_health()
    except BaseException:
        subprocess.run(["systemctl", "stop", "spn-bot"], check=False, timeout=60)
        for entry in reversed(installed):
            destination = target / entry["path"]
            if entry["base_sha256"] is not None:
                shutil.copy2(originals / entry["path"], destination)
            elif digest(destination) == entry["new_sha256"]:
                destination.unlink()
        subprocess.run(["systemctl", "start", "spn-bot"], check=True, timeout=60)
        print("ROLLED BACK application files; backup " + str(backup), flush=True)
        raise
    print("INSTALLED; spn-bot and /renew are healthy. Environment and subscription settings unchanged.", flush=True)


if __name__ == "__main__":
    main()
