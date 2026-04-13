import io
import json
import os
import tempfile
from calendar import monthrange
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from portfolio.ownership import AssetOwnershipCategory

from .broker_costs import estimate_broker_costs
from .models import EquityClosedPosition, EquityOptimizationRun, EquityPosition, EquityTicketSnapshot
from .optimization_runs import launch_equity_optimization_run, process_equity_optimization_run
from .services import (
    EURIBOR_REFERENCE_NAME,
    EURIBOR_REFERENCE_SYMBOL,
    MarketSeries,
    SPAIN_ELECTRICITY_DEMAND_NAME,
    SPAIN_ELECTRICITY_DEMAND_SYMBOL,
    SPAIN_GAS_CONSUMPTION_NAME,
    SPAIN_GAS_CONSUMPTION_SYMBOL,
    build_equity_allocation_plan,
    build_equity_analysis_dashboard,
    build_equity_history_cards,
    build_equity_investment_journey_context,
    build_equity_sale_preview,
    build_equity_ticket_tracking_context,
    archive_equity_position_sale,
    build_trade_alert,
    build_reference_suggestions_for_equity,
    clear_market_data_caches,
    capture_equity_ticket_snapshots,
    fetch_market_series,
    find_equity_company_profile,
    load_ibex_reference_workbook_snapshot,
    sync_equity_market_data,
)


def build_test_reference_workbook() -> str:
    from openpyxl import Workbook

    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)

    workbook = Workbook()
    summary = workbook.active
    summary.title = "Resumen"
    summary.append(["Ticker", "Empresa", "Sector", "Rent. 2025", "Ind. referencia", "Fuente", "Correl.", "PER 2025e", "Div. yield", "Capitaliz. (MrdEUR)", "Notas", "Ticker Yahoo"])
    summary.append(["SAN", "Banco Santander", "Banca", 0.923, "Euribor 12m / Tipos BCE", "BCE", 0.78, 7.2, 0.048, 92, "Banco de prueba", "SAN.MC"])

    quotes = workbook.create_sheet("Cotizaciones")
    quotes.append(["Ticker", "Empresa", "Sector", "2019", "2020", "2021", "2022", "2023", "2024", "2025"])
    quotes.append(["SAN", "Banco Santander", "Banca", 4.18, 2.31, 3.08, 2.83, 4.06, 5.42, 10.42])

    indicators = workbook.create_sheet("Indicadores")
    indicators.append(["Indicador", "Fuente", "Sector relacionado", "2019", "2020", "2021", "2022", "2023", "2024", "2025", "Var. 3a", "Var. total"])
    indicators.append(["Euribor 12m (%)", "BCE", "Banca", -0.24, -0.50, -0.50, 2.59, 4.16, 2.70, 2.10, "", ""])
    indicators.append(["IBEX 35 (puntos)", "BME", "Todos", 9549, 8073, 8713, 8229, 10102, 11595, 17315, "", ""])
    indicators.append(["Precio Brent (USD/barril prom.)", "ICE", "Consumo", 64, 42, 70, 101, 82, 79, 74, "", ""])

    correlations = workbook.create_sheet("Correlaciones")
    correlations.append(["Sector \\ Indicador", "Euribor 12m", "Brent"])
    correlations.append(["Banca", 0.78, 0.15])

    workbook.save(path)
    workbook.close()
    return path


