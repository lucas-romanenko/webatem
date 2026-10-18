"""The audio verbs on a classic (legacy, pre-Fairlight) audio mixer.

The consumer hands dispatch the state's ``audio.mixer``. On ``'legacy'`` the
fader, pan, Off/On/AFV, master and headphone verbs go to the classic mixer's
CAMI / CAMM / CAMm; every other ``set_audio_*`` verb (EQ, dynamics, input
gain) is refused rather than sent as a Fairlight CFSP / CEBP / CICP that the
switcher has no use for. On any other mixer value, None included, routing is
what it always was.

The bytes are the layouts in bmdwire's ``atemwire/messages/audio_legacy.py``;
no legacy switcher has checked them.
"""
import struct

import pytest

from atem_control.control.commands import dispatch


class _FakeConn:
    def __init__(self):
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd)


def _one(conn):
    assert len(conn.sent) == 1, conn.sent
    wire = conn.sent[0].get_command()
    return wire[4:8], wire[8:]          # opcode, payload


def _cami(payload):
    mask, source, mix, volume, balance = struct.unpack_from('>BxHBxHh', payload)
    return mask, source, mix, volume, balance


STRIP = {'source': 2, 'channel': -1}


@pytest.mark.parametrize('data, mask, volume', [
    ({'volume_db': 0.0}, 0x02, 32768),
    ({'volume_db': -6.0}, 0x02, 16422),
    ({'volume_db': 6.0}, 0x02, 65380),
    ({'volume_db': 10.0}, 0x02, 65381),     # Fairlight's +10 top clamps to +6
    ({'volume_db': None}, 0x02, 0),         # silenced
])
def test_strip_volume_is_cami(data, mask, volume):
    conn = _FakeConn()
    assert dispatch(conn, 'set_audio_strip_volume', {**STRIP, **data}, 'legacy') == (True, None)
    code, payload = _one(conn)
    assert code == b'CAMI'
    m, source, _, v, _ = _cami(payload)
    assert (m, source, v) == (mask, 2, volume)


def test_strip_pan_is_cami_balance():
    conn = _FakeConn()
    assert dispatch(conn, 'set_audio_strip_pan', {**STRIP, 'pan': -50.0}, 'legacy') == (True, None)
    code, payload = _one(conn)
    m, source, _, _, balance = _cami(payload)
    assert (code, m, source, balance) == (b'CAMI', 0x04, 2, -5000)


@pytest.mark.parametrize('mix_type, option', [
    ('Off', 0), ('On', 1), ('AudioFollowVideo', 2),
])
def test_strip_mix_type_is_cami_mix_option(mix_type, option):
    conn = _FakeConn()
    assert dispatch(conn, 'set_audio_strip_mix_type',
                    {**STRIP, 'mix_type': mix_type}, 'legacy') == (True, None)
    code, payload = _one(conn)
    m, source, mix, _, _ = _cami(payload)
    assert (code, m, source, mix) == (b'CAMI', 0x01, 2, option)


def test_unknown_mix_type_is_an_error_and_sends_nothing():
    conn = _FakeConn()
    ok, err = dispatch(conn, 'set_audio_strip_mix_type',
                       {**STRIP, 'mix_type': 'Loud'}, 'legacy')
    assert not ok and 'Loud' in err
    assert conn.sent == []


def test_master_volume_and_afv_are_camm():
    conn = _FakeConn()
    assert dispatch(conn, 'set_audio_master_volume', {'volume_db': -6.0}, 'legacy') == (True, None)
    assert dispatch(conn, 'set_audio_master_afv', {'afv': True}, 'legacy') == (True, None)
    codes = [c.get_command()[4:8] for c in conn.sent]
    assert codes == [b'CAMM', b'CAMM']
    vol, afv = (c.get_command()[8:] for c in conn.sent)
    assert struct.unpack_from('>BxH', vol) == (0x01, 16422)
    assert (afv[0], afv[6]) == (0x04, 1)


def test_headphones_are_the_monitor_bus():
    conn = _FakeConn()
    assert dispatch(conn, 'set_audio_headphones_volume', {'volume_db': 0.0}, 'legacy') == (True, None)
    assert dispatch(conn, 'set_audio_headphones_mute', {'mute': True}, 'legacy') == (True, None)
    assert [c.get_command()[4:8] for c in conn.sent] == [b'CAMm', b'CAMm']
    vol, mute = (c.get_command()[8:] for c in conn.sent)
    assert struct.unpack_from('>BxH', vol) == (0x02, 32768)
    assert (mute[0], mute[4]) == (0x04, 1)


