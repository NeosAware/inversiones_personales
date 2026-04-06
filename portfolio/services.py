from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.utils import timezone

from banking.models import BankBalance, BankInvestmentPosition, BankMovement, BankStatementImport
from banking.services import build_bank_account_overview, build_banking_ownership_overview
from equities.models import EquityPosition
from neos_additives.models import AdditivesHolding
from neos_ceramica.models import CeramicaHolding
from neos_materials.models import MaterialsHolding
from real_estate.models import PropertyInvestment
from real_estate.services import build_property_ownership_overview

from .company_group import NEOS_COMPANY_CONFIG, build_neos_owner_breakdown
from .metrics import ZERO, build_metrics
from .models import HouseholdAlertSettings, PortfolioSnapshot
from .ownership import AssetOwnershipCategory


def summarise_section(title, items, app_url_name, *, include_in_totals: bool = True, analysis_only_note: str = ""):
    invested_amount = sum((item["invested_amount"] for item in items), ZERO)
    current_value = sum((item["current_value"] for item in items), ZERO)
    annual_income = sum((item["annual_income"] for item in items), ZERO)
    unrealized_gain = current_value - invested_amount
    total_return_eur = unrealized_gain + annual_income
    total_return_pct = (total_return_eur / invested_amount * Decimal("100")) if invested_amount else ZERO

    return {
        "title": title,
        "items": items,
        "items_count": len(items),
        "invested_amount": invested_amount,
        "current_value": current_value,
        "annual_income": annual_income,
        "unrealized_gain": unrealized_gain,
        "total_return_eur": total_return_eur,
        "total_return_pct": total_return_pct,
        "app_url_name": app_url_name,
        "include_in_totals": include_in_totals,
        "analysis_only": not include_in_totals,
        "analysis_only_note": analysis_only_note,
    }


def build_svg_polyline(values, width: int = 720, height: int = 220, padding: int = 18) -> str:
    filtered = [float(value) for value in values if value is not None]
    if len(filtered) < 2:
        return ""

    min_value = min(filtered)
    max_value = max(filtered)
    if max_value == min_value:
        max_value += 1

    span_x = width - 2 * padding
    span_y = height - 2 * padding
    points = []
    total_points = len(values) - 1 or 1
    for index, value in enumerate(values):
        if value is None:
            continue
        value = float(value)
        x = padding + (span_x * index / total_points)
        normalized = (value - min_value) / (max_value - min_value)
        y = height - padding - (normalized * span_y)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def build_bank_liquidity_context():
    account_overview = build_bank_account_overview()
    imported_accounts = [account for account in account_overview["accounts"] if account["has_imported_data"]]
    imported_statements = list(
        BankStatementImport.objects.filter(
            import_status=BankStatementImport.ImportStatus.IMPORTED,
            statement_kind=BankStatementImport.StatementKind.ACCOUNT,
            period_end__isnull=False,
        )
        .exclude(closing_balance__isnull=True)
        .order_by("period_end", "imported_at", "id")
    )
    if not imported_statements:
        return {
            "positions": [],
            "accounts": [],
            "accounts_count": 0,
            "current_value": ZERO,
            "latest_month": None,
            "history": [],
            "history_line": "",
            "current_share_pct": ZERO,
        }

    latest_by_account_month = {}
    for statement in imported_statements:
        account_key = statement.iban or statement.account_name
        latest_by_account_month[(account_key, statement.month_label)] = statement

    latest_by_account = {}
    monthly_totals = defaultdict(
        lambda: {
            "label": "",
            "accounts": set(),
            "closing_balance": ZERO,
            "income": ZERO,
            "expenses": ZERO,
            "pension_contributions": ZERO,
            "dividends": ZERO,
        }
    )

    for (account_key, _month_label), statement in latest_by_account_month.items():
        account_key = statement.iban or statement.account_name
        latest_by_account[account_key] = statement

        bucket = monthly_totals[statement.month_label]
        bucket["label"] = statement.month_label
        bucket["accounts"].add(account_key)
        bucket["closing_balance"] += statement.closing_balance or ZERO
        bucket["income"] += statement.total_income
        bucket["expenses"] += statement.total_expenses
        bucket["pension_contributions"] += statement.total_pension_contributions
        bucket["dividends"] += statement.total_dividends

    positions = []
    for account in imported_accounts:
        balance = account["current_balance"] or ZERO
        positions.append(
            build_metrics(
                label=f"Liquidez {account['account_name']} ({account['ownership_label']})",
                asset_type="Efectivo bancario",
                invested_amount=balance,
                current_value=balance,
                annual_income=ZERO,
                app_url_name="banking:list",
                notes=f"Ultimo saldo final importado de {account['latest_month'] or 'sin periodo'}.",
            )
        )

    history = []
    for month_label in sorted(monthly_totals.keys()):
        row = monthly_totals[month_label]
        row["accounts_count"] = len(row["accounts"])
        row["net_cash_flow"] = row["income"] + row["dividends"] - row["expenses"] - row["pension_contributions"]
        history.append(row)

    return {
        "positions": positions,
        "accounts": imported_accounts,
        "accounts_count": len(imported_accounts),
        "current_value": sum((position["current_value"] for position in positions), ZERO),
        "latest_month": history[-1]["label"] if history else None,
        "history": history,
        "history_line": build_svg_polyline([row["closing_balance"] for row in history]),
        "current_share_pct": ZERO,
    }


