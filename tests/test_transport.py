"""
Transport unit tests: wb-mqtt-serial RPC errors must be classified into
"the device stayed silent" versus a real transport fault.
"""

import pytest
from mqttrpc.client import MQTTRPCError

from wb.dauerhaft_pro.transport import (
    DeviceTimeout,
    PortConfig,
    SerialTransport,
    TransportError,
)

PORT = PortConfig(path="/dev/ttyRS485-2")


class FailingRpc:
    def __init__(self, error):
        self._error = error

    def call(self, *_args, **_kwargs):
        raise self._error


@pytest.mark.parametrize(
    "code,data,expected",
    [
        (-32000, "Port IO error: Serial protocol error: request timed out", DeviceTimeout),
        (-32100, "RPC request timeout", DeviceTimeout),
        (-32600, "RPC request timeout", DeviceTimeout),
        (-32000, "Port IO error: /dev/ttyNOPE, can't open serial port", TransportError),
    ],
    ids=["silent-device", "rpc-timeout-legacy-code", "rpc-queue-expiry", "real-port-fault"],
)
def test_rpc_errors_are_classified(code, data, expected):
    """
    Timeouts (-32000 "timed out", -32100, -32600) map to DeviceTimeout; a real
    port fault maps to TransportError, not "device stayed silent".
    """
    transport = SerialTransport(FailingRpc(MQTTRPCError("Server error", code, data)))
    with pytest.raises(expected) as excinfo:
        transport.transceive(PORT, b"\x5f\x01\x01\x01\x83\xa0", 7)
    # DeviceTimeout subclasses TransportError: make sure a real fault is NOT
    # mistaken for a silent device
    assert isinstance(excinfo.value, DeviceTimeout) is (expected is DeviceTimeout)
