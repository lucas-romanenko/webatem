"""
ATEM Connection Logging - Tracks connect/disconnect events with session duration.

Mixin for ATEMConsumer that handles all database logging of connection events,
user extraction from WebSocket scope, and duration formatting.
"""

import logging
import asyncio
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)


def _format_duration(duration_seconds):
    """Format seconds into a human-readable string like '2m 30s'."""
    minutes, seconds = divmod(int(duration_seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


class ATEMConnectionLoggingMixin:
    """
    Mixin providing connection/disconnection event logging for the ATEM consumer.

    Expects the consumer to have:
        - self.scope (WebSocket scope with 'user')
        - self.connected_ip (str or None)
        - self.connect_time (float or None)
    """

    async def _log_connect(self, ip_address):
        """Log connection event (only if not already logged for this IP)."""
        if self.connected_ip == ip_address:
            return

        # If switching ATEMs, log disconnect from previous
        if self.connected_ip:
            await self._log_disconnect(reason='switched_atem')

        await self._write_connection_log('connection', ip_address)
        self.connected_ip = ip_address
        self.connect_time = asyncio.get_event_loop().time()

    async def _log_disconnect(self, reason='user_requested'):
        """Log disconnection event (only if we have a logged connection)."""
        if not self.connected_ip:
            return

        duration_seconds = None
        if self.connect_time:
            duration_seconds = asyncio.get_event_loop().time() - self.connect_time

        await self._write_connection_log(
            'disconnection', self.connected_ip,
            reason=reason, duration_seconds=duration_seconds
        )
        self.connected_ip = None
        self.connect_time = None

    async def _write_connection_log(self, event_type, ip_address, reason=None, duration_seconds=None):
        """Write connection/disconnection event to database."""
        try:
            from atem_control.models import ATEMControlLog

            log_data = {
                'ip_address': ip_address,
            }

            if reason:
                log_data['disconnect_reason'] = reason

            if duration_seconds is not None:
                log_data['duration_seconds'] = round(duration_seconds, 1)
                log_data['duration_formatted'] = _format_duration(duration_seconds)

            await sync_to_async(ATEMControlLog.objects.create)(
                type=event_type,
                data=log_data
            )

            # Mirror into the centralized activity log (2026-07-07). The
            # ATEMControlLog stays the canonical connect/disconnect table
            # (the Connect page "Recent ATEMs" reads it); this makes the
            # same events show in the cross-app admin activity view.
            from atem_control.activity import ActivityLog
            from atem_control.activity import arecord_activity
            eq_name = ''
            if event_type == 'connection':
                action = 'connect'
                summary = f"Connected to {eq_name or ip_address}"
            else:
                action = 'disconnect'
                dur = log_data.get('duration_formatted')
                summary = (f"Disconnected from {eq_name or ip_address}"
                           + (f" (session {dur})" if dur else ""))
            await arecord_activity(
                feature=ActivityLog.FEATURE_ATEM_CONTROL, device=ActivityLog.DEVICE_ATEM,
                action=action,
                target=ip_address, target_name=eq_name,
                summary=summary, reason=reason,
                duration_seconds=log_data.get('duration_seconds'),
            )

            duration_info = f" (duration: {log_data.get('duration_formatted', 'N/A')})" if duration_seconds else ""
            reason_info = f" (reason: {reason})" if reason else ""
            logger.info(f"Logged {event_type} to {ip_address}{reason_info}{duration_info}")

        except asyncio.CancelledError:
            logger.debug(f"Logging {event_type} cancelled (WebSocket closed)")
            raise
        except Exception as e:
            logger.error(f"Error logging {event_type} event: {e}", exc_info=True)

