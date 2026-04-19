# 🚀 РАЗВЁРТЫВАНИЕ НА НОВОМ СЕРВЕРЕ С НУЛЯ

**Сценарий:** Новый VPS, нужно развернуть бот + Supabase с нуля

**Время:** 20-25 минут

---

## 📋 ПЕРЕД НАЧАЛОМ - ПОДГОТОВЬ ЭТИ ЗНАЧЕНИЯ

### 1. VPS Информация
```
IP адрес VPS: ВАШ_IP_АДРЕС
Пароль root: ВАШ_ПАРОЛЬ
SSH ключ (опционально): /path/to/key.pem
```

### 2. Supabase Значения
```
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_supabase_key
DATABASE_URL=postgresql://postgres:PASSWORD@db.your-project.supabase.co:5432/postgres
```

### 3. Bot Значения (из BotFather в Telegram)
```
BOT_TOKEN=your_telegram_bot_token
ADMIN_ID=your_admin_id
```

### 4. API Значения
```
SUPPORT_URL=https://t.me/your_support
NEWS_CHANNEL_USERNAME=your_channel
TELEGRAPH_AGREEMENT_URL=https://telegra.ph/your-url
REMNAWAVE_BASE_URL=https://your-remnawave.example/api
REMNAWAVE_API_TOKEN=your_token
DEFAULT_SQUAD_UUID=your_uuid
CRYPTOBOT_TOKEN=your_token
CRYPTOBOT_API_URL=https://pay.crypt.bot/api
```

---

## ⚡ КОМАНДЫ ДЛЯ НОВОГО СЕРВЕРА (копируй поочередно)

### ЭТАП 1️⃣ - СОЗДАЁМ ТАБЛИЦЫ В SUPABASE (3 минуты)

**Вариант A: Через веб-интерфейс**

1. Перейди https://supabase.com/dashboard
2. Выбери свой проект
3. Левое меню → **SQL Editor**
4. Нажми **+ New Query**
5. Скопируй **весь текст** из файла `schema.sql`
6. Вставь в редактор
7. Нажми **Run** (или Ctrl+Enter)

**Готово, когда видишь:** `Query completed successfully` ✅

---

**Вариант B: Через pgAdmin/psql**

```bash
# На своём ПК (НЕ на VPS):
psql -h db.your-project.supabase.co \
     -U postgres \
     -d postgres \
     -f /path/to/schema.sql
# Введи пароль
```

---

### ЭТАП 2️⃣ - ПОДКЛЮЧИСЬ К НОВОМУ СЕРВЕРУ (30 секунд)

```bash
# Открой терминал и выполни:
ssh root@ВАШ_IP_АДРЕС
# Введи пароль

# Или если используешь SSH ключ:
ssh -i /path/to/key.pem root@ВАШ_IP_АДРЕС
```

✅ **Результат:** Ты в терминале VPS (видишь `root@hostname:~#`)

---

### ЭТАП 3️⃣ - ОБНОВЛЯЕШЬ СИСТЕМУ (2 минуты)

```bash
# Обновляешь список пакетов
apt update

# Обновляешь пакеты
apt upgrade -y

# Устанавливаешь необходимое
apt install -y git python3 python3-pip python3-venv curl wget nano
```

✅ **Результат:** Система обновлена, необходимые пакеты установлены

---

### ЭТАП 4️⃣ - КЛОНИРУЕШЬ ПРОЕКТ (1 минута)

```bash
# Переходишь в корень
cd /root

# Клонируешь репозиторий
git clone https://github.com/Hamzat-Yaudarov/spn-vpn-bot.git

# Переходишь в папку проекта
cd /root/spn-vpn-bot

# Проверяешь что скачалось
ls -la
# Должен видеть: main.py, config.py, database.py, handlers/, services/, и другие файлы
```

✅ **Результат:** Код скачан

---

### ЭТАП 5️⃣ - СОЗДАЁШЬ ВИРТУАЛЬНОЕ ОКРУЖЕНИЕ (1 минута)

