# 🧪 Тестирование SPN VPN Bot

Этот документ описывает как запустить и писать тесты для SPN VPN Bot.

---

## 📋 Содержание

1. [Установка](#установка)
2. [Запуск тестов](#запуск-тестов)
3. [Структура тестов](#структура-тестов)
4. [Написание тестов](#написание-тестов)
5. [Best Practices](#best-practices)

---

## Установка

### Установить dev зависимости

```bash
pip install -r requirements-dev.txt
```

Это установит:
- **pytest** - фреймворк для тестирования
- **pytest-asyncio** - поддержка async тестов
- **pytest-cov** - coverage отчеты
- **flake8** - linting
- **black** - форматирование кода
- **mypy** - type checking

---

## Запуск тестов

### Запустить все тесты

```bash
pytest
```

### Запустить тесты с coverage отчетом

```bash
pytest --cov=. --cov-report=html
```

Отчет будет в файле `htmlcov/index.html`

### Запустить конкретный файл тестов

```bash
pytest tests/test_validators.py
```

### Запустить конкретный тест

```bash
pytest tests/test_validators.py::TestValidateTgId::test_valid_tg_id
```

### Запустить тесты с verbose выводом

```bash
pytest -v
```

### Запустить только быстрые тесты (пропустить slow)

```bash
pytest -m "not slow"
```

### Запустить с loggers выводом

```bash
pytest -v -s
```

---

## Структура тестов

```
tests/
├── __init__.py              # Package init
├── test_validators.py       # Тесты функций валидации
├── test_database.py         # Тесты database функций
└── conftest.py             # Общие fixtures (если нужны)
```

### test_validators.py

Тесты для функций валидации в `handlers/admin.py`:
- `validate_tg_id()` - валидация Telegram ID
- `validate_days()` - валидация количества дней
- `validate_promo_code()` - валидация промокодов

### test_database.py

Тесты для функций базы данных и утилит:
- `UserLockContext` - context manager для блокировок
- Работа с timezone
- Идемпотентность платежей
- Граничные условия

---

## Написание тестов

### Базовая структура

```python
import pytest
from handlers.admin import validate_tg_id

class TestValidateTgId:
    """Тесты для функции validate_tg_id"""
    
    def test_valid_tg_id(self):
        """Описание теста"""
        # Arrange (подготовка данных)
        tg_id = 123456789
        
        # Act (выполнение тестируемого кода)
        result = validate_tg_id(tg_id)
        
        # Assert (проверка результата)
        assert result is True
```

### Async тесты

```python
import pytest

class TestAsyncFunction:
    @pytest.mark.asyncio
    async def test_async_operation(self):
        """Тест для async функции"""
        from database import UserLockContext
        
        context = UserLockContext(123456)
        async with context as acquired:
            assert acquired is not None
```

### Тесты с множественными assertions

```python
def test_multiple_conditions(self):
    """Тест множественных условий"""
    result = get_data()
    
    assert result is not None
    assert len(result) > 0
    assert result['status'] == 'success'
```

### Тесты исключений

```python
def test_invalid_input(self):
    """Тест обработки невалидного входа"""
    with pytest.raises(ValueError):
        parse_int("not_a_number")
```

### Parametrized тесты

```python
import pytest

class TestValidation:
    @pytest.mark.parametrize("value,expected", [
        (1, True),
        (0, False),
        (-1, False),
        (3650, True),
        (3651, False),
    ])
    def test_validate_days_multiple(self, value, expected):
        from handlers.admin import validate_days
        assert validate_days(value) is expected
```

---

## Best Practices

### 1. Naming

```python
# ✅ Хорошо
def test_validate_tg_id_with_valid_id():
    ...

# ❌ Плохо
def test_1():
    ...
```

### 2. Описания

```python
# ✅ Хорошо
def test_validate_tg_id_returns_false_for_negative(self):
    """Отрицательные ID должны быть невалидными"""
    assert validate_tg_id(-123) is False

# ❌ Плохо
def test_negative():
    assert validate_tg_id(-123) is False
```

### 3. One assertion per test (когда возможно)

```python
# ✅ Хорошо
def test_valid_tg_id(self):
    assert validate_tg_id(123456789) is True

def test_invalid_tg_id_zero(self):
    assert validate_tg_id(0) is False

# ❌ Плохо (если один assertion fail, остальные не выполнятся)
def test_validation(self):
    assert validate_tg_id(123456789) is True
    assert validate_tg_id(0) is False
    assert validate_tg_id(-1) is False
```

### 4. Используйте fixtures для shared setup

```python
import pytest

@pytest.fixture
def valid_tg_id():
    return 123456789

class TestValidation:
    def test_valid_id(self, valid_tg_id):
        assert validate_tg_id(valid_tg_id) is True
```

### 5. Группируйте тесты в классы

```python
# ✅ Хорошо
class TestValidateTgId:
    def test_valid_id(self):
        ...
    
    def test_invalid_zero(self):
        ...

# ❌ Плохо (все тесты в файле на одном уровне)
def test_valid_tg_id():
    ...

def test_invalid_tg_id_zero():
    ...
```

### 6. Используйте assert вместо assertTrue/assertEqual

```python
# ✅ Хорошо
assert result is True
assert result == expected_value
assert len(items) > 0

# ❌ Плохо (старый стиль unittest)
self.assertTrue(result)
self.assertEqual(result, expected_value)
```

---

## CI/CD Интеграция

### GitHub Actions

```yaml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    
    steps:
      - uses: actions/checkout@v2
      
      - name: Set up Python
        uses: actions/setup-python@v2
        with:
          python-version: 3.10
      
      - name: Install dependencies
        run: |
          pip install -r requirements-dev.txt
      
      - name: Run tests
        run: |
          pytest --cov=. --cov-report=xml
      
      - name: Upload coverage
        uses: codecov/codecov-action@v2
```

### GitLab CI

```yaml
test:
  image: python:3.10
  script:
    - pip install -r requirements-dev.txt
    - pytest --cov=. --cov-report=term
```

---

## Примеры тестов

### Пример 1: Тестирование функции валидации

```python
class TestValidateTgId:
    """Тесты для validate_tg_id"""
    
    def test_valid_id(self):
        assert validate_tg_id(123456789) is True
    
    def test_invalid_zero(self):
        assert validate_tg_id(0) is False
    
    def test_invalid_negative(self):
        assert validate_tg_id(-123) is False
    
    def test_invalid_type(self):
        assert validate_tg_id("123") is False
```

### Пример 2: Async тест

```python
class TestLockContext:
    @pytest.mark.asyncio
    async def test_lock_acquired(self):
        from database import UserLockContext
        
        context = UserLockContext(123456)
        
        async with context as acquired:
            assert acquired is True
```

### Пример 3: Parametrized тест

```python
class TestValidateDays:
    @pytest.mark.parametrize("days,expected", [
        (1, True),
        (30, True),
        (365, True),
        (3650, True),
        (0, False),
        (-1, False),
        (3651, False),
    ])
    def test_validate_days(self, days, expected):
        assert validate_days(days) is expected
```

---

## Troubleshooting

### Проблема: "ModuleNotFoundError"

**Решение:** Убедитесь что тесты запускаются из корневой директории проекта:

```bash
# ✅ Правильно
cd /path/to/spn-vpn-bot
pytest

# ❌ Неправильно
cd tests
pytest
```

### Проблема: Async тесты не работают

**Решение:** Убедитесь что установлен `pytest-asyncio`:

```bash
pip install pytest-asyncio
```

### Проблема: Imports не работают в тестах

**Решение:** Добавьте `__init__.py` в папки:

```bash
touch tests/__init__.py
touch handlers/__init__.py  # если не существует
```

---

## Дополнительные ресурсы

- [Pytest документация](https://docs.pytest.org/)
- [Pytest async](https://pytest-asyncio.readthedocs.io/)
- [Testing best practices](https://testdriven.io/)

---

**Happy Testing! 🚀**
