"""The app gives every thread the GIL at least every millisecond, so each
switcher session's UDP thread can acknowledge a still download's packets
within the ~80 ms after which the switcher resends them (2026-11-04)."""
import sys

from atem_control.apps import GIL_SWITCH_INTERVAL


def test_the_app_shortens_the_gil_switch_interval():
    assert sys.getswitchinterval() <= GIL_SWITCH_INTERVAL
