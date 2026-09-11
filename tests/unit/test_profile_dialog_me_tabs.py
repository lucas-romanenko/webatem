"""Stage 4C-2 — per-M/E save/load dialog descriptors + submission mapping.

Pure-function tests (no Django harness):

  * ``describe_save_sections`` — ``me_tabs`` availability and per-M/E
    USK-cell sizing from a fake connected topology (the same
    mixerstate shape ``pyatem._state._me_count`` / ``_me_keyer_count``
    read).
  * ``describe_load_sections`` — availability is topology AND file
    (a 1-M/E file on a 4-M/E topology greys M/E 2-4; a block beyond
    the connected me_count is greyed even though it's in the file;
    sections missing from a present block are omitted).
  * ``me_options_from_sections`` — the single parser of the
    ``me<N>_`` transport ids used by both submission-mapping
    functions: selections for me 0..3 land in ``options.mes[0..3]``,
    and the list is sized to the submitted tab count (= the real
    me_count on save), not the PLATFORM_MAX_MES default.
"""
from types import SimpleNamespace

from atemwire.profile import Profile
from atem_control.profile.dialog import (
    describe_load_sections,
    describe_save_sections,
    me_options_from_sections,
)


def _fake_atem(me_count=1, keyers=None):
    keyers = keyers or {}
    return SimpleNamespace(mixerstate={
        'topology': SimpleNamespace(me_units=me_count),
        'mixer-effect-config': {
            i: SimpleNamespace(keyers=keyers.get(i, 4))
            for i in range(me_count)
        },
    })


def _ids(desc):
    return {s['id'] for s in desc['sections']}


def _tabs(desc):
    return {t['index']: t for t in desc['me_tabs']}


# ---------------------------------------------------------------------------
# describe_save_sections — topology gating
# ---------------------------------------------------------------------------

def test_save_one_me_only_tab_0_available():
    desc = describe_save_sections(_fake_atem(me_count=1))
    tabs = _tabs(desc)
    assert sorted(tabs) == [0, 1, 2, 3]
    assert tabs[0]['available'] is True
    for i in (1, 2, 3):
        assert tabs[i]['available'] is False
        assert tabs[i]['reason'] == 'Not present on this switcher.'
    ids = _ids(desc)
    # Full 9-cell grid for M/E 1, nothing for M/E 2-4.
    for name in ('program', 'preview', 'next_transition',
                 'transition_style', 'fade_to_black',
                 'usk_1', 'usk_2', 'usk_3', 'usk_4'):
        assert f'me0_{name}' in ids
    assert not any(i.startswith(('me1_', 'me2_', 'me3_')) for i in ids)


def test_save_four_me_all_tabs_available():
    desc = describe_save_sections(_fake_atem(me_count=4))
    tabs = _tabs(desc)
    assert all(tabs[i]['available'] for i in range(4))
    ids = _ids(desc)
    for me in range(4):
        assert f'me{me}_program' in ids
        assert f'me{me}_usk_4' in ids


def test_save_two_usk_me_omits_usk_3_4():
    """A 2-keyer M/E gets exactly usk_1/usk_2 cells; usk_3/usk_4 are
    absent (not greyed). M/E 1 keeps its full 4."""
    desc = describe_save_sections(
        _fake_atem(me_count=2, keyers={0: 4, 1: 2}))
    ids = _ids(desc)
    assert 'me0_usk_4' in ids
    assert 'me1_usk_1' in ids
    assert 'me1_usk_2' in ids
    assert 'me1_usk_3' not in ids
    assert 'me1_usk_4' not in ids


def test_save_me_tab_field_matches_index():
    desc = describe_save_sections(_fake_atem(me_count=2))
    for s in desc['sections']:
        if s['group'] == 'switcher_me':
            assert s['id'].startswith(f"me{s['me_tab']}_")


