"""
Unit тесты для функций валидации в handlers/admin.py
"""
import pytest
from handlers.admin import validate_tg_id, validate_days, validate_promo_code


class TestValidateTgId:
    """Тесты для функции validate_tg_id"""
    
    def test_valid_tg_id(self):
        """Тест корректного ID"""
        assert validate_tg_id(123456789) is True
        assert validate_tg_id(1) is True
        assert validate_tg_id(999999999) is True
    
    def test_invalid_tg_id_zero(self):
        """ID = 0 должен быть невалидным"""
        assert validate_tg_id(0) is False
    
    def test_invalid_tg_id_negative(self):
        """Отрицательные ID должны быть невалидными"""
        assert validate_tg_id(-123) is False
    
    def test_invalid_tg_id_too_large(self):
        """ID больше 10^15 должен быть невалидным"""
        assert validate_tg_id(10**16) is False
    
    def test_invalid_tg_id_type(self):
        """Не-целые числа должны быть невалидными"""
        assert validate_tg_id("123456") is False
        assert validate_tg_id(123.456) is False
        assert validate_tg_id(None) is False


class TestValidateDays:
    """Тесты для функции validate_days"""
    
    def test_valid_days(self):
        """Тест корректных количеств дней"""
        assert validate_days(1) is True
        assert validate_days(30) is True
        assert validate_days(365) is True
        assert validate_days(3650) is True  # макс
    
    def test_invalid_days_zero(self):
        """0 дней должно быть невалидным"""
        assert validate_days(0) is False
    
    def test_invalid_days_negative(self):
        """Отрицательные дни должны быть невалидными"""
        assert validate_days(-30) is False
    
    def test_invalid_days_too_large(self):
        """Больше 3650 дней должно быть невалидным"""
        assert validate_days(3651) is False
        assert validate_days(10000) is False
    
    def test_invalid_days_type(self):
        """Не-целые числа должны быть невалидными"""
        assert validate_days("30") is False
        assert validate_days(30.5) is False
        assert validate_days(None) is False


class TestValidatePromoCode:
    """Тесты для функции validate_promo_code"""
    
    def test_valid_promo_code(self):
        """Тест корректных промокодов"""
        assert validate_promo_code("ABC123") is True
        assert validate_promo_code("SUMMER30") is True
        assert validate_promo_code("code2024") is True
        assert validate_promo_code("a1b2c3d4") is True
    
    def test_invalid_promo_code_too_short(self):
        """Промокод короче 3 символов невалиден"""
        assert validate_promo_code("AB") is False
        assert validate_promo_code("a") is False
    
    def test_invalid_promo_code_too_long(self):
        """Промокод длинее 50 символов невалиден"""
        long_code = "a" * 51
        assert validate_promo_code(long_code) is False
    
    def test_invalid_promo_code_special_chars(self):
        """Промокод со спецсимволами невалиден"""
        assert validate_promo_code("ABC@123") is False
        assert validate_promo_code("CODE-2024") is False
        assert validate_promo_code("CODE_2024") is False
        assert validate_promo_code("CODE 2024") is False
    
    def test_invalid_promo_code_type(self):
        """Не-строки должны быть невалидными"""
        assert validate_promo_code(123) is False
        assert validate_promo_code(None) is False
    
    def test_valid_promo_code_with_whitespace(self):
        """Промокод с пробелами в начале/конце должен быть валиден после strip()"""
        # Функция делает strip(), так что это должно работать
        assert validate_promo_code("  ABC123  ") is True


class TestValidationIntegration:
    """Интеграционные тесты для валидации"""
    
    def test_admin_command_parsing(self):
        """Тест парсинга админ команды"""
        # Пример: /give_sub 123456789 30
        tg_id_str = "123456789"
        days_str = "30"
        
        try:
            tg_id = int(tg_id_str)
            days = int(days_str)
            
            assert validate_tg_id(tg_id) is True
            assert validate_days(days) is True
        except ValueError:
            pytest.fail("Failed to parse admin command")
    
    def test_invalid_admin_command(self):
        """Тест невалидной админ команды"""
        # Пример: /give_sub invalid 999999
        tg_id_str = "invalid"
        days_str = "999999"
        
        with pytest.raises(ValueError):
            tg_id = int(tg_id_str)
        
        # days_str валидный, но слишком большой
        days = int(days_str)
        assert validate_days(days) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