def build_overview_metrics(state):
    section_map = {section["title"]: section for section in state["sections"]}
    total_current_value = state["summary"]["current_value"]
    liquid_cash = state["bank_liquidity"]["current_value"]
    banking_current_value = section_map.get("Banca", {}).get("current_value", ZERO)
    neos_group_current_value = state["neos_group_context"]["consolidated_current_value"]
    ibex_equities_current_value = section_map.get("Acciones cotizadas", {}).get("current_value", ZERO)
    highlighted_buckets_current_value = neos_group_current_value + ibex_equities_current_value + banking_current_value
    other_buckets_current_value = total_current_value - highlighted_buckets_current_value

    def share_of_total(value):
        return (value / total_current_value * Decimal("100")) if total_current_value else ZERO

    return {
        "total_current_value": total_current_value,
        "banking_current_value": banking_current_value,
        "banking_share_pct": share_of_total(banking_current_value),
        "liquid_cash": liquid_cash,
        "liquid_cash_share_pct": share_of_total(liquid_cash),
        "neos_group_current_value": neos_group_current_value,
        "neos_group_share_pct": share_of_total(neos_group_current_value),
        "ibex_equities_current_value": ibex_equities_current_value,
        "ibex_equities_share_pct": share_of_total(ibex_equities_current_value),
        "other_buckets_current_value": other_buckets_current_value,
    }


def build_owner_asset_overview(
    banking_ownership_overview: dict,
    property_ownership_overview: dict,
    equities,
    neos_group_context: dict,
) -> dict:
    groups = []
    equities = list(equities)
    banking_groups = {
        group["ownership_category"]: group for group in banking_ownership_overview["groups"]
    }
    property_groups = {
        group["ownership_category"]: group for group in property_ownership_overview["groups"]
    }
    neos_groups = {
        group["ownership_category"]: group for group in neos_group_context["owner_breakdown"]
    }

    for ownership_category, ownership_label in AssetOwnershipCategory.choices:
        bank_group = banking_groups.get(ownership_category, {})
        property_group = property_groups.get(ownership_category, {})
        neos_group = neos_groups.get(ownership_category, {})
        owner_equities = [position for position in equities if position.ownership_category == ownership_category]
        equities_current_value = sum((position.current_value for position in owner_equities), ZERO)
        equities_annual_income = sum((position.net_annual_income for position in owner_equities), ZERO)
        neos_current_value = neos_group.get("current_value", ZERO)
        neos_annual_income = neos_group.get("annual_income", ZERO)
        current_value = (
            bank_group.get("total_bank_value", ZERO)
            + property_group.get("current_value", ZERO)
            + equities_current_value
            + neos_current_value
        )
        annual_income = (
            bank_group.get("annual_income", ZERO)
            + property_group.get("annual_income", ZERO)
            + equities_annual_income
            + neos_annual_income
        )
        groups.append(
            {
                "ownership_category": ownership_category,
                "ownership_label": ownership_label,
                "current_value": current_value,
                "annual_income": annual_income,
                "banking_current_value": bank_group.get("total_bank_value", ZERO),
                "bank_liquidity_value": bank_group.get("current_balance", ZERO),
                "bank_products_value": bank_group.get("investment_value", ZERO),
                "business_current_value": neos_current_value,
                "business_annual_income": neos_annual_income,
                "equities_current_value": equities_current_value,
                "real_estate_current_value": property_group.get("current_value", ZERO),
                "real_estate_income": property_group.get("annual_income", ZERO),
                "bank_accounts_count": bank_group.get("accounts_count", 0),
                "properties_count": property_group.get("properties_count", 0),
                "equities_count": len(owner_equities),
                "has_data": bool(
                    current_value
                    or annual_income
                    or bank_group.get("cards_count", 0)
                    or property_group.get("properties_count", 0)
                    or neos_current_value
                ),
            }
        )

    return {
        "groups": groups,
        "summary": {
            "tracked_current_value": sum((group["current_value"] for group in groups), ZERO),
            "tracked_annual_income": sum((group["annual_income"] for group in groups), ZERO),
            "owners_with_data": sum(1 for group in groups if group["has_data"]),
        },
    }


