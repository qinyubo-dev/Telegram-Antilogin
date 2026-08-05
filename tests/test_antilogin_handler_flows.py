# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from handlers import antilogin_handlers


def run(awaitable):
    return asyncio.run(awaitable)


def register(handler_bot):
    handler_bot.send_message = AsyncMock()
    run(antilogin_handlers.setup_antilogin_handlers(handler_bot))
    return handler_bot


def test_antilogin_menu_denied_empty_and_paginated(handler_bot, event_factory):
    register(handler_bot)
    callback = handler_bot.find("manage_antilogin_callback")
    denied = event_factory(data=b"antilogin_settings")
    with patch("handlers.antilogin_handlers.AccountManager.check_access", return_value=False):
        run(callback(denied))
    denied.answer.assert_awaited_once()
    empty = event_factory(data=b"antilogin_settings")
    with patch("handlers.antilogin_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.antilogin_handlers.AccountManager.get_user_accounts", return_value={}
    ), patch("handlers.antilogin_handlers.safe_edit", new=AsyncMock()) as edit:
        run(callback(empty))
    edit.assert_awaited_once()

    accounts = {f"+{100000 + index}": {"display_phone": str(index)} for index in range(30)}
    page = event_factory(data=b"antilogin_settings_1")
    with patch("handlers.antilogin_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.antilogin_handlers.AccountManager.get_user_accounts", return_value=accounts
    ), patch(
        "handlers.antilogin_handlers.AccountManager.get_antilogin_status_text", return_value="status"
    ), patch("handlers.antilogin_handlers.safe_edit", new=AsyncMock()) as edit:
        run(callback(page))
    buttons = edit.await_args.kwargs["buttons"]
    assert any(button.data == b"antilogin_settings_0" for row in buttons for button in row)


def test_antilogin_select_denied_and_missing(handler_bot, event_factory):
    register(handler_bot)
    denied = event_factory(data=b"antilogin_sel_+123")
    with patch("handlers.antilogin_handlers.AccountManager.check_access", return_value=False):
        run(handler_bot.find("antilogin_select_account")(denied))
    missing = event_factory(data=b"antilogin_sel_+123")
    with patch("handlers.antilogin_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.antilogin_handlers.AccountManager.get_user_accounts", return_value={}
    ), patch("handlers.antilogin_handlers.AccountManager.normalize_phone", return_value="+123"):
        run(handler_bot.find("antilogin_select_account")(missing))
    assert missing.answer.await_count >= 1


@pytest.mark.parametrize(
    ("name", "data", "operation"),
    [
        ("antilogin_enable", b"antilogin_on_+123", "resume_anti_login"),
        ("antilogin_pause", b"antilogin_pause_+123", "pause_anti_login"),
    ],
)
def test_antilogin_enable_and_pause(handler_bot, event_factory, name, data, operation):
    register(handler_bot)
    event = event_factory(data=data)
    with patch("handlers.antilogin_handlers.AccountManager.check_access", return_value=True), patch(
        f"handlers.antilogin_handlers.AccountManager.{operation}",
        new=AsyncMock(return_value="done"),
    ) as action, patch(
        "handlers.antilogin_handlers.AccountManager.get_user_accounts", return_value={"+123": {"anti_login": True}}
    ), patch("handlers.antilogin_handlers.AccountManager.normalize_phone", return_value="+123"), patch(
        "handlers.antilogin_handlers.AccountManager.get_antilogin_status_text", return_value="status"
    ), patch("handlers.antilogin_handlers.AccountManager.is_account_online", return_value=True), patch(
        "handlers.antilogin_handlers.safe_edit", new=AsyncMock()
    ):
        run(handler_bot.find(name)(event))
    assert action.await_count == 1
    event.answer.assert_awaited_once()


