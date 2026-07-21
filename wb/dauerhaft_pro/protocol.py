"""
Wire protocol codec for Dauerhaft PRO RS-485 actuators (supported subset).

Implements just enough of the "Profkarniz Dauerhaft PRO RS-485 v2.3" protocol
for the driver: set the device address and drive the motor (up / down /
stop). This module is pure (no I/O) so it can be unit-tested against captured
frames.

Frame layout (both directions), section 2 of the spec::

    [address:1][function:1][data_length:1][data:N][crc16_lo:1][crc16_hi:1]

  * ``address``     -- 0x00..0xFF; 0x00 is the broadcast/universal address.
  * ``function``    -- see :class:`Function`.
  * ``data_length`` -- number of ``data`` bytes (N), one byte.
  * ``crc16``       -- CRC-16/Modbus (poly 0xA001, init 0xFFFF), low byte first.

Verified against the vendor spec and live bus traffic on the test stand (CRC
cross-checked by computation).

Spec quirk: section 5 calls the checksum "CRC-8/MAXIM", but both the reference C
code in the spec and the wire use CRC-16/Modbus. The label is a documentation bug.
"""

import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Dict, Optional, Sequence, Union

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

BROADCAST_ADDRESS = 0x00  # section 3.1.1: universal address, reaches every motor

POSITION_DOWN = 0x00  # fully down / closed
POSITION_UP = 0x64  # 100 %, fully up / open

MIN_FRAME_LEN = 5  # addr + func + len + the 2 CRC bytes (data may be empty)

SETTING_OK = 0x0A  # positive status byte in a set-address reply
ERROR_MARKER = 0xF0  # first data byte of an error report (function 0x00)


class Function(IntEnum):
    """
    Wire function codes (spec section 3), supported subset.
    """

    ERROR = 0x00  # 3.6  error report from slave: data = [0xF0, code]
    QUERY = 0x01  # 3.2  read a value (subcommand in first data byte)
    CONTROL = 0x04  # 3.4  motion control (move / stop)
    ACTIVE_REPORT = 0x08  # 3.5  unsolicited state report on movement (default-on on some motors)
    SET_ADDRESS = 0x10  # 3.1  change device address


class QuerySub(IntEnum):
    """
    Subcommands for :attr:`Function.QUERY` (first data byte).
    """

    ADDRESS = 0x01  # read the device's own address (also used as a liveness ping)


class ControlSub(IntEnum):
    """
    Subcommands for :attr:`Function.CONTROL` (first data byte).
    """

    MOVE = 0x01  # value: 0x00 down, 0x64 up
    STOP = 0x02  # value: 0x00


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class ProtocolError(Exception):
    """
    Base class for protocol errors.
    """


class FrameError(ProtocolError):
    """
    Malformed frame (too short, or declared length inconsistent).
    """


class CrcError(ProtocolError):
    """
    CRC check failed.
    """


# --------------------------------------------------------------------------- #
# CRC-16/Modbus
# --------------------------------------------------------------------------- #


