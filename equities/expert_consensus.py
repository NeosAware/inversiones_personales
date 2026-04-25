from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from django.conf import settings
from django.utils import timezone

from .models import EquityNightlyAnalysisSnapshot
from .news_context import (
    _fetch_google_news_xml_cached,
    build_news_rss_url,
    news_cache_bucket,
    normalize_news_text,
    parse_news_date,
    quantize_score,
    strip_html_tags,
)
from .services import (
    DEFAULT_BENCHMARK_NAME,
    DEFAULT_BENCHMARK_SYMBOL,
    MAX_MARKET_RANGE_KEY,
    ZERO,
    clamp_decimal,
    fetch_market_series,
    percentage_change,
    quantize_decimal,
    read_pdf_pages,
)


JSON_TYPE_KEY = "__equity_cached_type__"
DEFAULT_EXPERT_COMPANY_LOOKBACK_DAYS = 45
DEFAULT_EXPERT_MARKET_LOOKBACK_DAYS = 30
DEFAULT_EXPERT_MAX_ITEMS = 5
DEFAULT_EXPERT_HISTORY_HORIZON_DAYS = 120
DEFAULT_EXPERT_HISTORY_LOOKBACK_DAYS = 365 * 5
DEFAULT_EXPERT_SOURCE_OBSERVATIONS_TARGET = 10
DEFAULT_EXPERT_TIMEOUT_SECONDS = 10
DEFAULT_BRIDGEWATER_MAX_REPORTS = 6
DEFAULT_BRIDGEWATER_SCAN_LIMIT = 40
WALL_STREET_INDEX_SPECS = (
    ("^GSPC", "S&P 500"),
    ("^IXIC", "Nasdaq Composite"),
)
BRIDGEWATER_NAME_HINTS = (
    "bridgewater",
    "ray dalio",
    "all weather",
    "pure alpha",
    "daily observations",
    "daily observation",
)
BRIDGEWATER_MARKET_POSITIVE_TOKENS = {
    "soft landing": Decimal("1.25"),
    "disinflation": Decimal("0.85"),
    "productivity": Decimal("0.70"),
    "earnings growth": Decimal("1.00"),
    "risk assets": Decimal("0.75"),
    "equities attractive": Decimal("1.10"),
    "overweight equities": Decimal("1.20"),
    "improving growth": Decimal("0.95"),
    "liquidity tailwind": Decimal("0.80"),
    "bullish": Decimal("0.85"),
}
BRIDGEWATER_MARKET_NEGATIVE_TOKENS = {
    "recession": Decimal("1.20"),
    "hard landing": Decimal("1.35"),
    "stagflation": Decimal("1.30"),
    "tightening": Decimal("0.70"),
    "deleveraging": Decimal("0.90"),
    "margin pressure": Decimal("0.75"),
    "bearish": Decimal("0.85"),
    "valuation risk": Decimal("0.75"),
    "risk premia": Decimal("0.65"),
    "credit stress": Decimal("1.00"),
}
BRIDGEWATER_WALL_STREET_FOCUS_TOKENS = (
    "wall street",
    "u.s. equities",
    "us equities",
    "s&p 500",
    "nasdaq",
    "u.s. stocks",
    "american equities",
)

FORECAST_RELEVANCE_TOKENS = (
    "analista",
    "analistas",
    "estratega",
    "estrategas",
    "consenso",
    "precio objetivo",
    "target price",
    "recomendacion",
    "recomendaciones",
    "sobreponderar",
    "infraponderar",
    "overweight",
    "underweight",
    "equal weight",
    "market perform",
    "outperform",
    "underperform",
    "buy",
    "sell",
    "neutral",
    "forecast",
    "outlook",
    "prevision",
    "previsiones",
    "pronostico",
    "guidance",
    "potencial",
    "upside",
    "downside",
)

POSITIVE_FORECAST_TOKENS = {
    "compra": Decimal("1.25"),
    "comprar": Decimal("1.35"),
    "buy": Decimal("1.35"),
    "sobreponderar": Decimal("1.45"),
    "overweight": Decimal("1.45"),
    "outperform": Decimal("1.25"),
    "positivo": Decimal("1.00"),
    "alcista": Decimal("1.00"),
    "potencial": Decimal("0.70"),
    "upside": Decimal("0.70"),
    "mejora recomendacion": Decimal("1.10"),
    "sube precio objetivo": Decimal("1.15"),
    "eleva precio objetivo": Decimal("1.15"),
}

NEGATIVE_FORECAST_TOKENS = {
    "venta": Decimal("1.25"),
    "vender": Decimal("1.35"),
    "sell": Decimal("1.35"),
    "infraponderar": Decimal("1.45"),
    "underweight": Decimal("1.45"),
    "underperform": Decimal("1.25"),
    "negativo": Decimal("1.00"),
    "bajista": Decimal("1.00"),
    "downside": Decimal("0.70"),
    "riesgo bajista": Decimal("0.85"),
    "rebaja recomendacion": Decimal("1.10"),
    "recorta precio objetivo": Decimal("1.15"),
    "reduce precio objetivo": Decimal("1.15"),
}

NEUTRAL_FORECAST_TOKENS = (
    "mantener",
    "hold",
    "neutral",
    "equal weight",
    "market perform",
)

UPSIDE_REGEX = re.compile(
    r"(upside|potencial|revalorizacion|subida|alza)[^%]{0,24}?(?P<value>\d+(?:[.,]\d+)?)\s*%",
    re.IGNORECASE,
)
DOWNSIDE_REGEX = re.compile(
    r"(downside|caida|bajada|recorte|riesgo bajista)[^%]{0,24}?(?P<value>\d+(?:[.,]\d+)?)\s*%",
    re.IGNORECASE,
)
SOURCE_KEY_SANITIZER = re.compile(r"[^a-z0-9]+")

EXPERT_SOURCE_ALIASES = {
    "Goldman Sachs": ("goldman sachs", "goldman"),
    "JPMorgan": ("jpmorgan", "jp morgan", "j.p. morgan"),
    "Morgan Stanley": ("morgan stanley",),
    "Bank of America": ("bank of america", "bofa", "bofa securities"),
    "UBS": ("ubs",),
    "Citi": ("citi", "citigroup"),
    "Barclays": ("barclays",),
    "Deutsche Bank": ("deutsche bank",),
    "BNP Paribas Exane": ("bnp paribas exane", "exane bnp", "exane"),
    "Jefferies": ("jefferies",),
    "Oddo BHF": ("oddo bhf", "oddo"),
    "Kepler Cheuvreux": ("kepler cheuvreux", "kepler"),
    "Redburn": ("redburn",),
    "Mediobanca": ("mediobanca",),
    "Mirabaud": ("mirabaud",),
    "RBC": ("rbc", "royal bank of canada"),
    "Stifel": ("stifel",),
    "Alantra": ("alantra",),
    "Renta 4": ("renta 4", "renta4"),
    "Bankinter": ("bankinter",),
    "Bestinver": ("bestinver",),
    "Santander": ("santander", "banco santander"),
    "BBVA": ("bbva",),
    "Sabadell": ("sabadell", "banco sabadell"),
    "CaixaBank BPI": ("caixabank bpi", "bpi"),
    "Morningstar": ("morningstar",),
    "Bloomberg": ("bloomberg",),
    "Reuters": ("reuters",),
    "Expansion": ("expansion",),
    "Cinco Dias": ("cinco dias",),
    "El Economista": ("el economista",),
    "Investing.com": ("investing.com", "investing"),
    "MarketScreener": ("marketscreener",),
    "Bolsamania": ("bolsamania",),
    "Financial Times": ("financial times", "ft"),
    "Wall Street Journal": ("wall street journal", "wsj"),
}


