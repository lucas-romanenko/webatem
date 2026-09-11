"""
Media Pool Watcher — event-driven watcher + downloader for the on-ATEM media pool.

Lives in the app rather than pyatem/ because the thumbnail caching,
JPEG encoding, observer dispatch, ref-counted registry, and
upload-lockout coordination are application concerns, not protocol
concerns. pyatem owns the pooled connection, ``download_still``, and the
``change:mediaplayer-*`` events; orchestration is here.

The watcher RIDES THE SHARED POOLED CONNECTION (ASC-style step 5,
2026-07-02) — it owns no socket of its own. Downloads are native
interleaved transfers (``ATEMConnection.download_still``) on the same
connection operator control uses: they never freeze anyone (the old
dedicated-socket + exclusive_access design existed purely to isolate
raw downloads that DID freeze whoever shared the socket), and each ATEM
carries one server session instead of two.

Watchers are module-level and ref-counted. Many subscribers watching the
same ATEM share one watcher + one cache. The watcher holds one pool
reference for its lifetime and releases it (identity-guarded) after a
short grace period when the last subscriber leaves.

Updates are delivered to subscribers via the observer hook
``watcher.on(event, handler)``. The watcher itself has no knowledge of the
transport used to deliver events to browsers — that's the application
layer's concern (Django Channels, SSE, whatever). The three events:

    'snapshot'       → full {slots: [...], players: [...]} dict
    'slot_updated'   → {'slot': <slot dict>}
    'player_updated' → {'player': <player dict>}

Relies on single-uvicorn-worker (see CLAUDE.md in the app repo) — the
``_watchers`` registry is process-local.

Threads per watcher:
    1. Binder thread — owns the watcher's relationship to the pooled
       connection: holds one pool ref, (re)connects it when it's down,
       and (re)binds the event handlers whenever the pool hands out a
       fresh protocol (every pool reconnect builds a new AtemProtocol).
       On each bind the slot/player view is rebuilt from the pooled
       connection's own mixerstate — the state dump already lives there.
       pyatem event callbacks fire on the POOLED connection's worker
       thread; they only mutate the cache and push tasks onto the work
       queue (keep them cheap).
    2. Worker thread — consumes the work queue: downloads via
       ``conn.download_still`` (blocking THIS thread only — the pooled
       worker pumps the packets), PIL decode / JPEG encode, and the
       observer-handler invocations.

Upload-lockout pattern (2026-07-02 semantics):
    When an upload begins, ``note_upload_started()`` sets a 60s TTL lockout.
    The connection STAYS UP — the old drop-the-socket behaviour (abrupt
    close, no protocol goodbye) was observed live to wedge switchers'
    network engines until power cycle. While any lockout is active the
    worker defers ALL downloads (never contends with the uploader for the
    still-store lock), but mid-upload MPfe metadata is still tracked — it
    carries the new content's hash. The webhook calls
    ``note_upload_finished()``, which clears the lockout and, on a
    connected watcher, re-queues the slot's download if it has a settled
    hash but no thumb; the content-hash cache makes that free when the
    content is unchanged.
"""

import base64
import io
import logging
import queue
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from PIL import Image

# NB: we use atem_to_rgb, NOT atem_to_image. download_still already
# RLE-decodes the transfer before returning, so we only need the YCbCr→RGBA
# step. atem_to_image would double-decode.
from atemwire.imaging import atem_to_rgb
from atemwire.pool import ATEMInstanceManager
from atemwire._state import decode_name, md5_hex


logger = logging.getLogger(__name__)

MAX_STILL_SLOTS = 32
MAX_MEDIA_PLAYERS = 4
# Sized for the media-pool modal's media-player tiles, which render ~430px wide
# on a 1080p operator monitor and up to ~920px wide on a 4K display. The smaller
# slot tiles (~210px wide) just downscale this further. 320px was visibly soft
# in the player tiles at any resolution.
THUMBNAIL_WIDTH = 960
JPEG_QUALITY = 82

RECONNECT_BACKOFF = 3.0            # binder wait between pooled-connect attempts
BIND_POLL_INTERVAL = 0.5           # binder cadence for protocol-rebind checks
WATCHER_GRACE_PERIOD = 5.0         # seconds to keep watcher alive after last release
UPLOAD_LOCKOUT_SECONDS = 60.0      # safety net — webhook clears it immediately on success

EMPTY_HASH_HEX = '00' * 16

# Work queue task kinds
_TASK_BROADCAST = 'broadcast'
_TASK_DECODE = 'decode'
_TASK_DOWNLOAD = 'download'
_TASK_STOP = 'stop'

# Per-slot download timeout (native interleaved transfer). Healthy
# transfers are ~2s measured on LAN; 15s is ~7x headroom and still
# generous for VPN links, while keeping the retry cycle fast when a
# transfer dies (a long timeout only delays the retry).
DOWNLOAD_TIMEOUT = 15.0

# A failed download is NEVER dropped (changed 2026-07-03): while the watcher
# is alive and the slot still needs its thumb, the task re-queues itself
# forever — fast at first (DOWNLOAD_RETRY_BACKOFF x attempt), then settling
# at DOWNLOAD_RETRY_BACKOFF_MAX between probes. Rationale: a foreign client
# holding the ATEM's media lock (observed live: a BC-room switcher refused
# the lock for minutes, then freed it) used to exhaust a fixed 3-attempt
# budget and strand the pool as permanent spinners even AFTER the lock
# freed. The per-task recheck (hash changed / thumb arrived / slot cleared)
# retires stale tasks, so patience costs one ~15s probe per cycle, not
# redundant downloads.
DOWNLOAD_RETRY_BACKOFF = 2.0
DOWNLOAD_RETRY_BACKOFF_MAX = 30.0
# Log the first few failures per slot at WARNING, then only every Nth —
# an hour behind a held lock is ~120 probes and must not fill the log.
DOWNLOAD_FAILURE_LOG_EVERY = 10


# ---------------------------------------------------------------------------
# Module-level thumbnail cache, keyed by (ATEM IP, content hash).
#
# SURVIVES watcher reconnects AND teardown — that is the whole point. A still's
# content is identified by its MD5 (the MPfe hash); the rendered 960px JPEG for
# a given hash never changes, so caching by hash is always accurate: a slot
# whose content changed gets a NEW hash → cache miss → re-download; nothing
# stale is ever served. Only the small JPEG data-URL (tens of KB) is cached,
# never the ~8MB raw still, so the bound below stays cheap across many ATEMs.
#
# The per-watcher ``self._slots`` dict still holds the LIVE per-slot occupancy
# view and is rebuilt from the ATEM's authoritative re-dump on every connect
# (see ``_connect_protocol``) — that rebuild is the accuracy guard for a slot
# cleared while disconnected. This cache only restores the *thumbnail bytes*
# for a slot whose hash the re-dump re-confirms.
# ---------------------------------------------------------------------------
_thumb_cache: "Dict[Tuple[str, str], str]" = {}
_thumb_cache_lock = threading.Lock()
THUMB_CACHE_MAX_ENTRIES = 512     # ~tens of KB each → a few MB worst case


