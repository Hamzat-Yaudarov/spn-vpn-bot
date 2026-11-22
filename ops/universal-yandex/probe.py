"""Verified TLS/CDN and real authenticated XHTTP checks; no new subscribers."""
import concurrent.futures
import copy
from datetime import datetime, timezone
import hashlib
import http.client
import importlib.util
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time
import uuid


HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('yandex_existing_vpn_probe', HERE.parent / 'universal-reality/vpn_probe.py')
p = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p)
ProbeError = p.ProbeError


def addresses(domain):
    try:
        rows = socket.getaddrinfo(domain, 443, socket.AF_INET, socket.SOCK_STREAM)
        values = sorted({row[4][0] for row in rows})
        p.require(values and all(ipaddress.IPv4Address(ip).is_global and not ipaddress.IPv4Address(ip).is_multicast
                                 for ip in values), 'CDN-домен не разрешается в публичный IPv4.')
        return values
    except (OSError, ValueError):
        raise ProbeError('Не удалось получить публичный IPv4 CDN-домена; проверь DNS и дождись распространения.') from None


def health(domain, ip, code, *, require_h2=False):
    p.public_endpoint(ip)
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    try:
        if require_h2:
            context.set_alpn_protocols(['h2'])
            with socket.create_connection((ip, 443), timeout=6) as stream:
                with context.wrap_socket(stream, server_hostname=domain) as tls:
                    p.require(tls.selected_alpn_protocol() == 'h2', 'CDN не согласовал HTTP/2 (h2).')
        # Use a distinct HTTP/1.1 connection for the bounded health request.
        context.set_alpn_protocols(['http/1.1'])
        with socket.create_connection((ip, 443), timeout=6) as stream:
            with context.wrap_socket(stream, server_hostname=domain) as tls:
                tls.sendall(('GET /health HTTP/1.1\r\nHost: ' + domain + '\r\nConnection: close\r\n'
                             'Cache-Control: no-cache\r\n\r\n').encode())
                reply = http.client.HTTPResponse(tls)
                reply.begin()
                body = reply.read(4097)
                p.require(reply.status == 200 and len(body) <= 4096
                          and json.loads(body) == {'status': 'ok', 'deployment': code},
                          'HTTPS /health не подтверждает нужный origin; проверь источник CDN и Host/SNI.')
                age = reply.getheader('Age', '0')
                p.require(age.strip() in ('', '0') and 'HIT' not in reply.getheader('X-Cache', '').upper(),
                          'CDN кеширует /health; отключи кеш CDN и браузерный кеш.')
        return {'passed': True, 'domain': domain, 'ip': ip, 'tlsVerified': True, 'h2Verified': require_h2,
                'deployment': code}
    except ProbeError:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise ProbeError('Не удалось проверить HTTPS CDN: проверь CNAME раздачи, выпущенный сертификат CDN и HTTPS/Host/SNI источника.') from None


def _client_config(plan, source, user, local_port, edge_ip, interface, route, endpoint_domain):
    try:
        p.require(isinstance(user, str) and str(uuid.UUID(user)) == user, 'Некорректный UUID проверочной подписки.')
    except (ValueError, TypeError, AttributeError):
        raise ProbeError('Некорректный UUID проверочной подписки; значение скрыто.') from None
    p.public_endpoint(edge_ip)
    p.require(route in ('physical', 'system'), 'Для CDN допустимы только physical или system, не SSH-подмена.')
    p.validate_interface(interface, physical=route == 'physical')
    stream = {'network': 'xhttp', 'security': 'tls',
              'tlsSettings': {'serverName': endpoint_domain, 'allowInsecure': False,
                              'alpn': ['h2'], 'fingerprint': source['fingerprint']},
              'xhttpSettings': {'host': endpoint_domain, 'path': plan['path'], 'mode': 'packet-up',
                                'extra': copy.deepcopy(source['client'])}}
    if route == 'physical':
        stream['sockopt'] = {'interface': interface}
    return {'log': {'loglevel': 'error', 'access': 'none'},
            'inbounds': [{'tag': 'local-test', 'listen': '127.0.0.1', 'port': local_port,
                          'protocol': 'socks', 'settings': {'auth': 'noauth', 'udp': False}}],
            'outbounds': [{'tag': 'cdn-test', 'protocol': 'vless',
                           'settings': {'vnext': [{'address': edge_ip, 'port': 443,
                                                  'users': [{'id': user, 'encryption': 'none'}]}]},
                           'streamSettings': stream}]}


