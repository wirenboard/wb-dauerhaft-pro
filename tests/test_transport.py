"""Unit tests for the wb-mqtt-serial port/Load transport (no broker needed).

A fake RPC client stands in for mqttrpc's TMQTTRPCClient so we can assert the
exact params sent to ``port/Load`` and the error translation, without a broker.
"""

import pytest
from mqttrpc import client as rpcclient

from wb_mqtt_dauerhaft_pro import transport as t


class FakeRpc:
    """Records the last call and returns a canned result or raises."""

    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.last = None

    def call(self, driver, service, method, params, timeout):
        self.last = dict(
            driver=driver, service=service, method=method, params=params, timeout=timeout
        )
        if self.exc is not None:
            raise self.exc
        return self.result


PORT = t.PortConfig(path="/dev/ttyRS485-2", baud_rate=9600, parity="N", stop_bits=1)


def test_transceive_builds_params_and_returns_bytes():
    rpc = FakeRpc(result={"response": "5f010202fc1110"})
    tr = t.SerialTransport(rpc)
    reply = tr.transceive(PORT, bytes.fromhex("5F010102C3A1"), response_size=7)

    assert reply == bytes.fromhex("5f010202fc1110")
    p = rpc.last["params"]
    assert (rpc.last["driver"], rpc.last["service"], rpc.last["method"]) == (
        "wb-mqtt-serial",
        "port",
        "Load",
    )
    assert p["path"] == "/dev/ttyRS485-2"
    assert p["baud_rate"] == 9600
    assert p["parity"] == "N"
    assert p["data_bits"] == 8
    assert p["stop_bits"] == 1
    assert p["format"] == "HEX"
    assert p["msg"] == "5f010102c3a1"  # request.hex()
    assert p["response_size"] == 7
    assert p["total_timeout"] == t.DEFAULT_TOTAL_TIMEOUT_MS
    assert "response_timeout" not in p


def test_transceive_optional_response_timeout():
    rpc = FakeRpc(result={"response": "00"})
    tr = t.SerialTransport(rpc)
    tr.transceive(PORT, b"\x01", response_size=7, total_timeout_ms=300, response_timeout_ms=100)
    p = rpc.last["params"]
    assert p["total_timeout"] == 300
    assert p["response_timeout"] == 100


def test_device_timeout_from_rpc_error():
    err = rpcclient.MQTTRPCError(
        "Server error", -32000, "Port IO error: Serial protocol error: request timed out"
    )
    tr = t.SerialTransport(FakeRpc(exc=err))
    with pytest.raises(t.DeviceTimeout):
        tr.transceive(PORT, b"\x01", response_size=7)


def test_other_rpc_error_is_transport_error():
    err = rpcclient.MQTTRPCError("Server error", -32000, "some other failure")
    tr = t.SerialTransport(FakeRpc(exc=err))
    with pytest.raises(t.TransportError) as ei:
        tr.transceive(PORT, b"\x01", response_size=7)
    assert not isinstance(ei.value, t.DeviceTimeout)


def test_no_reply_is_device_timeout():
    tr = t.SerialTransport(FakeRpc(exc=rpcclient.TimeoutError()))
    with pytest.raises(t.DeviceTimeout):
        tr.transceive(PORT, b"\x01", response_size=7)
