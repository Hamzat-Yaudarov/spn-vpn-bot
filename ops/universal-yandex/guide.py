"""Personalized, offline-only manual Yandex CDN setup instructions.

No API, filesystem, browser, DNS or payment operation occurs while rendering.
The installer supplies the already shell-quoted command for its saved state.
"""
from __future__ import annotations

import ipaddress
import re


SOURCES = {
    'create': 'https://yandex.cloud/ru/docs/cdn/operations/resources/create-resource',
    'certificate': 'https://yandex.cloud/ru/docs/certificate-manager/operations/managed/cert-create',
    'challenge': 'https://yandex.cloud/ru/docs/certificate-manager/concepts/challenges',
    'caching': 'https://yandex.cloud/ru/docs/cdn/operations/resources/configure-caching',
    'methods': 'https://yandex.cloud/ru/docs/cdn/operations/resources/configure-http',
    'sni': 'https://yandex.cloud/ru/docs/cdn/operations/resources/configure-server-name',
    'tls': 'https://yandex.cloud/ru/docs/cdn/operations/resources/configure-tls-profile',
    'dns': 'https://yandex.cloud/ru/docs/cdn/operations/resources/get-resources-info',
    'pricing': 'https://yandex.cloud/ru/docs/cdn/pricing',
}


def _need(ok):
    if not ok:
        raise ValueError('Invalid public values for the manual CDN guide')


def _domain(value):
    _need(isinstance(value, str) and len(value) <= 253 and value == value.lower()
          and len(value.split('.')) >= 2
          and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                  for part in value.split('.')))
    return value


def _text(value):
    _need(isinstance(value, str) and 1 <= len(value) <= 128
          and all(ord(char) >= 32 and ord(char) != 127 for char in value))
    return re.sub(r'([\\`*_{}\[\]()#+.!|<>])', r'\\\1', value)