def _thumb_cache_get(ip_address: str, hash_hex: str) -> Optional[str]:
    """Cached JPEG data-URL for (ip, hash), or None. Never returns anything for
    an empty/zero hash (not real content). LRU-touches the entry on a hit."""
    if not hash_hex or hash_hex == EMPTY_HASH_HEX:
        return None
    key = (ip_address, hash_hex)
    with _thumb_cache_lock:
        thumb = _thumb_cache.get(key)
        if thumb is not None:
            _thumb_cache.pop(key, None)
            _thumb_cache[key] = thumb   # move to MRU end
        return thumb


def _thumb_cache_put(ip_address: str, hash_hex: str, thumb_b64: str) -> None:
    """Store a rendered thumbnail under (ip, hash). No-op for an empty hash or
    empty thumb. Evicts the least-recently-used entry past the cap."""
    if not hash_hex or hash_hex == EMPTY_HASH_HEX or not thumb_b64:
        return
    key = (ip_address, hash_hex)
    with _thumb_cache_lock:
        _thumb_cache.pop(key, None)
        _thumb_cache[key] = thumb_b64
        while len(_thumb_cache) > THUMB_CACHE_MAX_ENTRIES:
            # dict preserves insertion order → first key is the LRU entry.
            _thumb_cache.pop(next(iter(_thumb_cache)), None)


def group_name(ip_address: str) -> str:
    """Conventional per-IP string identifier. Used by the Django side as a
    Channels group name; callable itself is transport-agnostic."""
    return f"atem_mediapool_{ip_address.replace('.', '_')}"