def test_save_globals_unchanged_and_flat():
    """Globals stay outside the tabs with flat (un-namespaced) ids;
    the apply-gap greying (camera_control / counters) is intact."""
    desc = describe_save_sections(_fake_atem(me_count=4))
    by_id = {s['id']: s for s in desc['sections']}
    for sid in ('downstream_keys', 'color_generators', 'fairlight',
                'media_pool', 'media_players', 'auxiliaries', 'settings',
                'video_mode', 'hyperdecks', 'macros'):
        assert by_id[sid]['supported'] is True
        assert 'me_tab' not in by_id[sid]
    assert by_id['camera_control']['supported'] is False
    assert by_id['counters']['supported'] is False
    # Media Pool is ONE cell (ASC-style) — the two SaveOptions flags
    # are fanned out from it in build_save_options.
    assert 'media_pool_metadata' not in by_id
    assert 'media_pool_images' not in by_id


# ---------------------------------------------------------------------------
# describe_load_sections — topology AND file gating
# ---------------------------------------------------------------------------

_ONE_BLOCK_XML = '''<Profile majorVersion="2" minorVersion="1">
    <MixEffectBlocks>
        <MixEffectBlock index="0">
            <Program input="1"/>
            <Preview input="2"/>
            <FadeToBlack rate="25" isFullyBlack="False"/>
        </MixEffectBlock>
    </MixEffectBlocks>
</Profile>'''


def _four_block_xml():
    blocks = ''.join(
        f'<MixEffectBlock index="{i}"><Program input="{i + 1}"/>'
        f'<FadeToBlack rate="25" isFullyBlack="False"/></MixEffectBlock>'
        for i in range(4))
    return (f'<Profile majorVersion="2" minorVersion="1">'
            f'<MixEffectBlocks>{blocks}</MixEffectBlocks></Profile>')


def test_load_one_me_file_on_four_me_switcher_greys_tabs_1_to_3():
    desc = describe_load_sections(
        Profile.from_xml(_ONE_BLOCK_XML), _fake_atem(me_count=4))
    tabs = _tabs(desc)
    assert tabs[0]['available'] is True
    for i in (1, 2, 3):
        assert tabs[i]['available'] is False
        assert tabs[i]['reason'] == 'Not in this profile file.'
    assert not any(i.startswith(('me1_', 'me2_', 'me3_'))
                   for i in _ids(desc))


def test_load_four_me_file_on_one_me_switcher_greys_tabs_1_to_3():
    """Blocks beyond the connected me_count are greyed even though
    they're in the file — and contribute no sections (apply would
    skip them; cross-model load)."""
    desc = describe_load_sections(
        Profile.from_xml(_four_block_xml()), _fake_atem(me_count=1))
    tabs = _tabs(desc)
    assert tabs[0]['available'] is True
    for i in (1, 2, 3):
        assert tabs[i]['available'] is False
        assert tabs[i]['reason'] == 'Not present on this switcher.'
    assert not any(i.startswith(('me1_', 'me2_', 'me3_'))
                   for i in _ids(desc))


def test_load_four_me_file_on_four_me_switcher_all_available():
    desc = describe_load_sections(
        Profile.from_xml(_four_block_xml()), _fake_atem(me_count=4))
    assert all(t['available'] for t in desc['me_tabs'])
    ids = _ids(desc)
    for me in range(4):
        assert f'me{me}_program' in ids
        # The synthetic blocks carry no <Preview> → cell omitted.
        assert f'me{me}_preview' not in ids


def test_load_section_missing_from_present_block_is_omitted():
    """Within an available tab, only cells whose XML element exists in
    THAT block emit — the existing per-section presence rule, per-M/E."""
    xml = '''<Profile majorVersion="2" minorVersion="1">
        <MixEffectBlocks>
            <MixEffectBlock index="0">
                <Program input="1"/>
                <TransitionStyle style="Mix" nextStyle="Mix"/>
            </MixEffectBlock>
            <MixEffectBlock index="1">
                <Preview input="2"/>
                <Keys>
                    <Key index="0" type="Luma" inputCut="1" inputFill="1"/>
                    <Key index="2" type="Luma" inputCut="3" inputFill="3"/>
                </Keys>
            </MixEffectBlock>
        </MixEffectBlocks>
    </Profile>'''
    desc = describe_load_sections(
        Profile.from_xml(xml), _fake_atem(me_count=2))
    ids = {s['id'] for s in desc['sections']
           if s['group'] == 'switcher_me'}
    assert ids == {'me0_program', 'me0_transition_style',
                   'me1_preview', 'me1_usk_1', 'me1_usk_3'}


