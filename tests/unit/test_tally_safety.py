"""Unit tests for the media-pool upload tally guard (``_is_slot_safe``).

The guard refuses to overwrite a slot while a media player showing that slot is
live on PROGRAM output. The source of truth is the ATEM's own program tally
(``tally-source``) — the same on-air state ATEM Software Control shows, already
accounting for the full signal path (M/E chaining, USK/DSK, aux). Only when
tally hasn't arrived yet does it fall back to an ME1 program-path
reconstruction, so it never overwrites blind nor hangs.
"""
import types

from atem_control.uploader import _is_slot_safe

MP0_FILL = 3010   # media player 1 (index 0): fill 3010 / key 3011
MP0_KEY = 3011
MP1_FILL = 3020   # media player 2 (index 1)
SLOT = 5


def _ns(**kw):
    return types.SimpleNamespace(**kw)


class _Proto:
    def __init__(self, mixerstate):
        self.mixerstate = mixerstate


def _mx(*, players=None, tally=None, program=None, usk=None, dsk=None):
    """Build a fake mixerstate.

    players: {mp_idx: (source_type, slot)}    source_type 1 == still
    tally:   {source_id: (program, preview)}  -> mixerstate['tally-source']
    program/usk/dsk: drive the ME1 reconstruction FALLBACK (only consulted
        when ``tally`` is None).
    """
    mx = {}
    if players is not None:
        mx['mediaplayer-selected'] = {
            i: _ns(source_type=st, slot=sl) for i, (st, sl) in players.items()
        }
    if tally is not None:
        mx['tally-source'] = _ns(tally=dict(tally))
    if program is not None:
        mx['program-bus-input'] = {me: _ns(source=s) for me, s in program.items()}
    if usk is not None:
        mx['key-on-air'] = {
            me: {k: _ns(enabled=en) for k, (en, f, ky) in ks.items()}
            for me, ks in usk.items()
        }
        mx['key-properties-base'] = {
            me: {k: _ns(fill_source=f, key_source=ky) for k, (en, f, ky) in ks.items()}
            for me, ks in usk.items()
        }
    if dsk is not None:
        mx['dkey-state'] = {d: _ns(on_air=oa) for d, (oa, f, ky) in dsk.items()}
        mx['dkey-properties-base'] = {
            d: _ns(fill_source=f, key_source=ky) for d, (oa, f, ky) in dsk.items()
        }
    return mx


def _safe(mx, slot=SLOT):
    return _is_slot_safe(_Proto(mx), slot)


# --- authoritative path: the ATEM's tally-source -----------------------------

def test_program_tally_on_slots_player_blocks():
    # MP0 shows SLOT and is program-tallied → live, must block.
    mx = _mx(players={0: (1, SLOT)}, tally={MP0_FILL: (True, False)})
    assert _safe(mx) is False


def test_program_tally_via_key_id_blocks():
    # Self-keyed source: tally reported against the key id (3011).
    mx = _mx(players={0: (1, SLOT)}, tally={MP0_KEY: (True, False)})
    assert _safe(mx) is False


def test_preview_only_is_safe():
    mx = _mx(players={0: (1, SLOT)}, tally={MP0_FILL: (False, True)})
    assert _safe(mx) is True


def test_no_program_tally_is_safe():
    mx = _mx(players={0: (1, SLOT)}, tally={MP0_FILL: (False, False)})
    assert _safe(mx) is True


def test_slots_player_absent_from_tally_is_safe():
    mx = _mx(players={0: (1, SLOT)}, tally={})
    assert _safe(mx) is True


def test_different_player_tallied_does_not_block():
    # MP1 (3020) is program-tallied, but our slot lives in MP0.
    mx = _mx(players={0: (1, SLOT)}, tally={MP1_FILL: (True, False)})
    assert _safe(mx) is True


def test_slot_not_loaded_in_tallied_player_is_safe():
    # MP0 is program-tallied but holds slot 9, not the slot 5 we're uploading.
    mx = _mx(players={0: (1, 9)}, tally={MP0_FILL: (True, False)})
    assert _safe(mx) is True


def test_clip_mode_player_is_safe():
    # source_type 2 (clip) is not a still — overwriting the still slot is fine.
    mx = _mx(players={0: (2, SLOT)}, tally={MP0_FILL: (True, False)})
    assert _safe(mx) is True


def test_no_player_holds_slot_is_safe():
    mx = _mx(players={}, tally={MP0_FILL: (True, False)})
    assert _safe(mx) is True


def test_empty_mixerstate_is_safe():
    assert _safe({}) is True


# --- fallback path: tally-source absent → ME1 program-path reconstruction -----

def test_fallback_me1_program_blocks():
    mx = _mx(players={0: (1, SLOT)}, program={0: MP0_FILL})
    assert _safe(mx) is False


def test_fallback_me1_onair_usk_blocks():
    mx = _mx(players={0: (1, SLOT)}, program={0: 1},
             usk={0: {0: (True, MP0_FILL, 0)}})
    assert _safe(mx) is False


def test_fallback_onair_dsk_blocks():
    mx = _mx(players={0: (1, SLOT)}, program={0: 1},
             dsk={0: (True, MP0_FILL, 0)})
    assert _safe(mx) is False


def test_fallback_idle_me2_is_safe():
    # No tally; reconstruction is ME1-only, so an idle ME2 PGM does not block.
    mx = _mx(players={0: (1, SLOT)}, program={0: 1, 1: MP0_FILL})
    assert _safe(mx) is True


def test_fallback_offair_usk_is_safe():
    mx = _mx(players={0: (1, SLOT)}, program={0: 1},
             usk={0: {0: (False, MP0_FILL, 0)}})
    assert _safe(mx) is True
