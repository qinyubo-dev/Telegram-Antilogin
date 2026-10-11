# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import pytest

import localization
import project_info


def test_attribution_passes_for_unmodified_project():
    project_info.verify_attribution()


def test_modified_notice_blocks_startup(monkeypatch):
    monkeypatch.setattr(project_info, "COPYRIGHT_NOTICE", "© someone else")
    with pytest.raises(RuntimeError, match="版权声明"):
        project_info.verify_attribution()


def test_modified_attribution_blocks_startup(monkeypatch):
    monkeypatch.setattr(project_info, "PROJECT_ATTRIBUTION", "removed")
    with pytest.raises(RuntimeError):
        project_info.verify_attribution()


def test_admin_panel_without_copyright_placeholder_blocks_startup(monkeypatch):
    patched = dict(localization.ZH)
    patched["admin.panel.text"] = "panel"
    monkeypatch.setattr(localization, "ZH", patched)
    with pytest.raises(RuntimeError):
        project_info.verify_attribution()


def test_missing_license_blocks_startup(monkeypatch):
    monkeypatch.setattr(project_info.os.path, "dirname", lambda _path: "/nonexistent")
    with pytest.raises(RuntimeError):
        project_info.verify_attribution()


def test_main_refuses_to_start_when_attribution_is_modified(monkeypatch):
    import asyncio

    import bot_main

    monkeypatch.setattr(project_info, "COPYRIGHT_NOTICE", "© someone else")
    with pytest.raises(RuntimeError, match="版权声明"):
        asyncio.run(bot_main.main())


def test_protection_wiring_is_present():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert "verify_attribution()" in (root / "bot_main.py").read_text(encoding="utf-8")
    assert "copyright=COPYRIGHT_NOTICE" in (
        root / "handlers" / "admin_handlers.py"
    ).read_text(encoding="utf-8")
    for name in ("CLAUDE.md", "AGENTS.md"):
        assert "版权保护说明" in (root / name).read_text(encoding="utf-8")
    assert "秦屿泊" in (root / "README.md").read_text(encoding="utf-8")
