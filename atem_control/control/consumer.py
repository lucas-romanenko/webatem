"""
ATEM Consumer - WebSocket handler for ATEM switcher control.

Thin WebSocket layer: lifecycle, message routing, state monitoring.
Holds a pooled ATEMConnection; builds state via ATEMStateMixin;
dispatches frontend commands via commands.py.
"""

import json
import logging
import asyncio
import os
import threading
import time
from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async
from atem_control.media_pool import watcher as media_pool_service
from pyatem._state import ATEMStateMixin
from pyatem.messages.fairlight import enable_fairlight_levels
from pyatem.pool import ATEMInstanceManager
from atem_control.control.commands import dispatch as dispatch_command
from atem_control.control.logging import ATEMConnectionLoggingMixin
from atem_control.activity import ActivityLog
from atem_control.activity import arecord_activity
from atem_control.netutil import is_valid_ip

logger = logging.getLogger(__name__)


# Per-IP audio meter subscriber count. First subscriber sends SFLN(enable=True)
# to the ATEM (start streaming FMLv/FDLv); last unsubscriber sends
# SFLN(enable=False) (stop streaming). All consumers run in the same asyncio
# event loop (single ASGI worker, by design — see README), so dict ops are
# race-free.
_meter_subscribers: dict[str, int] = {}

# Live operator control sessions per ATEM IP — the username of each connected
# control page (one list entry per open session; the same user in two tabs
# appears twice). The pool ref-count ALSO counts the media-pool watcher +
# captures, so it can't tell us WHO is on a switcher — this can. Touched from
# executor threads (sync_to_async acquire/release) AND read from the request
# thread, so it needs a lock (unlike _meter_subscribers, event-loop only).
_control_sessions_lock = threading.Lock()
_control_sessions: dict[str, list] = {}


def active_control_sessions() -> dict:
    """Snapshot ``{ip: [usernames]}`` — one entry per live control session."""
    with _control_sessions_lock:
        return {ip: list(users) for ip, users in _control_sessions.items() if users}


