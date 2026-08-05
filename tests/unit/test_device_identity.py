"""``WhoI`` device-identity parse + reader.

Payload layout captured 2026-08-04 from an ATEM 1 M/E Constellation HD:
32-char hex device id, NUL-padded IP (16), mDNS hostname (64), and the
custom name set in ATEM Setup (64) — 176 bytes total.
"""
from pyatem.messages import RECV_BY_CODE
from pyatem.messages.system_info import (
    DeviceIdentityField,
    device_name,
    product_name,
)


def _pad(s, size):
    b = s.encode()
    assert len(b) <= size
    return b + b'\x00' * (size - len(b))


CAPTURED = (
    b'7665f2ed6f4042e6bbcbbe17ef0143e7'
    + _pad('192.168.81.85', 16)
    + _pad('ATEM-TEST-2-AV-OFFICE.local', 64)
    + _pad('ATEM TEST 2 - AV OFFICE', 64)
)


def test_payload_shape():
    assert len(CAPTURED) == 176


def test_parse_captured_payload():
    field = DeviceIdentityField(CAPTURED)
    assert field.device_id == '7665f2ed6f4042e6bbcbbe17ef0143e7'
    assert field.ip == '192.168.81.85'
    assert field.hostname == 'ATEM-TEST-2-AV-OFFICE.local'
    assert field.name == 'ATEM TEST 2 - AV OFFICE'


def test_recv_auto_registered():
    assert RECV_BY_CODE['WhoI'] is DeviceIdentityField


def test_device_name_reader():
    mx = {'device-identity': DeviceIdentityField(CAPTURED)}
    assert device_name(mx) == 'ATEM TEST 2 - AV OFFICE'


def test_device_name_absent_falls_back_to_default():
    # Older firmware never sends WhoI — the reader must default cleanly so
    # callers can fall back to product_name.
    assert device_name({}) == ''
    assert device_name({}, default=None) is None
