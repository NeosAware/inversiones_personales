from __future__ import annotations

from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from datetime import date, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
import html
import re
import threading
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

try:
    from xhtml2pdf import pisa
except Exception:  # pragma: no cover - dependencia opcional en runtime
    pisa = None

from .nightly_analysis import build_dashboard_from_nightly_cache
from .models import EquityOptimizationRun, EquityPosition
from .services import (
    OPTIMIZER_STRATEGY_12M_PRIMARY,
    OPTIMIZER_STRATEGY_5Y_PRIMARY,
    annualize_return_pct,
    build_equity_allocation_plan,
    build_equity_analysis_dashboard,
    build_equity_optimizer_candidate,
    build_ibex_universe_card,
    build_purchase_discipline_portfolio_context,
    clear_market_data_caches,
    find_ibex_universe_company,
    get_optimizer_strategy_config,
    apply_purchase_discipline_to_optimizer_candidates,
    projection_reliability_score,
    quantize_decimal,
    sync_all_equities_market_data,
)


ZERO = Decimal("0.00")
ONE_HUNDRED = Decimal("100.00")
SCHEDULED_OPTIMIZATION_MAX_COMPANY_PCT = Decimal("30.00")
SCHEDULED_OPTIMIZATION_MAX_TOTAL_POSITIONS = 5
SCHEDULED_OPTIMIZATION_MAX_SECTOR_POSITIONS = 2
SCHEDULED_OPTIMIZATION_RETENTION_MONTHS = 3
SCHEDULED_OPTIMIZATION_WEEKDAY_LABELS = {
    1: "lunes",
    2: "martes",
    3: "miercoles",
    4: "jueves",
    5: "viernes",
    6: "sabado",
    7: "domingo",
}
NEWS_LOOKBACK_DAYS = 45
NEWS_MAX_ITEMS = 6
NEWS_REQUEST_TIMEOUT_SECONDS = 10
RUN_STALE_MINUTES = 30
RUN_WORKER_MAX_WORKERS = 1
PROGRESS_STAGE_ORDER = [
    ("sync", "Sincronizando cartera"),
    ("dashboard", "Construyendo base de mercado"),
    ("ibex", "Analizando IBEX"),
    ("news", "Leyendo prensa reciente"),
    ("optimize", "Calculando cartera"),
    ("report", "Generando informe"),
]
POSITIVE_NEWS_TOKENS = {
    "sube": Decimal("1.0"),
    "subida": Decimal("1.0"),
    "mejora": Decimal("0.9"),
    "mejora": Decimal("0.9"),
    "beneficio": Decimal("1.1"),
    "beneficios": Decimal("1.1"),
    "crece": Decimal("0.9"),
    "crecimiento": Decimal("0.9"),
    "contrato": Decimal("1.0"),
    "contratos": Decimal("1.0"),
    "record": Decimal("1.0"),
    "maximos": Decimal("1.0"),
    "dividendo": Decimal("0.7"),
    "dividend": Decimal("0.7"),
    "alcista": Decimal("1.0"),
    "compra": Decimal("0.8"),
    "adjudica": Decimal("1.0"),
    "expansion": Decimal("0.8"),
    "upgrade": Decimal("1.0"),
    "beat": Decimal("0.8"),
}
NEGATIVE_NEWS_TOKENS = {
    "cae": Decimal("1.0"),
    "caida": Decimal("1.0"),
    "baja": Decimal("0.9"),
    "pierde": Decimal("0.9"),
    "riesgo": Decimal("0.7"),
    "deuda": Decimal("0.7"),
    "downgrade": Decimal("1.0"),
    "warning": Decimal("1.1"),
    "profit warning": Decimal("1.3"),
    "multa": Decimal("1.0"),
    "sancion": Decimal("1.0"),
    "demanda": Decimal("0.8"),
    "fraude": Decimal("1.4"),
    "investig": Decimal("1.1"),
    "recorte": Decimal("0.9"),
    "bajista": Decimal("1.0"),
    "deteriora": Decimal("0.9"),
    "suspende": Decimal("1.2"),
}

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_queued_run_ids: set[int] = set()
_queued_run_ids_lock = threading.Lock()


def optimization_async_enabled() -> bool:
    return bool(getattr(settings, "EQUITIES_OPTIMIZATION_ASYNC", True))


def scheduled_optimization_enabled() -> bool:
    return bool(getattr(settings, "EQUITIES_SCHEDULED_OPTIMIZATION_ENABLED", True))


def scheduled_optimization_iso_weekdays() -> tuple[int, ...]:
    return tuple(
        weekday
        for weekday in getattr(settings, "EQUITIES_SCHEDULED_OPTIMIZATION_ISO_WEEKDAYS", (2, 4))
        if 1 <= int(weekday) <= 7
    )


def build_scheduled_optimization_weekdays_label(weekdays: tuple[int, ...] | list[int]) -> str:
    labels = [
        SCHEDULED_OPTIMIZATION_WEEKDAY_LABELS.get(int(weekday), str(weekday))
        for weekday in weekdays
        if 1 <= int(weekday) <= 7
    ]
    if not labels:
        return "sin dias configurados"
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} y {labels[1]}"
    return f"{', '.join(labels[:-1])} y {labels[-1]}"


def resolve_next_scheduled_optimization_date(
    analysis_date: date,
    weekdays: tuple[int, ...],
    *,
    include_today: bool = True,
) -> date | None:
    if not weekdays:
        return None
    start_offset = 0 if include_today else 1
    for offset in range(start_offset, start_offset + 14):
        candidate = analysis_date + timedelta(days=offset)
        if candidate.isoweekday() in weekdays:
            return candidate
    return None


def should_launch_scheduled_optimizations(*, analysis_date: date | None = None, force: bool = False) -> bool:
    if not scheduled_optimization_enabled():
        return False
    if force:
        return True
    analysis_date = analysis_date or timezone.localdate()
    weekdays = scheduled_optimization_iso_weekdays()
    if not weekdays:
        return False
    return analysis_date.isoweekday() in weekdays


def build_scheduled_optimization_run_key(analysis_date: date) -> str:
    return f"scheduled-optimization:{analysis_date.isoformat()}"


def shift_date_by_months(value: date, months: int) -> date:
    total_month = (value.year * 12) + (value.month - 1) + months
    year = total_month // 12
    month = (total_month % 12) + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def scheduled_optimization_retention_cutoff(as_of: date | None = None) -> date:
    as_of = as_of or timezone.localdate()
    return shift_date_by_months(as_of, -SCHEDULED_OPTIMIZATION_RETENTION_MONTHS)


def resolve_scheduled_run_date(run: EquityOptimizationRun) -> date:
    progress_data = dict(run.progress_data or {})
    summary_data = dict(run.summary_data or {})
    run_date_label = progress_data.get("scheduled_analysis_date") or summary_data.get("scheduled_analysis_date") or ""
    try:
        return date.fromisoformat(run_date_label) if run_date_label else timezone.localtime(run.created_at).date()
    except ValueError:
        return timezone.localtime(run.created_at).date()


def scheduled_optimization_matches_policy(run: EquityOptimizationRun) -> bool:
    return (
        Decimal(str(run.max_company_pct or "0")) == SCHEDULED_OPTIMIZATION_MAX_COMPANY_PCT
        and int(run.max_total_positions or 0) == SCHEDULED_OPTIMIZATION_MAX_TOTAL_POSITIONS
        and int(run.max_sector_positions or 0) == SCHEDULED_OPTIMIZATION_MAX_SECTOR_POSITIONS
        and not list(run.selected_sectors or [])
        and not bool(run.selected_owned_tickers_applied)
        and not list(run.selected_owned_tickers or [])
    )


def purge_stale_scheduled_optimization_runs(*, as_of: date | None = None) -> int:
    cutoff = scheduled_optimization_retention_cutoff(as_of)
    stale_ids = []
    for run in EquityOptimizationRun.objects.filter(progress_data__schedule_kind="nightly").only(
        "id",
        "created_at",
        "max_company_pct",
        "max_total_positions",
        "max_sector_positions",
        "selected_sectors",
        "selected_owned_tickers_applied",
        "selected_owned_tickers",
        "progress_data",
        "summary_data",
    ):
        if resolve_scheduled_run_date(run) < cutoff or not scheduled_optimization_matches_policy(run):
            stale_ids.append(run.id)
    if stale_ids:
        deleted_count, _ = EquityOptimizationRun.objects.filter(id__in=stale_ids).delete()
        return deleted_count
    return 0


def load_existing_scheduled_optimization_runs(analysis_date: date) -> list[EquityOptimizationRun]:
    return list(
        EquityOptimizationRun.objects.filter(
            progress_data__scheduled_run_key=build_scheduled_optimization_run_key(analysis_date),
        ).order_by("created_at", "id")
    )


def resolve_scheduled_optimization_total_investment() -> Decimal:
    positions = list(EquityPosition.objects.all())
    current_value = sum((position.current_value for position in positions if position.is_owned), ZERO)
    if current_value > ZERO:
        return current_value.quantize(Decimal("0.01"))
    invested_amount = sum((position.invested_amount for position in positions if position.is_owned), ZERO)
    if invested_amount > ZERO:
        return invested_amount.quantize(Decimal("0.01"))
    return Decimal("100000.00")


def build_scheduled_optimization_note(analysis_date: date, weekdays_label: str) -> str:
    note = (
        f"Optimizacion programada automaticamente tras el analisis nocturno del {analysis_date.isoformat()}."
    )
    if weekdays_label != "sin dias configurados":
        note += f" Se refresca en {weekdays_label}."
    note += (
        f" Limites fijos: maximo {SCHEDULED_OPTIMIZATION_MAX_TOTAL_POSITIONS} empresas, "
        f"{SCHEDULED_OPTIMIZATION_MAX_SECTOR_POSITIONS} por sector y "
        f"{SCHEDULED_OPTIMIZATION_MAX_COMPANY_PCT.quantize(Decimal('0'))} % por empresa."
    )
    return note


def news_cache_bucket(now: datetime | None = None) -> int:
    current = now or timezone.now()
    return int(current.timestamp() // (12 * 60 * 60))


def get_optimizer_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=RUN_WORKER_MAX_WORKERS,
                thread_name_prefix="equity-optimizer",
            )
        return _executor


