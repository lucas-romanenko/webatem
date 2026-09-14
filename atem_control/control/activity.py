"""ATEM control command → activity-log classification + human summary
(2026-07-08).

Every operator action on the control page flows through the consumer's
``execute_command`` → this module decides how to log it:

  * DISCRETE actions (cut, program/preview, toggles, selects, macros) are
    logged immediately, one row each.
  * CONTINUOUS actions (slider/drag controls — chroma, wipe position,
    faders, DVE geometry…) fire ~20×/second during a drag; the consumer
    debounces them by ``debounce_key`` and logs ONE settled row per
    adjustment instead of the whole stream.

``summarize`` turns the command + its args into a readable one-liner,
resolving source ids to their labels from mixerstate where relevant.
"""
from atemwire.messages.input_video import input_label

# Args that identify WHICH control (not its value) — used to build the
# debounce key and the summary's instance prefix.
ID_ARGS = {'me', 'key_index', 'keyer', 'dsk', 'dsk_idx', 'generator',
           'strip', 'source_id', 'band', 'player', 'aux', 'mixer'}

# Continuous (drag-slider) classification. A command is continuous if it
# ends in one of these value suffixes (or is an explicit multi-param
# slider), UNLESS it's an explicit discrete override (a select/action that
# happens to share a suffix).
_CONTINUOUS_SUFFIXES = (
    '_x', '_y', '_hue', '_saturation', '_luma', '_gain', '_clip', '_softness',
    '_symmetry', '_width', '_rotation', '_top', '_bottom', '_left', '_right',
    '_opacity', '_duration', '_trigger_point', '_pre_roll', '_volume',
    '_input_gain', '_pan', '_makeup', '_direction', '_altitude', '_shadow',
    '_brightness', '_contrast', '_red', '_green', '_blue', '_key_edge',
    '_spill', '_flare_suppression', '_bevel_position', '_bevel_softness',
    '_sample_position', '_sample_size', '_band', '_size_x', '_size_y',
    '_foreground', '_background',
)
_CONTINUOUS_OVERRIDES = {
    'set_audio_compressor', 'set_audio_limiter', 'set_audio_expander',
    'set_transition_position',   # the T-bar drag (CTPs stream)
}
_DISCRETE_OVERRIDES = {
    'set_stinger_clip',       # selects a clip index, not a drag
    'set_stinger_source',
    'run_flying_key_infinite_direction',  # an action
}


def is_continuous(command: str) -> bool:
    if command in _DISCRETE_OVERRIDES:
        return False
    if command in _CONTINUOUS_OVERRIDES:
        return True
    return command.endswith(_CONTINUOUS_SUFFIXES)


def debounce_key(command: str, data: dict):
    """Key a slider stream by its control identity (command + which
    ME/keyer/strip/etc.), NOT its value — so USK1 hue and USK2 hue debounce
    independently but successive ticks of the same slider coalesce."""
    ident = tuple(sorted((k, data[k]) for k in data
                         if k in ID_ARGS and _scalar(data[k])))
    return (command, ident)


def _scalar(v):
    return isinstance(v, (str, int, float, bool)) or v is None


def _instance_prefix(data: dict) -> str:
    """Readable 'which control' prefix, e.g. 'USK2 ', 'DSK1 ', 'ME2 '."""
    parts = []
    me = data.get('me')
    if isinstance(me, int) and me > 0:
        parts.append(f"ME{me + 1}")
    if 'key_index' in data:
        parts.append(f"USK{_int(data['key_index']) + 1}")
    elif 'keyer' in data:
        parts.append(f"USK{_int(data['keyer']) + 1}")
    if 'dsk' in data:
        parts.append(f"DSK{_int(data['dsk']) + 1}")
    elif 'dsk_idx' in data:
        parts.append(f"DSK{_int(data['dsk_idx']) + 1}")
    if 'generator' in data:
        parts.append(f"ColorGen{_int(data['generator']) + 1}")
    if 'strip' in data:
        parts.append(f"Strip {data['strip']}")
    return (' '.join(parts) + ' ') if parts else ''


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _value_str(data: dict) -> str:
    """The value part of the command — every non-identity scalar arg,
    formatted compactly. Rounds floats; joins multiples with commas."""
    bits = []
    for k, v in data.items():
        if k in ID_ARGS or not _scalar(v):
            continue
        if isinstance(v, float):
            v = round(v, 2)
        bits.append(str(v))
    return ', '.join(bits)


