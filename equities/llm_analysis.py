from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone

from .models import EquityNightlyAnalysisRun


ZERO = Decimal("0.00")
ONE_MILLION = Decimal("1000000")
SUPPORTED_ACTIONS = {"Comprar", "Mantener", "Vigilar", "Reducir", "Vender"}
SUPPORTED_CONFIDENCE = {"Alta", "Media", "Baja"}


@dataclass
class ProviderConfig:
    provider: str
    label: str
    model: str
    api_key: str
    max_tokens: int
    timeout_seconds: int
    retry_attempts: int
    rate_limit_retry_seconds: int
    monthly_budget_usd: Decimal
    pricing: dict[str, dict[str, Decimal]]
    available: bool
    reason: str = ""


def decimal_to_str(value: Decimal | None, places: str = "0.01") -> str | None:
    if value is None:
        return None
    return str(value.quantize(Decimal(places)))


def humanize_model_label(provider: str, model: str) -> str:
    if provider == "anthropic":
        return f"Claude {model}"
    if provider == "openai":
        return f"ChatGPT {model}"
    return model or "Analista IA"


def normalize_pricing(raw_pricing: dict | None) -> dict[str, dict[str, Decimal]]:
    normalized: dict[str, dict[str, Decimal]] = {}
    for model, price_map in (raw_pricing or {}).items():
        normalized[str(model)] = {
            "input": Decimal(str((price_map or {}).get("input", "0"))),
            "output": Decimal(str((price_map or {}).get("output", "0"))),
        }
    return normalized


def resolve_ai_provider_config() -> ProviderConfig:
    provider = str(getattr(settings, "AI_LLM_PROVIDER", "anthropic") or "anthropic").strip().lower()
    timeout_seconds = max(int(getattr(settings, "AI_LLM_REQUEST_TIMEOUT_SECONDS", 45) or 45), 10)
    retry_attempts = max(int(getattr(settings, "AI_LLM_RETRY_ATTEMPTS", 4) or 4), 1)
    rate_limit_retry_seconds = max(int(getattr(settings, "AI_LLM_RATE_LIMIT_RETRY_SECONDS", 15) or 15), 1)

    if provider == "anthropic":
        model = str(getattr(settings, "CLAUDE_DEFAULT_MODEL", "claude-sonnet-4-20250514") or "claude-sonnet-4-20250514").strip()
        api_key = str(getattr(settings, "ANTHROPIC_API_KEY", "") or "").strip()
        pricing = normalize_pricing(getattr(settings, "CLAUDE_PRICING", {}))
        monthly_budget_usd = Decimal(str(getattr(settings, "CLAUDE_MONTHLY_BUDGET_USD", ZERO) or ZERO))
        label = humanize_model_label(provider, model)
        return ProviderConfig(
            provider=provider,
            label=label,
            model=model,
            api_key=api_key,
            max_tokens=max(int(getattr(settings, "CLAUDE_MAX_TOKENS", 1024) or 1024), 256),
            timeout_seconds=timeout_seconds,
            retry_attempts=retry_attempts,
            rate_limit_retry_seconds=rate_limit_retry_seconds,
            monthly_budget_usd=monthly_budget_usd,
            pricing=pricing,
            available=bool(api_key and model),
            reason="" if api_key and model else "Configura ANTHROPIC_API_KEY para activar el analista IA.",
        )

    if provider == "openai":
        model = str(getattr(settings, "OPENAI_DEFAULT_MODEL", "gpt-4o-mini") or "gpt-4o-mini").strip()
        api_key = str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()
        pricing = normalize_pricing(getattr(settings, "OPENAI_PRICING", {}))
        monthly_budget_usd = Decimal(
            str(
                getattr(
                    settings,
                    "OPENAI_MONTHLY_BUDGET_USD",
                    getattr(settings, "AI_LLM_MONTHLY_BUDGET_USD", ZERO),
                )
                or ZERO
            )
        )
        label = humanize_model_label(provider, model)
        return ProviderConfig(
            provider=provider,
            label=label,
            model=model,
            api_key=api_key,
            max_tokens=max(int(getattr(settings, "OPENAI_MAX_TOKENS", 2048) or 2048), 256),
            timeout_seconds=timeout_seconds,
            retry_attempts=retry_attempts,
            rate_limit_retry_seconds=rate_limit_retry_seconds,
            monthly_budget_usd=monthly_budget_usd,
            pricing=pricing,
            available=bool(api_key and model),
            reason="" if api_key and model else "Configura OPENAI_API_KEY para activar el analista IA.",
        )

    return ProviderConfig(
        provider=provider or "core",
        label="Analista cuantitativo",
        model="",
        api_key="",
        max_tokens=0,
        timeout_seconds=timeout_seconds,
        retry_attempts=retry_attempts,
        rate_limit_retry_seconds=rate_limit_retry_seconds,
        monthly_budget_usd=ZERO,
        pricing={},
        available=False,
        reason=f"AI_LLM_PROVIDER={provider!r} no esta soportado. Usa 'anthropic' u 'openai'.",
    )


