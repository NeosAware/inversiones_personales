from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from functools import lru_cache
import html
import re
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from django.conf import settings
from django.utils import timezone


ZERO = Decimal("0.00")
DEFAULT_COMPANY_LOOKBACK_DAYS = 12
DEFAULT_SECTOR_LOOKBACK_DAYS = 10
DEFAULT_MARKET_LOOKBACK_DAYS = 7
DEFAULT_MAX_ITEMS = 4
DEFAULT_TIMEOUT_SECONDS = 10

POSITIVE_NEWS_TOKENS = {
    "sube": Decimal("1.0"),
    "subida": Decimal("1.0"),
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
    "crisis": Decimal("1.2"),
    "guerra": Decimal("1.4"),
    "ataque": Decimal("1.4"),
    "bombarde": Decimal("1.7"),
    "misil": Decimal("1.5"),
    "tension": Decimal("0.9"),
    "petroleo dispara": Decimal("1.0"),
    "arancel": Decimal("1.0"),
}

NEWS_TAG_RULES = {
    "geopolitica": ("iran", "israel", "eeuu", "estados unidos", "guerra", "ataque", "bombarde", "misil", "ormuz"),
    "tipos": ("bce", "fed", "tipos", "euribor", "inflacion", "hipoteca"),
    "energia": ("energia", "petroleo", "crudo", "gas", "electricidad", "renovable", "solar", "eolica"),
    "vivienda": ("vivienda", "hipoteca", "inmobiliari", "promotora", "casa", "casas"),
    "regulacion": ("cnmv", "bruselas", "comision europea", "regulacion", "ley", "impuesto", "fiscal"),
    "resultados": ("beneficio", "ebitda", "ventas", "resultados", "guidance", "dividendo", "warning"),
    "contratos": ("contrato", "contratos", "adjudica", "pedido", "concesion"),
}


def nightly_llm_news_enabled() -> bool:
    return bool(getattr(settings, "EQUITIES_NIGHTLY_LLM_INCLUDE_NEWS", True))


def nightly_llm_news_shock_refresh_enabled() -> bool:
    return bool(getattr(settings, "EQUITIES_NIGHTLY_LLM_NEWS_SHOCK_REFRESH_ENABLED", True))


def nightly_llm_company_news_lookback_days() -> int:
    return max(int(getattr(settings, "EQUITIES_NIGHTLY_LLM_COMPANY_NEWS_LOOKBACK_DAYS", DEFAULT_COMPANY_LOOKBACK_DAYS) or DEFAULT_COMPANY_LOOKBACK_DAYS), 1)


def nightly_llm_sector_news_lookback_days() -> int:
    return max(int(getattr(settings, "EQUITIES_NIGHTLY_LLM_SECTOR_NEWS_LOOKBACK_DAYS", DEFAULT_SECTOR_LOOKBACK_DAYS) or DEFAULT_SECTOR_LOOKBACK_DAYS), 1)


def nightly_llm_market_news_lookback_days() -> int:
    return max(int(getattr(settings, "EQUITIES_NIGHTLY_LLM_MARKET_NEWS_LOOKBACK_DAYS", DEFAULT_MARKET_LOOKBACK_DAYS) or DEFAULT_MARKET_LOOKBACK_DAYS), 1)


def nightly_llm_news_max_items() -> int:
    return max(int(getattr(settings, "EQUITIES_NIGHTLY_LLM_NEWS_MAX_ITEMS", DEFAULT_MAX_ITEMS) or DEFAULT_MAX_ITEMS), 1)


