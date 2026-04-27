from __future__ import annotations

import json
import re
import unicodedata
from datetime import timedelta
from decimal import Decimal
from urllib.error import URLError
from urllib.request import Request, urlopen

from django.utils import timezone
from pypdf import PdfReader

from equities.llm_analysis import (
    call_anthropic_agent,
    call_openai_agent,
    resolve_ai_provider_config,
    trim_text,
)
from equities.news_context import (
    build_unavailable_news_signal,
    fetch_news_signal_for_query,
    strip_html_tags,
)

from .models import VentureAnalysisSnapshot, VentureDocument, VentureOpportunity


ZERO = Decimal("0")
MONEY_RE = re.compile(r"[-+]?\d{1,3}(?:[.\s]\d{3})*(?:,\d+)?|[-+]?\d+(?:[.,]\d+)?")


def _sum_optional(values):
    return sum((value for value in values if value is not None), ZERO)


def _average_score(opportunities):
    items = list(opportunities)
    if not items:
        return ZERO
    return sum((item.score_pct for item in items), ZERO) / Decimal(len(items))


def _choice_rows(choices, opportunities, attr_name):
    rows = []
    for value, label in choices:
        items = [item for item in opportunities if getattr(item, attr_name) == value]
        rows.append(
            {
                "value": value,
                "label": label,
                "count": len(items),
                "avg_score": _average_score(items),
            }
        )
    return rows


def decimal_or_none(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def parse_spanish_decimal(raw_value: str) -> Decimal | None:
    text = str(raw_value or "").strip().replace("\xa0", "").replace(" ", "")
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return Decimal(text)
    except Exception:
        return None


def normalize_search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(normalized.lower().split())


def extract_pdf_text(document: VentureDocument, *, max_chars: int = 70000) -> str:
    try:
        with document.file.open("rb") as pdf_file:
            reader = PdfReader(pdf_file)
            parts = []
            for page in reader.pages:
                page_text = page.extract_text() or ""
                if page_text:
                    parts.append(page_text)
                if sum(len(part) for part in parts) >= max_chars:
                    break
        text = "\n".join(parts).strip()[:max_chars]
    except Exception as exc:
        document.extraction_status = VentureDocument.ExtractionStatus.FAILED
        document.extraction_error = trim_text(str(exc), 500)
        document.save(update_fields=["extraction_status", "extraction_error"])
        return ""

    document.extracted_text = text
    document.extraction_status = (
        VentureDocument.ExtractionStatus.EXTRACTED
        if text
        else VentureDocument.ExtractionStatus.FAILED
    )
    document.extraction_error = "" if text else "El PDF no contiene texto extraible. Puede ser un escaneo."
    document.save(update_fields=["extracted_text", "extraction_status", "extraction_error"])
    return text


def _find_metric_after_labels(text: str, labels: tuple[str, ...]) -> Decimal | None:
    normalized = " ".join(str(text or "").split())
    lowered = normalize_search_text(normalized)
    for label in labels:
        position = lowered.find(normalize_search_text(label))
        if position < 0:
            continue
        window = normalized[position : position + 320]
        matches = MONEY_RE.findall(window)
        values = [value for value in (parse_spanish_decimal(match) for match in matches) if value is not None]
        plausible = [value for value in values if abs(value) >= Decimal("100")]
        if plausible:
            return plausible[0]
        if values:
            return values[0]
    return None


def parse_balance_metrics(text: str) -> dict:
    return {
        "annual_revenue": _find_metric_after_labels(
            text,
            (
                "importe neto de la cifra de negocios",
                "cifra de negocios",
                "ventas",
                "ingresos de explotacion",
                "ingresos",
            ),
        ),
        "ebitda": _find_metric_after_labels(text, ("ebitda",)),
        "net_equity": _find_metric_after_labels(
            text,
            (
                "patrimonio neto",
                "fondos propios",
                "capital y reservas",
            ),
        ),
        "total_assets": _find_metric_after_labels(text, ("total activo", "activo total")),
        "total_liabilities": _find_metric_after_labels(text, ("total pasivo", "pasivo total")),
        "debt": _find_metric_after_labels(
            text,
            (
                "deudas con entidades de credito",
                "deuda financiera",
                "prestamos",
                "acreedores financieros",
            ),
        ),
        "cash": _find_metric_after_labels(
            text,
            (
                "efectivo y otros activos liquidos equivalentes",
                "tesoreria",
                "efectivo",
                "caja",
            ),
        ),
        "profit": _find_metric_after_labels(
            text,
            (
                "resultado del ejercicio",
                "beneficio despues de impuestos",
                "resultado despues de impuestos",
            ),
        ),
    }


def fetch_website_context(url: str) -> dict:
    if not str(url or "").strip():
        return {"available": False, "title": "", "description": "", "error": ""}
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=8) as response:
            html_text = response.read(180000).decode("utf-8", errors="ignore")
    except (OSError, URLError, ValueError) as exc:
        return {
            "available": False,
            "title": "",
            "description": "",
            "error": trim_text(str(exc), 180),
        }

    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, flags=re.I | re.S)
    description_match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        html_text,
        flags=re.I | re.S,
    )
    title = strip_html_tags(title_match.group(1)) if title_match else ""
    description = strip_html_tags(description_match.group(1)) if description_match else ""
    return {
        "available": bool(title or description),
        "title": trim_text(title, 180),
        "description": trim_text(description, 260),
        "error": "",
    }


