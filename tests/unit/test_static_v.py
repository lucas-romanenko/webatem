"""{% static_v %}: the content-hash cache-buster on every control-page
script and stylesheet: the URL, its content hash, the fallback for a
missing file, and a cache that replaces an entry instead of growing."""
import re

from django.template import Context, Template
from django.test import override_settings

from atem_control.templatetags import static_v as mod


def render(path):
    return Template("{% load static_v %}{% static_v '" + path + "' %}").render(Context())


def test_appends_content_hash_for_real_file():
    # A real static of this app. The URL keeps the static prefix and gains
    # a 10-hex content hash.
    out = render('js/atem_control.js')
    assert re.search(r'/js/atem_control\.js\?v=[0-9a-f]{10}$', out), out


def test_stable_across_calls():
    assert render('js/atem_control.js') == render('js/atem_control.js')


def test_missing_file_falls_back_to_plain_url():
    out = render('js/does-not-exist-anywhere.js')
    assert out.endswith('/js/does-not-exist-anywhere.js')
    assert '?v=' not in out


def test_hash_changes_when_content_changes(tmp_path):
    f = tmp_path / 'thing.js'
    f.write_text('console.log(1)\n')
    with override_settings(STATICFILES_DIRS=[tmp_path]):
        first = render('thing.js')
        assert '?v=' in first
        f.write_text('console.log(2)  // changed\n')
        second = render('thing.js')
    assert first != second
    assert first.split('?v=')[0] == second.split('?v=')[0]


def test_cache_entry_replaced_not_grown(tmp_path):
    f = tmp_path / 'grow.js'
    f.write_text('a')
    with override_settings(STATICFILES_DIRS=[tmp_path]):
        render('grow.js')
        before = len(mod._hashes)
        f.write_text('bb')
        render('grow.js')
        assert len(mod._hashes) == before  # same key re-used, no growth
