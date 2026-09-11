"""Pytest bootstrap for WebATEM's own tests.

The tests run without a live ATEM. Test doubles for the protocol layer
(``FakeAtemProtocol`` and friends) ship inside the atemwire package as
``atemwire.testing``; tests import them from there. Django is configured by
pytest-django from pytest.ini (``DJANGO_SETTINGS_MODULE = config.settings``).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
