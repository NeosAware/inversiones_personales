from django.core.management.base import BaseCommand, CommandError

from banking.services import sync_open_banking_connections


class Command(BaseCommand):
    help = "Sincroniza las conexiones activas de Open Banking y actualiza cuentas/tarjetas."

    def handle(self, *args, **options):
        summary = sync_open_banking_connections()
        if summary["errors"]:
            for error in summary["errors"]:
                self.stderr.write(self.style.ERROR(error))
            raise CommandError(
                "La sincronizacion bancaria termino con errores. "
                f"Conexiones correctas: {summary['connections']}, "
                f"cuentas/tarjetas: {summary['external_accounts']}, "
                f"periodos actualizados: {summary['imported_statements']}."
            )

        self.stdout.write(
            self.style.SUCCESS(
                "Sincronizacion completada. "
                f"Conexiones: {summary['connections']}, "
                f"cuentas/tarjetas: {summary['external_accounts']}, "
                f"periodos actualizados: {summary['imported_statements']}."
            )
        )
