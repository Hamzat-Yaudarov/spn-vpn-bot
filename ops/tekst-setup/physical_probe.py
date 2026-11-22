"""Fixed owner-authorized HTTPS probes bound only to Mac en0.

No TUN, route changes, subscription creation, direct fallback or public listener.
Uses the owner's private saved subscription and a verified official engine.
"""
import concurrent.futures
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

ROOT = Path('/private/tmp/wayspn-tekst-auth-20261106')
ENGINES = {
    '26.7.28': (ROOT / 'xray', 'bd4154efa640c5b8e21f10b68afcc9177c4f1f543be3ec0485b10c499b2a4b27'),
    '26.9.9': (ROOT / 'core-2699/xray', '7616e4d8d5b8bedaee14ebe167954e24f35ccea58cdb941600e2ff76509fc31c'),
}
TARGETS = {('64.188.80.39', 2083), ('64.188.80.39', 2443), ('64.188.80.39', 443), ('185.86.147.77', 10016)}
SPEC = importlib.util.spec_from_file_location('e2e', Path(__file__).resolve().parents[1] / 'host-health-audit/e2e.py')
e = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(e)


def probe(endpoint, outbound, directory, physical_ip, engine, version, mode):
    child = request = None
    port = endpoint[1]
    result = {'address': endpoint[0], 'port': port, 'core': version, 'mode': mode,
              'interface': 'en0', 'physicalSourceObserved': False}
    config_path = directory / ('config-' + str(port) + '.json')
    try:
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            socks = reservation.getsockname()[1]
        config = e.single_config(outbound, socks)
        config['outbounds'][0].setdefault('streamSettings', {}).setdefault('sockopt', {})['interface'] = 'en0'
        e.write_private(config_path, config)
        with e.private_output(directory / ('engine-' + str(port) + '.log')) as log, \
                e.private_output(directory / ('curl-' + str(port) + '.log')) as errors:
            checked = subprocess.run([str(engine), 'run', '-test', '-c', str(config_path)],
                                     stdout=log, stderr=subprocess.STDOUT, timeout=12, env=e.ENV)
            if checked.returncode:
                return dict(result, status='uncovered', reason='config_rejected')
            child = subprocess.Popen([str(engine), 'run', '-c', str(config_path)], stdout=log,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=e.ENV,
                                     start_new_session=True)
            ready = False
            for _ in range(100):
                if child.poll() is not None:
                    break
                try:
                    with socket.create_connection(('127.0.0.1', socks), timeout=0.1):
                        ready = True
                    break
                except OSError:
                    time.sleep(0.05)
            if not ready:
                return dict(result, status='uncovered', reason='listener_unavailable')
            command = e.curl_args(socks)
            if mode == 'body256k':
                command[command.index(e.URL)] = 'https://speed.cloudflare.com/__down?bytes=262144'
                command[command.index('%{http_code} %{time_total}')] = '%{http_code} %{time_total} %{size_download}'
                command[command.index('--max-filesize') + 1] = '524288'
            request = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors,
                                       stdin=subprocess.DEVNULL, env=e.ENV, start_new_session=True)
            deadline = time.monotonic() + 23
            while request.poll() is None and time.monotonic() < deadline:
                sockets = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(child.pid), '-iTCP', '-Fn'],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2)
                expected = ('n' + physical_ip + ':').encode()
                peer = ('->' + endpoint[0] + ':' + str(port)).encode()
                if any(expected in line and peer in line for line in sockets.stdout.splitlines()):
                    result['physicalSourceObserved'] = True
                time.sleep(0.05)
            if request.poll() is None:
                return dict(result, status='failed', reason='probe_timeout')
            data, _ = request.communicate(timeout=2)
            found = re.fullmatch(rb'([0-9]{3}) ([0-9]+(?:\.[0-9]+)?)(?: ([0-9]+))?', data.strip())
            if request.returncode:
                return dict(result, status='failed', reason={28:'https_timeout', 35:'https_tls_error', 60:'certificate_error'}.get(request.returncode, 'transfer_failed'))
            if not found:
                return dict(result, status='uncovered', reason='unexpected_output')
            passed = found[1] == b'204' if mode == 'https204' else found[1] == b'200' and found[3] == b'262144'
            return dict(result, status='passed' if passed else 'failed',
                        reason='https_' + found[1].decode(), elapsedMs=round(float(found[2])*1000),
                        downloadedBytes=int(found[3]) if found[3] else 0)
    finally:
        e.stop(request)
        e.stop(child)
        if config_path.exists():
            config_path.unlink()


def main():
    if len(sys.argv) not in (2, 3, 4, 5) or not re.fullmatch(r'[a-z0-9-]{1,40}', sys.argv[1]):
        raise ValueError('Supply only a private run label')
    source = sys.argv[2] if len(sys.argv) >= 3 else 'subscription.private'
    if source not in {'subscription.private', 'subscription-tcp-vk.private'}:
        raise ValueError('Unsupported private subscription source')
    version = sys.argv[3] if len(sys.argv) >= 4 else '26.7.28'
    if version not in ENGINES:
        raise ValueError('Unsupported pinned engine version')
    mode = sys.argv[4] if len(sys.argv) == 5 else 'https204'
    if mode not in {'https204', 'body256k'}:
        raise ValueError('Unsupported bounded probe mode')
    engine, digest = ENGINES[version]
    e.pinned_engine(engine, digest)
    docs = e.protected_json(ROOT / source)
    physical_ip = subprocess.check_output(['/usr/sbin/ipconfig', 'getifaddr', 'en0'], text=True).strip()
    assert physical_ip and '\n' not in physical_ip
    found = {}
    for document in docs:
        for outbound in document.get('outbounds', []):
            endpoint, reason = e.endpoint(outbound)
            if not reason and (endpoint['address'], endpoint['port']) in TARGETS:
                found.setdefault((endpoint['address'], endpoint['port']), copy.deepcopy(outbound))
    assert set(found) == TARGETS, 'Missing selected host or working control'
    directory = ROOT / sys.argv[1]
    directory.mkdir(mode=0o700)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(probe, endpoint, outbound, directory, physical_ip, engine, version, mode)
                for endpoint, outbound in found.items()]
        results = [job.result() for job in jobs]
    e.write_private(directory / 'result.json', results)
    print(json.dumps(results))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Private probe stopped:', type(error).__name__, '; credentials and payload suppressed', file=sys.stderr)
        raise SystemExit(2)
