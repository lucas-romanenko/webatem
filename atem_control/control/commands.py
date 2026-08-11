"""
ATEM Command Dispatch — WebSocket verb → pyatem operation.

Each frontend command string maps to a lambda that extracts arguments from
the JSON message body and calls the corresponding function on the ``ops``
namespace. This file contains zero protocol knowledge: no mixerstate reads,
no wire-unit math, no pyatem Command construction. The library vocabulary
lives entirely in the ``pyatem.messages.<feature>`` modules.

``ops`` is built at module load by walking each ``pyatem.messages.<feature>``
module and collecting its public callables. The feature module list is the
explicit guard against silent miss-imports — a new feature module must be
added there to surface its ops through the dispatch table.

Dispatch contract preserved for the consumer:
    dispatch(connection, command: str, data: dict) -> (ok: bool, error: Optional[str])

On ok=False the consumer forwards ``error`` as the frontend message text.
Operations that raise on invalid args are caught here and mapped to the
tuple shape.
"""

import logging
from types import SimpleNamespace
from typing import Tuple, Optional

from pyatem.messages import (
    color_generator, downstream_keyer, fade_to_black, fairlight, hyperdeck,
    input_video, macros, media, switching, transition, upstream_keyer,
)


# Build the ops namespace at module load. Each feature module exports
# its operation wrappers as top-level functions; we walk the list of
# feature modules and copy each public callable onto ``ops``. Same
# pattern as ``pyatem.atem._OPERATIONS``.
_OPS_MODULES = (
    switching, color_generator, fade_to_black, media, input_video,
    upstream_keyer, macros, downstream_keyer, transition, fairlight, hyperdeck,
)

ops = SimpleNamespace()
for _mod in _OPS_MODULES:
    for _name in dir(_mod):
        if _name.startswith('_'):
            continue
        _obj = getattr(_mod, _name)
        if callable(_obj) and getattr(_obj, '__module__', '') == _mod.__name__:
            setattr(ops, _name, _obj)
del _mod, _name, _obj

logger = logging.getLogger(__name__)


HandlerResult = Tuple[bool, Optional[str]]


# ---------------------------------------------------------------------------
# Command map — frontend verb → lambda that unpacks data into an ops call
#
# Per-M/E verbs (program/preview/cut/auto, transition, USK, FtB) thread
# ``me=d.get('me', 0)`` — the frontend stamps every payload with the active
# M/E index. Absent means M/E 1, so older payloads without the stamp
# behave unchanged. Global verbs (DSK, aux, color gen, media, macros, fairlight,
# hyperdeck, video mode, input labels) ignore the stamp.
# ---------------------------------------------------------------------------