```bash
# Переходишь в папку проекта
cd /root/spn-vpn-bot

# Создаёшь виртуальное окружение
python3 -m venv venv

# Активируешь его
source venv/bin/activate

# Обновляешь pip
pip install --upgrade pip

# Устанавливаешь зависимости
pip install -r requirements.txt

# Выходишь из виртуального окружения
deactivate
```

✅ **Результат:** Виртуальное окружение создано, зависимости установлены

---

### ЭТАП 6️⃣ - СОЗДАЁШЬ .env ФАЙЛ (2 минуты)

```bash
# Создаёшь и редактируешь .env
nano /root/spn-vpn-bot/.env
```

**Вставляешь этот текст (замени значения на СВОИ):**

```env
# ────────────────────────────────────────────────
#                TELEGRAM BOT
# ────────────────────────────────────────────────

BOT_TOKEN=your_telegram_bot_token
ADMIN_ID=your_admin_id

# ────────────────────────────────────────────────
#           SUPPORT & NEWS CHANNELS
# ────────────────────────────────────────────────

SUPPORT_URL=https://t.me/your_support
NEWS_CHANNEL_USERNAME=your_channel_name
TELEGRAPH_AGREEMENT_URL=https://telegra.ph/your-agreement-url

# ────────────────────────────────────────────────
#              REMNAWAVE API CONFIG
# ────────────────────────────────────────────────

REMNAWAVE_BASE_URL=https://your-remnawave.example/api
REMNAWAVE_API_TOKEN=your_remnawave_token
DEFAULT_SQUAD_UUID=your_squad_uuid

# ────────────────────────────────────────────────
#            CRYPTOBOT PAYMENT CONFIG
# ────────────────────────────────────────────────

CRYPTOBOT_TOKEN=your_cryptobot_token
CRYPTOBOT_API_URL=https://pay.crypt.bot/api

# ────────────────────────────────────────────────
#          SUPABASE DATABASE CONFIG
# ────────────────────────────────────────────────

SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_supabase_key
DATABASE_URL=postgresql://postgres:PASSWORD@db.your-project.supabase.co:5432/postgres

# ────────────────────────────────────────────────
#                LOGGING CONFIG
# ────────────────────────────────────────────────

LOG_LEVEL=INFO
```

⚠️ **ЗАМЕНИ все значения на СВОИ!**

**Сохраняешь:**
- Нажми **Ctrl+O**
- Нажми **Enter**
- Нажми **Ctrl+X**

**Проверяешь что сохранилось:**
```bash
cat /root/spn-vpn-bot/.env | head -20
# Должны видеть все твои переменные
```

✅ **Результат:** .env создан

---

### ЭТАЖ 7️⃣ - ТЕСТИРУЕШЬ ПОДКЛЮЧЕНИЕ К SUPABASE (2 минуты)

```bash
# Активируешь виртуальное окружение
source /root/spn-vpn-bot/venv/bin/activate

# Пытаешься запустить бота (для теста подключения)
python3 /root/spn-vpn-bot/main.py

# Если в логах видишь "Database pool initialized successfully" - ОТЛИЧНО!
# Если видишь красные ошибки - смотри раздел ПОМОЩЬ внизу
```

Нажми **Ctrl+C** чтобы остановить

✅ **Результат:** Подключение к Supabase работает

---

### ЭТАП 8️⃣ - СОЗДАЁШЬ SYSTEMD СЕРВИС (2 минуты)

```bash
# Создаёшь файл сервиса
sudo nano /etc/systemd/system/spn-bot.service
```

**Вставляешь этот текст:**

```ini
[Unit]
Description=SPN VPN Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/spn-vpn-bot
Environment="PATH=/root/spn-vpn-bot/venv/bin"
ExecStart=/root/spn-vpn-bot/venv/bin/python3 /root/spn-vpn-bot/main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**Сохраняешь:**
- Нажми **Ctrl+O**
- Нажми **Enter**
- Нажми **Ctrl+X**

---

### ЭТАП 9️⃣ - АКТИВИРУЕШЬ И ЗАПУСКАЕШЬ СЕРВИС (1 минута)

```bash
# Обновляешь systemd
sudo systemctl daemon-reload

