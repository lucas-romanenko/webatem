"""``media_pool`` sub-package — MediaPoolWatcher + Channels broadcaster.

Sub-modules:
    watcher.py   — ``MediaPoolWatcher`` + module-level registry
    broadcast.py — Channels group_send bridge for watcher events

Re-exports below let callers use the sub-package as a flat namespace:
``from atem_control.media_pool import group_name`` resolves
without needing to know it lives in ``watcher.py``.
"""

from atem_control.media_pool.watcher import (  # noqa: F401
    # The class
    MediaPoolWatcher,
    # Registry / lifecycle
    acquire,
    release,
    on_watcher_created,
    get_snapshot,
    note_upload_started,
    note_upload_finished,
    seed_uploaded_thumb,
    requeue_missing,
    group_name,
    # Constants used by callers
    MAX_STILL_SLOTS,
    MAX_MEDIA_PLAYERS,
    THUMBNAIL_WIDTH,
    JPEG_QUALITY,
    BIND_POLL_INTERVAL,
    RECONNECT_BACKOFF,
    WATCHER_GRACE_PERIOD,
    UPLOAD_LOCKOUT_SECONDS,
    DOWNLOAD_TIMEOUT,
    EMPTY_HASH_HEX,
)
