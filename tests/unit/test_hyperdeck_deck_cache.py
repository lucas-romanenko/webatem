"""The HyperDeck panel's deck cache (2026-07-06 session hygiene, SH-20b/c).
Moved here from AV Server (2026-11-18), which tested this module from the
outside.

  * SH-20b: ``_connect`` closes the client when ``remote_enable`` raises
    after a successful ``connect()`` (``HyperdeckError`` isn't caught by
    ``with_deck``'s OSError handler, so before the fix every failed ~1 Hz
    poll abandoned a connected socket).
  * SH-20c: the per-IP deck cache evicts decks idle past
    ``DECK_IDLE_CLOSE_SECONDS`` (a lazy sweep at the start of any
    ``with_deck`` call, for OTHER ips), and the atexit hook closes every
    cached deck.
"""
import time

import pytest
from hyperdeckwire.client import HyperdeckError

from atem_control.hyperdeck import connection as deck_conn


class _FakeDeck:
    """Stand-in for hyperdeckwire.Hyperdeck in the connection-cache tests."""

    def __init__(self, ip, connect_timeout=None, read_timeout=None):
        self.ip = ip
        self.connected = False
        self.closed = False
        self.remote_enabled = False

    def connect(self):
        self.connected = True

    def remote_enable(self, enable=True):
        self.remote_enabled = enable

    def close(self):
        self.closed = True


@pytest.fixture
def fresh_deck_registry(monkeypatch):
    """Isolate the module-level deck cache so tests can't cross-pollute
    (or leave fakes behind for the real atexit hook)."""
    monkeypatch.setattr(deck_conn, '_DECKS', {})
    monkeypatch.setattr(deck_conn, '_LOCKS', {})
    monkeypatch.setattr(deck_conn, '_LAST_USED', {})


def _age_deck(ip):
    """Backdate a deck's last-use past the idle threshold (cheaper and more
    deterministic than monkeypatching time.monotonic)."""
    deck_conn._LAST_USED[ip] = (
        time.monotonic() - deck_conn.DECK_IDLE_CLOSE_SECONDS - 1.0
    )


# ---------------------------------------------------------------------------
# SH-20b: _connect closes the client when remote_enable raises
# ---------------------------------------------------------------------------

def test_connect_closes_deck_when_remote_enable_raises(monkeypatch, fresh_deck_registry):
    created = []

    class _RefusingDeck(_FakeDeck):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

        def remote_enable(self, enable=True):
            raise HyperdeckError(111, 'remote control disabled')

    monkeypatch.setattr(deck_conn, 'Hyperdeck', _RefusingDeck)
    with pytest.raises(HyperdeckError):
        deck_conn.with_deck('10.0.0.1', lambda hd: None)
    assert len(created) == 1
    assert created[0].connected          # connect() succeeded first
    assert created[0].closed             # ...so the failure must close it
    assert '10.0.0.1' not in deck_conn._DECKS  # no half-set-up cache entry


# ---------------------------------------------------------------------------
# SH-20c: idle eviction + atexit close of the deck cache
# ---------------------------------------------------------------------------

def test_idle_deck_evicted_when_another_deck_is_used(monkeypatch, fresh_deck_registry):
    monkeypatch.setattr(deck_conn, 'Hyperdeck', _FakeDeck)
    deck_conn.with_deck('10.0.0.1', lambda hd: None)
    deck_a = deck_conn._DECKS['10.0.0.1']

    _age_deck('10.0.0.1')
    deck_conn.with_deck('10.0.0.2', lambda hd: None)  # sweep runs here

    assert deck_a.closed
    assert '10.0.0.1' not in deck_conn._DECKS
    assert not deck_conn._DECKS['10.0.0.2'].closed  # the active deck stays


def test_recently_used_deck_survives_the_sweep(monkeypatch, fresh_deck_registry):
    monkeypatch.setattr(deck_conn, 'Hyperdeck', _FakeDeck)
    deck_conn.with_deck('10.0.0.1', lambda hd: None)
    deck_a = deck_conn._DECKS['10.0.0.1']

    deck_conn.with_deck('10.0.0.2', lambda hd: None)  # A is fresh — no evict

    assert not deck_a.closed
    assert '10.0.0.1' in deck_conn._DECKS


def test_sweep_skips_a_deck_whose_lock_is_held(monkeypatch, fresh_deck_registry):
    """A busy deck is by definition not idle — the non-blocking lock probe
    must leave it alone even if its last-use timestamp looks stale."""
    monkeypatch.setattr(deck_conn, 'Hyperdeck', _FakeDeck)
    deck_conn.with_deck('10.0.0.1', lambda hd: None)
    deck_a = deck_conn._DECKS['10.0.0.1']
    _age_deck('10.0.0.1')

    lock_a = deck_conn._lock_for('10.0.0.1')
    lock_a.acquire()
    try:
        deck_conn.with_deck('10.0.0.2', lambda hd: None)
        assert not deck_a.closed
        assert '10.0.0.1' in deck_conn._DECKS
    finally:
        lock_a.release()


def test_atexit_hook_closes_every_cached_deck(monkeypatch, fresh_deck_registry):
    monkeypatch.setattr(deck_conn, 'Hyperdeck', _FakeDeck)
    deck_conn.with_deck('10.0.0.1', lambda hd: None)
    deck_conn.with_deck('10.0.0.2', lambda hd: None)
    decks = list(deck_conn._DECKS.values())
    assert len(decks) == 2

    deck_conn._close_all_decks_at_exit()

    assert all(d.closed for d in decks)
    assert deck_conn._DECKS == {}
