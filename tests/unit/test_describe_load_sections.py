"""Unit tests for
``atem_control.profile.dialog.describe_load_sections``.

These guard against the bug where ``bool(Element)`` returned False
for self-closing XML elements (``<VideoMode videoMode="..."/>`` etc.),
making the load dialog show many sections as "(unavailable)" even
though they were in the XML.

Stage 4C-2: ``describe_load_sections`` takes the connected atem too
(M/E tab availability is topology AND file), per-M/E section ids are
me-namespaced (``me0_program`` …), and ``me_tabs`` is always 4 entries
of ``{index, available, reason}``. These tests run against a 1-M/E
fake topology; the multi-M/E gating cases live in
``test_profile_dialog_me_tabs.py``.
"""
import os
from types import SimpleNamespace

import pytest

from atemwire.profile import Profile
from atem_control.profile.dialog import describe_load_sections


_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TEST_2_XML = os.path.join(
    _REPO_ROOT, 'tools', 'profile_research',
    'Test_2_2026-04-29_11-08-14.xml')
_REFERENCE_XML = os.path.join(
    _REPO_ROOT, 'everything_2026-04-27_18-16-51.xml')


def _fake_atem(me_count=1, keyers=None):
    """Minimal connected-atem stand-in: a ``mixerstate`` shaped the way
    ``pyatem._state._me_count`` / ``_me_keyer_count`` read it."""
    keyers = keyers or {}
    return SimpleNamespace(mixerstate={
        'topology': SimpleNamespace(me_units=me_count),
        'mixer-effect-config': {
            i: SimpleNamespace(keyers=keyers.get(i, 4))
            for i in range(me_count)
        },
    })


_ATEM_1ME = _fake_atem(me_count=1)


def _ids(desc):
    return {s['id'] for s in desc['sections']}


def _supported(desc, sid):
    for s in desc['sections']:
        if s['id'] == sid:
            return s['supported']
    return None


def _by_group(desc, group):
    return [s['id'] for s in desc['sections'] if s['group'] == group]


def _available_tabs(desc):
    return [t['index'] for t in desc['me_tabs'] if t['available']]


# ---------------------------------------------------------------------------
# Reference XML — Test_2 (operator's complete save)
# ---------------------------------------------------------------------------

@pytest.fixture
def test_2_profile():
    if not os.path.exists(_TEST_2_XML):
        pytest.skip("Test_2 reference XML not present")
    with open(_TEST_2_XML, encoding='utf-8') as f:
        return Profile.from_xml(f.read())


def test_test_2_emits_all_present_me_cells(test_2_profile):
    """All 9 M/E cells emit because Test_2 has every M/E sub-element."""
    desc = describe_load_sections(test_2_profile, _ATEM_1ME)
    me_ids = set(_by_group(desc, 'switcher_me'))
    assert me_ids == {
        'me0_program', 'me0_preview', 'me0_next_transition',
        'me0_transition_style', 'me0_fade_to_black',
        'me0_usk_1', 'me0_usk_2', 'me0_usk_3', 'me0_usk_4',
    }


def test_test_2_self_closing_top_level_elements_are_supported(test_2_profile):
    """``<VideoMode/>`` and other self-closing top-level elements
    were the bug — they were reported as ``supported=False``. Now
    they emit with ``supported=True``."""
    desc = describe_load_sections(test_2_profile, _ATEM_1ME)
    for sid in ('video_mode', 'macros', 'auxiliaries', 'inputs',
                'downstream_keys', 'color_generators', 'audio_mixer',
                'media_players', 'media_pool_images', 'settings_flags'):
        assert _supported(desc, sid) is True, \
            f'{sid} should be supported in Test_2'


def test_test_2_omits_camera_hyperdecks_counters(test_2_profile):
    """The Test_2 XML has no <CameraControl>, <HyperDecks>, <Counters>
    so those sections aren't emitted at all (operator can't restore
    what wasn't saved)."""
    ids = _ids(describe_load_sections(test_2_profile, _ATEM_1ME))
    for sid in ('camera_control', 'hyperdecks', 'counters'):
        assert sid not in ids, f'{sid} should be hidden when not in XML'


