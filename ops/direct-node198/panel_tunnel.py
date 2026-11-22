"""Optional fixed-endpoint panel access over a localhost-only SSH forward.

The SSH forward terminates only TCP at the approved panel. Inner HTTPS keeps
the original hostname/SNI, certificate validation and bearer token on the Mac.
No credentials are installed on the intermediary VPS.
"""
import http.client
import io
import json
import socket
import ssl

import certifi
from dotenv import dotenv_values

HOST = 'panel.wayspn.online'
PORT = 18743
PATHS = {'/hosts', '/nodes', '/config-profiles', '/internal-squads', '/keygen'}


class Connection(http.client.HTTPSConnection):
    def connect(self):
        if self.host != HOST or self.port != 443 or self._tunnel_host:
            raise ValueError('Unexpected panel TLS destination')
        raw = socket.create_connection(('127.0.0.1', PORT), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=HOST)
        except Exception:
            raw.close()
            raise


def adapter(root):
    def api(method, path, body=None):
        if path not in PATHS or method not in {'GET', 'POST', 'PATCH'}:
            raise ValueError('Outside infrastructure scope')
        lines = [line for line in (root / '.env').read_text().splitlines()
                 if line.split('=', 1)[0].strip() in {'REMNAWAVE_BASE_URL', 'REMNAWAVE_API_TOKEN'}]
        settings = dotenv_values(stream=io.StringIO('\n'.join(lines)), interpolate=False)
        if settings['REMNAWAVE_BASE_URL'].rstrip('/') != 'https://' + HOST + '/api':
            raise ValueError('Unexpected panel origin')
        token = settings['REMNAWAVE_API_TOKEN']
        if not token or any(c.isspace() for c in token):
            raise ValueError('Invalid credentials')
        context = ssl.create_default_context(cafile=certifi.where())
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers['Content-Type'] = 'application/json'
        conn = Connection(HOST, 443, timeout=25, context=context)
        try:
            conn.request(method, '/api' + path, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read(8_000_001)
            if not 200 <= response.status < 300:
                raise RuntimeError('Panel HTTP ' + str(response.status) + '; body suppressed')
            if len(raw) > 8_000_000:
                raise ValueError('Oversized panel response')
            return json.loads(raw)['response']
        finally:
            conn.close()
    return api