def build_compound_market_series(
    symbol: str,
    name: str,
    growth: Decimal,
    months: int = 36,
    start_year: int = 2023,
    start_month: int = 1,
    start_price: Decimal = Decimal("10.0000"),
) -> MarketSeries:
    points = []
    price = start_price
    for index in range(months):
        month_number = (start_month - 1) + index
        year = start_year + (month_number // 12)
        month = (month_number % 12) + 1
        month_end = monthrange(year, month)[1]
        price = (price * growth).quantize(Decimal("0.0001"))
        points.append(
            {
                "date": date(year, month, month_end),
                "open": price,
                "high": (price * Decimal("1.0100")).quantize(Decimal("0.0001")),
                "low": (price * Decimal("0.9900")).quantize(Decimal("0.0001")),
                "close": price,
            }
        )

    return MarketSeries(
        symbol=symbol,
        name=name,
        latest_price=points[-1]["close"],
        latest_date=points[-1]["date"],
        points=points,
    )


class EquitiesServicesTests(TestCase):
    def tearDown(self):
        load_ibex_reference_workbook_snapshot.cache_clear()
        clear_market_data_caches()
        super().tearDown()

    def test_find_equity_company_profile_by_ibex_name_returns_indra_defaults(self):
        profile = find_equity_company_profile("Indra")

        self.assertIsNotNone(profile)
        self.assertEqual(profile["ticker"], "IDR")
        self.assertEqual(profile["quote_symbol"], "IDR.MC")
        self.assertEqual(profile["default_reference"]["benchmark_name"], "IBEX 35")

    def test_build_reference_suggestions_for_bank_prioritizes_euribor(self):
        suggestions = build_reference_suggestions_for_equity("Banco Santander", "SAN")

        self.assertGreaterEqual(len(suggestions), 2)
        self.assertEqual(suggestions[0]["benchmark_name"], EURIBOR_REFERENCE_NAME)
        self.assertEqual(suggestions[0]["benchmark_symbol"], EURIBOR_REFERENCE_SYMBOL)

    def test_build_reference_suggestions_for_enagas_prioritizes_gas_consumption(self):
        suggestions = build_reference_suggestions_for_equity("Enagas", "ENG")

        self.assertGreaterEqual(len(suggestions), 2)
        self.assertEqual(suggestions[0]["benchmark_name"], SPAIN_GAS_CONSUMPTION_NAME)
        self.assertEqual(suggestions[0]["benchmark_symbol"], SPAIN_GAS_CONSUMPTION_SYMBOL)

    def test_build_reference_suggestions_for_iberdrola_prioritizes_electricity_demand(self):
        suggestions = build_reference_suggestions_for_equity("Iberdrola", "IBE")

        self.assertGreaterEqual(len(suggestions), 2)
        self.assertEqual(suggestions[0]["benchmark_name"], SPAIN_ELECTRICITY_DEMAND_NAME)
        self.assertEqual(suggestions[0]["benchmark_symbol"], SPAIN_ELECTRICITY_DEMAND_SYMBOL)

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
                {
                    "date": date(2026, 3, 20),
                    "open": Decimal("17.8000"),
                    "high": Decimal("18.4000"),
                    "low": Decimal("17.7000"),
                    "close": Decimal("18.0000"),
                },
                {
                    "date": date(2026, 3, 21),
                    "open": Decimal("18.3000"),
                    "high": Decimal("19.2000"),
                    "low": Decimal("18.1000"),
                    "close": Decimal("19.0000"),
                },
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

        with (
            patch("equities.services.fetch_market_series", return_value=stock_series),
            patch("equities.services.fetch_reference_series", return_value=benchmark_series),
        ):
            sync_equity_market_data(position)

        position.refresh_from_db()
        self.assertEqual(position.current_price_per_share, Decimal("19.2500"))
        self.assertEqual(position.latest_price_date, date(2026, 3, 22))
        self.assertEqual(position.price_history.count(), 2)
        latest_point = position.price_history.order_by("-price_date").first()
        self.assertEqual(latest_point.open_price, Decimal("18.3000"))
        self.assertEqual(latest_point.high_price, Decimal("19.2000"))
        self.assertEqual(latest_point.low_price, Decimal("18.1000"))
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
        self.assertEqual(cards[0]["selected_period"]["label"], "1Y")

    def test_santander_broker_costs_reduce_net_annual_income(self):
        position = EquityPosition.objects.create(
            broker="Banco Santander",
            trade_channel=EquityPosition.TradeChannel.APP,
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola S.A.",
            shares=Decimal("100"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("12.0000"),
            annual_dividend_income=Decimal("40.00"),
            annual_maintenance_cost=Decimal("0.00"),
        )

        self.assertEqual(position.purchase_total_cost, Decimal("5.00"))
        self.assertEqual(position.estimated_broker_costs["annual_custody_cost"], Decimal("20.00"))
        self.assertEqual(position.estimated_broker_costs["annual_dividend_fee"], Decimal("2.00"))
        self.assertEqual(position.net_dividend_income, Decimal("38.00"))
        self.assertEqual(position.net_annual_income, Decimal("16.00"))

    def test_history_cards_include_suggested_reference_correlations(self):
        position = EquityPosition.objects.create(
            broker="Interactive Brokers",
            ticker="SAN",
            quote_symbol="SAN.MC",
            reference_profile=EquityPosition.ReferenceProfile.EURIBOR_12M,
            benchmark_symbol=EURIBOR_REFERENCE_SYMBOL,
            benchmark_name=EURIBOR_REFERENCE_NAME,
            company_name="Banco Santander, S.A.",
            shares=Decimal("30"),
            average_cost_per_share=Decimal("4.0000"),
            current_price_per_share=Decimal("5.0000"),
        )
        for price_date, close_price, benchmark_close in (
            (date(2026, 1, 31), Decimal("4.00"), Decimal("2.10")),
            (date(2026, 2, 28), Decimal("4.20"), Decimal("2.20")),
            (date(2026, 3, 31), Decimal("4.50"), Decimal("2.35")),
            (date(2026, 4, 30), Decimal("4.80"), Decimal("2.50")),
            (date(2026, 5, 31), Decimal("5.00"), Decimal("2.65")),
        ):
            position.price_history.create(
                price_date=price_date,
                close_price=close_price,
                benchmark_close=benchmark_close,
            )

        euribor_series = MarketSeries(
            symbol=EURIBOR_REFERENCE_SYMBOL,
            name=EURIBOR_REFERENCE_NAME,
            latest_price=Decimal("2.65"),
            latest_date=date(2026, 5, 1),
            points=[
                {"date": date(2026, 1, 1), "close": Decimal("2.10")},
                {"date": date(2026, 2, 1), "close": Decimal("2.20")},
                {"date": date(2026, 3, 1), "close": Decimal("2.35")},
                {"date": date(2026, 4, 1), "close": Decimal("2.50")},
                {"date": date(2026, 5, 1), "close": Decimal("2.65")},
            ],
        )
        generic_series = MarketSeries(
            symbol="^IBEX",
            name="IBEX 35",
            latest_price=Decimal("14000.00"),
            latest_date=date(2026, 5, 31),
            points=[
                {"date": date(2026, 1, 31), "close": Decimal("12000.00")},
                {"date": date(2026, 2, 28), "close": Decimal("12300.00")},
                {"date": date(2026, 3, 31), "close": Decimal("12700.00")},
                {"date": date(2026, 4, 30), "close": Decimal("13200.00")},
                {"date": date(2026, 5, 31), "close": Decimal("14000.00")},
            ],
        )

        def fake_reference_series(reference_profile, benchmark_symbol="", benchmark_name=""):
            if reference_profile == EquityPosition.ReferenceProfile.EURIBOR_12M:
                return euribor_series
            return generic_series

        with patch("equities.services.fetch_reference_series_for_choice", side_effect=fake_reference_series):
            cards = build_equity_history_cards([position])

        self.assertEqual(cards[0]["suggested_references"][0]["benchmark_name"], EURIBOR_REFERENCE_NAME)
        self.assertIsNotNone(cards[0]["suggested_references"][0]["correlation"]["coefficient"])
        self.assertTrue(cards[0]["best_correlation_chart"]["available"])
        self.assertEqual(cards[0]["best_correlation_chart"]["reference_label"], EURIBOR_REFERENCE_NAME)

    def test_history_cards_include_one_year_projection_from_six_months_and_reference(self):
        position = EquityPosition.objects.create(
            broker="Banco Sabadell",
            ticker="IDR",
            quote_symbol="IDR.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Indra Sistemas, S.A.",
            shares=Decimal("20"),
            average_cost_per_share=Decimal("18.0000"),
            current_price_per_share=Decimal("22.0000"),
        )
        for price_date, close_price, benchmark_close in (
            (date(2025, 12, 31), Decimal("15.00"), Decimal("100.00")),
            (date(2026, 1, 31), Decimal("16.00"), Decimal("102.00")),
            (date(2026, 2, 28), Decimal("17.20"), Decimal("104.50")),
            (date(2026, 3, 31), Decimal("18.60"), Decimal("107.00")),
            (date(2026, 4, 30), Decimal("19.80"), Decimal("109.80")),
            (date(2026, 5, 31), Decimal("21.10"), Decimal("112.40")),
            (date(2026, 6, 30), Decimal("22.00"), Decimal("115.00")),
        ):
            position.price_history.create(
                price_date=price_date,
                close_price=close_price,
                benchmark_close=benchmark_close,
            )

        cards = build_equity_history_cards([position])

        projection = cards[0]["projection"]
        self.assertTrue(projection["available"])
        self.assertGreater(projection["base_return_pct"], Decimal("0"))
        self.assertGreater(projection["projected_price"], Decimal("22.00"))
        self.assertEqual(len(projection["quarterly_path"]), 4)
        self.assertIsNotNone(projection["quarterly_path"][0]["projected_date"])
        self.assertIn("IBEX 35", projection["explanation"])
        self.assertTrue(cards[0]["projection_line"])
        self.assertTrue(cards[0]["historical_chart"]["available"])
        self.assertTrue(cards[0]["projection_12m_chart"]["available"])
        self.assertTrue(cards[0]["historical_chart"]["x_markers"])
        self.assertTrue(cards[0]["projection_12m_chart"]["x_markers"])

    def test_projection_includes_dividends_and_broker_drag(self):
        position = EquityPosition.objects.create(
            broker="Banco Santander",
            trade_channel=EquityPosition.TradeChannel.APP,
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola S.A.",
            shares=Decimal("80"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("13.0000"),
            annual_dividend_income=Decimal("55.00"),
        )
        for price_date, close_price, benchmark_close in (
            (date(2024, 12, 31), Decimal("9.60"), Decimal("100.00")),
            (date(2025, 3, 31), Decimal("10.10"), Decimal("102.00")),
            (date(2025, 6, 30), Decimal("10.70"), Decimal("104.00")),
            (date(2025, 9, 30), Decimal("11.20"), Decimal("105.50")),
            (date(2025, 12, 31), Decimal("11.70"), Decimal("107.20")),
            (date(2026, 3, 31), Decimal("12.20"), Decimal("109.80")),
            (date(2026, 6, 30), Decimal("13.00"), Decimal("112.40")),
        ):
            position.price_history.create(
                price_date=price_date,
                close_price=close_price,
                benchmark_close=benchmark_close,
            )

        cards = build_equity_history_cards([position])

        projection = cards[0]["projection"]
        self.assertTrue(projection["available"])
        self.assertGreater(projection["gross_dividend_yield_pct"], Decimal("0"))
        self.assertGreater(projection["net_income_yield_pct"], Decimal("0"))
        self.assertGreater(projection["transaction_drag_pct"], Decimal("0"))
        self.assertEqual(
            projection["base_return_pct"].quantize(Decimal("0.01")),
            (
                projection["price_return_pct"]
                + projection["net_income_yield_pct"]
                - projection["transaction_drag_pct"]
            ).quantize(Decimal("0.01")),
        )

    def test_watchlist_projection_normalizes_costs_to_analysis_ticket(self):
        position = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            broker="Banco Sabadell",
            trade_channel=EquityPosition.TradeChannel.APP,
            ticker="SCYR",
            quote_symbol="SCYR.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Sacyr, S.A.",
            shares=Decimal("1"),
            average_cost_per_share=Decimal("4.6500"),
            current_price_per_share=Decimal("4.6500"),
            annual_dividend_income=Decimal("0"),
        )
        for price_date, close_price, benchmark_close in (
            (date(2025, 12, 31), Decimal("3.10"), Decimal("100.00")),
            (date(2026, 1, 31), Decimal("3.25"), Decimal("102.00")),
            (date(2026, 2, 28), Decimal("3.45"), Decimal("104.00")),
            (date(2026, 3, 31), Decimal("3.70"), Decimal("106.00")),
            (date(2026, 4, 30), Decimal("4.10"), Decimal("108.00")),
            (date(2026, 5, 31), Decimal("4.35"), Decimal("110.00")),
            (date(2026, 6, 30), Decimal("4.65"), Decimal("112.00")),
        ):
            position.price_history.create(
                price_date=price_date,
                close_price=close_price,
                benchmark_close=benchmark_close,
            )

        cards = build_equity_history_cards([position])

        projection = cards[0]["projection"]
        self.assertTrue(projection["available"])
        self.assertEqual(projection["analysis_value_source"], "normalized_watchlist")
        self.assertEqual(projection["analysis_value_amount"], Decimal("10000.00"))
        self.assertGreater(projection["base_return_pct"], Decimal("0"))
        self.assertEqual(cards[0]["broker_costs"]["roundtrip_total_cost"], Decimal("32.00"))
        self.assertEqual(cards[0]["broker_costs"]["annual_cost_used"], Decimal("25.00"))

    def test_history_cards_include_projection_backtest_accuracy(self):
        position = EquityPosition.objects.create(
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola S.A.",
            shares=Decimal("15"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
        )

        stock_price = Decimal("10.00")
        benchmark_price = Decimal("100.00")
        for index in range(24):
            year = 2024 + (index // 12)
            month = (index % 12) + 1
            month_end = monthrange(year, month)[1]
            stock_price = (stock_price * Decimal("1.02")).quantize(Decimal("0.0001"))
            benchmark_price = (benchmark_price * Decimal("1.01")).quantize(Decimal("0.0001"))
            position.price_history.create(
                price_date=date(year, month, month_end),
                close_price=stock_price,
                benchmark_close=benchmark_price,
            )

        cards = build_equity_history_cards([position])

        backtest = cards[0]["projection_backtest"]
        self.assertTrue(backtest["available"])
        self.assertGreaterEqual(backtest["comparisons_count"], 5)
        self.assertLess(backtest["mean_absolute_error_pct"], Decimal("12.00"))
        self.assertGreater(backtest["direction_hit_rate_pct"], Decimal("80.00"))
        self.assertTrue(backtest["rows"])
        self.assertTrue(backtest["monthly_chart"]["available"])
        self.assertTrue(backtest["monthly_chart"]["forecast_line"])
        self.assertTrue(backtest["monthly_chart"]["actual_line"])

    def test_history_cards_include_trade_alert_from_prolonged_relative_trend(self):
        buy_position = EquityPosition.objects.create(
            broker="Interactive Brokers",
            ticker="IDR",
            quote_symbol="IDR.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Indra Sistemas, S.A.",
            shares=Decimal("25"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
            annual_dividend_income=Decimal("15.00"),
        )
        sell_position = EquityPosition.objects.create(
            broker="Interactive Brokers",
            ticker="TEF",
            quote_symbol="TEF.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Telefonica, S.A.",
            shares=Decimal("25"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
            annual_dividend_income=Decimal("20.00"),
        )

        benchmark_price = Decimal("100.0000")
        buy_price = Decimal("10.0000")
        sell_price = Decimal("10.0000")
        for index in range(30):
            year = 2023 + (index // 12)
            month = (index % 12) + 1
            month_end = monthrange(year, month)[1]
            benchmark_price = (benchmark_price * Decimal("1.0080")).quantize(Decimal("0.0001"))
            buy_price = (buy_price * Decimal("1.0240")).quantize(Decimal("0.0001"))
            sell_price = (sell_price * Decimal("0.9920")).quantize(Decimal("0.0001"))
            buy_position.price_history.create(
                price_date=date(year, month, month_end),
                close_price=buy_price,
                benchmark_close=benchmark_price,
            )
            sell_position.price_history.create(
                price_date=date(year, month, month_end),
                close_price=sell_price,
                benchmark_close=benchmark_price,
            )

        buy_position.current_price_per_share = buy_price
        buy_position.save(update_fields=["current_price_per_share"])
        sell_position.current_price_per_share = sell_price
        sell_position.save(update_fields=["current_price_per_share"])

        cards = build_equity_history_cards([buy_position, sell_position])
        cards_by_ticker = {card["position"].ticker: card for card in cards}

        self.assertEqual(cards_by_ticker["IDR"]["trade_alert"]["label"], "Comprar")
        self.assertEqual(cards_by_ticker["TEF"]["trade_alert"]["label"], "Vender")
        self.assertGreater(cards_by_ticker["IDR"]["trade_alert"]["score"], Decimal("0"))
        self.assertLess(cards_by_ticker["TEF"]["trade_alert"]["score"], Decimal("0"))

    def test_trade_alert_stays_in_watch_when_trend_and_12m_net_diverge(self):
        position = EquityPosition(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            broker="Banco Sabadell",
            ticker="SCYR",
            quote_symbol="SCYR.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Sacyr, S.A.",
            shares=Decimal("1"),
            average_cost_per_share=Decimal("4.6500"),
            current_price_per_share=Decimal("4.6500"),
        )

        alert = build_trade_alert(
            position,
            projection={
                "available": True,
                "safety_score": Decimal("60.00"),
                "base_return_pct": Decimal("-8.00"),
            },
            correlation={"coefficient": Decimal("0.76")},
            reliability={"score": Decimal("66.00")},
            relative_trend={
                "label": "Mejora moderada",
                "periods_label": "meses",
                "positive_streak": 3,
                "negative_streak": 0,
                "prolonged_positive": True,
                "prolonged_negative": False,
                "recent_gap_avg_pct": Decimal("5.20"),
                "gap_slope_pct": Decimal("0.45"),
            },
            six_month_snapshot={"available": True, "alpha_pct": Decimal("3.50")},
            one_year_snapshot={"available": True, "alpha_pct": Decimal("6.20")},
        )

        self.assertEqual(alert["label"], "Vigilar")
        self.assertEqual(alert["tone"], "watch")
        self.assertIn("neto 12M sigue en negativo", alert["note"])

    def test_watchlist_positions_do_not_count_into_portfolio_totals(self):
        EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.OWNED,
            broker="Banco Sabadell",
            ticker="IBE",
            quote_symbol="IBE.MC",
            reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola S.A.",
            shares=Decimal("10"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("12.0000"),
        )
        EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            broker="Banco Sabadell",
            ticker="FER",
            quote_symbol="FER.MC",
            reference_profile=EquityPosition.ReferenceProfile.SPAIN_HOUSE_PRICE,
            benchmark_symbol="EUROSTAT:prc_hpi_q:ES:TOTAL:I15_Q",
            benchmark_name="Precio vivienda Espana",
            company_name="Ferrovial",
            shares=Decimal("0.0000"),
            average_cost_per_share=Decimal("38.0000"),
            current_price_per_share=Decimal("39.5000"),
        )

        dashboard = build_equity_analysis_dashboard(list(EquityPosition.objects.prefetch_related("price_history")))

        self.assertEqual(dashboard["overview"]["owned_positions_count"], 1)
        self.assertEqual(dashboard["overview"]["watchlist_positions_count"], 1)
        self.assertEqual(dashboard["overview"]["invested_amount"], Decimal("100.0000"))
        self.assertEqual(dashboard["overview"]["current_value"], Decimal("120.0000"))

    @override_settings(EQUITIES_REFERENCE_WORKBOOK="")
    def test_dashboard_builds_reference_guide_from_workbook(self):
        workbook_path = build_test_reference_workbook()
        self.addCleanup(lambda: os.path.exists(workbook_path) and os.remove(workbook_path))
        load_ibex_reference_workbook_snapshot.cache_clear()

        with override_settings(EQUITIES_REFERENCE_WORKBOOK=workbook_path):
            position = EquityPosition.objects.create(
                broker="Interactive Brokers",
                ticker="SAN",
                quote_symbol="SAN.MC",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_symbol="^IBEX",
                benchmark_name="IBEX 35",
                company_name="Banco Santander",
                shares=Decimal("10"),
                average_cost_per_share=Decimal("4.0000"),
                current_price_per_share=Decimal("5.0000"),
            )

            dashboard = build_equity_analysis_dashboard([position])

        self.assertTrue(dashboard["reference_guide_summary"]["workbook_loaded"])
        self.assertEqual(len(dashboard["tracked_reference_rows"]), 1)
        tracked_row = dashboard["tracked_reference_rows"][0]
        self.assertEqual(tracked_row["company_name"], "Banco Santander")
        self.assertEqual(tracked_row["best_candidate"]["name"], "Euribor 12m (%)")
        self.assertTrue(tracked_row["best_candidate"]["supports_chart"])
        self.assertEqual(dashboard["history_cards"][0]["reference_playbook"]["best_candidate"]["name"], "Euribor 12m (%)")

    def test_ticket_tracking_captures_daily_snapshots_and_builds_global_chart(self):
        positions = []
        for ticker, company_name, cost, current in (
            ("IBE", "Iberdrola", Decimal("10.0000"), Decimal("12.0000")),
            ("ENG", "Enagas", Decimal("14.0000"), Decimal("15.5000")),
        ):
            position = EquityPosition.objects.create(
                broker="Interactive Brokers",
                ticker=ticker,
                quote_symbol=f"{ticker}.MC",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_symbol="^IBEX",
                benchmark_name="IBEX 35",
                company_name=company_name,
                shares=Decimal("20.0000"),
                average_cost_per_share=cost,
                current_price_per_share=current,
                annual_dividend_income=Decimal("40.00"),
            )
            stock_series = build_compound_market_series(
                f"{ticker}.MC",
                company_name,
                growth=Decimal("1.0180"),
                start_price=cost,
            )
            reference_series = build_compound_market_series(
                "^IBEX",
                "IBEX 35",
                growth=Decimal("1.0070"),
                start_price=Decimal("100.0000"),
            )
            for stock_point, reference_point in zip(stock_series.points, reference_series.points):
                position.price_history.create(
                    price_date=stock_point["date"],
                    open_price=stock_point["open"],
                    high_price=stock_point["high"],
                    low_price=stock_point["low"],
                    close_price=stock_point["close"],
                    benchmark_close=reference_point["close"],
                )
            positions.append(position)

        first_cards = build_equity_history_cards(positions)
        capture_equity_ticket_snapshots(first_cards, snapshot_date=date(2026, 4, 12))

        positions[0].current_price_per_share = Decimal("12.4000")
        positions[0].save(update_fields=["current_price_per_share", "updated_at"])
        positions[1].current_price_per_share = Decimal("15.9000")
        positions[1].save(update_fields=["current_price_per_share", "updated_at"])

        second_cards = build_equity_history_cards(list(EquityPosition.objects.prefetch_related("price_history")))
        capture_equity_ticket_snapshots(second_cards, snapshot_date=date(2026, 4, 13))
        tracking = build_equity_ticket_tracking_context(second_cards)

        self.assertEqual(EquityTicketSnapshot.objects.count(), 4)
        self.assertTrue(tracking["available"])
        self.assertEqual(tracking["tracked_ticket_count"], 2)
        self.assertTrue(tracking["global"]["available"])
        self.assertTrue(tracking["global"]["chart"]["available"])
        self.assertEqual(tracking["snapshot_days_count"], 2)
        self.assertEqual(len(tracking["tickets"]), 2)
        self.assertTrue(all(item["chart"]["available"] for item in tracking["tickets"]))
        self.assertIsNotNone(tracking["global"]["expected_today_value"])
        self.assertTrue(tracking["global"]["chart"]["x_markers"])

    def test_investment_journey_builds_active_and_closed_ticket_history(self):
        active = EquityPosition.objects.create(
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2024, 1, 15),
            shares=Decimal("20.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("12.5000"),
            annual_dividend_income=Decimal("30.00"),
            annual_maintenance_cost=Decimal("6.00"),
        )
        sold = EquityPosition.objects.create(
            broker="Banco Sabadell",
            ticker="ACS",
            quote_symbol="ACS.MC",
            reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="ACS",
            opened_on=date(2023, 2, 1),
            shares=Decimal("15.0000"),
            average_cost_per_share=Decimal("20.0000"),
            current_price_per_share=Decimal("24.0000"),
            annual_dividend_income=Decimal("18.00"),
            annual_maintenance_cost=Decimal("10.00"),
        )
        for position, start_price, growth in (
            (active, Decimal("10.00"), Decimal("1.0100")),
            (sold, Decimal("20.00"), Decimal("1.0120")),
        ):
            stock_series = build_compound_market_series(
                position.quote_symbol,
                position.company_name,
                growth=growth,
                start_price=start_price,
            )
            reference_series = build_compound_market_series(
                "^IBEX",
                "IBEX 35",
                growth=Decimal("1.0060"),
                start_price=Decimal("100.0000"),
            )
            for stock_point, reference_point in zip(stock_series.points, reference_series.points):
                position.price_history.create(
                    price_date=stock_point["date"],
                    open_price=stock_point["open"],
                    high_price=stock_point["high"],
                    low_price=stock_point["low"],
                    close_price=stock_point["close"],
                    benchmark_close=reference_point["close"],
                )

        archive_equity_position_sale(
            sold,
            closed_on=date(2025, 9, 30),
            sale_price_per_share=Decimal("25.0000"),
            notes="Cierre completo",
        )
        context = build_equity_investment_journey_context(
            list(EquityPosition.objects.prefetch_related("price_history")),
            list(EquityClosedPosition.objects.all()),
        )

        self.assertTrue(context["available"])
        self.assertEqual(context["active_count"], 1)
        self.assertEqual(context["closed_count"], 1)
        self.assertTrue(context["value_chart"]["available"])
        self.assertTrue(context["profit_chart"]["available"])
        self.assertTrue(context["annual_result_chart"]["available"])
        self.assertTrue(context["annual_rows"])
        self.assertIsNotNone(context["current_year_row"])
        self.assertEqual(context["closed_tickets"][0]["status_label"], "Vendida")
        self.assertIsNotNone(context["cumulative_margin_pct"])
        self.assertGreaterEqual(context["costs_total"], Decimal("0.00"))
        self.assertIsNotNone(context["average_annual_result"])

    def test_sale_preview_calculates_net_result_for_a_specific_sale_price(self):
        position = EquityPosition.objects.create(
            broker="Banco Santander",
            trade_channel=EquityPosition.TradeChannel.APP,
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2025, 4, 12),
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("12.5000"),
            annual_dividend_income=Decimal("12.00"),
            annual_maintenance_cost=Decimal("5.00"),
        )

        preview = build_equity_sale_preview(
            position,
            sale_price_per_share=Decimal("12.5000"),
            closed_on=date(2026, 4, 12),
        )

        self.assertTrue(preview["available"])
        self.assertEqual(preview["purchase_cost"], Decimal("3.20"))
        self.assertEqual(preview["sale_total_cost"], Decimal("3.00"))
        self.assertEqual(preview["net_exit_value"], Decimal("122.00"))
        self.assertEqual(preview["dividend_total"], Decimal("10.00"))
        self.assertEqual(preview["maintenance_total"], Decimal("5.00"))
        self.assertEqual(preview["net_result"], Decimal("23.80"))
        self.assertEqual(preview["annualized_margin_pct"], preview["cumulative_margin_pct"])

    def test_allocation_plan_respects_max_company_weight_and_sorts_by_projection(self):
        stronger = {
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                ticker="IDR",
                company_name="Indra",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_name="IBEX 35",
                benchmark_symbol="^IBEX",
                shares=Decimal("0"),
                average_cost_per_share=Decimal("0"),
                current_price_per_share=Decimal("18"),
            ),
            "reference_label": "IBEX 35",
            "projection": {
                "available": True,
                "base_return_pct": Decimal("24.0"),
                "projected_price": Decimal("22.32"),
                "confidence_label": "Alta",
                "coefficient": Decimal("0.55"),
            },
        }
        medium = {
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.OWNED,
                ticker="IBE",
                company_name="Iberdrola",
                reference_profile=EquityPosition.ReferenceProfile.SPAIN_ELECTRICITY_DEMAND,
                benchmark_name="Demanda electrica Espana",
                benchmark_symbol="REE:demand:es:peninsular",
                shares=Decimal("10"),
                average_cost_per_share=Decimal("10"),
                current_price_per_share=Decimal("14"),
            ),
            "reference_label": "Demanda electrica Espana",
            "projection": {
                "available": True,
                "base_return_pct": Decimal("16.0"),
                "projected_price": Decimal("16.24"),
                "confidence_label": "Media",
                "coefficient": Decimal("0.31"),
            },
        }
        weak = {
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                ticker="MRL",
                company_name="Merlin",
                reference_profile=EquityPosition.ReferenceProfile.SPAIN_HOUSE_PRICE,
                benchmark_name="Precio vivienda Espana",
                benchmark_symbol="EUROSTAT:prc_hpi_q:ES:TOTAL:I15_Q",
                shares=Decimal("0"),
                average_cost_per_share=Decimal("0"),
                current_price_per_share=Decimal("10"),
            ),
            "reference_label": "Precio vivienda Espana",
            "projection": {
                "available": True,
                "base_return_pct": Decimal("8.0"),
                "projected_price": Decimal("10.80"),
                "confidence_label": "Baja",
                "coefficient": Decimal("0.10"),
            },
        }

        plan = build_equity_allocation_plan([medium, weak, stronger], Decimal("100000"), Decimal("40"))

        self.assertTrue(plan["available"])
        self.assertEqual(len(plan["allocations"]), 3)
        self.assertEqual(plan["allocations"][0]["position"].ticker, "IDR")
        self.assertEqual(plan["allocations"][0]["allocated_amount"], Decimal("40000"))
        self.assertEqual(plan["allocations"][1]["allocated_amount"], Decimal("40000"))
        self.assertEqual(plan["allocations"][2]["allocated_amount"], Decimal("20000"))
        self.assertEqual(plan["cash_reserve_amount"], Decimal("0"))
        self.assertGreater(plan["projected_gain_total"], Decimal("0"))

    def test_allocation_plan_can_limit_max_positions_per_sector(self):
        utility_best = {
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                ticker="IBE",
                company_name="Iberdrola",
                reference_profile=EquityPosition.ReferenceProfile.SPAIN_ELECTRICITY_DEMAND,
                benchmark_name="Demanda electrica Espana",
                benchmark_symbol="REE:demand:es:peninsular",
                shares=Decimal("0"),
                average_cost_per_share=Decimal("0"),
                current_price_per_share=Decimal("14"),
            ),
            "sector_label": "Electrica",
            "reference_label": "Demanda electrica Espana",
            "trade_alert": {"label": "Comprar", "tone": "buy", "note": ""},
            "projection": {
                "available": True,
                "base_return_pct": Decimal("16.0"),
                "projected_price": Decimal("16.24"),
                "confidence_label": "Media",
                "coefficient": Decimal("0.31"),
                "safety_score": Decimal("70.00"),
                "net_income_yield_pct": Decimal("2.50"),
                "gross_dividend_yield_pct": Decimal("3.40"),
                "transaction_drag_pct": Decimal("0.20"),
                "annualized_volatility_pct": Decimal("14.00"),
                "positive_year_ratio_pct": Decimal("68.00"),
                "years_covered": Decimal("10.00"),
                "cycle_phase": "Expansion",
                "current_drawdown_pct": Decimal("-4.00"),
                "max_drawdown_pct": Decimal("-18.00"),
            },
            "projection_reliability": {"label": "Alta", "score": Decimal("80.00")},
        }
        utility_weaker = {
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                ticker="ELE",
                company_name="Endesa",
                reference_profile=EquityPosition.ReferenceProfile.SPAIN_ELECTRICITY_DEMAND,
                benchmark_name="Demanda electrica Espana",
                benchmark_symbol="REE:demand:es:peninsular",
                shares=Decimal("0"),
                average_cost_per_share=Decimal("0"),
                current_price_per_share=Decimal("18"),
            ),
            "sector_label": "Electrica",
            "reference_label": "Demanda electrica Espana",
            "trade_alert": {"label": "Comprar", "tone": "buy", "note": ""},
            "projection": {
                "available": True,
                "base_return_pct": Decimal("10.0"),
                "projected_price": Decimal("19.80"),
                "confidence_label": "Media",
                "coefficient": Decimal("0.28"),
                "safety_score": Decimal("68.00"),
                "net_income_yield_pct": Decimal("2.90"),
                "gross_dividend_yield_pct": Decimal("4.10"),
                "transaction_drag_pct": Decimal("0.15"),
                "annualized_volatility_pct": Decimal("13.00"),
                "positive_year_ratio_pct": Decimal("66.00"),
                "years_covered": Decimal("10.00"),
                "cycle_phase": "Expansion",
                "current_drawdown_pct": Decimal("-3.00"),
                "max_drawdown_pct": Decimal("-16.00"),
            },
            "projection_reliability": {"label": "Alta", "score": Decimal("79.00")},
        }
        defense = {
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                ticker="IDR",
                company_name="Indra",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_name="IBEX 35",
                benchmark_symbol="^IBEX",
                shares=Decimal("0"),
                average_cost_per_share=Decimal("0"),
                current_price_per_share=Decimal("18"),
            ),
            "sector_label": "Tecnologia y defensa",
            "reference_label": "IBEX 35",
            "trade_alert": {"label": "Comprar", "tone": "buy", "note": ""},
            "projection": {
                "available": True,
                "base_return_pct": Decimal("24.0"),
                "projected_price": Decimal("22.32"),
                "confidence_label": "Alta",
                "coefficient": Decimal("0.55"),
                "safety_score": Decimal("74.00"),
                "net_income_yield_pct": Decimal("1.20"),
                "gross_dividend_yield_pct": Decimal("1.50"),
                "transaction_drag_pct": Decimal("0.10"),
                "annualized_volatility_pct": Decimal("17.00"),
                "positive_year_ratio_pct": Decimal("72.00"),
                "years_covered": Decimal("10.00"),
                "cycle_phase": "Expansion",
                "current_drawdown_pct": Decimal("-5.00"),
                "max_drawdown_pct": Decimal("-22.00"),
            },
            "projection_reliability": {"label": "Alta", "score": Decimal("84.00")},
        }

        plan = build_equity_allocation_plan([utility_weaker, utility_best, defense], Decimal("100000"), Decimal("50"), 0, 1)

        self.assertTrue(plan["available"])
        self.assertEqual(len(plan["allocations"]), 2)
        self.assertEqual(plan["max_sector_positions"], 1)
        self.assertEqual(plan["sector_filtered_count"], 1)
        self.assertEqual({item["sector_label"] for item in plan["allocations"]}, {"Electrica", "Tecnologia y defensa"})
        self.assertEqual({item["position"].ticker for item in plan["allocations"]}, {"IBE", "IDR"})

    def test_allocation_plan_can_limit_total_number_of_companies(self):
        strongest = {
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                ticker="IDR",
                company_name="Indra",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_name="IBEX 35",
                benchmark_symbol="^IBEX",
                shares=Decimal("0"),
                average_cost_per_share=Decimal("0"),
                current_price_per_share=Decimal("18"),
            ),
            "sector_label": "Tecnologia y defensa",
            "reference_label": "IBEX 35",
            "trade_alert": {"label": "Comprar", "tone": "buy", "note": ""},
            "projection": {
                "available": True,
                "base_return_pct": Decimal("24.0"),
                "projected_price": Decimal("22.32"),
                "confidence_label": "Alta",
                "coefficient": Decimal("0.55"),
                "safety_score": Decimal("74.00"),
                "net_income_yield_pct": Decimal("1.20"),
                "gross_dividend_yield_pct": Decimal("1.50"),
                "transaction_drag_pct": Decimal("0.10"),
                "annualized_volatility_pct": Decimal("17.00"),
                "positive_year_ratio_pct": Decimal("72.00"),
                "years_covered": Decimal("10.00"),
                "cycle_phase": "Expansion",
                "current_drawdown_pct": Decimal("-5.00"),
                "max_drawdown_pct": Decimal("-22.00"),
            },
            "projection_reliability": {"label": "Alta", "score": Decimal("84.00")},
        }
        second = {
            **strongest,
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                ticker="IBE",
                company_name="Iberdrola",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_name="IBEX 35",
                benchmark_symbol="^IBEX",
                shares=Decimal("0"),
                average_cost_per_share=Decimal("0"),
                current_price_per_share=Decimal("11"),
            ),
            "sector_label": "Electrica",
            "projection": {**strongest["projection"], "base_return_pct": Decimal("17.0"), "projected_price": Decimal("12.87")},
        }
        third = {
            **strongest,
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                ticker="REP",
                company_name="Repsol",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_name="IBEX 35",
                benchmark_symbol="^IBEX",
                shares=Decimal("0"),
                average_cost_per_share=Decimal("0"),
                current_price_per_share=Decimal("14"),
            ),
            "sector_label": "Energia",
            "projection": {**strongest["projection"], "base_return_pct": Decimal("12.0"), "projected_price": Decimal("15.68")},
        }

        plan = build_equity_allocation_plan(
            [third, second, strongest],
            Decimal("100000"),
            Decimal("50"),
            2,
            0,
        )

        self.assertTrue(plan["available"])
        self.assertEqual(plan["max_total_positions"], 2)
        self.assertEqual(plan["position_cap_filtered_count"], 1)
        self.assertEqual(len(plan["allocations"]), 2)
        self.assertEqual({item["position"].ticker for item in plan["allocations"]}, {"IDR", "IBE"})

    def test_allocation_plan_can_filter_allowed_sectors(self):
        strongest = {
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                ticker="IDR",
                company_name="Indra",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_name="IBEX 35",
                benchmark_symbol="^IBEX",
                shares=Decimal("0"),
                average_cost_per_share=Decimal("0"),
                current_price_per_share=Decimal("18"),
            ),
            "sector_label": "Tecnologia y defensa",
            "reference_label": "IBEX 35",
            "trade_alert": {"label": "Comprar", "tone": "buy", "note": ""},
            "projection": {
                "available": True,
                "base_return_pct": Decimal("24.0"),
                "projected_price": Decimal("22.32"),
                "confidence_label": "Alta",
                "coefficient": Decimal("0.55"),
                "safety_score": Decimal("74.00"),
                "net_income_yield_pct": Decimal("1.20"),
                "gross_dividend_yield_pct": Decimal("1.50"),
                "transaction_drag_pct": Decimal("0.10"),
                "annualized_volatility_pct": Decimal("17.00"),
                "positive_year_ratio_pct": Decimal("72.00"),
                "years_covered": Decimal("10.00"),
                "cycle_phase": "Expansion",
                "current_drawdown_pct": Decimal("-5.00"),
                "max_drawdown_pct": Decimal("-22.00"),
            },
            "projection_reliability": {"label": "Alta", "score": Decimal("84.00")},
        }
        electric = {
            **strongest,
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                ticker="IBE",
                company_name="Iberdrola",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_name="IBEX 35",
                benchmark_symbol="^IBEX",
                shares=Decimal("0"),
                average_cost_per_share=Decimal("0"),
                current_price_per_share=Decimal("11"),
            ),
            "sector_label": "Electrica",
            "projection": {**strongest["projection"], "base_return_pct": Decimal("17.0"), "projected_price": Decimal("12.87")},
        }
        energy = {
            **strongest,
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                ticker="REP",
                company_name="Repsol",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_name="IBEX 35",
                benchmark_symbol="^IBEX",
                shares=Decimal("0"),
                average_cost_per_share=Decimal("0"),
                current_price_per_share=Decimal("14"),
            ),
            "sector_label": "Energia",
            "projection": {**strongest["projection"], "base_return_pct": Decimal("12.0"), "projected_price": Decimal("15.68")},
        }

        plan = build_equity_allocation_plan(
            [strongest, electric, energy],
            Decimal("100000"),
            Decimal("50"),
            0,
            0,
            selected_sectors=["Electrica"],
        )

        self.assertTrue(plan["available"])
        self.assertEqual(plan["selected_sectors"], ["Electrica"])
        self.assertIn("Electrica", plan["selected_sector_note"])
        self.assertEqual(len(plan["allocations"]), 1)
        self.assertEqual(plan["allocations"][0]["position"].ticker, "IBE")

    def test_allocation_plan_can_keep_cash_when_no_positive_candidates(self):
        losing_card = {
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                ticker="TEF",
                company_name="Telefonica",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_name="IBEX 35",
                benchmark_symbol="^IBEX",
                shares=Decimal("0"),
                average_cost_per_share=Decimal("0"),
                current_price_per_share=Decimal("4"),
            ),
            "reference_label": "IBEX 35",
            "projection": {
                "available": True,
                "base_return_pct": Decimal("-6.0"),
                "projected_price": Decimal("3.76"),
                "confidence_label": "Media",
                "coefficient": Decimal("0.22"),
            },
        }

        plan = build_equity_allocation_plan([losing_card], Decimal("50000"), Decimal("25"))

        self.assertFalse(plan["available"])
        self.assertIn("ninguna accion", plan["reason"].lower())

    def test_allocation_plan_recalculates_costs_and_dividends_for_assigned_capital(self):
        santander_card = {
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                broker="Banco Santander",
                trade_channel=EquityPosition.TradeChannel.OFFICE,
                ticker="IBE",
                quote_symbol="IBE.MC",
                company_name="Iberdrola",
                reference_profile=EquityPosition.ReferenceProfile.SPAIN_ELECTRICITY_DEMAND,
                benchmark_name="Demanda electrica Espana",
                benchmark_symbol="REE:demand:es:peninsular",
                shares=Decimal("0"),
                average_cost_per_share=Decimal("0"),
                current_price_per_share=Decimal("11.00"),
                annual_maintenance_cost=Decimal("0"),
            ),
            "reference_label": "Demanda electrica Espana",
            "projection_reliability": {
                "label": "Alta",
                "score": Decimal("80.00"),
            },
            "projection": {
                "available": True,
                "base_return_pct": Decimal("8.70"),
                "price_return_pct": Decimal("6.50"),
                "price_low_return_pct": Decimal("-6.00"),
                "price_high_return_pct": Decimal("16.00"),
                "projected_price": Decimal("11.7150"),
                "confidence_label": "Alta",
                "safety_score": Decimal("74.00"),
                "gross_dividend_yield_pct": Decimal("4.00"),
                "net_income_yield_pct": Decimal("3.70"),
                "transaction_drag_pct": Decimal("1.50"),
                "annualized_volatility_pct": Decimal("14.00"),
                "positive_year_ratio_pct": Decimal("72.00"),
                "years_covered": Decimal("10.00"),
                "cycle_phase": "Expansion",
                "current_drawdown_pct": Decimal("-4.00"),
                "max_drawdown_pct": Decimal("-22.00"),
            },
        }

        plan = build_equity_allocation_plan([santander_card], Decimal("10000"), Decimal("100"))

        self.assertTrue(plan["available"])
        self.assertEqual(plan["roundtrip_cost_total"], Decimal("150.00"))
        self.assertEqual(plan["annual_cost_total"], Decimal("27.00"))
        self.assertEqual(plan["net_dividend_income_total"], Decimal("398.00"))
        allocation = plan["allocations"][0]
        self.assertEqual(allocation["roundtrip_total_cost"], Decimal("150.00"))
        self.assertEqual(allocation["annual_cost_used"], Decimal("27.00"))
        self.assertEqual(allocation["expected_net_dividend_income"], Decimal("398.00"))
        self.assertEqual(allocation["net_projected_return_pct"], Decimal("8.71"))
        self.assertEqual(allocation["low_return_pct"], Decimal("-3.79"))
        self.assertLess(allocation["net_projected_return_pct"], Decimal("10.50"))

    def test_allocation_plan_discards_small_tickets_when_fixed_costs_are_too_high(self):
        base_projection = {
            "available": True,
            "base_return_pct": Decimal("9.50"),
            "price_return_pct": Decimal("7.40"),
            "price_low_return_pct": Decimal("-4.50"),
            "price_high_return_pct": Decimal("15.20"),
            "projected_price": Decimal("11.7400"),
            "confidence_label": "Alta",
            "safety_score": Decimal("72.00"),
            "gross_dividend_yield_pct": Decimal("3.80"),
            "net_income_yield_pct": Decimal("3.20"),
            "transaction_drag_pct": Decimal("1.50"),
            "annualized_volatility_pct": Decimal("14.00"),
            "positive_year_ratio_pct": Decimal("68.00"),
            "years_covered": Decimal("10.00"),
            "cycle_phase": "Expansion",
            "current_drawdown_pct": Decimal("-3.50"),
            "max_drawdown_pct": Decimal("-18.00"),
        }
        cards = []
        for ticker in ("IBE", "ELE", "ENG"):
            cards.append(
                {
                    "position": EquityPosition(
                        position_kind=EquityPosition.PositionKind.WATCHLIST,
                        broker="Banco Santander",
                        trade_channel=EquityPosition.TradeChannel.OFFICE,
                        ticker=ticker,
                        quote_symbol=f"{ticker}.MC",
                        company_name=ticker,
                        reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                        benchmark_name="IBEX 35",
                        benchmark_symbol="^IBEX",
                        shares=Decimal("0"),
                        average_cost_per_share=Decimal("0"),
                        current_price_per_share=Decimal("11.00"),
                        annual_maintenance_cost=Decimal("0"),
                    ),
                    "status_key": "ibex",
                    "status_label": "Radar IBEX",
                    "reference_label": "IBEX 35",
                    "trade_alert": {"label": "Comprar", "tone": "buy", "note": ""},
                    "projection_reliability": {
                        "label": "Alta",
                        "score": Decimal("80.00"),
                    },
                    "projection": dict(base_projection),
                }
            )

        plan = build_equity_allocation_plan(cards, Decimal("3000"), Decimal("50"))

        self.assertTrue(plan["available"])
        self.assertEqual(len(plan["allocations"]), 2)
        self.assertEqual(plan["ticket_filtered_count"], 1)
        self.assertIn("coste fijo+variable", plan["ticket_filter_note"].lower())
        self.assertTrue(all(item["purchase_total_cost"] == Decimal("13.00") for item in plan["allocations"]))
        self.assertTrue(all(item["allocated_amount"] == Decimal("1500.00") for item in plan["allocations"]))

    def test_unknown_broker_uses_santander_fallback_costs(self):
        costs = estimate_broker_costs(
            broker_name="Broker Desconocido",
            trade_channel="app",
            trade_amount=Decimal("10000"),
            valuation_amount=Decimal("10000"),
            annual_dividend_income=Decimal("400"),
            quote_symbol="IBE.MC",
        )

        self.assertEqual(costs["profile_key"], "santander_fallback")
        self.assertEqual(costs["purchase_total_cost"], Decimal("26.00"))
        self.assertEqual(costs["sale_total_cost"], Decimal("6.00"))
        self.assertEqual(costs["annual_custody_cost"], Decimal("25.00"))
        self.assertIn("Santander", costs["pdf_source_label"])

    @override_settings(EQUITIES_IBEX_UNIVERSE_LIMIT=1)
    def test_process_equity_optimization_run_persists_summary_and_report(self):
        run = EquityOptimizationRun.objects.create(
            reference_code="OPT-TEST-001",
            label="Cartera de prueba",
            total_investment=Decimal("100000"),
            max_company_pct=Decimal("20"),
            max_total_positions=8,
            max_sector_positions=1,
            selected_sectors=["Energia"],
        )
        position = EquityPosition(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            shares=Decimal("1.0000"),
            average_cost_per_share=Decimal("11.0000"),
            current_price_per_share=Decimal("11.0000"),
        )
        card = {
            "position": position,
            "status_key": "ibex",
            "status_label": "Radar IBEX",
            "sector_label": "Energia",
            "reference_label": "IBEX 35",
            "trade_alert": {"label": "Comprar", "tone": "buy", "note": "Tendencia favorable."},
            "projection_reliability": {"label": "Alta", "score": Decimal("82.00")},
            "projection": {
                "available": True,
                "base_return_pct": Decimal("8.50"),
                "price_return_pct": Decimal("6.20"),
                "price_low_return_pct": Decimal("-4.00"),
                "price_high_return_pct": Decimal("13.00"),
                "projected_price": Decimal("11.6800"),
                "confidence_label": "Alta",
                "safety_score": Decimal("74.00"),
                "gross_dividend_yield_pct": Decimal("4.10"),
                "net_income_yield_pct": Decimal("3.30"),
                "transaction_drag_pct": Decimal("0.90"),
                "annualized_volatility_pct": Decimal("13.00"),
                "positive_year_ratio_pct": Decimal("68.00"),
                "years_covered": Decimal("10.00"),
                "cycle_phase": "Expansion",
                "current_drawdown_pct": Decimal("-3.00"),
                "max_drawdown_pct": Decimal("-20.00"),
            },
        }
        dashboard = {
            "optimizer_cards": [card],
            "history_cards": [],
            "ibex_universe_summary": {"analyzed_count": 35},
        }

        with (
            patch("equities.optimization_runs.sync_all_equities_market_data", return_value=[]),
            patch("equities.optimization_runs.build_equity_analysis_dashboard", return_value=dashboard) as mocked_dashboard,
            patch("equities.optimization_runs.build_news_signal_map", return_value={"IBE": {"label": "Prensa favorable", "score": Decimal("2.10"), "items_count": 2, "items": [], "note": "Buen tono", "available": True, "positive_count": 2, "negative_count": 0, "neutral_count": 0}}),
            patch("equities.optimization_runs.build_report_entries", return_value=[]),
            patch("equities.optimization_runs.build_report_html", return_value="<html>informe</html>"),
            patch("equities.optimization_runs.build_report_pdf_html", return_value="<html>pdf</html>"),
        ):
            process_equity_optimization_run(run.id)

        run.refresh_from_db()
        self.assertIsNone(mocked_dashboard.call_args.kwargs["ibex_company_limit"])
        self.assertEqual(run.status, EquityOptimizationRun.Status.COMPLETED)
        self.assertEqual(run.report_html, "<html>informe</html>")
        self.assertEqual(run.report_pdf_html, "<html>pdf</html>")
        self.assertTrue(run.summary_data["available"])
        self.assertEqual(run.summary_data["max_total_positions"], 8)
        self.assertEqual(run.summary_data["selected_sectors"], ["Energia"])
        self.assertEqual(run.summary_data["top_pick_name"], "Iberdrola")
        self.assertEqual(run.progress_data["percent"], 100)
        self.assertEqual(run.progress_data["note"], "Optimizacion completada")
        self.assertEqual(run.progress_data["stage_key"], "report")
        self.assertTrue(run.progress_data["preview_candidates"])
        self.assertTrue(run.progress_data["preview_allocations"])
        self.assertTrue(all(stage["status"] == "completed" for stage in run.progress_data["stages"]))

    @override_settings(EQUITIES_OPTIMIZATION_ASYNC=False)
    def test_launch_optimization_run_still_queues_background_job_when_async_flag_is_off(self):
        with (
            patch("equities.optimization_runs.enqueue_equity_optimization_run") as mocked_enqueue,
            patch("equities.optimization_runs.process_equity_optimization_run") as mocked_process,
        ):
            run = launch_equity_optimization_run(
                total_investment=Decimal("80000"),
                max_company_pct=Decimal("20"),
                max_total_positions=6,
                max_sector_positions=1,
                selected_sectors=["Banca"],
            )

        self.assertEqual(run.status, EquityOptimizationRun.Status.PENDING)
        mocked_enqueue.assert_called_once_with(run.id)
        mocked_process.assert_not_called()

    def test_dashboard_can_extend_optimizer_to_full_ibex_universe(self):
        acerinox = find_equity_company_profile("Acerinox")
        acs = find_equity_company_profile("ACS")
        companies = [
            {
                "ticker": acerinox["ticker"],
                "company_name": acerinox["company_name"],
                "quote_symbol": acerinox["quote_symbol"],
                "sector": acerinox["sector_label"],
                "dividend_yield": Decimal("4.20"),
                "catalog_profile": acerinox,
            },
            {
                "ticker": acs["ticker"],
                "company_name": acs["company_name"],
                "quote_symbol": acs["quote_symbol"],
                "sector": acs["sector_label"],
                "dividend_yield": Decimal("3.10"),
                "catalog_profile": acs,
            },
        ]

        def fake_market_series(symbol, range_key="10y", interval="1d"):
            growth = Decimal("1.0210") if symbol.startswith("ACS") else Decimal("1.0170")
            return build_compound_market_series(symbol, symbol, growth=growth, start_price=Decimal("12.0000"))

        def fake_reference_series(reference_profile, benchmark_symbol="", benchmark_name=""):
            return build_compound_market_series(
                benchmark_symbol or "^IBEX",
                benchmark_name or "Referencia",
                growth=Decimal("1.0070"),
                start_price=Decimal("100.0000"),
            )

        empty_workbook = {
            "available": False,
            "path": "",
            "companies": [],
            "companies_by_key": {},
            "indicators_by_name": {},
            "indicators_by_key": {},
            "indicator_name_by_short": {},
            "sector_map": {},
        }

        with (
            patch("equities.services.load_ibex_reference_workbook_snapshot", return_value=empty_workbook),
            patch("equities.services.build_ibex_universe_companies", return_value=companies),
            patch("equities.services.fetch_market_series", side_effect=fake_market_series),
            patch("equities.services.fetch_reference_series_for_choice", side_effect=fake_reference_series),
        ):
            dashboard = build_equity_analysis_dashboard(
                [],
                include_ibex_universe=True,
                ibex_company_limit=2,
            )

        self.assertEqual(len(dashboard["ibex_universe_rows"]), 2)
        self.assertEqual(len(dashboard["optimizer_cards"]), 2)
        self.assertTrue(all(row["status_key"] == "ibex" for row in dashboard["ibex_universe_rows"]))
        self.assertGreaterEqual(dashboard["ibex_universe_summary"]["buy_alert_count"], 1)
        self.assertEqual(dashboard["ibex_universe_summary"]["broker_assumption"], "Interactive Brokers")

    def test_ibex_universe_master_list_includes_registered_positions_without_duplication(self):
        iberdrola = find_equity_company_profile("Iberdrola")
        acs = find_equity_company_profile("ACS")
        tracked = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Seguimiento",
            ticker=iberdrola["ticker"],
            quote_symbol=iberdrola["quote_symbol"],
            reference_profile=EquityPosition.ReferenceProfile.SPAIN_ELECTRICITY_DEMAND,
            benchmark_symbol=SPAIN_ELECTRICITY_DEMAND_SYMBOL,
            benchmark_name=SPAIN_ELECTRICITY_DEMAND_NAME,
            company_name=iberdrola["company_name"],
            shares=Decimal("0"),
            average_cost_per_share=Decimal("12.0000"),
            current_price_per_share=Decimal("12.2000"),
        )
        stock_series = build_compound_market_series(
            tracked.quote_symbol,
            tracked.company_name,
            growth=Decimal("1.0100"),
            start_price=Decimal("12.0000"),
        )
        reference_series = build_compound_market_series(
            tracked.benchmark_symbol,
            tracked.benchmark_name,
            growth=Decimal("1.0060"),
            start_price=Decimal("100.0000"),
        )
        for stock_point, reference_point in zip(stock_series.points, reference_series.points):
            tracked.price_history.create(
                price_date=stock_point["date"],
                open_price=stock_point["open"],
                high_price=stock_point["high"],
                low_price=stock_point["low"],
                close_price=stock_point["close"],
                benchmark_close=reference_point["close"],
            )

        companies = [
            {
                "ticker": iberdrola["ticker"],
                "company_name": iberdrola["company_name"],
                "quote_symbol": iberdrola["quote_symbol"],
                "sector": iberdrola["sector_label"],
                "dividend_yield": Decimal("4.20"),
                "catalog_profile": iberdrola,
            },
            {
                "ticker": acs["ticker"],
                "company_name": acs["company_name"],
                "quote_symbol": acs["quote_symbol"],
                "sector": acs["sector_label"],
                "dividend_yield": Decimal("3.10"),
                "catalog_profile": acs,
            },
        ]

        def fake_market_series(symbol, range_key="10y", interval="1d"):
            growth = Decimal("1.0210") if symbol.startswith("ACS") else Decimal("1.0170")
            return build_compound_market_series(symbol, symbol, growth=growth, start_price=Decimal("12.0000"))

        def fake_reference_series(reference_profile, benchmark_symbol="", benchmark_name=""):
            return build_compound_market_series(
                benchmark_symbol or "^IBEX",
                benchmark_name or "Referencia",
                growth=Decimal("1.0070"),
                start_price=Decimal("100.0000"),
            )

        empty_workbook = {
            "available": False,
            "path": "",
            "companies": [],
            "companies_by_key": {},
            "indicators_by_name": {},
            "indicators_by_key": {},
            "indicator_name_by_short": {},
            "sector_map": {},
        }

        with (
            patch("equities.services.load_ibex_reference_workbook_snapshot", return_value=empty_workbook),
            patch("equities.services.build_ibex_universe_companies", return_value=companies),
            patch("equities.services.fetch_market_series", side_effect=fake_market_series),
            patch("equities.services.fetch_reference_series_for_choice", side_effect=fake_reference_series),
        ):
            dashboard = build_equity_analysis_dashboard([tracked], include_ibex_universe=True, ibex_company_limit=5)

        rows = dashboard["ibex_universe_rows"]
        self.assertEqual(len(rows), 2)
        tracked_row = next(row for row in rows if row["ticker"] == "IBE")
        self.assertEqual(tracked_row["status_label"], "En seguimiento")
        self.assertEqual(dashboard["ibex_universe_summary"]["registered_watchlist_count"], 1)
        self.assertEqual(dashboard["ibex_universe_summary"]["radar_only_count"], 1)
        self.assertEqual(len(dashboard["optimizer_cards"]), 2)

    def test_dashboard_uses_summary_mode_for_full_ibex_universe_optimizer(self):
        acs = find_equity_company_profile("ACS")
        company = {
            "ticker": acs["ticker"],
            "company_name": acs["company_name"],
            "quote_symbol": acs["quote_symbol"],
            "sector": acs["sector_label"],
            "dividend_yield": Decimal("3.10"),
            "catalog_profile": acs,
        }

        def fake_market_series(symbol, range_key="10y", interval="1d"):
            growth = Decimal("1.0210") if symbol.startswith("ACS") else Decimal("1.0070")
            start_price = Decimal("12.0000") if symbol.startswith("ACS") else Decimal("100.0000")
            return build_compound_market_series(symbol, symbol, growth=growth, start_price=start_price)

        def fake_reference_series(reference_profile, benchmark_symbol="", benchmark_name=""):
            return build_compound_market_series(
                benchmark_symbol or "^IBEX",
                benchmark_name or "Referencia",
                growth=Decimal("1.0070"),
                start_price=Decimal("100.0000"),
            )

        empty_workbook = {
            "available": False,
            "path": "",
            "companies": [],
            "companies_by_key": {},
            "indicators_by_name": {},
            "indicators_by_key": {},
            "indicator_name_by_short": {},
            "sector_map": {},
        }

        with (
            patch("equities.services.load_ibex_reference_workbook_snapshot", return_value=empty_workbook),
            patch("equities.services.build_ibex_universe_companies", return_value=[company]),
            patch("equities.services.fetch_market_series", side_effect=fake_market_series),
            patch("equities.services.fetch_reference_series_for_choice", side_effect=fake_reference_series),
        ):
            dashboard = build_equity_analysis_dashboard(
                [],
                include_ibex_universe=True,
                ibex_company_limit=1,
            )

        self.assertEqual(len(dashboard["optimizer_cards"]), 1)
        card = dashboard["optimizer_cards"][0]
        self.assertFalse(card["historical_chart"]["available"])
        self.assertFalse(card["best_correlation_chart"]["available"])
        self.assertFalse(card["projection_12m_chart"]["available"])
        self.assertFalse(card["projection_backtest"]["monthly_chart"]["available"])
        self.assertEqual(card["suggested_references"], [])

    def test_fetch_market_series_reuses_cache_within_same_bucket(self):
        class FakeHTTPResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.close()
                return False

        payload = {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {
                            "regularMarketPrice": 12.34,
                            "regularMarketTime": 1712707200,
                            "shortName": "Iberdrola",
                        },
                        "timestamp": [1712620800, 1712707200],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [12.0, 12.2],
                                    "high": [12.3, 12.5],
                                    "low": [11.9, 12.1],
                                    "close": [12.15, 12.34],
                                }
                            ]
                        },
                    }
                ],
            }
        }

        with (
            patch("equities.services.build_market_data_cache_bucket", return_value=777),
            patch(
                "equities.services.urlopen",
                side_effect=[
                    FakeHTTPResponse(json.dumps(payload).encode("utf-8")),
                ],
            ) as mocked_urlopen,
        ):
            first_series = fetch_market_series("ibe.mc")
            second_series = fetch_market_series("IBE.MC")

        self.assertEqual(mocked_urlopen.call_count, 1)
        self.assertEqual(first_series.latest_price, Decimal("12.3400"))
        self.assertEqual(second_series.latest_price, Decimal("12.3400"))
        self.assertIsNot(first_series, second_series)
        self.assertIsNot(first_series.points, second_series.points)

