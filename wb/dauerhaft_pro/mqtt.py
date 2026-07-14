"""Minimal Wiren Board MQTT-conventions helper: a virtual device with controls.

Publishes retained ``/devices/<id>/...`` topics and registers per-control command
callbacks on ``<control>/on``. Modeled on wb-mqtt-urri's ``wbmqtt.Device``; works
on any paho-compatible client — the daemon passes the shared
``wb_common.mqtt_client.MQTTClient`` (the same client the RPC transport uses).

Meta format (per WB conventions)::

    /devices/<id>/meta         {"driver":..., "title":{"en":..,"ru":..}}  (single JSON)
    /devices/<id>/meta/name    "<title>"                (legacy backward-compat only)
    /devices/<id>/controls/<c>/meta  {"type","readonly","order","title":{...}}
    /devices/<id>/controls/<c>       "<value>"          (retained)
    /devices/<id>/controls/<c>/on    <- command (subscribed)
"""

import json
import logging

logger = logging.getLogger(__name__)

DRIVER_NAME = "wb-dauerhaft-pro"


class WbDevice:
    def __init__(self, client, device_id, title, driver=DRIVER_NAME):
        self._c = client
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
        name,
        control_type,
        order,
        *,
        readonly=False,
        title=None,
        min_value=None,
        max_value=None,
        initial="",
    ):
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

    def on_command(self, name, callback):
        """Subscribe to <control>/on and route matching messages to *callback*."""
        topic = f"{self._base}/controls/{name}/on"
        self._on_topics.append(topic)
        self._c.subscribe(topic)
        self._c.message_callback_add(topic, callback)

    def resubscribe(self):
        """Re-subscribe all command topics (a broker reconnect drops them)."""
        for topic in self._on_topics:
            self._c.subscribe(topic)

    def set_value(self, name, value):
        self._pub(f"{self._base}/controls/{name}", str(value))

    def error_topic(self):
        """The device-level error topic (usable as a Last Will target)."""
        return f"{self._base}/meta/error"

    def set_error(self, error):
        """Set/clear the device-level availability error.

        Per WB conventions a non-empty ``/meta/error`` marks the device
        unavailable; ``r``/``w``/``p`` mean read / write / period-miss. Pass an
        empty string to clear it.
        """
        self._pub(self.error_topic(), error or "")

    def remove(self):
        """Clear all retained topics for this device (called on shutdown)."""
        for name in self._controls:
            self._pub(f"{self._base}/controls/{name}", None)
            self._pub(f"{self._base}/controls/{name}/meta", None)
        self._pub(self.error_topic(), None)
        self._pub(f"{self._base}/meta", None)
        self._pub(f"{self._base}/meta/name", None)

    def _pub(self, topic, value):
        # Skip republishing an unchanged retained value — the poll loop would
        # otherwise resend the same error/address to every /devices/# subscriber
        # on each cycle.
        if topic in self._last and self._last[topic] == value:
            return
        self._last[topic] = value
        self._c.publish(topic, value, retain=True)
