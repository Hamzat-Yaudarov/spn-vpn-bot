#!/usr/bin/env python3
"""Validated, bounded scanner lists; this module never changes the firewall.

The community list is a useful extra filter against listed scanners. It does
not identify every scanner and cannot prevent passive traffic classification.
Only the antiscanner list is used: affiliation with a government organization
in the much broader government_networks list is not evidence of scanning.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import tempfile
import time


SOURCE = 'https://raw.githubusercontent.com/shadow-netlab/traffic-guard-lists/refs/heads/main/public/antiscanner.list'
PINNED_REVISION = '7b9623238d7420cbb31feaa149610738feb45fd5'
PINNED_SOURCE = SOURCE.replace('refs/heads/main', PINNED_REVISION)
LICENSE = 'MIT declared in upstream README; no separate LICENSE file provided'
BUNDLED = Path(__file__).with_name('scanner_lists.json')
MAX_BYTES = 131072
MAX_NETWORKS = 2048
MAX_V4_ADDRESSES = 65536
MAX_V6_NETWORKS = 64
DEADLINE = 12


class ScannerError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ScannerError(message)


def parse_list(raw):
    """Reject malformed, non-public, overbroad or oversized lists as a whole."""
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_BYTES,
            'Scanner list is empty or exceeds the size limit')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ScannerError('Scanner list is not UTF-8') from None
    require(not any(ord(character) < 32 and character not in '\n\r\t' for character in text),
            'Scanner list contains control characters')
    networks = set()
    for number, line in enumerate(text.splitlines(), 1):
        require(len(line) <= 512, 'Scanner list contains an oversized line')
        value = line.split('#', 1)[0].strip()
        if not value:
            continue
        require(re.fullmatch(r'[0-9a-fA-F.:]+/[0-9]{1,3}', value) is not None,
                'Scanner list contains an invalid CIDR at line ' + str(number))
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError:
            raise ScannerError('Scanner list contains an invalid CIDR at line ' + str(number)) from None
        require(network.network_address.is_global and network.broadcast_address.is_global
                and not network.is_multicast
                and network.prefixlen >= (20 if network.version == 4 else 48),
                'Scanner list contains a non-public or overbroad CIDR')
        networks.add(network)
        require(len(networks) <= MAX_NETWORKS, 'Scanner list contains too many CIDRs')
    require(bool(networks), 'Scanner list contains no CIDRs')
    ipv4 = list(ipaddress.collapse_addresses(n for n in networks if n.version == 4))
    ipv6 = list(ipaddress.collapse_addresses(n for n in networks if n.version == 6))
    require(sum(n.num_addresses for n in ipv4) <= MAX_V4_ADDRESSES
            and len(ipv6) <= MAX_V6_NETWORKS, 'Scanner list covers too many addresses')
    return [str(n) for n in ipv4], [str(n) for n in ipv6]


def timestamp(value=None):
    if value is None:
        value = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value),
            'Invalid scanner snapshot timestamp')
    try:
        dt.datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        raise ScannerError('Invalid scanner snapshot timestamp') from None
    return value


def snapshot(raw, *, updated_at=None, pinned=False):
    ipv4, ipv6 = parse_list(raw)
    return {'version': 1, 'sources': [{
        'name': 'traffic-guard-antiscanner', 'url': PINNED_SOURCE if pinned else SOURCE,
        'revision': PINNED_REVISION if pinned else None,
        'sha256': hashlib.sha256(raw).hexdigest(), 'license': LICENSE,
        'content': raw.decode('utf-8')}],
        'ipv4': ipv4, 'ipv6': ipv6, 'updatedAt': timestamp(updated_at)}


def validate_snapshot(value):
    require(isinstance(value, dict) and set(value) == {
        'version', 'sources', 'ipv4', 'ipv6', 'updatedAt'} and type(value['version']) is int
        and value['version'] == 1, 'Invalid scanner snapshot format')
    timestamp(value['updatedAt'])
    require(isinstance(value['sources'], list) and len(value['sources']) == 1,
            'Unexpected scanner snapshot sources')
    item = value['sources'][0]
    require(isinstance(item, dict) and set(item) == {
        'name', 'url', 'revision', 'sha256', 'license', 'content'}
        and item['name'] == 'traffic-guard-antiscanner'
        and item['license'] == LICENSE
        and ((item['url'] == SOURCE and item['revision'] is None)
             or (item['url'] == PINNED_SOURCE and item['revision'] == PINNED_REVISION)),
        'Unexpected scanner snapshot source identity')
    require(isinstance(item['content'], str) and isinstance(item['sha256'], str),
            'Invalid scanner snapshot source data')
    try:
        raw = item['content'].encode('utf-8')
    except UnicodeEncodeError:
        raise ScannerError('Invalid scanner snapshot source encoding') from None
    require(hashlib.sha256(raw).hexdigest() == item['sha256'], 'Scanner snapshot source digest differs')
    ipv4, ipv6 = parse_list(raw)
    require(value['ipv4'] == ipv4 and value['ipv6'] == ipv6,
            'Scanner snapshot addresses differ from its validated source')
    return value


def safe_parent(path):
    parent = path.parent
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'Unsafe scanner cache path')
    info = parent.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700,
            'Scanner cache directory must be owned with mode 0700')


def load_snapshot(path=None):
    bundled = path is None
    target = BUNDLED if bundled else Path(path).absolute()
    if not bundled:
        safe_parent(target)
    try:
        with os.fdopen(os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    and info.st_uid in ((0, os.geteuid()) if bundled else (os.geteuid(),))
                    and stat.S_IMODE(info.st_mode) in ((0o600, 0o644) if bundled else (0o600,)),
                    'Unsafe scanner snapshot file')
            raw = stream.read(MAX_BYTES * 2 + 1)
            require(len(raw) <= MAX_BYTES * 2, 'Scanner snapshot exceeds the size limit')
        return validate_snapshot(json.loads(raw))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScannerError('Cannot read a valid scanner snapshot: ' + type(error).__name__) from None


def download():
    """Fetch one fixed HTTPS source, without redirects, with bounded reads."""
    connection = http.client.HTTPSConnection('raw.githubusercontent.com', timeout=6,
                                              context=ssl.create_default_context())
    deadline = time.monotonic() + DEADLINE
    response = None
    try:
        connection.request('GET', '/shadow-netlab/traffic-guard-lists/refs/heads/main/public/antiscanner.list',
                           headers={'User-Agent': 'SPN-scanner-list/1', 'Accept-Encoding': 'identity',
                                    'Connection': 'close'})
        transport = connection.sock
        response = connection.getresponse()
        require(response.status == 200, 'Scanner source did not return HTTP 200; redirects are refused')
        require(response.getheader('Content-Encoding', 'identity') == 'identity',
                'Compressed scanner source is not supported')
        length = response.getheader('Content-Length')
        require(length is None or (length.isascii() and length.isdecimal() and 0 < int(length) <= MAX_BYTES),
                'Scanner source Content-Length exceeds the size limit')
        expected_length = int(length) if length is not None else None
        chunks, size = [], 0
        while True:
            # HTTPResponse closes its buffered socket when the final fixed-
            # length or chunked read completes. Do not set a timeout on that
            # already closed descriptor on the following iteration.
            if response.isclosed():
                break
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'Scanner source exceeded the download deadline')
            transport.settimeout(min(6, remaining))
            # read1 returns after one buffered/raw read; read(n) can keep
            # waiting for n bytes while a slow peer resets its socket timeout.
            chunk = response.read1(min(4096, MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            require(size <= MAX_BYTES, 'Scanner source exceeds the size limit')
            require(expected_length is None or size <= expected_length,
                    'Scanner source exceeds its declared Content-Length')
            if expected_length is not None and size == expected_length:
                break
        require(expected_length is None or size == expected_length,
                'Scanner source ended before its declared Content-Length')
        return b''.join(chunks)
    except (OSError, http.client.HTTPException):
        raise ScannerError('Scanner list HTTPS download failed; previous snapshot is retained') from None
    finally:
        if response is not None:
            response.close()
        connection.close()


def refresh(path, *, fetch=download, updated_at=None):
    """Replace the cache only after the complete response passes validation.

    This function does not apply firewall rules. Its caller owns the firewall
    transaction and must retain any controller/panel exceptions independently.
    """
    target = Path(path).absolute()
    safe_parent(target)
    before = load_snapshot(target) if target.exists() else None
    result = snapshot(fetch(), updated_at=updated_at)
    validate_snapshot(result)
    raw = (json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    require(len(raw) <= MAX_BYTES * 2, 'Scanner snapshot exceeds the size limit')
    fd, temporary = tempfile.mkstemp(prefix='.scanners-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        safe_parent(target)
        require((load_snapshot(target) if target.exists() else None) == before,
                'Scanner snapshot changed during refresh; retry the update')
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('show', 'refresh'))
    parser.add_argument('--cache', type=Path)
    args = parser.parse_args(argv)
    if args.action == 'refresh':
        require(args.cache is not None, 'Choose a private cache file with --cache')
        result = refresh(args.cache)
    else:
        result = load_snapshot(args.cache)
    print(json.dumps({key: result[key] for key in ('version', 'ipv4', 'ipv6', 'updatedAt')}, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except ScannerError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
