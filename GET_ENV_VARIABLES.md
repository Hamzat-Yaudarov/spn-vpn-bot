# 🔑 Где и как получить каждое значение для .env

## 1️⃣ BOT_TOKEN (Telegram Bot API Token)

**Шаги:**
1. Откройте Telegram и найдите @BotFather
2. Напишите `/newbot`
3. Укажите имя бота (например: "SPN VPN Bot")
4. Укажите username бота (должен заканчиваться на "bot", например: "spn_vpn_bot")
5. BotFather вышлет вам токен

**Что получится:**
```
123456789:ABCdefGHIjklmnoPQRstuvWXYZ
```

**Вставить в .env:**
```env
BOT_TOKEN=123456789:ABCdefGHIjklmnoPQRstuvWXYZ
```

---

## 2️⃣ ADMIN_ID (Ваш Telegram ID)

**Шаги:**
1. Откройте Telegram и найдите @getmyid_bot
2. Напишите любое сообщение (например: "hi")
3. Бот ответит вам с вашим ID

**Что получится:**
```
123456789
```

**Вставить в .env:**
```env
ADMIN_ID=123456789
```

---

## 3️⃣ SUPPORT_URL (Канал поддержки)

**Это просто ваша ссылка в Telegram**

**Примеры:**
```
https://t.me/yourusername
https://t.me/yoursupportbot
```

**Вставить в .env:**
```env
SUPPORT_URL=https://t.me/yourusername
```

---

## 4️⃣ NEWS_CHANNEL_USERNAME (Канал новостей)

**Это имя вашего канала БЕЗ @**

1. Создайте канал в Telegram (если ещё нет)
2. Посмотрите его имя (без @)

**Примеры:**
```
spn_newsvpn
my_news_channel
```

**Вставить в .env:**
```env
NEWS_CHANNEL_USERNAME=spn_newsvpn
```

---

## 5️⃣ TELEGRAPH_AGREEMENT_URL (Пользовательское соглашение)

**Шаги:**
1. Откройте https://telegra.ph
2. Создайте страницу с пользовательским соглашением
3. Скопируйте ссылку со страницы браузера

**Примеры:**
```
https://telegra.ph/User-Agreement-01-01
https://telegra.ph/SPN-Agreement-12-01
```

**Вставить в .env:**
```env
TELEGRAPH_AGREEMENT_URL=https://telegra.ph/User-Agreement-01-01
```

---

## 6️⃣ REMNAWAVE_BASE_URL (Remnawave API URL)

**Это должен предоставить ваш провайдер Remnawave**

**Примеры (обычно выглядит так):**
```
https://api.remnawave.com/api
https://remnawave.yourcompany.com/api
```

**Где узнать:**
- Спросите у провайдера Remnawave
- Смотрите в документации провайдера
- Спросите в технической поддержке

**Вставить в .env:**
```env
REMNAWAVE_BASE_URL=https://api.remnawave.com/api
```

---

## 7️⃣ REMNAWAVE_API_TOKEN (Remnawave API Token)

**Это должен предоставить ваш провайдер Remnawave**

**Обычно выглядит так:**
```
sk_test_abc123def456ghi789...
sk_live_xyz789abc123...
```

**Где узнать:**
- Личный кабинет провайдера Remnawave
- Попросите у технической поддержки

**Вставить в .env:**
```env
REMNAWAVE_API_TOKEN=sk_test_abc123def456...
```

---

## 8️⃣ DEFAULT_SQUAD_UUID (UUID группы Remnawave)

**Где найти:**
1. Войдите в админ-панель Remnawave
2. Найдите раздел "Squads" или "Groups"
3. Скопируйте UUID вашей основной группы

**Обычно выглядит так:**
```
550e8400-e29b-41d4-a716-446655440000
```

**Вставить в .env:**
```env
DEFAULT_SQUAD_UUID=550e8400-e29b-41d4-a716-446655440000
```

---

## 9️⃣ CRYPTOBOT_TOKEN (CryptoBot API Token)

**Если вы используете CryptoBot для оплат:**

**Шаги:**
1. Откройте https://pay.crypt.bot
2. Авторизуйтесь или зарегистрируйтесь
3. Найдите раздел "API"
4. Скопируйте ваш токен

**Обычно выглядит так:**
```
123456:ABC...
```

**Вставить в .env:**
```env
CRYPTOBOT_TOKEN=123456:ABC...
```

**Если не используете CryptoBot:**
```env
CRYPTOBOT_TOKEN=
CRYPTOBOT_API_URL=https://pay.crypt.bot/api
```

---

## 🔟 YOOKASSA параметры

### YOOKASSA_SHOP_ID (ID магазина)

**Если вы используете Yookassa:**

**Шаги:**
1. Откройте https://yookassa.ru
2. Авторизуйтесь в личном кабинете
3. Найдите "ID магазина" или "Shop ID"
4. Скопируйте числовой ID

**Обычно это просто число:**
```
12345678
```

**Вставить в .env:**
```env
YOOKASSA_SHOP_ID=12345678
```

---

### YOOKASSA_SECRET_KEY (Секретный ключ Yookassa)

**Шаги:**
1. Откройте https://yookassa.ru
2. Перейдите в "Параметры" → "API"
3. Скопируйте секретный ключ (Secret key)

**Обычно выглядит так:**
```
test_abc123def456ghi789...
live_xyz789abc123...
```

**Вставить в .env:**
```env
YOOKASSA_SECRET_KEY=test_abc123...
```

**Если не используете Yookassa:**
```env
YOOKASSA_SHOP_ID=
YOOKASSA_SECRET_KEY=
```

---