def test_new_device_action_invalid_missing_failure_unresolved_and_success(
    handler_bot, event_factory
):
    register(handler_bot)
    callback = handler_bot.find("new_device_authorization_action")
    invalid = event_factory(data=b"bad")
    run(callback(invalid))
    invalid.answer.assert_awaited_once()
    missing = event_factory(data=b"nda:a:123:9")
    with patch("handlers.antilogin_handlers.AccountManager.get_user_accounts", return_value={}):
        run(callback(missing))
    missing.answer.assert_awaited_once()

    failure = event_factory(
        data=b"nda:a:123:9",
        get_message=AsyncMock(return_value=SimpleNamespace(raw_text="prompt")),
    )
    with patch(
        "handlers.antilogin_handlers.AccountManager.get_user_accounts", return_value={"+123": {}}
    ), patch(
        "handlers.antilogin_handlers.AccountManager.resolve_new_authorization",
        new=AsyncMock(side_effect=RuntimeError("failure")),
    ):
        run(callback(failure))
    failure.answer.assert_awaited_once()

    unresolved = event_factory(
        data=b"nda:r:123:9",
        get_message=AsyncMock(return_value=SimpleNamespace(text="prompt")),
    )
    with patch(
        "handlers.antilogin_handlers.AccountManager.get_user_accounts", return_value={"+123": {}}
    ), patch(
        "handlers.antilogin_handlers.AccountManager.resolve_new_authorization",
        new=AsyncMock(return_value={"resolved": False, "message": "expired"}),
    ):
        run(callback(unresolved))
    assert unresolved.answer.await_args.args[0] == "expired"

    success = event_factory(
        data=b"nda:a:123:9",
        get_message=AsyncMock(return_value=SimpleNamespace(
            raw_text="Header\n\n" + antilogin_handlers.t("zh", "device.choose")
        )),
    )
    with patch(
        "handlers.antilogin_handlers.AccountManager.get_user_accounts", return_value={"+123": {}}
    ), patch(
        "handlers.antilogin_handlers.AccountManager.resolve_new_authorization",
        new=AsyncMock(return_value={"resolved": True, "message": "allowed"}),
    ), patch("handlers.antilogin_handlers.safe_edit", new=AsyncMock()) as edit:
        run(callback(success))
    assert edit.await_args.args[1].endswith("allowed")
    assert "请选择" not in edit.await_args.args[1]


def test_antilogin_delete_entry_cancel_and_confirm_success(handler_bot, event_factory):
    register(handler_bot)
    accounts = {"+123": {"display_phone": "shown"}}
    with patch("handlers.antilogin_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.antilogin_handlers.AccountManager.get_user_accounts", return_value=accounts
    ), patch("handlers.antilogin_handlers.AccountManager.normalize_phone", return_value="+123"), patch(
        "handlers.antilogin_handlers.safe_edit", new=AsyncMock()
    ) as edit:
        run(
            handler_bot.find("antilogin_delete")(
                event_factory(data=b"antilogin_del_+123")
            )
        )
    assert b"antilogin_del_confirm_+123" in [
        button.data for row in edit.await_args.kwargs["buttons"] for button in row
    ]

    with patch("handlers.antilogin_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.antilogin_handlers.AccountManager.get_user_accounts", return_value=accounts
    ), patch("handlers.antilogin_handlers.AccountManager.normalize_phone", return_value="+123"), patch(
        "handlers.antilogin_handlers.AccountManager.get_antilogin_status_text", return_value="status"
    ), patch("handlers.antilogin_handlers.AccountManager.is_account_online", return_value=False), patch(
        "handlers.antilogin_handlers.safe_edit", new=AsyncMock()
    ):
        cancel = event_factory(data=b"antilogin_del_cancel_+123")
        run(handler_bot.find("antilogin_delete_cancel")(cancel))
    cancel.answer.assert_awaited_once()

    confirm = event_factory(sender_id=1, data=b"antilogin_del_confirm_+123")
    with patch("handlers.antilogin_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.antilogin_handlers.AccountManager.delete_account",
        new=AsyncMock(return_value="🗑 deleted"),
    ), patch(
        "handlers.antilogin_handlers.delete_remembered_start_command", new=AsyncMock(return_value=True)
    ) as cleanup:
        run(handler_bot.find("antilogin_delete_confirm")(confirm))
    cleanup.assert_awaited_once_with(1)
    confirm.delete.assert_awaited_once_with()
    handler_bot.send_message.assert_awaited_once()


def test_antilogin_delete_confirm_failure_edits_card(handler_bot, event_factory):
    register(handler_bot)
    event = event_factory(data=b"antilogin_del_confirm_+123")
    with patch("handlers.antilogin_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.antilogin_handlers.AccountManager.delete_account",
        new=AsyncMock(side_effect=RuntimeError("delete")),
    ), patch("handlers.antilogin_handlers.safe_edit", new=AsyncMock()) as edit:
        run(handler_bot.find("antilogin_delete_confirm")(event))
    edit.assert_awaited_once()
