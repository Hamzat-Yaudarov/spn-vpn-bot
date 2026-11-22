"""One persistent client, physical en0, bounded real traffic; no fallback."""
import concurrent.futures
import argparse
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('single_probe', HERE / 'probe.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def main(fingerprint=None, exported=False, download_bytes=262144):
    p.e.pinned_engine(p.ENGINE, p.SHA)
    store = p.m.u.Store(p.m.STATE)
    assert not (fingerprint and exported), 'Export must be tested without overrides'
    out = p.exported_outbound(store) if exported else p.outbound(store)
    if fingerprint:
        out['streamSettings']['realitySettings']['fingerprint'] = fingerprint
    fingerprint = out['streamSettings']['realitySettings']['fingerprint']
    source = subprocess.check_output(['/usr/sbin/ipconfig', 'getifaddr', 'en0'], text=True).strip()
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    child = None
    with tempfile.TemporaryDirectory(prefix='spn-node75-session-', dir='/private/tmp') as tmp:
        directory = Path(tmp)
        config = p.e.single_config(out, port)
        config['log']['loglevel'] = 'debug'
        config['outbounds'][0].setdefault('streamSettings', {}).setdefault('sockopt', {})['interface'] = 'en0'
        p.e.write_private(directory / 'config.json', config)
        try:
            with p.e.private_output(directory / 'engine.log') as log:
                child = subprocess.Popen([str(p.ENGINE), 'run', '-c', str(directory / 'config.json')],
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                         env=p.e.ENV, start_new_session=True)
                for _ in range(100):
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.1):
                            break
                    except OSError:
                        time.sleep(.05)
                base = ['/usr/bin/curl', '-q', '-4sS', '--retry', '0', '--connect-timeout', '4', '--max-time', '8',
                        '--noproxy', '', '--socks5-hostname', '127.0.0.1:' + str(port)]
                def request(number, mode='204'):
                    if mode == 'trace':
                        url = 'https://1.1.1.1/cdn-cgi/trace'
                        args = ['--max-filesize', '4096']
                    else:
                        url = p.e.URL if mode == '204' else 'https://speed.cloudflare.com/__down?bytes=' + str(download_bytes)
                        args = ['--max-filesize', str(download_bytes + 4096), '--output', '/dev/null', '--write-out',
                                '%{http_code} %{time_total} %{size_download} %{time_connect} %{time_appconnect} %{time_starttransfer}']
                    if mode == 'body':
                        args += ['--max-time', '20']
                    r = subprocess.run(base + args + [url], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=22)
                    if mode == 'trace':
                        exits = [line[3:] for line in r.stdout.decode(errors='replace').splitlines() if line.startswith('ip=')]
                        result = {'test': number, 'mode': mode, 'passed': r.returncode == 0 and exits == [p.m.IP],
                                  'exitMatches': exits == [p.m.IP], 'curlExit': r.returncode}
                    else:
                        parts = r.stdout.decode(errors='replace').split()
                        code = '204' if mode == '204' else '200'
                        result = {'test': number, 'mode': mode, 'passed': r.returncode == 0 and len(parts) == 6 and parts[0] == code
                                  and (mode == '204' or parts[2] == str(download_bytes)), 'curlExit': r.returncode}
                        if len(parts) == 6:
                            result.update(http=parts[0], ms=round(float(parts[1])*1000), bytes=int(parts[2]),
                                          localConnectMs=round(float(parts[3])*1000), tlsMs=round(float(parts[4])*1000),
                                          firstByteMs=round(float(parts[5])*1000))
                            if mode == 'body' and float(parts[1]) > 0:
                                result['downloadMbpsIncludingHandshake'] = round(int(parts[2])*8/float(parts[1])/1e6, 2)
                    result['clientExit'] = child.poll()
                    if r.returncode:
                        # Targets are fixed public URLs; no subscription link or
                        # authentication material can be present in curl stderr.
                        result['curlError'] = r.stderr.decode(errors='replace').strip()[:400]
                    print(json.dumps(result), flush=True)
                    return result
                results = []
                # Sample the real physical socket once, not continuously.
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(request, 0)
                    time.sleep(.12)
                    sockets = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(child.pid), '-iTCP', '-Fn'],
                                             capture_output=True, timeout=3)
                    physical = any(('n' + source + ':').encode() in line and ('->' + p.m.IP + ':443').encode() in line
                                   for line in sockets.stdout.splitlines())
                    results.append(pending.result())
                for number in range(1, 5):
                    results.append(request(number))
                results.append(request(5, 'trace'))
                results.append(request(6, 'body'))
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    results.extend(pool.map(request, (7, 8)))
                passed = physical and all(r['passed'] and r['clientExit'] is None for r in results)
                summary = {'ip': p.m.IP, 'passed': passed, 'physicalInterface': 'en0',
                           'physicalSourceObserved': physical, 'tests': len(results), 'results': results,
                           'fingerprint': fingerprint, 'actualExport': exported}
                store.save('session-results-' + str(time.time_ns()) + '.json', summary)
                if passed:
                    store.save('probe-proof.json', {'ip': p.m.IP, 'passed': True, 'tests': len(results),
                               'physicalInterface': 'en0', 'fingerprint': fingerprint})
                print(json.dumps({'passed': passed, 'physicalSourceObserved': physical, 'tests': len(results)}), flush=True)
                return 0 if passed else 1
        finally:
            p.e.stop(child)
            debug = directory / 'engine.log'
            if debug.exists():
                # Keep debug private, rather than deleting decisive evidence.
                store.save('client-debug-' + str(time.time_ns()) + '.json', {'log': debug.read_text(errors='replace')})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fingerprint', choices=('chrome', 'safari', 'firefox'))
    parser.add_argument('--exported', action='store_true')
    parser.add_argument('--download-bytes', type=int, choices=(262144, 4194304), default=262144)
    try:
        args = parser.parse_args()
        raise SystemExit(main(args.fingerprint, args.exported, args.download_bytes))
    except Exception as error:
        print('Session probe stopped:', type(error).__name__, '; credentials suppressed')
        raise SystemExit(2)