def build_venture_news_query(opportunity: VentureOpportunity) -> str:
    parts = [f'"{opportunity.company_name}"']
    if opportunity.geography:
        parts.append(f'"{opportunity.geography}"')
    parts.append("(empresa OR startup OR pyme OR inversion OR industria OR financiacion)")
    return " ".join(parts)


def build_venture_sector_query(opportunity: VentureOpportunity) -> str:
    if not opportunity.sector:
        return ""
    parts = [
        f'"{opportunity.sector}"',
        "(industria OR ceramica OR aditivos OR materiales OR inversion OR demanda OR energia)",
    ]
    if opportunity.geography:
        parts.append(f'"{opportunity.geography}"')
    return " ".join(parts)


def fetch_venture_web_context(opportunity: VentureOpportunity) -> dict:
    try:
        company_signal = fetch_news_signal_for_query(
            build_venture_news_query(opportunity),
            label="Empresa",
            lookback_days=90,
            max_items=8,
        )
    except Exception as exc:
        company_signal = build_unavailable_news_signal("Empresa", f"No se ha podido buscar prensa de empresa: {exc}")

    sector_query = build_venture_sector_query(opportunity)
    if sector_query:
        try:
            sector_signal = fetch_news_signal_for_query(
                sector_query,
                label="Sector",
                lookback_days=90,
                max_items=6,
            )
        except Exception as exc:
            sector_signal = build_unavailable_news_signal("Sector", f"No se ha podido buscar prensa sectorial: {exc}")
    else:
        sector_signal = build_unavailable_news_signal("Sector", "La empresa no tiene sector para ampliar la busqueda.")

    website = fetch_website_context(opportunity.website)
    top_items = []
    seen_titles = set()
    for signal in (company_signal, sector_signal):
        for item in signal.get("items") or []:
            title = str(item.get("title") or "").strip()
            normalized = title.lower()
            if not title or normalized in seen_titles:
                continue
            seen_titles.add(normalized)
            top_items.append(item)
            if len(top_items) >= 8:
                break

    note_bits = []
    if company_signal.get("available"):
        note_bits.append(f"Empresa: {company_signal.get('label')}")
    if sector_signal.get("available"):
        note_bits.append(f"Sector: {sector_signal.get('label')}")
    if website.get("available"):
        note_bits.append("Web oficial leida")
    return {
        "available": bool(company_signal.get("available") or sector_signal.get("available") or website.get("available")),
        "captured_at": timezone.localtime().isoformat(),
        "captured_at_label": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
        "note": " | ".join(note_bits) if note_bits else "No se ha encontrado contexto web suficiente.",
        "company_signal": company_signal,
        "sector_signal": sector_signal,
        "website": website,
        "top_items": top_items,
    }


def _weighted_average(values: list[tuple[Decimal | None, Decimal]]) -> Decimal | None:
    clean_values = [(value, weight) for value, weight in values if value is not None and value > ZERO and weight > ZERO]
    if not clean_values:
        return None
    total_weight = sum((weight for _, weight in clean_values), ZERO)
    return sum((value * weight for value, weight in clean_values), ZERO) / total_weight


