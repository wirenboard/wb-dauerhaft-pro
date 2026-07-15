"""
Config-loading unit tests: field mapping with the ms-to-seconds conversion,
and the uniqueness check the JSON schema cannot express.
"""

import json

import pytest

from wb.dauerhaft_pro import config

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


def test_config_parses_with_unit_conversion(tmp_path):
    path = _write(tmp_path, {"debug": True, "connection_check_interval_ms": 2500, "devices": [DEVICE]})
    conf = config.load_config(path, schema_path=str(tmp_path / "absent.schema.json"))
    assert conf.debug is True
    assert conf.check_interval_s == 2.5  # ms in the config, seconds inside the daemon
    dev = conf.devices[0]
    assert (dev.device_id, dev.address, dev.port.path) == ("dauerhaft_5f", 95, "/dev/ttyRS485-2")


def test_duplicate_device_id_rejected(tmp_path):
    path = _write(tmp_path, {"devices": [DEVICE, dict(DEVICE, rs485_address=11)]})
    with pytest.raises(config.ConfigError, match="must be unique"):
        config.load_config(path, schema_path=str(tmp_path / "absent.schema.json"))
