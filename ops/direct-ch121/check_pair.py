"""Bounded auth checks for the two approved new pairs. No provider writes."""
import argparse
import concurrent.futures
import copy
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('physical', HERE.parent / 'tekst-setup/physical_probe.py')
physical = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(physical)
e = physical.e
TARGETS = {'ch121.wayspn.com': '179.254.109.121', 'node18253.wayspn.com': '48.102.182.53'}
SOURCES = {'grpc': ('191.217.252.188', 2083), 'ws': ('fi103.wayspn.com', 443)}


def main():
    engine, digest = physical.ENGINES['26.9.9']
    e.pinned_engine(engine, digest)
    local_ip = subprocess.check_output(['/usr/sbin/ipconfig', 'getifaddr', 'en0'], text=True).strip()
    e.require(local_ip)
    sources = {}
    for document in e.protected_json(physical.ROOT / 'subscription.private'):
        for outbound in document.get('outbounds', []):
            data, _ = e.endpoint(outbound)
            if not data or data['protocol'] != 'vless':
                continue
            kind = data['network']
            if (data['address'], data['port']) == SOURCES.get(kind):
                sources.setdefault(kind, copy.deepcopy(outbound['settings']['vnext'][0]['users']))
    e.require(set(sources) == set(SOURCES))

    def run(domain, ip, kind):
        stream = {'network': kind, 'security': 'tls', 'tlsSettings': {
            'serverName': domain, 'fingerprint': 'chrome',
            'alpn': ['h2' if kind == 'grpc' else 'http/1.1'], 'allowInsecure': False,
        }}
        if kind == 'grpc':
            stream['grpcSettings'] = {'serviceName': 'spngrpc'}
        else:
            stream['wsSettings'] = {'path': '/direct/ws/', 'headers': {'Host': domain}}
        outbound = {'tag': 'verified', 'protocol': 'vless',
                    'settings': {'vnext': [{'address': ip, 'port': 443, 'users': sources[kind]}]},
                    'streamSettings': stream}
        with tempfile.TemporaryDirectory(prefix='spn-new-pair-e2e-', dir='/private/tmp') as tmp:
            return {'domain': domain, 'transport': kind, **physical.probe(
                (ip, 443), outbound, Path(tmp), local_ip, engine, '26.9.9', 'https204')}

    for domain, ip in TARGETS.items():
        resolved = {row[4][0] for row in socket.getaddrinfo(domain, 443, socket.AF_INET, socket.SOCK_STREAM)}
        e.require(resolved == {ip})
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        checks = [pool.submit(run, domain, ip, kind) for domain, ip in TARGETS.items() for kind in SOURCES]
        results = [check.result() for check in checks]
    print(json.dumps(results, ensure_ascii=False))
    return 0 if all(row['status'] == 'passed' and row['physicalSourceObserved'] for row in results) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Use the local owner-approved export')
    if not parser.parse_args().run:
        print('Plan only. Use --run for four authenticated HTTPS checks.')
        raise SystemExit(0)
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('Auth probe stopped:', type(exc).__name__, '; credentials and payload suppressed')
        raise SystemExit(2) from None
