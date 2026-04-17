import io
import json
import os
import tempfile
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from portfolio.ownership import AssetOwnershipCategory

from .broker_costs import estimate_broker_costs
from .llm_analysis import enrich_dashboard_with_ai_analysis
from .models import (
    EquityClosedPosition,
    EquityNightlyAnalysisRun,
    EquityNightlyAnalysisSnapshot,
    EquityOptimizationRun,
    EquityPosition,
    EquityPriceHistory,
    EquityPurchaseForecastBaseline,
    EquityTicketSnapshot,
)
from .nightly_analysis import (
    build_dashboard_from_nightly_cache,
    build_ibex_recommendation_date_map,
    capture_purchase_forecast_baseline,
    persist_nightly_analysis_dashboard,
    run_nightly_equity_analysis,
    serialize_cached_value,
)
from .optimization_runs import launch_equity_optimization_run, launch_equity_optimization_run_pair, process_equity_optimization_run
from .optimization_runs import (
    build_scheduled_optimization_persistence_context,
    launch_scheduled_equity_optimization_runs,
    purge_stale_scheduled_optimization_runs,
)
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
    build_equity_decision_rows,
    build_equity_history_cards,
    build_equity_investment_journey_context,
    build_equity_round_investment_plan,
    build_equity_sale_preview,
    build_equity_ticket_tracking_context,
    build_owned_cycle_trade_timing_plan,
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


def populate_position_history(
    position: EquityPosition,
    *,
    growth: Decimal = Decimal("1.0150"),
    benchmark_growth: Decimal = Decimal("1.0080"),
    months: int = 60,
):
    stock_series = build_compound_market_series(
        position.quote_symbol or f"{position.ticker}.MC",
        position.company_name,
        growth,
        months=months,
        start_year=2021,
        start_month=1,
        start_price=position.average_cost_per_share or Decimal("10.0000"),
    )
    benchmark_series = build_compound_market_series(
        position.benchmark_symbol or "^IBEX",
        position.benchmark_name or "IBEX 35",
        benchmark_growth,
        months=months,
        start_year=2021,
        start_month=1,
        start_price=Decimal("100.0000"),
    )
    for stock_point, benchmark_point in zip(stock_series.points, benchmark_series.points):
        EquityPriceHistory.objects.create(
            position=position,
            price_date=stock_point["date"],
            open_price=stock_point["open"],
            high_price=stock_point["high"],
            low_price=stock_point["low"],
            close_price=stock_point["close"],
            benchmark_close=benchmark_point["close"],
        )
    position.current_price_per_share = stock_series.latest_price
    position.latest_price_date = stock_series.latest_date
    position.save(update_fields=["current_price_per_share", "latest_price_date"])
    return stock_series


class FakeHTTPResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


