"""Profile save (export) — domain function behind /atem/profile/save/.

``export_profile_zip`` opens an ATEM connection, downloads the profile
XML via ``pyatem.profile.Profile.from_atem``, optionally captures every
populated media-pool slot, and packages the result into a ZIP (or a
bare XML download when there are no images to ship).

The HTTP wrapper in ``profile/views.py`` does request parsing, builds a
``progress_callback`` that fans out to the per-IP Channels group, calls
this function, and renders the response. No Django objects cross this
boundary.

Watcher quiesce: see the comment inside ``export_profile_zip`` —
``note_upload_started`` / ``note_upload_finished`` bracket the entire
ATEM session, not just the media-pool capture. Macro bytecode download
inside ``Profile.from_atem`` uses the same ATEM file-transfer mechanism
the watcher's thumbnail downloads contend on.
"""

import io
import logging
import threading
import time
import zipfile
from typing import Callable, Optional

from django.conf import settings
from django.utils import timezone

from pyatem import ATEM, Profile
from pyatem.profile import SaveOptions, download_media_pool_images
from pyatem.messages.media import mediaplayer_slot_info

from atem_control.media_pool import watcher as _media_pool_service
from atem_control.profile.dialog import (
    me_options_from_sections, section_value,
)

logger = logging.getLogger(__name__)


# L10 (2026-07-07): cross-request cancel registry for the media-pool
# capture phase. The save runs in one long-lived POST; the cancel arrives
# on a SEPARATE POST (/profile/save/cancel/) carrying the same
# X-Capture-Session id. We record cancelled session ids here; the save's
# per-slot ``cancel_check`` (already supported by
# ``download_media_pool_images``) polls it and stops at the next slot
# boundary. Bounded: ids are discarded when their save consumes them, and
# a size cap drops the oldest on the rare abandoned-cancel path.
_cancelled_sessions: "collections.OrderedDict[str, float]" = None
_cancel_lock = threading.Lock()
_CANCEL_MAX_ENTRIES = 256


def request_capture_cancel(session_id: str) -> bool:
    """Mark a save session for cancellation. Returns True if recorded.
    No-op for an empty id (the legacy GET path has no session)."""
    import collections
    global _cancelled_sessions
    if not session_id:
        return False
    with _cancel_lock:
        if _cancelled_sessions is None:
            _cancelled_sessions = collections.OrderedDict()
        _cancelled_sessions[session_id] = time.monotonic()
        while len(_cancelled_sessions) > _CANCEL_MAX_ENTRIES:
            _cancelled_sessions.popitem(last=False)
    return True


def _is_capture_cancelled(session_id: str) -> bool:
    if not session_id:
        return False
    with _cancel_lock:
        return bool(_cancelled_sessions and session_id in _cancelled_sessions)


def _clear_capture_cancel(session_id: str) -> None:
    if not session_id:
        return
    with _cancel_lock:
        if _cancelled_sessions:
            _cancelled_sessions.pop(session_id, None)


# Read once at module load (matches the convention used in
# the drag-drop uploader). True (24-hour) is the
# fallback when settings doesn't have the attribute.
_PROFILE_SAVE_USE_24HR = getattr(settings, 'TIME_FORMAT_24HR', True)


# NOTE (2026-07-02): the historical 5 s pre-session settle is GONE. It
# existed because the watcher used to DROP its socket on
# ``note_upload_started`` (abandoning a session the ATEM took ~5 s to
# time out, which made our first PLCK bounce with FTDE code 1). The
# watcher now keeps its connection up and merely defers its own
# downloads while the lockout is held — there is no dead session to
# settle around, and the save's downloads are native interleaved
# transfers on the pooled connection anyway.


class ATEMConnectError(RuntimeError):
    """Connection to the ATEM at the given IP failed.

    Raised by ``export_profile_zip`` so the HTTP wrapper can render 503
    without needing knowledge of pyatem internals. The IP is in the
    message for the caller to forward.
    """


# ---------------------------------------------------------------------------
# Section-selection JSON → SaveOptions
# ---------------------------------------------------------------------------

