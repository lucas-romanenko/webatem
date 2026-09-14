"""U10 (F4) + master-rail sweep: every verb the master audio rail emits must
reach a master-bus op (CFMP / CMBP), never a per-strip op (CFSP / CEBP) at the
phantom source 0. Exercises the WS dispatch table (previously zero-coverage).

The JS ``_master`` branches / master-rail template bindings that emit these
verbs are browser code, not reachable from this harness — verified by
inspection (docs: the master-rail audit).
"""
import pytest

from atem_control.control.commands import dispatch


class _FakeConn:
    def __init__(self):
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd)


def test_master_eq_band_emits_cmbp_not_cebp():
    conn = _FakeConn()
    dispatch(conn, 'set_audio_master_eq_band', {'band': 2, 'gain_db': 3.0})
    assert len(conn.sent) == 1
    wire = conn.sent[0].get_command()
    assert b'CMBP' in wire        # master bus
    assert b'CEBP' not in wire    # NOT a per-strip band at source 0


def test_strip_eq_band_still_emits_cebp():
    conn = _FakeConn()
    dispatch(conn, 'set_audio_eq_band',
             {'source': 5, 'channel': -1, 'band': 2, 'gain_db': 3.0})
    assert len(conn.sent) == 1
    assert b'CEBP' in conn.sent[0].get_command()


# Every verb the master rail can emit → the master-bus wire code it must reach.
# (Master dynamics comp/limiter params are deliberately makeup-only in the UI,
# so there is no master comp/limiter verb to pin here.)
_MASTER_RAIL = [
    ('set_audio_master_volume',          {'volume_db': -6.0},        b'CFMP'),
    ('set_audio_master_eq_enable',       {'enabled': True},          b'CFMP'),
    ('set_audio_master_eq_gain',         {'gain_db': 3.0},           b'CFMP'),
    ('set_audio_master_dynamics_makeup', {'gain_db': 3.0},           b'CFMP'),
    ('set_audio_master_afv',             {'afv': True},              b'CFMP'),
    ('set_audio_master_eq_band',         {'band': 2, 'gain_db': 3.0}, b'CMBP'),
]


@pytest.mark.parametrize("verb,data,master_code", _MASTER_RAIL)
def test_master_rail_verb_reaches_master_op(verb, data, master_code):
    conn = _FakeConn()
    dispatch(conn, verb, data)
    assert len(conn.sent) == 1, f"{verb} emitted nothing"
    wire = conn.sent[0].get_command()
    assert master_code in wire                                   # master bus
    assert b'CFSP' not in wire and b'CEBP' not in wire           # never per-strip
