from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand

from equities.models import EquityPosition


# Dos posiciones reales de SpaceX (empresa privada) en dos brokers distintos, en
# EUR. SpaceX no cotiza, asi que -por decision del usuario- se usa DXYZ (Destiny
# Tech100, NASDAQ, cuya mayor posicion es SpaceX) como simbolo de cotizacion para
# que se autoactualicen. AVISO: el precio de DXYZ (~31 USD) no equivale al de las
# acciones de SpaceX (~131 EUR); la sync nocturna sobrescribira el precio actual,
# de modo que el VALOR DE MERCADO mostrado no sera el real. El coste medio (y por
# tanto el P&L) se mantiene fiel al broker.
QUOTE_SYMBOL = "DXYZ"
BENCHMARK_SYMBOL = "^IXIC"  # NASDAQ Composite (DXYZ cotiza en NASDAQ)
BENCHMARK_NAME = "NASDAQ Composite"

POSITIONS = [
    {
        "company_name": "SpaceX (Sabadell)",
        "ticker": "SPACEX",
        "broker": "Banco Sabadell",
        "shares": Decimal("90"),
        "cost": Decimal("117.6900"),
        "current": Decimal("131.5600"),
        "opened_on": date(2026, 8, 10),
    },
    {
        "company_name": "SpaceX (Santander)",
        "ticker": "SPACEX",
        "broker": "Banco Santander",
        "shares": Decimal("80"),
        "cost": Decimal("106.4200"),
        "current": Decimal("152.7100"),
        "opened_on": None,
    },
]


class Command(BaseCommand):
    help = (
        "Da de alta o actualiza las dos posiciones de SpaceX (en propiedad) usando "
        "DXYZ como simbolo de cotizacion para autoactualizar. Empareja por nombre "
        "de empresa exacto. Usa --dry-run para previsualizar sin guardar."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Muestra los cambios sin guardarlos.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        updated = 0
        created = 0
        for entry in POSITIONS:
            existing = EquityPosition.objects.filter(
                position_kind=EquityPosition.PositionKind.OWNED,
                company_name=entry["company_name"],
            ).first()

            if existing:
                self.stdout.write(
                    f"[ACTUALIZA] {entry['company_name']}: "
                    f"quote {existing.quote_symbol or '—'} -> {QUOTE_SYMBOL} | "
                    f"acc {existing.shares} -> {entry['shares']} | "
                    f"coste {existing.average_cost_per_share} -> {entry['cost']}"
                )
                if not dry_run:
                    existing.ticker = entry["ticker"]
                    existing.broker = entry["broker"]
                    existing.quote_symbol = QUOTE_SYMBOL
                    existing.benchmark_symbol = BENCHMARK_SYMBOL
                    existing.benchmark_name = BENCHMARK_NAME
                    existing.shares = entry["shares"]
                    existing.average_cost_per_share = entry["cost"]
                    existing.current_price_per_share = entry["current"]
                    existing.opened_on = entry["opened_on"]
                    existing.save(
                        update_fields=[
                            "ticker",
                            "broker",
                            "quote_symbol",
                            "benchmark_symbol",
                            "benchmark_name",
                            "shares",
                            "average_cost_per_share",
                            "current_price_per_share",
                            "opened_on",
                            "updated_at",
                        ]
                    )
                    updated += 1
            else:
                self.stdout.write(
                    f"[NUEVA] {entry['company_name']} ({entry['ticker']}·{QUOTE_SYMBOL}): "
                    f"{entry['shares']} acc @ coste {entry['cost']} | actual {entry['current']}"
                )
                if not dry_run:
                    EquityPosition.objects.create(
                        position_kind=EquityPosition.PositionKind.OWNED,
                        ticker=entry["ticker"],
                        broker=entry["broker"],
                        quote_symbol=QUOTE_SYMBOL,
                        benchmark_symbol=BENCHMARK_SYMBOL,
                        benchmark_name=BENCHMARK_NAME,
                        company_name=entry["company_name"],
                        shares=entry["shares"],
                        average_cost_per_share=entry["cost"],
                        current_price_per_share=entry["current"],
                        opened_on=entry["opened_on"],
                    )
                    created += 1

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY-RUN: no se ha guardado nada. Repite sin --dry-run para aplicar.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Actualizadas {updated}, creadas {created}. Ejecuta la sync de mercado "
                    "para traer el precio de DXYZ."
                )
            )
