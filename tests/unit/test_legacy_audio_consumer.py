"""The consumer end to end on a classic (legacy, pre-Fairlight) audio mixer:
the state it builds names the mixer, and a command goes to the mixer the
last pushed state named.

Packets go through bmdwire's real ``AtemProtocol.save_field_data`` in the
layouts its parsers read; the connection is a stand-in whose ``send``
records commands. No legacy switcher has checked those layouts.
"""
import asyncio
import json
import struct
import types

import pytest

from atem_control.control.consumer import ATEMConsumer
from atemwire.protocol import AtemProtocol


def _amip(source, *, type=0, plug=1, state=1, volume=32768, balance=0):
    return struct.pack('>HB 2x B x BB x Hh 2x', source, type, 0, plug, state,
                       volume, balance)


@pytest.fixture
def proto():
    p = AtemProtocol('127.0.0.1')
    p.send_commands = lambda commands: None
    try:
        yield p
    finally:
        p.transport.sock.close()
        p.transport.thread_queue.close()


def _consumer(proto, sent):
    c = ATEMConsumer.__new__(ATEMConsumer)       # skip __init__ / channel scope
    c.connection = types.SimpleNamespace(
        protocol=proto, mixerstate=proto.mixerstate, is_connected=True,
        send=sent.append)
    c.last_known_state = {}
    c.replies = []

    async def send_json(data):
        c.replies.append(data)
    c.send_json = send_json
    c._record_atem_command = lambda command, data: None   # no activity log
    return c


def _legacy_switcher(proto):
    proto.save_field_data(b'VidM', bytes([10, 0, 0, 0]))
    proto.save_field_data(b'AMMO', struct.pack('>H2x?3x', 32768, False))
    proto.save_field_data(b'AMIP', _amip(1))
    proto.save_field_data(b'AMIP', _amip(1001, type=2, plug=32))


def test_the_pushed_state_names_the_classic_mixer_and_routes_by_it(proto):
    sent = []
    c = _consumer(proto, sent)
    _legacy_switcher(proto)
    state = c.build_full_state(c.connection)
    json.dumps(state)                            # what the socket carries
    assert (state['audio']['mixer'], state['audio']['present']) == ('legacy', True)
    assert [s['strip_id'] for s in state['audio']['strips']] == ['1.0', '1001.0']

    # Before the first push nothing says which mixer: Fairlight, as always.
    asyncio.run(c.execute_command('set_audio_strip_volume',
                                  {'source': 1, 'channel': -1, 'volume_db': 0.0}))
    assert [cmd.get_command()[4:8] for cmd in sent] == [b'CFSP']

    c.last_known_state = state.copy()            # what the poll loop keeps
    sent.clear()
    asyncio.run(c.execute_command('set_audio_strip_volume',
                                  {'source': 1, 'channel': -1, 'volume_db': 0.0}))
    asyncio.run(c.execute_command('set_audio_master_afv', {'afv': True}))
    assert [cmd.get_command()[4:8] for cmd in sent] == [b'CAMI', b'CAMM']
    assert c.replies == []


def test_a_refused_verb_answers_the_page_and_sends_nothing(proto):
    sent = []
    c = _consumer(proto, sent)
    _legacy_switcher(proto)
    c.last_known_state = c.build_full_state(c.connection).copy()
    asyncio.run(c.execute_command('set_audio_strip_eq_enable',
                                  {'source': 1, 'channel': -1, 'enabled': True}))
    assert sent == []
    assert len(c.replies) == 1 and c.replies[0]['type'] == 'error'
    assert 'classic audio mixer' in c.replies[0]['message']
