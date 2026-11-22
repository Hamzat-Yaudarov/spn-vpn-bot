"""Four fixed owner-authorized DNS/UDP probes through localhost SOCKS.

One A question per existing VPN endpoint, no TUN, fallback, route changes,
account writes, server writes, or captured packet payload. Only the selected
outbound socket is bound to physical en0. Secrets stay in private files.
"""
import concurrent.futures
import copy
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('physical_reference', HERE / 'physical_probe.py')
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)
e = reference.e
ROOT = reference.ROOT
ENGINE, DIGEST = reference.ENGINES['26.9.9']
EXPECTED_DIGEST = '7616e4d8d5b8bedaee14ebe167954e24f35ccea58cdb941600e2ff76509fc31c'
TARGETS = {('64.188.80.39', 2083), ('64.188.80.39', 2443),
           ('64.188.80.39', 443), ('185.86.147.77', 10016)}
DNS_ADDRESS = '1.1.1.1'
DNS_PORT = 53
QUESTION = b'\x03www\x07gstatic\x03com\x00\x00\x01\x00\x01'
DEADLINE_SECONDS = 6


def exact(control, size, deadline):
    result = bytearray()
    while len(result) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        control.settimeout(remaining)
        chunk = control.recv(size - len(result))
        if not chunk:
            raise ConnectionError()
        result.extend(chunk)
    return bytes(result)


def address_from(stream, atyp, deadline):
    if atyp == 1:
        return socket.inet_ntop(socket.AF_INET, exact(stream, 4, deadline))
    if atyp == 4:
        return socket.inet_ntop(socket.AF_INET6, exact(stream, 16, deadline))
    if atyp == 3:
        length = exact(stream, 1, deadline)[0]
        return exact(stream, length, deadline).decode('ascii')
    raise ValueError('Unsupported address type')


def unwrap_reply(data):
    if len(data) < 10 or data[:3] != b'\x00\x00\x00':
        raise ValueError('Invalid UDP envelope')
    atyp = data[3]
    if atyp == 1:
        end = 8
        source = socket.inet_ntop(socket.AF_INET, data[4:end])
    elif atyp == 4 and len(data) >= 22:
        end = 20
        source = socket.inet_ntop(socket.AF_INET6, data[4:end])
    elif atyp == 3:
        end = 5 + data[4]
        if len(data) < end + 2:
            raise ValueError('Invalid domain envelope')
        source = data[5:end].decode('ascii')
    else:
        raise ValueError('Unsupported response address')
    if len(data) < end + 2 or source != DNS_ADDRESS or struct.unpack('!H', data[end:end + 2])[0] != DNS_PORT:
        raise ValueError('Unexpected DNS response source')
    return data[end + 2:]


def observed_physical(pid, physical_ip, endpoint):
    sockets = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(pid), '-iTCP', '-Fn'],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=1)
    local = ('n' + physical_ip + ':').encode()
    peer = ('->' + endpoint[0] + ':' + str(endpoint[1])).encode()
    return any(local in line and peer in line for line in sockets.stdout.splitlines())


