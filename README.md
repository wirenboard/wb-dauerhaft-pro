# wb-mqtt-dauerhaft-pro

Wiren Board MQTT driver for **Dauerhaft PRO RS-485** blind and shutter actuators.

The driver talks to the actuators over the RS-485 bus through **wb-mqtt-serial's
`port/Load` RPC** (the same mechanism the vendor's wb-rules driver uses). Because
wb-mqtt-serial stays the single owner of the serial port, Dauerhaft actuators can
share a bus with ordinary Modbus devices without collisions. Each actuator is
published as an MQTT device following the Wiren Board conventions, and the driver
is configured through wb-mqtt-confed.

## How it works

- A single-threaded control loop owns all bus I/O. paho-mqtt's network thread only
  delivers messages: RPC replies go to `mqttrpc`, and control commands (`.../on`)
  are pushed onto a priority queue (stop > control > config). The worker loop
  drains that queue and polls each device, so a command never blocks on the bus
  from within a callback.
- The Dauerhaft PRO protocol (v2.3) is Modbus-RTU framing (9600 8N1, CRC-16/Modbus)
  with a custom function set: query (position / state / angle / firmware / address),
  control (open / close / stop / move to % / slat angle / 3rd point), settings
  (limits, direction, 3rd point) and address change.

## Installation

On a Wiren Board controller:

```sh
apt update
apt install wb-mqtt-dauerhaft-pro
```

The package installs a systemd service (`wb-mqtt-dauerhaft-pro`), a confed schema,
and a default config at `/etc/wb-mqtt-dauerhaft-pro.conf`.

## Configuration

Edit via the Wiren Board web UI (Settings → Configs → *Dauerhaft PRO RS-485
driver*), or edit `/etc/wb-mqtt-dauerhaft-pro.conf` directly. Each device entry:

| Field              | Meaning                                                        |
|--------------------|----------------------------------------------------------------|
| `mqtt_id`          | MQTT device id (`/devices/<mqtt_id>/...`)                      |
| `title`            | Human-readable name                                            |
| `address`          | RS-485 address of the actuator (1..255)                        |
| `type`             | `roller`, `sliding` or `lamella` (only `lamella` has slats)   |
| `port`             | Serial port, e.g. `/dev/ttyRS485-2`                            |
| `baud_rate`        | Default 9600                                                    |
| `parity`           | `N` / `E` / `O`, default `N`                                    |
| `stop_bits`        | Default 1                                                       |
| `reverse_position` | Invert the 0..100 % position mapping                           |
| `poll_interval_s`  | Polling period, seconds                                        |

Example:

```json
{
  "debug": false,
  "devices": [
    {
      "mqtt_id": "dauerhaft_roller",
      "title": "Roller blind",
      "address": 95,
      "type": "roller",
      "port": "/dev/ttyRS485-2",
      "poll_interval_s": 1.0
    }
  ]
}
```

## MQTT controls

Per device: `open`, `close`, `stop`, `position` (0..100 %), `state`, `online`,
plus service controls (`third_point`, `set_upper_limit`, `set_lower_limit`,
`delete_limits`, `change_direction`, `query_address`, `set_address`).
`lamella` devices additionally expose `angle`, `jog_up`, `jog_down`.

Optional capabilities (slat angle, firmware version) are auto-detected: if a device
answers a query with an error frame, that capability is disabled and no longer
polled.

## Notes / limitations

- **Setting travel limits is a commissioning step.** `delete_limits` and setting a
  single limit are accepted by the actuators, but a full two-limit calibration over
  RS-485 depends on the actuator reaching its real mechanical end-stops — follow the
  motor's own commissioning procedure. Position reporting in `%` only becomes
  available once both limits are set on the actuator.
- Some actuators have the "active report" (unsolicited status) feature enabled by
  default; their spontaneous frames can occasionally collide with a poll and show up
  as a CRC error in the log.

## Development

```sh
pip install -r requirements.txt
python -m pytest tests/          # protocol + device tests run without a broker
```

The `transport` tests need `mqttrpc`; the full suite and any live testing run on a
controller (or the test stand) where wb-mqtt-serial and a broker are present.
