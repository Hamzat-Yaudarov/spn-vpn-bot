# Германия: дополнительный Yandex CDN — 24.10.2026

## Текущее состояние

Добавлен отдельный XHTTP-транспорт к работающим gRPC/WS. Новый хост **включён**:
аудит панели подтвердил его привязку к ноде и трём нужным сквадам. Доверенный
edge TLS, HTTP/2 и передача синтетического XHTTP-заголовка проверены. Полноценное
VPN-подключение и доступность из ограниченной мобильной сети пока не подтверждены.

| Объект | Значение |
| --- | --- |
| VPS / HTTPS origin | `144.31.168.231` / `deeee1.wayspn.com` |
| Существующая нода | `f87b14cf-438a-4c61-acc1-7522491c05ae` |
| Существующий профиль | `60714af0-bc08-44e5-bbb3-9aa0d2dedb53` |
| Новый inbound | `e12e3bef-9ea0-4509-a27e-21d420120621`, `vless-xhttp-yandex-deeee1` |
| Новый включённый хост | `0ae0ce08-7bbc-47a5-89b8-f6ea8c9b982b` |
| Yandex CDN / домен | `bc8rw4ybx4jrya4u65ch` / `cdn-deeee1.wayspn.com` |
| Сертификат | `fpqpqi4jcuvu91ehul7m`: ISSUED, подключён к ресурсу |

Origin использует HTTPS с явными Host/SNI `deeee1.wayspn.com`. DNS-only CNAME:

- `cdn-deeee1.wayspn.com` → `e2a9e51d3ba2edff.topology.gslb.yccdn.ru.`
- `_acme-challenge.cdn-deeee1.wayspn.com` → `fpqpqi4jcuvu91ehul7m.cm.yandexcloud.net.`

ACME-запись сохраняется для продления сертификата. Origin не должен становиться
CNAME на CDN: это отдельный адрес источника.

Профиль дополнен одним loopback-listener `127.0.0.1:10085`, path `/assets/app/`.
Настройки скопированы с рабочего DE11-хоста
`c1614c56-403d-4a67-b917-928a04600db8`: packet-up, GET/header,
`X-Transport-Data`, uplink до 2048 байт, server header limit 8192 байта.
Три сквада, уже содержавшие source CDN inbound, получили только новый UUID.
Нода Connected, Nginx active; неполный запрос напрямую к listener возвращает 400.
Это проверка маршрута без пользовательского ключа, а не доказательство работы VPN.
HTTPS через CDN: `/health` — 200, неполный `/assets/app/invalid-probe` с корректно
закодированными 2048 байтами `X-Transport-Data` — 400; оба по HTTP/2 с успешной
проверкой сертификата. Прежний WS возвращает 101; порт gRPC сохранён.
В консоли включена переадресация HTTP→HTTPS, TLSv1.2+; CDN/browser cache выключен,
разрешены GET/HEAD/OPTIONS. Переадресация ещё может распространяться по edge.

## Управление этапами

Без действия `manage.py` печатает только план. `prepare --snapshot … --state …`
создаёт новый приватный план локально, без сетевых записей. Для этой операции
план уже создан; не готовить новый из текущего изменённого состояния.

Стадии `apply-profile`, `bind-node`, `create-host`, `bind-squads`, `enable` выполнены.
Каждая записывает durable intent до API-вызова и делает свежий GET-аудит.
При потерянном ответе сначала аудит; слепых повторов POST/PATCH нет.

```sh
python3 -I -B ops/yandex-deeee1/manage.py audit --state /private/tmp/wayspn-yandex-deeee1-20261023/operation
```

Только после проверки TLS/DNS/CDN, отсутствия кеширования транспорта и
готовности origin оператор может отдельно включить **этот один** хост:

```sh
python3 -I -B ops/yandex-deeee1/manage.py enable --state /private/tmp/wayspn-yandex-deeee1-20261023/operation --confirm --tls-verified
```

`--tls-verified` — явное подтверждение оператора, не автоматическая TLS-проверка.
HTTP 200/400 не заменяет тест клиента после обновления подписки. Во время
GET→PATCH нельзя параллельно редактировать профиль/ноду/сквады: API не имеет CAS.

## Снимки и границы

- Локальный план: `/private/tmp/wayspn-yandex-deeee1-20261023/operation`, каталог
  0700, файлы 0600. `before.json`, `candidate.json`, `validation-profile.json`
  содержат серверные секреты: не публиковать и не печатать целиком.
- На VPS: `/var/backups/wayspn-yandex-deeee1-origin-20261024`, 0700/0600;
  `nginx.before.conf`, `nginx.candidate.conf`, `manifest.json`.
- `origin_patch.py` добавляет только location `/assets/app/` в выбранный vhost;
  request/response buffering и cache выключены. Перед reload выполняется
  `nginx -t`; при ошибке восстанавливается точный исходный файл и reload.
- Старые gRPC/WS, их UUID/ключи, сертификат origin, SNI-ограничения, default
  blocks, исходный DE11 CDN и остальные объекты сохраняются. IP уже был исправлен
  до этой операции; helper не меняет IP, DNS, firewall, Docker или пользователей.
- Автоматического отката панели/DNS/CDN нет. После чужих изменений старым
  снимком ничего не затирать: нужен свежий аудит и отдельно проверенный откат.

Локальная проверка: `python3 -m unittest discover -s ops/yandex-deeee1 -p 'test_*.py'`
— 21 синтетический тест, без сетевых вызовов и реальных пользовательских ключей.
