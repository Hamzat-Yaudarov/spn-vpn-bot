# 🗄️ Автоматическое создание таблиц PostgreSQL

## 📋 Как это работает

При запуске бота (`python main.py`):

1. ✅ **Инициализация пула подключений** - создаётся связь с PostgreSQL
2. ✅ **Создание схемы** - выполняется `schema.sql` (создаёт таблицы если их нет)
3. ✅ **Выполнение миграций** - добавляют новые колонки если они отсутствуют
4. ✅ **Бот готов к работе** - начинает обрабатывать команды пользователей

```
Запуск бота (main.py)
        ↓
  asyncio.run(main())
        ↓
  await db.init_db()
        ↓
  asyncpg.create_pool() - подключение к PostgreSQL
        ↓
  await run_schema() - создание таблиц из schema.sql
        ├─ CREATE TABLE IF NOT EXISTS users
        ├─ CREATE TABLE IF NOT EXISTS payments
        ├─ CREATE TABLE IF NOT EXISTS promo_codes
        ├─ CREATE INDEX ...
        └─ CREATE TRIGGER ...
        ↓
  await run_migrations() - добавление новых колонок
        ├─ ALTER TABLE users ADD COLUMN last_gift_attempt
        ├─ ALTER TABLE users ADD COLUMN last_promo_attempt
        ├─ ALTER TABLE users ADD COLUMN last_payment_check
        ├─ ALTER TABLE payments ADD COLUMN payment_guid
        └─ ALTER TABLE payments ADD COLUMN payment_method
        ↓
  Бот готов! ✅ Начинает polling Telegram
```

---

## 🚀 Запуск бота

### Локально (для тестирования)

```bash
# 1. Активируйте виртуальное окружение
source venv/bin/activate

# 2. Запустите бота
python main.py

# Вы должны увидеть:
# 2024-01-15 10:30:45 - INFO - main - Database initialized
# 2024-01-15 10:30:46 - INFO - database - Creating database schema...
# 2024-01-15 10:30:47 - INFO - database - Database schema created successfully ✅
# 2024-01-15 10:30:47 - INFO - database - Running migrations...
# 2024-01-15 10:30:48 - INFO - database - All migrations completed successfully ✅
# 2024-01-15 10:30:48 - INFO - main - Bot started polling...
```

### На сервере (production)

```bash
# Через systemd (если настроено)
sudo systemctl start spn-bot

# Или через экран
ssh root@77.233.214.150
cd /opt/spn-vpn-bot
source venv/bin/activate
python main.py
```

---

## ✅ Проверка инициализации

### Тестовый скрипт

Перед полным запуском бота вы можете проверить только инициализацию:

```bash
python test_db_init.py
```

**Ожидаемый вывод:**
```
2024-01-15 10:30:45 - INFO - test_db_init - Starting database initialization test...
2024-01-15 10:30:46 - INFO - database - Database pool initialized successfully
2024-01-15 10:30:46 - INFO - database - Creating database schema...
2024-01-15 10:30:47 - INFO - database - Database schema created successfully ✅
2024-01-15 10:30:47 - INFO - database - Running migrations...
2024-01-15 10:30:48 - INFO - database - All migrations completed successfully ✅
✅ Database initialized successfully!
Tables created:
  - users
  - payments
  - promo_codes
```

### Проверка в psql

Если хотите вручную проверить таблицы:

```bash
# Подключитесь к БД
psql "postgresql://user:password@host:port/database"

# Проверьте таблицы
\dt

# Должны быть видны:
# public | payments    | table
# public | promo_codes | table
# public | users       | table

# Посмотрите структуру таблицы
\d users

# Выход
\q
```

---

## 📄 Какие таблицы создаются

