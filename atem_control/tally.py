"""Shared tally cycle-wait primitive.

Both the still / media-pool uploader (``uploader._wait_for_safe_upload_window``)
and the HyperDeck clip path (``hyperdeck_tally.wait_for_safe_window``) gate their
mutations on the same cold -> hot -> cold cycle. That loop and its timings live
here so the two callers can't drift; each supplies its own liveness predicate (a
media-pool slot vs a deck's switcher input) and its own log label. Whether the
switcher's state was there to read is the cycle's own question, not the
predicate's: a session that drops inside the cycle starts it again.
"""

import time
from typing import Callable, Optional

from atemwire.transport import Wakeup

# Tally cycle-wait timings — single source of truth for every caller.
TALLY_POLL_INTERVAL = 0.05
TALLY_OBSERVATION_SECONDS = 40.0
TALLY_TIMEOUT = 300.0


def ticked_pump(protocol, idle_sleep: float = 0.0) -> None:
    """One ``protocol.loop()`` that is GUARANTEED to return.

    A bare ``loop()`` blocks in ``thread_recv_queue.get()`` until a
    consumer-visible packet arrives — and on a quiet established session
    none ever does (keepalive pings are consumed at transport level, and
    their arrival keeps the UDP thread's select timeout from firing).
    Every deadline/cancel check "between loop() calls" is unenforceable
    against that block: the uploader's 0.3s flush, the 90s upload timeout
    and the 300s tally timeout could all hang a replica forever (found
    live by lock_hygiene_smoke, 2026-07-06).

    When the recv queue is idle, enqueue a ``Wakeup`` sentinel (the same
    trick ATEMConnection.send() uses) and optionally pace with
    ``idle_sleep``; when packets are queued, drain at full speed with no
    tick and no sleep (the 2026-07-02 lesson: a fixed sleep per pump
    throttled 6000-packet transfers from ~2s to 60s+).
    """
    idle = True
    try:
        q = protocol.transport.thread_recv_queue
        # "Idle" cannot be judged by qsize(): on an ESTABLISHED session the
        # ATEM's keepalive pings land in this queue every second, and
        # ``receive_packet`` swallows those control packets with a
        # ``continue`` — straight into another blocking ``get()``. So a
        # queue that is never empty (always a ping in it) meant no sentinel
        # was ever queued, and loop() ate ping after ping until some other
        # session made the switcher emit a data packet — minutes, or never.
        # Found live: a HyperDeck job stalled at its post-connect pump
        # (2026-09-09), blocked 90 s until a second session connected.
        # A Wakeup behind a SHORT queue costs one early return; a long queue
        # is a real transfer and gets no sentinel (the 2026-07-02 lesson).
        idle = q.qsize() <= 8
        if idle:
            q.put(Wakeup())
    except Exception:
        # No real transport (test fakes) — treat as idle so the pacing
        # sleep below still runs (tests drive a virtual clock through it).
        pass
    if idle and idle_sleep:
        time.sleep(idle_sleep)
    protocol.loop()


def _pooled(conn) -> bool:
    """A pooled ``ATEMConnection`` (how a host calls ``_is_slot_safe``), not
    a bare ``AtemProtocol``: its worker thread pumps the loop and tracks the
    session, so it is asked rather than pumped."""
    return hasattr(conn, 'is_connected')


def _state_ready(conn) -> bool:
    """The switcher's state is there to read: its dump has finished and
    carried a video mode, the test ``atemwire.ready.wait_ready`` uses. A
    dropped session empties ``mixerstate`` while the transport re-handshakes
    in place, and the new dump refills it field by field, so anything short of
    this can be missing exactly the field that says a source is on air."""
    if 'video-mode' not in conn.mixerstate:
        return False
    if _pooled(conn):
        # is_ready (bmdwire >= 1.3) is False through a re-handshake, when
        # is_connected stays True; the media-pool watcher asks the same.
        return bool(getattr(conn, 'is_ready', conn.is_connected))
    # Set at the end-of-dump marker, cleared on a drop.
    return bool(getattr(conn, 'initialized', False))


class _StateWatch:
    """Says whether a reading of ``is_live`` came from switcher state that was
    there for the whole of it.

    When a session drops, bmdwire re-handshakes it in place (since 1.4.0, 5 s
    after the switcher's last packet, even while we send): the state is
    emptied, then REPLACED by a new dict that the new dump fills. Whatever a
    predicate answers across that is not a reading of the source. An empty
    state reads as off air to one predicate; another answers "live" because it
    cannot tell; the still check waits for the new dump and answers from it,
    with the gap behind it unseen. So a reading counts only if the state was
    ready (``_state_ready``) before and after it, and is the same dict before,
    after, and at the last reading that counted. The dict test catches a drop
    that came and went inside one reading.

    An object that reports no session at all (no ``mixerstate``, or neither
    ``initialized`` nor ``is_connected``, such as a test fake) cannot show a
    gap, so its readings always count, as they did before.
    """

    def __init__(self, protocol):
        self._protocol = protocol
        self._sees = (hasattr(protocol, 'mixerstate')
                      and (hasattr(protocol, 'initialized')
                           or hasattr(protocol, 'is_connected')))
        self._state = None      # the dict the last reading that counted came from

    def read(self, is_live: Callable[[], bool]) -> Optional[bool]:
        """``is_live()`` as the cycle may use it: True or False, or None when
        the reading does not count (a gap)."""
        if not self._sees:
            return bool(is_live())
        p = self._protocol
        ready_before, before = _state_ready(p), p.mixerstate
        live = is_live()
        after = p.mixerstate
        counts = (ready_before and _state_ready(p) and after is before
                  and (self._state is None or after is self._state))
        self._state = after if counts else None
        return bool(live) if counts else None


