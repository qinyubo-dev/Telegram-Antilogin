# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import ast
from pathlib import Path

from localization import ZH, t
from storage.data_manager import DataManager
from handlers.bot_handlers import main_menu_buttons, more_menu_buttons


def test_catalog_lookup_and_unknown_key():
    assert t("zh", "main.add_account") == "添加账户"
    assert t("anything", "main.add_account") == "添加账户"
    assert "language.menu" not in ZH


def test_language_is_always_chinese():
    assert DataManager.get_user_language(42) == "zh"


def test_main_and_more_menus_have_no_language_entry(monkeypatch):
    monkeypatch.setattr(DataManager, "is_admin", staticmethod(lambda user_id: True))
    assert "更多功能" in [b.text for row in main_menu_buttons(9) for b in row]
    entries = [b for row in more_menu_buttons(9) for b in row if b.data == b"language_menu"]
    assert not entries


def test_regular_user_output_has_no_inline_chinese_literals():
    """New UI text must go through localization; admin handlers are exempt."""
    files = (
        "handlers/account_handlers.py",
        "handlers/antilogin_handlers.py",
        "handlers/hosting_handlers.py",
        "handlers/transfer_handlers.py",
        "handlers/vip_handlers.py",
        "handlers/bot_handlers.py",
        "payments/payment_system.py",
        "reminders/reminder_system.py",
    )
    output_calls = {
        "respond", "answer", "send_message", "send_file", "safe_edit",
        "safe_edit_message", "edit_status_or_send", "update_login_status",
        "Button.inline", "Button.url",
    }
    failures = []
    for file_name in files:
        source = Path(file_name).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                base = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
                call_name = (base + "." if base else "") + node.func.attr
            elif isinstance(node.func, ast.Name):
                call_name = node.func.id
            else:
                continue
            if call_name not in output_calls and call_name.rsplit(".", 1)[-1] not in {
                "respond", "answer", "send_message", "send_file"
            }:
                continue
            segment = ast.get_source_segment(source, node) or ""
            if any("\u4e00" <= char <= "\u9fff" for char in segment):
                failures.append(f"{file_name}:{node.lineno}")
    assert not failures, "User-visible literals outside localization.py: " + ", ".join(failures)


def test_admin_output_calls_have_no_inline_chinese_literals():
    source = Path("handlers/admin_handlers.py").read_text(encoding="utf-8")
    output_names = {
        "respond", "answer", "safe_edit", "edit_or_respond", "Button.inline",
        "send_file",
    }
    failures = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            base = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
            call_name = (base + "." if base else "") + node.func.attr
        elif isinstance(node.func, ast.Name):
            call_name = node.func.id
        else:
            continue
        if call_name not in output_names and call_name.rsplit(".", 1)[-1] not in {
            "respond", "answer",
        }:
            continue
        segment = ast.get_source_segment(source, node) or ""
        if any("\u4e00" <= char <= "\u9fff" for char in segment):
            failures.append(f"handlers/admin_handlers.py:{node.lineno}")
    assert not failures, "Admin-visible literals outside localization.py: " + ", ".join(failures)
