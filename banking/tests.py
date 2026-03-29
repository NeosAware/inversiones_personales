import os
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.utils import override_settings

from .models import BankInvestmentPosition, BankMovement, BankStatementImport
from .services import build_banking_dashboard, classify_movement, parse_spanish_decimal


class BankingServicesTests(TestCase):
    def test_parse_spanish_decimal(self):
        self.assertEqual(parse_spanish_decimal("3.126,77"), Decimal("3126.77"))
        self.assertEqual(parse_spanish_decimal("-171,05"), Decimal("-171.05"))

    def test_classify_plan_contribution(self):
        classified = classify_movement(
            "APORTACION PERIODICA POL. 32000006 CERT. 95526 BS PLAN AHORRO SEMESTRAL",
            Decimal("-171.05"),
        )
        self.assertEqual(classified.group, BankMovement.MovementGroup.PENSION)
        self.assertEqual(classified.bucket, "Aportaciones a planes")

    def test_classify_dividend(self):
        classified = classify_movement("COBRO DIVIDENDO IBERDROLA", Decimal("82.30"))
        self.assertEqual(classified.group, BankMovement.MovementGroup.DIVIDEND)
        self.assertEqual(classified.bucket, "Dividendos de acciones")

    def test_classify_rent_income(self):
        classified = classify_movement("ALQUILER PISO ADRIAN", Decimal("650.00"))
        self.assertEqual(classified.group, BankMovement.MovementGroup.INCOME)
        self.assertEqual(classified.bucket, "Alquiler piso (Adrian)")

    def test_build_dashboard_groups_months_and_expenses(self):
        feb_statement = BankStatementImport.objects.create(
            source_filename="feb.xls",
            source_file="banking/statements/feb.xls",
            file_checksum="checksum-feb",
            account_label="Cuenta 1234",
            period_end="2026-02-28",
            total_income=Decimal("3126.77"),
            total_expenses=Decimal("211.90"),
            total_pension_contributions=Decimal("247.59"),
            total_dividends=Decimal("82.30"),
            closing_balance=Decimal("10234.55"),
            import_status=BankStatementImport.ImportStatus.IMPORTED,
        )
        mar_statement = BankStatementImport.objects.create(
            source_filename="mar.xls",
            source_file="banking/statements/mar.xls",
            file_checksum="checksum-mar",
            account_label="Cuenta 5678",
            period_end="2026-03-31",
            total_income=Decimal("2500.00"),
            total_expenses=Decimal("450.00"),
            total_pension_contributions=Decimal("100.00"),
            total_dividends=Decimal("0.00"),
            closing_balance=Decimal("9000.00"),
            import_status=BankStatementImport.ImportStatus.IMPORTED,
        )

        BankMovement.objects.create(
            statement_import=feb_statement,
            booking_date="2026-02-26",
            concept="NOMINA",
            normalized_concept="NOMINA",
            amount=Decimal("3126.77"),
            movement_group=BankMovement.MovementGroup.INCOME,
            concept_bucket="Nomina",
        )
        BankMovement.objects.create(
            statement_import=feb_statement,
            booking_date="2026-02-27",
            concept="COBRO DIVIDENDO IBERDROLA",
            normalized_concept="COBRO DIVIDENDO IBERDROLA",
            amount=Decimal("82.30"),
            movement_group=BankMovement.MovementGroup.DIVIDEND,
            concept_bucket="Dividendos de acciones",
        )
        BankMovement.objects.create(
            statement_import=mar_statement,
            booking_date="2026-03-05",
            concept="ALQUILER PISO ADRIAN",
            normalized_concept="ALQUILER PISO ADRIAN",
            amount=Decimal("650.00"),
            movement_group=BankMovement.MovementGroup.INCOME,
            concept_bucket="Alquiler piso (Adrian)",
        )
        BankMovement.objects.create(
            statement_import=feb_statement,
            booking_date="2026-02-23",
            concept="COMPRA TARJ. MERCADONA",
            normalized_concept="COMPRA TARJ. MERCADONA",
            amount=Decimal("-73.80"),
            movement_group=BankMovement.MovementGroup.EXPENSE,
            concept_bucket="Supermercado",
        )
        BankMovement.objects.create(
            statement_import=feb_statement,
            booking_date="2026-02-20",
            concept="NETFLIX",
            normalized_concept="NETFLIX",
            amount=Decimal("-19.99"),
            movement_group=BankMovement.MovementGroup.EXPENSE,
            concept_bucket="Suscripciones",
        )
        BankMovement.objects.create(
            statement_import=mar_statement,
            booking_date="2026-03-03",
            concept="PAGO BIZUM",
            normalized_concept="PAGO BIZUM",
            amount=Decimal("-45.00"),
            movement_group=BankMovement.MovementGroup.EXPENSE,
            concept_bucket="Bizum",
        )

        dashboard = build_banking_dashboard()

        self.assertEqual(dashboard["statement_summary"]["months_count"], 2)
        self.assertEqual(dashboard["monthly_summaries"][0]["label"], "2026-03")
        self.assertEqual(dashboard["monthly_summaries"][0]["net_cash_flow"], Decimal("1950.00"))
        self.assertEqual(dashboard["monthly_summaries"][1]["net_cash_flow"], Decimal("2749.58"))
        self.assertEqual(dashboard["income_months"], ["2026-02", "2026-03"])
        self.assertEqual(dashboard["income_matrix"][0]["concept"], "Nomina")
        self.assertEqual(dashboard["income_matrix"][0]["total"], Decimal("3126.77"))
        self.assertEqual(dashboard["income_matrix"][1]["total"], Decimal("650.00"))
        self.assertEqual(dashboard["income_matrix"][2]["total"], Decimal("82.30"))
        self.assertEqual(dashboard["expense_months"], ["2026-02", "2026-03"])
        self.assertEqual(dashboard["expense_matrix"][0]["concept"], "Supermercado")

    def test_bank_investment_position_uses_current_value_when_cost_basis_missing(self):
        position = BankInvestmentPosition.objects.create(
            institution="Banco Sabadell",
            product_name="Cuenta Ahorro 5, CIALP",
            product_type=BankInvestmentPosition.ProductType.SAVINGS_PLAN,
            current_value=Decimal("5045.51"),
        )
        self.assertEqual(position.invested_amount, Decimal("5045.51"))


class BankingImportDeletionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="banking-owner",
            password="secret123",
        )
        self.client.force_login(self.user)

    def _create_statement_with_file(self):
        uploaded_file = SimpleUploadedFile(
            "sample.xls",
            b"fake-xls-content",
            content_type="application/vnd.ms-excel",
        )
        statement = BankStatementImport.objects.create(
            source_filename="sample.xls",
            source_file=uploaded_file,
            file_checksum="checksum-sample",
            account_label="Cuenta prueba",
            period_end="2026-03-31",
            total_income=Decimal("100.00"),
            import_status=BankStatementImport.ImportStatus.IMPORTED,
        )
        BankMovement.objects.create(
            statement_import=statement,
            booking_date="2026-03-20",
            concept="NOMINA",
            normalized_concept="NOMINA",
            amount=Decimal("100.00"),
            movement_group=BankMovement.MovementGroup.INCOME,
            concept_bucket="Nomina",
        )
        return statement

    def test_delete_single_statement_removes_record_movements_and_file(self):
        with tempfile.TemporaryDirectory() as temp_media:
            with override_settings(MEDIA_ROOT=temp_media):
                statement = self._create_statement_with_file()
                file_path = statement.source_file.path

                response = self.client.post(
                    "/banking/",
                    {
                        "action": "delete_statement",
                        "statement_id": statement.id,
                    },
                )

                self.assertEqual(response.status_code, 302)
                self.assertFalse(BankStatementImport.objects.filter(pk=statement.id).exists())
                self.assertEqual(BankMovement.objects.count(), 0)
                self.assertFalse(os.path.exists(file_path))

    def test_delete_all_statements_clears_imports_and_unlocks_reimport(self):
        with tempfile.TemporaryDirectory() as temp_media:
            with override_settings(MEDIA_ROOT=temp_media):
                first = self._create_statement_with_file()
                second = BankStatementImport.objects.create(
                    source_filename="sample-2.xls",
                    source_file=SimpleUploadedFile(
                        "sample-2.xls",
                        b"fake-xls-content-2",
                        content_type="application/vnd.ms-excel",
                    ),
                    file_checksum="checksum-sample-2",
                    account_label="Cuenta prueba 2",
                    period_end="2026-02-28",
                    total_expenses=Decimal("50.00"),
                    import_status=BankStatementImport.ImportStatus.IMPORTED,
                )
                BankMovement.objects.create(
                    statement_import=second,
                    booking_date="2026-02-20",
                    concept="MERCADONA",
                    normalized_concept="MERCADONA",
                    amount=Decimal("-50.00"),
                    movement_group=BankMovement.MovementGroup.EXPENSE,
                    concept_bucket="Supermercado",
                )
                first_path = first.source_file.path
                second_path = second.source_file.path

                response = self.client.post(
                    "/banking/",
                    {"action": "delete_all_statements"},
                )

                self.assertEqual(response.status_code, 302)
                self.assertEqual(BankStatementImport.objects.count(), 0)
                self.assertEqual(BankMovement.objects.count(), 0)
                self.assertFalse(os.path.exists(first_path))
                self.assertFalse(os.path.exists(second_path))
                self.assertFalse(
                    BankStatementImport.objects.filter(file_checksum__in=["checksum-sample", "checksum-sample-2"]).exists()
                )
                replacement = BankStatementImport.objects.create(
                    source_filename="sample.xls",
                    source_file=SimpleUploadedFile(
                        "sample.xls",
                        b"fresh-xls-content",
                        content_type="application/vnd.ms-excel",
                    ),
                    file_checksum="checksum-sample",
                )
                self.assertTrue(BankStatementImport.objects.filter(pk=replacement.pk).exists())
