"""ASGI entrypoint. HTTP via Django, WebSocket via Channels.

Run with a SINGLE uvicorn worker: the ATEM connection pool, the media-pool
watcher registry, and the in-memory channel layer are all process-local.
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'webatem.settings')
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from django.conf import settings  # noqa: E402

import atem_control.routing  # noqa: E402
from webatem.websocket import SameOriginValidator  # noqa: E402

# Not AllowedHostsOriginValidator: that reads ALLOWED_HOSTS, whose '*'
# default would admit every Origin — a drive-by page in an operator's
# browser could cut program. See config/websocket.py.
application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': SameOriginValidator(
        URLRouter(atem_control.routing.websocket_urlpatterns),
        extra_origins=settings.WEBSOCKET_ALLOWED_ORIGINS,
    ),
})
