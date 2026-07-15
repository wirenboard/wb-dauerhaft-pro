"""
Actuator model unit tests: availability must obey the miss hysteresis.
"""

from wb.dauerhaft_pro.device import OFFLINE_AFTER_MISSES, Actuator, ActuatorConfig
from wb.dauerhaft_pro.transport import DeviceTimeout, PortConfig


class SilentTransport:
    def transceive(self, *_args, **_kwargs):
        raise DeviceTimeout("device stayed silent")


def test_offline_only_after_consecutive_misses():
    cfg = ActuatorConfig(
        device_id="a",
        name="a",
        curtain_type="roller",
        learning_type="physical_button",
        address=0x5F,
        port=PortConfig(path="/dev/ttyRS485-2"),
    )
    actuator = Actuator(cfg, SilentTransport())
    actuator.online = True  # was answering before it went silent
    for _ in range(OFFLINE_AFTER_MISSES - 1):
        actuator.ping()
        assert actuator.online  # single bus collisions must not flap availability
    actuator.ping()
    assert not actuator.online  # the third consecutive miss drops it
