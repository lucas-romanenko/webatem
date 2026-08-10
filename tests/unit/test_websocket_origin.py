"""Origin checking on the control WebSocket (config/websocket.py).

The Origin header is the only browser-side guard on ws://<host>/ws/atem/
(no CSRF, no CORS there). The stock AllowedHostsOriginValidator turned
itself off on the app's ALLOWED_HOSTS='*' default; these tests pin the
replacement: same-origin against the request's own Host header, an
explicit allow-list for proxy Host rewrites, and non-browser clients
(no Origin at all) passing through.
"""
import pytest
from django.core.exceptions import ImproperlyConfigured

from config.websocket import SameOriginValidator


def _scope(origin=None, host='192.168.1.5:8000', scheme='ws'):
    headers = []
    if host is not None:
        headers.append((b'host', host.encode()))
    if origin is not None:
        headers.append((b'origin', origin.encode()))
    return {'type': 'websocket', 'scheme': scheme, 'headers': headers}


def _allowed(scope, extra=()):
    return SameOriginValidator(None, extra_origins=extra).origin_allowed(scope)


# --- same-origin default -------------------------------------------------


def test_same_host_and_port_allowed():
    assert _allowed(_scope(origin='http://192.168.1.5:8000'))


def test_cross_origin_page_denied():
    # The drive-by case: operator browses evil.example, its JS dials our WS.
    assert not _allowed(_scope(origin='https://evil.example'))


def test_same_host_different_port_denied():
    assert not _allowed(_scope(origin='http://192.168.1.5:8001'))


def test_no_origin_header_allowed():
    # websocat / scripts / monitoring: an Origin check only defends against
    # code running in a browser, and standalone clients can forge anything.
    assert _allowed(_scope(origin=None))


def test_null_origin_denied():
    # Sandboxed iframe / opaque contexts send the literal string "null".
    assert not _allowed(_scope(origin='null'))


def test_garbage_origin_denied():
    assert not _allowed(_scope(origin='not a url'))


def test_missing_host_header_denied():
    assert not _allowed(_scope(origin='http://192.168.1.5:8000', host=None))


def test_hostname_comparison_is_case_insensitive():
    assert _allowed(_scope(origin='http://Panel.LOCAL:8000', host='panel.local:8000'))


def test_default_ports_normalized_ws():
    # Origin "http://panel.local" implies :80; a Host with no port on a ws
    # request is also :80.
    assert _allowed(_scope(origin='http://panel.local', host='panel.local'))


def test_default_ports_normalized_wss():
    assert _allowed(_scope(origin='https://panel.local', host='panel.local',
                           scheme='wss'))


def test_default_port_mismatch_denied():
    assert not _allowed(_scope(origin='http://panel.local', host='panel.local:8000'))


def test_ipv6_literal_allowed():
    assert _allowed(_scope(origin='http://[fd00::5]:8000', host='[fd00::5]:8000'))


# --- WEBSOCKET_ALLOWED_ORIGINS allow-list --------------------------------


def test_extra_origin_matches_any_port():
    scope = _scope(origin='https://atem.example.com', host='internal:8000')
    assert _allowed(scope, extra=['atem.example.com'])


def test_extra_origin_with_port_is_exact():
    extra = ['atem.example.com:8443']
    assert _allowed(
        _scope(origin='https://atem.example.com:8443', host='internal:8000'),
        extra=extra)
    assert not _allowed(
        _scope(origin='https://atem.example.com', host='internal:8000'),
        extra=extra)


def test_extra_origin_does_not_admit_others():
    scope = _scope(origin='https://evil.example', host='internal:8000')
    assert not _allowed(scope, extra=['atem.example.com'])


def test_wildcard_disables_the_check():
    assert _allowed(_scope(origin='https://evil.example'), extra=['*'])


def test_unparsable_allowlist_entry_fails_at_startup():
    # A typo'd entry must fail loudly at boot, not silently never match.
    with pytest.raises(ImproperlyConfigured):
        SameOriginValidator(None, extra_origins=['http://'])