# The cycle's phases (see wait_for_safe_cycle).
_START, _WAIT_COLD, _OBSERVE, _TRAILING = 'start', 'wait-cold', 'observe', 'trailing'


def wait_for_safe_cycle(
    protocol,
    is_live: Callable[[], bool],
    *,
    is_cancelled: Optional[Callable[[], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
    label: str = 'source',
    observation: float = TALLY_OBSERVATION_SECONDS,
    timeout: float = TALLY_TIMEOUT,
) -> bool:
    """Block until ``is_live()`` shows a stable cold window, then return True.
    Return False on cancel or after ``timeout`` seconds total.

    The cold -> hot -> cold cycle: if the source is currently live, wait for it
    to go cold; then OBSERVE for ``observation`` seconds -- if it goes live in
    that window it's a rotation (a macro cycling the source on/off air), so wait
    for the trailing cold; if nothing goes live it's already a stable safe gap.
    This beats a naive "wait until not live", which a rotating macro can satisfy
    in a transient gap that isn't actually safe to mutate in.

    ``is_live`` is the per-caller liveness predicate; ``protocol.loop()`` is
    pumped between polls so the mixerstate it reads stays fresh.

    A gap starts the cycle again. While the session has no state (it dropped
    and is re-handshaking, see ``_StateWatch``), nobody can see the source, and
    a rotation can go on and off air unseen. Readings taken then are ignored,
    whatever the predicate answered, and when the state is back the cycle
    forgets what it saw before the gap and starts from the top with the first
    reading of the new state. For each phase the gap interrupts:

    - waiting for cold: nothing has been claimed yet, so starting again only
      means going on from the new state.
    - observing: the window has unwatched seconds in it, so a full new window
      is watched, from when the state is back.
    - waiting for the trailing cold: the source may have gone cold during the
      gap, so how much of its off-air time is left is unknown. A first reading
      off air is not taken as the trailing cold; a full window is watched.

    A first reading on air after a gap is treated as on air at the start (wait
    for cold, then a full window), not as a rotation: the cold before it was
    not seen. The timeout counts from the first call and a restart never
    extends it, so a session that keeps dropping ends in False (not safe), not
    in a wait without end. A predicate that raises still ends the cycle, as
    before (the still check does when the state stays away too long).
    """
    sink = log or (lambda _line: None)
    start = time.time()
    watch = _StateWatch(protocol)

    def aborted() -> Optional[str]:
        if is_cancelled is not None:
            try:
                if is_cancelled():
                    return 'cancelled'
            except Exception:
                pass
        if time.time() - start >= timeout:
            return f'total wait exceeded {timeout:.0f}s'
        return None

    phase, obs_start, gap_since = _START, 0.0, None
    while True:
        reason = aborted()
        if reason:
            sink(f'{label}: aborted ({reason})')
            return False
        read_at = time.time()
        live = watch.read(is_live)
        now = time.time()

        if live is None:
            # A gap. Note when it began (the reading may have waited it out).
            if gap_since is None:
                gap_since = read_at
                sink(f'{label}: switcher state gone (session reconnecting), '
                     f'the source cannot be seen until it is back')
            phase = _START
        else:
            if gap_since is not None:
                sink(f'{label}: switcher state back after {now - gap_since:.1f}s, '
                     f'starting the cycle again')
                gap_since = None
            if phase == _START and live:
                # Phase 1 -- currently live, wait until cold.
                sink(f'{label}: currently live, waiting for cold...')
                phase = _WAIT_COLD
            elif phase in (_START, _WAIT_COLD) and not live:
                sink(f'{label}: observing up to {int(observation)}s for live...')
                phase, obs_start = _OBSERVE, now
            elif phase == _OBSERVE:
                # Phase 2 -- a source going live here is a rotation, not a gap.
                if live:
                    sink(f'{label}: rotation detected, waiting for trailing cold...')
                    phase = _TRAILING
                elif now - obs_start >= observation:
                    sink(f'{label}: no activity in {int(observation)}s, safe')
                    return True
            elif phase == _TRAILING and not live:
                # Phase 3 -- went live while observed; this is the trailing cold.
                sink(f'{label}: cycle complete, safe')
                return True

        ticked_pump(protocol, idle_sleep=TALLY_POLL_INTERVAL)
