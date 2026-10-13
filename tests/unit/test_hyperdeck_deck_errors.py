"""HyperDeck call sites against bmdwire 1.4.0's client.

hyperdeckwire 1.4.0 changed what a deck failure looks like to a caller:
  * a request that times out CLOSES the connection before raising
    ``TimeoutError`` (bmdwire#18); the same object then raises
    ``OSError('not connected')`` until ``connect()``;
  * ``connect()`` raises ``HyperdeckError`` when the deck refuses the
    connection (``120 connection failed``, bmdwire#21);
  * a command with a line break in it raises ``ValueError`` before anything is
    sent (bmdwire#20).

The only call sites are ``with_deck`` (the per-IP cached deck) and the three
views on it. These drive both through the real client: the deck is a scripted
socket, and a stand-in for the client's ``time.monotonic`` moves on when the
deck stays silent, so a timeout costs no real time.
"""
import functools
import socket

import pytest

import hyperdeckwire.client as hd_client
from atem_control.hyperdeck import connection as deck_conn
from hyperdeckwire import Hyperdeck

IP = '192.0.2.40'
GREETING = (b'500 connection info:\r\nprotocol version: 1.13\r\n'
            b'model: HyperDeck Studio HD Mini\r\n\r\n')
OK = b'200 ok\r\n'
STOPPED = b'208 transport info:\r\nstatus: stopped\r\n\r\n'
SILENCE = None          # a script item: the deck says nothing from here on


class _Clock:
    now = 1000.0

    def monotonic(self):
        return self.now


class _Deck:
    """One TCP connection to the deck: ``script`` is what it sends, chunk by
    chunk, in answer to what the client does; from ``SILENCE`` on, every
    wait times out. A non-blocking read (``settimeout(0)``) finds nothing waiting."""

    def __init__(self, script, clock):
        self.script = list(script)
        self.clock = clock
        self.sent = b''
        self.closed = False
        self._timeout = None
        self._inbox = b''

    def settimeout(self, t):
        self._timeout = t

    def sendall(self, data):
        if self.closed:
            raise OSError('closed')
        self.sent += data

    def recv(self, n):
        if self.closed:
            return b''
        if not self._inbox:
            if self._timeout == 0:
                raise BlockingIOError('nothing waiting')
            if not self.script:
                return b''
            if self.script[0] is SILENCE:
                # Stays at the head: every wait times out until the
                # client's deadline passes.
                self.clock.now += self._timeout
                raise socket.timeout('timed out')
            self._inbox = self.script.pop(0)
        chunk, self._inbox = self._inbox[:n], self._inbox[n:]
        return chunk

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def decks(monkeypatch):
    """Queue the connections the deck will accept, in order."""
    clock = _Clock()
    monkeypatch.setattr(hd_client.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(deck_conn, '_DECKS', {})
    monkeypatch.setattr(deck_conn, '_LOCKS', {})
    monkeypatch.setattr(deck_conn, '_LAST_USED', {})
    pending = []

    def factory(*_a, **_kw):
        if not pending:
            raise ConnectionRefusedError('no deck listening')
        return pending.pop(0)

    monkeypatch.setattr(deck_conn, 'Hyperdeck',
                        functools.partial(Hyperdeck, socket_factory=factory))

    def accept(*script):
        d = _Deck(script, clock)
        pending.append(d)
        return d
    return accept


def test_the_client_closes_itself_on_a_timeout(decks):
    # What with_deck relies on: the timeout is an OSError, and the client
    # has already closed the connection when it arrives.
    deck = decks(GREETING, OK, SILENCE)
    hd = deck_conn._connect(IP)
    with pytest.raises(TimeoutError):
        hd.transport_info()
    assert deck.closed and hd._sock is None
    with pytest.raises(OSError, match='not connected'):
        hd.transport_info()


def test_a_timeout_reconnects_once_and_answers(decks):
    # The cached connection's answer never comes; the reconnect answers.
    first = decks(GREETING, OK, SILENCE)            # greeting, remote: enable
    second = decks(GREETING, OK, STOPPED)
    assert deck_conn.with_deck(IP, lambda hd: hd.transport_info()) == {'status': 'stopped'}
    assert first.closed, 'the timed-out connection is closed, not reused'
    assert b'quit\n' in first.sent
    assert deck_conn._DECKS[IP]._sock is second


@pytest.mark.django_db
def test_a_timeout_on_both_attempts_is_a_502(decks, client):
    decks(GREETING, OK, SILENCE)
    decks(GREETING, OK, SILENCE)
    r = client.get('/atem/hyperdeck/status/', {'ip': IP})
    assert r.status_code == 502
    assert 'error' in r.json()
    assert IP not in deck_conn._DECKS, 'a closed deck must not stay cached'


def test_the_next_request_after_a_failure_reconnects(decks):
    decks(GREETING, OK, SILENCE)
    decks(GREETING, OK, SILENCE)
    with pytest.raises(TimeoutError):
        deck_conn.with_deck(IP, lambda hd: hd.transport_info())
    decks(GREETING, OK, STOPPED)
    assert deck_conn.with_deck(IP, lambda hd: hd.transport_info()) == {'status': 'stopped'}


@pytest.mark.django_db
def test_a_refused_connection_is_a_502_naming_it(decks, client):
    refusal = decks(b'120 connection failed\r\n')
    r = client.get('/atem/hyperdeck/state/', {'ip': IP})
    assert r.status_code == 502
    assert '120' in r.json()['error']
    assert refusal.closed
    assert IP not in deck_conn._DECKS


@pytest.mark.django_db
def test_a_line_break_in_a_clip_id_is_refused_before_sending(decks, client):
    deck = decks(GREETING, OK, OK)                  # greeting, remote, (unused)
    deck_conn.with_deck(IP, lambda hd: None)        # cache a connected deck
    before = deck.sent
    r = client.post('/atem/hyperdeck/transport/',
                    {'ip': IP, 'action': 'play', 'clip_id': '1\nclips clear'},
                    content_type='application/json')
    assert r.status_code == 400
    assert deck.sent == before, 'nothing reaches the deck'
    assert deck_conn._DECKS[IP]._sock is deck, 'and the deck stays connected'
