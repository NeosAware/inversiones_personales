import tempfile
import types
from decimal import Decimal
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.utils import override_settings

from portfolio.company_valuation import (
    extract_financial_metrics_from_pages,
    read_pdf_pages,
    recalculate_company_valuations,
    save_annual_valuation,
    sync_latest_valuation_to_holding,
)

from .models import AdditivesAnnualValuation, AdditivesHolding


class AdditivesAnnualValuationTests(TestCase):
    def test_extract_financial_metrics_from_balance_text(self):
        metrics = extract_financial_metrics_from_pages(
            [
                "\n".join(
                    [
                        "A) PATRIMONIO NETO 724.366,41",
                        "I. Capital 7 69.678,00",
                        "VII. Resultado del ejercicio 7 137.322,75",
                    ]
                )
            ]
        )

        self.assertEqual(metrics["net_equity"], Decimal("724366.41"))
        self.assertEqual(metrics["share_capital"], Decimal("69678.00"))
        self.assertEqual(metrics["profit_after_tax"], Decimal("137322.75"))

    def test_extract_financial_metrics_accepts_inline_resultado_del_ejercicio_lines(self):
        metrics = extract_financial_metrics_from_pages(
            [
                "\n".join(
                    [
                        "PATRIMONIO NETO 1.234,56",
                        "1. CAPITAL ESCRITURADO 3.000,00",
                        "RESULTADO DEL EJERCICIO 250,00",
                    ]
                )
            ]
        )

        self.assertEqual(metrics["net_equity"], Decimal("1234.56"))
        self.assertEqual(metrics["share_capital"], Decimal("3000.00"))
        self.assertEqual(metrics["profit_after_tax"], Decimal("250.00"))

    def test_extract_financial_metrics_accepts_values_in_following_lines(self):
        metrics = extract_financial_metrics_from_pages(
            [
                "\n".join(
                    [
                        "PATRIMONIO NETO Y PASIVO",
                        "A) PATRIMONIO NETO",
                        "70.362,18",
                        "I. Capital",
                        "7",
                        "3.000,00",
                        "VII. Resultado del ejercicio",
                        "7",
                        "67.362,18",
                    ]
                )
            ]
        )

        self.assertEqual(metrics["net_equity"], Decimal("70362.18"))
        self.assertEqual(metrics["share_capital"], Decimal("3000.00"))
        self.assertEqual(metrics["profit_after_tax"], Decimal("67362.18"))

    def test_read_pdf_pages_rewinds_source_on_repeated_reads(self):
        class FakePdfSource:
            def __init__(self):
                self.position = 0

            def open(self, mode):
                return None

            def seek(self, offset):
                self.position = offset

            def read(self):
                if self.position != 0:
                    return b""
                self.position = 999
                return b"RESULTADO DEL EJERCICIO 25,00"

            def close(self):
                return None

        class FakePdfReader:
            def __init__(self, source):
                text = source.getvalue().decode()
                self.pages = [types.SimpleNamespace(extract_text=lambda: text)]

        fake_source = FakePdfSource()
        fake_module = types.SimpleNamespace(PdfReader=FakePdfReader)

        with patch.dict("sys.modules", {"pypdf": fake_module}):
            first_pages = read_pdf_pages(fake_source)
            second_pages = read_pdf_pages(fake_source)

        self.assertEqual(first_pages, ["RESULTADO DEL EJERCICIO 25,00"])
        self.assertEqual(second_pages, ["RESULTADO DEL EJERCICIO 25,00"])

    def test_save_annual_valuation_keeps_record_when_pdf_cannot_be_parsed(self):
        with tempfile.TemporaryDirectory() as temp_media_root:
            with override_settings(MEDIA_ROOT=temp_media_root):
                record = save_annual_valuation(
                    {
                        "year": 2026,
                        "ownership_pct": Decimal("80.00"),
                        "balance_approved": False,
                        "audited_favorable": False,
                        "balance_pdf": SimpleUploadedFile(
                            "balance.pdf",
                            b"not-a-real-pdf",
                            content_type="application/pdf",
                        ),
                        "profit_loss_pdf": None,
                        "corporate_tax_pdf": None,
                        "net_equity": None,
                        "share_capital": None,
                        "profit_after_tax": None,
                    },
                    AdditivesAnnualValuation,
                    AdditivesHolding,
                    "Neos Additives fiscal valuation stake",
                )

        self.assertEqual(AdditivesAnnualValuation.objects.count(), 1)
        self.assertTrue(record.balance_pdf.name.endswith("balance.pdf"))
        self.assertIsNone(record.net_equity)
        self.assertIsNone(record.share_capital)
        self.assertIsNone(record.profit_after_tax)

    def test_recalculate_company_valuation_uses_aeat_greater_of_three_rule(self):
        AdditivesAnnualValuation.objects.create(
            year=2023,
            ownership_pct=Decimal("80.00"),
            share_capital=Decimal("69678.00"),
            profit_after_tax=Decimal("25314.51"),
        )
        AdditivesAnnualValuation.objects.create(
            year=2024,
            ownership_pct=Decimal("80.00"),
            share_capital=Decimal("69678.00"),
            profit_after_tax=Decimal("29643.23"),
        )
        target = AdditivesAnnualValuation.objects.create(
            year=2025,
            ownership_pct=Decimal("80.00"),
            share_capital=Decimal("69678.00"),
            net_equity=Decimal("724366.41"),
            profit_after_tax=Decimal("137322.75"),
        )

        recalculate_company_valuations(AdditivesAnnualValuation)
        target.refresh_from_db()

        self.assertEqual(target.three_year_average_profit, Decimal("64093.50"))
        self.assertEqual(target.capitalised_earnings_value, Decimal("320467.50"))
        self.assertEqual(target.tax_company_value, Decimal("724366.41"))
        self.assertEqual(target.owner_value, Decimal("579493.13"))
        self.assertEqual(target.valuation_method, AdditivesAnnualValuation.ValuationMethod.THEORETICAL_VALUE)

    def test_sync_latest_valuation_creates_portfolio_position(self):
        valuation = AdditivesAnnualValuation.objects.create(
            year=2025,
            ownership_pct=Decimal("80.00"),
            share_capital=Decimal("69678.00"),
            net_equity=Decimal("724366.41"),
            profit_after_tax=Decimal("137322.75"),
        )
        recalculate_company_valuations(AdditivesAnnualValuation)

        holding = sync_latest_valuation_to_holding(
            AdditivesAnnualValuation,
            AdditivesHolding,
            "Neos Additives fiscal valuation stake",
        )
        valuation.refresh_from_db()

        self.assertIsNotNone(holding)
        self.assertEqual(AdditivesHolding.objects.count(), 1)
        self.assertEqual(holding.current_valuation, valuation.owner_value)
