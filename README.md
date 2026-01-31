# SPN VPN Bot

Telegram бот для управления VPN подписками с поддержкой платежей через CryptoBot и интеграцией с Remnawave API.

## Структура проекта

```
project_root/
├── main.py                 # Точка входа приложения
├── config.py              # Конфигурация и переменные окружения
├── database.py            # Работа с базой данных SQLite
├── states.py              # FSM состояния
├── .env                   # Переменные окружения
├── requirements.txt       # Зависимости Python
├── handlers/              # Обработчики команд и callback'ов
│   ├── __init__.py
│   ├── start.py          # Команда /start и главное меню
│   ├── callbacks.py      # Общие callback обработчики
│   ├── subscription.py   # Покупка и продление подписок
│   ├── gift.py           # Получение подарка
│   ├── referral.py       # Реферальная программа
│   ├── promo.py          # Промокоды
│   └── admin.py          # Админ команды
└── services/              # Сервисы и интеграции
    ├── __init__.py
    ├── remnawave.py      # Интеграция с Remnawave API
    └── cryptobot.py      # Интеграция с CryptoBot API
```

## Установка

### Требования
- Python 3.10+
- pip или другой package manager

### Шаги установки

1. **Клонируйте репозиторий:**
```bash
git clone <your-repo>
cd spn-vpn-bot
```

2. **Установите зависимости:**
```bash
pip install -r requirements.txt
```

3. **Настройте переменные окружения:**
Отредактируйте файл `.env` и добавьте необходимые значения:
```env
BOT_TOKEN=your_telegram_bot_token
ADMIN_ID=your_admin_telegram_id
# ... остальные переменные
```

## Запуск

### Локальный запуск на macOS

```bash
# Убедитесь что вы в корне проекта
python3 main.py
```

Бот будет работать в режиме polling (прослушивание обновлений от Telegram).

### Webhook сервер

Бот также запускает внутренний webhook сервер для получения уведомлений о платежах от CryptoBot и Yookassa:

```
Endpoints:
  POST /webhook/cryptobot  - для уведомлений от CryptoBot
  POST /webhook/yookassa   - для уведомлений от Yookassa
  GET  /health             - проверка статуса
```

Webhook сервер слушает на адресе `0.0.0.0:{WEBHOOK_PORT}` (по умолчанию 8000).

## Функциональность

### Пользовательские функции
- ✅ Выбор и оплата подписок (через CryptoBot и Yookassa)
- ✅ Интеграция с Remnawave для управления VPN аккаунтами
- ✅ Платежи через CryptoBot (USDT, TON, BTC)
- ✅ Платежи через Yookassa (карты, СБП)
- ✅ Реферальная программа (получение бонусов за приглашённых друзей)
- ✅ Промокоды
- ✅ Подарки за подписку на канал новостей
- ✅ Просмотр статуса подписки

### Доступные тарифы

| Тариф | Период | Цена |
|-------|--------|------|
| 1m | 1 месяц (30 дней) | 200 ₽ |
| 3m | 3 месяца (90 дней) | 449 ₽ |
| 6m | 6 месяцев (180 дней) | 790 ₽ |
| 12m | 1 год (365 дней) | 1200 ₽ |

### Администраторские команды
- `/new_code CODE DAYS LIMIT` - Создать новый промокод
- `/give_sub TG_ID DAYS` - Выдать подписку пользователю
- `/stats` - Получить статистику (в разработке)

## Конфигурация

### Переменные окружения (.env)

