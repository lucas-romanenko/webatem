"""Audio meters re-arm when the switcher session is replaced in place
(webatem#38, piece 3).

The switcher streams Fairlight meter levels (FMLv/FDLv) only after SFLN
(``enable_fairlight_levels``). After about 5 s of silence the transport
re-handshakes: a new switcher session that has not had SFLN, on the SAME
``AtemProtocol`` object, which raises ``connected`` once the new state dump is
complete. The consumer re-armed only when the protocol object changed, so after
an in-place re-handshake the meters froze at their last levels. Since bmdwire
1.4.0 that silence timeout also fires while this side keeps sending
(bmdwire#11), so the re-handshake is the normal path for a switcher that went
quiet.

These use bmdwire's real ``AtemProtocol`` for event registration and dispatch
(``on`` / ``off`` / ``_raise``, as ``loop()`` raises them), so the event name
and its call signature are the library's own. The connection is a stand-in
whose ``send`` records commands; nothing touches the network.
"""
import asyncio
import types

import pytest

from atem_control.control import consumer as consumer_mod
from atem_control.control.consumer import ATEMConsumer
from atemwire.messages.fairlight import SendFairlightLevelsCommand
from atemwire.protocol import AtemProtocol

IP = '192.0.2.10'


def _protocol():
    p = AtemProtocol('127.0.0.1')
    p.transport.sock.close()          # never used; free the socket now
    return p


def _conn(protocol, sent):
    return types.SimpleNamespace(protocol=protocol, send=sent.append)


def _consumer(conn):
    c = ATEMConsumer.__new__(ATEMConsumer)   # skip __init__ / channel scope
    c.connection = conn
    c.current_ip = IP
    c._audio_subscribed = False
    c._meter_handler_ids = []
    c._meter_protocol = None
    c._meter_loop = None
    c._meter_batch_strips = {}
    c._meter_batch_master = None
    c._meter_batch_pending = False
    return c


def _subscribe(c):
    # _subscribe_audio_meters captures the running loop for the meter
    # handlers; give it one, as the websocket consumer has.
    async def run():
        c._subscribe_audio_meters()
    asyncio.run(run())


def _sfln(sent):
    return [cmd.enable for cmd in sent if isinstance(cmd, SendFairlightLevelsCommand)]


@pytest.fixture(autouse=True)
def _fresh_refcount(monkeypatch):
    monkeypatch.setattr(consumer_mod, '_meter_subscribers', {})


def test_a_new_session_on_the_same_protocol_rearms_the_meters():
    proto, sent = _protocol(), []
    c = _consumer(_conn(proto, sent))
    _subscribe(c)
    assert _sfln(sent) == [True]                 # the 0→1 subscribe

    # The transport re-handshakes: same protocol object, new session.
    proto._raise('disconnected')
    proto._raise('connected')

    assert _sfln(sent) == [True, True], (
        'the new switcher session has not had SFLN: the meters stay frozen '
        'unless the consumer re-sends it on connected'
    )
    assert proto is c._meter_protocol            # nothing else noticed


def test_every_subscribed_tab_rearms():
    # With two tabs on one switcher the per-IP refcount sends SFLN only on
    # 0→1, so a re-subscribe would send nothing; each consumer re-arms.
    proto, sent = _protocol(), []
    a, b = _consumer(_conn(proto, sent)), _consumer(_conn(proto, sent))
    _subscribe(a)
    _subscribe(b)
    assert _sfln(sent) == [True]

    proto._raise('connected')
    assert _sfln(sent) == [True, True, True]


def test_after_unsubscribe_a_new_session_sends_nothing():
    proto, sent = _protocol(), []
    c = _consumer(_conn(proto, sent))
    _subscribe(c)
    c._unsubscribe_audio_meters()
    assert _sfln(sent) == [True, False]          # 1→0 turns the stream off
    assert not proto.callbacks.get('connected'), 'the handler must come off'

    proto._raise('connected')
    assert _sfln(sent) == [True, False]


def test_a_replaced_protocol_gets_the_handler_too():
    # A fresh connect() after the worker died builds a new protocol: the
    # poll loop's identity check re-registers there, connected included.
    old, sent = _protocol(), []
    conn = _conn(old, sent)
    c = _consumer(conn)
    _subscribe(c)

    new = conn.protocol = _protocol()
    c._rearm_audio_meters_if_reconnected()
    assert _sfln(sent) == [True, True]           # the identity re-arm
    assert c._meter_protocol is new

    new._raise('connected')                      # and a later re-handshake
    assert _sfln(sent) == [True, True, True]

    c._unsubscribe_audio_meters()
    assert not new.callbacks.get('connected')
