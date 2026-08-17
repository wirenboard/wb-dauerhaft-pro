"""
Minimal Wiren Board MQTT-conventions helper: a virtual device with controls.

Publishes retained ``/devices/<id>/...`` topics and registers per-control command
callbacks on ``<control>/on``. Works on any paho-compatible client — the daemon
passes the shared ``wb_common.mqtt_client.MQTTClient`` (the same client the RPC
transport uses).

Meta format (per WB conventions)::

    /devices/<id>/meta         {"driver":..., "title":{"en":..,"ru":..}}  (single JSON)
    /devices/<id>/meta/name    "<title>"                (legacy backward-compat only)
    /devices/<id>/meta/error   ""|"r"/"w"/"p"           (retained; non-empty = unavailable, LWT target)
    /devices/<id>/controls/<c>/meta  {"type","readonly","order","title":{...}}
    /devices/<id>/controls/<c>       "<value>"          (retained)
    /devices/<id>/controls/<c>/on    <- command (subscribed)
"""

import json
import logging
import time
from typing import Optional, Union

logger = logging.getLogger(__name__)

DRIVER_NAME = "wb-dauerhaft-pro"


def build_error_topic(device_id: str) -> str:
    """
    Build the device-level availability/error topic for a device id.

    The single source of the topic format; a module-level helper (not a
    WbDevice method) so the daemon can set the Last Will topic before any
    WbDevice exists — the will must be registered before the client connects.
    """
    return f"/devices/{device_id}/meta/error"


