#!/usr/bin/env python3
"""
Проверка синтаксиса Python файлов и импортов
"""

import sys
import py_compile
import os
from pathlib import Path

files_to_check = [
    'main.py',
    'config.py',
    'database.py',
    'states.py',
    'services/oneplat.py',
    'services/cryptobot.py',
    'services/remnawave.py',
    'handlers/start.py',
    'handlers/callbacks.py',
    'handlers/subscription.py',
    'handlers/gift.py',
    'handlers/referral.py',
    'handlers/promo.py',
    'handlers/admin.py',
    'handlers/webhooks.py',
]

errors = []
warnings = []

print("🔍 Проверка синтаксиса Python файлов...\n")

for file_path in files_to_check:
    if not os.path.exists(file_path):
        errors.append(f"❌ {file_path} - файл не найден")
        continue
    
    try:
        py_compile.compile(file_path, doraise=True)
        print(f"✅ {file_path}")
    except py_compile.PyCompileError as e:
        errors.append(f"❌ {file_path}\n   {e}")

print("\n" + "="*60)

if errors:
    print("\n❌ ОШИБКИ СИНТАКСИСА:\n")
    for error in errors:
        print(f"  {error}\n")
    sys.exit(1)
else:
    print("\n✅ ВСЕ ФАЙЛЫ ИМЕЮТ ПРАВИЛЬНЫЙ СИНТАКСИС\n")
    sys.exit(0)
