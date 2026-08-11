# SPDX-License-Identifier: LGPL-3.0-only
"""HyperDeck binding messages — the ATEM's own HyperDeck control settings.

The ATEM acts as a HyperDeck client: in Software Control's HyperDecks
palette you add a deck by IP, connect, and assign it a switcher input.
The ATEM stores that binding and broadcasts it to every connected client
via ``RXMS``. Parsing it lets us read the deck's IP and the input it feeds
straight from the ATEM — per-deployment-correct, no hardcoding.

The ATEM emits all 10 slots regardless; slots with network address
0.0.0.0 are unconfigured.
"""

import socket
import struct

from pyatem._state import safe_int
from pyatem.messages._dsl import Recv, Send, u16


class HyperdeckSettingsField(Recv):
    """``RXMS`` — per-slot HyperDeck binding (IP + switcher input).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    HyperDeck slot id (0..9)
    4      4    u32    Network address (IPv4, big-endian); 0 = unconfigured
    8      2    u16    Switcher input the deck feeds
    ====== ==== ====== ===========

    Layout verified live 2026-05-29 against an ATEM with a deck bound at
    192.168.1.11 on input 4:
    ``RXMS[0] = 00 00 | 00 00 | c0 a8 01 0b | 00 04 | ...``.
    """
    CODE = 'RXMS'
    PRETTY = 'hyperdeck-settings'
    KEY_FORMAT = struct.Struct('>H')   # index by slot id (u16 @ offset 0)

    index = u16(at=0)
    input = u16(at=8)

    def __init__(self, raw: bytes):
        super().__init__(raw)
        self.network_address = socket.inet_ntoa(raw[4:8])
        self.configured = raw[4:8] != b'\x00\x00\x00\x00'

    def __repr__(self):
        return (f'<hyperdeck-settings id={self.index} '
                f'ip={self.network_address} input={self.input} '
                f'configured={self.configured}>')


class HyperdeckSettingsCommand(Send):
    """``CXMS`` — set a HyperDeck binding (network address + switcher input).

    Reverse-engineered from a Software Control capture (``addHyperdeck.pcap``,
    2026-06-09) and validated live: ASC sends one CXMS per changed field, and
    the ATEM echoes the result back via ``RXMS``.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Field mask (bit 0 = network address, bit 1 = input)
    1      1    ?      reserved (ASC leaves it uninitialised; we send 0)
    2      2    u16    HyperDeck slot id (0..9)
    4      4    u32    Network address (IPv4, big-endian)
    8      2    u16    Switcher input the deck feeds
    10     6    ?      auto-roll / frame-delay — not yet mapped; sent as 0
    ====== ==== ====== ===========

    Only the fields whose mask bit is set are applied, so passing one of
    ``network_address`` / ``switcher_input`` leaves the other untouched on
    the switcher (same partial-write contract as the other setters).
    """
    CODE = 'CXMS'

    def __init__(self, slot, network_address=None, switcher_input=None):
        self.slot = int(slot)
        self.network_address = network_address
        self.switcher_input = switcher_input

    def get_command(self) -> bytes:
        mask = 0
        if self.network_address is not None:
            mask |= 0x01
        if self.switcher_input is not None:
            mask |= 0x02
        ip = (socket.inet_aton(self.network_address)
              if self.network_address is not None else b'\x00\x00\x00\x00')
        data = (bytes([mask, 0])
                + struct.pack('>H', self.slot)
                + ip
                + struct.pack('>H', int(self.switcher_input or 0))
                + b'\x00' * 6)
        header = struct.pack('>H 2x 4s', len(data) + 8, self.CODE.encode())
        return header + data


# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def set_hyperdeck_settings(conn, *, slot, network_address=None,
                           switcher_input=None):
    """Set a HyperDeck binding's network address and/or switcher input (CXMS).

    Pass either or both; only provided fields are written. ``network_address``
    is a dotted-decimal IPv4 string (``'0.0.0.0'`` clears the slot).
    """
    conn.send(HyperdeckSettingsCommand(
        slot=slot, network_address=network_address,
        switcher_input=switcher_input))


# -----------------------------------------------------------------------------
# Readers
# -----------------------------------------------------------------------------


def hyperdeck_settings(mx, idx):
    """One HyperDeck slot's binding: ``{slot, network_address, input,
    configured}``. ``configured`` is False for an unbound slot (IP 0.0.0.0)."""
    node = (mx.get('hyperdeck-settings') or {}).get(idx)
    if node is None:
        return {'slot': idx, 'network_address': '0.0.0.0',
                'input': 0, 'configured': False}
    return {
        'slot': idx,
        'network_address': getattr(node, 'network_address', '0.0.0.0'),
        'input': safe_int(getattr(node, 'input', 0), 0),
        'configured': bool(getattr(node, 'configured', False)),
    }
