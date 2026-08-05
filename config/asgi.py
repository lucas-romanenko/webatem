"""ASGI entrypoint. HTTP via Django, WebSocket via Channels.

Run with a SINGLE uvicorn worker: the ATEM connection pool, the media-pool
watcher registry, and the in-memory channel layer are all process-local.
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

import atem_control.routing  # noqa: E402

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AllowedHostsOriginValidator(
        URLRouter(atem_control.routing.websocket_urlpatterns)
    ),
})