def _humanize(command: str, data: dict) -> str:
    """Generic fallback: 'set_usk_pattern_softness' + {keyer:0, softness:40}
    → 'USK1 pattern softness → 40'."""
    name = command
    for pre in ('set_', 'toggle_', 'run_'):
        if name.startswith(pre):
            name = name[len(pre):]
            break
    prefix = _instance_prefix(data)
    # Drop tokens the prefix already conveys, to avoid 'USK1 usk pattern …'.
    words = [w for w in name.split('_')
             if w not in ('usk', 'dsk', 'me', 'keyer', 'strip', 'generator')]
    label = ' '.join(words) or name.replace('_', ' ')
    value = _value_str(data)
    text = f"{prefix}{label}"
    if command.startswith('toggle_'):
        text = f"Toggled {text}"
    return f"{text} → {value}" if value else text.strip().capitalize()


def summarize(command: str, data: dict, mixerstate=None) -> str:
    """Human one-liner for a control command. ``mixerstate`` (optional)
    resolves source ids to labels for the source-picking commands."""
    d = data or {}
    me = d.get('me')
    me_suffix = f" (ME{me + 1})" if isinstance(me, int) and me > 0 else ''

    def src(sid):
        if mixerstate is not None:
            lbl = input_label(mixerstate, _int(sid))
            name = lbl.get('long') or lbl.get('short')
            if name:
                return name
        return f"input {sid}"

    if command == 'cut':
        return f"Cut{me_suffix}"
    if command == 'auto':
        return f"Auto transition{me_suffix}"
    if command == 'set_transition_position':
        return f"T-bar → {round(_int(d.get('position')) / 100)}%{me_suffix}"
    if command == 'set_program':
        return f"Program → {src(d.get('source'))}{me_suffix}"
    if command == 'set_preview':
        return f"Preview → {src(d.get('source'))}{me_suffix}"
    if command == 'toggle_usk':
        return f"Toggled USK{_int(d.get('key_index')) + 1} on air{me_suffix}"
    if command == 'toggle_dsk':
        return f"Toggled DSK{_int(d.get('dsk')) + 1} on air"
    if command == 'dsk_auto':
        return f"DSK{_int(d.get('dsk')) + 1} auto"
    if command == 'fade_to_black':
        return f"Fade to black{me_suffix}"
    if command == 'toggle_transition_background':
        return f"Toggled background in next transition{me_suffix}"
    if command == 'toggle_transition_key':
        return f"Toggled USK{_int(d.get('key_index')) + 1} in next transition{me_suffix}"
    if command == 'set_transition_style':
        return f"Transition style → {d.get('style')}{me_suffix}"
    if command == 'set_aux_output':
        aux = d.get('aux')
        aux_lbl = f"Aux {aux + 1}" if isinstance(aux, int) else "Aux"
        return f"{aux_lbl} → {src(d.get('source'))}"
    if command == 'set_video_mode':
        return f"Video mode → {d.get('mode', d.get('video_mode'))}"
    if command == 'set_input_label':
        lbl = d.get('long_name') or d.get('short_name') or ''
        return f"Renamed input {d.get('source')} to '{lbl}'"
    if command == 'execute_macro':
        idx = d.get('index', d.get('macro'))
        return f"Ran macro {idx + 1 if isinstance(idx, int) else idx}"
    if command == 'set_media_player_still':
        return (f"Media player {_int(d.get('player')) + 1} → still "
                f"{_int(d.get('still', d.get('index'))) + 1}")
    if command in ('set_wipe_fill_source', 'set_dve_fill_source',
                   'set_usk_fill_source', 'set_dsk_fill_source',
                   'set_dip_source', 'set_stinger_source', 'set_wipe_pattern'):
        val = d.get('source') or d.get('fillSource') or d.get('pattern') \
            or d.get('input_source')
        if command.endswith('_source') and str(val).isdigit():
            val = src(val)
        return f"{_humanize(command, {k: v for k, v in d.items() if k in ID_ARGS})} → {val}"

    return _humanize(command, d)
