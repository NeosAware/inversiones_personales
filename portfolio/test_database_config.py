from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.settings import build_database_settings, resolve_secret_key


class DatabaseSettingsTests(SimpleTestCase):
    def test_debug_defaults_to_sqlite_when_db_engine_is_missing(self):
        databases = build_database_settings(Path("/tmp/project"), env={}, debug=True)

        self.assertEqual(databases["default"]["ENGINE"], "django.db.backends.sqlite3")
        self.assertEqual(databases["default"]["NAME"], Path("/tmp/project/db.sqlite3"))

    def test_production_requires_explicit_database_engine(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "Produccion requiere PostgreSQL"):
            build_database_settings(Path("/srv/app"), env={}, debug=False)

    def test_postgres_requires_complete_configuration(self):
        env = {
            "DB_ENGINE": "postgresql",
            "POSTGRES_DB": "inversiones_personales",
            "POSTGRES_USER": "inversiones_personales",
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": "5432",
        }

        with self.assertRaisesMessage(ImproperlyConfigured, "POSTGRES_PASSWORD"):
            build_database_settings(Path("/srv/app"), env=env, debug=False)

    def test_production_rejects_sqlite_without_explicit_override(self):
        env = {"DB_ENGINE": "sqlite"}

        with self.assertRaisesMessage(ImproperlyConfigured, "SQLite esta deshabilitado"):
            build_database_settings(Path("/srv/app"), env=env, debug=False)

    def test_production_accepts_postgres_when_all_variables_are_present(self):
        env = {
            "DB_ENGINE": "postgresql",
            "POSTGRES_DB": "inversiones_personales",
            "POSTGRES_USER": "inversiones_personales",
            "POSTGRES_PASSWORD": "secreto",
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": "5432",
        }

        databases = build_database_settings(Path("/srv/app"), env=env, debug=False)

        self.assertEqual(databases["default"]["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(databases["default"]["NAME"], "inversiones_personales")
        self.assertEqual(databases["default"]["USER"], "inversiones_personales")

    def test_production_rejects_default_insecure_secret_key(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "DJANGO_SECRET_KEY propia"):
            resolve_secret_key({}, debug=False)

    def test_debug_can_use_default_secret_key_temporarily(self):
        self.assertEqual(
            resolve_secret_key({}, debug=True),
            "django-insecure-change-me-before-production",
        )
