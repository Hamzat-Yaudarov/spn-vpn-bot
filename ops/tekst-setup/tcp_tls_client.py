"""Prepare a private TLS2444 test using the existing owner's account.

The default action is offline. The explicit probe uses one isolated en0
outbound. Neither action changes the server, account, subscription or routes.
"""
import argparse
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from urllib.parse import urlencode
from uuid import UUID

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('probe', HERE / 'physical_probe.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

ADDRESS = '64.188.80.39'
PORT = 2444
SNI = 'tekst.site'
FLOW = 'xtls-rprx-vision'
VERSION = '26.9.9'


def derive():
    docs = p.e.protected_json(p.ROOT / 'subscription.private')
    candidates = []
    for doc in docs:
        for outbound in doc.get('outbounds', []):
            endpoint, reason = p.e.endpoint(outbound)
            if not reason and (endpoint['address'], endpoint['port']) == (ADDRESS, 2443):
                p.e.require(endpoint['protocol'] == 'vless' and endpoint['network'] == 'raw'
                            and endpoint['security'] == 'reality' and endpoint['sni'] == 'www.tekst.site')
                candidates.append(outbound)
    p.e.require(len(candidates) == 1)
    server, = candidates[0]['settings']['vnext']
    user, = server['users']
    identity = str(UUID(user['id']))
    p.e.require(identity == user['id'].lower() and user['flow'] == FLOW and user['encryption'] == 'none')
    outbound = {'tag': 'proxy', 'protocol': 'vless', 'settings': {'vnext': [
        {'address': ADDRESS, 'port': PORT, 'users': [
            {'id': identity, 'flow': FLOW, 'encryption': 'none'}]}]},
        'streamSettings': {'network': 'tcp', 'security': 'tls', 'tlsSettings': {
            'serverName': SNI, 'fingerprint': 'chrome', 'allowInsecure': False}}}
    endpoint, reason = p.e.endpoint(outbound)
    p.e.require(not reason and endpoint['security'] == 'tls' and endpoint['sni'] == SNI)
    return outbound


def write_uri(path, payload):
    try:
        with p.e.private_output(path) as stream:
            stream.write(payload)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            p.e.require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                        and info.st_uid == os.geteuid() and info.st_nlink == 1
                        and info.st_size == len(payload))
            p.e.require(stream.read(len(payload) + 1) == payload)


def prepare(outbound):
    user = outbound['settings']['vnext'][0]['users'][0]
    # Omit any allowInsecure link parameter; certificate validation is the default.
    query = urlencode({'encryption': 'none', 'security': 'tls', 'type': 'tcp',
                       'sni': SNI, 'fp': 'chrome', 'flow': FLOW})
    payload = (f"vless://{user['id']}@{ADDRESS}:{PORT}?{query}#SPN-TCP-TLS-TEST").encode()
    write_uri(p.ROOT / 'tcp-tls-test.private.txt', payload)
    checked = subprocess.run(['/usr/bin/xcrun', 'swift', str(HERE / 'private_qr.swift')],
                             input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=90, check=False)
    p.e.require(checked.returncode == 0)
    info = (p.ROOT / 'tcp-tls-test.png').lstat()
    p.e.require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_uid == os.geteuid() and info.st_size > 0)
    print('Prepared private TCP/TLS2444 URI and locally verified QR; credentials suppressed')


def probe(outbound):
    engine, digest = p.ENGINES[VERSION]
    p.e.pinned_engine(engine, digest)
    source = subprocess.check_output(['/usr/sbin/ipconfig', 'getifaddr', 'en0'], text=True).strip()
    p.e.require(ipaddress.ip_address(source).version == 4)
    directory = Path(tempfile.mkdtemp(prefix='tcp-tls2444-', dir=p.ROOT))
    result = p.probe((ADDRESS, PORT), outbound, directory, source, engine, VERSION, 'body256k')
    p.e.write_private(directory / 'result.json', result)
    print(json.dumps(result))
    return 0 if result['status'] == 'passed' and result['physicalSourceObserved'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=('prepare', 'probe'), default='prepare')
    action = parser.parse_args().action
    outbound = derive()
    return prepare(outbound) if action == 'prepare' else probe(outbound)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('Private TLS client stopped:', type(error).__name__, '; credentials suppressed')
        raise SystemExit(2) from None
