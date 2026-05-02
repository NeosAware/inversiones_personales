from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from banking.models import BankMovement, BankStatementImport

from .models import PlannedInvestmentPayment, SalesForecastSnapshot
from .ownership import AssetOwnershipCategory


ZERO = Decimal("0.00")
ONE = Decimal("1.00")
HUNDRED = Decimal("100")
CARD_SETTLEMENT_BUCKET = "Liquidacion de tarjeta"


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    return date(value.year + month_index // 12, month_index % 12 + 1, 1)


def month_label_for_date(value: date | None) -> str:
    if not value:
        return ""
    return value.strftime("%Y-%m")


def format_short_month(month_label: str) -> str:
    try:
        year_text, month_text = month_label.split("-", 1)
        return f"{month_text}/{year_text[-2:]}"
    except ValueError:
        return month_label


def clamp_decimal(value: Decimal, minimum: Decimal, maximum: Decimal) -> Decimal:
    return max(minimum, min(maximum, value))


def average_decimal(values: list[Decimal]) -> Decimal:
    values = [value for value in values if value is not None]
    if not values:
        return ZERO
    return sum(values, ZERO) / Decimal(len(values))


def signed_payment_amount(payment: PlannedInvestmentPayment, *, use_effective: bool = False) -> Decimal:
    if use_effective:
        return payment.signed_effective_amount
    return payment.signed_amount


def decorate_payment(payment: PlannedInvestmentPayment, today: date) -> dict:
    amount = signed_payment_amount(payment, use_effective=payment.status == PlannedInvestmentPayment.Status.PAID)
    is_open = payment.status == PlannedInvestmentPayment.Status.PLANNED
    days_until_due = (payment.due_date - today).days
    if payment.status == PlannedInvestmentPayment.Status.PAID:
        status_tone = "good"
    elif payment.status == PlannedInvestmentPayment.Status.CANCELLED:
        status_tone = "muted"
    elif days_until_due < 0:
        status_tone = "warn"
    else:
        status_tone = "good" if payment.flow_type == PlannedInvestmentPayment.FlowType.INFLOW else ""

    return {
        "payment": payment,
        "signed_amount": amount,
        "display_amount": abs(amount),
        "amount_tone": "warn" if amount < ZERO else "good",
        "is_open": is_open,
        "is_overdue": is_open and payment.due_date < today,
        "days_until_due": days_until_due,
        "status_tone": status_tone,
        "effective_date": payment.paid_date if payment.status == PlannedInvestmentPayment.Status.PAID else payment.due_date,
    }


def build_investment_cashflow_plan(
    *,
    current_liquidity: Decimal = ZERO,
    actual_monthly_rows: list[dict] | None = None,
    months_ahead: int = 6,
    today: date | None = None,
) -> dict:
    today = today or timezone.localdate()
    first_month = date(today.year, today.month, 1)
    current_month_label = month_label_for_date(first_month)
    payments = list(PlannedInvestmentPayment.objects.order_by("due_date", "id"))
    open_payments = [payment for payment in payments if payment.status == PlannedInvestmentPayment.Status.PLANNED]
    paid_payments = [payment for payment in payments if payment.status == PlannedInvestmentPayment.Status.PAID]
    finished_payments = [
        payment
        for payment in payments
        if payment.status in {PlannedInvestmentPayment.Status.PAID, PlannedInvestmentPayment.Status.CANCELLED}
    ]

    next_window_end = today + timedelta(days=90)
    open_outflow_total = sum(
        (payment.amount for payment in open_payments if payment.flow_type == PlannedInvestmentPayment.FlowType.OUTFLOW),
        ZERO,
    )
    open_inflow_total = sum(
        (payment.amount for payment in open_payments if payment.flow_type == PlannedInvestmentPayment.FlowType.INFLOW),
        ZERO,
    )
    open_net_total = open_inflow_total - open_outflow_total
    next_90_net = sum(
        (
            signed_payment_amount(payment)
            for payment in open_payments
            if payment.due_date <= next_window_end
        ),
        ZERO,
    )
    paid_this_year_net = sum(
        (
            signed_payment_amount(payment, use_effective=True)
            for payment in paid_payments
            if (payment.paid_date or payment.due_date).year == today.year
        ),
        ZERO,
    )

    month_map = defaultdict(
        lambda: {
            "label": "",
            "short_label": "",
            "planned_inflows": ZERO,
            "planned_outflows": ZERO,
            "planned_net": ZERO,
            "payments_count": 0,
        }
    )
    planned_month_labels = set()
    for payment in open_payments:
        due_label = month_label_for_date(payment.due_date)
        label = current_month_label if due_label < current_month_label else due_label
        planned_month_labels.add(label)
        row = month_map[label]
        row["label"] = label
        row["short_label"] = format_short_month(label)
        amount = signed_payment_amount(payment)
        if amount >= ZERO:
            row["planned_inflows"] += amount
        else:
            row["planned_outflows"] += abs(amount)
        row["planned_net"] += amount
        row["payments_count"] += 1

    base_labels = {month_label_for_date(add_months(first_month, index)) for index in range(max(months_ahead, 1))}
    visible_labels = sorted(base_labels | planned_month_labels)[:8]
    actual_by_label = {row.get("label"): row for row in actual_monthly_rows or [] if row.get("label")}
    projected_liquidity = current_liquidity
    month_rows = []
    for label in visible_labels:
        row = month_map[label]
        row["label"] = label
        row["short_label"] = row["short_label"] or format_short_month(label)
        row["actual_net_cash_flow"] = actual_by_label.get(label, {}).get("net_cash_flow")
        projected_liquidity += row["planned_net"]
        row["projected_liquidity"] = projected_liquidity
        row["net_tone"] = "good" if row["planned_net"] >= ZERO else "warn"
        month_rows.append(row)

    upcoming = sorted(open_payments, key=lambda item: (item.due_date, item.id))
    recent_finished = sorted(
        finished_payments,
        key=lambda item: (item.paid_date or item.due_date, item.id),
        reverse=True,
    )

    return {
        "available": bool(payments),
        "has_open_items": bool(open_payments),
        "summary": {
            "open_count": len(open_payments),
            "open_outflow_total": open_outflow_total,
            "open_inflow_total": open_inflow_total,
            "open_net_total": open_net_total,
            "next_90_days_net": next_90_net,
            "overdue_count": sum(1 for payment in open_payments if payment.due_date < today),
            "paid_this_year_net": paid_this_year_net,
            "current_liquidity": current_liquidity,
            "projected_liquidity_after_open": current_liquidity + open_net_total,
        },
        "upcoming_payments": [decorate_payment(payment, today) for payment in upcoming[:8]],
        "recent_finished_payments": [decorate_payment(payment, today) for payment in recent_finished[:4]],
        "month_rows": month_rows,
    }


def build_actual_cashflow_split_by_month() -> dict:
    rows = defaultdict(
        lambda: {
            "label": "",
            "income": ZERO,
            "dividends": ZERO,
            "general_expenses": ZERO,
            "personal_expenses": ZERO,
            "investment_plan_contributions": ZERO,
            "net_cash_flow": ZERO,
        }
    )
    movements = (
        BankMovement.objects.filter(
            statement_import__import_status=BankStatementImport.ImportStatus.IMPORTED,
        )
        .select_related("statement_import")
        .order_by("booking_date", "id")
    )
    has_card_details_by_month = set(
        BankStatementImport.objects.filter(
            import_status=BankStatementImport.ImportStatus.IMPORTED,
            statement_kind=BankStatementImport.StatementKind.CARD,
            period_end__isnull=False,
        ).values_list("period_end", flat=True)
    )
    card_month_labels = {month_label_for_date(item) for item in has_card_details_by_month if item}

    for movement in movements:
        statement = movement.statement_import
        month_label = month_label_for_date(movement.booking_date)
        if not month_label:
            continue

        if (
            statement.statement_kind == BankStatementImport.StatementKind.ACCOUNT
            and movement.movement_group == BankMovement.MovementGroup.EXPENSE
            and movement.concept_bucket == CARD_SETTLEMENT_BUCKET
            and month_label in card_month_labels
        ):
            continue

        row = rows[month_label]
        row["label"] = month_label
        amount_abs = abs(movement.amount)
        if movement.movement_group == BankMovement.MovementGroup.INCOME:
            row["income"] += amount_abs
        elif movement.movement_group == BankMovement.MovementGroup.DIVIDEND:
            row["dividends"] += amount_abs
        elif movement.movement_group == BankMovement.MovementGroup.PENSION:
            row["investment_plan_contributions"] += amount_abs
        elif movement.movement_group == BankMovement.MovementGroup.EXPENSE:
            if statement.ownership_category == AssetOwnershipCategory.JOINT:
                row["general_expenses"] += amount_abs
            else:
                row["personal_expenses"] += amount_abs

    for row in rows.values():
        row["total_expenses"] = row["general_expenses"] + row["personal_expenses"]
        row["gross_inflows"] = row["income"] + row["dividends"]
        row["net_cash_flow"] = row["gross_inflows"] - row["total_expenses"] - row["investment_plan_contributions"]

    return rows


def resolve_current_liquidity_from_imported_accounts() -> Decimal:
    latest_by_account = {}
    statements = (
        BankStatementImport.objects.filter(
            import_status=BankStatementImport.ImportStatus.IMPORTED,
            statement_kind=BankStatementImport.StatementKind.ACCOUNT,
            period_end__isnull=False,
        )
        .exclude(closing_balance__isnull=True)
        .order_by("period_end", "imported_at", "id")
    )
    for statement in statements:
        account_key = statement.iban or statement.account_name
        latest_by_account[account_key] = statement.closing_balance or ZERO
    return sum(latest_by_account.values(), ZERO)


def calculate_sales_deviation_pct(sales_rows: list[SalesForecastSnapshot]) -> Decimal:
    deviations = []
    for snapshot in sales_rows:
        if snapshot.actual_margin is None or not snapshot.forecast_margin:
            continue
        deviations.append(abs(snapshot.actual_margin - snapshot.forecast_margin) / abs(snapshot.forecast_margin))
    if not deviations:
        return Decimal("0.15")
    return clamp_decimal(average_decimal(deviations), Decimal("0.00"), Decimal("0.60"))


def investment_decision_for_score(score: Decimal, capacity: Decimal, liquidity_gap: Decimal) -> str:
    if liquidity_gap < ZERO or capacity <= ZERO:
        return "Esperar"
    if score >= Decimal("78"):
        return "Invertir ahora"
    if score >= Decimal("64"):
        return "Invertir por tramos"
    if score >= Decimal("50"):
        return "Preparar entrada"
    return "Esperar"


def build_cashflow_management_context(*, months_ahead: int = 9, today: date | None = None) -> dict:
    today = today or timezone.localdate()
    first_month = date(today.year, today.month, 1)
    actual_by_month = build_actual_cashflow_split_by_month()
    sales_rows = list(SalesForecastSnapshot.objects.order_by("month"))
    sales_by_label = {month_label_for_date(snapshot.month): snapshot for snapshot in sales_rows}
    investment_plan = build_investment_cashflow_plan(
        current_liquidity=ZERO,
        actual_monthly_rows=list(actual_by_month.values()),
        months_ahead=months_ahead,
        today=today,
    )
    plan_by_label = {row["label"]: row for row in investment_plan["month_rows"]}

    month_labels = {month_label_for_date(add_months(first_month, index)) for index in range(max(months_ahead, 1))}
    month_labels.update(label for label in sales_by_label if label >= month_label_for_date(first_month))
    month_labels.update(plan_by_label)
    month_labels = sorted(label for label in month_labels if label)[:12]

    recent_actuals = [
        row
        for label, row in sorted(actual_by_month.items(), reverse=True)
        if label < month_label_for_date(first_month)
    ][:6]
    average_general_expenses = average_decimal([row["general_expenses"] for row in recent_actuals])
    average_personal_expenses = average_decimal([row["personal_expenses"] for row in recent_actuals])
    average_gross_inflows = average_decimal([row["gross_inflows"] for row in recent_actuals])
    average_investment_plan_contributions = average_decimal([row["investment_plan_contributions"] for row in recent_actuals])
    average_total_expenses = average_general_expenses + average_personal_expenses + average_investment_plan_contributions
    minimum_reserve = max(average_total_expenses * Decimal("3.00"), Decimal("25000.00"))
    sales_deviation_pct = calculate_sales_deviation_pct(sales_rows)
    reserve_with_deviation = minimum_reserve * (ONE + sales_deviation_pct)

    latest_actual_liquidity = resolve_current_liquidity_from_imported_accounts()

    projected_liquidity = latest_actual_liquidity
    simulation_rows = []
    for label in month_labels:
        actual = actual_by_month.get(label)
        plan = plan_by_label.get(
            label,
            {
                "planned_inflows": ZERO,
                "planned_outflows": ZERO,
                "planned_net": ZERO,
                "payments_count": 0,
            },
        )
        sales = sales_by_label.get(label)
        sales_margin_forecast = sales.forecast_margin if sales else ZERO
        sales_margin_actual = sales.actual_margin if sales and sales.actual_margin is not None else None
        is_future = label >= month_label_for_date(first_month)

        expected_gross_inflows = average_gross_inflows if is_future else actual.get("gross_inflows", ZERO) if actual else ZERO
        expected_general_expenses = average_general_expenses if is_future else actual.get("general_expenses", ZERO) if actual else ZERO
        expected_personal_expenses = average_personal_expenses if is_future else actual.get("personal_expenses", ZERO) if actual else ZERO
        expected_plan_contributions = (
            average_investment_plan_contributions
            if is_future
            else actual.get("investment_plan_contributions", ZERO) if actual else ZERO
        )
        expected_operating_net = (
            expected_gross_inflows
            + sales_margin_forecast
            - expected_general_expenses
            - expected_personal_expenses
            - expected_plan_contributions
        )
        simulated_net = expected_operating_net + plan["planned_net"]
        if is_future:
            projected_liquidity += simulated_net
        elif actual:
            projected_liquidity += ZERO

        capacity = max(projected_liquidity - reserve_with_deviation, ZERO)
        liquidity_gap = projected_liquidity - reserve_with_deviation
        capacity_score = clamp_decimal((capacity / Decimal("30000.00")) * Decimal("35.00"), ZERO, Decimal("35.00"))
        generation_score = clamp_decimal((expected_operating_net / Decimal("15000.00")) * Decimal("25.00"), ZERO, Decimal("25.00"))
        commitment_score = Decimal("20.00")
        if plan["planned_outflows"] > ZERO:
            commitment_score = clamp_decimal(
                Decimal("20.00") - (plan["planned_outflows"] / Decimal("30000.00")) * Decimal("20.00"),
                ZERO,
                Decimal("20.00"),
            )
        margin_score = clamp_decimal((sales_margin_forecast / Decimal("20000.00")) * Decimal("15.00"), ZERO, Decimal("15.00"))
        deviation_penalty = sales_deviation_pct * Decimal("25.00")
        reserve_penalty = Decimal("20.00") if liquidity_gap < ZERO else ZERO
        score = clamp_decimal(
            capacity_score + generation_score + commitment_score + margin_score + Decimal("5.00") - deviation_penalty - reserve_penalty,
            ZERO,
            Decimal("100.00"),
        )

        simulation_rows.append(
            {
                "label": label,
                "short_label": format_short_month(label),
                "actual_net_cash_flow": actual.get("net_cash_flow") if actual else None,
                "actual_general_expenses": actual.get("general_expenses") if actual else None,
                "actual_personal_expenses": actual.get("personal_expenses") if actual else None,
                "expected_gross_inflows": expected_gross_inflows,
                "expected_general_expenses": expected_general_expenses,
                "expected_personal_expenses": expected_personal_expenses,
                "expected_total_expenses": expected_general_expenses + expected_personal_expenses + expected_plan_contributions,
                "sales_forecast": sales,
                "sales_margin_forecast": sales_margin_forecast,
                "sales_margin_actual": sales_margin_actual,
                "investment_net": plan["planned_net"],
                "planned_outflows": plan["planned_outflows"],
                "planned_inflows": plan["planned_inflows"],
                "payments_count": plan["payments_count"],
                "simulated_net": simulated_net,
                "projected_liquidity": projected_liquidity,
                "investment_capacity": capacity,
                "liquidity_gap": liquidity_gap,
                "score": score,
                "score_tone": "good" if score >= Decimal("64") else "warn" if score < Decimal("45") else "",
                "decision": investment_decision_for_score(score, capacity, liquidity_gap),
                "is_future": is_future,
            }
        )

    future_rows = [row for row in simulation_rows if row["is_future"]]
    best_window = max(future_rows, key=lambda item: (item["score"], item["investment_capacity"]), default=None)
    prudent_amount = ZERO
    if best_window:
        prudent_amount = (best_window["investment_capacity"] * Decimal("0.70")).quantize(Decimal("0.01"))

    real_vs_forecast = []
    for snapshot in sales_rows:
        actual_margin = snapshot.actual_margin
        if actual_margin is None:
            continue
        real_vs_forecast.append(
            {
                "snapshot": snapshot,
                "label": month_label_for_date(snapshot.month),
                "short_label": format_short_month(month_label_for_date(snapshot.month)),
                "forecast_margin": snapshot.forecast_margin,
                "actual_margin": actual_margin,
                "margin_deviation": actual_margin - snapshot.forecast_margin,
            }
        )

    return {
        "investment_plan": investment_plan,
        "sales_forecasts": list(reversed(sales_rows))[:8],
        "real_vs_forecast": list(reversed(real_vs_forecast))[:6],
        "simulation_rows": simulation_rows,
        "best_window": best_window,
        "summary": {
            "latest_actual_liquidity": latest_actual_liquidity,
            "minimum_reserve": minimum_reserve,
            "reserve_with_deviation": reserve_with_deviation,
            "sales_deviation_pct": sales_deviation_pct * HUNDRED,
            "average_general_expenses": average_general_expenses,
            "average_personal_expenses": average_personal_expenses,
            "average_gross_inflows": average_gross_inflows,
            "prudent_amount": prudent_amount,
        },
    }
