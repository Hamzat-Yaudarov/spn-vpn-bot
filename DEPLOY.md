# 🚀 Развёртывание SPN VPN Bot на VPS

## Требования

- **OS:** Ubuntu 18.04+ или CentOS 7+
- **Python:** 3.10+
- **SSH доступ** к VPS

## Быстрое развёртывание (5 минут)

### 1️⃣ Подключитесь к VPS

```bash
ssh root@YOUR_VPS_IP
```

### 2️⃣ Клонируйте репозиторий

```bash
cd /home
git clone https://github.com/YOUR_USERNAME/spn-vpn-bot.git
cd spn-vpn-bot
```

Или если используете URL с токеном:
```bash
git clone https://your-token@github.com/YOUR_USERNAME/spn-vpn-bot.git
cd spn-vpn-bot
```

### 3️⃣ Создайте .env файл

```bash
nano .env
```

Вставьте ваши переменные (те же что были на macOS):
```env
BOT_TOKEN=8520411926:AAFcduqngB2ZMCp3RS4yZ8hwkcyf-yOmWyU
ADMIN_ID=6910097562
SUPPORT_URL=https://t.me/Youdarov
NEWS_CHANNEL_USERNAME=spn_newsvpn
TELEGRAPH_AGREEMENT_URL=https://telegra.ph/Polzovatelskoe-soglashenie-dlya-servisa-SPN-Uskoritel-interneta-01-01
REMNAWAVE_BASE_URL=https://spn.idlebat.online/api
REMNAWAVE_API_TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1dWlkIjoiM2FkMmM4YmQtNDQ2Yy00YzE0LThhZGItMzViODdjZTVkNDc3IiwidXNlcm5hbWUiOm51bGwsInJvbGUiOiJBUEkiLCJpYXQiOjE3Njc5NzM4ODQsImV4cCI6MTA0MDc4ODc0ODR9.7T-2_nK8I3k7fgtlu1O0mt7WyWBNwsCItYsEJSD2SbI
DEFAULT_SQUAD_UUID=1fa28b9d-b745-4fd7-b93c-ce66f7ff4934
CRYPTOBOT_TOKEN=508663:AAZcVJabRaP6NTah1LVJVl3p1E0GYTid9GK
CRYPTOBOT_API_URL=https://pay.crypt.bot/api
DB_FILE=spn_vpn_bot.db
LOG_LEVEL=INFO
```

Нажмите **Ctrl+O**, **Enter**, **Ctrl+X** для сохранения.

### 4️⃣ Запустите deploy скрипт

```bash
chmod +x deploy.sh
./deploy.sh
```

### 5️⃣ Проверьте что всё работает

```bash
source venv/bin/activate
python3 main.py
```

Если видите логи без ошибок - отлично! ✅

Нажмите **Ctrl+C** чтобы остановить бота.

---

## Развёртывание с systemd (Автозапуск)

После успешного первого теста можно настроить автоматический запуск.

### 1️⃣ Создайте пользователя для бота

```bash
sudo useradd -m -s /bin/bash bot
sudo chown -R bot:bot /home/bot/spn-vpn-bot
```

### 2️⃣ Установите systemd сервис

```bash
sudo cp spn-vpn-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable spn-vpn-bot
sudo systemctl start spn-vpn-bot
```

### 3️⃣ Проверьте статус

```bash
sudo systemctl status spn-vpn-bot
```

Должно быть: `Active: active (running)`

### 4️⃣ Смотрите логи

```bash
sudo journalctl -u spn-vpn-bot -f
```

---

## Управление сервисом

```bash
# Запустить
sudo systemctl start spn-vpn-bot

# Остановить
sudo systemctl stop spn-vpn-bot

# Перезагрузить
sudo systemctl restart spn-vpn-bot

# Смотреть статус
sudo systemctl status spn-vpn-bot

# Смотреть последние логи
sudo journalctl -u spn-vpn-bot -n 100

# Следить за логами в реальном времени
sudo journalctl -u spn-vpn-bot -f
```

---

## Проблемы и решения

### ❌ "ImportError: No module named 'aiogram'"

Скорее всего виртуальное окружение не активировано или dependencies не установлены.

**Решение:**
```bash
cd /home/bot/spn-vpn-bot
source venv/bin/activate
pip install -r requirements.txt
```

### ❌ "ModuleNotFoundError: No module named 'dotenv'"

Зависимости не установлены.

**Решение:**
```bash
pip install -r requirements.txt
```

### ❌ "Connection refused" при запуске

Проверьте что bot token правильный в .env файле.

**Решение:**
```bash
cat .env | grep BOT_TOKEN
```

### ❌ Бот не отвечает на команды

1. Проверьте что бот работает:
   ```bash
   sudo systemctl status spn-vpn-bot
   ```

2. Смотрите логи:
   ```bash
   sudo journalctl -u spn-vpn-bot -f
   ```

3. Убедитесь что вы подписаны на бота в Telegram

---

## Обновление кода

Если обновили код в репозитории:

```bash
cd /home/bot/spn-vpn-bot
git pull origin main
sudo systemctl restart spn-vpn-bot
```

---

## Резервная копия БД

База данных SQLite хранится в `spn_vpn_bot.db`.

Для резервной копии:

```bash
cp /home/bot/spn-vpn-bot/spn_vpn_bot.db /home/bot/spn-vpn-bot/backups/spn_vpn_bot.db.$(date +%Y%m%d_%H%M%S)
```

---

## Мониторинг

Для мониторинга состояния бота можно использовать:

```bash
# Использование памяти
ps aux | grep "[p]ython3 main.py"

# Размер БД
du -h /home/bot/spn-vpn-bot/spn_vpn_bot.db

# Свободное место на диске
df -h
```

---

## Поддержка

Если возникли проблемы, проверьте:
1. Правильность .env файла
2. Доступ в интернет на VPS
3. Логи бота: `sudo journalctl -u spn-vpn-bot -f`
4. Версию Python: `python3 --version` (должна быть 3.10+)
