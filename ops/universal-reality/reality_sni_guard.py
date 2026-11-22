#!/usr/bin/env python3
"""Constrain an owned REALITY TCP front to its configured SNI without TLS termination.

The original HAProxy guard intent and template remain immutable. This separate,
forward-only upgrade can be resumed after a connection loss or failed reload.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import sys
import tempfile

import guard


FORMAT = 1
ANCHOR = (b'    acl complete_client_hello req.ssl_hello_type 1\n'
          b'    tcp-request content accept if complete_client_hello\n')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def hostname(value):
    guard.need(isinstance(value, str) and value == value.strip(), 'Expected a plain SNI hostname')
    try:
        result = value.encode('idna').decode('ascii').lower()
    except (UnicodeError, ValueError):
        raise guard.GuardError('Expected a valid SNI hostname') from None
    guard.need(re.fullmatch(r'(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+'
                           r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', result) is not None,
               'Expected a valid SNI hostname')
    try:
        ipaddress.ip_address(result)
    except ValueError:
        return result
    raise guard.GuardError('SNI must be a hostname, not an IP address')


def rendered(base, sni):
    sni = hostname(sni)
    guard.need(base.count(ANCHOR) == 1 and b'spn_allowed_sni' not in base,
               'Original HAProxy guard template differs')
    replacement = (b'    acl complete_client_hello req.ssl_hello_type 1\n'
                   b'    acl spn_allowed_sni req.ssl_sni -i ' + sni.encode() + b'\n'
                   b'    tcp-request content reject if complete_client_hello !spn_allowed_sni\n'
                   b'    tcp-request content accept if complete_client_hello spn_allowed_sni\n')
    return base.replace(ANCHOR, replacement)


def safe_directory(path):
    guard.need(path.is_dir() and not path.is_symlink(), 'Unsafe SNI upgrade directory')
    info = path.stat()
    guard.need(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
               'Unsafe SNI upgrade directory permissions')


def decoded(raw, label):
    guard.need(raw is not None, label + ' is missing')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise guard.GuardError(label + ' is invalid') from None
    guard.need(isinstance(value, dict), label + ' is invalid')
    return value


def base_ownership(state, deployment, base):
    """Tie the upgrade to the original install, never to a marker alone."""
    safe_directory(state)
    intent = decoded(guard.read_owned(state / 'intent.json'), 'Original guard intent')
    before = guard.read_owned(state / 'before.cfg')
    ready = decoded(guard.read_owned(state / 'ready.json'), 'Original guard readiness receipt')
    guard.need(intent.get('deployment') == deployment and before is not None
               and intent.get('beforeHash') == digest(before)
               and intent.get('afterHash') == digest(base)
               and intent.get('beforeMode') in guard.CONFIG_MODES
               and ready.get('guardInstalled') is True and ready.get('partialHelloRejected') is True,
               'Original guard ownership proof differs')


def ready_value(deployment, intent_raw, intent):
    return {'format': FORMAT, 'deployment': deployment, 'intentHash': digest(intent_raw),
            'configHash': intent['desiredHash'], 'sni': intent['sni'], 'sniFiltered': True,
            'validSniHandshakeVerified': True, 'unknownSniRejected': True,
            'missingSniRejected': True, 'tlsTerminated': False}


def upgrade_state(state, deployment, base, *, require_ready=True):
    """Return an upgrade only when all its private, owned proof files agree."""
    base_ownership(state, deployment, base)
    directory = state / 'sni-v1'
    safe_directory(directory)
    intent_raw = guard.read_owned(directory / 'intent.json')
    intent = decoded(intent_raw, 'SNI upgrade intent')
    guard.need(set(intent) == {'format', 'deployment', 'sni', 'baseHash', 'desiredHash'}
               and intent['format'] == FORMAT and intent['deployment'] == deployment
               and hostname(intent['sni']) == intent['sni']
               and intent['baseHash'] == digest(base), 'SNI upgrade intent differs')
    before = guard.read_owned(directory / 'before.cfg')
    desired = guard.read_owned(directory / 'desired.cfg')
    guard.need(before == base and desired == rendered(base, intent['sni'])
               and intent['desiredHash'] == digest(desired), 'SNI upgrade configuration proof differs')
    ready_raw = guard.read_owned(directory / 'ready.json')
    ready = None if ready_raw is None else decoded(ready_raw, 'SNI upgrade readiness receipt')
    guard.need(ready is None or ready == ready_value(deployment, intent_raw, intent),
               'SNI upgrade readiness receipt differs')
    guard.need(not require_ready or ready is not None,
               'SNI upgrade is unfinished; repeat reality_sni_guard.py install with the same SNI')
    return {'intent': intent, 'intent_raw': intent_raw, 'desired': desired, 'ready': ready}


def client_hello(sni):
    """Generate a real OpenSSL ClientHello, including the no-SNI case."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    connection = context.wrap_bio(incoming, outgoing, server_hostname=sni)
    try:
        connection.do_handshake()
    except ssl.SSLWantReadError:
        pass
    result = outgoing.read()
    guard.need(bool(result), 'Could not construct TLS ClientHello')
    return result


