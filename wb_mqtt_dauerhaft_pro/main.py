"""wb-mqtt-dauerhaft-pro daemon.

Single-threaded control loop: paho's network thread only delivers messages
(RPC replies -> mqttrpc; control /on -> a priority command queue). One worker
loop (the main thread) owns ALL bus I/O — it drains the command queue (with
priority: stop > control > config, and drained again between each device poll so
commands preempt polling) and polls each device, calling the transport
synchronously. This serializes bus access and avoids deadlocking the network
thread (a command callback must never call transceive).
"""

import argparse
import itertools
import logging
import queue
import signal
import threading
import time

import paho.mqtt.client as mqtt
from mqttrpc import client as rpcclient

from . import config as cfgmod
from . import protocol
from .device import Blind, BlindType
from .mqtt import DRIVER_NAME, WbDevice
from .transport import SerialTransport

logger = logging.getLogger(__name__)

MOTION_TEXT = {0: "стоп", 1: "открывается", 2: "закрывается"}
MARKER_TEXT = {
    "both_limits_unset": "пределы не заданы",
    "upper_limit_unset": "нет верхнего предела",
    "lower_limit_unset": "нет нижнего предела",
}

# Command priority for the worker queue: lower value = served first.
PRIO_STOP, PRIO_CONTROL, PRIO_CONFIG = 0, 1, 2
PRIORITY = {
    "stop": PRIO_STOP,
    "open": PRIO_CONTROL, "close": PRIO_CONTROL, "position": PRIO_CONTROL,
    "third_point": PRIO_CONTROL, "jog_up": PRIO_CONTROL, "jog_down": PRIO_CONTROL,
    "angle": PRIO_CONTROL,
    "set_third_point": PRIO_CONFIG, "set_upper_limit": PRIO_CONFIG,
    "set_lower_limit": PRIO_CONFIG, "delete_limits": PRIO_CONFIG,
    "change_direction": PRIO_CONFIG, "query_address": PRIO_CONFIG, "set_address": PRIO_CONFIG,
}


def _int(value, default=0):
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def build_controls(dev: WbDevice, blind: Blind, enqueue):
    """Create the blind's controls and wire command callbacks (enqueue only)."""

    def cmd(action):
        def _cb(_client, _userdata, msg):
            enqueue(blind, action, msg.payload.decode("utf-8", "replace"))
        return _cb

    def button(name, order, ru, en):
        dev.add_control(name, "pushbutton", order, title={"ru": ru, "en": en}, initial="")
        dev.on_command(name, cmd(name))

    button("open", 1, "Открыть", "Open")
    button("close", 2, "Закрыть", "Close")
    button("stop", 3, "Стоп", "Stop")
    dev.add_control("position", "range", 4, title={"ru": "Позиция", "en": "Position"},
                    min_value=0, max_value=100, initial=0)
    dev.on_command("position", cmd("position"))
    dev.add_control("state", "text", 5, readonly=True, title={"ru": "Состояние", "en": "State"}, initial="—")
    dev.add_control("online", "switch", 6, readonly=True, title={"ru": "На связи", "en": "Online"}, initial="0")

    if blind.cfg.blind_type == BlindType.LAMELLA:
        dev.add_control("angle", "range", 7, title={"ru": "Угол ламели", "en": "Slat angle"},
                        min_value=0, max_value=180, initial=0)
        dev.on_command("angle", cmd("angle"))
        button("jog_up", 8, "Шаг вверх", "Jog up")
        button("jog_down", 9, "Шаг вниз", "Jog down")

    # service / configuration
    button("third_point", 20, "К 3-й точке", "Go to 3rd point")
    button("set_third_point", 21, "Задать 3-ю точку", "Set 3rd point")
    button("set_upper_limit", 22, "Задать верхний предел", "Set upper limit")
    button("set_lower_limit", 23, "Задать нижний предел", "Set lower limit")
    button("delete_limits", 24, "Удалить пределы", "Delete limits")
    button("change_direction", 25, "Реверс направления", "Change direction")
    dev.add_control("firmware", "text", 30, readonly=True, title={"ru": "Версия ПО", "en": "Firmware"}, initial="")
    button("query_address", 31, "Запрос адреса", "Query address")
    dev.add_control("address", "text", 32, readonly=True, title={"ru": "Адрес", "en": "Address"}, initial="")
    dev.add_control("set_address", "value", 33, title={"ru": "Сменить адрес на", "en": "Set address to"},
                    min_value=1, max_value=255, initial=blind.cfg.address)
    dev.on_command("set_address", cmd("set_address"))