def estimate_cost_usd(config: ProviderConfig, input_tokens: int, output_tokens: int) -> Decimal:
    model_pricing = config.pricing.get(config.model)
    if not model_pricing:
        return ZERO
    input_cost = (Decimal(str(input_tokens)) / ONE_MILLION) * model_pricing.get("input", ZERO)
    output_cost = (Decimal(str(output_tokens)) / ONE_MILLION) * model_pricing.get("output", ZERO)
    return (input_cost + output_cost).quantize(Decimal("0.0001"))


def current_month_llm_usage(provider: str, analysis_date: date | None = None) -> dict:
    analysis_date = analysis_date or timezone.localdate()
    month_start = analysis_date.replace(day=1)
    if analysis_date.month == 12:
        month_end = analysis_date.replace(year=analysis_date.year + 1, month=1, day=1)
    else:
        month_end = analysis_date.replace(month=analysis_date.month + 1, day=1)

    usage = {
        "runs_count": 0,
        "completed_count": 0,
        "failed_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_usd": ZERO,
    }
    queryset = EquityNightlyAnalysisRun.objects.filter(
        analysis_date__gte=month_start,
        analysis_date__lt=month_end,
    ).order_by("analysis_date")
    for run in queryset:
        llm_data = (run.summary_data or {}).get("llm") or {}
        if llm_data.get("provider") != provider:
            continue
        usage["runs_count"] += 1
        if llm_data.get("completed_count"):
            usage["completed_count"] += 1
        if llm_data.get("failed_count"):
            usage["failed_count"] += int(llm_data.get("failed_count") or 0)
        usage["input_tokens"] += int(llm_data.get("input_tokens") or 0)
        usage["output_tokens"] += int(llm_data.get("output_tokens") or 0)
        usage["estimated_cost_usd"] += Decimal(str(llm_data.get("estimated_cost_usd") or "0"))
    usage["estimated_cost_usd"] = usage["estimated_cost_usd"].quantize(Decimal("0.0001"))
    return usage


def json_ready_number(value, places: str = "0.01"):
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal(places)))


def trim_text(value: str, max_length: int) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    if len(cleaned) <= max_length:
        return cleaned
    return cleaned[: max(max_length - 1, 1)].rstrip() + "…"


