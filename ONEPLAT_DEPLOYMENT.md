# Развёртывание интеграции 1Plat

## Что было изменено

### Новые файлы

1. **`services/oneplat.py`** - Сервис для работы с API 1Plat
   - `create_oneplat_payment()` - Создание платежа
   - `get_payment_info()` - Получение информации о платеже
   - `verify_callback()` - Проверка подписей callback'ов
   - `verify_callback_signature()` - Проверка одной подписи

2. **`handlers/webhooks.py`** - Webhook обработчик для callback'ов от 1Plat
   - `/1plat-webhook` - Получение callback'ов
   - Проверка подписи и обработка платежей

3. **`1PLAT_INTEGRATION.md`** - Подробная документация по интеграции

### Обновлённые файлы

1. **`config.py`**
   - Добавлены переменные для 1Plat:
     - `ONEPLAT_SHOP_ID`
     - `ONEPLAT_SHOP_SECRET`
     - `ONEPLAT_BASE_URL`
     - `ONEPLAT_CALLBACK_URL`

2. **`database.py`**
   - Добавлена миграция для полей `payment_guid` и `payment_method`
   - Добавлены функции для работы с 1Plat платежами:
     - `create_oneplat_payment()`
     - `get_payment_by_guid()`
     - `get_pending_oneplat_payments()`
     - `update_payment_status_by_guid()`
   - Обновлена `update_payment_status_by_invoice()` для работы с обоими провайдерами

3. **`handlers/subscription.py`**
   - Заменён обработчик `pay_yookassa` на реальную интеграцию с 1Plat
   - Добавлен `process_pay_yookassa()` - показывает выбор способа оплаты
   - Добавлен `process_pay_1plat()` - создание платежа и показ реквизитов
   - Добавлен `process_check_oneplat_payment()` - проверка статуса платежа
   - Добавлено состояние `choosing_1plat_method` в FSM

4. **`states.py`**
   - Добавлено состояние `choosing_1plat_method`

5. **`main.py`**
   - Добавлена интеграция Quart для webhook сервера
   - Запуск webhook сервера на порте 8080
   - Параллельный запуск bot polling и webhook сервера

6. **`.env.example`**
   - Добавлены переменные для 1Plat

7. **`requirements.txt`**
   - Добавлена зависимость `quart>=0.18.0`

## Что такое 1Plat

1Plat - это платежная система для приёма платежей от пользователей через:
- 💳 Банковские карты (российские и иностранные)
- 📱 СБП (система быстрых платежей по номеру телефона)

## Процесс оплаты

```
Пользователь
     ↓
Выбирает тариф в Telegram боте
     ↓
Выбирает способ оплаты: карта или СБП
     ↓
Бот создаёт платеж через API 1Plat
     ↓
Получает реквизиты платежа
     ↓
Показывает реквизиты пользователю
     ↓
Пользователь оплачивает платёж
     ↓
1Plat отправляет callback на вебхук
     ↓
Бот проверяет подпись callback'а
     ↓
Если платёж оплачен → активируется подписка в Remnawave
     ↓
Бот отправляет ссылку подписки пользователю
```

## Инструкция по развёртыванию на новом сервере

### Требования

- Python 3.10+
- PostgreSQL (Supabase)
- Доступ к интернету
- Домен `spn.bot.idlebat.online` (или ваш домен)

### Шаг 1: Подготовка сервера

```bash
# Обновляем систему
sudo apt update && sudo apt upgrade -y

# Устанавливаем Python и зависимости
sudo apt install -y python3.10 python3.10-venv python3-pip git

# Создаём папку для бота
mkdir -p /opt/spn-vpn-bot
cd /opt/spn-vpn-bot
```

### Шаг 2: Клонирование репозитория

```bash
git clone https://github.com/Hamzat-Yaudarov/spn-vpn-bot.git .
```

### Шаг 3: Создание виртуального окружения

```bash
python3.10 -m venv venv
source venv/bin/activate
```

### Шаг 4: Установка зависимостей

```bash
pip install -r requirements.txt
```

### Шаг 5: Настройка переменных окружения

```bash
cp .env.example .env
nano .env
```

Заполните следующие переменные:

```env
# Telegram Bot
BOT_TOKEN=your_telegram_bot_token

# Базы данных
DATABASE_URL=postgresql://user:password@host:port/database
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_supabase_anon_key

# Remnawave
REMNAWAVE_BASE_URL=https://spn.idlebat.online/api
REMNAWAVE_API_TOKEN=your_remnawave_token
DEFAULT_SQUAD_UUID=your_squad_uuid

# CryptoBot (если используется)
CRYPTOBOT_TOKEN=your_cryptobot_token
CRYPTOBOT_API_URL=https://pay.crypt.bot/api

# 1Plat (НОВОЕ)
ONEPLAT_SHOP_ID=12345
ONEPLAT_SHOP_SECRET=your_shop_secret_key
ONEPLAT_BASE_URL=https://1plat.cash
ONEPLAT_CALLBACK_URL=https://spn.bot.idlebat.online/1plat-webhook

# Остальные переменные...
ADMIN_ID=your_admin_id
SUPPORT_URL=https://t.me/your_support
NEWS_CHANNEL_USERNAME=your_channel
TELEGRAPH_AGREEMENT_URL=https://telegra.ph/your-agreement
```

### Шаг 6: Проверка подключения к БД

```bash
# Активируем виртуальное окружение
source venv/bin/activate

# Тестируем подключение
python -c "import asyncio; from database import init_db; asyncio.run(init_db())"
```

### Шаг 7: Запуск бота

```bash
source venv/bin/activate
python main.py
```

Бот будет запущен с:
- **Telegram polling** на стандартном порте
- **Webhook сервер (Quart)** на порте **8080**

