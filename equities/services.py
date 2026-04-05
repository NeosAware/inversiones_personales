from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone as django_timezone

from banking.services import load_rows_from_workbook
from portfolio.ownership import AssetOwnershipCategory

from .models import EquityPosition, EquityPriceHistory


ZERO = Decimal("0.00")
DEFAULT_BENCHMARK_SYMBOL = "^IBEX"
DEFAULT_BENCHMARK_NAME = "IBEX 35"
DEFAULT_EQUITY_COLUMN_MAP = {
    "broker": 0,
    "ticker": 1,
    "company_name": 2,
    "shares": 3,
    "average_cost_per_share": 4,
    "current_price_per_share": 5,
}


class MarketDataError(Exception):
    pass


class EquityDocumentImportError(Exception):
    pass


@dataclass
class MarketSeries:
    symbol: str
    name: str
    latest_price: Decimal
    latest_date: date
    points: list[dict]


@dataclass
class EquityDocumentPrefill:
    data: dict
    detected_fields: list[str]
    candidate_count: int
    source_kind: str


def fetch_market_series(symbol: str, range_key: str = "1y", interval: str = "1d") -> MarketSeries:
    params = urlencode({"range": range_key, "interval": interval, "includePrePost": "false"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{params}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=20) as response:
        payload = json.load(response)

    error = payload.get("chart", {}).get("error")
    if error:
        raise MarketDataError(error.get("description", f"No se han podido cargar los datos de mercado de {symbol}."))

    result = payload["chart"]["result"][0]
    meta = result["meta"]
    timestamps = result.get("timestamp", [])
    closes = result["indicators"]["quote"][0].get("close", [])
    points = []

    for timestamp, close in zip(timestamps, closes):
        if close is None:
            continue
        points.append(
            {
                "date": datetime.fromtimestamp(timestamp, tz=timezone.utc).date(),
                "close": Decimal(str(round(close, 4))),
            }
        )

    if not points:
        raise MarketDataError(f"No se han recibido precios historicos para {symbol}.")

    latest_raw = meta.get("regularMarketPrice")
    latest_timestamp = meta.get("regularMarketTime") or timestamps[-1]
    latest_price = Decimal(str(round(latest_raw if latest_raw is not None else float(points[-1]["close"]), 4)))
    latest_date = datetime.fromtimestamp(latest_timestamp, tz=timezone.utc).date()

    return MarketSeries(
        symbol=symbol,
        name=meta.get("longName") or meta.get("shortName") or symbol,
        latest_price=latest_price,
        latest_date=latest_date,
        points=points,
    )


def normalize_document_text(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip().upper()


def parse_document_decimal(value) -> Decimal | None:
    text = str(value or "").strip().replace("\xa0", "").replace(" ", "")
    text = text.replace("EUR", "").replace("€", "").replace("%", "")
    if not text:
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    if text.startswith("-"):
        negative = True
        text = text[1:]
    if text.endswith("-"):
        negative = True
        text = text[:-1]

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            normalized = text.replace(".", "").replace(",", ".")
        else:
            normalized = text.replace(",", "")
    elif "," in text:
        normalized = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1:
        normalized = text.replace(".", "")
    else:
        normalized = text

    try:
        number = Decimal(normalized)
    except Exception:
        return None
    return -number if negative else number


def clean_company_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def clean_symbol(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().upper())


def clean_ticker(value: str) -> str:
    symbol = clean_symbol(value)
    if "." in symbol:
        symbol = symbol.split(".", 1)[0]
    return symbol


def infer_ownership_category_from_text(text: str, default_category: str) -> str:
    normalized = normalize_document_text(text)
    has_ximo = "XIMO" in normalized
    has_monica = "MONICA" in normalized
    if has_ximo and has_monica:
        return AssetOwnershipCategory.JOINT
    if has_ximo:
        return AssetOwnershipCategory.XIMO
    if has_monica:
        return AssetOwnershipCategory.MONICA
    return default_category


def label_matches(label: str, *tokens: str) -> bool:
    return any(token in label for token in tokens)


def build_equity_column_map(row: list[str]) -> dict[str, int]:
    column_map: dict[str, int] = {}
    for index, cell in enumerate(row):
        label = normalize_document_text(cell)
        if not label:
            continue
        if "broker" not in column_map and label_matches(label, "BROKER", "ENTIDAD", "CUSTODIO", "INTERMEDIARIO"):
            column_map["broker"] = index
        elif "ticker" not in column_map and label_matches(label, "TICKER", "SIMBOLO", "SYMBOL", "CODIGO VALOR"):
            column_map["ticker"] = index
        elif "quote_symbol" not in column_map and label_matches(label, "SIMBOLO DE MERCADO", "MARKET SYMBOL", "COTIZACION"):
            column_map["quote_symbol"] = index
        elif "company_name" not in column_map and label_matches(
            label,
            "EMPRESA",
            "COMPANIA",
            "COMPANY",
            "DESCRIPCION",
            "INSTRUMENTO",
            "VALOR",
            "NOMBRE",
        ):
            column_map["company_name"] = index
        elif "shares" not in column_map and label_matches(label, "ACCIONES", "TITULOS", "SHARES", "UNIDADES", "CANTIDAD"):
            column_map["shares"] = index
        elif "average_cost_per_share" not in column_map and label_matches(
            label,
            "COSTE MEDIO",
            "PRECIO MEDIO",
            "AVERAGE COST",
            "COST BASIS",
        ):
            column_map["average_cost_per_share"] = index
        elif "current_price_per_share" not in column_map and label_matches(
            label,
            "PRECIO ACTUAL",
            "ULTIMO PRECIO",
            "LAST PRICE",
            "MARKET PRICE",
            "COTIZACION",
        ):
            column_map["current_price_per_share"] = index
        elif "current_value" not in column_map and label_matches(
            label,
            "VALOR ACTUAL",
            "VALOR MERCADO",
            "MARKET VALUE",
            "VALORACION",
        ):
            column_map["current_value"] = index
        elif "invested_amount" not in column_map and label_matches(
            label,
            "IMPORTE INVERTIDO",
            "COSTE TOTAL",
            "TOTAL COSTE",
            "INVESTED AMOUNT",
        ):
            column_map["invested_amount"] = index
        elif "annual_dividend_income" not in column_map and label_matches(
            label,
            "DIVIDENDO ANUAL",
            "ANNUAL DIVIDEND",
            "DIVIDENDOS ANUALES",
        ):
            column_map["annual_dividend_income"] = index
    return column_map


def has_useful_equity_columns(column_map: dict[str, int]) -> bool:
    return (
        ("ticker" in column_map or "company_name" in column_map)
        and "shares" in column_map
        and (
            "average_cost_per_share" in column_map
            or "current_price_per_share" in column_map
            or "invested_amount" in column_map
            or "current_value" in column_map
        )
    )


def get_row_cell(row: list[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    return str(row[index]).strip()


def build_equity_data_from_table_row(
    row: list[str],
    column_map: dict[str, int],
    default_broker: str,
    default_ownership_category: str,
) -> dict:
    shares = parse_document_decimal(get_row_cell(row, column_map.get("shares")))
    if shares in {None, ZERO}:
        return {}

    invested_amount = parse_document_decimal(get_row_cell(row, column_map.get("invested_amount")))
    current_value = parse_document_decimal(get_row_cell(row, column_map.get("current_value")))
    average_cost = parse_document_decimal(get_row_cell(row, column_map.get("average_cost_per_share")))
    current_price = parse_document_decimal(get_row_cell(row, column_map.get("current_price_per_share")))

    if average_cost is None and invested_amount is not None and shares:
        average_cost = invested_amount / shares
    if current_price is None and current_value is not None and shares:
        current_price = current_value / shares
    if current_price is None and average_cost is not None:
        current_price = average_cost

    ticker = clean_ticker(get_row_cell(row, column_map.get("ticker")))
    quote_symbol = clean_symbol(get_row_cell(row, column_map.get("quote_symbol")))
    if not ticker and quote_symbol:
        ticker = clean_ticker(quote_symbol)

    company_name = clean_company_name(get_row_cell(row, column_map.get("company_name")))
    broker = clean_company_name(get_row_cell(row, column_map.get("broker"))) or default_broker
    annual_dividend_income = parse_document_decimal(get_row_cell(row, column_map.get("annual_dividend_income"))) or ZERO
    ownership_category = infer_ownership_category_from_text(
        " ".join(str(cell) for cell in row),
        default_ownership_category,
    )

    if not ticker and not company_name:
        return {}

    return {
        "ownership_category": ownership_category,
        "broker": broker,
        "ticker": ticker,
        "company_name": company_name,
        "quote_symbol": quote_symbol,
        "benchmark_symbol": DEFAULT_BENCHMARK_SYMBOL,
        "benchmark_name": DEFAULT_BENCHMARK_NAME,
        "shares": shares,
        "average_cost_per_share": average_cost,
        "current_price_per_share": current_price,
        "annual_dividend_income": annual_dividend_income,
        "notes": "",
    }


def assign_prefill_value(target: dict, label: str, value: str):
    if not value:
        return
    if label_matches(label, "BROKER", "ENTIDAD", "CUSTODIO", "INTERMEDIARIO"):
        target["broker"] = clean_company_name(value)
    elif label_matches(label, "TICKER", "SIMBOLO", "SYMBOL", "CODIGO VALOR"):
        target["ticker"] = clean_ticker(value)
    elif label_matches(label, "SIMBOLO DE MERCADO", "MARKET SYMBOL", "COTIZACION"):
        target["quote_symbol"] = clean_symbol(value)
    elif label_matches(label, "EMPRESA", "COMPANIA", "COMPANY", "DESCRIPCION", "INSTRUMENTO", "NOMBRE"):
        target["company_name"] = clean_company_name(value)
    elif label_matches(label, "ACCIONES", "TITULOS", "SHARES", "UNIDADES", "CANTIDAD"):
        number = parse_document_decimal(value)
        if number is not None:
            target["shares"] = number
    elif label_matches(label, "COSTE MEDIO", "PRECIO MEDIO", "AVERAGE COST", "COST BASIS"):
        number = parse_document_decimal(value)
        if number is not None:
            target["average_cost_per_share"] = number
    elif label_matches(label, "PRECIO ACTUAL", "ULTIMO PRECIO", "LAST PRICE", "MARKET PRICE", "COTIZACION"):
        number = parse_document_decimal(value)
        if number is not None:
            target["current_price_per_share"] = number
    elif label_matches(label, "VALOR ACTUAL", "VALOR MERCADO", "MARKET VALUE", "VALORACION"):
        number = parse_document_decimal(value)
        if number is not None:
            target["current_value"] = number
    elif label_matches(label, "IMPORTE INVERTIDO", "COSTE TOTAL", "TOTAL COSTE", "INVESTED AMOUNT"):
        number = parse_document_decimal(value)
        if number is not None:
            target["invested_amount"] = number
    elif label_matches(label, "DIVIDENDO ANUAL", "ANNUAL DIVIDEND", "DIVIDENDOS ANUALES"):
        number = parse_document_decimal(value)
        if number is not None:
            target["annual_dividend_income"] = number


def build_equity_data_from_key_value_rows(
    rows: list[list[str]],
    default_broker: str,
    default_ownership_category: str,
) -> dict:
    parsed: dict = {}
    for row in rows[:80]:
        non_empty = [str(cell).strip() for cell in row if str(cell).strip()]
        if len(non_empty) < 2:
            continue
        pairs = [(non_empty[0], non_empty[1])]
        if len(non_empty) >= 4:
            pairs.append((non_empty[2], non_empty[3]))
        for raw_label, raw_value in pairs:
            assign_prefill_value(parsed, normalize_document_text(raw_label), raw_value)

    if not parsed:
        return {}

    if "quote_symbol" in parsed and "ticker" not in parsed:
        parsed["ticker"] = clean_ticker(parsed["quote_symbol"])

    shares = parsed.get("shares")
    invested_amount = parsed.get("invested_amount")
    current_value = parsed.get("current_value")
    average_cost = parsed.get("average_cost_per_share")
    current_price = parsed.get("current_price_per_share")

    if average_cost is None and shares and invested_amount is not None:
        parsed["average_cost_per_share"] = invested_amount / shares
    if current_price is None and shares and current_value is not None:
        parsed["current_price_per_share"] = current_value / shares
    if parsed.get("current_price_per_share") is None and parsed.get("average_cost_per_share") is not None:
        parsed["current_price_per_share"] = parsed["average_cost_per_share"]

    parsed.setdefault("broker", default_broker)
    parsed.setdefault("ownership_category", default_ownership_category)
    parsed.setdefault("benchmark_symbol", DEFAULT_BENCHMARK_SYMBOL)
    parsed.setdefault("benchmark_name", DEFAULT_BENCHMARK_NAME)
    parsed.setdefault("annual_dividend_income", ZERO)
    parsed.setdefault("notes", "")

    if not parsed.get("ticker") and not parsed.get("company_name"):
        return {}
    if parsed.get("shares") in {None, ZERO}:
        return {}
    if parsed.get("average_cost_per_share") is None:
        return {}

    return parsed


def score_equity_data(data: dict) -> int:
    return sum(
        1
        for field_name in (
            "broker",
            "ticker",
            "company_name",
            "quote_symbol",
            "shares",
            "average_cost_per_share",
            "current_price_per_share",
            "annual_dividend_income",
        )
        if data.get(field_name) not in {None, "", ZERO}
    )


def finalize_equity_prefill(data: dict, default_ownership_category: str, document_text: str) -> dict:
    result = dict(data)
    result["ownership_category"] = infer_ownership_category_from_text(
        document_text,
        result.get("ownership_category") or default_ownership_category,
    )
    result.setdefault("benchmark_symbol", DEFAULT_BENCHMARK_SYMBOL)
    result.setdefault("benchmark_name", DEFAULT_BENCHMARK_NAME)
    result.setdefault("annual_dividend_income", ZERO)
    result.setdefault("notes", "")
    result.setdefault("broker", "")
    result.setdefault("quote_symbol", "")
    if result.get("quote_symbol") and not result.get("ticker"):
        result["ticker"] = clean_ticker(result["quote_symbol"])
    if result.get("current_price_per_share") is None and result.get("average_cost_per_share") is not None:
        result["current_price_per_share"] = result["average_cost_per_share"]
    return result


def extract_equity_prefill_from_rows(
    rows: list[list[str]],
    default_broker: str,
    default_ownership_category: str,
) -> tuple[dict, int]:
    document_text = " ".join(" ".join(str(cell) for cell in row) for row in rows)
    candidates = []

    key_value_data = build_equity_data_from_key_value_rows(rows, default_broker, default_ownership_category)
    if key_value_data:
        candidates.append(key_value_data)

    for index, row in enumerate(rows):
        column_map = build_equity_column_map(row)
        if not has_useful_equity_columns(column_map):
            continue
        table_candidates = []
        for data_row in rows[index + 1 :]:
            candidate = build_equity_data_from_table_row(
                data_row,
                column_map,
                default_broker,
                default_ownership_category,
            )
            if candidate:
                table_candidates.append(candidate)
        if table_candidates:
            candidates.extend(table_candidates)
            break

    if not candidates:
        for index, row in enumerate(rows):
            candidate = build_equity_data_from_table_row(
                row,
                DEFAULT_EQUITY_COLUMN_MAP,
                default_broker,
                default_ownership_category,
            )
            if candidate:
                candidates.append(candidate)
                for data_row in rows[index + 1 :]:
                    next_candidate = build_equity_data_from_table_row(
                        data_row,
                        DEFAULT_EQUITY_COLUMN_MAP,
                        default_broker,
                        default_ownership_category,
                    )
                    if next_candidate:
                        candidates.append(next_candidate)
                break

    if not candidates:
        return {}, 0

    best = max(candidates, key=score_equity_data)
    return finalize_equity_prefill(best, default_ownership_category, document_text), len(candidates)


def read_pdf_pages(file_source) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise EquityDocumentImportError("Se necesita pypdf para leer documentos PDF de acciones.") from exc

    if hasattr(file_source, "open"):
        file_source.open("rb")
    try:
        if hasattr(file_source, "seek"):
            file_source.seek(0)
        reader = PdfReader(file_source)
        return [(page.extract_text() or "") for page in reader.pages]
    finally:
        if hasattr(file_source, "seek"):
            file_source.seek(0)
        if hasattr(file_source, "close"):
            file_source.close()


def extract_equity_position_prefill(
    uploaded_file,
    default_broker: str = "",
    default_ownership_category: str = AssetOwnershipCategory.JOINT,
) -> EquityDocumentPrefill:
    source_name = str(getattr(uploaded_file, "name", "documento")).lower()
    suffix = Path(source_name).suffix.lower()

    if suffix in {".xls", ".xlsx"}:
        try:
            rows = load_rows_from_workbook(uploaded_file)
        except ValidationError as exc:
            raise EquityDocumentImportError(str(exc)) from exc
        data, candidate_count = extract_equity_prefill_from_rows(rows, default_broker, default_ownership_category)
        source_kind = "XLS"
    elif suffix == ".pdf":
        pages = read_pdf_pages(uploaded_file)
        rows = []
        for page in pages:
            for line in page.splitlines():
                cells = [segment.strip() for segment in re.split(r"\s{2,}|\t+", line) if segment.strip()]
                if cells:
                    rows.append(cells)
        data, candidate_count = extract_equity_prefill_from_rows(rows, default_broker, default_ownership_category)
        source_kind = "PDF"
    else:
        raise EquityDocumentImportError("Tipo de fichero no compatible. Sube un XLS, XLSX o PDF.")

    if not data:
        raise EquityDocumentImportError(
            "No se han reconocido suficientes datos de la posicion en el documento. Revisa el fichero o completa el formulario manualmente."
        )

    detected_fields = [
        field_name
        for field_name in (
            "ownership_category",
            "broker",
            "ticker",
            "company_name",
            "quote_symbol",
            "shares",
            "average_cost_per_share",
            "current_price_per_share",
            "annual_dividend_income",
        )
        if data.get(field_name) not in {None, ""}
    ]

    return EquityDocumentPrefill(
        data=data,
        detected_fields=detected_fields,
        candidate_count=candidate_count,
        source_kind=source_kind,
    )


def sync_equity_market_data(position: EquityPosition) -> EquityPosition:
    if not position.quote_symbol:
        raise MarketDataError(f"{position.ticker} no tiene configurado un simbolo de cotizacion.")

    position_series = fetch_market_series(position.quote_symbol)
    benchmark_series = fetch_market_series(position.benchmark_symbol) if position.benchmark_symbol else None
    benchmark_map = {point["date"]: point["close"] for point in (benchmark_series.points if benchmark_series else [])}
    point_dates = {point["date"] for point in position_series.points}

    with transaction.atomic():
        EquityPriceHistory.objects.filter(position=position).exclude(price_date__in=point_dates).delete()
        for point in position_series.points:
            EquityPriceHistory.objects.update_or_create(
                position=position,
                price_date=point["date"],
                defaults={
                    "close_price": point["close"],
                    "benchmark_close": benchmark_map.get(point["date"]),
                },
            )

        position.current_price_per_share = position_series.latest_price
        position.latest_price_date = position_series.latest_date
        position.last_synced_at = django_timezone.now()
        if benchmark_series and not position.benchmark_name:
            position.benchmark_name = benchmark_series.name
        position.save(
            update_fields=[
                "current_price_per_share",
                "latest_price_date",
                "last_synced_at",
                "benchmark_name",
            ]
        )

    return position


def sync_all_equities_market_data(positions) -> list[tuple[EquityPosition, str | None]]:
    results = []
    for position in positions:
        try:
            sync_equity_market_data(position)
            results.append((position, None))
        except Exception as exc:
            results.append((position, str(exc)))
    return results


def build_svg_polyline(values, width: int = 640, height: int = 220, padding: int = 18) -> str:
    filtered = [value for value in values if value is not None]
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
        x = padding + (span_x * index / total_points)
        normalized = (value - min_value) / (max_value - min_value)
        y = height - padding - (normalized * span_y)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def build_equity_history_cards(positions) -> list[dict]:
    cards = []
    for position in positions:
        history = list(position.price_history.order_by("price_date"))
        if not history:
            cards.append(
                {
                    "position": position,
                    "has_history": False,
                }
            )
            continue

        first_price = history[0].close_price
        first_benchmark = next((point.benchmark_close for point in history if point.benchmark_close is not None), None)
        stock_series = [float((point.close_price / first_price) * Decimal("100")) for point in history]
        benchmark_series = []
        if first_benchmark:
            for point in history:
                if point.benchmark_close is None:
                    benchmark_series.append(None)
                else:
                    benchmark_series.append(float((point.benchmark_close / first_benchmark) * Decimal("100")))
        else:
            benchmark_series = [None for _ in history]

        cards.append(
            {
                "position": position,
                "has_history": True,
                "points_count": len(history),
                "start_date": history[0].price_date,
                "end_date": history[-1].price_date,
                "stock_return_pct": ((history[-1].close_price / first_price) - 1) * Decimal("100") if first_price else ZERO,
                "benchmark_return_pct": (
                    ((history[-1].benchmark_close / first_benchmark) - 1) * Decimal("100")
                    if first_benchmark and history[-1].benchmark_close
                    else None
                ),
                "stock_line": build_svg_polyline(stock_series),
                "benchmark_line": build_svg_polyline(benchmark_series),
            }
        )
    return cards
