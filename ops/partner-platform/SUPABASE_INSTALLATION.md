# Подготовка Supabase — 21.10.2026

Только отдельный проект **SPN franchise**, ref `crrmaqiakrqpfvhiwtkq`, Free,
Tokyo, PostgreSQL 17.11. Проект уже существовал; новый не создавался, тариф не
менялся. Пользователь отдельно подтвердил установку схемы/роли/RLS и отключение
Data API. Основная база SPN не открывалась и не изменялась.

## Установлено и проверено через кабинет

- До миграций в `public` было 0 таблиц.
- Data API выключен; сохранённое состояние подтверждено после перезагрузки.
- Роль `spn_partner_runtime` создана **NOLOGIN**, без пароля, SUPERUSER,
  CREATEDB, CREATEROLE, REPLICATION и BYPASSRLS.
- Все 15 подготовительных SQL-файлов применены в одном явном `BEGIN/COMMIT`:
  роль → schema → commerce → enrollment → entitlements → traffic → notifications
  → subscriptions → finance → management → branding → operations → public_checkout
  → panel_identity → supabase_security.
- Кабинет вернул `Success. No rows returned`. Итоговый read-only SQL подтвердил:
  **31 таблица, 31 с RLS, 31 server-only policy, 0 table grants для
  anon/authenticated/service_role**. Runtime имеет SELECT/INSERT/UPDATE/DELETE,
  не имеет TRUNCATE и административных привилегий; LOGIN=false.
- В базе **0 проектов и 0 заказов**. Тестовые клиенты/деньги не загружались.

Пакет `/private/tmp/spn-franchise-bootstrap-20261021-v2.sql` без секретов,
SHA-256 `98d7d9ac3b6edbde46037819977794dd285708295ebe975d40e719654bb9d7c5`.
Это временный артефакт; источником следующих миграций остаются SQL-файлы проекта.

Первый атомарный запуск был остановлен проверкой memberships и откатил таблицы.
PostgreSQL 17 автоматически создаёт creator-admin grant: parent=runtime,
member=postgres, grantor=supabase_admin, ADMIN=true, INHERIT=false, SET=false.
После воспроизведения и тестирования разрешена только эта строго ограниченная
административная связь (также допускается bootstrap grantor postgres). Любое
наследование/SET ROLE, другой получатель/выдающий или исходящее членство runtime
по-прежнему блокирует миграции и probe. Членство автоматически не удалялось.

## Следующий выполненный этап: закрытый manager-стенд

- Владелец через защищённый psql `\password` самостоятельно задал пароль роли
  и включил LOGIN, затем скрыто ввёл токен/пароль в настройщик на отдельном VPS.
  Пароли не передавались в чат, SQL Editor, Git или frontend.
- Использован официальный Supabase Root CA, без ослабления TLS/hostname проверки.
  `--probe` как `spn-partner` через session pooler вернул `database_ready: true`.
- Подключены только **@spn_franchise_bot** и owner-private HTTP API за HTTPS;
  bootstrap — `installed`, владелец подтвердил открытие кабинета в Telegram.
  Основной SPN не перезапускался. Состояние: [VPS_STAGING_STATUS.md](VPS_STAGING_STATUS.md).

## Что ещё не выполнено

- Не настроены wildcard DNS/HTTPS `*.sub.wayspn.online`, тестовый мерчант,
  тестовые клиентские Telegram/Remnawave-интеграции и регулярные копии базы
  с проверенным восстановлением. Закрытые копии исходной конфигурации есть.
- Не проверены настоящие Happ/INCY на телефоне. Новые читаемые panel identity
  подготовлены локально, но внешние пользователи не создавались/не переименовывались.

Это подготовленная схема и **закрытый manager-стенд**, не работающие продажи и не разрешение
на реальные продажи. Дальнейший стенд/публикация согласуются отдельно.
