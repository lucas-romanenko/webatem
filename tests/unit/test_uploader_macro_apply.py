"""Unit tests for ``_apply_macro_xml`` in the Content Change uploader.

The helper runs after image upload on the same socket. It either:
  * returns ``None`` when ``Profile.apply`` returns a clean result;
  * returns a synthetic ``ItemResult(slot=-1, success=False)`` on any
    failure path so the surrounding aggregation
    (``all(r.success for r in results)``) naturally rolls macro failure
    into the task status.

These tests pin the failure paths so a future change to the helper
doesn't accidentally drop a failure on the floor (which would leave
the operator broadcasting stale macros against new images without any
red flag on the monitor).
"""

import queue
import time
import types

import pytest


# The helper imports ``Profile`` / ``ApplyOptions`` from ``pyatem`` inside
# the function. Tests patch attributes on the ``pyatem`` module so each
# scenario can simulate a specific Profile behaviour without touching
# the real Profile class.


class _FakeTransport:
    def __init__(self):
        self.thread_recv_queue = queue.Queue()


class _FakeProtocol:
    """Minimal AtemProtocol stand-in for the pump-thread contract.

    ``loop()`` blocks on the recv queue (matching real pyatem behaviour);
    ``Wakeup()`` placed on the queue makes loop() return cleanly without
    flipping ``connected`` False. The helper relies on that.
    """

    def __init__(self):
        self.transport = _FakeTransport()
        self.connected = True
        self.loop_calls = 0

    def loop(self):
        self.loop_calls += 1
        # Block until something arrives — real pyatem behaviour.
        self.transport.thread_recv_queue.get(timeout=2.0)


@pytest.fixture
def fake_pyatem(monkeypatch):
    """Stash a callable factory on the ``pyatem`` module. Tests configure
    it per-scenario via ``configure(...)``."""
    import atemwire as real_atemwire

    state = {
        'apply_raises': None,
        'apply_errors': (),
        'from_file_raises': None,
        'apply_calls': [],
    }

    class _FakeApplyResult:
        def __init__(self, errors=()):
            self.applied = []
            self.skipped = []
            self.errors = list(errors)

        def summary(self):
            return f"applied: {len(self.applied)}, errors: {len(self.errors)}"

    class _FakeProfile:
        def __init__(self):
            pass

        @classmethod
        def from_file(cls, path):
            if state['from_file_raises'] is not None:
                raise state['from_file_raises']
            inst = cls()
            inst._loaded_from = path
            return inst

        def apply(self, conn, opts):
            state['apply_calls'].append((conn, opts))
            if state['apply_raises'] is not None:
                raise state['apply_raises']
            return _FakeApplyResult(errors=state['apply_errors'])

    class _FakeApplyOptions:
        # Just enough to satisfy the helper. The macros_only() classmethod
        # is what the helper actually calls.
        @classmethod
        def macros_only(cls):
            inst = cls()
            inst._is_macros_only = True
            return inst

    monkeypatch.setattr(real_atemwire, 'Profile', _FakeProfile, raising=False)
    monkeypatch.setattr(real_atemwire, 'ApplyOptions', _FakeApplyOptions,
                        raising=False)

    def configure(*, apply_raises=None, apply_errors=(),
                  from_file_raises=None):
        state['apply_raises'] = apply_raises
        state['apply_errors'] = list(apply_errors)
        state['from_file_raises'] = from_file_raises

    return types.SimpleNamespace(
        configure=configure,
        apply_calls=state['apply_calls'],
        ApplyOptions=_FakeApplyOptions,
    )


def _import_helper():
    """Import the helper. Lazy so the test collection doesn't fail at
    import time on machines without the pyatem C extension built."""
    from atem_control.uploader import _apply_macro_xml
    return _apply_macro_xml


def test_apply_returns_none_on_clean_success(fake_pyatem):
    fake_pyatem.configure()
    apply_fn = _import_helper()
    logs = []

    result = apply_fn(
        protocol=_FakeProtocol(),
        macro_xml_path='/tmp/dummy.xml',
        log_append=logs.append,
    )

    assert result is None
    # apply() must have been called with the macros_only() options.
    assert len(fake_pyatem.apply_calls) == 1
    _, opts = fake_pyatem.apply_calls[0]
    assert getattr(opts, '_is_macros_only', False)
    # Log should mention what we did.
    assert any('applying macros from dummy.xml' in line for line in logs)


def test_apply_returns_failure_when_apply_raises(fake_pyatem):
    fake_pyatem.configure(apply_raises=RuntimeError('boom'))
    apply_fn = _import_helper()
    logs = []

    result = apply_fn(
        protocol=_FakeProtocol(),
        macro_xml_path='/tmp/dummy.xml',
        log_append=logs.append,
    )

    assert result is not None
    assert result.success is False
    assert result.slot == -1
    assert 'boom' in result.error
    assert any('macro apply: raised' in line for line in logs)


