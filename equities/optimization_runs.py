from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from datetime import datetime, timedelta
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

from .models import EquityOptimizationRun, EquityPosition
from .services import (
    OPTIMIZER_STRATEGY_12M_PRIMARY,
    OPTIMIZER_STRATEGY_5Y_PRIMARY,
    build_equity_allocation_plan,
    build_equity_analysis_dashboard,
    build_equity_optimizer_candidate,
    build_ibex_universe_card,
    clear_market_data_caches,
    find_ibex_universe_company,
    get_optimizer_strategy_config,
    sync_all_equities_market_data,
)


ZERO = Decimal("0.00")
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
    candidates = [
        candidate
        for candidate in (build_equity_optimizer_candidate(card, strategy_mode) for card in cards)
        if candidate and candidate.get("optimization_score") is not None
    ]
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
    }
    run.status_note = note
    run.save(update_fields=["progress_data", "status_note", "updated_at"])


def serialize_summary_data(run: EquityOptimizationRun, plan: dict, dashboard: dict, news_overview: dict) -> dict:
    completed_at = timezone.localtime(run.completed_at) if run.completed_at else None
    created_at = timezone.localtime(run.created_at)
    top_pick = plan.get("top_pick")
    top_pick_name = top_pick["position"].company_name if top_pick else ""
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
        "projected_gain_total": float(plan.get("projected_gain_total", 0) or 0),
        "weighted_return_pct": float(plan.get("weighted_return_pct", 0) or 0) if plan.get("weighted_return_pct") is not None else None,
        "weighted_low_return_pct": float(plan.get("weighted_low_return_pct", 0) or 0) if plan.get("weighted_low_return_pct") is not None else None,
        "weighted_safety_score": float(plan.get("weighted_safety_score", 0) or 0) if plan.get("weighted_safety_score") is not None else None,
        "weighted_reliability_score": float(plan.get("weighted_reliability_score", 0) or 0) if plan.get("weighted_reliability_score") is not None else None,
        "weighted_cycle_return_annual_pct": float(plan.get("weighted_cycle_return_annual_pct", 0) or 0) if plan.get("weighted_cycle_return_annual_pct") is not None else None,
        "weighted_cycle_return_5y_pct": float(plan.get("weighted_cycle_return_5y_pct", 0) or 0) if plan.get("weighted_cycle_return_5y_pct") is not None else None,
        "net_dividend_income_total": float(plan.get("net_dividend_income_total", 0) or 0),
        "annual_cost_total": float(plan.get("annual_cost_total", 0) or 0),
        "roundtrip_cost_total": float(plan.get("roundtrip_cost_total", 0) or 0),
        "cash_reserve_amount": float(plan.get("cash_reserve_amount", 0) or 0),
        "allocations_count": len(plan.get("allocations", [])),
        "ibex_analyzed_count": dashboard.get("ibex_universe_summary", {}).get("analyzed_count", 0),
        "external_signal_used_count": plan.get("external_signal_used_count", 0),
        "max_company_pct": float(plan.get("max_company_pct", 0) or 0),
        "max_sector_positions": int(plan.get("max_sector_positions", 0) or 0),
        "top_pick_name": top_pick_name,
        "news_signals_count": news_overview["signals_count"],
        "news_items_count": news_overview["items_count"],
        "positive_news_count": news_overview["positive_count"],
        "negative_news_count": news_overview["negative_count"],
        "neutral_news_count": news_overview["neutral_count"],
        "methodology_note": plan.get("methodology_note", ""),
    }


def serialize_allocations_data(plan: dict) -> list[dict]:
    items = []
    for item in plan.get("allocations", []):
        position = item["position"]
        items.append(
            {
                "rank": item["rank"],
                "company_name": position.company_name,
                "ticker": position.ticker,
                "sector_label": item["sector_label"],
                "status_label": item["status_label"],
                "trade_alert_label": item["trade_alert_label"],
                "reference_label": item["reference_label"],
                "strategy_label": item.get("strategy_label", ""),
                "optimization_score": float(item["optimization_score"]),
                "allocated_weight_pct": float(item["allocated_weight_pct"]),
                "allocated_amount": float(item["allocated_amount"]),
                "net_projected_return_pct": float(item["net_projected_return_pct"]),
                "low_return_pct": float(item["low_return_pct"]),
                "blended_return_signal_pct": float(item["blended_return_signal_pct"]),
                "cycle_return_annual_pct": float(item["cycle_return_annual_pct"]) if item.get("cycle_return_annual_pct") is not None else None,
                "cycle_return_5y_pct": float(item["cycle_return_5y_pct"]) if item.get("cycle_return_5y_pct") is not None else None,
                "cycle_support_score": float(item["cycle_support_score"]),
                "expected_net_dividend_income": float(item["expected_net_dividend_income"]),
                "annual_cost_used": float(item["annual_cost_used"]),
                "roundtrip_total_cost": float(item["roundtrip_total_cost"]),
                "safety_score": float(item["safety_score"]),
                "reliability_label": item["reliability_label"],
                "external_signal_label": item.get("external_signal_label", ""),
                "external_signal_score": float(item.get("external_signal_score", 0) or 0),
            }
        )
    return items


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
    strategy_mode = (run.progress_data or {}).get("strategy_mode") or OPTIMIZER_STRATEGY_12M_PRIMARY
    strategy = get_optimizer_strategy_config(strategy_mode)

    EquityOptimizationRun.objects.filter(pk=run_id).update(
        status=EquityOptimizationRun.Status.RUNNING,
        status_note="Preparando analisis",
        progress_data={
            **build_optimizer_progress_payload(strategy_mode, "Preparando analisis", percent=1),
            "stages": build_progress_stages("sync"),
            "events": [{"label": "Preparando analisis", "stage_key": "sync", "recorded_at": timezone.localtime().strftime("%H:%M:%S")}],
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
    requested_by=None,
    reference_label: str = "",
    restrictions_note: str = "",
    run_inline: bool = False,
    strategy_mode: str = OPTIMIZER_STRATEGY_12M_PRIMARY,
    reference_code: str | None = None,
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
        restrictions_note=restrictions_note,
        progress_data=build_optimizer_progress_payload(strategy["mode"], "Pendiente de analisis"),
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
    requested_by=None,
    reference_label: str = "",
    restrictions_note: str = "",
    run_inline: bool = False,
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
                requested_by=requested_by,
                reference_label=reference_label,
                restrictions_note=restrictions_note,
                run_inline=run_inline,
                strategy_mode=strategy_mode,
                reference_code=generate_optimization_reference_code(family_code, suffix=suffix),
            )
        )
    return runs


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
