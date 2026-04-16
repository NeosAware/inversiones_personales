from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date

from equities.nightly_analysis import nightly_analysis_start_hour, run_nightly_equity_analysis


class Command(BaseCommand):
    help = "Ejecuta el analisis nocturno completo de acciones y guarda snapshots reutilizables para el dia."

    def add_arguments(self, parser):
        parser.add_argument(
            "--analysis-date",
            help="Fecha contable del analisis en formato YYYY-MM-DD. Por defecto usa la fecha local actual.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Ejecuta el analisis aunque todavia no se haya alcanzado la hora nocturna configurada.",
        )

    def handle(self, *args, **options):
        analysis_date = None
        if options.get("analysis_date"):
            analysis_date = parse_date(options["analysis_date"])
            if analysis_date is None:
                raise CommandError("La fecha indicada no es valida. Usa YYYY-MM-DD.")

        run = run_equity_nightly_analysis(
            analysis_date=analysis_date,
            force=bool(options.get("force")),
        )
        if run is None:
            current_label = timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")
            self.stdout.write(
                self.style.WARNING(
                    f"Analisis nocturno no ejecutado. Esperando a partir de las {nightly_analysis_start_hour():02d}:00. Hora actual: {current_label}."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Analisis nocturno {run.analysis_date} completado con {run.snapshots.count()} snapshot(s) usando {run.agent_label} ({run.agent_provider})."
            )
        )
