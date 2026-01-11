# ⚡ БЫСТРОЕ ОБНОВЛЕНИЕ ПРОЕКТА НА VPS

**Скопируй и вставляй эти команды в терминал VPS**

---

## 🚀 Быстрый способ (если у тебя есть git)

```bash
# Подключись к VPS
ssh root@ВАШ_IP

# Перейди в папку проекта
cd /root/spn-vpn-bot

# Обновляешь код
git pull origin main

# Обновляешь зависимости
source venv/bin/activate
pip install -r requirements.txt --upgrade
deactivate

# Перезагружаешь бота
sudo systemctl restart spn-bot

# Проверяешь логи
sudo journalctl -u spn-bot -f
```

---

## 📝 Обновляем .env файл перед обновлением

**Это главный шаг!**

```bash
nano /root/spn-vpn-bot/.env
```

Добавь эти строки (замени значения на СВОИ):

```env
SUPABASE_URL=https://rpzupbtpfcqnwlxzhndd.supabase.co
SUPABASE_KEY=sb_publishable_rAPEhWLXaexhMaKBbOvg-A_Xo1tz12I
DATABASE_URL=postgresql://postgres:Khamzat2Jaradat5612@db.rpzupbtpfcqnwlxzhndd.supabase.co:5432/postgres
```

И **удали** эту строку:
```
DB_FILE=spn_vpn_bot.db
```

Сохрани: Ctrl+O, Enter, Ctrl+X

---

## 🗄️ Создаём таблицы в Supabase

Перейди на https://supabase.com/dashboard:

1. Выбери свой проект
2. Нажми "SQL Editor" в левом меню
3. Нажми "+ New Query"
4. Копируешь весь код из файла `schema.sql`
5. Вставляешь в редактор и нажимаешь "Run"

---

## ✅ Проверяешь что всё работает

```bash
# Статус бота
sudo systemctl status spn-bot

# Логи в реальном времени
sudo journalctl -u spn-bot -f

# Должны видеть:
# ✅ "Database pool initialized successfully"
# ✅ "Bot started polling..."
```

---

## 🆘 Если ошибка "Connection refused"

```bash
# Проверь DATABASE_URL
cat /root/spn-vpn-bot/.env | grep DATABASE_URL

# Проверь что в URL нет символов < и >
# Проверь что это полный URL вида:
# postgresql://postgres:PASSWORD@db.HASH.supabase.co:5432/postgres
```

---

## 📊 Полезные команды

```bash
# Перезагрузить бота
sudo systemctl restart spn-bot

# Остановить бота
sudo systemctl stop spn-bot

# Запустить бота
sudo systemctl start spn-bot

# Смотреть логи (последние 100 строк)
sudo journalctl -u spn-bot -n 100

# Поиск ошибок в логах
sudo journalctl -u spn-bot | grep -i error
```

---

## 🎉 Всё готово!

Теперь бот работает с Supabase и БД в облаке! ☁️
