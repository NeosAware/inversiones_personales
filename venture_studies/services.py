from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata
from datetime import timedelta
from decimal import Decimal
from urllib.error import URLError
from urllib.request import Request, urlopen

from django.utils.dateparse import parse_date
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

from .models import VentureAnalysisSnapshot, VentureDiscoveryCandidate, VentureDocument, VentureOpportunity


ZERO = Decimal("0")
MONEY_RE = re.compile(r"[-+]?\d{1,3}(?:[.\s]\d{3})*(?:,\d+)?|[-+]?\d+(?:[.,]\d+)?")
EMAIL_RE = re.compile(r"[\w.\-+]+@[\w.\-]+\.[A-Za-z]{2,}", re.I)
URL_RE = re.compile(r"(?:https?://)?(?:www\.)?[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+(?:/[^\s]*)?", re.I)
PHONE_RE = re.compile(r"(?:\+34\s*)?(?:\d[\s.-]?){9,12}")
TAX_ID_RE = re.compile(r"\b(?:[ABCDEFGHJNPQRSUVW]\d{7}[0-9A-J]|[XYZ]?\d{7,8}[A-Z])\b", re.I)
COMPANY_SUFFIX_RE = re.compile(
    r"\b([^\W_][\w&.,' -]{2,90}?\s+(?:S\.?L\.?|S\.?A\.?|SOCIEDAD LIMITADA|SOCIEDAD ANONIMA))\b",
    re.I,
)
AI_TEXT_UPDATE_FIELDS = {
    "legal_name": 180,
    "tax_id": 24,
    "website": 200,
    "sector": 140,
    "geography": 120,
    "address": 240,
    "phone": 60,
    "email": 254,
    "cnae_code": 16,
    "cnae_label": 180,
    "contact_name": 140,
    "source": 160,
    "fit_summary": 1200,
    "growth_issue": 1200,
    "synergy_notes": 1200,
    "diligence_notes": 1200,
    "red_flags": 1200,
    "next_steps": 1200,
}
AI_DECIMAL_UPDATE_FIELDS = (
    "ticket_min",
    "ticket_max",
    "estimated_valuation",
    "annual_revenue",
    "ebitda",
    "cash_need",
)
AI_SCORE_UPDATE_FIELDS = (
    "neos_fit_score",
    "market_score",
    "team_score",
    "financial_score",
    "risk_control_score",
)
AI_CHOICE_UPDATE_FIELDS = {
    "stage": VentureOpportunity.Stage.choices,
    "status": VentureOpportunity.Status.choices,
    "strategic_fit": VentureOpportunity.StrategicFit.choices,
}


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


def int_or_none(value):
    if value in (None, ""):
        return None
    try:
        text = str(value).strip().replace(" ", "")
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        elif text.count(".") > 1:
            text = text.replace(".", "")
        return int(Decimal(text))
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


def normalize_website_url(value: str) -> str:
    cleaned = re.sub(r"\s+", "", str(value or "").strip().strip(" -|"))[:200]
    if not cleaned or "." not in cleaned or "@" in cleaned:
        return ""
    if not cleaned.lower().startswith(("http://", "https://")):
        cleaned = f"https://{cleaned}"
    return cleaned


def normalize_ai_text_value(value, *, max_length: int) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (list, tuple)):
        value = "; ".join(str(item) for item in value if str(item).strip())
    elif isinstance(value, dict):
        return ""
    return trim_text(" ".join(str(value).split()), max_length).strip()


