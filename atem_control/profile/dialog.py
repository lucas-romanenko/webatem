"""
Dialog descriptors — describe which sections are available for save /
restore on a given switcher / in a given profile, with topology gating
and per-section ``supported`` flags reflecting the apply implementation
status. Drives the section-selection modal on the control page.

Lives in the app rather than in atemwire's profile package because the descriptor
shape (``group``, ``me_tab``, ``checked``, ``supported``, ``reason``,
tooltip text) is UI scaffolding for the section-selection modal, not
part of the save/restore feature itself.

Per-M/E layout (Stage 4C-2): both descriptors always emit
``PLATFORM_MAX_MES`` (4) ``me_tabs`` entries — ``{index, available,
reason}`` — so the dialog renders the same 4-tab frame on every model.
Tab availability is capability-driven from the connected mixerstate
(``me_count``, the same source ``build_full_state`` uses); the load
dialog additionally requires the uploaded XML to contain that M/E's
``<MixEffectBlock>``. Sections inside an M/E tab carry me-namespaced
ids (``me<N>_program`` … ``me<N>_usk_<k>``, ``me_tab: N`` 0-based);
``me_options_from_sections`` below is the single place that parses the
namespacing back into ``mes[N]`` ``MixEffectOptions`` for the two
submission-mapping functions (``export.build_save_options`` /
``views._build_apply_options``) — the ids never travel past them.
Global section ids stay flat and match the ``SaveOptions`` /
``ApplyOptions`` field names so the mapping needs no translation table.
"""

import re
from typing import TYPE_CHECKING, List

from atemwire.state import me_count, me_keyer_count


def _int(s, default: int = 0) -> int:
    """Parse an XML attribute as an int, falling back rather than raising.
    Local on purpose: this used to import atemwire's XML internals for six
    lines of parsing, which tied the dialog to a private module."""
    if s is None or s == '':
        return default
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default
from atemwire.profile.options import MixEffectOptions, PLATFORM_MAX_MES

if TYPE_CHECKING:
    from atemwire.profile import Profile


def _accept_atem_or_protocol(atem) -> None:
    """Type-check that the caller passed an ATEM / ATEMConnection /
    AtemProtocol. Raises TypeError otherwise — same contract as
    ``Profile.from_atem`` so the dialog can be called with the same
    argument shape."""
    if hasattr(atem, 'mixerstate'):
        return
    if hasattr(atem, 'raw') and hasattr(atem.raw, 'mixerstate'):
        return
    raise TypeError(
        f"describe_save_sections expected ATEM/ATEMConnection/AtemProtocol, "
        f"got {type(atem).__name__}"
    )


def _mixerstate(atem) -> dict:
    """The mixerstate dict for any of the accepted argument shapes
    (``ATEM`` exposes it via ``.raw``; connection/protocol directly)."""
    if hasattr(atem, 'mixerstate'):
        return atem.mixerstate or {}
    return atem.raw.mixerstate or {}


# =============================================================================
# Submission mapping — me-namespaced selection ids → MixEffectOptions
# =============================================================================


def section_value(sections: dict, key: str, default: bool) -> bool:
    """Read a boolean from the section selection JSON, tolerating both
    'true'/'false' strings and bools.

    Shared by ``export.build_save_options`` and the load-side view's
    ApplyOptions translation (both import it from here)."""
    v = sections.get(key, default)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ('true', '1', 'yes', 'on')


_ME_SECTION_RE = re.compile(r'^me(\d+)_(.+)$')


