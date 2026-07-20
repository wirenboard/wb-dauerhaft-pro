"""
The wb-dauerhaft-pro daemon: monitoring only.

Reads the confed config, publishes one WB-conventions virtual device per
configured actuator — a read-only Address indicator plus availability via
``/meta/error`` — and polls each actuator for liveness at the configured
interval. Command controls (motion, address change) come in a later change.

The broker is reached through ``wb_common.mqtt_client.MQTTClient`` — by
default over mosquitto's unix socket, which stays open for local services
even after the user protects the TCP listeners with a password.

Threads: the MQTT network thread only delivers messages (RPC replies go to
mqttrpc); the main thread owns all bus I/O, so access to the half-duplex bus
is serialized by construction.
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time

from mqttrpc import client as rpcclient
from wb_common.mqtt_client import DEFAULT_BROKER_URL, MQTTClient

from . import config as cfgmod
from .device import Actuator
from .mqtt import DRIVER_NAME, WbDevice
from .transport import SerialTransport

logger = logging.getLogger(__name__)

# WB service exit-code contract: 6 = bad config (systemd NOTCONFIGURED; the
# unit's RestartPreventExitStatus stops a restart loop while genuine transient
# crashes still restart), 7 = clean exit on a signal (SuccessExitStatus),
# 2 = could not start.
EXIT_CONFIG_ERROR = 6
EXIT_SIGNAL = 7
EXIT_NOSTART = 2


def _stderr_goes_to_journal() -> bool:
    """
    Return True when this process's stderr is the systemd journal stream.

    systemd sets ``$JOURNAL_STREAM`` to the ``device:inode`` of the journal
    connection; comparing it against stderr confirms the stream is genuinely
    ours rather than inherited from a parent or redirected to a file.
    """
    stream = os.environ.get("JOURNAL_STREAM")
    if not stream or ":" not in stream:
        return False
    dev_str, _, inode_str = stream.partition(":")
    try:
        device, inode = int(dev_str), int(inode_str)
    except ValueError:
        return False
    try:
        stat = os.fstat(sys.stderr.fileno())
    except OSError:
        return False
    return stat.st_dev == device and stat.st_ino == inode


def _make_log_handler() -> logging.Handler:
    """
    Pick the log handler based on where the output goes.

    Under systemd we emit native journal records so each Python log level maps
    to a journal PRIORITY (``journalctl -p`` filtering and the web UI then show
    the real severity). Run from a console, or without python3-systemd, we fall
    back to a plain text handler so the level stays readable.
    """
    if _stderr_goes_to_journal():
        try:
            # Optional at import time; a hard dependency on the controller.
            # pylint: disable-next=import-outside-toplevel
            from systemd.journal import JournalHandler

            return JournalHandler(SYSLOG_IDENTIFIER=DRIVER_NAME)
        except ImportError:
            pass
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(logging.BASIC_FORMAT))
    return handler


def _setup_logging(debug: bool) -> None:
    """
    Attach a single WB-appropriate handler to the root logger.

    Deliberately not via ``logging.basicConfig(handlers=...)``: basicConfig
    assigns the default ``levelname:name:message`` formatter to any handler
    that has none, which would prepend a redundant text level to every journal
    record (the level is already the journal PRIORITY). Attaching the handler
    directly leaves the journal handler formatter-less, so it emits a clean
    message; the console fallback keeps its own explicit BASIC_FORMAT.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(_make_log_handler())
    root.setLevel(logging.DEBUG if debug else logging.INFO)


def build_controls(dev: WbDevice, act: Actuator) -> None:
    """
    Create the monitoring controls of one actuator.

    Order 5 keeps the indicator's position stable when the command controls
    (orders 1..4) arrive in a later change.
    """
    dev.add_control(
        "address",
        "text",
        5,
        readonly=True,
        title={"ru": "Адрес", "en": "Address"},
        initial=f"0x{act.cfg.address:02X}",
    )


def publish_state(dev: WbDevice, act: Actuator) -> None:
    """
    Publish the actuator's availability and its current address.

    Availability follows the WB convention: an empty ``/meta/error`` means OK,
    a non-empty value ("r") marks the device unavailable. Deduplicated retained
    publishing keeps unchanged states quiet.
    """
    dev.set_error("" if act.online else "r")
    dev.set_value("address", f"0x{act.cfg.address:02X}")