def normalize_ai_choice_value(field_name: str, value) -> str:
    text = normalize_search_text(value)
    if not text:
        return ""
    for choice_value, label in AI_CHOICE_UPDATE_FIELDS[field_name]:
        if text in {normalize_search_text(choice_value), normalize_search_text(label)}:
            return choice_value
    aliases = {
        "stage": {
            "inicial": VentureOpportunity.Stage.EARLY,
            "early": VentureOpportunity.Stage.EARLY,
            "crecimiento": VentureOpportunity.Stage.GROWTH_ISSUES,
            "problemas crecimiento": VentureOpportunity.Stage.GROWTH_ISSUES,
            "growth": VentureOpportunity.Stage.GROWTH_ISSUES,
            "reestructuracion": VentureOpportunity.Stage.TURNAROUND,
            "turnaround": VentureOpportunity.Stage.TURNAROUND,
            "escalado": VentureOpportunity.Stage.SCALEUP,
            "scaleup": VentureOpportunity.Stage.SCALEUP,
        },
        "status": {
            "screening": VentureOpportunity.Status.SCREENING,
            "primer filtro": VentureOpportunity.Status.SCREENING,
            "analisis": VentureOpportunity.Status.RESEARCH,
            "en analisis": VentureOpportunity.Status.RESEARCH,
            "research": VentureOpportunity.Status.RESEARCH,
            "due diligence": VentureOpportunity.Status.DUE_DILIGENCE,
            "diligence": VentureOpportunity.Status.DUE_DILIGENCE,
            "negociacion": VentureOpportunity.Status.NEGOTIATION,
            "negotiation": VentureOpportunity.Status.NEGOTIATION,
            "pausa": VentureOpportunity.Status.ON_HOLD,
            "aprobada": VentureOpportunity.Status.APPROVED,
            "descartada": VentureOpportunity.Status.REJECTED,
        },
        "strategic_fit": {
            "ceramica": VentureOpportunity.StrategicFit.CERAMICA,
            "neos ceramica": VentureOpportunity.StrategicFit.CERAMICA,
            "additives": VentureOpportunity.StrategicFit.ADDITIVES,
            "neos additives": VentureOpportunity.StrategicFit.ADDITIVES,
            "ceramica additives": VentureOpportunity.StrategicFit.BOTH,
            "ceramica + additives": VentureOpportunity.StrategicFit.BOTH,
            "ambas": VentureOpportunity.StrategicFit.BOTH,
            "grupo": VentureOpportunity.StrategicFit.GROUP,
            "grupo neos": VentureOpportunity.StrategicFit.GROUP,
        },
    }
    return aliases.get(field_name, {}).get(text, "")


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


def extract_pdf_text_from_file(uploaded_file, *, max_chars: int = 70000) -> str:
    uploaded_file.seek(0)
    reader = PdfReader(uploaded_file)
    parts = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        if page_text:
            parts.append(page_text)
        if sum(len(part) for part in parts) >= max_chars:
            break
    uploaded_file.seek(0)
    return "\n".join(parts).strip()[:max_chars]


def normalize_lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def clean_informa_value(value: str, *, max_length: int = 240) -> str:
    cleaned = " ".join(str(value or "").replace(":", " ").split())
    cleaned = cleaned.strip(" -|")
    return cleaned[:max_length].strip()


def find_value_after_label(text: str, labels: tuple[str, ...], *, max_length: int = 240) -> str:
    lines = normalize_lines(text)
    normalized_labels = [normalize_search_text(label) for label in labels]
    for index, line in enumerate(lines):
        normalized_line = normalize_search_text(line)
        for label, normalized_label in zip(labels, normalized_labels):
            if normalized_label not in normalized_line:
                continue
            after = line[normalized_line.find(normalized_label) + len(label) :].strip(" :-|")
            if after and normalize_search_text(after) != normalized_label:
                return clean_informa_value(after, max_length=max_length)
            if index + 1 < len(lines):
                candidate = clean_informa_value(lines[index + 1], max_length=max_length)
                if candidate and normalize_search_text(candidate) not in normalized_labels:
                    return candidate
    return ""


def find_company_name_from_informa(text: str) -> str:
    labels = (
        "denominacion social",
        "razon social",
        "nombre de la empresa",
        "empresa",
    )
    value = find_value_after_label(text, labels, max_length=180)
    if value and not normalize_search_text(value).startswith("informe"):
        return value
    for line in normalize_lines(text)[:30]:
        cleaned = clean_informa_value(line, max_length=180)
        normalized = normalize_search_text(cleaned)
        if (
            len(cleaned) >= 5
            and not normalized.startswith("informe")
            and not normalized.startswith("fecha")
            and any(token in normalized for token in (" sl", " s.l", " sa", " s.a", " sociedad limitada", " sociedad anonima"))
        ):
            return cleaned
    return ""


def find_tax_id_from_informa(text: str) -> str:
    labelled = find_value_after_label(text, ("cif", "nif", "nif/cif", "identificacion fiscal"), max_length=40)
    match = TAX_ID_RE.search(labelled or "")
    if match:
        return match.group(0).upper()
    match = TAX_ID_RE.search(text or "")
    return match.group(0).upper() if match else ""


