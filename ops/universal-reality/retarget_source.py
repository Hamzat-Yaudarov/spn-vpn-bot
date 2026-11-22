#!/usr/bin/env python3
"""Retarget one owned REALITY guest's preferred IPv4 without restarting it.

The journal is forward-only: after any partial write, repeat the same operation.
The existing boot helper and its service identity are deliberately retained.
This helper never assigns/deletes addresses, changes DNS or restarts services.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import tempfile
import sys

import preferred_source as source


def public_ipv4(value):
    result = source.ipv4(value)
    address = ipaddress.IPv4Address(result)
    source.require(address.is_global and not address.is_multicast, 'Choose an assigned public IPv4')
    return result


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replace_owned(path, old, new, *, mode=0o600):
    """Compare-and-swap an owned file; an already completed swap is resumable."""
    actual = source.read_file(path, mode=mode)
    source.require(actual in (old, new), 'IP move file changed outside this operation')
    if actual == new:
        return
    fd, temporary = tempfile.mkstemp(prefix='.ip-move-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(new)
            stream.flush()
            os.fsync(stream.fileno())
        source.require(source.read_file(path, mode=mode) == old,
                       'IP move file changed during compare-and-swap')
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


class RetargetSource:
    def __init__(self, target, old_ip, state_dir, operation, *, deployment=None,
                 root=Path('/'), run=source.command, probe=source.https_probe):
        self.target, self.old = public_ipv4(target), public_ipv4(old_ip)
        source.require(self.target != self.old, 'Old and new IPv4 must differ')
        source.require(isinstance(operation, str) and re.fullmatch(r'(?:[0-9]{6}|[0-9a-f]{32}|[0-9a-f]{64})', operation),
                       'IP move operation must be a six-digit generation or hexadecimal operation ID')
        self.root, self.operation = Path(root), operation
        self.manager = source.PreferredSource(self.target, state_dir, root=self.root, run=run, probe=probe)
        self.old_manager = source.PreferredSource(self.old, state_dir, root=self.root, run=run, probe=probe)
        self.state, self.id = self.manager.state, self.manager.id
        source.require(deployment in (None, self.id), 'IP move deployment differs from state directory')
        self.directory, self.unit = self.manager.directory, self.manager.unit
        self.journals = self.state / '.ip-moves'
        self.journal = self.journals / operation
        self.snapshot = self.journal / 'snapshot.json'
        self.run, self.probe = run, probe

    def ownership(self):
        source.safe_directory(self.directory)
        node = self.root / 'opt/spn-reality-node'
        source.safe_directory(node)
        source.require(source.read_file(node / '.spn-deployment') == (self.id + '\n').encode(),
                       'Installed node belongs to another deployment')
        source.require(self.unit.parent.is_dir()
                       and not any(p.is_symlink() for p in (self.unit.parent, *self.unit.parent.parents)),
                       'Unsafe systemd unit directory')

    def plan(self, raw, target):
        try:
            plan = json.loads(raw)
            source.require(isinstance(plan, dict) and set(plan) == {
                'version', 'deployment', 'target', 'before', 'after', 'beforeSource', 'helperHash'},
                'Unsupported saved preferred-source plan')
            source.require(plan['version'] == 1 and plan['deployment'] == self.id and plan['target'] == target,
                           'Saved source plan belongs to another target')
            before = plan['before']
            source.route_args(before, before['dev'], before.get('prefsrc'))
            source.require(plan['after'] == {**before, 'prefsrc': target}, 'Saved source transition differs')
            source.ipv4(plan['beforeSource'])
            source.require(re.fullmatch(r'[0-9a-f]{64}', plan['helperHash']) is not None,
                           'Invalid saved helper digest')
            return plan
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
            raise source.SourceError('Invalid saved preferred-source plan') from None

    def check_helper(self, plan):
        raw = source.read_file(self.directory / 'source.py')
        source.require(raw is not None and hashlib.sha256(raw).hexdigest() == plan['helperHash'],
                       'Persistent preferred-source helper differs')
        return raw

    @staticmethod
    def without_source(route):
        return {key: value for key, value in route.items() if key != 'prefsrc'}

    def validate_route(self, route, old_plan):
        source.require(self.without_source(route) == self.without_source(old_plan['after'])
                       and route.get('prefsrc') in (None, self.old, self.target),
                       'Default route changed outside this IP move')

    def preflight_pair(self, raw):
        try:
            fields = raw.decode('ascii').rstrip('\n').split('|')
            source.require(len(fields) == 4 and raw.endswith(b'\n') and raw.count(b'\n') == 1
                           and fields[0] == self.old and fields[3] == self.id,
                           'Saved preflight belongs to another IP or deployment')
            source.ipv4(fields[1])
            source.require(re.fullmatch('[1-9][0-9]{0,4}', fields[2]) is not None
                           and 1 <= int(fields[2]) <= 65535, 'Invalid saved preflight SSH port')
            fields[0] = self.target
            return ('|'.join(fields) + '\n').encode()
        except (AttributeError, UnicodeDecodeError):
            raise source.SourceError('Invalid saved preflight marker') from None

    def ensure_journal(self):
        for path in (self.journals, self.journal):
            if not path.exists() and not path.is_symlink():
                path.mkdir(mode=0o700)
                fsync_directory(path.parent)
            source.safe_directory(path)

    def create_snapshot(self):
        raw = source.read_file(self.state / source.PLAN)
        source.require(raw is not None and source.read_file(self.directory / 'plan.json') == raw,
                       'Stage and persistent source plans must match before an IP move')
        old_plan = self.plan(raw, self.old)
        self.check_helper(old_plan)
        old_unit = source.read_file(self.unit, mode=0o644)
        source.require(old_unit == self.old_manager.unit_bytes(), 'Old preferred-source service differs')
        old_preflight = source.read_file(self.state / 'preflight-ok')
        new_preflight = self.preflight_pair(old_preflight)
        route, selected = self.manager.inspect()
        self.validate_route(route, old_plan)
        # This immutable snapshot retains the actual pre-move route and the
        # complete prior plan; later rotations never discard earlier history.
        new_plan = {**old_plan, 'target': self.target, 'before': route,
                    'after': {**route, 'prefsrc': self.target}, 'beforeSource': selected}
        value = {'version': 1, 'deployment': self.id, 'operation': self.operation,
                 'oldIp': self.old, 'newIp': self.target, 'route': route, 'source': selected,
                 'oldPlan': raw.decode('utf-8'), 'newPlan': source.json_bytes(new_plan).decode('utf-8'),
                 'oldUnit': old_unit.decode('ascii'), 'newUnit': self.manager.unit_bytes().decode('ascii'),
                 'oldPreflight': old_preflight.decode('ascii'), 'newPreflight': new_preflight.decode('ascii')}
        source.owned_write(self.snapshot, source.json_bytes(value))
        return value

    def load_snapshot(self):
        raw = source.read_file(self.snapshot)
        if raw is None:
            return self.create_snapshot()
        try:
            value = json.loads(raw)
            source.require(isinstance(value, dict) and set(value) == {
                'version', 'deployment', 'operation', 'oldIp', 'newIp', 'route', 'source',
                'oldPlan', 'newPlan', 'oldUnit', 'newUnit', 'oldPreflight', 'newPreflight'},
                'Invalid IP move journal fields')
            source.require(value['version'] == 1 and value['deployment'] == self.id
                           and value['operation'] == self.operation and value['oldIp'] == self.old
                           and value['newIp'] == self.target, 'IP move journal belongs to another operation')
            old_plan = self.plan(value['oldPlan'].encode(), self.old)
            new_plan = self.plan(value['newPlan'].encode(), self.target)
            self.validate_route(value['route'], old_plan)
            source.require(new_plan == {**old_plan, 'target': self.target, 'before': value['route'],
                'after': {**value['route'], 'prefsrc': self.target}, 'beforeSource': value['source']},
                'IP move journal plan differs')
            source.require(value['oldUnit'].encode() == self.old_manager.unit_bytes()
                           and value['newUnit'].encode() == self.manager.unit_bytes()
                           and value['newPreflight'].encode() == self.preflight_pair(value['oldPreflight'].encode()),
                           'IP move journal service or preflight differs')
            return value
        except (KeyError, TypeError, AttributeError, json.JSONDecodeError, UnicodeDecodeError):
            raise source.SourceError('Invalid IP move journal') from None

    def file_pairs(self, snapshot):
        return [(self.directory / 'plan.json', snapshot['oldPlan'].encode(), snapshot['newPlan'].encode(), 0o600),
                (self.state / source.PLAN, snapshot['oldPlan'].encode(), snapshot['newPlan'].encode(), 0o600),
                (self.unit, snapshot['oldUnit'].encode(), snapshot['newUnit'].encode(), 0o644),
                (self.state / 'preflight-ok', snapshot['oldPreflight'].encode(), snapshot['newPreflight'].encode(), 0o600)]

    def apply_locked(self):
        self.ownership()
        self.ensure_journal()
        snapshot = self.load_snapshot()
        old_plan = self.plan(snapshot['oldPlan'].encode(), self.old)
        self.check_helper(old_plan)
        pairs = self.file_pairs(snapshot)
        # Check every file before the first change, including on recovery.
        for path, old, new, mode in pairs:
            source.require(source.read_file(path, mode=mode) in (old, new),
                           'IP move file changed outside this operation')
        route, _ = self.manager.inspect()
        self.validate_route(route, old_plan)
        self.probe(self.target, bind=True)
        self.manager.rules()
        source.require(self.manager.default() == route, 'Default route changed during IP move precheck')
        # Persistent plan first: even after a crash, a boot process using stale
        # --ip OLD cannot validate its plan and restore the old preferred source.
        # Both the stage and boot-helper locks remain held through verification.
        for path, old, new, mode in pairs:
            replace_owned(path, old, new, mode=mode)
        self.run('systemctl', 'daemon-reload')
        desired = self.plan(snapshot['newPlan'].encode(), self.target)['after']
        source.require(self.manager.default() == route, 'Default route changed while saving IP move persistence')
        if route != desired:
            self.run('ip', '-4', 'route', 'change', *source.route_args(route, route['dev'], self.target))
        self.run('systemctl', 'enable', self.manager.unit_name)
        result = self.manager.verify()
        source.require(source.read_file(self.state / 'preflight-ok') == snapshot['newPreflight'].encode(),
                       'Moved preflight marker differs')
        result.update({'oldIp': self.old, 'operation': self.operation, 'networkRestarted': False,
                       'vpnRestarted': False, 'helperUnchanged': True})
        source.owned_write(self.journal / 'verified.json', source.json_bytes(result))
        return result

    def apply(self):
        # The original helper locks its own --state-dir. Acquire BOTH its
        # installer and persistent locations, not a separate move-only lock.
        self.ownership()
        with contextlib.ExitStack() as locks:
            for state in sorted((self.state, self.directory), key=str):
                locks.enter_context(source.operation_lock(state))
            return self.apply_locked()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ip', required=True)
    parser.add_argument('--old-ip', required=True)
    parser.add_argument('--state-dir', required=True, type=Path)
    parser.add_argument('--operation', required=True)
    parser.add_argument('--deployment')
    args = parser.parse_args(argv)
    source.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux VPS as root')
    manager = RetargetSource(args.ip, args.old_ip, args.state_dir, args.operation, deployment=args.deployment)
    print(json.dumps(manager.apply(), sort_keys=True))
    return 0


if __name__ == '__main__':
    def interrupted(number, frame):
        raise InterruptedError('IP move interrupted; repeat the same operation')

    for signum in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(signum, interrupted)
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyboardInterrupt) as error:
        print('Stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