def dispatch(blind: Blind, action: str, value: str):
    actions = {
        "open": blind.open, "close": blind.close, "stop": blind.stop,
        "third_point": blind.third_point, "jog_up": blind.jog_up, "jog_down": blind.jog_down,
        "set_third_point": blind.set_third_point, "set_upper_limit": blind.set_upper_limit,
        "set_lower_limit": blind.set_lower_limit, "delete_limits": blind.delete_limits,
        "change_direction": blind.change_direction, "query_address": blind.query_address,
    }
    if action in actions:
        actions[action]()
    elif action == "position":
        blind.set_position(_int(value))
    elif action == "angle":
        blind.set_angle(_int(value))
    elif action == "set_address":
        blind.set_address(_int(value))
    else:
        logger.warning("unknown action %s", action)


def publish_state(dev: WbDevice, blind: Blind):
    s = blind.state
    dev.set_value("online", "1" if s.online else "0")
    if s.position is not None:
        dev.set_value("position", s.position)
    if s.firmware:
        dev.set_value("firmware", s.firmware)
    if s.angle is not None:
        dev.set_value("angle", s.angle)
    if s.address is not None:
        dev.set_value("address", "0x%02X" % s.address)
    if not s.online:
        text = "не в сети"
    elif s.motion == protocol.MotorState.MOVING_UP:
        text = "открывается"
    elif s.motion == protocol.MotorState.MOVING_DOWN:
        text = "закрывается"
    elif s.position_marker:
        text = MARKER_TEXT.get(s.position_marker, s.position_marker)
    else:
        text = "стоп"
    dev.set_value("state", text)
    logger.debug("state[%s]: online=%s pos=%s marker=%s motion=%s angle=%s",
                 dev.id, s.online, s.position, s.position_marker, s.motion, s.angle)


def main():
    parser = argparse.ArgumentParser(description="Wiren Board Dauerhaft PRO RS-485 driver")
    parser.add_argument("-c", "--config", default=cfgmod.CONFIG_PATH)
    parser.add_argument("-b", "--broker", default="127.0.0.1")
    parser.add_argument("-p", "--broker-port", type=int, default=1883)
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args()

    conf = cfgmod.load_config(args.config)
    logging.basicConfig(level=logging.DEBUG if (args.debug or conf.debug) else logging.INFO,
                        format="%(levelname)s: %(message)s")

    client = mqtt.Client(DRIVER_NAME)
    client.connect(args.broker, args.broker_port)
    rpc = rpcclient.TMQTTRPCClient(client)
    client.on_message = rpc.on_mqtt_message
    client.loop_start()
    transport = SerialTransport(rpc)

    cmd_q = queue.PriorityQueue()
    seq = itertools.count()

    def enqueue(blind, action, value):
        # (priority, seq) orders the queue; seq breaks ties (FIFO) and prevents
        # comparing the payload objects.
        cmd_q.put((PRIORITY.get(action, PRIO_CONTROL), next(seq), blind, action, value))

    entries = []  # [WbDevice, Blind, next_poll_monotonic]
    dev_by_blind = {}
    for de in conf.devices:
        blind = Blind(de.blind, transport)
        dev = WbDevice(client, de.mqtt_id, de.title)
        build_controls(dev, blind, enqueue)
        entries.append([dev, blind, 0.0])
        dev_by_blind[id(blind)] = dev
        logger.info("configured %s (addr 0x%02X, %s) on %s",
                    de.mqtt_id, de.blind.address, de.blind.blind_type.value, de.blind.port.path)

    def drain_commands():
        while True:
            try:
                _prio, _seq, blind, action, value = cmd_q.get_nowait()
            except queue.Empty:
                return
            logger.debug("command: %s = %r", action, value)
            try:
                dispatch(blind, action, value)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("command %s failed: %s", action, exc)
            publish_state(dev_by_blind[id(blind)], blind)

    stop = threading.Event()

    def _on_signal(*_):
        logger.info("signal received, stopping")
        stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logger.info("started with %d device(s)", len(entries))
    try:
        while not stop.is_set():
            drain_commands()
            now = time.monotonic()
            for entry in entries:
                dev, blind, next_poll = entry
                if now >= next_poll:
                    blind.poll()
                    publish_state(dev, blind)
                    entry[2] = time.monotonic() + blind.cfg.poll_interval_s
                drain_commands()  # keep commands responsive between device polls
            stop.wait(0.05)
    finally:
        logger.info("shutting down")
        for dev, _blind, _ in entries:
            dev.remove()
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
