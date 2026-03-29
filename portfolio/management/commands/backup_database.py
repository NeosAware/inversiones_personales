import os
import subprocess
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management import BaseCommand, CommandError, call_command


class Command(BaseCommand):
    help = "Create a dated database backup for local household use."

    def add_arguments(self, parser):
        parser.add_argument("--include-media", action="store_true", help="Zip the media folder alongside the database backup.")

    def handle(self, *args, **options):
        backup_root = Path(settings.BASE_DIR) / "backups"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target_dir = backup_root / datetime.now().strftime("%Y") / datetime.now().strftime("%m")
        target_dir.mkdir(parents=True, exist_ok=True)

        database = settings.DATABASES["default"]
        engine = database["ENGINE"]

        if engine.endswith("sqlite3"):
            source = Path(database["NAME"])
            target = target_dir / f"sqlite_backup_{stamp}.sqlite3"
            shutil.copy2(source, target)
            self.stdout.write(self.style.SUCCESS(f"SQLite backup created: {target}"))
        elif engine.endswith("postgresql"):
            pg_dump = shutil.which("pg_dump")
            if pg_dump:
                target = target_dir / f"postgres_backup_{stamp}.dump"
                command = [
                    pg_dump,
                    "-h",
                    database["HOST"],
                    "-p",
                    str(database["PORT"]),
                    "-U",
                    database["USER"],
                    "-F",
                    "c",
                    "-f",
                    str(target),
                    database["NAME"],
                ]
                env = dict(os.environ)
                if database.get("PASSWORD"):
                    env["PGPASSWORD"] = database["PASSWORD"]
                result = subprocess.run(command, capture_output=True, text=True, env=env)
                if result.returncode != 0:
                    raise CommandError(result.stderr.strip() or "pg_dump failed.")
                self.stdout.write(self.style.SUCCESS(f"PostgreSQL backup created: {target}"))
            else:
                target = target_dir / f"postgres_fallback_{stamp}.json"
                with target.open("w", encoding="utf-8") as handle:
                    call_command(
                        "dumpdata",
                        "--natural-foreign",
                        "--natural-primary",
                        "--exclude",
                        "contenttypes",
                        "--exclude",
                        "auth.permission",
                        stdout=handle,
                    )
                self.stdout.write(self.style.WARNING(f"pg_dump not found. JSON fallback backup created: {target}"))
        else:
            raise CommandError(f"Unsupported database engine: {engine}")

        if options["include_media"] and Path(settings.MEDIA_ROOT).exists():
            zip_target = target_dir / f"media_backup_{stamp}.zip"
            with zipfile.ZipFile(zip_target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for file_path in Path(settings.MEDIA_ROOT).rglob("*"):
                    if file_path.is_file():
                        archive.write(file_path, file_path.relative_to(settings.MEDIA_ROOT))
            self.stdout.write(self.style.SUCCESS(f"Media backup created: {zip_target}"))
