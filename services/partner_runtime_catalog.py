"""Explicit staging-only pricing fixture. No main config or database imports.

The operator supplies reviewed prices; this is NOT a replacement source for the
main bot's production catalogue. An omitted snapshot disables add-ons honestly.
"""

from copy import deepcopy
from datetime import datetime, timezone
import math

from services.partner_catalog import MIN_PRICE_KOPECKS, catalog
from services.partner_runtime import RuntimeConfigurationError


class StagingAddonCatalog:
    def __init__(self, snapshot=None):
        try:
            if snapshot is not None and (
                not isinstance(snapshot, dict)
                or set(snapshot) != {"traffic_packages", "device_rules"}
            ):
                raise ValueError()
            self.snapshot = deepcopy(snapshot)
            if snapshot is None:
                return
            catalog(MIN_PRICE_KOPECKS, snapshot["traffic_packages"])
            rules = snapshot["device_rules"]
            if rules is not None:
                if not isinstance(rules, dict) or set(rules) != {
                    "price_per_30_days",
                    "minimum_price",
                    "discount_step_percent",
                    "max_discount_percent",
                }:
                    raise ValueError()
                for key in ("price_per_30_days", "minimum_price"):
                    if type(rules[key]) is not int or not 1 <= rules[key] <= 100_000:
                        raise ValueError()
                for key in ("discount_step_percent", "max_discount_percent"):
                    if type(rules[key]) is not int or not 0 <= rules[key] <= 90:
                        raise ValueError()
        except Exception:
            raise RuntimeConfigurationError(
                "Проверьте явный каталог цен стенда."
            ) from None

    async def summary(self):
        if self.snapshot is None:
            return {"traffic_packages": None, "device_options": None}
        return {
            "traffic_packages": deepcopy(self.snapshot["traffic_packages"]),
            "device_options": {"addon_status": "quote_required", "packages": []},
        }

    async def quote(self, subscription, *, now):
        try:
            if (
                not isinstance(now, datetime)
                or now.tzinfo is None
                or not isinstance(subscription, dict)
            ):
                raise ValueError()
            if self.snapshot is None:
                return catalog(MIN_PRICE_KOPECKS)["addons"]
            expiry = subscription.get("expires_at")
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if expiry is None:
                expired = True
            elif not isinstance(expiry, datetime) or expiry.tzinfo is None:
                raise ValueError()
            else:
                expired = expiry <= now
            devices = subscription.get("device_limit")
            if type(devices) is not int or not 3 <= devices <= 10:
                raise ValueError()
            options = {"addon_status": "unavailable", "packages": []}
            if expired:
                options["addon_status"] = "expired"
            elif devices == 10:
                options["addon_status"] = "max_limit"
            elif self.snapshot["device_rules"] is not None:
                rules = self.snapshot["device_rules"]
                days = max(
                    1,
                    math.ceil(
                        (
                            expiry.astimezone(timezone.utc)
                            - now.astimezone(timezone.utc)
                        ).total_seconds()
                        / 86_400
                    ),
                )
                if days > 36_500:
                    raise ValueError()
                packages = []
                for count in (1, 2, 3):
                    if count > 10 - devices:
                        continue
                    discount = min(
                        (count - 1) * rules["discount_step_percent"],
                        rules["max_discount_percent"],
                    )
                    numerator = rules["price_per_30_days"] * count * days
                    packages.append(
                        {
                            "count": count,
                            "days": days,
                            "discount_percent": discount,
                            "original_price": (numerator + 29) // 30,
                            "price": max(
                                rules["minimum_price"],
                                (numerator * (100 - discount) + 2999) // 3000,
                            ),
                        }
                    )
                options = {"addon_status": "available", "packages": packages}
            result = catalog(
                MIN_PRICE_KOPECKS, self.snapshot["traffic_packages"], options
            )["addons"]
            if expired:
                result["traffic_status"], result["traffic_packages"] = "expired", []
            return result
        except Exception:
            raise RuntimeConfigurationError(
                "Не удалось проверить цену докупки на стенде."
            ) from None