def me_options_from_sections(sections: dict, *, program_default: bool,
                             preview_default: bool,
                             ) -> List[MixEffectOptions]:
    """Parse the me-namespaced selection ids (``me<N>_<section>``) out
    of a submitted section dict into a ``mes`` list of
    ``MixEffectOptions``, one entry per submitted M/E tab.

    This is the single place the ``me<N>_`` transport namespacing is
    parsed; the two submission-mapping functions
    (``export.build_save_options`` / ``views._build_apply_options``)
    call it and nothing else sees the namespaced ids.

    The list is sized to the highest submitted M/E index + 1. The
    dialog submits every section it rendered (checked or not), so on
    save that equals the connected switcher's real me_count — not the
    ``PLATFORM_MAX_MES`` default — and on load it is at most me_count
    (tabs absent from the file emit no sections). A gap (an index below
    the max with no submitted cells) maps to
    ``MixEffectOptions.none()``: the operator saw no checkboxes for
    that M/E, so nothing is saved/applied for it. The ``usk`` list is
    sized to the highest submitted ``usk_<k>`` cell for that M/E (the
    descriptor emits one cell per real keyer).

    ``program_default`` / ``preview_default`` fill cells missing from a
    submitted tab — True on save, False on apply (the cuts-to-air
    gate), mirroring the per-context dataclass defaults.
    """
    per_me: dict = {}
    for key, value in (sections or {}).items():
        m = _ME_SECTION_RE.match(key)
        if m:
            per_me.setdefault(int(m.group(1)), {})[m.group(2)] = value
    if not per_me:
        return []

    mes: List[MixEffectOptions] = []
    for me in range(max(per_me) + 1):
        cells = per_me.get(me)
        if cells is None:
            mes.append(MixEffectOptions.none())
            continue
        usk_count = max(
            (int(name[4:]) for name in cells
             if name.startswith('usk_') and name[4:].isdigit()),
            default=0)
        mes.append(MixEffectOptions(
            program=section_value(cells, 'program', program_default),
            preview=section_value(cells, 'preview', preview_default),
            next_transition=section_value(cells, 'next_transition', True),
            transition_style=section_value(cells, 'transition_style', True),
            fade_to_black=section_value(cells, 'fade_to_black', True),
            usk=[section_value(cells, f'usk_{k}', True)
                 for k in range(1, usk_count + 1)],
        ))
    return mes


# =============================================================================
# Section-selection dialog support
# =============================================================================


# The five non-USK cells of one M/E tab, in Software Control's grid
# order. (id-suffix, label, save-side reason or None).
_ME_CELLS = [
    ('program', 'Program',
     "Sets program bus on save; cuts to air on restore."),
    ('preview', 'Preview', None),
    ('next_transition', 'Next Transition', 'BG / Key1-4 selection mask.'),
    ('transition_style', 'Transition Style',
     'Mix/Dip/Wipe/Stinger/DVE + per-style parameters.'),
    ('fade_to_black', 'Fade To Black', None),
]

_REASON_NO_ME = 'Not present on this switcher.'
_REASON_NOT_IN_FILE = 'Not in this profile file.'


def _me_cell(me: int, name: str, label: str, *, checked: bool = True,
             reason: str = None) -> dict:
    s = {'id': f'me{me}_{name}', 'label': label, 'group': 'switcher_me',
         'me_tab': me, 'supported': True, 'checked': checked}
    if reason:
        s['reason'] = reason
    return s


