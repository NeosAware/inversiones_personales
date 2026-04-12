from __future__ import annotations

import unicodedata
from decimal import Decimal


ZERO = Decimal("0.00")

BROKER_TRADE_CHANNEL_CHOICES = (
    ("app", "App"),
    ("web", "Web"),
    ("office", "Oficina"),
    ("contact_center", "Contact Center"),
    ("other", "Otro"),
)

CHANNEL_LABELS = {key: label for key, label in BROKER_TRADE_CHANNEL_CHOICES}
MARKET_SCOPE_LABELS = {
    "national": "Mercado nacional",
    "international": "Mercado internacional",
}


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def normalize_broker_name(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_text.upper().split())


def resolve_trade_channel(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in CHANNEL_LABELS:
        return normalized
    return "app"


def infer_market_scope(quote_symbol: str) -> str:
    if str(quote_symbol or "").upper().endswith(".MC"):
        return "national"
    return "international"


def amount_fee(amount: Decimal, percent: Decimal, minimum: Decimal) -> Decimal:
    return quantize_money(max(amount * percent, minimum))


def santander_domestic_trade_fee(amount: Decimal, trade_channel: str) -> Decimal:
    if trade_channel in {"office", "contact_center"}:
        return amount_fee(amount, Decimal("0.0065"), Decimal("10.00"))
    if amount <= Decimal("2000.00"):
        return Decimal("3.00")
    if amount <= Decimal("15000.00"):
        return Decimal("6.00")
    return amount_fee(amount, Decimal("0.0025"), ZERO)


def santander_international_trade_fee(amount: Decimal, trade_channel: str) -> Decimal:
    if trade_channel in {"office", "contact_center"}:
        return amount_fee(amount, Decimal("0.0100"), Decimal("60.00"))
    if amount <= Decimal("15000.00"):
        return Decimal("20.00")
    return amount_fee(amount, Decimal("0.0035"), ZERO)


def estimate_santander_costs(
    trade_amount: Decimal,
    valuation_amount: Decimal,
    annual_dividend_income: Decimal,
    quote_symbol: str,
    trade_channel: str,
) -> dict:
    market_scope = infer_market_scope(quote_symbol)
    trade_channel = resolve_trade_channel(trade_channel)
    if market_scope == "national":
        entry_fee = santander_domestic_trade_fee(trade_amount, trade_channel)
        exit_fee = santander_domestic_trade_fee(trade_amount, trade_channel)
        annual_custody_cost = amount_fee(valuation_amount, Decimal("0.0025"), Decimal("20.00")) if valuation_amount > 0 else ZERO
        annual_dividend_fee = (
            amount_fee(annual_dividend_income, Decimal("0.0040"), Decimal("2.00"))
            if annual_dividend_income > 0
            else ZERO
        )
        transaction_tax_cost = quantize_money(trade_amount * Decimal("0.0020")) if trade_amount > 0 else ZERO
        notes = [
            "Tarifa Santander 2026 para mercado nacional.",
            "Se incluye ITF del 0,2% en compras nacionales cuando procede.",
        ]
    else:
        entry_fee = santander_international_trade_fee(trade_amount, trade_channel)
        exit_fee = santander_international_trade_fee(trade_amount, trade_channel)
        annual_custody_cost = amount_fee(valuation_amount, Decimal("0.0100"), Decimal("60.00")) if valuation_amount > 0 else ZERO
        annual_dividend_fee = (
            amount_fee(annual_dividend_income, Decimal("0.0120"), Decimal("30.00"))
            if annual_dividend_income > 0
            else ZERO
        )
        transaction_tax_cost = ZERO
        notes = [
            "Tarifa Santander 2026 para mercado internacional.",
            "El margen de cambio de divisa no se incluye porque el PDF no detalla una formula cerrada.",
        ]

    annual_recurring_cost = quantize_money(annual_custody_cost + annual_dividend_fee)
    purchase_total_cost = quantize_money(entry_fee + transaction_tax_cost)
    roundtrip_total_cost = quantize_money(entry_fee + exit_fee + transaction_tax_cost)
    return {
        "profile_key": "santander",
        "profile_label": "Banco Santander",
        "market_scope": market_scope,
        "market_scope_label": MARKET_SCOPE_LABELS[market_scope],
        "trade_channel": trade_channel,
        "trade_channel_label": CHANNEL_LABELS[trade_channel],
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "transaction_tax_cost": transaction_tax_cost,
        "purchase_total_cost": purchase_total_cost,
        "sale_total_cost": exit_fee,
        "roundtrip_total_cost": roundtrip_total_cost,
        "annual_custody_cost": annual_custody_cost,
        "annual_dividend_fee": annual_dividend_fee,
        "annual_recurring_cost": annual_recurring_cost,
        "notes": notes,
        "pdf_source_label": "Informe de Tarifas Santander 2026",
    }


def resolve_broker_cost_profile(broker_name: str) -> str | None:
    normalized = normalize_broker_name(broker_name)
    if "SANTANDER" in normalized:
        return "santander"
    return None


def estimate_broker_costs(
    broker_name: str,
    trade_channel: str,
    trade_amount: Decimal,
    valuation_amount: Decimal,
    annual_dividend_income: Decimal = ZERO,
    quote_symbol: str = "",
) -> dict:
    trade_amount = Decimal(trade_amount or 0)
    valuation_amount = Decimal(valuation_amount or 0)
    annual_dividend_income = Decimal(annual_dividend_income or 0)
    profile_key = resolve_broker_cost_profile(broker_name)
    if profile_key == "santander":
        return estimate_santander_costs(
            trade_amount=trade_amount,
            valuation_amount=valuation_amount,
            annual_dividend_income=annual_dividend_income,
            quote_symbol=quote_symbol,
            trade_channel=trade_channel,
        )
    fallback_costs = estimate_santander_costs(
        trade_amount=trade_amount,
        valuation_amount=valuation_amount,
        annual_dividend_income=annual_dividend_income,
        quote_symbol=quote_symbol,
        trade_channel=trade_channel,
    )
    market_scope = infer_market_scope(quote_symbol)
    trade_channel = resolve_trade_channel(trade_channel)
    return {
        **fallback_costs,
        "profile_key": "santander_fallback",
        "profile_label": str(broker_name or "").strip() or "Broker sin perfil",
        "notes": [
            f"Broker sin tarifa automatizada. Se usa Banco Santander como aproximacion conservadora para compra, venta, custodia y dividendos.",
            *fallback_costs.get("notes", []),
        ],
        "pdf_source_label": "Informe de Tarifas Santander 2026 (respaldo)",
    }


def resolve_recurring_cost_used(manual_annual_cost: Decimal, derived_recurring_cost: Decimal) -> tuple[Decimal, str]:
    manual_annual_cost = Decimal(manual_annual_cost or 0)
    derived_recurring_cost = Decimal(derived_recurring_cost or 0)
    if manual_annual_cost > 0:
        return quantize_money(manual_annual_cost), "manual"
    return quantize_money(derived_recurring_cost), "broker"
