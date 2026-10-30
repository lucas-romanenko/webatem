"""Controls for a block the switcher says it lacks are greyed, not hidden.

bmdwire's ``topology`` carries the switcher's DVE, stinger and SuperSource
block counts from its ``_top`` as ``dves``, ``stingers`` and ``superSources``:
an int, or None while unknown. A bmdwire from before them sends no such keys
at all. The page greys a control only on a reported 0 (``lacksBlock``), so an
unknown count, null or absent, leaves the page as it always was:

- the DVE and STING transition style buttons;
- the DVE and Stinger tabs of the transition settings;
- the DVE key type of each upstream keyer.

A greyed control keeps its place and its selected look, takes no clicks
(``opacity-50 pointer-events-none``, like the settings-dependent controls),
and the cell around it carries the tooltip, since a control with no pointer
events shows none. The store refuses the two commands as well.

Nothing here runs the page's JavaScript; these pin the wiring in the sources
and the built bundle.
"""
import pathlib
import re

import pytest

import atem_control

APP = pathlib.Path(atem_control.__file__).parent
STORE = (APP / 'js' / 'store.js').read_text()
BUNDLE = (APP / 'static' / 'js' / 'atem_control.js').read_text()
TEMPLATES = APP / 'templates'
GREY = "$store.atem.greyedClass($store.atem.lacksBlock('{kind}'))"
TITLE = ":title=\"$store.atem.lacksBlockTitle('{kind}')\""


def _template(name):
    return (TEMPLATES / 'control' / name).read_text()


def _topology_literal(source):
    m = re.search(r'\n\s*topology: \{(?P<body>[^{}]*)\}', source)
    assert m, 'the topology literal moved; update this test'
    body = re.sub(r'//[^\n]*', '', m['body'])
    return dict(re.findall(r'(\w+):\s*([^,\s]+)', body))


# --- the store ----------------------------------------------------------------

@pytest.mark.parametrize('source', [STORE, BUNDLE], ids=['store.js', 'bundle'])
def test_the_block_counts_start_unknown(source):
    declared = _topology_literal(source)
    assert {k: declared.get(k) for k in ('dves', 'stingers', 'superSources')} == {
        'dves': 'null', 'stingers': 'null', 'superSources': 'null'}


def test_every_topology_key_bmdwire_builds_is_declared():
    """The installed bmdwire's own topology keys are all declared, so a
    count reads as its declared default until the first snapshot. Works on
    a bmdwire with or without the block counts."""
    from atemwire.state import build_full_state
    from atemwire.testing import FakeAtemProtocol

    fake = FakeAtemProtocol('192.0.2.10')
    fake.connect()
    built = build_full_state(fake)['topology']
    assert set(built) <= set(_topology_literal(STORE))


def test_only_a_reported_zero_lacks_a_block():
    body = re.search(r'lacksBlock\(kind\) \{(?P<body>.*?)\n        \}', STORE, re.S)
    assert body, 'lacksBlock moved; update this test'
    # Strictly 0: null (unknown) and undefined (an older bmdwire) must not grey.
    assert body['body'].strip() == 'return this.state.topology?.[kind] === 0;'


@pytest.mark.parametrize('name', ['lacksBlock', 'lacksBlockTitle', 'transitionStyleMissing',
                                  'transitionStyleTitle', 'setTransitionStyle', 'greyedClass'])
def test_the_bundle_is_built_from_the_store(name):
    assert f'{name}(' in STORE and f'{name}(' in BUNDLE


def test_the_store_refuses_what_the_switcher_lacks():
    set_style = re.search(r'setTransitionStyle\(style\) \{(?P<body>.*?)\n        \}', STORE, re.S)
    assert 'if (this.transitionStyleMissing(style)) return;' in set_style['body']
    set_type = re.search(r'setUSKType\(uskIndex, keyType\) \{(?P<body>.*?)\n        \}', STORE, re.S)
    assert "if (typeValue === 3 && this.lacksBlock('dves')) return;" in set_type['body']
    styles = re.search(r'transitionStyleBlock\(style\) \{(?P<body>.*?)\n        \}', STORE, re.S)
    assert "{ 3: 'dves', 4: 'stingers' }" in styles['body']      # DVE and STING
    assert re.search(r"greyedClass\(missing\) \{\s*return missing \? ' opacity-50 pointer-events-none' : '';",
                     STORE)


# --- the templates ------------------------------------------------------------

def test_the_style_buttons_grey_through_the_store():
    html = _template('_transition_style.html')
    cell = re.search(r'<div :title="\$store\.atem\.transitionStyleTitle\(style\.value\)">\s*<button(?P<button>.*?)>',
                     html, re.S)
    assert cell, 'the transition style cell moved; update this test'
    button = cell['button']
    assert ':class="$store.atem.getTransitionStyleButtonClass(style.value)"' in button
    assert '@click="$store.atem.setTransitionStyle(style.value)"' in button
    assert ':tabindex="$store.atem.transitionStyleMissing(style.value) ? -1 : null"' in button
    # The class helper appends the greying to the selected look, not instead of it.
    cls = re.search(r'getTransitionStyleButtonClass\(styleIndex\) \{(?P<body>.*?)\n        \}', STORE, re.S)
    assert '+ this.greyedClass(this.transitionStyleMissing(styleIndex));' in cls['body']


def test_nothing_sets_a_transition_style_around_the_store():
    for path in TEMPLATES.rglob('*.html'):
        assert "send('set_transition_style'" not in path.read_text(), path.name


@pytest.mark.parametrize('template, label, kind', [
    ('_settings_transition.html', 'DVE', 'dves'),
    ('_settings_transition.html', 'Stinger', 'stingers'),
    ('_settings_usk.html', 'DVE', 'dves'),
])
def test_the_tabs_grey_with_a_tooltip(template, label, kind):
    html = _template(template)
    cells = re.findall(r'<div class="flex-1 flex" (?P<title>:title="[^"]*")>\s*'
                       r'<button(?P<button>(?:(?!</button>).)*?)>\s*'
                       + re.escape(label) + r'\s*</button>\s*</div>', html, re.S)
    assert len(cells) == 1, f'the {label} tab in {template} moved; update this test'
    title, button = cells[0]
    assert title == TITLE.format(kind=kind)
    assert GREY.format(kind=kind) in button
    assert f":tabindex=\"$store.atem.lacksBlock('{kind}') ? -1 : null\"" in button


def test_a_supersource_control_would_grey_too():
    """The page has no SuperSource controls yet; the count is carried for
    when it does. Any template that gains one greys it on the count."""
    for path in TEMPLATES.rglob('*.html'):
        html = path.read_text()
        if re.search(r'super\s*source', html, re.I):
            assert "lacksBlock('superSources')" in html, path.name
