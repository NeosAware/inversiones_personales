import os
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from config.storage import ENCRYPTION_MARKER
from portfolio.ownership import AssetOwnershipCategory

from .models import BankInvestmentPosition, BankMovement, BankStatementImport
from .services import (
    build_banking_dashboard,
    classify_movement,
    import_statement,
    load_rows_from_xls,
    parse_spanish_decimal,
    parse_statement_file,
)


class BankingServicesTests(TestCase):
    def _build_statement_html(self, holder_name: str = "", iban: str = "ES12 3456 7890 1234") -> str:
        holder_row = f"<tr><td>Titular: {holder_name}</td></tr>" if holder_name else ""
        return f"""
<html>
  <body>
    <table>
      <tr><td>Cuenta: {iban}</td></tr>
      {holder_row}
      <tr><td>Divisa: EUR</td></tr>
      <tr><td>Desde 01/04/2026 hasta 05/04/2026</td></tr>
      <tr>
        <td>F. Operativa</td>
        <td>Concepto</td>
        <td>F. Valor</td>
        <td>Importe</td>
        <td>Saldo</td>
        <td>Referencia 1</td>
        <td>Referencia 2</td>
      </tr>
      <tr>
        <td>05/04/2026</td>
        <td>NOMINA ABRIL</td>
        <td>05/04/2026</td>
        <td>1.200,50</td>
        <td>3.400,75</td>
        <td>ABC</td>
        <td>DEF</td>
      </tr>
    </table>
  </body>
</html>
"""

    def test_parse_spanish_decimal(self):
        self.assertEqual(parse_spanish_decimal("3.126,77"), Decimal("3126.77"))
        self.assertEqual(parse_spanish_decimal("-171,05"), Decimal("-171.05"))
        self.assertEqual(parse_spanish_decimal("3126.77"), Decimal("3126.77"))

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
            total_expenses=Decimal("93.79"),
            total_pension_contributions=Decimal("0.00"),
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
            total_income=Decimal("650.00"),
            total_expenses=Decimal("45.00"),
            total_pension_contributions=Decimal("0.00"),
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
        self.assertEqual(dashboard["monthly_summaries"][0]["net_cash_flow"], Decimal("605.00"))
        self.assertEqual(dashboard["monthly_summaries"][1]["net_cash_flow"], Decimal("3115.28"))
        self.assertEqual(dashboard["income_months"], ["2026-02", "2026-03"])
        self.assertEqual(dashboard["income_matrix"][0]["concept"], "Nomina")
        self.assertEqual(dashboard["income_matrix"][0]["total"], Decimal("3126.77"))
        self.assertEqual(dashboard["income_matrix"][1]["total"], Decimal("650.00"))
        self.assertEqual(dashboard["income_matrix"][2]["total"], Decimal("82.30"))
        self.assertEqual(dashboard["expense_months"], ["2026-02", "2026-03"])
        self.assertEqual(dashboard["expense_matrix"][0]["concept"], "Supermercado")

    def test_build_dashboard_splits_single_multi_month_import_by_movement_month(self):
        statement = BankStatementImport.objects.create(
            source_filename="q1.xls",
            source_file="banking/statements/q1.xls",
            file_checksum="checksum-q1",
            account_label="Cuenta 1234",
            period_start="2026-01-01",
            period_end="2026-03-31",
            import_status=BankStatementImport.ImportStatus.IMPORTED,
        )

        BankMovement.objects.create(
            statement_import=statement,
            booking_date="2026-01-15",
            concept="NOMINA",
            normalized_concept="NOMINA",
            amount=Decimal("2500.00"),
            balance=Decimal("5000.00"),
            movement_group=BankMovement.MovementGroup.INCOME,
            concept_bucket="Nomina",
        )
        BankMovement.objects.create(
            statement_import=statement,
            booking_date="2026-01-20",
            concept="MERCADONA",
            normalized_concept="MERCADONA",
            amount=Decimal("-100.00"),
            balance=Decimal("4900.00"),
            movement_group=BankMovement.MovementGroup.EXPENSE,
            concept_bucket="Supermercado",
        )
        BankMovement.objects.create(
            statement_import=statement,
            booking_date="2026-02-10",
            concept="ALQUILER PISO ADRIAN",
            normalized_concept="ALQUILER PISO ADRIAN",
            amount=Decimal("650.00"),
            balance=Decimal("5550.00"),
            movement_group=BankMovement.MovementGroup.INCOME,
            concept_bucket="Alquiler piso (Adrian)",
        )
        BankMovement.objects.create(
            statement_import=statement,
            booking_date="2026-02-25",
            concept="PAGO BIZUM",
            normalized_concept="PAGO BIZUM",
            amount=Decimal("-50.00"),
            balance=Decimal("5500.00"),
            movement_group=BankMovement.MovementGroup.EXPENSE,
            concept_bucket="Bizum",
        )
        BankMovement.objects.create(
            statement_import=statement,
            booking_date="2026-03-05",
            concept="COBRO DIVIDENDO IBERDROLA",
            normalized_concept="COBRO DIVIDENDO IBERDROLA",
            amount=Decimal("82.30"),
            balance=Decimal("5582.30"),
            movement_group=BankMovement.MovementGroup.DIVIDEND,
            concept_bucket="Dividendos de acciones",
        )
        BankMovement.objects.create(
            statement_import=statement,
            booking_date="2026-03-12",
            concept="APORTACION PERIODICA POL.",
            normalized_concept="APORTACION PERIODICA POL.",
            amount=Decimal("-171.05"),
            balance=Decimal("5411.25"),
            movement_group=BankMovement.MovementGroup.PENSION,
            concept_bucket="Aportaciones a planes",
        )

        dashboard = build_banking_dashboard()

        self.assertEqual(dashboard["statement_summary"]["months_count"], 3)
        self.assertEqual(dashboard["statement_summary"]["total_income"], Decimal("3150.00"))
        self.assertEqual(dashboard["statement_summary"]["total_expenses"], Decimal("150.00"))
        self.assertEqual(dashboard["statement_summary"]["total_dividends"], Decimal("82.30"))
        self.assertEqual(dashboard["statement_summary"]["total_pension_contributions"], Decimal("171.05"))
        self.assertEqual([row["label"] for row in dashboard["monthly_summaries"]], ["2026-03", "2026-02", "2026-01"])
        self.assertEqual(dashboard["monthly_summaries"][0]["closing_balance"], Decimal("5411.25"))
        self.assertEqual(dashboard["monthly_summaries"][1]["closing_balance"], Decimal("5500.00"))
        self.assertEqual(dashboard["monthly_summaries"][2]["closing_balance"], Decimal("4900.00"))
        self.assertEqual(dashboard["income_months"], ["2026-01", "2026-02", "2026-03"])
        self.assertEqual(dashboard["income_matrix"][0]["values"], [Decimal("2500.00"), Decimal("0.00"), Decimal("0.00")])
        self.assertEqual(dashboard["income_matrix"][1]["values"], [Decimal("0.00"), Decimal("650.00"), Decimal("0.00")])
        self.assertEqual(dashboard["income_matrix"][2]["values"], [Decimal("0.00"), Decimal("0.00"), Decimal("82.30")])

    def test_bank_investment_position_uses_current_value_when_cost_basis_missing(self):
        position = BankInvestmentPosition.objects.create(
            institution="Banco Sabadell",
            product_name="Cuenta Ahorro 5, CIALP",
            product_type=BankInvestmentPosition.ProductType.SAVINGS_PLAN,
            current_value=Decimal("5045.51"),
        )
        self.assertEqual(position.invested_amount, Decimal("5045.51"))

    def test_parse_statement_file_from_html_xls_export(self):
        html = """
<html>
  <body>
    <table>
      <tr><td>Cuenta: ES12 3456 7890 1234</td></tr>
      <tr><td>Titular: Ada Lovelace</td></tr>
      <tr><td>Divisa: EUR</td></tr>
      <tr><td>Desde 01/04/2026 hasta 05/04/2026</td></tr>
      <tr>
        <td>F. Operativa</td>
        <td>Concepto</td>
        <td>F. Valor</td>
        <td>Importe</td>
        <td>Saldo</td>
        <td>Referencia 1</td>
        <td>Referencia 2</td>
      </tr>
      <tr>
        <td>05/04/2026</td>
        <td>NOMINA ABRIL</td>
        <td>05/04/2026</td>
        <td>1.200,50</td>
        <td>3.400,75</td>
        <td>ABC</td>
        <td>DEF</td>
      </tr>
      <tr>
        <td>04/04/2026</td>
        <td>MERCADONA</td>
        <td>04/04/2026</td>
        <td>-45,10</td>
        <td>2.200,25</td>
        <td></td>
        <td></td>
      </tr>
    </table>
  </body>
</html>
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            statement_path = Path(temp_dir) / "statement.xls"
            statement_path.write_text(html, encoding="utf-8")

            parsed = parse_statement_file(str(statement_path))

        self.assertEqual(parsed["metadata"]["iban"], "ES12 3456 7890 1234")
        self.assertEqual(parsed["metadata"]["holder_name"], "Ada Lovelace")
        self.assertEqual(parsed["metadata"]["period_start"].isoformat(), "2026-04-01")
        self.assertEqual(parsed["metadata"]["period_end"].isoformat(), "2026-04-05")
        self.assertEqual(parsed["metadata"]["opening_balance"], Decimal("2245.35"))
        self.assertEqual(parsed["metadata"]["closing_balance"], Decimal("3400.75"))
        self.assertEqual(len(parsed["movements"]), 2)
        self.assertEqual(parsed["movements"][0].amount, Decimal("1200.50"))
        self.assertEqual(parsed["movements"][1].amount, Decimal("-45.10"))

    def test_parse_statement_file_accepts_flexible_header_names(self):
        html = """