def find_website_from_informa(text: str) -> str:
    labelled = find_value_after_label(text, ("web", "pagina web", "sitio web"), max_length=120)
    candidates = [labelled, *(URL_RE.findall(text or "")[:20])]
    for candidate in candidates:
        cleaned = clean_informa_value(candidate, max_length=120)
        normalized = cleaned.lower()
        if not cleaned or "einforma" in normalized or "informa.es" in normalized or "informa.com" in normalized or "linkedin" in normalized:
            continue
        if not re.search(r"[a-z]", normalized):
            continue
        if "." not in cleaned or "@" in cleaned:
            continue
        if not normalized.startswith(("http://", "https://")):
            cleaned = f"https://{cleaned}"
        return cleaned
    return ""


def find_email_from_informa(text: str) -> str:
    labelled = find_value_after_label(text, ("email", "e-mail", "correo electronico"), max_length=120)
    match = EMAIL_RE.search(labelled or "")
    if match:
        return match.group(0)
    match = EMAIL_RE.search(text or "")
    return match.group(0) if match else ""


def find_phone_from_informa(text: str) -> str:
    labelled = find_value_after_label(text, ("telefono", "tel.", "tel "), max_length=80)
    match = PHONE_RE.search(labelled or "")
    if match:
        return clean_informa_value(match.group(0), max_length=60)
    match = PHONE_RE.search(text or "")
    return clean_informa_value(match.group(0), max_length=60) if match else ""


def find_cnae_from_informa(text: str) -> tuple[str, str]:
    raw = find_value_after_label(text, ("cnae", "actividad cnae", "actividad principal"), max_length=220)
    match = re.search(r"\b(\d{3,4})\b", raw)
    if match:
        code = match.group(1)
        label = clean_informa_value(raw.replace(code, " ", 1), max_length=180)
        return code, label
    return "", raw


def find_employees_from_informa(text: str):
    raw = find_value_after_label(text, ("empleados", "numero de empleados", "trabajadores"), max_length=80)
    match = re.search(r"\d{1,5}", raw.replace(".", ""))
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def parse_informa_company_fields(text: str) -> dict:
    cnae_code, cnae_label = find_cnae_from_informa(text)
    legal_name = find_company_name_from_informa(text)
    address = find_value_after_label(
        text,
        (
            "domicilio social",
            "domicilio",
            "direccion",
            "sede social",
        ),
        max_length=240,
    )
    city = find_value_after_label(text, ("localidad", "municipio", "poblacion"), max_length=100)
    province = find_value_after_label(text, ("provincia",), max_length=100)
    geography = " - ".join(item for item in (city, province) if item)
    if not geography:
        geography = province or city
    sector = cnae_label or find_value_after_label(text, ("actividad", "objeto social"), max_length=140)
    metrics = parse_balance_metrics(text)
    return {
        "company_name": legal_name,
        "legal_name": legal_name,
        "tax_id": find_tax_id_from_informa(text),
        "website": find_website_from_informa(text),
        "sector": sector,
        "geography": geography,
        "address": address,
        "phone": find_phone_from_informa(text),
        "email": find_email_from_informa(text),
        "cnae_code": cnae_code,
        "cnae_label": cnae_label,
        "employees": find_employees_from_informa(text),
        "annual_revenue": metrics.get("annual_revenue"),
        "ebitda": metrics.get("ebitda"),
        "source": "Informe Informa",
    }


def resolve_informa_opportunity(parsed_fields: dict, selected_opportunity: VentureOpportunity | None = None) -> tuple[VentureOpportunity, bool]:
    if selected_opportunity is not None:
        return selected_opportunity, False

    tax_id = str(parsed_fields.get("tax_id") or "").strip()
    if tax_id:
        match = VentureOpportunity.objects.filter(tax_id__iexact=tax_id).order_by("company_name").first()
        if match:
            return match, False

    company_name = str(parsed_fields.get("company_name") or parsed_fields.get("legal_name") or "").strip()
    if company_name:
        match = VentureOpportunity.objects.filter(company_name__iexact=company_name).first()
        if match:
            return match, False
        opportunity = VentureOpportunity.objects.create(
            company_name=company_name,
            legal_name=parsed_fields.get("legal_name", ""),
            tax_id=tax_id,
            source="Informe Informa",
        )
        return opportunity, True

    raise ValueError("No se ha podido detectar el nombre de la empresa en el informe Informa.")