def test_test_2_program_preview_default_unchecked(test_2_profile):
    """Cuts-to-air gates: even when Program/Preview elements are in
    the XML, the cells default to ``checked: False``."""
    desc = describe_load_sections(test_2_profile, _ATEM_1ME)
    for s in desc['sections']:
        if s['id'] in ('me0_program', 'me0_preview'):
            assert s['checked'] is False
            assert s['supported'] is True


def test_test_2_me_tabs_tab_0_available(test_2_profile):
    """Always 4 tabs; only M/E 1 available on a 1-M/E switcher."""
    desc = describe_load_sections(test_2_profile, _ATEM_1ME)
    assert [t['index'] for t in desc['me_tabs']] == [0, 1, 2, 3]
    assert _available_tabs(desc) == [0]


# ---------------------------------------------------------------------------
# Older reference XML — has CameraControl / HyperDecks / Counters
# ---------------------------------------------------------------------------

@pytest.fixture
def reference_profile():
    if not os.path.exists(_REFERENCE_XML):
        pytest.skip("everything_*.xml not present")
    with open(_REFERENCE_XML, encoding='utf-8') as f:
        return Profile.from_xml(f.read())


def test_reference_xml_emits_all_apply_gap_sections_greyed(reference_profile):
    """Camera Control, HyperDecks, Counter — all in the older
    reference XML — emit but with ``supported=False`` (apply gap)."""
    desc = describe_load_sections(reference_profile, _ATEM_1ME)
    ids = _ids(desc)
    assert 'camera_control' in ids
    assert 'hyperdecks' in ids
    assert 'counters' in ids
    assert _supported(desc, 'camera_control') is False
    assert _supported(desc, 'hyperdecks') is False
    assert _supported(desc, 'counters') is False


def test_reference_xml_emits_supported_sections_supported(reference_profile):
    """Every non-gap section in the reference XML emits as
    ``supported=True``."""
    desc = describe_load_sections(reference_profile, _ATEM_1ME)
    for sid in ('me0_program', 'me0_preview', 'me0_next_transition',
                'me0_transition_style', 'me0_fade_to_black',
                'me0_usk_1', 'me0_usk_2', 'me0_usk_3', 'me0_usk_4',
                'downstream_keys', 'color_generators', 'audio_mixer',
                'auxiliaries', 'inputs',
                'media_pool_images', 'media_players',
                'settings_flags', 'video_mode', 'macros'):
        assert _supported(desc, sid) is True, f'{sid} should be supported'


# ---------------------------------------------------------------------------
# Synthetic partial XMLs
# ---------------------------------------------------------------------------

def test_partial_xml_with_only_audio_and_macros():
    """Sections not in the XML aren't emitted. Only ``audio_mixer``
    and ``macros`` should appear; every M/E tab is unavailable (no
    MixEffectBlock in the file)."""
    xml = '''<Profile majorVersion="2" minorVersion="1">
        <FairlightAudioMixer/>
        <MacroPool>
            <Macro index="0" name="X" description=""/>
        </MacroPool>
    </Profile>'''
    desc = describe_load_sections(Profile.from_xml(xml), _ATEM_1ME)
    assert _ids(desc) == {'audio_mixer', 'macros'}
    assert _available_tabs(desc) == []
    assert desc['me_tabs'][0]['reason'] == 'Not in this profile file.'
    assert desc['me_tabs'][1]['reason'] == 'Not present on this switcher.'


