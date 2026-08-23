"""
WebSocket Routing - Clean Version
"""

from django.urls import re_path
from atem_control.control import consumer as atem_consumer

websocket_urlpatterns = [
    # Single WebSocket endpoint handles all ATEM instances.
    # (ws/multiview/ moved to av_server.multiview.routing 2026-08-21.)
    re_path(r'ws/atem/$', atem_consumer.ATEMConsumer.as_asgi()),
]