def describe_save_sections(atem) -> dict:
    """Build the section descriptor the save dialog renders.

    ``me_tabs`` is always ``PLATFORM_MAX_MES`` (4) entries of
    ``{index, available, reason}``; a tab is available when its index
    is below the connected switcher's ``me_count``. Each available
    tab contributes per-M/E sections (program, preview,
    next_transition, transition_style, fade_to_black, usk_1..k where
    k is that M/E's ``me_keyer_count``) with me-namespaced ids
    (``me<N>_<name>``); unavailable tabs contribute no sections — the
    frontend renders them greyed and inert. USK cells beyond an M/E's
    real keyer count are absent, not greyed.

    Model-independent sections backed by apply gaps (Camera Control,
    Counter) still render with ``supported: False`` and a tooltip;
    sections that don't apply to the hardware at all (SuperSource,
    Media Player 3-4, Stream/Record, Audio Mapping, Remote Source)
    are omitted from the descriptor.

    Each ``section`` entry has:
      - ``id``: stable string identifier consumed by
        ``atem_control.profile.export.build_save_options``
        (me-namespaced for per-M/E cells, flat for globals).
      - ``label``: human-readable label.
      - ``group``: presentational group — one of ``switcher_me``
        (rendered inside its M/E tab box), ``switcher_global``
        (rendered below the M/E boxes but inside the Switcher group),
        ``media``, ``io``, ``others``.
      - ``me_tab``: the 0-based M/E index when ``group ==
        'switcher_me'`` (matches ``options.mes[N]``).
      - ``checked``: default state in the dialog.
      - ``supported``: False when the section is gated by an apply
        gap; the cell renders disabled with a tooltip.
      - ``reason``: tooltip text.
    """
    _accept_atem_or_protocol(atem)
    mx = _mixerstate(atem)
    live_me_count = me_count(mx)

    me_tabs = []
    sections = []
    for me in range(PLATFORM_MAX_MES):
        available = me < live_me_count
        me_tabs.append({
            'index': me,
            'available': available,
            'reason': '' if available else _REASON_NO_ME,
        })
        if not available:
            continue
        for name, label, reason in _ME_CELLS:
            sections.append(_me_cell(me, name, label, reason=reason))
        for k in range(me_keyer_count(mx, me)):
            sections.append(
                _me_cell(me, f'usk_{k + 1}', f'Upstream Key {k + 1}'))

    sections += [
        # ---- Switcher / global (siblings of the M/E boxes) ----
        {'id': 'downstream_keys', 'label': 'Downstream Keyers',
         'group': 'switcher_global',
         'supported': True, 'checked': True},
        {'id': 'color_generators', 'label': 'Color Generators',
         'group': 'switcher_global',
         'supported': True, 'checked': True},
        {'id': 'camera_control', 'label': 'Camera Control',
         'group': 'switcher_global',
         'supported': False, 'checked': False,
         'reason': 'Not yet supported in this version.'},
        {'id': 'fairlight', 'label': 'Audio Mixer',
         'group': 'switcher_global',
         'supported': True, 'checked': True,
         'reason': ('Compressor/limiter/expander parameters are not yet '
                    'supported — the rest applies normally.')},

        # ---- Media ----
        # One Media Pool cell, like ATEM Software Control's save
        # dialog: selecting it saves the slot listing AND captures the
        # used slots' images. build_save_options fans it out to the
        # two SaveOptions flags (media_pool_metadata + _images).
        {'id': 'media_pool', 'label': 'Media Pool',
         'group': 'media',
         'supported': True, 'checked': True,
         'reason': ('Slot listing + the used slots’ images '
                    '(capturing takes ~5–10 s per image).')},
        {'id': 'media_players', 'label': 'Media Players (1-2)',
         'group': 'media',
         'supported': True, 'checked': True},

        # ---- Input and Output ----
        {'id': 'auxiliaries', 'label': 'Outputs',
         'group': 'io',
         'supported': True, 'checked': True},
        {'id': 'counters', 'label': 'Counter',
         'group': 'io',
         'supported': False, 'checked': False,
         'reason': 'Not yet supported in this version.'},

        # ---- Others ----
        {'id': 'settings', 'label': 'Settings',
         'group': 'others',
         'supported': True, 'checked': True,
         'reason': 'FtB enabled, MV layout, button mapping'},
        {'id': 'video_mode', 'label': 'Video Mode',
         'group': 'others',
         'supported': True, 'checked': True,
         'reason': ('Restoring a different video mode causes a brief '
                    'output resync.')},
        {'id': 'hyperdecks', 'label': 'HyperDecks',
         'group': 'others',
         'supported': True, 'checked': True,
         'reason': 'Deck IP + switcher input per slot.'},
        {'id': 'macros', 'label': 'Macros',
         'group': 'others',
         'supported': True, 'checked': True},
    ]

    return {'sections': sections, 'me_tabs': me_tabs}


def referenced_images(profile: 'Profile') -> list:
    """``<Still>`` entries (with a usable ``path``) referenced by the
    profile — the image files the load flow expects the operator to
    supply. Used by the load descriptor and by the load view's
    image-matching pass."""
    out = []
    for st in profile.root.findall('./MediaPool/Stills/Still'):
        path = st.get('path', '')
        name = st.get('name', '')
        idx = st.get('index', '0')
        if path:
            out.append({
                'slot': int(idx) if idx.isdigit() else 0,
                'name': name,
                'path': path,
                'filename': path.rsplit('/', 1)[-1] if '/' in path else path,
            })
    return out