def build_neos_group_context(ceramica_items, additives_items, materials_items):
    ceramica_value = sum((item["current_value"] for item in ceramica_items), ZERO)
    ceramica_income = sum((item["annual_income"] for item in ceramica_items), ZERO)
    additives_value = sum((item["current_value"] for item in additives_items), ZERO)
    additives_income = sum((item["annual_income"] for item in additives_items), ZERO)
    materials_value = sum((item["current_value"] for item in materials_items), ZERO)
    materials_income = sum((item["annual_income"] for item in materials_items), ZERO)

    uses_parent_holding = ceramica_value > ZERO
    consolidated_current_value = ceramica_value if uses_parent_holding else additives_value + materials_value
    consolidated_annual_income = ceramica_income if uses_parent_holding else additives_income + materials_income

    subsidiaries = []
    for section_title, items in (
        ("Neos Additives", additives_items),
        ("Neos Materials", materials_items),
    ):
        current_value = sum((item["current_value"] for item in items), ZERO)
        if not items and not current_value:
            continue
        config = NEOS_COMPANY_CONFIG[section_title]
        subsidiaries.append(
            {
                "title": section_title,
                "display_name": config["display_name"],
                "group_pct": config["group_pct"],
                "tracked_current_value": current_value,
                "effective_owner_breakdown": build_neos_owner_breakdown(current_value, ZERO),
                "consolidated_in_parent": uses_parent_holding and config["uses_parent_consolidation"],
            }
        )

    return {
        "holding_title": "Neos Ceramica",
        "holding_value": ceramica_value,
        "holding_income": ceramica_income,
        "consolidated_current_value": consolidated_current_value,
        "consolidated_annual_income": consolidated_annual_income,
        "uses_parent_holding": uses_parent_holding,
        "owner_breakdown": build_neos_owner_breakdown(consolidated_current_value, consolidated_annual_income),
        "subsidiaries": subsidiaries,
    }


