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

from .models import EquityPosition
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
    build_reference_suggestions_for_equity,
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


class EquitiesServicesTests(TestCase):
    def tearDown(self):
        load_ibex_reference_workbook_snapshot.cache_clear()
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

        with patch("equities.services.fetch_market_series", side_effect=[stock_series, benchmark_series]):
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

@override_settings(EQUITIES_AUTO_SYNC_ON_VIEW=False)
class EquitiesViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="equity-owner",
            password="StrongPass123!",
        )
        self.client.force_login(self.user)

    def tearDown(self):
        load_ibex_reference_workbook_snapshot.cache_clear()
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
