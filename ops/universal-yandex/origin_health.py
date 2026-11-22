#!/usr/bin/env python3
"""Verify the assigned origin locally over authenticated SSH, not through CDN.

No certificate bypass, DNS dependency, credentials or engine/subscriber access.
This proves only the origin's local TLS/Host/deployment identity; it does not
prove that Yandex or a client network can reach the origin.
"""
import argparse
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
import time


class OriginError(RuntimeError):
    pass


def need(ok, message):
    if not ok:
        raise OriginError(message)


def check(domain, ip, code):
    need(isinstance(domain, str) and len(domain) <= 253 and len(domain.split('.')) >= 2
         and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in domain.split('.')),
         'Invalid origin hostname')
    try:
        address = ipaddress.IPv4Address(ip)
        need(str(address) == ip and address.is_global and not address.is_multicast, 'Invalid public origin IPv4')
    except (ValueError, TypeError):
        raise OriginError('Invalid public origin IPv4') from None
    need(isinstance(code, str) and re.fullmatch('[a-z0-9]{6}', code), 'Invalid origin deployment code')
    try:
        context = ssl.create_default_context()
        context.set_alpn_protocols(['http/1.1'])
        with socket.create_connection((ip, 443), timeout=5) as stream:
            with context.wrap_socket(stream, server_hostname=domain) as tls:
                tls.sendall(('GET /health HTTP/1.1\r\nHost: ' + domain + '\r\nConnection: close\r\n'
                             'Cache-Control: no-cache\r\n\r\n').encode('ascii'))
                reply = http.client.HTTPResponse(tls)
                reply.begin()
                body = reply.read(4097)
                need(reply.status == 200 and len(body) <= 4096
                     and json.loads(body) == {'status': 'ok', 'deployment': code},
                     'Origin /health does not match this deployment')
        return {'passed': True, 'scope': 'origin-local-ssh', 'domain': domain, 'ip': ip,
                'deployment': code, 'tlsVerified': True, 'cdnVerified': False}
    except OriginError:
        raise
    except ssl.SSLCertVerificationError as error:
        number = getattr(error, 'verify_code', 0)
        raise OriginError('Origin certificate verification failed (code ' + str(number) + '); TLS checking remains enabled') from None
    except ssl.SSLError:
        raise OriginError('Origin TLS handshake failed locally; check its certificate and SNI configuration') from None
    except (TimeoutError, socket.timeout):
        raise OriginError('Origin HTTPS timed out on the VPS itself; check Nginx and its assigned-IP listener') from None
    except (OSError, http.client.HTTPException):
        raise OriginError('Cannot connect to origin HTTPS locally; check Nginx, not the CDN resource') from None
    except (ValueError, UnicodeError):
        raise OriginError('Origin returned invalid /health JSON') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain', required=True)
    parser.add_argument('--ip', required=True)
    parser.add_argument('--code', required=True)
    args = parser.parse_args()
    for attempt in range(3):
        try:
            result = check(args.domain, args.ip, args.code)
            print(json.dumps(result, separators=(',', ':')))
            return
        except OriginError:
            if attempt == 2:
                raise
            time.sleep(1)


if __name__ == '__main__':
    try:
        main()
    except OriginError as error:
        print('Stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
