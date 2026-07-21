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
from dataclasses import dataclass
from typing import List

try:
    import jsonschema
except ImportError:  # absent on a dev box; the package declares it as a hard dependency
    jsonschema = None

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

    No field defaults on purpose: the fallback values live in one place —
    :func:`load_config` (mirroring the schema defaults) — not in three.
    """

    debug: bool
    check_interval_s: float
    devices: List[ActuatorConfig]


def _build_entry(raw: dict, index: int) -> ActuatorConfig:
    """
    Build one actuator config from a raw device entry.

    Raises :class:`ConfigError` on a missing or invalid field, or a value the
    schema would reject, so a config that skipped schema validation still fails
    at startup instead of at the first bus exchange.
    """
    try:
        entry = ActuatorConfig(
            device_id=raw["device_id"],
            name=raw["device_name"],
            curtain_type=raw["curtain_type"],
            learning_type=raw["learning_type"],
            address=int(raw["rs485_address"]),
            port=PortConfig(path=raw["port"]),
            slat_angle_mode=raw.get("slat_angle_mode", "none"),
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
    # Same reasoning for the id (schema pattern ^[^$#+/]+$): it goes straight
    # into topic names, so '/' would write into — and on shutdown clear —
    # another device's topic tree, and '+'/'#' would corrupt the command
    # subscription filter.
    bad_id = not isinstance(entry.device_id, str) or not entry.device_id
    if bad_id or any(char in entry.device_id for char in "$#+/"):
        raise ConfigError(f"device #{index}: device_id must be a non-empty string without '$ # + /'")
    # Same reasoning: the slat_angle_mode drives the wire scale, so an unknown
    # value must fail at startup, not at the first telemetry read.
    if entry.slat_angle_mode not in ("none", "direct", "compressed"):
        raise ConfigError(
            f"device #{index}: slat_angle_mode must be none/direct/compressed, got {entry.slat_angle_mode!r}"
        )
    return entry


def _validate_against_schema(raw, path: str, schema_path: str) -> None:
    """
    Validate *raw* against the installed confed schema.

    A missing jsonschema module (dev box) or a missing schema file only skip
    the validation; a schema that is present but broken is a broken
    installation and refuses startup like any other config problem.
    """
    if jsonschema is None:
        # warning, not debug: on a controller this means a broken installation
        logger.warning("python3-jsonschema is not installed; skipping config schema validation")
        return
    try:
        with open(schema_path, "r", encoding="utf-8") as schema_file:
            schema = json.load(schema_file)
    except FileNotFoundError:
        logger.debug("schema not found at %s, skipping validation", schema_path)
        return
    except UnicodeDecodeError as err:
        raise ConfigError(f"installed schema {schema_path} is not valid UTF-8: {err}") from err
    except OSError as err:
        # a present but unreadable schema is a broken installation, not a dev box
        raise ConfigError(f"cannot read installed schema {schema_path}: {err}") from err
    except json.JSONDecodeError as err:
        # a corrupted installed schema must give a clear refusal, not a traceback
        raise ConfigError(f"installed schema {schema_path} is not valid JSON: {err}") from err
    try:
        jsonschema.validate(raw, schema)
    except jsonschema.SchemaError as err:
        raise ConfigError(
            f"installed schema {schema_path} is not a valid JSON Schema: {err.message}"
        ) from err
    except jsonschema.ValidationError as err:
        raise ConfigError(f"{path} failed schema validation: {err.message}") from err


def load_config(path: str = CONFIG_PATH, schema_path: str = SCHEMA_PATH) -> Config:
    """
    Load and parse the config. Raises :class:`ConfigError` on any problem.
    """
    try:
        with open(path, "r", encoding="utf-8") as config_file:
            raw = json.load(config_file)
    except UnicodeDecodeError as err:
        # a hand-edited or backup-restored config saved in a non-UTF-8 encoding
        # (Cyrillic names are common here) must refuse cleanly, not traceback
        raise ConfigError(f"{path} is not valid UTF-8: {err}") from err
    except OSError as err:
        raise ConfigError(f"cannot read {path}: {err}") from err
    except json.JSONDecodeError as err:
        raise ConfigError(f"{path} is not valid JSON: {err}") from err

    _validate_against_schema(raw, path, schema_path)

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a JSON object")

    raw_devices = raw.get("devices", [])
    if not isinstance(raw_devices, list):
        raise ConfigError(f"{path}: 'devices' must be an array")
    devices = [_build_entry(raw_dev, index) for index, raw_dev in enumerate(raw_devices)]
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
    # Enforce the schema minimum too: a zero or negative interval would turn the
    # poll loop into a busy-loop hammering the shared RS-485 bus.
    if interval_ms < 100:
        raise ConfigError(f"connection_check_interval_ms must be >= 100, got {interval_ms}")
    return Config(
        debug=bool(raw.get("debug", False)),
        check_interval_s=interval_ms / 1000.0,
        devices=devices,
    )