def build_card_llm_context(card: dict, *, analysis_date: date, scope: str) -> dict:
    position = card["position"]
    projection = card.get("projection") or {}
    cycle_projection = card.get("cycle_projection_5y") or {}
    projection_backtest = card.get("projection_backtest") or {}
    correlation = card.get("correlation") or {}
    trade_alert = card.get("trade_alert") or {}
    reliability = card.get("projection_reliability") or {}
    sale_preview = card.get("sale_preview") or {}
    snapshots_by_label = {
        snapshot.get("label"): snapshot
        for snapshot in (card.get("period_snapshots") or [])
        if snapshot.get("available")
    }

    def build_path_steps(path_rows, *, value_key: str, step_limit: int = 6):
        rows = []
        for row in list(path_rows or [])[:step_limit]:
            rows.append(
                {
                    "date": row.get("projected_date").isoformat() if row.get("projected_date") else "",
                    "value": json_ready_number(row.get(value_key), "0.0001"),
                }
            )
        return rows

    context = {
        "analysis_date": analysis_date.isoformat(),
        "scope": scope,
        "company": {
            "ticker": position.ticker,
            "quote_symbol": position.quote_symbol,
            "company_name": position.company_name,
            "sector": card.get("sector_label") or "",
            "status": card.get("status_label") or "",
            "opened_on": position.opened_on.isoformat() if position.opened_on else None,
            "latest_price_date": position.latest_price_date.isoformat() if position.latest_price_date else None,
            "current_price": json_ready_number(position.current_price_per_share, "0.0001"),
            "reference_label": card.get("reference_label") or "",
        },
        "twelve_month_view": {
            "available": bool(projection.get("available")),
            "projected_total_return_pct": json_ready_number(projection.get("base_return_pct")),
            "projected_price_return_pct": json_ready_number(projection.get("price_return_pct")),
            "projected_price": json_ready_number(projection.get("projected_price"), "0.0001"),
            "low_return_pct": json_ready_number(projection.get("low_return_pct")),
            "high_return_pct": json_ready_number(projection.get("high_return_pct")),
            "safety_score": json_ready_number(projection.get("safety_score"), "0.1"),
            "confidence_label": projection.get("confidence_label"),
            "volatility_pct": json_ready_number(projection.get("annualized_volatility_pct")),
            "positive_year_ratio_pct": json_ready_number(projection.get("positive_year_ratio_pct")),
            "current_drawdown_pct": json_ready_number(projection.get("current_drawdown_pct")),
            "max_drawdown_pct": json_ready_number(projection.get("max_drawdown_pct")),
            "net_income_yield_pct": json_ready_number(projection.get("net_income_yield_pct")),
            "transaction_drag_pct": json_ready_number(projection.get("transaction_drag_pct")),
            "quarterly_path": build_path_steps(projection.get("quarterly_path"), value_key="projected_price", step_limit=4),
        },
        "five_year_view": {
            "available": bool(cycle_projection.get("available")),
            "annual_return_pct": json_ready_number(cycle_projection.get("annual_return_pct")),
            "five_year_return_pct": json_ready_number(cycle_projection.get("five_year_return_pct")),
            "projected_price": json_ready_number(cycle_projection.get("projected_price"), "0.0001"),
            "cycle_phase": cycle_projection.get("cycle_phase"),
            "analysis_years_used": json_ready_number(cycle_projection.get("analysis_years_used"), "0.1"),
            "half_year_path": build_path_steps(cycle_projection.get("path"), value_key="projected_price"),
        },
        "backtest": {
            "available": bool(projection_backtest.get("available")),
            "precision_label": projection_backtest.get("precision_label"),
            "comparisons_count": int(projection_backtest.get("comparisons_count") or 0),
            "mean_absolute_error_pct": json_ready_number(projection_backtest.get("mean_absolute_error_pct")),
            "direction_hit_rate_pct": json_ready_number(projection_backtest.get("direction_hit_rate_pct")),
            "in_range_rate_pct": json_ready_number(projection_backtest.get("in_range_rate_pct")),
        },
        "reference": {
            "correlation_10y": json_ready_number(correlation.get("coefficient"), "0.01"),
            "recent_correlation": json_ready_number(correlation.get("recent_coefficient"), "0.01"),
            "stability_label": correlation.get("stability_label"),
        },
        "decision": {
            "trade_alert_label": trade_alert.get("label") or "Vigilar",
            "trade_alert_trigger": trade_alert.get("trigger_label") or "",
            "trade_alert_note": trim_text(trade_alert.get("note") or "", 220),
            "reliability_label": reliability.get("label") or "Baja",
            "reliability_score": json_ready_number(reliability.get("score"), "0.1"),
        },
        "history_windows": {
            "one_year_stock_return_pct": json_ready_number((snapshots_by_label.get("1Y") or {}).get("stock_return_pct")),
            "one_year_reference_return_pct": json_ready_number((snapshots_by_label.get("1Y") or {}).get("benchmark_return_pct")),
            "five_year_stock_return_pct": json_ready_number((snapshots_by_label.get("5Y") or {}).get("stock_return_pct")),
            "five_year_reference_return_pct": json_ready_number((snapshots_by_label.get("5Y") or {}).get("benchmark_return_pct")),
        },
        "portfolio_context": {
            "is_owned": bool(position.is_owned),
            "shares": json_ready_number(position.shares, "0.0001"),
            "invested_amount_eur": json_ready_number(getattr(position, "invested_amount", None)),
            "current_value_eur": json_ready_number(getattr(position, "current_value", None)),
            "net_unrealized_return_pct": json_ready_number(card.get("net_unrealized_return_pct")),
            "annualized_margin_pct": json_ready_number(sale_preview.get("annualized_margin_pct")) if sale_preview.get("available") else None,
        },
    }
    return context


