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
from typing import Optional, Union

logger = logging.getLogger(__name__)

DRIVER_NAME = "wb-dauerhaft-pro"


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
        """
        topic = f"{self._base}/controls/{name}/on"
        self._on_topics.append(topic)
        self._client.subscribe(topic)
        self._client.message_callback_add(topic, callback)

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

    def error_topic(self) -> str:
        """
        The device-level error topic, usable as this connection's Last Will.

        One MQTT connection carries a single Will, so only one device's error
        can be the LWT target; the daemon uses the first device's. Every
        device's error is still updated live by the poll loop while the daemon
        runs — the Will only covers an ungraceful death of the process.
        """
        return f"{self._base}/meta/error"

    def set_error(self, error: str) -> None:
        """
        Set/clear the device-level availability error.

        Per WB conventions a non-empty ``/meta/error`` marks the device
        unavailable; ``r``/``w``/``p`` mean read / write / period-miss. Pass an
        empty string to clear it.
        """
        self._pub(self.error_topic(), error or "")

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
        """
        for topic in self._on_topics:
            self._client.unsubscribe(topic)
            self._client.message_callback_remove(topic)
        for name in self._controls:
            self._pub(f"{self._base}/controls/{name}", None)
            self._pub(f"{self._base}/controls/{name}/meta", None)
        self._pub(self.error_topic(), None)
        self._pub(f"{self._base}/meta", None)
        self._pub(f"{self._base}/meta/name", None)

    def _pub(self, topic: str, value) -> None:
        """
        Publish a retained value, skipping an unchanged repeat.

        The poll loop re-sends the same error/address every cycle; the dedup
        cache keeps those quiet. A publish dropped while disconnected stays
        cached on purpose — the daemon calls republish() on every (re)connect,
        which replays the whole cache and restores the device (broker restarts
        drop retained state), so recovery never hinges on one publish, and a
        one-shot meta topic dropped at startup is still restored.
        """
        if topic in self._last and self._last[topic] == value:
            return
        self._last[topic] = value
        self._client.publish(topic, value, retain=True)
