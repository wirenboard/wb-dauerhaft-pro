"""Unit tests for the Dauerhaft PRO wire protocol codec.

Test vectors are grounded in real data:

  * Frames captured live on the test stand (device 0x5F "Штора с кнопкой"),
    each independently CRC-checked. These are the strongest ground truth.
  * Frames from section 6 of the vendor spec whose CRC was cross-verified by
    computation (OCR-ambiguous examples were dropped).
"""

import pytest

from wb_mqtt_dauerhaft_pro import protocol as p


def hx(s: str) -> bytes:
    return bytes.fromhex(s)


# --------------------------------------------------------------------------- #
# CRC-16/Modbus
# --------------------------------------------------------------------------- #

# (payload_without_crc, wire_crc_low, wire_crc_high) from verified frames.
CRC_VECTORS = [
    ("5f010104", 0x43, 0xA3),  # live: query angle
    ("5f010102", 0xC3, 0xA1),  # live: query position
    ("5f010101", 0x83, 0xA0),  # live: query address (ping)
    ("0b010101", 0x93, 0x90),  # live: query address on 0x0B
    ("5f0002f003", 0x15, 0xCC),  # live: error response
    ("5f010202fc", 0x11, 0x10),  # live: position response (limits unset)
    ("5f0102015f", 0x51, 0x99),  # live: address response
    ("5604020164", 0xCC, 0x87),  # spec: control up, addr 0x56
    ("8804020100", 0x65, 0x7F),  # spec: control down, addr 0x88 (CRC corrected)
    ("56020104", 0xB0, 0x3F),  # spec: change direction, addr 0x56
    ("ff100188", 0x30, 0x03),  # spec: set address ff -> 0x88
    ("881002880a", 0x87, 0x18),  # spec: set-address response
]


@pytest.mark.parametrize("payload,lo,hi", CRC_VECTORS)
def test_crc16_modbus(payload, lo, hi):
    crc = p.crc16_modbus(hx(payload))
    assert crc & 0xFF == lo
    assert (crc >> 8) & 0xFF == hi
    assert p.crc_bytes(hx(payload)) == bytes([lo, hi])


# --------------------------------------------------------------------------- #
# Request builders vs captured / verified frames
# --------------------------------------------------------------------------- #

def test_query_builders_match_live_frames():
    assert p.query_angle(0x5F) == hx("5F01010443A3")
    assert p.query_position(0x5F) == hx("5F010102C3A1")
    assert p.query_address(0x5F) == hx("5F01010183A0")
    assert p.query_address(0x0B) == hx("0B0101019390")


def test_control_builders_match_spec_examples():
    assert p.control_open(0x56) == hx("5604020164CC87")
    assert p.control_move(0x88, 0) == hx("8804020100657F")
    assert p.control_close(0x88) == hx("8804020100657F")


def test_setting_and_address_builders_match_spec_examples():
    assert p.setting(0x56, p.SettingSub.CHANGE_DIRECTION) == hx("56020104B03F")
    assert p.set_address(0xFF, 0x88) == hx("FF1001883003")


def test_build_frame_length_and_structure():
    frame = p.build_frame(0x5F, p.Function.QUERY, [p.QuerySub.POSITION])
    assert frame[0] == 0x5F  # address
    assert frame[1] == 0x01  # function
    assert frame[2] == 0x01  # length = len(data)
    assert frame[3] == 0x02  # data (subcommand)


def test_build_frame_rejects_bad_input():
    with pytest.raises(ValueError):
        p.build_frame(0x100, p.Function.QUERY, b"\x01")  # address not a byte
    with pytest.raises(ValueError):
        p.control_move(0x5F, 101)  # position out of range
    with pytest.raises(ValueError):
        p.control_angle(0x5F, 181)  # angle out of range


# --------------------------------------------------------------------------- #
# Frame parsing / CRC validation
# --------------------------------------------------------------------------- #

def test_parse_frame_roundtrip():
    frame = p.parse_frame(hx("5F010102C3A1"))
    assert frame.address == 0x5F
    assert frame.function == p.Function.QUERY
    assert frame.data == b"\x02"
    assert frame.subcommand == 0x02


def test_parse_frame_bad_crc():
    with pytest.raises(p.CrcError):
        p.parse_frame(hx("5F010102C3A2"))  # last byte flipped


def test_parse_frame_too_short():
    with pytest.raises(p.FrameError):
        p.parse_frame(hx("5F0102"))


def test_parse_frame_length_mismatch():
    # declared length 0x05 but only 1 data byte present; rebuild CRC so only the
    # length check can fail.
    body = bytes([0x5F, 0x01, 0x05, 0x02])
    raw = body + p.crc_bytes(body)
    with pytest.raises(p.FrameError):
        p.parse_frame(raw)


# --------------------------------------------------------------------------- #
# Response decoding vs captured frames
# --------------------------------------------------------------------------- #

def test_decode_position_response_limits_unset():
    resp = p.parse_response(hx("5F010202FC1110"))
    assert isinstance(resp, p.QueryResponse)
    assert resp.sub == p.QuerySub.POSITION
    assert resp.position is None
    assert resp.position_marker == "both_limits_unset"


def test_decode_position_response_percent():
    # synthesised: position 0x12 = 18 %
    raw = p.build_frame(0x5F, p.Function.QUERY, [p.QuerySub.POSITION, 0x12])
    resp = p.parse_response(raw)
    assert resp.position == 18
    assert resp.position_marker is None


def test_decode_address_response():
    resp = p.parse_response(hx("5F0102015F5199"))
    assert isinstance(resp, p.QueryResponse)
    assert resp.sub == p.QuerySub.ADDRESS
    assert resp.address == 0x5F


def test_decode_error_response():
    resp = p.parse_response(hx("5F0002F00315CC"))
    assert isinstance(resp, p.ErrorResponse)
    assert resp.code == 0x03
    assert "error" in resp.description.lower() or "unsupported" in resp.description.lower()


def test_decode_state_response():
    raw = p.build_frame(0x5F, p.Function.QUERY, [p.QuerySub.STATE, p.MotorState.MOVING_UP])
    resp = p.parse_response(raw)
    assert resp.state == p.MotorState.MOVING_UP


def test_decode_firmware_response():
    # spec example: raw bytes 01 03 02 -> version 2.3.1
    raw = p.build_frame(0x5F, p.Function.QUERY, [p.QuerySub.FIRMWARE, 0x01, 0x03, 0x02])
    resp = p.parse_response(raw)
    assert resp.firmware == "2.3.1"


def test_decode_setting_response_ok():
    raw = p.build_frame(0x56, p.Function.SETTING, [p.SettingSub.CHANGE_DIRECTION, p.SETTING_OK])
    resp = p.parse_response(raw)
    assert isinstance(resp, p.SettingResponse)
    assert resp.ok is True


def test_decode_set_address_response():
    resp = p.parse_response(hx("881002880A8718"))
    assert isinstance(resp, p.SetAddressResponse)
    assert resp.address == 0x88
    assert resp.ok is True


def test_decode_control_response_reports_position():
    raw = p.build_frame(0x88, p.Function.CONTROL, [p.ControlSub.MOVE, 0x2A])
    resp = p.parse_response(raw)
    assert isinstance(resp, p.ControlResponse)
    assert resp.position == 0x2A


def test_decode_active_report():
    raw = p.build_frame(0x5F, p.Function.ACTIVE_REPORT, [0x32, p.MotorState.MOVING_DOWN])
    resp = p.parse_response(raw)
    assert isinstance(resp, p.ActiveReport)
    assert resp.position == 50
    assert resp.state == p.MotorState.MOVING_DOWN
