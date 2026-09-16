"""
atemwire-based media pool upload service.

Used by the application's uploader worker (``run_uploader`` management
command) and one-off callers (downtime-overlay button uploads, profile-
load image upload) to push stills into an ATEM media pool in-process.
Opens one short-lived AtemProtocol per unique IP, uses the transport's
aggressive-drain mode for bulk-upload throughput (~3 s per 1080p still
vs. ~25 s with ACK-paced sends), and verifies each upload via MPfe
hash equality.

Lives in the app rather than in atemwire because tally cycle-wait,
PIL image prep, progress callbacks, and hash-verify policy are feature
concerns, not protocol concerns. atemwire owns ``protocol.upload()``;
everything around it is policy.

Validated tuning + gotchas:

- ``aggressive_drain=True`` on the transport — replaces the old
  ``_install_fast_send`` monkeypatch with a proper constructor flag.
- One AtemProtocol connection reused across all items for a single IP.
- Verify success via MPfe hash equality (NOT "hash changed" — a re-upload
  of identical content won't change the hash).
- Cycle-wait tally safety: wait for cold → observe 40 s for live → wait
  for cold → upload. Naive "wait until not-live" misses the macro-rotation
  gap.

Public API:

    results = execute_upload(
        items: list[tuple[str, int, str]],   # (ip, slot_0_indexed, image_path)
        skip_tally: bool,
        check_cancelled: Callable[[], bool] | None = None,
        log_append: Callable[[str], None] | None = None,
    ) -> list[ItemResult]
"""

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError
from atemwire.protocol import AtemProtocol
from atemwire.imaging import rgb_to_atem
from atemwire.ready import wait_ready

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tuning constants — validated values
# ---------------------------------------------------------------------------

CONNECTION_TIMEOUT = 15.0
# Connect attempts per batch (parity with the control page's 3-attempt
# pool connect; this path used to give up after one timeout, failing
# jobs a moment's switcher busyness would otherwise have survived).
CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY = 2.0
UPLOAD_TIMEOUT = 90.0
HASH_SETTLE_TIMEOUT = 10.0
REUPLOAD_SETTLE = 1.0

# Tally cycle-wait timings + the shared cold->hot->cold loop live in
# ``content_change.tally`` so the media-pool and HyperDeck paths can't drift.
from atem_control.tally import (
    ticked_pump,
    wait_for_safe_cycle,
    TALLY_OBSERVATION_SECONDS,
    TALLY_TIMEOUT,
)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ItemResult:
    slot: int                      # 0-indexed
    image_path: str
    success: bool
    upload_seconds: float = 0.0
    settle_seconds: float = 0.0
    error: Optional[str] = None
    # MD5 (hex) of the converted ATEM-format pixels — the exact hash the
    # switcher stamps on the slot's MPfe. Set on success; the webhook seeds
    # the watcher's thumbnail cache with it so the post-upload refresh
    # cache-hits instead of re-downloading the still we just sent.
    content_hash: Optional[str] = None


# ---------------------------------------------------------------------------
# Cancellation helper
# ---------------------------------------------------------------------------

class _OwnershipLost(BaseException):
    """Raised by the cancel/ownership check when it can no longer confirm this
    replica still owns the row it is uploading (e.g. its DB session — and with
    it the session-level advisory lane lock — died mid-upload). It is a
    BaseException on PURPOSE so it sails past every ``except Exception`` in the
    upload path (which otherwise turns it into a failed ItemResult), unwinds
    through the per-IP ``finally`` (so the ATEM session goodbye still fires),
    and reaches run_uploader — which aborts the batch rather than keep pushing
    frames to a switcher the reaper may already have handed to another replica
    (UP-1). Sockets/locks are released on the way up; the row is left for the
    reaper to re-queue for a clean single retry."""


class _Canceller:
    """Wraps an optional callable that signals cancellation."""

    def __init__(self, check_cancelled: Optional[Callable[[], bool]] = None):
        self._check = check_cancelled

    def is_set(self) -> bool:
        if self._check is not None:
            try:
                if self._check():
                    return True
            # _OwnershipLost is a BaseException, so it is NOT caught here — it
            # propagates to abort the upload (see the class docstring). Only a
            # genuine bug in the check is swallowed as "not cancelled".
            except Exception:
                logger.exception("cancel check raised; treating as NOT cancelled")
        return False


