"""_has_state_changed pushes on ANY top-level change, not a curated whitelist.

The control page is a full realtime ATEM surface: every state change must reach
the browser. The old ``important_keys`` whitelist privileged some keys and
silently dropped changes to any it forgot — ``macros`` / ``lastRunMacro`` /
``videoMode`` / ``videoModes`` / the switcher ``atem_name`` (and historically
``hyperdecks``). So a macro fired from ASC or the overlay page, or a switcher
rename, didn't reach the control page live until an unrelated change or a page
refresh. A full value-compare fixes it; the per-tick state is idle-stable (live
meters ride a separate subscription), so it doesn't spam.
"""
import copy

import pytest

from atem_control.control.consumer import ATEMConsumer


def _consumer(last=None):
    c = ATEMConsumer.__new__(ATEMConsumer)  # skip __init__ / channel scope
    c.last_known_state = last if last is not None else {}
    return c


BASE = {
    'is_connected': True,
    'mes': [{'program': 1, 'preview': 2, 'ftb': {'active': False}}],
    'dsks': [], 'colorGenerators': {}, 'auxOutputs': {}, 'audio': {'present': False},
    'inputLabels': [], 'sources': [], 'topology': {},
    'macros': [{'index': 0, 'name': 'A', 'is_used': True}],
    'lastRunMacro': {'index': -1, 'name': None},
    'videoMode': {'format': '1080p50', 'id': 1, 'fps': 25},
    'videoModes': ['1080p50'],
    'atem_name': 'Studio',
    'hyperdecks': [],
}


def test_first_state_always_pushes():
    assert _consumer()._has_state_changed(BASE) is True


def test_identical_state_does_not_push():
    # A fresh build with the same VALUES (new nested objects) must NOT push —
    # this is the idle case, and is what keeps the surface from spamming.
    c = _consumer(copy.deepcopy(BASE))
    assert c._has_state_changed(copy.deepcopy(BASE)) is False


@pytest.mark.parametrize("key,newval", [
    ('macros', [{'index': 0, 'name': 'RENAMED', 'is_used': True}]),
    ('lastRunMacro', {'index': 3, 'name': 'Wipe In'}),   # a macro fired elsewhere
    ('videoMode', {'format': '2160p50', 'id': 2, 'fps': 25}),
    ('videoModes', ['1080p50', '2160p50']),
    ('atem_name', 'Renamed Studio'),
    ('hyperdecks', [{'slot': 0, 'ip': '10.0.0.9'}]),
    # keys the old whitelist DID cover — must still push:
    ('mes', [{'program': 5, 'preview': 2, 'ftb': {'active': False}}]),
    ('audio', {'present': True}),
])
def test_change_to_any_top_level_key_pushes(key, newval):
    c = _consumer(copy.deepcopy(BASE))
    changed = copy.deepcopy(BASE)
    changed[key] = newval
    assert c._has_state_changed(changed) is True, \
        f"a change to {key!r} alone must push (the old whitelist dropped it)"


def test_nested_ftb_change_pushes_via_mes():
    # FTB lives under mes[me]['ftb'] (never a top-level key), so it rides 'mes' —
    # the old whitelist's phantom top-level 'ftb' entry was a dead no-op.
    c = _consumer(copy.deepcopy(BASE))
    changed = copy.deepcopy(BASE)
    changed['mes'][0]['ftb']['active'] = True
    assert c._has_state_changed(changed) is True
