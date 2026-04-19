from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date

from portfolio.ownership import AssetOwnershipCategory

from equities.models import EquityPosition, EquityPriceHistory, EquityPurchaseForecastBaseline
from equities.nightly_analysis import capture_purchase_forecast_baseline
from equities.services import apply_equity_company_defaults


MONICA_EQUITY_POSITIONS = (
    {
        "ticker": "SAB",
        "company_name": "Banco de Sabadell",
        "isin": "ES0113860A34",
        "shares": Decimal("12552.0000"),
        "market_price": Decimal("3.3650"),
        "market_value": Decimal("42237.48"),
    },
    {
        "ticker": "REP",
        "company_name": "Repsol",
        "isin": "ES0173516115",
        "shares": Decimal("171.0000"),
        "market_price": Decimal("19.7200"),
        "market_value": Decimal("3372.12"),
    },
    {
        "ticker": "SAN",
        "company_name": "Banco Santander",
        "isin": "ES0113900J37",
        "shares": Decimal("2550.0000"),
        "market_price": Decimal("11.0420"),
        "market_value": Decimal("28157.10"),
    },
    {
        "ticker": "ELE",
        "company_name": "Endesa",
        "isin": "ES0130670112",
        "shares": Decimal("190.0000"),
        "market_price": Decimal("36.8800"),
        "market_value": Decimal("7007.20"),
    },
    {
        "ticker": "CAM",
        "company_name": "Cuotas CAM",
        "isin": "ES0114400007",
        "shares": Decimal("305.0000"),
        "market_price": Decimal("0.0000"),
        "market_value": Decimal("0.00"),
        "quote_symbol": "",
        "note": "Sin precio de mercado informado en el documento original.",
    },
)


def build_position_note(row: dict, as_of: date) -> str:
    base_note = (
        f"Alta automatica de la cartera de Monica el {as_of.isoformat()}. "
        "Se reinicia la medicion del beneficio como si la compra se hubiera hecho hoy."
    )
    details = [
        f"ISIN: {row['isin']}",
        f"Titulos: {row['shares']}",
    ]
    if row.get("market_price") is not None:
        details.append(f"Precio base: {row['market_price']}")
    if row.get("market_value") is not None:
        details.append(f"Valoracion documento: {row['market_value']} EUR")
    if row.get("note"):
        details.append(str(row["note"]).strip())
    return base_note + " " + " | ".join(details)


class Command(BaseCommand):
    help = (
        "Importa o actualiza la cartera de Monica como posiciones compradas y reinicia el beneficio desde hoy "
        "usando el precio actual como coste base."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--as-of",
            help="Fecha de compra base en formato YYYY-MM-DD. Por defecto usa hoy.",
        )
        parser.add_argument(
            "--broker",
            default="Cartera Monica",
            help="Broker o etiqueta interna a asignar a estas posiciones.",
        )

    def handle(self, *args, **options):
        as_of = timezone.localdate()
        if options.get("as_of"):
            as_of = parse_date(options["as_of"])
            if as_of is None:
                raise CommandError("La fecha indicada no es valida. Usa YYYY-MM-DD.")

        broker = str(options.get("broker") or "").strip() or "Cartera Monica"
        created_count = 0
        updated_count = 0
        baseline_count = 0

        for row in MONICA_EQUITY_POSITIONS:
            base_data = apply_equity_company_defaults(
                {
                    "ticker": row["ticker"],
                    "company_name": row["company_name"],
                    "quote_symbol": row.get("quote_symbol", ""),
                    "reference_profile": EquityPosition.ReferenceProfile.MARKET_INDEX,
                    "benchmark_symbol": "^IBEX",
                    "benchmark_name": "IBEX 35",
                }
            )
            market_price = Decimal(str(row.get("market_price") or "0"))
            note = build_position_note(row, as_of)
            position, created = EquityPosition.objects.update_or_create(
                broker=broker,
                ticker=base_data["ticker"],
                ownership_category=AssetOwnershipCategory.MONICA,
                position_kind=EquityPosition.PositionKind.OWNED,
                defaults={
                    "company_name": base_data["company_name"],
                    "quote_symbol": base_data.get("quote_symbol", ""),
                    "reference_profile": base_data.get("reference_profile") or EquityPosition.ReferenceProfile.MARKET_INDEX,
                    "benchmark_symbol": base_data.get("benchmark_symbol") or "^IBEX",
                    "benchmark_name": base_data.get("benchmark_name") or "IBEX 35",
                    "trade_channel": EquityPosition.TradeChannel.OTHER,
                    "opened_on": as_of,
                    "shares": row["shares"],
                    "average_cost_per_share": market_price,
                    "current_price_per_share": market_price,
                    "annual_dividend_income": Decimal("0.00"),
                    "annual_maintenance_cost": Decimal("0.00"),
                    "latest_price_date": as_of if market_price > 0 else None,
                    "last_synced_at": None,
                    "notes": note,
                },
            )
            if created:
                created_count += 1
            else:
                updated_count += 1

            position.ticket_snapshots.all().delete()
            EquityPurchaseForecastBaseline.objects.filter(position=position).delete()

            if market_price > 0:
                EquityPriceHistory.objects.update_or_create(
                    position=position,
                    price_date=as_of,
                    defaults={
                        "open_price": market_price,
                        "high_price": market_price,
                        "low_price": market_price,
                        "close_price": market_price,
                    },
                )

            baseline = capture_purchase_forecast_baseline(position, baseline_date=as_of)
            if baseline is not None:
                baseline_count += 1

            self.stdout.write(
                f"{'Creada' if created else 'Actualizada'} {position.ticker} · {position.company_name} · "
                f"{position.shares} titulos · base {position.average_cost_per_share} EUR"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Cartera de Monica importada para {as_of.isoformat()}: "
                f"{created_count} creada(s), {updated_count} actualizada(s), {baseline_count} baseline(s)."
            )
        )
