"""L10 (2026-07-07): the profile-save capture cancel registry. A cancel
arrives on another request than the save it stops, so it goes through a
module-level registry keyed by the page session: bounded, and consumed by
the save it cancelled. Moved here from AV Server's session-hygiene tests."""
import pytest

from atem_control.profile import export as export_mod


@pytest.fixture(autouse=True)
def _clear_cancel_registry():
    with export_mod._cancel_lock:
        if export_mod._cancelled_sessions is not None:
            export_mod._cancelled_sessions.clear()
    yield
    with export_mod._cancel_lock:
        if export_mod._cancelled_sessions is not None:
            export_mod._cancelled_sessions.clear()


def test_cancel_request_then_check():
    assert export_mod._is_capture_cancelled('sess-1') is False
    assert export_mod.request_capture_cancel('sess-1') is True
    assert export_mod._is_capture_cancelled('sess-1') is True
    # a different session is unaffected
    assert export_mod._is_capture_cancelled('sess-2') is False


def test_empty_session_is_noop():
    assert export_mod.request_capture_cancel('') is False
    assert export_mod._is_capture_cancelled('') is False


def test_clear_consumes_the_flag():
    export_mod.request_capture_cancel('sess-3')
    assert export_mod._is_capture_cancelled('sess-3') is True
    export_mod._clear_capture_cancel('sess-3')
    # so a reused id can't cancel a later save
    assert export_mod._is_capture_cancelled('sess-3') is False


def test_registry_is_bounded():
    cap = export_mod._CANCEL_MAX_ENTRIES
    for i in range(cap + 50):
        export_mod.request_capture_cancel(f's{i}')
    with export_mod._cancel_lock:
        assert len(export_mod._cancelled_sessions) <= cap
    # the newest survive; the oldest were dropped
    assert export_mod._is_capture_cancelled(f's{cap + 49}') is True
    assert export_mod._is_capture_cancelled('s0') is False