def nightly_llm_news_request_timeout_seconds() -> int:
    return max(int(getattr(settings, "EQUITIES_NIGHTLY_LLM_NEWS_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS) or DEFAULT_TIMEOUT_SECONDS), 3)


def quantize_score(value: Decimal | None) -> Decimal:
    return Decimal(str(value or ZERO)).quantize(Decimal("0.01"))


def normalize_news_text(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def strip_html_tags(value: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(html.unescape(cleaned).split())


def parse_news_date(value: str):
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


def detect_news_tags(title: str, description: str) -> list[str]:
    text = normalize_news_text(f"{title} {description}")
    hits = []
    for tag, tokens in NEWS_TAG_RULES.items():
        if any(token in text for token in tokens):
            hits.append(tag)
    return hits


def news_item_score(title: str, description: str, published_at) -> Decimal:
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
        if age_days <= 2:
            score *= Decimal("1.25")
        elif age_days <= 5:
            score *= Decimal("1.10")
        elif age_days <= 10:
            score *= Decimal("1.00")
        else:
            score *= Decimal("0.80")
    return score


def build_news_rss_url(query: str, *, lookback_days: int) -> str:
    full_query = f"{query} when:{max(int(lookback_days or 1), 1)}d"
    return f"https://news.google.com/rss/search?q={quote_plus(full_query)}&hl=es-419&gl=ES&ceid=ES:es-419"


def news_cache_bucket(now=None) -> int:
    current = now or timezone.now()
    return int(current.timestamp() // (6 * 60 * 60))


@lru_cache(maxsize=256)
def _fetch_google_news_xml_cached(url: str, bucket: int, timeout_seconds: int) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.read().decode("utf-8", errors="ignore")


def build_unavailable_news_signal(label: str, note: str) -> dict:
    return {
        "available": False,
        "label": label,
        "score": ZERO,
        "items_count": 0,
        "positive_count": 0,
        "negative_count": 0,
        "neutral_count": 0,
        "items": [],
        "top_tags": [],
        "note": note,
    }


def summarize_news_signal(items: list[dict], *, label: str) -> dict:
    if not items:
        return build_unavailable_news_signal(label, "No se han encontrado titulares recientes suficientes.")

    score_total = sum((Decimal(str(item.get("score") or ZERO)) for item in items), ZERO)
    average_score = score_total / Decimal(len(items))
    signal_score = max(Decimal("-10.00"), min(Decimal("10.00"), average_score * Decimal("2.40")))
    positive_count = sum(1 for item in items if Decimal(str(item.get("score") or ZERO)) > ZERO)
    negative_count = sum(1 for item in items if Decimal(str(item.get("score") or ZERO)) < ZERO)
    neutral_count = len(items) - positive_count - negative_count
    tag_counts: dict[str, int] = {}
    for item in items:
        for tag in item.get("tags") or []:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    top_tags = [
        tag
        for tag, _ in sorted(tag_counts.items(), key=lambda item: (-item[1], item[0]))
    ][:4]

    if signal_score >= Decimal("2.00"):
        signal_label = "Favorable"
        note = "Los titulares recientes apoyan un escenario de continuidad o mejora."
    elif signal_score <= Decimal("-2.00"):
        signal_label = "Adversa"
        note = "Los titulares recientes anaden riesgo o ruido adicional sobre esta lectura."
    else:
        signal_label = "Neutra"
        note = "La prensa reciente no altera demasiado la lectura cuantitativa base."

    return {
        "available": True,
        "label": f"{label} {signal_label}".strip(),
        "score": quantize_score(signal_score),
        "items_count": len(items),
        "positive_count": positive_count,
        "negative_count": negative_count,
        "neutral_count": neutral_count,
        "items": items,
        "top_tags": top_tags,
        "note": note,
    }


def fetch_news_signal_for_query(query: str, *, label: str, lookback_days: int, max_items: int) -> dict:
    url = build_news_rss_url(query, lookback_days=lookback_days)
    xml_text = _fetch_google_news_xml_cached(
        url,
        news_cache_bucket(),
        nightly_llm_news_request_timeout_seconds(),
    )
    root = ET.fromstring(xml_text)
    items = []
    seen_titles = set()
    for item in root.findall(".//item"):
        title = strip_html_tags(item.findtext("title", ""))
        normalized_title = normalize_news_text(title)
        if not title or normalized_title in seen_titles:
            continue
        seen_titles.add(normalized_title)
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
                "score": quantize_score(score),
                "tone": "positive" if score > ZERO else "negative" if score < ZERO else "neutral",
                "tags": detect_news_tags(title, description),
            }
        )
        if len(items) >= max(int(max_items or 1), 1):
            break
    return summarize_news_signal(items, label=label)


def build_company_news_query(company_name: str, ticker: str, sector_label: str = "") -> str:
    query_parts = [f'"{company_name}"']
    if ticker:
        query_parts.append(f'"{ticker}"')
    query_parts.append("(IBEX OR bolsa OR acciones)")
    if sector_label:
        query_parts.append(f'"{sector_label}"')
    return " ".join(part for part in query_parts if part)


def build_sector_news_query(sector_label: str) -> str:
    query_parts = [
        f'"{sector_label}"',
        "(IBEX OR bolsa OR acciones OR mercado)",
        "(regulacion OR resultados OR demanda OR precios OR tipos OR energia OR inflacion OR geopolitica)",
    ]
    return " ".join(part for part in query_parts if part)


def build_market_news_query() -> str:
    return (
        '(IBEX 35 OR "bolsa espanola" OR "mercado europeo") '
        '(BCE OR euribor OR inflacion OR tipos OR petroleo OR Iran OR guerra OR aranceles OR sanciones)'
    )


def fetch_company_news_signal(company_name: str, ticker: str, sector_label: str = "") -> dict:
    return fetch_news_signal_for_query(
        build_company_news_query(company_name, ticker, sector_label),
        label="Empresa",
        lookback_days=nightly_llm_company_news_lookback_days(),
        max_items=nightly_llm_news_max_items(),
    )


def fetch_sector_news_signal(sector_label: str) -> dict:
    if not str(sector_label or "").strip():
        return build_unavailable_news_signal("Sector", "La compania no tiene sector asociado para ampliar la busqueda.")
    return fetch_news_signal_for_query(
        build_sector_news_query(sector_label),
        label="Sector",
        lookback_days=nightly_llm_sector_news_lookback_days(),
        max_items=max(2, nightly_llm_news_max_items() - 1),
    )


def fetch_market_news_signal() -> dict:
    return fetch_news_signal_for_query(
        build_market_news_query(),
        label="Mercado",
        lookback_days=nightly_llm_market_news_lookback_days(),
        max_items=max(3, nightly_llm_news_max_items()),
    )


def sort_news_items(items: list[dict]) -> list[dict]:
    def item_key(item: dict):
        published_at = item.get("published_at")
        timestamp = published_at.timestamp() if published_at is not None else 0
        score = abs(Decimal(str(item.get("score") or ZERO)))
        return (score, timestamp)

    return sorted(items, key=item_key, reverse=True)


def merge_news_items(*item_groups: list[dict], max_items: int = 6) -> list[dict]:
    merged = []
    seen_titles = set()
    for group in item_groups:
        for item in group or []:
            normalized_title = normalize_news_text(item.get("title", ""))
            if not normalized_title or normalized_title in seen_titles:
                continue
            seen_titles.add(normalized_title)
            merged.append(item)
    return sort_news_items(merged)[: max(int(max_items or 1), 1)]


def detect_material_news_event(company_signal: dict, sector_signal: dict, market_signal: dict, top_items: list[dict]) -> tuple[bool, str, list[str]]:
    top_tags = []
    tag_counts: dict[str, int] = {}
    for item in top_items:
        for tag in item.get("tags") or []:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    top_tags = [tag for tag, _ in sorted(tag_counts.items(), key=lambda item: (-item[1], item[0]))][:4]

    signal_scores = [
        abs(Decimal(str(signal.get("score") or ZERO)))
        for signal in (company_signal, sector_signal, market_signal)
        if signal.get("available")
    ]
    max_signal_score = max(signal_scores, default=ZERO)
    recent_negative_count = sum(
        1
        for item in top_items
        if item.get("tone") == "negative"
        and item.get("published_at") is not None
        and item["published_at"] >= timezone.now() - timedelta(days=4)
    )
    geopolitical_recent = any(
        "geopolitica" in (item.get("tags") or [])
        and item.get("published_at") is not None
        and item["published_at"] >= timezone.now() - timedelta(days=5)
        for item in top_items
    )
    material_event = bool(
        max_signal_score >= Decimal("4.00")
        or recent_negative_count >= 3
        or geopolitical_recent
    )
    if not material_event:
        return False, "", top_tags
    if geopolitical_recent:
        return True, "Se detecta un evento geopolitico reciente con capacidad de alterar el escenario base.", top_tags
    if max_signal_score >= Decimal("4.00"):
        return True, "La intensidad de la prensa reciente es demasiado alta como para ignorarla en la lectura nocturna.", top_tags
    return True, "Se acumulan varias senales recientes adversas y conviene releer la tesis con contexto web.", top_tags


def build_card_news_context(company_signal: dict, sector_signal: dict, market_signal: dict) -> dict:
    top_items = merge_news_items(
        company_signal.get("items") or [],
        sector_signal.get("items") or [],
        market_signal.get("items") or [],
        max_items=6,
    )
    company_score = Decimal(str(company_signal.get("score") or ZERO))
    sector_score = Decimal(str(sector_signal.get("score") or ZERO))
    market_score = Decimal(str(market_signal.get("score") or ZERO))
    aggregate_score = (company_score * Decimal("0.55")) + (sector_score * Decimal("0.25")) + (market_score * Decimal("0.20"))
    material_event, material_note, top_tags = detect_material_news_event(
        company_signal,
        sector_signal,
        market_signal,
        top_items,
    )
    if aggregate_score >= Decimal("1.80"):
        label = "Contexto favorable"
    elif aggregate_score <= Decimal("-1.80"):
        label = "Contexto adverso"
    else:
        label = "Contexto mixto"
    note_bits = []
    if company_signal.get("available"):
        note_bits.append(f"Empresa: {company_signal.get('label', '').lower()}")
    if sector_signal.get("available"):
        note_bits.append(f"Sector: {sector_signal.get('label', '').lower()}")
    if market_signal.get("available"):
        note_bits.append(f"Mercado: {market_signal.get('label', '').lower()}")
    note = " | ".join(note_bits) if note_bits else "Sin contexto web suficiente para modular la tesis."
    return {
        "available": bool(company_signal.get("available") or sector_signal.get("available") or market_signal.get("available")),
        "label": label,
        "score": quantize_score(aggregate_score),
        "note": note,
        "items_count": len(top_items),
        "top_tags": top_tags,
        "material_event": material_event,
        "material_note": material_note,
        "company_signal": company_signal,
        "sector_signal": sector_signal,
        "market_signal": market_signal,
        "top_items": top_items,
        "captured_at": timezone.localtime().isoformat(),
        "captured_at_label": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
    }


def build_company_news_signal_map(cards: list[dict]) -> dict[str, dict]:
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
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="nightly-company-news") as executor:
        future_map = {
            executor.submit(
                fetch_company_news_signal,
                card["position"].company_name,
                card["position"].ticker,
                card.get("sector_label", ""),
            ): card
            for card in cards_to_analyze
        }
        for future in as_completed(future_map):
            card = future_map[future]
            ticker = str(card["position"].ticker or "").strip().upper()
            try:
                results[ticker] = future.result()
            except Exception as exc:
                results[ticker] = build_unavailable_news_signal("Empresa", f"No se ha podido leer la prensa reciente: {exc}")
    return results


def build_sector_news_signal_map(cards: list[dict]) -> dict[str, dict]:
    sectors = sorted({str(card.get("sector_label") or "").strip() for card in cards if str(card.get("sector_label") or "").strip()})
    results: dict[str, dict] = {}
    if not sectors:
        return results

    max_workers = min(4, len(sectors))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="nightly-sector-news") as executor:
        future_map = {
            executor.submit(fetch_sector_news_signal, sector_label): sector_label
            for sector_label in sectors
        }
        for future in as_completed(future_map):
            sector_label = future_map[future]
            try:
                results[sector_label] = future.result()
            except Exception as exc:
                results[sector_label] = build_unavailable_news_signal("Sector", f"No se ha podido leer la prensa sectorial: {exc}")
    return results


def build_llm_news_context_map(cards: list[dict]) -> tuple[dict[str, dict], dict]:
    context_map: dict[str, dict] = {}
    if not nightly_llm_news_enabled():
        return context_map, {
            "enabled": False,
            "signals_count": 0,
            "items_count": 0,
            "material_event_count": 0,
        }

    cards = list(cards or [])
    if not cards:
        return context_map, {
            "enabled": True,
            "signals_count": 0,
            "items_count": 0,
            "material_event_count": 0,
        }

    company_signals = build_company_news_signal_map(cards)
    sector_signals = build_sector_news_signal_map(cards)
    try:
        market_signal = fetch_market_news_signal()
    except Exception as exc:
        market_signal = build_unavailable_news_signal("Mercado", f"No se ha podido leer la prensa macro: {exc}")

    seen_tickers = set()
    for card in cards:
        ticker = str(card["position"].ticker or "").strip().upper()
        if not ticker or ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        sector_label = str(card.get("sector_label") or "").strip()
        company_signal = company_signals.get(ticker) or build_unavailable_news_signal("Empresa", "Sin prensa de empresa.")
        sector_signal = sector_signals.get(sector_label) or build_unavailable_news_signal("Sector", "Sin prensa sectorial.")
        context_map[ticker] = build_card_news_context(company_signal, sector_signal, market_signal)

    summary = {
        "enabled": True,
        "signals_count": len(context_map),
        "items_count": sum(int(context.get("items_count") or 0) for context in context_map.values()),
        "material_event_count": sum(1 for context in context_map.values() if context.get("material_event")),
        "market_signal_available": bool(market_signal.get("available")),
    }
    return context_map, summary


def attach_llm_news_context_to_dashboard(dashboard: dict) -> dict:
    cards = [
        *((dashboard.get("history_cards") or [])),
        *((dashboard.get("ibex_universe_cards") or [])),
    ]
    context_map, summary = build_llm_news_context_map(cards)
    for card in cards:
        ticker = str(card["position"].ticker or "").strip().upper()
        card["news_context"] = context_map.get(ticker) or {
            "available": False,
            "label": "Sin contexto web",
            "score": ZERO,
            "note": "No hay contexto web reciente asociado a esta accion.",
            "items_count": 0,
            "top_tags": [],
            "material_event": False,
            "material_note": "",
            "company_signal": build_unavailable_news_signal("Empresa", "Sin lectura."),
            "sector_signal": build_unavailable_news_signal("Sector", "Sin lectura."),
            "market_signal": build_unavailable_news_signal("Mercado", "Sin lectura."),
            "top_items": [],
            "captured_at": "",
            "captured_at_label": "",
        }
    dashboard["news_summary"] = summary
    return summary
