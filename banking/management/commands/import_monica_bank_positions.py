from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from portfolio.ownership import AssetOwnershipCategory

from banking.models import BankInvestmentPosition


MONICA_BANK_INVESTMENT_POSITIONS = (
    {
        "institution": "Ibercaja",
        "portfolio_reference": "4414450943",
        "product_name": "Ibercaja Renta Fija 2027, FI Clase A",
        "isin": "ES0147051025",
        "risk_label": "2/7",
        "units": Decimal("11739.7653"),
        "unit_value": Decimal("6.519452"),
        "current_value": Decimal("76536.83"),
        "revaluation_amount": Decimal("2557.89"),
        "simple_return_pct": Decimal("3.46"),
        "annual_equivalent_pct": Decimal("2.87"),
        "price_date": date(2026, 4, 16),
        "document_generated_at": "2026-04-19 11:51:44",
    },
    {
        "institution": "Ibercaja",
        "portfolio_reference": "4414450943",
        "product_name": "Ibercaja RF Horizonte 2029, FI",
        "isin": "ES0147056008",
        "risk_label": "2/7",
        "units": Decimal("15734.9226"),
        "unit_value": Decimal("6.654067"),
        "current_value": Decimal("104701.22"),
        "revaluation_amount": Decimal("3236.05"),
        "simple_return_pct": Decimal("3.19"),
        "annual_equivalent_pct": Decimal("2.65"),
        "price_date": date(2026, 4, 16),
        "document_generated_at": "2026-04-19 11:51:44",
    },
)


def build_position_note(row: dict) -> str:
    details = [
        f"Referencia cartera Ibercaja: {row['portfolio_reference']}",
        f"ISIN: {row['isin']}",
        f"Riesgo: {row['risk_label']}",
        f"Participaciones: {row['units']}",
        f"Valor liquidativo: {row['unit_value']} EUR",
        f"Revalorizacion: {row['revaluation_amount']} EUR",
        f"Rentabilidad simple: {row['simple_return_pct']} %",
        f"TAE: {row['annual_equivalent_pct']} %",
        f"Documento generado el {row['document_generated_at']}",
    ]
    return " | ".join(details)


class Command(BaseCommand):
    help = "Importa o actualiza los fondos bancarios de Monica en Ibercaja."

    def add_arguments(self, parser):
        parser.add_argument(
            "--price-date",
            help="Fecha de valoracion YYYY-MM-DD. Si no se indica, se usa la del documento original.",
        )

    def handle(self, *args, **options):
        override_price_date = None
        if options.get("price_date"):
            override_price_date = parse_date(options["price_date"])
            if override_price_date is None:
                raise CommandError("La fecha indicada no es valida. Usa YYYY-MM-DD.")

        created_count = 0
        updated_count = 0

        for row in MONICA_BANK_INVESTMENT_POSITIONS:
            current_value = Decimal(str(row["current_value"]))
            revaluation_amount = Decimal(str(row["revaluation_amount"]))
            invested_amount = current_value - revaluation_amount
            price_date = override_price_date or row["price_date"]

            position, created = BankInvestmentPosition.objects.update_or_create(
                ownership_category=AssetOwnershipCategory.MONICA,
                institution=row["institution"],
                product_name=row["product_name"],
                defaults={
                    "product_type": BankInvestmentPosition.ProductType.INVESTMENT_FUND,
                    "invested_amount_override": invested_amount,
                    "current_value": current_value,
                    "units": row["units"],
                    "price_date": price_date,
                    "annual_income": Decimal("0.00"),
                    "notes": build_position_note(row),
                },
            )

            if created:
                created_count += 1
            else:
                updated_count += 1

            self.stdout.write(
                f"{'Creado' if created else 'Actualizado'} {position.product_name} · "
                f"{position.current_value} EUR · titular Monica · fecha {price_date.isoformat()}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Fondos bancarios de Monica importados: {created_count} creado(s), {updated_count} actualizado(s)."
            )
        )
