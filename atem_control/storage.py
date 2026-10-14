"""Single source of truth for on-disk file storage under ``data/``.

Layout::

    data/                   DATA_DIR (the mounted volume; also holds the
    ├── db.sqlite3              SQLite database and the generated secret key)
    ├── still-cache/        STILL_CACHE_DIR — the media pool's stills at full
    │                           size, <md5>.png, bounded by size and age
    │                           (media_pool/still_cache.py; a host may move it
    │                           with Hooks.still_cache_dir). Never web-served.
    └── uploads/            UPLOADS_DIR = MEDIA_ROOT — transient per-job
        └── <batch>/            scratch for media-pool drag-drop uploads and
                                profile-restore images. Never web-served.
"""
from pathlib import Path

from django.conf import settings

DATA_DIR = Path(settings.MEDIA_ROOT).parent
UPLOADS_DIR = Path(settings.MEDIA_ROOT)
STILL_CACHE_DIR = DATA_DIR / 'still-cache'


def ensure_dir(path) -> Path:
    """Create ``path`` (and parents) if missing; return it as a ``Path``."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path
