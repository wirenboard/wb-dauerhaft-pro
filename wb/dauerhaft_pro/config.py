"""
Config loading for the Dauerhaft PRO driver.

The config file is plain JSON edited via wb-mqtt-confed (see the .schema.json).
This module reads it, validates it against the installed confed schema (the same
file the editor uses — configs may also be edited by hand over SSH or restored
from a backup), applies the semantic checks the schema cannot express, and turns
each entry into an :class:`~wb.dauerhaft_pro.device.ActuatorConfig`.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import List

from .device import ActuatorConfig
from .transport import PortConfig

logger = logging.getLogger(__name__)

CONFIG_PATH = "/etc/wb-dauerhaft-pro.conf"
SCHEMA_PATH = "/usr/share/wb-mqtt-confed/schemas/wb-dauerhaft-pro.schema.json"


class ConfigError(Exception):
    """
    The config file is missing, unreadable or invalid.
    """


@dataclass
class Config:
    """
    Parsed daemon configuration: global flags plus one entry per actuator.
    """

    debug: bool = False
    check_interval_s: float = 5.0
    devices: List[ActuatorConfig] = field(default_factory=list)


def _build_entry(raw: dict, index: int) -> ActuatorConfig:
    try:
        entry = ActuatorConfig(
            device_id=raw["device_id"],
            name=raw["device_name"],
            curtain_type=raw["curtain_type"],
            learning_type=raw["learning_type"],
            address=int(raw["rs485_address"]),
            port=PortConfig(path=raw["port"]),
        )
    except KeyError as err:
        raise ConfigError(f"device #{index}: missing required field {err}") from err
    except (TypeError, ValueError) as err:
        raise ConfigError(f"device #{index}: invalid field value: {err}") from err
    # The schema limits the address to 1..255 too; the guard matters when schema
    # validation was skipped — a bad address must fail at startup, not at the
    # first exchange.
    if not 1 <= entry.address <= 0xFF:
        raise ConfigError(f"device #{index}: rs485_address must be 1..255, got {entry.address}")
    return entry


def load_config(path: str = CONFIG_PATH, schema_path: str = SCHEMA_PATH) -> Config:
    """
    Load and parse the config. Raises :class:`ConfigError` on any problem.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as err:
        raise ConfigError(f"cannot read {path}: {err}") from err
    except json.JSONDecodeError as err:
        raise ConfigError(f"{path} is not valid JSON: {err}") from err

    try:
        import jsonschema  # validate when available; a hard dependency on the controller

        with open(schema_path, "r", encoding="utf-8") as sf:
            jsonschema.validate(raw, json.load(sf))
    except FileNotFoundError:
        logger.debug("schema not found at %s, skipping validation", schema_path)
    except ImportError:
        logger.debug("jsonschema not installed, skipping validation")
    except OSError as err:
        # a present but unreadable schema is a broken installation, not a dev box
        raise ConfigError(f"cannot read installed schema {schema_path}: {err}") from err
    except json.JSONDecodeError as err:
        # a corrupted installed schema must give a clear refusal, not a traceback
        raise ConfigError(f"installed schema {schema_path} is not valid JSON: {err}") from err
    except jsonschema.SchemaError as err:
        raise ConfigError(
            f"installed schema {schema_path} is not a valid JSON Schema: {err.message}"
        ) from err
    except jsonschema.ValidationError as err:
        raise ConfigError(f"{path} failed schema validation: {err.message}") from err

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a JSON object")

    devices = [_build_entry(d, i) for i, d in enumerate(raw.get("devices", []))]
    # The schema cannot express uniqueness. Duplicate device ids would make two
    # actuators fight over one set of /devices/<id>/... topics, so refuse the
    # config. RS-485 addresses MAY repeat — the protocol allows identical
    # addresses on a bus — so they are deliberately not checked.
    seen = {}
    for i, dev in enumerate(devices):
        if dev.device_id in seen:
            raise ConfigError(
                f"devices #{seen[dev.device_id]} and #{i} share the MQTT device id "
                f"{dev.device_id!r}; device ids must be unique"
            )
        seen[dev.device_id] = i
    if not devices:
        logger.warning("no devices configured in %s", path)
    try:
        # ms in the config (schema minimum 100), seconds inside the daemon. The
        # guard matters when schema validation was skipped (schema not installed).
        interval_ms = int(raw.get("connection_check_interval_ms", 5000))
    except (TypeError, ValueError) as err:
        raise ConfigError(f"connection_check_interval_ms must be an integer: {err}") from err
    return Config(
        debug=bool(raw.get("debug", False)),
        check_interval_s=interval_ms / 1000.0,
        devices=devices,
    )