def test_apply_returns_failure_when_from_file_raises(fake_pyatem):
    fake_pyatem.configure(from_file_raises=ValueError('bad xml'))
    apply_fn = _import_helper()
    logs = []

    result = apply_fn(
        protocol=_FakeProtocol(),
        macro_xml_path='/tmp/dummy.xml',
        log_append=logs.append,
    )

    assert result is not None
    assert result.success is False
    assert result.slot == -1
    assert 'bad xml' in result.error
    # apply() should NOT have been called when from_file already errored.
    assert fake_pyatem.apply_calls == []


def test_apply_pumps_protocol_loop_during_apply(fake_pyatem, monkeypatch):
    """The pump thread must drive ``protocol.loop()`` at least once
    while ``Profile.apply`` is running, so blocking calls inside apply
    (``upload_macro_bytecode`` waits on ``upload-done``) can complete.

    Simulated by making the fake ``apply()`` sleep briefly — the pump
    thread should accumulate ``loop_calls`` during that window. Then
    the finally block wakes the pump via ``Wakeup`` and joins it.
    """
    proto = _FakeProtocol()

    # Patch the fake Profile.apply to sleep + return a clean result.
    import atemwire as real_atemwire

    class _SlowResult:
        applied = ['macros']
        skipped = []
        errors = []

        def summary(self):
            return 'applied: 1'

    class _SlowProfile:
        @classmethod
        def from_file(cls, path):
            return cls()

        def apply(self, conn, opts):
            time.sleep(0.2)
            return _SlowResult()

    monkeypatch.setattr(real_atemwire, 'Profile', _SlowProfile, raising=False)

    apply_fn = _import_helper()
    logs = []
    result = apply_fn(
        protocol=proto,
        macro_xml_path='/tmp/slow.xml',
        log_append=logs.append,
    )
    assert result is None
    # Pump should have entered loop() at least once during the 200ms sleep.
    assert proto.loop_calls >= 1, (
        f"pump thread didn't drive protocol.loop() (loop_calls={proto.loop_calls})"
    )


def test_execute_upload_macros_only_runs_apply_for_macro_only_ip(
        fake_pyatem, monkeypatch, tmp_path):
    """Media Pool -> Hyperdeck offline case: empty image items but a
    macro XML and a ``macro_only_ips=[ip]`` argument. ``execute_upload``
    should open a connection for that IP and run the macro apply, with
    no image-upload work attempted."""
    fake_pyatem.configure()

    # Capture which IPs _run_for_single_ip is called for, and what
    # arguments it sees. Stub the actual implementation since it opens
    # a real AtemProtocol socket otherwise.
    calls = []

    def _stub_run_for_single_ip(ip, slot_paths, skip_tally, canceller,
                                log_append, macro_xml_path=None,
                                run_macro_after=None):
        calls.append({
            'ip': ip,
            'slot_paths': list(slot_paths),
            'macro_xml_path': macro_xml_path,
            'run_macro_after': run_macro_after,
        })
        return []  # no per-item results

    from atem_control import uploader as up_mod
    monkeypatch.setattr(up_mod, '_run_for_single_ip',
                        _stub_run_for_single_ip)

    xml_path = str(tmp_path / 'macros.xml')
    with open(xml_path, 'w') as fh:
        fh.write('<?xml version="1.0"?><Profile><MacroPool/></Profile>')

    results = up_mod.execute_upload(
        [],  # no image items
        skip_tally=True,
        macro_xml_path=xml_path,
        macro_only_ips=['192.168.1.84'],
    )

    # Exactly one per-IP call, for the macro-only IP, with no
    # slot_paths and the XML path forwarded.
    assert len(calls) == 1
    assert calls[0]['ip'] == '192.168.1.84'
    assert calls[0]['slot_paths'] == []
    assert calls[0]['macro_xml_path'] == xml_path
    # No image ItemResults to surface, no synthetic macro result added
    # by the stub.
    assert results == []


def test_execute_upload_macros_only_ignored_without_macro_xml(
        fake_pyatem, monkeypatch):
    """macro_only_ips is meaningless without macro_xml_path — guard
    against accidental no-op connections (which would open a socket,
    log noise, and do nothing useful)."""
    fake_pyatem.configure()
    calls = []

    def _stub_run_for_single_ip(ip, *_a, **_kw):
        calls.append(ip)
        return []

    from atem_control import uploader as up_mod
    monkeypatch.setattr(up_mod, '_run_for_single_ip',
                        _stub_run_for_single_ip)

    up_mod.execute_upload(
        [],
        skip_tally=True,
        macro_xml_path=None,         # nothing to apply
        macro_only_ips=['1.2.3.4'],  # should be ignored
    )

    assert calls == []


def test_apply_returns_failure_when_result_has_errors(fake_pyatem):
    fake_pyatem.configure(apply_errors=['macros: BadOp: unsupported',
                                        'macros: BadOp2: unsupported'])
    apply_fn = _import_helper()
    logs = []

    result = apply_fn(
        protocol=_FakeProtocol(),
        macro_xml_path='/tmp/dummy.xml',
        log_append=logs.append,
    )

    assert result is not None
    assert result.success is False
    assert result.slot == -1
    # Both errors should be present in the consolidated error message.
    assert 'BadOp:' in result.error
    assert 'BadOp2:' in result.error
    # And both should have been logged individually for forensics.
    assert sum(1 for line in logs if 'macro apply error:' in line) == 2