def _confidence_from_inputs(text: str, web_context: dict, metrics: dict) -> str:
    available_metrics = sum(1 for value in metrics.values() if value is not None)
    if len(text or "") > 500 and web_context.get("available") and available_metrics >= 3:
        return VentureAnalysisSnapshot.Confidence.HIGH
    if len(text or "") > 150 or available_metrics >= 2:
        return VentureAnalysisSnapshot.Confidence.MEDIUM
    return VentureAnalysisSnapshot.Confidence.LOW


def build_core_valuation(opportunity: VentureOpportunity, metrics: dict, web_context: dict, text: str) -> dict:
    revenue = metrics.get("annual_revenue") or opportunity.annual_revenue
    ebitda = metrics.get("ebitda") or opportunity.ebitda
    net_equity = metrics.get("net_equity")
    cash_need = metrics.get("cash_need") or opportunity.cash_need
    debt = metrics.get("debt")
    cash = metrics.get("cash")
    net_debt = (debt - cash) if debt is not None and cash is not None else debt
    score_pct = Decimal(str(opportunity.score_pct)).quantize(Decimal("0.01"))
    score_factor = max(Decimal("0.70"), min(Decimal("1.30"), score_pct / Decimal("75")))

    stage_discount = {
        VentureOpportunity.Stage.EARLY: Decimal("0.75"),
        VentureOpportunity.Stage.GROWTH_ISSUES: Decimal("0.85"),
        VentureOpportunity.Stage.TURNAROUND: Decimal("0.70"),
        VentureOpportunity.Stage.SCALEUP: Decimal("1.05"),
        VentureOpportunity.Stage.OTHER: Decimal("0.85"),
    }.get(opportunity.stage, Decimal("0.85"))

    revenue_multiple = Decimal("0.65") * score_factor * stage_discount
    ebitda_multiple = Decimal("4.50") * score_factor * stage_discount
    revenue_value = revenue * revenue_multiple if revenue and revenue > ZERO else None
    ebitda_value = ebitda * ebitda_multiple if ebitda and ebitda > ZERO else None
    equity_floor = net_equity * Decimal("0.85") if net_equity and net_equity > ZERO else None
    enterprise_value = _weighted_average(
        [
            (revenue_value, Decimal("0.40")),
            (ebitda_value, Decimal("0.45")),
            (equity_floor, Decimal("0.15")),
        ]
    )
    if enterprise_value is None and opportunity.estimated_valuation:
        enterprise_value = opportunity.estimated_valuation
    if enterprise_value is None:
        enterprise_value = ZERO

    equity_value = enterprise_value
    if net_debt and net_debt > ZERO:
        equity_value = max(enterprise_value - net_debt, ZERO)

    confidence = _confidence_from_inputs(text, web_context, metrics)
    margin_of_safety = Decimal("0.72")
    if confidence == VentureAnalysisSnapshot.Confidence.HIGH:
        margin_of_safety = Decimal("0.78")
    elif confidence == VentureAnalysisSnapshot.Confidence.LOW:
        margin_of_safety = Decimal("0.62")

    suggested_purchase_price = equity_value * margin_of_safety
    ticket_candidates = [
        opportunity.ticket_max,
        cash_need,
        suggested_purchase_price * Decimal("0.20") if suggested_purchase_price else None,
    ]
    suggested_ticket = min([value for value in ticket_candidates if value is not None and value > ZERO], default=None)
    target_ownership_pct = (
        (suggested_ticket / suggested_purchase_price * Decimal("100"))
        if suggested_ticket and suggested_purchase_price and suggested_purchase_price > ZERO
        else None
    )

    ask_valuation = opportunity.estimated_valuation
    ask_is_attractive = ask_valuation is not None and suggested_purchase_price and ask_valuation <= suggested_purchase_price
    score_supports_buy = score_pct >= Decimal("75")
    strong_fit_watch = score_pct >= Decimal("68") and opportunity.neos_fit_score >= 4
    recommendation = (
        VentureAnalysisSnapshot.Recommendation.BUY
        if (ask_is_attractive or (ask_valuation is None and score_supports_buy and confidence != VentureAnalysisSnapshot.Confidence.LOW))
        else VentureAnalysisSnapshot.Recommendation.WATCH
    )
    if recommendation == VentureAnalysisSnapshot.Recommendation.WATCH and strong_fit_watch and confidence == VentureAnalysisSnapshot.Confidence.HIGH:
        recommendation = VentureAnalysisSnapshot.Recommendation.BUY

    drivers = [
        f"Encaje Neos {opportunity.neos_fit_score}/5 y score total {score_pct:.0f} %.",
    ]
    if revenue:
        drivers.append(f"Facturacion detectada o registrada: {revenue:.0f} EUR.")
    if ebitda and ebitda > ZERO:
        drivers.append(f"EBITDA positivo usado en la valoracion: {ebitda:.0f} EUR.")
    if web_context.get("available"):
        drivers.append("Existe contexto web o sectorial para contrastar la tesis.")

    risks = []
    if not text:
        risks.append("El PDF no ha aportado texto util; la valoracion depende mas de datos manuales.")
    if ebitda is not None and ebitda <= ZERO:
        risks.append("EBITDA negativo o no demostrable: exige margen de seguridad.")
    if confidence == VentureAnalysisSnapshot.Confidence.LOW:
        risks.append("Confianza baja por informacion financiera insuficiente.")
    if not risks:
        risks.append("La valoracion sigue siendo orientativa y requiere due diligence legal, fiscal y comercial.")

    assumptions = [
        f"Multiplo ventas aproximado {revenue_multiple:.2f}x y multiplo EBITDA {ebitda_multiple:.2f}x, ajustados por estadio y score.",
        f"Margen de seguridad aplicado: {(Decimal('1') - margin_of_safety) * Decimal('100'):.0f} %.",
        "Precio de compra aproximado entendido como valor maximo orientativo del 100 % de la empresa antes de negociacion.",
    ]
    valuation_note = (
        "Valoracion por triangulacion de ventas, EBITDA y patrimonio neto cuando estan disponibles. "
        "Para empresas no cotizadas y con tension de crecimiento, el precio sugerido aplica descuento de seguridad."
    )
    summary = (
        f"{opportunity.company_name}: recomendacion {dict(VentureAnalysisSnapshot.Recommendation.choices)[recommendation].lower()} "
        f"con confianza {dict(VentureAnalysisSnapshot.Confidence.choices)[confidence].lower()}. "
        f"Precio maximo orientativo del 100 %: {suggested_purchase_price:.0f} EUR."
    )

    return {
        "recommendation": recommendation,
        "confidence": confidence,
        "score_pct": score_pct,
        "valuation_low": (equity_value * Decimal("0.75")).quantize(Decimal("0.01")) if equity_value else None,
        "valuation_base": equity_value.quantize(Decimal("0.01")) if equity_value else None,
        "valuation_high": (equity_value * Decimal("1.25")).quantize(Decimal("0.01")) if equity_value else None,
        "suggested_purchase_price": suggested_purchase_price.quantize(Decimal("0.01")) if suggested_purchase_price else None,
        "suggested_ticket": suggested_ticket.quantize(Decimal("0.01")) if suggested_ticket else None,
        "target_ownership_pct": target_ownership_pct.quantize(Decimal("0.01")) if target_ownership_pct else None,
        "annual_revenue": revenue,
        "ebitda": ebitda,
        "net_equity": net_equity,
        "net_debt": net_debt,
        "cash_need": cash_need,
        "summary": summary,
        "valuation_note": valuation_note,
        "web_summary": web_context.get("note", ""),
        "drivers": drivers[:5],
        "risks": risks[:5],
        "assumptions": assumptions,
        "agent_provider": "core",
        "agent_label": "Analisis interno",
        "analysis_payload": {
            "metrics": {key: str(value) for key, value in metrics.items() if value is not None},
            "revenue_multiple": str(revenue_multiple.quantize(Decimal("0.01"))),
            "ebitda_multiple": str(ebitda_multiple.quantize(Decimal("0.01"))),
            "margin_of_safety": str(margin_of_safety),
            "ask_valuation": str(ask_valuation) if ask_valuation else "",
        },
    }