def nightly_expert_consensus_enabled() -> bool:
    return bool(getattr(settings, "EQUITIES_NIGHTLY_EXPERT_CONSENSUS_ENABLED", True))


def nightly_expert_company_lookback_days() -> int:
    return max(
        int(
            getattr(
                settings,
                "EQUITIES_NIGHTLY_EXPERT_COMPANY_LOOKBACK_DAYS",
                DEFAULT_EXPERT_COMPANY_LOOKBACK_DAYS,
            )
            or DEFAULT_EXPERT_COMPANY_LOOKBACK_DAYS
        ),
        3,
    )


def nightly_expert_market_lookback_days() -> int:
    return max(
        int(
            getattr(
                settings,
                "EQUITIES_NIGHTLY_EXPERT_MARKET_LOOKBACK_DAYS",
                DEFAULT_EXPERT_MARKET_LOOKBACK_DAYS,
            )
            or DEFAULT_EXPERT_MARKET_LOOKBACK_DAYS
        ),
        3,
    )


def nightly_expert_max_items() -> int:
    return max(
        int(getattr(settings, "EQUITIES_NIGHTLY_EXPERT_MAX_ITEMS", DEFAULT_EXPERT_MAX_ITEMS) or DEFAULT_EXPERT_MAX_ITEMS),
        1,
    )


def nightly_expert_history_horizon_days() -> int:
    return max(
        int(
            getattr(
                settings,
                "EQUITIES_NIGHTLY_EXPERT_HISTORY_HORIZON_DAYS",
                DEFAULT_EXPERT_HISTORY_HORIZON_DAYS,
            )
            or DEFAULT_EXPERT_HISTORY_HORIZON_DAYS
        ),
        30,
    )


def nightly_expert_history_lookback_days() -> int:
    return max(
        int(
            getattr(
                settings,
                "EQUITIES_NIGHTLY_EXPERT_HISTORY_LOOKBACK_DAYS",
                DEFAULT_EXPERT_HISTORY_LOOKBACK_DAYS,
            )
            or DEFAULT_EXPERT_HISTORY_LOOKBACK_DAYS
        ),
        nightly_expert_history_horizon_days() + 30,
    )


def nightly_expert_source_observations_target() -> int:
    return max(
        int(
            getattr(
                settings,
                "EQUITIES_NIGHTLY_EXPERT_SOURCE_OBSERVATIONS_TARGET",
                DEFAULT_EXPERT_SOURCE_OBSERVATIONS_TARGET,
            )
            or DEFAULT_EXPERT_SOURCE_OBSERVATIONS_TARGET
        ),
        3,
    )


def nightly_expert_timeout_seconds() -> int:
    return max(
        int(getattr(settings, "EQUITIES_NIGHTLY_EXPERT_TIMEOUT_SECONDS", DEFAULT_EXPERT_TIMEOUT_SECONDS) or DEFAULT_EXPERT_TIMEOUT_SECONDS),
        3,
    )


def nightly_bridgewater_reports_enabled() -> bool:
    return bool(getattr(settings, "EQUITIES_NIGHTLY_BRIDGEWATER_REPORTS_ENABLED", True))


def nightly_bridgewater_max_reports() -> int:
    return max(
        int(getattr(settings, "EQUITIES_NIGHTLY_BRIDGEWATER_MAX_REPORTS", DEFAULT_BRIDGEWATER_MAX_REPORTS) or DEFAULT_BRIDGEWATER_MAX_REPORTS),
        1,
    )


def nightly_bridgewater_scan_limit() -> int:
    return max(
        int(getattr(settings, "EQUITIES_BRIDGEWATER_SCAN_LIMIT", DEFAULT_BRIDGEWATER_SCAN_LIMIT) or DEFAULT_BRIDGEWATER_SCAN_LIMIT),
        nightly_bridgewater_max_reports(),
    )


def resolve_bridgewater_default_dirs() -> list[Path]:
    base_dir = Path(str(getattr(settings, "BASE_DIR", Path.cwd())))
    return [
        base_dir / "data" / "bridgewater_reports",
        base_dir / "private" / "bridgewater_reports",
        Path.home() / "Downloads" / "bridgewater",
        Path.home() / "Documents" / "bridgewater",
        Path.home() / "Desktop" / "bridgewater",
        Path.home() / "Downloads",
        Path.home() / "Desktop",
        Path.home() / "Documents",
    ]


def coerce_path_rows(value) -> list[Path]:
    if value is None or value == "":
        return []
    if isinstance(value, (str, Path)):
        value = [value]
    rows = []
    for item in value or []:
        raw = str(item or "").strip()
        if raw:
            rows.append(Path(raw).expanduser())
    return rows


def resolve_bridgewater_report_paths() -> list[Path]:
    explicit_paths = coerce_path_rows(getattr(settings, "EQUITIES_BRIDGEWATER_REPORT_PATHS", ()))
    explicit_dirs = coerce_path_rows(getattr(settings, "EQUITIES_BRIDGEWATER_REPORT_DIRS", ()))
    candidates = []
    seen_paths = set()
    for path in explicit_paths:
        if path.exists() and path.is_file():
            normalized = str(path.resolve())
            if normalized not in seen_paths:
                seen_paths.add(normalized)
                candidates.append(path)
    search_dirs = explicit_dirs or resolve_bridgewater_default_dirs()
    scanned_candidates = []
    for directory in search_dirs:
        if not directory.exists() or not directory.is_dir():
            continue
        for path in directory.rglob("*.pdf"):
            normalized = str(path.resolve())
            if normalized in seen_paths:
                continue
            seen_paths.add(normalized)
            scanned_candidates.append(path)
    scanned_candidates.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    candidates.extend(scanned_candidates[: nightly_bridgewater_scan_limit()])
    return candidates


def normalize_source_key(value: str) -> str:
    normalized = SOURCE_KEY_SANITIZER.sub("-", normalize_news_text(value)).strip("-")
    return normalized[:80]


def deserialize_cached_marker(value):
    if not isinstance(value, dict):
        return value
    marker = value.get(JSON_TYPE_KEY)
    if marker == "decimal":
        return Decimal(str(value.get("value", "0")))
    if marker == "date":
        return date.fromisoformat(str(value.get("value")))
    if marker == "datetime":
        return datetime.fromisoformat(str(value.get("value")))
    return value


def parse_iso_date(value) -> date | None:
    raw_value = deserialize_cached_marker(value)
    if isinstance(raw_value, date) and not isinstance(raw_value, datetime):
        return raw_value
    if isinstance(raw_value, datetime):
        return raw_value.date()
    text = str(raw_value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        return None


def find_closest_market_point(points: list[dict], target_date: date, tolerance_days: int = 20) -> dict | None:
    candidates = [
        point
        for point in points or []
        if point.get("date") is not None and abs((point["date"] - target_date).days) <= tolerance_days
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda point: (abs((point["date"] - target_date).days), point["date"]))


def normalize_report_text(value: str) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())