# ---------------------------------------------------------------------------
# Tally safety (cycle-wait — ports Mediaupload.exe's WaitForSafeUploadWindow)
# ---------------------------------------------------------------------------

def _media_player_source_ids(mp_idx) -> set:
    """The program-bus source IDs a media player presents, BMD convention:
    MP1 fill=3010 / key=3011, MP2=3020/3021, MP3=3030/3031, … (0-indexed)."""
    fill = 3010 + int(mp_idx) * 10
    return {fill, fill + 1}


# ME1 is the broadcast program feed; other M/Es are assumed NOT on air. The
# studios air ME1, so a media player / hyperdeck parked on an unused ME2
# program bus must not block an upload. DSK is downstream of the program M/E,
# so it's global. (If a studio ever airs a different M/E, that's a config
# decision to revisit here — same assumption as hyperdeck_tally.is_input_live.)
PROGRAM_ME = 0


def _source_on_air(mixerstate, ids: set) -> bool:
    """Fallback on-air check, used only when the ATEM's ``tally-source`` hasn't
    arrived yet (it's part of the connect state-dump, so this is rare). True if
    any source id in ``ids`` is live on the program feed: the ME1 program bus,
    an on-air ME1 USK fill/key, or an on-air DSK fill/key.

    Deliberately ME1-scoped for program + USK (NOT all M/Es): a media player on
    an unused secondary M/E is not on air and must not block an upload. Same
    scope as ``hyperdeck_tally.is_input_live``, plus key-source coverage (a
    self-keyed source) and both keyer dimensions.
    """
    # ME1 program bus.
    pbi = mixerstate.get('program-bus-input') or {}
    me1 = pbi.get(PROGRAM_ME) if isinstance(pbi, dict) else None
    if me1 is not None and getattr(me1, 'source', None) in ids:
        return True

    # On-air ME1 upstream keyers — fill OR key source.
    on_air = mixerstate.get('key-on-air') or {}
    base = mixerstate.get('key-properties-base') or {}
    me1_on_air = on_air.get(PROGRAM_ME) if isinstance(on_air, dict) else None
    me1_base = base.get(PROGRAM_ME) if isinstance(base, dict) else None
    if isinstance(me1_on_air, dict) and isinstance(me1_base, dict):
        for k, ko in me1_on_air.items():
            if not getattr(ko, 'enabled', False):
                continue
            kb = me1_base.get(k)
            if kb is None:
                continue
            if getattr(kb, 'fill_source', None) in ids \
                    or getattr(kb, 'key_source', None) in ids:
                return True

    # On-air downstream keyers (downstream of the program M/E — global).
    dstate = mixerstate.get('dkey-state') or {}
    dbase = mixerstate.get('dkey-properties-base') or {}
    if isinstance(dstate, dict) and isinstance(dbase, dict):
        for d, node in dstate.items():
            if not getattr(node, 'on_air', False):
                continue
            db = dbase.get(d)
            if db is None:
                continue
            if getattr(db, 'fill_source', None) in ids \
                    or getattr(db, 'key_source', None) in ids:
                return True

    return False


def _is_slot_safe(protocol, slot_0_indexed: int) -> bool:
    """Safe to overwrite a media-pool slot when no media player currently
    showing that slot (as a still) is live on PROGRAM output.

    Source of truth is the ATEM's own program tally (``tally-source``, the TlSr
    broadcast) — the exact on-air state ATEM Software Control displays. The
    switcher already computes it from the full signal path (M/E chaining,
    USK/DSK, aux-to-program), so there's no per-M/E reconstruction in the
    normal path: whatever it reports on program blocks the upload, whatever it
    doesn't, doesn't.

    Only if tally hasn't arrived yet (rare — it's in the connect state-dump) do
    we fall back to an ME1 program-path reconstruction, so we neither overwrite
    blind nor hang waiting for tally.
    """
    mixerstate = protocol.mixerstate
    mps = mixerstate.get('mediaplayer-selected') or {}
    if not isinstance(mps, dict):
        return True

    # Source IDs of every media player currently showing this slot as a still.
    candidates = set()
    for mp_idx, mp in mps.items():
        if getattr(mp, 'source_type', 0) == 1 \
                and getattr(mp, 'slot', -1) == slot_0_indexed:
            candidates |= _media_player_source_ids(mp_idx)
    if not candidates:
        return True

    # Authoritative: the ATEM's own program tally (matches ATEM Software Control).
    tally = getattr(mixerstate.get('tally-source'), 'tally', None)
    if isinstance(tally, dict):
        for src in candidates:
            pv = tally.get(src)                 # (program, preview)
            if pv and pv[0]:
                return False
        return True

    # Tally not available yet — fall back to the ME1 program-path read.
    return not _source_on_air(mixerstate, candidates)


