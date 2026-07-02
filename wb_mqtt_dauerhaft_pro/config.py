"""Config loading for the Dauerhaft PRO driver.

The config file is plain JSON edited via wb-mqtt-confed (see the .schema.json).
This module reads it, optionally validates against the schema, and turns each
entry into a :class:`~wb_mqtt_dauerhaft_pro.device.BlindConfig`.
"""

import json
import logging
from dataclasses import dataclass
from typing import List

from .device import BlindConfig, BlindType
from .transport import PortConfig

logger = logging.getLogger(__name__)

CONFIG_PATH = "/etc/wb-mqtt-dauerhaft-pro.conf"
SCHEMA_PATH = "/usr/share/wb-mqtt-confed/schemas/wb-mqtt-dauerhaft-pro.schema.json"


@dataclass
class DeviceEntry:
    mqtt_id: str
    title: str
    blind: BlindConfig


@dataclass
class Config:
    debug: bool
    devices: List[DeviceEntry]


def _build_entry(raw: dict) -> DeviceEntry:
    port = PortConfig(
        path=raw["port"],
        baud_rate=raw.get("baud_rate", 9600),
        parity=raw.get("parity", "N"),
        data_bits=raw.get("data_bits", 8),
        stop_bits=raw.get("stop_bits", 1),
    )
    blind = BlindConfig(
        name=raw["title"],
        address=int(raw["address"]),
        port=port,
        blind_type=BlindType(raw.get("type", "roller")),
        reverse_position=bool(raw.get("reverse_position", False)),
        poll_interval_s=float(raw.get("poll_interval_s", 1.0)),
    )
    return DeviceEntry(mqtt_id=raw["mqtt_id"], title=raw["title"], blind=blind)


def load_config(path: str = CONFIG_PATH, schema_path: str = SCHEMA_PATH) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    try:
        import jsonschema  # optional; validate if available and schema present
        with open(schema_path, "r", encoding="utf-8") as sf:
            jsonschema.validate(raw, json.load(sf))
    except FileNotFoundError:
        logger.debug("schema not found at %s, skipping validation", schema_path)
    except ImportError:
        logger.debug("jsonschema not installed, skipping validation")

    devices = [_build_entry(d) for d in raw.get("devices", [])]
    if not devices:
        logger.warning("no devices configured in %s", path)
    return Config(debug=bool(raw.get("debug", False)), devices=devices)
