from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .models import VentureOpportunity


ZERO = Decimal("0")


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


def build_venture_study_context(opportunities):
    opportunities = list(opportunities)
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
            "ticket_min_total": _sum_optional(item.ticket_min for item in active_opportunities),
            "ticket_max_total": _sum_optional(item.ticket_max for item in active_opportunities),
            "avg_score": _average_score(active_opportunities),
            "review_due_count": len(review_due),
        },
        "priority_rows": priority_rows,
        "review_due_rows": sorted(review_due, key=lambda item: item.next_review_on),
        "status_rows": _choice_rows(VentureOpportunity.Status.choices, opportunities, "status"),
        "fit_rows": _choice_rows(VentureOpportunity.StrategicFit.choices, opportunities, "strategic_fit"),
        "stage_rows": _choice_rows(VentureOpportunity.Stage.choices, opportunities, "stage"),
    }
