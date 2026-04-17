import os
import socket
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent


def parse_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_csv(value):
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_int(value, default=0):
    if value is None or value == "":
        return default
    return int(str(value).strip())


def parse_int_set(value, default=None):
    items = parse_csv(value)
    if not items:
        if default is None:
            return tuple()
        if isinstance(default, str):
            items = parse_csv(default)
        else:
            items = [str(item).strip() for item in default if str(item).strip()]
    values = []
    for item in items:
        parsed = int(str(item).strip())
        if parsed not in values:
            values.append(parsed)
    return tuple(values)


def parse_decimal(value, default="0"):
    if value is None or value == "":
        value = default
    return Decimal(str(value).strip())


def parse_path(value, default):
    if not value:
        return Path(default)
    path = Path(value)
    if path.is_absolute():
        return path
    return BASE_DIR / path


def parse_secure_proxy_ssl_header(value):
    if not value:
        return None
    parts = [item.strip() for item in str(value).split(",", 1)]
    if len(parts) != 2 or not all(parts):
        raise ValueError("APP_SECURE_PROXY_SSL_HEADER must use the format HTTP_X_FORWARDED_PROTO,https")
    return tuple(parts)


def append_unique(items, values):
    existing = set(items)
    for value in values:
        if value and value not in existing:
            items.append(value)
            existing.add(value)
    return items


def build_database_settings(base_dir: Path, env: dict[str, str] | None = None, debug: bool = True):
    if env is None:
        env = os.environ
    db_engine = str(env.get("DB_ENGINE", "") or "").strip().lower()
    sqlite_override = parse_bool(env.get("APP_ALLOW_SQLITE"), False)

    if not db_engine:
        if debug:
            db_engine = "sqlite"
        else:
            raise ImproperlyConfigured(
                "DB_ENGINE no esta configurado. Produccion requiere PostgreSQL y ya no puede arrancar en SQLite por defecto."
            )

    if db_engine in {"postgres", "postgresql"}:
        required_keys = (
            "POSTGRES_DB",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_HOST",
            "POSTGRES_PORT",
        )
        missing_keys = [key for key in required_keys if not str(env.get(key, "") or "").strip()]
        if missing_keys:
            missing_list = ", ".join(missing_keys)
            raise ImproperlyConfigured(
                f"DB_ENGINE=postgresql pero faltan variables obligatorias: {missing_list}."
            )

        return {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": str(env["POSTGRES_DB"]).strip(),
                "USER": str(env["POSTGRES_USER"]).strip(),
                "PASSWORD": str(env["POSTGRES_PASSWORD"]).strip(),
                "HOST": str(env["POSTGRES_HOST"]).strip(),
                "PORT": str(env["POSTGRES_PORT"]).strip(),
            }
        }

    if db_engine in {"sqlite", "sqlite3"}:
        if not debug and not sqlite_override:
            raise ImproperlyConfigured(
                "SQLite esta deshabilitado fuera de desarrollo. Configura PostgreSQL o usa APP_ALLOW_SQLITE=1 solo para una sesion local controlada."
            )

        sqlite_name = str(env.get("SQLITE_NAME", "") or "").strip()
        if sqlite_name:
            sqlite_path = Path(sqlite_name)
            if not sqlite_path.is_absolute():
                sqlite_path = base_dir / sqlite_path
        else:
            sqlite_path = base_dir / "db.sqlite3"

        return {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": sqlite_path,
            }
        }

    raise ImproperlyConfigured(
        f"DB_ENGINE={db_engine!r} no es valido. Usa 'postgresql' o 'sqlite'."
    )


def get_local_network_hosts():
    hosts = {"127.0.0.1", "localhost", "0.0.0.0"}
    try:
        hostname = socket.gethostname()
        hosts.add(hostname)
        local_ip = socket.gethostbyname(hostname)
        if local_ip:
            hosts.add(local_ip)
    except OSError:
        pass
    return sorted(hosts)


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-change-me-before-production")
DEBUG = parse_bool(os.environ.get("DJANGO_DEBUG"), True)
HOME_NETWORK_MODE = parse_bool(os.environ.get("APP_HOME_NETWORK_MODE"), False)

ALLOWED_HOSTS = parse_csv(os.environ.get("APP_ALLOWED_HOSTS"))
if not ALLOWED_HOSTS:
    ALLOWED_HOSTS = get_local_network_hosts() if HOME_NETWORK_MODE else ["127.0.0.1", "localhost"]
