#!/bin/bash

# SPN VPN Bot - Deploy Script
# Скрипт для развёртывания бота на VPS

set -e

echo "=== SPN VPN Bot Deploy Script ==="
echo ""

# Проверяем Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 не установлен!"
    echo "Установите: sudo apt update && sudo apt install -y python3 python3-pip python3-venv"
    exit 1
fi

echo "✅ Python3 найден: $(python3 --version)"
echo ""

# Создаём директорию для бота
BOT_DIR="/home/$(whoami)/spn-vpn-bot"
echo "📁 Директория бота: $BOT_DIR"

if [ ! -d "$BOT_DIR" ]; then
    mkdir -p "$BOT_DIR"
    echo "✅ Директория создана"
else
    echo "✅ Директория уже существует"
fi

cd "$BOT_DIR"

# Создаём виртуальное окружение если его нет
if [ ! -d "venv" ]; then
    echo "📦 Создаю виртуальное окружение..."
    python3 -m venv venv
    echo "✅ Виртуальное окружение создано"
fi

# Активируем виртуальное окружение
echo "🔧 Активирую виртуальное окружение..."
source venv/bin/activate

# Обновляем pip
echo "📥 Обновляю pip..."
pip install --upgrade pip setuptools wheel > /dev/null

# Устанавливаем зависимости
if [ -f "requirements.txt" ]; then
    echo "📥 Устанавливаю зависимости из requirements.txt..."
    pip install -r requirements.txt
    echo "✅ Зависимости установлены"
else
    echo "❌ requirements.txt не найден в $BOT_DIR"
    exit 1
fi

# Проверяем наличие .env файла
if [ ! -f ".env" ]; then
    echo "❌ .env файл не найден!"
    echo "Создайте .env файл со следующими переменными:"
    echo "  BOT_TOKEN=..."
    echo "  ADMIN_ID=..."
    echo "  REMNAWAVE_BASE_URL=..."
    echo "  REMNAWAVE_API_TOKEN=..."
    echo "  И остальные переменные..."
    exit 1
fi

echo "✅ .env файл найден"
echo ""

# Тестируем импорт модулей
echo "🧪 Тестирую импорт модулей..."
python3 -c "from config import BOT_TOKEN; print('✅ Config импортирован успешно')" || {
    echo "❌ Ошибка при импорте config"
    exit 1
}

echo ""
echo "=== ✅ Развёртывание завершено ==="
echo ""
echo "Для запуска бота выполните:"
echo "  cd $BOT_DIR"
echo "  source venv/bin/activate"
echo "  python3 main.py"
echo ""
echo "Или используйте systemd сервис:"
echo "  sudo systemctl start spn-vpn-bot"
echo ""