### Шаг 8: Настройка 1Plat в административной панели

1. Перейдите в ЛК 1Plat
2. Найдите:
   - **Shop ID** → скопируйте в `ONEPLAT_SHOP_ID`
   - **Shop Secret** → скопируйте в `ONEPLAT_SHOP_SECRET`
3. В настройках магазина установите callback URL:
   ```
   https://spn.bot.idlebat.online/1plat-webhook
   ```
4. Выполните верификацию домена одним из методов (TXT запись, файл или meta тег)

### Шаг 9: Настройка Nginx (для проброса на 8080)

```bash
sudo nano /etc/nginx/sites-available/spn.bot.idlebat.online
```

Добавьте:

```nginx
server {
    listen 80;
    server_name spn.bot.idlebat.online;

    # Webhook endpoint
    location /1plat-webhook {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Для верификации домена 1Plat (если используется файл)
    location /1plat.txt {
        alias /opt/spn-vpn-bot/1plat.txt;
    }
}
```

Активируйте:

```bash
sudo ln -s /etc/nginx/sites-available/spn.bot.idlebat.online \
           /etc/nginx/sites-enabled/spn.bot.idlebat.online
sudo nginx -t
sudo systemctl reload nginx
```

### Шаг 10: SSL сертификат (Let's Encrypt)

```bash
sudo apt install certbot python3-certbot-nginx -y
sudo certbot certonly --nginx -d spn.bot.idlebat.online
```

Обновите Nginx конфиг для HTTPS:

```nginx
server {
    listen 443 ssl http2;
    server_name spn.bot.idlebat.online;

    ssl_certificate /etc/letsencrypt/live/spn.bot.idlebat.online/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/spn.bot.idlebat.online/privkey.pem;

    location /1plat-webhook {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /1plat.txt {
        alias /opt/spn-vpn-bot/1plat.txt;
    }
}

server {
    listen 80;
    server_name spn.bot.idlebat.online;
    return 301 https://$server_name$request_uri;
}
```

Перезагрузите Nginx:

```bash
sudo systemctl reload nginx
```

### Шаг 11: Создание systemd сервиса

```bash
sudo nano /etc/systemd/system/spn-bot.service
```

Добавьте:

```ini
[Unit]
Description=SPN VPN Bot
After=network.target postgresql.service

[Service]
Type=simple
User=bot
WorkingDirectory=/opt/spn-vpn-bot
Environment="PATH=/opt/spn-vpn-bot/venv/bin"
ExecStart=/opt/spn-vpn-bot/venv/bin/python main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Активируйте сервис:

```bash
sudo systemctl daemon-reload
sudo systemctl enable spn-bot
sudo systemctl start spn-bot
```

Проверьте статус:

```bash
sudo systemctl status spn-bot
sudo journalctl -u spn-bot -f
```

## Проверка работоспособности

### Проверка webhook'а

```bash
# Убедитесь, что сервер слушает на 8080
netstat -tlnp | grep 8080

# Или используйте curl
curl -X POST http://localhost:8080/1plat-webhook \
  -H "Content-Type: application/json" \
  -d '{"test": "data"}'
```

### Проверка логов

```bash
# Логи systemd сервиса
sudo journalctl -u spn-bot -f

# Или прямой запуск с выводом логов
cd /opt/spn-vpn-bot
source venv/bin/activate
python main.py
```

### Тестирование платежа

1. Напишите боту `/start`
2. Принимите условия
3. Нажмите "Оформить подписку"
4. Выберите тариф
5. Выберите "Yookassa" (теперь это 1Plat)
6. Выберите метод оплаты (карта или СБП)
7. Скопируйте реквизиты и проверьте в логах

## Мониторинг

### Важные метрики

- Статус webhook'а (получение callback'ов от 1Plat)
- Проверка подписей callback'ов
- Активация подписок после оплаты
- Ошибки подключения к Remnawave

### Команды для проверки

```bash
# Проверка логов 1Plat интеграции
sudo journalctl -u spn-bot -f | grep 1plat

# Проверка запросов к 1Plat API
curl -X GET https://1plat.cash/api/shop/info/by-api \
  -H "x-shop: YOUR_SHOP_ID" \
  -H "x-secret: YOUR_SHOP_SECRET"

# Проверка webhook'а с тестовым callback'ом
curl -X POST https://spn.bot.idlebat.online/1plat-webhook \
  -H "Content-Type: application/json" \
  -d '{
    "guid": "test-guid",
    "payment_id": 123,
    "status": 1,
    "merchant_id": "1234",
    "user_id": 123456789,
    "amount": 100,
    "signature": "test",
    "signature_v2": "test"
  }'
```

## Решение проблем

### Webhook не получает callback'и

1. Проверьте, что домен верифицирован в 1Plat ЛК
2. Проверьте callback URL в 1Plat ЛК
3. Проверьте, что Nginx пробрасывает на 8080
4. Посмотрите логи: `sudo journalctl -u spn-bot -f | grep webhook`

### Ошибка подключения к БД

```bash
# Проверьте переменную DATABASE_URL
echo $DATABASE_URL

# Попробуйте подключиться через psql
psql "postgresql://user:password@host:port/database"
```

### Сертификат SSL истёк

```bash
sudo certbot renew --dry-run
sudo certbot renew
sudo systemctl reload nginx
```

## Откат на случай проблем

Если что-то пошло не так, вернитесь на старую версию:

```bash
cd /opt/spn-vpn-bot
git log --oneline
git revert <commit-hash>
# или
git checkout <previous-branch>
```

## Дополнительные ссылки

- [Документация 1Plat](#1PLAT_INTEGRATION.md)
- [Основной README](README.md)
- [Документация Quart](https://quart.palletsprojects.com/)
- [Документация aiogram](https://docs.aiogram.dev/)
