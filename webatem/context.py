"""Template context: the app title (navbar / drawer brand) and the version
string the header and the launcher window show in mono."""
from django.conf import settings


def app(request):
    return {'app_title': settings.APP_TITLE, 'app_version': _version()}


def _version() -> str:
    try:
        from importlib.metadata import version
        return version('webatem')
    except Exception:  # noqa: BLE001 — a checkout without the package installed
        return 'dev'
