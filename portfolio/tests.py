from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from banking.models import BankMovement, BankStatementImport
from equities.models import EquityPosition
from neos_additives.models import AdditivesHolding
from .models import HouseholdAlertSettings, PortfolioSnapshot
from .services import (
    build_bank_liquidity_context,
    build_overview_metrics,
    build_portfolio_dashboard,
    build_spending_alerts,
    capture_portfolio_snapshot,
)


class PortfolioServicesTests(TestCase):
    def test_capture_portfolio_snapshot_stores_current_portfolio_value(self):
        EquityPosition.objects.create(
            broker="Banco Sabadell",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola S.A.",
            shares=Decimal("10"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("20.0000"),
        )
        BankStatementImport.objects.create(
            source_filename="mar.xls",
            source_file="banking/statements/mar.xls",
            file_checksum="snapshot-mar",
            account_label="Cuenta 1234",
            period_end="2026-03-31",
            closing_balance=Decimal("1500.00"),
            import_status=BankStatementImport.ImportStatus.IMPORTED,
        )

        snapshot = capture_portfolio_snapshot(date(2026, 3, 22))

        self.assertEqual(snapshot.current_value, Decimal("1700.00"))
        self.assertEqual(PortfolioSnapshot.objects.count(), 1)

    def test_build_bank_liquidity_context_uses_latest_balance_per_account_and_monthly_history(self):
        BankStatementImport.objects.create(
            source_filename="feb-1234.xls",
            source_file="banking/statements/feb-1234.xls",
            file_checksum="liq-feb-1234",
            account_label="Cuenta 1234",
            iban="ES001234",
            period_end="2026-02-28",
            closing_balance=Decimal("4000.00"),
            total_income=Decimal("3000.00"),
            total_expenses=Decimal("1200.00"),
            import_status=BankStatementImport.ImportStatus.IMPORTED,
        )
        BankStatementImport.objects.create(
            source_filename="mar-1234.xls",
            source_file="banking/statements/mar-1234.xls",
            file_checksum="liq-mar-1234",
            account_label="Cuenta 1234",
            iban="ES001234",
            period_end="2026-03-31",
            closing_balance=Decimal("4600.00"),
            total_income=Decimal("3200.00"),
            total_expenses=Decimal("1400.00"),
            total_dividends=Decimal("50.00"),
            import_status=BankStatementImport.ImportStatus.IMPORTED,
        )
        BankStatementImport.objects.create(
            source_filename="mar-5678.xls",
            source_file="banking/statements/mar-5678.xls",
            file_checksum="liq-mar-5678",
            account_label="Cuenta 5678",
            iban="ES005678",
            period_end="2026-03-31",
            closing_balance=Decimal("2000.00"),
            total_income=Decimal("800.00"),
            total_expenses=Decimal("300.00"),
            import_status=BankStatementImport.ImportStatus.IMPORTED,
        )

        liquidity = build_bank_liquidity_context()

        self.assertEqual(liquidity["accounts_count"], 2)
        self.assertEqual(liquidity["current_value"], Decimal("6600.00"))
        self.assertEqual(liquidity["latest_month"], "2026-03")
        self.assertEqual(liquidity["history"][0]["closing_balance"], Decimal("4000.00"))
        self.assertEqual(liquidity["history"][1]["closing_balance"], Decimal("6600.00"))
        self.assertEqual(liquidity["history"][1]["net_cash_flow"], Decimal("2350.00"))
        self.assertTrue(liquidity["history_line"])

    def test_build_overview_metrics_separates_neos_ibex_and_bank_liquidity(self):
        EquityPosition.objects.create(
            broker="Banco Sabadell",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola S.A.",
            shares=Decimal("10"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("12.0000"),
        )
        BankStatementImport.objects.create(
            source_filename="mar.xls",
            source_file="banking/statements/mar.xls",
            file_checksum="overview-mar",
            account_label="Cuenta 1234",
            period_end="2026-03-31",
            closing_balance=Decimal("1500.00"),
            import_status=BankStatementImport.ImportStatus.IMPORTED,
        )
        AdditivesHolding.objects.create(
            investment_name="Neos Additives fiscal valuation stake",
            invested_amount=Decimal("500.00"),
            current_valuation=Decimal("700.00"),
        )

        dashboard = build_portfolio_dashboard()
        overview = build_overview_metrics(dashboard)

        self.assertEqual(overview["total_current_value"], Decimal("2320.00"))
        self.assertEqual(overview["liquid_cash"], Decimal("1500.00"))
        self.assertEqual(overview["neos_group_current_value"], Decimal("700.00"))
        self.assertEqual(overview["ibex_equities_current_value"], Decimal("120.00"))
        self.assertEqual(overview["other_buckets_current_value"], Decimal("0.00"))
        self.assertEqual(overview["liquid_cash_share_pct"], Decimal("64.65517241379310344827586207"))

    def test_build_spending_alerts_flags_expense_spike(self):
        HouseholdAlertSettings.objects.update_or_create(
            name="default",
            defaults={
                "total_monthly_expense_limit": Decimal("500.00"),
                "concept_monthly_expense_limit": Decimal("200.00"),
                "expense_spike_threshold_pct": Decimal("20.00"),
                "lookback_months": 2,
                "active": True,
            },
        )
        feb_statement = BankStatementImport.objects.create(
            source_filename="feb.xls",
            source_file="banking/statements/feb.xls",
            file_checksum="portfolio-feb",
            account_label="Cuenta 1234",
            period_end="2026-02-28",
            total_income=Decimal("1000.00"),
            total_expenses=Decimal("300.00"),
            import_status=BankStatementImport.ImportStatus.IMPORTED,
        )
        mar_statement = BankStatementImport.objects.create(
            source_filename="mar.xls",
            source_file="banking/statements/mar.xls",
            file_checksum="portfolio-mar",
            account_label="Cuenta 1234",
            period_end="2026-03-31",
            total_income=Decimal("1000.00"),
            total_expenses=Decimal("950.00"),
            import_status=BankStatementImport.ImportStatus.IMPORTED,
        )
        BankMovement.objects.create(
            statement_import=mar_statement,
            booking_date="2026-03-10",
            concept="TARJETA CREDITO MONICA",
            normalized_concept="TARJETA CREDITO MONICA",
            amount=Decimal("-700.00"),
            movement_group=BankMovement.MovementGroup.EXPENSE,
            concept_bucket="Tarjeta de credito",
        )
        BankMovement.objects.create(
            statement_import=feb_statement,
            booking_date="2026-02-10",
            concept="TARJETA CREDITO MONICA",
            normalized_concept="TARJETA CREDITO MONICA",
            amount=Decimal("-150.00"),
            movement_group=BankMovement.MovementGroup.EXPENSE,
            concept_bucket="Tarjeta de credito",
        )

        alerts = build_spending_alerts()["alerts"]

        self.assertTrue(any(alert["scope"] == "Total monthly expenses" for alert in alerts))
        self.assertTrue(any(alert["scope"] == "Tarjeta de credito" for alert in alerts))

    def test_dashboard_includes_snapshot_history_context(self):
        capture_portfolio_snapshot(date(2026, 3, 21))
        capture_portfolio_snapshot(date(2026, 3, 22))

        dashboard = build_portfolio_dashboard()

        self.assertGreaterEqual(dashboard["snapshot_count"], 2)
        self.assertGreaterEqual(len(dashboard["snapshots"]), 2)
        self.assertIn("bank_liquidity", dashboard)
        self.assertIn("overview", dashboard)


class AccessControlTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="household",
            password="StrongPass123!",
        )

    def test_login_page_is_public(self):
        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)

    def test_dashboard_redirects_anonymous_user_to_login(self):
        response = self.client.get(reverse("portfolio:dashboard"))

        self.assertRedirects(response, f"{reverse('login')}?next={reverse('portfolio:dashboard')}")

    def test_healthcheck_is_public(self):
        response = self.client.get(reverse("healthcheck"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")

    def test_login_redirects_back_to_original_page(self):
        target_url = reverse("equities:list")
        response = self.client.post(
            f"{reverse('login')}?next={target_url}",
            {
                "username": self.user.username,
                "password": "StrongPass123!",
                "next": target_url,
            },
        )

        self.assertRedirects(response, target_url)

    def test_authenticated_user_can_open_dashboard(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("portfolio:dashboard"))

        self.assertEqual(response.status_code, 200)