def normalize_news_text(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def clamp_decimal(value: Decimal, minimum: Decimal, maximum: Decimal) -> Decimal:
    return max(minimum, min(value, maximum))


def normalize_price_value(value) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        return quantize_decimal(Decimal(str(value)), "0.0001")
    except Exception:
        return None


def normalize_date_value(value) -> date | None:
    if isinstance(value, date):
        return value
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        return None


def compute_trade_percent_delta(target_value, reference_value) -> Decimal | None:
    target = normalize_price_value(target_value)
    reference = normalize_price_value(reference_value)
    if target in {None, ZERO} or reference in {None, ZERO}:
        return None
    return quantize_decimal(((target - reference) / reference) * ONE_HUNDRED, "0.01")


def build_optimizer_live_quote_map(optimizer_cards: list[dict] | None) -> dict[str, dict]:
    live_quote_map: dict[str, dict] = {}
    for card in optimizer_cards or []:
        position = card.get("position") if isinstance(card, dict) else None
        if position is None:
            continue
        ticker = str(getattr(position, "ticker", "") or "").strip().upper()
        if not ticker:
            continue
        current_price = normalize_price_value(getattr(position, "current_price_per_share", None))
        current_price_date = getattr(position, "latest_price_date", None)
        existing = live_quote_map.get(ticker)
        if existing is not None:
            existing_date = existing.get("current_price_date")
            if current_price_date and existing_date and current_price_date < existing_date:
                continue
            if current_price_date == existing_date and existing.get("current_price") not in {None, ZERO} and current_price in {None, ZERO}:
                continue
        live_quote_map[ticker] = {
            "ticker": ticker,
            "company_name": str(getattr(position, "company_name", "") or ticker),
            "quote_symbol": str(getattr(position, "quote_symbol", "") or ""),
            "current_price": current_price,
            "current_price_date": current_price_date,
            "current_price_date_label": current_price_date.isoformat() if current_price_date else "",
        }
    return live_quote_map


def attach_trade_progress_metrics(payload: dict, *, live_quote_map: dict[str, dict] | None = None) -> dict:
    data = dict(payload or {})
    ticker = str(data.get("ticker") or "").strip().upper()
    live_quote = dict((live_quote_map or {}).get(ticker) or {})

    current_price = live_quote.get("current_price")
    if current_price in {None, ZERO}:
        current_price = (
            normalize_price_value(data.get("current_price"))
            or normalize_price_value(data.get("current_price_per_share"))
            or normalize_price_value(data.get("latest_current_price"))
        )
    current_price_date = live_quote.get("current_price_date") or normalize_date_value(
        data.get("current_price_date") or data.get("current_price_date_label")
    )
    current_price_date_label = (
        live_quote.get("current_price_date_label")
        or data.get("current_price_date_label")
        or (current_price_date.isoformat() if current_price_date else "")
    )

    entry_price = (
        normalize_price_value(data.get("entry_price"))
        or normalize_price_value(data.get("buy_price"))
        or normalize_price_value(data.get("latest_buy_price"))
        or normalize_price_value(data.get("average_buy_price"))
    )
    exit_price = (
        normalize_price_value(data.get("exit_price"))
        or normalize_price_value(data.get("sell_price"))
        or normalize_price_value(data.get("latest_sell_price"))
        or normalize_price_value(data.get("average_sell_price"))
    )

    current_vs_entry_pct = compute_trade_percent_delta(current_price, entry_price)
    current_vs_exit_pct = compute_trade_percent_delta(current_price, exit_price)
    remaining_to_exit_pct = compute_trade_percent_delta(exit_price, current_price)

    current_position_label = "Sin precio actual"
    current_position_tone = ""
    if current_price not in {None, ZERO}:
        if entry_price in {None, ZERO} and exit_price in {None, ZERO}:
            current_position_label = "Sin referencias de tramo"
        elif entry_price not in {None, ZERO} and current_vs_entry_pct is not None and current_vs_entry_pct < ZERO:
            current_position_label = "Debajo de la entrada sugerida"
            current_position_tone = "good"
        elif exit_price not in {None, ZERO} and current_vs_exit_pct is not None and current_vs_exit_pct >= ZERO:
            current_position_label = "Ya en zona de salida objetivo"
            current_position_tone = "warn"
        elif entry_price not in {None, ZERO} and exit_price not in {None, ZERO}:
            current_position_label = "Dentro del tramo esperado"
            current_position_tone = "neutral"
        elif entry_price not in {None, ZERO}:
            current_position_label = "Comparado con la entrada sugerida"
            current_position_tone = "neutral"
        elif exit_price not in {None, ZERO}:
            current_position_label = "Comparado con la salida objetivo"
            current_position_tone = "neutral"

    data.update(
        {
            "ticker": ticker or data.get("ticker", ""),
            "current_price": current_price,
            "current_price_per_share": current_price,
            "current_price_date": current_price_date,
            "current_price_date_label": current_price_date_label,
            "entry_price": entry_price or data.get("entry_price"),
            "buy_price": entry_price or data.get("buy_price"),
            "exit_price": exit_price or data.get("exit_price"),
            "sell_price": exit_price or data.get("sell_price"),
            "current_vs_entry_pct": current_vs_entry_pct,
            "current_vs_exit_pct": current_vs_exit_pct,
            "remaining_to_exit_pct": remaining_to_exit_pct,
            "current_position_label": current_position_label,
            "current_position_tone": current_position_tone,
        }
    )
    return data


def strip_html_tags(value: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(html.unescape(cleaned).split())


def google_news_search_url(company_name: str, ticker: str, sector_label: str = "") -> str:
    query_parts = [f'"{company_name}"']
    if ticker:
        query_parts.append(f'"{ticker}"')
    query_parts.append("(IBEX OR bolsa OR acciones)")
    if sector_label:
        query_parts.append(f'"{sector_label}"')
    query_parts.append(f"when:{NEWS_LOOKBACK_DAYS}d")
    query = " ".join(part for part in query_parts if part)
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=es-419&gl=ES&ceid=ES:es-419"


def _fetch_google_news_xml(url: str, bucket: int) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=NEWS_REQUEST_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="ignore")


def parse_news_date(value: str) -> datetime | None:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    try:
        parsed = parsedate_to_datetime(raw_value)
    except Exception:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed.astimezone(timezone.get_current_timezone())


def news_item_score(title: str, description: str, published_at: datetime | None) -> Decimal:
    text = normalize_news_text(f"{title} {description}")
    score = ZERO
    for token, weight in POSITIVE_NEWS_TOKENS.items():
        if token in text:
            score += weight
    for token, weight in NEGATIVE_NEWS_TOKENS.items():
        if token in text:
            score -= weight
    if published_at is not None:
        age_days = max((timezone.now() - published_at).days, 0)
        if age_days <= 3:
            score *= Decimal("1.20")
        elif age_days <= 10:
            score *= Decimal("1.00")
        elif age_days <= 20:
            score *= Decimal("0.85")
        else:
            score *= Decimal("0.70")
    return score


def summarize_news_signal(items: list[dict]) -> dict:
    if not items:
        return {
            "available": False,
            "label": "Sin prensa reciente",
            "score": ZERO,
            "items_count": 0,
            "positive_count": 0,
            "negative_count": 0,
            "neutral_count": 0,
            "items": [],
            "note": "No se han encontrado titulares recientes suficientes para modular la optimizacion.",
        }

    score_total = sum((item["score"] for item in items), ZERO)
    average_score = score_total / Decimal(len(items))
    signal_score = clamp_decimal(average_score * Decimal("2.4"), Decimal("-10.00"), Decimal("10.00"))
    positive_count = sum(1 for item in items if item["score"] > ZERO)
    negative_count = sum(1 for item in items if item["score"] < ZERO)
    neutral_count = len(items) - positive_count - negative_count

    if signal_score >= Decimal("2.00"):
        label = "Prensa favorable"
        note = "Los titulares recientes apoyan el escenario de continuidad o mejora."
    elif signal_score <= Decimal("-2.00"):
        label = "Prensa adversa"
        note = "Los titulares recientes introducen ruido o riesgo adicional sobre la compania."
    else:
        label = "Prensa neutra"
        note = "La prensa reciente no cambia mucho la lectura base de mercado y fundamentales."

    return {
        "available": True,
        "label": label,
        "score": signal_score.quantize(Decimal("0.01")),
        "items_count": len(items),
        "positive_count": positive_count,
        "negative_count": negative_count,
        "neutral_count": neutral_count,
        "items": items,
        "note": note,
    }


def fetch_company_news_signal(company_name: str, ticker: str, sector_label: str = "") -> dict:
    url = google_news_search_url(company_name, ticker, sector_label)
    xml_text = _fetch_google_news_xml(url, news_cache_bucket())
    root = ET.fromstring(xml_text)
    items = []
    seen_titles = set()
    for item in root.findall(".//item"):
        title = strip_html_tags(item.findtext("title", ""))
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        description = strip_html_tags(item.findtext("description", ""))
        link = (item.findtext("link", "") or "").strip()
        published_at = parse_news_date(item.findtext("pubDate", ""))
        score = news_item_score(title, description, published_at)
        source = ""
        if " - " in title:
            source = title.rsplit(" - ", 1)[-1].strip()
        items.append(
            {
                "title": title,
                "description": description,
                "link": link,
                "source": source,
                "published_at": published_at,
                "published_label": published_at.strftime("%Y-%m-%d") if published_at else "",
                "score": score.quantize(Decimal("0.01")),
                "tone": "positive" if score > ZERO else "negative" if score < ZERO else "neutral",
            }
        )
        if len(items) >= NEWS_MAX_ITEMS:
            break
    return summarize_news_signal(items)


def build_news_signal_map(cards: list[dict], progress_callback=None) -> dict[str, dict]:
    cards_to_analyze = []
    seen_tickers = set()
    for card in cards:
        position = card["position"]
        ticker = str(position.ticker or "").strip().upper()
        if not ticker or ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        cards_to_analyze.append(card)

    results: dict[str, dict] = {}
    if not cards_to_analyze:
        return results

    max_workers = min(6, len(cards_to_analyze))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="equity-news") as executor:
        future_map = {
            executor.submit(
                fetch_company_news_signal,
                card["position"].company_name,
                card["position"].ticker,
                card.get("sector_label", ""),
            ): card
            for card in cards_to_analyze
        }
        completed_count = 0
        for future in as_completed(future_map):
            card = future_map[future]
            ticker = card["position"].ticker
            try:
                results[ticker] = future.result()
            except Exception as exc:
                results[ticker] = {
                    "available": False,
                    "label": "Sin prensa reciente",
                    "score": ZERO,
                    "items_count": 0,
                    "positive_count": 0,
                    "negative_count": 0,
                    "neutral_count": 0,
                    "items": [],
                    "note": f"No se ha podido leer la prensa reciente: {exc}",
                }
            completed_count += 1
            if progress_callback:
                progress_callback(
                    completed_count=completed_count,
                    total_count=len(cards_to_analyze),
                    card=card,
                    signal=results[ticker],
                )
    return results


def build_svg_bar_chart(items: list[dict], value_key: str, width: int = 760, height: int = 260) -> str:
    if not items:
        return ""
    rows = items[:10]
    padding_left = 150
    padding_right = 28
    padding_top = 18
    row_height = 22
    chart_height = max(len(rows) * row_height + 40, 120)
    height = max(height, chart_height)
    bar_area_width = width - padding_left - padding_right
    max_value = max((Decimal(str(item.get(value_key) or 0)) for item in rows), default=Decimal("1"))
    if max_value <= ZERO:
        max_value = Decimal("1")

    fragments = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="22" fill="#f8fbfd" />',
    ]
    for index, item in enumerate(rows):
        label = html.escape(str(item.get("label") or ""))
        value = Decimal(str(item.get(value_key) or 0))
        bar_width = float((value / max_value) * Decimal(str(bar_area_width)))
        y = padding_top + (index * row_height)
        fragments.append(f'<text x="18" y="{y + 14}" font-size="12" fill="#17384e">{label}</text>')
        fragments.append(
            f'<rect x="{padding_left}" y="{y}" width="{bar_area_width}" height="14" rx="7" fill="#e6eef4" />'
        )
        fragments.append(
            f'<rect x="{padding_left}" y="{y}" width="{bar_width:.1f}" height="14" rx="7" fill="#0f5f88" />'
        )
        fragments.append(
            f'<text x="{padding_left + bar_area_width + 8}" y="{y + 12}" font-size="11" fill="#486072">{value:.2f}</text>'
        )
    fragments.append("</svg>")
    return "".join(fragments)


def build_svg_scatter_chart(allocations: list[dict], width: int = 760, height: int = 280) -> str:
    if not allocations:
        return ""
    points = [item for item in allocations if item.get("annualized_volatility_pct") is not None]
    if not points:
        return ""

    padding = 36
    plot_width = width - (padding * 2)
    plot_height = height - (padding * 2)
    x_values = [Decimal(str(item["annualized_volatility_pct"])) for item in points]
    y_values = [Decimal(str(item["net_projected_return_pct"])) for item in points]
    x_min = min(x_values)
    x_max = max(x_values)
    y_min = min(y_values)
    y_max = max(y_values)
    if x_min == x_max:
        x_max += Decimal("1")
    if y_min == y_max:
        y_max += Decimal("1")

    def to_x(value: Decimal) -> float:
        return float(padding + (((value - x_min) / (x_max - x_min)) * Decimal(str(plot_width))))

    def to_y(value: Decimal) -> float:
        return float(height - padding - (((value - y_min) / (y_max - y_min)) * Decimal(str(plot_height))))

    fragments = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="22" fill="#f8fbfd" />',
        f'<line x1="{padding}" y1="{height - padding}" x2="{width - padding}" y2="{height - padding}" stroke="#d7e4ee" stroke-width="1" />',
        f'<line x1="{padding}" y1="{padding}" x2="{padding}" y2="{height - padding}" stroke="#d7e4ee" stroke-width="1" />',
        f'<text x="{width / 2}" y="{height - 8}" font-size="11" text-anchor="middle" fill="#627380">Volatilidad anualizada (%)</text>',
        f'<text x="12" y="{height / 2}" font-size="11" text-anchor="middle" fill="#627380" transform="rotate(-90 12 {height / 2})">Retorno neto 12M (%)</text>',
    ]
    for item in points:
        x = to_x(Decimal(str(item["annualized_volatility_pct"])))
        y = to_y(Decimal(str(item["net_projected_return_pct"])))
        radius = max(float(item["allocated_weight_pct"]) * 0.18, 6.0)
        tone = "#177245" if item["net_projected_return_pct"] >= 0 else "#b26400"
        label = html.escape(str(item["position"].ticker))
        fragments.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" fill="{tone}" opacity="0.78" />')
        fragments.append(f'<text x="{x:.1f}" y="{y - radius - 4:.1f}" font-size="10" text-anchor="middle" fill="#17384e">{label}</text>')
    fragments.append("</svg>")
    return "".join(fragments)


def generate_optimization_reference_family_code() -> str:
    prefix = timezone.localtime().strftime("OPT-%Y%m%d-%H%M%S")
    family_codes = set()
    for code in EquityOptimizationRun.objects.filter(reference_code__startswith=prefix).values_list("reference_code", flat=True):
        code_text = str(code or "")
        if code_text.endswith("-12M") or code_text.endswith("-5A"):
            family_codes.add(code_text.rsplit("-", 1)[0])
        else:
            family_codes.add(code_text)
    similar_count = len(family_codes) + 1
    return f"{prefix}-{similar_count:02d}"


def generate_optimization_reference_code(base_code: str | None = None, suffix: str = "") -> str:
    base = base_code or generate_optimization_reference_family_code()
    normalized_suffix = str(suffix or "").strip().upper()
    return f"{base}-{normalized_suffix}" if normalized_suffix else base


def build_optimizer_run_label(reference_label: str, strategy_mode: str) -> str:
    strategy = get_optimizer_strategy_config(strategy_mode)
    base_label = str(reference_label or "").strip() or "Optimizacion robusta"
    return f"{base_label} - {strategy['label']}"


def build_optimizer_progress_payload(strategy_mode: str, note: str, percent: int = 0) -> dict:
    strategy = get_optimizer_strategy_config(strategy_mode)
    return {
        "strategy_mode": strategy["mode"],
        "strategy_label": strategy["label"],
        "percent": percent,
        "stage_key": "sync",
        "stage_label": dict(PROGRESS_STAGE_ORDER)["sync"],
        "note": note,
        "current_step": None,
        "total_steps": None,
        "current_label": "",
        "stages": build_progress_stages(None if percent == 0 else "sync"),
        "preview_candidates": [],
        "preview_allocations": [],
        "events": [],
        "updated_at_label": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
    }


def build_news_overview(signals: dict[str, dict]) -> dict:
    entries = list(signals.values())
    positive_count = sum(1 for item in entries if Decimal(str(item.get("score", 0) or 0)) > ZERO)
    negative_count = sum(1 for item in entries if Decimal(str(item.get("score", 0) or 0)) < ZERO)
    neutral_count = len(entries) - positive_count - negative_count
    items_count = sum(int(item.get("items_count", 0) or 0) for item in entries)
    return {
        "signals_count": len(entries),
        "positive_count": positive_count,
        "negative_count": negative_count,
        "neutral_count": neutral_count,
        "items_count": items_count,
    }


def build_progress_stages(active_key: str | None, *, finalized: bool = False) -> list[dict]:
    active_index = next((index for index, stage in enumerate(PROGRESS_STAGE_ORDER) if stage[0] == active_key), -1)
    stages = []
    for index, (stage_key, stage_label) in enumerate(PROGRESS_STAGE_ORDER):
        if active_index == -1:
            status = "pending"
        elif finalized and index <= active_index:
            status = "completed"
        elif index < active_index:
            status = "completed"
        elif index == active_index:
            status = "active"
        else:
            status = "pending"
        stages.append(
            {
                "key": stage_key,
                "label": stage_label,
                "status": status,
            }
        )
    return stages