def _me_load_sections(me: int, block) -> list:
    """The per-M/E cells for one present ``<MixEffectBlock>`` — only
    cells whose XML element is actually inside the block are emitted
    (the per-M/E extension of the "operator can't restore what wasn't
    saved" rule)."""
    sections = []
    if block.find('Program') is not None:
        sections.append(_me_cell(
            me, 'program', 'Program', checked=False,
            reason='Off by default — applying would cut to air.'))
    if block.find('Preview') is not None:
        sections.append(_me_cell(
            me, 'preview', 'Preview', checked=False,
            reason='Off by default — applying could change preview bus.'))
    if block.find('NextTransition') is not None:
        sections.append(_me_cell(
            me, 'next_transition', 'Next Transition',
            reason='BG / Key1-4 selection mask.'))
    if block.find('TransitionStyle') is not None:
        sections.append(_me_cell(
            me, 'transition_style', 'Transition Style',
            reason='Mix/Dip/Wipe/Stinger/DVE + per-style parameters.'))
    if block.find('FadeToBlack') is not None:
        sections.append(_me_cell(me, 'fade_to_black', 'Fade To Black'))

    has_key = [False, False, False, False]
    for k_e in block.findall('./Keys/Key'):
        try:
            idx_int = int(k_e.get('index', ''))
        except (TypeError, ValueError):
            continue
        if 0 <= idx_int < 4:
            has_key[idx_int] = True
    for i in range(4):
        if has_key[i]:
            sections.append(
                _me_cell(me, f'usk_{i + 1}', f'Upstream Key {i + 1}'))
    return sections


