#!/usr/bin/env python3
"""Build a source-only monitor archive. Never includes credentials or journals."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parents[2]


def sources(root=ROOT):
    root = Path(root)
    paths = list((root / 'spn_reserve_fleet').glob('*.py'))
    paths += list((root / 'spn_reserve_fleet/static').glob('*'))
    paths += list((root / 'spn_reserve_fleet/deploy').glob('*.py'))
    paths += [root / 'spn_reserve_fleet/deploy/spn-reserve-fleet.service']
    paths += [path for path in (root / 'ops/universal-reality').glob('*')
              if path.suffix in ('.py', '.sh', '.cfg') and not path.name.startswith('test_')]
    paths += [root / 'ops/universal-reality/scanner_lists.json',
              root / 'ops/universal-reality/tls-site-index.html']
    paths += [root / 'ops/universal-direct/deploy.py', root / 'ops/host-health-audit/e2e.py']
    paths += [path for path in (root / 'ops/universal-yandex').glob('*')
              if path.suffix in ('.py', '.sh', '.cfg') and not path.name.startswith('test_')]
    paths += [root / 'ops/universal-cascade/host_checks.py']
    files = {}
    for path in sorted(set(paths)):
        if path.is_symlink() or not path.is_file():
            raise ValueError('Bundle sources must be ordinary source files')
        relative = path.relative_to(root).as_posix()
        if (path.suffix not in ('.py', '.sh', '.cfg', '.service', '.js', '.css', '.html')
                and relative != 'ops/universal-reality/scanner_lists.json'):
            raise ValueError('Unexpected runtime source type')
        files[relative] = path.read_bytes()
    return files


def build(output, root=ROOT):
    files = sources(root)
    manifest = {'format': 1, 'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}}
    encoded = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    output = Path(output).expanduser().absolute()
    if output.is_symlink():
        raise ValueError('Unsafe bundle output')
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for name, raw in {**files, 'runtime-manifest.json': encoded}.items():
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.uid, info.gid, info.mtime = len(raw), 0o644, 0, 0, 0
                archive.addfile(info, io.BytesIO(raw))
    return {'archive': str(output), 'files': len(files), 'sourceHash': hashlib.sha256(encoded).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build(args.output), ensure_ascii=False))
    except (OSError, ValueError) as error:
        parser.exit(2, 'Bundle stopped: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
