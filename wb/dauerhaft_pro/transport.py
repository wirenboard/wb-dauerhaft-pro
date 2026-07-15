"""
RS-485 transport for Dauerhaft PRO via wb-mqtt-serial's ``port/Load`` MQTT-RPC.

Rather than opening the serial port directly (which would fight wb-mqtt-serial
for the bus), the driver asks wb-mqtt-serial to put a raw frame on the wire and
return the reply. This is the same mechanism the vendor's wb-rules driver uses
and lets Dauerhaft devices share a bus with regular Modbus devices.

RPC contract (verified against wb-mqtt-serial sources and the reference tools
``modbus-utils-rpc`` / ``wb-mqtt-dali``)::

    call("wb-mqtt-serial", "port", "Load", params, timeout_s)
    params = {path, baud_rate, parity, data_bits, stop_bits,
              format="HEX", msg=<hex>, response_size,
              [response_timeout], total_timeout}
    -> {"response": "<hex reply>"}

``response_timeout`` bounds the wait for the device's first reply byte and is
normally NOT sent: wb-mqtt-serial then applies the maximum of its own default
(500 ms) and the port's configured ``response_timeout_ms``, so retuning the
port in the wb-mqtt-serial settings is picked up automatically. Slow
operations that write the actuator's flash (set-address) are the exception —
they must pass a larger value explicitly, or the read gives up too early.
``total_timeout`` is a queue-expiry deadline: wb-mqtt-serial checks it once,
before the exchange starts (a stale queued request fails fast); it never cuts
short a running exchange, so it does not need to track the port timings.

A bus/device timeout is reported by wb-mqtt-serial as an RPC error (code -32000,
"... request timed out"); a total absence of an RPC reply raises TimeoutError.
Both are surfaced here as :class:`DeviceTimeout`.

Works with the versions shipped on the controller: mqttrpc 1.3.x on top of the
shared ``wb_common.mqtt_client.MQTTClient`` (a paho client connected to the
broker's unix socket by default).
"""

from dataclasses import dataclass
from typing import Optional

# mqttrpc is imported lazily (in transceive) so this module — and everything that
# depends on it — imports on a dev box without mqttrpc. The daemon runs on the
# controller where it exists.

# How long a request may sit in wb-mqtt-serial's queue before it is dropped as
# stale (checked only before the exchange starts, never during it). Our own
# budget: commands and liveness pings older than this are not worth executing.
DEFAULT_TOTAL_TIMEOUT_MS = 500

# Safety net for the RPC round-trip itself (broker/wb-mqtt-serial dead). Must
# comfortably exceed any port-configured response timeout, which we do not know.
_RPC_MARGIN_S = 10.0

# wb-mqtt-serial error codes. E_RPC_REQUEST_TIMEOUT is version-dependent (-32100
# in libwbmqtt, -32600 in newer tooling) and always means "no answer in time".
# E_RPC_SERVER_ERROR (-32000) is GENERIC ("Port IO error: ..."): only a serial
# read timeout ("... request timed out") means the device stayed silent — other
# Port IO errors (wrong path, busy port) are real faults worth a transport error.
E_RPC_SERVER_ERROR = -32000
RPC_TIMEOUT_CODES = (-32100, -32600)


class TransportError(Exception):
    """
    Transport-level failure talking to wb-mqtt-serial.
    """


class DeviceTimeout(TransportError):
    """
    The device did not answer in time (offline, wrong address, powered off).
    """


@dataclass
class PortConfig:
    """
    Serial port parameters passed to wb-mqtt-serial per request.
    """

    path: str
    baud_rate: int = 9600
    parity: str = "N"
    data_bits: int = 8
    stop_bits: int = 1


class SerialTransport:
    """
    Sends raw frames through wb-mqtt-serial ``port/Load`` and returns replies.

    Constructed with an already-connected ``mqttrpc`` RPC client so it is easy to
    unit-test with a fake; the daemon wires a connected RPC client in its entry
    point.
    """

    def __init__(
        self,
        rpc_client,
        *,
        default_total_timeout_ms: int = DEFAULT_TOTAL_TIMEOUT_MS,
    ):
        self._rpc = rpc_client
        self._default_total_timeout_ms = default_total_timeout_ms

    def transceive(
        self,
        port: PortConfig,
        request: bytes,
        response_size: int,
        *,
        response_timeout_ms: Optional[int] = None,
        total_timeout_ms: Optional[int] = None,
    ) -> bytes:
        """
        Send *request* on *port*, wait for *response_size* bytes, return the reply.

        ``response_timeout_ms`` is normally left None and NOT sent — the reply
        wait is then governed by wb-mqtt-serial (its 500 ms default vs the
        port's configured timeout, whichever is larger). Pass it only for
        operations the default cannot cover (flash writes).

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
        if response_timeout_ms is not None:
            params["response_timeout"] = response_timeout_ms

        from mqttrpc import client as rpcclient  # lazy: only needed at call time

        # Safety net beyond the device-side budgets; generous because the
        # effective reply wait may come from the port settings, unknown here.
        rpc_timeout_s = max(total, response_timeout_ms or 0) / 1000.0 + _RPC_MARGIN_S
        try:
            result = self._rpc.call("wb-mqtt-serial", "port", "Load", params, rpc_timeout_s)
        except rpcclient.TimeoutError as err:
            raise DeviceTimeout("no RPC reply from wb-mqtt-serial") from err
        except rpcclient.MQTTRPCError as err:
            msg = str(err.data or "")
            lowered = msg.lower()
            is_timeout = err.code in RPC_TIMEOUT_CODES or (
                err.code == E_RPC_SERVER_ERROR and ("timed out" in lowered or "timeout" in lowered)
            )
            if is_timeout:
                raise DeviceTimeout(msg or f"code {err.code}") from err
            raise TransportError(f"port/Load error [{err.code}]: {err.data}") from err

        try:
            response_hex = (result or {}).get("response", "")
            return bytes.fromhex(response_hex)
        except (AttributeError, TypeError, ValueError) as err:
            # A malformed RPC result (non-dict, or an odd-length / non-hex string)
            # must not crash the daemon — surface it as a transport error instead.
            raise TransportError(f"bad port/Load response: {result!r}") from err
