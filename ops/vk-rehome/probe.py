"""Scoped HTTPS checks with the protected, existing owner test export."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time

OPS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('physical', OPS / 'tekst-setup/physical_probe.py')
physical = importlib.util.module_from_spec(spec)
spec.loader.exec_module(physical)
e = physical.e
FRONT = ('185.86.147.77', 10016)
LOCAL_SE_FORWARD = 19443
REFERENCES = {'grpc': ('191.217.252.188', 2083), 'ws': ('fi103.wayspn.com', 443)}
SITES = {'de-direct': ('de177.wayspn.com', '48.102.177.3'), 'se-direct': ('ch121.wayspn.com', '31.59.153.161')}


def source_outbounds():
    result = {}
    for doc in e.protected_json(physical.ROOT / 'subscription.private'):
        for out in doc.get('outbounds', []):
            row, _ = e.endpoint(out)
            if not row or row['protocol'] != 'vless':
                continue
            point = row['address'], row['port']
            if point == FRONT and (row['network'], row['security'], row['sni']) == ('raw', 'reality', 'login.vk.com'):
                result.setdefault('vk', copy.deepcopy(out))
            if REFERENCES.get(row['network']) == point:
                result.setdefault(row['network'], copy.deepcopy(out['settings']['vnext'][0]['users']))
    e.require(set(result) == {'vk', *REFERENCES})
    return result


def direct(kind, domain, ip, users):
    stream = {'network': kind, 'security': 'tls', 'tlsSettings': {'serverName': domain,
              'fingerprint': 'chrome', 'alpn': ['h2' if kind == 'grpc' else 'http/1.1'], 'allowInsecure': False}}
    if kind == 'grpc':
        stream['grpcSettings'] = {'serviceName': 'spngrpc'}
    else:
        stream['wsSettings'] = {'path': '/direct/ws/', 'headers': {'Host': domain}}
    return {'tag': kind, 'protocol': 'vless', 'streamSettings': stream,
            'settings': {'vnext': [{'address': ip, 'port': 443, 'users': users}]}}


def verify_vk_exit(engine, outbound):
    """Prove the relay reaches the Swedish backend, not just any backend."""
    expected = SITES['se-direct'][1]
    child = None
    with tempfile.TemporaryDirectory(prefix='spn-vk-exit-', dir='/private/tmp') as tmp:
        config = Path(tmp) / 'engine.private.json'
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        proxy = e.single_config(outbound, port)
        proxy['outbounds'][0]['streamSettings'].setdefault('sockopt', {})['interface'] = 'en0'
        e.write_private(config, proxy)
        try:
            child = subprocess.Popen([str(engine), 'run', '-config', str(config)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, env=e.ENV, start_new_session=True)
            for _ in range(60):
                if child.poll() is not None:
                    break
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=0.15):
                        break
                except OSError:
                    time.sleep(0.05)
            for url in ('https://www.cloudflare.com/cdn-cgi/trace', 'https://1.1.1.1/cdn-cgi/trace'):
                result = subprocess.run(['/usr/bin/curl', '-q', '-4fsS', '--connect-timeout', '5', '--max-time', '10',
                                         '--max-filesize', '4096', '--noproxy', '', '--socks5-hostname',
                                         f'127.0.0.1:{port}', url], env=e.ENV,
                                        capture_output=True, timeout=12, check=False)
                exits = [line[3:] for line in result.stdout.splitlines() if line.startswith(b'ip=')]
                matched = result.returncode == 0 and exits == [expected.encode()]
                old = exits in ([b'152.234.235.106'], [b'48.102.177.3'])
                print(json.dumps({'test': 'vk-exit', 'swedenConfirmed': matched, 'germanyObserved': old,
                                  'httpsExitCode': result.returncode, 'reportedAnExit': bool(exits)}), flush=True)
                if matched:
                    return 0
            return 1
        finally:
            e.stop(child)


def run(action):
    engine, digest = physical.ENGINES['26.9.9']
    e.pinned_engine(engine, digest)
    refs = source_outbounds()
    if action == 'vk-exit':
        return verify_vk_exit(engine, refs['vk'])
    through_ssh = action == 'se-vk-local'
    if action in SITES:
        domain, ip = SITES[action]
        resolved = {row[4][0] for row in socket.getaddrinfo(domain, 443, socket.AF_INET, socket.SOCK_STREAM)}
        e.require(resolved == {ip})
        target = (ip, 443)
        configs = {kind: direct(kind, domain, ip, refs[kind]) for kind in REFERENCES}
    else:
        target = FRONT
        configs = {'vk': refs['vk']}
    if through_ssh:
        validated = e.single_config

        def via_loopback(out, socks):
            config = validated(out, socks)
            server = config['outbounds'][0]['settings']['vnext'][0]
            e.require((server['address'], server['port']) == FRONT)
            server.update(address='127.0.0.1', port=LOCAL_SE_FORWARD)
            return config

        e.single_config = via_loopback
    local_ip = None if through_ssh else subprocess.check_output(
        ['/usr/sbin/ipconfig', 'getifaddr', 'en0'], text=True).strip()
    e.require(through_ssh or local_ip)

    def test(kind, out):
        with tempfile.TemporaryDirectory(prefix='spn-vk-rehome-e2e-', dir='/private/tmp') as tmp:
            checked = (e.test_one(engine, out, Path(tmp), 0) if through_ssh else
                       physical.probe(target, out, Path(tmp), local_ip, engine, '26.9.9', 'https204'))
            return {'test': action, 'transport': kind, **checked}

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(test, kind, out) for kind, out in configs.items()]
        results = [job.result() for job in jobs]
    print(json.dumps(results))
    return 0 if all(row['status'] == 'passed' and (through_ssh or row['physicalSourceObserved']) for row in results) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('test', nargs='?', choices=(*SITES, 'se-vk-local', 'vk-front', 'vk-exit'))
    args = parser.parse_args()
    if not args.test:
        print('Plan only; choose one scoped test. No changes.')
    else:
        try:
            raise SystemExit(run(args.test))
        except Exception as exc:
            print('Probe stopped:', type(exc).__name__, '; credentials suppressed')
            raise SystemExit(2) from None