def main() -> int:
    """
    Entry point: parse arguments, load the config and run the poll loop.
    """
    parser = argparse.ArgumentParser(description="Wiren Board Dauerhaft PRO RS-485 driver")
    parser.add_argument("-c", "--config", default=cfgmod.CONFIG_PATH)
    parser.add_argument(
        "-b",
        "--broker-url",
        default=DEFAULT_BROKER_URL,
        help="MQTT broker url (default: mosquitto unix socket; tcp://host:port for debugging)",
    )
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.debug)
    try:
        conf = cfgmod.load_config(args.config)
    except cfgmod.ConfigError as err:
        logger.error("config error: %s", err)
        return EXIT_CONFIG_ERROR
    if conf.debug and not args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    client = MQTTClient(DRIVER_NAME, broker_url=args.broker_url)
    # Last Will: if the daemon dies ungracefully (SIGKILL / OOM / power loss),
    # the broker marks the device unavailable. A single MQTT connection carries
    # one will, so it covers the first configured device (enough for the common
    # single-device setup); the poll loop keeps every device's error up to date
    # while the daemon is alive.
    if conf.devices:
        client.will_set(f"/devices/{conf.devices[0].device_id}/meta/error", "r", retain=True)
    rpc = rpcclient.TMQTTRPCClient(client)
    client.on_message = rpc.on_mqtt_message
    try:
        client.start()  # connect (unix socket by default) + network thread
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("cannot connect to broker %s: %s", args.broker_url, exc)
        return EXIT_NOSTART
    transport = SerialTransport(rpc)

    entries = []  # [(WbDevice, Actuator)]
    for de in conf.devices:
        act = Actuator(de, transport)
        dev = WbDevice(client, de.device_id, de.name)
        build_controls(dev, act)
        dev.set_error("r")  # start unavailable until the first successful poll clears it
        entries.append((dev, act))
        logger.info("configured %s (addr 0x%02X) on %s", de.device_id, de.address, de.port.path)

    reconnected = threading.Event()

    def _on_connect(_client, _userdata, _flags, rc):
        """
        Flag a (re)connect for the poll loop to recover on the main thread.

        Runs on the MQTT network thread, so it only sets an event: the recovery
        it triggers (resetting mqttrpc's subscription cache and replaying
        retained state) touches WbDevice state shared with the poll loop, so it
        must run on the one thread that owns that state.
        """
        if rc != 0:
            logger.error("broker refused connection, rc=%d", rc)
            return
        logger.info("(re)connected to broker")
        reconnected.set()

    client.on_connect = _on_connect

    stop = threading.Event()

    def _on_signal(*_):
        """
        Stop the poll loop on SIGINT / SIGTERM.
        """
        logger.info("signal received, stopping")
        stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logger.info("started with %d device(s)", len(entries))
    try:
        while not stop.is_set():
            if reconnected.is_set():
                reconnected.clear()
                # Recover on the main thread (it owns WbDevice state): the clean
                # session dropped mqttrpc's reply subscription, so reset its
                # cache to re-subscribe on the next RPC call; and retained state
                # does not survive a broker restart (persistence is off), so
                # replay every device's retained topics.
                rpc.subscribes.clear()
                for dev, _act in entries:
                    dev.republish()
            for dev, act in entries:
                # A single device's poll must never take down the loop: an
                # unexpected error from paho/mqttrpc outside the transport's
                # handled classes would otherwise kill the daemon.
                try:
                    act.ping()
                    publish_state(dev, act)
                except Exception as exc:  # pylint: disable=broad-except
                    logger.warning("poll of %s failed: %s", dev.id, exc)
            stop.wait(conf.check_interval_s)
    finally:
        logger.info("shutting down")
        for dev, _act in entries:
            dev.remove()
        # dev.remove() publishes retained clears asynchronously; let the network
        # loop flush them before we stop it, otherwise a device would linger in
        # the UI with its last retained state.
        time.sleep(0.3)
        client.stop()
    return EXIT_SIGNAL


if __name__ == "__main__":
    sys.exit(main())