def client_config(plan, source, user, local_port, edge_ip, interface, route):
    return _client_config(plan, source, user, local_port, edge_ip, interface, route, plan['cdnDomain'])


def _validate_route(interface, route):
    p.require(route in ('physical', 'system'), 'Проверка CDN/origin не может быть подменена SSH.')
    p.validate_interface(interface, physical=route == 'physical')
    p.require(route != 'physical' or p.platform.system() == 'Darwin',
              'Физическая проверка CDN/origin рассчитана на Mac; на контроллере выберите system.')


def _run_endpoint(plan, source, subscription_file, *, endpoint_domain, endpoint_ip,
                  engine=None, interface='en0', route='physical'):
    # Both proof scopes use the identical authenticated XHTTP engine lifecycle.
    # Only the explicitly selected TCP address and TLS/HTTP name are different.
    _validate_route(interface, route)
    edge_ip = endpoint_ip
    p.public_endpoint(edge_ip)
    tls = health(endpoint_domain, edge_ip, plan['code'], require_h2=True)
    tcp = p.physical_tcp_preflight(edge_ip, interface) if route == 'physical' else None
    selected = p.select_engine(engine)
    user = p.fetch_credential(subscription_file, interface)
    local_source = None
    if route == 'physical':
        try:
            local_source = subprocess.check_output(['/usr/sbin/ipconfig', 'getifaddr', interface], text=True, timeout=3).strip()
            p.require(not ipaddress.IPv4Address(local_source).is_loopback, 'Нет физического IPv4 для проверки.')
        except (subprocess.SubprocessError, ValueError):
            raise ProbeError('Не удалось определить физический IPv4 Mac.') from None
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0)); port = reservation.getsockname()[1]
    config = _client_config(plan, source, user, port, edge_ip, interface, route, endpoint_domain)
    child = None
    try:
        temporary_root = '/private/tmp' if p.platform.system() == 'Darwin' else None
        with tempfile.TemporaryDirectory(prefix='spn-yandex-vpn-test-', dir=temporary_root) as temporary:
            directory = Path(temporary)
            p.e.write_private(directory / 'config.json', config)
            with p.e.private_output(directory / 'engine.log') as log:
                checked = subprocess.run([str(selected), 'run', '-test', '-c', str(directory / 'config.json')],
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=p.e.ENV, timeout=12)
                p.require(checked.returncode == 0, 'Xray не принял XHTTP GET/header; журнал и ключи скрыты.')
                child = subprocess.Popen([str(selected), 'run', '-c', str(directory / 'config.json')],
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                         env=p.e.ENV, start_new_session=True)
                ready = False
                for _ in range(100):
                    if child.poll() is not None:
                        break
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.1):
                            ready = True
                        break
                    except OSError:
                        time.sleep(.05)
                p.require(ready, 'Изолированный XHTTP-клиент не запустился.')
                observed, rows = False, []
                for number in range(7):
                    mode = 'trace' if number == 5 else 'body' if number == 6 else '204'
                    if route == 'physical' and number in (0, 6):
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                            pending = pool.submit(p.request, port, child, plan['ip'], number, mode)
                            time.sleep(.12)
                            observed = p.physical_observed(child.pid, local_source, edge_ip) or observed
                            rows.append(pending.result())
                    else:
                        rows.append(p.request(port, child, plan['ip'], number, mode))
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    rows.extend(pool.map(lambda n: p.request(port, child, plan['ip'], n), (7, 8)))
                passed = len(rows) == 9 and all(r['passed'] and r['clientAlive'] for r in rows) \
                         and (route != 'physical' or observed)
                return {'passed': passed, 'tests': len(rows), 'results': rows, 'transport': 'xhttp-get-header',
                        'domain': endpoint_domain, 'edgeIp': edge_ip, 'expectedExitIp': plan['ip'],
                        'testRoute': route, 'directReachabilityVerified': passed and route == 'physical',
                        'physicalSourceObserved': observed, 'tls': tls, 'physicalTcp': tcp,
                        'fingerprint': source['fingerprint'], 'checkedAt': datetime.now(timezone.utc).isoformat()}
    except ProbeError:
        raise
    except Exception:
        raise ProbeError('XHTTP VPN-тест остановлен; подписка, UUID и приватные журналы скрыты.') from None
    finally:
        p.e.stop(child)


