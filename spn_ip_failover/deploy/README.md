# Постоянный запуск контроллера Way SPN

Пример для Linux с systemd 252+ и Python 3.11+ в `/usr/bin/python3`. На контроллере
размести код в `/opt/wayspn` под владельцем `root`. Подготовь инвентарь пилота и
команды хостинга по [основной инструкции](../README.md). Контроллер запускает
команды хостинга от своего пользователя. Если они настраивают гостевую ОС,
эта операция выполняется на управляемом VPS через отдельный доступ.

Создай служебную учётную запись и установи подготовленные файлы. В каждом
файле учётных данных хранится один токен. Для этого примера используются
стандартные имена переменных токенов в инвентаре.

```sh
sudo useradd --system --user-group --no-create-home --home-dir /var/lib/spn-ip-failover \
  --shell /usr/sbin/nologin spn-failover
sudo install -d -o root -g spn-failover -m 0750 /etc/spn-ip-failover
sudo install -d -o root -g root -m 0700 /etc/spn-ip-failover/credentials
sudo install -d -o spn-failover -g spn-failover -m 0700 \
  /var/lib/spn-ip-failover /var/lib/spn-ip-failover/.ssh
sudo install -o root -g spn-failover -m 0640 /path/to/rotation.json \
  /etc/spn-ip-failover/rotation.json
sudo install -o root -g root -m 0600 /path/to/remnawave-token \
  /etc/spn-ip-failover/credentials/remnawave
```

Если инвентарь меняет записи Cloudflare, установи отдельный токен нужной зоны.

```sh
sudo install -o root -g root -m 0600 /path/to/cloudflare-token \
  /etc/spn-ip-failover/credentials/cloudflare
```

Для адаптера Xorek добавь в этот же защищённый каталог пару
`xorek-username` и `xorek-password` с режимом `0600`. При истечении
сеанса адаптер использует их для повторного входа. Можно также задать
`xorek-session` для текущего сеанса.

Для автоматического входа в 1Cent добавь `onecent-email` и
`onecent-password` с режимом `0600`. Обёртка передаст их адаптеру через
переменные окружения. CAPTCHA или дополнительный код требуют ручного входа.
Профиль Chromium и журнал авторизации хранятся в приватном каталоге
служебного пользователя. Цены и параметры выбранных услуг находятся
в `/etc/spn-ip-failover/onecent.json` с владельцем `root:spn-failover`
и режимом `0640`.

Установи проверенную конфигурацию заказа Xorek как
`/etc/spn-ip-failover/xorek-order.json` с режимом `0640` и владельцем
`root:spn-failover`. Укажи отдельный `journal_path` внутри
`/var/lib/spn-ip-failover`, например `/var/lib/spn-ip-failover/xorek-orders`.

Для SSH-алиасов из инвентаря настрой `config`, приватные ключи и проверенные
`known_hosts` в `/var/lib/spn-ip-failover/.ssh`. Файлы принадлежат
`spn-failover`, приватные ключи имеют режим `0600`. Если адаптеру нужен
локальный журнал или кэш, используй каталог из `$STATE_DIRECTORY`.

Установи и проверь unit, затем запусти разовую проверку под служебной учётной
записью. Она читает инвентарь и запускает проверки подключения.

```sh
sudo install -o root -g root -m 0644 \
  /opt/wayspn/spn_ip_failover/deploy/spn-ip-failover.service \
  /etc/systemd/system/spn-ip-failover.service
sudo systemd-analyze verify /etc/systemd/system/spn-ip-failover.service
sudo systemctl daemon-reload
sudo systemd-run --wait --pipe --collect --unit=spn-ip-failover-check \
  -p User=spn-failover -p Group=spn-failover -p WorkingDirectory=/opt/wayspn \
  -p StateDirectory=spn-ip-failover -p StateDirectoryMode=0700 \
  -p NoNewPrivileges=yes -p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes \
  -p LoadCredential=rotation:/etc/spn-ip-failover/credentials \
  /bin/sh /opt/wayspn/spn_ip_failover/deploy/run-controller.sh \
  --config /etc/spn-ip-failover/rotation.json --state /var/lib/spn-ip-failover check
```

Команда ниже включает **рабочий режим**. Для серверов с настроенным адаптером
он может покупать и освобождать IPv4, менять Remnawave и DNS. Запускай после
проверки пилота и лимитов в аккаунте хостинга. Сервис поднимется после
перезагрузки и перезапустится при аварийном завершении. Журнал остаётся в
`/var/lib/spn-ip-failover` и используется при продолжении операции.

```sh
sudo systemctl enable --now spn-ip-failover.service
sudo systemctl status spn-ip-failover.service
sudo journalctl -u spn-ip-failover.service -n 50 --no-pager
```

Для временной остановки используй `sudo systemctl stop spn-ip-failover.service`.
Для остановки с отключением автозапуска используй
`sudo systemctl disable --now spn-ip-failover.service`. Если контроллер
остановился во время покупки, сверь запрос по журналу и используй `reconcile`
по [основной инструкции](../README.md) перед продолжением. Команду, которая
читает или меняет журнал, запускай от имени `spn-failover`.
