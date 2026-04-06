from __future__ import annotations

from decimal import Decimal

from .ownership import AssetOwnershipCategory


NEOS_OWNER_SPLIT = {
    AssetOwnershipCategory.XIMO: Decimal("90.00"),
    AssetOwnershipCategory.MONICA: Decimal("10.00"),
}


NEOS_COMPANY_CONFIG = {
    "Neos Ceramica": {
        "display_name": "Neos Ceramica",
        "group_pct": Decimal("100.00"),
        "default_ownership_pct": Decimal("100.00"),
        "uses_parent_consolidation": False,
        "role_title": "Holding cabecera del grupo",
        "role_blurb": (
            "La participacion del hogar en Neos Ceramica se reparte entre Ximo (90 %) y Monica (10 %). "
            "Desde este holding se controla el 80 % de Neos Additives y el 33,3 % de Neos Materials."
        ),
    },
    "Neos Additives": {
        "display_name": "Neos Additives",
        "group_pct": Decimal("80.00"),
        "default_ownership_pct": Decimal("80.00"),
        "uses_parent_consolidation": True,
        "role_title": "Filial seguida via Neos Ceramica",
        "role_blurb": (
            "Esta participacion se analiza como filial del grupo. El valor economico ya queda consolidado en "
            "Neos Ceramica, por lo que en el resumen general no debe sumarse dos veces."
        ),
    },
    "Neos Materials": {
        "display_name": "Neos Materials",
        "group_pct": Decimal("33.30"),
        "default_ownership_pct": Decimal("33.30"),
        "uses_parent_consolidation": True,
        "role_title": "Filial seguida via Neos Ceramica",
        "role_blurb": (
            "Esta participacion se analiza como filial del grupo. El valor economico ya queda consolidado en "
            "Neos Ceramica, por lo que en el resumen general no debe sumarse dos veces."
        ),
    },
}


def build_neos_owner_breakdown(total_value: Decimal, annual_income: Decimal) -> list[dict]:
    rows = []
    for ownership_category, ownership_pct in NEOS_OWNER_SPLIT.items():
        rows.append(
            {
                "ownership_category": ownership_category,
                "ownership_pct": ownership_pct,
                "current_value": total_value * ownership_pct / Decimal("100"),
                "annual_income": annual_income * ownership_pct / Decimal("100"),
            }
        )
    return rows
