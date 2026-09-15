"""``control`` sub-package — ATEM control WebSocket consumer + dispatch.

Sub-modules:
    consumer.py — ``ATEMConsumer`` (Channels WebSocket consumer)
    commands.py — WebSocket-verb → op dispatch table (``ops`` built by walking
                  the per-feature ``atemwire.messages.<feature>`` modules)
    logging.py  — ``ATEMConnectionLoggingMixin`` (connect/disconnect audit trail)
"""
