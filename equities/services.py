from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.db import transaction
from django.utils import timezone as django_timezone

from .models import EquityPosition, EquityPriceHistory


ZERO = Decimal("0.00")


class MarketDataError(Exception):
    pass


@dataclass
class MarketSeries:
    symbol: str
    name: str
    latest_price: Decimal
    latest_date: date
    points: list[dict]


def fetch_market_series(symbol: str, range_key: str = "1y", interval: str = "1d") -> MarketSeries:
    params = urlencode({"range": range_key, "interval": interval, "includePrePost": "false"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{params}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=20) as response:
        payload = json.load(response)

    error = payload.get("chart", {}).get("error")
    if error:
        raise MarketDataError(error.get("description", f"Unable to load market data for {symbol}."))

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
        raise MarketDataError(f"No historical prices were returned for {symbol}.")

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


def sync_equity_market_data(position: EquityPosition) -> EquityPosition:
    if not position.quote_symbol:
        raise MarketDataError(f"{position.ticker} has no market quote symbol configured.")

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
