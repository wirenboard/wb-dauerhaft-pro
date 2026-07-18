"""
Actuator model unit tests: availability must obey the miss hysteresis, and the
learning/broadcast address writes must not distort it.
"""

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
    cfg = _make_config()
    actuator = Actuator(cfg, SilentTransport())
    actuator.online = True  # was answering before it went silent
    for _ in range(OFFLINE_AFTER_MISSES - 1):
        actuator.ping()
        assert actuator.online  # single bus collisions must not flap availability
    actuator.ping()
    assert not actuator.online  # the third consecutive miss drops it


def test_learning_write_accepts_any_reply_address_and_keeps_the_config():
    actuator = Actuator(_make_config(), AckFromNewAddressTransport())
    assert actuator.set_address_learning(0x5E) is True  # ack came from 0x5E, not 0x5F
    assert actuator.cfg.address == 0x5F  # the config stays the source of truth


def test_learning_timeouts_do_not_flap_availability():
    actuator = Actuator(_make_config(), SilentTransport())
    actuator.online = True
    for _ in range(OFFLINE_AFTER_MISSES + 1):
        # no motor in its learning window: a timeout is the expected outcome
        assert actuator.set_address_learning(0x5E) is False
    assert actuator.online