def update_opportunity_from_informa(opportunity: VentureOpportunity, parsed_fields: dict, *, overwrite_existing: bool = False) -> list[str]:
    updated_fields = []
    field_names = (
        "company_name",
        "legal_name",
        "tax_id",
        "website",
        "sector",
        "geography",
        "address",
        "phone",
        "email",
        "cnae_code",
        "cnae_label",
        "employees",
        "annual_revenue",
        "ebitda",
        "source",
    )
    for field_name in field_names:
        value = parsed_fields.get(field_name)
        if value in (None, ""):
            continue
        current_value = getattr(opportunity, field_name)
        if overwrite_existing or current_value in (None, ""):
            if current_value != value:
                setattr(opportunity, field_name, value)
                updated_fields.append(field_name)
    if updated_fields:
        updated_fields.append("updated_at")
        opportunity.save(update_fields=updated_fields)
    return updated_fields


def import_informa_report(
    uploaded_file,
    *,
    selected_opportunity: VentureOpportunity | None = None,
    title: str = "",
    document_date=None,
    overwrite_existing: bool = False,
) -> dict:
    text = extract_pdf_text_from_file(uploaded_file)
    parsed_fields = parse_informa_company_fields(text)
    opportunity, created = resolve_informa_opportunity(parsed_fields, selected_opportunity)
    updated_fields = update_opportunity_from_informa(
        opportunity,
        parsed_fields,
        overwrite_existing=overwrite_existing,
    )
    document_title = clean_informa_value(title, max_length=180) or f"Informe Informa {timezone.localdate():%Y-%m-%d}"
    document = VentureDocument.objects.create(
        opportunity=opportunity,
        document_kind=VentureDocument.DocumentKind.INFORMA,
        title=document_title,
        document_date=document_date,
        file=uploaded_file,
        extracted_text=text,
        extraction_status=VentureDocument.ExtractionStatus.EXTRACTED if text else VentureDocument.ExtractionStatus.FAILED,
        extraction_error="" if text else "El informe no contiene texto extraible.",
        notes=json.dumps(
            {
                "created_opportunity": created,
                "updated_fields": updated_fields,
                "parsed_fields": {
                    key: str(value)
                    for key, value in parsed_fields.items()
                    if value not in (None, "")
                },
            },
            ensure_ascii=True,
        ),
    )
    return {
        "opportunity": opportunity,
        "document": document,
        "created": created,
        "updated_fields": updated_fields,
        "parsed_fields": parsed_fields,
    }


