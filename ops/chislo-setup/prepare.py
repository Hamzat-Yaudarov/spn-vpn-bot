"""Prepare CHISLO deployment artifacts. All Remnawave requests are GET-only.

The generated profile and node environment are private and stay outside Git.
"""
from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.prepare_direct_transports import api

STATE = Path('/private/tmp/wayspn-chislo-setup')
DOMAIN = 'chislo.site'
IP = '31.76.50.87'
PANEL_IP = '31.77.202.66'


def write_private(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(value)


def publish_handoff() -> None:
    target = ROOT / 'ops/chislo-setup/private'
    target.mkdir(mode=0o700, exist_ok=True)
    os.chmod(target, 0o700)
    for name in ('profile.json', 'public.json', 'xhttp-extra.json', 'НАСТРОЙКА-REMNAWAVE.md'):
        destination = target / name
        if destination.exists():
            if destination.read_bytes() != (STATE / name).read_bytes():
                raise RuntimeError('Existing private handoff differs; do not overwrite blindly.')
        else:
            shutil.copyfile(STATE / name, destination)
        os.chmod(destination, 0o600)


def prepare() -> None:
    os.umask(0o077)
    STATE.mkdir(mode=0o700, exist_ok=True)
    if (STATE / 'ready.json').exists():
        publish_handoff()
        print('CHISLO artifacts already prepared; preserving keys and paths.')
        return
    if any(STATE.iterdir()):
        raise RuntimeError('Incomplete existing state; inspect it before continuing.')

    profiles = api('GET', '/config-profiles')['configProfiles']
    source = next(p for p in profiles if p['name'] == 'VK_CDN_TEKST')
    if any(p['name'] == 'VK_CDN_CHISLO' for p in profiles):
        raise RuntimeError('CHISLO profile already exists in panel; refuse duplicate keys.')
    config = copy.deepcopy(source['config'])
    assert len(config['inbounds']) == 4
    assert {i['streamSettings']['network'] for i in config['inbounds']} == {'xhttp', 'grpc', 'ws', 'hysteria'}
    assert all(i['settings']['clients'] == [] for i in config['inbounds'])
    # Keep the working settings; replace domain/tag identities only.
    config = json.loads(json.dumps(config).replace('tekst.site', DOMAIN).replace('-tekst', '-chislo'))
    key = X25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    b64 = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
    short_id = secrets.token_hex(8)
    edge_path = '/edge/' + secrets.token_hex(12) + '/'
    for inbound in config['inbounds']:
        stream = inbound['streamSettings']
        if stream['network'] == 'xhttp':
            stream['xhttpSettings']['path'] = edge_path
        if stream['security'] == 'reality':
            stream['realitySettings']['privateKey'] = b64(raw)
            stream['realitySettings']['shortIds'] = [short_id]
            # Same TLS site on this VPS, without a public-DNS dependency in fallback.
            stream['realitySettings']['target'] = '127.0.0.1:443'
    secret = api('GET', '/keygen')['secretKey']
    assert isinstance(secret, str) and len(secret) > 20 and not re.search(r'\s', secret)
    write_private(STATE / 'profile.json', json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    write_private(STATE / 'node.env', 'NODE_PORT=2222\nSECRET_KEY=' + secret + '\n')
    template = (ROOT / 'ops/chislo-setup/nginx.conf.template').read_text()
    write_private(STATE / 'chislo.site.conf', template.replace('__EDGE_PATH__', edge_path))
    info = {'address': IP, 'domain': DOMAIN, 'panelIp': PANEL_IP,
            'publicKey': b64(public), 'shortId': short_id, 'edgePath': edge_path,
            'profileName': 'VK_CDN_CHISLO', 'nodeName': 'node-chislo-50.87'}
    write_private(STATE / 'public.json', json.dumps(info, indent=2) + '\n')
    extra = copy.deepcopy(next(i for i in config['inbounds'] if i['streamSettings']['network'] == 'xhttp')['streamSettings']['xhttpSettings'])
    for field in ('host', 'mode', 'path'):
        extra.pop(field, None)
    write_private(STATE / 'xhttp-extra.json', json.dumps(extra, indent=2) + '\n')
    instructions = f'''# CHISLO: настройка Remnawave

Панель автоматически не изменялась. На сервере установлен Remnawave Node.
Действующих пользовательских ключей в подготовленном профиле нет.

## 1. Профиль

Создайте отдельный профиль **VK_CDN_CHISLO** и вставьте весь JSON из `profile.json`.
Не заменяйте существующие профили. Оставьте `clients: []`: пользователей добавляет панель.
В профиле ровно четыре инбаунда:
`vless-xhttp-vk-chislo`, `ordinary-grpc-reality-chislo`,
`ordinary-ws-chislo`, `ordinary-hy2-chislo`.

`profile.json` содержит приватный REALITY-ключ. Не публикуйте его и не добавляйте в Git.

## 2. Нода

Имя: **node-chislo-50.87**. Адрес: **31.76.50.87**. Порт ноды: **2222**.
Выберите профиль **VK_CDN_CHISLO** и все четыре его инбаунда.
Страну выберите по фактической локации VPS в кабинете хостинга.
Повторно устанавливать Docker или выполнять установочный compose из панели не нужно:
контейнер уже подготовлен для этой панели.

После сохранения дождитесь Connected / Running. Если появится ошибка сертификата
или запуска Xray, не включайте хосты: пришлите текст ошибки без секретных ключей.

## 3. Четыре хоста

Каждый хост привяжите только к этой новой ноде и соответствующему инбаунду.
На время первой проверки оставьте их выключенными для остальных пользователей.

### CHISLO • ОБХОД

- Инбаунд: `vless-xhttp-vk-chislo`.
- Адрес: `cdn.chislo.site`. Порт: **443**.
- Security Layer: **TLS**. SNI и Host: `cdn.chislo.site`.
- Path: `{edge_path}`. ALPN: **h2**. Fingerprint: **firefox**.
- Режим XHTTP: **packet-up** (унаследовать из инбаунда).
- Дополнительные параметры XHTTP: вставьте JSON из `xhttp-extra.json`.
- Не включайте Allow Insecure. Flow не задавайте.

### CHISLO • gRPC

- Инбаунд: `ordinary-grpc-reality-chislo`.
- Адрес: **31.76.50.87**. Порт: **2083**.
- Security Layer: **DEFAULT / из инбаунда** — итоговый клиент должен получить REALITY.
- SNI: `www.chislo.site`. ALPN: **h2**. Fingerprint: **random**, как на TEKST.
- gRPC Service Name / Path: **grpc**. HTTP Host оставьте пустым.
- REALITY Public Key: `{b64(public)}`.
- REALITY Short ID: `{short_id}`.
- Flow оставьте пустым; **xtls-rprx-vision не включать**.
- При DEFAULT панель должна взять Public Key и Short ID из профиля.
  Если ваша версия показывает эти поля вручную, используйте значения выше.

### CHISLO • Hysteria2

- Инбаунд: `ordinary-hy2-chislo`.
- Адрес: **31.76.50.87**. Порт: **8443** (UDP).
- Security Layer: **TLS**. SNI: `www.chislo.site`. ALPN: **h3**.
- Fingerprint: **random**, как на TEKST; Host и Path пустые.
- Не включайте Allow Insecure. Пароль/ID пользователя отдельно не придумывайте:
  его выдаёт Remnawave для выбранной подписки.

### CHISLO • WS

- Инбаунд: `ordinary-ws-chislo`.
- Адрес: `chislo.site`. Порт: **443**.
- Security Layer: **TLS**. SNI и Host: `chislo.site`.
- Path: **/direct/ws/**. ALPN: **http/1.1**. Fingerprint: **firefox**.
- Flow пустой. Не включайте Allow Insecure и WS Early Data для первой проверки.

## 4. Доступ подписки

Добавьте новые инбаунды сначала в ваш тестовый Internal Squad и проверьте,
что тестовая подписка состоит в нём. Выберите этот Squad в видимости хостов,
если ваша версия панели использует дополнительный фильтр.
Включите хосты для теста, обновите подписку в Happ/INCY и проверьте открытие сайтов.
Только после успешного теста добавляйте подключения в пользовательские сквады.
Для CDN также требуется готовый сертификат VK и корректный CNAME.

## VK Cloud

Источник: `chislo.site`, без схемы и пути. Протокол к источнику: **HTTPS**.
Персональный домен: `cdn.chislo.site`. Host: **Кастомный → chislo.site**.
Доступ включить. Разрешить **GET, HEAD**. Сертификат CDN: **Let's Encrypt**.
Кеширование CDN/браузера, игнорирование query-параметров, сжатие, большие файлы,
Secure Token и проверки Referer для туннеля не включать.
CNAME `cdn` должен указывать на техническое имя, показанное именно этим ресурсом.

Наличие сайта и корректной конфигурации не гарантирует отсутствие блокировки VK.
'''
    write_private(STATE / 'НАСТРОЙКА-REMNAWAVE.md', instructions)
    write_private(STATE / 'ready.json', json.dumps({'source': source['name'], 'prepared': True}) + '\n')
    publish_handoff()
    print('Prepared profile, node environment, nginx and manual panel instructions in', STATE)
    print('No profiles, nodes, hosts or squads were changed in Remnawave.')


if __name__ == '__main__':
    prepare()
