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
import settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_localization_fallbacks():
    with pytest.raises(KeyError, match="unknown localization key"):
        localization.t("missing.key")
    assert localization.localized_result("验证码已发送") == "验证码已发送"
    assert localization.localized_payment_error(None) == ""


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
