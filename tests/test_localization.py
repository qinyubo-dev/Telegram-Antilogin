# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import copy
import json
import ast
from pathlib import Path

from localization import EN, ZH, normalize_language, t
from storage import data_manager
from storage.data_manager import DataManager
from handlers.bot_handlers import (
    LANGUAGE_CUSTOM_EMOJI_ID,
    language_buttons,
    main_menu_buttons,
    more_menu_buttons,
)


def test_catalogs_have_matching_keys_and_placeholders():
    assert set(ZH) == set(EN)
    assert t("zh", "main.add_account") == "添加账户"
    assert t("en", "main.add_account") == "Add account"


def test_telegram_language_normalization():
    assert normalize_language("zh") == "zh"
    assert normalize_language("zh-CN") == "zh"
    assert normalize_language("zh_Hans") == "zh"
    assert normalize_language("en-US") == "en"
    assert normalize_language("ru") == "en"
    assert normalize_language(None) == "zh"
    assert normalize_language("") == "zh"


def test_help_covers_account_cleanup_in_both_languages():
    zh_help = t("zh", "help.text")
    en_help = t("en", "help.text")
    assert "清理账户：删除全部对话、联系人，或执行全部清理" in zh_help
    assert "清理操作不可恢复" in zh_help
    assert "delete all chats, contacts, or everything" in en_help
    assert "It cannot be undone" in en_help


def test_language_is_initialized_once_and_round_trips(tmp_path, monkeypatch):
    original_data = data_manager.user_data
    original_loaded = data_manager.data_load_succeeded
    original_orders = data_manager.payment_orders
    original_orders_loaded = data_manager.payment_orders_load_succeeded
    try:
        users_path = tmp_path / "users.json"
        orders_path = tmp_path / "orders.json"
        monkeypatch.setattr(data_manager, "DATA_FILE", str(users_path))
        monkeypatch.setattr(data_manager, "PAYMENT_ORDERS_FILE", str(orders_path))
        data_manager.user_data = DataManager._default_data()
        data_manager.payment_orders = {}
        data_manager.data_load_succeeded = True
        data_manager.payment_orders_load_succeeded = True

        assert DataManager.initialize_user_language(42, "en-US")
        assert DataManager.get_user_language(42) == "en"
        assert DataManager.initialize_user_language(42, "zh-CN")
        assert DataManager.get_user_language(42) == "en"

        with users_path.open(encoding="utf-8") as stream:
            assert json.load(stream)["42"]["language"] == "en"
    finally:
        data_manager.user_data = original_data
        data_manager.data_load_succeeded = original_loaded
        data_manager.payment_orders = original_orders
        data_manager.payment_orders_load_succeeded = original_orders_loaded
        DataManager.rebuild_subscription_index()


def test_language_update_rolls_back_when_save_fails(monkeypatch):
    original_data = data_manager.user_data
    original_loaded = data_manager.data_load_succeeded
    try:
        data_manager.user_data = {7: {"language": "zh"}}
        data_manager.data_load_succeeded = True
        before = copy.deepcopy(data_manager.user_data[7])
        monkeypatch.setattr(DataManager, "save_user_data", staticmethod(lambda: False))

        assert not DataManager.set_user_language(7, "en")
        assert data_manager.user_data[7] == before
        assert not DataManager.set_user_language(7, "fr")
    finally:
        data_manager.user_data = original_data
        data_manager.data_load_succeeded = original_loaded
        DataManager.rebuild_subscription_index()


def test_main_menu_and_admin_entry_use_saved_language(monkeypatch):
    original_data = data_manager.user_data
    try:
        data_manager.user_data = {9: {"language": "en"}}
        monkeypatch.setattr(DataManager, "is_admin", staticmethod(lambda user_id: True))
        labels = [button.text for row in main_menu_buttons(9) for button in row]
        assert "Add account" in labels
        assert "More" in labels
        assert "Admin panel" in labels
        assert [[button.text for button in row] for row in language_buttons()] == [
            ["简体中文", "English"]
        ]
        language_choices = [button for row in language_buttons() for button in row]
        assert all(button.style.icon == LANGUAGE_CUSTOM_EMOJI_ID for button in language_choices)
        language_entries = [button for row in more_menu_buttons(9) for button in row if button.data == b"language_menu"]
        assert len(language_entries) == 1
        assert language_entries[0].style.icon == LANGUAGE_CUSTOM_EMOJI_ID
        data_manager.user_data[9]["language"] = "zh"
        assert "更多功能" in [button.text for row in main_menu_buttons(9) for button in row]
        assert "切换语言" in [button.text for row in more_menu_buttons(9) for button in row]
        assert t("zh", "language.choose") == "请选择界面语言\nChoose a language"
        assert t("en", "language.choose") == "请选择界面语言\nChoose a language"
    finally:
        data_manager.user_data = original_data
        DataManager.rebuild_subscription_index()


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
            if file_name == "handlers/bot_handlers.py" and "language_set_zh" in segment:
                continue
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
