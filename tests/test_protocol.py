"""
Codec unit tests: byte-exact checks against frames captured from the live
actuators on the test stand, plus the frame-validation failure modes.
"""

import logging

import pytest

from wb.dauerhaft_pro import protocol

# Frames captured on the stand (roller 0x5F, sliding curtain 0x0B).
PING_REPLY_5F = "5f0102015f5199"
VALID_REPLY = bytes.fromhex(PING_REPLY_5F)

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
    ("0b080261000bf1", protocol.ActiveReport, {"data": bytes.fromhex("6100")}),
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


def test_set_address_nack_decodes_not_ok():
    # a status byte other than 0x0A means the device refused the change
    raw = protocol.build_frame(0x5E, protocol.Function.SET_ADDRESS, [0x5E, 0x00])
    resp = protocol.parse_response(raw)
    assert isinstance(resp, protocol.SetAddressResponse)
    assert resp.ok is False
    assert resp.address == 0x5E


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


def test_empty_data_frame_decodes_gracefully():
    # subcommand must be None (not an IndexError) and the decoders must
    # tolerate a payload shorter than they would like
    frame = protocol.parse_frame(protocol.build_frame(0x5F, protocol.Function.QUERY, b""))
    assert frame.subcommand is None
    resp = protocol.decode_response(frame)
    assert isinstance(resp, protocol.QueryResponse)
    assert resp.address is None


@pytest.mark.parametrize(
    "truncated,message",
    [(VALID_REPLY[:4], "too short"), (VALID_REPLY[:-1], "shorter than declared")],
    ids=["below-minimum", "shorter-than-declared"],
)
def test_truncated_frames_raise_frame_error(truncated, message):
    with pytest.raises(protocol.FrameError, match=message):
        protocol.parse_frame(truncated)


def test_frame_longer_than_declared_is_trimmed(caplog):
    # trailing line junk after a complete frame: salvage the declared span
    caplog.set_level(logging.WARNING, logger="wb.dauerhaft_pro.protocol")
    resp = protocol.parse_response(VALID_REPLY + b"\x00\xff")
    assert isinstance(resp, protocol.QueryResponse)
    assert resp.address == 0x5F
    assert "longer than declared" in caplog.text


@pytest.mark.parametrize(
    "index,suffix",
    [(3, b""), (4, b"\x12")],
    ids=["corrupted-byte", "corrupted-byte-with-trailing-junk"],
)
def test_corrupted_frames_raise_crc_error(index, suffix):
    corrupted = bytearray(VALID_REPLY)
    corrupted[index] ^= 0x10
    with pytest.raises(protocol.CrcError, match="CRC mismatch"):
        protocol.parse_frame(bytes(corrupted) + suffix)