def build_system_prompt() -> str:
    return (
        "Eres el analista nocturno de un dashboard de inversion en acciones. "
        "Trabajas SOLO con el JSON cuantitativo que recibes; no inventes noticias, resultados ni fundamentales externos. "
        "Responde siempre en espanol y devuelve un JSON valido, sin markdown ni texto extra. "
        "La salida debe incluir: summary, action_label, action_note, confidence_label, drivers, risks, backtest_note y cycle_note. "
        "summary debe ser una sintesis clara de 2 a 4 frases. drivers y risks deben tener entre 1 y 3 elementos cada uno. "
        "action_label debe ser una de estas opciones: Comprar, Mantener, Vigilar, Reducir, Vender. "
        "confidence_label debe ser Alta, Media o Baja. "
        "El objetivo es explicar de forma potente pero compacta el escenario 12M, la lectura 5A y la validacion historica del modelo."
    )


def build_user_prompt(card_context: dict) -> str:
    payload = json.dumps(card_context, ensure_ascii=True, separators=(",", ":"))
    return (
        "Analiza esta empresa del IBEX o de la cartera usando exclusivamente este JSON cuantitativo. "
        "Prioriza rentabilidad 12M, ciclo 5A, backtest y coherencia con la alerta cuantitativa. "
        "Si la fiabilidad o el historico son flojos, dilo claramente. JSON de entrada: "
        f"{payload}"
    )


def extract_openai_text(response_payload: dict) -> str:
    choices = response_payload.get("choices") or []
    if not choices:
        raise ValueError("OpenAI no ha devuelto choices.")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                parts.append(item.get("text") or "")
        if parts:
            return "".join(parts)
    raise ValueError("OpenAI no ha devuelto texto interpretable.")


def extract_anthropic_text(response_payload: dict) -> str:
    content_items = response_payload.get("content") or []
    parts = []
    for item in content_items:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text") or "")
    if not parts:
        raise ValueError("Anthropic no ha devuelto texto interpretable.")
    return "".join(parts)


def strip_code_fences(text: str) -> str:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


def parse_agent_json(text: str) -> dict:
    cleaned = strip_code_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def resolve_rate_limit_delay_seconds(exc: HTTPError, *, attempt: int, base_delay_seconds: int) -> int:
    delay_seconds = max(base_delay_seconds * max(attempt, 1), 1)
    headers = getattr(exc, "headers", None) or getattr(exc, "hdrs", None)
    if hasattr(headers, "get"):
        retry_after = str(headers.get("Retry-After", "") or headers.get("retry-after", "")).strip()
        if retry_after:
            try:
                delay_seconds = max(delay_seconds, int(float(retry_after)))
            except ValueError:
                pass
    return min(delay_seconds, 300)


def post_json(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str],
    timeout_seconds: int,
    retry_attempts: int = 1,
    rate_limit_retry_seconds: int = 15,
) -> dict:
    total_attempts = max(int(retry_attempts or 1), 1)
    for attempt in range(1, total_attempts + 1):
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            if exc.code == 429 and attempt < total_attempts:
                time.sleep(
                    resolve_rate_limit_delay_seconds(
                        exc,
                        attempt=attempt,
                        base_delay_seconds=rate_limit_retry_seconds,
                    )
                )
                continue
            raise RuntimeError(f"HTTP {exc.code}: {trim_text(body or exc.reason, 240)}") from exc
        except URLError as exc:
            raise RuntimeError(f"Error de red: {exc.reason}") from exc
    raise RuntimeError("No se ha podido completar la peticion IA.")


def call_openai_agent(config: ProviderConfig, *, system_prompt: str, user_prompt: str) -> tuple[dict, dict]:
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": config.max_tokens,
        "response_format": {"type": "json_object"},
    }
    response_payload = post_json(
        "https://api.openai.com/v1/chat/completions",
        payload,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        timeout_seconds=config.timeout_seconds,
        retry_attempts=config.retry_attempts,
        rate_limit_retry_seconds=config.rate_limit_retry_seconds,
    )
    usage = response_payload.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    return parse_agent_json(extract_openai_text(response_payload)), {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": estimate_cost_usd(config, input_tokens, output_tokens),
    }


