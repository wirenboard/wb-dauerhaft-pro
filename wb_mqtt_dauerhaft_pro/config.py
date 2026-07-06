"""Config loading for the Dauerhaft PRO driver (MVP).

The config file is plain JSON edited via wb-mqtt-confed (see the .schema.json).
This module reads it, optionally validates against the schema if jsonschema is
present, and turns each entry into an
:class:`~wb_mqtt_dauerhaft_pro.device.ActuatorConfig`.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import List

from .device import ActuatorConfig
from .transport import PortConfig

logger = logging.getLogger(__name__)

CONFIG_PATH = "/etc/wb-mqtt-dauerhaft-pro.conf"
SCHEMA_PATH = "/usr/share/wb-mqtt-confed/schemas/wb-mqtt-dauerhaft-pro.schema.json"


@dataclass
class Config:
    debug: bool = False
    liveness_interval_s: float = 5.0
    devices: List[ActuatorConfig] = field(default_factory=list)


def _build_entry(raw: dict) -> ActuatorConfig:
    port = PortConfig(
        path=raw["port"],
        baud_rate=int(raw.get("baud_rate", 9600)),
        parity=raw.get("parity", "N"),
        data_bits=int(raw.get("data_bits", 8)),
        stop_bits=int(raw.get("stop_bits", 1)),
    )
    return ActuatorConfig(
        mqtt_id=raw["mqtt_id"],
        title=raw["title"],
        address=int(raw["address"]),
        port=port,
    )


def load_config(path: str = CONFIG_PATH, schema_path: str = SCHEMA_PATH) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    try:
        import jsonschema  # optional; validate only if available and schema present
        with open(schema_path, "r", encoding="utf-8") as sf:
            jsonschema.validate(raw, json.load(sf))
    except FileNotFoundError:
        logger.debug("schema not found at %s, skipping validation", schema_path)
    except ImportError:
        logger.debug("jsonschema not installed, skipping validation")

    devices = [_build_entry(d) for d in raw.get("devices", [])]
    if not devices:
        logger.warning("no devices configured in %s", path)
    return Config(
        debug=bool(raw.get("debug", False)),
        liveness_interval_s=float(raw.get("liveness_interval_s", 5.0)),
        devices=devices,
    )
