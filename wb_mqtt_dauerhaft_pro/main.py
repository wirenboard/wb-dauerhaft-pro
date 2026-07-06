"""wb-mqtt-dauerhaft-pro daemon (MVP).

Single control loop. paho's network thread only *delivers* messages (RPC replies
-> mqttrpc; control ``/on`` -> a command queue). One worker loop (the main thread)
owns ALL bus I/O: it drains the command queue and periodically pings each device
for liveness, calling the transport synchronously. This serializes bus access and
avoids deadlocking the network thread (a command callback must never call
transceive itself — the reply is delivered on that same thread).

Controls per device: Up / Down / Stop, a "set address" field and a read-only
Address indicator; availability is published via the conventional /meta/error
topic. That is the whole MVP surface.
"""

import argparse
import logging
import queue
import signal
import threading
import time

from mqttrpc import client as rpcclient

from . import config as cfgmod
from .device import Actuator
from .mqtt import DRIVER_NAME, WbDevice, make_client
from .transport import SerialTransport

logger = logging.getLogger(__name__)

# Exit code for a bad config (EX_CONFIG from sysexits.h). The systemd unit sets
# RestartPreventExitStatus to this, so a broken config does not restart-loop
# while genuine transient crashes (exit 1) still restart.
EXIT_CONFIG_ERROR = 78


def build_controls(dev: WbDevice, act: Actuator, enqueue):
    """Create the actuator's controls and wire command callbacks (enqueue only)."""

    def cmd(action):
        def _cb(_client, _userdata, msg):
            enqueue(dev, act, action, msg.payload.decode("utf-8", "replace"))

        return _cb

    def button(name, order, ru, en):
        # A pushbutton has no retained state (its only value is a momentary "1"),
        # so publish no initial value — otherwise the value topic keeps a retained "".
        dev.add_control(name, "pushbutton", order, title={"ru": ru, "en": en}, initial=None)
        dev.on_command(name, cmd(name))

    button("up", 1, "Открыть (вверх)", "Open (up)")
    button("down", 2, "Закрыть (вниз)", "Close (down)")
    button("stop", 3, "Стоп", "Stop")

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
        # int() raises ValueError on a non-numeric payload, which drain_commands
        # logs — better than silently coercing garbage to 0.
        act.set_address(int(value))
    else:
        logger.warning("unknown action %s", action)


def resubscribe_all(entries, rpc):
    """Re-establish subscriptions after a broker (re)connect.

    The default clean session drops all subscriptions on reconnect and neither
    WbDevice nor mqttrpc re-subscribe on their own, so re-subscribe every device's
    command topics and reset mqttrpc's reply-topic cache (it re-subscribes on the
    next call).
    """
    for dev, _act in entries:
        dev.resubscribe()
    rpc.subscribes.clear()


def publish_state(dev: WbDevice, act: Actuator):
    # Availability follows the WB convention: an empty /meta/error means OK, a
    # non-empty value ("r") marks the device unavailable.
    dev.set_error("" if act.online else "r")
    dev.set_value("address", "0x%02X" % act.cfg.address)
    # Track the current address in the set-address field too, so a reloaded UI
    # shows the real value and re-sending it will not revert the device. Deduped
    # publishing means this only fires when the address actually changes.
    dev.set_value("set_address", act.cfg.address)


def main():
    parser = argparse.ArgumentParser(description="Wiren Board Dauerhaft PRO RS-485 driver (MVP)")
    parser.add_argument("-c", "--config", default=cfgmod.CONFIG_PATH)
    parser.add_argument("-b", "--broker", default="127.0.0.1")
    parser.add_argument("-p", "--broker-port", type=int, default=1883)
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    try:
        conf = cfgmod.load_config(args.config)
    except cfgmod.ConfigError as err:
        logger.error("config error: %s", err)
        return EXIT_CONFIG_ERROR
    if conf.debug and not args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    client = make_client(DRIVER_NAME)
    # Last Will: if the daemon dies ungracefully (SIGKILL / OOM / power loss), the
    # broker marks the device unavailable. A single MQTT connection carries one
    # will, so it covers the first configured device (enough for the common
    # single-device setup); the poll loop keeps every device's error up to date
    # while the daemon is alive.
    if conf.devices:
        client.will_set(f"/devices/{conf.devices[0].mqtt_id}/meta/error", "r", retain=True)
    client.connect(args.broker, args.broker_port)
    rpc = rpcclient.TMQTTRPCClient(client)
    client.on_message = rpc.on_mqtt_message
    client.loop_start()
    transport = SerialTransport(rpc)

    cmd_q = queue.Queue()

    def enqueue(dev, act, action, value):
        cmd_q.put((dev, act, action, value))

    entries = []  # [(WbDevice, Actuator)]
    for de in conf.devices:
        act = Actuator(de, transport)
        dev = WbDevice(client, de.mqtt_id, de.title)
        build_controls(dev, act, enqueue)
        dev.set_error("r")  # start unavailable until the first successful poll clears it
        entries.append((dev, act))
        logger.info("configured %s (addr 0x%02X) on %s", de.mqtt_id, de.address, de.port.path)

    def _on_connect(_client, _userdata, _flags, _rc):
        logger.info("(re)connected to broker; re-subscribing")
        resubscribe_all(entries, rpc)

    client.on_connect = _on_connect

    def drain_commands():
        while True:
            try:
                dev, act, action, value = cmd_q.get_nowait()
            except queue.Empty:
                return
            logger.debug("command: %s = %r", action, value)
            try:
                dispatch(act, action, value)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("command %s failed: %s", action, exc)
            publish_state(dev, act)

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
                    # A single device's poll must never take down the loop: an
                    # unexpected error from paho/mqttrpc outside the transport's
                    # handled classes would otherwise kill the daemon.
                    try:
                        act.ping()
                        publish_state(dev, act)
                    except Exception as exc:  # pylint: disable=broad-except
                        logger.warning("poll of %s failed: %s", dev.id, exc)
                    drain_commands()  # keep commands responsive between pings
                next_ping = time.monotonic() + conf.liveness_interval_s
            stop.wait(0.05)
    finally:
        logger.info("shutting down")
        for dev, _act in entries:
            dev.remove()
        # dev.remove() publishes retained clears asynchronously; let the network
        # loop flush them before we stop it, otherwise a device would linger in
        # the UI with its last retained state.
        time.sleep(0.3)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
