from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from .models import EquityPosition
from .services import MarketSeries, build_equity_history_cards, sync_equity_market_data


class EquitiesServicesTests(TestCase):
    def test_sync_equity_market_data_updates_latest_price_and_history(self):
        position = EquityPosition.objects.create(
            broker="Banco Sabadell",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola S.A.",
            shares=Decimal("100"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
        )

        stock_series = MarketSeries(
            symbol="IBE.MC",
            name="Iberdrola S.A.",
            latest_price=Decimal("19.2500"),
            latest_date=date(2026, 3, 22),
            points=[
                {"date": date(2026, 3, 20), "close": Decimal("18.0000")},
                {"date": date(2026, 3, 21), "close": Decimal("19.0000")},
            ],
        )
        benchmark_series = MarketSeries(
            symbol="^IBEX",
            name="IBEX 35",
            latest_price=Decimal("16714.0000"),
            latest_date=date(2026, 3, 22),
            points=[
                {"date": date(2026, 3, 20), "close": Decimal("16500.0000")},
                {"date": date(2026, 3, 21), "close": Decimal("16600.0000")},
            ],
        )

        with patch("equities.services.fetch_market_series", side_effect=[stock_series, benchmark_series]):
            sync_equity_market_data(position)

        position.refresh_from_db()
        self.assertEqual(position.current_price_per_share, Decimal("19.2500"))
        self.assertEqual(position.latest_price_date, date(2026, 3, 22))
        self.assertEqual(position.price_history.count(), 2)
        latest_point = position.price_history.order_by("-price_date").first()
        self.assertEqual(latest_point.close_price, Decimal("19.0000"))
        self.assertEqual(latest_point.benchmark_close, Decimal("16600.0000"))

    def test_build_equity_history_cards_returns_relative_performance(self):
        position = EquityPosition.objects.create(
            broker="Banco Sabadell",
            ticker="ENG",
            quote_symbol="ENG.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Enagas, S.A.",
            shares=Decimal("50"),
            average_cost_per_share=Decimal("6.8700"),
            current_price_per_share=Decimal("14.7000"),
        )
        position.price_history.create(price_date=date(2026, 3, 20), close_price=Decimal("10.0000"), benchmark_close=Decimal("100.0000"))
        position.price_history.create(price_date=date(2026, 3, 21), close_price=Decimal("12.0000"), benchmark_close=Decimal("110.0000"))

        cards = build_equity_history_cards([position])

        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0]["has_history"])
        self.assertEqual(cards[0]["stock_return_pct"], Decimal("20.00"))
        self.assertEqual(cards[0]["benchmark_return_pct"], Decimal("10.00"))
        self.assertTrue(cards[0]["stock_line"])