if DEBUG:
    ALLOWED_HOSTS = append_unique(ALLOWED_HOSTS, [".trycloudflare.com"])

CSRF_TRUSTED_ORIGINS = parse_csv(os.environ.get("APP_CSRF_TRUSTED_ORIGINS"))
if DEBUG:
    CSRF_TRUSTED_ORIGINS = append_unique(
        CSRF_TRUSTED_ORIGINS,
        [
            "http://127.0.0.1:8000",
            "http://localhost:8000",
            "https://*.trycloudflare.com",
        ],
    )


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "portfolio",
    "banking",
    "equities",
    "neos_additives",
    "neos_ceramica",
    "neos_materials",
    "real_estate",
]


MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "config.middleware.GlobalLoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


ROOT_URLCONF = "config.urls"


TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "config.context_processors.user_management_context",
            ],
        },
    },
]


WSGI_APPLICATION = "config.wsgi.application"


DATABASES = build_database_settings(BASE_DIR, debug=DEBUG)


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


LANGUAGE_CODE = "es-es"
TIME_ZONE = "Europe/Madrid"
USE_I18N = True
USE_TZ = True


STATIC_URL = os.environ.get("APP_STATIC_URL", "/static/")
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
STATIC_ROOT = parse_path(os.environ.get("APP_STATIC_ROOT"), BASE_DIR / "staticfiles")
MEDIA_URL = os.environ.get("APP_MEDIA_URL", "/media/")
MEDIA_ROOT = parse_path(os.environ.get("APP_MEDIA_ROOT"), BASE_DIR / "media")
APP_MEDIA_ENCRYPTION_KEY = os.environ.get("APP_MEDIA_ENCRYPTION_KEY", "").strip()
MEDIA_ENCRYPTION_ENABLED = bool(APP_MEDIA_ENCRYPTION_KEY)

