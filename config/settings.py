import os
import socket
from pathlib import Path


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
            ],
        },
    },
]


WSGI_APPLICATION = "config.wsgi.application"


db_engine = os.environ.get("DB_ENGINE", "sqlite").strip().lower()
if db_engine in {"postgres", "postgresql"}:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "inversiones_personales"),
            "USER": os.environ.get("POSTGRES_USER", "postgres"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


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
