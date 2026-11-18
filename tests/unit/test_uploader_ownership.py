"""UP-1 (2026-08-28): when a host's upload worker loses its DB session
mid-upload it can no longer prove it still owns the job (its lane lock died
with the session), and another worker may take the job over. The host's
cancel check raises ``OwnershipLost`` then, and the upload has to ABORT
instead of carrying on. These pin the half that lives in this module."""
import pytest

from atem_control.uploader import OwnershipLost, _Canceller


def test_canceller_propagates_ownership_lost():
    # A DB-death signal must NOT be swallowed: it has to unwind and abort.
    def check():
        raise OwnershipLost("db session gone")
    with pytest.raises(OwnershipLost):
        _Canceller(check).is_set()


def test_canceller_swallows_ordinary_check_errors():
    # A genuine bug in the check is still treated as "not cancelled" (continue).
    def check():
        raise ValueError("bug in the cancel check")
    assert _Canceller(check).is_set() is False


def test_ownership_lost_is_baseexception_not_exception():
    # It must bypass every `except Exception` in the upload path.
    assert issubclass(OwnershipLost, BaseException)
    assert not issubclass(OwnershipLost, Exception)