# Включаешь автозапуск
sudo systemctl enable spn-bot

# Запускаешь бота
sudo systemctl start spn-bot

# Проверяешь статус
sudo systemctl status spn-bot
# Должно быть: Active: active (running)
```

✅ **Результат:** Бот запущен и будет автоматически запускаться при перезагрузке

---

### ЭТАП 🔟 - ПРОВЕРЯЕШЬ ЛОГИ (1 минута)

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

### ЭТАП 1️⃣1️⃣ - ТЕСТИРУЕШЬ В TELEGRAM (1 минута)

1. Открой Telegram
2. Найди бота по токену или по username
3. Напиши боту `/start`
4. Проверь что всё работает

✅ **Результат:** Бот работает с Supabase на новом сервере!

---

## 🆘 ПОМОЩЬ - ТИПИЧНЫЕ ПРОБЛЕМЫ

### ❌ "fatal: could not read Username"

**Причина:** Нет доступа к GitHub репо

**Решение:**

```bash
# Вариант 1: Если репо публичный (не нужна аутентификация)
# Просто скопируй файлы через SCP с ПК:
scp -r ~/path/to/spn-vpn-bot root@ВАШ_IP:/root/

# Вариант 2: Если репо приватный, используй HTTPS с токеном
git clone https://YOUR_GITHUB_TOKEN@github.com/YOUR_USERNAME/spn-vpn-bot.git

# Или используй SSH ключ (если настроен на GitHub)
git clone git@github.com:YOUR_USERNAME/spn-vpn-bot.git
```

---

### ❌ "Connection refused" при запуске бота

**Причина:** Суpabase недоступна или DATABASE_URL неправильный

**Проверки:**

```bash
# 1. Проверь что DATABASE_URL правильный
cat /root/spn-vpn-bot/.env | grep DATABASE_URL

# 2. Тестируй подключение (если psql установлен на VPS)
apt install -y postgresql-client
psql -h db.your-project.supabase.co \
     -U postgres \
     -d postgres \
     -c "SELECT 1"
# Введи пароль

# 3. Если "password authentication failed":
# - Проверь что пароль правильный в DATABASE_URL
# - Убедись что нет символов < > в пароле

# 4. Если "could not connect":
# - IP VPS может быть заблокирован в Supabase
# - Узнай IP VPS
curl ifconfig.me

# - Добавь IP в Supabase:
# https://supabase.com/dashboard
# → Project Settings → Database → Firewall rules
# → Add IPv4 address
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

### ❌ "Table 'users' doesn't exist"

**Причина:** Не создал таблицы в Supabase

**Решение:**

```
Перейди на https://supabase.com/dashboard
→ SQL Editor → New Query
→ Скопируй весь код из schema.sql
→ Вставь → Run
```

---

### ❌ Бот запущен но не отвечает в Telegram

```bash
# 1. Проверь что BOT_TOKEN правильный
cat /root/spn-vpn-bot/.env | grep BOT_TOKEN

# 2. Проверь статус бота
sudo systemctl status spn-bot

# 3. Посмотри логи
sudo journalctl -u spn-bot -n 50

# 4. Перезагрузи бота
sudo systemctl restart spn-bot

# 5. Посмотри логи после перезагрузки
sudo journalctl -u spn-bot -f
```

---

### ❌ "pip: command not found" или "python3: command not found"

**Причина:** Python или pip не установлены

**Решение:**

```bash
# Установляешь Python и pip
apt install -y python3 python3-pip python3-venv

# Проверяешь версию
python3 --version  # Должна быть 3.10+
pip3 --version
```

---

## 📊 ПОЛЕЗНЫЕ КОМАНДЫ ДЛЯ НОВОГО СЕРВЕРА

