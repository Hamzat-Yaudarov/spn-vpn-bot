"""Prepare one private client link using the existing authorized subscription.

No server, account, subscription, price, quota, TUN or route writes.
The credential is sent to the native local QR encoder through stdin only.
"""
import importlib.util
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
s = importlib.util.spec_from_file_location('probe', HERE / 'physical_probe.py')
p = importlib.util.module_from_spec(s)
s.loader.exec_module(p)


def main():
    docs = p.e.protected_json(p.ROOT / 'subscription.private')
    candidates = []
    for doc in docs:
        for outbound in doc.get('outbounds', []):
            endpoint, reason = p.e.endpoint(outbound)
            if not reason and endpoint['address'] == '64.188.80.39' and endpoint['port'] == 2443:
                candidates.append(outbound)
    assert len(candidates) == 1
    outbound = candidates[0]
    server, = outbound['settings']['vnext']
    user, = server['users']
    assert re.fullmatch(r'[0-9a-fA-F-]{36}', user['id'])
    assert user['flow'] == 'xtls-rprx-vision' and user['encryption'] == 'none'
    stream = outbound['streamSettings']
    reality = stream['realitySettings']
    assert stream['security'] == 'reality' and reality['serverName'] == 'www.tekst.site'
    query = {'encryption': 'none', 'security': 'reality', 'type': 'tcp',
             'sni': reality['serverName'], 'fp': reality['fingerprint'],
             'pbk': reality.get('publicKey', reality.get('password')),
             'sid': reality['shortId'], 'flow': user['flow']}
    assert query['pbk'] and query['fp'] == 'chrome'
    uri = 'vless://' + user['id'] + '@64.188.80.39:2443?' + urlencode(query) + '#SPN-TCP-CLEAN-TEST'
    path = p.ROOT / 'tcp-clean-test.private.txt'
    if path.exists():
        assert path.stat().st_mode & 0o777 == 0o600 and path.read_text() == uri
    else:
        with p.e.private_output(path) as f:
            f.write(uri.encode())
    process = subprocess.run(['/usr/bin/xcrun', 'swift', str(HERE / 'private_qr.swift')],
                             input=uri.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=90, check=False)
    if process.returncode:
        raise RuntimeError('Local QR generation failed; payload/output suppressed')
    print('Prepared existing-account TCP profile; QR verified privately, no new subscription')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Private client preparation stopped:', type(error).__name__, '; credential suppressed')
        raise SystemExit(2)