ATEM_COMMAND_MAP = {
    # Basic switching
    'set_program': lambda c, d: ops.set_program(c, source=d['source'], me=d.get('me', 0)),
    'set_preview': lambda c, d: ops.set_preview(c, source=d['source'], me=d.get('me', 0)),
    'cut': lambda c, d: ops.cut(c, me=d.get('me', 0)),
    'auto': lambda c, d: ops.auto(c, me=d.get('me', 0)),

    # Transition control
    'set_transition_style': lambda c, d: ops.set_transition_style(c, style=d['style'], me=d.get('me', 0)),
    'set_transition_rate': lambda c, d: ops.set_transition_rate(c, rate_str=d['rate'], me=d.get('me', 0)),
    'set_mix_rate': lambda c, d: ops.set_mix_rate(c, rate_str=d['rate'], me=d.get('me', 0)),
    'set_dip_rate': lambda c, d: ops.set_dip_rate(c, rate_str=d['rate'], me=d.get('me', 0)),
    'set_wipe_rate': lambda c, d: ops.set_wipe_rate(c, rate_str=d['rate'], me=d.get('me', 0)),
    'set_dve_rate': lambda c, d: ops.set_dve_rate(c, rate_str=d['rate'], me=d.get('me', 0)),

    # USK/DSK quick-toggle
    'toggle_usk': lambda c, d: ops.toggle_usk(c, keyer=d['key_index'], me=d.get('me', 0)),
    'toggle_dsk': lambda c, d: ops.toggle_dsk(c, dsk_idx=d.get('dsk', 0)),
    'dsk_auto': lambda c, d: ops.dsk_auto(c, dsk_idx=d.get('dsk', 0)),
    'set_dsk_rate': lambda c, d: ops.set_dsk_rate(c, rate_str=d['rate'], dsk_idx=d.get('dsk', 0)),

    # Transition selection
    'toggle_transition_key': lambda c, d: ops.toggle_transition_key(c, key=d['key_index'], me=d.get('me', 0)),
    'toggle_transition_background': lambda c, d: ops.toggle_transition_background(c, me=d.get('me', 0)),

    # Fade to Black
    'fade_to_black': lambda c, d: ops.fade_to_black(c, me=d.get('me', 0)),
    'set_ftb_rate': lambda c, d: ops.set_ftb_rate(c, rate_str=d['rate'], me=d.get('me', 0)),
    'set_ftb_disabled': lambda c, d: ops.set_ftb_disabled(c, disabled=d.get('disabled'), me=d.get('me', 0)),

    # Color generators
    'set_color_generator_hue': lambda c, d: ops.set_color_generator_hue(
        c, generator=d['generator'], hue=d['hue']),
    'set_color_generator_saturation': lambda c, d: ops.set_color_generator_saturation(
        c, generator=d['generator'], saturation=d['saturation']),
    'set_color_generator_luma': lambda c, d: ops.set_color_generator_luma(
        c, generator=d['generator'], luma=d['luma']),

    # Wipe transition settings
    'set_wipe_pattern': lambda c, d: ops.set_wipe_pattern(c, pattern=d['pattern'], me=d.get('me', 0)),
    'set_wipe_fill_source': lambda c, d: ops.set_wipe_fill_source(c, source=d['fillSource'], me=d.get('me', 0)),
    'set_wipe_flip_flop': lambda c, d: ops.set_wipe_flip_flop(c, flip_flop=d['flipFlop'], me=d.get('me', 0)),
    'set_wipe_position_x': lambda c, d: ops.set_wipe_position_x(c, position_x=d['positionX'], me=d.get('me', 0)),
    'set_wipe_position_y': lambda c, d: ops.set_wipe_position_y(c, position_y=d['positionY'], me=d.get('me', 0)),
    'set_wipe_reverse': lambda c, d: ops.set_wipe_reverse(c, reverse=d['reverse'], me=d.get('me', 0)),
    'set_wipe_softness': lambda c, d: ops.set_wipe_softness(c, softness=d['softness'], me=d.get('me', 0)),
    'set_wipe_symmetry': lambda c, d: ops.set_wipe_symmetry(c, symmetry=d['symmetry'], me=d.get('me', 0)),
    'set_wipe_width': lambda c, d: ops.set_wipe_width(c, width=d['width'], me=d.get('me', 0)),

    # Dip transition settings
    'set_dip_source': lambda c, d: ops.set_dip_source(c, source=d['input_source'], me=d.get('me', 0)),

    # Stinger transition settings
    'set_stinger_source': lambda c, d: ops.set_stinger_source(c, source=d['source'], me=d.get('me', 0)),
    'set_stinger_clip_duration': lambda c, d: ops.set_stinger_clip_duration(
        c, clip_duration=d['clip_duration'], me=d.get('me', 0)),
    'set_stinger_trigger_point': lambda c, d: ops.set_stinger_trigger_point(
        c, trigger_point=d['trigger_point'], me=d.get('me', 0)),
    'set_stinger_mix_rate': lambda c, d: ops.set_stinger_mix_rate(c, rate_str=d['mix_rate'], me=d.get('me', 0)),
    'set_stinger_pre_roll': lambda c, d: ops.set_stinger_pre_roll(c, pre_roll=d['pre_roll'], me=d.get('me', 0)),
    'set_stinger_clip': lambda c, d: ops.set_stinger_clip(c, clip=d['clip'], me=d.get('me', 0)),
    'set_stinger_gain': lambda c, d: ops.set_stinger_gain(c, gain=d['gain'], me=d.get('me', 0)),
    'set_stinger_pre_multiplied': lambda c, d: ops.set_stinger_pre_multiplied(
        c, pre_multiplied=d['pre_multiplied'], me=d.get('me', 0)),
    'set_stinger_invert_key': lambda c, d: ops.set_stinger_invert_key(
        c, invert_key=d['invert_key'], me=d.get('me', 0)),

    # DVE transition settings
    'set_dve_fill_source': lambda c, d: ops.set_dve_fill_source(c, source=d['fillSource'], me=d.get('me', 0)),
    'set_dve_key_source': lambda c, d: ops.set_dve_key_source(c, source=d['keySource'], me=d.get('me', 0)),
    'set_dve_enable_key': lambda c, d: ops.set_dve_enable_key(c, enable_key=d['enableKey'], me=d.get('me', 0)),
    'set_dve_clip': lambda c, d: ops.set_dve_clip(c, clip=d['clip'], me=d.get('me', 0)),
    'set_dve_gain': lambda c, d: ops.set_dve_gain(c, gain=d['gain'], me=d.get('me', 0)),
    'set_dve_pre_multiplied': lambda c, d: ops.set_dve_pre_multiplied(
        c, pre_multiplied=d['preMultiplied'], me=d.get('me', 0)),
    'set_dve_invert_key': lambda c, d: ops.set_dve_invert_key(c, invert_key=d['invertKey'], me=d.get('me', 0)),
    'set_dve_style': lambda c, d: ops.set_dve_style(c, style=d['style'], me=d.get('me', 0)),
    'set_dve_reverse': lambda c, d: ops.set_dve_reverse(c, reverse=d['reverse'], me=d.get('me', 0)),
    'set_dve_flip_flop': lambda c, d: ops.set_dve_flip_flop(c, flip_flop=d['flipFlop'], me=d.get('me', 0)),

    # Aux outputs
    'set_aux_output': lambda c, d: ops.set_aux_output(
        c, aux=d['aux_channel'], source=d['input_source']),

    # HyperDeck binding (deck IP + switcher input per slot). Either field may
    # be omitted — only what's present is written (partial CXMS mask).
    'set_hyperdeck': lambda c, d: ops.set_hyperdeck_settings(
        c, slot=d['slot'], network_address=d.get('network_address'),
        switcher_input=d.get('switcher_input')),

    # Video mode (settings menu — destructive, drops outputs ~3 s on resync)
    'set_video_mode': lambda c, d: ops.set_video_mode(c, mode=int(d['mode'])),

    # Input labels — rename cameras (long ≤ 20 chars, short ≤ 4 chars).
    # Either or both may be supplied; missing → unchanged.
    'set_input_label': lambda c, d: ops.set_input_label(
        c, source=int(d['source']),
        long_name=d.get('long_name'),
        short_name=d.get('short_name')),

    # Fairlight dynamics — per-strip compressor / limiter / expander
    # parameters. Each takes any subset of params; only those provided
    # end up in the wire mask. ``channel`` defaults to -1 (stereo).
    # Verb prefix matches the rest of the audio dispatch entries
    # (set_audio_*); operations under the hood are set_fairlight_*.
    'set_audio_compressor': lambda c, d: ops.set_fairlight_compressor(
        c, source=int(d['source']),
        channel=int(d.get('channel', -1)),
        enabled=d.get('enabled'),
        threshold_db=d.get('threshold_db'),
        ratio=d.get('ratio'),
        attack_ms=d.get('attack_ms'),
        hold_ms=d.get('hold_ms'),
        release_ms=d.get('release_ms')),
    'set_audio_limiter': lambda c, d: ops.set_fairlight_limiter(
        c, source=int(d['source']),
        channel=int(d.get('channel', -1)),
        enabled=d.get('enabled'),
        threshold_db=d.get('threshold_db'),
        attack_ms=d.get('attack_ms'),
        hold_ms=d.get('hold_ms'),
        release_ms=d.get('release_ms')),
    'set_audio_expander': lambda c, d: ops.set_fairlight_expander(
        c, source=int(d['source']),
        channel=int(d.get('channel', -1)),
        enabled=d.get('enabled'),
        mode=d.get('mode'),
        threshold_db=d.get('threshold_db'),
        range_db=d.get('range_db'),
        ratio=d.get('ratio'),
        attack_ms=d.get('attack_ms'),
        hold_ms=d.get('hold_ms'),
        release_ms=d.get('release_ms')),

    # Media players — point at a media-pool still slot (both 0-indexed)
    'set_media_player_still': lambda c, d: ops.set_media_player_still(
        c, player=d['player'], slot=d['slot']),

    # Macros
    'execute_macro': lambda c, d: ops.execute_macro(c, macro_number=d['macro_number']),

    # USK type & sources
    'set_usk_type': lambda c, d: ops.set_usk_type(
        c, keyer=d['key_index'], key_type=d['key_type'], me=d.get('me', 0)),
    'set_usk_fill_source': lambda c, d: ops.set_usk_fill_source(
        c, keyer=d['key_index'], source=d['source'], me=d.get('me', 0)),
    'set_usk_key_source': lambda c, d: ops.set_usk_key_source(
        c, keyer=d['key_index'], source=d['source'], me=d.get('me', 0)),

    # USK Luma
    'set_usk_luma_clip': lambda c, d: ops.set_usk_luma_clip(
        c, keyer=d['key_index'], clip=d['clip'], me=d.get('me', 0)),
    'set_usk_luma_gain': lambda c, d: ops.set_usk_luma_gain(
        c, keyer=d['key_index'], gain=d['gain'], me=d.get('me', 0)),
    'set_usk_luma_invert': lambda c, d: ops.set_usk_luma_invert(
        c, keyer=d['key_index'], invert=d['invert'], me=d.get('me', 0)),
    'set_usk_luma_pre_multiplied': lambda c, d: ops.set_usk_luma_pre_multiplied(
        c, keyer=d['key_index'], pre_multiplied=d['pre_multiplied'], me=d.get('me', 0)),

    # USK Pattern
    'set_usk_pattern_style': lambda c, d: ops.set_usk_pattern_style(
        c, keyer=d['key_index'], pattern=d['pattern'], me=d.get('me', 0)),
    'set_usk_pattern_size': lambda c, d: ops.set_usk_pattern_size(
        c, keyer=d['key_index'], size=d['size'], me=d.get('me', 0)),
    'set_usk_pattern_symmetry': lambda c, d: ops.set_usk_pattern_symmetry(
        c, keyer=d['key_index'], symmetry=d['symmetry'], me=d.get('me', 0)),
    'set_usk_pattern_softness': lambda c, d: ops.set_usk_pattern_softness(
        c, keyer=d['key_index'], softness=d['softness'], me=d.get('me', 0)),
    'set_usk_pattern_invert': lambda c, d: ops.set_usk_pattern_invert(
        c, keyer=d['key_index'], invert=d['invert'], me=d.get('me', 0)),
    'set_usk_pattern_position_x': lambda c, d: ops.set_usk_pattern_position_x(
        c, keyer=d['key_index'], position_x=d['positionX'], me=d.get('me', 0)),
    'set_usk_pattern_position_y': lambda c, d: ops.set_usk_pattern_position_y(
        c, keyer=d['key_index'], position_y=d['positionY'], me=d.get('me', 0)),

    # USK Mask
    'set_usk_mask_enabled': lambda c, d: ops.set_usk_mask_enabled(
        c, keyer=d['key_index'], enabled=d['enabled'], me=d.get('me', 0)),
    'set_usk_mask_top': lambda c, d: ops.set_usk_mask_top(
        c, keyer=d['key_index'], top=d['top'], me=d.get('me', 0)),
    'set_usk_mask_bottom': lambda c, d: ops.set_usk_mask_bottom(
        c, keyer=d['key_index'], bottom=d['bottom'], me=d.get('me', 0)),
    'set_usk_mask_left': lambda c, d: ops.set_usk_mask_left(
        c, keyer=d['key_index'], left=d['left'], me=d.get('me', 0)),
    'set_usk_mask_right': lambda c, d: ops.set_usk_mask_right(
        c, keyer=d['key_index'], right=d['right'], me=d.get('me', 0)),

    # USK DVE
    'set_usk_dve_position_x': lambda c, d: ops.set_usk_dve_position_x(
        c, keyer=d['key_index'], position_x=d['positionX'], me=d.get('me', 0)),
    'set_usk_dve_position_y': lambda c, d: ops.set_usk_dve_position_y(
        c, keyer=d['key_index'], position_y=d['positionY'], me=d.get('me', 0)),
    'set_usk_dve_size_x': lambda c, d: ops.set_usk_dve_size_x(
        c, keyer=d['key_index'], size_x=d['sizeX'], me=d.get('me', 0)),
    'set_usk_dve_size_y': lambda c, d: ops.set_usk_dve_size_y(
        c, keyer=d['key_index'], size_y=d['sizeY'], me=d.get('me', 0)),
    'set_usk_dve_rotation': lambda c, d: ops.set_usk_dve_rotation(
        c, keyer=d['key_index'], rotation=d['rotation'], me=d.get('me', 0)),
    'set_usk_dve_masked': lambda c, d: ops.set_usk_dve_masked(
        c, keyer=d['key_index'], masked=d['masked'], me=d.get('me', 0)),
    'set_usk_dve_top': lambda c, d: ops.set_usk_dve_top(
        c, keyer=d['key_index'], top=d['top'], me=d.get('me', 0)),
    'set_usk_dve_bottom': lambda c, d: ops.set_usk_dve_bottom(
        c, keyer=d['key_index'], bottom=d['bottom'], me=d.get('me', 0)),
    'set_usk_dve_left': lambda c, d: ops.set_usk_dve_left(
        c, keyer=d['key_index'], left=d['left'], me=d.get('me', 0)),
    'set_usk_dve_right': lambda c, d: ops.set_usk_dve_right(
        c, keyer=d['key_index'], right=d['right'], me=d.get('me', 0)),
    'set_usk_dve_border_enabled': lambda c, d: ops.set_usk_dve_border_enabled(
        c, keyer=d['key_index'], enabled=d['enabled'], me=d.get('me', 0)),
    'set_usk_dve_border_hue': lambda c, d: ops.set_usk_dve_border_hue(
        c, keyer=d['key_index'], hue=d['hue'], me=d.get('me', 0)),
    'set_usk_dve_border_saturation': lambda c, d: ops.set_usk_dve_border_saturation(
        c, keyer=d['key_index'], saturation=d['saturation'], me=d.get('me', 0)),
    'set_usk_dve_border_luma': lambda c, d: ops.set_usk_dve_border_luma(
        c, keyer=d['key_index'], luma=d['luma'], me=d.get('me', 0)),
    'set_usk_dve_border_opacity': lambda c, d: ops.set_usk_dve_border_opacity(
        c, keyer=d['key_index'], opacity=d['opacity'], me=d.get('me', 0)),
    'set_usk_dve_border_outer_width': lambda c, d: ops.set_usk_dve_border_outer_width(
        c, keyer=d['key_index'], outer_width=d['outerWidth'], me=d.get('me', 0)),
    'set_usk_dve_border_inner_width': lambda c, d: ops.set_usk_dve_border_inner_width(
        c, keyer=d['key_index'], inner_width=d['innerWidth'], me=d.get('me', 0)),
    'set_usk_dve_border_outer_softness': lambda c, d: ops.set_usk_dve_border_outer_softness(
        c, keyer=d['key_index'], outer_softness=d['outerSoftness'], me=d.get('me', 0)),
    'set_usk_dve_border_inner_softness': lambda c, d: ops.set_usk_dve_border_inner_softness(
        c, keyer=d['key_index'], inner_softness=d['innerSoftness'], me=d.get('me', 0)),
    'set_usk_dve_border_bevel_position': lambda c, d: ops.set_usk_dve_border_bevel_position(
        c, keyer=d['key_index'], bevel_position=d['bevelPosition'], me=d.get('me', 0)),
    'set_usk_dve_border_bevel_softness': lambda c, d: ops.set_usk_dve_border_bevel_softness(
        c, keyer=d['key_index'], bevel_softness=d['bevelSoftness'], me=d.get('me', 0)),
    'set_usk_dve_light_direction': lambda c, d: ops.set_usk_dve_light_direction(
        c, keyer=d['key_index'], direction=d['direction'], me=d.get('me', 0)),
    'set_usk_dve_light_altitude': lambda c, d: ops.set_usk_dve_light_altitude(
        c, keyer=d['key_index'], altitude=d['altitude'], me=d.get('me', 0)),
    'set_usk_dve_shadow': lambda c, d: ops.set_usk_dve_shadow(
        c, keyer=d['key_index'], shadow=d['shadow'], me=d.get('me', 0)),
    'set_usk_dve_rate': lambda c, d: ops.set_usk_dve_rate(
        c, keyer=d['key_index'], rate_str=d['rate'], me=d.get('me', 0)),

    # Flying key (keyframes)
    'set_keyer_fly_keyframe': lambda c, d: ops.set_keyer_fly_keyframe(
        c, keyer=d['key_index'], keyframe=d['keyframe'], me=d.get('me', 0)),
    'run_flying_key_keyframe': lambda c, d: ops.run_flying_key_keyframe(
        c, keyer=d['key_index'], keyframe=d['keyframe'], me=d.get('me', 0)),
    'run_flying_key_infinite_direction': lambda c, d: ops.run_flying_key_infinite_direction(
        c, keyer=d['key_index'], direction=d['direction'], me=d.get('me', 0)),
    'set_usk_fly_enabled': lambda c, d: ops.set_usk_fly_enabled(
        c, keyer=d['key_index'], enabled=d['enabled'], me=d.get('me', 0)),

    # USK Advanced Chroma — sample-based
    'set_usk_chroma_sample': lambda c, d: ops.set_usk_chroma_sample(
        c, keyer=d['key_index'], enabled=d['enabled'], me=d.get('me', 0)),
    'set_usk_chroma_sample_position': lambda c, d: ops.set_usk_chroma_sample_position(
        c, keyer=d['key_index'], x=d['x'], y=d['y'], me=d.get('me', 0)),
    'set_usk_chroma_sample_size': lambda c, d: ops.set_usk_chroma_sample_size(
        c, keyer=d['key_index'], size=d['size'], me=d.get('me', 0)),
    'set_usk_chroma_preview': lambda c, d: ops.set_usk_chroma_preview(
        c, keyer=d['key_index'], preview=d['preview'], me=d.get('me', 0)),

    # USK Chroma — color correction / spill / edge
    'set_usk_chroma_foreground': lambda c, d: ops.set_usk_chroma_foreground(
        c, keyer=d['key_index'], foreground=d['foreground'], me=d.get('me', 0)),
    'set_usk_chroma_background': lambda c, d: ops.set_usk_chroma_background(
        c, keyer=d['key_index'], background=d['background'], me=d.get('me', 0)),
    'set_usk_chroma_key_edge': lambda c, d: ops.set_usk_chroma_key_edge(
        c, keyer=d['key_index'], key_edge=d['keyEdge'], me=d.get('me', 0)),
    'set_usk_chroma_spill': lambda c, d: ops.set_usk_chroma_spill(
        c, keyer=d['key_index'], spill=d['spill'], me=d.get('me', 0)),
    'set_usk_chroma_flare_suppression': lambda c, d: ops.set_usk_chroma_flare_suppression(
        c, keyer=d['key_index'], flare_suppression=d['flareSuppression'], me=d.get('me', 0)),
    'set_usk_chroma_brightness': lambda c, d: ops.set_usk_chroma_brightness(
        c, keyer=d['key_index'], brightness=d['brightness'], me=d.get('me', 0)),
    'set_usk_chroma_contrast': lambda c, d: ops.set_usk_chroma_contrast(
        c, keyer=d['key_index'], contrast=d['contrast'], me=d.get('me', 0)),
    'set_usk_chroma_saturation': lambda c, d: ops.set_usk_chroma_saturation(
        c, keyer=d['key_index'], saturation=d['saturation'], me=d.get('me', 0)),
    'set_usk_chroma_red': lambda c, d: ops.set_usk_chroma_red(
        c, keyer=d['key_index'], red=d['red'], me=d.get('me', 0)),
    'set_usk_chroma_green': lambda c, d: ops.set_usk_chroma_green(
        c, keyer=d['key_index'], green=d['green'], me=d.get('me', 0)),
    'set_usk_chroma_blue': lambda c, d: ops.set_usk_chroma_blue(
        c, keyer=d['key_index'], blue=d['blue'], me=d.get('me', 0)),

    # DSK — ``dsk`` selects the keyer (0-based); absent means DSK 1, so
    # pre-3B clients keep working unchanged.
    'set_dsk_fill_source': lambda c, d: ops.set_dsk_fill_source(
        c, source=d['source'], dsk_idx=d.get('dsk', 0)),
    'set_dsk_key_source': lambda c, d: ops.set_dsk_key_source(
        c, source=d['source'], dsk_idx=d.get('dsk', 0)),
    'set_dsk_mask_enabled': lambda c, d: ops.set_dsk_mask_enabled(
        c, enabled=d['enabled'], dsk_idx=d.get('dsk', 0)),
    'set_dsk_mask_top': lambda c, d: ops.set_dsk_mask_top(
        c, top=d['top'], dsk_idx=d.get('dsk', 0)),
    'set_dsk_mask_bottom': lambda c, d: ops.set_dsk_mask_bottom(
        c, bottom=d['bottom'], dsk_idx=d.get('dsk', 0)),
    'set_dsk_mask_left': lambda c, d: ops.set_dsk_mask_left(
        c, left=d['left'], dsk_idx=d.get('dsk', 0)),
    'set_dsk_mask_right': lambda c, d: ops.set_dsk_mask_right(
        c, right=d['right'], dsk_idx=d.get('dsk', 0)),
    'set_dsk_pre_multiplied': lambda c, d: ops.set_dsk_pre_multiplied(
        c, pre_multiplied=d['pre_multiplied'], dsk_idx=d.get('dsk', 0)),
    'set_dsk_clip': lambda c, d: ops.set_dsk_clip(
        c, clip=d['clip'], dsk_idx=d.get('dsk', 0)),
    'set_dsk_gain': lambda c, d: ops.set_dsk_gain(
        c, gain=d['gain'], dsk_idx=d.get('dsk', 0)),
    'set_dsk_invert_key': lambda c, d: ops.set_dsk_invert_key(
        c, invert_key=d['invert_key'], dsk_idx=d.get('dsk', 0)),

    # =========================================================================
    # Fairlight audio mixer
    # =========================================================================
    # The frontend sends ``volume_db`` as a number or ``null`` (silenced).
    # Translate ``null`` to ``float('-inf')`` here so the operations layer
    # can clamp via _db_to_wire to the wire's silenced sentinel (-10000).

    # ----- Master out -----
    'set_audio_master_volume': lambda c, d: ops.set_fairlight_master(
        c, volume_db=(float('-inf') if d.get('volume_db') is None
                       else float(d['volume_db']))),
    'set_audio_master_eq_enable': lambda c, d: ops.set_fairlight_master(
        c, eq_enable=bool(d['enabled'])),
    'set_audio_master_eq_gain': lambda c, d: ops.set_fairlight_master(
        c, eq_gain_db=float(d['gain_db'])),
    'set_audio_master_dynamics_makeup': lambda c, d: ops.set_fairlight_master(
        c, dynamics_makeup_db=float(d['gain_db'])),
    'set_audio_master_afv': lambda c, d: ops.set_fairlight_master(
        c, afv=bool(d['afv'])),

    # ----- Per-strip -----
    # ``source`` + ``channel`` come straight from the state's per-strip
    # dict; the frontend echoes them back unchanged.
    'set_audio_strip_volume': lambda c, d: ops.set_fairlight_strip(
        c, source=int(d['source']), channel=int(d['channel']),
        fader_gain_db=(float('-inf') if d.get('volume_db') is None
                        else float(d['volume_db']))),
    'set_audio_strip_input_gain': lambda c, d: ops.set_fairlight_strip(
        c, source=int(d['source']), channel=int(d['channel']),
        input_gain_db=float(d['gain_db'])),
    'set_audio_strip_pan': lambda c, d: ops.set_fairlight_strip(
        c, source=int(d['source']), channel=int(d['channel']),
        pan=float(d['pan'])),
    'set_audio_strip_mix_type': lambda c, d: ops.set_fairlight_strip(
        c, source=int(d['source']), channel=int(d['channel']),
        mix_option=d['mix_type']),  # str ('Off' / 'On' / 'AudioFollowVideo')
    'set_audio_strip_eq_enable': lambda c, d: ops.set_fairlight_strip(
        c, source=int(d['source']), channel=int(d['channel']),
        eq_enable=bool(d['enabled'])),
    'set_audio_strip_eq_gain': lambda c, d: ops.set_fairlight_strip(
        c, source=int(d['source']), channel=int(d['channel']),
        eq_gain_db=float(d['gain_db'])),
    'set_audio_strip_dynamics_makeup': lambda c, d: ops.set_fairlight_strip(
        c, source=int(d['source']), channel=int(d['channel']),
        dynamics_makeup_db=float(d['gain_db'])),

    # ----- EQ band (master uses source=0 channel=-1; per-strip uses real source/channel) -----
    'set_audio_eq_band': lambda c, d: ops.set_fairlight_eq_band(
        c,
        source=int(d['source']), channel=int(d['channel']),
        band=int(d['band']),
        enabled=(None if d.get('enabled') is None else bool(d['enabled'])),
        shape=d.get('shape'),
        frequency_hz=(None if d.get('frequency') is None
                       else int(d['frequency'])),
        gain_db=(None if d.get('gain_db') is None else float(d['gain_db'])),
        q=(None if d.get('q') is None else float(d['q'])),
        frequency_range=d.get('range'),
    ),
    # Master-bus per-band EQ → CMBP (no source/channel), not a per-strip
    # CEBP at the phantom source 0.
    'set_audio_master_eq_band': lambda c, d: ops.set_fairlight_master_eq_band(
        c,
        band=int(d['band']),
        enabled=(None if d.get('enabled') is None else bool(d['enabled'])),
        shape=d.get('shape'),
        frequency_hz=(None if d.get('frequency') is None
                       else int(d['frequency'])),
        gain_db=(None if d.get('gain_db') is None else float(d['gain_db'])),
        q=(None if d.get('q') is None else float(d['q'])),
        frequency_range=d.get('range'),
    ),
}


def dispatch(connection, command: str, data: dict) -> HandlerResult:
    """Look up the handler for a command name and execute it. Returns
    ``(ok, error)`` tuple. Consumer treats ok=False as a failed command and
    emits ``error`` back to the frontend."""
    handler = ATEM_COMMAND_MAP.get(command)
    if handler is None:
        return False, f"unknown command: {command}"
    try:
        handler(connection, data)
        return True, None
    except KeyError as e:
        return False, f"{command}: missing arg {e}"
    except Exception as e:
        logger.exception(f"Error executing {command}")
        return False, f"{command}: {type(e).__name__}: {e}"
