"""What keeps a control session alive under the host's idle disconnect
(``hooks.idle_disconnect_seconds``): the operator using the page, nothing else.

- Every control command counts, and so does the page's ``activity`` ping,
  which it sends on real input (pointer, touch, keys) and which covers what
  goes over HTTP (HyperDeck, uploads, profiles, the switcher's name) or never
  leaves the browser (Settings, the M/E selector, typing).
- What the page sends on its own does not: the session verbs and the
  panel/tab housekeeping (audio-meter subscribe on a tab switch, the media
  pool's refresh).
- State coming back from the switcher never counts. Most switchers are also
  driven by another controller around the clock; their changes say nothing
  about whether this operator is there.

The connection is a stand-in; nothing touches the network.
"""
import asyncio
import json
import pathlib
import re
import types

from atem_control.control import consumer as consumer_mod
from atem_control.control.consumer import ATEMConsumer

IP = '192.0.2.10'
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _consumer(conn=None):
    c = ATEMConsumer.__new__(ATEMConsumer)   # skip __init__ / channel scope
    c.connection = conn or types.SimpleNamespace(is_connected=True, is_ready=True)
    c.current_ip = IP
    c.last_known_state = {}
    c.last_command_time = 0
    c.last_change_time = 0
    c.sent = []

    async def send_json(data):
        c.sent.append(data)
    c.send_json = send_json
    c._safe_send_json = send_json
    return c


def _stamp_after(command, monkeypatch, **data):
    """last_command_time after the page sends ``command`` (it was 1.0)."""
    monkeypatch.setattr(consumer_mod, 'dispatch_command', lambda *a: (True, None))
    c = _consumer()
    for name in ('send_current_state', 'send_media_pool_snapshot'):
        async def _noop(*a, **k):
            return None
        setattr(c, name, _noop)
    c._subscribe_audio_meters = lambda: None
    c._unsubscribe_audio_meters = lambda: None
    c._record_atem_command = lambda *a: None
    c.last_command_time = 1.0

    async def go():
        await c.receive(text_data=json.dumps({'command': command, **data}))
    asyncio.run(go())
    return c


# --- what counts ----------------------------------------------------------------

def test_a_control_command_counts(monkeypatch):
    c = _stamp_after('cut', monkeypatch)
    assert c.last_command_time > 1.0


def test_the_activity_ping_counts_and_is_not_an_error(monkeypatch):
    calls = []
    monkeypatch.setattr(consumer_mod, 'dispatch_command', lambda *a: calls.append(a[1]) or (False, 'unknown'))
    c = _consumer()
    c.last_command_time = 1.0
    asyncio.run(c.receive(text_data='{"command": "activity"}'))
    assert c.last_command_time > 1.0
    assert calls == []                                   # never sent to the switcher
    assert [m for m in c.sent if m.get('type') == 'error'] == []


# --- what the page sends on its own does not ----------------------------------

def test_housekeeping_the_page_sends_by_itself_does_not_count(monkeypatch):
    for verb in ('refresh', 'subscribe_audio_meters', 'unsubscribe_audio_meters',
                 'media_pool_refresh', 'disconnect'):
        c = _stamp_after(verb, monkeypatch)
        assert c.last_command_time == 1.0, verb


# --- state from the switcher never counts --------------------------------------

def test_a_switcher_changing_all_the_time_is_still_dropped_when_nobody_uses_the_page(monkeypatch):
    """A steady stream of state changes (another controller flipping program
    and preview) with no operator input: the session is still dropped at the
    timeout, and the changes never touch the activity clock."""
    c = ATEMConsumer()                       # a real one: the loop reads its whole state
    c.connection = types.SimpleNamespace(is_connected=True, is_ready=True)
    c.current_ip = IP
    sent = []

    async def send_json(data):
        sent.append(data)
    c.send_json = send_json
    c.is_monitoring = True
    c.idle_timeout = 0.6
    c._cleanup_started = False
    c._rearm_audio_meters_if_reconnected = lambda: None
    flips = {'n': 0}

    def build_full_state(conn):
        flips['n'] += 1
        n = flips['n']
        return {'is_connected': True,
                'mes': [{'program': {'source': 1 + n % 2}, 'preview': {'source': 2 - n % 2}}]}
    c.build_full_state = build_full_state
    c._state_has_changed = lambda s: True

    dropped = {}

    async def auto_disconnect():
        dropped['at'] = asyncio.get_event_loop().time()
        c.is_monitoring = False
    c._auto_disconnect_inactive = auto_disconnect

    async def go():
        t0 = asyncio.get_event_loop().time()
        c.last_command_time = t0                         # the connect
        await asyncio.wait_for(c._monitor_loop(), timeout=5)
        return t0
    t0 = asyncio.run(go())

    assert flips['n'] >= 3                               # the switcher really kept changing
    assert sum(m.get('type') == 'atem_state' for m in sent) >= 3   # and the page saw it
    assert c.last_command_time == t0                     # and never reset the clock
    assert 'at' in dropped and dropped['at'] - t0 >= 0.6


# --- the page: only real input pings ---------------------------------------------

def test_the_page_pings_on_real_input_only():
    src = (ROOT / 'atem_control/js/runtime.js').read_text()
    block = re.search(r'ACTIVITY_EVENTS\s*=\s*\[([^\]]*)\]', src)
    assert block, 'runtime.js has no ACTIVITY_EVENTS list'
    events = set(re.findall(r"'(\w+)'", block.group(1)))
    assert events == {'pointerdown', 'keydown', 'touchstart'}   # no mousemove, no wheel
    assert 'isTrusted' in src                                    # script-made events never count
    assert "'activity'" in src


def test_the_bundle_carries_the_ping():
    bundle = (ROOT / 'atem_control/static/js/atem_control.js').read_text()
    assert 'ACTIVITY_EVENTS' in bundle and '"activity"' in bundle
