from django.core.management import BaseCommand

from portfolio.services import capture_portfolio_snapshot


class Command(BaseCommand):
    help = "Capture the daily household portfolio snapshot."

    def handle(self, *args, **options):
        snapshot = capture_portfolio_snapshot()
        self.stdout.write(
            self.style.SUCCESS(
                f"Snapshot stored for {snapshot.snapshot_date}: {snapshot.current_value} EUR current value."
            )
        )

