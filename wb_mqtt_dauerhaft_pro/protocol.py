"""Wire protocol codec for Dauerhaft PRO RS-485 actuators.

Implements the "Profkarniz Dauerhaft PRO RS-485 v2.3" protocol: Modbus-RTU
framing (9600 8N1, T3.5 idle gaps) with a vendor-specific function set. This
module is pure (no I/O): it builds request frames and parses response frames so
it can be unit-tested against captured traffic.

Frame layout (both directions), section 2 of the spec::

    [address:1][function:1][data_length:1][data:N][crc16_lo:1][crc16_hi:1]

  * ``address``       -- 0x00..0xFF; 0x00 is the broadcast/universal address.
  * ``function``      -- see :class:`Function`.
  * ``data_length``   -- number of ``data`` bytes (N), one byte.
  * ``crc16``         -- CRC-16/Modbus (poly 0xA001, init 0xFFFF), low byte first.

Verified against the vendor spec, the running wb-rules driver and live bus
traffic on the test stand (CRC cross-checked by computation).

Spec quirks worth knowing:
  * Section 5 calls the checksum "CRC-8/MAXIM", but both the reference C code in
    the spec and the wire use CRC-16/Modbus. The label is a documentation bug.
  * Section 4 ("bus collision avoidance") is empty in the spec.
"""

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Sequence, Union


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

BROADCAST_ADDRESS = 0x00  # section 3.1.1: universal address, reaches every motor

POSITION_MIN = 0x00  # fully down / closed
POSITION_MAX = 0x64  # 100 %, fully up / open
ANGLE_MAX = 0xB4  # 180 degrees

MIN_FRAME_LEN = 5  # addr + func + len + at least the 2 CRC bytes (data may be empty)


class Function(IntEnum):
    """Wire function codes (spec section 3)."""

    ERROR = 0x00  # 3.6  error report from slave: data = [0xF0, code]
    QUERY = 0x01  # 3.2  read a value (subcommand in first data byte)
    SETTING = 0x02  # 3.3  change a setting (limits, direction, speed, ...)
    CONTROL = 0x04  # 3.4  motion control (move / stop / 3rd point / angle)
    ACTIVE_REPORT = 0x08  # 3.5  unsolicited state report: data = [position, state]
    GROUP = 0x09  # 3.7  group control
    SET_ADDRESS = 0x10  # 3.1  change device address


class QuerySub(IntEnum):
    """Subcommands for :attr:`Function.QUERY` (first data byte)."""

    ADDRESS = 0x01
    POSITION = 0x02
    STATE = 0x03
    ANGLE = 0x04
    FIRMWARE = 0x05


class ControlSub(IntEnum):
    """Subcommands for :attr:`Function.CONTROL` (first data byte)."""

    MOVE = 0x01  # value: 0x00 down, 0x01..0x63 position %, 0x64 up
    STOP = 0x02  # value: 0x00
    THIRD_POINT = 0x03  # value: 0x00
    ANGLE = 0x04  # value: 0x00..0xB4 degrees, 0xFE jog down, 0xFF jog up


class SettingSub(IntEnum):
    """Subcommands for :attr:`Function.SETTING` (first data byte)."""

    SET_UPPER_LIMIT = 0x01
    SET_LOWER_LIMIT = 0x02
    DELETE_LIMITS = 0x03
    CHANGE_DIRECTION = 0x04
    SET_THIRD_POINT = 0x05
    ENABLE_LIMIT_POINT = 0x06
    DISABLE_LIMIT_POINT = 0x07
    MANUAL_MODE_1 = 0x08
    MANUAL_MODE_2 = 0x09
    SLAVE_REPORT = 0x0A  # value: 0x00 disable, 0x01 enable
    SILENT_MODE = 0x0B  # value: 0x00 normal, 0x01 silent
    SPEED = 0x0C  # value: 0x01 / 0x02 / 0x03 = mode 1 / 2 / 3


class MotorState(IntEnum):
    """Motion state (query STATE data byte / active report second byte)."""

    STOPPED = 0x00
    MOVING_UP = 0x01
    MOVING_DOWN = 0x02


# Jog (small-step) angle values.
ANGLE_JOG_DOWN = 0xFE
ANGLE_JOG_UP = 0xFF

# Position-response markers (a value in this range is a status, not a percent).
POSITION_MARKERS = {
    0xFC: "both_limits_unset",
    0xFD: "lower_limit_unset",
    0xFE: "upper_limit_unset",
}
THIRD_POINT_UNSET = 0xF8

# Setting-response status bytes.
SETTING_OK = 0x0A
SETTING_FAIL = 0xA5

# Error report (function 0x00): data = [ERROR_MARKER, code].
ERROR_MARKER = 0xF0
ERROR_CODES = {
    0x02: "command code not supported",
    0x03: "command code / data error or unsupported",
}


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class ProtocolError(Exception):
    """Base class for protocol errors."""


class FrameError(ProtocolError):
    """Malformed frame (too short, or declared length inconsistent)."""


class CrcError(ProtocolError):
    """CRC check failed."""


