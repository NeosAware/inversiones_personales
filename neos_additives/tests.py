from decimal import Decimal

from django.test import TestCase

from portfolio.company_valuation import (
    extract_financial_metrics_from_pages,
    recalculate_company_valuations,
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
