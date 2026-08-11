"""
Config-loading unit tests: field mapping with the ms-to-seconds conversion,
the checks the JSON schema cannot express, and the startup guards that back up
the schema when validation is skipped.
"""

import json
import logging
import os

import pytest

from wb.dauerhaft_pro import config

REAL_SCHEMA = os.path.join(os.path.dirname(__file__), "..", "configs", "wb-dauerhaft-pro.schema.json")

DEVICE = {
    "device_id": "dauerhaft_5f",
    "device_name": "Штора",
    "curtain_type": "roller",
    "learning_type": "physical_button",
    "rs485_address": 95,
    "port": "/dev/ttyRS485-2",
}


def _write(tmp_path, content):
    path = tmp_path / "test.conf"
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _load_without_schema(tmp_path, path):
    return config.load_config(path, schema_path=str(tmp_path / "absent.schema.json"))


def test_config_parses_with_unit_conversion(tmp_path):
    """
    Device fields parse correctly and the ms interval in the config becomes seconds.
    """
    path = _write(tmp_path, {"debug": True, "connection_check_interval_ms": 2500, "devices": [DEVICE]})
    conf = _load_without_schema(tmp_path, path)
    assert conf.debug is True
    assert conf.check_interval_s == 2.5  # ms in the config, seconds inside the daemon
    dev = conf.devices[0]
    assert (dev.device_id, dev.address, dev.port.path) == ("dauerhaft_5f", 95, "/dev/ttyRS485-2")


def test_legacy_config_without_slat_mode_passes_real_schema(tmp_path, caplog):
    """
    A config from before slat_angle_mode became required in the editor schema
    (the device entry has no such field) must still validate and load: the
    loader defaults the field before schema validation, saying so in the log.
    """
    pytest.importorskip("jsonschema")
    path = _write(tmp_path, {"devices": [DEVICE]})
    with caplog.at_level(logging.INFO, logger="wb.dauerhaft_pro.config"):
        conf = config.load_config(path, schema_path=REAL_SCHEMA)
    assert conf.devices[0].slat_angle_mode == "none"
    assert "slat_angle_mode is not set, defaulting to none" in caplog.text


def test_duplicate_device_id_rejected(tmp_path):
    """
    Two identical device_ids raise ConfigError (a check the schema cannot express).
    """
    path = _write(tmp_path, {"devices": [DEVICE, dict(DEVICE, rs485_address=11)]})
    with pytest.raises(config.ConfigError, match="must be unique"):
        _load_without_schema(tmp_path, path)


@pytest.mark.parametrize(
    "content,match",
    [
        ({"devices": [dict(DEVICE, device_id="bad/id")]}, "device_id"),
        ({"devices": [dict(DEVICE, rs485_address=300)]}, "1..254"),
        ({"devices": [{k: v for k, v in DEVICE.items() if k != "port"}]}, "missing required field"),
        ({"devices": "oops"}, "must be an array"),
        ({"devices": [DEVICE], "connection_check_interval_ms": 0}, ">= 100"),
    ],
    ids=[
        "device-id-metachar",
        "address-out-of-range",
        "missing-field",
        "devices-not-a-list",
        "interval-too-small",
    ],
)
def test_skipped_schema_guards_reject_bad_config(tmp_path, content, match):
    """
    The manual startup guards (metachar in the id, address out of range, a
    missing field, a non-list "devices", interval < 100) reject a bad config
    when schema validation was skipped.
    """
    # These back up the schema for the case it was skipped (dev box or broken
    # install); on a healthy controller the schema catches them first.
    path = _write(tmp_path, content)
    with pytest.raises(config.ConfigError, match=match):
        _load_without_schema(tmp_path, path)