### 1. users
```sql
CREATE TABLE users (
    tg_id BIGINT PRIMARY KEY,
    username TEXT,
    accepted_terms BOOLEAN DEFAULT FALSE,
    remnawave_uuid TEXT UNIQUE,
    remnawave_username TEXT,
    subscription_until TEXT,
    squad_uuid TEXT,
    referrer_id BIGINT,
    gift_received BOOLEAN DEFAULT FALSE,
    referral_count INTEGER DEFAULT 0,
    active_referrals INTEGER DEFAULT 0,
    first_payment BOOLEAN DEFAULT FALSE,
    last_gift_attempt TIMESTAMP,
    last_promo_attempt TIMESTAMP,
    last_payment_check TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

**Индексы:**
- `idx_users_tg_id` - по tg_id (быстрый поиск)
- `idx_users_remnawave_uuid` - по remnawave_uuid
- `idx_users_referrer_id` - по referrer_id

### 2. payments
```sql
CREATE TABLE payments (
    id SERIAL PRIMARY KEY,
    tg_id BIGINT NOT NULL,
    tariff_code TEXT NOT NULL,
    amount DECIMAL(10, 2) NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    provider TEXT,
    invoice_id TEXT UNIQUE,
    payload TEXT,
    payment_guid TEXT,              -- 1Plat платежи
    payment_method TEXT              -- карта/СБП
)
```

**Индексы:**
- `idx_payments_tg_id` - по tg_id
- `idx_payments_status` - по status
- `idx_payments_provider` - по provider
- `idx_payments_invoice_id` - по invoice_id

### 3. promo_codes
```sql
CREATE TABLE promo_codes (
    code TEXT PRIMARY KEY,
    days INTEGER NOT NULL,
    max_uses INTEGER NOT NULL,
    used_count INTEGER DEFAULT 0,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

**Индексы:**
- `idx_promo_codes_code` - по code
- `idx_promo_codes_active` - по active

---

## 🔧 Настройка переменных окружения

В `.env` нужно указать:

```env
# PostgreSQL подключение
DATABASE_URL=postgresql://username:password@host:port/database_name

# Примеры:
# Локально:
DATABASE_URL=postgresql://postgres:password@localhost:5432/spn_bot

# На сервере Supabase:
DATABASE_URL=postgresql://postgres:password@db.supabase.co:5432/postgres

# На сервере Google Cloud:
DATABASE_URL=postgresql://user:password@10.0.0.2:5432/postgres
```

### Проверка подключения

```bash
# Проверьте что строка подключения верна
psql $DATABASE_URL -c "SELECT version();"

# Должны увидеть версию PostgreSQL:
# PostgreSQL 14.0 (Ubuntu 14.0-1.pgdg20.04+1) on x86_64...
```

---

## ⚠️ Решение проблем

### Проблема: "connection refused"

**Причина:** PostgreSQL сервер не запущен или неверная строка подключения

**Решение:**
```bash
# Проверьте что PostgreSQL запущен
sudo systemctl status postgresql

# Если не запущен:
sudo systemctl start postgresql

# Проверьте DATABASE_URL в .env
cat .env | grep DATABASE_URL

# Тестируйте подключение
psql "postgresql://user:password@host:port/database"
```

### Проблема: "permission denied"

**Причина:** Неверный пароль или пользователь

**Решение:**
```bash
# Проверьте учётные данные
psql -U postgres -h localhost

# Если не подходит пароль, сбросьте:
sudo -u postgres psql
ALTER USER postgres WITH PASSWORD 'new_password';
```

### Проблема: "relation \"users\" does not exist"

**Причина:** Таблицы не были созданы

**Решение:**
1. Запустите `python test_db_init.py` чтобы создать таблицы
2. Проверьте логи: `grep "Creating database schema" bot.log`
3. Вручную создайте таблицы:
   ```bash
   psql $DATABASE_URL -f schema.sql
   ```

### Проблема: "duplicate key value violates unique constraint"

**Причина:** Пытаетесь вставить дублирующееся значение

**Решение:**
```bash
# Очистите таблицу (если нужно)
psql $DATABASE_URL -c "DELETE FROM payments;"

# Или просто продолжайте, новые данные добавятся корректно
```

---

## 🔄 Миграция данных (если были старые таблицы)

Если у вас уже были таблицы:

```bash
# 1. Экспортируйте старые данные (опционально)
pg_dump $DATABASE_URL > backup.sql

# 2. Удалите старые таблицы (внимание - данные будут потеряны!)
psql $DATABASE_URL -c "
  DROP TABLE IF EXISTS payments CASCADE;
  DROP TABLE IF EXISTS promo_codes CASCADE;
  DROP TABLE IF EXISTS users CASCADE;
"

# 3. Запустите бот чтобы пересоздать таблицы
python main.py

# 4. Восстановите данные если нужно (осторожно с FK)
# psql $DATABASE_URL < backup.sql
```

---

## 📊 Проверка данных

### Посчитайте строки в таблицах

```bash
psql $DATABASE_URL -c "
SELECT 
  'users' as table_name, COUNT(*) as count FROM users
UNION ALL
SELECT 'payments' as table_name, COUNT(*) FROM payments
UNION ALL
SELECT 'promo_codes' as table_name, COUNT(*) FROM promo_codes;
"
```

### Посмотрите структуру таблицы

```bash
psql $DATABASE_URL -c "\d+ users"
```

### Посмотрите индексы

```bash
psql $DATABASE_URL -c "SELECT * FROM pg_indexes WHERE tablename = 'users';"
```

---

## ✨ Что происходит при каждом запуске

### Первый запуск
- ✅ Создаёт все таблицы
- ✅ Добавляет индексы
- ✅ Создаёт триггеры
- ✅ Добавляет колонки миграций
- **Результат:** 3 полностью готовые таблицы

### Последующие запуски
- ✅ Проверяет что таблицы существуют (они уже есть)
- ✅ Пропускает создание (конфликтов не будет)
- ✅ Проверяет что все колонки есть (они уже есть)
- ✅ Пропускает добавление (конфликтов не будет)
- **Результат:** Быстрый старт бота (~1 секунда)

---

## 📈 Масштабирование

Если нужны дополнительные индексы или оптимизации:

### 1. Для быстрого поиска платежей по статусу

```sql
CREATE INDEX idx_payments_status_tg ON payments(status, tg_id);
```

### 2. Для быстрого поиска по провайдеру и статусу

```sql
CREATE INDEX idx_payments_provider_status ON payments(provider, status);
```

### 3. Для аналитики по датам

```sql
CREATE INDEX idx_payments_created ON payments(created_at);
CREATE INDEX idx_users_created ON users(created_at);
```

Выполните в psql или добавьте в `schema.sql`.

---

## 🚀 Production Checklist

- [x] DATABASE_URL заполнена в .env
- [x] PostgreSQL сервер доступен
- [x] Можно подключиться через psql
- [x] Тест подключения пройден (`python test_db_init.py`)
- [x] Первый запуск бота создал таблицы
- [x] Данные сохраняются в БД
- [x] Бот отвечает на команды
- [x] Платежи записываются в payments
- [x] Промокоды работают

---

## 📚 Дополнительные команды

### Просмотр всех таблиц
```bash
psql $DATABASE_URL -c "\dt"
```

### Просмотр всех функций
```bash
psql $DATABASE_URL -c "\df"
```

### Просмотр всех триггеров
```bash
psql $DATABASE_URL -c "SELECT * FROM information_schema.triggers;"
```

### Резервная копия
```bash
pg_dump $DATABASE_URL > backup-$(date +%Y%m%d).sql
```

### Восстановление из резервной копии
```bash
psql $DATABASE_URL < backup-20240115.sql
```

---

## ✅ Итого

При запуске бота `python main.py`:

1. ✅ Подключение к PostgreSQL
2. ✅ Создание таблиц (если их нет)
3. ✅ Добавление колонок (если их нет)
4. ✅ Готово к работе

**Все данные будут автоматически сохраняться в PostgreSQL!** 🎉
