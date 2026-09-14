"""``ticked_pump`` must return even when the recv queue holds only what the
transport swallows.

The transport queues bare ``True`` markers; ``receive_packet`` skips them
with ``continue`` and blocks on the next ``get()``. The pump used to treat a
non-empty queue as busy and add no ``Wakeup`` — so a lone tick meant a
``loop()`` that never returned (a HyperDeck job stalled at its post-connect
pump, 2026-09-09). Modelled here with a fake that mimics exactly that
receive path, run under a hard timeout.
"""

import queue
import threading

import pytest

from atem_control.tally import ticked_pump
from atemwire.transport import Wakeup


class _Transport:
    def __init__(self):
        self.thread_recv_queue = queue.Queue()

    def receive_packet(self):
        while True:                     # the real loop: control packets are swallowed, then block
            item = self.thread_recv_queue.get()
            if item is True or item == 'ping':
                continue
            return item


class _Protocol:
    def __init__(self):
        self.transport = _Transport()
        self.got = []

    def loop(self):
        self.got.append(self.transport.receive_packet())


def _pump_with_timeout(p, seconds=2.0):
    done = threading.Event()
    def run():
        ticked_pump(p); done.set()
    threading.Thread(target=run, daemon=True).start()
    return done.wait(seconds)


def test_an_empty_queue_gets_a_wakeup_and_returns():
    p = _Protocol()
    assert _pump_with_timeout(p)
    assert isinstance(p.got[0], Wakeup)


def test_a_queue_holding_only_ticks_still_returns():
    p = _Protocol()
    p.transport.thread_recv_queue.put(True)
    p.transport.thread_recv_queue.put(True)
    assert _pump_with_timeout(p), 'loop() blocked behind the ticks — no Wakeup was queued'
    assert isinstance(p.got[0], Wakeup)


def test_a_queue_of_keepalive_pings_still_returns():
    # The live case: an idle switcher pings every second, so the queue is
    # never empty and nothing in it ever reaches the consumer.
    p = _Protocol()
    for _ in range(3):
        p.transport.thread_recv_queue.put('ping')
    assert _pump_with_timeout(p), 'loop() ate the pings and blocked — no Wakeup was queued'
    assert isinstance(p.got[0], Wakeup)


def test_a_long_queue_is_a_transfer_and_gets_no_sentinel():
    p = _Protocol()
    for i in range(20):
        p.transport.thread_recv_queue.put('packet%d' % i)
    assert _pump_with_timeout(p)
    assert p.got == ['packet0']                # drained at full speed, no Wakeup queued
    assert p.transport.thread_recv_queue.qsize() == 19
