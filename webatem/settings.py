"""Django settings for WebATEM.

Everything is env-configurable with working defaults — `docker compose up`
needs no .env file. See .env.example for the knobs.
"""
import mimetypes
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# data/ — the persistent volume: SQLite DB, generated secret key, upload scratch.
DATA_DIR = Path(os.getenv('DATA_DIR', BASE_DIR / 'data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _env_bool(key, default):
    return os.getenv(key, str(default)).strip().lower() in ('1', 'true', 'yes', 'on')


def _secret_key():
    """Env var wins; otherwise generate once and persist in the data volume
    so sessions survive container recreation."""
    env = os.getenv('SECRET_KEY', '').strip()
    if env:
        return env
    keyfile = DATA_DIR / '.secret_key'
    if keyfile.exists():
        return keyfile.read_text().strip()
    key = secrets.token_urlsafe(50)
    keyfile.write_text(key)
    try:
        keyfile.chmod(0o600)
    except OSError:
        pass
    return key


SECRET_KEY = _secret_key()

DEBUG = _env_bool('DEBUG', False)

# LAN tool: default open. Restrict via env if exposed beyond the studio LAN.
ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', '*').split(',') if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()]

# WebSocket Origin checking is same-origin by default and deliberately does
# NOT follow ALLOWED_HOSTS: the '*' above must never switch off the one
# browser-side guard on the control socket (see config/websocket.py). Only
# needed when a reverse proxy rewrites Host on the way to the app.
WEBSOCKET_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv('WEBSOCKET_ALLOWED_ORIGINS', '').split(',') if o.strip()
]

# No accounts, no login: this is an open LAN tool (like the hardware panel).
# django.contrib.auth stays installed only because the app's initial
# migration was born with a (since-removed) FK into it.
INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.staticfiles',
    'channels',
    'atem_control',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'webatem.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [Path(__file__).resolve().parent / 'templates'],   # webatem/templates (base.html)
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'webatem.context.app',
            ],
        },
    },
]

ASGI_APPLICATION = 'webatem.asgi.application'

# Process-local by design: the app runs a SINGLE uvicorn worker (the ATEM
# connection pool and media-pool watcher registry are process-local too).
CHANNEL_LAYERS = {
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'},
}

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': DATA_DIR / 'db.sqlite3',
    }
}

LANGUAGE_CODE = 'en-us'
TIME_ZONE = os.getenv('TIME_ZONE', 'UTC')
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
# Where collectstatic puts the files WhiteNoise serves. The launcher points
# this into the per-user data dir (a pip install's package dir is read-only);
# the Docker image collects into /app/staticfiles at build time.
STATIC_ROOT = Path(os.getenv('STATIC_ROOT', BASE_DIR / 'staticfiles'))
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage'},
}

# Upload scratch (drag-drop / profile-restore images). Server-side only —
# there is deliberately no public /media/ URL route.
MEDIA_ROOT = DATA_DIR / 'uploads'

# Branding shown in the navbar / browser title.
# The web app manifest needs its own content type or a phone will not parse
# it (WhiteNoise sends nosniff, so a generic type is fatal). WhiteNoise 6
# keeps its OWN table — adding to the stdlib one is not enough, hence both.
mimetypes.add_type('application/manifest+json', '.webmanifest')
WHITENOISE_MIMETYPES = {'.webmanifest': 'application/manifest+json'}

APP_TITLE = os.getenv('APP_TITLE', 'webATEM')      # the wordmark's spelling: tab, home screen, installed app

# The seam for a platform hosting the ATEM control app (atem_control/hooks.py):
# a dotted path to a Hooks subclass. Unset = standalone WebATEM's defaults.
WEBATEM_HOOKS = os.getenv('WEBATEM_HOOKS') or None

# 24h vs 12h operator-facing timestamps (capture filenames etc.)
TIME_FORMAT_24HR = _env_bool('TIME_FORMAT_24HR', True)

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'default': {'format': '{asctime} {levelname} {name}: {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'default'},
    },
    'root': {'handlers': ['console'], 'level': os.getenv('LOG_LEVEL', 'INFO')},
}