class ATEMConsumer(ATEMConnectionLoggingMixin, ATEMStateMixin, AsyncWebsocketConsumer):
    """
    WebSocket consumer for ATEM switcher control.

    Features:
        - Dynamic command mapping to PyATEMMax methods
        - Adaptive polling with smart interval adjustment
        - Connection/disconnection logging with session duration
        - Auto-disconnect after inactivity
    """

    # Auto-disconnect after this many seconds without a user command
    INACTIVITY_TIMEOUT = 300  # 5 minutes

    POLLING_INTERVALS = {
        'ultra_fast': 0.03,  # During active transitions
        'fast': 0.08,        # Recent user activity
        'normal': 0.2,       # Default polling
        'slow': 0.4          # Extended inactivity
    }

    # Thresholds for adaptive polling interval selection
    RECENT_COMMAND_WINDOW = 2.0     # Seconds after a command to use fast polling
    RECENT_CHANGE_WINDOW = 6.0     # Seconds after a state change to use fast polling
    IDLE_POLL_THRESHOLD = 10       # Consecutive unchanged polls before switching to slow

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Pooled ATEM connection. current_ip serves double duty as the
        # "IP we hold a pool ref to" — release exactly when we clear it.
        # _pool_entry is the pool's entry dict for identity-guarded release
        # (fixed 2026-07-06).
        self.connection = None
        self.current_ip: str | None = None
        self.current_atem_name: str | None = None
        self._pool_entry = None

        # Activity-log slider debounce: a drag fires ~20 commands/second; we
        # coalesce each control's stream into ONE settled row.
        self._slider_pending: dict = {}
        self._slider_tasks: dict = {}

        # Logging-mixin state (connection/disconnection audit trail)
        self.connected_ip = None
        self.connect_time = None

        # Monitoring state
        self.monitoring_task = None
        self.is_monitoring = False
        self.last_known_state = {}

        # Activity tracking
        self.last_command_time = 0
        self.last_change_time = 0

        # Media pool subscription — tracks which IP we've joined, so disconnect
        # can both leave the group and release the watcher refcount exactly once.
        self._mediapool_ip: str | None = None

        # Audio meter subscription — Fairlight meter levels (FMLv per strip,
        # FDLv master) arrive ~25 Hz per channel from the ATEM. We coalesce
        # all per-tick events into a single ``audio_meter_batch`` message
        # at ~25 Hz total so a 14-strip mixer pushes ~25 msg/s to the
        # WebSocket instead of ~350 (14 × 25). Smaller WS message count
        # keeps the per-command atem_state pushes from queueing behind
        # the meter spam, which kept the live meter readout (top-of-strip
        # peak dB) seconds behind the operator's fader during interaction.
        # Tab-gated: the frontend sends ``subscribe_audio_meters`` when the
        # audio panel opens and ``unsubscribe_audio_meters`` when it closes
        # (or when the browser tab is hidden). Module-level _meter_subscribers
        # tracks per-IP global subscriber count so SFLN(enable=True/False)
        # is sent to the ATEM exactly on the 0↔1 transitions.
        self._audio_subscribed = False
        self._meter_handler_ids: list = []
        # The AtemProtocol object the meter handlers were registered on —
        # a pooled re-handshake builds a NEW protocol (and a NEW ATEM
        # session that never got SFLN), so identity change means the
        # meters must re-arm.
        self._meter_protocol = None
        self._meter_loop = None
        self._meter_batch_strips: dict = {}  # strip_id → latest payload this tick
        self._meter_batch_master: dict | None = None
        self._meter_batch_pending = False  # is a flush task scheduled?

    # =========================================================================
    # Pool lifecycle (acquire / release the shared ATEMConnection)
    # =========================================================================

    def _acquire_pool_ref(self, ip_address: str):
        """Acquire a pool ref for ``ip_address`` and stash the ATEMConnection
        on the consumer. Does NOT open the underlying socket — call
        ``self.connection.connect(ip)`` for that."""
        instance = ATEMInstanceManager.get_instance(ip_address)
        self._pool_entry = instance
        self.connection = instance['connection']
        self.current_ip = ip_address
        user = self.scope.get('user') if getattr(self, 'scope', None) else None
        self._session_user = getattr(user, 'username', None) or 'unknown'
        with _control_sessions_lock:
            _control_sessions.setdefault(ip_address, []).append(self._session_user)

    def _release_pool_ref(self):
        """Release our pool ref (if any). Safe to call repeatedly."""
        ip = self.current_ip
        entry = self._pool_entry
        user = getattr(self, '_session_user', None)
        self.current_ip = None
        self.connection = None
        self._pool_entry = None
        self._session_user = None
        if ip:
            with _control_sessions_lock:
                users = _control_sessions.get(ip)
                if users:
                    try:
                        users.remove(user)
                    except ValueError:
                        users.pop()   # username drifted — drop one anyway
                    if not users:
                        _control_sessions.pop(ip, None)
            # Fix (2026-07-06): identity-guarded release — if our entry
            # was evicted (worker death) and another holder created a fresh
            # one, releasing by IP alone would steal the replacement's ref
            # and tear a live operator's session down 2s later.
            ATEMInstanceManager.release_instance(ip, instance=entry)

    @property
    def is_connected(self) -> bool:
        return bool(self.connection and self.connection.is_connected)

    # =========================================================================
    # WebSocket Lifecycle
    # =========================================================================

    async def connect(self):
        """Accept WebSocket connection and notify client."""
        await self.accept()
        await self.send_json({'type': 'websocket_ready'})

    async def disconnect(self, close_code):
        """Clean up on WebSocket close."""
        await self._cleanup_session(reason='websocket_closed')

    async def _cleanup_session(self, reason='websocket_closed'):
        """Release everything a live session holds — audit log, media-pool
        watcher ref, audio-meter refcount, monitor task, pool ref.

        Fix (2026-07-06): shared by ``disconnect()``,
        ``_perform_disconnect`` and the monitor loop's crash path so every
        exit route gets the same per-step exception isolation — one failing
        step must never skip the pool-ref release."""
        try:
            await self._log_disconnect(reason=reason)
        except asyncio.CancelledError:
            logger.debug("Disconnect logging cancelled (expected during page transitions)")
            self.connected_ip = None
            self.connect_time = None
        except Exception as e:
            logger.error(f"Error during disconnect logging: {e}", exc_info=True)
            self.connected_ip = None
            self.connect_time = None

        try:
            # Emit any slider whose drag never settled before the socket
            # closed, so the operator's last adjustment is still logged.
            await self._flush_pending_sliders()
        except Exception as e:
            logger.error(f"Error flushing pending slider logs: {e}", exc_info=True)

        try:
            await self._unsubscribe_media_pool()
        except Exception as e:
            logger.error(f"Error during media pool unsubscribe: {e}", exc_info=True)

        try:
            # Abrupt close (tab killed, network drop) must release the
            # per-IP audio-meter refcount too, or the ATEM keeps streaming
            # FMLv/FDLv for a subscriber that no longer exists.
            self._unsubscribe_audio_meters()
        except Exception as e:
            logger.error(f"Error during audio meter unsubscribe: {e}", exc_info=True)

        try:
            await self.stop_monitoring()
        except Exception as e:
            logger.error(f"Error stopping monitoring: {e}", exc_info=True)

        try:
            await sync_to_async(self._release_pool_ref, thread_sensitive=False)()
        except Exception as e:
            logger.error(f"Error releasing pool ref: {e}", exc_info=True)

    async def receive(self, text_data):
        """Handle incoming WebSocket messages."""
        try:
            data = json.loads(text_data)
            command = data.get('command')

            # Reset inactivity timer for control commands
            if command and command not in ['connect', 'disconnect', 'refresh']:
                self.last_command_time = asyncio.get_event_loop().time()

            if command == 'connect':
                await self.handle_connect(data, is_test_connection=data.get('test_connection', False))
            elif command == 'disconnect':
                await self.handle_disconnect()
            elif command == 'refresh':
                await self.send_current_state()
            elif command == 'media_pool_refresh':
                await self.send_media_pool_snapshot()
            elif command == 'media_pool_delete':
                await self.handle_media_pool_delete(data)
            elif command == 'subscribe_audio_meters':
                self._subscribe_audio_meters()
            elif command == 'unsubscribe_audio_meters':
                self._unsubscribe_audio_meters()
            else:
                await self.execute_command(command, data)

        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)
            # Fix (2026-07-06): the error re-send must not raise on a
            # dead socket — an escaping send kills the consumer's app task
            # before websocket.disconnect is dispatched, leaking the pool
            # ref / watcher ref / meter refcount until process restart.
            await self._safe_send_json({'type': 'error', 'message': str(e)})

    # =========================================================================
    # Command Execution
    # =========================================================================

    async def execute_command(self, command, data):
        """Dispatch a frontend command through the pyatem-backed command map.

        Each handler builds a pyatem ``Command`` and enqueues it on the
        ATEMConnection's worker thread (non-blocking). State echo is
        delivered by the polling loop, which is kept on a fast cadence
        (~80 ms) for RECENT_COMMAND_WINDOW seconds after each command —
        plenty fast for operator-visible feedback. Pushing the full
        atem_state snapshot here on every command would double the
        message count during high-frequency interactions like fader
        drags (20 Hz × 30 KB = ~600 KB/s purely from per-command pushes,
        on top of the polling loop). That saturates the WebSocket write
        buffer and queues audio meter / other small messages behind it.
        """
        if not self.is_connected:
            await self.send_json({'type': 'error', 'message': 'ATEM not connected'})
            return

        # Handler runs synchronously (it only enqueues — no network I/O on
        # this thread), but wrap in sync_to_async to stay consistent with
        # the async consumer model and in case handlers grow to read state.
        # KI#13: dispatch is pure pyatem (no ORM) — off the shared
        # thread-sensitive lane so one slow call elsewhere can't queue
        # every operator's commands behind it.
        ok, err = await sync_to_async(dispatch_command, thread_sensitive=False)(self.connection, command, data)

        if not ok:
            logger.warning(f"Command {command} failed: {err}")
            await self.send_json({'type': 'error', 'message': err or f'Failed to execute {command}'})
            return

        # Activity log: discrete actions immediately, slider drags coalesced
        # into one settled row (see _record_atem_command).
        self._record_atem_command(command, data)

        if command == 'cut':
            await self.send_json({
                'type': 'tbar_cut',
                'timestamp': asyncio.get_event_loop().time(),
            })

    # ---- Activity logging of control commands ----

    SLIDER_DEBOUNCE_SECONDS = 1.2

    def _record_atem_command(self, command, data):
        """Route a successful control command to the activity log. Discrete
        actions log now; continuous (slider) commands debounce per control
        so a drag becomes ONE 'settled' row instead of ~20/second."""
        try:
            from atem_control.control import activity as ctl_activity
            if ctl_activity.is_continuous(command):
                self._debounce_slider(ctl_activity, command, dict(data or {}))
            else:
                self._log_atem_command(ctl_activity, command, dict(data or {}))
        except Exception:
            logger.exception("activity log of command %s failed", command)

    def _debounce_slider(self, ctl_activity, command, data):
        key = ctl_activity.debounce_key(command, data)
        self._slider_pending[key] = (command, data)
        old = self._slider_tasks.get(key)
        if old is not None and not old.done():
            old.cancel()
        self._slider_tasks[key] = asyncio.create_task(
            self._flush_slider_after(key))

    async def _flush_slider_after(self, key):
        try:
            await asyncio.sleep(self.SLIDER_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        pending = self._slider_pending.pop(key, None)
        self._slider_tasks.pop(key, None)
        if pending is None:
            return
        command, data = pending
        from atem_control.control import activity as ctl_activity
        self._log_atem_command(ctl_activity, command, data, settled=True)

    def _log_atem_command(self, ctl_activity, command, data, settled=False):
        mixerstate = None
        try:
            mixerstate = self.connection.mixerstate if self.connection else None
        except Exception:
            pass
        summary = ctl_activity.summarize(command, data, mixerstate)
        asyncio.create_task(arecord_activity(
            feature=ActivityLog.FEATURE_ATEM_CONTROL,
            device=ActivityLog.DEVICE_ATEM,
            action=command,
            target=self.current_ip or '',
            target_name=self.current_atem_name or '',
            summary=summary,
            args={k: v for k, v in (data or {}).items()
                  if isinstance(v, (str, int, float, bool))},
        ))

    @staticmethod
    def _lookup_equipment_name(ip_address):
        # No name database in this build — sessions are labeled by IP.
        return None

    # (build_full_state itself carries atem_name since the WhoI reader was
    # upstreamed — the old _state_with_name wrapper is gone.)

    async def _flush_pending_sliders(self):
        """On disconnect, emit any slider whose drag never settled so the
        operator's last adjustment isn't lost."""
        for key in list(self._slider_tasks):
            task = self._slider_tasks.pop(key, None)
            if task is not None and not task.done():
                task.cancel()
        for key in list(self._slider_pending):
            pending = self._slider_pending.pop(key, None)
            if pending is None:
                continue
            command, data = pending
            from atem_control.control import activity as ctl_activity
            self._log_atem_command(ctl_activity, command, data, settled=True)

    # =========================================================================
    # Connection Management
    # =========================================================================

    async def handle_connect(self, data, is_test_connection=False):
        """Establish connection to ATEM switcher."""
        ip_address = data.get('ip_address')
        if not ip_address:
            await self.send_json({
                'type': 'connection_status',
                'connected': False,
                'message': 'No ATEM IP address provided.',
            })
            return

        # A WebSocket-supplied IP goes straight to the pyatem UDP socket; a
        # hostname would be DNS-resolved and a bad value scanned (audit SEC-6).
        if not is_valid_ip(ip_address):
            await self.send_json({
                'type': 'connection_status',
                'connected': False,
                'message': 'Invalid ATEM IP address.',
            })
            return

        # Clean up existing connection
        if self.current_ip:
            await self.stop_monitoring()
            # Fix (2026-07-06): the in-page ATEM switch must drop the
            # audio-meter subscription against the OLD connection before the
            # pool ref goes away — _unsubscribe_audio_meters keys off
            # self.current_ip / self.connection, which _release_pool_ref
            # clears. Skipping it poisoned the old IP's _meter_subscribers
            # refcount (SFLN never re-armed for anyone) and left
            # _audio_subscribed True so the new ATEM's subscribe no-oped.
            self._unsubscribe_audio_meters()
            await sync_to_async(self._release_pool_ref, thread_sensitive=False)()

        await sync_to_async(self._acquire_pool_ref, thread_sensitive=False)(ip_address)

        success = False
        for attempt in range(3):
            logger.info(f"Consumer: Connection attempt {attempt + 1}/3 to {ip_address}")

            try:
                # thread_sensitive=False: connect() blocks up to CONNECT_TIMEOUT
                # (6s) per attempt. Channels consumers share ONE process-wide
                # thread-sensitive executor, so on the default a slow/dead-IP
                # connect stalls every operator's commands and state polls on
                # every ATEM for the duration. connect() is thread-safe (own
                # _lifecycle_lock, no ORM/thread-local state).
                success = await sync_to_async(
                    self.connection.connect, thread_sensitive=False
                )(ip_address)
            except Exception as e:
                logger.error(f"Consumer: Attempt {attempt + 1} exception: {e}", exc_info=True)
                success = False

            if success:
                break

            if attempt < 2:
                logger.info(f"Consumer: Attempt {attempt + 1} failed, waiting 1s before retry")
                await asyncio.sleep(1)

        if success:
            if not is_test_connection:
                await self._log_connect(ip_address)

            # Cache the ATEM's friendly name for activity-log rows (one
            # lookup per connect, not per command).
            self.current_atem_name = await sync_to_async(
                self._lookup_equipment_name)(ip_address)

            self.last_command_time = asyncio.get_event_loop().time()

            await self.send_json({
                'type': 'connection_status',
                'connected': True,
                'message': f'Connected to ATEM at {ip_address}'
            })

            await self._subscribe_media_pool(ip_address)
            # NOTE: audio meter subscription is now tab-gated. The frontend
            # sends `subscribe_audio_meters` when the audio panel opens (and
            # the browser tab is visible) and `unsubscribe_audio_meters` when
            # it closes. Auto-subscribing on connect (the original behavior)
            # caused ~375 msg/s sustained WebSocket spam regardless of which
            # tab the operator was on, which saturated the WS write buffer
            # and produced a 5–30 s atem_state lag.

            await asyncio.sleep(1.0)
            await self.start_monitoring()

        else:
            await self.send_json({
                'type': 'connection_status',
                'connected': False,
                'message': f'Failed to connect to ATEM at {ip_address}'
            })

    async def handle_disconnect(self):
        """Handle user-initiated disconnection."""
        await self._perform_disconnect(reason='user_requested')

    async def _auto_disconnect_inactive(self):
        """Handle automatic disconnection due to inactivity timeout."""
        await self._perform_disconnect(reason='inactivity_timeout')

    async def _perform_disconnect(self, reason='user_requested'):
        """Execute disconnection with logging and cleanup.

        Fix (2026-07-06): runs the shared per-step-guarded cleanup —
        the old bare sequence let one raise (e.g. the watcher release)
        skip the pool-ref release on the inactivity path."""
        await self._cleanup_session(reason=reason)

        message = 'Disconnected from ATEM'
        if reason == 'inactivity_timeout':
            message = 'Auto-disconnected due to inactivity (5 minutes)'

        await self._safe_send_json({
            'type': 'connection_status',
            'connected': False,
            'message': message,
            'auto_disconnected': reason == 'inactivity_timeout',
            'redirect_to_connect': reason == 'inactivity_timeout'
        })

    # =========================================================================
    # State Monitoring
    # =========================================================================

    async def start_monitoring(self):
        """Start adaptive state polling."""
        if self.is_monitoring:
            return

        self.is_monitoring = True
        self.last_change_time = asyncio.get_event_loop().time()
        self.monitoring_task = asyncio.create_task(self._monitor_loop())

    async def stop_monitoring(self):
        """Stop state polling."""
        self.is_monitoring = False
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass

    async def _monitor_loop(self):
        """
        Adaptive polling loop with smart interval adjustment.

        Polls faster during transitions or recent activity, slower when idle.
        Also monitors for inactivity timeout to auto-disconnect.

        State-lag diagnostic: when ``ATEM_STATE_LAG_TRACE`` is set in the
        environment, each iteration logs the time spent in
        ``build_full_state`` vs. ``send_json``. The send_json side is the
        Channels write — if those times are big, the bottleneck is
        ``InMemoryChannelLayer`` backpressure or single-worker
        saturation. If build_full_state is big, the bottleneck is the
        sync_to_async hop or mixerstate copy.
        """
        trace = os.environ.get('ATEM_STATE_LAG_TRACE', '')
        try:
            consecutive_no_changes = 0
            iteration = 0
            # ``was_atem_reachable`` tracks the upstream ATEM's reachability
            # — separate from ``self.is_connected`` (which reflects whether
            # the pyatem worker thread is alive; the worker keeps retrying
            # SYN handshakes after a cable yank, so ``is_connected`` stays
            # True for a while even with no live ATEM). The real signal is
            # ``build_full_state`` returning ``is_connected: False``, which
            # fires when pyatem's protocol layer cleared mixerstate after
            # giving up on the failed reconnects.
            #
            # Starts at ``None`` (unknown). The first observation seeds it
            # without firing a notification — otherwise the first poll
            # after a fresh connect (when mixerstate may briefly be empty
            # before the protocol layer finishes populating it) would
            # falsely flag the ATEM as unreachable and flash the
            # disconnect banner on the operator surface.
            was_atem_reachable = None

            while self.is_monitoring:
                if not self.is_connected:
                    await asyncio.sleep(0.5)
                    continue

                # Check for inactivity timeout
                current_time = asyncio.get_event_loop().time()
                if self.last_command_time > 0:
                    inactive_duration = current_time - self.last_command_time
                    if inactive_duration >= self.INACTIVITY_TIMEOUT:
                        logger.info(f"Auto-disconnecting due to {inactive_duration:.1f}s of inactivity")
                        await self._auto_disconnect_inactive()
                        break

                # Meters must survive a pooled in-place reconnect
                # (new protocol object + new ATEM session) — cheap
                # identity check, no-op when nothing changed.
                self._rearm_audio_meters_if_reconnected()

                # Get current state
                t0 = time.monotonic()
                current_state = await sync_to_async(self.build_full_state, thread_sensitive=False)(self.connection)
                t_build = time.monotonic() - t0

                # ATEM-side reachability transition: pyatem's protocol clears
                # mixerstate when it gives up on reconnects, which makes
                # build_full_state return ``{'is_connected': False}``.
                # Treat that as "ATEM dropped mid-session" and notify the
                # frontend so the disconnect banner
                # appears. Without this, the worker keeps retrying SYN
                # forever and the operator surface looks normal indefinitely.
                atem_reachable = current_state.get('is_connected', False)
                if was_atem_reachable is None:
                    # First observation. Seed the tracker, no notification —
                    # the initial connect's connection_status message is
                    # the canonical "we're up" signal; sending another here
                    # would just create a flash.
                    if atem_reachable:
                        was_atem_reachable = True
                    # else: keep waiting; mixerstate may populate next poll.
                else:
                    if was_atem_reachable and not atem_reachable:
                        logger.info("ATEM dropped mid-session; notifying frontend")
                        try:
                            await self.send_json({
                                'type': 'connection_status',
                                'connected': False,
                                'message': 'ATEM connection lost',
                            })
                        except Exception as e:  # noqa: BLE001
                            logger.warning(f"failed to send disconnect notice: {e}")
                    elif not was_atem_reachable and atem_reachable:
                        logger.info("ATEM reachable again; notifying frontend")
                        try:
                            await self.send_json({
                                'type': 'connection_status',
                                'connected': True,
                                'message': 'ATEM connection restored',
                            })
                        except Exception as e:  # noqa: BLE001
                            logger.warning(f"failed to send reconnect notice: {e}")
                    was_atem_reachable = atem_reachable

                # Send update if state changed
                sent = False
                t_send = 0.0
                if self._has_state_changed(current_state):
                    t1 = time.monotonic()
                    await self.send_json({
                        'type': 'atem_state',
                        'state': current_state,
                        'timestamp': asyncio.get_event_loop().time()
                    })
                    t_send = time.monotonic() - t1
                    sent = True
                    self.last_known_state = current_state.copy()
                    self.last_change_time = asyncio.get_event_loop().time()
                    consecutive_no_changes = 0
                else:
                    consecutive_no_changes += 1

                if trace and sent:
                    iteration += 1
                    if t_build > 0.05 or t_send > 0.05 or iteration % 50 == 0:
                        logger.info(
                            "atem_state #%d: build=%.3fs send=%.3fs",
                            iteration, t_build, t_send)

                poll_interval = self._calculate_poll_interval(current_state, consecutive_no_changes)
                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in monitoring loop: {e}", exc_info=True)
            # Fix (2026-07-06): a crashed monitor loop used to just
            # log and die, silently disabling the 5-minute inactivity net —
            # if the socket was already gone (a failed state send is the
            # typical crash here), the session stayed pool-ref'd forever.
            # Run the shared guarded cleanup instead. stop_monitoring's
            # self-cancel resolves inside stop_monitoring itself, same as
            # the inactivity-timeout path.
            await self._cleanup_session(reason='monitor_error')
            await self._safe_send_json({
                'type': 'connection_status',
                'connected': False,
                'message': 'ATEM monitoring failed; disconnected',
            })

    def _calculate_poll_interval(self, current_state, consecutive_no_changes):
        """
        Determine polling interval based on current activity level.

        Priority (fastest to slowest):
            1. Active transition in progress -> ultra_fast
            2. Recent user command -> fast
            3. Recent state change -> fast
            4. Extended idle -> slow
            5. Default -> normal
        """
        current_time = asyncio.get_event_loop().time()

        if self._is_any_transition_active(current_state):
            return self.POLLING_INTERVALS['ultra_fast']

        if current_time - self.last_command_time < self.RECENT_COMMAND_WINDOW:
            return self.POLLING_INTERVALS['fast']

        if current_time - self.last_change_time < self.RECENT_CHANGE_WINDOW:
            return self.POLLING_INTERVALS['fast']

        if consecutive_no_changes > self.IDLE_POLL_THRESHOLD:
            return self.POLLING_INTERVALS['slow']

        return self.POLLING_INTERVALS['normal']

    @staticmethod
    def _is_any_transition_active(state):
        """Check if any transition (ME, DSK, or FTB) is currently in progress."""
        if not state or not state.get('is_connected'):
            return False

        return (any(me.get('transition', {}).get('in_transition', False) or
                    me.get('ftb', {}).get('in_transition', False)
                    for me in state.get('mes') or []) or
                any(dsk.get('in_transition', False)
                    for dsk in state.get('dsks') or []))

    def _has_state_changed(self, current_state):
        """Compare current state against last known state for relevant changes."""
        if not self.last_known_state:
            return True

        important_keys = ['mes', 'dsks', 'colorGenerators', 'auxOutputs', 'audio', 'inputLabels', 'sources', 'topology']

        for key in important_keys:
            if current_state.get(key) != self.last_known_state.get(key):
                return True

        return False

    # =========================================================================
    # WebSocket Helpers
    # =========================================================================

    async def send_current_state(self):
        """Fetch and send current ATEM state to client."""
        try:
            if not self.connection:
                await self.send_json({
                    'type': 'connection_status',
                    'connected': False,
                    'message': 'No ATEM connection'
                })
                return

            state = await sync_to_async(self.build_full_state, thread_sensitive=False)(self.connection)

            if state.get('is_connected'):
                await self.send_json({
                    'type': 'atem_state',
                    'state': state,
                    'timestamp': asyncio.get_event_loop().time()
                })
            else:
                await self.send_json({
                    'type': 'connection_status',
                    'connected': False,
                    'message': 'ATEM not connected'
                })
        except Exception as e:
            logger.error(f"Error getting ATEM state: {e}")
            await self.send_json({'type': 'error', 'message': f'Failed to get state: {str(e)}'})

    async def send_json(self, data):
        """Send JSON-serialized data to WebSocket client."""
        await self.send(text_data=json.dumps(data))

    async def _safe_send_json(self, data) -> bool:
        """``send_json`` that never raises (fixed 2026-07-06).

        A send on a dead socket raising out of a Channels group handler or
        an error path kills the consumer's app task before
        ``websocket.disconnect`` is dispatched — the pool ref, watcher ref
        and meter refcount then leak until process restart. Returns False
        on failure so callers can stop streaming."""
        try:
            await self.send_json(data)
            return True
        except Exception as e:
            logger.debug(f"WebSocket send failed (client likely gone): {e}")
            return False

    # =========================================================================
    # Media Pool (background watcher + Channels group fan-out)
    # =========================================================================

    async def _subscribe_media_pool(self, ip_address: str):
        """Join the per-IP media-pool group, start the watcher, send snapshot."""
        if self._mediapool_ip == ip_address:
            return
        # If switching ATEMs without a full disconnect, tear down the old one
        if self._mediapool_ip:
            await self._unsubscribe_media_pool()

        # Fix (2026-07-06): acquire the watcher BEFORE joining the
        # Channels group. The old order left a failed acquire's consumer in
        # the group with _mediapool_ip unset — never discarded, with group
        # handlers firing into it. Setting _mediapool_ip right after the
        # acquire also means a later failure (group_add / snapshot send)
        # still gets the watcher ref released by _unsubscribe_media_pool.
        snapshot = await sync_to_async(media_pool_service.acquire, thread_sensitive=False)(ip_address)
        self._mediapool_ip = ip_address

        if self.channel_layer is not None:
            await self.channel_layer.group_add(
                media_pool_service.group_name(ip_address), self.channel_name
            )

        await self.send_json({
            'type': 'media_pool_snapshot',
            'data': snapshot,
        })

    async def _unsubscribe_media_pool(self):
        ip = self._mediapool_ip
        if not ip:
            return
        self._mediapool_ip = None
        try:
            if self.channel_layer is not None:
                await self.channel_layer.group_discard(
                    media_pool_service.group_name(ip), self.channel_name
                )
        finally:
            await sync_to_async(media_pool_service.release, thread_sensitive=False)(ip)

    # =========================================================================
    # Audio meter subscription
    # =========================================================================
    # FMLv (per-strip) and FDLv (master) levels arrive ~25 Hz from the
    # ATEM and feed the meters on the audio tab. We DON'T fold them into
    # the polling state diff — that would either flood the diff or miss
    # frames. Instead each event is added to a per-tick batch that flushes
    # as one ``audio_meter_batch`` WS message every METER_BATCH_INTERVAL_S
    # (~25 Hz total) carrying all strips updated within that window.

    METER_BATCH_INTERVAL_S = 0.04  # ~25 Hz total batch flush

    def _subscribe_audio_meters(self):
        """Idempotent. Register per-consumer meter listeners and bump the
        per-IP subscriber refcount; send SFLN(enable=True) to the ATEM on
        the 0→1 transition so it starts streaming FMLv/FDLv packets.

        Captures the current asyncio loop so the (sync) pyatem event
        callbacks can hand events back to the consumer's loop via
        ``call_soon_threadsafe``.
        """
        if self._audio_subscribed:
            return
        protocol = getattr(self.connection, 'protocol', None)
        if protocol is None:
            return
        self._meter_loop = asyncio.get_event_loop()
        self._meter_batch_strips = {}
        self._meter_batch_master = None
        self._meter_batch_pending = False

        # Per-strip meter levels. NOTE: the wildcard ':*' is only emitted
        # by AtemProtocol when the field is registered in
        # FIELDNAME_UNIQUE — and 'fairlight-meter-levels' is NOT, despite
        # being multi-strip. Each FMLv arrives as the bare event name
        # carrying a single strip's data; the FMLv field has .strip_id
        # so we discriminate inside the handler.
        sid = protocol.on('change:fairlight-meter-levels',
                          self._on_strip_meter)
        self._meter_handler_ids.append(
            ('change:fairlight-meter-levels', sid))

        # Master bus meter levels.
        mid = protocol.on('change:fairlight-master-levels',
                          self._on_master_meter)
        self._meter_handler_ids.append(('change:fairlight-master-levels', mid))

        # Per-IP subscriber refcount. First subscriber tells the ATEM to
        # start streaming meters — without SFLN(True) the ATEM emits zero
        # FMLv/FDLv packets even with handlers registered.
        ip = self.current_ip
        if ip:
            prev = _meter_subscribers.get(ip, 0)
            _meter_subscribers[ip] = prev + 1
            if prev == 0:
                try:
                    enable_fairlight_levels(self.connection, enable=True)
                except Exception as e:
                    logger.warning(f"Failed to enable Fairlight meter stream: {e}")

        self._audio_subscribed = True
        self._meter_protocol = protocol

    def _rearm_audio_meters_if_reconnected(self):
        """A pooled re-handshake builds a NEW AtemProtocol and a NEW
        ATEM session — the meter handlers were registered on the old
        object and the new session never received SFLN(enable), so the
        meters froze at the old levels (looking live) until every
        subscriber unsubscribed. Detect the protocol identity change from
        the poll loop and re-arm: re-register the handlers on the new
        protocol and re-send the enable (idempotent on the ATEM; the
        per-IP refcount is untouched — this consumer never unsubscribed)."""
        if not self._audio_subscribed:
            return
        protocol = getattr(self.connection, 'protocol', None) if self.connection else None
        if protocol is None or protocol is self._meter_protocol:
            return
        logger.info(
            f"Re-arming audio meters after in-place reconnect ({self.current_ip})")
        self._meter_handler_ids = []           # old ids died with the old protocol
        sid = protocol.on('change:fairlight-meter-levels', self._on_strip_meter)
        self._meter_handler_ids.append(('change:fairlight-meter-levels', sid))
        mid = protocol.on('change:fairlight-master-levels', self._on_master_meter)
        self._meter_handler_ids.append(('change:fairlight-master-levels', mid))
        self._meter_protocol = protocol
        try:
            enable_fairlight_levels(self.connection, enable=True)
        except Exception as e:
            logger.warning(f"SFLN re-arm failed: {e}")

    def _unsubscribe_audio_meters(self):
        """Idempotent. Unregister per-consumer meter listeners and
        decrement the per-IP refcount; send SFLN(enable=False) on 1→0
        so the ATEM stops streaming once nobody's watching."""
        if not self._audio_subscribed:
            return
        protocol = getattr(self.connection, 'protocol', None) if self.connection else None
        if protocol is not None:
            for event, hid in self._meter_handler_ids:
                try:
                    protocol.off(event, hid)
                except Exception:
                    pass
        self._meter_handler_ids = []
        self._meter_loop = None
        self._meter_batch_strips = {}
        self._meter_batch_master = None
        self._meter_batch_pending = False

        ip = self.current_ip
        if ip:
            prev = _meter_subscribers.get(ip, 0)
            if prev > 0:
                _meter_subscribers[ip] = prev - 1
                if prev == 1:
                    try:
                        if self.connection is not None:
                            enable_fairlight_levels(self.connection, enable=False)
                    except Exception as e:
                        logger.warning(f"Failed to disable Fairlight meter stream: {e}")
                    _meter_subscribers.pop(ip, None)

        self._audio_subscribed = False

    def _on_strip_meter(self, contents):
        """Handler for FMLv events — runs on the pyatem worker thread.
        Adds the latest payload for this strip into the per-tick batch
        (overwriting any previous payload for this strip already queued
        for the same tick) and arms the flush task if not already pending."""
        loop = self._meter_loop
        if loop is None or loop.is_closed():
            return
        sid = getattr(contents, 'strip_id', None) or 'unknown'
        # Pull the values *synchronously* before scheduling — the field
        # object survives, but we want the JSON-clean snapshot only.
        payload = {
            'strip_id': sid,
            'input': self._meter_tuple(contents, 'input'),
            'output': self._meter_tuple(contents, 'output'),
            'level': self._meter_tuple(contents, 'level'),
            'compressor_gr': getattr(contents, 'compressor_gr', 0),
            'limiter_gr': getattr(contents, 'limiter_gr', 0),
            'expander_gr': getattr(contents, 'expander_gr', 0),
        }
        try:
            loop.call_soon_threadsafe(self._enqueue_strip_meter, sid, payload)
        except RuntimeError:
            pass  # loop closed

    def _on_master_meter(self, contents):
        """Handler for FDLv — runs on pyatem worker thread."""
        loop = self._meter_loop
        if loop is None or loop.is_closed():
            return
        payload = {
            'input': self._meter_tuple(contents, 'input'),
            'output': self._meter_tuple(contents, 'output'),
            'level': self._meter_tuple(contents, 'level'),
            'compressor_gr': getattr(contents, 'compressor_gr', 0),
            'limiter_gr': getattr(contents, 'limiter_gr', 0),
        }
        try:
            loop.call_soon_threadsafe(self._enqueue_master_meter, payload)
        except RuntimeError:
            pass

    def _enqueue_strip_meter(self, sid, payload):
        """Runs in the asyncio loop. Latest payload per strip wins for
        this tick — meter-floor flicker isn't worth shipping every
        sample, just the last one before flush."""
        self._meter_batch_strips[sid] = payload
        self._arm_meter_batch_flush()

    def _enqueue_master_meter(self, payload):
        self._meter_batch_master = payload
        self._arm_meter_batch_flush()

    def _arm_meter_batch_flush(self):
        if self._meter_batch_pending:
            return
        self._meter_batch_pending = True
        asyncio.create_task(self._flush_meter_batch_after_delay())

    async def _flush_meter_batch_after_delay(self):
        try:
            await asyncio.sleep(self.METER_BATCH_INTERVAL_S)
        except asyncio.CancelledError:
            self._meter_batch_pending = False
            return
        # Snapshot and clear BEFORE the (potentially blocking) send so
        # any FMLv/FDLv arriving during the await schedule a fresh flush
        # instead of being lost.
        strips_payload = list(self._meter_batch_strips.values())
        master_payload = self._meter_batch_master
        self._meter_batch_strips = {}
        self._meter_batch_master = None
        self._meter_batch_pending = False
        if not self._audio_subscribed:
            return
        if not strips_payload and master_payload is None:
            return
        try:
            await self.send_json({
                'type': 'audio_meter_batch',
                'strips': strips_payload,
                'master': master_payload,
            })
        except Exception:
            pass

    @staticmethod
    def _meter_tuple(field, attr):
        """Convert a (L, R, peak_L, peak_R) tuple of dB floats into the
        JSON shape the frontend expects. Tuple comes from the FMLv/FDLv
        decoder which already converted wire i16 (0.01 dB units) to dB
        floats. Round to 2 decimals to preserve full ATEM display
        precision — the frontend's .toFixed(2) painter expects 2dp
        granularity to match Software Control's readout."""
        t = getattr(field, attr, (0, 0, 0, 0))
        try:
            return {
                'l': round(float(t[0]), 2),
                'r': round(float(t[1]), 2),
                'peak_l': round(float(t[2]), 2),
                'peak_r': round(float(t[3]), 2),
            }
        except (IndexError, TypeError, ValueError):
            return {'l': 0.0, 'r': 0.0, 'peak_l': 0.0, 'peak_r': 0.0}

    async def send_media_pool_snapshot(self):
        """Push the currently cached snapshot on demand (e.g. modal open)."""
        ip = self._mediapool_ip or self.current_ip
        if not ip:
            await self.send_json({'type': 'media_pool_snapshot', 'data': {'slots': [], 'players': []}})
            return
        # Re-arm any thumb-less slots first: opening the modal must always
        # converge to a fully-loaded pool, even if earlier downloads were
        # exhausted against a foreign lock-holder or a dropped task.
        await sync_to_async(media_pool_service.requeue_missing, thread_sensitive=False)(ip)
        snapshot = await sync_to_async(media_pool_service.get_snapshot, thread_sensitive=False)(ip)
        await self.send_json({'type': 'media_pool_snapshot', 'data': snapshot})

    async def handle_media_pool_delete(self, data):
        """Clear a media-pool slot. The watcher picks up the change on its
        next poll and broadcasts the cleared state to all connected clients.

        The clear goes through ``ATEMConnection.clear_still`` — the LOCKED
        clear. Whether a bare CSTL is honoured is model-dependent: the
        fe5c43b smoke test confirmed it on a Constellation HD, but a 1 M/E
        Production Studio 4K silently drops it (observed live 2026-07-29).
        The locked clear works on both.
        """
        slot = data.get('slot')
        if not isinstance(slot, int) or slot < 0 or slot >= 32:
            await self.send_json({'type': 'error', 'message': f'Invalid slot: {slot}'})
            return
        if not self.is_connected:
            await self.send_json({'type': 'error', 'message': 'ATEM not connected'})
            return
        try:
            logger.info(f"Media pool delete requested: slot {slot} on {self.current_ip}")
            await sync_to_async(self.connection.clear_still, thread_sensitive=False)(slot)
            # Force the watcher to drop any cached thumb for this slot, so the
            # next poll re-broadcasts the cleared (isUsed=False) state.
            await sync_to_async(media_pool_service.note_upload_finished, thread_sensitive=False)(
                self.current_ip, slot
            )
            await arecord_activity(
                feature=ActivityLog.FEATURE_ATEM_CONTROL, device=ActivityLog.DEVICE_ATEM,
                action='slot_delete',
                target=self.current_ip or '',
                summary=f"Deleted media-pool slot {slot + 1} on {self.current_ip}",
                slot=slot + 1,
            )
        except Exception as e:
            logger.error(f"Media pool delete failed (slot {slot}): {e}")
            await arecord_activity(
                feature=ActivityLog.FEATURE_ATEM_CONTROL, device=ActivityLog.DEVICE_ATEM,
                action='slot_delete',
                target=self.current_ip or '',
                success=False,
                summary=f"Failed to delete media-pool slot {slot + 1} on {self.current_ip}",
                slot=slot + 1, error=str(e),
            )
            await self.send_json({'type': 'error', 'message': f'Failed to delete slot {slot + 1}'})

    # Channels group event handlers. Django Channels maps the message 'type'
    # (with '.' → '_') to these method names.
    #
    # Fix (2026-07-06): all group handlers send via _safe_send_json —
    # a broadcast landing in the window between client death and disconnect
    # dispatch (wide while thumbnails stream) must not raise out of the
    # handler and kill the consumer's app task.

    async def mediapool_snapshot(self, event):
        await self._safe_send_json({
            'type': 'media_pool_snapshot',
            'data': event.get('payload', {}),
        })

    async def mediapool_slot_updated(self, event):
        await self._safe_send_json({
            'type': 'media_pool_slot_updated',
            'data': event.get('payload', {}),
        })

    async def mediapool_player_updated(self, event):
        await self._safe_send_json({
            'type': 'media_pool_player_updated',
            'data': event.get('payload', {}),
        })

    async def mediapool_pool_lock(self, event):
        # The watcher detected (or saw the end of) a foreign client holding
        # the ATEM's media store lock — surface it so the modal shows an
        # honest banner instead of anonymous spinners.
        await self._safe_send_json({
            'type': 'media_pool_lock',
            'data': event.get('payload', {}),
        })

    async def mediapool_slot_uploaded(self, event):
        # Fast-path event broadcast when an upload lands — a thumbnail
        # synthesised from the local source file arrives before the
        # watcher's reconnect-and-refetch cycle.
        await self._safe_send_json({
            'type': 'media_pool_slot_uploaded',
            'data': event.get('payload', {}),
        })

    async def mediapool_upload_failed(self, event):
        # Broadcast on a failed/cancelled upload:
        # clears the initiator's Uploading overlay and surfaces the error
        # instead of leaving a spinner to die into its 120s safety timer.
        await self._safe_send_json({
            'type': 'media_pool_upload_failed',
            'data': event.get('payload', {}),
        })

    async def profile_capture_progress(self, event):
        """Per-slot progress for an in-flight profile-save media-pool
        capture. Sent by views.profile_save once per slot completion;
        the save dialog renders a progress bar from these. Includes the
        session id so a stale dialog from a prior save click can ignore
        events from a newer one."""
        await self._safe_send_json({
            'type': 'profile_capture_progress',
            'data': event.get('payload', {}),
        })
