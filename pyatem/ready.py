# SPDX-License-Identifier: LGPL-3.0-only
"""
Wait for a pyatem connection to complete its initial state dump.

"Ready" means: pyatem has fired the 'connected' event (which happens at
the END of the initial state dump — see protocol.py) AND 'video-mode' is
present in mixerstate. Almost every caller in this codebase used to open
its own socket, call protocol.connect(), then spin on this same two-part
condition with slightly different timeouts and sleep intervals.

The caller is responsible for calling ``protocol.connect()`` before
invoking ``wait_ready()``. This helper only waits; it does not open the
connection.
"""

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


class WaitAborted(Exception):
    """Raised by ``wait_ready()`` when ``stop_event`` fires before the
    connection becomes ready."""


def wait_ready(
    protocol,
    *,
    timeout: float = 8.0,
    pump_interval: float = 0.01,
    extra_settle: float = 0.0,
    stop_event: Optional[threading.Event] = None,
) -> dict:
    """Block until ``protocol`` has completed the ATEM's initial state dump.

    Pumps ``protocol.loop()`` at ``pump_interval`` until the 'connected'
    event has fired AND ``'video-mode'`` is present in ``mixerstate``, then
    optionally keeps pumping for ``extra_settle`` seconds to let late-
    arriving state fields (e.g. macro-properties) populate.

    :param protocol: an ``AtemProtocol`` whose ``connect()`` has been called.
    :param timeout: overall wait budget in seconds.
    :param pump_interval: sleep between ``protocol.loop()`` calls. Pass 0
        to busy-loop (the media pool watcher does this during connect).
    :param extra_settle: additional pumping seconds AFTER ready.
    :param stop_event: optional cancellation signal.

    :returns: ``protocol.mixerstate`` on success.
    :raises TimeoutError: if not ready within ``timeout`` seconds.
    :raises WaitAborted: if ``stop_event`` is set before ready.
    """
    # Fast path: already connected and initial state dumped.
    if getattr(protocol, 'connected', False) and 'video-mode' in protocol.mixerstate:
        if extra_settle > 0:
            _pump_for(protocol, extra_settle, pump_interval, stop_event)
        return protocol.mixerstate

    connected = {'flag': False}

    def _on_connected():
        connected['flag'] = True

    handler_id = protocol.on('connected', _on_connected)

    # The handler must come off on EVERY exit path — wait_ready runs per
    # connect attempt, and a leaked closure per attempt accumulates on
    # long-lived protocols (session-hygiene audit, 2026-07-06).
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise WaitAborted("stop_event set before ready")
            try:
                protocol.loop()
            except Exception as e:
                logger.debug("wait_ready: protocol.loop() error: %s", e)
            if (
                (connected['flag'] or getattr(protocol, 'connected', False))
                and 'video-mode' in protocol.mixerstate
            ):
                break
            time.sleep(pump_interval)
        else:
            raise TimeoutError(f"wait_ready: not ready within {timeout}s")
    finally:
        try:
            protocol.off('connected', handler_id)
        except Exception:
            pass

    if extra_settle > 0:
        _pump_for(protocol, extra_settle, pump_interval, stop_event)

    return protocol.mixerstate


def _pump_for(
    protocol,
    duration: float,
    pump_interval: float,
    stop_event: Optional[threading.Event],
) -> None:
    """Keep calling ``protocol.loop()`` for ``duration`` seconds."""
    deadline = time.time() + duration
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            protocol.loop()
        except Exception as e:
            logger.debug("_pump_for: protocol.loop() error: %s", e)
        time.sleep(pump_interval)