## 1️⃣1️⃣ DATABASE_URL (PostgreSQL Connection String)

**Это зависит от вашей БД:**

### Если используете Supabase:
1. Откройте https://supabase.com
2. Откройте ваш проект
3. Перейдите в "Database" → "Connection"
4. Выберите "URI"
5. Скопируйте строку подключения

**Обычно выглядит так:**
```
postgresql://postgres:password@db.supabase.co:5432/postgres
```

### Если используете другой PostgreSQL:
```
postgresql://username:password@hostname:5432/database_name
```

**Вставить в .env:**
```env
DATABASE_URL=postgresql://postgres:mypassword@db.supabase.co:5432/postgres
```

⚠️ **ВАЖНО**: Замените `password` на реальный пароль!

---

## 1️⃣2️⃣ SUPABASE параметры (опционально, только если используете Supabase)

### SUPABASE_URL

**Шаги:**
1. Откройте https://supabase.com
2. Откройте ваш проект
3. Перейдите в "Settings" → "API"
4. Найдите "Project URL"
5. Скопируйте URL

**Обычно выглядит так:**
```
https://myproject.supabase.co
```

**Вставить в .env:**
```env
SUPABASE_URL=https://myproject.supabase.co
```

---

### SUPABASE_KEY

**Где взять:**
1. Откройте https://supabase.com
2. Откройте ваш проект
3. Перейдите в "Settings" → "API"
4. Скопируйте один из ключей (обычно "anon public key" или "service role key")

**Обычно выглядит так:**
```
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

**Вставить в .env:**
```env
SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

---

## 1️⃣3️⃣ 3X-UI параметры (Встроенные значения)

**ЭТО ВСТРОЕННЫЕ ЗНАЧЕНИЯ - НЕ МЕНЯЙТЕ!**

```env
XUI_PANEL_URL=https://51.250.117.234:2053
XUI_PANEL_PATH=/sXvL8myMex46uSa3NP/panel
XUI_USERNAME=U0UiUl76S0
XUI_PASSWORD=2W1SwoZ0Ix
SUB_PORT=2096
SUB_EXTERNAL_HOST=51.250.117.234
INBOUND_ID=1
```

---

## 1️⃣4️⃣ Параметры Webhook (опционально)

### LOG_LEVEL
**Выберите один из:**
- `INFO` — нормальные логи (рекомендуется)
- `DEBUG` — подробные логи (для отладки)
- `ERROR` — только ошибки

```env
LOG_LEVEL=INFO
```

### WEBHOOK_CONFIGURATION

```env
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=8000
WEBHOOK_USE_POLLING=False
```

---

## ✅ Полный чек-лист

Перед тем как запустить бота, убедитесь что заполнили:

### ОБЯЗАТЕЛЬНЫЕ:
- [ ] `BOT_TOKEN` — от @BotFather
- [ ] `ADMIN_ID` — ваш Telegram ID
- [ ] `SUPPORT_URL` — ссылка поддержки
- [ ] `NEWS_CHANNEL_USERNAME` — имя канала новостей
- [ ] `TELEGRAPH_AGREEMENT_URL` — ссылка на соглашение
- [ ] `REMNAWAVE_BASE_URL` — URL API Remnawave
- [ ] `REMNAWAVE_API_TOKEN` — токен Remnawave
- [ ] `DEFAULT_SQUAD_UUID` — UUID группы Remnawave
- [ ] `DATABASE_URL` — строка подключения БД

### НА ВЫБОР (ХОТЯ БЫ ОДИН):
- [ ] `CRYPTOBOT_TOKEN` (если используете CryptoBot)
- [ ] `YOOKASSA_SHOP_ID` + `YOOKASSA_SECRET_KEY` (если используете Yookassa)

### ОПЦИОНАЛЬНО:
- [ ] `SUPABASE_URL` + `SUPABASE_KEY` (если используете Supabase)
- [ ] Параметры Webhook

---

## 🚀 Готовые примеры .env

### Минимальный вариант (только Remnawave + CryptoBot):
```bash
BOT_TOKEN=123456789:ABCdefGHIjklmnoPQRstuvWXYZ
ADMIN_ID=987654321
SUPPORT_URL=https://t.me/mysupport
NEWS_CHANNEL_USERNAME=mynewschannel
TELEGRAPH_AGREEMENT_URL=https://telegra.ph/Agreement-01-01
REMNAWAVE_BASE_URL=https://api.remnawave.com/api
REMNAWAVE_API_TOKEN=sk_test_abc123...
DEFAULT_SQUAD_UUID=550e8400-e29b-41d4-a716-446655440000
CRYPTOBOT_TOKEN=123456:ABC...
CRYPTOBOT_API_URL=https://pay.crypt.bot/api
DATABASE_URL=postgresql://postgres:pass@db.supabase.co:5432/postgres
XUI_PANEL_URL=https://51.250.117.234:2053
XUI_PANEL_PATH=/sXvL8myMex46uSa3NP/panel
XUI_USERNAME=U0UiUl76S0
XUI_PASSWORD=2W1SwoZ0Ix
SUB_PORT=2096
SUB_EXTERNAL_HOST=51.250.117.234
INBOUND_ID=1
LOG_LEVEL=INFO
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=8000
WEBHOOK_USE_POLLING=False
```

---

## ❓ Если что-то не ясно

1. Прочитайте `ENV_GUIDE_RU.md` для подробного объяснения каждой переменной
2. Используйте `.env.example` как шаблон
3. Используйте `.env.minimal` для быстрого старта
4. Спросите у провайдера (Remnawave, CryptoBot, Yookassa) если не можете найти параметр
