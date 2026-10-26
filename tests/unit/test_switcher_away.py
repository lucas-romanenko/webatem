"""A switcher that goes quiet mid-session keeps the control page (#38, #39).

After about 5 s without packets bmdwire re-handshakes in place, as long as
it takes. Through that gap ``is_connected`` stays True (the worker lives) and
``is_ready`` is False (the state is gone until the new dump). So:

- the poll loop's "connection lost" carries ``reconnecting``, which keeps the
  page on screen under a banner, and "restored" follows when the state is back;
- commands and the media-pool delete are refused at once while not ready,
  instead of going out under the session being replaced.

The connection is a stand-in; nothing touches the network.
"""
import asyncio
import types

from atem_control.control import consumer as consumer_mod
from atem_control.control.consumer import ATEMConsumer

IP = '192.0.2.10'


def _consumer(conn):
    c = ATEMConsumer.__new__(ATEMConsumer)   # skip __init__ / channel scope
    c.connection = conn
    c.current_ip = IP
    c.last_known_state = {}
    c.sent = []

    async def send_json(data):
        c.sent.append(data)
    c.send_json = send_json
    return c


def _conn(*, connected=True, ready=True, **kw):
    return types.SimpleNamespace(is_connected=connected, is_ready=ready, **kw)


def _errors(c):
    return [m['message'] for m in c.sent if m.get('type') == 'error']


# --- is_ready -----------------------------------------------------------------

def test_is_ready_follows_the_connection():
    assert _consumer(_conn(ready=True)).is_ready is True
    assert _consumer(_conn(ready=False)).is_ready is False
    assert _consumer(None).is_ready is False


def test_is_ready_is_is_connected_before_bmdwire_has_it():
    old = types.SimpleNamespace(is_connected=True)       # bmdwire < 1.3: no is_ready
    assert _consumer(old).is_ready is True


# --- commands -----------------------------------------------------------------

def test_a_command_while_the_switcher_is_away_is_refused(monkeypatch):
    calls = []
    monkeypatch.setattr(consumer_mod, 'dispatch_command', lambda *a: calls.append(a) or (True, None))
    c = _consumer(_conn(connected=True, ready=False))
    asyncio.run(c.execute_command('cut', {}))
    assert calls == [] and _errors(c) == ['ATEM not connected']


def test_a_command_while_ready_goes_to_the_switcher(monkeypatch):
    calls = []
    # A refusal from the dispatch ends the call early: what matters is that
    # it got there.
    monkeypatch.setattr(consumer_mod, 'dispatch_command', lambda *a: calls.append(a[1]) or (False, 'stop'))
    c = _consumer(_conn(connected=True, ready=True))
    asyncio.run(c.execute_command('cut', {}))
    assert calls == ['cut'] and _errors(c) == ['stop']


def test_a_delete_while_the_switcher_is_away_is_refused_at_once():
    cleared = []
    c = _consumer(_conn(connected=True, ready=False, clear_still=cleared.append))
    asyncio.run(c.handle_media_pool_delete({'slot': 3}))
    assert cleared == [] and _errors(c) == ['ATEM not connected']


def test_a_delete_while_ready_goes_to_the_switcher():
    cleared = []

    def clear_still(slot):
        cleared.append(slot)
        raise TimeoutError('stand-in')                     # ends at the failed-delete reply
    c = _consumer(_conn(connected=True, ready=True, clear_still=clear_still))
    asyncio.run(c.handle_media_pool_delete({'slot': 3}))
    assert cleared == [3] and _errors(c) == ['Failed to delete slot 4']   # the operator counts from 1


# --- the poll loop's notices --------------------------------------------------

def _run_monitor(states):
    """Run the real _monitor_loop over one build_full_state per poll, then
    stop; return the connection_status messages it sent."""
    c = _consumer(_conn())
    c.is_monitoring = True
    c.idle_timeout = None
    c.last_command_time = 0
    c._rearm_audio_meters_if_reconnected = lambda: None
    c._has_state_changed = lambda state: False
    c._calculate_poll_interval = lambda state, n: 0
    queue = list(states)

    def build_full_state(connection):
        state = queue.pop(0)
        if not queue:
            c.is_monitoring = False
        return state
    c.build_full_state = build_full_state
    asyncio.run(c._monitor_loop())
    return [m for m in c.sent if m.get('type') == 'connection_status']


def test_a_mid_session_drop_says_reconnecting_and_restored_follows():
    up, gone = {'is_connected': True}, {'is_connected': False}
    notices = _run_monitor([up, up, gone, gone, up])
    assert notices == [
        {'type': 'connection_status', 'connected': False, 'reconnecting': True,
         'message': 'ATEM connection lost'},
        {'type': 'connection_status', 'connected': True, 'message': 'ATEM connection restored'},
    ]


def test_no_notice_while_the_first_state_comes_in():
    # A fresh connect's first polls may find the state empty: no flash.
    up, gone = {'is_connected': True}, {'is_connected': False}
    assert _run_monitor([gone, gone, up, up]) == []