@override_settings(EQUITIES_FETCH_FUNDAMENTALS=False)
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

    def test_projection_chart_uses_last_year_while_history_keeps_full_cycle(self):
        position = EquityPosition.objects.create(
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola, S.A.",
            shares=Decimal("20"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
        )
        stock_price = Decimal("10.00")
        benchmark_price = Decimal("100.00")
        for index in range(30):
            year = 2024 + (index // 12)
            month = (index % 12) + 1
            month_end = monthrange(year, month)[1]
            stock_price = (stock_price * Decimal("1.015")).quantize(Decimal("0.0001"))
            benchmark_price = (benchmark_price * Decimal("1.008")).quantize(Decimal("0.0001"))
            position.price_history.create(
                price_date=date(year, month, month_end),
                close_price=stock_price,
                benchmark_close=benchmark_price,
            )

        cards = build_equity_history_cards([position])

        chart = cards[0]["projection_12m_chart"]
        self.assertTrue(chart["available"])
        self.assertEqual(chart["history_window_label"], "Ultimo ano")
        self.assertEqual(chart["start_label"], "2025-06-30")
        self.assertEqual(cards[0]["historical_chart"]["start_label"], "2024-01-31")
        self.assertGreater(cards[0]["historical_chart"]["points_count"], chart["points_count"])

    def test_cycle_projection_5y_uses_last_five_years_on_chart_and_ten_years_for_model(self):
        position = EquityPosition.objects.create(
            broker="Interactive Brokers",
            ticker="REP",
            quote_symbol="REP.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Repsol, S.A.",
            shares=Decimal("25"),
            average_cost_per_share=Decimal("12.0000"),
            current_price_per_share=Decimal("12.0000"),
        )
        stock_price = Decimal("12.00")
        benchmark_price = Decimal("100.00")
        pattern = [Decimal("1.025"), Decimal("0.985"), Decimal("1.030"), Decimal("0.992")]
        benchmark_pattern = [Decimal("1.010"), Decimal("1.004"), Decimal("1.012"), Decimal("0.998")]
        for index in range(120):
            year = 2016 + (index // 12)
            month = (index % 12) + 1
            month_end = monthrange(year, month)[1]
            stock_price = (stock_price * pattern[index % len(pattern)]).quantize(Decimal("0.0001"))
            benchmark_price = (benchmark_price * benchmark_pattern[index % len(benchmark_pattern)]).quantize(Decimal("0.0001"))
            position.price_history.create(
                price_date=date(year, month, month_end),
                close_price=stock_price,
                benchmark_close=benchmark_price,
            )

        cards = build_equity_history_cards([position])

        cycle_projection = cards[0]["cycle_projection_5y"]
        cycle_chart = cards[0]["cycle_projection_5y_chart"]
        self.assertTrue(cycle_projection["available"])
        self.assertTrue(cycle_chart["available"])
        self.assertEqual(cycle_chart["history_window_label"], "Ultimos 5 anos")
        self.assertIn("10.0 anos", cycle_chart["model_window_label"])
        self.assertEqual(cycle_chart["start_label"], "2021-01-31")
        self.assertTrue(cycle_chart["projection_end_label"].startswith("2030-"))
        self.assertEqual(cycle_projection["path"][-1]["label"], "5A")
        self.assertIn("10.0 anos", cycle_projection["explanation"])
        self.assertTrue(
            any(
                current["projected_price"] < previous["projected_price"]
                for previous, current in zip(cycle_projection["path"], cycle_projection["path"][1:])
            )
        )

    def test_decision_rows_include_five_year_projection_and_yearly_margins(self):
        position = EquityPosition.objects.create(
            broker="Interactive Brokers",
            ticker="REP",
            quote_symbol="REP.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Repsol, S.A.",
            shares=Decimal("25"),
            average_cost_per_share=Decimal("100.0000"),
            current_price_per_share=Decimal("100.0000"),
        )

        row = build_equity_decision_rows(
            [
                {
                    "position": position,
                    "has_history": True,
                    "status_key": "ibex",
                    "status_label": "Solo radar",
                    "status_note": "",
                    "detail_anchor": "",
                    "reference_label": "IBEX 35",
                    "correlation": {"coefficient": Decimal("0.74")},
                    "projection": {
                        "available": True,
                        "base_return_pct": Decimal("12.50"),
                        "projected_price": Decimal("112.5000"),
                        "years_covered": Decimal("10.00"),
                        "safety_score": Decimal("68.00"),
                        "safety_label": "Alta",
                        "benefit_risk_ratio": Decimal("1.90"),
                        "cycle_phase": "Expansion",
                        "decision_score": Decimal("82.00"),
                    },
                    "projection_reliability": {"label": "Alta", "score": Decimal("79.00")},
                    "trade_alert": {
                        "label": "Comprar",
                        "tone": "buy",
                        "score": Decimal("75.00"),
                        "note": "Buena combinacion de retorno y seguridad.",
                        "trigger_label": "Retorno 12M y seguridad apoyan compra",
                    },
                    "coefficient_alert": {
                        "label": "Solido",
                        "tone": "good",
                        "trigger_label": "Coeficiente consistente",
                    },
                    "reference_playbook": {"best_candidate": None},
                    "cycle_projection_5y": {
                        "available": True,
                        "cycle_phase": "Expansion",
                        "five_year_return_pct": Decimal("61.0510"),
                        "path": [
                            {"label": "6M", "projected_price": Decimal("104.8809")},
                            {"label": "1A", "projected_price": Decimal("110.0000")},
                            {"label": "18M", "projected_price": Decimal("115.3687")},
                            {"label": "2A", "projected_price": Decimal("121.0000")},
                            {"label": "30M", "projected_price": Decimal("126.9016")},
                            {"label": "3A", "projected_price": Decimal("133.1000")},
                            {"label": "42M", "projected_price": Decimal("139.5968")},
                            {"label": "4A", "projected_price": Decimal("146.4100")},
                            {"label": "54M", "projected_price": Decimal("153.5574")},
                            {"label": "5A", "projected_price": Decimal("161.0510")},
                        ],
                    },
                }
            ]
        )[0]

        self.assertEqual(row["cycle_return_5y_pct"], Decimal("61.05"))
        self.assertEqual(row["cycle_return_annual_pct"], Decimal("10.00"))
        self.assertEqual(
            [item["label"] for item in row["cycle_yearly_margins"]],
            ["AÑO 1", "AÑO 2", "AÑO 3", "AÑO 4", "AÑO 5"],
        )
        self.assertEqual(
            [item["margin_pct"] for item in row["cycle_yearly_margins"]],
            [Decimal("12.50"), Decimal("7.56"), Decimal("10.00"), Decimal("10.00"), Decimal("10.00")],
        )
        self.assertEqual(row["cycle_yearly_margins"][0]["margin_pct"], row["projected_return_pct"])

    def test_capture_purchase_forecast_baseline_uses_latest_nightly_snapshot_before_purchase(self):
        position = EquityPosition.objects.create(
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola, S.A.",
            opened_on=date(2026, 4, 17),
            shares=Decimal("20"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.4000"),
        )
        run = EquityNightlyAnalysisRun.objects.create(
            analysis_date=date(2026, 4, 17),
            status=EquityNightlyAnalysisRun.Status.COMPLETED,
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        cached_position = EquityPosition(
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola, S.A.",
            shares=Decimal("0"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
        )
        card = {
            "position": cached_position,
            "reference_label": "IBEX 35",
            "projection": {
                "available": True,
                "projected_price": Decimal("11.2500"),
                "base_return_pct": Decimal("12.50"),
                "safety_score": Decimal("68.00"),
            },
            "projection_reliability": {"label": "Alta", "score": Decimal("79.00")},
            "trade_alert": {"label": "Comprar"},
            "cycle_projection_5y": {
                "available": True,
                "path": [
                    {"label": "1A", "projected_price": Decimal("11.0000")},
                    {"label": "2A", "projected_price": Decimal("12.3750")},
                    {"label": "3A", "projected_price": Decimal("13.6125")},
                    {"label": "4A", "projected_price": Decimal("14.9738")},
                    {"label": "5A", "projected_price": Decimal("16.4712")},
                ],
            },
        }
        EquityNightlyAnalysisSnapshot.objects.create(
            run=run,
            analysis_date=run.analysis_date,
            scope=EquityNightlyAnalysisSnapshot.Scope.IBEX,
            analysis_key="ibex:IBE",
            ticker="IBE",
            quote_symbol="IBE.MC",
            company_name="Iberdrola, S.A.",
            status_key="ibex",
            sector_label="Utilities",
            agent_provider="core",
            analysis_payload=serialize_cached_value(card),
        )

        baseline = capture_purchase_forecast_baseline(position)

        self.assertIsNotNone(baseline)
        self.assertEqual(EquityPurchaseForecastBaseline.objects.count(), 1)
        self.assertEqual(baseline.source_analysis_date, date(2026, 4, 17))
        self.assertEqual(baseline.baseline_date, date(2026, 4, 17))
        self.assertEqual(baseline.trade_alert_label, "Comprar")
        self.assertEqual(baseline.projected_price_1y, Decimal("11.2500"))
        self.assertEqual(baseline.projected_return_pct_1y, Decimal("12.50"))
        self.assertEqual(baseline.projected_price_2y, Decimal("12.3750"))
        self.assertEqual(baseline.projected_return_pct_2y, Decimal("23.75"))

    def test_capture_purchase_forecast_baseline_falls_back_to_first_available_run_after_purchase(self):
        position = EquityPosition.objects.create(
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola, S.A.",
            opened_on=date(2026, 4, 15),
            shares=Decimal("20"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.4000"),
        )
        run = EquityNightlyAnalysisRun.objects.create(
            analysis_date=date(2026, 4, 17),
            status=EquityNightlyAnalysisRun.Status.COMPLETED,
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        cached_position = EquityPosition(
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola, S.A.",
            shares=Decimal("0"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
        )
        card = {
            "position": cached_position,
            "reference_label": "IBEX 35",
            "projection": {
                "available": True,
                "projected_price": Decimal("11.2500"),
                "base_return_pct": Decimal("12.50"),
                "safety_score": Decimal("68.00"),
            },
            "projection_reliability": {"label": "Alta", "score": Decimal("79.00")},
            "trade_alert": {"label": "Comprar"},
            "cycle_projection_5y": {
                "available": True,
                "path": [
                    {"label": "1A", "projected_price": Decimal("11.0000")},
                    {"label": "2A", "projected_price": Decimal("12.3750")},
                    {"label": "3A", "projected_price": Decimal("13.6125")},
                    {"label": "4A", "projected_price": Decimal("14.9738")},
                    {"label": "5A", "projected_price": Decimal("16.4712")},
                ],
            },
        }
        EquityNightlyAnalysisSnapshot.objects.create(
            run=run,
            analysis_date=run.analysis_date,
            scope=EquityNightlyAnalysisSnapshot.Scope.IBEX,
            analysis_key="ibex:IBE",
            ticker="IBE",
            quote_symbol="IBE.MC",
            company_name="Iberdrola, S.A.",
            status_key="ibex",
            sector_label="Utilities",
            agent_provider="core",
            analysis_payload=serialize_cached_value(card),
        )

        baseline = capture_purchase_forecast_baseline(position)

        self.assertIsNotNone(baseline)
        self.assertEqual(baseline.source_analysis_date, date(2026, 4, 17))
        self.assertEqual(baseline.baseline_date, date(2026, 4, 17))

    def test_build_ibex_recommendation_date_map_tracks_last_buy_and_sell_starts(self):
        for analysis_day, label in (
            (date(2026, 4, 14), "Vigilar"),
            (date(2026, 4, 15), "Comprar"),
            (date(2026, 4, 16), "Comprar"),
            (date(2026, 4, 17), "Vender"),
            (date(2026, 4, 18), "Vender"),
        ):
            run = EquityNightlyAnalysisRun.objects.create(
                analysis_date=analysis_day,
                status=EquityNightlyAnalysisRun.Status.COMPLETED,
                started_at=timezone.now(),
                completed_at=timezone.now(),
            )
            EquityNightlyAnalysisSnapshot.objects.create(
                run=run,
                analysis_date=analysis_day,
                scope=EquityNightlyAnalysisSnapshot.Scope.IBEX,
                analysis_key=f"ibex:ACS:{analysis_day.isoformat()}",
                ticker="ACS",
                quote_symbol="ACS.MC",
                company_name="ACS",
                status_key="ibex",
                sector_label="Construccion",
                agent_provider="core",
                analysis_payload=serialize_cached_value({"trade_alert": {"label": label}}),
            )

        recommendation_dates = build_ibex_recommendation_date_map(["ACS"])

        self.assertEqual(recommendation_dates["ACS"]["buy_recommended_on"], date(2026, 4, 15))
        self.assertEqual(recommendation_dates["ACS"]["sell_recommended_on"], date(2026, 4, 17))

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

    def test_history_cards_include_fundamentals_summary_when_enabled(self):
        position = EquityPosition.objects.create(
            broker="Interactive Brokers",
            ticker="SAN",
            quote_symbol="SAN.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Banco Santander, S.A.",
            shares=Decimal("25"),
            average_cost_per_share=Decimal("4.0000"),
            current_price_per_share=Decimal("5.0000"),
        )
        for price_date, close_price, benchmark_close in (
            (date(2025, 12, 31), Decimal("4.00"), Decimal("100.00")),
            (date(2026, 1, 31), Decimal("4.20"), Decimal("101.20")),
            (date(2026, 2, 28), Decimal("4.35"), Decimal("102.40")),
            (date(2026, 3, 31), Decimal("4.55"), Decimal("104.10")),
            (date(2026, 4, 30), Decimal("4.80"), Decimal("105.30")),
            (date(2026, 5, 31), Decimal("5.00"), Decimal("107.00")),
        ):
            position.price_history.create(
                price_date=price_date,
                close_price=close_price,
                benchmark_close=benchmark_close,
            )

        fundamentals_snapshot = {
            "symbol": "SAN.MC",
            "available": True,
            "currency_code": "EUR",
            "net_income_rows": [
                {
                    "as_of_date": date(2023, 12, 31),
                    "period_type": "12M",
                    "currency_code": "EUR",
                    "value": Decimal("11076000000"),
                },
                {
                    "as_of_date": date(2024, 12, 31),
                    "period_type": "12M",
                    "currency_code": "EUR",
                    "value": Decimal("12574000000"),
                },
                {
                    "as_of_date": date(2025, 12, 31),
                    "period_type": "12M",
                    "currency_code": "EUR",
                    "value": Decimal("14101000000"),
                },
            ],
            "market_cap_rows": [
                {
                    "as_of_date": date(2026, 4, 10),
                    "period_type": "TTM",
                    "currency_code": "EUR",
                    "value": Decimal("151560161340"),
                }
            ],
            "market_cap": Decimal("151560161340"),
            "market_cap_as_of_date": date(2026, 4, 10),
        }

        with patch("equities.services.fetch_equity_fundamentals", return_value=fundamentals_snapshot):
            cards = build_equity_history_cards([position], include_fundamentals=True)

        fundamentals = cards[0]["fundamentals"]
        self.assertTrue(fundamentals["available"])
        self.assertEqual(fundamentals["trend_label"], "Mejora")
        self.assertEqual(fundamentals["market_cap_label"], "151.6 mM EUR")
        self.assertEqual(fundamentals["net_income_rows"][0]["year_label"], "2025")
        self.assertEqual(fundamentals["net_income_rows"][-1]["year_label"], "2023")

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

    def test_history_cards_flag_sell_when_reference_coefficient_breaks_down_for_months(self):
        position = EquityPosition.objects.create(
            broker="Interactive Brokers",
            ticker="REP",
            quote_symbol="REP.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Repsol, S.A.",
            shares=Decimal("30"),
            average_cost_per_share=Decimal("12.0000"),
            current_price_per_share=Decimal("12.0000"),
        )

        benchmark_price = Decimal("100.0000")
        stock_price = Decimal("12.0000")
        benchmark_return_pattern = [
            Decimal("0.0060"),
            Decimal("0.0120"),
            Decimal("0.0080"),
            Decimal("0.0150"),
            Decimal("0.0070"),
            Decimal("0.0110"),
        ]
        for index in range(36):
            year = 2023 + (index // 12)
            month = (index % 12) + 1
            month_end = monthrange(year, month)[1]
            benchmark_return = benchmark_return_pattern[index % len(benchmark_return_pattern)]
            if index < 24:
                stock_return = (benchmark_return * Decimal("1.30")) + Decimal("0.0010")
            else:
                stock_return = -(benchmark_return * Decimal("1.10"))
            benchmark_price = (benchmark_price * (Decimal("1.00") + benchmark_return)).quantize(Decimal("0.0001"))
            stock_price = (stock_price * (Decimal("1.00") + stock_return)).quantize(Decimal("0.0001"))
            position.price_history.create(
                price_date=date(year, month, month_end),
                close_price=stock_price,
                benchmark_close=benchmark_price,
            )

        position.current_price_per_share = stock_price
        position.save(update_fields=["current_price_per_share"])

        cards = build_equity_history_cards([position])

        coefficient_alert = cards[0]["coefficient_alert"]
        self.assertTrue(coefficient_alert["available"])
        self.assertEqual(coefficient_alert["label"], "Vender")
        self.assertEqual(coefficient_alert["tone"], "sell")
        self.assertGreaterEqual(coefficient_alert["deterioration_streak"], 3)
        self.assertIn("Reciente", coefficient_alert["trigger_label"])

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

    def test_dashboard_overview_includes_first_owned_sale_recommendation(self):
        iberdrola = EquityPosition.objects.create(
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
        enagas = EquityPosition.objects.create(
            broker="Interactive Brokers",
            ticker="ENG",
            quote_symbol="ENG.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Enagas",
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("14.0000"),
            current_price_per_share=Decimal("15.0000"),
        )
        populate_position_history(iberdrola, growth=Decimal("1.0150"), benchmark_growth=Decimal("1.0060"), months=36)
        populate_position_history(enagas, growth=Decimal("1.0140"), benchmark_growth=Decimal("1.0060"), months=36)
        plan_payloads = {
            "IBE": {
                "available": True,
                "mode": "sale_reentry",
                "sale_month_number": 12,
                "sale_date": date(2027, 4, 17),
                "sale_window_label": "abril 2027 (mes 12)",
                "signal_value_pct": Decimal("-0.13"),
                "summary": "Salida tactica en abril 2027.",
            },
            "ENG": {
                "available": True,
                "mode": "sale_review",
                "sale_month_number": 9,
                "sale_date": date(2027, 1, 17),
                "sale_window_label": "enero 2027 (mes 9)",
                "signal_value_pct": Decimal("-0.22"),
                "summary": "Salida tactica en enero 2027.",
            },
        }

        with patch(
            "equities.services.build_owned_cycle_trade_timing_plan",
            side_effect=lambda card: plan_payloads[card["position"].ticker],
        ):
            dashboard = build_equity_analysis_dashboard(
                list(EquityPosition.objects.prefetch_related("price_history"))
            )

        recommendation = dashboard["overview"]["next_sale_recommendation"]
        self.assertTrue(recommendation["available"])
        self.assertEqual(recommendation["ticker"], "ENG")
        self.assertEqual(recommendation["company_name"], "Enagas")
        self.assertEqual(recommendation["sale_window_label"], "enero 2027 (mes 9)")

    def test_dashboard_builds_comparable_return_summary_for_owned_positions(self):
        owned = EquityPosition.objects.create(
            broker="Interactive Brokers",
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
        owned.price_history.create(price_date=date(2025, 4, 12), close_price=Decimal("10.0000"), benchmark_close=Decimal("100.0000"))
        owned.price_history.create(price_date=date(2026, 4, 12), close_price=Decimal("12.5000"), benchmark_close=Decimal("110.0000"))

        dashboard = build_equity_analysis_dashboard([owned])

        comparable_summary = dashboard["overview"]["comparable_summary"]
        self.assertTrue(comparable_summary["available"])
        self.assertEqual(comparable_summary["positions_count"], 1)
        self.assertEqual(comparable_summary["best_ticker"], "IBE")
        self.assertIsNotNone(comparable_summary["weighted_monthly_return_pct"])
        self.assertIsNotNone(comparable_summary["weighted_annual_return_pct"])

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
        benchmark_series = build_compound_market_series(
            "^IBEX",
            "IBEX 35",
            growth=Decimal("1.0060"),
            start_price=Decimal("100.0000"),
        )
        with patch("equities.services.fetch_reference_series_for_choice", return_value=benchmark_series):
            tracking = build_equity_ticket_tracking_context(second_cards)

        self.assertEqual(EquityTicketSnapshot.objects.count(), 4)
        self.assertTrue(tracking["available"])
        self.assertEqual(tracking["tracked_ticket_count"], 2)
        self.assertTrue(tracking["global"]["available"])
        self.assertTrue(tracking["global"]["chart"]["available"])
        self.assertTrue(tracking["global"]["benchmark"]["available"])
        self.assertTrue(tracking["global"]["chart"]["benchmark_line"])
        self.assertEqual(tracking["snapshot_days_count"], 2)
        self.assertEqual(len(tracking["tickets"]), 2)
        self.assertTrue(all(item["chart"]["available"] for item in tracking["tickets"]))
        self.assertIsNotNone(tracking["global"]["expected_today_value"])
        self.assertTrue(tracking["global"]["chart"]["x_markers"])
        self.assertEqual(tracking["global"]["net_gain_value"], Decimal("16.00"))
        self.assertEqual(tracking["global"]["invested_return_pct"], Decimal("2.91"))
        self.assertEqual(tracking["global"]["annualized_return_pct"], Decimal("3512395.03"))

    def test_ticket_tracking_uses_first_common_snapshot_date_as_shared_base(self):
        def create_position(ticker, company_name, start_price):
            position = EquityPosition.objects.create(
                broker="Interactive Brokers",
                ticker=ticker,
                quote_symbol=f"{ticker}.MC",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_symbol="^IBEX",
                benchmark_name="IBEX 35",
                company_name=company_name,
                shares=Decimal("20.0000"),
                average_cost_per_share=start_price,
                current_price_per_share=(start_price * Decimal("1.1000")).quantize(Decimal("0.0001")),
                annual_dividend_income=Decimal("20.00"),
            )
            stock_series = build_compound_market_series(
                f"{ticker}.MC",
                company_name,
                growth=Decimal("1.0150"),
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
            return position

        first_positions = [
            create_position("IBE", "Iberdrola", Decimal("10.0000")),
            create_position("ENG", "Enagas", Decimal("14.0000")),
        ]
        first_cards = build_equity_history_cards(first_positions)
        capture_equity_ticket_snapshots(first_cards, snapshot_date=date(2026, 4, 12))

        create_position("SCYR", "Sacyr", Decimal("4.0000"))
        second_cards = build_equity_history_cards(list(EquityPosition.objects.prefetch_related("price_history")))
        capture_equity_ticket_snapshots(second_cards, snapshot_date=date(2026, 4, 13))

        benchmark_series = build_compound_market_series(
            "^IBEX",
            "IBEX 35",
            growth=Decimal("1.0060"),
            start_price=Decimal("100.0000"),
        )
        with patch("equities.services.fetch_reference_series_for_choice", return_value=benchmark_series):
            tracking = build_equity_ticket_tracking_context(second_cards)

        self.assertEqual(tracking["anchor_date"], date(2026, 4, 13))
        self.assertEqual(tracking["snapshot_days_count"], 1)
        self.assertEqual(tracking["tracked_ticket_count"], 3)
        self.assertTrue(all(item["baseline_snapshot"].snapshot_date == date(2026, 4, 13) for item in tracking["tickets"]))
        self.assertEqual(tracking["global"]["chart"]["start_label"], "2026-04-13")

    def test_build_owned_cycle_trade_timing_plan_detects_monthly_trend_turns(self):
        position = EquityPosition(
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2023, 1, 31),
            shares=Decimal("20.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("100.0000"),
            latest_price_date=date(2026, 4, 17),
        )
        card = {
            "position": position,
            "cycle_projection_5y": {
                "available": True,
                "path": [
                    {"label": "6M", "projected_price": Decimal("118.0000")},
                    {"label": "12M", "projected_price": Decimal("126.0000")},
                    {"label": "18M", "projected_price": Decimal("112.0000")},
                    {"label": "24M", "projected_price": Decimal("96.0000")},
                    {"label": "30M", "projected_price": Decimal("101.0000")},
                    {"label": "36M", "projected_price": Decimal("118.0000")},
                    {"label": "42M", "projected_price": Decimal("132.0000")},
                    {"label": "48M", "projected_price": Decimal("145.0000")},
                    {"label": "54M", "projected_price": Decimal("156.0000")},
                    {"label": "60M", "projected_price": Decimal("168.0000")},
                ],
            },
        }

        plan = build_owned_cycle_trade_timing_plan(card)

        self.assertTrue(plan["available"])
        self.assertEqual(plan["mode"], "sale_reentry")
        self.assertEqual(plan["sale_month_number"], 12)
        self.assertEqual(plan["sale_window_label"], "abril 2027 (mes 12)")
        self.assertEqual(plan["reentry_month_number"], 26)
        self.assertEqual(plan["reentry_window_label"], "junio 2028 (mes 26)")
        self.assertEqual(plan["signal_value_pct"], Decimal("-0.13"))
        self.assertEqual(plan["pre_sale_return_pct"], Decimal("26.00"))
        self.assertIn("pendiente desestacionalizada de 5 meses", plan["summary"].lower())

    def test_ticket_tracking_includes_sale_and_reentry_plan_from_current_cycle_trend(self):
        position = EquityPosition.objects.create(
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2023, 1, 31),
            shares=Decimal("20.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("12.0000"),
            annual_dividend_income=Decimal("20.00"),
        )
        populate_position_history(position, growth=Decimal("1.0150"), benchmark_growth=Decimal("1.0060"), months=36)
        alternative = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            broker="Seguimiento",
            ticker="ELE",
            quote_symbol="ELE.MC",
            reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Endesa",
            shares=Decimal("0.0000"),
            average_cost_per_share=Decimal("18.0000"),
            current_price_per_share=Decimal("18.0000"),
        )
        populate_position_history(alternative, growth=Decimal("1.0220"), benchmark_growth=Decimal("1.0060"), months=36)

        cards = build_equity_history_cards([position, alternative])
        capture_equity_ticket_snapshots(cards, snapshot_date=date(2026, 4, 16))
        position.current_price_per_share = Decimal("12.3000")
        position.save(update_fields=["current_price_per_share", "updated_at"])
        refreshed_cards = build_equity_history_cards(list(EquityPosition.objects.prefetch_related("price_history")))
        capture_equity_ticket_snapshots(refreshed_cards, snapshot_date=date(2026, 4, 17))

        EquityPurchaseForecastBaseline.objects.create(
            position=position,
            source_analysis_date=date(2026, 4, 16),
            baseline_date=date(2026, 4, 16),
            reference_label="IBEX 35",
            trade_alert_label="Comprar",
            reliability_label="Alta",
            baseline_price=Decimal("10.0000"),
            projected_price_1y=Decimal("11.2000"),
            projected_price_2y=Decimal("9.9000"),
            projected_price_3y=Decimal("11.7000"),
            projected_price_4y=Decimal("12.4000"),
            projected_price_5y=Decimal("13.2000"),
            projected_return_pct_1y=Decimal("12.00"),
            projected_return_pct_2y=Decimal("-1.00"),
            projected_return_pct_3y=Decimal("17.00"),
            projected_return_pct_4y=Decimal("24.00"),
            projected_return_pct_5y=Decimal("32.00"),
        )

        benchmark_series = build_compound_market_series(
            "^IBEX",
            "IBEX 35",
            growth=Decimal("1.0060"),
            start_price=Decimal("100.0000"),
        )
        trade_plan_payload = {
            "available": True,
            "mode": "sale_reentry",
            "analysis_basis_label": "Pendiente 5M desestacionalizada sobre la senda 5A vigente",
            "sale_month_number": 12,
            "sale_year_number": 1,
            "sale_window_label": "abril 2027 (mes 12)",
            "sale_date": date(2027, 4, 17),
            "sale_date_label": "2027-04-17",
            "reentry_month_number": 26,
            "reentry_year_number": 3,
            "reentry_window_label": "junio 2028 (mes 26)",
            "reentry_date": date(2028, 6, 17),
            "reentry_date_label": "2028-06-17",
            "summary": "La pendiente desestacionalizada de 5 meses gira a negativo y luego vuelve a positivo.",
            "signal_label": "Pendiente 5M negativa",
            "signal_value_pct": Decimal("-0.13"),
            "monthly_rows": [],
            "yearly_rows": [],
            "drawdown_month_number": 12,
            "drawdown_year_number": 1,
            "drawdown_margin_pct": Decimal("-0.13"),
            "pre_sale_return_pct": Decimal("8.00"),
        }
        with (
            patch("equities.services.fetch_reference_series_for_choice", return_value=benchmark_series),
            patch("equities.services.build_owned_cycle_trade_timing_plan", return_value=trade_plan_payload),
        ):
            tracking = build_equity_ticket_tracking_context(refreshed_cards, optimizer_cards=refreshed_cards)

        trade_plan = tracking["tickets"][0]["trade_plan"]
        self.assertTrue(trade_plan["available"])
        self.assertEqual(trade_plan["mode"], "sale_reentry")
        self.assertEqual(trade_plan["sale_month_number"], 12)
        self.assertEqual(trade_plan["sale_window_label"], "abril 2027 (mes 12)")
        self.assertEqual(trade_plan["reentry_month_number"], 26)
        self.assertEqual(trade_plan["reentry_window_label"], "junio 2028 (mes 26)")
        self.assertEqual(trade_plan["drawdown_month_number"], 12)
        self.assertEqual(trade_plan["drawdown_margin_pct"], Decimal("-0.13"))
        self.assertEqual(tracking["tickets"][0]["rotation_plan"]["action"], "rotar")
        self.assertEqual(tracking["tickets"][0]["rotation_plan"]["alternative_ticker"], "ELE")

    def test_round_investment_plan_respects_existing_weights_and_review_dates(self):
        owned = {
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.OWNED,
                broker="Interactive Brokers",
                ticker="IBE",
                quote_symbol="IBE.MC",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_symbol="^IBEX",
                benchmark_name="IBEX 35",
                company_name="Iberdrola",
                shares=Decimal("1000.0000"),
                average_cost_per_share=Decimal("20.0000"),
                current_price_per_share=Decimal("22.0000"),
            ),
            "status_key": "owned",
            "status_label": "Comprada",
            "sector_label": "Electrica",
            "reference_label": "IBEX 35",
            "trade_alert": {"label": "Comprar", "tone": "buy", "note": ""},
            "projection_reliability": {"label": "Alta", "score": Decimal("82.00")},
            "projection": {
                "available": True,
                "base_return_pct": Decimal("20.00"),
                "price_return_pct": Decimal("16.00"),
                "price_low_return_pct": Decimal("-8.00"),
                "price_high_return_pct": Decimal("28.00"),
                "projected_price": Decimal("26.4000"),
                "confidence_label": "Alta",
                "safety_score": Decimal("76.00"),
                "gross_dividend_yield_pct": Decimal("3.20"),
                "net_income_yield_pct": Decimal("2.80"),
                "transaction_drag_pct": Decimal("0.20"),
                "annualized_volatility_pct": Decimal("12.00"),
                "positive_year_ratio_pct": Decimal("70.00"),
                "years_covered": Decimal("10.00"),
                "cycle_phase": "Expansion",
                "current_drawdown_pct": Decimal("-2.00"),
                "max_drawdown_pct": Decimal("-18.00"),
            },
            "cycle_projection_5y": {"available": True, "annual_return_pct": Decimal("6.00"), "five_year_return_pct": Decimal("34.00")},
        }
        endesa = {
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                broker="Seguimiento",
                ticker="ELE",
                quote_symbol="ELE.MC",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_symbol="^IBEX",
                benchmark_name="IBEX 35",
                company_name="Endesa",
                shares=Decimal("0.0000"),
                average_cost_per_share=Decimal("18.0000"),
                current_price_per_share=Decimal("18.0000"),
            ),
            "status_key": "watchlist",
            "status_label": "Seguimiento",
            "sector_label": "Electrica",
            "reference_label": "IBEX 35",
            "trade_alert": {"label": "Comprar", "tone": "buy", "note": ""},
            "projection_reliability": {"label": "Alta", "score": Decimal("84.00")},
            "projection": {
                "available": True,
                "base_return_pct": Decimal("18.00"),
                "price_return_pct": Decimal("14.50"),
                "price_low_return_pct": Decimal("-6.00"),
                "price_high_return_pct": Decimal("24.00"),
                "projected_price": Decimal("20.6100"),
                "confidence_label": "Alta",
                "safety_score": Decimal("75.00"),
                "gross_dividend_yield_pct": Decimal("3.50"),
                "net_income_yield_pct": Decimal("3.00"),
                "transaction_drag_pct": Decimal("0.10"),
                "annualized_volatility_pct": Decimal("11.50"),
                "positive_year_ratio_pct": Decimal("71.00"),
                "years_covered": Decimal("10.00"),
                "cycle_phase": "Expansion",
                "current_drawdown_pct": Decimal("-2.00"),
                "max_drawdown_pct": Decimal("-17.00"),
            },
            "cycle_projection_5y": {"available": True, "annual_return_pct": Decimal("5.80"), "five_year_return_pct": Decimal("32.00")},
        }
        indra = {
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                broker="Seguimiento",
                ticker="IDR",
                quote_symbol="IDR.MC",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_symbol="^IBEX",
                benchmark_name="IBEX 35",
                company_name="Indra",
                shares=Decimal("0.0000"),
                average_cost_per_share=Decimal("18.0000"),
                current_price_per_share=Decimal("18.0000"),
            ),
            "status_key": "watchlist",
            "status_label": "Seguimiento",
            "sector_label": "Tecnologia y defensa",
            "reference_label": "IBEX 35",
            "trade_alert": {"label": "Comprar", "tone": "buy", "note": ""},
            "projection_reliability": {"label": "Alta", "score": Decimal("86.00")},
            "projection": {
                "available": True,
                "base_return_pct": Decimal("24.00"),
                "price_return_pct": Decimal("21.50"),
                "price_low_return_pct": Decimal("-7.00"),
                "price_high_return_pct": Decimal("34.00"),
                "projected_price": Decimal("22.3200"),
                "confidence_label": "Alta",
                "safety_score": Decimal("78.00"),
                "gross_dividend_yield_pct": Decimal("1.20"),
                "net_income_yield_pct": Decimal("1.00"),
                "transaction_drag_pct": Decimal("0.10"),
                "annualized_volatility_pct": Decimal("16.00"),
                "positive_year_ratio_pct": Decimal("73.00"),
                "years_covered": Decimal("10.00"),
                "cycle_phase": "Expansion",
                "current_drawdown_pct": Decimal("-4.00"),
                "max_drawdown_pct": Decimal("-22.00"),
            },
            "cycle_projection_5y": {"available": True, "annual_return_pct": Decimal("7.00"), "five_year_return_pct": Decimal("41.00")},
        }

        plan = build_equity_round_investment_plan(
            [owned],
            [owned, endesa, indra],
            Decimal("70000"),
            Decimal("10000"),
            Decimal("30"),
            as_of=date(2026, 4, 17),
        )

        self.assertTrue(plan["available"])
        self.assertEqual(plan["current_overweights_count"], 1)
        self.assertEqual(plan["rounds"][0]["round_date_label"], "2026-04-17")
        self.assertEqual(plan["rounds"][1]["round_date_label"], "2026-04-21")
        self.assertTrue(all(item["amount"] <= Decimal("10000") for item in plan["rounds"]))
        self.assertTrue(all(item["post_weight_pct"] <= Decimal("30.00") for item in plan["rounds"]))
        self.assertTrue(all(item["ticker"] != "IBE" for item in plan["rounds"]))

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
        self.assertIsNotNone(context["monthly_equivalent_return_pct"])
        self.assertIsNotNone(context["active_tickets"][0]["monthly_equivalent_return_pct"])
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
        self.assertGreater(preview["monthly_equivalent_return_pct"], Decimal("0.00"))
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

    def test_allocation_plan_uses_five_year_cycle_as_secondary_signal(self):
        near_term_only = {
            "position": EquityPosition(
                position_kind=EquityPosition.PositionKind.WATCHLIST,
                ticker="LOG",
                company_name="Logista",
                reference_profile=EquityPosition.ReferenceProfile.MARKET_INDEX,
                benchmark_name="IBEX 35",
                benchmark_symbol="^IBEX",
                shares=Decimal("0"),
                average_cost_per_share=Decimal("0"),
                current_price_per_share=Decimal("26"),
            ),
            "sector_label": "Distribucion",
            "reference_label": "IBEX 35",
            "trade_alert": {"label": "Vigilar", "tone": "watch", "note": ""},
            "projection": {
                "available": True,
                "base_return_pct": Decimal("14.40"),
                "projected_price": Decimal("29.74"),
                "confidence_label": "Alta",
                "coefficient": Decimal("0.48"),
                "safety_score": Decimal("74.00"),
                "net_income_yield_pct": Decimal("2.10"),
                "gross_dividend_yield_pct": Decimal("2.90"),
                "transaction_drag_pct": Decimal("0.20"),
                "annualized_volatility_pct": Decimal("13.00"),
                "positive_year_ratio_pct": Decimal("67.00"),
                "years_covered": Decimal("10.00"),
                "cycle_phase": "Expansion",
                "current_drawdown_pct": Decimal("-2.00"),
                "max_drawdown_pct": Decimal("-19.00"),
            },
            "projection_reliability": {"label": "Alta", "score": Decimal("82.00")},
            "cycle_projection_5y": {
                "available": True,
                "annual_return_pct": Decimal("-4.50"),
                "five_year_return_pct": Decimal("-20.00"),
            },
        }
        balanced_cycle = {
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
            "sector_label": "Energia",
            "reference_label": "Demanda electrica Espana",
            "trade_alert": {"label": "Vigilar", "tone": "watch", "note": ""},
            "projection": {
                "available": True,
                "base_return_pct": Decimal("13.90"),
                "projected_price": Decimal("15.95"),
                "confidence_label": "Alta",
                "coefficient": Decimal("0.52"),
                "safety_score": Decimal("75.00"),
                "net_income_yield_pct": Decimal("2.20"),
                "gross_dividend_yield_pct": Decimal("3.10"),
                "transaction_drag_pct": Decimal("0.20"),
                "annualized_volatility_pct": Decimal("12.50"),
                "positive_year_ratio_pct": Decimal("69.00"),
                "years_covered": Decimal("10.00"),
                "cycle_phase": "Expansion",
                "current_drawdown_pct": Decimal("-2.00"),
                "max_drawdown_pct": Decimal("-18.00"),
            },
            "projection_reliability": {"label": "Alta", "score": Decimal("82.00")},
            "cycle_projection_5y": {
                "available": True,
                "annual_return_pct": Decimal("6.20"),
                "five_year_return_pct": Decimal("35.00"),
            },
        }

        plan = build_equity_allocation_plan([near_term_only, balanced_cycle], Decimal("100000"), Decimal("60"))

        self.assertTrue(plan["available"])
        self.assertEqual(plan["allocations"][0]["position"].ticker, "IBE")
        self.assertGreater(plan["allocations"][0]["cycle_support_score"], Decimal("0"))
        self.assertGreater(plan["weighted_cycle_return_annual_pct"], Decimal("0"))

    def test_allocation_plan_can_prioritize_five_year_cycle(self):
        short_term_better = {
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
            "reference_label": "IBEX 35",
            "trade_alert": {"label": "Vigilar", "tone": "watch", "note": ""},
            "projection": {
                "available": True,
                "base_return_pct": Decimal("18.50"),
                "projected_price": Decimal("16.59"),
                "confidence_label": "Alta",
                "coefficient": Decimal("0.50"),
                "safety_score": Decimal("68.00"),
                "net_income_yield_pct": Decimal("1.80"),
                "gross_dividend_yield_pct": Decimal("2.80"),
                "transaction_drag_pct": Decimal("0.30"),
                "annualized_volatility_pct": Decimal("18.00"),
                "positive_year_ratio_pct": Decimal("58.00"),
                "years_covered": Decimal("10.00"),
                "cycle_phase": "Transicion",
                "current_drawdown_pct": Decimal("-5.00"),
                "max_drawdown_pct": Decimal("-28.00"),
            },
            "projection_reliability": {"label": "Alta", "score": Decimal("80.00")},
            "cycle_projection_5y": {
                "available": True,
                "annual_return_pct": Decimal("-2.20"),
                "five_year_return_pct": Decimal("-10.50"),
            },
        }
        long_term_better = {
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
            "trade_alert": {"label": "Vigilar", "tone": "watch", "note": ""},
            "projection": {
                "available": True,
                "base_return_pct": Decimal("8.80"),
                "projected_price": Decimal("15.23"),
                "confidence_label": "Alta",
                "coefficient": Decimal("0.52"),
                "safety_score": Decimal("76.00"),
                "net_income_yield_pct": Decimal("2.40"),
                "gross_dividend_yield_pct": Decimal("3.20"),
                "transaction_drag_pct": Decimal("0.20"),
                "annualized_volatility_pct": Decimal("12.00"),
                "positive_year_ratio_pct": Decimal("70.00"),
                "years_covered": Decimal("10.00"),
                "cycle_phase": "Expansion",
                "current_drawdown_pct": Decimal("-2.00"),
                "max_drawdown_pct": Decimal("-18.00"),
            },
            "projection_reliability": {"label": "Alta", "score": Decimal("82.00")},
            "cycle_projection_5y": {
                "available": True,
                "annual_return_pct": Decimal("6.60"),
                "five_year_return_pct": Decimal("37.50"),
            },
        }

        plan = build_equity_allocation_plan(
            [short_term_better, long_term_better],
            Decimal("100000"),
            Decimal("60"),
            strategy_mode="5y_primary",
        )

        self.assertTrue(plan["available"])
        self.assertEqual(plan["strategy_mode"], "5y_primary")
        self.assertEqual(plan["strategy_label"], "5A principal")
        self.assertEqual(plan["allocations"][0]["position"].ticker, "IBE")
        self.assertGreater(plan["allocations"][0]["cycle_return_annual_pct"], Decimal("0"))

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
        self.assertIsNone(run.summary_data["weighted_cycle_return_annual_pct"])
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

    @override_settings(EQUITIES_OPTIMIZATION_ASYNC=False)
    def test_launch_optimization_run_pair_creates_two_strategy_runs(self):
        with (
            patch("equities.optimization_runs.enqueue_equity_optimization_run") as mocked_enqueue,
            patch("equities.optimization_runs.process_equity_optimization_run") as mocked_process,
        ):
            runs = launch_equity_optimization_run_pair(
                total_investment=Decimal("80000"),
                max_company_pct=Decimal("20"),
                max_total_positions=6,
                max_sector_positions=1,
                selected_sectors=["Banca"],
                reference_label="Cartera dual",
            )

        self.assertEqual(len(runs), 2)
        self.assertEqual({run.progress_data["strategy_mode"] for run in runs}, {"12m_primary", "5y_primary"})
        self.assertEqual({run.progress_data["strategy_label"] for run in runs}, {"12M principal", "5A principal"})
        self.assertTrue(any(run.reference_code.endswith("-12M") for run in runs))
        self.assertTrue(any(run.reference_code.endswith("-5A") for run in runs))
        self.assertEqual(mocked_enqueue.call_count, 2)
        mocked_process.assert_not_called()

    @override_settings(
        EQUITIES_OPTIMIZATION_ASYNC=False,
        EQUITIES_SCHEDULED_OPTIMIZATION_ENABLED=True,
        EQUITIES_SCHEDULED_OPTIMIZATION_ISO_WEEKDAYS=(2, 4),
    )
    def test_launch_scheduled_optimization_runs_creates_pair_once_per_day(self):
        analysis_day = date(2026, 4, 21)

        with (
            patch("equities.optimization_runs.enqueue_equity_optimization_run") as mocked_enqueue,
            patch("equities.optimization_runs.process_equity_optimization_run") as mocked_process,
        ):
            first_runs = launch_scheduled_equity_optimization_runs(
                analysis_date=analysis_day,
                force=False,
            )
            second_runs = launch_scheduled_equity_optimization_runs(
                analysis_date=analysis_day,
                force=False,
            )

        self.assertEqual(len(first_runs), 2)
        self.assertEqual(len(second_runs), 2)
        self.assertEqual(EquityOptimizationRun.objects.count(), 2)
        self.assertEqual(mocked_enqueue.call_count, 2)
        mocked_process.assert_not_called()
        self.assertEqual(
            {run.progress_data["scheduled_run_key"] for run in first_runs},
            {"scheduled-optimization:2026-04-21"},
        )
        self.assertEqual(
            {run.progress_data["schedule_kind"] for run in first_runs},
            {"nightly"},
        )
        self.assertEqual(
            {run.progress_data["scheduled_weekdays_label"] for run in first_runs},
            {"martes y jueves"},
        )
        self.assertEqual({Decimal(str(run.max_company_pct)) for run in first_runs}, {Decimal("30.00")})
        self.assertEqual({run.max_total_positions for run in first_runs}, {5})
        self.assertEqual({run.max_sector_positions for run in first_runs}, {2})
        self.assertEqual({run.id for run in first_runs}, {run.id for run in second_runs})

    @override_settings(
        EQUITIES_OPTIMIZATION_ASYNC=False,
        EQUITIES_SCHEDULED_OPTIMIZATION_ENABLED=True,
        EQUITIES_SCHEDULED_OPTIMIZATION_ISO_WEEKDAYS=(2, 4),
    )
    def test_launch_scheduled_optimization_runs_replaces_failed_attempts(self):
        analysis_day = date(2026, 4, 21)
        failed_runs = [
            EquityOptimizationRun.objects.create(
                reference_code=f"OPT-FAILED-{suffix}",
                label=f"Fallida {suffix}",
                total_investment=Decimal("100000"),
                max_company_pct=Decimal("30"),
                max_total_positions=5,
                max_sector_positions=2,
                status=EquityOptimizationRun.Status.FAILED,
                progress_data={
                    "strategy_mode": strategy_mode,
                    "strategy_label": strategy_label,
                    "schedule_kind": "nightly",
                    "scheduled_run_key": "scheduled-optimization:2026-04-21",
                    "scheduled_analysis_date": "2026-04-21",
                    "scheduled_weekdays_label": "martes y jueves",
                },
            )
            for suffix, strategy_mode, strategy_label in (
                ("12M", "12m_primary", "12M principal"),
                ("5A", "5y_primary", "5A principal"),
            )
        ]

        with (
            patch("equities.optimization_runs.enqueue_equity_optimization_run") as mocked_enqueue,
            patch("equities.optimization_runs.process_equity_optimization_run") as mocked_process,
        ):
            runs = launch_scheduled_equity_optimization_runs(
                analysis_date=analysis_day,
                force=False,
            )

        self.assertEqual(len(runs), 2)
        self.assertEqual(EquityOptimizationRun.objects.count(), 2)
        self.assertFalse(EquityOptimizationRun.objects.filter(id__in=[run.id for run in failed_runs]).exists())
        self.assertEqual(mocked_enqueue.call_count, 2)
        mocked_process.assert_not_called()

    def test_purge_stale_scheduled_optimizations_removes_old_and_wrong_policy_runs(self):
        recent_valid = EquityOptimizationRun.objects.create(
            reference_code="OPT-RECENT-VALID",
            label="Reciente valida",
            total_investment=Decimal("100000"),
            max_company_pct=Decimal("30"),
            max_total_positions=5,
            max_sector_positions=2,
            status=EquityOptimizationRun.Status.COMPLETED,
            progress_data={
                "schedule_kind": "nightly",
                "scheduled_run_key": "scheduled-optimization:2026-04-16",
                "scheduled_analysis_date": "2026-04-16",
            },
        )
        old_run = EquityOptimizationRun.objects.create(
            reference_code="OPT-OLD-RUN",
            label="Antigua",
            total_investment=Decimal("100000"),
            max_company_pct=Decimal("30"),
            max_total_positions=5,
            max_sector_positions=2,
            status=EquityOptimizationRun.Status.COMPLETED,
            progress_data={
                "schedule_kind": "nightly",
                "scheduled_run_key": "scheduled-optimization:2025-12-01",
                "scheduled_analysis_date": "2025-12-01",
            },
        )
        wrong_policy_run = EquityOptimizationRun.objects.create(
            reference_code="OPT-WRONG-POLICY",
            label="Politica vieja",
            total_investment=Decimal("100000"),
            max_company_pct=Decimal("20"),
            max_total_positions=0,
            max_sector_positions=0,
            status=EquityOptimizationRun.Status.COMPLETED,
            progress_data={
                "schedule_kind": "nightly",
                "scheduled_run_key": "scheduled-optimization:2026-04-10",
                "scheduled_analysis_date": "2026-04-10",
            },
        )

        deleted_count = purge_stale_scheduled_optimization_runs(as_of=date(2026, 4, 17))

        self.assertGreaterEqual(deleted_count, 2)
        self.assertTrue(EquityOptimizationRun.objects.filter(pk=recent_valid.pk).exists())
        self.assertFalse(EquityOptimizationRun.objects.filter(pk=old_run.pk).exists())
        self.assertFalse(EquityOptimizationRun.objects.filter(pk=wrong_policy_run.pk).exists())

    @override_settings(
        EQUITIES_SCHEDULED_OPTIMIZATION_ENABLED=True,
        EQUITIES_SCHEDULED_OPTIMIZATION_ISO_WEEKDAYS=(2, 4),
    )
    def test_build_scheduled_optimization_persistence_context_aggregates_recent_repetition(self):
        today = timezone.localdate()
        scheduled_days = [today - timedelta(days=3), today - timedelta(days=10), today - timedelta(days=38), today - timedelta(days=110)]
        strategy_runs = [
            ("12M principal", "12m_primary"),
            ("5A principal", "5y_primary"),
        ]

        for index, analysis_day in enumerate(scheduled_days, start=1):
            for strategy_label, strategy_mode in strategy_runs:
                run = EquityOptimizationRun.objects.create(
                    reference_code=f"OPT-SCHED-{index}-{strategy_mode}",
                    label=f"Programada {analysis_day.isoformat()} - {strategy_label}",
                    total_investment=Decimal("100000"),
                    max_company_pct=Decimal("30"),
                    max_total_positions=5,
                    max_sector_positions=2,
                    status=EquityOptimizationRun.Status.COMPLETED,
                    progress_data={
                        "strategy_label": strategy_label,
                        "strategy_mode": strategy_mode,
                        "schedule_kind": "nightly",
                        "scheduled_run_key": f"scheduled-optimization:{analysis_day.isoformat()}",
                        "scheduled_analysis_date": analysis_day.isoformat(),
                        "scheduled_weekdays_label": "martes y jueves",
                    },
                    summary_data={
                        "available": True,
                        "strategy_label": strategy_label,
                        "scheduled_analysis_date": analysis_day.isoformat(),
                    },
                    allocations_data=[
                        {
                            "rank": 1 if strategy_mode == "12m_primary" else 2,
                            "company_name": "Iberdrola",
                            "ticker": "IBE",
                            "net_projected_return_pct": 12.0 if strategy_mode == "12m_primary" else 10.0,
                            "cycle_return_5y_pct": 74.0 if strategy_mode == "12m_primary" else 70.0,
                            "reliability_label": "Alta",
                            "reliability_score": 82.0 if strategy_mode == "12m_primary" else 78.0,
                            "cycle_yearly_margins": [
                                {"year_number": 1, "label": "AÑO 1", "margin_pct": 12.0 if strategy_mode == "12m_primary" else 10.0},
                                {"year_number": 2, "label": "AÑO 2", "margin_pct": 7.0},
                                {"year_number": 3, "label": "AÑO 3", "margin_pct": 8.0},
                                {"year_number": 4, "label": "AÑO 4", "margin_pct": 9.0},
                                {"year_number": 5, "label": "AÑO 5", "margin_pct": 10.0},
                            ],
                        },
                        {
                            "rank": 4,
                            "company_name": "Repsol",
                            "ticker": "REP",
                            "net_projected_return_pct": 6.0,
                            "cycle_return_5y_pct": 28.0,
                            "reliability_label": "Media",
                            "reliability_score": 64.0,
                            "cycle_yearly_margins": [
                                {"year_number": 1, "label": "AÑO 1", "margin_pct": 6.0},
                                {"year_number": 2, "label": "AÑO 2", "margin_pct": 4.0},
                            ],
                        },
                        {
                            "rank": 5,
                            "company_name": "ACS",
                            "ticker": "ACS",
                            "net_projected_return_pct": 8.0,
                            "cycle_return_5y_pct": 40.0,
                            "reliability_label": "Media",
                            "reliability_score": 60.0,
                            "cycle_yearly_margins": [
                                {"year_number": 1, "label": "AÃ‘O 1", "margin_pct": 8.0},
                                {"year_number": 2, "label": "AÃ‘O 2", "margin_pct": 5.0},
                            ],
                        },
                    ],
                )
                EquityOptimizationRun.objects.filter(pk=run.pk).update(
                    created_at=timezone.make_aware(datetime.combine(analysis_day, datetime.min.time())),
                    completed_at=timezone.make_aware(datetime.combine(analysis_day, datetime.min.time())),
                )

        context = build_scheduled_optimization_persistence_context(as_of=today)

        self.assertTrue(context["available"])
        self.assertEqual(context["runs_count_3m"], 6)
        self.assertEqual(context["distinct_days_count_3m"], 3)
        self.assertEqual(context["policy"]["max_total_positions"], 5)
        self.assertEqual(context["policy"]["max_sector_positions"], 2)
        self.assertEqual(context["policy"]["max_company_pct"], Decimal("30"))
        top_row = context["rows"][0]
        self.assertEqual(top_row["ticker"], "IBE")
        self.assertEqual(top_row["appearances_3m"], 6)
        self.assertEqual(top_row["presence_pct_3m"], Decimal("100.0"))
        self.assertEqual(top_row["distinct_days_3m"], 3)
        self.assertEqual(top_row["day_presence_pct_3m"], Decimal("100.0"))
        self.assertEqual(top_row["top3_3m"], 6)
        self.assertEqual(top_row["top3_pct_3m"], Decimal("100.0"))
        self.assertEqual(top_row["persistence_label"], "Media")
        self.assertEqual(top_row["strategy_labels_3m_label"], "12M principal, 5A principal")
        self.assertEqual(top_row["average_return_12m_3m"], Decimal("11.0"))
        self.assertEqual(top_row["average_return_5y_3m"], Decimal("72.0"))
        self.assertEqual(top_row["average_reliability_label_3m"], "Alta")
        self.assertEqual(top_row["average_reliability_score_3m"], Decimal("80.0"))
        self.assertEqual([item["ticker"] for item in context["rows"][:3]], ["IBE", "ACS", "REP"])
        self.assertEqual(
            [item["margin_pct"] for item in top_row["average_year_margins"]],
            [Decimal("11.0"), Decimal("7.0"), Decimal("8.0"), Decimal("9.0"), Decimal("10.0")],
        )

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
        self.assertFalse(card["cycle_projection_5y_chart"]["available"])
        self.assertFalse(card["projection_backtest"]["monthly_chart"]["available"])
        self.assertEqual(card["suggested_references"], [])

    def test_fetch_market_series_reuses_cache_within_same_bucket(self):
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

    @override_settings(
        AI_LLM_PROVIDER="openai",
        OPENAI_API_KEY="test-openai-key",
        OPENAI_DEFAULT_MODEL="gpt-4o-mini",
        OPENAI_MAX_TOKENS=768,
    )
    def test_enrich_dashboard_with_ai_analysis_uses_openai_account(self):
        position = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.OWNED,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2024, 1, 10),
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
            annual_dividend_income=Decimal("18.00"),
            annual_maintenance_cost=Decimal("4.00"),
        )
        populate_position_history(position)
        history_card = build_equity_history_cards([position])[0]
        dashboard = {
            "history_cards": [history_card],
            "ibex_universe_cards": [],
        }
        payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "La lectura 12M sigue positiva y el backtest aguanta razonablemente bien.",
                                "action_label": "Mantener",
                                "action_note": "Mantener mientras no pierda fiabilidad ni empeore el alpha.",
                                "confidence_label": "Media",
                                "drivers": [
                                    "El retorno esperado 12M sigue en positivo",
                                    "La seguridad no esta deteriorada",
                                ],
                                "risks": [
                                    "La volatilidad puede ampliar el rango",
                                    "La validacion historica no es perfecta",
                                ],
                                "backtest_note": "El modelo acierta la direccion mas veces de las que falla.",
                                "cycle_note": "El ciclo 5A aun acompana, aunque con posibles fases de correccion.",
                            }
                        )
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 1200,
                "completion_tokens": 300,
            },
        }

        with patch(
            "equities.llm_analysis.urlopen",
            return_value=FakeHTTPResponse(json.dumps(payload).encode("utf-8")),
        ) as mocked_urlopen:
            summary = enrich_dashboard_with_ai_analysis(
                dashboard,
                analysis_date=timezone.localdate(),
            )

        self.assertEqual(mocked_urlopen.call_count, 1)
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["provider"], "openai")
        self.assertEqual(summary["completed_count"], 1)
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(summary["estimated_cost_usd"], "0.0004")
        self.assertTrue(history_card["ai_analysis"]["available"])
        self.assertEqual(history_card["ai_analysis"]["action_label"], "Mantener")
        self.assertEqual(history_card["ai_analysis"]["confidence_label"], "Media")
        self.assertIn("ChatGPT", history_card["ai_analysis"]["model_label"])

    @override_settings(
        AI_LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY="test-anthropic-key",
        CLAUDE_DEFAULT_MODEL="claude-sonnet-4-20250514",
        CLAUDE_MAX_TOKENS=768,
    )
    def test_enrich_dashboard_with_ai_analysis_uses_claude_account(self):
        position = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="ACS",
            quote_symbol="ACS.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="ACS",
            shares=Decimal("1.0000"),
            average_cost_per_share=Decimal("34.0000"),
            current_price_per_share=Decimal("34.0000"),
            annual_dividend_income=Decimal("0.50"),
            annual_maintenance_cost=Decimal("0.00"),
        )
        populate_position_history(position)
        history_card = build_equity_history_cards([position])[0]
        dashboard = {
            "history_cards": [],
            "ibex_universe_cards": [history_card],
        }
        payload = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "summary": "La lectura 12M sigue constructiva y el ciclo 5A aun acompana.",
                            "action_label": "Comprar",
                            "action_note": "Comprar con disciplina porque el modelo sigue favorable.",
                            "confidence_label": "Alta",
                            "drivers": [
                                "La seguridad sigue por encima de la media",
                                "El ciclo largo no esta roto",
                            ],
                            "risks": [
                                "Una correccion de mercado puede enfriar la entrada",
                            ],
                            "backtest_note": "La comprobacion historica acompana la decision actual.",
                            "cycle_note": "El escenario 5A sigue sesgado a expansion con pausas.",
                        }
                    ),
                }
            ],
            "usage": {
                "input_tokens": 1500,
                "output_tokens": 260,
            },
        }

        with patch(
            "equities.llm_analysis.urlopen",
            return_value=FakeHTTPResponse(json.dumps(payload).encode("utf-8")),
        ) as mocked_urlopen:
            summary = enrich_dashboard_with_ai_analysis(
                dashboard,
                analysis_date=timezone.localdate(),
            )

        self.assertEqual(mocked_urlopen.call_count, 1)
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["provider"], "anthropic")
        self.assertEqual(summary["completed_count"], 1)
        self.assertEqual(summary["estimated_cost_usd"], "0.0084")
        self.assertTrue(history_card["ai_analysis"]["available"])
        self.assertEqual(history_card["ai_analysis"]["action_label"], "Comprar")
        self.assertEqual(history_card["ai_analysis"]["confidence_label"], "Alta")
        self.assertIn("Claude", history_card["ai_analysis"]["model_label"])

    @override_settings(
        AI_LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY="test-anthropic-key",
        CLAUDE_DEFAULT_MODEL="claude-sonnet-4-20250514",
        AI_LLM_RETRY_ATTEMPTS=3,
        AI_LLM_RATE_LIMIT_RETRY_SECONDS=15,
    )
    def test_enrich_dashboard_with_ai_analysis_retries_rate_limited_claude_calls(self):
        position = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="LOG",
            quote_symbol="LOG.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Logista",
            shares=Decimal("1.0000"),
            average_cost_per_share=Decimal("24.0000"),
            current_price_per_share=Decimal("24.0000"),
            annual_dividend_income=Decimal("0.50"),
            annual_maintenance_cost=Decimal("0.00"),
        )
        populate_position_history(position)
        history_card = build_equity_history_cards([position])[0]
        dashboard = {
            "history_cards": [],
            "ibex_universe_cards": [history_card],
        }
        success_payload = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "summary": "La lectura cuantitativa vuelve a ser util tras el reintento.",
                            "action_label": "Mantener",
                            "action_note": "Mantener mientras no empeore la fiabilidad.",
                            "confidence_label": "Media",
                            "drivers": [
                                "La rentabilidad esperada sigue positiva",
                            ],
                            "risks": [
                                "La volatilidad sigue presente",
                            ],
                            "backtest_note": "El backtest sigue siendo razonable.",
                            "cycle_note": "El ciclo no se ha roto.",
                        }
                    ),
                }
            ],
            "usage": {
                "input_tokens": 900,
                "output_tokens": 180,
            },
        }
        rate_limit_body = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "rate_limit_error",
                    "message": "This request would exceed your organization's rate limit.",
                },
            }
        ).encode("utf-8")
        rate_limit_error = HTTPError(
            url="https://api.anthropic.com/v1/messages",
            code=429,
            msg="Too Many Requests",
            hdrs={"Retry-After": "1"},
            fp=io.BytesIO(rate_limit_body),
        )

        with (
            patch(
                "equities.llm_analysis.urlopen",
                side_effect=[rate_limit_error, FakeHTTPResponse(json.dumps(success_payload).encode("utf-8"))],
            ) as mocked_urlopen,
            patch("equities.llm_analysis.time.sleep") as mocked_sleep,
        ):
            summary = enrich_dashboard_with_ai_analysis(
                dashboard,
                analysis_date=timezone.localdate(),
            )

        self.assertEqual(mocked_urlopen.call_count, 2)
        mocked_sleep.assert_called_once_with(15)
        self.assertEqual(summary["completed_count"], 1)
        self.assertEqual(summary["failed_count"], 0)
        self.assertTrue(history_card["ai_analysis"]["available"])
        self.assertEqual(history_card["ai_analysis"]["action_label"], "Mantener")

    def test_run_nightly_equity_analysis_persists_full_ibex_cache(self):
        analysis_day = timezone.localdate()
        position = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.OWNED,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2024, 1, 10),
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
            annual_dividend_income=Decimal("18.00"),
            annual_maintenance_cost=Decimal("4.00"),
        )
        populate_position_history(position)
        tracked_card = build_equity_history_cards([position])[0]
        ibex_position = EquityPosition(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="ACS",
            quote_symbol="ACS.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="ACS",
            shares=Decimal("1.0000"),
            average_cost_per_share=Decimal("34.0000"),
            current_price_per_share=Decimal("34.0000"),
            annual_dividend_income=Decimal("0.50"),
        )
        ibex_card = {
            **tracked_card,
            "position": ibex_position,
            "status_key": "ibex",
            "status_label": "Solo radar",
            "detail_anchor": "",
            "sector_label": "Construccion",
        }
        tracked_card["ai_analysis"] = {
            "available": True,
            "provider": "anthropic",
            "label": "Claude claude-sonnet-4-20250514",
            "model": "claude-sonnet-4-20250514",
            "model_label": "Claude claude-sonnet-4-20250514",
            "summary": "Lectura Claude para posicion en cartera.",
            "action_label": "Mantener",
            "action_note": "Mantener mientras no cambie la tesis.",
            "confidence_label": "Media",
            "drivers": ["Retorno esperado positivo"],
            "risks": ["Backtest mejorable"],
            "backtest_note": "El backtest sigue siendo util.",
            "cycle_note": "El ciclo 5A sigue acompasado.",
            "consistency_label": "Alineado",
            "consistency_note": "La IA coincide con el motor cuantitativo.",
            "generated_at": timezone.now().isoformat(),
            "generated_at_label": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
            "usage": {"input_tokens": 1100, "output_tokens": 320, "estimated_cost_usd": "0.0081"},
        }
        ibex_card["ai_analysis"] = {
            "available": True,
            "provider": "anthropic",
            "label": "Claude claude-sonnet-4-20250514",
            "model": "claude-sonnet-4-20250514",
            "model_label": "Claude claude-sonnet-4-20250514",
            "summary": "Lectura Claude para radar IBEX.",
            "action_label": "Comprar",
            "action_note": "Comprar con disciplina si aguanta la tendencia.",
            "confidence_label": "Media",
            "drivers": ["Rentabilidad 12M positiva"],
            "risks": ["Ciclo todavia sensible"],
            "backtest_note": "La validacion historica acompana.",
            "cycle_note": "El ciclo largo sigue favorable.",
            "consistency_label": "Alineado",
            "consistency_note": "La IA coincide con el motor cuantitativo.",
            "generated_at": timezone.now().isoformat(),
            "generated_at_label": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
            "usage": {"input_tokens": 1100, "output_tokens": 320, "estimated_cost_usd": "0.0081"},
        }
        dashboard = {
            "history_cards": [tracked_card],
            "owned_history_cards": [tracked_card],
            "ibex_universe_cards": [ibex_card],
            "ibex_universe_summary": {
                "available": True,
                "analyzed_count": 1,
                "buy_alert_count": 1,
                "sell_alert_count": 0,
                "watch_alert_count": 0,
                "registered_count": 0,
                "registered_owned_count": 0,
                "registered_watchlist_count": 0,
                "radar_only_count": 1,
                "failed_count": 0,
                "failures": [],
                "broker_assumption": "Interactive Brokers",
                "trade_channel_label": "App",
                "top_pick": {"ticker": "ACS", "company_name": "ACS"},
            },
            "reference_guide_summary": {
                "available": True,
                "workbook_loaded": False,
                "source_label": "",
                "tracked_count": 1,
                "owned_count": 1,
                "watchlist_count": 0,
                "guide_only_count": 0,
            },
        }

        with (
            patch("equities.nightly_analysis.sync_all_equities_market_data", return_value=[]),
            patch("equities.nightly_analysis.build_equity_analysis_dashboard", return_value=dashboard) as mocked_dashboard,
            patch(
                "equities.nightly_analysis.resolve_ai_provider_config",
                return_value=type(
                    "ProviderConfig",
                    (),
                    {
                        "available": True,
                        "provider": "anthropic",
                        "label": "Claude claude-sonnet-4-20250514",
                        "model": "claude-sonnet-4-20250514",
                        "monthly_budget_usd": Decimal("50.00"),
                    },
                )(),
            ),
            patch(
                "equities.nightly_analysis.enrich_dashboard_with_ai_analysis",
                return_value={
                    "enabled": True,
                    "provider": "anthropic",
                    "label": "Claude claude-sonnet-4-20250514",
                    "model": "claude-sonnet-4-20250514",
                    "completed_count": 2,
                    "failed_count": 0,
                    "skipped_budget_count": 0,
                    "total_count": 2,
                    "input_tokens": 2200,
                    "output_tokens": 640,
                    "estimated_cost_usd": "0.0162",
                    "monthly_budget_usd": "50.0000",
                    "monthly_cost_before_run_usd": "0.0000",
                    "monthly_cost_after_run_usd": "0.0162",
                    "failures": [],
                },
            ) as mocked_llm,
        ):
            run = run_nightly_equity_analysis(
                analysis_date=analysis_day,
                force=True,
            )

        self.assertIsNotNone(run)
        self.assertEqual(run.status, EquityNightlyAnalysisRun.Status.COMPLETED)
        self.assertEqual(run.snapshots.count(), 2)
        self.assertEqual(run.agent_provider, "anthropic")
        self.assertIn("Claude", run.agent_label)
        self.assertEqual(
            mocked_dashboard.call_args.kwargs,
            {
                "include_ibex_universe": True,
                "ibex_company_limit": None,
                "ibex_include_visuals": True,
                "ibex_include_reference_suggestions": True,
                "ibex_include_fundamentals": True,
            },
        )
        mocked_llm.assert_called_once()
        self.assertEqual(run.summary_data["llm"]["provider"], "anthropic")
        self.assertEqual(run.summary_data["llm"]["completed_count"], 2)
        self.assertTrue(
            EquityNightlyAnalysisSnapshot.objects.filter(
                run=run,
                scope=EquityNightlyAnalysisSnapshot.Scope.IBEX,
                ticker="ACS",
            ).exists()
        )

    @override_settings(
        AI_LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY="test-anthropic-key",
        CLAUDE_DEFAULT_MODEL="claude-sonnet-4-20250514",
        EQUITIES_NIGHTLY_LLM_REFRESH_ISO_WEEKDAYS=(2, 4),
    )
    def test_run_nightly_equity_analysis_reuses_last_claude_refresh_outside_scheduled_days(self):
        refresh_day = date(2026, 4, 16)
        analysis_day = date(2026, 4, 17)
        position = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.OWNED,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2024, 1, 10),
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
            annual_dividend_income=Decimal("18.00"),
            annual_maintenance_cost=Decimal("4.00"),
        )
        populate_position_history(position)
        previous_card = build_equity_history_cards([position])[0]
        previous_card["ai_analysis"] = {
            "available": True,
            "provider": "anthropic",
            "label": "Claude claude-sonnet-4-20250514",
            "model": "claude-sonnet-4-20250514",
            "model_label": "Claude claude-sonnet-4-20250514",
            "summary": "Lectura Claude guardada para reutilizar entre semana.",
            "action_label": "Mantener",
            "action_note": "Mantener con disciplina.",
            "confidence_label": "Media",
            "drivers": ["Retorno esperado positivo"],
            "risks": ["Volatilidad controlable"],
            "backtest_note": "El backtest sigue razonable.",
            "cycle_note": "El ciclo aun acompana.",
            "consistency_label": "Alineado",
            "consistency_note": "La IA coincide con el motor cuantitativo.",
            "generated_at": timezone.now().isoformat(),
            "generated_at_label": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
            "usage": {"input_tokens": 300, "output_tokens": 120, "estimated_cost_usd": "0.0030"},
        }
        persist_nightly_analysis_dashboard(
            {
                "history_cards": [previous_card],
                "ibex_universe_cards": [],
                "ibex_universe_summary": {
                    "available": False,
                    "analyzed_count": 0,
                    "buy_alert_count": 0,
                    "sell_alert_count": 0,
                    "watch_alert_count": 0,
                    "registered_count": 0,
                    "registered_owned_count": 0,
                    "registered_watchlist_count": 0,
                    "radar_only_count": 0,
                    "failed_count": 0,
                    "failures": [],
                    "broker_assumption": "",
                    "trade_channel_label": "",
                    "top_pick": None,
                },
                "reference_guide_summary": {
                    "available": True,
                    "workbook_loaded": False,
                    "source_label": "",
                    "tracked_count": 1,
                    "owned_count": 1,
                    "watchlist_count": 0,
                    "guide_only_count": 0,
                },
            },
            [position],
            analysis_date=refresh_day,
            agent_provider="anthropic",
            agent_label="Claude claude-sonnet-4-20250514",
            llm_summary={
                "enabled": True,
                "provider": "anthropic",
                "label": "Claude claude-sonnet-4-20250514",
                "model": "claude-sonnet-4-20250514",
                "completed_count": 1,
                "failed_count": 0,
                "skipped_budget_count": 0,
                "total_count": 1,
                "input_tokens": 300,
                "output_tokens": 120,
                "estimated_cost_usd": "0.0030",
                "monthly_budget_usd": "50.0000",
                "monthly_cost_before_run_usd": "0.0000",
                "monthly_cost_after_run_usd": "0.0030",
                "failures": [],
                "refresh_performed": True,
                "source_analysis_date": refresh_day.isoformat(),
                "source_analysis_date_label": refresh_day.isoformat(),
            },
        )

        fresh_card = build_equity_history_cards([position])[0]
        dashboard = {
            "history_cards": [fresh_card],
            "owned_history_cards": [fresh_card],
            "ibex_universe_cards": [],
            "ibex_universe_summary": {
                "available": False,
                "analyzed_count": 0,
                "buy_alert_count": 0,
                "sell_alert_count": 0,
                "watch_alert_count": 0,
                "registered_count": 0,
                "registered_owned_count": 0,
                "registered_watchlist_count": 0,
                "radar_only_count": 0,
                "failed_count": 0,
                "failures": [],
                "broker_assumption": "",
                "trade_channel_label": "",
                "top_pick": None,
            },
            "reference_guide_summary": {
                "available": True,
                "workbook_loaded": False,
                "source_label": "",
                "tracked_count": 1,
                "owned_count": 1,
                "watchlist_count": 0,
                "guide_only_count": 0,
            },
        }

        with (
            patch("equities.nightly_analysis.sync_all_equities_market_data", return_value=[]),
            patch("equities.nightly_analysis.build_equity_analysis_dashboard", return_value=dashboard),
            patch("equities.nightly_analysis.enrich_dashboard_with_ai_analysis") as mocked_llm,
        ):
            run = run_nightly_equity_analysis(
                analysis_date=analysis_day,
                force=False,
            )

        self.assertIsNotNone(run)
        mocked_llm.assert_not_called()
        self.assertTrue(run.summary_data["llm"]["reused"])
        self.assertEqual(run.summary_data["llm"]["source_analysis_date"], refresh_day.isoformat())
        self.assertEqual(run.summary_data["llm"]["next_refresh_date"], "2026-04-21")
        snapshot = run.snapshots.get(scope=EquityNightlyAnalysisSnapshot.Scope.TRACKED, ticker="IBE")
        self.assertTrue(snapshot.analysis_payload["ai_analysis"]["available"])
        self.assertEqual(
            snapshot.analysis_payload["ai_analysis"]["summary"],
            "Lectura Claude guardada para reutilizar entre semana.",
        )
        self.assertIn("reutilizando la ultima lectura IA", run.status_note)

    @override_settings(
        AI_LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY="test-anthropic-key",
        CLAUDE_DEFAULT_MODEL="claude-sonnet-4-20250514",
        EQUITIES_NIGHTLY_LLM_REFRESH_ISO_WEEKDAYS=(2, 4),
    )
    def test_run_nightly_equity_analysis_keeps_previous_claude_when_refresh_fails(self):
        previous_day = date(2026, 4, 16)
        analysis_day = date(2026, 4, 21)
        position = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.OWNED,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2024, 1, 10),
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
            annual_dividend_income=Decimal("18.00"),
            annual_maintenance_cost=Decimal("4.00"),
        )
        populate_position_history(position)
        previous_card = build_equity_history_cards([position])[0]
        previous_card["ai_analysis"] = {
            "available": True,
            "provider": "anthropic",
            "label": "Claude claude-sonnet-4-20250514",
            "model": "claude-sonnet-4-20250514",
            "model_label": "Claude claude-sonnet-4-20250514",
            "summary": "Lectura Claude anterior que no debe perderse.",
            "action_label": "Mantener",
            "action_note": "Mantener mientras no cambie la tesis.",
            "confidence_label": "Alta",
            "drivers": ["Retorno esperado positivo"],
            "risks": ["Backtest mejorable"],
            "backtest_note": "El backtest sigue siendo util.",
            "cycle_note": "El ciclo sigue constructivo.",
            "consistency_label": "Alineado",
            "consistency_note": "La IA coincide con el motor cuantitativo.",
            "generated_at": timezone.now().isoformat(),
            "generated_at_label": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
            "usage": {"input_tokens": 320, "output_tokens": 110, "estimated_cost_usd": "0.0031"},
        }
        persist_nightly_analysis_dashboard(
            {
                "history_cards": [previous_card],
                "ibex_universe_cards": [],
                "ibex_universe_summary": {
                    "available": False,
                    "analyzed_count": 0,
                    "buy_alert_count": 0,
                    "sell_alert_count": 0,
                    "watch_alert_count": 0,
                    "registered_count": 0,
                    "registered_owned_count": 0,
                    "registered_watchlist_count": 0,
                    "radar_only_count": 0,
                    "failed_count": 0,
                    "failures": [],
                    "broker_assumption": "",
                    "trade_channel_label": "",
                    "top_pick": None,
                },
                "reference_guide_summary": {
                    "available": True,
                    "workbook_loaded": False,
                    "source_label": "",
                    "tracked_count": 1,
                    "owned_count": 1,
                    "watchlist_count": 0,
                    "guide_only_count": 0,
                },
            },
            [position],
            analysis_date=previous_day,
            agent_provider="anthropic",
            agent_label="Claude claude-sonnet-4-20250514",
            llm_summary={
                "enabled": True,
                "provider": "anthropic",
                "label": "Claude claude-sonnet-4-20250514",
                "model": "claude-sonnet-4-20250514",
                "completed_count": 1,
                "failed_count": 0,
                "skipped_budget_count": 0,
                "total_count": 1,
                "input_tokens": 320,
                "output_tokens": 110,
                "estimated_cost_usd": "0.0031",
                "monthly_budget_usd": "50.0000",
                "monthly_cost_before_run_usd": "0.0000",
                "monthly_cost_after_run_usd": "0.0031",
                "failures": [],
                "refresh_performed": True,
                "source_analysis_date": previous_day.isoformat(),
                "source_analysis_date_label": previous_day.isoformat(),
            },
        )

        fresh_card = build_equity_history_cards([position])[0]
        dashboard = {
            "history_cards": [fresh_card],
            "owned_history_cards": [fresh_card],
            "ibex_universe_cards": [],
            "ibex_universe_summary": {
                "available": False,
                "analyzed_count": 0,
                "buy_alert_count": 0,
                "sell_alert_count": 0,
                "watch_alert_count": 0,
                "registered_count": 0,
                "registered_owned_count": 0,
                "registered_watchlist_count": 0,
                "radar_only_count": 0,
                "failed_count": 0,
                "failures": [],
                "broker_assumption": "",
                "trade_channel_label": "",
                "top_pick": None,
            },
            "reference_guide_summary": {
                "available": True,
                "workbook_loaded": False,
                "source_label": "",
                "tracked_count": 1,
                "owned_count": 1,
                "watchlist_count": 0,
                "guide_only_count": 0,
            },
        }

        def failing_refresh(current_dashboard, analysis_date):
            current_dashboard["history_cards"][0]["ai_analysis"] = {
                "available": False,
                "provider": "anthropic",
                "label": "Claude claude-sonnet-4-20250514",
                "model": "claude-sonnet-4-20250514",
                "model_label": "Claude claude-sonnet-4-20250514",
                "note": "Analisis IA no disponible: HTTP 429",
            }
            return {
                "enabled": True,
                "provider": "anthropic",
                "label": "Claude claude-sonnet-4-20250514",
                "model": "claude-sonnet-4-20250514",
                "completed_count": 0,
                "failed_count": 1,
                "skipped_budget_count": 0,
                "total_count": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_usd": "0.0000",
                "monthly_budget_usd": "50.0000",
                "monthly_cost_before_run_usd": "0.0031",
                "monthly_cost_after_run_usd": "0.0031",
                "failures": [{"ticker": "IBE", "company_name": "Iberdrola", "error": "HTTP 429"}],
            }

        with (
            patch("equities.nightly_analysis.sync_all_equities_market_data", return_value=[]),
            patch("equities.nightly_analysis.build_equity_analysis_dashboard", return_value=dashboard),
            patch("equities.nightly_analysis.enrich_dashboard_with_ai_analysis", side_effect=failing_refresh) as mocked_llm,
        ):
            run = run_nightly_equity_analysis(
                analysis_date=analysis_day,
                force=False,
            )

        self.assertIsNotNone(run)
        mocked_llm.assert_called_once()
        self.assertEqual(run.summary_data["llm"]["completed_count"], 1)
        self.assertEqual(run.summary_data["llm"]["failed_count"], 0)
        self.assertEqual(run.summary_data["llm"]["refresh_failed_count"], 1)
        self.assertEqual(run.summary_data["llm"]["retained_previous_count"], 1)
        snapshot = run.snapshots.get(scope=EquityNightlyAnalysisSnapshot.Scope.TRACKED, ticker="IBE")
        self.assertTrue(snapshot.analysis_payload["ai_analysis"]["available"])
        self.assertEqual(
            snapshot.analysis_payload["ai_analysis"]["summary"],
            "Lectura Claude anterior que no debe perderse.",
        )
        self.assertIn("respaldo previo", run.status_note)

    def test_management_command_invokes_nightly_analysis_runner(self):
        with (
            patch("equities.management.commands.run_equity_nightly_analysis.run_nightly_equity_analysis") as mocked_runner,
            patch("equities.management.commands.run_equity_nightly_analysis.launch_scheduled_equity_optimization_runs") as mocked_scheduler,
        ):
            mocked_run = type(
                "NightlyRun",
                (),
                {
                    "analysis_date": timezone.localdate(),
                    "snapshots": type("Snapshots", (), {"count": staticmethod(lambda: 3)})(),
                    "agent_label": "Analista nocturno",
                    "agent_provider": "core",
                },
            )()
            mocked_runner.return_value = mocked_run
            mocked_scheduler.return_value = []
            call_command("run_equity_nightly_analysis", "--force")

        mocked_runner.assert_called_once()
        mocked_scheduler.assert_called_once()
        self.assertTrue(mocked_scheduler.call_args.kwargs["run_inline"])

    def test_scheduled_optimization_management_command_invokes_scheduler(self):
        with patch("equities.management.commands.run_scheduled_equity_optimizations.launch_scheduled_equity_optimization_runs") as mocked_scheduler:
            mocked_scheduler.return_value = [
                EquityOptimizationRun(
                    reference_code="OPT-SCHED-001",
                    label="Programada - 12M principal",
                    total_investment=Decimal("100000"),
                    max_company_pct=Decimal("20"),
                    max_total_positions=0,
                    max_sector_positions=0,
                    progress_data={"scheduled_analysis_date": "2026-04-21"},
                ),
                EquityOptimizationRun(
                    reference_code="OPT-SCHED-002",
                    label="Programada - 5A principal",
                    total_investment=Decimal("100000"),
                    max_company_pct=Decimal("20"),
                    max_total_positions=0,
                    max_sector_positions=0,
                    progress_data={"scheduled_analysis_date": "2026-04-21"},
                ),
            ]
            call_command("run_scheduled_equity_optimizations", "--analysis-date", "2026-04-21")

        mocked_scheduler.assert_called_once()
        self.assertTrue(mocked_scheduler.call_args.kwargs["run_inline"])

    def test_scheduled_optimization_management_command_can_use_background_mode(self):
        with patch("equities.management.commands.run_scheduled_equity_optimizations.launch_scheduled_equity_optimization_runs") as mocked_scheduler:
            mocked_scheduler.return_value = [
                EquityOptimizationRun(
                    reference_code="OPT-SCHED-BG-001",
                    label="Programada - 12M principal",
                    total_investment=Decimal("100000"),
                    max_company_pct=Decimal("20"),
                    max_total_positions=0,
                    max_sector_positions=0,
                    progress_data={"scheduled_analysis_date": "2026-04-21"},
                ),
            ]
            call_command("run_scheduled_equity_optimizations", "--analysis-date", "2026-04-21", "--background")

        mocked_scheduler.assert_called_once()
        self.assertFalse(mocked_scheduler.call_args.kwargs["run_inline"])

    def test_build_dashboard_from_nightly_cache_uses_live_position_values(self):
        analysis_day = timezone.localdate()
        position = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.OWNED,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2024, 1, 10),
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
            annual_dividend_income=Decimal("18.00"),
            annual_maintenance_cost=Decimal("4.00"),
        )
        populate_position_history(position)
        tracked_card = build_equity_history_cards([position])[0]
        ibex_position = EquityPosition(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="ACS",
            quote_symbol="ACS.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="ACS",
            shares=Decimal("1.0000"),
            average_cost_per_share=Decimal("34.0000"),
            current_price_per_share=Decimal("34.0000"),
            annual_dividend_income=Decimal("0.50"),
        )
        ibex_card = {
            **tracked_card,
            "position": ibex_position,
            "status_key": "ibex",
            "status_label": "Solo radar",
            "detail_anchor": "",
            "sector_label": "Construccion",
        }
        dashboard = {
            "history_cards": [tracked_card],
            "ibex_universe_cards": [ibex_card],
            "ibex_universe_summary": {
                "available": True,
                "analyzed_count": 1,
                "buy_alert_count": 1,
                "sell_alert_count": 0,
                "watch_alert_count": 0,
                "registered_count": 0,
                "registered_owned_count": 0,
                "registered_watchlist_count": 0,
                "radar_only_count": 1,
                "failed_count": 0,
                "failures": [],
                "broker_assumption": "Interactive Brokers",
                "trade_channel_label": "App",
                "top_pick": {"ticker": "ACS", "company_name": "ACS"},
            },
            "reference_guide_summary": {
                "available": True,
                "workbook_loaded": False,
                "source_label": "",
                "tracked_count": 1,
                "owned_count": 1,
                "watchlist_count": 0,
                "guide_only_count": 0,
            },
        }
        persist_nightly_analysis_dashboard(
            dashboard,
            [position],
            analysis_date=analysis_day,
            agent_provider="core",
            agent_label="Analista nocturno",
        )

        position.current_price_per_share = Decimal("19.5000")
        position.latest_price_date = date(2026, 4, 16)
        position.save(update_fields=["current_price_per_share", "latest_price_date"])

        cached_dashboard = build_dashboard_from_nightly_cache(
            [position],
            include_ibex_universe=True,
        )

        self.assertIsNotNone(cached_dashboard)
        self.assertEqual(cached_dashboard["history_cards"][0]["position"].current_price_per_share, Decimal("19.5000"))
        self.assertTrue(cached_dashboard["history_cards"][0]["projection_12m_chart"]["available"])
        self.assertEqual(cached_dashboard["ibex_universe_cards"][0]["position"].ticker, "ACS")
        self.assertTrue(cached_dashboard["nightly_analysis"]["available"])

    def test_process_equity_optimization_run_uses_nightly_cache_when_available(self):
        run = EquityOptimizationRun.objects.create(
            reference_code="OPT-CACHE-001",
            label="Cartera cacheada",
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
            "cycle_projection_5y": {"available": False},
        }
        dashboard = {
            "optimizer_cards": [card],
            "history_cards": [],
            "ibex_universe_summary": {"analyzed_count": 35},
        }

        with (
            patch("equities.optimization_runs.sync_all_equities_market_data", return_value=[]),
            patch("equities.optimization_runs.build_dashboard_from_nightly_cache", return_value=dashboard) as mocked_cached_dashboard,
            patch("equities.optimization_runs.build_equity_analysis_dashboard") as mocked_live_dashboard,
            patch("equities.optimization_runs.build_news_signal_map", return_value={"IBE": {"label": "Prensa favorable", "score": Decimal("2.10"), "items_count": 2, "items": [], "note": "Buen tono", "available": True, "positive_count": 2, "negative_count": 0, "neutral_count": 0}}),
            patch("equities.optimization_runs.build_report_entries", return_value=[]),
            patch("equities.optimization_runs.build_report_html", return_value="<html>informe</html>"),
            patch("equities.optimization_runs.build_report_pdf_html", return_value="<html>pdf</html>"),
        ):
            process_equity_optimization_run(run.id)

        run.refresh_from_db()
        self.assertEqual(run.status, EquityOptimizationRun.Status.COMPLETED)
        mocked_cached_dashboard.assert_called_once()
        mocked_live_dashboard.assert_not_called()

