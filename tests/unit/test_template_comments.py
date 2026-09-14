"""Django's ``{# #}`` comment is single-line: a ``{#`` whose ``#}`` sits on a
later line is not a comment — the lexer emits the text onto the page. Two of
those shipped in 0.4.1 (the connect page's switcher-list note and the
control page's ``control_extra`` note). Anything longer than one line is a
``{% comment %}`` block.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _multiline_hash_comments():
    hits = []
    for top in ('atem_control', 'webatem'):
        for path in sorted((ROOT / top).rglob('*.html')):
            for lineno, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
                start = line.find('{#')
                if start != -1 and '#}' not in line[start:]:
                    hits.append(f'{path.relative_to(ROOT)}:{lineno}: {line.strip()[:80]}')
    return hits


def test_no_hash_comment_spans_lines():
    assert _multiline_hash_comments() == []