<html>
  <body>
    <table>
      <tr><td>IBAN: ES98 7654 3210 9876</td></tr>
      <tr><td>Titulares: Ximo y Monica</td></tr>
      <tr><td>Moneda: EUR</td></tr>
      <tr><td>Del 01/04/2026 al 05/04/2026</td></tr>
      <tr>
        <td>Fecha operacion</td>
        <td>Descripcion</td>
        <td>Fecha valor</td>
        <td>Importe EUR</td>
        <td>Saldo contable</td>
      </tr>
      <tr>
        <td>05/04/2026</td>
        <td>NOMINA ABRIL</td>
        <td>05/04/2026</td>
        <td>1.200,50</td>
        <td>3.400,75</td>
      </tr>
      <tr>
        <td>04/04/2026</td>
        <td>MERCADONA</td>
        <td>04/04/2026</td>
        <td>-45,10</td>
        <td>2.200,25</td>
      </tr>
    </table>
  </body>
</html>
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            statement_path = Path(temp_dir) / "statement-flex.xls"
            statement_path.write_text(html, encoding="utf-8")

            parsed = parse_statement_file(str(statement_path))

        self.assertEqual(parsed["metadata"]["iban"], "ES98 7654 3210 9876")
        self.assertEqual(parsed["metadata"]["holder_name"], "Ximo y Monica")
        self.assertEqual(parsed["metadata"]["period_start"].isoformat(), "2026-04-01")
        self.assertEqual(parsed["metadata"]["period_end"].isoformat(), "2026-04-05")
        self.assertEqual(parsed["movements"][0].amount, Decimal("1200.50"))
        self.assertEqual(parsed["movements"][1].amount, Decimal("-45.10"))

    def test_parse_statement_file_accepts_debit_and_credit_columns(self):
        html = """
<html>
  <body>
    <table>
      <tr><td>Cuenta: ES12 3456 7890 1234</td></tr>
      <tr><td>Titular: Monica</td></tr>
      <tr><td>Divisa: EUR</td></tr>
      <tr><td>Desde 01/04/2026 hasta 05/04/2026</td></tr>
      <tr>
        <td>Fecha</td>
        <td>Descripcion</td>
        <td>Cargo</td>
        <td>Abono</td>
        <td>Saldo</td>
      </tr>
      <tr>
        <td>05/04/2026</td>
        <td>NOMINA ABRIL</td>
        <td></td>
        <td>1.200,50</td>
        <td>3.400,75</td>
      </tr>
      <tr>
        <td>04/04/2026</td>
        <td>MERCADONA</td>
        <td>45,10</td>
        <td></td>
        <td>2.200,25</td>
      </tr>
    </table>
  </body>
</html>
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            statement_path = Path(temp_dir) / "statement-debit-credit.xls"
            statement_path.write_text(html, encoding="utf-8")

            parsed = parse_statement_file(str(statement_path))

        self.assertEqual(len(parsed["movements"]), 2)
        self.assertEqual(parsed["movements"][0].amount, Decimal("1200.50"))
        self.assertEqual(parsed["movements"][1].amount, Decimal("-45.10"))
        self.assertEqual(parsed["metadata"]["holder_name"], "Monica")

    def test_load_rows_from_xls_reads_legacy_workbook_with_xlrd(self):
        import xlrd
        from xlrd.xldate import xldate_from_date_tuple

        class FakeSheet:
            def __init__(self, values, cell_types):
                self._values = values
                self._cell_types = cell_types
                self.nrows = len(values)
                self.ncols = len(values[0])

            def cell_value(self, row_index, column_index):
                return self._values[row_index][column_index]

            def cell_type(self, row_index, column_index):
                return self._cell_types[row_index][column_index]

        class FakeWorkbook:
            datemode = 0

            def __init__(self, sheet):
                self._sheet = sheet
                self.released = False

            def sheet_by_index(self, index):
                return self._sheet

            def release_resources(self):
                self.released = True

        booking_date = xldate_from_date_tuple((2026, 4, 5), 0)
        value_date = xldate_from_date_tuple((2026, 4, 5), 0)
        values = [
            ["F. Operativa", "Concepto", "F. Valor", "Importe", "Saldo"],
            [booking_date, "NOMINA", value_date, 3126.77, 10000.55],
        ]
        cell_types = [
            [xlrd.XL_CELL_TEXT] * 5,
            [xlrd.XL_CELL_DATE, xlrd.XL_CELL_TEXT, xlrd.XL_CELL_DATE, xlrd.XL_CELL_NUMBER, xlrd.XL_CELL_NUMBER],
        ]
        workbook = FakeWorkbook(FakeSheet(values, cell_types))

        with tempfile.TemporaryDirectory() as temp_dir:
            statement_path = Path(temp_dir) / "legacy.xls"
            statement_path.write_bytes(b"legacy-xls")

            with patch("xlrd.open_workbook", return_value=workbook):
                rows = load_rows_from_xls(str(statement_path))

        self.assertEqual(
            rows,
            [
                ["F. Operativa", "Concepto", "F. Valor", "Importe", "Saldo"],
                ["05/04/2026", "NOMINA", "05/04/2026", "3126.77", "10000.55"],
            ],
        )
        self.assertTrue(workbook.released)

    def test_import_statement_infers_ownership_from_holder_name(self):
        with tempfile.TemporaryDirectory() as temp_media:
            with override_settings(MEDIA_ROOT=temp_media):
                statement = BankStatementImport.objects.create(
                    source_filename="ximo.xls",
                    source_file=SimpleUploadedFile(
                        "ximo.xls",
                        self._build_statement_html(holder_name="Ximo").encode("utf-8"),
                        content_type="application/vnd.ms-excel",
                    ),
                    file_checksum="checksum-ximo-import",
                )

                import_statement(statement)
                statement.refresh_from_db()

        self.assertEqual(statement.ownership_category, AssetOwnershipCategory.XIMO)

    def test_import_statement_reuses_existing_account_ownership(self):
        with tempfile.TemporaryDirectory() as temp_media:
            with override_settings(MEDIA_ROOT=temp_media):
                BankStatementImport.objects.create(
                    source_filename="anterior.xls",
                    source_file=SimpleUploadedFile(
                        "anterior.xls",
                        b"anterior",
                        content_type="application/vnd.ms-excel",
                    ),
                    file_checksum="checksum-anterior",
                    ownership_category=AssetOwnershipCategory.MONICA,
                    iban="ES12 3456 7890 1234",
                    account_label="Cuenta 1234",
                    import_status=BankStatementImport.ImportStatus.IMPORTED,
                )
                statement = BankStatementImport.objects.create(
                    source_filename="nuevo.xls",
                    source_file=SimpleUploadedFile(
                        "nuevo.xls",
                        self._build_statement_html(holder_name="").encode("utf-8"),
                        content_type="application/vnd.ms-excel",
                    ),
                    file_checksum="checksum-nuevo",
                )

                import_statement(statement)
                statement.refresh_from_db()

        self.assertEqual(statement.ownership_category, AssetOwnershipCategory.MONICA)


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


class EncryptedMediaTests(TestCase):
    encryption_key = "4nTlab58x8n6qwc_cJ3mt-SN5QSDkQ6L7fL2JH57UNM="

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="secure-media-user",
            password="secret123",
        )

    def _statement_html(self):
        return """
