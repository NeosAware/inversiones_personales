from django.core.management.base import BaseCommand

from venture_studies.services import discover_web_candidates


class Command(BaseCommand):
    help = "Busca candidatos web para el radar de empresas no cotizadas."

    def add_arguments(self, parser):
        parser.add_argument("--geography", default="Castellon")
        parser.add_argument("--sector-focus", default="ceramica aditivos materiales industria")
        parser.add_argument("--max-candidates", type=int, default=8)

    def handle(self, *args, **options):
        result = discover_web_candidates(
            geography=options["geography"],
            sector_focus=options["sector_focus"],
            max_candidates=options["max_candidates"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Radar web actualizado: {result['created_count']} nuevo(s), "
                f"{result['updated_count']} revisado(s)."
            )
        )