def build_current_portfolio_state():
    bank_liquidity = build_bank_liquidity_context()
    bank_account_overview = build_bank_account_overview()
    banking_ownership_overview = build_banking_ownership_overview(
        bank_account_overview["accounts"],
        [],
        BankInvestmentPosition.objects.all(),
    )
    properties = list(PropertyInvestment.objects.all())
    property_ownership_overview = build_property_ownership_overview(properties)
    equities = list(EquityPosition.objects.all())
    additives_items = [obj.as_portfolio_position() for obj in AdditivesHolding.objects.all()]
    ceramica_items = [obj.as_portfolio_position() for obj in CeramicaHolding.objects.all()]
    materials_items = [obj.as_portfolio_position() for obj in MaterialsHolding.objects.all()]
    neos_group_context = build_neos_group_context(ceramica_items, additives_items, materials_items)
    owner_asset_overview = build_owner_asset_overview(
        banking_ownership_overview,
        property_ownership_overview,
        equities,
        neos_group_context,
    )
    linked_manual_account_ids = {
        account["account_id"] for account in bank_liquidity["accounts"] if account.get("account_id")
    }
    banking_items = [
        *bank_liquidity["positions"],
        *[
            obj.as_portfolio_position()
            for obj in BankBalance.objects.exclude(id__in=linked_manual_account_ids)
        ],
        *[obj.as_portfolio_position() for obj in BankInvestmentPosition.objects.all()],
    ]
    sections = [
        summarise_section(
            "Banca",
            banking_items,
            "banking:list",
        ),
        summarise_section(
            "Acciones cotizadas",
            [obj.as_portfolio_position() for obj in equities],
            "equities:list",
        ),
        summarise_section(
            "Neos Additives",
            additives_items,
            "neos_additives:list",
            include_in_totals=not neos_group_context["uses_parent_holding"],
            analysis_only_note=(
                "Se muestra para analisis del conglomerado, pero su valor ya queda consolidado en Neos Ceramica."
                if neos_group_context["uses_parent_holding"]
                else ""
            ),
        ),
        summarise_section(
            "Neos Ceramica",
            ceramica_items,
            "neos_ceramica:list",
        ),
        summarise_section(
            "Neos Materials",
            materials_items,
            "neos_materials:list",
            include_in_totals=not neos_group_context["uses_parent_holding"],
            analysis_only_note=(
                "Se muestra para analisis del conglomerado, pero su valor ya queda consolidado en Neos Ceramica."
                if neos_group_context["uses_parent_holding"]
                else ""
            ),
        ),
        summarise_section(
            "Inmuebles",
            [obj.as_portfolio_position() for obj in properties],
            "real_estate:list",
        ),
    ]

    included_sections = [section for section in sections if section["include_in_totals"]]
    all_items = sorted(
        [item for section in included_sections for item in section["items"]],
        key=lambda item: item["total_return_pct"],
        reverse=True,
    )

    invested_amount = sum((section["invested_amount"] for section in included_sections), ZERO)
    current_value = sum((section["current_value"] for section in included_sections), ZERO)
    annual_income = sum((section["annual_income"] for section in included_sections), ZERO)
    unrealized_gain = current_value - invested_amount
    total_return_eur = unrealized_gain + annual_income
    total_return_pct = (total_return_eur / invested_amount * Decimal("100")) if invested_amount else ZERO

    summary = {
        "invested_amount": invested_amount,
        "current_value": current_value,
        "annual_income": annual_income,
        "unrealized_gain": unrealized_gain,
        "total_return_eur": total_return_eur,
        "total_return_pct": total_return_pct,
    }

    return {
        "sections": sections,
        "top_positions": all_items[:10],
        "summary": summary,
        "bank_liquidity": bank_liquidity,
        "banking_ownership_overview": banking_ownership_overview,
        "property_ownership_overview": property_ownership_overview,
        "owner_asset_overview": owner_asset_overview,
        "neos_group_context": neos_group_context,
    }


def get_household_alert_settings():
    settings, _ = HouseholdAlertSettings.objects.get_or_create(name="default")
    return settings


def capture_portfolio_snapshot(snapshot_date: date | None = None):
    snapshot_date = snapshot_date or timezone.localdate()
    state = build_current_portfolio_state()
    summary = state["summary"]
    section_values = {
        section["title"]: {
            "invested_amount": float(section["invested_amount"]),
            "current_value": float(section["current_value"]),
            "annual_income": float(section["annual_income"]),
            "include_in_totals": section["include_in_totals"],
        }
        for section in state["sections"]
    }
    snapshot, _ = PortfolioSnapshot.objects.update_or_create(
        snapshot_date=snapshot_date,
        defaults={
            "invested_amount": summary["invested_amount"],
            "current_value": summary["current_value"],
            "annual_income": summary["annual_income"],
            "total_return_eur": summary["total_return_eur"],
            "total_return_pct": summary["total_return_pct"],
            "section_values": section_values,
        },
    )
    return snapshot


def ensure_daily_snapshot():
    today = timezone.localdate()
    if not PortfolioSnapshot.objects.filter(snapshot_date=today).exists():
        return capture_portfolio_snapshot(today)
    return PortfolioSnapshot.objects.get(snapshot_date=today)


def build_snapshot_context():
    snapshots = list(PortfolioSnapshot.objects.order_by("snapshot_date"))
    chart_values = [snapshot.current_value for snapshot in snapshots]
    chart_line = build_svg_polyline(chart_values)
    latest_snapshot = snapshots[-1] if snapshots else None
    previous_snapshot = snapshots[-2] if len(snapshots) >= 2 else None
    current_change_pct = None
    if latest_snapshot and previous_snapshot and previous_snapshot.current_value:
        current_change_pct = ((latest_snapshot.current_value / previous_snapshot.current_value) - 1) * Decimal("100")

    return {
        "snapshots": list(reversed(snapshots[-10:])),
        "snapshot_count": len(snapshots),
        "snapshot_line": chart_line,
        "latest_snapshot": latest_snapshot,
        "current_change_pct": current_change_pct,
    }