class WbDevice:
    """
    A virtual device published per the WB MQTT conventions.

    Owns the retained ``/devices/<id>/...`` topics of one device: the device
    meta, controls with their meta, the availability error topic and the
    per-control command subscriptions. Unchanged retained values are not
    republished; :meth:`remove` clears everything on shutdown.
    """

    def __init__(self, client, device_id: str, title: str, driver: str = DRIVER_NAME) -> None:
        self._client = client
        self.id = device_id
        self._base = f"/devices/{device_id}"
        self._controls = []
        self._on_topics = []
        self._last = {}  # topic -> last published value, to skip unchanged retained publishes
        self._pending = []  # publish confirmations not yet awaited (see wait_published)
        meta = {"driver": driver, "title": {"en": title, "ru": title}}
        self._pub(f"{self._base}/meta", json.dumps(meta, ensure_ascii=False))
        self._pub(f"{self._base}/meta/name", title)  # legacy backward-compat

    def add_control(
        self,
        name: str,
        control_type: str,
        order: int,
        *,
        readonly: bool = False,
        title: Optional[Union[str, dict]] = None,
        min_value: Optional[int] = None,
        max_value: Optional[int] = None,
        initial="",
    ) -> None:
        """
        Publish a control's meta and, unless *initial* is None, its first value.
        """
        meta = {"type": control_type, "readonly": readonly, "order": order}
        if title is not None:
            meta["title"] = title if isinstance(title, dict) else {"en": title}
        if min_value is not None:
            meta["min"] = min_value
        if max_value is not None:
            meta["max"] = max_value
        self._pub(f"{self._base}/controls/{name}/meta", json.dumps(meta, ensure_ascii=False))
        self._controls.append(name)
        if initial is not None:
            self.set_value(name, initial)

    def on_command(self, name: str, callback) -> None:
        """
        Subscribe to <control>/on and route matching messages to *callback*.

        Every command passes through here, so the cross-cutting rules live in
        this one place: retained messages are dropped (a command retained on
        the broker would replay on every daemon restart — a control the user
        never actually pressed), and accepted commands are logged at INFO so
        user actions leave a journal trace.
        """
        topic = f"{self._base}/controls/{name}/on"

        def handler(client, userdata, msg):
            if msg.retain:
                logger.warning("%s: ignoring retained command %s", self.id, name)
                return
            payload = msg.payload.decode("utf-8", "replace")
            logger.info("%s: command %s <- %s", self.id, name, payload[:64])
            callback(client, userdata, msg)

        self._on_topics.append(topic)
        self._client.subscribe(topic)
        self._client.message_callback_add(topic, handler)

    def resubscribe(self) -> None:
        """
        Re-subscribe all command topics (a broker reconnect drops them).
        """
        for topic in self._on_topics:
            self._client.subscribe(topic)

    def set_value(self, name: str, value) -> None:
        """
        Publish a control's retained value (skipped when unchanged).
        """
        self._pub(f"{self._base}/controls/{name}", str(value))

    def set_error(self, error: str) -> None:
        """
        Set/clear the device-level availability error.

        Per WB conventions a non-empty ``/meta/error`` marks the device
        unavailable; ``r``/``w``/``p`` mean read / write / period-miss. Pass an
        empty string to clear it. The state is mirrored onto every control
        (``<control>/meta/error``): the panel's device list reflects only
        control-level errors, so without the mirror an unavailable device
        looks indistinguishable from a live one there.
        """
        value = error or ""
        self._pub(build_error_topic(self.id), value)
        for name in self._controls:
            self._pub(f"{self._base}/controls/{name}/meta/error", value)

    def republish(self) -> None:
        """
        Re-send every retained topic of this device.

        Retained messages are not guaranteed to survive a broker restart
        (mosquitto persistence is off by default on the controller). The dedup
        cache holds the current value of every topic published so far, so
        re-sending it straight to the broker (bypassing the dedup, which would
        otherwise suppress the unchanged values) restores the whole device
        after a reconnect. The cache stays valid, so it is not rebuilt.
        """
        for topic, value in list(self._last.items()):
            self._client.publish(topic, value, retain=True)

    def remove(self) -> None:
        """
        Unsubscribe commands and clear all retained topics (called on shutdown).

        Waits for the clears to be confirmed (:meth:`wait_published`): the
        daemon calls client.stop() right after, and an unconfirmed clear would
        die in the paho queue, leaving a ghost device in the panel.
        """
        for topic in self._on_topics:
            self._client.unsubscribe(topic)
            self._client.message_callback_remove(topic)
        for name in self._controls:
            self._pub(f"{self._base}/controls/{name}", None)
            self._pub(f"{self._base}/controls/{name}/meta", None)
            self._pub(f"{self._base}/controls/{name}/meta/error", None)
        self._pub(build_error_topic(self.id), None)
        self._pub(f"{self._base}/meta", None)
        self._pub(f"{self._base}/meta/name", None)
        self.wait_published()

    def wait_published(self, timeout: float = 2.0) -> None:
        """
        Wait until every pending publish has been handed to the network.

        paho's publish() only queues; the network thread sends asynchronously,
        so callers that stop the client right after publishing must wait for
        the confirmations or risk losing the tail of the queue. QoS-0
        "published" means written to the socket — no broker ack exists — which
        is exactly the guarantee needed against stop() racing the network
        thread. One deadline bounds the whole batch (a dead connection must
        not stall shutdown per-topic); leftovers are logged, not raised — the
        callers (shutdown cleanup, config-error announcement) are best-effort.
        """
        deadline = time.monotonic() + timeout
        unconfirmed = 0
        for info in self._pending:
            try:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    info.wait_for_publish(remaining)
                if not info.is_published():
                    unconfirmed += 1
            except (RuntimeError, ValueError):  # paho: not queued / rejected outright
                unconfirmed += 1
        self._pending.clear()
        if unconfirmed:
            logger.warning(
                "%s: %d retained publish(es) unconfirmed after %.1fs", self.id, unconfirmed, timeout
            )

    def _pub(self, topic: str, value):
        """
        Publish a retained value, skipping an unchanged repeat.

        The poll loop re-sends the same error/address every cycle; the dedup
        cache keeps those quiet. A publish dropped while disconnected stays
        cached on purpose — the daemon calls republish() on every (re)connect,
        which replays the whole cache and restores the device (broker restarts
        drop retained state), so recovery never hinges on one publish, and a
        one-shot meta topic dropped at startup is still restored.

        Returns the paho publish confirmation (also queued for
        :meth:`wait_published`), or None for a deduplicated repeat.
        """
        if topic in self._last and self._last[topic] == value:
            return None
        self._last[topic] = value
        info = self._client.publish(topic, value, retain=True)
        # prune confirmed receipts eagerly: the device lives for months with
        # wait_published() only called on shutdown, so hoarding every state
        # change's receipt until then would be an unbounded leak
        self._pending = [pending for pending in self._pending if not pending.is_published()]
        self._pending.append(info)
        return info
