#!/usr/bin/env python3
"""Resume the single reviewed scanner downloader fix without changing any policy.

Only the pinned old/new scanner source pair is supported. The nftables table,
systemd units, scanner list and all other executable sources are preserved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import node_guard as g


OLD_SCANNER_HASH = '02514a4f5d7d61fb060d97aa8fd6c1c2b99b81c6116b6779605d17b5a7dcddd2'
NEW_SCANNER_HASH = '981de4b974358cf96f69ea0ab8ccf4961a70740c1465d2abbb671ea2156cbbba'
UPGRADE = 'scanner-download-v1'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw, name):
    g.require(raw is not None, name + ' is missing')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise g.GuardError(name + ' is invalid') from None
    g.require(isinstance(value, dict) and raw == g.encode(value), name + ' is invalid')
    return value


class Upgrade:
    def __init__(self, deployment, *, root=Path('/'), run=g.command, source_dir=None, manager=None):
        self.manager = manager or g.NodeGuard(deployment, root=root, run=run, source_dir=source_dir)
        g.require(self.manager.deployment == deployment, 'Upgrade manager belongs to another deployment')
        self.id, self.owner = deployment, self.manager.owner
        self.directory = self.manager.directory / UPGRADE

    def kernel_policy(self, state):
        actual = self.manager.table()
        policies = [state['current']]
        if state['pending'] is not None:
            policies.append(state['pending']['after'])
        expected = [g.normalize(g.policy_objects(policy)) for policy in policies if policy is not None]
        g.require(actual is not None and actual in expected,
                  'Existing node-guard kernel policy is not confirmed; code was not upgraded')
        return actual

    def units(self):
        for path, raw in self.manager.persistence_files().items():
            g.require(g.read_file(path, owner=self.owner, modes=(0o644,)) == raw,
                      'Node-guard systemd persistence differs')
        g.require_directory(self.manager.library, owner=self.owner, mode=0o755)
        g.require(g.read_file(self.manager.library / '.owner', owner=self.owner) == (g.OWNER + '\n').encode(),
                  'Installed node-guard code has another owner')

    def run(self):
        g.safe_path(self.manager.directory)
        if not self.manager.directory.exists():
            return {'deployment': self.id, 'upgraded': False, 'reason': 'not-installed'}
        self.manager.ownership()
        g.require_directory(self.manager.directory, owner=self.owner, mode=0o700)
        with g.operation_lock(self.manager.directory, self.owner):
            state = self.manager.state()
            self.units()
            source = self.manager.source_files()
            g.require(digest(source['scanner_guard.py']) == NEW_SCANNER_HASH,
                      'Staged scanner fix differs from the reviewed source')
            hashes = state['codeHashes']
            scanner_hash = hashes['scanner_guard.py']
            g.require(scanner_hash in (OLD_SCANNER_HASH, NEW_SCANNER_HASH),
                      'Saved scanner source is not a supported upgrade version')
            installed = {}
            for name in g.CODE:
                raw = g.read_file(self.manager.library / name, owner=self.owner, modes=(0o644,))
                g.require(raw is not None, 'Installed node-guard source is missing')
                installed[name] = digest(raw)
                if name != 'scanner_guard.py':
                    g.require(installed[name] == hashes[name] == digest(source[name]),
                              'Another node-guard source changed; this narrow upgrade cannot replace it')
            g.require(installed['scanner_guard.py'] in (OLD_SCANNER_HASH, NEW_SCANNER_HASH),
                      'Installed scanner source is not a supported upgrade version')
            # A current source needs no upgrade. The following ordinary guard
            # install may be recovering a missing table after reboot/failed first
            # apply, so code-only no-ops must not prevent that recovery.
            changed = installed['scanner_guard.py'] != NEW_SCANNER_HASH or scanner_hash != NEW_SCANNER_HASH
            before_kernel = self.kernel_policy(state) if changed else None
            g.safe_path(self.directory)
            if not self.directory.exists() and scanner_hash == NEW_SCANNER_HASH:
                g.require(installed['scanner_guard.py'] == NEW_SCANNER_HASH,
                          'Unjournaled scanner source mismatch')
                self.manager.verify_persistence(state)
                return {'deployment': self.id, 'upgraded': False, 'reason': 'already-current',
                        'scannerHash': NEW_SCANNER_HASH, 'kernelPolicyUnchanged': True}
            before_hashes = {**hashes, 'scanner_guard.py': OLD_SCANNER_HASH}
            after_hashes = {**hashes, 'scanner_guard.py': NEW_SCANNER_HASH}
            expected = {'version': 1, 'upgrade': UPGRADE, 'deployment': self.id,
                        'beforeHashes': before_hashes, 'afterHashes': after_hashes,
                        'oldScannerHash': OLD_SCANNER_HASH, 'newScannerHash': NEW_SCANNER_HASH}
            g.make_owned_directory(self.directory, owner=self.owner, mode=0o700,
                                   marker=UPGRADE + ':' + self.id + '\n')
            intent_path = self.directory / 'intent.json'
            intent_raw = g.read_file(intent_path, owner=self.owner)
            if intent_raw is None:
                g.require(scanner_hash == OLD_SCANNER_HASH and installed['scanner_guard.py'] == OLD_SCANNER_HASH,
                          'Scanner mismatch has no owned upgrade intent')
                g.atomic_write(intent_path, g.encode(expected), owner=self.owner)
                intent_raw = g.encode(expected)
            intent = decode(intent_raw, 'Scanner upgrade intent')
            g.require(intent == expected, 'Scanner upgrade intent differs; resume the same reviewed pair')
            receipt_path = self.directory / 'complete.json'
            receipt = {'version': 1, 'deployment': self.id, 'upgrade': UPGRADE,
                       'intentHash': digest(intent_raw), 'scannerHash': NEW_SCANNER_HASH,
                       'kernelPolicyUnchanged': True}
            complete_raw = g.read_file(receipt_path, owner=self.owner)
            if complete_raw is not None:
                g.require(decode(complete_raw, 'Scanner upgrade receipt') == receipt,
                          'Scanner upgrade completion receipt differs')
                g.require(scanner_hash == NEW_SCANNER_HASH and installed['scanner_guard.py'] == NEW_SCANNER_HASH,
                          'Completed scanner upgrade no longer matches installed code')
            if installed['scanner_guard.py'] != NEW_SCANNER_HASH:
                path = self.manager.library / 'scanner_guard.py'
                old_raw = g.read_file(path, owner=self.owner, modes=(0o644,))
                g.require(digest(old_raw) == OLD_SCANNER_HASH, 'Scanner source changed during upgrade')
                g.atomic_write(path, source['scanner_guard.py'], owner=self.owner, mode=0o644, before=old_raw)
            if scanner_hash != NEW_SCANNER_HASH:
                updated = {**state, 'codeHashes': after_hashes}
                self.manager.save_state(updated, state)
                state = updated
            self.manager.verify_persistence(state)
            if changed:
                g.require(self.manager.table() == before_kernel, 'Kernel policy changed during scanner source upgrade')
            if complete_raw is None:
                g.atomic_write(receipt_path, g.encode(receipt), owner=self.owner)
            return {'deployment': self.id, 'upgraded': changed, 'scannerHash': NEW_SCANNER_HASH,
                    'kernelPolicyUnchanged': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment', required=True)
    args = parser.parse_args(argv)
    g.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run the owned upgrade on the Linux VPS as root')
    print(json.dumps(Upgrade(args.deployment).run(), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('Node-guard code upgrade stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