# --------------------------------------------------------------------------- #
# CRC-16/Modbus
# --------------------------------------------------------------------------- #

def crc16_modbus(data: bytes) -> int:
    """Return the CRC-16/Modbus of *data* as a 16-bit int.

    Polynomial 0xA001 (reversed 0x8005), initial value 0xFFFF. On the wire the
    checksum is transmitted low byte first (see :func:`crc_bytes`).
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def crc_bytes(data: bytes) -> bytes:
    """Return the 2 CRC bytes for *data* in wire order (low, high)."""
    crc = crc16_modbus(data)
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


# --------------------------------------------------------------------------- #
# Frame build / parse
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Frame:
    """A decoded protocol frame (CRC already verified)."""

    address: int
    function: int
    data: bytes

    @property
    def subcommand(self) -> Optional[int]:
        """First data byte, echoed by query/setting/control responses."""
        return self.data[0] if self.data else None


def _check_byte(name: str, value: int) -> int:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must be a byte (0..255), got {value}")
    return value


def build_frame(address: int, function: int, data: Union[bytes, Sequence[int]] = b"") -> bytes:
    """Build a complete frame with CRC from *address*, *function* and *data*.

    ``data`` is the payload after the length byte (subcommand + arguments).
    The length byte is computed as ``len(data)``.
    """
    data = bytes(data)
    _check_byte("address", address)
    _check_byte("function", function)
    if len(data) > 0xFF:
        raise ValueError(f"data too long: {len(data)} bytes (max 255)")
    body = bytes([address, function, len(data)]) + data
    return body + crc_bytes(body)


def parse_frame(raw: Union[bytes, Sequence[int]]) -> Frame:
    """Parse and CRC-check a raw frame, returning a :class:`Frame`.

    Raises :class:`FrameError` on a malformed frame and :class:`CrcError` on a
    CRC mismatch.
    """
    raw = bytes(raw)
    if len(raw) < MIN_FRAME_LEN:
        raise FrameError(f"frame too short: {len(raw)} bytes (min {MIN_FRAME_LEN})")

    body, crc_recv = raw[:-2], raw[-2:]
    if crc_bytes(body) != crc_recv:
        raise CrcError(
            f"CRC mismatch: got {crc_recv.hex()}, expected {crc_bytes(body).hex()} "
            f"for {body.hex()}"
        )

    declared_len = raw[2]
    data = raw[3:-2]
    if declared_len != len(data):
        raise FrameError(
            f"length byte {declared_len} != actual data length {len(data)}"
        )
    return Frame(address=raw[0], function=raw[1], data=data)


# --------------------------------------------------------------------------- #
# Request builders
# --------------------------------------------------------------------------- #

def query(address: int, sub: QuerySub) -> bytes:
    """Build a query request (function 0x01) for *sub*."""
    return build_frame(address, Function.QUERY, bytes([sub]))


def query_address(address: int) -> bytes:
    """Query the device address. Also used as a liveness "ping"."""
    return query(address, QuerySub.ADDRESS)


def query_position(address: int) -> bytes:
    """Query the current position (0..100 %)."""
    return query(address, QuerySub.POSITION)


def query_state(address: int) -> bytes:
    """Query the motion state (stopped / up / down)."""
    return query(address, QuerySub.STATE)


def query_angle(address: int) -> bytes:
    """Query the slat angle (0..180 deg). Roller shutters answer with an error."""
    return query(address, QuerySub.ANGLE)


def query_firmware(address: int) -> bytes:
    """Query the firmware version (3 bytes)."""
    return query(address, QuerySub.FIRMWARE)


def control_move(address: int, position: int) -> bytes:
    """Move to *position* (0=down/closed .. 100=up/open)."""
    if not POSITION_MIN <= position <= POSITION_MAX:
        raise ValueError(f"position must be 0..100, got {position}")
    return build_frame(address, Function.CONTROL, bytes([ControlSub.MOVE, position]))


def control_open(address: int) -> bytes:
    """Fully open (up, position 100)."""
    return control_move(address, POSITION_MAX)


def control_close(address: int) -> bytes:
    """Fully close (down, position 0)."""
    return control_move(address, POSITION_MIN)


def control_stop(address: int) -> bytes:
    """Stop motion."""
    return build_frame(address, Function.CONTROL, bytes([ControlSub.STOP, 0x00]))


def control_third_point(address: int) -> bytes:
    """Move to the configured third point."""
    return build_frame(address, Function.CONTROL, bytes([ControlSub.THIRD_POINT, 0x00]))


def control_angle(address: int, degrees: int) -> bytes:
    """Set the slat angle to *degrees* (0..180)."""
    if not 0 <= degrees <= ANGLE_MAX:
        raise ValueError(f"angle must be 0..180, got {degrees}")
    return build_frame(address, Function.CONTROL, bytes([ControlSub.ANGLE, degrees]))


def control_jog_up(address: int) -> bytes:
    """Nudge the slat up by a small angle."""
    return build_frame(address, Function.CONTROL, bytes([ControlSub.ANGLE, ANGLE_JOG_UP]))


def control_jog_down(address: int) -> bytes:
    """Nudge the slat down by a small angle."""
    return build_frame(address, Function.CONTROL, bytes([ControlSub.ANGLE, ANGLE_JOG_DOWN]))


def setting(address: int, sub: SettingSub, value: Optional[int] = None) -> bytes:
    """Build a setting request (function 0x02).

    *value* is required for subcommands that take an argument (SLAVE_REPORT,
    SILENT_MODE, SPEED) and must be omitted for the rest.
    """
    data = bytes([sub]) if value is None else bytes([sub, _check_byte("value", value)])
    return build_frame(address, Function.SETTING, data)


def set_address(address: int, new_address: int) -> bytes:
    """Change the device address (function 0x10) to *new_address*.

    Send to a specific *address*, to :data:`BROADCAST_ADDRESS` (all motors), or
    to a motor put into independent-setting mode by its button (section 3.1).
    """
    _check_byte("new_address", new_address)
    return build_frame(address, Function.SET_ADDRESS, bytes([new_address]))


# --------------------------------------------------------------------------- #
# Response decoding
# --------------------------------------------------------------------------- #

@dataclass
class ErrorResponse:
    code: int
    description: str


@dataclass
class QueryResponse:
    sub: int
    # exactly one of the following is set depending on ``sub``:
    address: Optional[int] = None
    position: Optional[int] = None  # 0..100 %, or None if a marker is present
    position_marker: Optional[str] = None
    state: Optional[MotorState] = None
    angle: Optional[int] = None
    firmware: Optional[str] = None


@dataclass
class ControlResponse:
    sub: int
    position: Optional[int] = None  # echoed current position, 0..100 %
    position_marker: Optional[str] = None
    angle: Optional[int] = None


@dataclass
class SettingResponse:
    sub: int
    ok: bool
    value: int  # raw status/echo byte


@dataclass
class SetAddressResponse:
    address: int
    ok: bool


@dataclass
class ActiveReport:
    position: Optional[int]
    position_marker: Optional[str]
    state: MotorState


def _decode_position(value: int):
    """Return ``(percent_or_None, marker_or_None)`` for a position byte."""
    if value in POSITION_MARKERS:
        return None, POSITION_MARKERS[value]
    return value, None


def decode_firmware(data: bytes) -> str:
    """Decode a 3-byte firmware version.

    Per the spec example ``0x010302`` maps to ``2.3.1``: the three data bytes
    are little-endian, so the version reads as ``d[2].d[1].d[0]``.

    NOTE: derived from the (image-only) spec example; confirm against a live
    device once transport is in place (Stage 2).
    """
    if len(data) != 3:
        raise FrameError(f"firmware expects 3 bytes, got {len(data)}")
    return f"{data[2]}.{data[1]}.{data[0]}"


def decode_response(frame: Frame):
    """Interpret a parsed response *frame* into a typed dataclass.

    Returns one of :class:`ErrorResponse`, :class:`QueryResponse`,
    :class:`ControlResponse`, :class:`SettingResponse`,
    :class:`SetAddressResponse` or :class:`ActiveReport`.
    """
    func = frame.function
    data = frame.data

    if func == Function.ERROR:
        # data = [0xF0, code]
        code = data[1] if len(data) >= 2 else -1
        return ErrorResponse(code=code, description=ERROR_CODES.get(code, "unknown error"))

    if func == Function.QUERY:
        sub = data[0]
        payload = data[1:]
        resp = QueryResponse(sub=sub)
        if sub == QuerySub.ADDRESS:
            resp.address = payload[0]
        elif sub == QuerySub.POSITION:
            resp.position, resp.position_marker = _decode_position(payload[0])
        elif sub == QuerySub.STATE:
            resp.state = MotorState(payload[0])
        elif sub == QuerySub.ANGLE:
            resp.angle = payload[0]
        elif sub == QuerySub.FIRMWARE:
            resp.firmware = decode_firmware(payload)
        return resp

    if func == Function.CONTROL:
        sub = data[0]
        value = data[1] if len(data) >= 2 else None
        resp = ControlResponse(sub=sub)
        if sub == ControlSub.ANGLE:
            resp.angle = value
        elif value is not None:
            resp.position, resp.position_marker = _decode_position(value)
        return resp

    if func == Function.SETTING:
        sub = data[0]
        status = data[1] if len(data) >= 2 else -1
        return SettingResponse(sub=sub, ok=(status == SETTING_OK), value=status)

    if func == Function.SET_ADDRESS:
        # data = [address, 0x0A]
        return SetAddressResponse(address=data[0], ok=(len(data) >= 2 and data[1] == SETTING_OK))

    if func == Function.ACTIVE_REPORT:
        pos, marker = _decode_position(data[0])
        return ActiveReport(position=pos, position_marker=marker, state=MotorState(data[1]))

    raise ProtocolError(f"unknown response function 0x{func:02X}")


def parse_response(raw: Union[bytes, Sequence[int]]):
    """Convenience: :func:`parse_frame` + :func:`decode_response`."""
    return decode_response(parse_frame(raw))