def describe_load_sections(profile: 'Profile', atem) -> dict:
    """Section descriptor for the load dialog. Same shape as
    ``describe_save_sections`` (per-section ``group`` + ``me_tab``,
    4-entry ``me_tabs``) but driven by the parsed XML AND the connected
    switcher's topology.

    An M/E tab is available iff its index is below the connected
    ``me_count`` AND the XML contains its ``<MixEffectBlock>``
    (block→index resolution mirrors apply's ``_iter_me_blocks``:
    missing/invalid ``index`` attribute counts as 0). Within an
    available tab, **only cells whose XML element is actually present
    in that block are emitted** — an operator can't restore what
    wasn't saved, so omitting absent sections keeps the dialog honest.
    Unavailable tabs contribute no sections; their ``reason`` says
    whether the switcher or the file is the limiter.

    The ``supported`` flag on emitted sections indicates whether the
    apply path supports that section (``False`` for known gaps —
    Camera Control, HyperDecks, Counter — even when they're in the
    XML); ``unsupported`` cells render greyed with a tooltip.

    Defaults: every emitted section pre-checked, except Program /
    Preview (cuts-to-air gate) which start unchecked.

    The presence checks use ``element is not None`` rather than
    ``bool(element)``: ElementTree's ``__bool__`` is False when an
    element has no subelements, so self-closing tags like
    ``<VideoMode videoMode="1080p60"/>`` would have been falsely
    reported as missing if ``bool()`` were used.
    """
    _accept_atem_or_protocol(atem)
    mx = _mixerstate(atem)
    live_me_count = me_count(mx)
    root = profile.root

    # ---- Per-M/E blocks, keyed by index (first block wins on a
    # duplicate index — apply would apply both, but the selection is
    # one checkbox set either way).
    blocks: dict = {}
    for blk in root.findall('./MixEffectBlocks/MixEffectBlock'):
        blocks.setdefault(_int(blk.get('index'), 0), blk)

    me_tabs = []
    sections = []
    for me in range(PLATFORM_MAX_MES):
        block = blocks.get(me)
        if me >= live_me_count:
            me_tabs.append({'index': me, 'available': False,
                            'reason': _REASON_NO_ME})
            continue
        if block is None:
            me_tabs.append({'index': me, 'available': False,
                            'reason': _REASON_NOT_IN_FILE})
            continue
        me_tabs.append({'index': me, 'available': True, 'reason': ''})
        sections.extend(_me_load_sections(me, block))

    # ---- Per-element XML presence for the global sections (always
    # use ``is not None``; an element with attributes-only and no
    # children is still present).
    has_dsk = root.find('./DownstreamKeys') is not None
    has_cg = root.find('./ColorGenerators') is not None
    has_camera_control = root.find('./CameraControl') is not None
    has_audio = root.find('./FairlightAudioMixer') is not None
    has_aux = root.find('./Auxiliaries') is not None
    has_inputs = root.find('./Settings/Inputs') is not None
    has_counters = root.find('./Counters') is not None
    has_settings = root.find('./Settings') is not None
    has_video_mode = root.find('./VideoMode') is not None
    has_hyperdecks = root.find('./HyperDecks') is not None
    has_macros = root.find('./MacroPool') is not None
    has_media_players = root.find('./MediaPlayers') is not None
    # media_pool_images: at least one <Still> with a path attribute,
    # since ``Profile.apply`` only acts on stills with a usable path.
    has_stills = any(
        s.get('path') for s in root.findall('./MediaPool/Stills/Still'))

    # ---- Switcher / global (siblings of the M/E boxes) ----
    if has_dsk:
        sections.append({
            'id': 'downstream_keys', 'label': 'Downstream Keyers',
            'group': 'switcher_global',
            'supported': True, 'checked': True})
    if has_cg:
        sections.append({
            'id': 'color_generators', 'label': 'Color Generators',
            'group': 'switcher_global',
            'supported': True, 'checked': True})
    if has_camera_control:
        sections.append({
            'id': 'camera_control', 'label': 'Camera Control',
            'group': 'switcher_global',
            # Apply gap — element in XML but the apply path skips it.
            'supported': False, 'checked': False,
            'reason': 'Camera control apply is not yet supported.'})
    if has_audio:
        sections.append({
            'id': 'audio_mixer', 'label': 'Audio Mixer',
            'group': 'switcher_global',
            'supported': True, 'checked': True,
            'reason': ('Compressor/limiter/expander apply still gap; '
                       'master fader, per-strip gain, EQ apply normally.')})

    # ---- Media ----
    # The load side is one Media Pool cell already — the id stays
    # ``media_pool_images`` because the apply mapping
    # (restore_media_pool_images) and the frontend's image-picker
    # prompt (runLoad reads sections.media_pool_images) key off it.
    if has_stills:
        sections.append({
            'id': 'media_pool_images',
            'label': 'Media Pool',
            'group': 'media',
            'supported': True, 'checked': True,
            'reason': ('Uploads the matching image files into the '
                       'media-pool slots — you will be prompted to '
                       'pick them.')})
    if has_media_players:
        sections.append({
            'id': 'media_players', 'label': 'Media Players (1-2)',
            'group': 'media',
            'supported': True, 'checked': True})

    # ---- Input and Output ----
    if has_aux:
        sections.append({
            'id': 'auxiliaries', 'label': 'Outputs',
            'group': 'io',
            'supported': True, 'checked': True})
    if has_inputs:
        sections.append({
            'id': 'inputs', 'label': 'Input renames',
            'group': 'io',
            'supported': True, 'checked': True})
    if has_counters:
        sections.append({
            'id': 'counters', 'label': 'Counter',
            'group': 'io',
            'supported': False, 'checked': False,
            'reason': 'Counter / display clock apply is not yet supported.'})

    # ---- Others ----
    if has_settings:
        sections.append({
            'id': 'settings_flags', 'label': 'Settings',
            'group': 'others',
            'supported': True, 'checked': True,
            'reason': 'FtB enable, etc.'})
    if has_video_mode:
        sections.append({
            'id': 'video_mode', 'label': 'Video Mode',
            'group': 'others',
            'supported': True, 'checked': True,
            'reason': 'Mode change causes a brief output resync.'})
    if has_hyperdecks:
        sections.append({
            'id': 'hyperdecks', 'label': 'HyperDecks',
            'group': 'others',
            'supported': True, 'checked': True,
            'reason': 'Restores deck IP + switcher input per slot.'})
    if has_macros:
        sections.append({
            'id': 'macros', 'label': 'Macros',
            'group': 'others',
            'supported': True, 'checked': True})

    return {
        'product': profile.product,
        'video_mode': profile.video_mode,
        'macros_count': len(root.findall('./MacroPool/Macro')),
        'referenced_images': referenced_images(profile),
        'sections': sections,
        'me_tabs': me_tabs,
    }
