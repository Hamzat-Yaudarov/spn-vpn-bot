"""Narrow helpers for an owner-approved EU WireGuard UDP-port migration.

No command runs at import. The orchestrator owns SSH, locking, payload routing,
firewall commands and rollback sequencing. Snapshots can contain WG secrets;
never print or persist them outside a private authenticated repair channel.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile


UFW_SOURCES = ('/etc/ufw/ufw.conf', '/etc/ufw/before.rules', '/etc/ufw/after.rules',
               '/etc/ufw/user.rules', '/etc/ufw/before6.rules', '/etc/ufw/after6.rules',
               '/etc/ufw/user6.rules', '/etc/default/ufw')


class RepairError(RuntimeError):
    """Messages contain no file contents, keys or untrusted exception text."""


def need(condition, message):
    if not condition:
        raise RepairError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _port(value):
    return type(value) is int and (value == 443 or 1024 <= value <= 65535) and value not in (2222, 2443)


def _plan(plan):
    try:
        need(type(plan['format']) is int and plan['format'] in (1, 2), 'Invalid repair plan format')
        need(re.fullmatch('[0-9a-f]{32}', plan['id']), 'Invalid repair deployment identity')
        need(re.fullmatch('spnwg[a-z0-9]{1,10}', plan['link']['interface']), 'Invalid repair interface')
        for ip in (plan['ru']['ip'], plan['eu']['ip'], plan['panelIp']):
            address = ipaddress.IPv4Address(ip)
            need(str(address) == ip and address.is_global and not address.is_multicast, 'Invalid repair public address')
        need(len({plan['ru']['ip'], plan['eu']['ip'], plan['panelIp']}) == 3, 'Repair server identities overlap')
        for role in ('ru', 'eu'):
            address = ipaddress.IPv4Address(plan['link'][role])
            need(str(address) == plan['link'][role] and address.is_private, 'Invalid repair tunnel address')
            ssh = plan[role]['sshPort']
            need(type(ssh) is int and 1 <= ssh <= 65535, 'Invalid repair SSH port')
        entry = plan.get('entryPort', 443)
        udp_ru = plan['link'].get('ruPort', plan['link']['port'])
        udp_eu = plan['link']['port']
        need(_port(entry) and _port(udp_ru) and _port(udp_eu), 'Invalid repair UDP or TCP port')
        need(entry != plan['ru']['sshPort'] and udp_ru != plan['ru']['sshPort']
             and udp_eu != plan['eu']['sshPort'], 'Repair port conflicts with SSH')
        return entry, udp_ru, udp_eu
    except (KeyError, TypeError, ValueError):
        raise RepairError('Invalid public repair plan') from None


def eu_parameters(plan):
    """Exact EU .spn-parameters line used by managed-cascade.sh, no newline."""
    entry, ru_udp, eu_udp = _plan(plan)
    link = plan['link']
    fields = ('eu', plan['eu']['ip'], plan['ru']['ip'], plan['panelIp'], plan['eu']['sshPort'],
              plan['id'], link['interface'], link['eu'], link['ru'], eu_udp)
    text = '|'.join(map(str, fields))
    if (entry, ru_udp) != (443, eu_udp):
        text += '|' + str(entry) + '|' + str(ru_udp)
    return text


def ru_preflight(plan):
    """Exact shared-RU preflight line, no newline."""
    entry, ru_udp, eu_udp = _plan(plan)
    link = plan['link']
    fields = ('ru', plan['ru']['ip'], plan['eu']['ip'], plan['panelIp'], plan['ru']['sshPort'],
              plan['id'], link['interface'], link['ru'], link['eu'], eu_udp, entry, ru_udp)
    return '|'.join(map(str, fields))


def update_ru_registry(raw, deployment, old_port, new_port, old_wg, new_wg):
    """Change only target EU UDP metadata and the two owned WG-file hashes."""
    need(isinstance(raw, bytes) and isinstance(old_wg, bytes) and isinstance(new_wg, bytes), 'Invalid repair bytes')
    need(isinstance(deployment, str) and re.fullmatch('[0-9a-f]{32}', deployment), 'Invalid repair deployment identity')
    need(_port(old_port) and _port(new_port) and old_port != new_port, 'Invalid UDP-port migration')
    try:
        state = json.loads(raw)
        need(isinstance(state, dict) and set(state) == {'version', 'branches', 'baseline', 'initialized'}
             and type(state['version']) is int and state['version'] == 1
             and isinstance(state['branches'], list), 'Invalid owned RU registry')
        matches = [b for b in state['branches'] if isinstance(b, dict) and b.get('id') == deployment]
        need(len(matches) == 1, 'Repair branch is missing or ambiguous')
        branch = matches[0]
        need(branch.get('status') == 'active' and branch.get('legacy') is False,
             'Only an active shared-RU branch can migrate its EU UDP port')
        need(branch['euWGPort'] == old_port, 'Owned EU UDP port differs from repair intent')
        need(isinstance(branch['interface'], str) and re.fullmatch('spnwg[a-z0-9]{1,10}', branch['interface']),
             'Invalid owned repair interface')
        address = str(ipaddress.IPv4Address(branch['euIP']))
        old_endpoint = ('Endpoint = ' + address + ':' + str(old_port) + '\n').encode('ascii')
        new_endpoint = ('Endpoint = ' + address + ':' + str(new_port) + '\n').encode('ascii')
        need(old_wg.count(old_endpoint) == 1 and new_wg == old_wg.replace(old_endpoint, new_endpoint, 1),
             'Repair would change more than the RU peer endpoint port')
        files = branch['files']
        paths = ('/opt/spn-cascade-ru/branches/' + deployment + '/wg.conf',
                 '/etc/wireguard/' + branch['interface'] + '.conf')
        need(isinstance(files, dict) and all(files.get(path) == sha256(old_wg) for path in paths),
             'Owned RU WireGuard hashes differ from repair intent')
        branch['euWGPort'] = new_port
        for path in paths:
            files[path] = sha256(new_wg)
        return (json.dumps(state, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise RepairError('Invalid owned RU registry or repair inputs') from None


def _safe_file(path, uid=0, private=True):
    path = Path(path).absolute()
    for parent in path.parents:
        need(not parent.is_symlink(), 'Unsafe repair file parent')
    need(not path.is_symlink(), 'Unsafe repair file')
    info = path.stat()
    need(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
         and not info.st_mode & 0o022 and (not private or stat.S_IMODE(info.st_mode) == 0o600),
         'Unsafe repair file metadata')
    return path


def firewall_manifest(root=Path('/'), uid=0):
    """GNU sha256sum-compatible manifest for exactly the original eight files."""
    result = []
    for name in UFW_SOURCES:
        path = _safe_file(Path(root) / name.lstrip('/'), uid=uid, private=False)
        result.append(sha256(path.read_bytes()) + '  ' + name + '\n')
    return ''.join(result).encode('ascii')


def snapshot_file(path, uid=0):
    path = _safe_file(path, uid=uid)
    data = path.read_bytes()
    return {'data': base64.b64encode(data).decode('ascii'), 'sha256': sha256(data), 'mode': 0o600}


def atomic_private_replace(path, expected, data, uid=0):
    """Replace one already-owned private file only after an exact byte match."""
    need(isinstance(expected, bytes) and isinstance(data, bytes), 'Invalid private replacement bytes')
    path = _safe_file(path, uid=uid)
    current = path.read_bytes()
    if current == data:
        return False
    need(current == expected, 'Repair file changed concurrently; refusing replacement')
    parent = path.parent.stat()
    need(parent.st_uid == uid and not parent.st_mode & 0o022, 'Unsafe repair target directory')
    fd, temporary = tempfile.mkstemp(prefix='.spn-udp-repair-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True