def test_partial_me_xml_no_preview():
    """``<Preview>`` missing from the M/E block → only the cells whose
    XML elements are present emit."""
    xml = '''<Profile majorVersion="2" minorVersion="1">
        <MixEffectBlocks>
            <MixEffectBlock index="0">
                <Program input="1"/>
                <NextTransition selection="Background" nextSelection="Background"/>
                <FadeToBlack rate="25" isFullyBlack="False"/>
            </MixEffectBlock>
        </MixEffectBlocks>
    </Profile>'''
    desc = describe_load_sections(Profile.from_xml(xml), _ATEM_1ME)
    me_ids = set(_by_group(desc, 'switcher_me'))
    assert me_ids == {'me0_program', 'me0_next_transition',
                      'me0_fade_to_black'}
    assert _available_tabs(desc) == [0]


def test_per_usk_presence_independent():
    """Only the Key indices in the XML emit cells; missing indices
    don't appear."""
    xml = '''<Profile majorVersion="2" minorVersion="1">
        <MixEffectBlocks>
            <MixEffectBlock index="0">
                <Keys>
                    <Key index="0" type="Luma" inputCut="1" inputFill="1" onAir="False" masked="False" maskTop="0" maskBottom="0" maskLeft="0" maskRight="0"/>
                    <Key index="2" type="Luma" inputCut="3" inputFill="3" onAir="False" masked="False" maskTop="0" maskBottom="0" maskLeft="0" maskRight="0"/>
                </Keys>
            </MixEffectBlock>
        </MixEffectBlocks>
    </Profile>'''
    desc = describe_load_sections(Profile.from_xml(xml), _ATEM_1ME)
    me_ids = set(_by_group(desc, 'switcher_me'))
    assert me_ids == {'me0_usk_1', 'me0_usk_3'}


def test_empty_profile_emits_no_sections():
    """A profile with nothing in it produces an empty descriptor."""
    desc = describe_load_sections(
        Profile.from_xml('<Profile majorVersion="2" minorVersion="1"/>'),
        _ATEM_1ME)
    assert desc['sections'] == []
    assert _available_tabs(desc) == []
    assert desc['referenced_images'] == []


def test_self_closing_video_mode_is_present():
    """Direct regression: the bug was ``bool(<VideoMode/>)`` → False
    even though the element existed. Verify the descriptor now picks
    up self-closing top-level elements."""
    xml = '<Profile majorVersion="2" minorVersion="1"><VideoMode videoMode="1080p60"/></Profile>'
    desc = describe_load_sections(Profile.from_xml(xml), _ATEM_1ME)
    assert _ids(desc) == {'video_mode'}
    assert _supported(desc, 'video_mode') is True


def test_media_pool_images_requires_path_attr():
    """``<Still>`` without a ``path`` attribute doesn't count — the
    apply path needs the path to upload the file."""
    xml = '''<Profile majorVersion="2" minorVersion="1">
        <MediaPool>
            <Stills>
                <Still index="0" name="empty"/>
            </Stills>
        </MediaPool>
    </Profile>'''
    desc = describe_load_sections(Profile.from_xml(xml), _ATEM_1ME)
    assert 'media_pool_images' not in _ids(desc)


def test_media_pool_images_with_path_present():
    xml = '''<Profile majorVersion="2" minorVersion="1">
        <MediaPool>
            <Stills>
                <Still index="0" name="x" path="ATEM Media Pool/x.png"/>
            </Stills>
        </MediaPool>
    </Profile>'''
    desc = describe_load_sections(Profile.from_xml(xml), _ATEM_1ME)
    assert 'media_pool_images' in _ids(desc)
    assert _supported(desc, 'media_pool_images') is True


def test_referenced_images_populated():
    """``referenced_images`` carries the Still entries with paths,
    one per slot."""
    xml = '''<Profile majorVersion="2" minorVersion="1">
        <MediaPool>
            <Stills>
                <Still index="0" name="a" path="ATEM Media Pool/a.png"/>
                <Still index="3" name="b" path="ATEM Media Pool/b.png"/>
            </Stills>
        </MediaPool>
    </Profile>'''
    desc = describe_load_sections(Profile.from_xml(xml), _ATEM_1ME)
    refs = desc['referenced_images']
    assert {r['slot'] for r in refs} == {0, 3}
    assert {r['filename'] for r in refs} == {'a.png', 'b.png'}
