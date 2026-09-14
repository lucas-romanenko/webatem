"""The T-bar drives the switcher (2026-09-11).

The control page streams ``set_transition_position`` while the handle is
dragged; it must reach atemwire's ``CTPs`` with the u16 position in the
switcher's 0..10000 space (clamped — the JS rounds, the wire cannot carry
more), on the right M/E; and the activity log must treat the stream as ONE
slider adjustment (a continuous command), summarised as a percentage.
"""
import struct

from atem_control.control import activity
from atem_control.control.commands import dispatch


class _FakeConn:
    def __init__(self):
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd)


def _wire(position, me=0):
    conn = _FakeConn()
    ok, err = dispatch(conn, 'set_transition_position', {'position': position, 'me': me})
    assert ok, err
    assert len(conn.sent) == 1
    return conn.sent[0].get_command()


def test_position_reaches_ctps_as_u16_on_the_me():
    wire = _wire(5000)
    assert b'CTPs' in wire
    # body: me u8, unknown u8, position u16 BE
    assert wire[-4:] == bytes([0, 0]) + struct.pack('>H', 5000)
    assert _wire(2500, me=1)[-4:] == bytes([1, 0]) + struct.pack('>H', 2500)


def test_position_is_clamped_to_the_wire_range():
    assert _wire(12000)[-2:] == struct.pack('>H', 10000)   # the end = complete
    assert _wire(-5)[-2:] == struct.pack('>H', 0)


def test_the_drag_is_one_activity_row_not_forty_a_second():
    assert activity.is_continuous('set_transition_position')
    assert activity.summarize('set_transition_position', {'position': 5000, 'me': 0}) == 'T-bar → 50%'
    assert activity.summarize('set_transition_position', {'position': 10000, 'me': 1}) == 'T-bar → 100% (ME2)'