def _analysis_json_payload(opportunity: VentureOpportunity, metrics: dict, web_context: dict, core_payload: dict, text: str) -> dict:
    return {
        "company": {
            "name": opportunity.company_name,
            "website": opportunity.website,
            "sector": opportunity.sector,
            "geography": opportunity.geography,
            "stage": opportunity.get_stage_display(),
            "strategic_fit": opportunity.get_strategic_fit_display(),
            "manual_score_pct": str(opportunity.score_pct),
        },
        "manual_inputs": {
            "estimated_valuation": str(opportunity.estimated_valuation or ""),
            "annual_revenue": str(opportunity.annual_revenue or ""),
            "ebitda": str(opportunity.ebitda or ""),
            "cash_need": str(opportunity.cash_need or ""),
            "ticket_min": str(opportunity.ticket_min or ""),
            "ticket_max": str(opportunity.ticket_max or ""),
            "fit_summary": trim_text(opportunity.fit_summary, 800),
            "synergy_notes": trim_text(opportunity.synergy_notes, 800),
            "red_flags": trim_text(opportunity.red_flags, 800),
        },
        "parsed_balance_metrics": {key: str(value) for key, value in metrics.items() if value is not None},
        "core_valuation": {
            key: str(value)
            for key, value in core_payload.items()
            if key
            in {
                "recommendation",
                "confidence",
                "score_pct",
                "valuation_low",
                "valuation_base",
                "valuation_high",
                "suggested_purchase_price",
                "suggested_ticket",
                "target_ownership_pct",
            }
        },
        "web_context": {
            "note": web_context.get("note", ""),
            "website": web_context.get("website", {}),
            "headlines": [
                {
                    "title": item.get("title", ""),
                    "source": item.get("source", ""),
                    "published_label": item.get("published_label", ""),
                    "tone": item.get("tone", ""),
                }
                for item in (web_context.get("top_items") or [])[:8]
            ],
        },
        "balance_text_excerpt": trim_text(text, 14000),
    }


