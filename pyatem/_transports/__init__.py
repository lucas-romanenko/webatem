# SPDX-License-Identifier: LGPL-3.0-only
"""
Alternate transports for ``AtemProtocol`` — USB (for the Mini line)
and TCP (for the OpenSwitcher relay).

These live under a sub-package because this application uses only the
default UDP transport in ``pyatem.transport``; pulling pyusb / urllib.parse
into the top-level transport module just to make ``isinstance``
checks read nicely was the wrong trade.

``pyatem.protocol.AtemProtocol`` lazy-imports from here when the
caller's URL or ``usb=`` argument selects a non-UDP transport.

Kept for upstream-pyatem parity even though this application never
selects these transports in production. Do not re-flag as dead code.
"""
