#!/usr/bin/env python3
"""Select the preferred-source helper without rewriting an established plan."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


LEGACY_HASH = 'cf988e389588da27975a69ce20785f46ee9f0f4bc3c4f1f3b386eb54743f22b0'
HELPER = 'preferred_source.py'
INCOMING = 'preferred_source.incoming.py'


def read_owned(path):
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError('Unsafe source helper path')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or not 0 < info.st_size <= 131072):
            raise ValueError('Unsafe source helper or plan')
        data = stream.read(131073)
    if len(data) > 131072:
        raise ValueError('Source helper or plan too large')
    return data


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def select(stage, persistent, current_hash):
    info = stage.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('Unsafe source helper directory')
    incoming = stage / INCOMING
    helper = stage / HELPER
    if digest(read_owned(incoming)) != current_hash:
        raise ValueError('Incoming preferred-source helper differs')
    local_plan = read_owned(stage / 'preferred-source-plan.json')
    boot_plan = read_owned(persistent / 'plan.json')
    plans = [json.loads(raw) for raw in (local_plan, boot_plan) if raw is not None]
    existing_hash = digest(read_owned(helper))
    if plans:
        pinned = plans[0].get('helperHash')
        # A resumable IP retarget can leave the two plans temporarily different.
        # Their helper identity is immutable throughout that operation.
        if any(plan.get('helperHash') != pinned for plan in plans):
            raise ValueError('Stage and persistent source helper pins differ')
        if pinned not in (LEGACY_HASH, current_hash):
            raise ValueError('Unknown saved preferred-source helper')
        if boot_plan is not None and digest(read_owned(persistent / 'source.py')) not in (None, pinned):
            raise ValueError('Persistent preferred-source helper differs')
        if existing_hash == pinned:
            incoming.unlink()
            return
        if pinned != current_hash or existing_hash is not None:
            raise ValueError('Staged preferred-source helper differs; existing plans stay pinned')
    elif existing_hash not in (None, LEGACY_HASH, current_hash):
        raise ValueError('Unrecognized preferred-source helper in the staging directory')
    os.replace(incoming, helper)


if __name__ == '__main__':
    try:
        deployment, expected = sys.argv[1:]
        if not re.fullmatch(r'[0-9a-f]{32}', deployment) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('Invalid source helper selection')
        select(Path('/root/spn-reality-managed-' + deployment),
               Path('/opt/spn-reality-network') / deployment, expected)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        print('Stopped: preferred-source helper selection failed (' + type(error).__name__ + ')', file=sys.stderr)
        raise SystemExit(2)