def test_load_block_without_index_attr_counts_as_block_0():
    """Mirror of apply's ``_iter_me_blocks``: a legacy block with no
    ``index`` attribute is M/E 1."""
    xml = '''<Profile majorVersion="2" minorVersion="1">
        <MixEffectBlocks>
            <MixEffectBlock>
                <Program input="1"/>
            </MixEffectBlock>
        </MixEffectBlocks>
    </Profile>'''
    desc = describe_load_sections(
        Profile.from_xml(xml), _fake_atem(me_count=1))
    assert _tabs(desc)[0]['available'] is True
    assert 'me0_program' in _ids(desc)


# ---------------------------------------------------------------------------
# Submission mapping — me<N>_ ids → options.mes
# ---------------------------------------------------------------------------

def _full_tab(me, **overrides):
    cells = {
        f'me{me}_program': True,
        f'me{me}_preview': True,
        f'me{me}_next_transition': True,
        f'me{me}_transition_style': True,
        f'me{me}_fade_to_black': True,
        f'me{me}_usk_1': True,
        f'me{me}_usk_2': True,
        f'me{me}_usk_3': True,
        f'me{me}_usk_4': True,
    }
    cells.update({f'me{me}_{k}': v for k, v in overrides.items()})
    return cells


def test_mapping_four_tabs_sized_to_me_count():
    """Selections for me 0..3 land in mes[0..3]; the list is sized to
    the submitted tab count (= real me_count), not PLATFORM_MAX_MES."""
    sections = {}
    for me in range(4):
        sections.update(_full_tab(me))
    sections['me2_program'] = False
    sections['me3_usk_2'] = False

    mes = me_options_from_sections(
        sections, program_default=True, preview_default=True)
    assert len(mes) == 4
    assert mes[0].program is True
    assert mes[2].program is False
    assert mes[2].preview is True
    assert mes[3].usk == [True, False, True, True]


def test_mapping_single_tab_gives_one_element_list():
    mes = me_options_from_sections(
        _full_tab(0), program_default=True, preview_default=True)
    assert len(mes) == 1
    assert mes[0].any_selected()


def test_mapping_gap_me_is_fully_deselected():
    """A submitted me2 with no me1 cells (load: block 1 not in file)
    pads index 1 with a fully-deselected entry."""
    sections = {}
    sections.update(_full_tab(0))
    sections.update(_full_tab(2))
    mes = me_options_from_sections(
        sections, program_default=False, preview_default=False)
    assert len(mes) == 3
    assert not mes[1].any_selected()
    assert mes[2].any_selected()


def test_mapping_usk_sized_to_submitted_cells():
    """A 2-USK tab submits usk_1/usk_2 only → 2-element usk list."""
    sections = {
        'me0_program': True,
        'me0_usk_1': True,
        'me0_usk_2': False,
    }
    mes = me_options_from_sections(
        sections, program_default=True, preview_default=True)
    assert mes[0].usk == [True, False]


def test_mapping_apply_defaults_gate_program_preview():
    """Cells missing from a submitted tab fall back to the per-context
    defaults — False for program/preview on apply (cuts-to-air gate),
    True for the rest."""
    sections = {'me0_fade_to_black': True}
    mes = me_options_from_sections(
        sections, program_default=False, preview_default=False)
    assert mes[0].program is False
    assert mes[0].preview is False
    assert mes[0].next_transition is True
    assert mes[0].fade_to_black is True


def test_mapping_string_values_coerce():
    """The transport may carry 'true'/'false' strings — same coercion
    as the flat globals."""
    sections = {'me0_program': 'true', 'me0_preview': 'false'}
    mes = me_options_from_sections(
        sections, program_default=False, preview_default=True)
    assert mes[0].program is True
    assert mes[0].preview is False


def test_mapping_no_me_keys_returns_empty_list():
    """Flat/global-only submissions produce an empty mes — every M/E
    fully deselected via ``me_options``'s out-of-range rule."""
    assert me_options_from_sections(
        {'macros': True, 'video_mode': False},
        program_default=True, preview_default=True) == []