def run(plan, source, subscription_file, *, engine=None, interface='en0', route='physical'):
    """Verify the public CDN endpoint and its real authenticated exit traffic."""
    _validate_route(interface, route)
    edge_ip = addresses(plan['cdnDomain'])[0]
    return _run_endpoint(plan, source, subscription_file, endpoint_domain=plan['cdnDomain'],
                         endpoint_ip=edge_ip, engine=engine, interface=interface, route=route)


def run_origin(plan, source, subscription_file, *, engine=None, interface='lo', route='system'):
    """Verify the new origin directly before DNS cutover; never prove the CDN."""
    _validate_route(interface, route)
    p.public_endpoint(plan['ip'])
    result = _run_endpoint(plan, source, subscription_file, endpoint_domain=plan['domain'],
                           endpoint_ip=plan['ip'], engine=engine, interface=interface, route=route)
    # A direct origin proof must never be accepted as the later public CDN proof.
    result['originIp'] = result.pop('edgeIp')
    return {**result, 'scope': 'origin-direct', 'cdnVerified': False}


def verify_export(plan, source, subscription_file, *, interface='en0'):
    documents = p.fetch_documents(subscription_file, interface)
    expected_user = p.credential(documents)
    docs = [documents] if isinstance(documents, dict) else documents
    candidates = []
    for document in docs:
        for out in document.get('outbounds', []):
            for server in out.get('settings', {}).get('vnext', []):
                if server.get('address') == plan['cdnDomain'] and server.get('port') == 443:
                    candidates.append(out)
    unique = {json.dumps(out, sort_keys=True, separators=(',', ':')): out for out in candidates}
    p.require(len(unique) == 1, 'В свежей подписке нет однозначного нового CDN-хоста.')
    out = next(iter(unique.values()))
    try:
        endpoint, reason = p.e.endpoint(out)
        p.require(reason is None and endpoint['address'] == plan['cdnDomain'] and endpoint['port'] == 443,
                  'Выгрузка содержит неоднозначный или зависимый CDN endpoint.')
        stream = out['streamSettings']; tls = stream['tlsSettings']; xhttp = stream['xhttpSettings']
        user = out['settings']['vnext'][0]['users'][0]
        extras = xhttp.get('extra', xhttp)
        if isinstance(extras, str):
            extras = json.loads(extras)
        allowed = set(source['client']) if 'extra' in xhttp else set(source['client']) | {'host', 'path', 'mode'}
        p.require(isinstance(extras, dict) and set(extras) <= allowed,
                  'Выгрузка добавила непроверенные XHTTP-параметры или отдельный download endpoint.')
        p.require(out['protocol'] == 'vless' and stream['network'] == 'xhttp' and stream['security'] == 'tls'
                  and tls.get('serverName') == plan['cdnDomain'] and tls.get('fingerprint') == source['fingerprint']
                  and tls.get('allowInsecure', False) is False and 'h2' in tls.get('alpn', [])
                  and xhttp.get('host') == plan['cdnDomain'] and xhttp.get('path') == plan['path']
                  and xhttp.get('mode', 'auto') in ('auto', 'packet-up')
                  and all(extras.get(key) == value for key, value in source['client'].items())
                  and str(uuid.UUID(user['id'])) == expected_user and user.get('encryption') == 'none'
                  and user.get('flow', '') == '',
                  'Выгрузка нового хоста изменила TLS/HTTP2/XHTTP GET/header; хост не подтверждён.')
    except (KeyError, IndexError, TypeError, ValueError):
        raise ProbeError('Не удалось сверить параметры нового CDN-хоста в подписке; ответ скрыт.') from None
    return {'passed': True, 'domain': plan['cdnDomain'], 'transport': 'xhttp-get-header',
            'exportVerified': True, 'checkedAt': datetime.now(timezone.utc).isoformat()}
