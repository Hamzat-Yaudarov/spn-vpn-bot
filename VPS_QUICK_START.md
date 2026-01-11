# ⚡ VPS Quick Start - 5 минут

## 🚀 Самый быстрый способ

```bash
# 1. Подключитесь к VPS
ssh root@YOUR_VPS_IP

# 2. Скопируйте файлы проекта (выберите способ):

# Способ A: Через git (если репозиторий есть)
cd /root && git clone https://github.com/YOUR_USERNAME/spn-vpn-bot.git && cd spn-vpn-bot

# Способ B: Через scp с вашего ПК (в отдельном терминале на ПК)
scp -r ~/path/to/spn-vpn-bot root@YOUR_VPS_IP:/root/
ssh root@YOUR_VPS_IP "cd /root/spn-vpn-bot"

# 3. Создайте .env файл
cat > .env << 'EOF'
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
EOF

# 4. Установите и запустите
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

## ✅ Готово!

Если видите логи без ошибок - бот работает! 🎉

Нажмите **Ctrl+C** чтобы остановить.

---

## 🔄 Запуск в фоне (чтобы не закрывалось после отключения SSH)

```bash
# Способ 1: screen (самый простой)
screen -S spnbot
source venv/bin/activate
python3 main.py
# Нажмите Ctrl+A потом D чтобы выйти из screen

# Способ 2: nohup
nohup python3 -m venv venv && source venv/bin/activate && python3 main.py > bot.log 2>&1 &

# Способ 3: systemd (более сложный, см. DEPLOY.md)
```

---

## 📋 Команды управления screen

```bash
# Список всех screen сессий
screen -ls

# Подключиться к сессии
screen -r spnbot

# Выйти из screen (не закрывая его)
# Нажмите: Ctrl+A потом D

# Закрыть screen сессию
# Нажмите: Ctrl+D (когда в сессии)
```

---

## 🐛 Проверка что бот работает

```bash
# Если используете screen
screen -ls
# Должна быть строка с "spnbot"

# Если используете nohup
ps aux | grep main.py
tail -f bot.log
```

---

## 📊 Полная инструкция

Смотрите **DEPLOY.md** для:
- Установки systemd сервиса с автозапуском
- Мониторинга и логирования
- Обновления кода
- Решения проблем
