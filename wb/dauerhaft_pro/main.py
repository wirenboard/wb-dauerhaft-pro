"""
The wb-dauerhaft-pro daemon: monitoring and basic commands.

Reads the confed config, publishes one WB-conventions virtual device per
configured actuator — a read-only Address indicator, availability via
``/meta/error``, and the command controls (open / stop / close and a unicast
address change) — and polls each actuator for liveness at the configured
interval. Command callbacks only enqueue; the poll loop drains the queue, so
all bus I/O stays on the one thread that owns the half-duplex bus.

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

from mqttrpc import client as rpcclient
from wb_common.mqtt_client import DEFAULT_BROKER_URL, MQTTClient

from . import config as cfgmod
from .commands import CommandQueue
from .controls import DeviceControls, publish_state
from .device import Actuator
from .mqtt import DRIVER_NAME, WbDevice, build_error_topic
from .transport import SerialTransport

logger = logging.getLogger(__name__)

# WB service exit-code contract (matches the python-service template):
#   6 = bad config -> systemd NOTCONFIGURED; RestartPreventExitStatus stops a
#       restart loop until the user fixes the config;
#   7 = clean exit on a signal -> SuccessExitStatus;
#   1 = transient failure (e.g. the broker is not up yet) -> NOT in
#       RestartPreventExitStatus, so Restart=on-failure keeps retrying it.
# argparse exits 2 on bad command-line arguments; the unit lists 2 as
# non-restartable too, since bad arguments will not fix themselves on a restart.
EXIT_CONFIG_ERROR = 6
EXIT_SIGNAL = 7
EXIT_FAILURE = 1


def _detect_journal_stderr() -> bool:
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
    if _detect_journal_stderr():
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


def _config_error_device(client) -> WbDevice:
    """
    The driver-status pseudo-device that carries a config error message.

    Built identically on the publish and the clear paths, so the retained
    topics always match: a failed start publishes the error text, the next
    successful start removes the whole pseudo-device.
    """
    dev = WbDevice(client, DRIVER_NAME, "Dauerhaft PRO")
    dev.add_control(
        "config_error",
        "text",
        1,
        readonly=True,
        title={"ru": "Ошибка конфигурации", "en": "Config Error"},
        initial=None,
    )
    return dev


def _announce_config_error(broker_url: str, message: str) -> None:
    """
    Best-effort: leave the config error visible in the panel before exiting.

    The daemon refuses to start on a bad config (fail-fast, no restart loop),
    but a journal-only reason is easy to miss — the editor accepts configs the
    daemon rejects (e.g. duplicate device ids), and the devices then just
    disappear from the panel. So the reason is also published retained; the
    next successful start clears it. No state is read back from the broker
    between runs: this path only publishes the report, and the clearing start
    blindly publishes removals for the same fixed topics. Broker trouble here
    only degrades the announcement back to the journal message.
    """
    try:
        client = MQTTClient(DRIVER_NAME, broker_url=broker_url)
        client.start()
        dev = _config_error_device(client)
        dev.set_value("config_error", message[:200])
        dev.wait_published()  # confirm delivery before stop() kills the network thread
        client.stop()
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("cannot publish the config error to MQTT: %s", exc)


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
        _announce_config_error(args.broker_url, str(err))
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
        client.will_set(build_error_topic(conf.devices[0].device_id), "r", retain=True)
    rpc = rpcclient.TMQTTRPCClient(client)
    client.on_message = rpc.on_mqtt_message
    try:
        client.start()  # connect (unix socket by default) + network thread
    except Exception as exc:  # pylint: disable=broad-except
        # transient (mosquitto not up yet / restarting): exit with a restartable
        # code so systemd's Restart=on-failure retries, instead of a code in
        # RestartPreventExitStatus that would leave the daemon permanently down
        logger.error("cannot connect to broker %s: %s", args.broker_url, exc)
        return EXIT_FAILURE
    # A previous start may have left a config-error report in the panel (see
    # _announce_config_error); this start got past config loading, so clear it.
    try:
        _config_error_device(client).remove()
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("cannot clear a stale config error: %s", exc)
    transport = SerialTransport(rpc)

    queue = CommandQueue()  # MQTT callbacks enqueue; the poll loop drains on the bus thread
    entries = []  # [(WbDevice, Actuator, DeviceControls)]
    for dev_cfg in conf.devices:
        actuator = Actuator(dev_cfg, transport)
        dev = WbDevice(client, dev_cfg.device_id, dev_cfg.name)
        controls = DeviceControls(dev, actuator, queue)
        controls.create()
        dev.set_error("r")  # start unavailable until the first successful poll clears it
        entries.append((dev, actuator, controls))
        logger.info(
            "configured %s (addr 0x%02X) on %s",
            dev_cfg.device_id,
            dev_cfg.address,
            dev_cfg.port.path,
        )

    reconnected = threading.Event()

    def _on_connect(_client, _userdata, _flags, rc):
        """
        Flag a (re)connect for the poll loop to recover on the main thread.

        Runs on the MQTT network thread, so it only sets events:
        - the recovery it triggers touches WbDevice state owned by the poll
          loop, so it must run on that thread, not here;
        - waking ``queue.ready`` runs that recovery at once instead of after the
          poll interval, so a command pressed in the gap is not lost (its topic
          is not re-subscribed yet).
        """
        if rc != 0:
            logger.error("broker refused connection, rc=%d", rc)
            return
        logger.info("(re)connected to broker")
        reconnected.set()
        queue.ready.set()  # wake the poll loop now so re-subscribe happens immediately

    client.on_connect = _on_connect

    stop = threading.Event()

    def _on_signal(*_):
        """
        Stop the poll loop on SIGINT / SIGTERM.
        """
        logger.info("signal received, stopping")
        stop.set()
        queue.ready.set()  # wake the poll loop out of its wait at once

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logger.info("started with %d device(s)", len(entries))
    try:
        while not stop.is_set():
            if reconnected.is_set():
                reconnected.clear()
                # Recover on the main thread (it owns WbDevice state). A reconnect
                # drops mqttrpc's reply subscription and the command subscriptions,
                # and retained state does not survive a broker restart. So:
                #   - reset mqttrpc's subscription cache,
                #   - replay every device's retained topics,
                #   - re-subscribe its command topics.
                # Guarded like the poll below so a paho/mqttrpc hiccup can't kill
                # the loop.
                try:
                    rpc.subscribes.clear()
                    for dev, _actuator, _controls in entries:
                        dev.republish()
                        dev.resubscribe()
                except Exception as exc:  # pylint: disable=broad-except
                    logger.warning("reconnect recovery failed: %s", exc)
            queue.drain()  # run queued commands (bus I/O) on this thread
            for dev, actuator, controls in entries:
                if stop.is_set():
                    break  # a signal mid-pass: stop now so the finally-block cleanup runs
                # A single device's poll must never take down the loop: an
                # unexpected error from paho/mqttrpc outside the transport's
                # handled classes would otherwise kill the daemon.
                try:
                    actuator.ping()
                    publish_state(dev, actuator)
                    if actuator.online:
                        controls.publish_telemetry()  # position (and slat angle) while reachable
                except Exception as exc:  # pylint: disable=broad-except
                    logger.warning("poll of %s failed: %s", dev.id, exc)
            # Sleep until the next poll, a queued command, or a signal — whichever
            # comes first (put()/_on_signal set queue.ready).
            queue.ready.wait(conf.check_interval_s)
    finally:
        logger.info("shutting down")
        for dev, _actuator, _controls in entries:
            dev.remove()  # publishes the retained clears and waits them out
        client.stop()
    return EXIT_SIGNAL


if __name__ == "__main__":
    sys.exit(main())
