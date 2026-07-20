"""
Config-loading unit tests: field mapping with the ms-to-seconds conversion,
the checks the JSON schema cannot express, the startup guards that back up the
schema when validation is skipped, and validation against the shipped schema.
"""

import json
import pathlib

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


def _find_shipped_schema():
    # Walk up to the repo root that holds configs/ — robust to the extra nesting
    # of the pybuild build tree (.pybuild/.../build/tests/) so the test runs in
    # CI, not only from a plain checkout.
    for parent in pathlib.Path(__file__).resolve().parents:
        candidate = parent / "configs" / "wb-dauerhaft-pro.schema.json"
        if candidate.exists():
            return str(candidate)
    return None


# The schema this package ships and installs; used to exercise the real
# validation path (the one that runs on every controller startup).
REPO_SCHEMA = _find_shipped_schema()


def _write(tmp_path, content):
    path = tmp_path / "test.conf"
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _load_without_schema(tmp_path, path):
    return config.load_config(path, schema_path=str(tmp_path / "absent.schema.json"))


def test_config_parses_with_unit_conversion(tmp_path):
    path = _write(tmp_path, {"debug": True, "connection_check_interval_ms": 2500, "devices": [DEVICE]})
    conf = _load_without_schema(tmp_path, path)
    assert conf.debug is True
    assert conf.check_interval_s == 2.5  # ms in the config, seconds inside the daemon
    dev = conf.devices[0]
    assert (dev.device_id, dev.address, dev.port.path) == ("dauerhaft_5f", 95, "/dev/ttyRS485-2")


def test_duplicate_device_id_rejected(tmp_path):
    path = _write(tmp_path, {"devices": [DEVICE, dict(DEVICE, rs485_address=11)]})
    with pytest.raises(config.ConfigError, match="must be unique"):
        _load_without_schema(tmp_path, path)


@pytest.mark.parametrize(
    "content,match",
    [
        ({"devices": [dict(DEVICE, device_id="bad/id")]}, "device_id"),
        ({"devices": [dict(DEVICE, rs485_address=300)]}, "1..255"),
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
    # These back up the schema for the case it was skipped (dev box or broken
    # install); on a healthy controller the schema catches them first.
    path = _write(tmp_path, content)
    with pytest.raises(config.ConfigError, match=match):
        _load_without_schema(tmp_path, path)


def test_non_utf8_config_rejected(tmp_path):
    # a hand-edited / backup-restored config saved in cp1251 (Cyrillic names)
    path = tmp_path / "test.conf"
    path.write_bytes(json.dumps({"devices": [DEVICE]}, ensure_ascii=False).encode("cp1251"))
    with pytest.raises(config.ConfigError, match="not valid UTF-8"):
        _load_without_schema(tmp_path, str(path))


def test_real_schema_accepts_valid_and_rejects_invalid(tmp_path):
    # Exercises the production validation path against the shipped schema — and
    # so also proves the schema is valid and agrees with the parser's fields.
    pytest.importorskip("jsonschema")
    assert REPO_SCHEMA is not None, "shipped schema not found next to the tests"
    good = config.load_config(_write(tmp_path, {"devices": [DEVICE]}), schema_path=REPO_SCHEMA)
    assert good.devices[0].device_id == "dauerhaft_5f"
    bad = _write(tmp_path, {"devices": [dict(DEVICE, rs485_address=0)]})  # schema minimum is 1
    with pytest.raises(config.ConfigError, match="failed schema validation"):
        config.load_config(bad, schema_path=REPO_SCHEMA)
