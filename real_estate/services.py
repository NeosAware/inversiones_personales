from decimal import Decimal

from portfolio.ownership import AssetOwnershipCategory

from .models import PropertyInvestment


ZERO = Decimal("0")


def build_property_ownership_overview(properties=None) -> dict:
    if properties is None:
        properties = list(PropertyInvestment.objects.all())
    else:
        properties = list(properties)

    groups = []
    for ownership_category, ownership_label in AssetOwnershipCategory.choices:
        owner_properties = [item for item in properties if item.ownership_category == ownership_category]
        groups.append(
            {
                "ownership_category": ownership_category,
                "ownership_label": ownership_label,
                "properties_count": len(owner_properties),
                "invested_amount": sum((item.invested_equity for item in owner_properties), ZERO),
                "market_value": sum((item.market_value for item in owner_properties), ZERO),
                "current_value": sum((item.current_value for item in owner_properties), ZERO),
                "annual_income": sum((item.annual_income for item in owner_properties), ZERO),
                "properties": owner_properties,
                "has_data": bool(owner_properties),
            }
        )

    return {
        "groups": groups,
        "summary": {
            "properties_count": len(properties),
            "current_value": sum((item.current_value for item in properties), ZERO),
            "annual_income": sum((item.annual_income for item in properties), ZERO),
        },
    }
