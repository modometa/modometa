"""Base settings for Modometa."""

import os
from pathlib import Path

from core.formats import FORMAT_SLUGS


BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "django-insecure-modometa-dev-key-change-in-prod"
)

# Admin path prefix (empty string by default -> /admin/)
# Set DJANGO_ADMIN_PATH to e.g. "abcdefg/" for /admin/abcdefg/
_admin_path = os.environ.get("DJANGO_ADMIN_PATH", "").strip().strip("/")
DJANGO_ADMIN_PATH = f"{_admin_path}/" if _admin_path else ""

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "core.apps.CoreConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "core" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.modometa_globals",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "modometa.db",
        "OPTIONS": {
            "timeout": 30,  # 30s timeout for concurrent SQLite access
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "core" / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Site branding
SITE_NAME = "MODOMeta"

# Outbound HTTP User-Agent
USER_AGENT = os.environ.get(
    "MODOMETA_USER_AGENT", "Modometa/1.0 (https://github.com/modometa/modometa)"
)

# Modometa domain configuration
MODOMETA_FORMATS = FORMAT_SLUGS

# Formats actively displayed in navigation and homepage
# These are ordered manually here by number of decks (desc)
# That's a proxy for popularity
ACTIVE_FORMAT_SLUGS = ["modern", "pauper", "legacy", "premodern", "vintage"]

ARCHETYPES_DIR = None
DEFAULT_DECKLIST_DIR = BASE_DIR.parent / "modometa-mtgo-data" / "Tournaments" / "MTGO"
DEFAULT_COMMUNITY_DATA_DIR = BASE_DIR.parent / "modometa-community-data" / "datasources"
KNN_INDEX_PATH = BASE_DIR / "data" / "knn_index.npz"
SEARCH_INDEX_PATH = BASE_DIR / "data" / "search_index.json"
DATABASE_READ_ONLY = False
IS_TESTING = False
