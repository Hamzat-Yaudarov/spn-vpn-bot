# Закрытый тест Managed Bots — 22.10.2026

Локальный архив: `spn-partner-managed-bots-20261022-ack.tar.gz`, 85 явно выбранных
файлов, без секретов и основного SPN. SHA-256:
`8b7e781b30fd2e8a7e4231b22aad72d3a61de97a59952014d86e74b7b114d13e`.

Включение предназначено только для прежнего закрытого владельца 6910097562;
10 ₽ — реальное вступление владельца, клиентские продажи и выдача VPN выключены.
Старый оплаченный бот не заменяется и не требует повторной оплаты.

## Проверки и резервирование

- 882 Python-теста прошли без пропусков на точных VPS-зависимостях с PGlite.
- Дополнительные offline browser smokes: 116 проверок автоплатёжного статуса,
  199 проверок создания бота. Реальных запросов и счетов в них нет.
- Live read-only Telegram `getMe`: manager verified, `can_manage_bots: true`.
- Live 5 замеров БД: median SELECT ≈523 мс, release соединения ≈262 мс.
  Измерения не воспроизводили update и не меняли данные.
- Повторные простые действия сокращены с 5–7 запросов до 2–3, без кэша денег или
  полномочий. Первый запуск/привязки и изменения сохраняют транзакции/блокировки.
- Callback acknowledgement выполняется параллельно обычной обработке. Свежая
  exact-tenant запись даёт токен только внутреннему адаптеру; timeout 2,5 с,
  ошибки не ломают оплату и не раскрывают URL/токены. Экран остаётся webhook reply.
- Root-only полный снимок 31 таблицы до DDL:
  `/etc/spn-partner-private/before-managed-bots-20261022.json`, SHA-256:
  `8e532cfe3375e16feb046fbb2240ba57157b425a0c0c4dd6c75038720339b122`.

## Отдельная база Supabase

Владелец отдельно одобрил миграцию/доступ прежней ограниченной роли и обновление
закрытого VPS. В проекте `crrmaqiakrqpfvhiwtkq` одной транзакцией применены:

1. `managed_bots.sql` — служебные привязки/один pending draft на владельца.
2. `entry_launch_notifications.sql` — новый вид существующей очереди.
3. Прежний `supabase_security.sql` — RLS/ограниченная роль/отсутствие публичных прав.

SQL Editor подтвердил `managed_bots_ready=true`, `launch_notifications_ready=true`,
`projects_preserved=1`, `entry_orders_preserved=1`. Цены, балансы и денежные записи
миграциями не изменялись. Screenshot:
`/private/tmp/spn-franchise-managed-migration-applied-20261022.png`.

## Порядок установки

Использовать `upgrade_private_pilot.py`, а не прежний OFF-only updater:
сначала `--upgrade` с неизменным master и его unit backup, затем отдельный
`--enable-new-features` после probe/capability. Указать точные archive/backup SHA.
Новые флаги — только `enable_managed_bots`, `enable_entry_launch_notifications`.
Токен, DSN, ключи, магазин, цена и закрытый allowlist не меняются. Manager bootstrap
обновляет только прежний webhook/секрет/allowed_updates, не сбрасывает update.

Не объявлять фактическое создание нового Managed Bot проверенным до подтверждения
владельцем в Telegram. Уже оплаченный legacy-проект не перепривязывать ради теста.

## Фактический выпуск

- Код установлен в `/opt/spn-franchise-staging/releases/20261022-8b7e781b30fd`.
- `spn-partner-managed-upgrade-20261022`: success, exit 0, 23,309 с.
- `spn-partner-managed-features-20261022`: success, exit 0, 24,849 с.
- Оба новых флага включены; HTTP/worker — enabled/active.
- Read-only getWebhookInfo: прежний origin, `message` + `managed_bot`,
  pending 0, последней ошибки нет. Закрытый allowlist не изменён.
- Catch-up ровно одного ранее завершённого запуска: `delivered: 1`.
  Новых счетов/покупок/Managed Bots оператор не создавал.
- Проектов 1, заказов вступления 1. HTTPS страницы — 200, API без подписи — 401.
- Master backup: `/etc/spn-partner-private/staging.before-managed-bots.json`.
  Customer provisioning и public checkout остались выключенными.
- Обновление и enable одноразовые: не удалять pending/backup для слепого retry.

## Последующий одобренный сброс owner-теста

22.10.2026 владелец отдельно запросил и подтвердил очистку для повторной покупки.
Старый проект/заказ/тестовые клиентские строки архивированы и удалены из рабочей
базы; старый tenant webhook отключён, сам Telegram-бот не удалён. Manager,
release, конфигурация, цена 10 ₽ и закрытые ограничения сохранены.

Новый защищённый архив 32 таблиц/16 строк:
`/etc/spn-partner-private/before-owner-reset-20261022.json`, SHA-256
`c3c267bf2a0b3bc9842aa7cfbdf0e38dd2df6317697ec45117f44a321c67a7ed`.
Запись реального платежа у провайдера не менялась. Скрипт не обращается к ЮKassa
и не обходит append-only guards. Полный текущий статус — `VPS_STAGING_STATUS.md`.
