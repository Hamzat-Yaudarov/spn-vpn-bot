# 🔄 ОБНОВЛЕНИЕ ПРОЕКТА НА СУЩЕСТВУЮЩЕМ СЕРВЕРЕ

**Сценарий:** Бот уже работает на VPS, нужно обновить код и перейти на Supabase

**Время:** 10-15 минут

---

## 📋 ПЕРЕД НАЧАЛОМ - ПОДГОТОВЬ ЭТИ ЗНАЧЕНИЯ

Найди в письме и скопируй эти значения (подставишь в команды ниже):
```
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_supabase_key
DATABASE_URL=postgresql://postgres:PASSWORD@db.your-project.supabase.co:5432/postgres
```

⚠️ В DATABASE_URL замени `PASSWORD` на реальный пароль из Supabase

---

## ⚡ КОМАНДЯ ДЛЯ ОБНОВЛЕНИЯ (копируй поочередно)

### ЭТАП 1️⃣ - СОЗДАЁМ ТАБЛИЦЫ В SUPABASE (3 минуты)

**Вариант A: Через веб-интерфейс (если не уверен)**

1. Перейди https://supabase.com/dashboard
2. Выбери свой проект
3. Левое меню → **SQL Editor**
4. Нажми **+ New Query**
5. Скопируй **весь текст** из файла `schema.sql` (из проекта)
6. Вставь в редактор
7. Нажми **Run** (или Ctrl+Enter)

**Готово, когда видишь:** `Query completed successfully` ✅

---

**Вариант B: Через pgAdmin/psql (если установлен PostgreSQL)** 

```bash
# На своем ПК (НЕ на VPS):
psql -h db.your-project.supabase.co \
     -U postgres \
     -d postgres \
     -f /path/to/schema.sql
# Введи пароль
```

---

### ЭТАП 2️⃣ - ПОДКЛЮЧИСЬ К СЕРВЕРУ (30 секунд)

```bash
# Открой терминал и выполни:
ssh root@ВАШ_IP_АДРЕС
# Введи пароль если потребуется
```

✅ **Результат:** Ты в терминале VPS (видишь `root@hostname:~#`)

---

### ЭТАП 3️⃣ - ОСТАНОВИ БОТА (30 секунд)

```bash
# Останавливаешь сервис systemd
sudo systemctl stop spn-bot

# Проверяешь что остановился
sudo systemctl status spn-bot
# Должно быть: inactive (dead)
```

✅ **Результат:** Бот остановлен

---

### ЭТАП 4️⃣ - ОБНОВЛЯЕШЬ КОД ИЗ GITHUB (1 минута)

```bash
# Переходишь в папку проекта
cd /root/spn-vpn-bot

# Обновляешь код
git pull origin main

# Если видишь конфликты - смотри раздел "ПОМОЩЬ" внизу
```

✅ **Результат:** Код обновлен

---

### ЭТАП 5️⃣ - ОБНОВЛЯЕШЬ ЗАВИСИМОСТИ (2 минуты)

```bash
# Активируешь виртуальное окружение
source /root/spn-vpn-bot/venv/bin/activate

# Обновляешь зависимости (включая новый asyncpg)
pip install -r /root/spn-vpn-bot/requirements.txt --upgrade

# Выходишь из виртуального окружения
deactivate
```

✅ **Результат:** asyncpg установлен

---

### ЭТАП 6️⃣ - ОБНОВЛЯЕШЬ .env ФАЙЛ (2 минуты)

Это **самый важный шаг!**

```bash
# Открываешь .env файл
nano /root/spn-vpn-bot/.env
```

**В файле найди строки и измени их:**

```bash
# УДАЛИ ЭТА СТРОКА (если есть):
# DB_FILE=spn_vpn_bot.db

# ДОБАВЬ ЭТИ СТРОКИ в конец файла:
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_supabase_key
DATABASE_URL=postgresql://postgres:PASSWORD@db.your-project.supabase.co:5432/postgres
```

⚠️ **ЗАМЕНИ `PASSWORD` на реальный пароль!**

**Сохраняешь:**
- Нажми **Ctrl+O**
- Нажми **Enter**
- Нажми **Ctrl+X**

**Проверяешь что сохранилось:**
```bash
cat /root/spn-vpn-bot/.env | grep -E "SUPABASE|DATABASE_URL"
# Должны видеть все три строки
```

✅ **Результат:** .env обновлен

---

### ЭТАП 7️⃣ - ЗАПУСКАЕШЬ БОТА (30 секунд)

```bash
# Запускаешь сервис
sudo systemctl start spn-bot

# Проверяешь статус (должно быть active (running))
sudo systemctl status spn-bot
```

✅ **Результат:** Бот запущен

---

### ЭТАП 8️⃣ - ПРОВЕРЯЕШЬ ЛОГИ (1 минута)

```bash
# Смотришь логи в реальном времени
sudo journalctl -u spn-bot -f
```

