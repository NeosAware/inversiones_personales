from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .llm_analysis import enrich_dashboard_with_ai_analysis, resolve_ai_provider_config
from .models import (
    EquityNightlyAnalysisRun,
    EquityNightlyAnalysisSnapshot,
    EquityPosition,
)
from .services import (
    ZERO,
    build_analysis_broker_costs,
    build_equity_analysis_dashboard,
    build_equity_analysis_overview,
    build_equity_decision_rows,
    build_equity_reference_guide,
    build_equity_sale_preview,
    build_optimizer_master_cards,
    capture_equity_ticket_snapshots,
    clean_ticker,
    clear_market_data_caches,
    percentage_change,
    quantize_decimal,
    resolve_analysis_broker_profile,
    sync_all_equities_market_data,
)


JSON_TYPE_KEY = "__equity_cached_type__"
POSITION_SIGNATURE_FIELDS = (
    "id",
    "position_kind",
    "ownership_category",
    "broker",
    "ticker",
    "quote_symbol",
    "reference_profile",
    "benchmark_symbol",
    "benchmark_name",
    "company_name",
    "trade_channel",
    "opened_on",
    "shares",
    "average_cost_per_share",
    "annual_dividend_income",
    "annual_maintenance_cost",
    "notes",
)
POSITION_CACHE_FIELDS = (
    "position_kind",
    "ownership_category",
    "broker",
    "ticker",
    "quote_symbol",
    "reference_profile",
    "benchmark_symbol",
    "benchmark_name",
    "company_name",
    "trade_channel",
    "opened_on",
    "shares",
    "average_cost_per_share",
    "current_price_per_share",
    "annual_dividend_income",
    "annual_maintenance_cost",
    "latest_price_date",
    "last_synced_at",
    "notes",
)


def nightly_analysis_enabled() -> bool:
    return bool(getattr(settings, "EQUITIES_NIGHTLY_ANALYSIS_ENABLED", True))


def nightly_analysis_start_hour() -> int:
    return max(int(getattr(settings, "EQUITIES_NIGHTLY_ANALYSIS_START_HOUR", 0) or 0), 0)


def nightly_analysis_max_age_hours() -> int:
    return max(int(getattr(settings, "EQUITIES_NIGHTLY_ANALYSIS_MAX_AGE_HOURS", 36) or 36), 1)


def resolve_nightly_analysis_agent() -> dict:
    ai_config = resolve_ai_provider_config()
    if ai_config.available:
        return {
            "provider": ai_config.provider,
            "label": ai_config.label,
            "model": ai_config.model,
        }
    provider = str(getattr(settings, "EQUITIES_NIGHTLY_ANALYSIS_AGENT_PROVIDER", "core") or "core").strip() or "core"
    label = str(getattr(settings, "EQUITIES_NIGHTLY_ANALYSIS_AGENT_LABEL", "Analista nocturno") or "Analista nocturno").strip()
    return {
        "provider": provider,
        "label": label or "Analista nocturno",
    }


def can_run_nightly_analysis_now(current: datetime | None = None) -> bool:
    current = current or timezone.localtime()
    return current.hour >= nightly_analysis_start_hour()


