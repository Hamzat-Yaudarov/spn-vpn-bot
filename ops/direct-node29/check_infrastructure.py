"""Keyless checks; no subscription links or subscriber UUIDs are accessed."""
import base64
import hashlib
import http.client
import secrets
import socket
import ssl

import certifi

IP = '64.188.78.29'
DOMAIN = 'node29.prestizh.site'


def tls(port, alpn):
    context = ssl.create_default_context(cafile=certifi.where())
    context.set_alpn_protocols(alpn)
    return context.wrap_socket(socket.create_connection((IP, port), timeout=8), server_hostname=DOMAIN)


def check():
    with tls(443, ['http/1.1']) as s:
        s.sendall(f'GET /health HTTP/1.1\r\nHost: {DOMAIN}\r\nConnection: close\r\n\r\n'.encode())
        response = http.client.HTTPResponse(s)
        response.begin()
        assert response.status == 200 and response.read().strip() == b'{"status":"ok"}'
    print('PASS trusted certificate and HTTPS health endpoint')
    with tls(443, ['http/1.1']) as s:
        nonce = base64.b64encode(secrets.token_bytes(16)).decode()
        s.sendall((f'GET /direct/ws/ HTTP/1.1\r\nHost: {DOMAIN}\r\n'
                   'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                   f'Sec-WebSocket-Key: {nonce}\r\n\r\n').encode())
        headers = b''
        while b'\r\n\r\n' not in headers and len(headers) < 16384:
            data = s.recv(4096)
            assert data, 'WS closed before upgrade'
            headers += data
        assert headers.startswith(b'HTTP/1.1 101 '), headers.split(b'\r\n')[0]
        parsed = {k.lower():v.strip() for k,v in (line.split(b':', 1) for line in headers.split(b'\r\n')[1:] if b':' in line)}
        expected = base64.b64encode(hashlib.sha1((nonce + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        assert parsed[b'sec-websocket-accept'] == expected
    print('PASS external WS Upgrade 101 and correct WebSocket acceptance')
    with tls(2083, ['h2']) as s:
        assert s.version() == 'TLSv1.3' and s.selected_alpn_protocol() == 'h2'
    print('PASS external gRPC REALITY unauthenticated fallback, TLS 1.3 and h2')
    probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    probe.check_hostname = False
    probe.verify_mode = ssl.CERT_NONE  # Diagnostic firewall probe only, never VPN settings.
    try:
        with socket.create_connection((IP, 2222), timeout=3) as raw:
            with probe.wrap_socket(raw, server_hostname=DOMAIN):
                raise AssertionError('Node API reachable from a non-panel source')
    except (TimeoutError, ConnectionRefusedError, ConnectionResetError, ssl.SSLEOFError):
        print('PASS no node API TLS response from non-panel source')
    print('Subscriber traffic not tested: confirm all three in Happ/INCY after refreshing.')


if __name__ == '__main__':
    check()
