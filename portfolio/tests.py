from io import StringIO
from unittest.mock import patch

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse

from banking.models import BankBalance, BankMovement, BankStatementImport
from equities.models import EquityPosition
from neos_additives.models import AdditivesHolding
from neos_ceramica.models import CeramicaHolding
from neos_materials.models import MaterialsHolding
from real_estate.models import PropertyInvestment
from .models import HouseholdAlertSettings, PlannedInvestmentPayment, PortfolioSnapshot, SalesForecastSnapshot
from .ownership import AssetOwnershipCategory
from .services import (
    build_bank_liquidity_context,
    build_overview_metrics,
    build_portfolio_dashboard,
    build_snapshot_context,
    build_svg_polyline,
    build_spending_alerts,
    capture_portfolio_snapshot,
)
from .planning import build_cashflow_management_context


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
        self.assertEqual(len(liquidity["accounts"]), 2)
        self.assertEqual(liquidity["accounts"][0]["current_balance"], Decimal("4600.00"))
        self.assertEqual(liquidity["history"][0]["closing_balance"], Decimal("4000.00"))
        self.assertEqual(liquidity["history"][1]["closing_balance"], Decimal("6600.00"))
        self.assertEqual(liquidity["history"][1]["net_cash_flow"], Decimal("2350.00"))
        self.assertTrue(liquidity["history_line"])

    def test_cashflow_management_adds_planned_investment_payments_to_simulation_only(self):
        today = timezone.localdate()
        due_date = today + timedelta(days=20)
        BankStatementImport.objects.create(
            source_filename="cash-plan.xls",
            source_file="banking/statements/cash-plan.xls",
            file_checksum="portfolio-cash-plan",
            account_label="Cuenta plan",
            period_end=today,
            closing_balance=Decimal("10000.00"),
            import_status=BankStatementImport.ImportStatus.IMPORTED,
        )
        PlannedInvestmentPayment.objects.create(
            title="Compra Iberdrola",
            due_date=due_date,
            amount=Decimal("2500.00"),
            flow_type=PlannedInvestmentPayment.FlowType.OUTFLOW,
            investment_block=PlannedInvestmentPayment.InvestmentBlock.EQUITIES,
        )

        cashflow = build_cashflow_management_context(today=today)
        plan = cashflow["investment_plan"]

        self.assertEqual(plan["summary"]["open_outflow_total"], Decimal("2500.00"))
        self.assertEqual(plan["summary"]["next_90_days_net"], Decimal("-2500.00"))
        self.assertEqual(plan["upcoming_payments"][0]["payment"].title, "Compra Iberdrola")
        self.assertEqual(cashflow["simulation_rows"][0]["investment_net"], Decimal("-2500.00"))

    def test_cashflow_management_uses_sales_forecast_to_recommend_window(self):
        today = timezone.localdate()
        forecast_month = (today.replace(day=1) + timedelta(days=35)).replace(day=1)
        SalesForecastSnapshot.objects.create(
            month=forecast_month,
            forecast_revenue=Decimal("90000.00"),
            forecast_purchase_cost=Decimal("60000.00"),
            actual_revenue=Decimal("88000.00"),
            actual_purchase_cost=Decimal("61000.00"),
        )

        cashflow = build_cashflow_management_context(today=today)

        self.assertIsNotNone(cashflow["best_window"])
        self.assertGreaterEqual(cashflow["summary"]["prudent_amount"], Decimal("0.00"))
        self.assertEqual(cashflow["real_vs_forecast"][0]["margin_deviation"], Decimal("-3000.00"))

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
        self.assertEqual(overview["banking_current_value"], Decimal("1500.00"))
        self.assertEqual(overview["liquid_cash"], Decimal("1500.00"))
        self.assertEqual(overview["neos_group_current_value"], Decimal("700.00"))
        self.assertEqual(overview["ibex_equities_current_value"], Decimal("120.00"))
        self.assertEqual(overview["other_buckets_current_value"], Decimal("0.00"))
        self.assertEqual(overview["liquid_cash_share_pct"], Decimal("64.65517241379310344827586207"))

    def test_dashboard_consolidates_neos_group_under_ceramica_holding(self):
        CeramicaHolding.objects.create(
            investment_name="Neos Ceramica fiscal valuation stake",
            invested_amount=Decimal("900.00"),
            current_valuation=Decimal("1000.00"),
            annual_dividend_income=Decimal("100.00"),
        )
        AdditivesHolding.objects.create(
            investment_name="Neos Additives fiscal valuation stake",
            invested_amount=Decimal("700.00"),
            current_valuation=Decimal("800.00"),
            annual_dividend_income=Decimal("0.00"),
        )
        MaterialsHolding.objects.create(
            investment_name="Neos Materials fiscal valuation stake",
            invested_amount=Decimal("300.00"),
            current_valuation=Decimal("333.00"),
            annual_dividend_income=Decimal("0.00"),
        )

        dashboard = build_portfolio_dashboard()
        sections = {section["title"]: section for section in dashboard["sections"]}
        owner_groups = {
            group["ownership_category"]: group for group in dashboard["owner_asset_overview"]["groups"]
        }

        self.assertEqual(dashboard["summary"]["current_value"], Decimal("1000.00"))
        self.assertEqual(dashboard["overview"]["neos_group_current_value"], Decimal("1000.00"))
        self.assertFalse(sections["Neos Ceramica"]["analysis_only"])
        self.assertTrue(sections["Neos Additives"]["analysis_only"])
        self.assertTrue(sections["Neos Materials"]["analysis_only"])
        self.assertEqual(owner_groups[AssetOwnershipCategory.XIMO]["business_current_value"], Decimal("900.00"))
        self.assertEqual(owner_groups[AssetOwnershipCategory.MONICA]["business_current_value"], Decimal("100.00"))

    def test_dashboard_does_not_double_count_manual_balance_if_account_also_has_imported_statement(self):
        BankBalance.objects.create(
            institution="Banco Sabadell",
            account_name="Cuenta 1234",
            deposited_amount=Decimal("1400.00"),
            current_balance=Decimal("1500.00"),
            annual_interest_income=Decimal("0.00"),
        )
        BankStatementImport.objects.create(
            source_filename="mar.xls",
            source_file="banking/statements/mar.xls",
            file_checksum="portfolio-dedup-mar",
            account_label="Cuenta 1234",
            period_end="2026-03-31",
            closing_balance=Decimal("1500.00"),
            import_status=BankStatementImport.ImportStatus.IMPORTED,
        )

        dashboard = build_portfolio_dashboard()

        self.assertEqual(dashboard["summary"]["current_value"], Decimal("1500.00"))

    def test_build_snapshot_context_uses_comparable_return_curve_and_keeps_full_history(self):
        for day, invested, current, total_return_pct in (
            (date(2026, 4, 1), Decimal("100000.00"), Decimal("101000.00"), Decimal("1.00")),
            (date(2026, 4, 2), Decimal("100000.00"), Decimal("101500.00"), Decimal("1.50")),
            (date(2026, 4, 3), Decimal("135000.00"), Decimal("136500.00"), Decimal("1.11")),
            (date(2026, 4, 4), Decimal("135000.00"), Decimal("136800.00"), Decimal("1.33")),
            (date(2026, 4, 5), Decimal("135000.00"), Decimal("137025.00"), Decimal("1.50")),
            (date(2026, 4, 6), Decimal("135000.00"), Decimal("137295.00"), Decimal("1.70")),
            (date(2026, 4, 7), Decimal("135000.00"), Decimal("137430.00"), Decimal("1.80")),
            (date(2026, 4, 8), Decimal("135000.00"), Decimal("137565.00"), Decimal("1.90")),
            (date(2026, 4, 9), Decimal("135000.00"), Decimal("137700.00"), Decimal("2.00")),
            (date(2026, 4, 10), Decimal("135000.00"), Decimal("137835.00"), Decimal("2.10")),
            (date(2026, 4, 11), Decimal("135000.00"), Decimal("137970.00"), Decimal("2.20")),
        ):
            PortfolioSnapshot.objects.create(
                snapshot_date=day,
                invested_amount=invested,
                current_value=current,
                annual_income=Decimal("0.00"),
                total_return_eur=current - invested,
                total_return_pct=total_return_pct,
            )

        context = build_snapshot_context()

        self.assertEqual(len(context["snapshots"]), 11)
        self.assertEqual(context["snapshots"][0].snapshot_date, date(2026, 4, 11))
        self.assertEqual(context["snapshots"][-1].snapshot_date, date(2026, 4, 1))
        self.assertEqual(
            context["snapshot_line"],
            build_svg_polyline([Decimal("1.00"), Decimal("1.50"), Decimal("1.11"), Decimal("1.33"), Decimal("1.50"), Decimal("1.70"), Decimal("1.80"), Decimal("1.90"), Decimal("2.00"), Decimal("2.10"), Decimal("2.20")]),
        )
        self.assertEqual(context["comparable_change_pp"], Decimal("0.10"))

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

        self.assertTrue(any(alert["scope"] == "Gasto mensual total" for alert in alerts))
        self.assertTrue(any(alert["scope"] == "Tarjeta de credito" for alert in alerts))

    def test_card_statements_do_not_alter_liquidity_or_account_alerts(self):
        BankStatementImport.objects.create(
            source_filename="visa-mar.xls",
            source_file="banking/statements/visa-mar.xls",
            file_checksum="portfolio-card-only-mar",
            statement_kind=BankStatementImport.StatementKind.CARD,
            account_label="Visa Monica",
            period_end="2026-03-31",
            total_expenses=Decimal("900.00"),
            import_status=BankStatementImport.ImportStatus.IMPORTED,
        )

        liquidity = build_bank_liquidity_context()
        alerts = build_spending_alerts()

        self.assertEqual(liquidity["accounts_count"], 0)
        self.assertEqual(liquidity["current_value"], Decimal("0.00"))
        self.assertEqual(alerts["alerts"], [])

    def test_dashboard_includes_snapshot_history_context(self):
        capture_portfolio_snapshot(date(2026, 3, 21))
        capture_portfolio_snapshot(date(2026, 3, 22))

        dashboard = build_portfolio_dashboard()

        self.assertGreaterEqual(dashboard["snapshot_count"], 2)
        self.assertGreaterEqual(len(dashboard["snapshots"]), 2)
        self.assertIn("bank_liquidity", dashboard)
        self.assertIn("overview", dashboard)

    def test_dashboard_builds_owner_asset_breakdown_for_banking_equities_and_real_estate(self):
        BankBalance.objects.create(
            ownership_category=AssetOwnershipCategory.XIMO,
            institution="Banco Sabadell",
            account_name="Cuenta herencia Ximo",
            deposited_amount=Decimal("10000.00"),
            current_balance=Decimal("11000.00"),
            annual_interest_income=Decimal("120.00"),
        )
        EquityPosition.objects.create(
            ownership_category=AssetOwnershipCategory.MONICA,
            broker="Banco Sabadell",
            ticker="IBE",
            quote_symbol="IBE.MC",
            benchmark_symbol="^IBEX",
            benchmark_name="IBEX 35",
            company_name="Iberdrola S.A.",
            shares=Decimal("10"),
            average_cost_per_share=Decimal("10.0000"),
            current_price_per_share=Decimal("12.0000"),
            annual_dividend_income=Decimal("35.00"),
        )
        PropertyInvestment.objects.create(
            ownership_category=AssetOwnershipCategory.MONICA,
            property_name="Pintor Oliet 13 2o",
            city="Castellon",
            invested_equity=Decimal("80000.00"),
            market_value=Decimal("110000.00"),
            mortgage_balance=Decimal("10000.00"),
            annual_rent_income=Decimal("8400.00"),
            annual_expenses=Decimal("1400.00"),
        )

        dashboard = build_portfolio_dashboard()
        owner_groups = {
            group["ownership_category"]: group for group in dashboard["owner_asset_overview"]["groups"]
        }

        self.assertEqual(owner_groups[AssetOwnershipCategory.XIMO]["banking_current_value"], Decimal("11000.00"))
        self.assertEqual(owner_groups[AssetOwnershipCategory.MONICA]["equities_current_value"], Decimal("120.0000"))
        self.assertEqual(owner_groups[AssetOwnershipCategory.MONICA]["real_estate_current_value"], Decimal("100000.00"))
        self.assertEqual(owner_groups[AssetOwnershipCategory.MONICA]["annual_income"], Decimal("7011.00"))


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
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.assertEqual(response.headers.get("Pragma"), "no-cache")

    def test_non_admin_cannot_capture_manual_portfolio_snapshot(self):
        self.client.force_login(self.user)
        self.client.get(reverse("portfolio:dashboard"))
        previous_count = PortfolioSnapshot.objects.count()

        response = self.client.post(reverse("portfolio:dashboard"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Solo un administrador puede guardar fotos manuales de cartera.")
        self.assertEqual(PortfolioSnapshot.objects.count(), previous_count)


class UserManagementTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="admin",
            password="AdminPass123!",
        )
        self.admin.is_staff = True
        self.admin.is_superuser = True
        self.admin.save()

        self.user = User.objects.create_user(
            username="household",
            password="StrongPass123!",
        )

    def test_staff_user_can_open_user_management(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("portfolio:user_management"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Gestion de accesos")
        self.assertContains(response, "Crear nuevo acceso")

    def test_non_staff_user_is_redirected_when_admin_exists(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("portfolio:user_management"))

        self.assertRedirects(response, reverse("portfolio:dashboard"))

    def test_non_staff_user_can_open_user_management_in_recovery_mode(self):
        self.admin.is_staff = False
        self.admin.is_superuser = False
        self.admin.save(update_fields=["is_staff", "is_superuser"])
        self.client.force_login(self.user)

        response = self.client.get(reverse("portfolio:user_management"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Modo recuperacion")
        self.assertContains(response, "Convertir mi usuario en administrador")

    def test_staff_user_can_create_user_from_management_page(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("portfolio:user_management"),
            {
                "action": "create_user",
                "username": "monica",
                "password1": "SeguraMonica2026!",
                "password2": "SeguraMonica2026!",
                "access_level": "user",
            },
        )

        self.assertRedirects(response, reverse("portfolio:user_management"))
        created_user = get_user_model().objects.get(username="monica")
        self.assertTrue(created_user.is_active)
        self.assertFalse(created_user.is_staff)
        self.assertFalse(created_user.is_superuser)

    def test_recovery_mode_can_promote_current_user_to_admin(self):
        self.admin.is_staff = False
        self.admin.is_superuser = False
        self.admin.save(update_fields=["is_staff", "is_superuser"])
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("portfolio:user_management"),
            {
                "action": "promote_self_to_admin",
            },
        )

        self.assertRedirects(response, reverse("portfolio:user_management"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)
        self.assertTrue(self.user.is_superuser)

    def test_last_active_admin_cannot_be_downgraded(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("portfolio:user_management"),
            {
                "action": "update_user_role",
                "user_id": self.admin.id,
                "access_level": "user",
            },
        )

        self.assertRedirects(response, reverse("portfolio:user_management"))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_staff)
        self.assertTrue(self.admin.is_superuser)


class EnsureHouseholdUserCommandTests(TestCase):
    @patch.dict("os.environ", {"HOUSEHOLD_TEST_PASSWORD": "StrongPass123!"})
    def test_command_can_read_password_from_environment(self):
        out = StringIO()

        call_command(
            "ensure_household_user",
            username="deploy-admin",
            password_env="HOUSEHOLD_TEST_PASSWORD",
            superuser=True,
            stdout=out,
        )

        user = get_user_model().objects.get(username="deploy-admin")
        self.assertTrue(user.check_password("StrongPass123!"))
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertIn("deploy-admin", out.getvalue())

    def test_command_requires_password_source(self):
        with self.assertRaisesMessage(CommandError, "Usa --password o --password-env"):
            call_command("ensure_household_user", username="deploy-admin")


class PortfolioDashboardViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="portfolio_view", password="secret123")
        self.client.force_login(self.user)

    def test_dashboard_explains_net_annual_income_column(self):
        response = self.client.get(reverse("portfolio:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Renta anual neta")
        self.assertContains(response, "intereses netos bancarios")

    def test_dashboard_uses_scrollable_history_table_and_comparable_curve_copy(self):
        PortfolioSnapshot.objects.create(
            snapshot_date=date(2026, 4, 18),
            invested_amount=Decimal("100000.00"),
            current_value=Decimal("100800.00"),
            annual_income=Decimal("0.00"),
            total_return_eur=Decimal("800.00"),
            total_return_pct=Decimal("0.80"),
        )
        PortfolioSnapshot.objects.create(
            snapshot_date=date(2026, 4, 19),
            invested_amount=Decimal("135000.00"),
            current_value=Decimal("136080.00"),
            annual_income=Decimal("0.00"),
            total_return_eur=Decimal("1080.00"),
            total_return_pct=Decimal("0.80"),
        )

        response = self.client.get(reverse("portfolio:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "La curva usa la rentabilidad total historica")
        self.assertContains(response, "Cambio comparable diario")
        self.assertContains(response, "portfolio-history-table-scroll")

    def test_staff_user_can_create_and_complete_planned_payment(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        due_date = timezone.localdate() + timedelta(days=12)

        response = self.client.post(
            reverse("portfolio:cashflow_management"),
            {
                "action": "save_planned_payment",
                "title": "Entrada en banco",
                "investment_block": PlannedInvestmentPayment.InvestmentBlock.BANKING_PRODUCT,
                "ownership_category": PlannedInvestmentPayment._meta.get_field("ownership_category").default,
                "flow_type": PlannedInvestmentPayment.FlowType.OUTFLOW,
                "due_date": due_date.isoformat(),
                "amount": "1500,00",
                "notes": "Tramo inicial",
            },
        )

        self.assertRedirects(response, reverse("portfolio:cashflow_management"))
        payment = PlannedInvestmentPayment.objects.get(title="Entrada en banco")
        self.assertEqual(payment.amount, Decimal("1500.00"))
        self.assertEqual(payment.status, PlannedInvestmentPayment.Status.PLANNED)

        response = self.client.post(
            reverse("portfolio:cashflow_management"),
            {
                "action": "mark_planned_payment_paid",
                "payment_id": payment.id,
            },
        )

        self.assertRedirects(response, reverse("portfolio:cashflow_management"))
        payment.refresh_from_db()
        self.assertEqual(payment.status, PlannedInvestmentPayment.Status.PAID)
        self.assertEqual(payment.paid_amount, Decimal("1500.00"))

    def test_staff_user_can_save_sales_forecast(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        month = timezone.localdate().replace(day=1)

        response = self.client.post(
            reverse("portfolio:cashflow_management"),
            {
                "action": "save_sales_forecast",
                "month": month.strftime("%Y-%m"),
                "forecast_units": "100",
                "forecast_average_purchase_price": "40",
                "forecast_average_sale_price": "55",
                "actual_revenue": "5200",
                "actual_purchase_cost": "4100",
                "notes": "Desde sales",
            },
        )

        self.assertRedirects(response, reverse("portfolio:cashflow_management"))
        snapshot = SalesForecastSnapshot.objects.get(month=month)
        self.assertEqual(snapshot.forecast_revenue, Decimal("5500.0000"))
        self.assertEqual(snapshot.forecast_purchase_cost, Decimal("4000.0000"))

    def test_investment_alias_opens_cashflow_for_staff_only(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        response = self.client.get(reverse("portfolio:investment"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cashflow de gestion")

    def test_non_staff_user_cannot_open_cashflow_management(self):
        response = self.client.get(reverse("portfolio:cashflow_management"))

        self.assertRedirects(response, reverse("portfolio:dashboard"))
