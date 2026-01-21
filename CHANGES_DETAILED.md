# 📝 Детальные изменения по файлам

## 📄 config.py

### Добавлено:
- **XUI_PANEL_URL** — адрес 3X-UI панели
- **XUI_PANEL_PATH** — путь к API 3X-UI
- **XUI_USERNAME** — имя пользователя 3X-UI
- **XUI_PASSWORD** — пароль 3X-UI
- **SUB_PORT** — порт для ссылок подписки
- **SUB_EXTERNAL_HOST** — внешний хост 3X-UI
- **INBOUND_ID** — ID inbound в 3X-UI

### Изменено:
- **TARIFFS** → разбит на **TARIFFS_REGULAR** и **TARIFFS_ANTI_JAMMING**
- **TARIFFS_REGULAR**: 100, 249, 449, 990
- **TARIFFS_ANTI_JAMMING**: 150, 349, 599, 1190
- **TARIFFS** = TARIFFS_REGULAR (для обратной совместимости)

## 💾 database.py

### Новые столбцы в таблице users:
```sql
subscription_type TEXT DEFAULT 'regular'     -- тип подписки
balance NUMERIC DEFAULT 0                     -- баланс пользователя
xui_uuid TEXT                                 -- UUID клиента в 3X-UI
xui_username TEXT                             -- имя пользователя в 3X-UI
xui_subscription_until TIMESTAMP              -- дата истечения 3X-UI подписки
```

### Новые функции:
```python
# Управление балансом
get_balance(tg_id)                    # получить баланс
add_balance(tg_id, amount)            # добавить средства
subtract_balance(tg_id, amount)       # снять средства (атомарно)
set_balance(tg_id, amount)            # установить баланс

# Управление типом подписки
get_subscription_type(tg_id)          # получить тип
set_subscription_type(tg_id, sub_type) # установить тип

# Управление 3X-UI подписками
update_xui_subscription(...)          # обновить 3X-UI данные
get_xui_subscription(tg_id)           # получить 3X-UI данные
has_xui_subscription(tg_id)           # проверить наличие 3X-UI подписки
```

### Изменено:
- Добавлены миграции для новых столбцов
- Все функции БД добавлены в конец файла

## 🔧 services/xui.py (НОВЫЙ ФАЙЛ)

### Функции:
```python
get_xui_session()                      # авторизация в 3X-UI
create_xui_client(tg_id, days)        # создать клиента в 3X-UI
extend_xui_subscription(xui_uuid, days) # продлить подписку
get_xui_client_traffic(xui_username)   # получить информацию о клиенте
```

### Возвращаемые данные:
- **create_xui_client** → 
  ```python
  {
    'xui_uuid': str,
    'xui_username': str,
    'subscription_url': str,
    'subscription_until': str
  }
  ```

## 📊 states.py

### Добавлено:
```python
UserStates.choosing_subscription_type  # выбор типа подписки (новое состояние)
```

### Существующие состояния (не изменены):
- `waiting_for_agreement`
- `choosing_tariff`
- `choosing_payment`
- `waiting_for_promo`

## 💳 handlers/subscription.py

### Полная переработка:

#### 1. Новый обработчик выбора типа подписки:
```python
@router.callback_query(F.data == "buy_subscription")
async def process_buy_subscription()  # показывает выбор между regular/anti_jamming
```

#### 2. Обработчик выбора типа:
```python
@router.callback_query(UserStates.choosing_subscription_type, F.data.startswith("subscription_type_"))
async def process_subscription_type_choice()  # сохраняет тип и показывает тарифы
```

#### 3. Помощник для показа тарифов:
```python
async def show_tariffs_for_type()  # показывает правильные тарифы для типа
```

#### 4. Обновлённый выбор тарифа:
```python
@router.callback_query(UserStates.choosing_tariff, F.data.startswith("tariff_"))
async def process_tariff_choice()  # теперь учитывает subscription_type
```

#### 5. Обновлённые обработчики оплаты:
```python
async def process_pay_cryptobot()    # использует правильный тариф для типа
async def process_pay_yookassa()     # использует правильный тариф для типа
```

#### 6. Проверка платежа обновлена:
```python
@router.callback_query(F.data == "check_payment")
async def process_check_payment()    # передаёт subscription_type в обработчики
```

#### 7. Просмотр подписки полностью переработан:
```python
@router.callback_query(F.data == "my_subscription")
async def process_my_subscription()  # показывает обе ссылки для anti_jamming
```

## 💰 services/cryptobot.py

### Изменено:

#### Функция process_paid_invoice:
```python
async def process_paid_invoice(
    bot, tg_id, invoice_id, tariff_code,
    subscription_type='regular'  # НОВЫЙ ПАРАМЕТР
)
```

### Логика:
1. Выбирает правильный словарь тарифов (regular или anti_jamming)
2. Создаёт аккаунт в Remnawave
3. **Если anti_jamming**: создаёт аккаунт в 3X-UI
4. Сохраняет 3X-UI данные в БД
5. Отправляет подходящее сообщение с ссылками

### Функция check_cryptobot_invoices:
- Передаёт `subscription_type` при обработке платежей

## 💳 services/yookassa.py

### Идентично CryptoBot:

#### Функция process_paid_yookassa_payment:
```python
async def process_paid_yookassa_payment(
    bot, tg_id, payment_id, tariff_code,
    subscription_type='regular'  # НОВЫЙ ПАРАМЕТР
)
```

### Логика:
- Полностью аналогична CryptoBot
- Поддерживает оба типа подписок
- Создаёт 3X-UI клиентов для anti_jamming

### Функция check_yookassa_payments:
- Передаёт `subscription_type` при обработке платежей

## 🎁 handlers/gift.py

### Изменено:
✅ **НЕ ИЗМЕНЕНО** — выдаёт 3 дня только Remnawave подписки, независимо от типа

## 🔐 handlers/promo.py

### Изменено:
✅ **НЕ ИЗМЕНЕНО** — активирует промокоды только для Remnawave

## 👥 handlers/referral.py

### Изменено:
✅ **НЕ ИЗМЕНЕНО** — показывает реферальную информацию

---

## 📊 Общая статистика изменений

| Файл | Статус | Строк добавлено | Строк изменено |
|------|--------|-----------------|----------------|
| config.py | ✏️ Изменён | +30 | +5 |
| database.py | ✏️ Изменён | +150 | +15 |
| services/xui.py | ✨ Создан | +241 | 0 |
| states.py | ✏️ Изменён | +2 | 0 |
| handlers/subscription.py | ✏️ Изменён | +200 | +150 |
| services/cryptobot.py | ✏️ Изменён | +40 | +40 |
| services/yookassa.py | ✏️ Изменён | +40 | +40 |
| handlers/gift.py | ✅ Не изменён | 0 | 0 |
| handlers/promo.py | ✅ Не изменён | 0 | 0 |
| handlers/referral.py | ✅ Не изменён | 0 | 0 |

---

## 🔄 Порядок выполнения при запуске

1. **config.py** → загружаются все параметры (включая новые 3X-UI)
2. **database.py** → выполняются миграции, добавляются новые столбцы
3. **services/xui.py** → готов к использованию
4. **states.py** → новое состояние доступно
5. **handlers/subscription.py** → готов обрабатывать новый процесс
6. **services/cryptobot.py** и **yookassa.py** → готовы создавать двойные подписки
