from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand

from equities.models import EquityPosition


# Precio medio de compra y fecha de apertura facilitados por el usuario.
# Se empareja por palabra clave del nombre de empresa (mas robusto que el ticker)
# y solo sobre posiciones en propiedad.
PURCHASE_DATA = [
    {"keyword": "Enag", "label": "Enagas", "cost": Decimal("6.5000"), "opened_on": date(2002, 6, 25)},
    {"keyword": "Iberdrola", "label": "Iberdrola", "cost": Decimal("10.0700"), "opened_on": date(2012, 7, 27)},
    {"keyword": "Santander", "label": "Banco Santander", "cost": Decimal("4.5800"), "opened_on": date(2011, 11, 2)},
    {"keyword": "Endesa", "label": "Endesa", "cost": Decimal("12.9700"), "opened_on": date(1998, 10, 21)},
    {"keyword": "Repsol", "label": "Repsol", "cost": Decimal("8.0010"), "opened_on": date(2012, 7, 13)},
]


class Command(BaseCommand):
    help = (
        "Fija el precio medio de compra y la fecha de apertura de las posiciones "
        "en propiedad indicadas. Muestra cada cambio (antes -> despues); usa "
        "--dry-run para previsualizar sin guardar."
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
        for entry in PURCHASE_DATA:
            matches = list(
                EquityPosition.objects.filter(
                    position_kind=EquityPosition.PositionKind.OWNED,
                    company_name__icontains=entry["keyword"],
                )
            )
            if not matches:
                self.stderr.write(
                    f"[OMITIDA] Sin posicion en propiedad que coincida con «{entry['label']}» "
                    f"(clave: {entry['keyword']})."
                )
                continue
            if len(matches) > 1:
                names = ", ".join(f"{p.ticker}·{p.company_name}" for p in matches)
                self.stderr.write(
                    f"[OMITIDA] Varias posiciones coinciden con «{entry['label']}»: {names}. "
                    "Afina el criterio antes de aplicar."
                )
                continue

            position = matches[0]
            self.stdout.write(
                f"{position.ticker} · {position.company_name}: "
                f"coste {position.average_cost_per_share} -> {entry['cost']} | "
                f"apertura {position.opened_on} -> {entry['opened_on']}"
            )
            if not dry_run:
                position.average_cost_per_share = entry["cost"]
                position.opened_on = entry["opened_on"]
                position.save(update_fields=["average_cost_per_share", "opened_on", "updated_at"])
                updated += 1

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY-RUN: no se ha guardado nada. Repite sin --dry-run para aplicar.")
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"Actualizadas {updated} posicion(es)."))