def crc16_modbus(data: bytes) -> int:
    """
    Return the CRC-16/Modbus of *data* as a 16-bit int.

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
    """
    Return the 2 CRC bytes for *data* in wire order (low, high).
    """
    crc = crc16_modbus(data)
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


# --------------------------------------------------------------------------- #
# Frame build / parse
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Frame:
    """
    A decoded protocol frame (CRC already verified).
    """

    address: int
    function: int
    data: bytes

    @property
    def subcommand(self) -> Optional[int]:
        """
        First data byte, echoed by query/control responses.
        """
        return self.data[0] if self.data else None


def _check_byte(name: str, value: int) -> int:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must be a byte (0..255), got {value}")
    return value


def build_frame(address: int, function: int, data: Union[bytes, Sequence[int]] = b"") -> bytes:
    """
    Build a complete frame with CRC from *address*, *function* and *data*.

    ``data`` is the payload after the length byte (subcommand + arguments); the
    length byte is computed as ``len(data)``.
    """
    data = bytes(data)
    _check_byte("address", address)
    _check_byte("function", function)
    if len(data) > 0xFF:
        raise ValueError(f"data too long: {len(data)} bytes (max 255)")
    body = bytes([address, function, len(data)]) + data
    return body + crc_bytes(body)


def parse_frame(raw: Union[bytes, Sequence[int]]) -> Frame:
    """
    Parse and validate a raw frame, returning a :class:`Frame`.

    The length is validated before the CRC so that each failure mode on a
    noisy line gets its own diagnostic instead of everything surfacing as a
    CRC mismatch: a frame shorter than its length byte declares (truncated
    read) and corrupted bits (real CRC error) raise distinct errors, while
    trailing junk after a complete frame is trimmed with a warning and the
    declared span is CRC-checked on its own.

    Raises :class:`FrameError` when the frame is shorter than the minimum or
    shorter than declared, :class:`CrcError` on a CRC mismatch.
    """
    raw = bytes(raw)
    if len(raw) < MIN_FRAME_LEN:
        raise FrameError(f"frame too short: {len(raw)} bytes (min {MIN_FRAME_LEN})")

    expected = MIN_FRAME_LEN + raw[2]  # header (3) + declared data bytes + CRC (2)
    if len(raw) < expected:
        raise FrameError(
            f"frame shorter than declared: got {len(raw)} bytes, length byte promises {expected}"
        )
    if len(raw) > expected:
        logger.warning(
            "frame longer than declared (%d > %d bytes), trimming trailing junk: %s",
            len(raw),
            expected,
            raw.hex(),
        )
        raw = raw[:expected]

    body, crc_recv = raw[:-2], raw[-2:]
    if crc_bytes(body) != crc_recv:
        raise CrcError(
            f"CRC mismatch: got {crc_recv.hex()}, expected {crc_bytes(body).hex()} " f"for {body.hex()}"
        )
    return Frame(address=raw[0], function=raw[1], data=raw[3:-2])


# --------------------------------------------------------------------------- #
# Request builders (address + move up/down + stop)
# --------------------------------------------------------------------------- #


def query_address(address: int) -> bytes:
    """
    Read the device's own address. Doubles as a liveness "ping".
    """
    return build_frame(address, Function.QUERY, bytes([QuerySub.ADDRESS]))


def control_up(address: int) -> bytes:
    """
    Drive the actuator up / open (move, value 0x64).
    """
    return build_frame(address, Function.CONTROL, bytes([ControlSub.MOVE, POSITION_UP]))


def control_down(address: int) -> bytes:
    """
    Drive the actuator down / close (move, value 0x00).
    """
    return build_frame(address, Function.CONTROL, bytes([ControlSub.MOVE, POSITION_DOWN]))


def control_stop(address: int) -> bytes:
    """
    Stop motion (stop, value 0x00).
    """
    return build_frame(address, Function.CONTROL, bytes([ControlSub.STOP, 0x00]))


def set_address(address: int, new_address: int) -> bytes:
    """
    Change the device address (function 0x10) to *new_address*.

    Sent to the device's current *address* (unicast), to
    :data:`BROADCAST_ADDRESS` (all motors), or to a motor put into
    independent-setting mode by its button (section 3.1).
    """
    _check_byte("new_address", new_address)
    return build_frame(address, Function.SET_ADDRESS, bytes([new_address]))


# --------------------------------------------------------------------------- #
# Response decoding (supported subset)
# --------------------------------------------------------------------------- #


@dataclass
class ErrorResponse:
    code: int


@dataclass
class QueryResponse:
    sub: Optional[int]
    address: Optional[int] = None


@dataclass
class ControlResponse:
    sub: Optional[int]
    value: Optional[int] = None  # echoed status/position byte


@dataclass
class SetAddressResponse:
    address: Optional[int]  # the reply comes FROM the new address (spec 3.1)
    ok: bool


@dataclass
class ActiveReport:
    """
    An unsolicited movement report (function 0x08). Recognized so it does not
    raise, but the driver treats it as a stray frame, not a command reply.
    """

    data: bytes


# Any decoded response. The alias annotates what decode_response can return.
Response = Union[ErrorResponse, QueryResponse, ControlResponse, SetAddressResponse, ActiveReport]


def _decode_error(frame: Frame) -> ErrorResponse:
    """
    Decode an error report: data = [0xF0, code].
    """
    return ErrorResponse(code=frame.data[1] if len(frame.data) >= 2 else -1)


def _decode_query(frame: Frame) -> QueryResponse:
    """
    Decode a query response; the address is present for the ADDRESS subcommand.
    """
    addr = frame.data[1] if len(frame.data) >= 2 and frame.subcommand == QuerySub.ADDRESS else None
    return QueryResponse(sub=frame.subcommand, address=addr)


def _decode_control(frame: Frame) -> ControlResponse:
    """
    Decode a control echo: subcommand plus the echoed status/position byte.
    """
    return ControlResponse(sub=frame.subcommand, value=frame.data[1] if len(frame.data) >= 2 else None)


def _decode_set_address(frame: Frame) -> SetAddressResponse:
    """
    Decode a set-address ack: data = [address, 0x0A on success].
    """
    return SetAddressResponse(
        address=frame.subcommand,
        ok=len(frame.data) >= 2 and frame.data[1] == SETTING_OK,
    )


def _decode_active_report(frame: Frame) -> ActiveReport:
    """
    Wrap an unsolicited active report as-is.
    """
    return ActiveReport(data=frame.data)


DECODERS: Dict[int, Callable[[Frame], Response]] = {
    Function.ERROR: _decode_error,
    Function.QUERY: _decode_query,
    Function.CONTROL: _decode_control,
    Function.SET_ADDRESS: _decode_set_address,
    Function.ACTIVE_REPORT: _decode_active_report,
}


def decode_response(frame: Frame) -> Response:
    """
    Interpret a parsed response *frame* into a typed dataclass via DECODERS.
    """
    decoder = DECODERS.get(frame.function)
    if not decoder:
        raise ProtocolError(f"unknown response function 0x{frame.function:02X}")
    return decoder(frame)


def parse_response(raw: Union[bytes, Sequence[int]]) -> Response:
    """
    Convenience: :func:`parse_frame` + :func:`decode_response`.
    """
    return decode_response(parse_frame(raw))