<html>
  <body>
    <table>
      <tr><td>Cuenta: ES12 3456 7890 1234</td></tr>
      <tr><td>Titular: Monica</td></tr>
      <tr><td>Divisa: EUR</td></tr>
      <tr><td>Desde 01/04/2026 hasta 05/04/2026</td></tr>
      <tr>
        <td>F. Operativa</td>
        <td>Concepto</td>
        <td>F. Valor</td>
        <td>Importe</td>
        <td>Saldo</td>
        <td>Referencia 1</td>
        <td>Referencia 2</td>
      </tr>
      <tr>
        <td>05/04/2026</td>
        <td>NOMINA ABRIL</td>
        <td>05/04/2026</td>
        <td>1.200,50</td>
        <td>3.400,75</td>
        <td>ABC</td>
        <td>DEF</td>
      </tr>
    </table>
  </body>
</html>
"""

    def test_uploaded_statement_is_stored_encrypted_and_can_still_be_imported(self):
        with tempfile.TemporaryDirectory() as temp_media:
            with override_settings(MEDIA_ROOT=temp_media, APP_MEDIA_ENCRYPTION_KEY=self.encryption_key):
                payload = self._statement_html().encode("utf-8")
                statement = BankStatementImport.objects.create(
                    source_filename="secure.xls",
                    source_file=SimpleUploadedFile(
                        "secure.xls",
                        payload,
                        content_type="application/vnd.ms-excel",
                    ),
                    file_checksum="checksum-secure",
                )

                raw_on_disk = Path(statement.source_file.path).read_bytes()
                self.assertTrue(raw_on_disk.startswith(ENCRYPTION_MARKER))
                self.assertNotIn(b"NOMINA ABRIL", raw_on_disk)

                import_statement(statement)
                statement.refresh_from_db()

        self.assertEqual(statement.import_status, BankStatementImport.ImportStatus.IMPORTED)
        self.assertEqual(statement.ownership_category, AssetOwnershipCategory.MONICA)

    def test_secure_media_download_requires_login_and_returns_decrypted_content(self):
        with tempfile.TemporaryDirectory() as temp_media:
            with override_settings(MEDIA_ROOT=temp_media, APP_MEDIA_ENCRYPTION_KEY=self.encryption_key):
                payload = self._statement_html().encode("utf-8")
                statement = BankStatementImport.objects.create(
                    source_filename="secure.xls",
                    source_file=SimpleUploadedFile(
                        "secure.xls",
                        payload,
                        content_type="application/vnd.ms-excel",
                    ),
                    file_checksum="checksum-secure-download",
                )

                self.assertTrue(statement.source_file.url.startswith("/secure-media/"))

                anonymous_response = self.client.get(statement.source_file.url)
                self.assertRedirects(
                    anonymous_response,
                    f"{reverse('login')}?next={statement.source_file.url}",
                )

                self.client.force_login(self.user)
                response = self.client.get(statement.source_file.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), payload)
