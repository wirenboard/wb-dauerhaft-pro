# wb-dauerhaft-pro

Wiren Board MQTT driver for **Dauerhaft PRO RS-485** blind and shutter actuators.

This is an MVP: it can drive an actuator **Up / Down / Stop** and **change its
RS-485 address**. Position feedback, limit calibration, slat angle and other
service commands are intentionally out of scope for now.

## How it works

The driver does not open the serial port directly. Instead it sends each raw
protocol frame through **wb-mqtt-serial**'s `port/Load` MQTT-RPC, so Dauerhaft
actuators share the RS-485 bus with regular Modbus devices without collisions.

The Dauerhaft PRO protocol is Modbus-RTU framing (9600 8N1 by default) with a
vendor-specific function set. The driver implements just the MVP subset:

| Action        | Function | Data          |
|---------------|----------|---------------|
| Up / open     | `0x04`   | `01 64`       |
| Down / close  | `0x04`   | `01 00`       |
| Stop          | `0x04`   | `02 00`       |
| Query address | `0x01`   | `01`          |
| Set address   | `0x10`   | `<new addr>`  |

## MQTT device

Each configured actuator becomes an MQTT device with these controls:

* **Up / Down / Stop** — push buttons that drive the motor;
* **Online** — read-only liveness indicator (the driver periodically reads the
  device address as a ping);
* **Address** — read-only current RS-485 address;
* **Set address to** — writes a new RS-485 address to the device.

> Changing the address takes effect on the device immediately and the driver
> follows it at runtime, but you must also update the address in the config
> (via wb-mqtt-confed) to make it persist across restarts.

## Configuration

Configured via **wb-mqtt-confed** (`/etc/wb-dauerhaft-pro.conf`):

```json
{
  "debug": false,
  "liveness_interval_s": 5.0,
  "devices": [
    {
      "mqtt_id": "curtain1",
      "title": "Штора",
      "address": 95,
      "port": "/dev/ttyRS485-2",
      "baud_rate": 9600,
      "parity": "N",
      "stop_bits": 1
    }
  ]
}
```

## Build

Standard Debian package build (see the WB debianisation guide):

```sh
sudo apt update && sudo apt install git dpkg-dev debhelper dh-python python3-all python3-pytest
dpkg-buildpackage -rfakeroot -us -uc
sudo apt install ../wb-dauerhaft-pro_*.deb
```

On the Wiren Board build server the package is built by Jenkins (`Jenkinsfile`,
`buildDebSbuild`).

## Tests

```sh
python3 -m pytest tests/
```
