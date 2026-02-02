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

    # Выбор устройства для инструкции подключения
    choosing_device = State()

    # Партнёрская программа
    viewing_partnership = State()
    choosing_agreement = State()
    waiting_partnership_agreement_response = State()
    choosing_withdrawal_method = State()
    entering_withdrawal_amount = State()
    entering_bank_name = State()
    entering_phone_number = State()
    entering_wallet_address = State()
