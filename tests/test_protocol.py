"""
Codec unit tests: byte-exact checks against frames captured from the live
actuators on the test stand, plus the frame-validation failure modes.
"""

import pytest

from wb.dauerhaft_pro import protocol

# Frames captured on the stand (roller 0x5F, sliding curtain 0x0B).
PING_REPLY_5F = "5f0102015f5199"

BUILDERS = [
    (protocol.query_address(0x5F), "5f01010183a0"),
    (protocol.query_address(0x0B), "0b0101019390"),
    (protocol.control_up(0x5F), "5f040201641086"),
    (protocol.control_down(0x5F), "5f04020100116d"),
    (protocol.control_stop(0x5F), "5f04020200119d"),
    (protocol.set_address(0x5F, 0x5E), "5f10015e939d"),
]

DECODED = [
    (PING_REPLY_5F, protocol.QueryResponse, {"address": 0x5F}),
    ("0b0102010b61aa", protocol.QueryResponse, {"address": 0x0B}),
    ("5f040201fc112c", protocol.ControlResponse, {"value": 0xFC}),
    # the set-address ack comes FROM the new address (spec 3.1)
    ("5e10025e0a90aa", protocol.SetAddressResponse, {"address": 0x5E, "ok": True}),
    ("0b080261000bf1", protocol.ActiveReport, {}),
]


@pytest.mark.parametrize("built,captured", BUILDERS)
def test_builders_match_captured_requests(built, captured):
    assert built == bytes.fromhex(captured)


@pytest.mark.parametrize("raw,expected_type,fields", DECODED)
def test_captured_replies_decode(raw, expected_type, fields):
    resp = protocol.parse_response(bytes.fromhex(raw))
    assert isinstance(resp, expected_type)
    for name, value in fields.items():
        assert getattr(resp, name) == value


def test_error_response_decodes():
    raw = protocol.build_frame(0x5F, protocol.Function.ERROR, [protocol.ERROR_MARKER, 0x03])
    resp = protocol.parse_response(raw)
    assert isinstance(resp, protocol.ErrorResponse)
    assert resp.code == 0x03


def test_unknown_function_raises():
    with pytest.raises(protocol.ProtocolError, match="unknown response function"):
        protocol.parse_response(protocol.build_frame(0x5F, 0x77, b""))


def test_build_parse_round_trip():
    frame = protocol.parse_frame(protocol.build_frame(0x0B, protocol.Function.CONTROL, [0x01, 0x64]))
    assert (frame.address, frame.function, frame.data) == (0x0B, protocol.Function.CONTROL, b"\x01\x64")
    assert frame.subcommand == 0x01


VALID = bytes.fromhex(PING_REPLY_5F)


def test_too_short_frame_raises():
    with pytest.raises(protocol.FrameError, match="too short"):
        protocol.parse_frame(VALID[:4])


def test_frame_shorter_than_declared_raises():
    # a truncated read: the length byte promises more bytes than arrived
    with pytest.raises(protocol.FrameError, match="shorter than declared"):
        protocol.parse_frame(VALID[:-1])


def test_frame_longer_than_declared_is_trimmed(caplog):
    # trailing line junk after a complete frame: salvage the declared span
    resp = protocol.parse_response(VALID + b"\x00\xff")
    assert isinstance(resp, protocol.QueryResponse)
    assert resp.address == 0x5F
    assert "longer than declared" in caplog.text


def test_corrupted_byte_raises_crc_error():
    corrupted = bytearray(VALID)
    corrupted[3] ^= 0x01
    with pytest.raises(protocol.CrcError, match="CRC mismatch"):
        protocol.parse_frame(bytes(corrupted))


def test_trailing_junk_does_not_mask_a_corrupted_frame():
    corrupted = bytearray(VALID)
    corrupted[4] ^= 0x10
    with pytest.raises(protocol.CrcError):
        protocol.parse_frame(bytes(corrupted) + b"\x12")
