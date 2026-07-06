"""Unit tests for the MVP protocol codec (pure, no I/O)."""

import pytest
from wb_mqtt_dauerhaft_pro import protocol as p

# --------------------------------------------------------------------------- #
# CRC-16/Modbus
# --------------------------------------------------------------------------- #


def test_crc_bytes_low_high_order():
    # Body of a captured query-position frame to 0x5F: 5F 01 01 02
    # It was seen on the wire as 5F 01 01 02 C3 A1, i.e. CRC low byte first.
    assert p.crc_bytes(bytes.fromhex("5F010102")) == bytes.fromhex("C3A1")


def test_crc16_value():
    assert p.crc16_modbus(bytes.fromhex("5F010102")) == 0xA1C3


# --------------------------------------------------------------------------- #
# frame build / parse
# --------------------------------------------------------------------------- #


def test_build_frame_appends_length_and_crc():
    frame = p.build_frame(0x5F, p.Function.QUERY, bytes([0x02]))
    assert frame == bytes.fromhex("5F010102C3A1")


def test_parse_captured_frame_round_trips():
    frame = p.parse_frame(bytes.fromhex("5F010102C3A1"))
    assert frame.address == 0x5F
    assert frame.function == p.Function.QUERY
    assert frame.data == bytes([0x02])
    assert frame.subcommand == 0x02


def test_parse_rejects_bad_crc():
    with pytest.raises(p.CrcError):
        p.parse_frame(bytes.fromhex("5F010102FFFF"))


def test_parse_rejects_short_frame():
    with pytest.raises(p.FrameError):
        p.parse_frame(bytes.fromhex("5F0101"))


def test_parse_rejects_length_mismatch():
    # length byte says 5 data bytes, but only 1 present; fix CRC so we reach the
    # length check rather than failing on CRC first.
    body = bytes([0x5F, 0x01, 0x05, 0x02])
    with pytest.raises(p.FrameError):
        p.parse_frame(body + p.crc_bytes(body))


def test_build_frame_rejects_out_of_range_address():
    with pytest.raises(ValueError):
        p.build_frame(0x100, p.Function.CONTROL, b"")


# --------------------------------------------------------------------------- #
# request builders (MVP: address + up/down + stop)
# --------------------------------------------------------------------------- #


def test_control_up_is_move_0x64():
    frame = p.parse_frame(p.control_up(0x5F))
    assert frame.function == p.Function.CONTROL
    assert frame.data == bytes([p.ControlSub.MOVE, 0x64])


def test_control_down_is_move_0x00():
    frame = p.parse_frame(p.control_down(0x5F))
    assert frame.function == p.Function.CONTROL
    assert frame.data == bytes([p.ControlSub.MOVE, 0x00])


def test_control_stop():
    frame = p.parse_frame(p.control_stop(0x5F))
    assert frame.function == p.Function.CONTROL
    assert frame.data == bytes([p.ControlSub.STOP, 0x00])


def test_query_address():
    frame = p.parse_frame(p.query_address(0x5F))
    assert frame.function == p.Function.QUERY
    assert frame.data == bytes([p.QuerySub.ADDRESS])


def test_set_address_builds_function_0x10():
    frame = p.parse_frame(p.set_address(0x5F, 0x5E))
    assert frame.function == p.Function.SET_ADDRESS
    assert frame.data == bytes([0x5E])


def test_set_address_rejects_bad_new_address():
    with pytest.raises(ValueError):
        p.set_address(0x5F, 0x100)


# --------------------------------------------------------------------------- #
# response decoding
# --------------------------------------------------------------------------- #


def test_decode_query_address_response():
    raw = p.build_frame(0x5F, p.Function.QUERY, bytes([p.QuerySub.ADDRESS, 0x5F]))
    resp = p.parse_response(raw)
    assert isinstance(resp, p.QueryResponse)
    assert resp.address == 0x5F


def test_decode_set_address_ok_from_new_address():
    # The device answers from the NEW address with status 0x0A (spec 3.1).
    raw = p.build_frame(0x5E, p.Function.SET_ADDRESS, bytes([0x5E, p.SETTING_OK]))
    resp = p.parse_response(raw)
    assert isinstance(resp, p.SetAddressResponse)
    assert resp.address == 0x5E
    assert resp.ok is True


def test_decode_set_address_not_ok():
    raw = p.build_frame(0x5E, p.Function.SET_ADDRESS, bytes([0x5E, 0x00]))
    resp = p.parse_response(raw)
    assert isinstance(resp, p.SetAddressResponse)
    assert resp.ok is False


def test_decode_control_echo():
    raw = p.build_frame(0x5F, p.Function.CONTROL, bytes([p.ControlSub.MOVE, 0x64]))
    resp = p.parse_response(raw)
    assert isinstance(resp, p.ControlResponse)
    assert resp.sub == p.ControlSub.MOVE
    assert resp.value == 0x64


def test_decode_error_response():
    raw = p.build_frame(0x5F, p.Function.ERROR, bytes([p.ERROR_MARKER, 0x03]))
    resp = p.parse_response(raw)
    assert isinstance(resp, p.ErrorResponse)
    assert resp.code == 0x03


def test_decode_unknown_function_raises():
    raw = p.build_frame(0x5F, 0x42, bytes([0x00]))
    with pytest.raises(p.ProtocolError):
        p.parse_response(raw)