@override_settings(EQUITIES_AUTO_SYNC_ON_VIEW=False, EQUITIES_IBEX_UNIVERSE_ANALYSIS=False)
class EquitiesViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="equity-owner",
            password="StrongPass123!",
        )
        self.client.force_login(self.user)

    def tearDown(self):
        load_ibex_reference_workbook_snapshot.cache_clear()
        clear_market_data_caches()
        super().tearDown()

    def test_can_create_equity_position_from_page_form(self):
        response = self.client.post(
            reverse("equities:list"),
            {
                "action": "create_position",
                "position_kind": EquityPosition.PositionKind.OWNED,
                "ownership_category": AssetOwnershipCategory.XIMO,
                "broker": "Interactive Brokers",
                "ticker": "ibe",
                "company_name": "Iberdrola",
                "quote_symbol": "ibe.mc",
                "reference_profile": EquityPosition.ReferenceProfile.MARKET_INDEX,
                "benchmark_symbol": "^ibex",
                "benchmark_name": "IBEX 35",
                "shares": "125,5000",
                "average_cost_per_share": "10,2500",
                "current_price_per_share": "",
                "annual_dividend_income": "72,50",
                "annual_maintenance_cost": "18,75",
                "notes": "Posicion principal",
            },
        )

        self.assertRedirects(response, reverse("equities:list"))
        position = EquityPosition.objects.get(ticker="IBE", broker="Interactive Brokers")
        self.assertEqual(position.company_name, "Iberdrola")
        self.assertEqual(position.quote_symbol, "IBE.MC")
        self.assertEqual(position.benchmark_symbol, "^IBEX")
        self.assertEqual(position.ownership_category, AssetOwnershipCategory.XIMO)
        self.assertEqual(position.position_kind, EquityPosition.PositionKind.OWNED)
        self.assertEqual(position.trade_channel, EquityPosition.TradeChannel.APP)
        self.assertEqual(position.shares, Decimal("125.5000"))
        self.assertEqual(position.average_cost_per_share, Decimal("10.2500"))
        self.assertEqual(position.current_price_per_share, Decimal("10.2500"))
        self.assertEqual(position.annual_dividend_income, Decimal("72.50"))
        self.assertEqual(position.annual_maintenance_cost, Decimal("18.75"))

    def test_can_create_equity_position_by_only_typing_indra(self):
        response = self.client.post(
            reverse("equities:list"),
            {
                "action": "create_position",
                "position_kind": EquityPosition.PositionKind.OWNED,
                "ownership_category": AssetOwnershipCategory.XIMO,
                "broker": "Interactive Brokers",
                "ticker": "",
                "company_name": "Indra",
                "quote_symbol": "",
                "reference_profile": EquityPosition.ReferenceProfile.MARKET_INDEX,
                "benchmark_symbol": "",
                "benchmark_name": "",
                "shares": "20",
                "average_cost_per_share": "18,5000",
                "current_price_per_share": "",
                "annual_dividend_income": "0",
                "annual_maintenance_cost": "5,00",
                "notes": "Alta rapida",
            },
        )

        self.assertRedirects(response, reverse("equities:list"))
        position = EquityPosition.objects.get(ticker="IDR", broker="Interactive Brokers")
        self.assertEqual(position.ticker, "IDR")
        self.assertEqual(position.quote_symbol, "IDR.MC")
        self.assertEqual(position.benchmark_symbol, "^IBEX")

    def test_can_delete_watchlist_position(self):
        position = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            ownership_category=AssetOwnershipCategory.XIMO,
            broker="Seguimiento",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            shares=Decimal("0"),
            average_cost_per_share=Decimal("12.0000"),
            current_price_per_share=Decimal("12.0000"),
        )

        response = self.client.post(
            reverse("equities:list"),
            {
                "action": "delete_position",
                "position_id": str(position.id),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"{reverse('equities:list')}#equity-ibex")
        self.assertFalse(EquityPosition.objects.filter(pk=position.id).exists())

    def test_can_close_owned_position_and_move_it_to_sales_history(self):
        position = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.OWNED,
            ownership_category=AssetOwnershipCategory.XIMO,
            broker="Interactive Brokers",
            trade_channel=EquityPosition.TradeChannel.APP,
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2025, 1, 10),
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("12.3000"),
            annual_dividend_income=Decimal("18.00"),
            annual_maintenance_cost=Decimal("4.00"),
        )

        response = self.client.post(
            reverse("equities:list"),
            {
                "action": "close_position",
                "position_id": str(position.id),
                "closed_on": "2026-04-12",
                "sale_price_per_share": "12,8000",
                "notes": "Venta completa",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"{reverse('equities:list')}#equity-journey")
        self.assertFalse(EquityPosition.objects.filter(pk=position.id).exists())
        closed = EquityClosedPosition.objects.get(ticker="IBE")
        self.assertEqual(closed.sale_price_per_share, Decimal("12.8000"))
        self.assertEqual(closed.closed_on, date(2026, 4, 12))
        self.assertEqual(closed.notes, "Venta completa")

    def test_bank_company_name_switches_default_reference_to_euribor(self):
        response = self.client.post(
            reverse("equities:list"),
            {
                "action": "create_position",
                "position_kind": EquityPosition.PositionKind.OWNED,
                "ownership_category": AssetOwnershipCategory.JOINT,
                "broker": "Banco Sabadell",
                "ticker": "",
                "company_name": "Banco Santander",
                "quote_symbol": "",
                "reference_profile": EquityPosition.ReferenceProfile.MARKET_INDEX,
                "benchmark_symbol": "",
                "benchmark_name": "",
                "shares": "15",
                "average_cost_per_share": "4,2500",
                "current_price_per_share": "",
                "annual_dividend_income": "20",
                "annual_maintenance_cost": "0",
                "notes": "",
            },
        )

        self.assertRedirects(response, reverse("equities:list"))
        position = EquityPosition.objects.get(ticker="SAN", broker="Banco Sabadell")
        self.assertEqual(position.reference_profile, EquityPosition.ReferenceProfile.EURIBOR_12M)
        self.assertEqual(position.benchmark_symbol, EURIBOR_REFERENCE_SYMBOL)

    def test_watchlist_can_be_saved_without_shares_or_prices(self):
        response = self.client.post(
            reverse("equities:list"),
            {
                "action": "create_position",
                "position_kind": EquityPosition.PositionKind.WATCHLIST,
                "ownership_category": AssetOwnershipCategory.JOINT,
                "broker": "",
                "ticker": "",
                "company_name": "Iberdrola",
                "quote_symbol": "",
                "reference_profile": EquityPosition.ReferenceProfile.MARKET_INDEX,
                "benchmark_symbol": "",
                "benchmark_name": "",
                "shares": "",
                "average_cost_per_share": "",
                "current_price_per_share": "",
                "annual_dividend_income": "",
                "annual_maintenance_cost": "",
                "notes": "",
            },
        )

        self.assertRedirects(response, reverse("equities:list"))
        position = EquityPosition.objects.get(ticker="IBE", broker="Seguimiento")
        self.assertEqual(position.position_kind, EquityPosition.PositionKind.WATCHLIST)
        self.assertEqual(position.shares, Decimal("0"))
        self.assertEqual(position.average_cost_per_share, Decimal("0"))
        self.assertEqual(position.current_price_per_share, Decimal("0"))
        self.assertEqual(position.reference_profile, EquityPosition.ReferenceProfile.SPAIN_ELECTRICITY_DEMAND)

    def test_can_change_reference_from_analysis_card(self):
        position = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Seguimiento",
            ticker="IBE",
            quote_symbol="IBE.MC",
            reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            shares=Decimal("0"),
            average_cost_per_share=Decimal("0"),
            current_price_per_share=Decimal("0"),
        )

        with patch("equities.views.sync_equity_market_data") as mocked_sync:
            response = self.client.post(
                reverse("equities:list"),
                {
                    "action": "change_reference",
                    "position_id": str(position.id),
                    "reference_profile": EquityPosition.ReferenceProfile.SPAIN_ELECTRICITY_DEMAND,
                    "benchmark_symbol": SPAIN_ELECTRICITY_DEMAND_SYMBOL,
                    "benchmark_name": SPAIN_ELECTRICITY_DEMAND_NAME,
                },
            )

        self.assertRedirects(response, reverse("equities:list"))
        position.refresh_from_db()
        self.assertEqual(position.reference_profile, EquityPosition.ReferenceProfile.SPAIN_ELECTRICITY_DEMAND)
        self.assertEqual(position.benchmark_symbol, SPAIN_ELECTRICITY_DEMAND_SYMBOL)
        mocked_sync.assert_called_once()

    def test_can_store_same_ticker_for_same_broker_with_different_owner(self):
        EquityPosition.objects.create(
            ownership_category=AssetOwnershipCategory.XIMO,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("9.0000"),
            current_price_per_share=Decimal("10.0000"),
        )

        response = self.client.post(
            reverse("equities:list"),
            {
                "action": "create_position",
                "position_kind": EquityPosition.PositionKind.OWNED,
                "ownership_category": AssetOwnershipCategory.MONICA,
                "broker": "Interactive Brokers",
                "ticker": "IBE",
                "company_name": "Iberdrola",
                "quote_symbol": "IBE.MC",
                "reference_profile": EquityPosition.ReferenceProfile.MARKET_INDEX,
                "benchmark_symbol": "^IBEX",
                "benchmark_name": "IBEX 35",
                "shares": "25",
                "average_cost_per_share": "10,5000",
                "current_price_per_share": "11,2500",
                "annual_dividend_income": "20,00",
                "annual_maintenance_cost": "4,50",
                "notes": "",
            },
        )

        self.assertRedirects(response, reverse("equities:list"))
        self.assertEqual(EquityPosition.objects.filter(broker="Interactive Brokers", ticker="IBE").count(), 2)
        self.assertTrue(
            EquityPosition.objects.filter(
                broker="Interactive Brokers",
                ticker="IBE",
                ownership_category=AssetOwnershipCategory.MONICA,
                shares=Decimal("25.0000"),
                annual_maintenance_cost=Decimal("4.50"),
            ).exists()
        )

    def test_updating_position_does_not_fail_if_duplicate_rows_already_exist(self):
        first = EquityPosition.objects.create(
            ownership_category=AssetOwnershipCategory.XIMO,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola antigua",
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("9.0000"),
            current_price_per_share=Decimal("10.0000"),
        )
        EquityPosition.objects.create(
            ownership_category=AssetOwnershipCategory.XIMO,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola duplicada",
            shares=Decimal("12.0000"),
            average_cost_per_share=Decimal("9.5000"),
            current_price_per_share=Decimal("10.5000"),
        )

        response = self.client.post(
            reverse("equities:list"),
            {
                "action": "create_position",
                "position_kind": EquityPosition.PositionKind.OWNED,
                "ownership_category": AssetOwnershipCategory.XIMO,
                "broker": "Interactive Brokers",
                "ticker": "IBE",
                "company_name": "Iberdrola revisada",
                "quote_symbol": "IBE.MC",
                "reference_profile": EquityPosition.ReferenceProfile.MARKET_INDEX,
                "benchmark_symbol": "^IBEX",
                "benchmark_name": "IBEX 35",
                "shares": "25",
                "average_cost_per_share": "10,5000",
                "current_price_per_share": "11,2500",
                "annual_dividend_income": "20,00",
                "annual_maintenance_cost": "3,10",
                "notes": "Actualizada",
            },
        )

        self.assertRedirects(response, reverse("equities:list"))
        self.assertEqual(EquityPosition.objects.filter(broker="Interactive Brokers", ticker="IBE").count(), 2)
        first.refresh_from_db()
        self.assertEqual(first.company_name, "Iberdrola antigua")
        latest = EquityPosition.objects.order_by("-updated_at", "-id").first()
        self.assertEqual(latest.company_name, "Iberdrola revisada")
        self.assertEqual(latest.shares, Decimal("25.0000"))
        self.assertEqual(latest.annual_maintenance_cost, Decimal("3.10"))

    def test_can_prefill_equity_form_from_xls_document(self):
        document = SimpleUploadedFile(
            "posicion.xls",
            b"""
<html>
  <body>
    <table>
      <tr><td>Titular: Monica</td></tr>
      <tr>
        <td>Broker</td>
        <td>Ticker</td>
        <td>Empresa</td>
        <td>Acciones</td>
        <td>Coste medio</td>
        <td>Precio actual</td>
        <td>Dividendo anual</td>
      </tr>
      <tr>
        <td>ING</td>
        <td>ENG</td>
        <td>Enagas</td>
        <td>150,0000</td>
        <td>13,4500</td>
        <td>14,2000</td>
        <td>95,00</td>
      </tr>
    </table>
  </body>
</html>
""",
            content_type="application/vnd.ms-excel",
        )

        response = self.client.post(
            reverse("equities:list"),
            {
                "action": "prefill_from_document",
                "document": document,
                "default_broker": "",
                "default_ownership_category": AssetOwnershipCategory.JOINT,
            },
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["position_form"]
        self.assertEqual(form.initial["broker"], "ING")
        self.assertEqual(form.initial["ticker"], "ENG")
        self.assertEqual(form.initial["company_name"], "Enagas")
        self.assertEqual(form.initial["ownership_category"], AssetOwnershipCategory.MONICA)
        self.assertEqual(form.initial["position_kind"], EquityPosition.PositionKind.OWNED)
        self.assertEqual(form.initial["shares"], Decimal("150.0000"))
        self.assertEqual(form.initial["average_cost_per_share"], Decimal("13.4500"))
        self.assertEqual(form.initial["current_price_per_share"], Decimal("14.2000"))
        self.assertEqual(form.initial["annual_dividend_income"], Decimal("95.00"))
        self.assertEqual(response.context["prefill_source_filename"], "posicion.xls")

    def test_can_prefill_equity_form_from_pdf_document(self):
        document = SimpleUploadedFile(
            "posicion.pdf",
            b"%PDF-1.4 fake",
            content_type="application/pdf",
        )

        with patch(
            "equities.services.read_pdf_pages",
            return_value=[
                "\n".join(
                    [
                        "Titular:\tXimo",
                        "Broker\tSelf Bank",
                        "Ticker\tIBE",
                        "Empresa\tIberdrola",
                        "Acciones\t125,5000",
                        "Coste medio\t10,2500",
                        "Precio actual\t11,0000",
                        "Dividendos anuales\t72,50",
                    ]
                )
            ],
        ):
            response = self.client.post(
                reverse("equities:list"),
                {
                    "action": "prefill_from_document",
                    "document": document,
                    "default_broker": "",
                    "default_ownership_category": AssetOwnershipCategory.JOINT,
                },
            )

        self.assertEqual(response.status_code, 200)
        form = response.context["position_form"]
        self.assertEqual(form.initial["broker"], "Self Bank")
        self.assertEqual(form.initial["ticker"], "IBE")
        self.assertEqual(form.initial["company_name"], "Iberdrola")
        self.assertEqual(form.initial["ownership_category"], AssetOwnershipCategory.XIMO)
        self.assertEqual(form.initial["position_kind"], EquityPosition.PositionKind.OWNED)
        self.assertEqual(form.initial["shares"], Decimal("125.5000"))
        self.assertEqual(form.initial["average_cost_per_share"], Decimal("10.2500"))
        self.assertEqual(form.initial["current_price_per_share"], Decimal("11.0000"))
        self.assertEqual(form.initial["annual_dividend_income"], Decimal("72.50"))

    def test_equities_page_renders_without_auto_sync_when_disabled(self):
        EquityPosition.objects.create(
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Banco Sabadell",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("12.0000"),
            annual_dividend_income=Decimal("40.00"),
            annual_maintenance_cost=Decimal("6.00"),
        )

        response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cockpit de acciones")
        self.assertContains(response, "Canal de compra")
        self.assertContains(response, "Indra Sistemas, S.A.")

    @override_settings(EQUITIES_AUTO_SYNC_ON_VIEW=True)
    def test_optimizer_request_skips_auto_sync_even_when_enabled(self):
        EquityPosition.objects.create(
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("12.0000"),
        )

        with patch("equities.views.sync_all_equities_market_data") as mocked_sync:
            response = self.client.get(
                reverse("equities:list"),
                {
                    "total_investment": "400000",
                    "max_company_pct": "20",
                    "max_sector_positions": "1",
                },
            )

        self.assertEqual(response.status_code, 200)
        mocked_sync.assert_not_called()

    @override_settings(EQUITIES_IBEX_UNIVERSE_ANALYSIS=False, EQUITIES_IBEX_UNIVERSE_LIMIT=3)
    def test_optimizer_request_forces_full_ibex_analysis_even_if_page_limit_is_disabled(self):
        EquityPosition.objects.create(
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("12.0000"),
        )

        with patch("equities.views.build_equity_analysis_dashboard") as mocked_dashboard:
            mocked_dashboard.return_value = {
                "overview": {
                    "owned_positions_count": 0,
                    "watchlist_positions_count": 0,
                    "invested_amount": Decimal("0"),
                    "current_value": Decimal("0"),
                    "net_annual_income_total": Decimal("0"),
                },
                "history_cards": [],
                "owned_positions": [],
                "watchlist_positions": [],
                "owned_history_cards": [],
                "watchlist_history_cards": [],
                "decision_rows": [],
                "ibex_universe_rows": [],
                "ibex_universe_summary": {
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
                },
                "tracked_reference_rows": [],
                "reference_guide_rows": [],
                "reference_guide_summary": {"workbook_loaded": False},
                "optimizer_cards": [],
            }

            response = self.client.get(
                reverse("equities:list"),
                {
                    "total_investment": "400000",
                    "max_company_pct": "20",
                    "max_sector_positions": "1",
                },
            )

        self.assertEqual(response.status_code, 200)
        mocked_dashboard.assert_called_once()
        self.assertTrue(mocked_dashboard.call_args.kwargs["include_ibex_universe"])
        self.assertIsNone(mocked_dashboard.call_args.kwargs["ibex_company_limit"])

    def test_equities_page_uses_ibex_radar_as_master_list_for_saved_watchlist(self):
        with patch("equities.views.build_equity_analysis_dashboard") as mocked_dashboard:
            mocked_dashboard.return_value = {
                "overview": {
                    "owned_positions_count": 1,
                    "watchlist_positions_count": 1,
                    "invested_amount": Decimal("1000"),
                    "current_value": Decimal("1100"),
                    "annual_dividends_total": Decimal("30"),
                    "net_dividends_total": Decimal("24"),
                    "annual_maintenance_total": Decimal("6"),
                    "purchase_cost_total": Decimal("4"),
                    "net_annual_income_total": Decimal("20"),
                    "unrealized_gain_total": Decimal("50"),
                    "unrealized_return_pct": Decimal("5"),
                    "weighted_projected_return_12m": Decimal("8"),
                    "weighted_safety_score": Decimal("62"),
                    "weighted_periods": [],
                    "best_decision": None,
                },
                "history_cards": [],
                "owned_positions": [],
                "watchlist_positions": [],
                "owned_history_cards": [],
                "watchlist_history_cards": [],
                "decision_rows": [],
                "ibex_universe_rows": [
                    {
                        "ticker": "IBE",
                        "company_name": "Iberdrola",
                        "status_label": "En seguimiento",
                        "status_note": "La tienes guardada",
                        "trade_alert_label": "Comprar",
                        "trade_alert_tone": "buy",
                        "trade_alert_trigger": "Pendiente positiva",
                        "reference_label": "IBEX 35",
                        "best_reference_label": "IBEX 35",
                        "correlation": Decimal("0.72"),
                        "years_covered": Decimal("10.00"),
                        "projected_return_pct": Decimal("8.50"),
                        "safety_score": Decimal("70.00"),
                        "reliability_label": "Alta",
                        "benefit_risk_ratio": Decimal("1.80"),
                        "cycle_phase": "Expansion",
                    }
                ],
                "ibex_universe_summary": {
                    "available": True,
                    "analyzed_count": 35,
                    "buy_alert_count": 10,
                    "sell_alert_count": 5,
                    "watch_alert_count": 20,
                    "registered_count": 2,
                    "registered_owned_count": 1,
                    "registered_watchlist_count": 1,
                    "radar_only_count": 33,
                    "failed_count": 0,
                    "failures": [],
                    "broker_assumption": "Interactive Brokers",
                    "trade_channel_label": "App",
                    "top_pick": {"ticker": "IBE", "company_name": "Iberdrola"},
                },
                "tracked_reference_rows": [],
                "reference_guide_rows": [],
                "reference_guide_summary": {"workbook_loaded": False},
                "optimizer_cards": [],
            }

            response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Esta es ahora la lista maestra del IBEX")
        self.assertContains(response, "seguimientos guardados")
        self.assertContains(response, "La tienes guardada")
        self.assertNotContains(response, "Acciones en seguimiento")

    def test_can_launch_background_optimizer_run_from_page(self):
        run = EquityOptimizationRun.objects.create(
            reference_code="OPT-TEST-LAUNCH",
            total_investment=Decimal("100000"),
            max_company_pct=Decimal("20"),
            max_total_positions=0,
            max_sector_positions=1,
            status=EquityOptimizationRun.Status.PENDING,
        )

        with patch("equities.views.launch_equity_optimization_run", return_value=run) as mocked_launch:
            response = self.client.post(
                reverse("equities:list"),
                {
                    "action": "launch_optimizer_run",
                    "reference_label": "Cartera defensiva",
                    "total_investment": "400000",
                    "max_company_pct": "20",
                    "max_total_positions": "8",
                    "max_sector_positions": "1",
                    "selected_sectors": ["Electrica", "Banca"],
                    "restrictions_note": "Maximo una empresa por sector",
                },
            )

        self.assertRedirects(response, f"{reverse('equities:list')}?optimizer_status=1#equity-optimizer")
        mocked_launch.assert_called_once()
        self.assertEqual(mocked_launch.call_args.kwargs["max_total_positions"], 8)
        self.assertEqual(mocked_launch.call_args.kwargs["selected_sectors"], ["Electrica", "Banca"])

    def test_equities_page_renders_optimizer_sector_selection(self):
        response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sectores donde si comprar")
        self.assertContains(response, "Banca")

    def test_equities_page_defers_full_ibex_radar_while_optimization_is_active(self):
        EquityOptimizationRun.objects.create(
            reference_code="OPT-RUNNING-001",
            label="Ejecucion activa",
            total_investment=Decimal("250000"),
            max_company_pct=Decimal("20"),
            max_total_positions=6,
            max_sector_positions=1,
            status=EquityOptimizationRun.Status.RUNNING,
        )
        dashboard = {
            "overview": {
                "invested_amount": Decimal("0"),
                "current_value": Decimal("0"),
                "net_annual_income_total": Decimal("0"),
                "owned_positions_count": 0,
                "watchlist_positions_count": 0,
                "unrealized_return_pct": None,
                "unrealized_gain_total": Decimal("0"),
                "weighted_projected_return_12m": None,
                "weighted_safety_score": None,
                "weighted_periods": [],
                "best_decision": None,
            },
            "history_cards": [],
            "owned_positions": [],
            "watchlist_positions": [],
            "owned_history_cards": [],
            "watchlist_history_cards": [],
            "decision_rows": [],
            "ibex_universe_rows": [],
            "ibex_universe_summary": {"available": False, "analyzed_count": 0},
            "tracked_reference_rows": [],
            "reference_guide_rows": [],
            "reference_guide_summary": {"workbook_loaded": False},
            "optimizer_cards": [],
        }

        with patch("equities.views.build_equity_analysis_dashboard", return_value=dashboard) as mocked_dashboard:
            response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(mocked_dashboard.call_args.kwargs["include_ibex_universe"])
        self.assertContains(response, "se aplaza temporalmente mientras hay optimizaciones activas")

    def test_equities_page_renders_completed_optimization_comparison_table(self):
        first = EquityOptimizationRun.objects.create(
            reference_code="OPT-COMP-001",
            label="Cartera defensiva",
            total_investment=Decimal("300000"),
            max_company_pct=Decimal("20"),
            max_total_positions=6,
            max_sector_positions=1,
            selected_sectors=["Electrica", "Tecnologia y defensa"],
            status=EquityOptimizationRun.Status.COMPLETED,
            report_html="<html>uno</html>",
            summary_data={
                "available": True,
                "created_at_label": "2026-04-10 08:00",
                "completed_at_label": "2026-04-10 08:12",
                "total_investment": 300000,
                "max_company_pct": 20,
                "max_total_positions": 6,
                "max_sector_positions": 1,
                "selected_sectors": ["Electrica", "Tecnologia y defensa"],
                "projected_gain_total": 42000,
                "weighted_return_pct": 14.0,
                "weighted_low_return_pct": -4.5,
                "weighted_safety_score": 76,
                "weighted_reliability_score": 71,
                "net_dividend_income_total": 4200,
                "annual_cost_total": 350,
                "roundtrip_cost_total": 980,
                "cash_reserve_amount": 12000,
                "allocations_count": 4,
                "top_pick_name": "Iberdrola",
            },
            allocations_data=[
                {"company_name": "Iberdrola", "ticker": "IBE", "sector_label": "Electrica"},
                {"company_name": "Indra", "ticker": "IDR", "sector_label": "Tecnologia y defensa"},
            ],
        )
        second = EquityOptimizationRun.objects.create(
            reference_code="OPT-COMP-002",
            label="Cartera agresiva",
            total_investment=Decimal("300000"),
            max_company_pct=Decimal("25"),
            max_total_positions=8,
            max_sector_positions=2,
            status=EquityOptimizationRun.Status.COMPLETED,
            report_html="<html>dos</html>",
            summary_data={
                "available": True,
                "created_at_label": "2026-04-11 09:00",
                "completed_at_label": "2026-04-11 09:18",
                "total_investment": 300000,
                "max_company_pct": 25,
                "max_total_positions": 8,
                "max_sector_positions": 2,
                "selected_sectors": [],
                "projected_gain_total": 57000,
                "weighted_return_pct": 19.0,
                "weighted_low_return_pct": -9.0,
                "weighted_safety_score": 62,
                "weighted_reliability_score": 66,
                "net_dividend_income_total": 2600,
                "annual_cost_total": 410,
                "roundtrip_cost_total": 1320,
                "cash_reserve_amount": 0,
                "allocations_count": 5,
                "top_pick_name": "Indra",
            },
            allocations_data=[
                {"company_name": "Indra", "ticker": "IDR", "sector_label": "Tecnologia y defensa"},
                {"company_name": "Repsol", "ticker": "REP", "sector_label": "Energia"},
            ],
        )

        response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Comparacion entre optimizaciones cerradas")
        self.assertContains(response, first.display_label)
        self.assertContains(response, second.display_label)
        self.assertContains(response, "IBE, IDR")
        self.assertContains(response, "IDR, REP")
        self.assertContains(response, "Mayor rentabilidad")
        self.assertContains(response, "Electrica, Tecnologia y defensa")
        self.assertContains(response, "todos los del IBEX")

    def test_optimization_history_table_shows_company_weights(self):
        EquityOptimizationRun.objects.create(
            reference_code="OPT-HIST-001",
            label="Ticket visible",
            total_investment=Decimal("250000"),
            max_company_pct=Decimal("20"),
            max_total_positions=5,
            max_sector_positions=1,
            status=EquityOptimizationRun.Status.COMPLETED,
            report_html="<html>hist</html>",
            summary_data={
                "available": True,
                "projected_gain_total": 12000,
                "weighted_return_pct": 8.4,
                "allocations_count": 3,
                "selected_sectors": ["Electrica", "Energia"],
            },
            allocations_data=[
                {"company_name": "Iberdrola", "ticker": "IBE", "allocated_weight_pct": 35.0},
                {"company_name": "Repsol", "ticker": "REP", "allocated_weight_pct": 25.0},
                {"company_name": "Endesa", "ticker": "ELE", "allocated_weight_pct": 20.0},
            ],
        )

        response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ticket propuesto")
        self.assertContains(response, "Iberdrola")
        self.assertContains(response, "35,0 %")
        self.assertContains(response, "Repsol")
        self.assertContains(response, "25,0 %")

    def test_completed_optimization_run_can_render_and_download_report(self):
        run = EquityOptimizationRun.objects.create(
            reference_code="OPT-TEST-REPORT",
            label="Informe guardado",
            total_investment=Decimal("50000"),
            max_company_pct=Decimal("20"),
            max_sector_positions=1,
            status=EquityOptimizationRun.Status.COMPLETED,
            report_html="<html><body>Informe optimizado</body></html>",
            summary_data={"available": True, "projected_gain_total": 5000},
        )

        report_response = self.client.get(reverse("equities:optimization_report", args=[run.id]))
        download_response = self.client.get(reverse("equities:optimization_download", args=[run.id]))
        download_html_response = self.client.get(reverse("equities:optimization_download_html", args=[run.id]))

        self.assertEqual(report_response.status_code, 200)
        self.assertContains(report_response, "Informe optimizado")
        self.assertEqual(download_response.status_code, 200)
        self.assertEqual(download_response["Content-Type"], "application/pdf")
        self.assertIn("attachment;", download_response.headers["Content-Disposition"])
        self.assertIn("opt-test-report.pdf", download_response.headers["Content-Disposition"])
        self.assertTrue(download_response.content.startswith(b"%PDF"))
        self.assertEqual(download_html_response.status_code, 200)
        self.assertIn("attachment;", download_html_response.headers["Content-Disposition"])
        self.assertIn("opt-test-report.html", download_html_response.headers["Content-Disposition"])

    def test_progress_endpoint_returns_live_payload(self):
        run = EquityOptimizationRun.objects.create(
            reference_code="OPT-TEST-PROGRESS",
            label="Seguimiento visible",
            total_investment=Decimal("50000"),
            max_company_pct=Decimal("20"),
            max_sector_positions=1,
            status=EquityOptimizationRun.Status.RUNNING,
            status_note="Analizando el IBEX",
            progress_data={
                "percent": 47,
                "stage_key": "ibex",
                "stage_label": "Analizando IBEX",
                "note": "Analizando el IBEX: 18/35",
                "current_step": 18,
                "total_steps": 35,
                "current_label": "Iberdrola",
                "stages": [
                    {"key": "sync", "label": "Sincronizando cartera", "status": "completed"},
                    {"key": "dashboard", "label": "Construyendo base de mercado", "status": "completed"},
                    {"key": "ibex", "label": "Analizando IBEX", "status": "active"},
                ],
                "preview_candidates": [
                    {
                        "ticker": "IBE",
                        "company_name": "Iberdrola",
                        "sector_label": "Energia",
                        "trade_alert_label": "Comprar",
                        "net_return_pct": 8.5,
                        "safety_score": 74,
                    }
                ],
                "preview_allocations": [],
                "events": [
                    {"label": "Analizando el IBEX: 18/35", "detail": "Iberdrola", "recorded_at": "16:22:10"}
                ],
                "updated_at_label": "2026-04-12 16:22:10",
            },
        )

        response = self.client.get(reverse("equities:optimization_progress", args=[run.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["reference_code"], "OPT-TEST-PROGRESS")
        self.assertEqual(payload["status"], EquityOptimizationRun.Status.RUNNING)
        self.assertFalse(payload["finished"])
        self.assertEqual(payload["progress"]["percent"], 47)
        self.assertEqual(payload["progress"]["current_label"], "Iberdrola")
        self.assertEqual(payload["progress"]["events"][0]["detail"], "Iberdrola")

    def test_equities_page_renders_ticket_tracking_section(self):
        for ticker, company_name, average_cost, current_price in (
            ("IBE", "Iberdrola", Decimal("10.0000"), Decimal("12.0000")),
            ("ENG", "Enagas", Decimal("14.0000"), Decimal("15.5000")),
        ):
            position = EquityPosition.objects.create(
                ownership_category=AssetOwnershipCategory.JOINT,
                broker="Interactive Brokers",
                ticker=ticker,
                quote_symbol=f"{ticker}.MC",
                benchmark_symbol="^IBEX",
                benchmark_name="IBEX 35",
                company_name=company_name,
                shares=Decimal("20.0000"),
                average_cost_per_share=average_cost,
                current_price_per_share=current_price,
            )
            stock_series = build_compound_market_series(
                f"{ticker}.MC",
                company_name,
                growth=Decimal("1.0180"),
                start_price=average_cost,
            )
            reference_series = build_compound_market_series(
                "^IBEX",
                "IBEX 35",
                growth=Decimal("1.0070"),
                start_price=Decimal("100.0000"),
            )
            for stock_point, reference_point in zip(stock_series.points, reference_series.points):
                position.price_history.create(
                    price_date=stock_point["date"],
                    open_price=stock_point["open"],
                    high_price=stock_point["high"],
                    low_price=stock_point["low"],
                    close_price=stock_point["close"],
                    benchmark_close=reference_point["close"],
                )

        response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Seguimiento Desde Hoy")
        self.assertContains(response, "Cartera global")
        self.assertContains(response, "Ticket IBE")
        self.assertContains(response, "Ticket ENG")
        self.assertEqual(EquityTicketSnapshot.objects.count(), 2)

    def test_equities_page_renders_investment_journey_section(self):
        active = EquityPosition.objects.create(
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2024, 1, 15),
            shares=Decimal("20.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("12.0000"),
        )
        sold = EquityPosition.objects.create(
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Banco Sabadell",
            ticker="ACS",
            quote_symbol="ACS.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="ACS",
            opened_on=date(2023, 2, 1),
            shares=Decimal("15.0000"),
            average_cost_per_share=Decimal("20.0000"),
            current_price_per_share=Decimal("24.0000"),
        )
        for position, start_price in ((active, Decimal("10.00")), (sold, Decimal("20.00"))):
            stock_series = build_compound_market_series(
                position.quote_symbol,
                position.company_name,
                growth=Decimal("1.0100"),
                start_price=start_price,
            )
            reference_series = build_compound_market_series(
                "^IBEX",
                "IBEX 35",
                growth=Decimal("1.0060"),
                start_price=Decimal("100.0000"),
            )
            for stock_point, reference_point in zip(stock_series.points, reference_series.points):
                position.price_history.create(
                    price_date=stock_point["date"],
                    open_price=stock_point["open"],
                    high_price=stock_point["high"],
                    low_price=stock_point["low"],
                    close_price=stock_point["close"],
                    benchmark_close=reference_point["close"],
                )
        archive_equity_position_sale(
            sold,
            closed_on=date(2025, 9, 30),
            sale_price_per_share=Decimal("25.0000"),
            notes="Cierre completo",
        )

        response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cuadro de Gestion")
        self.assertContains(response, "Rentabilidad anual por ejercicio")
        self.assertContains(response, "Cuenta de resultados")
        self.assertContains(response, "Resultado neto de las posiciones cerradas")
        self.assertContains(response, "Margen neto por ticket abierto")

    def test_equities_page_surfaces_sale_simulator_and_unfollow_action(self):
        EquityPosition.objects.create(
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Banco Santander",
            trade_channel=EquityPosition.TradeChannel.APP,
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2025, 1, 10),
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("12.0000"),
            annual_dividend_income=Decimal("12.00"),
            annual_maintenance_cost=Decimal("5.00"),
        )
        EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Seguimiento",
            ticker="ACS",
            quote_symbol="ACS.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="ACS",
            shares=Decimal("0"),
            average_cost_per_share=Decimal("0"),
            current_price_per_share=Decimal("0"),
        )

        response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Si vendes esta posicion, aqui ves el neto real")
        self.assertContains(response, "Dejar de seguir y volver al radar")
        self.assertContains(response, "Si vendo hoy")
        self.assertContains(response, "Calcular / vender")

    @override_settings(EQUITIES_REFERENCE_WORKBOOK="")
    def test_equities_page_renders_workbook_reference_guide(self):
        workbook_path = build_test_reference_workbook()
        self.addCleanup(lambda: os.path.exists(workbook_path) and os.remove(workbook_path))
        load_ibex_reference_workbook_snapshot.cache_clear()

        with override_settings(EQUITIES_REFERENCE_WORKBOOK=workbook_path):
            EquityPosition.objects.create(
                ownership_category=AssetOwnershipCategory.JOINT,
                broker="Banco Sabadell",
                ticker="SAN",
                quote_symbol="SAN.MC",
                benchmark_symbol="^IBEX",
                benchmark_name="IBEX 35",
                company_name="Banco Santander",
                shares=Decimal("10.0000"),
                average_cost_per_share=Decimal("4.0000"),
                current_price_per_share=Decimal("5.0000"),
            )

            response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Guia completa IBEX del Excel")
        self.assertContains(response, "Euribor 12m (%)")

    @override_settings(EQUITIES_IBEX_UNIVERSE_ANALYSIS=True, EQUITIES_IBEX_UNIVERSE_LIMIT=1)
    def test_ibex_rows_open_detail_in_new_tab(self):
        acs = find_equity_company_profile("ACS")
        company = {
            "ticker": acs["ticker"],
            "company_name": acs["company_name"],
            "quote_symbol": acs["quote_symbol"],
            "sector": acs["sector_label"],
            "dividend_yield": Decimal("3.10"),
            "catalog_profile": acs,
        }
        empty_workbook = {
            "available": False,
            "path": "",
            "companies": [],
            "companies_by_key": {},
            "indicators_by_name": {},
            "indicators_by_key": {},
            "indicator_name_by_short": {},
            "sector_map": {},
        }

        def fake_market_series(symbol, range_key="10y", interval="1d"):
            return build_compound_market_series(symbol, symbol, growth=Decimal("1.0200"), start_price=Decimal("12.0000"))

        def fake_reference_series(reference_profile, benchmark_symbol="", benchmark_name=""):
            return build_compound_market_series(
                benchmark_symbol or "^IBEX",
                benchmark_name or "Referencia",
                growth=Decimal("1.0060"),
                start_price=Decimal("100.0000"),
            )

        with (
            patch("equities.services.load_ibex_reference_workbook_snapshot", return_value=empty_workbook),
            patch("equities.services.build_ibex_universe_companies", return_value=[company]),
            patch("equities.services.fetch_market_series", side_effect=fake_market_series),
            patch("equities.services.fetch_reference_series_for_choice", side_effect=fake_reference_series),
        ):
            response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("equities:ibex_detail", kwargs={"ticker": acs["ticker"]}))
        self.assertContains(response, 'target="_blank"', html=False)
        self.assertContains(response, "Abrir analisis completo")

    def test_can_open_ibex_detail_page_with_company_title_and_charts(self):
        acs = find_equity_company_profile("ACS")
        company = {
            "ticker": acs["ticker"],
            "company_name": acs["company_name"],
            "quote_symbol": acs["quote_symbol"],
            "sector": acs["sector_label"],
            "dividend_yield": Decimal("3.10"),
            "catalog_profile": acs,
        }
        empty_workbook = {
            "available": False,
            "path": "",
            "companies": [],
            "companies_by_key": {},
            "indicators_by_name": {},
            "indicators_by_key": {},
            "indicator_name_by_short": {},
            "sector_map": {},
        }

        def fake_market_series(symbol, range_key="10y", interval="1d"):
            return build_compound_market_series(symbol, symbol, growth=Decimal("1.0200"), start_price=Decimal("12.0000"))

        def fake_reference_series(reference_profile, benchmark_symbol="", benchmark_name=""):
            return build_compound_market_series(
                benchmark_symbol or "^IBEX",
                benchmark_name or "Referencia",
                growth=Decimal("1.0060"),
                start_price=Decimal("100.0000"),
            )

        with (
            patch("equities.services.load_ibex_reference_workbook_snapshot", return_value=empty_workbook),
            patch("equities.services.build_ibex_universe_companies", return_value=[company]),
            patch("equities.services.fetch_market_series", side_effect=fake_market_series),
            patch("equities.services.fetch_reference_series_for_choice", side_effect=fake_reference_series),
        ):
            response = self.client.get(reverse("equities:ibex_detail", kwargs={"ticker": acs["ticker"]}))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "equities/ibex_equity_detail.html")
        self.assertContains(response, f"<title>{acs['company_name']}</title>", html=False)
        self.assertContains(response, "Historico")
        self.assertContains(response, "Mejor correlacion")
        self.assertContains(response, "Prevision 12M")

    def test_can_store_same_ticker_as_owned_and_watchlist_without_collision(self):
        EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.OWNED,
            ownership_category=AssetOwnershipCategory.XIMO,
            broker="Interactive Brokers",
            ticker="SAN",
            quote_symbol="SAN.MC",
            reference_profile=EquityPosition.ReferenceProfile.EURIBOR_12M,
            benchmark_symbol="ECB:M.S0.N.C_EUR1Y.E",
            benchmark_name="Euribor 12M",
            company_name="Banco Santander",
            shares=Decimal("30.0000"),
            average_cost_per_share=Decimal("4.2000"),
            current_price_per_share=Decimal("4.7000"),
        )

        response = self.client.post(
            reverse("equities:list"),
            {
                "action": "create_position",
                "position_kind": EquityPosition.PositionKind.WATCHLIST,
                "ownership_category": AssetOwnershipCategory.XIMO,
                "broker": "Interactive Brokers",
                "ticker": "SAN",
                "company_name": "Banco Santander",
                "quote_symbol": "SAN.MC",
                "reference_profile": EquityPosition.ReferenceProfile.EURIBOR_12M,
                "benchmark_symbol": "",
                "benchmark_name": "",
                "shares": "0",
                "average_cost_per_share": "4,5000",
                "current_price_per_share": "4,7000",
                "annual_dividend_income": "0",
                "annual_maintenance_cost": "0",
                "notes": "Seguimiento",
            },
        )

        self.assertRedirects(response, reverse("equities:list"))
        self.assertEqual(
            EquityPosition.objects.filter(broker="Interactive Brokers", ticker="SAN", ownership_category=AssetOwnershipCategory.XIMO).count(),
            2,
        )