def build_spending_alerts():
    settings = get_household_alert_settings()
    if not settings.active:
        return {"alerts": [], "latest_month": None}

    imported_statements = list(
        BankStatementImport.objects.filter(
            import_status=BankStatementImport.ImportStatus.IMPORTED,
            statement_kind=BankStatementImport.StatementKind.ACCOUNT,
            period_end__isnull=False,
        )
    )
    if not imported_statements:
        return {"alerts": [], "latest_month": None}

    month_totals = defaultdict(lambda: ZERO)
    concept_totals = defaultdict(lambda: defaultdict(lambda: ZERO))
    months = sorted({statement.month_label for statement in imported_statements})
    latest_month = months[-1]

    for statement in imported_statements:
        month_totals[statement.month_label] += statement.total_expenses

    expense_movements = (
        BankMovement.objects.filter(
            statement_import__import_status=BankStatementImport.ImportStatus.IMPORTED,
            statement_import__statement_kind=BankStatementImport.StatementKind.ACCOUNT,
            statement_import__period_end__isnull=False,
            movement_group=BankMovement.MovementGroup.EXPENSE,
        )
        .select_related("statement_import")
    )
    for movement in expense_movements:
        month_label = movement.statement_import.month_label
        concept_totals[movement.concept_bucket][month_label] += abs(movement.amount)

    alerts = []
    previous_months = months[:-1][-settings.lookback_months :]
    latest_total = month_totals[latest_month]
    avg_previous_total = (
        sum((month_totals[month] for month in previous_months), ZERO) / len(previous_months)
        if previous_months
        else None
    )

    if latest_total > settings.total_monthly_expense_limit:
        alerts.append(
            {
                "scope": "Gasto mensual total",
                "severity": "Alta",
                "month": latest_month,
                "current_amount": latest_total,
                "reference_amount": settings.total_monthly_expense_limit,
                "message": "El gasto mensual total supera el limite del hogar.",
            }
        )
    if avg_previous_total and latest_total > avg_previous_total * (Decimal("1") + settings.expense_spike_threshold_pct / Decimal("100")):
        alerts.append(
            {
                "scope": "Gasto mensual total",
                "severity": "Media",
                "month": latest_month,
                "current_amount": latest_total,
                "reference_amount": avg_previous_total,
                "message": "El gasto mensual total esta claramente por encima de la media reciente.",
            }
        )

    for concept, month_values in concept_totals.items():
        current_amount = month_values.get(latest_month, ZERO)
        if current_amount <= ZERO:
            continue
        if current_amount > settings.concept_monthly_expense_limit:
            alerts.append(
                {
                    "scope": concept,
                    "severity": "Alta",
                    "month": latest_month,
                    "current_amount": current_amount,
                    "reference_amount": settings.concept_monthly_expense_limit,
                    "message": "Este concepto de gasto supera el limite mensual por concepto.",
                }
            )
        previous_values = [month_values.get(month, ZERO) for month in previous_months if month_values.get(month, ZERO) > ZERO]
        if previous_values:
            avg_previous = sum(previous_values, ZERO) / len(previous_values)
            if current_amount > avg_previous * (Decimal("1") + settings.expense_spike_threshold_pct / Decimal("100")):
                alerts.append(
                    {
                        "scope": concept,
                        "severity": "Media",
                        "month": latest_month,
                        "current_amount": current_amount,
                        "reference_amount": avg_previous,
                        "message": "Este concepto de gasto esta muy por encima de su media reciente.",
                    }
                )

    alerts.sort(key=lambda item: (item["severity"] != "Alta", -item["current_amount"]))
    return {"alerts": alerts, "latest_month": latest_month, "settings": settings}


def build_portfolio_dashboard():
    ensure_daily_snapshot()
    state = build_current_portfolio_state()
    snapshot_context = build_snapshot_context()
    alert_context = build_spending_alerts()
    if state["summary"]["current_value"]:
        state["bank_liquidity"]["current_share_pct"] = (
            state["bank_liquidity"]["current_value"] / state["summary"]["current_value"] * Decimal("100")
        )

    return {
        "page_title": "Centro de inversiones personales",
        "overview": build_overview_metrics(state),
        **state,
        **snapshot_context,
        "spending_alerts": alert_context["alerts"],
        "alerts_latest_month": alert_context["latest_month"],
        "alert_settings": alert_context.get("settings"),
    }
