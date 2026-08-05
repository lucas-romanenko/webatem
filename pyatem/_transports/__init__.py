"""
Alternate transports for ``AtemProtocol`` — USB (for the Mini line)
and TCP (for the OpenSwitcher relay).

These live under a sub-package because AV Server uses only the default
UDP transport in ``pyatem.transport``; pulling pyusb / urllib.parse
into the top-level transport module just to make ``isinstance``
checks read nicely was the wrong trade.

``pyatem.protocol.AtemProtocol`` lazy-imports from here when the
caller's URL or ``usb=`` argument selects a non-UDP transport.

Kept for upstream-pyatem parity even though production AV Server never
selects these transports. Do not re-flag as dead code.
"""
