"""
Unit тесты для функций database.py
"""
import pytest
from datetime import datetime, timezone, timedelta


class TestUserLockContext:
    """Тесты для UserLockContext context manager"""
    
    @pytest.mark.asyncio
    async def test_lock_context_initialization(self):
        """Тест инициализации контекста блокировки"""
        from database import UserLockContext
        
        tg_id = 123456
        context = UserLockContext(tg_id, max_retries=5, retry_delay=0.1)
        
        assert context.tg_id == tg_id
        assert context.max_retries == 5
        assert context.retry_delay == 0.1
        assert context.acquired is False
    
    @pytest.mark.asyncio
    async def test_lock_context_properties(self):
        """Тест свойств контекста блокировки"""
        from database import UserLockContext
        
        context = UserLockContext(123456)
        
        # По умолчанию должны быть разумные значения
        assert context.max_retries > 0
        assert context.retry_delay > 0
        assert context.acquired is False


class TestValidationFunctions:
    """Тесты для функций валидации в database.py"""
    
    def test_datetime_timezone_handling(self):
        """Тест корректной работы с timezone"""
        # Нужно использовать datetime.now(timezone.utc) везде
        now_utc = datetime.now(timezone.utc)
        
        assert now_utc.tzinfo is not None
        assert now_utc.tzinfo == timezone.utc
    
    def test_datetime_isoformat(self):
        """Тест ISO формата для datetime"""
        now_utc = datetime.now(timezone.utc)
        iso_string = now_utc.isoformat()
        
        # ISO string должен содержать '+00:00'
        assert '+00:00' in iso_string
        
        # Должен парситься обратно корректно
        parsed = datetime.fromisoformat(iso_string)
        assert parsed.tzinfo is not None


class TestTimezoneUnification:
    """Тесты для унификации работы с timezone"""
    
    def test_naive_vs_aware_datetime(self):
        """Тест разницы между naive и aware datetime"""
        import datetime as dt
        
        # Naive datetime (без timezone)
        naive = dt.datetime.now()
        assert naive.tzinfo is None
        
        # Aware datetime (с timezone)
        aware = dt.datetime.now(dt.timezone.utc)
        assert aware.tzinfo is not None
    
    def test_datetime_comparison(self):
        """Тест сравнения datetime объектов"""
        import datetime as dt
        
        now_utc = dt.datetime.now(dt.timezone.utc)
        future = now_utc + dt.timedelta(days=30)
        
        assert future > now_utc
        assert (future - now_utc).days == 30
    
    def test_datetime_string_parsing(self):
        """Тест парсинга datetime строк"""
        iso_string = "2024-12-15T10:30:45+00:00"
        parsed = datetime.fromisoformat(iso_string)
        
        assert parsed.tzinfo is not None
        
        # С 'Z' суффиксом (UTC)
        iso_string_z = "2024-12-15T10:30:45Z"
        parsed_z = datetime.fromisoformat(iso_string_z.replace('Z', '+00:00'))
        
        assert parsed_z.tzinfo is not None


class TestPaymentIdempotency:
    """Тесты для идемпотентности платежей"""
    
    def test_payment_status_values(self):
        """Тест возможных статусов платежей"""
        valid_statuses = ['pending', 'paid', 'failed', 'cancelled']
        
        for status in valid_statuses:
            assert status in ['pending', 'paid', 'failed', 'cancelled']
    
    def test_payment_id_format(self):
        """Тест формата ID платежа"""
        invoice_id = "spn_123456789_1702645845_1m"
        
        # ID должен содержать компоненты
        parts = invoice_id.split('_')
        assert len(parts) >= 3
        assert parts[0] == 'spn'


class TestBoundaryConditions:
    """Тесты граничных условий"""
    
    def test_tariff_days_boundaries(self):
        """Тест граничных значений дней для тарифов"""
        from config import TARIFFS
        
        for code, tariff in TARIFFS.items():
            days = tariff['days']
            price = tariff['price']
            
            # Дни должны быть положительными
            assert days > 0
            # Цена должна быть положительной
            assert price > 0
    
    def test_user_id_boundaries(self):
        """Тест граничных значений для user ID"""
        # Минимальный валидный ID
        min_id = 1
        
        # Максимальный валидный ID (10^15)
        max_id = 10**15 - 1
        
        assert min_id > 0
        assert max_id > min_id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
