"""Unit tests for config loading and its error handling."""

import json

import pytest

from wb_dauerhaft_pro.config import ConfigError, load_config

NO_SCHEMA = "/nonexistent/schema.json"  # skips validation (file/ImportError handled)


def _write(tmp_path, text):
    path = tmp_path / "conf.json"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_missing_file_raises_config_error():
    with pytest.raises(ConfigError):
        load_config("/nonexistent/path/wb-dauerhaft-pro.conf", NO_SCHEMA)


def test_bad_json_raises_config_error(tmp_path):
    path = _write(tmp_path, "{ not valid json")
    with pytest.raises(ConfigError):
        load_config(path, NO_SCHEMA)


def test_missing_required_field_raises_config_error(tmp_path):
    path = _write(tmp_path, json.dumps({"devices": [{"mqtt_id": "x", "title": "X"}]}))
    with pytest.raises(ConfigError):
        load_config(path, NO_SCHEMA)


def test_valid_config_parses(tmp_path):
    path = _write(
        tmp_path,
        json.dumps({"devices": [{"mqtt_id": "x", "title": "X", "address": 95, "port": "/dev/ttyRS485-2"}]}),
    )
    conf = load_config(path, NO_SCHEMA)
    assert len(conf.devices) == 1
    assert conf.devices[0].address == 95
    assert conf.devices[0].port.path == "/dev/ttyRS485-2"


def test_empty_devices_is_ok(tmp_path):
    path = _write(tmp_path, json.dumps({"devices": []}))
    conf = load_config(path, NO_SCHEMA)
    assert conf.devices == []


def test_schema_validation_error_raises_config_error(tmp_path):
    pytest.importorskip("jsonschema")  # only meaningful when jsonschema is installed
    schema = {
        "type": "object",
        "required": ["devices"],
        "properties": {"devices": {"type": "array", "items": {"type": "object", "required": ["address"]}}},
    }
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    # valid JSON, but the device is missing the schema-required "address"
    conf_path = _write(tmp_path, json.dumps({"devices": [{"mqtt_id": "x", "title": "X"}]}))
    with pytest.raises(ConfigError):
        load_config(conf_path, str(schema_path))
