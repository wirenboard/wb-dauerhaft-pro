"""
Actuator model unit tests: the miss hysteresis, and the count_misses contract —
telemetry reads and learning writes stay liveness-neutral (they neither drop nor
raise availability), a query reply to a different subcommand is rejected, and an
address write refuses the reserved/out-of-range targets.
"""

import pytest

from wb.dauerhaft_pro import protocol
from wb.dauerhaft_pro.device import OFFLINE_AFTER_MISSES, Actuator, ActuatorConfig
from wb.dauerhaft_pro.transport import DeviceTimeout, PortConfig


class SilentTransport:
    def transceive(self, *_args, **_kwargs):
        raise DeviceTimeout("device stayed silent")


class AckFromNewAddressTransport:
    """
    Answers a set-address write with an ack sent FROM the motor's new address.
    """

    def transceive(self, _port, request, _size, **_kwargs):
        new_address = request[3]
        return protocol.build_frame(
            new_address, protocol.Function.SET_ADDRESS, bytes([new_address, protocol.SETTING_OK])
        )


class ReplyTransport:
    """
    Always returns the same pre-built reply frame.
    """

    def __init__(self, frame):
        self._frame = frame

    def transceive(self, *_args, **_kwargs):
        return self._frame


def _make_config():
    return ActuatorConfig(
        device_id="a",
        name="a",
        curtain_type="roller",
        learning_type="physical_button",
        address=0x5F,
        port=PortConfig(path="/dev/ttyRS485-2"),
    )


def test_offline_only_after_consecutive_misses():
    """
    Availability drops only after 3 consecutive ping misses; one or two do not
    flap it.
    """
    actuator = Actuator(_make_config(), SilentTransport())
    actuator.online = True  # was answering before it went silent
    for _ in range(OFFLINE_AFTER_MISSES - 1):
        actuator.ping()
        assert actuator.online  # single bus collisions must not flap availability
    actuator.ping()
    assert not actuator.online  # the third consecutive miss drops it


def test_telemetry_reads_do_not_flap_availability():
    """
    Telemetry reads are liveness-neutral: repeated silent position/angle reads
    do not drop an online device (only the ping drives the hysteresis).
    """
    actuator = Actuator(_make_config(), SilentTransport())
    actuator.online = True
    for _ in range(OFFLINE_AFTER_MISSES + 2):
        assert actuator.query_position() is None
        assert actuator.query_angle_raw() is None
    assert actuator.online


def test_learning_write_is_liveness_neutral_and_keeps_the_config():
    """
    A learning write accepts the ack from any address, does NOT flip this device
    online (the ack proves some motor took the address, not that this one is
    alive), and does not move the stored config.
    """
    actuator = Actuator(_make_config(), AckFromNewAddressTransport())
    assert actuator.online is False
    assert actuator.set_address_learning(0x5E) is True  # ack came from 0x5E, not 0x5F
    assert actuator.cfg.address == 0x5F  # config stays the source of truth
    assert actuator.online is False  # a learning ack must not mark THIS device online


def test_learning_timeouts_do_not_flap_availability():
    """
    An expected learning-write timeout (no motor in its window) is not counted
    as an availability miss.
    """
    actuator = Actuator(_make_config(), SilentTransport())
    actuator.online = True
    for _ in range(OFFLINE_AFTER_MISSES + 1):
        assert actuator.set_address_learning(0x5E) is False
    assert actuator.online


def test_query_position_rejects_a_reply_to_a_different_subcommand():
    """
    A position read returns the value only for a POSITION reply; a delayed
    ADDRESS reply (same function, same address) is rejected as None — otherwise
    the address byte would surface as a position.
    """
    cfg = _make_config()
    position_reply = protocol.build_frame(
        cfg.address,
        protocol.Function.QUERY,
        bytes([protocol.QuerySub.POSITION, protocol.POSITION_BOTH_LIMITS_UNSET]),
    )
    address_reply = protocol.build_frame(
        cfg.address, protocol.Function.QUERY, bytes([protocol.QuerySub.ADDRESS, cfg.address])
    )
    assert (
        Actuator(cfg, ReplyTransport(position_reply)).query_position() == protocol.POSITION_BOTH_LIMITS_UNSET
    )
    assert Actuator(cfg, ReplyTransport(address_reply)).query_position() is None


def test_address_change_refuses_reserved_and_out_of_range():
    """
    Assigning the reserved broadcast/learning addresses, or an out-of-range
    value, raises ValueError instead of writing a bad address.
    """
    actuator = Actuator(_make_config(), SilentTransport())
    for reserved in (protocol.BROADCAST_ADDRESS, protocol.LEARNING_ADDRESS):
        with pytest.raises(ValueError, match="reserved"):
            actuator.set_address(reserved)
    with pytest.raises(ValueError, match="1..254"):
        actuator.set_address(300)