@override_settings(EQUITIES_AUTO_SYNC_ON_VIEW=False, EQUITIES_IBEX_UNIVERSE_ANALYSIS=False)
@override_settings(EQUITIES_FETCH_FUNDAMENTALS=False)
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

    def test_equities_page_shows_nightly_analysis_status_in_hero(self):
        analysis_day = timezone.localdate()
        position = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.OWNED,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2024, 1, 10),
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
            annual_dividend_income=Decimal("18.00"),
            annual_maintenance_cost=Decimal("4.00"),
        )
        populate_position_history(position)
        tracked_card = build_equity_history_cards([position])[0]
        tracked_card["ai_analysis"] = {
            "available": True,
            "provider": "anthropic",
            "label": "Claude claude-sonnet-4-20250514",
            "model": "claude-sonnet-4-20250514",
            "model_label": "Claude claude-sonnet-4-20250514",
            "summary": "La tesis nocturna sigue favoreciendo mantener por retorno esperado y ciclo aun estable.",
            "action_label": "Mantener",
            "action_note": "Mantener si la fiabilidad no cae.",
            "confidence_label": "Media",
            "drivers": ["Retorno 12M positivo"],
            "risks": ["El backtest aun no es de nivel alto"],
            "backtest_note": "La validacion historica es util pero no impecable.",
            "cycle_note": "El ciclo 5A sigue acompasado con posibles tramos de correccion.",
            "consistency_label": "Mixto",
            "consistency_note": "La IA matiza la alerta cuantitativa.",
            "generated_at": timezone.now().isoformat(),
            "generated_at_label": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
            "usage": {"input_tokens": 400, "output_tokens": 120, "estimated_cost_usd": "0.0030"},
        }
        persist_nightly_analysis_dashboard(
            {
                "history_cards": [tracked_card],
                "ibex_universe_cards": [],
                "ibex_universe_summary": {
                    "available": False,
                    "analyzed_count": 0,
                    "buy_alert_count": 0,
                    "sell_alert_count": 0,
                    "watch_alert_count": 0,
                    "registered_count": 0,
                    "registered_owned_count": 0,
                    "registered_watchlist_count": 0,
                    "radar_only_count": 0,
                    "failed_count": 0,
                    "failures": [],
                    "broker_assumption": "",
                    "trade_channel_label": "",
                    "top_pick": None,
                },
                "reference_guide_summary": {
                    "available": True,
                    "workbook_loaded": False,
                    "source_label": "",
                    "tracked_count": 1,
                    "owned_count": 1,
                    "watchlist_count": 0,
                    "guide_only_count": 0,
                },
            },
            [position],
            analysis_date=analysis_day,
            agent_provider="anthropic",
            agent_label="Claude claude-sonnet-4-20250514",
            llm_summary={
                "enabled": True,
                "provider": "anthropic",
                "label": "Claude claude-sonnet-4-20250514",
                "model": "claude-sonnet-4-20250514",
                "completed_count": 1,
                "failed_count": 0,
                "skipped_budget_count": 0,
                "total_count": 1,
                "input_tokens": 400,
                "output_tokens": 120,
                "estimated_cost_usd": "0.0030",
                "monthly_budget_usd": "50.0000",
                "monthly_cost_before_run_usd": "0.0000",
                "monthly_cost_after_run_usd": "0.0030",
                "failures": [],
            },
        )

        with patch(
            "equities.services.build_owned_cycle_trade_timing_plan",
            return_value={
                "available": True,
                "mode": "sale_reentry",
                "sale_month_number": 12,
                "sale_date": date(2027, 4, 17),
                "sale_window_label": "abril 2027 (mes 12)",
                "signal_value_pct": Decimal("-0.13"),
                "summary": "Salida tactica en abril 2027.",
            },
        ):
            response = self.client.get(reverse("equities:list"))

        self.assertContains(response, "Analisis exhaustivo OK")
        self.assertContains(response, "Ultima ejecucion")
        self.assertContains(response, "Claude claude-sonnet-4-20250514")
        self.assertContains(response, "IA 1/1")
        self.assertContains(response, "Primera venta IBE abril 2027 (mes 12)")
        self.assertContains(response, "La primera salida tactica sugerida hoy seria Iberdrola en abril 2027 (mes 12).")

    def test_equities_page_reused_ai_status_does_not_label_pending_as_api_failures(self):
        analysis_day = timezone.localdate()
        position = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.OWNED,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2024, 1, 10),
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
            annual_dividend_income=Decimal("18.00"),
            annual_maintenance_cost=Decimal("4.00"),
        )
        populate_position_history(position)
        tracked_card = build_equity_history_cards([position])[0]
        tracked_card["ai_analysis"] = {
            "available": True,
            "provider": "anthropic",
            "label": "Claude claude-sonnet-4-20250514",
            "model": "claude-sonnet-4-20250514",
            "model_label": "Claude claude-sonnet-4-20250514",
            "summary": "Lectura Claude reutilizada desde la ultima actualizacion valida.",
            "action_label": "Mantener",
            "action_note": "Mantener si no cambia la tesis.",
            "confidence_label": "Media",
            "drivers": ["Retorno esperado positivo"],
            "risks": ["Backtest mejorable"],
            "backtest_note": "La validacion historica sigue siendo util.",
            "cycle_note": "El ciclo largo sigue acompasado.",
            "consistency_label": "Alineado",
            "consistency_note": "La IA coincide con el motor cuantitativo.",
            "generated_at": timezone.now().isoformat(),
            "generated_at_label": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
            "usage": {"input_tokens": 400, "output_tokens": 120, "estimated_cost_usd": "0.0030"},
        }
        persist_nightly_analysis_dashboard(
            {
                "history_cards": [tracked_card],
                "ibex_universe_cards": [],
                "ibex_universe_summary": {
                    "available": False,
                    "analyzed_count": 0,
                    "buy_alert_count": 0,
                    "sell_alert_count": 0,
                    "watch_alert_count": 0,
                    "registered_count": 0,
                    "registered_owned_count": 0,
                    "registered_watchlist_count": 0,
                    "radar_only_count": 0,
                    "failed_count": 0,
                    "failures": [],
                    "broker_assumption": "",
                    "trade_channel_label": "",
                    "top_pick": None,
                },
                "reference_guide_summary": {
                    "available": True,
                    "workbook_loaded": False,
                    "source_label": "",
                    "tracked_count": 1,
                    "owned_count": 1,
                    "watchlist_count": 0,
                    "guide_only_count": 0,
                },
            },
            [position],
            analysis_date=analysis_day,
            agent_provider="anthropic",
            agent_label="Claude claude-sonnet-4-20250514",
            llm_summary={
                "enabled": True,
                "provider": "anthropic",
                "label": "Claude claude-sonnet-4-20250514",
                "model": "claude-sonnet-4-20250514",
                "completed_count": 1,
                "failed_count": 11,
                "refresh_failed_count": 0,
                "retained_previous_count": 1,
                "pending_count": 11,
                "skipped_budget_count": 0,
                "total_count": 12,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_usd": "0.0000",
                "monthly_budget_usd": "50.0000",
                "monthly_cost_before_run_usd": "0.0030",
                "monthly_cost_after_run_usd": "0.0030",
                "failures": [],
                "reused": True,
                "refresh_performed": False,
                "source_analysis_date": "2026-04-16",
                "source_analysis_date_label": "2026-04-16",
                "next_refresh_date": "2026-04-21",
                "next_refresh_date_label": "2026-04-21",
            },
        )

        response = self.client.get(reverse("equities:list"))

        self.assertContains(response, "Quedan 11 valores sin lectura IA previa.")
        self.assertNotContains(response, "Hubo 11 fallo(s) de API.")

    def test_ibex_detail_view_uses_cached_nightly_snapshot(self):
        analysis_day = timezone.localdate()
        position = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.OWNED,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2024, 1, 10),
            shares=Decimal("10.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
            annual_dividend_income=Decimal("18.00"),
            annual_maintenance_cost=Decimal("4.00"),
        )
        populate_position_history(position)
        tracked_card = build_equity_history_cards([position])[0]
        ibex_position = EquityPosition(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="ACS",
            quote_symbol="ACS.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="ACS",
            shares=Decimal("1.0000"),
            average_cost_per_share=Decimal("34.0000"),
            current_price_per_share=Decimal("34.0000"),
            annual_dividend_income=Decimal("0.50"),
        )
        ibex_card = {
            **tracked_card,
            "position": ibex_position,
            "status_key": "ibex",
            "status_label": "Solo radar",
            "detail_anchor": "",
            "sector_label": "Construccion",
            "ai_analysis": {
                "available": True,
                "provider": "anthropic",
                "label": "Claude claude-sonnet-4-20250514",
                "model": "claude-sonnet-4-20250514",
                "model_label": "Claude claude-sonnet-4-20250514",
                "summary": "La lectura IA ve una oportunidad razonable mientras la validacion historica no empeore.",
                "action_label": "Comprar",
                "action_note": "Comprar si se mantiene la pendiente relativa.",
                "confidence_label": "Media",
                "drivers": ["Retorno esperado en positivo"],
                "risks": ["La fiabilidad no es plena"],
                "backtest_note": "El modelo acierta mas de lo que falla, pero no con precision alta.",
                "cycle_note": "El ciclo 5A sigue constructivo con correcciones intermedias.",
                "consistency_label": "Alineado",
                "consistency_note": "La IA coincide con el motor cuantitativo.",
                "generated_at": timezone.now().isoformat(),
                "generated_at_label": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
                "usage": {"input_tokens": 300, "output_tokens": 110, "estimated_cost_usd": "0.0020"},
            },
        }
        persist_nightly_analysis_dashboard(
            {
                "history_cards": [tracked_card],
                "ibex_universe_cards": [ibex_card],
                "ibex_universe_summary": {
                    "available": True,
                    "analyzed_count": 1,
                    "buy_alert_count": 1,
                    "sell_alert_count": 0,
                    "watch_alert_count": 0,
                    "registered_count": 0,
                    "registered_owned_count": 0,
                    "registered_watchlist_count": 0,
                    "radar_only_count": 1,
                    "failed_count": 0,
                    "failures": [],
                    "broker_assumption": "Interactive Brokers",
                    "trade_channel_label": "App",
                    "top_pick": {"ticker": "ACS", "company_name": "ACS"},
                },
                "reference_guide_summary": {
                    "available": True,
                    "workbook_loaded": False,
                    "source_label": "",
                    "tracked_count": 1,
                    "owned_count": 1,
                    "watchlist_count": 0,
                    "guide_only_count": 0,
                },
            },
            [position],
            analysis_date=analysis_day,
            agent_provider="core",
            agent_label="Analista nocturno",
        )

        company = {
            "ticker": "ACS",
            "company_name": "ACS",
            "quote_symbol": "ACS.MC",
            "sector": "Construccion",
        }
        workbook_snapshot = {
            "available": False,
            "path": "",
            "companies": [],
        }

        with (
            patch("equities.views.find_ibex_universe_company", return_value=(company, workbook_snapshot)),
            patch("equities.views.build_ibex_universe_card", side_effect=AssertionError("no deberia recalcular en vivo")),
        ):
            response = self.client.get(reverse("equities:ibex_detail", kwargs={"ticker": "ACS"}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ACS")
        self.assertContains(response, "Lectura IA nocturna")
        self.assertContains(response, "La lectura IA ve una oportunidad razonable")

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

    def test_create_owned_position_captures_purchase_forecast_baseline(self):
        analysis_day = date(2026, 4, 17)
        run = EquityNightlyAnalysisRun.objects.create(
            analysis_date=analysis_day,
            status=EquityNightlyAnalysisRun.Status.COMPLETED,
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        cached_position = EquityPosition(
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            shares=Decimal("0"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
        )
        EquityNightlyAnalysisSnapshot.objects.create(
            run=run,
            analysis_date=analysis_day,
            scope=EquityNightlyAnalysisSnapshot.Scope.IBEX,
            analysis_key="ibex:IBE",
            ticker="IBE",
            quote_symbol="IBE.MC",
            company_name="Iberdrola",
            status_key="ibex",
            sector_label="Utilities",
            agent_provider="core",
            analysis_payload=serialize_cached_value(
                {
                    "position": cached_position,
                    "reference_label": "IBEX 35",
                    "projection": {
                        "available": True,
                        "projected_price": Decimal("11.2500"),
                        "base_return_pct": Decimal("12.50"),
                        "safety_score": Decimal("68.00"),
                    },
                    "projection_reliability": {"label": "Alta", "score": Decimal("79.00")},
                    "trade_alert": {"label": "Comprar"},
                    "cycle_projection_5y": {
                        "available": True,
                        "path": [
                            {"label": "1A", "projected_price": Decimal("11.0000")},
                            {"label": "2A", "projected_price": Decimal("12.3750")},
                            {"label": "3A", "projected_price": Decimal("13.6125")},
                            {"label": "4A", "projected_price": Decimal("14.9738")},
                            {"label": "5A", "projected_price": Decimal("16.4712")},
                        ],
                    },
                }
            ),
        )

        response = self.client.post(
            reverse("equities:list"),
            {
                "action": "create_position",
                "position_kind": EquityPosition.PositionKind.OWNED,
                "ownership_category": AssetOwnershipCategory.XIMO,
                "broker": "Interactive Brokers",
                "ticker": "IBE",
                "company_name": "Iberdrola",
                "quote_symbol": "IBE.MC",
                "reference_profile": EquityPosition.ReferenceProfile.MARKET_INDEX,
                "benchmark_symbol": "^IBEX",
                "benchmark_name": "IBEX 35",
                "opened_on": "2026-04-17",
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
        baseline = EquityPurchaseForecastBaseline.objects.get(position=position)
        self.assertEqual(baseline.source_analysis_date, analysis_day)
        self.assertEqual(baseline.projected_return_pct_1y, Decimal("12.50"))
        self.assertEqual(baseline.projected_price_5y, Decimal("16.4712"))

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
        runs = [
            EquityOptimizationRun(
                reference_code="OPT-TEST-LAUNCH-12M",
                label="Cartera defensiva - 12M principal",
                total_investment=Decimal("100000"),
                max_company_pct=Decimal("20"),
                max_total_positions=0,
                max_sector_positions=1,
                status=EquityOptimizationRun.Status.PENDING,
            ),
            EquityOptimizationRun(
                reference_code="OPT-TEST-LAUNCH-5A",
                label="Cartera defensiva - 5A principal",
                total_investment=Decimal("100000"),
                max_company_pct=Decimal("20"),
                max_total_positions=0,
                max_sector_positions=1,
                status=EquityOptimizationRun.Status.PENDING,
            ),
        ]

        with patch("equities.views.launch_equity_optimization_run_pair", return_value=runs) as mocked_launch:
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

    def test_equities_page_keeps_single_optimization_history_table(self):
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
        self.assertContains(response, "Optimizaciones realizadas")
        self.assertContains(response, "equity-optimization-history")
        self.assertContains(response, first.display_label)
        self.assertContains(response, second.display_label)
        self.assertContains(response, "Iberdrola")
        self.assertContains(response, "Repsol")
        self.assertNotContains(response, "Comparacion entre optimizaciones cerradas")

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

    def helper_equities_page_shows_scheduled_optimization_persistence_panel_legacy(self):
        today = timezone.localdate()
        scheduled_day = today - timedelta(days=3)
        for strategy_label, strategy_mode in (("12M principal", "12m_primary"), ("5A principal", "5y_primary")):
            run = EquityOptimizationRun.objects.create(
                reference_code=f"OPT-PERSIST-{strategy_mode}",
                label=f"Programada {strategy_label}",
                total_investment=Decimal("200000"),
                max_company_pct=Decimal("30"),
                max_total_positions=5,
                max_sector_positions=2,
                status=EquityOptimizationRun.Status.COMPLETED,
                progress_data={
                    "strategy_label": strategy_label,
                    "strategy_mode": strategy_mode,
                    "schedule_kind": "nightly",
                    "scheduled_run_key": f"scheduled-optimization:{scheduled_day.isoformat()}",
                    "scheduled_analysis_date": scheduled_day.isoformat(),
                    "scheduled_weekdays_label": "martes y jueves",
                },
                summary_data={
                    "available": True,
                    "strategy_label": strategy_label,
                    "scheduled_analysis_date": scheduled_day.isoformat(),
                },
                allocations_data=[
                    {
                        "rank": 1,
                        "company_name": "Iberdrola",
                        "ticker": "IBE",
                        "net_projected_return_pct": 12.5,
                        "cycle_return_5y_pct": 68.0,
                        "reliability_label": "Alta",
                        "reliability_score": 82.0,
                        "cycle_yearly_margins": [
                            {"year_number": 1, "label": "AÑO 1", "margin_pct": 12.5},
                            {"year_number": 2, "label": "AÑO 2", "margin_pct": 8.0},
                            {"year_number": 3, "label": "AÑO 3", "margin_pct": 9.0},
                            {"year_number": 4, "label": "AÑO 4", "margin_pct": 10.0},
                            {"year_number": 5, "label": "AÑO 5", "margin_pct": 11.0},
                        ],
                    },
                    {
                        "rank": 3,
                        "company_name": "Endesa",
                        "ticker": "ELE",
                        "net_projected_return_pct": 9.0,
                        "cycle_return_5y_pct": 44.0,
                        "reliability_label": "Media",
                        "reliability_score": 64.0,
                        "cycle_yearly_margins": [
                            {"year_number": 1, "label": "AÑO 1", "margin_pct": 9.0},
                            {"year_number": 2, "label": "AÑO 2", "margin_pct": 6.0},
                        ],
                    },
                ],
            )
            EquityOptimizationRun.objects.filter(pk=run.pk).update(
                created_at=timezone.make_aware(datetime.combine(scheduled_day, datetime.min.time())),
                completed_at=timezone.make_aware(datetime.combine(scheduled_day, datetime.min.time())),
            )

        response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Valores que mas se repiten en optimizaciones automáticas")
        self.assertContains(response, "Iberdrola")
        self.assertContains(response, "100 %")
        self.assertContains(response, "2/2 ejec.")
        self.assertContains(response, "12M medio")
        self.assertContains(response, "Fiabilidad")
        self.assertContains(response, "A&Ntilde;O 1")
        self.assertContains(response, "5A acumulada")
        self.assertContains(response, "Max 5 empresas")

    def test_equities_page_shows_presence_percentage_in_scheduled_optimization_persistence_panel(self):
        today = timezone.localdate()
        scheduled_day = today - timedelta(days=3)
        for strategy_label, strategy_mode in (("12M principal", "12m_primary"), ("5A principal", "5y_primary")):
            run = EquityOptimizationRun.objects.create(
                reference_code=f"OPT-PERSIST-V2-{strategy_mode}",
                label=f"Programada {strategy_label}",
                total_investment=Decimal("200000"),
                max_company_pct=Decimal("30"),
                max_total_positions=5,
                max_sector_positions=2,
                status=EquityOptimizationRun.Status.COMPLETED,
                progress_data={
                    "strategy_label": strategy_label,
                    "strategy_mode": strategy_mode,
                    "schedule_kind": "nightly",
                    "scheduled_run_key": f"scheduled-optimization:{scheduled_day.isoformat()}",
                    "scheduled_analysis_date": scheduled_day.isoformat(),
                    "scheduled_weekdays_label": "martes y jueves",
                },
                summary_data={
                    "available": True,
                    "strategy_label": strategy_label,
                    "scheduled_analysis_date": scheduled_day.isoformat(),
                },
                allocations_data=[
                    {
                        "rank": 1,
                        "company_name": "Iberdrola",
                        "ticker": "IBE",
                        "net_projected_return_pct": 12.5,
                        "cycle_return_5y_pct": 68.0,
                        "reliability_label": "Alta",
                        "reliability_score": 82.0,
                        "cycle_yearly_margins": [
                            {"year_number": 1, "label": "AÃ‘O 1", "margin_pct": 12.5},
                            {"year_number": 2, "label": "AÃ‘O 2", "margin_pct": 8.0},
                            {"year_number": 3, "label": "AÃ‘O 3", "margin_pct": 9.0},
                            {"year_number": 4, "label": "AÃ‘O 4", "margin_pct": 10.0},
                            {"year_number": 5, "label": "AÃ‘O 5", "margin_pct": 11.0},
                        ],
                    },
                    {
                        "rank": 3,
                        "company_name": "Endesa",
                        "ticker": "ELE",
                        "net_projected_return_pct": 9.0,
                        "cycle_return_5y_pct": 44.0,
                        "reliability_label": "Media",
                        "reliability_score": 64.0,
                        "cycle_yearly_margins": [
                            {"year_number": 1, "label": "AÃ‘O 1", "margin_pct": 9.0},
                            {"year_number": 2, "label": "AÃ‘O 2", "margin_pct": 6.0},
                        ],
                    },
                ],
            )
            EquityOptimizationRun.objects.filter(pk=run.pk).update(
                created_at=timezone.make_aware(datetime.combine(scheduled_day, datetime.min.time())),
                completed_at=timezone.make_aware(datetime.combine(scheduled_day, datetime.min.time())),
            )

        response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Valores mas consistentes en optimizaciones automaticas")
        self.assertContains(response, "Iberdrola")
        self.assertContains(response, "100 %")
        self.assertContains(response, "2/2 ejec.")
        self.assertContains(response, "12M medio")
        self.assertContains(response, "Fiabilidad")
        self.assertContains(response, "A&Ntilde;O 1")
        self.assertContains(response, "5A acumulada")
        self.assertContains(response, "Max 5 empresas")

    def test_completed_optimization_run_can_be_deleted_from_history(self):
        run = EquityOptimizationRun.objects.create(
            reference_code="OPT-DELETE-001",
            label="Borrar historico",
            total_investment=Decimal("75000"),
            max_company_pct=Decimal("20"),
            max_total_positions=5,
            max_sector_positions=1,
            status=EquityOptimizationRun.Status.COMPLETED,
            report_html="<html>borrar</html>",
            summary_data={"available": True, "projected_gain_total": 8200, "allocations_count": 2},
        )

        response = self.client.post(
            reverse("equities:list"),
            {
                "action": "delete_optimization_run",
                "run_id": str(run.id),
            },
        )

        self.assertRedirects(response, f"{reverse('equities:list')}#equity-optimizer")
        self.assertFalse(EquityOptimizationRun.objects.filter(pk=run.pk).exists())

    def test_running_optimization_run_cannot_be_deleted_from_history(self):
        run = EquityOptimizationRun.objects.create(
            reference_code="OPT-DELETE-002",
            label="No borrar",
            total_investment=Decimal("75000"),
            max_company_pct=Decimal("20"),
            max_total_positions=5,
            max_sector_positions=1,
            status=EquityOptimizationRun.Status.RUNNING,
        )

        response = self.client.post(
            reverse("equities:list"),
            {
                "action": "delete_optimization_run",
                "run_id": str(run.id),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(EquityOptimizationRun.objects.filter(pk=run.pk).exists())
        self.assertContains(response, "Solo puedes borrar optimizaciones ya cerradas.")

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

    def test_pdf_download_falls_back_to_simplified_template_when_rich_pdf_fails(self):
        run = EquityOptimizationRun.objects.create(
            reference_code="OPT-TEST-PDF-FALLBACK",
            label="Fallback PDF",
            total_investment=Decimal("50000"),
            max_company_pct=Decimal("20"),
            max_sector_positions=1,
            status=EquityOptimizationRun.Status.COMPLETED,
            report_html="<html><body>Informe optimizado</body></html>",
            report_pdf_html="<html><body>Informe rico</body></html>",
            summary_data={"available": True, "projected_gain_total": 5000},
        )

        with (
            patch("equities.views.render_report_pdf", side_effect=[ValueError("rich failed"), b"%PDF-fallback"]),
            patch("equities.views.build_fallback_report_pdf_html", return_value="<html><body>Fallback</body></html>") as mocked_fallback,
        ):
            response = self.client.get(reverse("equities:optimization_download", args=[run.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        mocked_fallback.assert_called_once_with(run)

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

        benchmark_series = build_compound_market_series(
            "^IBEX",
            "IBEX 35",
            growth=Decimal("1.0060"),
            start_price=Decimal("100.0000"),
        )
        with (
            patch("equities.services.fetch_reference_series_for_choice", return_value=benchmark_series),
            patch("equities.views.build_equity_investment_journey_context", return_value={"available": False}),
        ):
            response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        page = response.content.decode("utf-8")
        self.assertContains(response, "Seguimiento desde")
        self.assertContains(response, "Cartera global")
        self.assertContains(response, "IBEX 35 normalizado")
        self.assertContains(response, "Acciones compradas")
        self.assertContains(response, "Resumen cartera y tablas")
        self.assertContains(response, "Prevision 12M")
        self.assertContains(response, "Ciclo 5A")
        self.assertContains(response, "Prediccion frente a realidad")
        self.assertContains(response, "Ticket IBE")
        self.assertContains(response, "Ticket ENG")
        self.assertContains(response, "Beneficio neto")
        self.assertContains(response, "Rentabilidad sobre base")
        self.assertContains(response, "Rentabilidad anualizada")
        self.assertContains(response, 'id="tracked-ticket-tab-', html=False)
        self.assertContains(response, 'aria-label="Tickets comprados"', html=False)
        self.assertContains(response, 'id="equity-portfolio-summary"', html=False)
        self.assertContains(response, 'id="equity-decision"', html=False)
        self.assertNotContains(response, 'id="equity-analysis"', html=False)
        self.assertContains(response, 'href="#tracked-ticket-', html=False)
        self.assertLess(page.index('class="equity-hero"'), page.index('id="equity-ticket-tracking"'))
        self.assertLess(page.index("Cartera global"), page.index("Acciones compradas"))
        self.assertLess(page.index('id="equity-ticket-tracking"'), page.index('id="equity-portfolio-summary"'))
        self.assertLess(page.index('id="equity-portfolio-summary"'), page.index('id="equity-decision"'))
        self.assertLess(page.index('id="equity-decision"'), page.index('id="equity-ibex"'))
        self.assertEqual(EquityTicketSnapshot.objects.count(), 2)

    def test_equities_page_tracking_shows_recommended_sale_and_reentry(self):
        position = EquityPosition.objects.create(
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2023, 1, 31),
            shares=Decimal("20.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("12.0000"),
        )
        populate_position_history(position, growth=Decimal("1.0180"), benchmark_growth=Decimal("1.0070"), months=48)
        alternative = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Seguimiento",
            ticker="ELE",
            quote_symbol="ELE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Endesa",
            shares=Decimal("0.0000"),
            average_cost_per_share=Decimal("18.0000"),
            current_price_per_share=Decimal("18.0000"),
        )
        populate_position_history(alternative, growth=Decimal("1.0220"), benchmark_growth=Decimal("1.0070"), months=48)
        EquityPurchaseForecastBaseline.objects.create(
            position=position,
            source_analysis_date=date(2026, 4, 16),
            baseline_date=date(2026, 4, 16),
            reference_label="IBEX 35",
            trade_alert_label="Comprar",
            reliability_label="Alta",
            baseline_price=Decimal("10.0000"),
            projected_price_1y=Decimal("11.2000"),
            projected_price_2y=Decimal("9.9000"),
            projected_price_3y=Decimal("11.7000"),
            projected_price_4y=Decimal("12.4000"),
            projected_price_5y=Decimal("13.2000"),
            projected_return_pct_1y=Decimal("12.00"),
            projected_return_pct_2y=Decimal("-1.00"),
            projected_return_pct_3y=Decimal("17.00"),
            projected_return_pct_4y=Decimal("24.00"),
            projected_return_pct_5y=Decimal("32.00"),
        )

        benchmark_series = build_compound_market_series(
            "^IBEX",
            "IBEX 35",
            growth=Decimal("1.0060"),
            start_price=Decimal("100.0000"),
        )
        trade_plan_payload = {
            "available": True,
            "mode": "sale_reentry",
            "analysis_basis_label": "Pendiente 5M desestacionalizada sobre la senda 5A vigente",
            "sale_month_number": 12,
            "sale_year_number": 1,
            "sale_window_label": "abril 2027 (mes 12)",
            "sale_date": date(2027, 4, 17),
            "sale_date_label": "2027-04-17",
            "reentry_month_number": 26,
            "reentry_year_number": 3,
            "reentry_window_label": "junio 2028 (mes 26)",
            "reentry_date": date(2028, 6, 17),
            "reentry_date_label": "2028-06-17",
            "summary": "La pendiente desestacionalizada de 5 meses gira a negativo y luego vuelve a positivo.",
            "signal_label": "Pendiente 5M negativa",
            "signal_value_pct": Decimal("-0.13"),
            "monthly_rows": [],
            "yearly_rows": [],
            "drawdown_month_number": 12,
            "drawdown_year_number": 1,
            "drawdown_margin_pct": Decimal("-0.13"),
            "pre_sale_return_pct": Decimal("8.00"),
        }
        with (
            patch("equities.services.fetch_reference_series_for_choice", return_value=benchmark_series),
            patch("equities.views.build_equity_investment_journey_context", return_value={"available": False}),
            patch("equities.services.build_owned_cycle_trade_timing_plan", return_value=trade_plan_payload),
        ):
            response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Salida tactica")
        self.assertContains(response, "abril 2027 (mes 12)")
        self.assertContains(response, "Reentrada tactica")
        self.assertContains(response, "junio 2028 (mes 26)")
        self.assertContains(response, "Pendiente 5M")
        self.assertContains(response, "Rotacion radar")
        self.assertContains(response, "Rotar a ELE")

    def test_equities_page_backfills_purchase_baseline_for_existing_owned_position(self):
        position = EquityPosition.objects.create(
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            opened_on=date(2025, 12, 15),
            shares=Decimal("20.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.3000"),
        )
        populate_position_history(position, growth=Decimal("1.0180"), benchmark_growth=Decimal("1.0070"), months=48)
        alternative = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Seguimiento",
            ticker="ELE",
            quote_symbol="ELE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Endesa",
            shares=Decimal("0.0000"),
            average_cost_per_share=Decimal("18.0000"),
            current_price_per_share=Decimal("18.0000"),
        )
        populate_position_history(alternative, growth=Decimal("1.0220"), benchmark_growth=Decimal("1.0070"), months=48)
        run = EquityNightlyAnalysisRun.objects.create(
            analysis_date=date(2026, 4, 17),
            status=EquityNightlyAnalysisRun.Status.COMPLETED,
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        cached_position = EquityPosition(
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            shares=Decimal("0"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("10.0000"),
        )
        card = {
            "position": cached_position,
            "reference_label": "IBEX 35",
            "projection": {
                "available": True,
                "projected_price": Decimal("11.2000"),
                "base_return_pct": Decimal("12.00"),
                "safety_score": Decimal("68.00"),
            },
            "projection_reliability": {"label": "Alta", "score": Decimal("79.00")},
            "trade_alert": {"label": "Comprar"},
            "cycle_projection_5y": {
                "available": True,
                "path": [
                    {"label": "1A", "projected_price": Decimal("11.2000")},
                    {"label": "2A", "projected_price": Decimal("9.9000")},
                    {"label": "3A", "projected_price": Decimal("11.7000")},
                    {"label": "4A", "projected_price": Decimal("12.4000")},
                    {"label": "5A", "projected_price": Decimal("13.2000")},
                ],
            },
        }
        EquityNightlyAnalysisSnapshot.objects.create(
            run=run,
            analysis_date=run.analysis_date,
            scope=EquityNightlyAnalysisSnapshot.Scope.IBEX,
            analysis_key="ibex:IBE",
            ticker="IBE",
            quote_symbol="IBE.MC",
            company_name="Iberdrola",
            status_key="ibex",
            sector_label="Utilities",
            agent_provider="core",
            analysis_payload=serialize_cached_value(card),
        )
        cards = build_equity_history_cards(list(EquityPosition.objects.prefetch_related("price_history")))
        capture_equity_ticket_snapshots(
            [item for item in cards if item["position"].ticker == "IBE"],
            snapshot_date=date(2026, 4, 17),
        )

        benchmark_series = build_compound_market_series(
            "^IBEX",
            "IBEX 35",
            growth=Decimal("1.0060"),
            start_price=Decimal("100.0000"),
        )
        trade_plan_payload = {
            "available": True,
            "mode": "sale_reentry",
            "analysis_basis_label": "Pendiente 5M desestacionalizada sobre la senda 5A vigente",
            "sale_month_number": 12,
            "sale_year_number": 1,
            "sale_window_label": "abril 2027 (mes 12)",
            "sale_date": date(2027, 4, 17),
            "sale_date_label": "2027-04-17",
            "reentry_month_number": 26,
            "reentry_year_number": 3,
            "reentry_window_label": "junio 2028 (mes 26)",
            "reentry_date": date(2028, 6, 17),
            "reentry_date_label": "2028-06-17",
            "summary": "La pendiente desestacionalizada de 5 meses gira a negativo y luego vuelve a positivo.",
            "signal_label": "Pendiente 5M negativa",
            "signal_value_pct": Decimal("-0.13"),
            "monthly_rows": [],
            "yearly_rows": [],
            "drawdown_month_number": 12,
            "drawdown_year_number": 1,
            "drawdown_margin_pct": Decimal("-0.13"),
            "pre_sale_return_pct": Decimal("8.00"),
        }
        with (
            patch("equities.services.fetch_reference_series_for_choice", return_value=benchmark_series),
            patch("equities.views.build_equity_investment_journey_context", return_value={"available": False}),
            patch("equities.services.build_owned_cycle_trade_timing_plan", return_value=trade_plan_payload),
        ):
            response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        baseline = EquityPurchaseForecastBaseline.objects.get(position=position)
        self.assertEqual(baseline.source_analysis_date, date(2026, 4, 17))
        self.assertEqual(baseline.baseline_date, date(2026, 4, 17))
        self.assertContains(response, "Salida tactica")
        self.assertContains(response, "abril 2027 (mes 12)")

    def test_equities_page_can_render_round_investment_plan(self):
        owned = EquityPosition.objects.create(
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            shares=Decimal("1000.0000"),
            average_cost_per_share=Decimal("20.0000"),
            current_price_per_share=Decimal("22.0000"),
        )
        watchlist = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Seguimiento",
            ticker="IDR",
            quote_symbol="IDR.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Indra",
            shares=Decimal("0.0000"),
            average_cost_per_share=Decimal("18.0000"),
            current_price_per_share=Decimal("18.0000"),
        )
        populate_position_history(owned, growth=Decimal("1.0180"), benchmark_growth=Decimal("1.0070"), months=48)
        populate_position_history(watchlist, growth=Decimal("1.0240"), benchmark_growth=Decimal("1.0070"), months=48)

        benchmark_series = build_compound_market_series(
            "^IBEX",
            "IBEX 35",
            growth=Decimal("1.0060"),
            start_price=Decimal("100.0000"),
        )
        with patch("equities.services.fetch_reference_series_for_choice", return_value=benchmark_series):
            response = self.client.get(
                f"{reverse('equities:list')}?round_target_total_capital=70000&round_max_round_amount=10000&round_max_company_pct=30"
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Plan por rondas")
        self.assertContains(response, "Despliegue escalonado")
        self.assertContains(response, "Paquete objetivo")
        self.assertContains(response, "2026-04-17")
        self.assertContains(response, "martes y jueves")

    def test_equities_page_keeps_watchlist_analysis_separate_from_owned_tabs(self):
        owned = EquityPosition.objects.create(
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola",
            shares=Decimal("20.0000"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("12.0000"),
        )
        watchlist = EquityPosition.objects.create(
            position_kind=EquityPosition.PositionKind.WATCHLIST,
            ownership_category=AssetOwnershipCategory.JOINT,
            broker="Interactive Brokers",
            ticker="ACS",
            quote_symbol="ACS.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="ACS",
            shares=Decimal("0.0000"),
            average_cost_per_share=Decimal("38.0000"),
            current_price_per_share=Decimal("40.0000"),
        )

        for position, company_name, start_price, growth in (
            (owned, "Iberdrola", Decimal("10.0000"), Decimal("1.0180")),
            (watchlist, "ACS", Decimal("38.0000"), Decimal("1.0150")),
        ):
            stock_series = build_compound_market_series(
                position.quote_symbol,
                company_name,
                growth=growth,
                start_price=start_price,
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

        benchmark_series = build_compound_market_series(
            "^IBEX",
            "IBEX 35",
            growth=Decimal("1.0060"),
            start_price=Decimal("100.0000"),
        )
        with patch("equities.services.fetch_reference_series_for_choice", return_value=benchmark_series):
            response = self.client.get(reverse("equities:list"))

        self.assertEqual(response.status_code, 200)
        page = response.content.decode("utf-8")
        self.assertContains(response, "Seguimientos guardados")
        self.assertContains(response, 'id="equity-analysis"', html=False)
        self.assertContains(response, 'aria-label="Seguimientos guardados"', html=False)
        self.assertContains(response, f'id="stock-tab-{watchlist.id}"', html=False)
        self.assertNotContains(response, f'id="stock-tab-{owned.id}"', html=False)
        self.assertIn(f'href="#tracked-ticket-{owned.id}"', page)

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
        self.assertContains(response, "Resultado neto comparable de las posiciones cerradas")
        self.assertContains(response, "Rentabilidad comparable por ticket abierto")

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
        self.assertContains(response, "Mes eq.")
        self.assertContains(response, "Ano eq.")
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

    @override_settings(EQUITIES_IBEX_UNIVERSE_ANALYSIS=True, EQUITIES_IBEX_UNIVERSE_LIMIT=1)
    def test_ibex_table_shows_five_year_projection_columns(self):
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
            return build_compound_market_series(symbol, symbol, growth=Decimal("1.0200"), months=120, start_price=Decimal("12.0000"))

        def fake_reference_series(reference_profile, benchmark_symbol="", benchmark_name=""):
            return build_compound_market_series(
                benchmark_symbol or "^IBEX",
                benchmark_name or "Referencia",
                growth=Decimal("1.0060"),
                months=120,
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
        self.assertContains(response, "Pred. 5 AÑOS")
        self.assertContains(response, "Márgenes por AÑO")
        self.assertContains(response, "AÑO 1")
        self.assertContains(response, "AÑO 5")

    @override_settings(EQUITIES_IBEX_UNIVERSE_ANALYSIS=True, EQUITIES_IBEX_UNIVERSE_LIMIT=1)
    def test_ibex_table_shows_buy_and_sell_recommendation_dates(self):
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

        for analysis_day, label in (
            (date(2026, 4, 14), "Vigilar"),
            (date(2026, 4, 15), "Comprar"),
            (date(2026, 4, 16), "Comprar"),
            (date(2026, 4, 17), "Vender"),
        ):
            run = EquityNightlyAnalysisRun.objects.create(
                analysis_date=analysis_day,
                status=EquityNightlyAnalysisRun.Status.COMPLETED,
                started_at=timezone.now(),
                completed_at=timezone.now(),
            )
            EquityNightlyAnalysisSnapshot.objects.create(
                run=run,
                analysis_date=analysis_day,
                scope=EquityNightlyAnalysisSnapshot.Scope.IBEX,
                analysis_key=f"ibex:ACS:{analysis_day.isoformat()}",
                ticker="ACS",
                quote_symbol="ACS.MC",
                company_name="ACS",
                status_key="ibex",
                sector_label="Construccion",
                agent_provider="core",
                analysis_payload=serialize_cached_value({"trade_alert": {"label": label}}),
            )

        def fake_market_series(symbol, range_key="10y", interval="1d"):
            return build_compound_market_series(symbol, symbol, growth=Decimal("1.0200"), months=120, start_price=Decimal("12.0000"))

        def fake_reference_series(reference_profile, benchmark_symbol="", benchmark_name=""):
            return build_compound_market_series(
                benchmark_symbol or "^IBEX",
                benchmark_name or "Referencia",
                growth=Decimal("1.0060"),
                months=120,
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
        self.assertContains(response, "Compra recom.")
        self.assertContains(response, "Venta recom.")
        self.assertContains(response, "2026-04-15")
        self.assertContains(response, "2026-04-17")

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
        self.assertContains(response, "Ciclo 5A")

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