def detect_bridgewater_identity_score(path: Path, text: str) -> int:
    haystack = f"{path.name} {text}".lower()
    return sum(1 for token in BRIDGEWATER_NAME_HINTS if token in haystack)


def extract_focus_text(text: str, focus_tokens: tuple[str, ...], *, max_chunks: int = 6) -> str:
    rows = []
    for chunk in re.split(r"(?<=[\.\!\?])\s+|\n{2,}", str(text or "")):
        cleaned = normalize_report_text(chunk)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if any(token in lowered for token in focus_tokens):
            rows.append(cleaned)
        if len(rows) >= max_chunks:
            break
    return " ".join(rows)


def score_text_with_token_maps(text: str, positive_tokens: dict[str, Decimal], negative_tokens: dict[str, Decimal]) -> Decimal:
    lowered = str(text or "").lower()
    score = ZERO
    for token, weight in positive_tokens.items():
        if token in lowered:
            score += weight
    for token, weight in negative_tokens.items():
        if token in lowered:
            score -= weight
    return clamp_decimal(score, Decimal("-4.00"), Decimal("4.00"))


def parse_bridgewater_report(path: Path, *, force_include: bool = False) -> dict | None:
    try:
        pages = read_pdf_pages(str(path))
    except Exception:
        return None
    combined_text = normalize_report_text(" ".join(page for page in pages if page))
    if not combined_text:
        return None
    identity_score = detect_bridgewater_identity_score(path, combined_text[:8000])
    if identity_score <= 0 and not force_include:
        return None
    market_focus_text = extract_focus_text(
        combined_text,
        (
            "equities",
            "stocks",
            "markets",
            "growth",
            "inflation",
            "recession",
            "policy",
            "central bank",
            "risk assets",
        ),
    )
    wall_street_focus_text = extract_focus_text(combined_text, BRIDGEWATER_WALL_STREET_FOCUS_TOKENS)
    market_text = market_focus_text or combined_text[:5000]
    wall_street_text = wall_street_focus_text or market_text
    market_score = score_text_with_token_maps(
        market_text,
        BRIDGEWATER_MARKET_POSITIVE_TOKENS,
        BRIDGEWATER_MARKET_NEGATIVE_TOKENS,
    )
    wall_street_score = score_text_with_token_maps(
        wall_street_text,
        BRIDGEWATER_MARKET_POSITIVE_TOKENS,
        BRIDGEWATER_MARKET_NEGATIVE_TOKENS,
    )
    market_tone = expert_tone_from_score(market_score)
    wall_street_tone = expert_tone_from_score(wall_street_score)
    published_on = datetime.fromtimestamp(path.stat().st_mtime).date()
    report_title = normalize_report_text(pages[0][:140] if pages else "") or path.stem.replace("_", " ")
    preview = market_focus_text[:420] if market_focus_text else combined_text[:420]
    quality_score = Decimal("60.00")
    return {
        "path": str(path),
        "title": report_title[:180],
        "preview": preview,
        "published_on": published_on,
        "published_label": published_on.isoformat(),
        "market_score": quantize_score(market_score),
        "market_tone": market_tone,
        "market_excerpt": market_focus_text[:800],
        "wall_street_score": quantize_score(wall_street_score),
        "wall_street_tone": wall_street_tone,
        "wall_street_excerpt": wall_street_focus_text[:800],
        "quality_score": quality_score,
        "quality_label": build_quality_label(quality_score),
        "identity_score": identity_score,
        "source_key": "bridgewater",
    }


def extract_publication_source(title: str) -> str:
    if " - " not in title:
        return ""
    return title.rsplit(" - ", 1)[-1].strip()


def resolve_expert_source(title: str, description: str, publication_source: str = "") -> tuple[str, str]:
    text = normalize_news_text(f"{title} {description}")
    for canonical_name, aliases in EXPERT_SOURCE_ALIASES.items():
        if any(alias in text for alias in aliases):
            return canonical_name, normalize_source_key(canonical_name)
    cleaned_publication = str(publication_source or "").strip()
    if cleaned_publication:
        return cleaned_publication, normalize_source_key(cleaned_publication)
    return "Fuente sin identificar", "fuente-sin-identificar"


def expert_item_relevant(title: str, description: str) -> bool:
    text = normalize_news_text(f"{title} {description}")
    return any(token in text for token in FORECAST_RELEVANCE_TOKENS)


def extract_percentage_signal(text: str) -> Decimal:
    score = ZERO
    upside_match = UPSIDE_REGEX.search(text or "")
    downside_match = DOWNSIDE_REGEX.search(text or "")
    if upside_match:
        value = Decimal(str(upside_match.group("value")).replace(",", "."))
        score += clamp_decimal(value / Decimal("14.00"), ZERO, Decimal("1.60"))
    if downside_match:
        value = Decimal(str(downside_match.group("value")).replace(",", "."))
        score -= clamp_decimal(value / Decimal("14.00"), ZERO, Decimal("1.60"))
    return score


def expert_item_score(title: str, description: str, published_at) -> Decimal:
    text = normalize_news_text(f"{title} {description}")
    score = ZERO
    for token, weight in POSITIVE_FORECAST_TOKENS.items():
        if token in text:
            score += weight
    for token, weight in NEGATIVE_FORECAST_TOKENS.items():
        if token in text:
            score -= weight
    score += extract_percentage_signal(text)
    if score == ZERO and any(token in text for token in NEUTRAL_FORECAST_TOKENS):
        score = ZERO
    if published_at is not None:
        age_days = max((timezone.now() - published_at).days, 0)
        if age_days <= 3:
            score *= Decimal("1.18")
        elif age_days <= 10:
            score *= Decimal("1.08")
        elif age_days <= 20:
            score *= Decimal("1.00")
        else:
            score *= Decimal("0.88")
    return clamp_decimal(score, Decimal("-4.00"), Decimal("4.00"))


def expert_tone_from_score(score: Decimal) -> str:
    if score >= Decimal("0.65"):
        return "positive"
    if score <= Decimal("-0.65"):
        return "negative"
    return "neutral"


def build_unavailable_expert_signal(label: str, note: str) -> dict:
    return {
        "available": False,
        "label": label,
        "score": ZERO,
        "quality_score": Decimal("56.00"),
        "quality_label": "Base",
        "items_count": 0,
        "positive_count": 0,
        "negative_count": 0,
        "neutral_count": 0,
        "items": [],
        "source_rows": [],
        "note": note,
    }


def build_neutral_source_quality(source_name: str) -> dict:
    quality_score = Decimal("56.00")
    return {
        "source": source_name,
        "source_key": normalize_source_key(source_name),
        "quality_score": quality_score,
        "quality_label": "Base",
        "source_weight": quantize_decimal(Decimal("0.60") + (quality_score / Decimal("160.00")), "0.01") or Decimal("0.95"),
        "observations_count": 0,
        "hit_rate_pct": None,
        "average_outcome_score": ZERO,
        "note": "Sin historico suficiente; se aplica un peso neutral.",
    }


def build_quality_label(score: Decimal | None) -> str:
    if score is None:
        return "Base"
    if score >= Decimal("74.00"):
        return "Alta"
    if score >= Decimal("60.00"):
        return "Media"
    return "Baja"


def decimal_from_signal(value, default: str = "0") -> Decimal:
    return Decimal(str(value or default))


