"""Pytest bootstrap for WebATEM's own tests.

The tests run without a live ATEM. Test doubles for the protocol layer
(``FakeAtemProtocol`` and friends) ship inside the atemwire package as
``atemwire.testing``; tests import them from there. Django is configured by
pytest-django from pytest.ini (``DJANGO_SETTINGS_MODULE = webatem.settings``).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest


@pytest.fixture(autouse=True)
def _still_cache_in_tmp(tmp_path, monkeypatch):
    """The still cache's default place is the data folder: every test gets a
    folder of its own instead, so none writes into a real one."""
    from atem_control import storage
    monkeypatch.setattr(storage, 'STILL_CACHE_DIR', tmp_path / 'still-cache')