| Переменная | Описание |
|-----------|---------|
| `BOT_TOKEN` | Токен Telegram бота |
| `ADMIN_ID` | ID администратора Telegram |
| `SUPPORT_URL` | URL поддержки (Telegram ссылка) |
| `NEWS_CHANNEL_USERNAME` | Имя канала новостей |
| `TELEGRAPH_AGREEMENT_URL` | Ссылка на условия использования |
| `REMNAWAVE_BASE_URL` | URL API Remnawave |
| `REMNAWAVE_API_TOKEN` | API токен Remnawave |
| `DEFAULT_SQUAD_UUID` | UUID сквада по умолчанию |
| `CRYPTOBOT_TOKEN` | Токен CryptoBot |
| `CRYPTOBOT_API_URL` | URL API CryptoBot |
| `YOOKASSA_SHOP_ID` | ID магазина Yookassa |
| `YOOKASSA_SECRET_KEY` | Secret ключ Yookassa |
| `WEBHOOK_HOST` | IP адрес webhook сервера (по умолчанию `0.0.0.0`) |
| `WEBHOOK_PORT` | Порт webhook сервера (по умолчанию `8000`) |
| `WEBHOOK_USE_POLLING` | Использовать polling вместо webhook'ов (`true`/`false`) |
| `DATABASE_URL` | Строка подключения к PostgreSQL |
| `LOG_LEVEL` | Уровень логирования (INFO, DEBUG, WARNING и т.д.) |

### Настройка Webhook'ов платёжных систем

**ВАЖНО:** Для работы webhook'ов нужно настроить уведомления в панелях платёжных систем!

#### CryptoBot Webhook

1. Перейдите на [CryptoBot Dashboard](https://app.cryptobot.me/)
2. В разделе "Webhooks" или "Settings" установите webhook URL:
   ```
   https://your-domain.com/webhook/cryptobot
   ```
   где `your-domain.com` - ваш домен или IP адрес сервера

3. Убедитесь, что webhook отправляет события типа `invoice.paid`

4. Тестируйте webhook на панели CryptoBot

#### Yookassa Webhook

1. Войдите на [Yookassa Merchant Dashboard](https://yookassa.ru/my/merchant/integration)
2. В разделе "Webhook'и и уведомления" добавьте endpoint:
   ```
   https://your-domain.com/webhook/yookassa
   ```

3. Убедитесь, что активны события:
   - `payment.succeeded` - платёж прошёл успешно

4. Сохраните и протестируйте webhook на панели Yookassa

#### Проверка webhook'ов

Чтобы проверить, работают ли webhook'ы, откройте в браузере:
```
https://your-domain.com/health
```

Ответ должен показать статус `"status": "ok"` и `"bot_available": "✅ Yes"`

#### Альтернатива: Polling режим

Если webhook'ы не работают, можно использовать polling режим (периодическая проверка статусов платежей):

```env
WEBHOOK_USE_POLLING=true
```

В этом режиме бот будет проверять статусы платежей каждые 30 секунд (настраивается в `config.py` - `PAYMENT_CHECK_INTERVAL`)

**Примечание:** Polling более медленный, рекомендуется использовать webhook'ы для быстрой обработки платежей.

## Архитектура

### Модули

**config.py** - Загружает переменные окружения и определяет конфигурацию приложения

**database.py** - Работает с SQLite базой данных:
- Управление пользователями
- Управление платежами
- Управление промокодами
- Система блокировок для защиты от race conditions

**states.py** - Определяет FSM состояния для управления диалогами с пользователями

**handlers/** - Модули обработчиков:
- `start.py` - Инициализация бота и главное меню
- `subscription.py` - Покупка и управление подписками
- `gift.py` - Система подарков
- `referral.py` - Реферальная программа
- `promo.py` - Система промокодов
- `callbacks.py` - Общие callback обработчики
- `admin.py` - Администраторские команды

**services/** - Сервисы для интеграций:
- `remnawave.py` - Управление VPN аккаунтами через Remnawave API
- `cryptobot.py` - Обработка платежей через CryptoBot API с фоновой проверкой

## Безопасность

- ✅ Использование переменных окружения для секретных данных
- ✅ Система блокировок в БД для предотвращения race conditions
- ✅ Проверка прав администратора перед выполнением админ команд
- ✅ Проверка подписки на канал перед выдачей подарков

## Лицензия

MIT

## Поддержка

Для вопросов и поддержки обращайтесь через Telegram.
