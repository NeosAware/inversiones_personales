from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction

from .metrics import ZERO


TWO_PLACES = Decimal("0.01")
SPANISH_NUMBER_RE = re.compile(r"-?\d{1,3}(?:\.\d{3})*,\d{2}")


def quantize_money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def parse_spanish_decimal(value: str) -> Decimal:
    cleaned = value.replace(".", "").replace(",", ".").strip()
    return Decimal(cleaned)


def normalize_text(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip().upper()


def extract_last_decimal(value: str) -> Decimal | None:
    matches = SPANISH_NUMBER_RE.findall(value)
    if not matches:
        return None
    return quantize_money(parse_spanish_decimal(matches[-1]))


def extract_financial_metrics_from_pages(pages: list[str]) -> dict:
    net_equity = None
    share_capital = None
    profit_after_tax = None

    for page in pages:
        for line in page.splitlines():
            normalized = normalize_text(line)
            if net_equity is None and "PATRIMONIO NETO" in normalized:
                net_equity = extract_last_decimal(line)
            if share_capital is None and (
                normalized.startswith("I. CAPITAL")
                or normalized.startswith("1. CAPITAL ESCRITURADO")
                or "CAPITAL ESCRITURADO" in normalized
            ):
                share_capital = extract_last_decimal(line)
            if profit_after_tax is None and (
                normalized.startswith("D) RESULTADO DEL EJERCICIO")
                or normalized.startswith("VII. RESULTADO DEL EJERCICIO")
                or normalized.startswith("RESULTADO DEL EJERCICIO")
            ):
                inline_profit = extract_last_decimal(line)
                if inline_profit is not None:
                    profit_after_tax = inline_profit

    if profit_after_tax is None:
        for page in pages:
            page_lines = page.splitlines()
            if any(
                normalize_text(line).startswith("RESULTADO DE LA CUENTA DE PERDIDAS Y GANANCIAS")
                for line in page_lines
            ):
                page_numbers = [parse_spanish_decimal(match) for match in SPANISH_NUMBER_RE.findall(page)]
                if page_numbers:
                    profit_after_tax = quantize_money(page_numbers[-1])
                    break

    return {
        "net_equity": net_equity,
        "share_capital": share_capital,
        "profit_after_tax": profit_after_tax,
    }


def read_pdf_pages(file_source) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValidationError("Se necesita pypdf para procesar los PDF de valoracion anual.") from exc

    if hasattr(file_source, "open"):
        file_source.open("rb")
    try:
        reader = PdfReader(file_source)
        return [(page.extract_text() or "") for page in reader.pages]
    finally:
        if hasattr(file_source, "close"):
            file_source.close()


def extract_financial_metrics_from_source(file_source) -> dict:
    try:
        return extract_financial_metrics_from_pages(read_pdf_pages(file_source))
    except Exception:
        return {
            "net_equity": None,
            "share_capital": None,
            "profit_after_tax": None,
        }


def extract_financial_metrics_from_record(record) -> dict:
    metrics = {
        "net_equity": None,
        "share_capital": None,
        "profit_after_tax": None,
    }

    balance_sources = [record.balance_pdf, record.corporate_tax_pdf, record.profit_loss_pdf]
    profit_sources = [record.profit_loss_pdf, record.corporate_tax_pdf, record.balance_pdf]

    for source in balance_sources:
        if not source:
            continue
        parsed = extract_financial_metrics_from_source(source)
        if metrics["net_equity"] is None and parsed["net_equity"] is not None:
            metrics["net_equity"] = parsed["net_equity"]
        if metrics["share_capital"] is None and parsed["share_capital"] is not None:
            metrics["share_capital"] = parsed["share_capital"]

    for source in profit_sources:
        if not source:
            continue
        parsed = extract_financial_metrics_from_source(source)
        if parsed["profit_after_tax"] is not None:
            metrics["profit_after_tax"] = parsed["profit_after_tax"]
            break

    return metrics


def recalculate_company_valuations(valuation_model):
    records = list(valuation_model.objects.order_by("year"))
    profit_map = {record.year: record.profit_after_tax for record in records if record.profit_after_tax is not None}

    for record in records:
        required_years = [record.year - 2, record.year - 1, record.year]
        average_profit = None
        capitalised_value = None
        if all(profit_map.get(year) is not None for year in required_years):
            average_profit = quantize_money(sum((profit_map[year] for year in required_years), ZERO) / Decimal("3"))
            capitalised_value = quantize_money(average_profit * Decimal("5"))

        theoretical_value = quantize_money(record.net_equity) if record.net_equity is not None else None
        nominal_value = quantize_money(record.share_capital) if record.share_capital is not None else None

        calculation_notes = []
        if record.balance_approved and record.audited_favorable and theoretical_value is not None:
            tax_company_value = theoretical_value
            valuation_method = valuation_model.ValuationMethod.AUDITED_BALANCE
        else:
            candidates = []
            if nominal_value is not None:
                candidates.append((valuation_model.ValuationMethod.NOMINAL_VALUE, nominal_value))
            if theoretical_value is not None:
                candidates.append((valuation_model.ValuationMethod.THEORETICAL_VALUE, theoretical_value))
            if capitalised_value is not None:
                candidates.append((valuation_model.ValuationMethod.EARNINGS_CAPITALISATION, capitalised_value))

            if candidates:
                valuation_method, tax_company_value = max(candidates, key=lambda item: item[1])
            else:
                valuation_method = ""
                tax_company_value = None

            missing_years = [str(year) for year in required_years if profit_map.get(year) is None]
            if missing_years:
                calculation_notes.append(
                    f"El valor por capitalizacion de beneficios esta pendiente. Faltan datos de resultado para: {', '.join(missing_years)}."
                )

        if theoretical_value is None:
            calculation_notes.append("Falta el patrimonio neto del ultimo balance.")
        if nominal_value is None:
            calculation_notes.append("Falta el capital social.")
        if record.balance_approved and not record.audited_favorable:
            calculation_notes.append(
                "Se ha cargado un balance aprobado sin auditoria favorable. La comparacion AEAT usa la regla del mayor de tres."
            )

        owner_value = None
        if tax_company_value is not None:
            owner_value = quantize_money(tax_company_value * record.ownership_pct / Decimal("100"))

        record.three_year_average_profit = average_profit
        record.capitalised_earnings_value = capitalised_value
        record.tax_company_value = quantize_money(tax_company_value) if tax_company_value is not None else None
        record.owner_value = owner_value
        record.valuation_method = valuation_method
        record.calculation_note = " ".join(calculation_notes).strip()
        record.save(
            update_fields=[
                "three_year_average_profit",
                "capitalised_earnings_value",
                "tax_company_value",
                "owner_value",
                "valuation_method",
                "calculation_note",
                "updated_at",
            ]
        )


def sync_latest_valuation_to_holding(valuation_model, holding_model, holding_name: str):
    latest = valuation_model.objects.exclude(owner_value__isnull=True).order_by("-year").first()
    if latest is None:
        return None

    holding, created = holding_model.objects.get_or_create(
        investment_name=holding_name,
        defaults={
            "invested_amount": latest.owner_value or ZERO,
            "current_valuation": latest.owner_value or ZERO,
            "annual_dividend_income": ZERO,
            "notes": f"Sincronizado automaticamente desde la valoracion AEAT anual de {latest.year}.",
        },
    )
    holding.current_valuation = latest.owner_value or holding.current_valuation
    if created or not holding.invested_amount:
        holding.invested_amount = latest.owner_value or ZERO
    holding.notes = (
        f"Sincronizado automaticamente desde la valoracion AEAT anual. "
        f"Ultimo ejercicio {latest.year}, participacion {latest.ownership_pct} %, metodo {latest.get_valuation_method_display().lower()}."
    )
    holding.save()
    return holding


@transaction.atomic
def save_annual_valuation(cleaned_data: dict, valuation_model, holding_model, holding_name: str):
    record, _created = valuation_model.objects.get_or_create(year=cleaned_data["year"])
    record.ownership_pct = cleaned_data["ownership_pct"]
    record.balance_approved = cleaned_data["balance_approved"]
    record.audited_favorable = cleaned_data["audited_favorable"]

    for field_name in ("balance_pdf", "profit_loss_pdf", "corporate_tax_pdf"):
        uploaded_file = cleaned_data.get(field_name)
        if uploaded_file:
            setattr(record, field_name, uploaded_file)

    record.save()

    parsed_metrics = extract_financial_metrics_from_record(record)
    for field_name in ("net_equity", "share_capital", "profit_after_tax"):
        manual_value = cleaned_data.get(field_name)
        parsed_value = parsed_metrics.get(field_name)
        if manual_value is not None:
            setattr(record, field_name, manual_value)
        elif parsed_value is not None:
            setattr(record, field_name, parsed_value)

    record.save()

    recalculate_company_valuations(valuation_model)
    sync_latest_valuation_to_holding(valuation_model, holding_model, holding_name)
    record.refresh_from_db()
    return record


def build_company_valuation_context(holding_model, valuation_model):
    holdings = list(holding_model.objects.all())
    valuations = list(valuation_model.objects.order_by("-year"))
    latest = valuations[0] if valuations else None

    summary = {
        "invested_amount": sum((holding.invested_amount for holding in holdings), ZERO),
        "current_value": sum((holding.current_valuation for holding in holdings), ZERO),
        "annual_income": sum((holding.annual_dividend_income for holding in holdings), ZERO),
    }

    annual_summary = {
        "years_loaded": len(valuations),
        "latest_year": latest.year if latest else None,
        "company_value": latest.tax_company_value if latest and latest.tax_company_value is not None else ZERO,
        "owner_value": latest.owner_value if latest and latest.owner_value is not None else ZERO,
        "ownership_pct": latest.ownership_pct if latest else None,
        "method": latest.get_valuation_method_display() if latest and latest.valuation_method else "-",
        "capitalised_value": latest.capitalised_earnings_value if latest and latest.capitalised_earnings_value is not None else None,
        "three_year_average_profit": latest.three_year_average_profit if latest else None,
        "note": latest.calculation_note if latest else "",
    }

    return {
        "holdings": holdings,
        "summary": summary,
        "annual_valuations": valuations,
        "annual_summary": annual_summary,
        "latest_annual_valuation": latest,
    }