def _parse_ai_decimal(payload: dict, key: str):
    value = payload.get(key)
    if value in (None, ""):
        return None
    return decimal_or_none(value)


def _normalize_ai_list(value, fallback):
    rows = [trim_text(item, 220) for item in list(value or []) if trim_text(item, 220)]
    return rows[:5] or fallback


def try_ai_venture_analysis(opportunity, metrics, web_context, core_payload, text, *, enabled=True) -> dict | None:
    if not enabled:
        return None
    config = resolve_ai_provider_config()
    if not config.available:
        return None

    system_prompt = (
        "Eres un analista de inversion en empresas no cotizadas industriales complementarias a Neos Ceramica y Neos Additives. "
        "Trabajas solo con el JSON recibido: datos manuales, texto extraido del balance PDF y contexto web. "
        "No inventes cifras. Si una cifra no aparece, dilo. Devuelve solo JSON valido. "
        "La recomendacion debe ser buy o watch. El precio sugerido es orientativo para el 100 % de la empresa, con margen de seguridad."
    )
    user_payload = _analysis_json_payload(opportunity, metrics, web_context, core_payload, text)
    user_prompt = (
        "Analiza esta oportunidad no cotizada. Devuelve JSON con: recommendation, confidence, score_pct, "
        "suggested_purchase_price, valuation_low, valuation_base, valuation_high, suggested_ticket, target_ownership_pct, "
        "summary, valuation_note, web_summary, drivers, risks y assumptions. JSON de entrada: "
        f"{json.dumps(user_payload, ensure_ascii=True, separators=(',', ':'))}"
    )
    try:
        if config.provider == "anthropic":
            payload, usage = call_anthropic_agent(config, system_prompt=system_prompt, user_prompt=user_prompt)
        elif config.provider == "openai":
            payload, usage = call_openai_agent(config, system_prompt=system_prompt, user_prompt=user_prompt)
        else:
            return None
    except Exception:
        return None

    recommendation_text = str(payload.get("recommendation") or "").strip().lower()
    recommendation = {
        "buy": VentureAnalysisSnapshot.Recommendation.BUY,
        "compra": VentureAnalysisSnapshot.Recommendation.BUY,
        "comprar": VentureAnalysisSnapshot.Recommendation.BUY,
        "watch": VentureAnalysisSnapshot.Recommendation.WATCH,
        "vigilancia": VentureAnalysisSnapshot.Recommendation.WATCH,
        "vigilar": VentureAnalysisSnapshot.Recommendation.WATCH,
    }.get(recommendation_text, core_payload["recommendation"])
    confidence_text = str(payload.get("confidence") or "").strip().lower()
    confidence = {
        "alta": VentureAnalysisSnapshot.Confidence.HIGH,
        "high": VentureAnalysisSnapshot.Confidence.HIGH,
        "media": VentureAnalysisSnapshot.Confidence.MEDIUM,
        "medium": VentureAnalysisSnapshot.Confidence.MEDIUM,
        "baja": VentureAnalysisSnapshot.Confidence.LOW,
        "low": VentureAnalysisSnapshot.Confidence.LOW,
    }.get(confidence_text, core_payload["confidence"])

    enriched = {
        **core_payload,
        "recommendation": recommendation,
        "confidence": confidence,
        "score_pct": _parse_ai_decimal(payload, "score_pct") or core_payload["score_pct"],
        "valuation_low": _parse_ai_decimal(payload, "valuation_low") or core_payload["valuation_low"],
        "valuation_base": _parse_ai_decimal(payload, "valuation_base") or core_payload["valuation_base"],
        "valuation_high": _parse_ai_decimal(payload, "valuation_high") or core_payload["valuation_high"],
        "suggested_purchase_price": _parse_ai_decimal(payload, "suggested_purchase_price") or core_payload["suggested_purchase_price"],
        "suggested_ticket": _parse_ai_decimal(payload, "suggested_ticket") or core_payload["suggested_ticket"],
        "target_ownership_pct": _parse_ai_decimal(payload, "target_ownership_pct") or core_payload["target_ownership_pct"],
        "summary": trim_text(payload.get("summary") or core_payload["summary"], 700),
        "valuation_note": trim_text(payload.get("valuation_note") or core_payload["valuation_note"], 700),
        "web_summary": trim_text(payload.get("web_summary") or core_payload["web_summary"], 500),
        "drivers": _normalize_ai_list(payload.get("drivers"), core_payload["drivers"]),
        "risks": _normalize_ai_list(payload.get("risks"), core_payload["risks"]),
        "assumptions": _normalize_ai_list(payload.get("assumptions"), core_payload["assumptions"]),
        "agent_provider": config.provider,
        "agent_label": config.label,
        "analysis_payload": {
            **core_payload.get("analysis_payload", {}),
            "ai_usage": {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "estimated_cost_usd": str(usage.get("estimated_cost_usd") or ZERO),
            },
        },
    }
    return enriched