def probe_sni(sni, *, address='127.0.0.1', port=443, timeout=7):
    """Prove matching SNI reaches the real fallback and other hellos close."""
    try:
        context = ssl.create_default_context()
        with socket.create_connection((address, port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            with context.wrap_socket(connection, server_hostname=sni) as secured:
                guard.need(bool(secured.version()), 'Matching SNI did not complete TLS')
        other_sni = 'invalid-sni.spn.invalid' if sni != 'invalid-sni.spn.invalid' else 'other-sni.spn.invalid'
        for other in (other_sni, None):
            with socket.create_connection((address, port), timeout=timeout) as connection:
                connection.settimeout(timeout)
                connection.sendall(client_hello(other))
                try:
                    response = connection.recv(1)
                except ConnectionResetError:
                    response = b''
                guard.need(response == b'', 'Unknown or missing SNI reached the TLS backend')
    except (OSError, ssl.SSLError):
        raise guard.GuardError('SNI filtering connection proof failed; upgrade remains resumable') from None
    return {'validSniHandshakeVerified': True, 'unknownSniRejected': True, 'missingSniRejected': True}


class SniGuard:
    def __init__(self, ip, deployment, sni, *, root=Path('/'), run=guard.command,
                 probe=None, manager=None):
        self.sni = hostname(sni)
        self.manager = manager or guard.Guard(ip, deployment, root=root, run=run)
        self.id, self.ip = self.manager.id, self.manager.ip
        guard.need(self.id == deployment and self.ip == ip, 'SNI guard manager identity differs')
        self.state, self.directory = self.manager.state, self.manager.state / 'sni-v1'
        self.run, self.probe = self.manager.run, probe or probe_sni

    def inspect(self, *, require_ready=True):
        self.manager.ownership()
        value = upgrade_state(self.state, self.id, self.manager.rendered(), require_ready=require_ready)
        guard.need(value['intent']['sni'] == self.sni,
                   'SNI upgrade already belongs to another hostname; repeat the same SNI')
        return value

    def install(self):
        self.manager.ownership()
        base = self.manager.rendered()
        base_ownership(self.state, self.id, base)
        self.manager.backend()
        desired = rendered(base, self.sni)
        current = guard.read_owned(self.manager.config, modes=guard.CONFIG_MODES)
        guard.need(current in (base, desired), 'HAProxy configuration changed outside the owned SNI upgrade')
        # Syntax check precedes the durable upgrade intent and the live change.
        with tempfile.TemporaryDirectory(prefix='spn-sni-check-', dir=self.manager.node) as temporary:
            candidate = Path(temporary) / 'haproxy.cfg'
            candidate.write_bytes(desired)
            candidate.chmod(0o600)
            self.run('haproxy', '-c', '-f', str(candidate))
        self.directory.mkdir(mode=0o700, exist_ok=True)
        safe_directory(self.directory)
        raw = guard.read_owned(self.directory / 'intent.json')
        if raw is None:
            for name, value in (('before.cfg', base), ('desired.cfg', desired)):
                old = guard.read_owned(self.directory / name)
                guard.need(old in (None, value), 'Unfinished SNI upgrade contains another configuration')
                if old is None:
                    guard.replace_owned(self.directory / name, value, expected=None, mode=0o600)
            intent = {'format': FORMAT, 'deployment': self.id, 'sni': self.sni,
                      'baseHash': digest(base), 'desiredHash': digest(desired)}
            guard.replace_owned(self.directory / 'intent.json', json_bytes(intent), expected=None, mode=0o600)
        value = self.inspect(require_ready=False)
        changed = current != desired
        if changed:
            guard.replace_owned(self.manager.config, desired, expected=current, mode=0o644,
                                expected_modes=guard.CONFIG_MODES)
        # Forward-only: a failed reload/probe keeps this exact intent, backup and
        # candidate. Never silently restore the former permissive SNI policy.
        if changed or value['ready'] is None:
            active = self.run('systemctl', 'is-active', 'haproxy', allowed=(0, 3, 4)) == 'active'
            self.run('systemctl', 'reload' if active else 'start', 'haproxy')
        result = self.manager.verify(allow_pending_sni=True)
        proof = self.probe(self.sni)
        guard.need(proof == {'validSniHandshakeVerified': True, 'unknownSniRejected': True,
                             'missingSniRejected': True}, 'SNI filtering proof differs')
        ready = ready_value(self.id, value['intent_raw'], value['intent'])
        if value['ready'] is None:
            guard.replace_owned(self.directory / 'ready.json', json_bytes(ready), expected=None, mode=0o600)
        self.inspect()
        return {**result, **proof, 'sniFiltered': True, 'allowedSni': self.sni, 'tlsTerminated': False}

    def verify(self):
        self.inspect()
        result = self.manager.verify()
        proof = self.probe(self.sni)
        guard.need(proof == {'validSniHandshakeVerified': True, 'unknownSniRejected': True,
                             'missingSniRejected': True}, 'SNI filtering proof differs')
        return {**result, **proof, 'sniFiltered': True, 'allowedSni': self.sni, 'tlsTerminated': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'verify'))
    parser.add_argument('--ip', required=True)
    parser.add_argument('--deployment', required=True)
    parser.add_argument('--sni', required=True)
    args = parser.parse_args(argv)
    guard.need(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux VPS as root')
    print(json.dumps(getattr(SniGuard(args.ip, args.deployment, args.sni), args.action)(), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print('Stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