def serialize_candidate_preview(card: dict) -> dict:
    position = card["position"]
    projection = card.get("projection") or {}
    return {
        "ticker": position.ticker,
        "company_name": position.company_name,
        "sector_label": card.get("sector_label", ""),
        "status_label": card.get("status_label", ""),
        "reference_label": card.get("reference_label", ""),
        "trade_alert_label": (card.get("trade_alert") or {}).get("label", "Vigilar"),
        "base_return_pct": float(projection.get("base_return_pct", 0) or 0) if projection.get("base_return_pct") is not None else None,
        "safety_score": float(projection.get("safety_score", 0) or 0) if projection.get("safety_score") is not None else None,
    }


def build_optimizer_candidate_preview(
    cards: list[dict],
    limit: int = 5,
    strategy_mode: str = OPTIMIZER_STRATEGY_12M_PRIMARY,
) -> list[dict]:
    candidates = apply_purchase_discipline_to_optimizer_candidates(
        [
            candidate
            for candidate in (build_equity_optimizer_candidate(card, strategy_mode) for card in cards)
            if candidate and candidate.get("optimization_score") is not None
        ],
        build_purchase_discipline_portfolio_context(cards),
    )
    ranked = sorted(
        candidates,
        key=lambda item: (
            item["optimization_score"],
            item["primary_signal_pct"],
            item["secondary_signal_pct"],
        ),
        reverse=True,
    )[:limit]
    preview = []
    for item in ranked:
        position = item["position"]
        preview.append(
            {
                "ticker": position.ticker,
                "company_name": position.company_name,
                "sector_label": item["sector_label"],
                "status_label": item["status_label"],
                "trade_alert_label": item["trade_alert_label"],
                "reference_label": item["reference_label"],
                "optimization_score": float(item["optimization_score"]),
                "net_return_pct": float(item["base_return_pct"]),
                "safety_score": float(item["safety_score"]),
                "external_signal_label": item.get("external_signal_label", ""),
                "purchase_discipline_label": item.get("purchase_discipline_label", ""),
                "purchase_discipline_score": float(item["purchase_discipline_score"]) if item.get("purchase_discipline_score") is not None else None,
                "purchase_discipline_reason": item.get("purchase_discipline_reason", ""),
            }
        )
    return preview


def build_allocation_preview(plan: dict, limit: int = 5) -> list[dict]:
    preview = []
    for item in plan.get("allocations", [])[:limit]:
        preview.append(
            {
                "ticker": item["position"].ticker,
                "company_name": item["position"].company_name,
                "sector_label": item["sector_label"],
                "status_label": item["status_label"],
                "weight_pct": float(item["allocated_weight_pct"]),
                "amount": float(item["allocated_amount"]),
                "net_return_pct": float(item["net_projected_return_pct"]),
                "trade_alert_label": item["trade_alert_label"],
                "external_signal_label": item.get("external_signal_label", ""),
            }
        )
    return preview


def update_run_progress(
    run_id: int,
    *,
    percent: int,
    stage_key: str,
    note: str,
    current_step: int | None = None,
    total_steps: int | None = None,
    current_label: str = "",
    preview_candidates: list[dict] | None = None,
    preview_allocations: list[dict] | None = None,
) -> None:
    run = EquityOptimizationRun.objects.get(pk=run_id)
    progress_data = dict(run.progress_data or {})
    preserved_metadata = {
        key: value
        for key, value in progress_data.items()
        if key in {"strategy_mode", "strategy_label", "schedule_kind", "scheduled_run_key", "scheduled_analysis_date", "scheduled_weekdays_label"}
    }
    previous_stage = progress_data.get("stage_key")
    previous_note = progress_data.get("note")
    events = list(progress_data.get("events") or [])
    if previous_stage != stage_key or previous_note != note or current_label:
        events.append(
            {
                "label": note,
                "detail": current_label,
                "stage_key": stage_key,
                "recorded_at": timezone.localtime().strftime("%H:%M:%S"),
            }
        )
    run.progress_data = {
        "percent": max(min(int(percent), 100), 0),
        "stage_key": stage_key,
        "stage_label": dict(PROGRESS_STAGE_ORDER).get(stage_key, stage_key),
        "note": note,
        "current_step": current_step,
        "total_steps": total_steps,
        "current_label": current_label,
        "stages": build_progress_stages(stage_key),
        "preview_candidates": preview_candidates or progress_data.get("preview_candidates", []),
        "preview_allocations": preview_allocations or progress_data.get("preview_allocations", []),
        "events": events[-8:],
        "updated_at_label": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
        **preserved_metadata,
    }
    run.status_note = note
    run.save(update_fields=["progress_data", "status_note", "updated_at"])


def serialize_summary_data(run: EquityOptimizationRun, plan: dict, dashboard: dict, news_overview: dict) -> dict:
    completed_at = timezone.localtime(run.completed_at) if run.completed_at else None
    created_at = timezone.localtime(run.created_at)
    top_pick = plan.get("top_pick")
    top_pick_name = top_pick["position"].company_name if top_pick else ""
    top_pick_purchase_timing = dict(plan.get("top_pick_purchase_timing") or {})
    progress_data = dict(run.progress_data or {})
    return {
        "reference_code": run.reference_code,
        "label": run.display_label,
        "status": run.status,
        "status_note": run.status_note,
        "created_at_label": created_at.strftime("%Y-%m-%d %H:%M"),
        "completed_at_label": completed_at.strftime("%Y-%m-%d %H:%M") if completed_at else "",
        "strategy_mode": plan.get("strategy_mode") or (run.progress_data or {}).get("strategy_mode"),
        "strategy_label": plan.get("strategy_label") or (run.progress_data or {}).get("strategy_label", ""),
        "primary_horizon_label": plan.get("primary_horizon_label", ""),
        "secondary_horizon_label": plan.get("secondary_horizon_label", ""),
        "available": bool(plan.get("available")),
        "reason": plan.get("reason", ""),
        "total_investment": float(plan.get("total_investment", 0) or 0),
        "max_total_positions": int(plan.get("max_total_positions", 0) or 0),
        "selected_sectors": list(plan.get("selected_sectors") or []),
        "selected_owned_tickers": list(plan.get("selected_owned_tickers") or []),
        "selected_owned_tickers_applied": bool(plan.get("selected_owned_tickers_applied")),
        "owned_positions_available_count": int(plan.get("owned_positions_available_count", 0) or 0),
        "projected_gain_total": float(plan.get("projected_gain_total", 0) or 0),
        "weighted_return_pct": float(plan.get("weighted_return_pct", 0) or 0) if plan.get("weighted_return_pct") is not None else None,
        "weighted_low_return_pct": float(plan.get("weighted_low_return_pct", 0) or 0) if plan.get("weighted_low_return_pct") is not None else None,
        "weighted_expected_return_pct": float(plan.get("weighted_expected_return_pct", 0) or 0) if plan.get("weighted_expected_return_pct") is not None else None,
        "weighted_stress_return_pct": float(plan.get("weighted_stress_return_pct", 0) or 0) if plan.get("weighted_stress_return_pct") is not None else None,
        "weighted_safety_score": float(plan.get("weighted_safety_score", 0) or 0) if plan.get("weighted_safety_score") is not None else None,
        "weighted_reliability_score": float(plan.get("weighted_reliability_score", 0) or 0) if plan.get("weighted_reliability_score") is not None else None,
        "weighted_purchase_discipline_score": float(plan.get("weighted_purchase_discipline_score", 0) or 0) if plan.get("weighted_purchase_discipline_score") is not None else None,
        "purchase_discipline_rows": [
            {
                **row,
                "score": float(row["score"]) if row.get("score") is not None else None,
                "holding_annualized_return_pct": float(row["holding_annualized_return_pct"]) if row.get("holding_annualized_return_pct") is not None else None,
            }
            for row in plan.get("purchase_discipline_rows", [])
        ],
        "weighted_cycle_return_annual_pct": float(plan.get("weighted_cycle_return_annual_pct", 0) or 0) if plan.get("weighted_cycle_return_annual_pct") is not None else None,
        "weighted_cycle_return_5y_pct": float(plan.get("weighted_cycle_return_5y_pct", 0) or 0) if plan.get("weighted_cycle_return_5y_pct") is not None else None,
        "target_holding_annualized_return_pct": float(plan.get("target_holding_annualized_return_pct", 0) or 0) if plan.get("target_holding_annualized_return_pct") is not None else None,
        "weighted_holding_annualized_return_pct": float(plan.get("weighted_holding_annualized_return_pct", 0) or 0) if plan.get("weighted_holding_annualized_return_pct") is not None else None,
        "weighted_holding_annualized_target_gap_pct": float(plan.get("weighted_holding_annualized_target_gap_pct", 0) or 0) if plan.get("weighted_holding_annualized_target_gap_pct") is not None else None,
        "allocations_with_timing_count": int(plan.get("allocations_with_timing_count", 0) or 0),
        "allocations_meeting_target_count": int(plan.get("allocations_meeting_target_count", 0) or 0),
        "weighted_target_compliance_pct": float(plan.get("weighted_target_compliance_pct", 0) or 0) if plan.get("weighted_target_compliance_pct") is not None else None,
        "weighted_conservative_profile_compliance_pct": float(plan.get("weighted_conservative_profile_compliance_pct", 0) or 0) if plan.get("weighted_conservative_profile_compliance_pct") is not None else None,
        "annualized_target_filtered_count": int(plan.get("annualized_target_filtered_count", 0) or 0),
        "risk_profile_label": plan.get("risk_profile_label", ""),
        "conservative_profile_filtered_count": int(plan.get("conservative_profile_filtered_count", 0) or 0),
        "weighted_uncertainty_penalty_pct": float(plan.get("weighted_uncertainty_penalty_pct", 0) or 0) if plan.get("weighted_uncertainty_penalty_pct") is not None else None,
        "net_dividend_income_total": float(plan.get("net_dividend_income_total", 0) or 0),
        "annual_cost_total": float(plan.get("annual_cost_total", 0) or 0),
        "roundtrip_cost_total": float(plan.get("roundtrip_cost_total", 0) or 0),
        "cash_reserve_amount": float(plan.get("cash_reserve_amount", 0) or 0),
        "allocations_count": len(plan.get("allocations", [])),
        "ibex_analyzed_count": dashboard.get("ibex_universe_summary", {}).get("analyzed_count", 0),
        "external_signal_used_count": plan.get("external_signal_used_count", 0),
        "material_event_allocations_count": plan.get("material_event_allocations_count", 0),
        "shock_adjusted_allocations_count": plan.get("shock_adjusted_allocations_count", 0),
        "max_company_pct": float(plan.get("max_company_pct", 0) or 0),
        "max_sector_positions": int(plan.get("max_sector_positions", 0) or 0),
        "top_pick_name": top_pick_name,
        "top_pick_buy_window_label": top_pick_purchase_timing.get("buy_window_label", ""),
        "top_pick_buy_date": top_pick_purchase_timing.get("buy_date_label", ""),
        "top_pick_buy_price": float(top_pick_purchase_timing["buy_price"]) if top_pick_purchase_timing.get("buy_price") is not None else None,
        "top_pick_exit_window_label": top_pick_purchase_timing.get("exit_window_label", ""),
        "top_pick_exit_date": top_pick_purchase_timing.get("exit_date_label", ""),
        "top_pick_exit_price": float(top_pick_purchase_timing["exit_price"]) if top_pick_purchase_timing.get("exit_price") is not None else None,
        "top_pick_interval_window_label": top_pick_purchase_timing.get("interval_window_label", ""),
        "top_pick_interval_return_pct": float(top_pick_purchase_timing["interval_return_pct"]) if top_pick_purchase_timing.get("interval_return_pct") is not None else None,
        "top_pick_holding_annualized_return_pct": float(top_pick_purchase_timing["holding_annualized_return_pct"]) if top_pick_purchase_timing.get("holding_annualized_return_pct") is not None else None,
        "top_pick_buy_mode_label": top_pick_purchase_timing.get("mode_label", ""),
        "top_pick_allocated_amount": float(top_pick.get("allocated_amount", 0) or 0) if top_pick else None,
        "news_signals_count": news_overview["signals_count"],
        "news_items_count": news_overview["items_count"],
        "positive_news_count": news_overview["positive_count"],
        "negative_news_count": news_overview["negative_count"],
        "neutral_news_count": news_overview["neutral_count"],
        "methodology_note": plan.get("methodology_note", ""),
        "annualized_target_note": plan.get("annualized_target_note", ""),
        "conservative_profile_note": plan.get("conservative_profile_note", ""),
        "schedule_kind": progress_data.get("schedule_kind", ""),
        "scheduled_run_key": progress_data.get("scheduled_run_key", ""),
        "scheduled_analysis_date": progress_data.get("scheduled_analysis_date", ""),
        "scheduled_analysis_date_label": progress_data.get("scheduled_analysis_date", ""),
        "scheduled_weekdays_label": progress_data.get("scheduled_weekdays_label", ""),
    }