STORAGES = {
    "default": {
        "BACKEND": "config.storage.EncryptedFileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "portfolio:dashboard"
LOGOUT_REDIRECT_URL = "login"

USE_X_FORWARDED_HOST = parse_bool(os.environ.get("APP_USE_X_FORWARDED_HOST"), False)
SECURE_PROXY_SSL_HEADER = parse_secure_proxy_ssl_header(os.environ.get("APP_SECURE_PROXY_SSL_HEADER"))
SECURE_SSL_REDIRECT = parse_bool(os.environ.get("APP_SECURE_SSL_REDIRECT"), False)
SESSION_COOKIE_SECURE = parse_bool(os.environ.get("APP_SESSION_COOKIE_SECURE"), not DEBUG)
CSRF_COOKIE_SECURE = parse_bool(os.environ.get("APP_CSRF_COOKIE_SECURE"), not DEBUG)
SECURE_HSTS_SECONDS = parse_int(os.environ.get("APP_SECURE_HSTS_SECONDS"), 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = parse_bool(os.environ.get("APP_SECURE_HSTS_INCLUDE_SUBDOMAINS"), False)
SECURE_HSTS_PRELOAD = parse_bool(os.environ.get("APP_SECURE_HSTS_PRELOAD"), False)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = os.environ.get("APP_SECURE_REFERRER_POLICY", "same-origin")


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

BANK_ROBOT_IMPORT_TOKEN = os.environ.get("BANK_ROBOT_IMPORT_TOKEN", "").strip()
EQUITIES_AUTO_SYNC_ON_VIEW = parse_bool(os.environ.get("APP_EQUITIES_AUTO_SYNC_ON_VIEW"), True)
EQUITIES_IBEX_UNIVERSE_ANALYSIS = parse_bool(os.environ.get("APP_EQUITIES_IBEX_UNIVERSE_ANALYSIS"), True)
EQUITIES_IBEX_UNIVERSE_LIMIT = parse_int(os.environ.get("APP_EQUITIES_IBEX_UNIVERSE_LIMIT"), 0)
EQUITIES_MARKET_REQUEST_TIMEOUT_SECONDS = parse_int(os.environ.get("APP_EQUITIES_MARKET_REQUEST_TIMEOUT_SECONDS"), 12)
EQUITIES_MARKET_DATA_CACHE_MINUTES = parse_int(os.environ.get("APP_EQUITIES_MARKET_DATA_CACHE_MINUTES"), 60)
EQUITIES_IBEX_UNIVERSE_MAX_WORKERS = parse_int(os.environ.get("APP_EQUITIES_IBEX_UNIVERSE_MAX_WORKERS"), 8)
EQUITIES_OPTIMIZATION_ASYNC = parse_bool(os.environ.get("APP_EQUITIES_OPTIMIZATION_ASYNC"), True)
EQUITIES_NIGHTLY_ANALYSIS_ENABLED = parse_bool(os.environ.get("APP_EQUITIES_NIGHTLY_ANALYSIS_ENABLED"), True)
EQUITIES_NIGHTLY_ANALYSIS_START_HOUR = parse_int(os.environ.get("APP_EQUITIES_NIGHTLY_ANALYSIS_START_HOUR"), 0)
EQUITIES_NIGHTLY_ANALYSIS_MAX_AGE_HOURS = parse_int(os.environ.get("APP_EQUITIES_NIGHTLY_ANALYSIS_MAX_AGE_HOURS"), 36)
EQUITIES_NIGHTLY_LLM_REFRESH_ISO_WEEKDAYS = parse_int_set(
    os.environ.get("APP_EQUITIES_NIGHTLY_LLM_REFRESH_ISO_WEEKDAYS"),
    (2, 4),
)
EQUITIES_SCHEDULED_OPTIMIZATION_ENABLED = parse_bool(
    os.environ.get("APP_EQUITIES_SCHEDULED_OPTIMIZATION_ENABLED"),
    True,
)
EQUITIES_SCHEDULED_OPTIMIZATION_ISO_WEEKDAYS = parse_int_set(
    os.environ.get("APP_EQUITIES_SCHEDULED_OPTIMIZATION_ISO_WEEKDAYS"),
    EQUITIES_NIGHTLY_LLM_REFRESH_ISO_WEEKDAYS or (2, 4),
)
EQUITIES_NIGHTLY_ANALYSIS_AGENT_PROVIDER = os.environ.get("APP_EQUITIES_NIGHTLY_ANALYSIS_AGENT_PROVIDER", "core").strip() or "core"
EQUITIES_NIGHTLY_ANALYSIS_AGENT_LABEL = os.environ.get("APP_EQUITIES_NIGHTLY_ANALYSIS_AGENT_LABEL", "Analista nocturno").strip() or "Analista nocturno"
AI_LLM_PROVIDER = os.environ.get("AI_LLM_PROVIDER", "anthropic").strip().lower()
AI_LLM_REQUEST_TIMEOUT_SECONDS = parse_int(os.environ.get("AI_LLM_REQUEST_TIMEOUT_SECONDS"), 45)
AI_LLM_RETRY_ATTEMPTS = parse_int(os.environ.get("AI_LLM_RETRY_ATTEMPTS"), 4)
AI_LLM_RATE_LIMIT_RETRY_SECONDS = parse_int(os.environ.get("AI_LLM_RATE_LIMIT_RETRY_SECONDS"), 15)
AI_LLM_MONTHLY_BUDGET_USD = parse_decimal(os.environ.get("AI_LLM_MONTHLY_BUDGET_USD"), "0")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CLAUDE_PRICING = {
    "claude-sonnet-4-20250514": {"input": Decimal("3.00"), "output": Decimal("15.00")},
    "claude-haiku-4-5-20251001": {"input": Decimal("0.80"), "output": Decimal("4.00")},
    "claude-opus-4-20250514": {"input": Decimal("15.00"), "output": Decimal("75.00")},
}
CLAUDE_DEFAULT_MODEL = os.environ.get("CLAUDE_DEFAULT_MODEL", "claude-sonnet-4-20250514").strip() or "claude-sonnet-4-20250514"
CLAUDE_MAX_TOKENS = parse_int(os.environ.get("CLAUDE_MAX_TOKENS"), 1024)
CLAUDE_MONTHLY_BUDGET_USD = parse_decimal(os.environ.get("CLAUDE_MONTHLY_BUDGET_USD"), "50.00")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_DEFAULT_MODEL = os.environ.get("OPENAI_DEFAULT_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
OPENAI_MAX_TOKENS = parse_int(os.environ.get("OPENAI_MAX_TOKENS"), 2048)
OPENAI_MONTHLY_BUDGET_USD = parse_decimal(
    os.environ.get("OPENAI_MONTHLY_BUDGET_USD"),
    str(AI_LLM_MONTHLY_BUDGET_USD),
)
OPENAI_PRICING = {
    "gpt-4o-mini": {"input": Decimal("0.15"), "output": Decimal("0.60")},
}