def serialize_cached_value(value):
    if isinstance(value, Decimal):
        return {JSON_TYPE_KEY: "decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {JSON_TYPE_KEY: "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {JSON_TYPE_KEY: "date", "value": value.isoformat()}
    if isinstance(value, EquityPosition):
        return {
            JSON_TYPE_KEY: "position",
            "value": serialize_cached_position(value),
        }
    if isinstance(value, dict):
        return {str(key): serialize_cached_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize_cached_value(item) for item in value]
    return value


def deserialize_cached_value(value):
    if isinstance(value, list):
        return [deserialize_cached_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    marker = value.get(JSON_TYPE_KEY)
    if marker == "decimal":
        return Decimal(str(value.get("value", "0")))
    if marker == "date":
        return date.fromisoformat(value["value"])
    if marker == "datetime":
        return datetime.fromisoformat(value["value"])
    if marker == "position":
        return deserialize_cached_position(value.get("value") or {})
    return {key: deserialize_cached_value(item) for key, item in value.items()}


def serialize_cached_position(position: EquityPosition) -> dict:
    payload = {}
    for field_name in POSITION_CACHE_FIELDS:
        payload[field_name] = serialize_cached_value(getattr(position, field_name, None))
    payload["id"] = position.id
    return payload


def deserialize_cached_position(payload: dict) -> EquityPosition:
    kwargs = {
        field_name: deserialize_cached_value(payload.get(field_name))
        for field_name in POSITION_CACHE_FIELDS
    }
    position = EquityPosition(**kwargs)
    position.id = payload.get("id")
    position.pk = payload.get("id")
    position._state.adding = False
    return position


def build_positions_analysis_signature(positions) -> str:
    rows = []
    for position in sorted(
        positions,
        key=lambda item: (
            item.id or 0,
            item.position_kind,
            item.ticker,
            item.quote_symbol,
        ),
    ):
        row = {}
        for field_name in POSITION_SIGNATURE_FIELDS:
            value = getattr(position, field_name, None)
            if isinstance(value, Decimal):
                row[field_name] = str(value)
            elif isinstance(value, date):
                row[field_name] = value.isoformat()
            else:
                row[field_name] = value
        rows.append(row)
    signature_source = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(signature_source.encode("utf-8")).hexdigest()


def build_cached_analysis_key(card: dict, scope: str) -> str:
    position = card["position"]
    if scope == EquityNightlyAnalysisSnapshot.Scope.TRACKED and position.id:
        return f"tracked:{position.id}"
    return f"{scope}:{clean_ticker(position.ticker)}"


def build_fallback_ibex_summary(cards: list[dict], rows: list[dict], positions) -> dict:
    tracked_cards = [card for card in cards if card.get("status_key") in {"owned", "watchlist"}]
    buy_alert_count = sum(1 for card in cards if card.get("trade_alert", {}).get("label") == "Comprar")
    sell_alert_count = sum(1 for card in cards if card.get("trade_alert", {}).get("label") == "Vender")
    broker_profile = resolve_analysis_broker_profile(positions)
    return {
        "available": bool(cards),
        "analyzed_count": len(cards),
        "target_count": len(cards),
        "buy_alert_count": buy_alert_count,
        "sell_alert_count": sell_alert_count,
        "watch_alert_count": max(len(cards) - buy_alert_count - sell_alert_count, 0),
        "registered_count": len(tracked_cards),
        "registered_owned_count": sum(1 for card in tracked_cards if card.get("status_key") == "owned"),
        "registered_watchlist_count": sum(1 for card in tracked_cards if card.get("status_key") == "watchlist"),
        "radar_only_count": max(len(cards) - len(tracked_cards), 0),
        "failed_count": 0,
        "failures": [],
        "broker_assumption": broker_profile["broker"],
        "trade_channel_label": broker_profile["trade_channel_label"],
        "top_pick": rows[0] if rows else None,
    }


def refresh_cached_card_with_live_position(card: dict, position: EquityPosition) -> dict:
    projection = card.get("projection") or {}
    cached_broker_costs = card.get("broker_costs") or {}
    analysis_value_amount = projection.get("analysis_value_amount")
    if analysis_value_amount in {None, ZERO}:
        analysis_value_amount = quantize_decimal(position.current_value or position.invested_amount, "0.01") or ZERO
    annual_dividend_income = projection.get("annual_dividend_income_used")
    if annual_dividend_income is None:
        annual_dividend_income = position.annual_dividend_income
    broker_costs = {
        **cached_broker_costs,
        **build_analysis_broker_costs(position, analysis_value_amount, annual_dividend_income),
        "analysis_value_amount": quantize_decimal(analysis_value_amount, "0.01") or ZERO,
        "analysis_value_source": projection.get("analysis_value_source", cached_broker_costs.get("analysis_value_source", "actual")),
        "annual_dividend_income_used": quantize_decimal(annual_dividend_income, "0.01") or ZERO,
    }
    annual_cost_used = broker_costs.get("annual_cost_used", ZERO) or ZERO
    maintenance_drag_pct = (
        (annual_cost_used / analysis_value_amount) * Decimal("100")
        if analysis_value_amount
        else (
            (position.recurring_cost_used / position.invested_amount) * Decimal("100")
            if position.invested_amount
            else ZERO
        )
    )
    card["position"] = position
    card["reference_label"] = position.analysis_reference_label
    card["reference_profile_label"] = position.get_reference_profile_display()
    card["status_key"] = card.get("status_key") or ("owned" if position.is_owned else "watchlist")
    card["status_label"] = card.get("status_label") or position.get_position_kind_display()
    card["net_unrealized_gain"] = position.unrealized_gain_after_costs
    card["net_unrealized_return_pct"] = position.unrealized_return_pct
    card["net_annual_income"] = position.net_annual_income
    card["maintenance_drag_pct"] = maintenance_drag_pct
    card["price_vs_cost_pct"] = percentage_change(position.current_price_per_share, position.average_cost_per_share)
    card["broker_costs"] = broker_costs
    card["sale_preview"] = build_equity_sale_preview(position)
    return card


def load_latest_completed_nightly_analysis_run() -> EquityNightlyAnalysisRun | None:
    if not nightly_analysis_enabled():
        return None

    run = EquityNightlyAnalysisRun.objects.filter(
        status=EquityNightlyAnalysisRun.Status.COMPLETED,
    ).first()
    if run is None:
        return None

    completed_at = run.completed_at or run.updated_at or run.created_at
    if completed_at and completed_at < timezone.now() - timedelta(hours=nightly_analysis_max_age_hours()):
        return None
    return run


def load_latest_nightly_analysis_run() -> EquityNightlyAnalysisRun | None:
    if not nightly_analysis_enabled():
        return None
    return EquityNightlyAnalysisRun.objects.order_by("-analysis_date", "-id").first()


def nightly_analysis_matches_positions(run: EquityNightlyAnalysisRun, positions) -> bool:
    summary_data = deserialize_cached_value(run.summary_data or {})
    expected_signature = summary_data.get("tracked_signature")
    if not expected_signature:
        return False
    return expected_signature == build_positions_analysis_signature(positions)


def build_nightly_analysis_status(
    positions,
    *,
    cache_available: bool = False,
) -> dict:
    run = load_latest_nightly_analysis_run()
    if run is None:
        return {
            "available": False,
            "status_key": "missing",
            "status_label": "Sin ejecutar",
            "status_badge": "NO OK",
            "status_tone": "warn",
            "completed_at_label": "",
            "analysis_date_label": "",
            "agent_label": "",
            "agent_provider": "",
            "cache_available": cache_available,
            "matches_positions": False,
            "llm": {},
        }

    completed_at = run.completed_at or run.updated_at or run.created_at
    summary_data = deserialize_cached_value(run.summary_data or {})
    matches_positions = nightly_analysis_matches_positions(run, positions) if run.status == EquityNightlyAnalysisRun.Status.COMPLETED else False
    status_map = {
        EquityNightlyAnalysisRun.Status.COMPLETED: ("ok", "OK", "good"),
        EquityNightlyAnalysisRun.Status.FAILED: ("failed", "NO OK", "warn"),
        EquityNightlyAnalysisRun.Status.RUNNING: ("running", "EN CURSO", ""),
        EquityNightlyAnalysisRun.Status.PENDING: ("pending", "PENDIENTE", ""),
    }
    status_key, status_badge, status_tone = status_map.get(run.status, ("unknown", "NO OK", "warn"))
    return {
        "available": True,
        "status": run.status,
        "status_key": status_key,
        "status_label": run.get_status_display(),
        "status_badge": status_badge,
        "status_tone": status_tone,
        "status_note": run.status_note,
        "completed_at": completed_at,
        "completed_at_label": timezone.localtime(completed_at).strftime("%Y-%m-%d %H:%M") if completed_at else "",
        "analysis_date": run.analysis_date,
        "analysis_date_label": run.analysis_date.isoformat() if run.analysis_date else "",
        "agent_label": run.agent_label,
        "agent_provider": run.agent_provider,
        "error_message": run.error_message,
        "cache_available": cache_available,
        "matches_positions": matches_positions,
        "llm": summary_data.get("llm") or {},
    }


def build_dashboard_from_nightly_cache(
    positions,
    *,
    include_ibex_universe: bool = False,
    selected_start_date: date | None = None,
    selected_end_date: date | None = None,
) -> dict | None:
    if selected_start_date or selected_end_date:
        return None

    run = load_latest_completed_nightly_analysis_run()
    if run is None or not nightly_analysis_matches_positions(run, positions):
        return None

    summary_data = deserialize_cached_value(run.summary_data or {})
    snapshots = list(run.snapshots.all().order_by("scope", "company_name", "ticker"))
    tracked_snapshots = [snapshot for snapshot in snapshots if snapshot.scope == EquityNightlyAnalysisSnapshot.Scope.TRACKED]
    ibex_snapshots = [snapshot for snapshot in snapshots if snapshot.scope == EquityNightlyAnalysisSnapshot.Scope.IBEX]
    current_positions_by_id = {position.id: position for position in positions if position.id}

    tracked_cards_by_id: dict[int, dict] = {}
    for snapshot in tracked_snapshots:
        card = deserialize_cached_value(snapshot.analysis_payload or {})
        position_id = snapshot.position_id
        if not position_id or position_id not in current_positions_by_id:
            return None
        tracked_cards_by_id[position_id] = refresh_cached_card_with_live_position(card, current_positions_by_id[position_id])

    history_cards = []
    for position in positions:
        if position.id not in tracked_cards_by_id:
            return None
        history_cards.append(tracked_cards_by_id[position.id])

    ibex_cards = []
    if include_ibex_universe:
        ibex_cards = [
            deserialize_cached_value(snapshot.analysis_payload or {})
            for snapshot in ibex_snapshots
        ]

    decision_rows = build_equity_decision_rows(history_cards)
    reference_guide = build_equity_reference_guide(history_cards)
    ibex_rows = build_equity_decision_rows(ibex_cards) if include_ibex_universe else []
    ibex_summary = summary_data.get("ibex_universe_summary")
    if include_ibex_universe and not ibex_summary:
        ibex_summary = build_fallback_ibex_summary(ibex_cards, ibex_rows, positions)
    if not include_ibex_universe:
        ibex_summary = {
            "available": False,
            "analyzed_count": 0,
            "buy_alert_count": 0,
            "sell_alert_count": 0,
            "watch_alert_count": 0,
            "failed_count": 0,
            "failures": [],
            "broker_assumption": "",
            "trade_channel_label": "",
            "top_pick": None,
        }

    overview = build_equity_analysis_overview(
        positions,
        history_cards,
        decision_rows,
        ibex_summary,
        selected_start_date=selected_start_date,
        selected_end_date=selected_end_date,
    )
    return {
        "overview": overview,
        "history_cards": history_cards,
        "owned_positions": [position for position in positions if position.is_owned],
        "watchlist_positions": [position for position in positions if not position.is_owned],
        "owned_history_cards": [card for card in history_cards if card["position"].is_owned],
        "watchlist_history_cards": [card for card in history_cards if not card["position"].is_owned],
        "decision_rows": decision_rows,
        "ibex_universe_cards": ibex_cards,
        "ibex_universe_rows": ibex_rows,
        "ibex_universe_summary": ibex_summary,
        "optimizer_cards": build_optimizer_master_cards(history_cards, ibex_cards),
        "reference_guide_rows": reference_guide["rows"],
        "tracked_reference_rows": reference_guide["tracked_rows"],
        "reference_guide_summary": summary_data.get("reference_guide_summary") or reference_guide["summary"],
        "nightly_analysis": {
            "available": True,
            "analysis_date": run.analysis_date,
            "completed_at": run.completed_at,
            "agent_provider": run.agent_provider,
            "agent_label": run.agent_label,
            "llm": summary_data.get("llm") or {},
        },
    }


def build_nightly_completion_note(llm_summary: dict | None) -> str:
    if not llm_summary or not llm_summary.get("enabled"):
        return "Analisis nocturno completado con motor cuantitativo."

    total_count = int(llm_summary.get("total_count") or 0)
    completed_count = int(llm_summary.get("completed_count") or 0)
    failed_count = int(llm_summary.get("failed_count") or 0)
    skipped_budget_count = int(llm_summary.get("skipped_budget_count") or 0)
    cost_label = str(llm_summary.get("estimated_cost_usd") or "0")
    note = f"Analisis nocturno completado. IA {llm_summary.get('label') or 'Analista IA'} en {completed_count}/{total_count} valores"
    detail_bits = []
    if failed_count:
        detail_bits.append(f"{failed_count} fallo(s)")
    if skipped_budget_count:
        detail_bits.append(f"{skipped_budget_count} omitidos por presupuesto")
    if detail_bits:
        note += f" ({', '.join(detail_bits)})"
    note += f". Coste estimado {cost_label} USD."
    return note


def load_cached_ibex_card(
    ticker: str,
    positions,
    *,
    selected_start_date: date | None = None,
    selected_end_date: date | None = None,
) -> dict | None:
    if selected_start_date or selected_end_date:
        return None

    run = load_latest_completed_nightly_analysis_run()
    if run is None or not nightly_analysis_matches_positions(run, positions):
        return None

    normalized_ticker = clean_ticker(ticker)
    snapshot = run.snapshots.filter(
        scope=EquityNightlyAnalysisSnapshot.Scope.IBEX,
        ticker=normalized_ticker,
    ).first()
    if snapshot is None:
        return None
    return deserialize_cached_value(snapshot.analysis_payload or {})


def persist_nightly_analysis_dashboard(
    dashboard: dict,
    positions,
    *,
    analysis_date: date,
    agent_provider: str,
    agent_label: str,
    llm_summary: dict | None = None,
) -> EquityNightlyAnalysisRun:
    tracked_signature = build_positions_analysis_signature(positions)
    with transaction.atomic():
        run, _ = EquityNightlyAnalysisRun.objects.update_or_create(
            analysis_date=analysis_date,
            defaults={
                "status": EquityNightlyAnalysisRun.Status.RUNNING,
                "status_note": "Guardando analisis nocturno",
                "agent_provider": agent_provider,
                "agent_label": agent_label,
                "error_message": "",
                "started_at": timezone.now(),
                "completed_at": None,
                "summary_data": {},
            },
        )
        run.snapshots.all().delete()

        snapshot_rows = []
        for card in dashboard["history_cards"]:
            position = card["position"]
            snapshot_rows.append(
                EquityNightlyAnalysisSnapshot(
                    run=run,
                    analysis_date=analysis_date,
                    scope=EquityNightlyAnalysisSnapshot.Scope.TRACKED,
                    analysis_key=build_cached_analysis_key(card, EquityNightlyAnalysisSnapshot.Scope.TRACKED),
                    position=position if position.id else None,
                    ticker=clean_ticker(position.ticker),
                    quote_symbol=position.quote_symbol or "",
                    company_name=position.company_name,
                    status_key=card.get("status_key", ""),
                    sector_label=card.get("sector_label", ""),
                    agent_provider=agent_provider,
                    analysis_payload=serialize_cached_value(card),
                )
            )
        for card in dashboard["ibex_universe_cards"]:
            position = card["position"]
            snapshot_rows.append(
                EquityNightlyAnalysisSnapshot(
                    run=run,
                    analysis_date=analysis_date,
                    scope=EquityNightlyAnalysisSnapshot.Scope.IBEX,
                    analysis_key=build_cached_analysis_key(card, EquityNightlyAnalysisSnapshot.Scope.IBEX),
                    position=position if position.id else None,
                    ticker=clean_ticker(position.ticker),
                    quote_symbol=position.quote_symbol or "",
                    company_name=position.company_name,
                    status_key=card.get("status_key", ""),
                    sector_label=card.get("sector_label", ""),
                    agent_provider=agent_provider,
                    analysis_payload=serialize_cached_value(card),
                )
            )
        EquityNightlyAnalysisSnapshot.objects.bulk_create(snapshot_rows)

        run.status = EquityNightlyAnalysisRun.Status.COMPLETED
        run.status_note = build_nightly_completion_note(llm_summary)
        run.completed_at = timezone.now()
        run.summary_data = serialize_cached_value(
            {
                "tracked_signature": tracked_signature,
                "tracked_count": len(dashboard["history_cards"]),
                "ibex_count": len(dashboard["ibex_universe_cards"]),
                "ibex_universe_summary": dashboard["ibex_universe_summary"],
                "reference_guide_summary": dashboard["reference_guide_summary"],
                "llm": llm_summary or {},
            }
        )
        run.save(
            update_fields=[
                "status",
                "status_note",
                "completed_at",
                "summary_data",
                "updated_at",
            ]
        )
    return run


def run_nightly_equity_analysis(
    *,
    analysis_date: date | None = None,
    force: bool = False,
) -> EquityNightlyAnalysisRun | None:
    if not nightly_analysis_enabled() and not force:
        return None
    if not force and not can_run_nightly_analysis_now():
        return None

    analysis_date = analysis_date or timezone.localdate()
    agent = resolve_nightly_analysis_agent()
    started_at = timezone.now()
    run, _ = EquityNightlyAnalysisRun.objects.update_or_create(
        analysis_date=analysis_date,
        defaults={
            "status": EquityNightlyAnalysisRun.Status.RUNNING,
            "status_note": "Iniciando analisis nocturno",
            "agent_provider": agent["provider"],
            "agent_label": agent["label"],
            "error_message": "",
            "started_at": started_at,
            "completed_at": None,
            "summary_data": {},
        },
    )

    try:
        clear_market_data_caches()
        tracked_positions = list(EquityPosition.objects.all())
        if tracked_positions:
            sync_all_equities_market_data(tracked_positions)
        positions = list(EquityPosition.objects.prefetch_related("price_history"))
        dashboard = build_equity_analysis_dashboard(
            positions,
            include_ibex_universe=True,
            ibex_company_limit=None,
            ibex_include_visuals=True,
            ibex_include_reference_suggestions=True,
            ibex_include_fundamentals=True,
        )
        llm_summary = enrich_dashboard_with_ai_analysis(
            dashboard,
            analysis_date=analysis_date,
        )
        capture_equity_ticket_snapshots(dashboard["owned_history_cards"], snapshot_date=analysis_date)
        return persist_nightly_analysis_dashboard(
            dashboard,
            positions,
            analysis_date=analysis_date,
            agent_provider=agent["provider"],
            agent_label=agent["label"],
            llm_summary=llm_summary,
        )
    except Exception as exc:
        run.status = EquityNightlyAnalysisRun.Status.FAILED
        run.status_note = "Analisis nocturno fallido"
        run.error_message = str(exc)
        run.completed_at = timezone.now()
        run.save(
            update_fields=[
                "status",
                "status_note",
                "error_message",
                "completed_at",
                "updated_at",
            ]
        )
        raise