```bash
# Проверить IP адрес VPS
curl ifconfig.me

# Проверить свободное место на диске
df -h

# Проверить использование памяти
free -h

# Просмотр всех процессов Python
ps aux | grep python3

# Удалить старый проект (если нужно переделать)
rm -rf /root/spn-vpn-bot

# Посмотреть размер папки проекта
du -sh /root/spn-vpn-bot

# Посмотреть размер базы данных (хотя уже в облаке)
du -sh /root/spn-vpn-bot/spn_vpn_bot.db

# Перезагрузить сервер (если нужно)
sudo reboot
# После перезагрузки бот автоматически запустится благодаря systemd

# Остановить бота
sudo systemctl stop spn-bot

# Запустить бота
sudo systemctl start spn-bot

# Перезагрузить бота
sudo systemctl restart spn-bot

# Проверить статус
sudo systemctl status spn-bot

# Смотреть последние 100 строк логов
sudo journalctl -u spn-bot -n 100

# Смотреть логи в реальном времени
sudo journalctl -u spn-bot -f

# Поиск ошибок в логах
sudo journalctl -u spn-bot | grep -i error

# Очистить логи systemd (если много занимают места)
sudo journalctl --vacuum=100M
```

---

## ✅ ФИНАЛЬНЫЙ ЧЕК-ЛИСТ ДЛЯ НОВОГО СЕРВЕРА

После развёртывания проверь:

- [ ] `ssh root@ВАШ_IP` - подключение работает
- [ ] `/root/spn-vpn-bot` - папка проекта существует
- [ ] `/root/spn-vpn-bot/.env` - .env файл создан и содержит все переменные
- [ ] `pip list | grep asyncpg` - asyncpg установлен
- [ ] `/etc/systemd/system/spn-bot.service` - сервис создан
- [ ] `sudo systemctl status spn-bot` - показывает `active (running)`
- [ ] `sudo journalctl -u spn-bot -f` - логи показывают "Database pool initialized successfully"
- [ ] Бот отвечает на `/start` в Telegram
- [ ] https://supabase.com/dashboard - видны таблицы в БД

---

## 🎉 ВСЕ ГОТОВО!

Теперь у тебя есть:
- ✅ **Новый VPS** с установленным ботом
- ✅ **PostgreSQL в Supabase** в облаке
- ✅ **Автозапуск** - бот запускается при перезагрузке
- ✅ **Логирование** - можно смотреть логи через systemd

**Бот работает 24/7 даже когда ты спишь! 🚀**

---

## 💡 ЕСЛИ ХОЧЕШЬ СДЕЛАТЬ БОЛЬШЕ

### Добавить Firewall (защита от хакеров)
```bash
apt install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 22
ufw enable
```

### Добавить мониторинг (знать когда что-то сломалось)
```bash
# Установить htop для мониторинга
apt install -y htop
htop  # Нажми q чтобы выйти

# Или использовать Sentry для мониторинга ошибок
# (требует отдельная настройка, смотри Sentry docs)
```

### Сделать резервную копию проекта
```bash
tar -czf /root/spn-vpn-bot-backup-$(date +%Y%m%d).tar.gz /root/spn-vpn-bot
```

### Включить шифрование диска (если очень важна безопасность)
```bash
# Это требует переустановку сервера, обычно не нужно для бота
```

---

## 🚨 ВАЖНЫЕ ЗАМЕЧАНИЯ

1. **Резервные копии БД**: Supabase сам делает резервные копии каждый час
2. **Обновления**: Просто `git pull origin main` + `sudo systemctl restart spn-bot`
3. **IP Firewall Supabase**: Добавь IP VPS в Supabase если нужна дополнительная безопасность
4. **Пароли**: Никогда не публикуй .env в GitHub (у тебя должен быть .gitignore)
5. **Масштабирование**: PostgreSQL на Supabase может справиться с миллионами пользователей

---

## ❓ ЕСЛИ ОСТАЛИСЬ ВОПРОСЫ

1. Посмотри логи: `sudo journalctl -u spn-bot -f`
2. Проверь .env: `cat /root/spn-vpn-bot/.env`
3. Смотри документацию в проекте:
   - QUICK_START_SUPABASE.md
   - SUPABASE_MIGRATION.md
   - DEPLOY_UPDATE_EXISTING_SERVER.md (этот файл)
