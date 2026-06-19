from decimal import Decimal, ROUND_HALF_UP


def _applies(discount, product_type: str, code: str, plan_kind: str | None) -> bool:
    target_type = discount.get("target_type")
    target_code = discount.get("target_code")
    if target_type == "all":
        return True
    if target_type == product_type:
        return True
    if target_type in {"regular", "bypass"}:
        return product_type == "subscription" and plan_kind == target_type
    if target_type == "tariff":
        return product_type == "subscription" and target_code == code
    if target_type == "traffic_package":
        return product_type == "traffic" and target_code == code
    return False


def calculate_discounted_price(
    base_price: int | float,
    discounts,
    *,
    product_type: str,
    code: str,
    plan_kind: str | None = None,
) -> dict:
    base = Decimal(str(base_price))
    best_price = base
    best_discount = None

    for discount in discounts or []:
        if not _applies(discount, product_type, code, plan_kind):
            continue
        value = Decimal(str(discount["value"]))
        if discount["discount_type"] == "percent":
            candidate = base * (Decimal("1") - value / Decimal("100"))
        else:
            candidate = base - value
        candidate = max(Decimal("1"), candidate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        if candidate < best_price:
            best_price = candidate
            best_discount = discount

    result = {
        "price": float(best_price),
        "original_price": float(base),
        "discount": None,
    }
    if best_discount:
        result["discount"] = {
            "id": best_discount["id"],
            "name": best_discount["name"],
            "type": best_discount["discount_type"],
            "value": float(best_discount["value"]),
        }
    return result


async def current_price(base_price, *, product_type: str, code: str, plan_kind: str | None = None) -> dict:
    import database as db
    discounts = await db.get_active_discounts()
    return calculate_discounted_price(
        base_price,
        discounts,
        product_type=product_type,
        code=code,
        plan_kind=plan_kind,
    )