def serialize_allocations_data(plan: dict) -> list[dict]:
    items = []
    for item in plan.get("allocations", []):
        position = item["position"]
        purchase_timing = dict(item.get("purchase_timing") or {})
        items.append(
            {
                "rank": item["rank"],
                "company_name": position.company_name,
                "ticker": position.ticker,
                "quote_symbol": position.quote_symbol,
                "current_price_per_share": float(position.current_price_per_share) if getattr(position, "current_price_per_share", None) is not None else None,
                "latest_price_date": position.latest_price_date.isoformat() if getattr(position, "latest_price_date", None) else "",
                "sector_label": item["sector_label"],
                "status_label": item["status_label"],
                "status_key": item.get("status_key", ""),
                "is_owned": bool(position.is_owned),
                "trade_alert_label": item["trade_alert_label"],
                "reference_label": item["reference_label"],
                "strategy_label": item.get("strategy_label", ""),
                "base_optimization_score": float(item["base_optimization_score"]) if item.get("base_optimization_score") is not None else None,
                "optimization_score": float(item["optimization_score"]),
                "purchase_discipline_score": float(item["purchase_discipline_score"]) if item.get("purchase_discipline_score") is not None else None,
                "purchase_discipline_label": item.get("purchase_discipline_label", ""),
                "purchase_discipline_reason": item.get("purchase_discipline_reason", ""),
                "purchase_discipline_adjustment_pct": float(item["purchase_discipline_adjustment_pct"]) if item.get("purchase_discipline_adjustment_pct") is not None else None,
                "purchase_discipline": {
                    **(item.get("purchase_discipline") or {}),
                    "score": float((item.get("purchase_discipline") or {}).get("score")) if (item.get("purchase_discipline") or {}).get("score") is not None else None,
                    "return_score": float((item.get("purchase_discipline") or {}).get("return_score")) if (item.get("purchase_discipline") or {}).get("return_score") is not None else None,
                    "risk_score": float((item.get("purchase_discipline") or {}).get("risk_score")) if (item.get("purchase_discipline") or {}).get("risk_score") is not None else None,
                    "memory_score": float((item.get("purchase_discipline") or {}).get("memory_score")) if (item.get("purchase_discipline") or {}).get("memory_score") is not None else None,
                    "portfolio_fit_score": float((item.get("purchase_discipline") or {}).get("portfolio_fit_score")) if (item.get("purchase_discipline") or {}).get("portfolio_fit_score") is not None else None,
                    "timing_score": float((item.get("purchase_discipline") or {}).get("timing_score")) if (item.get("purchase_discipline") or {}).get("timing_score") is not None else None,
                    "adjustment_pct": float((item.get("purchase_discipline") or {}).get("adjustment_pct")) if (item.get("purchase_discipline") or {}).get("adjustment_pct") is not None else None,
                },
                "allocated_weight_pct": float(item["allocated_weight_pct"]),
                "allocated_amount": float(item["allocated_amount"]),
                "net_projected_return_pct": float(item["net_projected_return_pct"]),
                "low_return_pct": float(item["low_return_pct"]),
                "scenario_expected_return_pct": float(item["scenario_expected_return_pct"]) if item.get("scenario_expected_return_pct") is not None else None,
                "downside_stress_return_pct": float(item["downside_stress_return_pct"]) if item.get("downside_stress_return_pct") is not None else None,
                "uncertainty_penalty_pct": float(item["uncertainty_penalty_pct"]) if item.get("uncertainty_penalty_pct") is not None else None,
                "material_event": bool(item.get("material_event")),
                "blended_return_signal_pct": float(item["blended_return_signal_pct"]),
                "cycle_return_annual_pct": float(item["cycle_return_annual_pct"]) if item.get("cycle_return_annual_pct") is not None else None,
                "cycle_return_5y_pct": float(item["cycle_return_5y_pct"]) if item.get("cycle_return_5y_pct") is not None else None,
                "holding_annualized_return_pct": float(item["holding_annualized_return_pct"]) if item.get("holding_annualized_return_pct") is not None else None,
                "annualized_target_return_pct": float(item["annualized_target_return_pct"]) if item.get("annualized_target_return_pct") is not None else None,
                "annualized_target_gap_pct": float(item["annualized_target_gap_pct"]) if item.get("annualized_target_gap_pct") is not None else None,
                "meets_target_annualized_return": bool(item.get("meets_target_annualized_return")),
                "passes_conservative_profile": bool(item.get("passes_conservative_profile")),
                "cycle_support_score": float(item["cycle_support_score"]),
                "expected_net_dividend_income": float(item["expected_net_dividend_income"]),
                "annual_cost_used": float(item["annual_cost_used"]),
                "roundtrip_total_cost": float(item["roundtrip_total_cost"]),
                "safety_score": float(item["safety_score"]),
                "reliability_label": item["reliability_label"],
                "external_signal_label": item.get("external_signal_label", ""),
                "external_signal_score": float(item.get("external_signal_score", 0) or 0),
                "reliability_score": float(item["reliability_score"]) if item.get("reliability_score") is not None else None,
                "purchase_timing": {
                    "available": bool(purchase_timing.get("available")),
                    "mode": purchase_timing.get("mode", ""),
                    "mode_label": purchase_timing.get("mode_label", ""),
                    "plan_horizon_months": int(purchase_timing.get("plan_horizon_months", 0) or 0) if purchase_timing.get("plan_horizon_months") is not None else None,
                    "analysis_basis_label": purchase_timing.get("analysis_basis_label", ""),
                    "entry_month_number": int(purchase_timing.get("entry_month_number", 0) or 0) if purchase_timing.get("entry_month_number") is not None else None,
                    "entry_date": purchase_timing.get("entry_date_label", ""),
                    "entry_window_label": purchase_timing.get("entry_window_label", ""),
                    "entry_price": float(purchase_timing["entry_price"]) if purchase_timing.get("entry_price") is not None else None,
                    "buy_month_number": int(purchase_timing.get("buy_month_number", 0) or 0) if purchase_timing.get("buy_month_number") is not None else None,
                    "buy_date": purchase_timing.get("buy_date_label", ""),
                    "buy_window_label": purchase_timing.get("buy_window_label", ""),
                    "buy_price": float(purchase_timing["buy_price"]) if purchase_timing.get("buy_price") is not None else None,
                    "discount_vs_now_pct": float(purchase_timing["discount_vs_now_pct"]) if purchase_timing.get("discount_vs_now_pct") is not None else None,
                    "exit_month_number": int(purchase_timing.get("exit_month_number", 0) or 0) if purchase_timing.get("exit_month_number") is not None else None,
                    "exit_date": purchase_timing.get("exit_date_label", ""),
                    "exit_window_label": purchase_timing.get("exit_window_label", ""),
                    "exit_price": float(purchase_timing["exit_price"]) if purchase_timing.get("exit_price") is not None else None,
                    "expected_exit_month_number": int(purchase_timing.get("expected_exit_month_number", 0) or 0) if purchase_timing.get("expected_exit_month_number") is not None else None,
                    "expected_exit_date": purchase_timing.get("expected_exit_date_label", ""),
                    "expected_exit_window_label": purchase_timing.get("expected_exit_window_label", ""),
                    "expected_exit_price": float(purchase_timing["expected_exit_price"]) if purchase_timing.get("expected_exit_price") is not None else None,
                    "expected_holding_months": int(purchase_timing.get("expected_holding_months", 0) or 0) if purchase_timing.get("expected_holding_months") is not None else None,
                    "holding_months": int(purchase_timing.get("holding_months", 0) or 0) if purchase_timing.get("holding_months") is not None else None,
                    "interval_window_label": purchase_timing.get("interval_window_label", ""),
                    "interval_return_pct": float(purchase_timing["interval_return_pct"]) if purchase_timing.get("interval_return_pct") is not None else None,
                    "expected_trade_return_pct": float(purchase_timing["expected_trade_return_pct"]) if purchase_timing.get("expected_trade_return_pct") is not None else None,
                    "holding_annualized_return_pct": float(purchase_timing["holding_annualized_return_pct"]) if purchase_timing.get("holding_annualized_return_pct") is not None else None,
                    "calendar_adjusted_return_pct": float(purchase_timing["calendar_adjusted_return_pct"]) if purchase_timing.get("calendar_adjusted_return_pct") is not None else None,
                    "summary": purchase_timing.get("summary", ""),
                },
                "cycle_yearly_margins": [
                    {
                        "year_number": int(year_item.get("year_number") or 0),
                        "label": str(year_item.get("label") or ""),
                        "margin_pct": float(year_item["margin_pct"]) if year_item.get("margin_pct") is not None else None,
                        "cumulative_return_pct": float(year_item["cumulative_return_pct"]) if year_item.get("cumulative_return_pct") is not None else None,
                    }
                    for year_item in (item.get("cycle_yearly_margins") or [])
                ],
            }
        )
    return items


def enrich_allocations_with_live_quote_data(
    allocations: list[dict] | None,
    *,
    live_quote_map: dict[str, dict] | None = None,
) -> list[dict]:
    enriched_items = []
    for raw_item in allocations or []:
        item = dict(raw_item or {})
        purchase_timing = dict(item.get("purchase_timing") or {})
        metrics = attach_trade_progress_metrics(
            {
                "ticker": item.get("ticker"),
                "current_price_per_share": item.get("current_price_per_share"),
                "current_price_date_label": item.get("latest_price_date", ""),
                "entry_price": purchase_timing.get("entry_price") or purchase_timing.get("buy_price"),
                "buy_price": purchase_timing.get("buy_price"),
                "exit_price": purchase_timing.get("exit_price") or purchase_timing.get("expected_exit_price"),
                "sell_price": purchase_timing.get("exit_price") or purchase_timing.get("expected_exit_price"),
            },
            live_quote_map=live_quote_map,
        )
        item["current_price_per_share"] = metrics.get("current_price")
        item["current_price_date"] = metrics.get("current_price_date")
        item["current_price_date_label"] = metrics.get("current_price_date_label", "")
        item["current_vs_entry_pct"] = metrics.get("current_vs_entry_pct")
        item["current_vs_exit_pct"] = metrics.get("current_vs_exit_pct")
        item["remaining_to_exit_pct"] = metrics.get("remaining_to_exit_pct")
        item["current_position_label"] = metrics.get("current_position_label", "")
        item["current_position_tone"] = metrics.get("current_position_tone", "")
        purchase_timing.update(
            {
                "current_price": metrics.get("current_price"),
                "current_price_date": metrics.get("current_price_date"),
                "current_price_date_label": metrics.get("current_price_date_label", ""),
                "current_vs_entry_pct": metrics.get("current_vs_entry_pct"),
                "current_vs_exit_pct": metrics.get("current_vs_exit_pct"),
                "remaining_to_exit_pct": metrics.get("remaining_to_exit_pct"),
                "current_position_label": metrics.get("current_position_label", ""),
                "current_position_tone": metrics.get("current_position_tone", ""),
            }
        )
        item["purchase_timing"] = purchase_timing
        enriched_items.append(item)
    return enriched_items


def build_optimization_comparison_context(runs: list[EquityOptimizationRun]) -> dict:
    completed_runs = [
        run
        for run in runs
        if run.status == EquityOptimizationRun.Status.COMPLETED and (run.summary_data or run.allocations_data)
    ]
    rows = []
    for run in completed_runs:
        summary = dict(run.summary_data or {})
        allocations = list(run.allocations_data or [])
        sectors = sorted({item.get("sector_label") for item in allocations if item.get("sector_label")})
        company_tokens = [item.get("ticker") or item.get("company_name") or "" for item in allocations]
        displayed_tokens = [token for token in company_tokens if token][:6]
        constituents_label = ", ".join(displayed_tokens)
        if len(company_tokens) > len(displayed_tokens):
            constituents_label += f" +{len(company_tokens) - len(displayed_tokens)}"
        rows.append(
            {
                "run": run,
                "display_label": run.display_label,
                "reference_code": run.reference_code,
                "created_at_label": summary.get("created_at_label") or timezone.localtime(run.created_at).strftime("%Y-%m-%d %H:%M"),
                "completed_at_label": summary.get("completed_at_label") or (timezone.localtime(run.completed_at).strftime("%Y-%m-%d %H:%M") if run.completed_at else ""),
                "restrictions_note": run.restrictions_note,
                "total_investment": summary.get("total_investment"),
                "max_company_pct": summary.get("max_company_pct"),
                "max_total_positions": summary.get("max_total_positions") or 0,
                "max_sector_positions": summary.get("max_sector_positions") or 0,
                "selected_sectors": list(summary.get("selected_sectors") or run.selected_sectors or []),
                "selected_sectors_label": ", ".join(summary.get("selected_sectors") or run.selected_sectors or []),
                "selected_owned_tickers": list(summary.get("selected_owned_tickers") or run.selected_owned_tickers or []),
                "selected_owned_tickers_label": ", ".join(summary.get("selected_owned_tickers") or run.selected_owned_tickers or []),
                "selected_owned_tickers_applied": bool(
                    summary.get("selected_owned_tickers_applied")
                    or run.selected_owned_tickers_applied
                ),
                "owned_positions_available_count": int(
                    summary.get("owned_positions_available_count", 0) or 0
                ),
                "strategy_label": summary.get("strategy_label") or (run.progress_data or {}).get("strategy_label", ""),
                "allocations_count": summary.get("allocations_count", len(allocations)),
                "constituents_label": constituents_label,
                "sectors_count": len(sectors),
                "top_pick_name": summary.get("top_pick_name") or (allocations[0].get("company_name") if allocations else ""),
                "projected_gain_total": summary.get("projected_gain_total"),
                "weighted_return_pct": summary.get("weighted_return_pct"),
                "weighted_low_return_pct": summary.get("weighted_low_return_pct"),
                "weighted_safety_score": summary.get("weighted_safety_score"),
                "weighted_reliability_score": summary.get("weighted_reliability_score"),
                "net_dividend_income_total": summary.get("net_dividend_income_total"),
                "annual_cost_total": summary.get("annual_cost_total"),
                "roundtrip_cost_total": summary.get("roundtrip_cost_total"),
                "cash_reserve_amount": summary.get("cash_reserve_amount"),
            }
        )

    if not rows:
        return {"available": False, "rows": []}

    best_return = max(
        rows,
        key=lambda row: Decimal(str(row.get("weighted_return_pct"))) if row.get("weighted_return_pct") is not None else Decimal("-9999"),
    )
    best_protection = max(
        rows,
        key=lambda row: Decimal(str(row.get("weighted_low_return_pct"))) if row.get("weighted_low_return_pct") is not None else Decimal("-9999"),
    )
    best_safety = max(
        rows,
        key=lambda row: Decimal(str(row.get("weighted_safety_score"))) if row.get("weighted_safety_score") is not None else Decimal("-9999"),
    )
    return {
        "available": True,
        "rows": rows,
        "runs_count": len(rows),
        "best_return": best_return,
        "best_protection": best_protection,
        "best_safety": best_safety,
    }


