"""Activity rows for control-page commands (2026-11-13).

A command row credits the session's logged-in user, as the connect row
does; before, a host's log showed every command as "system". A macro row
names the macro and its number from the payload the page really sends
({macro_number: N}, 1-based); before, it read keys the page never sends
and said "Ran macro None".
"""
import asyncio
import struct
import types

from atemwire.messages.macros import MacroPropertiesField
from django.contrib.auth.models import AnonymousUser

from atem_control import hooks
from atem_control.control import activity as A
from atem_control.control.consumer import ATEMConsumer


def _mprp(index, name, used=True):
    """One macro slot as the switcher sends it (MPrp)."""
    raw = name.encode()
    return MacroPropertiesField(struct.pack('>H??HH', index, used, False, len(raw), 0) + raw)


def _mx(*slots):
    return {'macro-properties': {s.index: s for s in slots}}


# --- macro wording ------------------------------------------------------------

def test_a_named_macro_shows_number_and_name():
    mx = _mx(_mprp(2, 'Open table'))
    assert A.summarize('execute_macro', {'macro_number': 3}, mx) == "Ran macro 3 'Open table'"


def test_the_number_is_the_buttons_not_the_wire_index():
    # The page's button 1 is wire slot 0; bmdwire's execute_macro takes 1 off.
    mx = _mx(_mprp(0, 'Intro'))
    assert A.summarize('execute_macro', {'macro_number': 1}, mx) == "Ran macro 1 'Intro'"


def test_no_name_or_the_default_name_shows_the_number_only():
    mx = _mx(_mprp(0, ''), _mprp(2, 'Macro 3'), _mprp(3, '  macro 4 '), _mprp(4, 'Old', used=False))
    assert A.summarize('execute_macro', {'macro_number': 1}, mx) == 'Ran macro 1'
    assert A.summarize('execute_macro', {'macro_number': 3}, mx) == 'Ran macro 3'
    assert A.summarize('execute_macro', {'macro_number': 4}, mx) == 'Ran macro 4'
    assert A.summarize('execute_macro', {'macro_number': 5}, mx) == 'Ran macro 5'
    # a slot no MPrp has arrived for, and no state at all
    assert A.summarize('execute_macro', {'macro_number': 9}, mx) == 'Ran macro 9'
    assert A.summarize('execute_macro', {'macro_number': 9}) == 'Ran macro 9'


def test_another_slots_default_name_is_a_real_name():
    mx = _mx(_mprp(2, 'Macro 7'))
    assert A.summarize('execute_macro', {'macro_number': 3}, mx) == "Ran macro 3 'Macro 7'"


def test_never_none():
    assert 'None' not in A.summarize('execute_macro', {})


# --- who did it -----------------------------------------------------------------

class _Recorder(hooks.Hooks):
    def __init__(self):
        self.rows = []

    def record_activity(self, **kw):
        self.rows.append(kw)


def _user(name):
    return types.SimpleNamespace(username=name, is_authenticated=True)


def _consumer(scope_user, mx=None):
    c = ATEMConsumer.__new__(ATEMConsumer)   # skip __init__ / channel scope
    if scope_user is not ...:
        c.scope = {'user': scope_user}
    c.connection = types.SimpleNamespace(mixerstate=mx or {})
    c.current_ip = '192.0.2.10'
    c.current_atem_name = 'BC15.5'
    c._slider_pending = {}
    c._slider_tasks = {}
    return c


def _run(c, command, data, monkeypatch, wait=0.0):
    rec = _Recorder()
    monkeypatch.setattr(hooks, 'get', lambda: rec)
    monkeypatch.setattr(ATEMConsumer, 'SLIDER_DEBOUNCE_SECONDS', 0.01)

    async def go():
        c._record_atem_command(command, data)
        await asyncio.sleep(wait)
        await asyncio.sleep(0)
    asyncio.run(go())
    return rec.rows


def test_a_command_row_credits_the_logged_in_user(monkeypatch):
    sorin = _user('sorin.i')
    mx = _mx(_mprp(2, 'Open table'))
    rows = _run(_consumer(sorin, mx), 'execute_macro', {'macro_number': 3}, monkeypatch)
    assert len(rows) == 1
    assert rows[0]['user'] is sorin
    assert rows[0]['action'] == 'execute_macro'
    assert rows[0]['summary'] == "Ran macro 3 'Open table'"


def test_the_settled_slider_row_credits_the_user_too(monkeypatch):
    sorin = _user('sorin.i')
    rows = _run(_consumer(sorin), 'set_usk_pattern_softness',
                {'me': 0, 'keyer': 0, 'softness': 40}, monkeypatch, wait=0.1)
    assert len(rows) == 1 and rows[0]['user'] is sorin


def test_standalone_without_a_login_still_logs(monkeypatch):
    for scope_user in (AnonymousUser(), None, ...):   # anonymous, no user, no scope
        rows = _run(_consumer(scope_user), 'cut', {'me': 0}, monkeypatch)
        assert len(rows) == 1 and rows[0]['user'] is None and rows[0]['summary'] == 'Cut'


def test_the_default_hook_writes_the_username(caplog):
    # Standalone WebATEM's log line names the user, or '-' without a login.
    caplog.set_level('INFO', logger='atem_control.activity')
    hooks.Hooks().record_activity(action='cut', user=_user('sorin.i'), summary='Cut')
    hooks.Hooks().record_activity(action='cut', user=None, summary='Cut')
    msgs = [r.getMessage() for r in caplog.records]
    assert 'user=sorin.i' in msgs[0] and 'user=-' in msgs[1]


def test_hyperdeck_transport_row_credits_the_user(monkeypatch):
    from atem_control.hyperdeck import views as hd
    rec = _Recorder()
    monkeypatch.setattr(hooks, 'get', lambda: rec)
    monkeypatch.setattr(hd, '_deck_name', lambda ip: 'Deck 1')
    sorin = _user('sorin.i')
    hd._log_transport(types.SimpleNamespace(user=sorin), '192.0.2.20', 'play', {})
    hd._log_transport(types.SimpleNamespace(user=AnonymousUser()), '192.0.2.20', 'stop', {})
    assert [r['user'] for r in rec.rows] == [sorin, None]


def test_the_pages_exact_message_end_to_end(monkeypatch):
    """The text the page's button sends (ATEMControl.cmd: {command, me,
    ...params}) through receive → dispatch → the switcher and the log."""
    import json
    rec = _Recorder()
    monkeypatch.setattr(hooks, 'get', lambda: rec)
    sent = []
    sorin = _user('sorin.i')
    c = _consumer(sorin, _mx(_mprp(2, 'Open table')))
    c.connection.is_connected = c.connection.is_ready = True
    c.connection.send = sent.append
    c.last_known_state = {}

    async def go():
        await c.receive(json.dumps({'command': 'execute_macro', 'me': 0, 'macro_number': 3}))
        await asyncio.sleep(0)
    asyncio.run(go())
    assert [(type(s).__name__, s.index) for s in sent] == [('MacroActionCommand', 2)]
    assert [(r['user'], r['summary']) for r in rec.rows] == [(sorin, "Ran macro 3 'Open table'")]