def run_document_analysis(document: VentureDocument, *, use_ai: bool = True) -> VentureAnalysisSnapshot:
    text = document.extracted_text or extract_pdf_text(document)
    metrics = parse_balance_metrics(text)
    opportunity = document.opportunity
    web_context = fetch_venture_web_context(opportunity)
    core_payload = build_core_valuation(opportunity, metrics, web_context, text)
    payload = try_ai_venture_analysis(
        opportunity,
        metrics,
        web_context,
        core_payload,
        text,
        enabled=use_ai,
    ) or core_payload

    snapshot = VentureAnalysisSnapshot.objects.create(
        opportunity=opportunity,
        source_document=document,
        recommendation=payload["recommendation"],
        confidence=payload["confidence"],
        score_pct=payload["score_pct"],
        valuation_low=payload["valuation_low"],
        valuation_base=payload["valuation_base"],
        valuation_high=payload["valuation_high"],
        suggested_purchase_price=payload["suggested_purchase_price"],
        suggested_ticket=payload["suggested_ticket"],
        target_ownership_pct=payload["target_ownership_pct"],
        annual_revenue=payload["annual_revenue"],
        ebitda=payload["ebitda"],
        net_equity=payload["net_equity"],
        net_debt=payload["net_debt"],
        cash_need=payload["cash_need"],
        summary=payload["summary"],
        valuation_note=payload["valuation_note"],
        web_summary=payload["web_summary"],
        drivers=payload["drivers"],
        risks=payload["risks"],
        assumptions=payload["assumptions"],
        web_context=web_context,
        analysis_payload=payload["analysis_payload"],
        agent_provider=payload["agent_provider"],
        agent_label=payload["agent_label"],
    )
    update_fields = []
    if payload["annual_revenue"] is not None and opportunity.annual_revenue is None:
        opportunity.annual_revenue = payload["annual_revenue"]
        update_fields.append("annual_revenue")
    if payload["ebitda"] is not None and opportunity.ebitda is None:
        opportunity.ebitda = payload["ebitda"]
        update_fields.append("ebitda")
    if payload["cash_need"] is not None and opportunity.cash_need is None:
        opportunity.cash_need = payload["cash_need"]
        update_fields.append("cash_need")
    if update_fields:
        update_fields.append("updated_at")
        opportunity.save(update_fields=update_fields)
    return snapshot


