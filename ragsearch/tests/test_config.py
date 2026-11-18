import json
import os

import pytest

from ragsearch import config


def test_load_registry_resolves_relative_paths(tmp_path):
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"foo": "some/dir"}), encoding="utf-8")
    kbs = config.load_registry(str(reg))
    assert kbs["foo"] == os.path.join(config._PLAYWRIGHT_DIR, "some/dir")


def test_load_registry_keeps_absolute_paths(tmp_path):
    reg = tmp_path / "registry.json"
    abs_dir = str(tmp_path / "data")
    reg.write_text(json.dumps({"bar": abs_dir}), encoding="utf-8")
    kbs = config.load_registry(str(reg))
    assert kbs["bar"] == abs_dir


def test_load_registry_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        config.load_registry(str(tmp_path / "nope.json"))
