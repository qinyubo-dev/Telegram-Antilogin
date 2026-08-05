# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import localization
import migrate_runtime_data
import settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_localization_catalog_validation_and_fallbacks():
    assert localization._fields("hello {user.name} {items[0]}") == {"user", "items"}
    localization.validate_catalogs()
    with patch.object(localization, "EN", {**localization.EN, "extra": "x"}):
        with pytest.raises(RuntimeError, match="locale key mismatch"):
            localization.validate_catalogs()
    changed = dict(localization.EN)
    key = next(iter(changed))
    changed[key] = changed[key] + " {different}"
    with patch.object(localization, "EN", changed):
        with pytest.raises(RuntimeError, match="placeholder mismatch"):
            localization.validate_catalogs()
    assert localization.t("unsupported", "common.back") == localization.t("zh", "common.back")
    with pytest.raises(KeyError, match="unknown localization key"):
        localization.t("en", "missing.key")
    assert localization.localized_result("zh", "验证码已发送") == "验证码已发送"
    assert "Login code" in localization.localized_result("en", "验证码已发送")
    assert localization.localized_result("en", "未匹配中文") == "The operation finished."


def test_settings_numeric_paths_and_validation(tmp_path):
    assert settings.API_ID == 2040
    assert settings.API_HASH == "b18441a1ff607e10a989891a5462e627"
    assert settings.PAYMENT_RETURN_URL == "https://t.me/QinShield_Bot"

    with patch("settings._configured", return_value=2):
        assert settings._positive_int("X", 1) == 2
        assert settings._positive_float("X", 1) == 2.0
    with patch("settings._configured", return_value=0):
        with pytest.raises(ValueError):
            settings._positive_int("X", 1)
        with pytest.raises(ValueError):
            settings._positive_float("X", 1)
    assert settings._resolve_root(tmp_path) == tmp_path.resolve()
    with patch.object(settings, "DATA_ROOT", tmp_path), patch("settings._configured", return_value="x.json"):
        assert settings._runtime_path("X", "x.json") == str((tmp_path / "x.json").resolve())

    required = ("API_ID", "API_HASH", "BOT_TOKEN", "MERCHANT_ID", "PAYMENT_TOKEN")
    patches = [patch.object(settings, name, 0 if name == "API_ID" else "") for name in required]
    for item in patches:
        item.start()
    try:
        with pytest.raises(RuntimeError, match="Missing required"):
            settings.validate_runtime_settings()
    finally:
        for item in reversed(patches):
            item.stop()
    with patch.object(settings, "API_ID", 1), patch.object(settings, "API_HASH", "hash"), patch.object(
        settings, "BOT_TOKEN", "token"
    ), patch.object(settings, "MERCHANT_ID", "merchant"), patch.object(settings, "PAYMENT_TOKEN", "payment"):
        settings.validate_runtime_settings()


def test_config_template_only_contains_operator_values():
    tree = ast.parse(
        (PROJECT_ROOT / "config.example.py").read_text(encoding="utf-8")
    )
    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert assigned == {
        "API_ID",
        "API_HASH",
        "BOT_TOKEN",
        "ADMIN_IDS",
        "MERCHANT_ID",
        "PAYMENT_TOKEN",
        "PAYMENT_RETURN_URL",
    }


def test_migration_read_stage_apply_and_main(tmp_path, capsys):
    missing = tmp_path / "missing.json"
    assert migrate_runtime_data._read_json(missing, {"x": 1}) == {"x": 1}
    broken = tmp_path / "broken.json"
    broken.write_text("bad", encoding="utf-8")
    with pytest.raises(ValueError):
        migrate_runtime_data._read_json(broken, {})

    target = tmp_path / "data.json"
    target.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert migrate_runtime_data.apply_migration({target: {"a": 1}}) == []
    changed = migrate_runtime_data.apply_migration({target: {"a": 2}})
    assert changed == [target]
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}
    assert list(tmp_path.glob("*.migration.backup"))

    outputs = {target: {"a": 3}}
    report = {"ok": True}
    with patch.object(migrate_runtime_data, "build_migration", return_value=(outputs, report)), patch.object(
        migrate_runtime_data.settings, "DATA_FILE", str(target)
    ), patch.object(migrate_runtime_data.settings, "PAYMENT_ORDERS_FILE", str(target)), patch.object(
        migrate_runtime_data.settings, "HOSTED_ACCOUNT_METADATA_FILE", str(target)
    ), patch.object(migrate_runtime_data.settings, "SESSIONS_DIR", str(tmp_path)), patch.object(
        sys, "argv", ["migrate_runtime_data.py", "--check"]
    ):
        assert migrate_runtime_data.main() == 0
    assert '"ok": true' in capsys.readouterr().out
