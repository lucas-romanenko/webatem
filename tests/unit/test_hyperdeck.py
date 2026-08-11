"""HyperDeck binding wire-format tests — CXMS set command + RXMS reader.

Layout reverse-engineered from a Software Control capture (addHyperdeck.pcap)
and validated live, 2026-06-09.
"""
import socket
import struct

from pyatem.messages.hyperdeck import (
    HyperdeckSettingsCommand, HyperdeckSettingsField, hyperdeck_settings,
)


def _payload(cmd):
    pkt = cmd.get_command()
    assert pkt[4:8] == b'CXMS'
    p = pkt[8:]
    assert len(p) == 16
    return p


def test_cxms_network_address_only():
    p = _payload(HyperdeckSettingsCommand(slot=1, network_address='10.20.30.40'))
    assert p[0] == 0x01                                   # mask: IP only
    assert struct.unpack_from('>H', p, 2)[0] == 1         # slot
    assert socket.inet_ntoa(p[4:8]) == '10.20.30.40'      # IP big-endian @4-7


def test_cxms_input_only():
    p = _payload(HyperdeckSettingsCommand(slot=2, switcher_input=8))
    assert p[0] == 0x02                                   # mask: input only
    assert struct.unpack_from('>H', p, 2)[0] == 2         # slot
    assert struct.unpack_from('>H', p, 8)[0] == 8         # input @8


def test_cxms_both_fields_set_both_mask_bits():
    p = _payload(HyperdeckSettingsCommand(
        slot=0, network_address='1.2.3.4', switcher_input=5))
    assert p[0] == 0x03                                   # IP + input
    assert socket.inet_ntoa(p[4:8]) == '1.2.3.4'
    assert struct.unpack_from('>H', p, 8)[0] == 5


def test_cxms_clear_slot_with_zero_ip():
    p = _payload(HyperdeckSettingsCommand(slot=4, network_address='0.0.0.0'))
    assert p[0] == 0x01
    assert p[4:8] == b'\x00\x00\x00\x00'


def _rxms_raw(slot, ip, inp):
    return (struct.pack('>H', slot) + b'\x00\x00' + socket.inet_aton(ip)
            + struct.pack('>H', inp) + b'\x00' * 10)


def test_rxms_reader_configured():
    node = HyperdeckSettingsField(_rxms_raw(0, '192.168.1.11', 4))
    hd = hyperdeck_settings({'hyperdeck-settings': {0: node}}, 0)
    assert hd == {'slot': 0, 'network_address': '192.168.1.11',
                  'input': 4, 'configured': True}


def test_rxms_reader_unconfigured_and_missing():
    node = HyperdeckSettingsField(_rxms_raw(1, '0.0.0.0', 0))
    hd = hyperdeck_settings({'hyperdeck-settings': {1: node}}, 1)
    assert hd['configured'] is False and hd['network_address'] == '0.0.0.0'
    # absent slot → safe default
    assert hyperdeck_settings({}, 7) == {
        'slot': 7, 'network_address': '0.0.0.0', 'input': 0, 'configured': False}
