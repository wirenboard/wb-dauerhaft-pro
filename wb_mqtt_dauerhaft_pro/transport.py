"""RS-485 transport for Dauerhaft PRO via wb-mqtt-serial's ``port/Load`` MQTT-RPC.

Rather than opening the serial port directly (which would fight wb-mqtt-serial
for the bus), the driver asks wb-mqtt-serial to put a raw frame on the wire and
return the reply. This is the same mechanism the vendor's wb-rules driver uses
and lets Dauerhaft devices share a bus with regular Modbus devices.

RPC contract (verified against wb-mqtt-serial and the reference tools
``modbus-utils-rpc`` / ``wb-mqtt-dali``)::

    call("wb-mqtt-serial", "port", "Load", params, timeout_s)
    params = {path, baud_rate, parity, data_bits, stop_bits,
              format="HEX", msg=<hex>, response_size, total_timeout}
    -> {"response": "<hex reply>"}

A bus/device timeout is reported by wb-mqtt-serial as an RPC error (code -32000,
"... request timed out"); a total absence of an RPC reply raises TimeoutError.
Both are surfaced here as :class:`DeviceTimeout`.

Targets the versions shipped on the controller: paho-mqtt 1.5.1, mqttrpc 1.3.5.
"""

import contextlib
from dataclasses import dataclass
from typing import Optional

# paho / mqttrpc are imported lazily (in transceive/connect) so this module — and
# everything that depends on it — imports on a dev box without paho/mqttrpc. The
# daemon runs on the controller where both exist.

DEFAULT_BROKER_HOST = "127.0.0.1"
DEFAULT_BROKER_PORT = 1883

# Single request/response budget. Reads/motion answer in well under this; the
# address change writes flash and is given a longer budget by the caller.
DEFAULT_TOTAL_TIMEOUT_MS = 500


class TransportError(Exception):
    """Transport-level failure talking to wb-mqtt-serial."""


class DeviceTimeout(TransportError):
    """The device did not answer in time (offline, wrong address, powered off)."""


@dataclass
class PortConfig:
    """Serial port parameters passed to wb-mqtt-serial per request."""

    path: str
    baud_rate: int = 9600
    parity: str = "N"
    data_bits: int = 8
    stop_bits: int = 1


class SerialTransport:
    """Sends raw frames through wb-mqtt-serial ``port/Load`` and returns replies.

    Constructed with an already-connected ``mqttrpc`` RPC client so it is easy to
    unit-test with a fake. Use :func:`connect` for a ready-to-go instance.
    """

    def __init__(self, rpc_client, *, default_total_timeout_ms: int = DEFAULT_TOTAL_TIMEOUT_MS):
        self._rpc = rpc_client
        self._default_total_timeout_ms = default_total_timeout_ms

    def transceive(
        self,
        port: PortConfig,
        request: bytes,
        response_size: int,
        *,
        total_timeout_ms: Optional[int] = None,
    ) -> bytes:
        """Send *request* on *port*, wait for *response_size* bytes, return the reply.

        Raises :class:`DeviceTimeout` if the device does not answer and
        :class:`TransportError` for other RPC/bus errors.
        """
        total = total_timeout_ms if total_timeout_ms is not None else self._default_total_timeout_ms
        params = {
            "path": port.path,
            "baud_rate": port.baud_rate,
            "parity": port.parity,
            "data_bits": port.data_bits,
            "stop_bits": port.stop_bits,
            "format": "HEX",
            "msg": request.hex(),
            "response_size": response_size,
            "total_timeout": total,
        }

        from mqttrpc import client as rpcclient  # lazy: only needed at call time

        # Give the RPC round-trip headroom beyond the device-side budget.
        rpc_timeout_s = total / 1000.0 + 2.0
        try:
            result = self._rpc.call("wb-mqtt-serial", "port", "Load", params, rpc_timeout_s)
        except rpcclient.TimeoutError as err:
            raise DeviceTimeout("no RPC reply from wb-mqtt-serial") from err
        except rpcclient.MQTTRPCError as err:
            if err.data and "timed out" in str(err.data).lower():
                raise DeviceTimeout(str(err.data)) from err
            raise TransportError(f"port/Load error [{err.code}]: {err.data}") from err

        response_hex = (result or {}).get("response", "")
        return bytes.fromhex(response_hex)


@contextlib.contextmanager
def connect(
    host: str = DEFAULT_BROKER_HOST,
    port: int = DEFAULT_BROKER_PORT,
    client_id: str = "wb-mqtt-dauerhaft-pro",
    **kwargs,
):
    """Context manager yielding a connected :class:`SerialTransport`."""
    import paho.mqtt.client as mqtt  # lazy import (only on the controller)
    from mqttrpc import client as rpcclient

    client = mqtt.Client(client_id)
    client.connect(host, port)
    rpc = rpcclient.TMQTTRPCClient(client)
    client.on_message = rpc.on_mqtt_message
    client.loop_start()
    try:
        yield SerialTransport(rpc, **kwargs)
    finally:
        client.loop_stop()
        client.disconnect()
