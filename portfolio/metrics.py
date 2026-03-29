from decimal import Decimal


ZERO = Decimal("0")


def to_decimal(value):
    return Decimal(value or 0)


def build_metrics(label, asset_type, invested_amount, current_value, annual_income, app_url_name, notes=""):
    invested_amount = to_decimal(invested_amount)
    current_value = to_decimal(current_value)
    annual_income = to_decimal(annual_income)
    unrealized_gain = current_value - invested_amount
    total_return_eur = unrealized_gain + annual_income
    total_return_pct = (total_return_eur / invested_amount * Decimal("100")) if invested_amount else ZERO
    cash_yield_pct = (annual_income / invested_amount * Decimal("100")) if invested_amount else ZERO

    return {
        "label": label,
        "asset_type": asset_type,
        "invested_amount": invested_amount,
        "current_value": current_value,
        "annual_income": annual_income,
        "unrealized_gain": unrealized_gain,
        "total_return_eur": total_return_eur,
        "total_return_pct": total_return_pct,
        "cash_yield_pct": cash_yield_pct,
        "app_url_name": app_url_name,
        "notes": notes,
    }