def probe(endpoint, outbound, directory, physical_ip):
    port = endpoint[1]
    child = control = datagram = None
    config_path = directory / ('config-' + str(port) + '.json')
    result = {'address': endpoint[0], 'port': port, 'core': '26.9.9',
              'interface': 'en0', 'physicalSourceObserved': False}
    try:
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            socks_port = reservation.getsockname()[1]
        config = e.single_config(outbound, socks_port)
        config['inbounds'][0]['settings']['udp'] = True
        config['outbounds'][0].setdefault('streamSettings', {}).setdefault('sockopt', {})['interface'] = 'en0'
        e.write_private(config_path, config)
        with e.private_output(directory / ('engine-' + str(port) + '.log')) as log:
            check = subprocess.run([str(ENGINE), 'run', '-test', '-c', str(config_path)],
                                   stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   env=e.ENV, timeout=6, check=False)
            if check.returncode:
                return dict(result, status='uncovered', reason='engine_config_rejected')
            child = subprocess.Popen([str(ENGINE), 'run', '-c', str(config_path)],
                                     stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     env=e.ENV, start_new_session=True)
            ready_deadline = time.monotonic() + 5
            while time.monotonic() < ready_deadline and child.poll() is None:
                try:
                    control = socket.create_connection(('127.0.0.1', socks_port), timeout=0.2)
                    break
                except OSError:
                    time.sleep(0.05)
            if control is None or child.poll() is not None:
                return dict(result, status='uncovered', reason='local_listener_unavailable')
            start = time.monotonic()
            deadline = start + DEADLINE_SECONDS
            control.sendall(b'\x05\x01\x00')
            if exact(control, 2, deadline) != b'\x05\x00':
                return dict(result, status='uncovered', reason='socks_negotiation_rejected')
            datagram = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            datagram.bind(('127.0.0.1', 0))
            request = b'\x05\x03\x00\x01' + socket.inet_aton('127.0.0.1') + struct.pack('!H', datagram.getsockname()[1])
            control.sendall(request)
            header = exact(control, 4, deadline)
            if header[:3] != b'\x05\x00\x00':
                return dict(result, status='uncovered', reason='udp_associate_rejected')
            relay = address_from(control, header[3], deadline)
            relay_port = struct.unpack('!H', exact(control, 2, deadline))[0]
            if relay == '0.0.0.0' or relay == 'localhost':
                relay = '127.0.0.1'
            if not ipaddress.ip_address(relay).is_loopback or relay_port == 0:
                return dict(result, status='uncovered', reason='nonlocal_udp_relay_refused')
            if ':' in relay:
                return dict(result, status='uncovered', reason='ipv6_local_relay_not_supported')
            transaction = os.urandom(2)
            dns = transaction + struct.pack('!5H', 0x0100, 1, 0, 0, 0) + QUESTION
            envelope = b'\x00\x00\x00\x01' + socket.inet_aton(DNS_ADDRESS) + struct.pack('!H', DNS_PORT)
            datagram.connect((relay, relay_port))
            datagram.send(envelope + dns)
            data = None
            while time.monotonic() < deadline:
                result['physicalSourceObserved'] |= observed_physical(child.pid, physical_ip, endpoint)
                datagram.settimeout(min(0.15, max(0.001, deadline - time.monotonic())))
                try:
                    data = datagram.recv(4096)
                    break
                except socket.timeout:
                    pass
            if data is None:
                return dict(result, status='failed', reason='dns_udp_timeout')
            response = unwrap_reply(data)
            if len(response) < 12 or response[:2] != transaction:
                return dict(result, status='failed', reason='invalid_dns_response')
            flags, questions, answers, _, _ = struct.unpack('!5H', response[2:12])
            if not flags & 0x8000 or questions != 1 or response[12:12 + len(QUESTION)] != QUESTION:
                return dict(result, status='failed', reason='unexpected_dns_response')
            if flags & 0x000f:
                return dict(result, status='failed', reason='dns_rcode_' + str(flags & 0x000f))
            if not answers:
                return dict(result, status='failed', reason='dns_no_answer')
            return dict(result, status='passed', reason='dns_udp_answer', answers=answers,
                        elapsedMs=round((time.monotonic() - start) * 1000))
    except (TimeoutError, socket.timeout):
        return dict(result, status='failed', reason='dns_udp_timeout')
    except (OSError, ValueError, ConnectionError, subprocess.TimeoutExpired):
        return dict(result, status='uncovered', reason='local_probe_or_protocol_error')
    finally:
        for stream in (datagram, control):
            if stream is not None:
                stream.close()
        e.stop(child)
        if config_path.exists():
            config_path.unlink()


def main():
    assert len(sys.argv) == 1, 'This probe has no configurable destinations'
    assert DIGEST == EXPECTED_DIGEST and reference.TARGETS == TARGETS
    e.pinned_engine(ENGINE, EXPECTED_DIGEST)
    documents = e.protected_json(ROOT / 'subscription.private')
    physical_ip = subprocess.check_output(['/usr/sbin/ipconfig', 'getifaddr', 'en0'], text=True).strip()
    assert ipaddress.ip_address(physical_ip).version == 4
    found = {}
    for document in documents:
        for outbound in document.get('outbounds', []):
            endpoint, reason = e.endpoint(outbound)
            if not reason and (endpoint['address'], endpoint['port']) in TARGETS:
                found.setdefault((endpoint['address'], endpoint['port']), copy.deepcopy(outbound))
    assert set(found) == TARGETS
    directory = Path(tempfile.mkdtemp(prefix='udp-core2699-', dir=ROOT))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(probe, endpoint, outbound, directory, physical_ip)
                   for endpoint, outbound in sorted(found.items())]
        results = [future.result() for future in futures]
    e.write_private(directory / 'result.json', results)
    print(json.dumps({'results': results, 'private_result': str(directory / 'result.json')}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('UDP diagnostic stopped:', type(error).__name__, '; credentials and payload suppressed', file=sys.stderr)
        raise SystemExit(2)