def build_save_options(sections: dict) -> SaveOptions:
    """Translate save-side section-selection JSON → SaveOptions.

    Per-M/E cells arrive me-namespaced (``me<N>_program`` …
    ``me<N>_usk_<k>``) — one set per M/E tab the save descriptor made
    available, i.e. one per real M/E on the connected switcher — and
    map into ``options.mes[N]`` via
    ``dialog.me_options_from_sections`` (the one place the namespacing
    is parsed, shared with the load view). ``mes`` is therefore sized
    to the real me_count, not the PLATFORM_MAX_MES default. Globals
    stay flat — their ids match the SaveOptions field names 1:1.
    """
    return SaveOptions(
        # Per-M/E grids → mes[N].
        mes=me_options_from_sections(
            sections, program_default=True, preview_default=True),
        # Switcher-global.
        downstream_keys=section_value(sections, 'downstream_keys', True),
        color_generators=section_value(sections, 'color_generators', True),
        fairlight=section_value(sections, 'fairlight', True),
        camera_control=section_value(sections, 'camera_control', True),
        # Media. The dialog has ONE Media Pool cell (ASC-style); it
        # gates both the <MediaPool> slot listing and the image capture.
        media_pool_metadata=section_value(sections, 'media_pool', True),
        media_pool_images=section_value(sections, 'media_pool', True),
        media_players=section_value(sections, 'media_players', True),
        # I/O.
        auxiliaries=section_value(sections, 'auxiliaries', True),
        counters=section_value(sections, 'counters', True),
        # Others.
        settings=section_value(sections, 'settings', True),
        video_mode=section_value(sections, 'video_mode', True),
        hyperdecks=section_value(sections, 'hyperdecks', True),
        macros=section_value(sections, 'macros', True),
    )


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def _slug_filename(name: str) -> str:
    """Sanitize a string for use in a download filename."""
    safe = ''.join(c if c.isalnum() or c in ('-', '_') else '_' for c in name)
    return safe.strip('_') or 'atem'


def _format_save_timestamp() -> str:
    """Filename-safe timestamp respecting TIME_FORMAT_24HR.

    24-hour: ``2026-04-29_14-30-15``
    12-hour: ``2026-04-29_2-30-15-PM`` (no leading zero on hour, dash
             before AM/PM so the whole string stays filesystem-safe).
    ``%-I`` strips the leading zero on the hour and is GNU strftime —
    fine on Linux/Docker; would need a manual conversion if this ever
    runs on Windows.
    """
    fmt = ('%Y-%m-%d_%H-%M-%S' if _PROFILE_SAVE_USE_24HR
           else '%Y-%m-%d_%-I-%M-%S-%p')
    # Studio wall time (settings.TIME_ZONE), not the container clock —
    # the containers run UTC, which put filenames hours off local.
    return timezone.localtime().strftime(fmt)


def _resolve_atem_db_name(ip: str) -> str:
    """No name database in this build — filenames fall back to the IP."""
    return ''


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------

# Progress signature: (slot, completed, total, slot_name, from_cache).
# slot == -1 with completed == 0 is the "loop is starting, here's the
# total" event so the frontend bar can render at 0% immediately rather
# than waiting for the first slot to finish.
ProgressCallback = Callable[[int, int, int, str, bool], None]