def normalize_company_name_candidate(value: str) -> str:
    cleaned = strip_html_tags(value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:|")
    cleaned = re.sub(r"\b(SL|SA)\b", lambda match: match.group(1).upper(), cleaned, flags=re.I)
    return cleaned[:180].strip()


def guess_company_name_from_upload(uploaded_file, text: str = "", fallback_name: str = "") -> str:
    fallback_name = normalize_company_name_candidate(fallback_name)
    if fallback_name:
        return fallback_name

    parsed_name = find_company_name_from_informa(text)
    if parsed_name:
        return parsed_name

    filename = Path(str(getattr(uploaded_file, "name", "") or "")).stem
    filename = re.sub(r"[_]+", " ", filename)
    filename = re.sub(r"\s+", " ", filename).strip()
    pieces = [piece.strip() for piece in re.split(r"\s+-\s+|\s+\|\s+", filename) if piece.strip()]
    for piece in reversed(pieces or [filename]):
        match = COMPANY_SUFFIX_RE.search(piece)
        if match:
            return normalize_company_name_candidate(match.group(1))
    return normalize_company_name_candidate(filename)


def build_opportunity_seed_from_pdf(uploaded_file, *, fallback_company_name: str = "") -> dict:
    text = ""
    parsed_fields = {}
    try:
        text = extract_pdf_text_from_file(uploaded_file)
    except Exception:
        text = ""
    if text:
        parsed_fields = parse_informa_company_fields(text)

    company_name = guess_company_name_from_upload(
        uploaded_file,
        text,
        fallback_name=fallback_company_name or parsed_fields.get("company_name", ""),
    )
    opportunity_field_names = {field.name for field in VentureOpportunity._meta.fields}
    seed_fields = {
        key: value
        for key, value in parsed_fields.items()
        if key in opportunity_field_names
    }
    seed_fields.pop("company_name", None)
    if company_name and not seed_fields.get("source"):
        seed_fields["source"] = "PDF financiero/comercial"
    return {
        "company_name": company_name,
        "fields": seed_fields,
        "text": text,
    }


def extract_candidate_company_name(title: str, description: str = "") -> str:
    text = f"{title} {description}"
    match = COMPANY_SUFFIX_RE.search(text)
    if match:
        return normalize_company_name_candidate(match.group(1))

    title = strip_html_tags(title)
    if " - " in title:
        title = title.rsplit(" - ", 1)[0]
    title = re.sub(
        r"\b(recibe|invierte|abre|lanza|crea|desarrolla|amplia|compra|vende|firma|presenta|obtiene|capta)\b.*",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(r"^[^:]{0,40}:\s*", "", title)
    return normalize_company_name_candidate(title)


def score_discovery_item(title: str, description: str, *, sector_focus: str = "") -> tuple[Decimal, list[str], str]:
    text = normalize_search_text(f"{title} {description} {sector_focus}")
    score = Decimal("45.00")
    tags = []
    rules = (
        ("neos-fit", ("ceramica", "azulejo", "esmalte", "frita", "aditivo", "material", "minerales"), Decimal("18")),
        ("crecimiento", ("amplia", "expansion", "crecimiento", "nueva planta", "inversion", "financiacion"), Decimal("14")),
        ("innovacion", ("i+d", "innovacion", "patente", "tecnologia", "sostenible", "circular", "recicl"), Decimal("12")),
        ("tension", ("reestructuracion", "concurso", "problemas", "deuda", "rescate", "necesita financiacion"), Decimal("8")),
        ("local", ("castellon", "vila-real", "onda", "alcora", "nules", "valencia"), Decimal("8")),
    )
    for tag, tokens, weight in rules:
        if any(token in text for token in tokens):
            score += weight
            tags.append(tag)
    score = min(score, Decimal("96.00"))
    if "premio" in text or "feria" in text:
        score -= Decimal("6")
    if len(strip_html_tags(title)) < 8:
        score -= Decimal("12")
    rationale = "Coincidencia web con " + ", ".join(tags) if tags else "Coincidencia general con el radar de empresas no cotizadas."
    return score.quantize(Decimal("0.01")), tags, rationale


def build_discovery_query(*, geography: str, sector_focus: str) -> str:
    geography = geography or "Castellon"
    sector_focus = sector_focus or "ceramica aditivos materiales industria"
    return (
        f'("{geography}") ({sector_focus}) '
        "(empresa OR pyme OR startup OR fabrica OR inversion OR ampliacion OR financiacion OR innovacion)"
    )


def discover_web_candidates(*, geography: str = "Castellon", sector_focus: str = "", max_candidates: int = 8) -> dict:
    query = build_discovery_query(geography=geography, sector_focus=sector_focus)
    try:
        signal = fetch_news_signal_for_query(
            query,
            label="Radar web",
            lookback_days=60,
            max_items=max(int(max_candidates or 8), 3),
        )
    except Exception as exc:
        signal = build_unavailable_news_signal(
            "Radar web",
            f"No se ha podido leer la web en este momento: {trim_text(str(exc), 220)}",
        )
    candidates = []
    created_count = 0
    updated_count = 0
    for item in signal.get("items") or []:
        company_name = extract_candidate_company_name(item.get("title", ""), item.get("description", ""))
        if not company_name:
            continue
        score_pct, tags, rationale = score_discovery_item(
            item.get("title", ""),
            item.get("description", ""),
            sector_focus=sector_focus,
        )
        defaults = {
            "sector": sector_focus[:140],
            "geography": geography[:120],
            "source_title": trim_text(item.get("title", ""), 240),
            "source_label": trim_text(item.get("source", ""), 120),
            "summary": trim_text(item.get("description", "") or item.get("title", ""), 700),
            "rationale": rationale,
            "score_pct": score_pct,
            "tags": tags,
        }
        source_url = str(item.get("link") or "").strip()[:1000]
        candidate, created = VentureDiscoveryCandidate.objects.update_or_create(
            company_name=company_name,
            source_url=source_url,
            defaults={
                **defaults,
                "source_url": source_url,
            },
        )
        if created:
            created_count += 1
        else:
            updated_count += 1
        candidates.append(candidate)
    return {
        "query": query,
        "signal": signal,
        "candidates": candidates,
        "created_count": created_count,
        "updated_count": updated_count,
    }


def promote_discovery_candidate(candidate: VentureDiscoveryCandidate) -> VentureOpportunity:
    opportunity, _ = VentureOpportunity.objects.update_or_create(
        company_name=candidate.company_name,
        defaults={
            "sector": candidate.sector,
            "geography": candidate.geography,
            "stage": VentureOpportunity.Stage.EARLY,
            "status": VentureOpportunity.Status.SCREENING,
            "strategic_fit": VentureOpportunity.StrategicFit.BOTH,
            "source": "Radar web",
            "fit_summary": candidate.rationale,
            "next_steps": "Validar empresa, pedir Informe Informa y revisar encaje con Neos.",
        },
    )
    candidate.status = VentureDiscoveryCandidate.Status.PROMOTED
    candidate.promoted_opportunity = opportunity
    candidate.save(update_fields=["status", "promoted_opportunity", "updated_at"])
    return opportunity


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


def _analysis_json_payload(
    opportunity: VentureOpportunity,
    metrics: dict,
    web_context: dict,
    core_payload: dict,
    text: str,
    document: VentureDocument | None = None,
) -> dict:
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
        "source_document": {
            "title": document.title if document else "",
            "kind": document.get_document_kind_display() if document else "",
            "fiscal_year": document.fiscal_year if document else None,
            "document_date": document.document_date.isoformat() if document and document.document_date else "",
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
        "document_text_excerpt": trim_text(text, 14000),
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


def normalize_ai_opportunity_updates(payload: dict) -> dict:
    raw_updates = (
        payload.get("opportunity_updates")
        or payload.get("company_updates")
        or payload.get("form_fields")
        or {}
    )
    if not isinstance(raw_updates, dict):
        raw_updates = {}

    updates = {}
    for field_name, max_length in AI_TEXT_UPDATE_FIELDS.items():
        value = normalize_ai_text_value(raw_updates.get(field_name), max_length=max_length)
        if field_name == "website":
            value = normalize_website_url(value)
        if field_name == "tax_id":
            value = value.upper()
        if value:
            updates[field_name] = value

    employees = int_or_none(raw_updates.get("employees"))
    if employees is not None and employees >= 0:
        updates["employees"] = employees

    next_review_on = raw_updates.get("next_review_on")
    if next_review_on:
        parsed_review_date = parse_date(str(next_review_on).strip())
        if parsed_review_date:
            updates["next_review_on"] = parsed_review_date.isoformat()

    for field_name in AI_DECIMAL_UPDATE_FIELDS:
        value = decimal_or_none(raw_updates.get(field_name))
        if value is not None:
            updates[field_name] = str(value)

    for field_name in AI_SCORE_UPDATE_FIELDS:
        value = int_or_none(raw_updates.get(field_name))
        if value is not None:
            updates[field_name] = min(max(value, 1), 5)

    for field_name in AI_CHOICE_UPDATE_FIELDS:
        value = normalize_ai_choice_value(field_name, raw_updates.get(field_name))
        if value:
            updates[field_name] = value

    return updates


def apply_ai_opportunity_updates(opportunity: VentureOpportunity, payload: dict) -> list[str]:
    updates = dict(payload.get("opportunity_updates") or {})
    for field_name in ("annual_revenue", "ebitda", "cash_need"):
        if payload.get(field_name) is not None and field_name not in updates:
            updates[field_name] = str(payload[field_name])
    if payload.get("valuation_base") is not None and "estimated_valuation" not in updates:
        updates["estimated_valuation"] = str(payload["valuation_base"])
    if payload.get("suggested_ticket") is not None and "ticket_max" not in updates:
        updates["ticket_max"] = str(payload["suggested_ticket"])

    changed_fields = []
    protected_statuses = {VentureOpportunity.Status.APPROVED, VentureOpportunity.Status.REJECTED}

    for field_name, raw_value in updates.items():
        if field_name in AI_TEXT_UPDATE_FIELDS:
            current_value = getattr(opportunity, field_name)
            if current_value:
                continue
            value = normalize_ai_text_value(raw_value, max_length=AI_TEXT_UPDATE_FIELDS[field_name])
            if field_name == "website":
                value = normalize_website_url(value)
            if field_name == "tax_id":
                value = value.upper()
            if value:
                setattr(opportunity, field_name, value)
                changed_fields.append(field_name)
            continue

        if field_name == "employees":
            if opportunity.employees is not None:
                continue
            value = int_or_none(raw_value)
            if value is not None and value >= 0:
                opportunity.employees = value
                changed_fields.append(field_name)
            continue

        if field_name == "next_review_on":
            if opportunity.next_review_on:
                continue
            value = parse_date(str(raw_value).strip())
            if value:
                opportunity.next_review_on = value
                changed_fields.append(field_name)
            continue

        if field_name in AI_DECIMAL_UPDATE_FIELDS:
            current_value = getattr(opportunity, field_name)
            if current_value is not None:
                continue
            value = decimal_or_none(raw_value)
            if value is not None:
                setattr(opportunity, field_name, value)
                changed_fields.append(field_name)
            continue

        if field_name in AI_SCORE_UPDATE_FIELDS:
            value = int_or_none(raw_value)
            if value is not None:
                value = min(max(value, 1), 5)
                if getattr(opportunity, field_name) != value:
                    setattr(opportunity, field_name, value)
                    changed_fields.append(field_name)
            continue

        if field_name in AI_CHOICE_UPDATE_FIELDS:
            if field_name == "status" and opportunity.status in protected_statuses:
                continue
            value = normalize_ai_choice_value(field_name, raw_value)
            if value and getattr(opportunity, field_name) != value:
                setattr(opportunity, field_name, value)
                changed_fields.append(field_name)

    if changed_fields:
        opportunity.save(update_fields=sorted(set(changed_fields + ["updated_at"])))
    return sorted(set(changed_fields))


def try_ai_venture_analysis(opportunity, metrics, web_context, core_payload, text, *, document=None, enabled=True) -> dict | None:
    if not enabled:
        return None
    config = resolve_ai_provider_config()
    if not config.available:
        return None

    system_prompt = (
        "Eres un analista de inversion en empresas no cotizadas industriales complementarias a Neos Ceramica y Neos Additives. "
        "Trabajas solo con el JSON recibido: datos manuales, texto extraido del PDF y contexto web. "
        "El PDF puede ser un balance, cuentas anuales, un dossier financiero/comercial, un deck comercial o un informe mixto. "
        "Evalua finanzas, calidad comercial, clientes, recurrencia, cartera de pedidos, pipeline, pricing, canales, dependencia de clientes, equipo y encaje industrial con Neos. "
        "No inventes cifras. Si una cifra no aparece, dilo. Devuelve solo JSON valido. "
        "La recomendacion debe ser buy o watch. El precio sugerido es orientativo para el 100 % de la empresa, con margen de seguridad."
    )
    user_payload = _analysis_json_payload(opportunity, metrics, web_context, core_payload, text, document=document)
    user_prompt = (
        "Analiza esta oportunidad no cotizada usando la informacion financiera y comercial disponible. "
        "Devuelve JSON con: recommendation, confidence, score_pct, "
        "suggested_purchase_price, valuation_low, valuation_base, valuation_high, suggested_ticket, target_ownership_pct, "
        "annual_revenue, ebitda, cash_need, summary, valuation_note, web_summary, drivers, risks y assumptions. "
        "Incluye tambien opportunity_updates con los campos del formulario que puedas completar sin inventar: "
        "legal_name, tax_id, website, sector, geography, address, phone, email, cnae_code, cnae_label, employees, "
        "stage, status, strategic_fit, contact_name, source, next_review_on, ticket_min, ticket_max, estimated_valuation, "
        "annual_revenue, ebitda, cash_need, neos_fit_score, market_score, team_score, financial_score, risk_control_score, "
        "fit_summary, growth_issue, synergy_notes, diligence_notes, red_flags y next_steps. "
        "Usa fechas ISO YYYY-MM-DD, importes numericos sin simbolos, scores enteros de 1 a 5, y deja vacio cualquier campo no soportado por la informacion. JSON de entrada: "
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
        "annual_revenue": _parse_ai_decimal(payload, "annual_revenue") or core_payload["annual_revenue"],
        "ebitda": _parse_ai_decimal(payload, "ebitda") or core_payload["ebitda"],
        "cash_need": _parse_ai_decimal(payload, "cash_need") or core_payload["cash_need"],
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
            "opportunity_updates": normalize_ai_opportunity_updates(payload),
        },
        "opportunity_updates": normalize_ai_opportunity_updates(payload),
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
        document=document,
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
    updated_fields = apply_ai_opportunity_updates(opportunity, payload)
    if updated_fields:
        snapshot.analysis_payload = {
            **(snapshot.analysis_payload or {}),
            "applied_opportunity_updates": updated_fields,
        }
        snapshot.save(update_fields=["analysis_payload"])
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
