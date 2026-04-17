from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from equities.optimization_runs import launch_scheduled_equity_optimization_runs


class Command(BaseCommand):
    help = "Lanza la pareja de optimizaciones programadas para los dias configurados."

    def add_arguments(self, parser):
        parser.add_argument(
            "--analysis-date",
            help="Fecha programada en formato YYYY-MM-DD. Por defecto usa la fecha local actual.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Lanza las optimizaciones aunque la fecha no caiga en un dia programado.",
        )
        parser.add_argument(
            "--background",
            action="store_true",
            help="Deja las optimizaciones en segundo plano. Por defecto el comando las procesa en este mismo proceso para que cron y SSH no corten el trabajo.",
        )

    def handle(self, *args, **options):
        analysis_date = None
        if options.get("analysis_date"):
            analysis_date = parse_date(options["analysis_date"])
            if analysis_date is None:
                raise CommandError("La fecha indicada no es valida. Usa YYYY-MM-DD.")

        runs = launch_scheduled_equity_optimization_runs(
            analysis_date=analysis_date,
            force=bool(options.get("force")),
            run_inline=not bool(options.get("background")),
        )
        if not runs:
            self.stdout.write(
                self.style.WARNING(
                    "No tocaba lanzar optimizaciones programadas en esta fecha."
                )
            )
            return

        analysis_label = analysis_date.isoformat() if analysis_date else str(
            runs[0].progress_data.get("scheduled_analysis_date") or ""
        ).strip()
        self.stdout.write(
            self.style.SUCCESS(
                f"Optimizaciones programadas {analysis_label or 'actuales'}: {len(runs)} ejecucion(es) completadas."
            )
        )
