"""Unit tests for SerialTransport.transceive using a fake mqttrpc client.

transport.py imports ``mqttrpc`` lazily inside transceive; conftest provides it
(a stub unless the real package is installed). The tests raise the exception
classes the code actually catches and drive it with a fake RPC client.
"""

import pytest
from mqttrpc.client import (  # noqa: A004 - provided by conftest
    MQTTRPCError,
    TimeoutError,
)

from wb_mqtt_dauerhaft_pro.transport import (
    DeviceTimeout,
    PortConfig,
    SerialTransport,
    TransportError,
)

PORT = PortConfig(path="/dev/ttyRS485-2")
REQ = bytes.fromhex("5f01010183a0")


class FakeRpc:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.last_params = None

    def call(self, service, obj, method, params, timeout):  # matches TMQTTRPCClient.call
        self.last_params = params
        if self.exc is not None:
            raise self.exc
        return self.result


def _err(code, data):
    return MQTTRPCError("err", code, data)


def test_valid_response_returns_bytes():
    tr = SerialTransport(FakeRpc(result={"response": "5f0102015f5199"}))
    assert tr.transceive(PORT, REQ, 7) == bytes.fromhex("5f0102015f5199")


def test_response_timeout_and_total_timeout_in_params():
    rpc = FakeRpc(result={"response": ""})
    tr = SerialTransport(rpc)
    tr.transceive(PORT, REQ, 7, response_timeout_ms=1234, total_timeout_ms=5678)
    assert rpc.last_params["response_timeout"] == 1234
    assert rpc.last_params["total_timeout"] == 5678


def test_defaults_go_into_params():
    rpc = FakeRpc(result={"response": ""})
    SerialTransport(rpc).transceive(PORT, REQ, 7)
    assert rpc.last_params["response_timeout"] == 500
    assert rpc.last_params["total_timeout"] == 500


@pytest.mark.parametrize(
    "code,data",
    [
        (-32600, "RPC request timeout"),  # E_RPC_REQUEST_TIMEOUT (newer)
        (-32100, "timeout"),  # E_RPC_REQUEST_TIMEOUT (libwbmqtt)
        (-32000, "Port IO error: Serial protocol error: request timed out"),  # server error + text
    ],
)
def test_timeouts_map_to_device_timeout(code, data):
    tr = SerialTransport(FakeRpc(exc=_err(code, data)))
    with pytest.raises(DeviceTimeout):
        tr.transceive(PORT, REQ, 7)


@pytest.mark.parametrize(
    "code,data",
    [
        (-32000, "Port IO error: No such file or directory"),  # generic server error, NOT a timeout
        (-32700, "parse error"),
        (-32000, "Port IO error: device is busy"),
    ],
)
def test_non_timeout_errors_map_to_transport_error(code, data):
    tr = SerialTransport(FakeRpc(exc=_err(code, data)))
    with pytest.raises(TransportError) as excinfo:
        tr.transceive(PORT, REQ, 7)
    assert not isinstance(excinfo.value, DeviceTimeout)


def test_rpc_client_timeout_is_device_timeout():
    tr = SerialTransport(FakeRpc(exc=TimeoutError("no reply")))
    with pytest.raises(DeviceTimeout):
        tr.transceive(PORT, REQ, 7)


def test_bad_hex_response_is_transport_error():
    tr = SerialTransport(FakeRpc(result={"response": "zzz"}))
    with pytest.raises(TransportError):
        tr.transceive(PORT, REQ, 7)


def test_non_dict_result_is_transport_error():
    tr = SerialTransport(FakeRpc(result="not a dict"))
    with pytest.raises(TransportError):
        tr.transceive(PORT, REQ, 7)
