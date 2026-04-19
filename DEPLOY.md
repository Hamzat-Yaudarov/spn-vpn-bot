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
BOT_TOKEN=your_telegram_bot_token
ADMIN_ID=your_admin_id
SUPPORT_URL=https://t.me/your_support
NEWS_CHANNEL_USERNAME=your_channel_username
TELEGRAPH_AGREEMENT_URL=https://telegra.ph/your-agreement
REMNAWAVE_BASE_URL=https://your-remnawave.example/api
REMNAWAVE_API_TOKEN=your_remnawave_api_token
DEFAULT_SQUAD_UUID=your_default_squad_uuid
CRYPTOBOT_TOKEN=your_cryptobot_token
CRYPTOBOT_API_URL=https://pay.crypt.bot/api
YOOKASSA_SHOP_ID=your_yookassa_shop_id
YOOKASSA_SECRET_KEY=your_yookassa_secret_key
DATABASE_URL=postgresql://postgres:password@db.your-project.supabase.co:5432/postgres
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

Основные данные хранятся в PostgreSQL/Supabase. Резервные копии базы делаются средствами провайдера БД.

Для локальной резервной копии конфигурации полезно сохранить `.env`:

```bash
cp /home/bot/spn-vpn-bot/.env /home/bot/spn-vpn-bot/.env.backup.$(date +%Y%m%d_%H%M%S)
```

---

## Мониторинг

Для мониторинга состояния бота можно использовать:

```bash
# Использование памяти
ps aux | grep "[p]ython3 main.py"

# Проверка файла конфигурации
ls -lh /home/bot/spn-vpn-bot/.env

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