def blend_available_signal_metric(signal_weights: list[tuple[dict, Decimal]], metric_key: str, *, default: Decimal) -> Decimal:
    weighted_total = ZERO
    total_weight = ZERO
    for signal, weight in signal_weights:
        if not signal or not signal.get("available"):
            continue
        weighted_total += decimal_from_signal(signal.get(metric_key), str(default)) * weight
        total_weight += weight
    if total_weight <= ZERO:
        return default
    return weighted_total / total_weight


def evaluate_expert_forecast_outcome(forecast_score: Decimal, future_return_pct: Decimal | None) -> dict | None:
    if future_return_pct is None:
        return None
    if abs(forecast_score) < Decimal("0.65"):
        hit = abs(future_return_pct) <= Decimal("5.00")
        raw_outcome = clamp_decimal(
            (Decimal("5.00") - abs(future_return_pct)) / Decimal("5.00"),
            Decimal("-1.00"),
            Decimal("1.00"),
        ) * Decimal("0.70")
        return {
            "direction": "neutral",
            "hit": hit,
            "outcome_score": raw_outcome,
        }
    direction_multiplier = Decimal("1.00") if forecast_score > ZERO else Decimal("-1.00")
    signed_return_pct = future_return_pct * direction_multiplier
    conviction = clamp_decimal(abs(forecast_score) / Decimal("3.20"), Decimal("0.55"), Decimal("1.15"))
    outcome_score = clamp_decimal(
        (signed_return_pct / Decimal("14.00")) * conviction,
        Decimal("-1.00"),
        Decimal("1.00"),
    )
    return {
        "direction": "bull" if forecast_score > ZERO else "bear",
        "hit": signed_return_pct >= ZERO,
        "outcome_score": outcome_score,
    }


def build_source_quality_row(source_name: str, source_key: str, observations: list[dict]) -> dict:
    if not observations:
        return build_neutral_source_quality(source_name)
    observations_count = len(observations)
    hit_rate_pct = (Decimal(sum(1 for row in observations if row.get("hit"))) * Decimal("100.00")) / Decimal(observations_count)
    average_outcome_score = sum((Decimal(str(row.get("outcome_score") or ZERO)) for row in observations), ZERO) / Decimal(observations_count)
    sample_factor = clamp_decimal(
        Decimal(observations_count) / Decimal(nightly_expert_source_observations_target()),
        Decimal("0.20"),
        Decimal("1.00"),
    )
    quality_score = clamp_decimal(
        Decimal("42.00")
        + (hit_rate_pct * Decimal("0.34"))
        + (average_outcome_score * Decimal("18.00"))
        + (sample_factor * Decimal("9.00")),
        Decimal("28.00"),
        Decimal("92.00"),
    )
    source_weight = clamp_decimal(
        Decimal("0.60") + (quality_score / Decimal("160.00")),
        Decimal("0.72"),
        Decimal("1.18"),
    )
    note = (
        f"{observations_count} observaciones historicas; acierto direccional "
        f"{quantize_decimal(hit_rate_pct) or ZERO}%."
    )
    return {
        "source": source_name,
        "source_key": source_key,
        "quality_score": quantize_decimal(quality_score) or quality_score,
        "quality_label": build_quality_label(quality_score),
        "source_weight": quantize_decimal(source_weight, "0.01") or source_weight,
        "observations_count": observations_count,
        "hit_rate_pct": quantize_decimal(hit_rate_pct),
        "average_outcome_score": quantize_decimal(average_outcome_score, "0.01") or average_outcome_score,
        "note": note,
    }


def fetch_expert_items_for_query(
    query: str,
    *,
    target_symbol: str,
    target_label: str,
    lookback_days: int,
    max_items: int,
) -> list[dict]:
    url = build_news_rss_url(query, lookback_days=lookback_days)
    xml_text = _fetch_google_news_xml_cached(
        url,
        news_cache_bucket(),
        nightly_expert_timeout_seconds(),
    )
    root = ET.fromstring(xml_text)
    items = []
    seen_titles = set()
    for item in root.findall(".//item"):
        title = strip_html_tags(item.findtext("title", ""))
        normalized_title = normalize_news_text(title)
        if not title or normalized_title in seen_titles:
            continue
        description = strip_html_tags(item.findtext("description", ""))
        if not expert_item_relevant(title, description):
            continue
        seen_titles.add(normalized_title)
        publication_source = extract_publication_source(title)
        expert_source, source_key = resolve_expert_source(title, description, publication_source)
        published_at = parse_news_date(item.findtext("pubDate", ""))
        raw_score = expert_item_score(title, description, published_at)
        tone = expert_tone_from_score(raw_score)
        items.append(
            {
                "title": title,
                "description": description,
                "link": (item.findtext("link", "") or "").strip(),
                "source": publication_source,
                "expert_source": expert_source,
                "source_key": source_key,
                "published_at": published_at,
                "published_label": published_at.strftime("%Y-%m-%d") if published_at else "",
                "published_on": published_at.date().isoformat() if published_at else "",
                "captured_on": timezone.localdate().isoformat(),
                "score": quantize_score(raw_score),
                "tone": tone,
                "target_symbol": target_symbol,
                "target_label": target_label,
            }
        )
        if len(items) >= max(int(max_items or 1), 1):
            break
    return items


def build_company_expert_query(company_name: str, ticker: str) -> str:
    query_parts = [
        f'"{company_name}"',
        f'"{ticker}"' if ticker else "",
        '(analista OR consenso OR "precio objetivo" OR recomendacion OR buy OR sell OR overweight OR underweight OR outlook OR forecast)',
        "(bolsa OR acciones OR ibex)",
    ]
    return " ".join(part for part in query_parts if part)


def build_market_expert_query() -> str:
    return (
        '("IBEX 35" OR "bolsa espanola" OR "mercado europeo") '
        '(prevision OR consenso OR estratega OR analista OR outlook OR forecast OR recomendacion)'
    )


def fetch_company_expert_items(company_name: str, ticker: str, quote_symbol: str) -> list[dict]:
    return fetch_expert_items_for_query(
        build_company_expert_query(company_name, ticker),
        target_symbol=quote_symbol,
        target_label=company_name,
        lookback_days=nightly_expert_company_lookback_days(),
        max_items=nightly_expert_max_items(),
    )


def fetch_market_expert_items() -> list[dict]:
    return fetch_expert_items_for_query(
        build_market_expert_query(),
        target_symbol=DEFAULT_BENCHMARK_SYMBOL,
        target_label=DEFAULT_BENCHMARK_NAME,
        lookback_days=nightly_expert_market_lookback_days(),
        max_items=max(3, nightly_expert_max_items()),
    )