# Every verb the panel can send that the classic mixer has nothing for.
_FAIRLIGHT_ONLY = [
    ('set_audio_strip_input_gain', {**STRIP, 'gain_db': 3.0}),
    ('set_audio_strip_eq_enable', {**STRIP, 'enabled': True}),
    ('set_audio_strip_eq_gain', {**STRIP, 'gain_db': 3.0}),
    ('set_audio_strip_dynamics_makeup', {**STRIP, 'gain_db': 3.0}),
    ('set_audio_eq_band', {**STRIP, 'band': 2, 'gain_db': 3.0}),
    ('set_audio_master_eq_enable', {'enabled': True}),
    ('set_audio_master_eq_gain', {'gain_db': 3.0}),
    ('set_audio_master_dynamics_makeup', {'gain_db': 3.0}),
    ('set_audio_master_eq_band', {'band': 2, 'gain_db': 3.0}),
    ('set_audio_compressor', {**STRIP, 'enabled': True}),
    ('set_audio_limiter', {**STRIP, 'enabled': True}),
    ('set_audio_expander', {**STRIP, 'enabled': True}),
]


@pytest.mark.parametrize('verb, data', _FAIRLIGHT_ONLY, ids=[v for v, _ in _FAIRLIGHT_ONLY])
def test_fairlight_only_verbs_are_refused_on_the_classic_mixer(verb, data):
    conn = _FakeConn()
    ok, err = dispatch(conn, verb, data, 'legacy')
    assert not ok
    assert err.startswith(f'{verb}: ') and 'classic audio mixer' in err
    assert conn.sent == []


@pytest.mark.parametrize('verb, data', _FAIRLIGHT_ONLY, ids=[v for v, _ in _FAIRLIGHT_ONLY])
@pytest.mark.parametrize('mixer', ['fairlight', 'unknown', None])
def test_the_same_verbs_still_reach_fairlight(verb, data, mixer):
    conn = _FakeConn()
    assert dispatch(conn, verb, data, mixer) == (True, None)
    assert len(conn.sent) == 1


_SHARED = [
    ('set_audio_strip_volume', {**STRIP, 'volume_db': -6.0}, b'CFSP'),
    ('set_audio_strip_pan', {**STRIP, 'pan': -50.0}, b'CFSP'),
    ('set_audio_strip_mix_type', {**STRIP, 'mix_type': 'On'}, b'CFSP'),
    ('set_audio_master_volume', {'volume_db': -6.0}, b'CFMP'),
    ('set_audio_master_afv', {'afv': True}, b'CFMP'),
]


@pytest.mark.parametrize('verb, data, code', _SHARED, ids=[v for v, _, _ in _SHARED])
@pytest.mark.parametrize('mixer', ['fairlight', 'unknown', None])
def test_shared_verbs_are_fairlight_unless_the_mixer_is_legacy(verb, data, code, mixer):
    conn = _FakeConn()
    assert dispatch(conn, verb, data, mixer) == (True, None)
    assert _one(conn)[0] == code
    # And without the argument at all: the old three-argument call.
    conn = _FakeConn()
    assert dispatch(conn, verb, data) == (True, None)
    assert _one(conn)[0] == code


@pytest.mark.parametrize('verb, data', [
    ('set_audio_headphones_volume', {'volume_db': 0.0}),
    ('set_audio_headphones_mute', {'mute': True}),
])
@pytest.mark.parametrize('mixer', ['fairlight', 'unknown', None])
def test_headphone_verbs_are_classic_only(verb, data, mixer):
    conn = _FakeConn()
    ok, err = dispatch(conn, verb, data, mixer)
    assert not ok and 'classic audio mixer' in err
    assert conn.sent == []


def test_other_verbs_do_not_care_which_mixer():
    a, b = _FakeConn(), _FakeConn()
    assert dispatch(a, 'cut', {'me': 0}, 'legacy') == (True, None)
    assert dispatch(b, 'cut', {'me': 0}, 'fairlight') == (True, None)
    assert [c.get_command() for c in a.sent] == [c.get_command() for c in b.sent]
    assert dispatch(_FakeConn(), 'no_such_verb', {}, 'legacy') == (False, 'unknown command: no_such_verb')
