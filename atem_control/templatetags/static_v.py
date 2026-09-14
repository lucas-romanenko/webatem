"""``{% static_v 'js/atem_control.js' %}`` — content-hashed static URL.

Replaces the manual ``?v=YYYY-MM-DD-X`` cache-bust convention, which relied
on humans remembering to bump a string and demonstrably drifted (stamps were
observed out of sync with file mtimes across the six control-page tags).
Renders the same URL ``{% static %}`` would, plus ``?v=<md5[:10]>`` of the
file's CURRENT content.

The static STORAGE deliberately stays WhiteNoise Compressed (non-Manifest —
see CLAUDE.md KI#21 / the collectstatic notes): filenames never change, so
this query-string hash is the one and only cache-buster.

- Dev (DEBUG finders): hashes the source file; editing a JS/CSS file changes
  the hash on the very next request — no restart, no manual bump (watchmedo
  doesn't watch .js anyway).
- Prod (baked image): sources are immutable, so each file hashes once per
  process; later renders are a dict hit + one os.stat.
- Missing file: falls back to the plain static URL — never breaks a page.
"""
import hashlib
import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()

# path -> (mtime_ns, size, hash). Keyed by absolute path so a re-edited file
# replaces its own entry instead of growing the cache.
_hashes = {}


def _content_hash(absolute_path):
    st = os.stat(absolute_path)
    cached = _hashes.get(absolute_path)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    with open(absolute_path, 'rb') as f:
        digest = hashlib.md5(f.read()).hexdigest()[:10]
    _hashes[absolute_path] = (st.st_mtime_ns, st.st_size, digest)
    return digest


@register.simple_tag
def static_v(path):
    url = static(path)
    absolute = finders.find(path)
    if not absolute:
        return url
    try:
        return f'{url}?v={_content_hash(absolute)}'
    except OSError:
        return url
