"""
Channels broadcaster bridge for MediaPoolWatcher.

``media_pool.watcher`` stays Channels-free — the watcher fires events via
its observer hook (``watcher.on(event, handler)``) and has no knowledge
of Channels. This module is the glue: when a new watcher is created via
``media_pool.acquire(ip)``, the registry fires this module's
``install_broadcasters`` callback, which hooks the three watcher events
to ``channel_layer.group_send`` on the conventional per-IP group name.

The message shape matches what ``control/consumer.py``'s dispatched handlers
expect — ``{'type': 'mediapool.<event>', 'payload': <data>}``:

    mediapool_snapshot(event)       ← {'type': 'mediapool.snapshot',       ...}
    mediapool_slot_updated(event)   ← {'type': 'mediapool.slot_updated',   ...}
    mediapool_player_updated(event) ← {'type': 'mediapool.player_updated', ...}

Channels translates the dot to underscore to find the handler method.

Registration happens once at module import time — the one-line
``on_watcher_created(install_broadcasters)`` at module scope. Make sure
this module is imported before the first ``acquire()`` call —
``atem_control/apps.py`` does that during AppConfig.ready().
"""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from atem_control.media_pool.watcher import (
    MediaPoolWatcher,
    group_name,
    on_watcher_created,
)

logger = logging.getLogger(__name__)


def install_broadcasters(watcher: MediaPoolWatcher, ip_address: str) -> None:
    """Register Channels-group-send handlers for a new watcher.

    Called exactly once per watcher by ``media_pool.acquire`` via the
    on-watcher-created hook. Registers three handlers — one per event —
    each of which wraps the data in the Channels message shape and
    fans out to every consumer subscribed to the per-IP group.
    """
    group = group_name(ip_address)
    layer = get_channel_layer()
    if layer is None:
        logger.warning(
            f"media_pool_broadcast: channel layer not configured; "
            f"watcher {ip_address} events will not reach browsers"
        )
        return

    def _make_handler(msg_type: str):
        def handler(data: dict) -> None:
            try:
                async_to_sync(layer.group_send)(
                    group, {'type': msg_type, 'payload': data},
                )
            except Exception as e:
                logger.warning(
                    f"media_pool_broadcast[{ip_address}] {msg_type} "
                    f"group_send failed: {e}"
                )
        return handler

    watcher.on('snapshot', _make_handler('mediapool.snapshot'))
    watcher.on('slot_updated', _make_handler('mediapool.slot_updated'))
    watcher.on('player_updated', _make_handler('mediapool.player_updated'))
    watcher.on('pool_lock', _make_handler('mediapool.pool_lock'))
    logger.info(f"media_pool_broadcast[{ip_address}] handlers installed")


# Register at import time. Idempotent — duplicate imports don't duplicate
# the handler (on_watcher_created dedupes by callable identity).
on_watcher_created(install_broadcasters)
