"""wb-mqtt-dauerhaft-pro daemon (MVP).

Single control loop. paho's network thread only *delivers* messages (RPC replies
-> mqttrpc; control ``/on`` -> a command queue). One worker loop (the main thread)
owns ALL bus I/O: it drains the command queue and periodically pings each device
for liveness, calling the transport synchronously. This serializes bus access and
avoids deadlocking the network thread (a command callback must never call
transceive itself — the reply is delivered on that same thread).

Controls per device: Up / Down / Stop, a "set address" field, plus read-only
Online and Address indicators. That is the whole MVP surface.
"""

import argparse
import logging
import queue
import signal
import threading
import time

import paho.mqtt.client as mqtt
from mqttrpc import client as rpcclient

from . import config as cfgmod
from .device import Actuator
from .mqtt import DRIVER_NAME, WbDevice
from .transport import SerialTransport

logger = logging.getLogger(__name__)


def _int(value, default=0):
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def build_controls(dev: WbDevice, act: Actuator, enqueue):
    """Create the actuator's controls and wire command callbacks (enqueue only)."""

    def cmd(action):
        def _cb(_client, _userdata, msg):
            enqueue(act, action, msg.payload.decode("utf-8", "replace"))

        return _cb

    def button(name, order, ru, en):
        dev.add_control(name, "pushbutton", order, title={"ru": ru, "en": en}, initial="")
        dev.on_command(name, cmd(name))

    button("up", 1, "Открыть (вверх)", "Open (up)")
    button("down", 2, "Закрыть (вниз)", "Close (down)")
    button("stop", 3, "Стоп", "Stop")

    dev.add_control(
        "online", "switch", 4, readonly=True, title={"ru": "На связи", "en": "Online"}, initial="0"
    )
    dev.add_control(
        "address",
        "text",
        5,
        readonly=True,
        title={"ru": "Адрес", "en": "Address"},
        initial="0x%02X" % act.cfg.address,
    )
    dev.add_control(
        "set_address",
        "value",
        6,
        title={"ru": "Сменить адрес на", "en": "Set address to"},
        min_value=1,
        max_value=255,
        initial=act.cfg.address,
    )
    dev.on_command("set_address", cmd("set_address"))


def dispatch(act: Actuator, action: str, value: str):
    if action == "up":
        act.up()
    elif action == "down":
        act.down()
    elif action == "stop":
        act.stop()
    elif action == "set_address":
        act.set_address(_int(value))
    else:
        logger.warning("unknown action %s", action)


def publish_state(dev: WbDevice, act: Actuator):
    dev.set_value("online", "1" if act.online else "0")
    dev.set_value("address", "0x%02X" % act.cfg.address)


def main():
    parser = argparse.ArgumentParser(description="Wiren Board Dauerhaft PRO RS-485 driver (MVP)")
    parser.add_argument("-c", "--config", default=cfgmod.CONFIG_PATH)
    parser.add_argument("-b", "--broker", default="127.0.0.1")
    parser.add_argument("-p", "--broker-port", type=int, default=1883)
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args()

    conf = cfgmod.load_config(args.config)
    logging.basicConfig(
        level=logging.DEBUG if (args.debug or conf.debug) else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    client = mqtt.Client(DRIVER_NAME)
    client.connect(args.broker, args.broker_port)
    rpc = rpcclient.TMQTTRPCClient(client)
    client.on_message = rpc.on_mqtt_message
    client.loop_start()
    transport = SerialTransport(rpc)

    cmd_q = queue.Queue()

    def enqueue(act, action, value):
        cmd_q.put((act, action, value))

    entries = []  # [WbDevice, Actuator]
    dev_by_act = {}
    for de in conf.devices:
        act = Actuator(de, transport)
        dev = WbDevice(client, de.mqtt_id, de.title)
        build_controls(dev, act, enqueue)
        entries.append((dev, act))
        dev_by_act[id(act)] = dev
        logger.info("configured %s (addr 0x%02X) on %s", de.mqtt_id, de.address, de.port.path)

    def drain_commands():
        while True:
            try:
                act, action, value = cmd_q.get_nowait()
            except queue.Empty:
                return
            logger.debug("command: %s = %r", action, value)
            try:
                dispatch(act, action, value)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("command %s failed: %s", action, exc)
            publish_state(dev_by_act[id(act)], act)

    stop = threading.Event()

    def _on_signal(*_):
        logger.info("signal received, stopping")
        stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logger.info("started with %d device(s)", len(entries))
    next_ping = 0.0
    try:
        while not stop.is_set():
            drain_commands()
            now = time.monotonic()
            if now >= next_ping:
                for dev, act in entries:
                    act.ping()
                    publish_state(dev, act)
                    drain_commands()  # keep commands responsive between pings
                next_ping = time.monotonic() + conf.liveness_interval_s
            stop.wait(0.05)
    finally:
        logger.info("shutting down")
        for dev, _act in entries:
            dev.remove()
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