def build_bridgewater_signal(*, source_quality_map: dict[str, dict] | None = None) -> dict:
    if not nightly_bridgewater_reports_enabled():
        return build_unavailable_expert_signal("Bridgewater", "La lectura local de informes Bridgewater esta desactivada.")
    report_paths = resolve_bridgewater_report_paths()
    if not report_paths:
        return build_unavailable_expert_signal("Bridgewater", "No se han encontrado informes locales de Bridgewater.")
    source_quality_map = source_quality_map or {}
    parsed_reports = []
    explicit_paths = {
        str(Path(raw).expanduser().resolve())
        for raw in coerce_path_rows(getattr(settings, "EQUITIES_BRIDGEWATER_REPORT_PATHS", ()))
        if str(raw or "").strip()
    }
    for path in report_paths:
        force_include = str(path.resolve()) in explicit_paths
        parsed = parse_bridgewater_report(path, force_include=force_include)
        if parsed is not None:
            parsed_reports.append(parsed)
        if len(parsed_reports) >= nightly_bridgewater_max_reports():
            break
    if not parsed_reports:
        return build_unavailable_expert_signal(
            "Bridgewater",
            "Hay PDFs candidatos, pero no se ha podido confirmar que sean informes de Bridgewater con texto legible.",
        )
    items = []
    for report in sorted(parsed_reports, key=lambda row: row.get("published_on") or date.min, reverse=True):
        report_date = report.get("published_on")
        market_score = decimal_from_signal(report.get("market_score"))
        wall_street_score = decimal_from_signal(report.get("wall_street_score"))
        blended_report_score = clamp_decimal(
            (market_score * Decimal("0.70")) + (wall_street_score * Decimal("0.30")),
            Decimal("-4.00"),
            Decimal("4.00"),
        )
        items.append(
            {
                "title": report.get("title") or "Informe Bridgewater",
                "description": report.get("preview") or "Lectura local extraida de un informe de Bridgewater.",
                "link": report.get("path") or "",
                "source": "Informe local",
                "expert_source": "Bridgewater",
                "source_key": "bridgewater",
                "published_at": timezone.make_aware(datetime.combine(report_date, datetime.min.time())) if report_date else None,
                "published_label": report.get("published_label") or "",
                "published_on": report_date.isoformat() if report_date else "",
                "captured_on": timezone.localdate().isoformat(),
                "score": quantize_score(blended_report_score),
                "tone": expert_tone_from_score(blended_report_score),
                "target_symbol": DEFAULT_BENCHMARK_SYMBOL,
                "target_label": DEFAULT_BENCHMARK_NAME,
                "market_score": quantize_score(market_score),
                "market_tone": report.get("market_tone") or "neutral",
                "wall_street_score": quantize_score(wall_street_score),
                "wall_street_tone": report.get("wall_street_tone") or "neutral",
                "report_path": report.get("path") or "",
            }
        )
    signal = summarize_expert_signal(
        items,
        label="Bridgewater",
        source_quality_map=source_quality_map,
    )
    signal_score = decimal_from_signal(signal.get("score"))
    average_wall_street_score = sum((decimal_from_signal(item.get("wall_street_score")) for item in items), ZERO) / Decimal(len(items))
    source_row = (signal.get("source_rows") or [{}])[0]
    observations_count = int(source_row.get("observations_count") or 0)
    if signal_score >= Decimal("2.20"):
        label = "Bridgewater favorable"
        note = "Los informes locales de Bridgewater refuerzan un sesgo constructivo."
    elif signal_score <= Decimal("-2.20"):
        label = "Bridgewater adversa"
        note = "Los informes locales de Bridgewater introducen una lectura mas defensiva."
    else:
        label = "Bridgewater mixta"
        note = "Los informes locales de Bridgewater no dejan una direccion unica."
    if observations_count > 0:
        note = f"{note} Track record {str(source_row.get('quality_label') or 'base').lower()} sobre {observations_count} observaciones."
    return {
        **signal,
        "label": label,
        "note": note,
        "wall_street_score": quantize_decimal(average_wall_street_score, "0.01") or ZERO,
    }


def build_wall_street_behavior_component(symbol: str, label: str) -> dict | None:
    try:
        series = fetch_market_series(symbol, range_key="1y")
    except Exception:
        return None
    points = list(series.points or [])
    if len(points) < 3:
        return None
    latest_point = points[-1]
    latest_date = latest_point.get("date")
    latest_close = latest_point.get("close")
    if latest_date is None or latest_close in {None, ZERO}:
        return None
    point_3m = find_closest_market_point(points, latest_date - timedelta(days=91), tolerance_days=16)
    point_12m = find_closest_market_point(points, latest_date - timedelta(days=365), tolerance_days=28)
    return_3m_pct = percentage_change(latest_close, point_3m.get("close") if point_3m else None)
    return_12m_pct = percentage_change(latest_close, point_12m.get("close") if point_12m else None)
    recent_window = [
        point
        for point in points
        if point.get("date") is not None and point["date"] >= latest_date - timedelta(days=180)
    ]
    recent_high = max((point.get("close") for point in recent_window if point.get("close") is not None), default=None)
    drawdown_pct = percentage_change(latest_close, recent_high)
    component_score = ZERO
    if return_3m_pct is not None:
        component_score += clamp_decimal(return_3m_pct * Decimal("0.15"), Decimal("-2.20"), Decimal("2.20"))
    if return_12m_pct is not None:
        component_score += clamp_decimal(return_12m_pct * Decimal("0.06"), Decimal("-1.80"), Decimal("1.80"))
    if drawdown_pct is not None and drawdown_pct < ZERO:
        component_score += clamp_decimal(drawdown_pct * Decimal("0.11"), Decimal("-1.60"), ZERO)
    if (return_3m_pct or ZERO) > ZERO and (return_12m_pct or ZERO) > ZERO:
        component_score += Decimal("0.55")
    elif (return_3m_pct or ZERO) < ZERO and (return_12m_pct or ZERO) < ZERO:
        component_score -= Decimal("0.55")
    component_score = clamp_decimal(component_score, Decimal("-4.00"), Decimal("4.00"))
    tone = expert_tone_from_score(component_score)
    quality_score = Decimal("80.00")
    summary_bits = []
    if return_3m_pct is not None:
        summary_bits.append(f"3M {quantize_decimal(return_3m_pct) or ZERO}%")
    if return_12m_pct is not None:
        summary_bits.append(f"12M {quantize_decimal(return_12m_pct) or ZERO}%")
    if drawdown_pct is not None:
        summary_bits.append(f"drawdown 6M {quantize_decimal(drawdown_pct) or ZERO}%")
    title = f"{label}: {' | '.join(summary_bits)}" if summary_bits else label
    return {
        "title": title,
        "description": f"Lectura cuantitativa de Wall Street sobre {label}.",
        "source": "Mercado USA",
        "expert_source": label,
        "source_key": normalize_source_key(label),
        "published_at": None,
        "published_label": latest_date.isoformat(),
        "published_on": latest_date.isoformat(),
        "captured_on": timezone.localdate().isoformat(),
        "score": quantize_score(component_score),
        "tone": tone,
        "target_symbol": symbol,
        "target_label": label,
        "quality_score": quality_score,
        "quality_label": build_quality_label(quality_score),
        "source_weight": Decimal("1.10"),
        "observations_count": 0,
        "hit_rate_pct": None,
        "weighted_score": quantize_decimal(component_score * Decimal("1.10"), "0.01") or component_score,
        "return_3m_pct": quantize_decimal(return_3m_pct),
        "return_12m_pct": quantize_decimal(return_12m_pct),
        "drawdown_6m_pct": quantize_decimal(drawdown_pct),
    }