def build_svg_polyline(values, width: int = 720, height: int = 160, padding: int = 16) -> str:
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


def build_venture_study_context(opportunities):
    opportunities = list(opportunities)
    all_snapshots = list(
        VentureAnalysisSnapshot.objects.select_related("opportunity", "source_document").order_by(
            "opportunity__company_name",
            "analysis_date",
            "created_at",
            "id",
        )
    )
    snapshots_by_opportunity = {}
    for snapshot in all_snapshots:
        snapshots_by_opportunity.setdefault(snapshot.opportunity_id, []).append(snapshot)
    latest_analysis_by_opportunity = {}
    for opportunity_id, rows in snapshots_by_opportunity.items():
        latest_analysis_by_opportunity[opportunity_id] = sorted(rows, key=lambda item: (item.analysis_date, item.created_at, item.id), reverse=True)[0]
    for opportunity in opportunities:
        history = snapshots_by_opportunity.get(opportunity.id, [])
        setattr(opportunity, "analysis_history", sorted(history, key=lambda item: (item.analysis_date, item.created_at, item.id), reverse=True))
        setattr(opportunity, "latest_analysis", latest_analysis_by_opportunity.get(opportunity.id))
        setattr(
            opportunity,
            "valuation_line",
            build_svg_polyline([snapshot.suggested_purchase_price for snapshot in history]),
        )
    active_opportunities = [item for item in opportunities if item.is_active]
    today = timezone.localdate()
    review_limit = today + timedelta(days=30)
    high_priority = [
        item
        for item in active_opportunities
        if item.score_pct >= Decimal("80") and item.status != VentureOpportunity.Status.REJECTED
    ]
    review_due = [
        item
        for item in active_opportunities
        if item.next_review_on and item.next_review_on <= review_limit
    ]
    priority_rows = sorted(
        active_opportunities,
        key=lambda item: (item.score_pct, item.next_review_on or today),
        reverse=True,
    )

    return {
        "summary": {
            "total_count": len(opportunities),
            "active_count": len(active_opportunities),
            "high_priority_count": len(high_priority),
            "due_diligence_count": sum(
                1 for item in active_opportunities if item.status == VentureOpportunity.Status.DUE_DILIGENCE
            ),
            "analysed_count": len(latest_analysis_by_opportunity),
            "buy_count": sum(
                1
                for analysis in latest_analysis_by_opportunity.values()
                if analysis.recommendation == VentureAnalysisSnapshot.Recommendation.BUY
            ),
            "ticket_min_total": _sum_optional(item.ticket_min for item in active_opportunities),
            "ticket_max_total": _sum_optional(item.ticket_max for item in active_opportunities),
            "suggested_purchase_total": _sum_optional(
                analysis.suggested_purchase_price for analysis in latest_analysis_by_opportunity.values()
            ),
            "avg_score": _average_score(active_opportunities),
            "review_due_count": len(review_due),
        },
        "priority_rows": priority_rows,
        "latest_analyses": sorted(
            latest_analysis_by_opportunity.values(),
            key=lambda item: (item.recommendation != VentureAnalysisSnapshot.Recommendation.BUY, -item.score_pct, item.opportunity.company_name),
        ),
        "analysis_history": sorted(all_snapshots, key=lambda item: (item.analysis_date, item.created_at, item.id), reverse=True)[:40],
        "review_due_rows": sorted(review_due, key=lambda item: item.next_review_on),
        "status_rows": _choice_rows(VentureOpportunity.Status.choices, opportunities, "status"),
        "fit_rows": _choice_rows(VentureOpportunity.StrategicFit.choices, opportunities, "strategic_fit"),
        "stage_rows": _choice_rows(VentureOpportunity.Stage.choices, opportunities, "stage"),
    }