def _wait_for_safe_upload_window(protocol, slot_0_indexed: int,
                                 canceller: _Canceller,
                                 log_append: Callable[[str], None],
                                 observation: float = TALLY_OBSERVATION_SECONDS,
                                 timeout: float = TALLY_TIMEOUT) -> bool:
    """Cycle-wait for tally safety before overwriting a media-pool slot. Thin
    wrapper over the shared ``wait_for_safe_cycle`` (content_change.tally) with
    the media-pool slot liveness predicate, so the cold->hot->cold loop is
    identical to the HyperDeck path."""
    slot_user = slot_0_indexed + 1
    tally_present = isinstance(
        getattr(protocol.mixerstate.get('tally-source'), 'tally', None), dict)
    log_append(f"slot {slot_user}: tally source "
               f"{'present (authoritative)' if tally_present else 'ABSENT — using ME1 fallback'}")
    return wait_for_safe_cycle(
        protocol,
        is_live=lambda: not _is_slot_safe(protocol, slot_0_indexed),
        is_cancelled=canceller.is_set,
        log=log_append,
        label=f'slot {slot_user}',
        observation=observation,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Frame preparation
# ---------------------------------------------------------------------------

def _validate_image_fast(image_path: str):
    """Upfront check — is this a file PIL can read? Catches corrupt / truncated
    / zero-byte / not-an-image files BEFORE we waste tally-wait time on them.

    Also the decompression-bomb gate. Pillow raises DecompressionBombError from
    ``Image.open`` on the header's declared size, BEFORE decoding a pixel, so a
    30000x30000 PNG that is 200 KB on disk never becomes 3.3 GB of RGBA. That
    error is not an OSError or a ValueError, so it has to be named explicitly
    or it escapes every caller as an unhandled exception instead of a refusal."""
    try:
        with Image.open(image_path) as im:
            im.verify()
    except DecompressionBombError as e:
        raise ValueError(f"image is too large to decode safely: {e}") from e
    except (UnidentifiedImageError, OSError, SyntaxError) as e:
        raise ValueError(f"not a readable image: {e}") from e


def _prepare_frame(image_path: str, width: int, height: int) -> bytes:
    with Image.open(image_path) as im:
        if im.mode != 'RGBA':
            im = im.convert('RGBA')
        else:
            im.load()

        target_aspect = width / height
        src_aspect = im.width / im.height
        scale = (im.height / height) if src_aspect < target_aspect else (im.width / width)
        scaled = im.resize(
            (max(1, int(im.width / scale)), max(1, int(im.height / scale))),
            Image.Resampling.LANCZOS,
        )

    canvas = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    canvas.paste(scaled, ((width - scaled.width) // 2, (height - scaled.height) // 2), scaled)

    premultiply = image_path.lower().endswith('.png')
    return rgb_to_atem(canvas.tobytes(), width, height, premultiply=premultiply)


# ---------------------------------------------------------------------------
# Per-item upload
# ---------------------------------------------------------------------------

def _slot_hash(protocol, slot: int) -> Optional[bytes]:
    info = protocol.mixerstate.get('mediaplayer-file-info') or {}
    entry = info.get(slot) if isinstance(info, dict) else None
    return getattr(entry, 'hash', None) if entry else None


def _upload_one(protocol, slot: int, image_path: str, width: int, height: int,
                canceller: _Canceller, log_append: Callable[[str], None],
                skip_tally: bool) -> ItemResult:
    slot_user = slot + 1
    filename = os.path.basename(image_path)
    name = os.path.splitext(filename)[0]

    try:
        _validate_image_fast(image_path)
    except ValueError as e:
        log_append(f"slot {slot_user}: FAIL — image validation: {e}")
        return ItemResult(slot=slot, image_path=image_path, success=False,
                          error=f"image validation failed: {e}")

    if not skip_tally:
        if canceller.is_set():
            return ItemResult(slot=slot, image_path=image_path, success=False,
                              error="cancelled before tally check")
        if not _wait_for_safe_upload_window(protocol, slot, canceller, log_append):
            err = ("cancelled during tally wait" if canceller.is_set()
                   else f"tally timeout: slot {slot_user} stayed live > {TALLY_TIMEOUT:.0f}s")
            return ItemResult(slot=slot, image_path=image_path, success=False, error=err)

    if canceller.is_set():
        return ItemResult(slot=slot, image_path=image_path, success=False,
                          error="cancelled before upload")

    # Frame prep runs on a worker WITH THE LOOP PUMPED, because nothing else
    # pumps it here. The session is already open at this point and the switcher
    # expects its keepalives answered; event dispatch only happens inside
    # protocol.loop(), which the upload busy-wait below does but which nothing
    # does during prep. That was survivable while every image arrived already
    # at frame size and prep was milliseconds. It stopped being survivable when
    # the host default stopped resizing on the way in (1.3.0): a large source
    # is seconds of Lanczos with the loop silent, the switcher times the
    # session out mid-upload, and the abandoned session takes other clients
    # (ATEM Software Control included) down with it. Same pump-thread shape as
    # the macro apply below, and the same reason.
    t_prep = time.time()
    prep_result: dict = {}
    stop_prep_pump = threading.Event()

    def _prep_pump():
        while not stop_prep_pump.is_set():
            try:
                protocol.loop()
            except Exception:
                return

    def _do_prep():
        try:
            prep_result['data'] = _prepare_frame(image_path, width, height)
        except Exception as exc:
            prep_result['error'] = exc

    prep_pump = threading.Thread(target=_prep_pump, name='frame-prep-pump', daemon=True)
    prep_worker = threading.Thread(target=_do_prep, name='frame-prep', daemon=True)
    prep_pump.start()
    prep_worker.start()
    prep_worker.join()
    stop_prep_pump.set()
    try:
        from atemwire.transport import Wakeup
        protocol.transport.thread_recv_queue.put(Wakeup())
    except Exception:
        pass
    prep_pump.join(timeout=2.0)

    if 'error' in prep_result:
        e = prep_result['error']
        log_append(f"slot {slot_user}: FAIL — frame prep: {e}")
        return ItemResult(slot=slot, image_path=image_path, success=False,
                          error=f"frame prep failed: {e}")
    atem_data = prep_result['data']
    log_append(f"slot {slot_user}: frame prep took {time.time() - t_prep:.2f}s")

    expected_hash = hashlib.md5(atem_data).digest()
    pre_hash = _slot_hash(protocol, slot)

    upload_done = {'flag': False}

    def _on_done(store, uploaded_slot):
        if store == 0 and uploaded_slot == slot:
            upload_done['flag'] = True

    done_id = protocol.on('upload-done', _on_done)

    def _fail_and_release(reason: str, **kw) -> ItemResult:
        # A failed/abandoned upload leaves the protocol's transfer lane
        # wedged (transfer_requested stuck, store lock held) — every later
        # item in this batch would queue behind the corpse and time out
        # too. abort_transfers resets the lane AND releases the store
        # lock on the switcher, so the next item starts clean.
        try:
            protocol.abort_transfers()
        except Exception:
            pass
        log_append(f"slot {slot_user}: FAIL — {reason}")
        return ItemResult(slot=slot, image_path=image_path, success=False,
                          error=reason, **kw)

    t_upload = time.time()
    try:
        try:
            protocol.upload(store=0, index=slot, data=atem_data, compress=True, name=name)
        except Exception as e:
            return _fail_and_release(f"upload() raised: {e}")

        while not upload_done['flag']:
            if canceller.is_set():
                return _fail_and_release("cancelled during upload",
                                         upload_seconds=time.time() - t_upload)
            if time.time() - t_upload > UPLOAD_TIMEOUT:
                return _fail_and_release(
                    f"upload timeout after {UPLOAD_TIMEOUT:.0f}s — likely lock never "
                    f"acquired or transfer stalled",
                    upload_seconds=time.time() - t_upload)
            # ticked: a stalled transfer's quiet wire must not block this
            # loop past the deadline checks above (2026-07-06).
            ticked_pump(protocol, idle_sleep=0.02)
    finally:
        try:
            protocol.off('upload-done', done_id)
        except Exception:
            pass
    upload_duration = time.time() - t_upload

    # Verify via MPfe hash equality.
    settle_start = time.time()
    if pre_hash == expected_hash:
        t_deadline = settle_start + REUPLOAD_SETTLE
        while time.time() < t_deadline:
            if canceller.is_set():
                break
            ticked_pump(protocol, idle_sleep=0.02)
        settle = time.time() - settle_start
        log_append(f"slot {slot_user}: PASS upload={upload_duration:.2f}s "
                   f"settle={settle:.2f}s (pre==expected)")
        return ItemResult(slot=slot, image_path=image_path, success=True,
                          upload_seconds=upload_duration, settle_seconds=settle,
                          content_hash=expected_hash.hex())

    while time.time() - settle_start < HASH_SETTLE_TIMEOUT:
        if canceller.is_set():
            return ItemResult(slot=slot, image_path=image_path, success=False,
                              upload_seconds=upload_duration,
                              settle_seconds=time.time() - settle_start,
                              error="cancelled during hash verify")
        ticked_pump(protocol, idle_sleep=0.02)
        current = _slot_hash(protocol, slot)
        if current == expected_hash:
            break
        if (current and pre_hash and current != pre_hash and current != expected_hash):
            settle = time.time() - settle_start
            err = (f"hash mismatch: expected {expected_hash.hex()[:16]}, "
                   f"got {current.hex()[:16]}")
            log_append(f"slot {slot_user}: FAIL — {err}")
            return ItemResult(slot=slot, image_path=image_path, success=False,
                              upload_seconds=upload_duration, settle_seconds=settle,
                              error=err)
        time.sleep(0.02)

    settle = time.time() - settle_start
    final = _slot_hash(protocol, slot)
    if final != expected_hash:
        err = (f"silent failure: hash never settled to expected "
               f"(still {final.hex()[:16] if final else 'None'})")
        log_append(f"slot {slot_user}: FAIL — {err}")
        return ItemResult(slot=slot, image_path=image_path, success=False,
                          upload_seconds=upload_duration, settle_seconds=settle, error=err)

    log_append(f"slot {slot_user}: PASS upload={upload_duration:.2f}s "
               f"settle={settle:.2f}s")
    return ItemResult(slot=slot, image_path=image_path, success=True,
                      upload_seconds=upload_duration, settle_seconds=settle,
                      content_hash=expected_hash.hex())


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _apply_macro_xml(protocol, macro_xml_path: str,
                     log_append: Callable[[str], None]) -> Optional[ItemResult]:
    """Apply only the <MacroPool> section of ``macro_xml_path`` over an
    already-connected ``AtemProtocol``.

    Returns ``None`` on success or a synthetic ``ItemResult(slot=-1,
    success=False)`` on failure so the caller's "all-succeeded" status
    aggregation naturally rolls it in. Logs each step via ``log_append``.

    Used for Hyperdeck->Media Pool content changes where the new macro
    set replaces references from a camera input (the Hyperdeck feed) to
    media player slots loaded by the same Content Change.

    Loop-pump contract: ``upload_macro_bytecode`` blocks on
    ``protocol.upload(...)`` and waits for an ``upload-done`` event.
    Event dispatch only happens inside ``protocol.loop()``. The image
    upload pumps the loop itself inside its busy-wait, but macro apply
    delegates to ``Profile.apply`` which doesn't pump. We spawn a
    short-lived daemon thread that pumps ``protocol.loop()`` for the
    duration of the apply, then wake it with a ``Wakeup`` sentinel and
    join. The pump thread takes the role ``ATEMConnection``'s worker
    thread normally plays — without it every macro upload times out at
    10s waiting for ``upload-done``.
    """
    name = os.path.basename(macro_xml_path)
    log_append(f"applying macros from {name}")
    try:
        from atemwire import Profile, ApplyOptions
        from atemwire.transport import Wakeup
    except Exception as e:
        msg = f"macro apply: import failed: {e}"
        log_append(msg)
        return ItemResult(
            slot=-1, image_path=f'<macros:{macro_xml_path}>',
            success=False, error=msg,
        )

    try:
        profile = Profile.from_file(macro_xml_path)
    except Exception as e:
        msg = f"macro apply: could not load XML: {e}"
        log_append(msg)
        return ItemResult(
            slot=-1, image_path=f'<macros:{macro_xml_path}>',
            success=False, error=msg,
        )

    # Start the pump thread before kicking off apply.
    stop_pump = threading.Event()
    pump_error: list = []

    def _pump_loop():
        while not stop_pump.is_set():
            try:
                protocol.loop()
            except Exception as exc:
                pump_error.append(exc)
                return

    pump_thread = threading.Thread(
        target=_pump_loop, name='macro-apply-pump', daemon=True,
    )
    pump_thread.start()

    try:
        result = profile.apply(protocol, ApplyOptions.macros_only())
    except Exception as e:
        msg = f"macro apply: raised: {e}"
        log_append(msg)
        return ItemResult(
            slot=-1, image_path=f'<macros:{macro_xml_path}>',
            success=False, error=msg,
        )
    finally:
        stop_pump.set()
        # Unblock the pump's protocol.loop() — a Wakeup sentinel is a
        # no-op in dispatch (does NOT trip disconnect logic the way a
        # None sentinel would), so the protocol stays connected for the
        # caller's subsequent flush+close.
        try:
            protocol.transport.thread_recv_queue.put(Wakeup())
        except Exception:
            pass
        pump_thread.join(timeout=2.0)
        if pump_error:
            log_append(f"macro apply: pump thread raised: {pump_error[0]!r}")

    log_append(f"macro apply: {result.summary()}")
    if result.errors:
        for err in result.errors:
            log_append(f"macro apply error: {err}")
        return ItemResult(
            slot=-1, image_path=f'<macros:{macro_xml_path}>',
            success=False, error='; '.join(result.errors),
        )
    return None


def _run_for_single_ip(ip_address: str,
                       slot_paths: List[tuple],
                       skip_tally: bool,
                       canceller: _Canceller,
                       log_append: Callable[[str], None],
                       macro_xml_path: Optional[str] = None) -> List[ItemResult]:
    """Open one atemwire socket, upload all slot_paths tuples for this IP.

    If ``macro_xml_path`` is provided and every image upload succeeded,
    the XML's <MacroPool> section is applied to the ATEM on the same
    socket after the last image, before close. Skipped on partial image
    failure — broadcasting against new macros that point at stale image
    slots is worse than broadcasting against old macros + new images
    (the latter still plays the old hyperdeck feed, which is the
    pre-change state).
    """
    out: List[ItemResult] = []

    t_setup = time.time()
    # Connect with retries: the control page gives its pool connect 3
    # attempts and the watcher retries forever, but this path used to give
    # up after ONE wait_ready timeout — so a switcher that was momentarily
    # busy (ASC operating on it, mid-recovery, session churn) failed the
    # whole job where a second attempt seconds later would have landed.
    # Each attempt uses a fresh protocol/socket; a half-open attempt is
    # closed (close_session no-ops below ESTABLISHED).
    protocol = None
    t_connect = time.time()
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        # aggressive_drain=True replaces the old _install_fast_send
        # monkeypatch — bulk-upload throughput via whole-queue drain per
        # queue_trigger.
        candidate = AtemProtocol(ip=ip_address, aggressive_drain=True)
        log_append(f"connecting to {ip_address} (attempt {attempt}/{CONNECT_ATTEMPTS})")
        candidate.connect()
        try:
            # Preserves the original 0.005s pump interval from this site.
            wait_ready(candidate, timeout=CONNECTION_TIMEOUT, pump_interval=0.005)
            protocol = candidate
            break
        except TimeoutError:
            _close_protocol(candidate)
            if attempt < CONNECT_ATTEMPTS:
                log_append(f"attempt {attempt} timed out — retrying in "
                           f"{CONNECT_RETRY_DELAY:.0f}s")
                time.sleep(CONNECT_RETRY_DELAY)
    if protocol is None:
        for slot, image_path in slot_paths:
            out.append(ItemResult(
                slot=slot, image_path=image_path,
                success=False,
                error=f"connection timeout ({CONNECT_ATTEMPTS} attempts)",
            ))
        return out

    # EVERYTHING from here runs under try/finally: whatever happens —
    # unexpected exception, SIGTERM-raised SystemExit mid-upload, a
    # missing mixerstate key — the protocol goodbye ALWAYS goes out.
    # A skipped goodbye is an abandoned session on the switcher, and an
    # abandoned session holds any store lock we took until the switcher
    # reaps it (or, on a corrupted session table, forever — the fossil
    # locks of 2026-07). Verified 2026-07-06: close_session frees a held
    # lock instantly, no explicit unlock needed.
    try:
        video_mode = protocol.mixerstate['video-mode']
        t_connected = time.time()
        log_append(f"connect+state-dump took {t_connected - t_connect:.2f}s")

        width, height = video_mode.get_resolution()
        log_append(f"video mode: {width}x{height} on {ip_address}")
        # A sighting for the equipment row (best-effort, never raises): the
        # content-change form validates stills against what was last seen here.
        try:
            from atem_control.sightings import record_video_mode
            record_video_mode(ip_address, video_mode.get_label())
        except Exception:
            logger.debug('video-mode sighting skipped for %s', ip_address, exc_info=True)

        for idx, (slot, image_path) in enumerate(slot_paths):
            if canceller.is_set():
                for remain_slot, remain_path in slot_paths[idx:]:
                    out.append(ItemResult(
                        slot=remain_slot, image_path=remain_path,
                        success=False, error="cancelled — batch terminated",
                    ))
                break
            t_item = time.time()
            item_result = _upload_one(
                protocol, slot, image_path, width, height,
                canceller, log_append, skip_tally,
            )
            log_append(f"slot {slot + 1}: total item time {time.time() - t_item:.2f}s")
            out.append(item_result)

        # Macro apply runs on the same socket after the last image. Skipped on
        # cancel or partial-failure — see _apply_macro_xml docstring. The
        # ``all(...)`` check is vacuously True for an empty ``out`` (no images
        # attempted = macros-only Media Pool -> Hyperdeck submission), which
        # is exactly what we want — proceed to the macro apply.
        if macro_xml_path and not canceller.is_set():
            if all(r.success for r in out):
                macro_result = _apply_macro_xml(protocol, macro_xml_path, log_append)
                if macro_result is not None:
                    out.append(macro_result)
            else:
                log_append("skipping macro apply: not all image uploads succeeded")

        # Flush pending outbound for a moment so the lock release etc. goes
        # out before we close the socket.
        #
        # Ticked (2026-07-06, found live by lock_hygiene_smoke): on a
        # session with no consumer-visible traffic, a bare protocol.loop()
        # blocks in thread_recv_queue.get() indefinitely — keepalive pings
        # keep the UDP thread's select from ever timing out but never reach
        # the consumer queue. This "0.3s" flush then wedged the replica
        # forever. ticked_pump guarantees every loop() returns.
        t_flush = time.time()
        while time.time() - t_flush < 0.3:
            try:
                ticked_pump(protocol, idle_sleep=0.02)
            except Exception:
                break
    finally:
        t_close = time.time()
        _close_protocol(protocol)
        log_append(f"close took {time.time() - t_close:.2f}s")

    return out


def execute_upload(items: List[tuple],
                   *,
                   skip_tally: bool,
                   macro_xml_path: Optional[str] = None,
                   macro_only_ips: Optional[List[str]] = None,
                   check_cancelled: Optional[Callable[[], bool]] = None,
                   log_append: Optional[Callable[[str], None]] = None,
                   log_line_prefix: str = '') -> List[ItemResult]:
    """Upload N images to M ATEMs. Items can span multiple IPs; the function
    groups by IP internally and opens one atemwire socket per unique IP.

    :param items: list of ``(ip, slot, image_path)`` tuples. ``slot`` is
        0-indexed. May be empty for a macros-only submission (see
        ``macro_only_ips``).
    :param skip_tally: if True, bypass cycle-wait tally safety for every item.
        Tally is a property of the upload context (drag-drop vs content-change
        + game_type), not of individual items — see callers.
    :param macro_xml_path: optional absolute filesystem path to an ATEM
        Software Control profile XML. When set and every image for an IP
        uploaded successfully, the XML's <MacroPool> section is applied
        to that IP on the same socket. Used for Hyperdeck->Media Pool
        macro swaps. A macro apply failure appears in the returned list
        as a synthetic ``ItemResult(slot=-1, success=False)``.
    :param macro_only_ips: IPs to apply macros to even though no image
        items were supplied for them. Used by the Media Pool->Hyperdeck
        offline-Hyperdeck flow where the operator confirmed the
        Hyperdeck is already set up locally and only the macros need
        pushing. Must be paired with ``macro_xml_path``; ignored
        otherwise.
    :param check_cancelled: optional callable returning True to cancel. Use
        this to poll DB-backed cancellation state (e.g. task.status ==
        'cancelled'). The upload aborts at the next checkpoint.
    :param log_append: optional callable receiving each log line. If None, an
        internal list is kept (no external destination).
    :param log_line_prefix: prepended to each log line before log_append, used
        for correlating with task IDs in the centralized log.

    Returns one ItemResult per input item, in input order. When
    ``macro_xml_path`` is set and macros fail to apply, an extra
    synthetic result is appended at the end (per IP for which the apply
    was attempted).
    """
    canceller = _Canceller(check_cancelled)

    sink: Callable[[str], None]
    if log_append is not None:
        def sink(msg: str) -> None:
            line = f"{log_line_prefix}{msg}" if log_line_prefix else msg
            log_append(line)
            logger.info(msg)
    else:
        def sink(msg: str) -> None:
            logger.info(msg)

    # Preserve input order across the grouped per-IP runs.
    grouped: dict = {}
    order: List[tuple] = []
    for idx, (ip, slot, path) in enumerate(items):
        order.append((ip, slot, path))
        grouped.setdefault(ip, []).append((idx, slot, path))

    # Macros-only IPs (Media Pool -> Hyperdeck offline case): include
    # them in the grouped dict with empty image lists so
    # ``_run_for_single_ip`` opens a connection and runs the macro
    # apply step. Only meaningful when ``macro_xml_path`` is also set.
    if macro_only_ips and macro_xml_path:
        for ip in macro_only_ips:
            grouped.setdefault(ip, [])

    per_key_result: dict = {}
    extra_results: List[ItemResult] = []  # macro-apply synthetic rows
    for ip, indexed_tuples in grouped.items():
        slot_paths = [(slot, path) for _, slot, path in indexed_tuples]
        if canceller.is_set():
            for orig_idx, slot, path in indexed_tuples:
                per_key_result[orig_idx] = ItemResult(
                    slot=slot, image_path=path,
                    success=False, error="cancelled — batch terminated",
                )
            continue

        t_ip_start = time.time()
        results = _run_for_single_ip(
            ip, slot_paths, skip_tally, canceller, sink,
            macro_xml_path=macro_xml_path,
        )
        sink(f"=== {ip}: _run_for_single_ip total {time.time() - t_ip_start:.2f}s for {len(slot_paths)} item(s)")
        # _run_for_single_ip may append one synthetic macro-apply result.
        # Map the image results back to their original input indices; keep
        # the macro result on the side for end-of-list append.
        image_results = results[:len(indexed_tuples)]
        macro_results = results[len(indexed_tuples):]
        for (orig_idx, _, _), result in zip(indexed_tuples, image_results):
            per_key_result[orig_idx] = result
        extra_results.extend(macro_results)

    ordered = [per_key_result[i] for i in range(len(order))]
    return ordered + extra_results


def _close_protocol(protocol: AtemProtocol) -> None:
    # Give the media lock back BEFORE the goodbye. An upload holds the store
    # lock per frame and a failed or abandoned one can leave it held; a lock
    # still ours at teardown is kept by the switcher for the rest of the
    # session's life, and refuses every later client that wants the media
    # store. That is a switcher that will not load a still, and an ATEM
    # Software Control slot spinning on a thumbnail it cannot download.
    # Synchronous, because the worker that would drain a queued command is
    # already going away (atemwire.protocol.release_locks_now).
    try:
        protocol.release_locks_now()
    except Exception:
        pass
    # Protocol-level goodbye BEFORE the socket close — an abandoned session
    # wedges the switcher (see transport.close_session).
    try:
        protocol.transport.close_session()
    except Exception:
        pass
    try:
        protocol.transport.sock.close()
    except Exception:
        pass
    # Free the SocketQueue's socketpair FDs too (L4, 2026-07-06).
    try:
        protocol.transport.thread_queue.close()
    except Exception:
        pass
