"""The seam between the ATEM control app and whatever hosts it.

Standalone WebATEM needs no permissions, no audit table, no inventory: the
switchers are whatever discovery found, the connection log is the app's own
small table, a drag-drop upload runs in this process. A larger platform
hosting this app has all of those — roles, an audit log, an equipment
database with names and sightings, an upload queue — and supplies them by
subclassing ``Hooks`` and pointing ``settings.WEBATEM_HOOKS`` at the class.

Every method has a working default, so a host overrides only what it has.
Nothing in this app reaches past this class for those concerns: views call
``hooks.get()``, the consumer gates on ``can_use``, the audit facade
(``atem_control.activity``) and the sightings facade
(``atem_control.sightings``) forward here.

    # settings.py of the host
    WEBATEM_HOOKS = 'myplatform.webatem_hooks.PlatformHooks'
"""
import logging
from functools import wraps

from django.conf import settings
from django.utils.module_loading import import_string

_log = logging.getLogger('atem_control.activity')
_instance = None


def get():
    """The configured Hooks instance (one per process)."""
    global _instance
    if _instance is None:
        path = getattr(settings, 'WEBATEM_HOOKS', None) or 'atem_control.hooks.Hooks'
        _instance = import_string(path)()
    return _instance


def reset():
    """Forget the instance (tests that switch WEBATEM_HOOKS)."""
    global _instance
    _instance = None


def access_required(view):
    """View decorator: ``check_access`` may answer instead of the view."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        denied = get().check_access(request)
        if denied is not None:
            return denied
        return view(request, *args, **kwargs)
    return wrapped


class Hooks:
    """Override what the host has; the defaults are standalone WebATEM."""

    # ---- access -----------------------------------------------------------
    def check_access(self, request):
        """Return an HttpResponse to refuse a page or API call (a redirect to
        a login page, a 403), or None to let it through."""
        return None

    def can_use(self, user) -> bool:
        """May this user drive a switcher over the WebSocket? ``user`` is
        ``scope['user']`` when the host's ASGI stack authenticates, else None."""
        return True

    # ---- audit ------------------------------------------------------------
    def record_activity(self, *, feature='', device='', action='', user=None,
                        target='', target_name='', summary='', **extra):
        """One operator action (a command, a profile load, a name change).
        Default: a line in the application log."""
        username = getattr(user, 'username', None) or str(user or '-')
        _log.info("activity user=%s action=%s target=%s: %s",
                  username, action, target_name or target, summary)

    async def arecord_activity(self, **kwargs):
        self.record_activity(**kwargs)

    def record_connection(self, *, user, event_type, ip, data):
        """A connect / disconnect (``event_type``) to ``ip``; ``data`` carries
        the reason and duration. Default: the app's own ATEMControlLog."""
        from atem_control.models import ATEMControlLog
        if user is not None and not getattr(user, 'is_authenticated', False):
            user = None
        ATEMControlLog.objects.create(type=event_type, data=data, user=user)

    def recent_atems(self, request, limit=5):
        """[{ip_address, equipment_name, last_connected}] for the Connect
        page's quick list. Default: the app's own log, everyone's sessions."""
        from atem_control.models import get_recent_atems
        return get_recent_atems(limit=limit)

    # ---- names and inventory ---------------------------------------------
    def name_for_ip(self, ip):
        """The switcher's friendly name for an address, or None. Default:
        what mDNS discovery has seen."""
        try:
            from atem_control import discovery
            name, _model = discovery._name_for_ip(ip)
            return name or None
        except Exception:  # noqa: BLE001
            return None

    def switchers(self):
        """[{name, ip, location}] for the name-or-IP suggestion dropdowns.
        Default: none (discovery fills the Connect page live instead)."""
        return []

    def hyperdeck_names(self):
        """{deck_ip: name} so Settings > HyperDecks can label a bound slot."""
        return {}

    def discovery_enabled(self) -> bool:
        """Show the On Your Network section and serve the discovery API."""
        return True

    def server_settings_enabled(self) -> bool:
        """Show the Connect page's gear (the listen address / start-at-login
        dialog): a launcher thing, not a hosting platform's."""
        return True

    # ---- sightings ------------------------------------------------------
    def record_video_mode(self, ip, label):
        """The switcher at ``ip`` reported video mode ``label``."""
        return None

    def record_deck_model(self, ip, model):
        """The HyperDeck at ``ip`` reported its model."""
        return None

    def hyperdeck_binding_diff(self, connection, ip):
        """After connecting: compare the switcher's HyperDeck bindings with
        what the host knows. Return {'orphans': [...]} to have the page list
        decks bound on the switcher that the host does not know, or None."""
        return None

    # ---- uploads ----------------------------------------------------------
    def validate_still(self, ip, file_path, file_name):
        """Check (and, if the host allows, resize in place) a still dropped
        on a media-pool slot of ``ip``. Return (ok, error). Default: exactly
        1920x1080, or a larger 16:9 image resized down to it."""
        from atem_control.media_pool import views as media_pool_views
        return media_pool_views.validate_and_resize_1080p(file_path, file_name)

    def upload_still(self, ip, slot, file_path, job_dir, user=None):
        """A validated 1920x1080 file dropped on media-pool slot ``slot`` of
        ``ip``. Must return (ok, error). The caller keeps nothing: delete
        ``job_dir`` when done. Default: upload from this process."""
        from atem_control.media_pool import views as media_pool_views
        media_pool_views.start_in_process_upload(ip, slot, file_path, job_dir)
        return True, None

    # ---- presentation ---------------------------------------------------
    def time_format_24h(self) -> bool:
        return True

    def template_context(self, request) -> dict:
        """Extra context for the Connect and control pages."""
        return {}
