from aiogram.fsm.state import State, StatesGroup


class UserStates(StatesGroup):
    """Состояния для взаимодействия пользователя с ботом"""

    # Принятие условий использования
    waiting_for_agreement = State()

    # Выбор и оплата тарифа
    choosing_tariff = State()
    choosing_payment = State()

    # Ввод промокода
    waiting_for_promo_target = State()
    waiting_for_promo = State()

    # Выбор устройства для инструкции подключения
    choosing_device = State()

    # Партнёрская программа
    partnership_viewing_agreement = State()
    partnership_withdrawing_sbp = State()
    partnership_withdrawing_usdt = State()
    partnership_waiting_sbp_amount = State()
    partnership_waiting_sbp_bank = State()
    partnership_waiting_sbp_phone = State()
    partnership_waiting_usdt_amount = State()
    partnership_waiting_usdt_address = State()

    # Реферальная программа (выводы денег)
    referral_waiting_sbp_amount = State()
    referral_waiting_sbp_bank = State()
    referral_waiting_sbp_phone = State()
    referral_waiting_usdt_amount = State()
    referral_waiting_usdt_address = State()