**Ищешь эти сообщения:**
```
✅ "Database pool initialized successfully"
✅ "All handlers registered"
✅ "Bot started polling..."
```

**Если видишь эти - ВСЕ ОТЛИЧНО! 🎉**

Нажми **Ctrl+C** чтобы выйти

---

### ЭТАП 9️⃣ - ТЕСТИРУЕШЬ В TELEGRAM (1 минута)

1. Открой Telegram
2. Напиши боту `/start`
3. Нажми кнопку "💳 Оформить подписку"
4. Проверь что всё работает как раньше

✅ **Результат:** Бот работает с Supabase!

---

## 🆘 ПОМОЩЬ - ЧТО ДЕЛАТЬ ЕСЛИ ОШИБКА

### ❌ "fatal: Your local changes to the following files would be overwritten"

**Причина:** Ты редактировал локально файлы которые изменились в GitHub

**Решение:**
```bash
cd /root/spn-vpn-bot

# Вариант A: Забыть о своих изменениях (если они не важны)
git reset --hard origin/main

# Вариант B: Сохранить свои изменения
git stash
git pull origin main
git stash pop  # Применить свои изменения обратно
```

---

### ❌ "Connection refused" или "Database pool initialization failed"

**Причина:** Проблема с Supabase подключением

**Проверки:**
```bash
# 1. Проверь что DATABASE_URL правильный
cat /root/spn-vpn-bot/.env | grep DATABASE_URL

# Должно быть что-то вроде:
# postgresql://postgres:PASSWORD@db.HASH.supabase.co:5432/postgres

# 2. Убедись что нет символов < и > в пароле
# 3. Убедись что пароль правильный
# 4. Посмотри полную ошибку в логах
sudo journalctl -u spn-bot -n 100 | tail -30
```

**Если IP VPS заблокирован в Supabase:**
```
Перейди: https://supabase.com/dashboard
→ Project Settings
→ Database
→ Connection pooling
→ Firewall rules
→ Add IPv4 address

Узнай IP VPS:
curl ifconfig.me
```

---

### ❌ "ModuleNotFoundError: No module named 'asyncpg'"

**Причина:** asyncpg не установлен

**Решение:**
```bash
source /root/spn-vpn-bot/venv/bin/activate
pip install asyncpg>=0.28.0
deactivate
sudo systemctl restart spn-bot
```

---

### ❌ "Table doesn't exist" ошибка в логах

**Причина:** Не создал таблицы в Supabase

**Решение:**
```
Перейди на https://supabase.com/dashboard
SQL Editor → New Query
Скопируй код из schema.sql → Paste → Run
```

---

### ⚠️ Бот запущен но не отвечает в Telegram

```bash
# 1. Проверь что процесс работает
ps aux | grep "python3 main.py"

# 2. Проверь логи
sudo journalctl -u spn-bot -f

# 3. Перезагрузи
sudo systemctl restart spn-bot

# 4. Проверь что BOT_TOKEN правильный
cat /root/spn-vpn-bot/.env | grep BOT_TOKEN
```

---

## 📊 ПОЛЕЗНЫЕ КОМАНДЫ

```bash
# Остановить бота
sudo systemctl stop spn-bot

# Запустить бота
sudo systemctl start spn-bot

# Перезагрузить бота
sudo systemctl restart spn-bot

# Смотреть статус
sudo systemctl status spn-bot

# Смотреть последние 50 строк логов
sudo journalctl -u spn-bot -n 50

# Смотреть логи в реальном времени
sudo journalctl -u spn-bot -f

# Поиск ошибок в логах
sudo journalctl -u spn-bot | grep -i error

# Проверить .env файл
cat /root/spn-vpn-bot/.env

# Проверить что таблицы созданы в Supabase
# Перейди на https://supabase.com/dashboard
# Table Editor → должны видеть users, payments, promo_codes
```

---

## ✅ ФИНАЛЬНЫЙ ЧЕК-ЛИСТ

После обновления проверь:

- [ ] `sudo systemctl status spn-bot` показывает `active (running)`
- [ ] `sudo journalctl -u spn-bot -f` показывает "Database pool initialized successfully"
- [ ] Бот отвечает на `/start` в Telegram
- [ ] Можешь открыть "/my_subscription" без ошибок
- [ ] В логах нет красных ошибок
- [ ] На https://supabase.com/dashboard видны таблицы (users, payments, promo_codes)

---

## 🎉 ГОТОВО!

Твой бот теперь работает с **Supabase PostgreSQL**!

**Преимущества:**
- ✅ БД в облаке (безопаснее)
- ✅ Автоматические бэкапы (спокойнее)
- ✅ Может масштабироваться (готово к росту)

---

## 💡 ЕСЛИ ЧТО-ТО НЕ ПОНЯТНО

- Смотри `QUICK_START_SUPABASE.md` (быстрый способ)
- Смотри `SUPABASE_MIGRATION.md` (подробная инструкция)
- Смотри логи: `sudo journalctl -u spn-bot -f`
- Спроси в поддержке Supabase если проблема там
