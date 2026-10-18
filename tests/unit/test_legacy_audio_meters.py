"""Audio meters on a classic (legacy, pre-Fairlight) audio mixer.

A switcher whose pushed state says ``audio.mixer == 'legacy'`` streams its
levels as AMLv after SALN (``enable_audio_levels``), not FMLv/FDLv after SFLN.
The consumer arms whichever the state names, re-arms it after a reconnect as
the Fairlight path does (test_audio_meter_rearm.py), and turns AMLv into the
same ``audio_meter_batch`` the panel already paints: strip ``"<source>.0"``
per input, and the master. A subscribe that comes before the state has named
the mixer waits for the first push that does.

bmdwire's real ``AtemProtocol`` raises the events; the connection is a
stand-in whose ``send`` records commands. The AMLv bytes follow bmdwire's
parser; no legacy switcher has checked them.
"""
import asyncio
import struct
import types

import pytest

from atem_control.control import consumer as consumer_mod
from atem_control.control.consumer import ATEMConsumer
from atemwire.messages.audio_legacy import SendAudioLevelsCommand
from atemwire.messages.fairlight import SendFairlightLevelsCommand
from atemwire.protocol import AtemProtocol

IP = '192.0.2.20'
LEGACY = {'audio': {'mixer': 'legacy'}}
FAIRLIGHT = {'audio': {'mixer': 'fairlight'}}


def _protocol():
    p = AtemProtocol('127.0.0.1')
    p.transport.sock.close()          # never used; free the socket now
    return p


def _conn(protocol, sent):
    return types.SimpleNamespace(protocol=protocol, send=sent.append)


def _consumer(conn, state):
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
    c._meter_mixer = None
    c._meter_pending = False
    c.last_known_state = state
    return c


def _run(fn):
    async def run():
        fn()
    asyncio.run(run())


def _saln(sent):
    return [cmd.enable for cmd in sent if isinstance(cmd, SendAudioLevelsCommand)]


def _sfln(sent):
    return [cmd.enable for cmd in sent if isinstance(cmd, SendFairlightLevelsCommand)]


@pytest.fixture(autouse=True)
def _fresh_refcount(monkeypatch):
    monkeypatch.setattr(consumer_mod, '_meter_subscribers', {})


def test_a_classic_mixer_arms_saln_and_amlv():
    proto, sent = _protocol(), []
    c = _consumer(_conn(proto, sent), LEGACY)
    _run(c._subscribe_audio_meters)
    assert (_saln(sent), _sfln(sent)) == ([True], [])
    assert set(proto.callbacks) == {'change:audio-meter-levels', 'connected'}
    assert all(proto.callbacks[e] for e in proto.callbacks)

    c._unsubscribe_audio_meters()
    assert (_saln(sent), _sfln(sent)) == ([True, False], [])
    assert not any(proto.callbacks.values()), 'every handler must come off'
    assert c._meter_mixer is None


def test_classic_meters_rearm_after_a_reconnect():
    old, sent = _protocol(), []
    conn = _conn(old, sent)
    c = _consumer(conn, LEGACY)
    _run(c._subscribe_audio_meters)

    old._raise('connected')                      # in-place re-handshake
    assert _saln(sent) == [True, True]

    new = conn.protocol = _protocol()            # a fresh connect()
    c._rearm_audio_meters_if_reconnected()
    assert _saln(sent) == [True, True, True]
    assert new.callbacks.get('change:audio-meter-levels')
    assert not new.callbacks.get('change:fairlight-meter-levels')
    new._raise('connected')
    assert _saln(sent) == [True, True, True, True]
    assert _sfln(sent) == []


@pytest.mark.parametrize('first', [{}, {'audio': {'mixer': 'unknown'}}, {'is_connected': False}],
                         ids=['no state yet', 'unknown', 'no audio'])
@pytest.mark.parametrize('then, saln, sfln', [
    (LEGACY, [True], []),
    (FAIRLIGHT, [], [True]),
], ids=['legacy', 'fairlight'])
def test_a_subscribe_before_the_mixer_is_known_waits_for_the_push(first, then, saln, sfln):
    proto, sent = _protocol(), []
    c = _consumer(_conn(proto, sent), first)

    async def run():
        c._subscribe_audio_meters()
        assert sent == [] and c._meter_pending and not c._audio_subscribed
        c._subscribe_audio_meters()              # still idempotent
        c._arm_deferred_audio_meters()           # a push that says nothing new
        assert sent == []
        c.last_known_state = then                # the push that names it
        c._arm_deferred_audio_meters()
    asyncio.run(run())

    assert (_saln(sent), _sfln(sent)) == (saln, sfln)
    assert c._audio_subscribed and not c._meter_pending


def test_an_unsubscribe_while_waiting_sends_nothing():
    proto, sent = _protocol(), []
    c = _consumer(_conn(proto, sent), {})
    _run(c._subscribe_audio_meters)
    c._unsubscribe_audio_meters()
    c.last_known_state = LEGACY
    _run(c._arm_deferred_audio_meters)
    assert sent == [] and not c._audio_subscribed and not c._meter_pending
    assert consumer_mod._meter_subscribers == {}


def test_a_state_from_bmdwire_before_1_6_is_fairlight():
    # Audio but no 'mixer' key: Fairlight, armed at once as it always was.
    proto, sent = _protocol(), []
    c = _consumer(_conn(proto, sent), {'audio': {'present': True}})
    _run(c._subscribe_audio_meters)
    assert (_saln(sent), _sfln(sent)) == ([], [True])


_UNITY = 128 * 65536        # 0 dB on AMLv's scale


def _amlv(master, inputs):
    out = struct.pack('>H2x', len(inputs)) + struct.pack('>8I', *master, 0, 0, 0, 0)
    out += struct.pack(f'>{len(inputs)}H', *(s for s, _ in inputs))
    out += bytes(-len(out) % 4)
    for _, levels in inputs:
        out += struct.pack('>4I', *levels)
    return out


def test_amlv_becomes_the_batch_the_panel_paints():
    proto, sent = _protocol(), []
    c = _consumer(_conn(proto, sent), LEGACY)
    batches = []

    async def send_json(data):
        batches.append(data)
    c.send_json = send_json

    async def run():
        c._subscribe_audio_meters()
        proto.save_field_data(b'AMLv', _amlv(
            (_UNITY, _UNITY // 2, _UNITY, _UNITY // 2),
            [(1, (_UNITY // 2, 0, _UNITY, _UNITY // 2)), (1001, (0, 0, 0, 0))]))
        await asyncio.sleep(c.METER_BATCH_INTERVAL_S * 3)
    asyncio.run(run())

    assert len(batches) == 1
    batch = batches[0]
    assert batch['type'] == 'audio_meter_batch'
    by_id = {s['strip_id']: s for s in batch['strips']}
    assert set(by_id) == {'1.0', '1001.0'}
    half = {'l': -6.02, 'r': -60.0, 'peak_l': 0.0, 'peak_r': -6.02}
    assert by_id['1.0'] == {'strip_id': '1.0', 'input': half, 'level': half}
    silent = {'l': -60.0, 'r': -60.0, 'peak_l': -60.0, 'peak_r': -60.0}
    assert by_id['1001.0']['level'] == silent
    master = {'l': 0.0, 'r': -6.02, 'peak_l': 0.0, 'peak_r': -6.02}
    assert batch['master'] == {'input': master, 'level': master}
