"""Shared tally cycle-wait primitive.

Both the still / media-pool uploader (``uploader._wait_for_safe_upload_window``)
and the HyperDeck clip path (``hyperdeck_tally.wait_for_safe_window``) gate their
mutations on the same cold -> hot -> cold cycle. That loop and its timings live
here so the two callers can't drift; each supplies its own liveness predicate (a
media-pool slot vs a deck's switcher input) and its own log label.
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
    """
    sink = log or (lambda _line: None)
    start = time.time()

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

    # Phase 1 -- if currently live, wait until cold.
    if is_live():
        sink(f'{label}: currently live, waiting for cold...')
        while is_live():
            reason = aborted()
            if reason:
                sink(f'{label}: aborted ({reason})')
                return False
            ticked_pump(protocol, idle_sleep=TALLY_POLL_INTERVAL)

    # Phase 2 -- observe; a source going live here is a rotation, not a gap.
    sink(f'{label}: observing up to {int(observation)}s for live...')
    obs_start = time.time()
    went_live = False
    while time.time() - obs_start < observation:
        reason = aborted()
        if reason:
            sink(f'{label}: aborted ({reason})')
            return False
        ticked_pump(protocol)
        if is_live():
            went_live = True
            break
        time.sleep(TALLY_POLL_INTERVAL)

    if not went_live:
        sink(f'{label}: no activity in {int(observation)}s, safe')
        return True

    # Phase 3 -- went live during observation; wait for the trailing cold.
    sink(f'{label}: rotation detected, waiting for trailing cold...')
    while is_live():
        reason = aborted()
        if reason:
            sink(f'{label}: aborted ({reason})')
            return False
        ticked_pump(protocol, idle_sleep=TALLY_POLL_INTERVAL)

    sink(f'{label}: cycle complete, safe')
    return True
