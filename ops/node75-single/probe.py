"""Bounded real owner-account traffic to the new node, no new subscriptions."""
import concurrent.futures
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.parse
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location('single_node', HERE / 'manage.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
spec = importlib.util.spec_from_file_location('physical_probe', HERE.parent / 'tekst-setup/physical_probe.py')
physical = importlib.util.module_from_spec(spec)
spec.loader.exec_module(physical)
e = physical.e
ENGINE = Path('/private/tmp/spn-runtime-probe.wnRZpc/xray')
SHA = '7616e4d8d5b8bedaee14ebe167954e24f35ccea58cdb941600e2ff76509fc31c'


def source_config():
    file = ROOT / '.vpn-test-link.txt'
    assert not file.is_symlink() and file.stat().st_mode & 0o777 == 0o600
    url = file.read_text().strip()
    parsed = urllib.parse.urlsplit(url)
    assert parsed.scheme == 'https' and parsed.hostname == 'sub.wayspn.online'
    assert not parsed.username and not parsed.password and not parsed.query
    assert not any(c in url for c in '\r\n"')
    config = 'url = "' + url + '"\nuser-agent = "Happ/4.0"\nheader = "Accept: application/json"\n'
    for attempt in range(4):
        result = subprocess.run(['/usr/bin/curl', '--config', '-', '-4sS', '--interface', 'en0', '--noproxy', '*',
                                 '--connect-timeout', '5', '--max-time', '12', '--max-filesize', '4000000'],
                                input=config.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode == 0:
            docs = json.loads(result.stdout)
            return [docs] if isinstance(docs, dict) else docs
        time.sleep(.3)
    raise RuntimeError('Saved subscription could not be read; link/body suppressed')


def outbound(store):
    keys = store.load('reality.private.json')
    cached = store.load('owner-probe.private.json')
    if cached is not None:
        point, reason = e.endpoint(cached)
        assert reason is None and point['address'] == m.IP and point['port'] == 443
        assert cached['streamSettings']['realitySettings']['publicKey'] == keys['public']
        result = copy.deepcopy(cached)
        result['streamSettings']['realitySettings']['serverName'] = m.active_sni(store)
        result['streamSettings']['realitySettings']['fingerprint'] = m.active_fingerprint(store)
        return result
    users = set()
    for doc in source_config():
        for out in doc.get('outbounds', []):
            point, reason = e.endpoint(out)
            if reason is None and point['address'] == 'fi104.wayspn.com' and point['network'] == 'ws':
                users.add(str(uuid.UUID(out['settings']['vnext'][0]['users'][0]['id'])))
    assert len(users) == 1, 'Owner subscription credentials ambiguous'
    result = {'tag': 'node75-owner-probe', 'protocol': 'vless',
            'settings': {'vnext': [{'address': m.IP, 'port': 443,
                'users': [{'id': users.pop(), 'encryption': 'none', 'flow': 'xtls-rprx-vision'}]}]},
            'streamSettings': {'network': 'raw', 'security': 'reality', 'realitySettings': {
                'serverName': m.active_sni(store), 'fingerprint': m.active_fingerprint(store), 'publicKey': keys['public'],
                'shortId': keys['short'], 'spiderX': '/'}}}
    store.save('owner-probe.private.json', result)
    return result


def exported_outbound(store):
    """Use the exact newly rendered subscription leaf, never a hand-built one."""
    matches = []
    for document in source_config():
        for out in document.get('outbounds', []):
            point, reason = e.endpoint(out)
            if reason is None and point['address'] == m.IP and point['port'] == 443:
                matches.append(out)
    unique = {m.u.digest(out): out for out in matches}
    assert len(unique) == 1, 'New exported endpoint missing or ambiguous'
    result = copy.deepcopy(next(iter(unique.values())))
    expected = outbound(store)
    point, reason = e.endpoint(result)
    assert reason is None and point['network'] == 'raw' and point['security'] == 'reality'
    reality = result['streamSettings']['realitySettings']
    for key in ('serverName', 'fingerprint', 'publicKey', 'shortId'):
        assert reality[key] == expected['streamSettings']['realitySettings'][key], 'Export transport mismatch'
    user = result['settings']['vnext'][0]['users'][0]
    assert user['id'] == expected['settings']['vnext'][0]['users'][0]['id']
    assert user['flow'] == 'xtls-rprx-vision' and user['encryption'] == 'none'
    store.save('export-check-' + str(time.time_ns()) + '.json', {
        'ip': m.IP, 'matches': len(matches), 'uniqueLeaves': len(unique),
        'fingerprint': reality['fingerprint'], 'realityKeysMatch': True, 'flowMatches': True})
    print(json.dumps({'exportVerified': True, 'fingerprint': reality['fingerprint'],
                      'realityKeysMatch': True, 'flowMatches': True}), flush=True)
    return result


def main(control=False):
    e.pinned_engine(ENGINE, SHA)
    store = m.u.Store(m.STATE)
    out = outbound(store)
    physical_ip = subprocess.check_output(['/usr/sbin/ipconfig', 'getifaddr', 'en0'], text=True).strip()
    assert physical_ip
    def test(number):
        mode = 'body256k' if number == 4 else 'https204'
        with tempfile.TemporaryDirectory(prefix='spn-node75-real-', dir='/private/tmp') as tmp:
            if control:
                result = e.test_one(ENGINE, copy.deepcopy(out), Path(tmp), 0, transport_proxy=19775)
            else:
                result = physical.probe((m.IP, 443), copy.deepcopy(out), Path(tmp), physical_ip, ENGINE, '26.9.9', mode)
            logs = '\n'.join(p.read_text(errors='replace').lower() for p in Path(tmp).glob('engine-*.log'))
            result['errorCategories'] = [x for x in ('context deadline exceeded', 'connection refused', 'failed to dial',
                'reality: received real certificate', 'tls handshake', 'invalid user', 'connection reset') if x in logs]
            print(json.dumps({'test': number, 'route': 'ssh-control' if control else 'en0', **result}), flush=True)
            return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(test, range(3 if control else 5)))
    passed = all(r['status'] == 'passed' and (control or r['physicalSourceObserved']) for r in results)
    store.save('probe-results-' + str(time.time_ns()) + '.json', {'ip': m.IP, 'results': results, 'passed': passed})
    if passed and not control:
        store.save('probe-proof.json', {'ip': m.IP, 'passed': True, 'tests': 5, 'physicalInterface': 'en0'})
    return 0 if passed else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control', action='store_true')
    try:
        raise SystemExit(main(parser.parse_args().control))
    except Exception as error:
        print('Real traffic probe stopped:', type(error).__name__, '; private details suppressed')
        raise SystemExit(2)
