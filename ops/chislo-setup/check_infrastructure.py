"""Unauthenticated infrastructure checks. Never uses subscriber credentials."""
from __future__ import annotations

import base64
import gzip
import hashlib
import http.client
import json
from pathlib import Path
import secrets
import socket
import ssl

import certifi

STATE = Path('/private/tmp/wayspn-chislo-setup')
PUBLIC = json.loads((STATE / 'public.json').read_text())


def connect(port: int, hostname: str, address: str = '31.76.50.87', alpn=None):
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.set_alpn_protocols(alpn or ['http/1.1'])
    return ctx.wrap_socket(socket.create_connection((address, port), timeout=8), server_hostname=hostname)


def get(path: str, hostname: str = 'chislo.site', address: str = '31.76.50.87'):
    with connect(443, hostname, address) as sock:
        sock.sendall(f'GET {path} HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n'.encode())
        response = http.client.HTTPResponse(sock)
        response.begin()
        body = response.read()
        # VK may serve an already-cached gzip representation even without an
        # Accept-Encoding header. Compare decoded assets, not wire encodings.
        encoding = response.getheader('Content-Encoding', '').lower()
        if encoding == 'gzip':
            body = gzip.decompress(body)
        elif encoding not in ('', 'identity'):
            raise AssertionError('Unsupported response content encoding: ' + encoding)
        return response.status, dict(response.getheaders()), body


def check():
    status, headers, body = get('/')
    assert status == 200 and b'CHISLO' in body
    print('OK origin HTTPS site and trusted certificate')
    for asset in ('site.css', 'mark.svg', 'orbit.svg', 'balance.svg', 'rhythm.svg'):
        assert get('/assets/' + asset)[0] == 200
    print('OK all five static assets')
    with connect(443, 'chislo.site') as sock:
        nonce = base64.b64encode(secrets.token_bytes(16)).decode()
        sock.sendall((f'GET /direct/ws/ HTTP/1.1\r\nHost: chislo.site\r\n'
                      f'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                      f'Sec-WebSocket-Key: {nonce}\r\n\r\n').encode())
        response = b''
        while b'\r\n\r\n' not in response and len(response) < 16384:
            chunk = sock.recv(4096)
            assert chunk, 'WS connection closed before headers'
            response += chunk
        assert response.startswith(b'HTTP/1.1 101 '), response.split(b'\r\n')[0]
        accept = base64.b64encode(hashlib.sha1((nonce + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        response_headers = dict(line.split(b':', 1) for line in response.split(b'\r\n')[1:] if b':' in line)
        response_headers = {k.lower(): v.strip() for k, v in response_headers.items()}
        assert response_headers[b'sec-websocket-accept'] == accept
    print('OK external WS 101 and correct WebSocket acceptance')
    with connect(2083, 'www.chislo.site', alpn=['h2']) as sock:
        assert sock.selected_alpn_protocol() == 'h2'
        assert sock.version() == 'TLSv1.3'
    print('OK external gRPC REALITY fallback: trusted TLS 1.3, ALPN h2')
    status, headers, _ = get(PUBLIC['edgePath'])
    assert status in (400, 404), status
    assert headers.get('Cache-Control') == 'no-store'
    print(f'OK origin XHTTP proxy: {status} for intentionally incomplete request, no-store')
    # A local VPN/TUN can acknowledge TCP before the remote SYN completes.
    # Require a TLS server response, not just a successful local TCP connect.
    probe_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    probe_context.check_hostname = False
    probe_context.verify_mode = ssl.CERT_NONE  # ONLY diagnostic API probe, not VPN settings.
    try:
        with socket.create_connection(('31.76.50.87', 2222), timeout=3) as raw:
            with probe_context.wrap_socket(raw, server_hostname='31.76.50.87'):
                raise AssertionError('Node API unexpectedly exposed to non-panel source')
    except (TimeoutError, ConnectionRefusedError, ConnectionResetError, ssl.SSLEOFError):
        print('OK no node API TLS response from this source; confirmed UFW permits panel only')
    # CDN can still be awaiting the owner-created resource, certificate or DNS propagation.
    edge = '178.176.128.203'
    try:
        status, _, cdn_body = get('/', 'cdn.chislo.site', edge)
        assert status == 200, f'CDN root status {status}'
        assert b'CHISLO' in cdn_body, 'CDN is not serving the CHISLO gallery'
        stale = []
        if cdn_body != body:
            stale.append('/')
        for asset in ('site.css', 'mark.svg', 'orbit.svg', 'balance.svg', 'rhythm.svg'):
            origin = get('/assets/' + asset)[2]
            cdn_status, _, delivered = get('/assets/' + asset, 'cdn.chislo.site', edge)
            assert cdn_status == 200 and delivered, f'CDN asset missing: {asset}'
            if origin != delivered:
                stale.append('/assets/' + asset)
        status, headers, _ = get(PUBLIC['edgePath'], 'cdn.chislo.site', edge)
        assert status in (400, 404), f'CDN tunnel path status {status}'
        assert headers.get('Cache-Control') == 'no-store', headers.get('Cache-Control')
        print('OK VK CDN trusted TLS, gallery/assets and uncached XHTTP proxy')
        if stale:
            print('CDN static-cache warning (not a tunnel failure):', ', '.join(stale))
        return True
    except (OSError, AssertionError) as exc:
        print('CDN check failed from this client:', type(exc).__name__, str(exc))
        return False


if __name__ == '__main__':
    ready = check()
    print('Real subscriber/app connectivity still requires an owner app test; no subscriber keys used.')
    print('CDN_INFRASTRUCTURE_READY=' + str(ready))