def call_anthropic_agent(config: ProviderConfig, *, system_prompt: str, user_prompt: str) -> tuple[dict, dict]:
    payload = {
        "model": config.model,
        "system": system_prompt,
        "max_tokens": config.max_tokens,
        "temperature": 0.2,
        "messages": [
            {
                "role": "user",
                "content": user_prompt,
            }
        ],
    }
    response_payload = post_json(
        "https://api.anthropic.com/v1/messages",
        payload,
        headers={
            "x-api-key": config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        timeout_seconds=config.timeout_seconds,
        retry_attempts=config.retry_attempts,
        rate_limit_retry_seconds=config.rate_limit_retry_seconds,
    )
    usage = response_payload.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    return parse_agent_json(extract_anthropic_text(response_payload)), {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": estimate_cost_usd(config, input_tokens, output_tokens),
    }


def compute_consistency_label(agent_action: str, quant_action: str) -> tuple[str, str]:
    if not quant_action:
        return "Mixto", "La alerta cuantitativa no estaba disponible para contrastar la lectura IA."
    if agent_action == quant_action:
        return "Alineado", f"La IA y el motor cuantitativo coinciden en {quant_action.lower()}."
    soft_pairs = {
        ("Mantener", "Vigilar"),
        ("Reducir", "Vender"),
        ("Comprar", "Vigilar"),
    }
    if (agent_action, quant_action) in soft_pairs or (quant_action, agent_action) in soft_pairs:
        return "Mixto", f"La IA matiza la alerta cuantitativa: IA {agent_action.lower()} frente a motor {quant_action.lower()}."
    return "Contradictorio", f"La IA contradice al motor cuantitativo: IA {agent_action.lower()} frente a motor {quant_action.lower()}."


def normalize_text_list(value, *, fallback: str) -> list[str]:
    rows = []
    for item in value or []:
        cleaned = trim_text(item, 180)
        if cleaned:
            rows.append(cleaned)
    if rows:
        return rows[:3]
    return [fallback]


def normalize_agent_response(raw_payload: dict, *, card: dict, config: ProviderConfig, usage: dict) -> dict:
    trade_alert = card.get("trade_alert") or {}
    fallback_action = trade_alert.get("label") or "Vigilar"
    action_label = str(raw_payload.get("action_label") or fallback_action).strip().title()
    if action_label not in SUPPORTED_ACTIONS:
        action_label = fallback_action if fallback_action in SUPPORTED_ACTIONS else "Vigilar"

    confidence_label = str(raw_payload.get("confidence_label") or (card.get("projection_reliability") or {}).get("label") or "Media").strip().title()
    if confidence_label not in SUPPORTED_CONFIDENCE:
        confidence_label = "Media"

    consistency_label, consistency_note = compute_consistency_label(action_label, fallback_action)
    generated_at = timezone.localtime()
    return {
        "available": True,
        "provider": config.provider,
        "label": config.label,
        "model": config.model,
        "model_label": config.label,
        "summary": trim_text(raw_payload.get("summary") or trade_alert.get("note") or "La IA no ha devuelto una sintesis util.", 520),
        "action_label": action_label,
        "action_note": trim_text(raw_payload.get("action_note") or trade_alert.get("trigger_label") or "", 220),
        "confidence_label": confidence_label,
        "drivers": normalize_text_list(
            raw_payload.get("drivers"),
            fallback="La lectura 12M sigue siendo la pieza que mas pesa en la tesis actual.",
        ),
        "risks": normalize_text_list(
            raw_payload.get("risks"),
            fallback="La principal cautela es que la serie historica no garantiza que el escenario vuelva a repetirse.",
        ),
        "backtest_note": trim_text(
            raw_payload.get("backtest_note")
            or (card.get("projection_backtest") or {}).get("plain_explanation")
            or "Sin backtest suficiente para validar el modelo.",
            220,
        ),
        "cycle_note": trim_text(
            raw_payload.get("cycle_note")
            or (card.get("cycle_projection_5y") or {}).get("explanation")
            or "Sin ciclo 5A disponible.",
            220,
        ),
        "consistency_label": consistency_label,
        "consistency_note": consistency_note,
        "generated_at": generated_at.isoformat(),
        "generated_at_label": generated_at.strftime("%Y-%m-%d %H:%M"),
        "usage": {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "estimated_cost_usd": str(usage.get("estimated_cost_usd") or ZERO),
        },
    }


def build_ai_unavailable_payload(config: ProviderConfig, note: str) -> dict:
    return {
        "available": False,
        "provider": config.provider,
        "label": config.label,
        "model": config.model,
        "model_label": config.label,
        "note": note,
    }


def analyze_card_with_ai(card: dict, *, analysis_date: date, scope: str, config: ProviderConfig) -> tuple[dict, dict]:
    card_context = build_card_llm_context(card, analysis_date=analysis_date, scope=scope)
    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(card_context)
    if config.provider == "anthropic":
        raw_payload, usage = call_anthropic_agent(config, system_prompt=system_prompt, user_prompt=user_prompt)
    elif config.provider == "openai":
        raw_payload, usage = call_openai_agent(config, system_prompt=system_prompt, user_prompt=user_prompt)
    else:
        raise RuntimeError(f"Proveedor {config.provider!r} no soportado.")
    return normalize_agent_response(raw_payload, card=card, config=config, usage=usage), usage


def enrich_dashboard_with_ai_analysis(dashboard: dict, *, analysis_date: date) -> dict:
    config = resolve_ai_provider_config()
    if not config.available:
        return {
            "enabled": False,
            "provider": config.provider,
            "label": config.label,
            "model": config.model,
            "reason": config.reason,
            "completed_count": 0,
            "failed_count": 0,
            "skipped_budget_count": 0,
            "total_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": "0",
            "monthly_budget_usd": str(config.monthly_budget_usd),
            "monthly_cost_before_run_usd": "0",
            "monthly_cost_after_run_usd": "0",
        }

    cards = [
        *((dashboard.get("history_cards") or [])),
        *((dashboard.get("ibex_universe_cards") or [])),
    ]
    monthly_usage_before = current_month_llm_usage(config.provider, analysis_date)
    total_cost = Decimal(str(monthly_usage_before.get("estimated_cost_usd") or "0"))
    budget = config.monthly_budget_usd

    summary = {
        "enabled": True,
        "provider": config.provider,
        "label": config.label,
        "model": config.model,
        "reason": "",
        "total_count": len(cards),
        "completed_count": 0,
        "failed_count": 0,
        "skipped_budget_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_usd": ZERO,
        "monthly_budget_usd": budget,
        "monthly_cost_before_run_usd": total_cost,
        "monthly_cost_after_run_usd": total_cost,
        "failures": [],
    }

    for card in cards:
        if budget > ZERO and total_cost >= budget:
            card["ai_analysis"] = build_ai_unavailable_payload(
                config,
                "Analisis IA omitido para respetar el presupuesto mensual configurado.",
            )
            summary["skipped_budget_count"] += 1
            continue

        scope = "tracked" if card in (dashboard.get("history_cards") or []) else "ibex"
        try:
            ai_analysis, usage = analyze_card_with_ai(
                card,
                analysis_date=analysis_date,
                scope=scope,
                config=config,
            )
            card["ai_analysis"] = ai_analysis
            summary["completed_count"] += 1
            summary["input_tokens"] += int(usage.get("input_tokens") or 0)
            summary["output_tokens"] += int(usage.get("output_tokens") or 0)
            summary["estimated_cost_usd"] += Decimal(str(usage.get("estimated_cost_usd") or "0"))
            total_cost += Decimal(str(usage.get("estimated_cost_usd") or "0"))
        except Exception as exc:
            card["ai_analysis"] = build_ai_unavailable_payload(
                config,
                f"Analisis IA no disponible: {trim_text(str(exc), 180)}",
            )
            summary["failed_count"] += 1
            if len(summary["failures"]) < 8:
                summary["failures"].append(
                    {
                        "ticker": card["position"].ticker,
                        "company_name": card["position"].company_name,
                        "error": trim_text(str(exc), 180),
                    }
                )

    summary["estimated_cost_usd"] = summary["estimated_cost_usd"].quantize(Decimal("0.0001"))
    summary["monthly_cost_after_run_usd"] = total_cost.quantize(Decimal("0.0001"))
    summary["monthly_budget_usd"] = budget.quantize(Decimal("0.0001")) if budget > ZERO else ZERO
    summary["monthly_cost_before_run_usd"] = Decimal(str(summary["monthly_cost_before_run_usd"])).quantize(Decimal("0.0001"))

    return {
        **summary,
        "estimated_cost_usd": str(summary["estimated_cost_usd"]),
        "monthly_budget_usd": str(summary["monthly_budget_usd"]),
        "monthly_cost_before_run_usd": str(summary["monthly_cost_before_run_usd"]),
        "monthly_cost_after_run_usd": str(summary["monthly_cost_after_run_usd"]),
    }