def _encode_thumbnail_from_rgba(rgba_bytes: bytes, width: int, height: int) -> bytes:
    """Build a JPEG thumbnail from pyatem's RGBA8888 output. pyatem returns
    a raw RGBA byte string sized width*height*4 — PIL adopts that directly
    via Image.frombytes."""
    img = Image.frombytes('RGBA', (width, height), rgba_bytes)
    if img.mode != 'RGB':
        img = img.convert('RGB')
    if img.width > THUMBNAIL_WIDTH:
        ratio = THUMBNAIL_WIDTH / img.width
        img = img.resize((THUMBNAIL_WIDTH, int(img.height * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=JPEG_QUALITY, optimize=False)
    return buf.getvalue()


def _empty_slot(idx: int) -> dict:
    return {'index': idx, 'isUsed': False, 'hash': '', 'fileName': '', 'thumb': None}


class MediaPoolWatcher:
    """Per-IP watcher. Rides the SHARED pooled connection (no socket of
    its own); a binder thread keeps it bound across pool reconnects."""

    def __init__(self, ip_address: str):
        self.ip_address = ip_address

        # Lifecycle
        self._ref_count = 0
        self._teardown_timer: Optional[threading.Timer] = None
        self._stop_event = threading.Event()
        self._binder_thread: Optional[threading.Thread] = None
        self._worker_thread: Optional[threading.Thread] = None

        # Cache (read under _state_lock).
        # Sizes come from the ATEM itself on first _mpl event — don't pre-assume.
        self._state_lock = threading.Lock()
        self._slots: Dict[int, dict] = {}
        self._players: Dict[int, dict] = {}

        # Upload lockouts: slot_idx -> unix timestamp until which the watcher
        # must NOT trigger downloads for this slot (and the worker defers ALL
        # downloads while any lockout is active — no store-lock contention
        # with the uploader).
        self._upload_locks: Dict[int, float] = {}

        # Work queue consumed by the worker thread. Holds download + decode +
        # broadcast tasks so the pooled connection's worker thread (where the
        # pyatem event handlers fire) never runs PIL / observer handlers.
        self._work_queue: "queue.Queue" = queue.Queue()

        # Pending-download claims: slot_idx -> hash currently queued or in
        # flight. Producers (MPfe events, heal ticks, modal opens) all route
        # through _queue_download, which refuses a duplicate claim — so a
        # slow transfer (15s timeout against a foreign lock-holder) can
        # never accumulate a backlog of identical tasks behind it. The
        # retry path keeps its claim across attempts; the claim is released
        # when the task retires (guarded under _state_lock).
        self._pending_downloads: Dict[int, str] = {}

        # True while downloads are starving because the ATEM refuses to
        # grant the media store lock (another client — automation software,
        # a stuck session — is holding it). Broadcast to the UI so the
        # modal shows an honest banner instead of anonymous spinners.
        # Verified live 2026-07-02: while ANY client holds the lock, no
        # other client can download (not even ASC) — there is no denial
        # message and no queue-jump; the only signal is our own timeout
        # with the lock never granted.
        self._pool_lock_starved = False

        # The SHARED pooled connection this watcher rides (binder-owned).
        # _pool_instance is the pool entry dict, kept for the identity-
        # guarded release; _protocol is the currently-BOUND AtemProtocol
        # (handlers registered on it) — compared by identity so a pool
        # reconnect (fresh protocol) triggers a rebind + state resync.
        self._conn = None
        self._pool_instance: Optional[dict] = None
        self._protocol = None
        self._heal_tick = 0
        self._handler_ids: list = []
        self._connected_flag = False

        # Observer registry. Handlers invoked from the worker thread.
        # Multiple handlers per event permitted; additions are thread-safe.
        self._observers_lock = threading.Lock()
        self._observers: Dict[str, List[Callable]] = {}

    # ---------- Public API ----------

    def on(self, event: str, handler: Callable) -> None:
        """Register a handler for one of: 'snapshot', 'slot_updated',
        'player_updated'. Handlers are called synchronously from the watcher's
        worker thread with a single ``data`` argument — keep them cheap (no
        network I/O, no PIL work). Exceptions inside handlers are caught and
        logged; they do not stop other handlers."""
        with self._observers_lock:
            self._observers.setdefault(event, []).append(handler)

    def acquire(self):
        """Increment ref count; start worker + supervisor if first caller."""
        with self._state_lock:
            if self._teardown_timer is not None:
                self._teardown_timer.cancel()
                self._teardown_timer = None
                logger.info(f"MediaPool[{self.ip_address}] cancelled pending teardown")

            self._ref_count += 1
            logger.info(
                f"MediaPool[{self.ip_address}] acquire, ref_count={self._ref_count}"
            )

            # SH-19: an acquire can land while a grace-teardown is mid-
            # flight — stop event set, threads alive but COMMITTED to
            # exiting. The old is_alive()-only checks then skipped both
            # restart blocks and the watcher died with ref_count > 0: a
            # frozen media pool until the next page open. Treat "stopping"
            # as "needs restart".
            #
            # PER-GENERATION stop events: threads capture self._stop_event
            # at entry and loop on that captured object. When we (re)start,
            # we install a FRESH unset event — the old generation keeps its
            # captured (set) event and exits, while the new threads capture
            # the fresh one. Clearing the shared event in place would let
            # the old binder read "not stopping" and run forever alongside
            # the new one on the same pooled connection.
            stopping = self._stop_event.is_set()
            need_worker = (self._worker_thread is None
                           or not self._worker_thread.is_alive())
            need_binder = (self._binder_thread is None
                           or not self._binder_thread.is_alive())

            if stopping or need_worker or need_binder:
                # The exiting binder mutates shared attrs (handler ids,
                # _protocol, pool ref) in its finally — the new binder joins
                # it first so generations never overlap. Only meaningful
                # when it's actually alive-but-stopping.
                old_binder = self._binder_thread if stopping else None
                self._stop_event = threading.Event()   # fresh generation

                if stopping or need_worker:
                    # Fresh work queue: a prior teardown may have left a STOP
                    # sentinel (and unprocessed tasks). The OLD worker
                    # captured its own queue, so it drains that one (with its
                    # STOP) regardless. Pending claims die with it — stale
                    # ones here would suppress every fresh enqueue.
                    self._work_queue = queue.Queue()
                    self._pending_downloads.clear()
                    self._worker_thread = threading.Thread(
                        target=self._run_worker,
                        daemon=True,
                        name=f"MediaPoolWorker-{self.ip_address}",
                    )
                    self._worker_thread.start()

                binder = threading.Thread(
                    target=self._run_binder,
                    args=(old_binder,),
                    daemon=True,
                    name=f"MediaPoolBinder-{self.ip_address}",
                )
                self._binder_thread = binder
                binder.start()

    def release(self):
        """Decrement ref count; schedule teardown if this was the last caller."""
        with self._state_lock:
            self._ref_count = max(0, self._ref_count - 1)
            logger.info(
                f"MediaPool[{self.ip_address}] release, ref_count={self._ref_count}"
            )
            if self._ref_count > 0:
                return

            if self._teardown_timer is not None:
                self._teardown_timer.cancel()

            def _teardown():
                with self._state_lock:
                    if self._ref_count > 0:
                        return  # someone reconnected during grace
                    self._stop_event.set()
                    # The binder wakes on its next stop_event.wait tick
                    # (≤ BIND_POLL_INTERVAL) and releases the pool ref.
                    # Wake the worker.
                    try:
                        self._work_queue.put_nowait((_TASK_STOP,))
                    except Exception:
                        pass
                    logger.info(f"MediaPool[{self.ip_address}] grace expired, stopping")

            self._teardown_timer = threading.Timer(WATCHER_GRACE_PERIOD, _teardown)
            self._teardown_timer.daemon = True
            self._teardown_timer.start()

    def note_upload_started(self, slot_idx: int):
        """Mark a slot as actively being written by the uploader for
        UPLOAD_LOCKOUT_SECONDS: the watcher will not trigger downloads for it
        (and defers ALL downloads while any upload is active — see
        _do_download) so it never contends with the uploader for the
        ATEM's still-store lock.

        The connection is deliberately KEPT ALIVE (changed 2026-07-02). The
        old behaviour dropped the socket for the upload's duration — an
        abrupt close with no protocol goodbye — and that abandonment was
        observed to WEDGE the switcher's network engine (existing sessions
        keep working, all new sessions refused, until power cycle): verified
        live on two dev ATEMs, where every drag-drop wedged the switcher the
        moment the watcher dropped its socket, while scheduled content-change
        uploads (which never dropped it) worked. An idle-but-connected
        watcher costs a trickle of ACK traffic during the upload; a wedged
        switcher costs a power cycle."""
        with self._state_lock:
            self._upload_locks[slot_idx] = time.time() + UPLOAD_LOCKOUT_SECONDS
        logger.info(
            f"MediaPool[{self.ip_address}] upload lockout: slot {slot_idx} "
            f"for {UPLOAD_LOCKOUT_SECONDS}s (connection stays up)"
        )

    def note_upload_finished(self, slot_idx: int):
        """Clear the upload lockout for a slot — called from the uploader's
        webhook when the new image has landed.

        The watcher rides the pooled connection and never disconnects for
        an upload, so no resync (and no further MPfe for settled content)
        is coming on its own. If the slot has a settled hash but no thumb,
        re-queue a download for the current hash so the tile can't be
        stranded as a permanent spinner; the hash-keyed cache makes this
        free when the content turns out unchanged.

        The old behaviour (wipe hash+thumb unconditionally) predates the
        content-hash cache: the identical-content case it existed for is now
        handled by the cache being content-addressed, and on the connected
        path the wipe itself created the stranded-spinner state.
        """
        requeue_hash = None
        with self._state_lock:
            self._upload_locks.pop(slot_idx, None)
            if self._connected_flag:
                current = self._slots.get(slot_idx)
                if (current is not None
                        and current.get('isUsed')
                        and current.get('hash')
                        and current.get('hash') != EMPTY_HASH_HEX
                        and current.get('thumb') is None):
                    requeue_hash = current['hash']
        logger.info(f"MediaPool[{self.ip_address}] upload lockout cleared: slot {slot_idx}")
        if requeue_hash is not None:
            if self._queue_download(slot_idx, requeue_hash):
                logger.info(
                    f"MediaPool[{self.ip_address}] slot {slot_idx} re-queued "
                    f"post-upload (watcher stayed connected)"
                )

    def _queue_download(self, slot_idx: int, hash_hex: str) -> bool:
        """Single choke-point for enqueueing a download task. Refuses when
        a task for this slot+hash is already queued or in flight, so
        producers (MPfe events, heal ticks, modal opens, post-upload
        requeues) can fire liberally without stacking duplicates behind a
        slow transfer. A pending claim for a DIFFERENT hash is superseded:
        the stale task retires on its own recheck without touching the new
        claim. Returns True if a new task was enqueued."""
        with self._state_lock:
            if self._pending_downloads.get(slot_idx) == hash_hex:
                return False
            self._pending_downloads[slot_idx] = hash_hex
            try:
                self._work_queue.put_nowait((_TASK_DOWNLOAD, slot_idx, hash_hex))
            except Exception:
                self._pending_downloads.pop(slot_idx, None)
                return False
            return True

    def requeue_missing(self, reason: str = 'modal refresh'):
        """Re-arm downloads for every used slot with a settled hash and no
        thumbnail. Idempotent (_queue_download deduplicates; per-task
        rechecks + the cache short-circuit retire anything stale) — called
        when the operator opens the media pool modal and by the binder's
        heal tick, so a visible pool is always actively converging even
        if some pathological earlier state lost a task."""
        to_queue = []
        with self._state_lock:
            for idx, slot in self._slots.items():
                if (slot['isUsed'] and slot['hash']
                        and slot['hash'] != EMPTY_HASH_HEX
                        and slot['thumb'] is None
                        and not self._is_upload_locked_locked(idx)):
                    to_queue.append((idx, slot['hash']))
        queued = [idx for idx, hash_hex in to_queue
                  if self._queue_download(idx, hash_hex)]
        if queued:
            logger.info(
                f"MediaPool[{self.ip_address}] {reason} re-armed "
                f"{len(queued)} missing thumb(s): {queued}"
            )

    def get_snapshot(self) -> dict:
        """Return the current cached state (safe to call from any thread)."""
        with self._state_lock:
            return self._build_snapshot_locked()

    def _set_pool_lock_starved(self, value: bool):
        """Track + broadcast the 'media lock held by another client' state.
        Set when a download times out with the store lock never granted;
        cleared when a wire download succeeds (proof the lock is grantable
        again). Broadcasts only on change."""
        with self._state_lock:
            if self._pool_lock_starved == value:
                return
            self._pool_lock_starved = value
        if value:
            logger.warning(
                f"MediaPool[{self.ip_address}] media store lock is held by "
                f"another client — previews can't be fetched until it releases"
            )
        else:
            logger.info(
                f"MediaPool[{self.ip_address}] media store lock released — "
                f"downloads flowing again"
            )
        self._enqueue_broadcast('pool_lock', {'locked': value})

    # ---------- Upload lockout helpers ----------

    def _is_upload_locked_locked(self, slot_idx: int) -> bool:
        """Caller must hold _state_lock."""
        expiry = self._upload_locks.get(slot_idx)
        if expiry is None:
            return False
        if time.time() >= expiry:
            del self._upload_locks[slot_idx]
            return False
        return True

    def _any_upload_active_locked(self) -> bool:
        """Caller must hold _state_lock. Returns True if ANY slot is currently
        within its upload-lockout window. Cull expired entries while here."""
        now = time.time()
        expired = [k for k, exp in self._upload_locks.items() if now >= exp]
        for k in expired:
            del self._upload_locks[k]
        return bool(self._upload_locks)


    # ---------- Binder thread ----------

    def _run_binder(self, predecessor=None):
        """Owns the watcher's relationship to the SHARED pooled connection
        (ASC-style step 5, 2026-07-02): holds one pool reference for the
        watcher's lifetime, (re)connects it when it is down, and (re)binds
        this watcher's event handlers whenever the pool hands out a fresh
        protocol — every pool reconnect builds a new ``AtemProtocol``, so
        the bound protocol is compared by identity each tick. On every
        bind the slot/player view is rebuilt from the pooled connection's
        own mixerstate (the state dump already lives there; no extra
        round-trip). The watcher owns NO socket.

        ``predecessor`` (SH-19): the previous generation's binder thread
        when this one was started mid-teardown. Its finally block mutates
        shared attributes (handler ids, _protocol, pool ref), so wait for
        it to fully exit before touching anything. It is already stopped
        (stop event was set) and exits within one 0.5s tick."""
        # Capture this generation's stop event + work queue at entry (SH-19).
        # A later acquire-during-teardown installs FRESH ones for the next
        # generation; looping on self._stop_event would then read the
        # successor's (unset) event and this binder would run forever
        # alongside the new one on the same pooled connection.
        my_stop = self._stop_event
        my_queue = self._work_queue
        if predecessor is not None and predecessor.is_alive():
            predecessor.join(timeout=10.0)
            if predecessor.is_alive():
                logger.error(
                    f"MediaPool[{self.ip_address}] predecessor binder still "
                    f"alive after 10s — proceeding anyway (shared-attr race "
                    f"possible)")
        logger.info(f"MediaPool[{self.ip_address}] binder thread started")
        try:
            while not my_stop.is_set():
                try:
                    if not self._ensure_pool_ref():
                        if my_stop.wait(RECONNECT_BACKOFF):
                            break
                        continue
                    conn = self._conn
                    if not conn.is_connected:
                        self._connected_flag = False
                        ok = False
                        try:
                            ok = conn.connect(self.ip_address)
                        except Exception as e:
                            logger.warning(
                                f"MediaPool[{self.ip_address}] pooled connect "
                                f"failed: {e}")
                        if not ok:
                            # A dead pool entry can't be revived through this
                            # object — drop the (identity-guarded) ref and
                            # re-acquire fresh next round: get_instance evicts
                            # dead entries and builds a new connection.
                            self._drop_pool_ref()
                            if my_stop.wait(RECONNECT_BACKOFF):
                                break
                            continue
                    proto = conn.protocol
                    if (proto is not None and conn.is_connected
                            and proto is not self._protocol):
                        self._rebind(proto)
                    elif (proto is not None and conn.is_connected
                            and proto is self._protocol):
                        # LEVEL-TRIGGERED convergence (2026-07-03): the
                        # pooled mixerstate is always current, so never
                        # depend on edge-triggered events for correctness.
                        # Binding mid-state-dump ("bound: 0 used") used to
                        # rely on catching the one-shot 'connected' event to
                        # correct itself — a race, observed twice live. Any
                        # divergence now heals within one tick; lost
                        # download tasks re-arm every ~5s.
                        if self._diverged_from_mixerstate(conn):
                            self._sync_from_mixerstate(
                                'converged to mixerstate (divergence heal)')
                        else:
                            self._heal_tick += 1
                            if self._heal_tick >= 10:
                                self._heal_tick = 0
                                self.requeue_missing(reason='heal tick')
                except Exception:
                    logger.exception(
                        f"MediaPool[{self.ip_address}] binder iteration failed")
                if my_stop.wait(BIND_POLL_INTERVAL):
                    break
        finally:
            self._unregister_handlers()
            self._protocol = None
            self._connected_flag = False
            self._drop_pool_ref()
            try:
                my_queue.put_nowait((_TASK_STOP,))
            except Exception:
                pass
            logger.info(f"MediaPool[{self.ip_address}] binder thread exited")

    def _ensure_pool_ref(self) -> bool:
        """Hold exactly one pool reference while the watcher lives."""
        if self._pool_instance is not None and self._conn is not None:
            return True
        try:
            instance = ATEMInstanceManager.get_instance(self.ip_address)
        except Exception as e:
            logger.warning(
                f"MediaPool[{self.ip_address}] pool acquire failed: {e}")
            return False
        self._pool_instance = instance
        self._conn = instance['connection']
        return True

    def _drop_pool_ref(self):
        """Release our pool reference — identity-guarded, so if our entry
        was evicted (worker death) and replaced by another holder's fresh
        entry, we never steal a reference from them."""
        instance, self._pool_instance = self._pool_instance, None
        self._conn = None
        if instance is None:
            return
        try:
            ATEMInstanceManager.release_instance(
                self.ip_address, instance=instance)
        except Exception:
            pass

    def _diverged_from_mixerstate(self, conn) -> bool:
        """True when our slot view disagrees with the pooled connection's
        mixerstate on any slot's used-flag or content hash. Cheap (~32
        attribute reads); runs every binder tick so a missed event or a
        bind that raced the state dump can never persist."""
        try:
            infos = conn.mixerstate.get('mediaplayer-file-info') or {}
        except Exception:
            return False
        if not isinstance(infos, dict):
            return False
        with self._state_lock:
            for i, entry in infos.items():
                try:
                    idx = int(i)
                except Exception:
                    continue
                if idx < 0 or idx >= MAX_STILL_SLOTS:
                    continue
                is_used = bool(getattr(entry, 'is_used', False))
                hash_hex = md5_hex(getattr(entry, 'hash', None))
                ours = self._slots.get(idx)
                if ours is None:
                    if is_used:
                        return True
                    continue
                if bool(ours.get('isUsed')) != is_used:
                    return True
                if is_used and ours.get('hash') != hash_hex:
                    return True
        return False

    def _rebind(self, proto):
        """Bind handlers to a (new) pooled protocol and rebuild state from
        its mixerstate. Runs on the binder thread."""
        if self._protocol is not None:
            self._unregister_handlers()
        self._protocol = proto
        self._register_handlers()
        self._sync_from_mixerstate('bound to pooled connection')

    def _sync_from_mixerstate(self, reason: str):
        """Rebuild the slot/player view from the pooled connection's own
        mixerstate — authoritative, because the pool's state dump already
        ran — restoring thumbs from the content-hash cache and queueing
        downloads for the misses (player-loaded slots first). The clear-
        and-rebuild is the accuracy guard for slots changed while we were
        unbound. Replays through the normal event handlers so the cache-
        restore / lockout / download logic stays in exactly one place."""
        conn = self._conn
        if conn is None:
            return
        mx = conn.mixerstate
        with self._state_lock:
            self._slots.clear()
            self._players.clear()
        self._connected_flag = True

        slots_field = mx.get('mediaplayer-slots')
        if slots_field is not None:
            self._on_slots(slots_field)

        players = mx.get('mediaplayer-selected') or {}
        if isinstance(players, dict):
            for idx in sorted(players.keys(), key=int):
                self._on_selected(players[idx])

        infos = mx.get('mediaplayer-file-info') or {}
        if isinstance(infos, dict):
            with self._state_lock:
                player_slots = {
                    p['stillIndex'] for p in self._players.values()
                    if p.get('type') == 'still'
                }
            order = sorted(
                infos.keys(),
                key=lambda i: (0 if int(i) in player_slots else 1, int(i)),
            )
            for i in order:
                self._on_file_info(infos[i])

        with self._state_lock:
            used = sum(1 for s in self._slots.values() if s['isUsed'])
            hits = sum(1 for s in self._slots.values()
                       if s['isUsed'] and s['thumb'] is not None)
        # The one line that answers "which scenario was this (re)bind":
        # all-hits = warm cache; downloads > 0 = changed/new content.
        logger.info(
            f"MediaPool[{self.ip_address}] {reason}: {used} used, "
            f"{hits} thumb(s) from cache, {max(0, used - hits)} download(s) queued"
        )
        self._enqueue_broadcast('snapshot', self.get_snapshot())

    # ---------- pyatem event handlers (supervisor thread) ----------

    def _register_handlers(self):
        p = self._protocol
        if p is None:
            return
        self._handler_ids = [
            ('connected',                        p.on('connected',                        self._on_connected)),
            ('disconnected',                     p.on('disconnected',                     self._on_disconnected)),
            ('change:mediaplayer-slots',         p.on('change:mediaplayer-slots',         self._on_slots)),
            ('change:mediaplayer-file-info:*',   p.on('change:mediaplayer-file-info:*',   self._on_file_info)),
            ('change:mediaplayer-selected:*',    p.on('change:mediaplayer-selected:*',    self._on_selected)),
        ]

    def _unregister_handlers(self):
        p = self._protocol
        if p is None:
            self._handler_ids = []
            return
        for event, handler_id in self._handler_ids:
            try:
                p.off(event, handler_id)
            except Exception:
                pass
        self._handler_ids = []

    def _on_connected(self):
        """Fires on the POOLED protocol at the end of a state (re)dump.
        The binder handles the initial bind (it only binds a ready
        connection, i.e. post-dump); this handler catches the transport's
        own IN-PLACE auto-reconnect — same protocol object, fresh dump —
        which the binder's identity check cannot see. Resync so slots
        changed while the link was down are reflected."""
        self._sync_from_mixerstate('re-synced (state dump complete)')

    def _on_disconnected(self):
        # Pooled link dropped. In-flight/queued downloads fail fast
        # (download_still raises on a dead connection) and re-queue
        # themselves; the binder rebinds + resyncs when the pool
        # reconnects (fresh protocol), and _on_connected covers the
        # transport's in-place reconnect.
        self._connected_flag = False

    def _on_slots(self, contents):
        """_mpl — authoritative slot count. Pre-populate empty slot entries
        for every valid index so per-slot MPfe diffs land on a known index
        (the frontend ignores slot_updated for unknown indices). Also
        broadcasts a snapshot so topology changes propagate to clients."""
        try:
            stills = int(getattr(contents, 'stills', 0) or 0)
        except Exception:
            stills = 0
        slot_count = max(0, min(stills, MAX_STILL_SLOTS))

        with self._state_lock:
            # Add entries for any newly-present index
            for i in range(slot_count):
                if i not in self._slots:
                    self._slots[i] = _empty_slot(i)
            # Drop entries outside the new range
            for i in list(self._slots.keys()):
                if i >= slot_count:
                    del self._slots[i]
            snapshot = self._build_snapshot_locked()

        self._enqueue_broadcast('snapshot', snapshot)

    def _on_file_info(self, contents):
        """MPfe — per-slot metadata. Updates cache, broadcasts diff, and
        kicks off a download when the slot is used, has a settled (non-zero)
        hash, and we don't already have a cached thumb for that hash."""
        try:
            idx = int(getattr(contents, 'index', -1))
            is_used = bool(getattr(contents, 'is_used', False))
            hash_hex = md5_hex(getattr(contents, 'hash', None))
            name = decode_name(getattr(contents, 'name', None))
        except Exception:
            return
        if idx < 0 or idx >= MAX_STILL_SLOTS:
            return

        should_download = False
        snapshot_payload: Optional[dict] = None
        slot_payload: Optional[dict] = None

        with self._state_lock:
            # A slot the uploader owns still gets its METADATA tracked — with
            # the connection now staying up through uploads, the mid-upload
            # MPfe is the only carrier of the new content's hash, and the
            # post-upload refresh (note_upload_finished) keys on it. Only the
            # DOWNLOAD is suppressed while the lock is held (the uploader owns
            # the wire; the worker additionally defers all downloads while any
            # upload is active).
            slot_locked = self._is_upload_locked_locked(idx)

            existing = self._slots.get(idx)
            new_index = existing is None
            if new_index:
                new_slot = _empty_slot(idx)
                new_slot['isUsed'] = is_used
                new_slot['hash'] = hash_hex
                new_slot['fileName'] = name
                self._slots[idx] = new_slot
            else:
                hash_changed = existing['hash'] != hash_hex
                new_slot = dict(existing)
                new_slot['isUsed'] = is_used
                new_slot['hash'] = hash_hex
                new_slot['fileName'] = name
                if hash_changed:
                    new_slot['thumb'] = None
                self._slots[idx] = new_slot

            # Restore a previously-rendered thumbnail from the SURVIVING module
            # cache if we have one for this slot's CURRENT hash. This is what
            # makes a reconnect fast — including the common post-upload case
            # where only ONE slot's hash actually changed: every unchanged slot
            # hits the cache and shows instantly with no download, only the
            # genuinely new/changed hash misses. A cleared or replaced slot has
            # a different (or empty) hash, so it can never hit a stale entry.
            if (new_slot.get('thumb') is None
                    and is_used and hash_hex and hash_hex != EMPTY_HASH_HEX):
                hit = _thumb_cache_get(self.ip_address, hash_hex)
                if hit is not None:
                    new_slot['thumb'] = hit
                    self._slots[idx] = new_slot

            # ATEM sometimes reports is_used=True before finishing its MD5.
            # Skip downloads while the hash is still all-zero; MPfe re-fires
            # once hashing completes.
            #
            # Gate on _connected_flag (set when pyatem fires 'connected' after
            # InCm): during the initial state dump, MPfe can arrive before
            # VidM. If we kick off a transfer in that window, pyatem's
            # file-transfer-data handler hits KeyError on
            # mixerstate['video-mode'] (protocol.py:316) every 20 packets and
            # the receive loop dies. _on_connected back-fills downloads for
            # every used slot after the dump completes.
            if (is_used
                    and hash_hex
                    and hash_hex != EMPTY_HASH_HEX
                    and new_slot.get('thumb') is None
                    and not slot_locked
                    and self._connected_flag):
                should_download = True

            if new_index:
                # Unknown index → frontend can't handle a slot_updated diff.
                snapshot_payload = self._build_snapshot_locked()
            else:
                slot_payload = dict(new_slot)

        if snapshot_payload is not None:
            self._enqueue_broadcast('snapshot', snapshot_payload)
        elif slot_payload is not None:
            self._enqueue_broadcast('slot_updated', {'slot': slot_payload})

        if should_download:
            self._queue_download(idx, hash_hex)

    def _on_selected(self, contents):
        """MPCE — which media-pool slot each media player currently has loaded."""
        try:
            idx = int(getattr(contents, 'index', -1))
            type_val = int(getattr(contents, 'source_type', 0) or 0)
            slot_idx = int(getattr(contents, 'slot', 0) or 0)
        except Exception:
            return
        if idx < 0 or idx >= MAX_MEDIA_PLAYERS:
            return
        if type_val == 1:
            type_str = 'still'
        elif type_val == 2:
            type_str = 'clip'
        else:
            return

        new_player = {
            'index': idx,
            'type': type_str,
            'stillIndex': slot_idx if type_val == 1 else 0,
            'clipIndex': slot_idx if type_val == 2 else 0,
        }

        snapshot_payload: Optional[dict] = None
        player_payload: Optional[dict] = None

        with self._state_lock:
            existing = self._players.get(idx)
            new_index = existing is None
            if new_index or existing != new_player:
                self._players[idx] = new_player
                if new_index:
                    snapshot_payload = self._build_snapshot_locked()
                else:
                    player_payload = dict(new_player)
                    logger.info(
                        f"MediaPool[{self.ip_address}] player diff mp{idx}: "
                        f"{existing} -> {new_player}"
                    )

        if snapshot_payload is not None:
            self._enqueue_broadcast('snapshot', snapshot_payload)
        elif player_payload is not None:
            self._enqueue_broadcast('player_updated', {'player': player_payload})

    # ---------- Snapshot builder ----------

    def _build_snapshot_locked(self) -> dict:
        """Caller must hold _state_lock."""
        slot_indices = sorted(self._slots.keys())
        player_indices = sorted(self._players.keys())
        return {
            'slots': [dict(self._slots[i]) for i in slot_indices],
            'players': [dict(self._players[i]) for i in player_indices],
            'poolLocked': self._pool_lock_starved,
        }

    # ---------- Worker thread ----------

    def _enqueue_broadcast(self, kind: str, payload: dict):
        try:
            self._work_queue.put_nowait((_TASK_BROADCAST, kind, payload))
        except Exception as e:
            logger.warning(f"MediaPool[{self.ip_address}] broadcast enqueue failed: {e}")

    def _run_worker(self):
        """Consume decode + broadcast tasks. Exits on STOP sentinel.

        The queue is CAPTURED at entry (SH-19): an acquire landing mid-
        teardown replaces ``self._work_queue`` for the new generation; this
        (old) worker must keep draining ITS OWN queue — whose STOP sentinel
        was queued by the teardown — instead of racing the new worker on
        the fresh one."""
        my_queue = self._work_queue
        logger.info(f"MediaPool[{self.ip_address}] worker thread started")
        try:
            while True:
                item = my_queue.get()
                kind = item[0]
                if kind == _TASK_STOP:
                    return
                try:
                    if kind == _TASK_BROADCAST:
                        _, evt, payload = item
                        self._emit(evt, payload)
                    elif kind == _TASK_DECODE:
                        _, slot_idx, raw, expected_hash, w, h = item
                        self._do_decode(slot_idx, raw, expected_hash, w, h)
                    elif kind == _TASK_DOWNLOAD:
                        # Producers enqueue 3-tuples (attempt defaults to 1);
                        # only the retry path re-queues with an explicit count.
                        _, slot_idx, expected_hash = item[:3]
                        attempt = item[3] if len(item) > 3 else 1
                        self._do_download(slot_idx, expected_hash, attempt)
                except Exception as e:
                    logger.exception(
                        f"MediaPool[{self.ip_address}] worker task {kind} failed: {e}"
                    )
        finally:
            logger.info(f"MediaPool[{self.ip_address}] worker thread exited")

    def _do_download(self, slot_idx: int, expected_hash: str, attempt: int = 1):
        """Worker-thread task wrapper: run the download, then release the
        slot's pending-download claim unless the task re-queued itself
        (defer/retry keep their claim so producers can't stack duplicate
        tasks for the slot behind a slow transfer)."""
        requeued = False
        try:
            requeued = self._download_task(slot_idx, expected_hash, attempt)
        finally:
            if not requeued:
                with self._state_lock:
                    if self._pending_downloads.get(slot_idx) == expected_hash:
                        del self._pending_downloads[slot_idx]

    def _download_task(self, slot_idx: int, expected_hash: str,
                       attempt: int) -> bool:
        """Re-check the task is still needed, then download via the pooled
        connection's native interleaved transfer (``download_still``) and
        decode inline. Blocks only this worker thread — the pooled
        connection keeps serving control traffic and state events
        throughout (nothing freezes). Returns True iff the task re-queued
        itself (pending claim retained across the requeue)."""
        # Teardown in progress: drop silently. Anything still needed is
        # re-armed on the next acquire (resync + heal tick) — draining a
        # backlog against a dead connection is pure log noise.
        if self._stop_event.is_set():
            return False

        # Defer while ANY upload to this ATEM is in flight: the uploader owns
        # the still-store lock; a watcher transfer here would contend with
        # it. Re-queue rather than drop — the lockout clears via the webhook
        # (or its 60s TTL) and the task then proceeds; the brief wait stops
        # the single worker from spinning on the requeue.
        with self._state_lock:
            upload_active = self._any_upload_active_locked()
        if upload_active:
            if self._stop_event.wait(0.5):
                return False
            try:
                self._work_queue.put_nowait((_TASK_DOWNLOAD, slot_idx, expected_hash))
            except Exception:
                return False
            return True

        # Re-check: the task may have been queued seconds ago and state may
        # have shifted (hash changed from a resync, an MPfe/cache hit beat
        # us to it).
        with self._state_lock:
            current = self._slots.get(slot_idx)
            if not current or current['hash'] != expected_hash:
                return False
            if current['thumb'] is not None:
                return False

        # Cache short-circuit: a sibling slot with identical content, or an
        # MPfe that landed after this task was queued, may have rendered this
        # exact hash already. Use it instead of a redundant ~8MB transfer.
        cached = _thumb_cache_get(self.ip_address, expected_hash)
        if cached is not None:
            logger.info(
                f"MediaPool[{self.ip_address}] slot {slot_idx} cache hit — "
                f"skipping download"
            )
            self._apply_thumb(slot_idx, expected_hash, cached)
            return False

        conn = self._conn
        w, h = 1920, 1080
        try:
            if conn is not None:
                vmode = conn.mixerstate.get('video-mode')
                if vmode is not None:
                    w, h = vmode.get_resolution()
        except Exception:
            pass

        # A retry attempt means the previous one just failed against a busy,
        # recovering, or lock-held ATEM — give it breathing room before
        # probing again, settling at the max backoff for the long patient
        # phase. (Blocks only this single worker; downloads are sequential
        # by design anyway, and the recheck above retires the task the
        # moment it stops being needed.)
        if attempt > 1:
            if self._stop_event.wait(min(DOWNLOAD_RETRY_BACKOFF * (attempt - 1),
                                         DOWNLOAD_RETRY_BACKOFF_MAX)):
                return False

        # Failures re-queue the whole task FOREVER (capped backoff) while
        # the slot still needs its thumb — settled content gets no further
        # MPfe, so any give-up strands the tile as a spinner. (The ATEM's
        # transient "try again" status is retried inside the protocol's own
        # transfer machinery; what surfaces here is real: timeouts, foreign
        # lock-holders, dead connection, mid-stream death.)
        raw_ycbcr = None
        failure: Optional[str] = None
        download_started = time.monotonic()
        if conn is None or not conn.is_connected:
            failure = 'pooled connection not ready'
        else:
            try:
                raw_ycbcr = conn.download_still(
                    slot_idx, timeout=DOWNLOAD_TIMEOUT)
            except Exception as e:
                failure = str(e)
        if raw_ycbcr is None:
            if failure is not None and not self._stop_event.is_set():
                # 'lock never granted' is pyatem's timeout enrichment when
                # the PLCK went unanswered — the foreign-lock-holder
                # signature. Any other failure shape leaves the flag alone.
                if 'lock never granted' in failure:
                    self._set_pool_lock_starved(True)
                if attempt <= 3 or attempt % DOWNLOAD_FAILURE_LOG_EVERY == 0:
                    logger.warning(
                        f"MediaPool[{self.ip_address}] download slot "
                        f"{slot_idx} failed (attempt {attempt}): {failure} — "
                        f"will keep retrying while the slot needs it"
                    )
                try:
                    self._work_queue.put_nowait(
                        (_TASK_DOWNLOAD, slot_idx, expected_hash, attempt + 1)
                    )
                except Exception as e:
                    logger.warning(
                        f"MediaPool[{self.ip_address}] retry enqueue "
                        f"({slot_idx}) failed: {e}"
                    )
                    return False
                return True
            return False

        download_ms = int((time.monotonic() - download_started) * 1000)
        logger.info(
            f"MediaPool[{self.ip_address}] slot {slot_idx} downloaded "
            f"({len(raw_ycbcr)} bytes in {download_ms}ms)"
        )
        self._set_pool_lock_starved(False)   # lock demonstrably grantable

        self._do_decode(slot_idx, raw_ycbcr, expected_hash, w, h)
        return False

    def _do_decode(self, slot_idx: int, raw: bytes, expected_hash: str, w: int, h: int):
        if not raw:
            return
        logger.info(
            f"MediaPool[{self.ip_address}] decoding slot {slot_idx} ({len(raw)} bytes)"
        )
        try:
            rgba_bytes = atem_to_rgb(raw, w, h)
            thumb_bytes = _encode_thumbnail_from_rgba(rgba_bytes, w, h)
            thumb_b64 = 'data:image/jpeg;base64,' + base64.b64encode(thumb_bytes).decode('ascii')
        except Exception as e:
            logger.exception(
                f"MediaPool[{self.ip_address}] decode failed for slot {slot_idx}: {e}"
            )
            return

        # Cache the rendered thumb by content hash so it survives the next
        # reconnect/teardown. Done before _apply_thumb (which may discard the
        # live apply if the slot's hash moved on) — the bytes are valid for
        # this hash regardless of where the slot is now.
        _thumb_cache_put(self.ip_address, expected_hash, thumb_b64)

        self._apply_thumb(slot_idx, expected_hash, thumb_b64)

    def _apply_thumb(self, slot_idx: int, expected_hash: str, thumb_b64: str) -> bool:
        """Set a slot's thumbnail iff its current hash still matches, then
        broadcast a slot_updated diff. Returns False (result discarded) if the
        slot's content changed since the thumb was produced — the accuracy
        guard that stops a stale image landing on a slot whose hash moved on.
        Shared by the download/decode path and the cache-hit short-circuit."""
        slot_payload: Optional[dict] = None
        with self._state_lock:
            current = self._slots.get(slot_idx)
            if not current or current['hash'] != expected_hash:
                logger.info(
                    f"MediaPool[{self.ip_address}] slot {slot_idx} hash changed "
                    "before thumb apply; discarding result"
                )
                return False
            current['thumb'] = thumb_b64
            self._slots[slot_idx] = current
            slot_payload = dict(current)

        if slot_payload is not None:
            self._emit('slot_updated', {'slot': slot_payload})
        return True

    def _emit(self, event: str, data: dict):
        """Fire all registered handlers for an event. Called from the worker
        thread. Exceptions inside handlers are caught and logged; other
        handlers still run."""
        with self._observers_lock:
            handlers = list(self._observers.get(event, []))
        for h in handlers:
            try:
                h(data)
            except Exception as e:
                logger.warning(
                    f"MediaPool[{self.ip_address}] observer for {event} failed: {e}"
                )


# ============================================================================
# Module-level registry
# ============================================================================

_watchers: Dict[str, MediaPoolWatcher] = {}
_registry_lock = threading.Lock()

# Callbacks invoked when the registry creates a NEW MediaPoolWatcher.
# Host applications register here to install observer handlers (e.g. a
# Channels group_send broadcaster) at the single point where a watcher
# starts existing. Fired exactly once per new watcher, before its ref
# count has been incremented and before the caller sees the snapshot.
_watcher_created_callbacks: List[Callable[[MediaPoolWatcher, str], None]] = []


def on_watcher_created(callback: Callable[[MediaPoolWatcher, str], None]) -> None:
    """Register a callback fired once per newly-created watcher. Invoked
    with ``(watcher, ip_address)``; register observer handlers via
    ``watcher.on(event, handler)``.

    Idempotent for a given callable — duplicate registrations are
    tolerated but the callback still fires only once per watcher."""
    if callback not in _watcher_created_callbacks:
        _watcher_created_callbacks.append(callback)


def _fire_watcher_created(watcher: MediaPoolWatcher, ip_address: str) -> None:
    for cb in list(_watcher_created_callbacks):
        try:
            cb(watcher, ip_address)
        except Exception as e:
            logger.warning(f"watcher-created callback {cb} failed: {e}")


def acquire(ip_address: str) -> dict:
    """Acquire a watcher for this IP and return the current snapshot."""
    fire_created = False
    with _registry_lock:
        watcher = _watchers.get(ip_address)
        if watcher is None:
            watcher = MediaPoolWatcher(ip_address)
            _watchers[ip_address] = watcher
            fire_created = True
        watcher.acquire()
    if fire_created:
        _fire_watcher_created(watcher, ip_address)
    return watcher.get_snapshot()


def release(ip_address: str):
    with _registry_lock:
        watcher = _watchers.get(ip_address)
    if watcher is not None:
        watcher.release()


def note_upload_started(ip_address: str, slot_idx: int):
    """Tell the watcher (if one exists) to leave a slot alone for the duration
    of an in-flight upload. Safe to call from any thread."""
    with _registry_lock:
        watcher = _watchers.get(ip_address)
    if watcher is not None:
        try:
            watcher.note_upload_started(slot_idx)
        except Exception as e:
            logger.warning(f"note_upload_started({ip_address}, {slot_idx}) failed: {e}")


def note_upload_finished(ip_address: str, slot_idx: int):
    """Tell the watcher the upload has landed (called from the webhook) so
    it can reconnect and pull the new thumbnail."""
    with _registry_lock:
        watcher = _watchers.get(ip_address)
    if watcher is not None:
        try:
            watcher.note_upload_finished(slot_idx)
        except Exception as e:
            logger.warning(f"note_upload_finished({ip_address}, {slot_idx}) failed: {e}")


def seed_uploaded_thumb(ip_address: str, slot_idx: int,
                        hash_hex: str, thumb_b64: str):
    """Seed the content-hash thumbnail cache with a thumb synthesised from
    the just-uploaded source file (webhook fast-path, 2026-07-07).

    The hash is the uploader's MD5 of the converted ATEM pixels — the exact
    value the switcher stamps on the slot's MPfe (it's how the upload was
    verified). Seeding it means the watcher's post-upload refresh becomes a
    cache HIT: no re-download of the still we just sent, no store lock
    taken — so a concurrent ASC preview fetch of the same new still can't
    lose the race and show its placeholder circle. Self-verifying: had the
    upload corrupted, the switcher would report a DIFFERENT hash, the cache
    misses, and the watcher downloads as before.

    Works without a live watcher too (pure cache seed for a later acquire);
    with one, the thumb is applied to the slot immediately if its MPfe hash
    already matches (``_apply_thumb`` guards the hash and broadcasts)."""
    if not hash_hex or hash_hex == EMPTY_HASH_HEX or not thumb_b64:
        return
    _thumb_cache_put(ip_address, hash_hex, thumb_b64)
    with _registry_lock:
        watcher = _watchers.get(ip_address)
    if watcher is None:
        logger.info(
            f"MediaPool[{ip_address}] seeded uploaded thumb for slot "
            f"{slot_idx} (no live watcher — cached for a later acquire)"
        )
        return
    try:
        applied = watcher._apply_thumb(slot_idx, hash_hex, thumb_b64)
        logger.info(
            f"MediaPool[{ip_address}] seeded uploaded thumb for slot "
            f"{slot_idx} ({'applied' if applied else 'cached for MPfe'})"
        )
    except Exception as e:
        logger.warning(
            f"seed_uploaded_thumb({ip_address}, {slot_idx}) failed: {e}")


def requeue_missing(ip_address: str):
    """Re-arm downloads for any thumb-less used slots (modal-open nudge).
    No-op when no watcher exists for this IP."""
    with _registry_lock:
        watcher = _watchers.get(ip_address)
    if watcher is not None:
        try:
            watcher.requeue_missing()
        except Exception as e:
            logger.warning(f"requeue_missing({ip_address}) failed: {e}")


def get_snapshot(ip_address: str) -> dict:
    with _registry_lock:
        watcher = _watchers.get(ip_address)
    if watcher is None:
        return {
            'slots': [_empty_slot(i) for i in range(MAX_STILL_SLOTS)],
            'players': [{'index': i, 'type': 0, 'stillIndex': 0, 'clipIndex': 0}
                        for i in range(MAX_MEDIA_PLAYERS)],
        }
    return watcher.get_snapshot()
