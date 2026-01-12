from aiogram.fsm.state import State, StatesGroup


class UserStates(StatesGroup):
    """Состояния для взаимодействия пользователя с ботом"""
    
    # Принятие условий использования
    waiting_for_agreement = State()
    
    # Выбор и оплата тарифа
    choosing_tariff = State()
    choosing_payment = State()
    
    # Ввод промокода
    waiting_for_promo = State()