def build_wall_street_behavior_signal() -> dict:
    items = []
    for symbol, label in WALL_STREET_INDEX_SPECS:
        component = build_wall_street_behavior_component(symbol, label)
        if component is not None:
            items.append(component)
    if not items:
        return build_unavailable_expert_signal("Wall Street", "No se ha podido leer el pulso reciente de Wall Street.")
    average_score = sum((decimal_from_signal(item.get("score")) for item in items), ZERO) / Decimal(len(items))
    signal_score = clamp_decimal(average_score * Decimal("2.25"), Decimal("-10.00"), Decimal("10.00"))
    quality_score = sum((decimal_from_signal(item.get("quality_score"), "80") for item in items), ZERO) / Decimal(len(items))
    positive_count = sum(1 for item in items if item.get("tone") == "positive")
    negative_count = sum(1 for item in items if item.get("tone") == "negative")
    neutral_count = len(items) - positive_count - negative_count
    if signal_score >= Decimal("2.20"):
        label = "Wall Street favorable"
        note = "S&P 500 y Nasdaq acompanan un sesgo positivo de mercado."
    elif signal_score <= Decimal("-2.20"):
        label = "Wall Street adversa"
        note = "S&P 500 y Nasdaq introducen una lectura mas defensiva."
    else:
        label = "Wall Street mixta"
        note = "Wall Street no aporta una direccion suficientemente clara."
    source_rows = [
        {
            "source": item.get("expert_source") or item.get("target_label") or "Wall Street",
            "source_key": item.get("source_key") or normalize_source_key(item.get("target_label") or "wall-street"),
            "quality_score": item.get("quality_score") or Decimal("80.00"),
            "quality_label": item.get("quality_label") or "Alta",
            "source_weight": item.get("source_weight") or Decimal("1.10"),
            "observations_count": 0,
            "hit_rate_pct": None,
            "current_items_count": 1,
            "current_score": item.get("score") or ZERO,
            "weighted_score": item.get("weighted_score") or item.get("score") or ZERO,
        }
        for item in items
    ]
    return {
        "available": True,
        "label": label,
        "score": quantize_score(signal_score),
        "quality_score": quantize_decimal(quality_score) or Decimal("80.00"),
        "quality_label": build_quality_label(quality_score),
        "items_count": len(items),
        "positive_count": positive_count,
        "negative_count": negative_count,
        "neutral_count": neutral_count,
        "items": items,
        "source_rows": source_rows,
        "note": note,
    }


def iter_snapshot_expert_items(payload: dict) -> list[dict]:
    consensus = payload.get("expert_consensus") or {}
    rows = []
    for scope_key in ("company_signal", "market_signal", "wall_street_signal", "bridgewater_signal"):
        signal = consensus.get(scope_key) or {}
        for item in signal.get("items") or []:
            rows.append(
                {
                    "scope": "market" if scope_key in {"market_signal", "wall_street_signal", "bridgewater_signal"} else "company",
                    "item": item,
                }
            )
    return rows


def build_source_quality_map(source_keys: set[str], *, as_of_date: date | None = None) -> dict[str, dict]:
    source_keys = {str(key).strip() for key in (source_keys or set()) if str(key).strip()}
    if not source_keys:
        return {}
    horizon_days = nightly_expert_history_horizon_days()
    as_of_date = as_of_date or timezone.localdate()
    min_analysis_date = as_of_date - timedelta(days=nightly_expert_history_lookback_days())
    max_analysis_date = as_of_date - timedelta(days=horizon_days)
    snapshot_queryset = EquityNightlyAnalysisSnapshot.objects.filter(
        analysis_date__gte=min_analysis_date,
        analysis_date__lte=max_analysis_date,
    ).only("analysis_date", "analysis_payload")
    snapshots = snapshot_queryset.iterator()
    observations_by_source: dict[str, list[dict]] = defaultdict(list)
    source_name_by_key: dict[str, str] = {}
    series_cache: dict[str, list] = {}
    seen_rows = set()
    for snapshot in snapshots:
        payload = snapshot.analysis_payload or {}
        for row in iter_snapshot_expert_items(payload):
            item = row["item"] or {}
            source_key = normalize_source_key(
                str(
                    deserialize_cached_marker(item.get("source_key"))
                    or deserialize_cached_marker(item.get("expert_source"))
                    or deserialize_cached_marker(item.get("source"))
                    or ""
                )
            )
            if not source_key or source_key not in source_keys:
                continue
            source_name = str(deserialize_cached_marker(item.get("expert_source")) or deserialize_cached_marker(item.get("source")) or source_key)
            source_name_by_key[source_key] = source_name
            anchor_date = parse_iso_date(item.get("published_on") or item.get("captured_on") or snapshot.analysis_date)
            if anchor_date is None or anchor_date > max_analysis_date:
                continue
            target_symbol = str(deserialize_cached_marker(item.get("target_symbol")) or "").strip()
            if not target_symbol:
                target_symbol = DEFAULT_BENCHMARK_SYMBOL if row["scope"] == "market" else ""
            if not target_symbol:
                continue
            title_key = normalize_news_text(str(deserialize_cached_marker(item.get("title")) or ""))
            dedupe_key = (
                row["scope"],
                source_key,
                target_symbol.upper(),
                title_key,
                anchor_date.isoformat(),
            )
            if dedupe_key in seen_rows:
                continue
            seen_rows.add(dedupe_key)
            forecast_score = Decimal(str(deserialize_cached_marker(item.get("score")) or "0"))
            history = series_cache.get(target_symbol.upper())
            if history is None:
                try:
                    history = fetch_market_series(target_symbol, range_key=MAX_MARKET_RANGE_KEY).points
                except Exception:
                    history = []
                series_cache[target_symbol.upper()] = history
            if not history:
                continue
            anchor_point = find_closest_market_point(
                history,
                anchor_date,
                tolerance_days=8 if row["scope"] == "company" else 15,
            )
            target_point = find_closest_market_point(
                history,
                anchor_date + timedelta(days=horizon_days),
                tolerance_days=22,
            )
            if anchor_point is None or target_point is None or target_point["date"] <= anchor_point["date"]:
                continue
            future_return_pct = percentage_change(target_point.get("close"), anchor_point.get("close"))
            outcome = evaluate_expert_forecast_outcome(forecast_score, future_return_pct)
            if outcome is None:
                continue
            observations_by_source[source_key].append(
                {
                    **outcome,
                    "future_return_pct": future_return_pct,
                }
            )
    quality_map = {}
    for source_key in source_keys:
        quality_map[source_key] = build_source_quality_row(
            source_name_by_key.get(source_key) or source_key.replace("-", " ").title(),
            source_key,
            observations_by_source.get(source_key) or [],
        )
    return quality_map


def decorate_expert_item(item: dict, source_quality_map: dict[str, dict]) -> dict:
    source_key = normalize_source_key(str(item.get("source_key") or ""))
    source_quality = source_quality_map.get(source_key) or build_neutral_source_quality(
        str(item.get("expert_source") or item.get("source") or "Fuente sin identificar")
    )
    score = Decimal(str(item.get("score") or ZERO))
    certainty_weight = clamp_decimal(
        Decimal("0.80") + (abs(score) / Decimal("6.50")),
        Decimal("0.80"),
        Decimal("1.25"),
    )
    source_weight = Decimal(str(source_quality.get("source_weight") or "0.95"))
    item_weight = source_weight * certainty_weight
    weighted_score = score * item_weight
    return {
        **item,
        "quality_score": source_quality.get("quality_score"),
        "quality_label": source_quality.get("quality_label"),
        "source_weight": quantize_decimal(source_weight, "0.01") or source_weight,
        "item_weight": quantize_decimal(item_weight, "0.01") or item_weight,
        "observations_count": int(source_quality.get("observations_count") or 0),
        "hit_rate_pct": source_quality.get("hit_rate_pct"),
        "weighted_score": quantize_decimal(weighted_score, "0.01") or weighted_score,
    }


