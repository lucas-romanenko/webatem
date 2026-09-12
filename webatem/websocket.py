"""WebSocket origin validation.

Django's CSRF middleware does not cover WebSockets, and neither does CORS:
the ONLY browser-side guard on ws://<host>/ws/atem/ is the Origin header
check. The stock ``AllowedHostsOriginValidator`` keys that check off
``ALLOWED_HOSTS``, which this app defaults to ``*`` (it's a LAN tool) —
and on ``*`` the validator admits every origin. That combination meant a
drive-by page on any website an operator's browser had open could dial
the control socket and cut program.

``SameOriginValidator`` closes this without demanding configuration:

* Same-origin needs no setting. The frontend always dials
  ``window.location.host``, so a legitimate browser connection carries an
  Origin whose host:port equals the request's own Host header — whatever
  hostname or IP the operator happens to browse by.
* ``WEBSOCKET_ALLOWED_ORIGINS`` covers the one legitimate cross-host
  shape: a reverse proxy that rewrites Host on the way through. ``*``
  disables the check entirely, and logs that it did.
* Requests with no Origin header (websocat, scripts, monitoring) are
  allowed. An Origin check only defends against code running inside a
  victim's browser; a standalone client can forge any Origin it likes,
  so denying the missing header breaks debugging while stopping nobody.

Known limit: same-origin compares against the Host header, so DNS
rebinding can line the two up while ``ALLOWED_HOSTS`` is ``*``. Pinning
``ALLOWED_HOSTS`` closes that half too.
"""
import logging
from urllib.parse import urlsplit

from channels.security.websocket import WebsocketDenier
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {'http': 80, 'ws': 80, 'https': 443, 'wss': 443}


def _parse_hostport(netloc, default_port=None):
    """``'host:port'`` -> ``(hostname_lowercased, port)``, or None on garbage.

    Handles IPv6 bracket syntax via urlsplit. ``default_port`` fills in
    when the netloc carries no explicit port.
    """
    try:
        parts = urlsplit('//' + netloc)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    return host, port if port is not None else default_port


def _parse_origin(value):
    """An Origin header value -> ``(hostname, port)``, or None if it isn't
    a well-formed http(s) origin (including the literal ``null`` a browser
    sends for sandboxed/opaque contexts — those stay denied)."""
    try:
        parts = urlsplit(value)
        scheme, host, port = parts.scheme, parts.hostname, parts.port
    except ValueError:
        return None
    if scheme not in ('http', 'https') or not host:
        return None
    return host, port if port is not None else _DEFAULT_PORTS[scheme]


class SameOriginValidator:
    """ASGI middleware: admit same-origin (and explicitly listed) browser
    connections, deny the rest with a proper close frame."""

    def __init__(self, application, extra_origins=()):
        self.application = application
        entries = [e.strip() for e in extra_origins if e and e.strip()]
        self.allow_any = '*' in entries
        if self.allow_any:
            logger.warning(
                "WEBSOCKET_ALLOWED_ORIGINS contains '*': cross-origin WebSocket "
                "protection is OFF, and any web page an operator's browser "
                "visits can drive the switchers. Not for live use."
            )
        self.extra = []
        for entry in entries:
            if entry == '*':
                continue
            parsed = (_parse_origin(entry) if '://' in entry
                      else _parse_hostport(entry, default_port=None))
            if parsed is None:
                raise ImproperlyConfigured(
                    f"WEBSOCKET_ALLOWED_ORIGINS entry {entry!r} is not a "
                    f"hostname[:port] or origin URL"
                )
            self.extra.append(parsed)

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'websocket' and not self.origin_allowed(scope):
            return await WebsocketDenier()(scope, receive, send)
        return await self.application(scope, receive, send)

    def origin_allowed(self, scope):
        headers = dict(scope.get('headers') or [])
        raw_origin = headers.get(b'origin')
        if raw_origin is None:
            return True  # non-browser client; see module docstring
        if self.allow_any:
            return True

        origin = _parse_origin(raw_origin.decode('latin1'))
        if origin is None:
            logger.warning("Denied WebSocket with unparsable Origin %r", raw_origin)
            return False

        # A port-less allow-list entry matches that host on any port.
        for host, port in self.extra:
            if origin[0] == host and port in (None, origin[1]):
                return True

        raw_host = headers.get(b'host')
        if raw_host is None:
            logger.warning("Denied WebSocket with no Host header (Origin %r)", raw_origin)
            return False
        scheme_default = _DEFAULT_PORTS.get(scope.get('scheme'), 80)
        request_host = _parse_hostport(raw_host.decode('latin1'), scheme_default)
        if origin != request_host:
            logger.warning(
                "Denied cross-origin WebSocket: Origin %r does not match host %r "
                "(a legitimate proxy rewrite belongs in WEBSOCKET_ALLOWED_ORIGINS)",
                raw_origin, raw_host,
            )
            return False
        return True
