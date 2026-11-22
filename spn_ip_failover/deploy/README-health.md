# Постоянные проверки с обновлением подписки

`spn-controller-health.timer` запускает один цикл, затем ждёт 120 секунд после
его завершения. Сначала служба получает актуальные конфигурации существующего
Admin через API Remnawave, затем проверяет VPN. Ошибка обновления сохраняет
прежние приватные файлы и завершает цикл с ошибкой. Платных операций служба
не выполняет.

В `/var/lib/spn-ip-failover/refresh.json` укажи поля из модуля `refresh.py`:
`panel_url`, `token_env`, `username`, `xray`, `sha256`, `output`,
`coverage_output`. Используются `username: "Admin"`, `output`:
`/var/lib/spn-ip-failover/routes.json` и `coverage_output`:
`/var/lib/spn-ip-failover/coverage.json`. Каталог принадлежит `spn-failover`
с режимом `0700`, конфиг — тому же пользователю с режимом `0600`.

API-токен устанавливается root в
`/etc/spn-ip-failover/credentials/remnawave`, режим `0600`, и передаётся службе
через systemd LoadCredential. В отчётах нет клиентских ключей.

После установки кода и обоих unit-файлов проверь разовый запуск и результат:

```sh
systemctl daemon-reload
systemctl start spn-controller-health.service
systemctl show spn-controller-health.service -p Result -p ExecMainStatus
cat /var/lib/spn-ip-failover/latest.json
systemctl enable --now spn-controller-health.timer
```

Результаты сохраняются в `latest.json`, покрытие поддерживаемых и пропущенных
конфигураций — в `coverage.json`. Для остановки постоянных проверок:

```sh
systemctl disable --now spn-controller-health.timer
systemctl stop spn-controller-health.service
```
