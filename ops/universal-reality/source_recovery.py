#!/usr/bin/env python3
"""Explicit, journaled recovery of one reviewed boot-to-static default route.

The original preferred_source helper, unit and prior plan are retained. Only a
route whose exact digest and machine identity were reviewed can become the new
baseline; this is deliberately not part of automatic deployment preflight.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import tempfile

import preferred_source as source


OPERATION = 'networkd-static-v1'


def route_digest(route):
    return hashlib.sha256(json.dumps(route, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def replace_owned(path, old, new):
    actual = source.read_file(path)
    source.require(actual in (old, new), 'Recovery plan changed outside this operation')
    if actual == new:
        return
    descriptor, temporary = tempfile.mkstemp(prefix='.source-recovery-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(new)
            stream.flush()
            os.fsync(stream.fileno())
        source.require(source.read_file(path) == old, 'Recovery plan changed during compare-and-swap')
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


class SourceRecovery:
    def __init__(self, target, state_dir, machine_id, route_sha256, *, root=Path('/'),
                 run=source.command, probe=source.https_probe, helper=None):
        source.require(isinstance(machine_id, str) and re.fullmatch(r'[0-9a-f]{32}', machine_id),
                       'Expected machine ID is required')
        source.require(isinstance(route_sha256, str) and re.fullmatch(r'[0-9a-f]{64}', route_sha256),
                       'Expected reviewed route digest is required')
        self.manager = source.PreferredSource(target, state_dir, root=root, run=run, probe=probe,
                                              helper=helper or Path(state_dir) / 'preferred_source.py')
        self.root, self.machine_id, self.route_sha256 = Path(root), machine_id, route_sha256
        self.target, self.id = self.manager.target, self.manager.id
        self.journal = self.manager.directory / ('recovery-' + OPERATION)
        self.snapshot_path = self.journal / 'snapshot.json'

    def _machine(self):
        path = self.root / 'etc/machine-id'
        source.require(not any(p.is_symlink() for p in (path, *path.parents)), 'Unsafe machine ID path')
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            info = os.fstat(stream.fileno())
            source.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                           and stat.S_IMODE(info.st_mode) in (0o444, 0o644) and info.st_nlink == 1,
                           'Unsafe machine ID file')
            raw = stream.read(129)
        source.require(raw in (self.machine_id.encode(), (self.machine_id + '\n').encode()),
                       'VPS machine identity differs; recovery refused')

    def _ownership(self):
        self._machine()
        source.safe_directory(self.manager.directory)
        node = self.root / 'opt/spn-reality-node'
        source.safe_directory(node)
        source.require(source.read_file(node / '.spn-deployment') == (self.id + '\n').encode(),
                       'Installed node belongs to another deployment')
        source.require(not any(p.is_symlink() for p in (self.manager.unit.parent, *self.manager.unit.parent.parents)),
                       'Unsafe unit directory')
        if self.journal.exists() or self.journal.is_symlink():
            source.safe_directory(self.journal)

    def _plan(self, raw):
        try:
            value = json.loads(raw)
            source.require(isinstance(value, dict) and set(value) == {
                'version', 'deployment', 'target', 'before', 'after', 'beforeSource', 'helperHash'},
                'Unsupported preferred-source plan')
            source.require(type(value['version']) is int and value['version'] == 1
                           and value['deployment'] == self.id and value['target'] == self.target,
                           'Preferred-source plan belongs to another deployment or IP')
            before = value['before']
            source.route_args(before, before['dev'], before.get('prefsrc'))
            source.require(value['after'] == {**before, 'prefsrc': self.target}, 'Saved transition differs')
            source.require(value['beforeSource'] == self.target,
                           'Recovery supports only an already selected original target IPv4')
            source.require(isinstance(value['helperHash'], str)
                           and re.fullmatch(r'[0-9a-f]{64}', value['helperHash']), 'Invalid helper digest')
            return value
        except (KeyError, TypeError, ValueError, UnicodeDecodeError):
            raise source.SourceError('Invalid or unsupported preferred-source plan') from None

    def _transition(self, old, observed):
        before = old['before']
        source.require(isinstance(observed, dict), 'Invalid reviewed route snapshot')
        source.require(before.get('protocol', 'boot') == 'boot' and observed.get('protocol') == 'static',
                       'Only an explicitly reviewed boot-to-static route is recoverable')
        remove = lambda route: {key: value for key, value in route.items() if key not in ('protocol', 'prefsrc')}
        source.require(remove(before) == remove(observed)
                       and before.get('prefsrc') in (None, self.target)
                       and observed.get('prefsrc') in (None, self.target),
                       'Gateway, interface, metric, flags or source changed; recovery refused')
        source.route_args(observed, observed['dev'], observed.get('prefsrc'))
        source.require(route_digest(observed) == self.route_sha256, 'Current route is not the reviewed snapshot')
        return {**old, 'before': observed, 'after': {**observed, 'prefsrc': self.target}}

    def _helpers(self, plan):
        for path in (self.manager.helper, self.manager.directory / 'source.py'):
            raw = source.read_file(path)
            source.require(raw is not None and hashlib.sha256(raw).hexdigest() == plan['helperHash'],
                           'Preferred-source helper changed; recovery refused')

    def _marker(self, snapshot):
        return {'version': 1, 'snapshotSha256': hashlib.sha256(source.json_bytes(snapshot)).hexdigest()}

    def _read_marker(self, name, expected):
        raw = source.read_file(self.journal / name)
        source.require(raw is None or raw == source.json_bytes(expected), 'Recovery marker differs')
        return raw is not None

    def _load(self):
        self._ownership()
        stage = source.read_file(self.manager.state / source.PLAN)
        persistent = source.read_file(self.manager.directory / 'plan.json')
        snapshot_raw = source.read_file(self.snapshot_path) if self.journal.exists() else None
        route, selected = self.manager.inspect()
        source.require(selected == self.target, 'Selected IPv4 changed; recovery refused')
        if snapshot_raw is None:
            source.require(stage is not None and stage == persistent, 'Stage and persistent plans must match')
            old = self._plan(stage)
            new = self._transition(old, route)
            snapshot = {'version': 1, 'operation': OPERATION, 'deployment': self.id,
                        'machineId': self.machine_id, 'target': self.target,
                        'routeSha256': self.route_sha256, 'observedRoute': route,
                        'oldPlan': stage.decode(), 'newPlan': new}
            pending = applying = complete = False
            if self.journal.exists():
                source.require(all(source.read_file(self.journal / name) is None
                                   for name in ('pending.json', 'applying.json', 'receipt.json')),
                               'Recovery markers exist without their snapshot')
        else:
            try:
                snapshot = json.loads(snapshot_raw)
                source.require(isinstance(snapshot, dict) and set(snapshot) == {
                    'version', 'operation', 'deployment', 'machineId', 'target', 'routeSha256',
                    'observedRoute', 'oldPlan', 'newPlan'}, 'Invalid recovery snapshot')
                source.require(type(snapshot['version']) is int and snapshot['version'] == 1
                               and snapshot['operation'] == OPERATION and snapshot['deployment'] == self.id
                               and snapshot['machineId'] == self.machine_id and snapshot['target'] == self.target
                               and snapshot['routeSha256'] == self.route_sha256,
                               'Recovery snapshot belongs to another request')
                source.require(isinstance(snapshot['oldPlan'], str), 'Invalid original plan snapshot')
                old = self._plan(snapshot['oldPlan'].encode())
                new = self._transition(old, snapshot['observedRoute'])
                source.require(snapshot['newPlan'] == new, 'Recovery transition was changed')
            except (KeyError, TypeError, ValueError, UnicodeDecodeError):
                raise source.SourceError('Invalid recovery snapshot') from None
            marker = self._marker(snapshot)
            pending = self._read_marker('pending.json', marker)
            applying = self._read_marker('applying.json', marker)
            receipt = source.read_file(self.journal / 'receipt.json')
            complete = receipt is not None
            source.require(not applying or pending, 'Apply marker has no pending recovery')
            source.require(not complete or applying, 'Receipt has no apply marker')
            old_raw, new_raw = snapshot['oldPlan'].encode(), source.json_bytes(new)
            source.require(stage in (old_raw, new_raw) and persistent in (old_raw, new_raw),
                           'Recovery plans changed outside this operation')
            source.require(pending or (stage == persistent == old_raw), 'Unjournaled recovery plan write')
            source.require(not applying or stage == persistent == new_raw, 'Apply marker precedes plan persistence')
            source.require(route in (snapshot['observedRoute'], new['after']),
                           'Default route changed outside this recovery')
            if complete:
                source.require(receipt == source.json_bytes(self._receipt(snapshot)), 'Recovery receipt differs')
        self._helpers(old)
        unit = source.read_file(self.manager.unit, mode=0o644)
        source.require(unit == self.manager.unit_bytes() or (unit is None and applying and not complete),
                       'Preferred-source unit changed; recovery refused')
        return snapshot, pending, applying, complete

    def _receipt(self, snapshot):
        return {**self._marker(snapshot), 'deployment': self.id, 'machineId': self.machine_id,
                'ip': self.target, 'routeSha256': self.route_sha256,
                'preferredSourceVerified': True, 'bootPersistenceVerified': True, 'networkRestarted': False}

    def check(self):
        snapshot, _, _, complete = self._load()
        return {'planOnly': True, 'ip': self.target, 'deployment': self.id, 'machineId': self.machine_id,
                'routeSha256': self.route_sha256, 'recovery': OPERATION,
                'completed': complete, 'changes': False}

    def apply(self):
        self._ownership()
        with contextlib.ExitStack() as stack:
            stack.enter_context(source.operation_lock(self.manager.state))
            stack.enter_context(source.operation_lock(self.manager.directory))
            snapshot, _, _, complete = self._load()
            if complete:
                self.manager.verify()
                return self._receipt(snapshot)
            self.manager.probe(self.target, bind=True)
            self.manager.probe(self.target)
            # Re-read all ownership, route and journal fields after HTTPS checks.
            source.require(self._load()[0] == snapshot, 'Recovery changed during HTTPS preflight')
            if not self.journal.exists():
                self.journal.mkdir(mode=0o700)
                sync_directory(self.journal.parent)
            source.safe_directory(self.journal)
            source.owned_write(self.snapshot_path, source.json_bytes(snapshot))
            marker = self._marker(snapshot)
            source.owned_write(self.journal / 'pending.json', source.json_bytes(marker))
            self._load()
            old_raw, new_raw = snapshot['oldPlan'].encode(), source.json_bytes(snapshot['newPlan'])
            replace_owned(self.manager.state / source.PLAN, old_raw, new_raw)
            replace_owned(self.manager.directory / 'plan.json', old_raw, new_raw)
            self._load()
            # This marker also explains an absent unit after the unchanged
            # original helper rolled back its own failed HTTPS/systemd attempt.
            source.owned_write(self.journal / 'applying.json', source.json_bytes(marker))
            self.manager.apply()
            self.manager.verify()
            self._load()
            receipt = self._receipt(snapshot)
            source.owned_write(self.journal / 'receipt.json', source.json_bytes(receipt))
            return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ip', required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--machine-id', required=True)
    parser.add_argument('--route-sha256', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    source.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux VPS as root')
    recovery = SourceRecovery(args.ip, args.state_dir, args.machine_id, args.route_sha256)
    print(json.dumps(recovery.apply() if args.apply else recovery.check(), sort_keys=True))
    return 0


if __name__ == '__main__':
    def interrupted(number, frame):
        raise InterruptedError('Preferred-source recovery interrupted')

    for signum in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(signum, interrupted)
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyboardInterrupt) as error:
        print('Stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
