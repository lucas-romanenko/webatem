"""ATEM control command → activity classification + human summary
(2026-07-08).

Pins the discrete/continuous split (so slider drags debounce and discrete
actions log immediately), the debounce key (per-control identity, not
value), and the human summaries.
"""
from atem_control.control import activity as A


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def test_discrete_actions_are_not_continuous():
    for cmd in ['cut', 'auto', 'set_program', 'set_preview', 'toggle_usk',
                'toggle_dsk', 'fade_to_black', 'execute_macro',
                'set_aux_output', 'set_transition_style', 'set_video_mode',
                'set_input_label', 'set_wipe_pattern', 'set_wipe_fill_source',
                'set_stinger_clip', 'set_stinger_source',
                'set_usk_type', 'set_usk_pattern_style',
                'set_transition_rate', 'set_dsk_rate']:
        assert not A.is_continuous(cmd), cmd


def test_slider_commands_are_continuous():
    for cmd in ['set_color_generator_hue', 'set_wipe_position_x',
                'set_wipe_softness', 'set_dve_gain', 'set_dsk_clip',
                'set_usk_luma_gain', 'set_usk_pattern_softness',
                'set_usk_dve_position_x', 'set_usk_dve_rotation',
                'set_usk_dve_border_hue', 'set_usk_chroma_foreground',
                'set_usk_chroma_red', 'set_dsk_mask_top',
                'set_audio_master_volume', 'set_audio_strip_pan',
                'set_audio_eq_band', 'set_audio_compressor',
                'set_stinger_gain', 'set_stinger_pre_roll']:
        assert A.is_continuous(cmd), cmd


def test_debounce_key_ignores_value_but_splits_by_identity():
    # same slider, different values → same key (coalesces)
    k1 = A.debounce_key('set_usk_chroma_hue', {'me': 0, 'keyer': 0, 'hue': 100})
    k2 = A.debounce_key('set_usk_chroma_hue', {'me': 0, 'keyer': 0, 'hue': 142})
    assert k1 == k2
    # different keyer → different key (independent debounce)
    k3 = A.debounce_key('set_usk_chroma_hue', {'me': 0, 'keyer': 1, 'hue': 100})
    assert k1 != k3


# --------------------------------------------------------------------------
# summaries
# --------------------------------------------------------------------------

class _FakeInputProps:
    def __init__(self, name):
        self.name = name.encode()
        self.short_name = name[:4].encode()


def _mx(sources):
    return {'input-properties': {i: _FakeInputProps(n) for i, n in sources.items()}}


def test_cut_and_auto():
    assert A.summarize('cut', {'me': 0}) == 'Cut'
    assert A.summarize('cut', {'me': 1}) == 'Cut (ME2)'
    assert A.summarize('auto', {'me': 0}) == 'Auto transition'


def test_program_resolves_source_name():
    mx = _mx({3010: 'CAM2'})
    assert A.summarize('set_program', {'source': 3010, 'me': 0}, mx) == 'Program → CAM2'
    # unknown source falls back to the id
    assert 'input 99' in A.summarize('set_preview', {'source': 99, 'me': 0}, mx)


def test_toggles_read_the_index():
    assert A.summarize('toggle_usk', {'key_index': 0, 'me': 0}) == 'Toggled USK1 on air'
    assert A.summarize('toggle_dsk', {'dsk': 1}) == 'Toggled DSK2 on air'


def test_macro_and_aux():
    assert A.summarize('execute_macro', {'index': 2}) == 'Ran macro 3'
    mx = _mx({3010: 'MP1'})
    assert A.summarize('set_aux_output', {'aux': 0, 'source': 3010}, mx) == 'Aux 1 → MP1'


def test_settled_slider_generic_summary():
    s = A.summarize('set_usk_pattern_softness', {'me': 0, 'keyer': 0, 'softness': 40})
    assert 'USK1' in s and 'softness' in s.lower() and '40' in s


def test_input_label_summary():
    s = A.summarize('set_input_label', {'source': 5, 'short_name': 'BJ3'})
    assert "input 5" in s and 'BJ3' in s