def build_optimization_purchase_timeline(
    run: EquityOptimizationRun | None,
    *,
    max_rows: int = 8,
    live_quote_map: dict[str, dict] | None = None,
) -> dict:
    if run is None:
        return {"available": False, "rows": []}

    allocations = list(run.allocations_data or [])
    if not allocations:
        return {"available": False, "rows": []}

    progress_data = dict(run.progress_data or {})
    summary_data = dict(run.summary_data or {})
    reference_date_label = (
        summary_data.get("scheduled_analysis_date")
        or progress_data.get("scheduled_analysis_date")
        or ""
    )
    try:
        reference_date = date.fromisoformat(reference_date_label) if reference_date_label else None
    except ValueError:
        reference_date = None
    if reference_date is None:
        reference_date = timezone.localtime(run.completed_at or run.created_at).date()

    scheduled_rows = []
    unscheduled_rows = []
    for item in allocations:
        purchase_timing = dict(item.get("purchase_timing") or {})
        if not purchase_timing.get("available"):
            unscheduled_rows.append(
                {
                    "ticker": item.get("ticker", ""),
                    "company_name": item.get("company_name", ""),
                    "reason": "Sin ventana de compra clara",
                }
            )
            continue
        buy_date_label = str(purchase_timing.get("buy_date") or "").strip()
        try:
            buy_date = date.fromisoformat(buy_date_label) if buy_date_label else None
        except ValueError:
            buy_date = None
        exit_date_label = str(
            purchase_timing.get("exit_date")
            or purchase_timing.get("expected_exit_date")
            or ""
        ).strip()
        try:
            exit_date = date.fromisoformat(exit_date_label) if exit_date_label else None
        except ValueError:
            exit_date = None
        if buy_date is None:
            unscheduled_rows.append(
                {
                    "ticker": item.get("ticker", ""),
                    "company_name": item.get("company_name", ""),
                    "reason": "Fecha de compra no disponible",
                }
            )
            continue
        if exit_date is None or exit_date <= buy_date:
            unscheduled_rows.append(
                {
                    "ticker": item.get("ticker", ""),
                    "company_name": item.get("company_name", ""),
                    "reason": "Fecha de salida no disponible",
                }
            )
            continue
        buy_price = purchase_timing.get("buy_price")
        exit_price = purchase_timing.get("exit_price") or purchase_timing.get("expected_exit_price")
        allocated_amount = item.get("allocated_amount")
        holding_months = int(
            purchase_timing.get("holding_months")
            or purchase_timing.get("expected_holding_months")
            or 0
        ) or None
        interval_return_pct = (
            Decimal(str(purchase_timing["interval_return_pct"]))
            if purchase_timing.get("interval_return_pct") is not None
            else (
                Decimal(str(purchase_timing["expected_trade_return_pct"]))
                if purchase_timing.get("expected_trade_return_pct") is not None
                else None
            )
        )
        holding_annualized_return_pct = (
            Decimal(str(purchase_timing["holding_annualized_return_pct"]))
            if purchase_timing.get("holding_annualized_return_pct") is not None
            else quantize_decimal(annualize_return_pct(interval_return_pct, holding_months or 0), "0.01")
        )
        days_until_buy = max((buy_date - reference_date).days, 0)
        if days_until_buy <= 14:
            status_key = "urgent"
            status_label = "Entrada inmediata"
        elif days_until_buy <= 90:
            status_key = "soon"
            status_label = "Entrada proxima"
        else:
            status_key = "scheduled"
            status_label = "Tramo programado"
        scheduled_rows.append(
            attach_trade_progress_metrics(
                {
                    "ticker": item.get("ticker", ""),
                    "company_name": item.get("company_name", ""),
                    "rank": int(item.get("rank") or 0),
                    "is_owned": bool(item.get("is_owned")),
                    "status_key": status_key,
                    "status_label": status_label,
                    "buy_date": buy_date,
                    "entry_date": buy_date,
                    "buy_window_label": purchase_timing.get("buy_window_label", "") or buy_date.isoformat(),
                    "entry_window_label": purchase_timing.get("entry_window_label", "") or purchase_timing.get("buy_window_label", "") or buy_date.isoformat(),
                    "buy_mode_label": purchase_timing.get("mode_label", "") or status_label,
                    "buy_price": Decimal(str(buy_price)) if buy_price is not None else None,
                    "entry_price": Decimal(str(buy_price)) if buy_price is not None else None,
                    "current_price_per_share": item.get("current_price_per_share"),
                    "current_price_date_label": item.get("latest_price_date", ""),
                    "exit_date": exit_date,
                    "exit_window_label": purchase_timing.get("exit_window_label", "") or purchase_timing.get("expected_exit_window_label", "") or exit_date.isoformat(),
                    "exit_price": Decimal(str(exit_price)) if exit_price is not None else None,
                    "allocated_amount": Decimal(str(allocated_amount)) if allocated_amount is not None else None,
                    "allocated_weight_pct": Decimal(str(item.get("allocated_weight_pct"))) if item.get("allocated_weight_pct") is not None else None,
                    "holding_months": holding_months,
                    "interval_window_label": purchase_timing.get("interval_window_label", ""),
                    "interval_return_pct": interval_return_pct,
                    "expected_trade_return_pct": Decimal(str(purchase_timing["expected_trade_return_pct"])) if purchase_timing.get("expected_trade_return_pct") is not None else None,
                    "holding_annualized_return_pct": holding_annualized_return_pct,
                },
                live_quote_map=live_quote_map,
            )
        )

    if not scheduled_rows:
        return {
            "available": False,
            "rows": [],
            "reference_date": reference_date,
            "unscheduled_rows": unscheduled_rows,
        }

    scheduled_rows.sort(key=lambda item: (item["buy_date"], item["rank"], item["ticker"]))
    visible_rows = scheduled_rows[:max_rows]
    entry_horizon_end = shift_date_by_months(reference_date, 12)
    horizon_end = max(
        entry_horizon_end,
        max(item["exit_date"] for item in visible_rows),
    )
    horizon_days = max((horizon_end - reference_date).days, 1)
    horizon_months = max(
        ((horizon_end.year - reference_date.year) * 12) + (horizon_end.month - reference_date.month),
        1,
    )
    markers = []
    marker_months = {0, 3, 6, 9, 12, horizon_months}
    yearly_marker = 24
    while yearly_marker < horizon_months:
        marker_months.add(yearly_marker)
        yearly_marker += 12
    marker_months = sorted(marker_months)
    for month_offset in marker_months:
        marker_date = shift_date_by_months(reference_date, month_offset)
        left_pct = min(max(((marker_date - reference_date).days / horizon_days) * 100, 0), 100)
        markers.append(
            {
                "label": marker_date.strftime("%Y-%m"),
                "left_pct": f"{left_pct:.2f}",
            }
        )

    planned_amount_total = ZERO
    immediate_count = 0
    exit_within_horizon_count = 0
    long_exit_count = 0
    for row in visible_rows:
        if row.get("allocated_amount") is not None:
            planned_amount_total += row["allocated_amount"]
        if row["status_key"] == "urgent":
            immediate_count += 1
        if row.get("exit_date") is not None and row["exit_date"] <= horizon_end:
            exit_within_horizon_count += 1
        if row.get("exit_date") is not None and row["exit_date"] > entry_horizon_end:
            long_exit_count += 1
            row["extends_beyond_entry_window"] = True
        else:
            row["extends_beyond_entry_window"] = False
        entry_left_pct = min(max((((row["buy_date"] - reference_date).days) / horizon_days) * 100, 0), 100)
        exit_left_pct = min(max((((row["exit_date"] - reference_date).days) / horizon_days) * 100, 0), 100)
        row["entry_pin_left_pct"] = f"{entry_left_pct:.2f}"
        row["exit_pin_left_pct"] = f"{exit_left_pct:.2f}"
        row["pin_left_pct"] = row["entry_pin_left_pct"]
        row["bar_left_pct"] = f"{entry_left_pct:.2f}"
        row["bar_width_pct"] = f"{max(exit_left_pct - entry_left_pct, 2.6):.2f}"
    entry_horizon_pct = min(max(((entry_horizon_end - reference_date).days / horizon_days) * 100, 0), 100)
    next_row = visible_rows[0]
    next_new_row = next((row for row in visible_rows if not row.get("is_owned")), None)
    return {
        "available": True,
        "rows": visible_rows,
        "markers": markers,
        "reference_date": reference_date,
        "entry_horizon_end": entry_horizon_end,
        "entry_horizon_months": 12,
        "entry_horizon_pct": f"{entry_horizon_pct:.2f}",
        "horizon_end": horizon_end,
        "horizon_months": horizon_months,
        "horizon_is_extended": horizon_end > entry_horizon_end,
        "total_scheduled_count": len(scheduled_rows),
        "hidden_scheduled_count": max(len(scheduled_rows) - len(visible_rows), 0),
        "scheduled_count": len(visible_rows),
        "unscheduled_count": len(unscheduled_rows),
        "planned_amount_total": planned_amount_total.quantize(Decimal("0.01")),
        "immediate_count": immediate_count,
        "exit_within_horizon_count": exit_within_horizon_count,
        "long_exit_count": long_exit_count,
        "new_positions_count": sum(1 for row in visible_rows if not row.get("is_owned")),
        "top_up_count": sum(1 for row in visible_rows if row.get("is_owned")),
        "next_row": next_row,
        "next_new_row": next_new_row,
        "unscheduled_rows": unscheduled_rows[:max_rows],
    }


def build_optimization_compact_timeline(
    run: EquityOptimizationRun | None,
    *,
    max_rows: int = 5,
    live_quote_map: dict[str, dict] | None = None,
) -> dict:
    timeline = build_optimization_purchase_timeline(run, max_rows=max_rows, live_quote_map=live_quote_map)
    if not timeline.get("available"):
        return {"available": False, "rows": []}

    rows = []
    for row in timeline.get("rows", []):
        rows.append(
            {
                "ticker": row.get("ticker", ""),
                "company_name": row.get("company_name", ""),
                "status_key": row.get("status_key", "scheduled"),
                "entry_pin_left_pct": row.get("entry_pin_left_pct", "0"),
                "exit_pin_left_pct": row.get("exit_pin_left_pct", "0"),
                "bar_left_pct": row.get("bar_left_pct", "0"),
                "bar_width_pct": row.get("bar_width_pct", "0"),
                "extends_beyond_entry_window": bool(row.get("extends_beyond_entry_window")),
                "holding_months": row.get("holding_months"),
                "entry_window_label": row.get("entry_window_label", "") or row.get("buy_window_label", ""),
                "entry_price": row.get("entry_price") or row.get("buy_price"),
                "exit_window_label": row.get("exit_window_label", ""),
                "exit_price": row.get("exit_price"),
                "holding_annualized_return_pct": row.get("holding_annualized_return_pct"),
                "interval_return_pct": row.get("interval_return_pct") or row.get("expected_trade_return_pct"),
                "current_price": row.get("current_price"),
                "current_vs_entry_pct": row.get("current_vs_entry_pct"),
                "remaining_to_exit_pct": row.get("remaining_to_exit_pct"),
                "current_position_label": row.get("current_position_label", ""),
                "current_position_tone": row.get("current_position_tone", ""),
            }
        )

    return {
        "available": True,
        "rows": rows,
        "entry_horizon_pct": timeline.get("entry_horizon_pct", "100"),
        "entry_horizon_end": timeline.get("entry_horizon_end"),
        "horizon_end": timeline.get("horizon_end"),
        "horizon_is_extended": bool(timeline.get("horizon_is_extended")),
        "hidden_rows_count": timeline.get("hidden_scheduled_count", 0),
        "scheduled_count": timeline.get("total_scheduled_count", len(rows)),
    }