def sort_expert_items(items: list[dict]) -> list[dict]:
    def item_key(item: dict):
        published_at = item.get("published_at")
        timestamp = published_at.timestamp() if published_at is not None else 0
        weighted_score = abs(Decimal(str(item.get("weighted_score") or ZERO)))
        score = abs(Decimal(str(item.get("score") or ZERO)))
        return (weighted_score, score, timestamp)

    return sorted(items, key=item_key, reverse=True)


def summarize_expert_signal(items: list[dict], *, label: str, source_quality_map: dict[str, dict]) -> dict:
    if not items:
        return build_unavailable_expert_signal(label, "No se han encontrado previsiones recientes de expertos.")
    decorated_items = [decorate_expert_item(item, source_quality_map) for item in items]
    total_weight = sum((Decimal(str(item.get("item_weight") or "1.00")) for item in decorated_items), ZERO)
    weighted_score_total = sum((Decimal(str(item.get("weighted_score") or ZERO)) for item in decorated_items), ZERO)
    raw_signal_score = (weighted_score_total / total_weight) if total_weight else ZERO
    signal_score = clamp_decimal(raw_signal_score * Decimal("2.35"), Decimal("-10.00"), Decimal("10.00"))
    positive_count = sum(1 for item in decorated_items if item.get("tone") == "positive")
    negative_count = sum(1 for item in decorated_items if item.get("tone") == "negative")
    neutral_count = len(decorated_items) - positive_count - negative_count
    by_source: dict[str, dict] = {}
    for item in decorated_items:
        source_key = normalize_source_key(str(item.get("source_key") or ""))
        if source_key not in by_source:
            by_source[source_key] = {
                "source": item.get("expert_source") or item.get("source") or source_key,
                "source_key": source_key,
                "quality_score": item.get("quality_score"),
                "quality_label": item.get("quality_label"),
                "source_weight": item.get("source_weight"),
                "observations_count": int(item.get("observations_count") or 0),
                "hit_rate_pct": item.get("hit_rate_pct"),
                "current_items_count": 0,
                "score_sum": ZERO,
                "weighted_score_sum": ZERO,
            }
        row = by_source[source_key]
        row["current_items_count"] += 1
        row["score_sum"] += Decimal(str(item.get("score") or ZERO))
        row["weighted_score_sum"] += Decimal(str(item.get("weighted_score") or ZERO))
    source_rows = []
    for row in by_source.values():
        items_count = max(int(row["current_items_count"]), 1)
        source_rows.append(
            {
                "source": row["source"],
                "source_key": row["source_key"],
                "quality_score": row["quality_score"],
                "quality_label": row["quality_label"],
                "source_weight": row["source_weight"],
                "observations_count": row["observations_count"],
                "hit_rate_pct": row["hit_rate_pct"],
                "current_items_count": row["current_items_count"],
                "current_score": quantize_decimal(row["score_sum"] / Decimal(items_count), "0.01") or ZERO,
                "weighted_score": quantize_decimal(row["weighted_score_sum"] / Decimal(items_count), "0.01") or ZERO,
            }
        )
    source_rows.sort(
        key=lambda row: (
            -(Decimal(str(row.get("quality_score") or ZERO))),
            -int(row.get("current_items_count") or 0),
            row.get("source") or "",
        )
    )
    weighted_quality_score = ZERO
    if total_weight:
        weighted_quality_score = sum(
            (
                Decimal(str(item.get("quality_score") or Decimal("56.00")))
                * Decimal(str(item.get("source_weight") or "0.95"))
            )
            for item in decorated_items
        ) / total_weight
    if signal_score >= Decimal("2.20"):
        signal_label = "Consenso favorable"
        note = "La media ponderada por acierto historico apunta a una lectura positiva."
    elif signal_score <= Decimal("-2.20"):
        signal_label = "Consenso adverso"
        note = "La media ponderada por acierto historico introduce una lectura mas cauta."
    else:
        signal_label = "Consenso mixto"
        note = "Las previsiones de expertos estan divididas o con poco diferencial informativo."
    return {
        "available": True,
        "label": f"{label} {signal_label}".strip(),
        "score": quantize_score(signal_score),
        "quality_score": quantize_decimal(weighted_quality_score) or Decimal("56.00"),
        "quality_label": build_quality_label(weighted_quality_score or Decimal("56.00")),
        "items_count": len(decorated_items),
        "positive_count": positive_count,
        "negative_count": negative_count,
        "neutral_count": neutral_count,
        "items": sort_expert_items(decorated_items),
        "source_rows": source_rows[:4],
        "note": note,
    }


def merge_expert_items(*item_groups: list[dict], max_items: int = 6) -> list[dict]:
    merged = []
    seen_titles = set()
    for group in item_groups:
        for item in group or []:
            title_key = normalize_news_text(item.get("title") or "")
            if not title_key or title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            merged.append(item)
    return sort_expert_items(merged)[: max(int(max_items or 1), 1)]


def merge_source_rows(*row_groups: list[dict], max_items: int = 6) -> list[dict]:
    merged: dict[str, dict] = {}
    for group in row_groups:
        for row in group or []:
            source_key = normalize_source_key(str(row.get("source_key") or ""))
            if not source_key:
                continue
            existing = merged.get(source_key)
            if existing is None:
                merged[source_key] = dict(row)
                continue
            existing["current_items_count"] = int(existing.get("current_items_count") or 0) + int(row.get("current_items_count") or 0)
            existing["weighted_score"] = quantize_decimal(
                (Decimal(str(existing.get("weighted_score") or ZERO)) + Decimal(str(row.get("weighted_score") or ZERO))) / Decimal("2.00"),
                "0.01",
            )
    rows = list(merged.values())
    rows.sort(
        key=lambda row: (
            -(Decimal(str(row.get("quality_score") or ZERO))),
            -int(row.get("current_items_count") or 0),
            row.get("source") or "",
        )
    )
    return rows[: max(int(max_items or 1), 1)]