def export_profile_zip(
    ip: str,
    save_opts: SaveOptions,
    *,
    legacy_get: bool = False,
    progress_callback: Optional[ProgressCallback] = None,
    cancel_session: str = '',
) -> tuple[bytes, str, str]:
    """Build a profile save deliverable for the ATEM at ``ip``.

    Returns ``(payload_bytes, content_type, filename)``:
        - ZIP archive (``application/zip``, ``<basename>.zip``) when
          there's at least one captured media-pool image to ship.
        - Bare XML (``application/xml``, ``<basename>.xml``) otherwise
          (operator opted out of images, ATEM has no populated slots,
          or capture failed).

    ``save_opts`` is the parsed SaveOptions; build via
    ``build_save_options`` from the section-selection dict.

    ``legacy_get=True`` is the curl-style ``GET /atem/profile/save/``
    path: no watcher quiesce, no media-pool capture, XML-only output
    regardless of ``save_opts.media_pool_images``. ``legacy_get=False``
    is the dialog flow: watcher is quiesced for the whole ATEM session
    (macros + media), and media-pool images are captured if
    ``save_opts.media_pool_images``.

    ``progress_callback`` receives per-slot updates during media-pool
    capture: ``(slot, completed, total, slot_name, from_cache)``. The
    initial ``(-1, 0, total, '', False)`` event fires before any slot
    download to render the bar at 0% immediately. ``None`` disables
    progress reporting.

    Raises ``ATEMConnectError`` on connect failure (HTTP 503 territory).
    All other exceptions propagate uncaught (HTTP 500 territory).
    """

    # Per-request hash dedupe (see ``_cache_get`` / ``_cache_put`` below).
    # ATEMs frequently end up with multiple slots holding identical content
    # (e.g. operator drag-dropped the same image to slots 1-20). Without
    # this, ``download_media_pool_images`` would do 20 round-trips for
    # 20 slots even though their MPfe hashes match. With it, the second
    # through twentieth slots hit ``_cache_get`` and reuse the bytes
    # from the first download — turning ~60 s into ~6 s for the
    # 20-slot-2-unique case.
    _hash_dedupe: dict[str, bytes] = {}

    # Sidechannel: ``download_media_pool_images``'s ``progress_callback``
    # signature is ``(slot, completed, total)`` — it has no way to tell
    # us whether the just-completed slot was a fresh download or a cache
    # hit. We stash that bit in this mutable cell from ``_cache_get``
    # (the last thing called before progress); ``_progress`` reads it
    # to enrich the outward-facing callback's ``from_cache`` flag.
    _last_from_cache = {'value': False}

    def _cache_get(slot: int, hash_hex: str):
        val = _hash_dedupe.get(hash_hex)
        _last_from_cache['value'] = val is not None
        return val

    def _cache_put(slot: int, hash_hex: str, png_bytes: bytes) -> None:
        _last_from_cache['value'] = False
        _hash_dedupe[hash_hex] = png_bytes

    # Watcher quiet-down for the ENTIRE save. The MediaPoolWatcher's
    # thumbnail downloads compete with our file-transfer activity at the
    # ATEM-protocol level — we lose every PLCK race and get FTDE code 1.
    # This affects both ``Profile.from_atem`` (which downloads macro
    # bytecode via the same file-transfer mechanism) and the media-pool
    # capture below. ``note_upload_started`` was designed for the
    # uploader's slot writes but works identically for any file-transfer
    # activity — one locked slot makes the watcher disconnect entirely.
    # Paired with ``note_upload_finished`` in ``finally`` so the watcher
    # reconnects and resumes thumbnail-fetching when we're done. The
    # 60 s TTL is a backstop in case our finally doesn't fire.
    watcher_quiesced = False
    captured: list = []
    atem_name = 'ATEM'
    xml = ''
    try:
        if not legacy_get:
            # Quiesce = the watcher defers its own downloads while this
            # lockout is held (its connection stays up), so the save's
            # transfers never contend with it for the ATEM's store lock.
            _media_pool_service.note_upload_started(ip, 0)
            watcher_quiesced = True

        with ATEM(ip) as atem:
            if not atem.connected:
                raise ATEMConnectError(f'failed to connect to ATEM at {ip}')

            profile = Profile.from_atem(atem, save_opts)
            xml = profile.to_xml()
            atem_name = atem.product_name or 'ATEM'

            if save_opts.media_pool_images and not legacy_get:
                # Captures all used slots via native interleaved
                # transfers on the pooled connection — ~2 s per fresh
                # slot, and operators sharing the connection stay live
                # throughout (no exclusive_access freeze; ASC-style
                # step 3). Cache hits via the per-request hash dedupe
                # are sub-millisecond. Progress fans out via
                # ``progress_callback``; cancel is checked at every
                # slot boundary inside ``download_media_pool_images``.
                #
                # ATEM facade exposes mixerstate via .raw (the
                # ATEMConnection); the facade itself doesn't proxy it.
                mx = atem.raw.mixerstate

                # Emit an initial 0/N progress so the bar appears at 0%
                # the moment the busy state shows in the dialog. Without
                # this the bar is hidden until the first slot finishes
                # (~5–10 s later) — confusing because the cancel button
                # and "capturing" status both appear immediately and
                # the missing bar reads like "is it actually working?".
                files = mx.get('mediaplayer-file-info', {}) or {}
                initial_total = sum(
                    1 for _, info in files.items()
                    if getattr(info, 'is_used', False)
                )
                if initial_total > 0 and progress_callback is not None:
                    progress_callback(-1, 0, initial_total, '', False)

                def _progress(slot, completed, total):
                    if progress_callback is None:
                        return
                    info = mediaplayer_slot_info(mx, slot)
                    sname = info.get('name') or f'Slot{slot}'
                    progress_callback(
                        slot, completed, total, sname,
                        _last_from_cache['value'],
                    )

                try:
                    captured = download_media_pool_images(
                        atem,
                        progress_callback=_progress,
                        cancel_check=(lambda: _is_capture_cancelled(cancel_session)),
                        cache_get=_cache_get,
                        cache_put=_cache_put,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "media-pool capture failed for %s: %s", ip, exc,
                    )
                    captured = []
    finally:
        if watcher_quiesced:
            _media_pool_service.note_upload_finished(ip, 0)
        # Consume the cancel flag so a reused session id can't cancel a
        # later save (and so the registry doesn't accumulate).
        _clear_capture_cancel(cancel_session)

    # Filename name component: prefer the AV Server's database name for
    # the ATEM (matches what operators see in the equipment list); fall
    # back to the device's self-reported model when the equipment row
    # is missing so a save never fails on a misconfigured equipment DB.
    db_name = _resolve_atem_db_name(ip)
    name_for_file = db_name or atem_name
    basename = f"{_slug_filename(name_for_file)}_{_format_save_timestamp()}"

    # Bundle into a ZIP only when there's something to bundle. A
    # single-XML "ZIP" would just be friction; the bare XML download
    # is the better UX.
    if captured:
        buf = io.BytesIO()
        # ZIP_DEFLATED: PNGs barely compress (already deflate-encoded
        # internally) but the XML compresses well. No compression for
        # the PNG entries to skip the wasted CPU.
        #
        # Dedupe entries by arcname AND content. The capture-side hash
        # dedupe means slots with identical content share the same
        # bytes object — an ``is`` identity check is enough to spot
        # them. Slots with the same display name but different bytes
        # (rare but possible) get a numeric suffix so neither slot's
        # data is silently dropped.
        # Every entry is stamped with the studio-local save time, matching
        # ATEM Software Control's zips. A bare ZipInfo(arcname) defaults to
        # the ZIP epoch (files dated Jan 1 1980 on extract), and writestr's
        # implicit stamp uses the container clock (UTC) — both wrong.
        save_time = timezone.localtime().timetuple()[:6]
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                zipfile.ZipInfo(f'{basename}.xml', date_time=save_time),
                xml,
                compress_type=zipfile.ZIP_DEFLATED,
            )
            written: dict = {}  # arcname → png_bytes (identity-checked)
            for entry in captured:
                base = entry["name"]
                arcname = f'ATEM Media Pool/{base}.png'
                # Skip if we've already written this exact bytes object
                # under any name (true content duplicate, e.g. all 20
                # slots with the same image).
                if any(v is entry['png_bytes'] for v in written.values()):
                    continue
                # Otherwise resolve filename collisions by suffixing.
                counter = 2
                while arcname in written:
                    arcname = f'ATEM Media Pool/{base}_{counter}.png'
                    counter += 1
                written[arcname] = entry['png_bytes']
                zf.writestr(
                    zipfile.ZipInfo(arcname, date_time=save_time),
                    entry['png_bytes'],
                    compress_type=zipfile.ZIP_STORED,
                )
        return buf.getvalue(), 'application/zip', f'{basename}.zip'

    # XML-only path — bare download whether the operator opted out of
    # images, the ATEM has no populated slots, or capture failed.
    return xml.encode('utf-8'), 'application/xml', f'{basename}.xml'