def build_scheduled_optimization_persistence_context(
    *,
    as_of: date | None = None,
    max_rows: int = 12,
    requested_by=None,
    include_all_users: bool = False,
    live_quote_map: dict[str, dict] | None = None,
) -> dict:
    as_of = as_of or timezone.localdate()
    cutoff_date = scheduled_optimization_retention_cutoff(as_of)
    completed_runs_queryset = EquityOptimizationRun.objects.filter(
        status=EquityOptimizationRun.Status.COMPLETED,
        progress_data__schedule_kind="nightly",
    )
    if not include_all_users and requested_by is not None:
        completed_runs_queryset = completed_runs_queryset.filter(requested_by=requested_by)
    completed_runs = list(completed_runs_queryset.order_by("-created_at", "-id"))
    weekdays = scheduled_optimization_iso_weekdays()
    weekdays_label = build_scheduled_optimization_weekdays_label(weekdays)
    next_run_date = resolve_next_scheduled_optimization_date(as_of, weekdays, include_today=False)
    completed_runs = [
        run
        for run in completed_runs
        if resolve_scheduled_run_date(run) >= cutoff_date and scheduled_optimization_matches_policy(run)
    ]
    if not completed_runs:
        return {
            "available": False,
            "rows": [],
            "weekdays_label": weekdays_label,
            "next_run_date_label": next_run_date.isoformat() if next_run_date else "",
            "window_label": "ultimos 3 meses",
            "policy": {
                "max_company_pct": SCHEDULED_OPTIMIZATION_MAX_COMPANY_PCT.quantize(Decimal("0")),
                "max_total_positions": SCHEDULED_OPTIMIZATION_MAX_TOTAL_POSITIONS,
                "max_sector_positions": SCHEDULED_OPTIMIZATION_MAX_SECTOR_POSITIONS,
            },
        }

    def ratio_percent(numerator: int, denominator: int) -> Decimal | None:
        if denominator <= 0:
            return None
        return (
            Decimal(str(numerator)) * Decimal("100") / Decimal(str(denominator))
        ).quantize(Decimal("0.1"))

    def reliability_label_from_score(score: Decimal | None) -> str:
        if score is None:
            return "-"
        if score >= Decimal("75"):
            return "Alta"
        if score >= Decimal("55"):
            return "Media"
        return "Baja"

    currently_owned_tickers = {
        str(position.ticker or "").strip().upper()
        for position in EquityPosition.objects.all()
        if position.is_owned
    }
    stats_by_ticker: dict[str, dict] = {}
    runs_count = 0
    distinct_days = set()

    for run in completed_runs:
        progress_data = dict(run.progress_data or {})
        summary_data = dict(run.summary_data or {})
        run_date = resolve_scheduled_run_date(run)
        strategy_label = (
            summary_data.get("strategy_label")
            or progress_data.get("strategy_label")
            or ""
        )
        runs_count += 1
        distinct_days.add(run_date)

        for item in run.allocations_data or []:
            ticker = str(item.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            stats = stats_by_ticker.setdefault(
                ticker,
                {
                    "ticker": ticker,
                    "company_name": str(item.get("company_name") or ticker),
                    "last_seen_on": run_date,
                    "strategy_labels_3m": set(),
                    "distinct_days_3m": set(),
                    "appearances_3m": 0,
                    "top3_3m": 0,
                    "rank_total_3m": Decimal("0"),
                    "rank_count_3m": 0,
                    "return_total_12m_3m": Decimal("0"),
                    "return_count_12m_3m": 0,
                    "return_total_5y_3m": Decimal("0"),
                    "return_count_5y_3m": 0,
                    "reliability_total_3m": Decimal("0"),
                    "reliability_count_3m": 0,
                    "year_margin_totals": {year_number: Decimal("0") for year_number in range(1, 6)},
                    "year_margin_counts": {year_number: 0 for year_number in range(1, 6)},
                    "daily_strategies": {},
                    "buy_dates_3m": [],
                    "sell_dates_3m": [],
                    "buy_window_labels_3m": set(),
                    "sell_window_labels_3m": set(),
                    "interval_window_labels_3m": set(),
                    "buy_modes_3m": set(),
                    "buy_price_total_3m": Decimal("0"),
                    "buy_price_count_3m": 0,
                    "sell_price_total_3m": Decimal("0"),
                    "sell_price_count_3m": 0,
                    "allocated_amount_total_3m": Decimal("0"),
                    "allocated_amount_count_3m": 0,
                    "allocated_weight_total_3m": Decimal("0"),
                    "allocated_weight_count_3m": 0,
                    "interval_return_total_3m": Decimal("0"),
                    "interval_return_count_3m": 0,
                    "annualized_return_total_3m": Decimal("0"),
                    "annualized_return_count_3m": 0,
                    "holding_months_total_3m": Decimal("0"),
                    "holding_months_count_3m": 0,
                    "latest_buy_date": None,
                    "latest_buy_window_label": "",
                    "latest_buy_price": None,
                    "latest_current_price": None,
                    "latest_current_price_date": None,
                    "latest_sell_date": None,
                    "latest_sell_window_label": "",
                    "latest_sell_price": None,
                    "latest_interval_window_label": "",
                    "latest_interval_return_pct": None,
                    "latest_annualized_return_pct": None,
                    "latest_holding_months": None,
                    "latest_allocated_amount": None,
                    "latest_allocated_weight_pct": None,
                },
            )
            stats["company_name"] = str(item.get("company_name") or stats["company_name"] or ticker)
            stats["appearances_3m"] += 1
            stats["distinct_days_3m"].add(run_date)
            if strategy_label:
                stats["strategy_labels_3m"].add(strategy_label)
                stats["daily_strategies"].setdefault(run_date, set()).add(strategy_label)
            net_return_pct = item.get("net_projected_return_pct")
            if net_return_pct is not None:
                stats["return_total_12m_3m"] += Decimal(str(net_return_pct))
                stats["return_count_12m_3m"] += 1
            cycle_return_5y_pct = item.get("cycle_return_5y_pct")
            if cycle_return_5y_pct is not None:
                stats["return_total_5y_3m"] += Decimal(str(cycle_return_5y_pct))
                stats["return_count_5y_3m"] += 1
            reliability_score = item.get("reliability_score")
            if reliability_score is None and item.get("reliability_label"):
                reliability_score = projection_reliability_score(str(item.get("reliability_label") or ""))
            if reliability_score is not None:
                stats["reliability_total_3m"] += Decimal(str(reliability_score))
                stats["reliability_count_3m"] += 1
            purchase_timing = dict(item.get("purchase_timing") or {})
            buy_date = None
            buy_date_value = str(purchase_timing.get("buy_date") or "").strip()
            if buy_date_value:
                try:
                    buy_date = date.fromisoformat(buy_date_value)
                except ValueError:
                    buy_date = None
            if buy_date is not None:
                stats["buy_dates_3m"].append(buy_date)
            buy_window_label = str(purchase_timing.get("buy_window_label") or "").strip()
            if buy_window_label:
                stats["buy_window_labels_3m"].add(buy_window_label)
            sell_date = None
            sell_date_value = str(
                purchase_timing.get("exit_date")
                or purchase_timing.get("expected_exit_date")
                or ""
            ).strip()
            if sell_date_value:
                try:
                    sell_date = date.fromisoformat(sell_date_value)
                except ValueError:
                    sell_date = None
            if sell_date is not None:
                stats["sell_dates_3m"].append(sell_date)
            sell_window_label = str(
                purchase_timing.get("exit_window_label")
                or purchase_timing.get("expected_exit_window_label")
                or ""
            ).strip()
            if sell_window_label:
                stats["sell_window_labels_3m"].add(sell_window_label)
            interval_window_label = str(purchase_timing.get("interval_window_label") or "").strip()
            if interval_window_label:
                stats["interval_window_labels_3m"].add(interval_window_label)
            buy_mode_label = str(purchase_timing.get("mode_label") or "").strip()
            if buy_mode_label:
                stats["buy_modes_3m"].add(buy_mode_label)
            buy_price = purchase_timing.get("buy_price")
            if buy_price is not None:
                stats["buy_price_total_3m"] += Decimal(str(buy_price))
                stats["buy_price_count_3m"] += 1
            current_price = item.get("current_price_per_share")
            current_price_date = normalize_date_value(item.get("latest_price_date"))
            sell_price = purchase_timing.get("exit_price") or purchase_timing.get("expected_exit_price")
            if sell_price is not None:
                stats["sell_price_total_3m"] += Decimal(str(sell_price))
                stats["sell_price_count_3m"] += 1
            interval_return_pct = purchase_timing.get("interval_return_pct") or purchase_timing.get("expected_trade_return_pct")
            if interval_return_pct is not None:
                stats["interval_return_total_3m"] += Decimal(str(interval_return_pct))
                stats["interval_return_count_3m"] += 1
            holding_months = purchase_timing.get("holding_months") or purchase_timing.get("expected_holding_months")
            annualized_return_pct = purchase_timing.get("holding_annualized_return_pct")
            if annualized_return_pct is None and interval_return_pct is not None and holding_months is not None:
                annualized_return_pct = annualize_return_pct(Decimal(str(interval_return_pct)), int(holding_months))
            if annualized_return_pct is not None:
                stats["annualized_return_total_3m"] += Decimal(str(annualized_return_pct))
                stats["annualized_return_count_3m"] += 1
            if holding_months is not None:
                stats["holding_months_total_3m"] += Decimal(str(holding_months))
                stats["holding_months_count_3m"] += 1
            allocated_amount = item.get("allocated_amount")
            if allocated_amount is not None:
                stats["allocated_amount_total_3m"] += Decimal(str(allocated_amount))
                stats["allocated_amount_count_3m"] += 1
            allocated_weight_pct = item.get("allocated_weight_pct")
            if allocated_weight_pct is not None:
                stats["allocated_weight_total_3m"] += Decimal(str(allocated_weight_pct))
                stats["allocated_weight_count_3m"] += 1
            if buy_date is not None and (stats["latest_buy_date"] is None or buy_date >= stats["latest_buy_date"]):
                stats["latest_buy_date"] = buy_date
                stats["latest_buy_window_label"] = buy_window_label
                stats["latest_buy_price"] = Decimal(str(buy_price)) if buy_price is not None else None
                stats["latest_current_price"] = normalize_price_value(current_price)
                stats["latest_current_price_date"] = current_price_date
                stats["latest_sell_date"] = sell_date
                stats["latest_sell_window_label"] = sell_window_label
                stats["latest_sell_price"] = Decimal(str(sell_price)) if sell_price is not None else None
                stats["latest_interval_window_label"] = interval_window_label
                stats["latest_interval_return_pct"] = Decimal(str(interval_return_pct)) if interval_return_pct is not None else None
                stats["latest_annualized_return_pct"] = Decimal(str(annualized_return_pct)) if annualized_return_pct is not None else None
                stats["latest_holding_months"] = int(holding_months) if holding_months is not None else None
                stats["latest_allocated_amount"] = Decimal(str(allocated_amount)) if allocated_amount is not None else None
                stats["latest_allocated_weight_pct"] = Decimal(str(allocated_weight_pct)) if allocated_weight_pct is not None else None
            for year_item in item.get("cycle_yearly_margins") or []:
                try:
                    year_number = int(year_item.get("year_number") or 0)
                except (TypeError, ValueError):
                    year_number = 0
                if year_number not in stats["year_margin_totals"]:
                    continue
                margin_pct = year_item.get("margin_pct")
                if margin_pct is None:
                    continue
                stats["year_margin_totals"][year_number] += Decimal(str(margin_pct))
                stats["year_margin_counts"][year_number] += 1
            rank_value = item.get("rank")
            if rank_value is not None:
                try:
                    rank_number = int(rank_value)
                except (TypeError, ValueError):
                    rank_number = 0
                if rank_number > 0:
                    stats["rank_total_3m"] += Decimal(str(rank_number))
                    stats["rank_count_3m"] += 1
                    if rank_number <= 3:
                        stats["top3_3m"] += 1
            if run_date >= stats["last_seen_on"]:
                stats["last_seen_on"] = run_date

    rows = []
    total_days_count = len(distinct_days)
    for stats in stats_by_ticker.values():
        latest_strategies = stats["daily_strategies"].get(stats["last_seen_on"], set())
        average_rank = None
        if stats["rank_count_3m"]:
            average_rank = (
                stats["rank_total_3m"] / Decimal(str(stats["rank_count_3m"]))
            ).quantize(Decimal("0.1"))
        average_return_12m = None
        if stats["return_count_12m_3m"]:
            average_return_12m = (
                stats["return_total_12m_3m"] / Decimal(str(stats["return_count_12m_3m"]))
            ).quantize(Decimal("0.1"))
        average_return_5y = None
        if stats["return_count_5y_3m"]:
            average_return_5y = (
                stats["return_total_5y_3m"] / Decimal(str(stats["return_count_5y_3m"]))
            ).quantize(Decimal("0.1"))
        average_reliability_score = None
        if stats["reliability_count_3m"]:
            average_reliability_score = (
                stats["reliability_total_3m"] / Decimal(str(stats["reliability_count_3m"]))
            ).quantize(Decimal("0.1"))
        average_buy_price = None
        if stats["buy_price_count_3m"]:
            average_buy_price = (
                stats["buy_price_total_3m"] / Decimal(str(stats["buy_price_count_3m"]))
            ).quantize(Decimal("0.0001"))
        average_sell_price = None
        if stats["sell_price_count_3m"]:
            average_sell_price = (
                stats["sell_price_total_3m"] / Decimal(str(stats["sell_price_count_3m"]))
            ).quantize(Decimal("0.0001"))
        average_allocated_amount = None
        if stats["allocated_amount_count_3m"]:
            average_allocated_amount = (
                stats["allocated_amount_total_3m"] / Decimal(str(stats["allocated_amount_count_3m"]))
            ).quantize(Decimal("0.01"))
        average_allocated_weight_pct = None
        if stats["allocated_weight_count_3m"]:
            average_allocated_weight_pct = (
                stats["allocated_weight_total_3m"] / Decimal(str(stats["allocated_weight_count_3m"]))
            ).quantize(Decimal("0.1"))
        average_interval_return_pct = None
        if stats["interval_return_count_3m"]:
            average_interval_return_pct = (
                stats["interval_return_total_3m"] / Decimal(str(stats["interval_return_count_3m"]))
            ).quantize(Decimal("0.1"))
        average_annualized_return_pct = None
        if stats["annualized_return_count_3m"]:
            average_annualized_return_pct = (
                stats["annualized_return_total_3m"] / Decimal(str(stats["annualized_return_count_3m"]))
            ).quantize(Decimal("0.1"))
        average_holding_months = None
        if stats["holding_months_count_3m"]:
            average_holding_months = (
                stats["holding_months_total_3m"] / Decimal(str(stats["holding_months_count_3m"]))
            ).quantize(Decimal("0.1"))
        average_year_margins = []
        for year_number in range(1, 6):
            year_average = None
            if stats["year_margin_counts"][year_number]:
                year_average = (
                    stats["year_margin_totals"][year_number] / Decimal(str(stats["year_margin_counts"][year_number]))
                ).quantize(Decimal("0.1"))
            average_year_margins.append(
                {
                    "year_number": year_number,
                    "label": f"AÑO {year_number}",
                    "margin_pct": year_average,
                }
            )
        distinct_days_count = len(stats["distinct_days_3m"])
        presence_pct = ratio_percent(stats["appearances_3m"], runs_count)
        day_presence_pct = ratio_percent(distinct_days_count, total_days_count)
        top3_pct = ratio_percent(stats["top3_3m"], stats["appearances_3m"])
        latest_strategy_count = len(latest_strategies)
        sorted_buy_dates = sorted(set(stats["buy_dates_3m"]))
        sorted_sell_dates = sorted(set(stats["sell_dates_3m"]))
        buy_dates_sample_label = "-"
        if sorted_buy_dates:
            if len(sorted_buy_dates) <= 3:
                buy_dates_sample_label = ", ".join(item.isoformat() for item in sorted_buy_dates)
            else:
                buy_dates_sample_label = (
                    f"{sorted_buy_dates[0].isoformat()} a {sorted_buy_dates[-1].isoformat()} "
                    f"(+{len(sorted_buy_dates) - 2})"
                )
        sell_dates_sample_label = "-"
        if sorted_sell_dates:
            if len(sorted_sell_dates) <= 3:
                sell_dates_sample_label = ", ".join(item.isoformat() for item in sorted_sell_dates)
            else:
                sell_dates_sample_label = (
                    f"{sorted_sell_dates[0].isoformat()} a {sorted_sell_dates[-1].isoformat()} "
                    f"(+{len(sorted_sell_dates) - 2})"
                )
        latest_buy_date = stats["latest_buy_date"]
        latest_buy_window_label = stats["latest_buy_window_label"] or (
            format(latest_buy_date, "%Y-%m-%d") if latest_buy_date else ""
        )
        latest_sell_date = stats["latest_sell_date"]
        latest_sell_window_label = stats["latest_sell_window_label"] or (
            format(latest_sell_date, "%Y-%m-%d") if latest_sell_date else ""
        )
        if distinct_days_count >= 4 and latest_strategy_count >= 2:
            persistence_label = "Alta"
        elif distinct_days_count >= 2:
            persistence_label = "Media"
        else:
            persistence_label = "Puntual"
        rows.append(
            attach_trade_progress_metrics(
                {
                    "ticker": stats["ticker"],
                    "company_name": stats["company_name"],
                    "appearances_3m": stats["appearances_3m"],
                    "presence_pct_3m": presence_pct,
                    "distinct_days_3m": distinct_days_count,
                    "day_presence_pct_3m": day_presence_pct,
                    "top3_3m": stats["top3_3m"],
                    "top3_pct_3m": top3_pct,
                    "average_rank_3m": average_rank,
                    "average_return_12m_3m": average_return_12m,
                    "average_return_5y_3m": average_return_5y,
                    "average_year_margins": average_year_margins,
                    "average_reliability_score_3m": average_reliability_score,
                    "average_reliability_label_3m": reliability_label_from_score(average_reliability_score),
                    "persistence_label": persistence_label,
                    "strategy_labels_3m": sorted(stats["strategy_labels_3m"]),
                    "strategy_labels_3m_label": ", ".join(sorted(stats["strategy_labels_3m"])) or "-",
                    "latest_day_strategy_count": latest_strategy_count,
                    "latest_day_strategy_label": ", ".join(sorted(latest_strategies)) or "-",
                    "currently_owned": stats["ticker"] in currently_owned_tickers,
                    "buy_recommendation_available": latest_buy_date is not None,
                    "latest_buy_date": latest_buy_date,
                    "latest_buy_date_label": latest_buy_date.isoformat() if latest_buy_date else "",
                    "latest_buy_window_label": latest_buy_window_label,
                    "latest_buy_price": stats["latest_buy_price"],
                    "latest_current_price": stats["latest_current_price"],
                    "current_price_date": stats["latest_current_price_date"],
                    "current_price_date_label": stats["latest_current_price_date"].isoformat() if stats["latest_current_price_date"] else "",
                    "latest_sell_date": latest_sell_date,
                    "latest_sell_date_label": latest_sell_date.isoformat() if latest_sell_date else "",
                    "latest_sell_window_label": latest_sell_window_label,
                    "latest_sell_price": stats["latest_sell_price"],
                    "latest_interval_window_label": stats["latest_interval_window_label"],
                    "latest_interval_return_pct": stats["latest_interval_return_pct"],
                    "latest_annualized_return_pct": stats["latest_annualized_return_pct"],
                    "latest_holding_months": stats["latest_holding_months"],
                    "latest_allocated_amount": stats["latest_allocated_amount"],
                    "latest_allocated_weight_pct": stats["latest_allocated_weight_pct"],
                    "average_buy_price_3m": average_buy_price,
                    "average_sell_price_3m": average_sell_price,
                    "average_allocated_amount_3m": average_allocated_amount,
                    "average_allocated_weight_pct_3m": average_allocated_weight_pct,
                    "average_interval_return_pct_3m": average_interval_return_pct,
                    "average_annualized_return_pct_3m": average_annualized_return_pct,
                    "average_holding_months_3m": average_holding_months,
                    "buy_dates_count_3m": len(sorted_buy_dates),
                    "buy_dates_sample_label": buy_dates_sample_label,
                    "sell_dates_count_3m": len(sorted_sell_dates),
                    "sell_dates_sample_label": sell_dates_sample_label,
                    "buy_window_labels_3m": sorted(stats["buy_window_labels_3m"]),
                    "buy_window_labels_3m_label": ", ".join(sorted(stats["buy_window_labels_3m"])) or "-",
                    "sell_window_labels_3m": sorted(stats["sell_window_labels_3m"]),
                    "sell_window_labels_3m_label": ", ".join(sorted(stats["sell_window_labels_3m"])) or "-",
                    "interval_window_labels_3m": sorted(stats["interval_window_labels_3m"]),
                    "interval_window_labels_3m_label": ", ".join(sorted(stats["interval_window_labels_3m"])) or "-",
                    "buy_modes_3m": sorted(stats["buy_modes_3m"]),
                    "buy_modes_3m_label": ", ".join(sorted(stats["buy_modes_3m"])) or "-",
                    "last_seen_on": stats["last_seen_on"],
                    "last_seen_on_label": stats["last_seen_on"].isoformat(),
                },
                live_quote_map=live_quote_map,
            )
        )

    rows.sort(
        key=lambda item: (
            -item["appearances_3m"],
            -(
                item["average_annualized_return_pct_3m"]
                if item["average_annualized_return_pct_3m"] is not None
                else Decimal("-9999")
            ),
            -(
                item["average_return_12m_3m"]
                if item["average_return_12m_3m"] is not None
                else Decimal("-9999")
            ),
            -(
                item["average_return_5y_3m"]
                if item["average_return_5y_3m"] is not None
                else Decimal("-9999")
            ),
            -item["distinct_days_3m"],
            -item["top3_3m"],
            item["average_rank_3m"] if item["average_rank_3m"] is not None else Decimal("999.9"),
            item["ticker"],
        )
    )
    top_non_owned_recommendation = next(
        (
            {
                "available": True,
                "ticker": row["ticker"],
                "company_name": row["company_name"],
                "entry_date": row.get("latest_buy_date"),
                "entry_date_label": row.get("latest_buy_date_label", ""),
                "entry_window_label": row.get("latest_buy_window_label", ""),
                "entry_price": row.get("latest_buy_price"),
                "buy_date": row.get("latest_buy_date"),
                "buy_date_label": row.get("latest_buy_date_label", ""),
                "buy_window_label": row.get("latest_buy_window_label", ""),
                "buy_price": row.get("latest_buy_price"),
                "average_buy_price": row.get("average_buy_price_3m"),
                "current_price": row.get("current_price"),
                "current_price_per_share": row.get("current_price"),
                "current_price_date": row.get("current_price_date"),
                "current_price_date_label": row.get("current_price_date_label", ""),
                "current_vs_entry_pct": row.get("current_vs_entry_pct"),
                "current_vs_exit_pct": row.get("current_vs_exit_pct"),
                "remaining_to_exit_pct": row.get("remaining_to_exit_pct"),
                "current_position_label": row.get("current_position_label", ""),
                "current_position_tone": row.get("current_position_tone", ""),
                "exit_date": row.get("latest_sell_date"),
                "exit_date_label": row.get("latest_sell_date_label", ""),
                "exit_window_label": row.get("latest_sell_window_label", ""),
                "exit_price": row.get("latest_sell_price"),
                "sell_date": row.get("latest_sell_date"),
                "sell_date_label": row.get("latest_sell_date_label", ""),
                "sell_window_label": row.get("latest_sell_window_label", ""),
                "sell_price": row.get("latest_sell_price"),
                "average_sell_price": row.get("average_sell_price_3m"),
                "interval_window_label": row.get("latest_interval_window_label", ""),
                "interval_return_pct": row.get("latest_interval_return_pct"),
                "average_interval_return_pct": row.get("average_interval_return_pct_3m"),
                "holding_annualized_return_pct": row.get("latest_annualized_return_pct"),
                "average_holding_annualized_return_pct": row.get("average_annualized_return_pct_3m"),
                "holding_months": row.get("latest_holding_months"),
                "average_holding_months": row.get("average_holding_months_3m"),
                "allocated_amount": row.get("latest_allocated_amount"),
                "average_allocated_amount": row.get("average_allocated_amount_3m"),
                "allocated_weight_pct": row.get("latest_allocated_weight_pct"),
                "average_allocated_weight_pct": row.get("average_allocated_weight_pct_3m"),
                "presence_pct_3m": row.get("presence_pct_3m"),
                "strategy_labels_3m_label": row.get("strategy_labels_3m_label", ""),
                "summary": (
                    f"Aparece en {row.get('presence_pct_3m') or Decimal('0'):.0f} % de las optimizaciones "
                    f"programadas y el ultimo tramo 12M propone entrar en "
                    f"{(row.get('latest_buy_window_label') or row.get('latest_buy_date_label') or '').lower()} "
                    f"y salir en {(row.get('latest_sell_window_label') or row.get('latest_sell_date_label') or '').lower()}"
                    f"{(' con %.1f %% anualizado' % row.get('latest_annualized_return_pct')) if row.get('latest_annualized_return_pct') is not None else ''}."
                ),
            }
            for row in rows
            if not row.get("currently_owned") and row.get("buy_recommendation_available")
        ),
        {"available": False},
    )
    return {
        "available": bool(rows),
        "rows": rows[:max_rows],
        "total_rows": len(rows),
        "top_non_owned_recommendation": top_non_owned_recommendation,
        "weekdays_label": weekdays_label,
        "next_run_date_label": next_run_date.isoformat() if next_run_date else "",
        "window_label": "ultimos 3 meses",
        "runs_count_3m": runs_count,
        "distinct_days_count_3m": len(distinct_days),
        "cutoff_date_label": cutoff_date.isoformat(),
        "policy": {
            "max_company_pct": SCHEDULED_OPTIMIZATION_MAX_COMPANY_PCT.quantize(Decimal("0")),
            "max_total_positions": SCHEDULED_OPTIMIZATION_MAX_TOTAL_POSITIONS,
            "max_sector_positions": SCHEDULED_OPTIMIZATION_MAX_SECTOR_POSITIONS,
        },
    }


def build_report_entries(plan: dict, history_cards: list[dict], positions: list[EquityPosition], news_signals: dict[str, dict]) -> list[dict]:
    history_card_map = {
        card["position"].id: card
        for card in history_cards
        if getattr(card["position"], "id", None)
    }
    entries = []
    for allocation in plan.get("allocations", []):
        position = allocation["position"]
        try:
            card = history_card_map.get(position.id) if position.id else None
            if card is None and allocation["status_key"] == "ibex":
                company, workbook_snapshot = find_ibex_universe_company(position.ticker)
                if company:
                    card = build_ibex_universe_card(
                        company,
                        positions,
                        workbook_snapshot=workbook_snapshot,
                        include_visuals=True,
                        include_reference_suggestions=True,
                    )
            if card is None:
                continue
            signal = news_signals.get(position.ticker, {})
            card["external_signal"] = signal
            entries.append(
                {
                    "allocation": allocation,
                    "card": card,
                    "external_signal": signal,
                }
            )
        except Exception:
            continue
    return entries


def build_allocation_weight_chart(allocations: list[dict]) -> str:
    chart_rows = [
        {
            "label": f'{item["position"].ticker} {item["position"].company_name[:24]}',
            "value": item["allocated_weight_pct"],
        }
        for item in allocations
    ]
    return build_svg_bar_chart(chart_rows, "value")


def build_sector_weight_chart(allocations: list[dict]) -> str:
    sector_totals: dict[str, Decimal] = {}
    for item in allocations:
        sector_label = item["sector_label"] or "Sin sector"
        sector_totals[sector_label] = sector_totals.get(sector_label, ZERO) + item["allocated_weight_pct"]
    chart_rows = [
        {"label": sector, "value": value}
        for sector, value in sorted(sector_totals.items(), key=lambda entry: entry[1], reverse=True)
    ]
    return build_svg_bar_chart(chart_rows, "value")


def build_report_html(run: EquityOptimizationRun, dashboard: dict, plan: dict, report_entries: list[dict], news_overview: dict) -> str:
    context = {
        "run": run,
        "dashboard": dashboard,
        "plan": plan,
        "report_entries": report_entries,
        "news_overview": news_overview,
        "weight_chart_svg": build_allocation_weight_chart(plan.get("allocations", [])),
        "sector_chart_svg": build_sector_weight_chart(plan.get("allocations", [])),
        "risk_return_chart_svg": build_svg_scatter_chart(plan.get("allocations", [])),
        "generated_at": timezone.localtime(run.completed_at or timezone.now()),
    }
    return render_to_string("equities/optimization_report.html", context)


def build_report_pdf_html(run: EquityOptimizationRun, dashboard: dict, plan: dict, report_entries: list[dict], news_overview: dict) -> str:
    context = {
        "run": run,
        "dashboard": dashboard,
        "plan": plan,
        "report_entries": report_entries,
        "news_overview": news_overview,
        "weight_chart_svg": build_allocation_weight_chart(plan.get("allocations", [])),
        "sector_chart_svg": build_sector_weight_chart(plan.get("allocations", [])),
        "risk_return_chart_svg": build_svg_scatter_chart(plan.get("allocations", [])),
        "generated_at": timezone.localtime(run.completed_at or timezone.now()),
    }
    return render_to_string("equities/optimization_report_pdf.html", context)


def build_fallback_report_pdf_html(run: EquityOptimizationRun) -> str:
    return render_to_string(
        "equities/optimization_report_pdf_fallback.html",
        {
            "run": run,
            "summary": run.summary_data or {},
            "allocations": run.allocations_data or [],
            "generated_at": timezone.localtime(run.completed_at or run.created_at or timezone.now()),
        },
    )


def render_report_pdf(report_html: str) -> bytes:
    if pisa is None:
        raise RuntimeError(
            "La exportacion PDF necesita xhtml2pdf. Instala requirements.txt o requirements-prod.txt antes de descargar."
        )
    output = BytesIO()
    result = pisa.CreatePDF(report_html, dest=output, encoding="utf-8")
    if result.err:
        raise ValueError("No se ha podido convertir el informe HTML a PDF.")
    return output.getvalue()


def mark_run_failed(run_id: int, exc: Exception) -> None:
    run = EquityOptimizationRun.objects.get(pk=run_id)
    progress_data = dict(run.progress_data or {})
    events = list(progress_data.get("events") or [])
    events.append(
        {
            "label": "La optimizacion ha fallado.",
            "detail": str(exc)[:180],
            "stage_key": progress_data.get("stage_key") or "report",
            "recorded_at": timezone.localtime().strftime("%H:%M:%S"),
        }
    )
    progress_data["note"] = "La optimizacion ha fallado."
    progress_data["stage_key"] = progress_data.get("stage_key") or "report"
    progress_data["stage_label"] = dict(PROGRESS_STAGE_ORDER).get(progress_data["stage_key"], "Error")
    progress_data["stages"] = progress_data.get("stages") or build_progress_stages(progress_data["stage_key"])
    progress_data["events"] = events[-8:]
    progress_data["updated_at_label"] = timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")
    run.status = EquityOptimizationRun.Status.FAILED
    run.status_note = "La optimizacion ha fallado."
    run.error_message = str(exc)
    run.completed_at = timezone.now()
    run.progress_data = progress_data
    run.save(update_fields=["status", "status_note", "error_message", "completed_at", "progress_data", "updated_at"])


def process_equity_optimization_run(run_id: int) -> EquityOptimizationRun:
    run = EquityOptimizationRun.objects.get(pk=run_id)
    if run.status == EquityOptimizationRun.Status.COMPLETED and run.report_html:
        return run
    existing_progress_data = dict(run.progress_data or {})
    preserved_metadata = {
        key: value
        for key, value in existing_progress_data.items()
        if key in {"strategy_mode", "strategy_label", "schedule_kind", "scheduled_run_key", "scheduled_analysis_date", "scheduled_weekdays_label"}
    }
    strategy_mode = existing_progress_data.get("strategy_mode") or OPTIMIZER_STRATEGY_12M_PRIMARY
    strategy = get_optimizer_strategy_config(strategy_mode)

    EquityOptimizationRun.objects.filter(pk=run_id).update(
        status=EquityOptimizationRun.Status.RUNNING,
        status_note="Preparando analisis",
        progress_data={
            **build_optimizer_progress_payload(strategy_mode, "Preparando analisis", percent=1),
            "stages": build_progress_stages("sync"),
            "events": [{"label": "Preparando analisis", "stage_key": "sync", "recorded_at": timezone.localtime().strftime("%H:%M:%S")}],
            **preserved_metadata,
        },
        started_at=timezone.now(),
        completed_at=None,
        error_message="",
        updated_at=timezone.now(),
    )
    run.refresh_from_db()

    try:
        clear_market_data_caches()
        update_run_progress(
            run_id,
            percent=6,
            stage_key="sync",
            note="Sincronizando posiciones guardadas",
        )
        positions = list(EquityPosition.objects.all())
        if positions:
            sync_all_equities_market_data(positions)
        positions = list(EquityPosition.objects.prefetch_related("price_history"))

        update_run_progress(
            run_id,
            percent=16,
            stage_key="dashboard",
            note="Construyendo base de mercado y cartera actual",
        )
        preview_candidates: list[dict] = []
        dashboard = build_dashboard_from_nightly_cache(
            positions,
            include_ibex_universe=True,
        )
        if dashboard is not None:
            update_run_progress(
                run_id,
                percent=56,
                stage_key="dashboard",
                note="Usando analisis nocturno guardado",
                preview_candidates=preview_candidates,
            )
        else:
            def ibex_progress_callback(completed_count, total_count, company, completed_cards, failures_count):
                percent = 22 + int((completed_count / max(total_count, 1)) * 34)
                update_run_progress(
                    run_id,
                    percent=percent,
                    stage_key="ibex",
                    note=f"Analizando el IBEX: {completed_count}/{total_count}",
                    current_step=completed_count,
                    total_steps=total_count,
                    current_label=company.get("company_name") or company.get("ticker", ""),
                    preview_candidates=preview_candidates,
                )

            dashboard = build_equity_analysis_dashboard(
                positions,
                include_ibex_universe=True,
                ibex_company_limit=None,
                ibex_progress_callback=ibex_progress_callback,
            )

        optimizer_cards = list(dashboard["optimizer_cards"])
        preview_candidates = build_optimizer_candidate_preview(optimizer_cards, strategy_mode=strategy_mode)
        update_run_progress(
            run_id,
            percent=58,
            stage_key="dashboard",
            note="Base de mercado completada",
            preview_candidates=preview_candidates,
        )

        def news_progress_callback(completed_count, total_count, card, signal):
            percent = 62 + int((completed_count / max(total_count, 1)) * 18)
            update_run_progress(
                run_id,
                percent=percent,
                stage_key="news",
                note=f"Leyendo prensa reciente: {completed_count}/{total_count}",
                current_step=completed_count,
                total_steps=total_count,
                current_label=card["position"].company_name,
                preview_candidates=preview_candidates,
            )

        update_run_progress(
            run_id,
            percent=62,
            stage_key="news",
            note="Iniciando lectura de prensa reciente",
            preview_candidates=preview_candidates,
        )
        news_signals = build_news_signal_map(optimizer_cards, progress_callback=news_progress_callback)
        for card in optimizer_cards:
            card["external_signal"] = news_signals.get(card["position"].ticker, {})

        update_run_progress(
            run_id,
            percent=84,
            stage_key="optimize",
            note=f"Calculando la cartera optima ({strategy['label']})",
            preview_candidates=build_optimizer_candidate_preview(optimizer_cards, strategy_mode=strategy_mode),
        )
        plan = build_equity_allocation_plan(
            optimizer_cards,
            run.total_investment,
            run.max_company_pct,
            run.max_total_positions,
            run.max_sector_positions,
            selected_sectors=run.selected_sectors,
            selected_owned_tickers=run.selected_owned_tickers,
            selected_owned_tickers_applied=run.selected_owned_tickers_applied,
            strategy_mode=strategy_mode,
        )
        preview_allocations = build_allocation_preview(plan)
        update_run_progress(
            run_id,
            percent=92,
            stage_key="report",
            note="Generando informe descargable",
            preview_candidates=build_optimizer_candidate_preview(optimizer_cards, strategy_mode=strategy_mode),
            preview_allocations=preview_allocations,
        )

        news_overview = build_news_overview(news_signals)
        report_entries = build_report_entries(plan, dashboard["history_cards"], positions, news_signals)
        run.refresh_from_db()
        run.summary_data = serialize_summary_data(run, plan, dashboard, news_overview)
        run.allocations_data = serialize_allocations_data(plan)
        run.status = EquityOptimizationRun.Status.COMPLETED
        run.status_note = "Optimizacion completada"
        run.completed_at = timezone.now()
        run.error_message = ""
        run.progress_data = {
            **dict(run.progress_data or {}),
            "percent": 100,
            "stage_key": "report",
            "stage_label": dict(PROGRESS_STAGE_ORDER)["report"],
            "note": "Optimizacion completada",
            "stages": build_progress_stages("report", finalized=True),
            "preview_candidates": build_optimizer_candidate_preview(optimizer_cards, strategy_mode=strategy_mode),
            "preview_allocations": preview_allocations,
            "updated_at_label": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
            "current_step": None,
            "total_steps": None,
            "current_label": "",
        }
        run.report_html = build_report_html(run, dashboard, plan, report_entries, news_overview)
        run.report_pdf_html = build_report_pdf_html(run, dashboard, plan, report_entries, news_overview)
        run.save(
            update_fields=[
                "progress_data",
                "summary_data",
                "allocations_data",
                "status",
                "status_note",
                "completed_at",
                "error_message",
                "report_html",
                "report_pdf_html",
                "updated_at",
            ]
        )
        return run
    except Exception as exc:
        mark_run_failed(run_id, exc)
        raise


def _run_job(run_id: int) -> None:
    try:
        process_equity_optimization_run(run_id)
    finally:
        with _queued_run_ids_lock:
            _queued_run_ids.discard(run_id)


def enqueue_equity_optimization_run(run_id: int) -> None:
    with _queued_run_ids_lock:
        if run_id in _queued_run_ids:
            return
        _queued_run_ids.add(run_id)
    get_optimizer_executor().submit(_run_job, run_id)


def launch_equity_optimization_run(
    *,
    total_investment: Decimal,
    max_company_pct: Decimal,
    max_total_positions: int,
    max_sector_positions: int,
    selected_sectors: list[str],
    selected_owned_tickers: list[str],
    selected_owned_tickers_applied: bool = False,
    requested_by=None,
    reference_label: str = "",
    restrictions_note: str = "",
    run_inline: bool = False,
    strategy_mode: str = OPTIMIZER_STRATEGY_12M_PRIMARY,
    reference_code: str | None = None,
    progress_metadata: dict | None = None,
) -> EquityOptimizationRun:
    strategy = get_optimizer_strategy_config(strategy_mode)
    run = EquityOptimizationRun.objects.create(
        reference_code=reference_code or generate_optimization_reference_code(suffix="12M" if strategy["mode"] == OPTIMIZER_STRATEGY_12M_PRIMARY else "5A"),
        label=build_optimizer_run_label(reference_label, strategy["mode"]),
        requested_by=requested_by,
        status=EquityOptimizationRun.Status.PENDING,
        status_note="Pendiente de analisis",
        total_investment=total_investment,
        max_company_pct=max_company_pct,
        max_total_positions=max_total_positions,
        max_sector_positions=max_sector_positions,
        selected_sectors=selected_sectors,
        selected_owned_tickers_applied=selected_owned_tickers_applied,
        selected_owned_tickers=selected_owned_tickers,
        restrictions_note=restrictions_note,
        progress_data={
            **build_optimizer_progress_payload(strategy["mode"], "Pendiente de analisis"),
            **(progress_metadata or {}),
        },
    )
    if run_inline:
        process_equity_optimization_run(run.id)
        run.refresh_from_db()
    else:
        enqueue_equity_optimization_run(run.id)
    return run


def launch_equity_optimization_run_pair(
    *,
    total_investment: Decimal,
    max_company_pct: Decimal,
    max_total_positions: int,
    max_sector_positions: int,
    selected_sectors: list[str],
    selected_owned_tickers: list[str],
    selected_owned_tickers_applied: bool = False,
    requested_by=None,
    reference_label: str = "",
    restrictions_note: str = "",
    run_inline: bool = False,
    progress_metadata: dict | None = None,
) -> list[EquityOptimizationRun]:
    family_code = generate_optimization_reference_family_code()
    runs = []
    for strategy_mode, suffix in (
        (OPTIMIZER_STRATEGY_12M_PRIMARY, "12M"),
        (OPTIMIZER_STRATEGY_5Y_PRIMARY, "5A"),
    ):
        runs.append(
            launch_equity_optimization_run(
                total_investment=total_investment,
                max_company_pct=max_company_pct,
                max_total_positions=max_total_positions,
                max_sector_positions=max_sector_positions,
                selected_sectors=selected_sectors,
                selected_owned_tickers=selected_owned_tickers,
                selected_owned_tickers_applied=selected_owned_tickers_applied,
                requested_by=requested_by,
                reference_label=reference_label,
                restrictions_note=restrictions_note,
                run_inline=run_inline,
                strategy_mode=strategy_mode,
                reference_code=generate_optimization_reference_code(family_code, suffix=suffix),
                progress_metadata=progress_metadata,
            )
        )
    return runs


def launch_scheduled_equity_optimization_runs(
    *,
    analysis_date: date | None = None,
    force: bool = False,
    run_inline: bool = False,
) -> list[EquityOptimizationRun]:
    analysis_date = analysis_date or timezone.localdate()
    purge_stale_scheduled_optimization_runs(as_of=analysis_date)
    if not should_launch_scheduled_optimizations(analysis_date=analysis_date, force=force):
        return []

    existing_runs = [
        run
        for run in load_existing_scheduled_optimization_runs(analysis_date)
        if scheduled_optimization_matches_policy(run)
    ]
    if existing_runs:
        reusable_runs = [
            run
            for run in existing_runs
            if run.status in {
                EquityOptimizationRun.Status.PENDING,
                EquityOptimizationRun.Status.RUNNING,
                EquityOptimizationRun.Status.COMPLETED,
            }
        ]
        if reusable_runs:
            return reusable_runs
        EquityOptimizationRun.objects.filter(id__in=[run.id for run in existing_runs]).delete()

    weekdays = scheduled_optimization_iso_weekdays()
    weekdays_label = build_scheduled_optimization_weekdays_label(weekdays)
    return launch_equity_optimization_run_pair(
        total_investment=resolve_scheduled_optimization_total_investment(),
        max_company_pct=SCHEDULED_OPTIMIZATION_MAX_COMPANY_PCT,
        max_total_positions=SCHEDULED_OPTIMIZATION_MAX_TOTAL_POSITIONS,
        max_sector_positions=SCHEDULED_OPTIMIZATION_MAX_SECTOR_POSITIONS,
        selected_sectors=[],
        selected_owned_tickers_applied=False,
        selected_owned_tickers=[],
        reference_label=f"Optimizacion programada {analysis_date.isoformat()}",
        restrictions_note=build_scheduled_optimization_note(analysis_date, weekdays_label),
        run_inline=run_inline,
        progress_metadata={
            "schedule_kind": "nightly",
            "scheduled_run_key": build_scheduled_optimization_run_key(analysis_date),
            "scheduled_analysis_date": analysis_date.isoformat(),
            "scheduled_weekdays_label": weekdays_label,
        },
    )


def resume_equity_optimization_runs() -> None:
    stale_before = timezone.now() - timedelta(minutes=RUN_STALE_MINUTES)
    stale_running_ids = list(
        EquityOptimizationRun.objects.filter(
            status=EquityOptimizationRun.Status.RUNNING,
            updated_at__lt=stale_before,
        ).values_list("id", flat=True)
    )
    if stale_running_ids:
        EquityOptimizationRun.objects.filter(id__in=stale_running_ids).update(
            status=EquityOptimizationRun.Status.PENDING,
            status_note="Reanudando optimizacion pendiente",
            updated_at=timezone.now(),
        )
    for run_id in EquityOptimizationRun.objects.filter(
        status=EquityOptimizationRun.Status.PENDING
    ).values_list("id", flat=True)[:3]:
        enqueue_equity_optimization_run(run_id)
