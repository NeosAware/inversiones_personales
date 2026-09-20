from decimal import Decimal

from django.core.management.base import BaseCommand

from banking.models import AssetOwnershipCategory, BankBalance


# Saldos de cuentas corrientes facilitados por el usuario (extractos Sabadell).
# Se empareja una cuenta existente por los ultimos 4 digitos del IBAN (en el
# nombre de cuenta o en las notas). Si no existe, se crea. Solo actualiza el
# "saldo actual"; el capital depositado se respeta salvo que la cuenta sea nueva.
BALANCES = [
    {
        "institution": "Banco Sabadell",
        "iban_last4": "2439",
        "current_balance": Decimal("9851.81"),
    },
    {
        "institution": "Banco Sabadell",
        "iban_last4": "1236",
        "current_balance": Decimal("66177.56"),
    },
]


class Command(BaseCommand):
    help = (
        "Fija el saldo actual de las cuentas corrientes indicadas. Empareja por "
        "los ultimos 4 digitos del IBAN (nombre o notas). Usa --list para ver las "
        "cuentas existentes y --dry-run para previsualizar sin guardar."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Muestra los cambios sin guardarlos.",
        )
        parser.add_argument(
            "--list",
            action="store_true",
            help="Solo lista las cuentas corrientes existentes y termina.",
        )

    def _find_match(self, entry):
        last4 = entry["iban_last4"]
        matches = [
            balance
            for balance in BankBalance.objects.filter(
                institution__icontains=entry["institution"].split()[-1]
            )
            if last4 in (balance.account_name or "") or last4 in (balance.notes or "")
        ]
        return matches

    def handle(self, *args, **options):
        if options["list"]:
            rows = BankBalance.objects.all()
            if not rows:
                self.stdout.write("No hay cuentas registradas.")
                return
            for balance in rows:
                self.stdout.write(
                    f"[{balance.pk}] {balance.institution} · {balance.account_name}: "
                    f"saldo {balance.current_balance} | depositado {balance.deposited_amount}"
                )
            return

        dry_run = options["dry_run"]
        updated = 0
        created = 0
        for entry in BALANCES:
            matches = self._find_match(entry)
            if len(matches) > 1:
                names = ", ".join(f"[{b.pk}] {b.account_name}" for b in matches)
                self.stderr.write(
                    f"[OMITIDA] Varias cuentas coinciden con «…{entry['iban_last4']}»: {names}. "
                    "Afina el nombre/notas antes de aplicar."
                )
                continue

            if matches:
                balance = matches[0]
                self.stdout.write(
                    f"{balance.institution} · {balance.account_name}: "
                    f"saldo {balance.current_balance} -> {entry['current_balance']}"
                )
                if not dry_run:
                    balance.current_balance = entry["current_balance"]
                    balance.save(update_fields=["current_balance", "updated_at"])
                    updated += 1
            else:
                account_name = f"Cuenta corriente ...{entry['iban_last4']}"
                self.stdout.write(
                    f"[NUEVA] {entry['institution']} · {account_name}: "
                    f"saldo -> {entry['current_balance']} (depositado = saldo)"
                )
                if not dry_run:
                    BankBalance.objects.create(
                        ownership_category=AssetOwnershipCategory.JOINT,
                        institution=entry["institution"],
                        account_name=account_name,
                        deposited_amount=entry["current_balance"],
                        current_balance=entry["current_balance"],
                        notes=f"IBAN termina en {entry['iban_last4']}.",
                    )
                    created += 1

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY-RUN: no se ha guardado nada. Repite sin --dry-run para aplicar.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Actualizadas {updated} cuenta(s), creadas {created}.")
            )
