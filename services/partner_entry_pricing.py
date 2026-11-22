"""Pure, immutable entry pricing. No configuration discovery or external I/O."""
from dataclasses import dataclass

from services.partner_catalog import ENTRY_FEE_KOPECKS


ENTRY_PILOT_OWNER_ID = 6910097562
ENTRY_PILOT_PRICE_KOPECKS = 1000


class EntryPricingError(ValueError):
    """An entry price or owner is outside the explicitly supported policy."""


def _owner(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise EntryPricingError("Некорректный владелец заказа вступления.")
    return value


@dataclass(frozen=True)
class EntryPricingPolicy:
    owner_pilot_enabled: bool = False
    public_trial_enabled: bool = False
    # Regular public enrollment is a separate capability, not a trial rename.
    # It does not authorize tenant subscription/add-on payments.
    public_sales_enabled: bool = False

    def __post_init__(self):
        if (type(self.owner_pilot_enabled) is not bool
                or type(self.public_trial_enabled) is not bool
                or type(self.public_sales_enabled) is not bool
                or sum((self.owner_pilot_enabled, self.public_trial_enabled,
                        self.public_sales_enabled)) > 1):
            raise EntryPricingError("Пилотная цена требует явного логического флага.")

    def is_pilot_for(self, owner_id) -> bool:
        return self.owner_pilot_enabled and type(owner_id) is int and owner_id == ENTRY_PILOT_OWNER_ID

    def price_for(self, owner_id) -> int:
        owner_id = _owner(owner_id)
        return ENTRY_PILOT_PRICE_KOPECKS if self.public_trial_enabled or self.is_pilot_for(owner_id) else ENTRY_FEE_KOPECKS

    def validate_saved_order(self, order) -> None:
        try:
            owner_id, price = _owner(order["owner_tg_id"]), order["price_kopecks"]
        except (KeyError, TypeError, IndexError):
            raise EntryPricingError("Не удалось проверить цену сохранённого вступления.") from None
        if type(price) is not int or not (
            price == ENTRY_FEE_KOPECKS
            # A saved public-trial invoice must remain reconcilable after the
            # trial ends. Only NEW invoices use the current pricing policy.
            or price == ENTRY_PILOT_PRICE_KOPECKS
        ):
            raise EntryPricingError("Стоимость сохранённого вступления требует проверки.")

    def validate_new_invoice(self, order) -> None:
        self.validate_saved_order(order)
        if order["price_kopecks"] != self.price_for(order["owner_tg_id"]):
            raise EntryPricingError("Стоимость вступления изменилась. Подтвердите новый заказ; сохранённая цена не меняется.")