def render(plan, resume_command: str) -> str:
    """Return exact console choices for one origin, never purchase a resource."""
    _need(isinstance(plan, dict))
    try:
        origin, cdn = _domain(plan['domain']), _domain(plan['cdnDomain'])
        zone, code = _domain(plan['dns']['zone']), plan['code']
        name, path = _text(plan['name']), plan['path']
        ip = str(ipaddress.IPv4Address(plan['ip']))
        _need(ip == plan['ip'] and ipaddress.IPv4Address(ip).is_global
              and not ipaddress.IPv4Address(ip).is_multicast)
        _need(origin != cdn and all(host.endswith('.' + zone) for host in (origin, cdn)))
        _need(isinstance(code, str) and re.fullmatch('[a-z0-9]{6}', code))
        _need(isinstance(path, str) and re.fullmatch(r'/[A-Za-z0-9/_-]+/', path))
        _need(isinstance(resume_command, str) and resume_command.strip()
              and not any(char in resume_command for char in ('\n', '\r', '\x00'))
              and '```' not in resume_command)
    except (KeyError, TypeError, ipaddress.AddressValueError):
        raise ValueError('Invalid public values for the manual CDN guide') from None

    return f'''# Ручное подключение Яндекс CDN — {code}

Проект: {name}. VPS и origin подготавливает установщик. Сам CDN и его сертификат
ты создаёшь в консоли Яндекса; скрипт не создаёт платные ресурсы и не выполняет
платный финальный клик. Хост Remnawave остаётся выключенным до завершающей проверки.

## 1. Значения именно этой установки

| Назначение | Значение |
| --- | --- |
| IP VPS | `{ip}` |
| Источник — отдельный origin-домен | `{origin}` |
| Домен для клиентов CDN | `{cdn}` |
| Рекомендуемое имя CDN-ресурса | `spn-cdn-{code}` |
| Имя сертификата в Яндексе | `spn-cdn-{code}-tls` |
| DNS-зона Cloudflare | `{zone}` |
| Путь транспорта, уже настроенный на VPS | `{path}` |

Не меняй местами origin и CDN-домен. Запись A для `{origin}` должна вести на
`{ip}`, а не на CDN. В поле источника Яндекса вписывается только `{origin}` —
без `https://`, без пути `{path}` и без добавления `/health`.

## 2. Сначала выпусти сертификат для CDN-домена

1. Открой [консоль Яндекса](https://console.yandex.cloud/), выбери нужный каталог.
   Запомни его: сертификат и будущий CDN-ресурс должны находиться в одном каталоге.
2. Открой Certificate Manager и добавь сертификат от Let's Encrypt.
3. Имя: `spn-cdn-{code}-tls`. Единственный домен: `{cdn}`.
4. Выбери DNS-подтверждение через **CNAME**, не HTTP и не TXT.
5. Открой созданный сертификат и его проверку домена. Яндекс покажет имя
   `_acme-challenge.{cdn}` и значение CNAME в домене `cm.yandexcloud.net`.
   Скопируй полное значение из своей консоли, а не пример из инструкции.

Такой способ подходит для автоматического продления.
[Описание выпуска сертификата]({SOURCES['certificate']}).

В команде ниже замени `CERTIFICATE_CNAME_TARGET` скопированным значением.
Не запускай команду с буквальным текстом-заглушкой:

```bash
{resume_command} --certificate-target "CERTIFICATE_CNAME_TARGET" --apply
```

Установщик добавит в Cloudflare DNS-only CNAME
`_acme-challenge.{cdn}` → твоё значение проверки. Не создавай рядом TXT или
второй CNAME с этим же именем и не удаляй запись после выпуска: она нужна при
продлении. Если имя уже занято чужой записью, остановись и проверь конфликт,
не удаляй её вслепую. [Как работает DNS-проверка]({SOURCES['challenge']}).

Дождись статуса **Issued** у сертификата. Статусы Validating или Pending ещё не
означают, что сертификат готов для CDN. Закрывать и пересоздавать сертификат
только из-за ожидания не нужно.

## 3. Вручную создай один CDN-ресурс

Открой Cloud CDN в том же каталоге, начни создание нового ресурса.
Существующий «Германия • ОБХОД 1» не редактируй. Заполни основную часть:

| Поле / смысл настройки | Что выбрать или вписать |
| --- | --- |
| Доступ к контенту | Включён |
| Источники | Один источник |
| Тип источника | Сервер |
| Адрес источника | `{origin}` |
| Соединение CDN → источник | HTTPS, порт 443 |
| Основной домен раздачи | `{cdn}` |
| Сертификат для клиентов | Из Certificate Manager: `spn-cdn-{code}-tls`, статус Issued |
| Host при обращении к источнику | Свой: `{origin}` |
| Rewrite / переписывание пути | Выключено |
| Перенаправление клиентов | Не использовать |
| Защищённые токены, ограничения по IP и странам | Не включать |

Основной CDN-домен после создания нельзя просто переименовать — проверь его до
подтверждения. [Порядок создания ресурса]({SOURCES['create']}).

Для HTTPS к источнику также задай его **SNI = `{origin}`**. Если этого поля нет
в мастере, после создания открой настройки ресурса, раздел соединения с
источником, включи пользовательское имя сервера SNI и введи `{origin}`.
Host и SNI источника здесь не равны `{cdn}`.
[Где настраивается origin SNI]({SOURCES['sni']}).

## 4. Остальные экраны мастера

- Кеш CDN: выключить. Кеш браузера: выключить. Не подставлять принудительный TTL
  и не включать выдачу устаревших ответов. Запросы транспорта должны идти к VPS,
  а не получать предыдущие ответы из кеша.
  [Настройки кеширования]({SOURCES['caching']}).
- Gzip и другая компрессия, сегментация больших файлов: выключить.
- Не удалять query-параметры, не переписывать путь и не подменять заголовок
  `X-Transport-Data`. Не добавлять собственные заголовки запросов/ответов,
  CORS-настройки или скрытие всех заголовков источника.
- Разрешённые методы: **GET, HEAD, OPTIONS**. Не пытайся включить POST:
  он недоступен новым ресурсам по умолчанию. Здесь уже подготовлен XHTTP
  GET/header, без тела GET-запроса; GET с телом CDN может отклонять кодом 413.
  [Ограничения методов]({SOURCES['methods']}).
- В настройках TLS выбери профиль **«Защищённый (TLSv1.2+ с поддержкой PFS и AEAD)»**.
  Отдельный переключатель HTTP/2 искать не нужно: согласование HTTP/2 проверит
  установщик при завершении. [Профили TLS]({SOURCES['tls']}).
- Выгрузка логов, экранирование источника и выделенные IP: не включать.

Перед платным подтверждением проверь показанную консолью стоимость. Она
**не обязательно ограничивается фиксированной суммой в месяц**: трафик и
запросы сверх пакета, а также дополнительные функции могут оплачиваться отдельно.
Скрипт ничего не покупает. Нажми платное подтверждение сам только если условия
устраивают. На последнем экране оставь логи и экранирование выключенными и
заверши создание. Не создавай второй ресурс из-за задержки первого.
[Актуальные правила тарификации]({SOURCES['pricing']}).

## 5. Передай установщику адрес CNAME выданного ресурса

В созданном CDN-ресурсе открой **Обзор → Настройки DNS**. Скопируй полный адрес
провайдера вида `…topology.gslb.yccdn.ru`. Это не `{origin}`, не `{cdn}` и не IP
сервера. Используй именно адрес из своего ресурса.
[Где найти адрес провайдера]({SOURCES['dns']}).

Замени `CDN_CNAME_TARGET` скопированным адресом; буквальную заглушку запускать нельзя:

```bash
{resume_command} --cdn-target "CDN_CNAME_TARGET" --apply
```

Установщик создаст DNS-only CNAME `{cdn}` → адрес провайдера. Не включай оранжевое
облако Cloudflare, не заменяй запись A origin-домена и не делай origin CNAME на CDN.
Для этой схемы нужны **две разные CNAME-записи**: подтверждение сертификата и
раздача CDN, плюс уже подготовленная A-запись origin.

## 6. Дождись готовности и запусти финальную проверку

Проверь: сертификат Issued, ресурс активен, настройки и DNS успели
распространиться. Это может занять время; не отключай проверку TLS и не
пересоздавай объекты только из-за ожидания. Затем выполни:

```bash
{resume_command} --finish --apply
```

Этот этап проверит HTTPS/CDN и настоящий VPN-трафик перед включением хоста.
Ответ `/health` или цифра пинга сами по себе не доказывают работу VPN.
После успешного завершения обнови подписку в Happ/INCY и проверь открытие сайтов.
Доступность через белые списки зависит от сети оператора: скрипт и Яндекс CDN
не дают гарантии обхода ограничений.
'''