def build_card_expert_consensus(
    company_signal: dict,
    market_signal: dict,
    wall_street_signal: dict | None = None,
    bridgewater_signal: dict | None = None,
) -> dict:
    wall_street_signal = wall_street_signal or build_unavailable_expert_signal("Wall Street", "Sin lectura de Wall Street.")
    bridgewater_signal = bridgewater_signal or build_unavailable_expert_signal("Bridgewater", "Sin lectura de Bridgewater.")
    top_items = merge_expert_items(
        company_signal.get("items") or [],
        market_signal.get("items") or [],
        wall_street_signal.get("items") or [],
        bridgewater_signal.get("items") or [],
        max_items=6,
    )
    signal_weights = [
        (company_signal, Decimal("0.50")),
        (market_signal, Decimal("0.20")),
        (wall_street_signal, Decimal("0.15")),
        (bridgewater_signal, Decimal("0.15")),
    ]
    aggregate_score = blend_available_signal_metric(
        signal_weights,
        "score",
        default=ZERO,
    )
    quality_score = blend_available_signal_metric(
        signal_weights,
        "quality_score",
        default=Decimal("56.00"),
    )
    if aggregate_score >= Decimal("1.90"):
        label = "Consenso experto favorable"
    elif aggregate_score <= Decimal("-1.90"):
        label = "Consenso experto adverso"
    else:
        label = "Consenso experto mixto"
    merged_source_rows = merge_source_rows(
        company_signal.get("source_rows"),
        market_signal.get("source_rows"),
        wall_street_signal.get("source_rows"),
        bridgewater_signal.get("source_rows"),
    )
    best_sources = [row.get("source") for row in merged_source_rows if row.get("source")]
    note_bits = []
    if company_signal.get("available"):
        note_bits.append(f"Empresa: {str(company_signal.get('label') or '').lower()}")
    if market_signal.get("available"):
        note_bits.append(f"Mercado: {str(market_signal.get('label') or '').lower()}")
    if wall_street_signal.get("available"):
        note_bits.append(f"Wall Street: {str(wall_street_signal.get('label') or '').lower()}")
    if bridgewater_signal.get("available"):
        note_bits.append(f"Bridgewater: {str(bridgewater_signal.get('label') or '').lower()}")
    if best_sources:
        note_bits.append(f"Fuentes mejor rankeadas: {', '.join(best_sources[:3])}")
    note = " | ".join(note_bits) if note_bits else "Sin previsiones recientes suficientemente filtradas."
    captured_at = timezone.localtime()
    return {
        "available": bool(
            company_signal.get("available")
            or market_signal.get("available")
            or wall_street_signal.get("available")
            or bridgewater_signal.get("available")
        ),
        "label": label,
        "score": quantize_score(aggregate_score),
        "quality_score": quantize_decimal(quality_score) or Decimal("56.00"),
        "quality_label": build_quality_label(quality_score),
        "items_count": len(top_items),
        "note": note,
        "best_sources": best_sources[:4],
        "source_rows": merged_source_rows,
        "company_signal": company_signal,
        "market_signal": market_signal,
        "wall_street_signal": wall_street_signal,
        "bridgewater_signal": bridgewater_signal,
        "top_items": top_items,
        "captured_at": captured_at.isoformat(),
        "captured_at_label": captured_at.strftime("%Y-%m-%d %H:%M"),
    }


def build_company_expert_items_map(cards: list[dict]) -> dict[str, list[dict]]:
    cards_to_analyze = []
    seen_tickers = set()
    for card in cards:
        position = card["position"]
        ticker = str(position.ticker or "").strip().upper()
        quote_symbol = str(position.quote_symbol or "").strip().upper()
        if not ticker or not quote_symbol or ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        cards_to_analyze.append(card)
    results: dict[str, list[dict]] = {}
    if not cards_to_analyze:
        return results
    max_workers = min(6, len(cards_to_analyze))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="nightly-company-expert") as executor:
        future_map = {
            executor.submit(
                fetch_company_expert_items,
                card["position"].company_name,
                card["position"].ticker,
                card["position"].quote_symbol,
            ): card
            for card in cards_to_analyze
        }
        for future in as_completed(future_map):
            card = future_map[future]
            ticker = str(card["position"].ticker or "").strip().upper()
            try:
                results[ticker] = future.result()
            except Exception:
                results[ticker] = []
    return results


def build_expert_consensus_context_map(cards: list[dict]) -> tuple[dict[str, dict], dict]:
    context_map: dict[str, dict] = {}
    if not nightly_expert_consensus_enabled():
        return context_map, {
            "enabled": False,
            "signals_count": 0,
            "items_count": 0,
            "ranked_sources_count": 0,
            "strong_consensus_count": 0,
        }
    cards = list(cards or [])
    if not cards:
        return context_map, {
            "enabled": True,
            "signals_count": 0,
            "items_count": 0,
            "ranked_sources_count": 0,
            "strong_consensus_count": 0,
        }
    company_items = build_company_expert_items_map(cards)
    try:
        market_items = fetch_market_expert_items()
    except Exception:
        market_items = []
    try:
        wall_street_signal = build_wall_street_behavior_signal()
    except Exception:
        wall_street_signal = build_unavailable_expert_signal("Wall Street", "No se ha podido leer Wall Street.")
    source_keys = set()
    for rows in company_items.values():
        for item in rows:
            source_keys.add(normalize_source_key(str(item.get("source_key") or "")))
    for item in market_items:
        source_keys.add(normalize_source_key(str(item.get("source_key") or "")))
    if nightly_bridgewater_reports_enabled():
        source_keys.add("bridgewater")
    source_quality_map = build_source_quality_map(source_keys)
    try:
        bridgewater_signal = build_bridgewater_signal(source_quality_map=source_quality_map)
    except Exception:
        bridgewater_signal = build_unavailable_expert_signal("Bridgewater", "No se han podido leer los informes de Bridgewater.")
    seen_tickers = set()
    for card in cards:
        ticker = str(card["position"].ticker or "").strip().upper()
        if not ticker or ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        company_signal = summarize_expert_signal(
            company_items.get(ticker) or [],
            label="Empresa",
            source_quality_map=source_quality_map,
        )
        market_signal = summarize_expert_signal(
            market_items,
            label="Mercado",
            source_quality_map=source_quality_map,
        )
        context_map[ticker] = build_card_expert_consensus(company_signal, market_signal, wall_street_signal, bridgewater_signal)
    summary = {
        "enabled": True,
        "signals_count": len(context_map),
        "items_count": sum(int(context.get("items_count") or 0) for context in context_map.values()),
        "ranked_sources_count": len([row for row in source_quality_map.values() if int(row.get("observations_count") or 0) > 0]),
        "high_quality_source_count": len(
            [row for row in source_quality_map.values() if Decimal(str(row.get("quality_score") or ZERO)) >= Decimal("74.00")]
        ),
        "strong_consensus_count": len(
            [context for context in context_map.values() if abs(Decimal(str(context.get("score") or ZERO))) >= Decimal("2.50")]
        ),
    }
    return context_map, summary


def attach_expert_consensus_to_dashboard(dashboard: dict) -> dict:
    cards = [
        *((dashboard.get("history_cards") or [])),
        *((dashboard.get("ibex_universe_cards") or [])),
    ]
    context_map, summary = build_expert_consensus_context_map(cards)
    for card in cards:
        ticker = str(card["position"].ticker or "").strip().upper()
        card["expert_consensus"] = context_map.get(ticker) or {
            "available": False,
            "label": "Sin consenso experto",
            "score": ZERO,
            "quality_score": Decimal("56.00"),
            "quality_label": "Base",
            "items_count": 0,
            "note": "No hay previsiones recientes suficientemente estructuradas.",
            "best_sources": [],
            "source_rows": [],
            "company_signal": build_unavailable_expert_signal("Empresa", "Sin previsiones de empresa."),
            "market_signal": build_unavailable_expert_signal("Mercado", "Sin previsiones de mercado."),
            "wall_street_signal": build_unavailable_expert_signal("Wall Street", "Sin lectura de Wall Street."),
            "bridgewater_signal": build_unavailable_expert_signal("Bridgewater", "Sin lectura de Bridgewater."),
            "top_items": [],
            "captured_at": "",
            "captured_at_label": "",
        }
    dashboard["expert_consensus_summary"] = summary
    return summary
