# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from handlers import hosting_handlers
from handlers.handler_utils import clear_state, get_state, set_state


def run(awaitable):
    return asyncio.run(awaitable)


def register(handler_bot):
    run(hosting_handlers.setup_hosting_handlers(handler_bot))
    return handler_bot


class PatternMatch:
    def __init__(self, *groups):
        self.groups = groups

    def group(self, index):
        return self.groups[index - 1]


def test_hosting_menu_denied_empty_and_paginated(handler_bot, event_factory):
    register(handler_bot)
    callback = handler_bot.find("hosting_menu")
    denied = event_factory(data=b"hosting_menu")
    with patch("handlers.hosting_handlers.AccountManager.check_access", return_value=False):
        run(callback(denied))
    denied.answer.assert_awaited_once()

    empty = event_factory(data=b"hosting_menu")
    with patch("handlers.hosting_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.hosting_handlers.AccountManager.get_user_accounts", return_value={}
    ), patch("handlers.hosting_handlers.safe_edit", new=AsyncMock()) as edit:
        run(callback(empty))
    assert edit.await_args.kwargs["buttons"][0][0].data == b"back_to_main"

    accounts = {
        f"+{100000 + index}": {"display_phone": f"phone-{index}"}
        for index in range(30)
    }
    page = event_factory(data=b"hosting_menu_1")
    with patch("handlers.hosting_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.hosting_handlers.AccountManager.get_user_accounts", return_value=accounts
    ), patch(
        "handlers.hosting_handlers.AccountManager.get_compact_hosting_status_text",
        return_value="online",
    ), patch("handlers.hosting_handlers.safe_edit", new=AsyncMock()) as edit:
        run(callback(page))
    buttons = edit.await_args.kwargs["buttons"]
    assert any(button.data == b"hosting_menu_0" for row in buttons for button in row)
    assert buttons[-1][0].data == b"back_to_main"


def test_hosting_select_invalid_denied_missing_and_success(handler_bot, event_factory):
    register(handler_bot)
    callback = handler_bot.find("hosting_select_account")
    invalid = event_factory(data=b"bad")
    run(callback(invalid))
    invalid.answer.assert_awaited_once_with()
    denied = event_factory(data=b"hosting_sel_123")
    with patch("handlers.hosting_handlers.AccountManager.check_access", return_value=False):
        run(callback(denied))
    missing = event_factory(data=b"hosting_sel_123")
    with patch("handlers.hosting_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.hosting_handlers.AccountManager.get_user_accounts", return_value={}
    ):
        run(callback(missing))
    success = event_factory(data=b"hosting_sel_123")
    accounts = {"+123": {"display_phone": "+1 23"}}
    with patch("handlers.hosting_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.hosting_handlers.AccountManager.get_user_accounts", return_value=accounts
    ), patch(
        "handlers.hosting_handlers.AccountManager.get_hosting_status_text", return_value="ready"
    ), patch("handlers.hosting_handlers.hosting_account_buttons", return_value=[]), patch(
        "handlers.hosting_handlers.safe_edit", new=AsyncMock()
    ) as edit:
        run(callback(success))
    assert "+1 23" in edit.await_args.args[1]


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("hosting_kick", b"bad"),
        ("hosting_kick_confirm", b"bad"),
        ("hosting_code", b"bad"),
        ("hosting_code_exit", b"bad"),
    ],
)
def test_hosting_regex_callbacks_ignore_invalid_data(handler_bot, event_factory, name, data):
    register(handler_bot)
    event = event_factory(data=data)
    run(handler_bot.find(name)(event))
    event.answer.assert_awaited_once_with()


def test_hosting_kick_confirm_code_and_exit_flows(handler_bot, event_factory):
    register(handler_bot)
    with patch("handlers.hosting_handlers.safe_edit", new=AsyncMock()) as edit:
        run(handler_bot.find("hosting_kick")(event_factory(data=b"hosting_kick_123")))
    assert b"hosting_kick_confirm_123" in [
        button.data for row in edit.await_args.kwargs["buttons"] for button in row
    ]

    with patch(
        "handlers.hosting_handlers.AccountManager.kick_other_sessions",
        new=AsyncMock(return_value="done"),
    ), patch("handlers.hosting_handlers.safe_edit", new=AsyncMock()) as edit:
        run(
            handler_bot.find("hosting_kick_confirm")(
                event_factory(data=b"hosting_kick_confirm_123")
            )
        )
    assert "done" in edit.await_args.args[1]

    for result in ("active", "❌ failed"):
        with patch(
            "handlers.hosting_handlers.AccountManager.start_code_fetch",
            new=AsyncMock(return_value=result),
        ), patch("handlers.hosting_handlers.safe_edit", new=AsyncMock()) as edit:
            run(handler_bot.find("hosting_code")(event_factory(data=b"hosting_code_123")))
        assert "+123" in edit.await_args.args[1]

    with patch(
        "handlers.hosting_handlers.AccountManager.stop_code_fetch",
        new=AsyncMock(return_value="stopped"),
    ), patch(
        "handlers.hosting_handlers.AccountManager.get_user_accounts",
        return_value={"+123": {"display_phone": "shown"}},
    ), patch(
        "handlers.hosting_handlers.AccountManager.get_hosting_status_text",
        return_value="status",
    ), patch("handlers.hosting_handlers.hosting_account_buttons", return_value=[]), patch(
        "handlers.hosting_handlers.safe_edit", new=AsyncMock()
    ) as edit:
        run(handler_bot.find("hosting_code_exit")(event_factory(data=b"hosting_code_exit_123")))
    assert "stopped" in edit.await_args.args[1]


@pytest.mark.parametrize("case", ["denied", "missing", "offline", "young", "success"])
def test_hosting_clean_menu_guards_and_success(handler_bot, event_factory, case):
    register(handler_bot)
    event = event_factory(pattern_match=PatternMatch(b"123"))
    access = case != "denied"
    accounts = {} if case == "missing" else {"+123": {"display_phone": "shown"}}
    online = case != "offline"
    remaining = 30 if case == "young" else 0
    with patch("handlers.hosting_handlers.AccountManager.check_access", return_value=access), patch(
        "handlers.hosting_handlers.AccountManager.get_user_accounts", return_value=accounts
    ), patch("handlers.hosting_handlers.AccountManager.is_account_online", return_value=online), patch(
        "handlers.hosting_handlers.AccountManager.get_hosting_clean_remaining_seconds",
        return_value=remaining,
    ), patch(
        "handlers.hosting_handlers.AccountManager.hosting_clean_age_message", return_value="wait"
    ), patch("handlers.hosting_handlers.safe_edit", new=AsyncMock()) as edit:
        run(handler_bot.find("hosting_clean_menu")(event))
    if case == "success":
        edit.assert_awaited_once()
    else:
        edit.assert_not_awaited()
        event.answer.assert_awaited_once()


def test_hosting_clean_pick_and_confirm(handler_bot, event_factory):
    register(handler_bot)
    pick = event_factory(pattern_match=PatternMatch(b"all", b"123"))
    with patch("handlers.hosting_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.hosting_handlers.AccountManager.get_user_accounts",
        return_value={"+123": {"display_phone": "shown"}},
    ), patch(
        "handlers.hosting_handlers.AccountManager.get_hosting_clean_remaining_seconds",
        return_value=0,
    ), patch("handlers.hosting_handlers.safe_edit", new=AsyncMock()) as edit:
        run(handler_bot.find("hosting_clean_pick")(pick))
    assert "shown" in edit.await_args.args[1]
    assert b"hosting_clean_confirm_all_123" in [
        button.data for row in edit.await_args.kwargs["buttons"] for button in row
    ]

    result = SimpleNamespace(
        status="success",
        chats_deleted=1,
        contacts_deleted=1,
        errors=[],
    )
    confirm = event_factory(pattern_match=PatternMatch(b"all", b"123"))
    with patch(
        "handlers.hosting_handlers.AccountManager.clean_hosted_account",
        new=AsyncMock(return_value=result),
    ), patch("handlers.hosting_handlers.safe_edit", new=AsyncMock()) as edit:
        run(handler_bot.find("hosting_clean_confirm")(confirm))
    assert edit.await_count == 2
    assert "删除对话" in edit.await_args_list[-1].args[1]


@pytest.mark.parametrize("case", ["invalid", "denied", "missing", "success"])
def test_hosting_2fa_menu_paths(handler_bot, event_factory, case):
    register(handler_bot)
    data = b"bad" if case == "invalid" else b"hosting_2fa_123"
    event = event_factory(data=data)
    with patch(
        "handlers.hosting_handlers.AccountManager.check_access", return_value=case != "denied"
    ), patch(
        "handlers.hosting_handlers.AccountManager.get_user_accounts",
        return_value={} if case == "missing" else {"+123": {"display_phone": "shown"}},
    ), patch("handlers.hosting_handlers.safe_edit", new=AsyncMock()) as edit:
        run(handler_bot.find("hosting_2fa_menu")(event))
    if case == "success":
        edit.assert_awaited_once()
    elif case == "invalid":
        event.answer.assert_not_awaited()
    else:
        event.answer.assert_awaited_once()


def test_hosting_2fa_reset_change_and_set_prompts(handler_bot, event_factory):
    register(handler_bot)
    with patch(
        "handlers.hosting_handlers.AccountManager.request_2fa_reset",
        new=AsyncMock(return_value="requested"),
    ), patch("handlers.hosting_handlers.safe_edit", new=AsyncMock()) as edit:
        run(
            handler_bot.find("hosting_reset_password")(
                event_factory(data=b"hosting_2fa_reset_123")
            )
        )
    assert "requested" in edit.await_args.args[1]

    for name, data, action in (
        ("hosting_2fa_change", b"hosting_2fa_change_123", "change"),
        ("hosting_2fa_set", b"hosting_2fa_set_123", "set"),
    ):
        clear_state(1)
        with patch("handlers.hosting_handlers.AccountManager.check_access", return_value=True), patch(
            "handlers.hosting_handlers.time.time", return_value=100
        ), patch("handlers.hosting_handlers.safe_edit", new=AsyncMock()):
            run(handler_bot.find(name)(event_factory(sender_id=1, data=data)))
        assert get_state(1)["hosting_2fa_action"] == action


def test_hosting_2fa_text_input_ignores_commands_and_inactive_state(handler_bot, event_factory):
    register(handler_bot)
    callback = handler_bot.find("hosting_2fa_text_input")
    command = event_factory(sender_id=1, text="/cancel")
    run(callback(command))
    command.respond.assert_not_awaited()
    plain = event_factory(sender_id=2, text="password")
    run(callback(plain))
    plain.respond.assert_not_awaited()


def test_hosting_2fa_text_input_expired_and_invalid_state(handler_bot, event_factory):
    register(handler_bot)
    callback = handler_bot.find("hosting_2fa_text_input")
    set_state(1, waiting_hosting_2fa_input=True, hosting_2fa_action="bad")
    invalid = event_factory(sender_id=1, text="password")
    run(callback(invalid))
    invalid.delete.assert_awaited_once()
    assert get_state(1) == {}
    set_state(
        2,
        waiting_hosting_2fa_input=True,
        hosting_2fa_action="set",
        hosting_2fa_phone="+123",
        hosting_2fa_created_at=1,
    )
    expired = event_factory(sender_id=2, text="password")
    with patch("handlers.hosting_handlers.time.time", return_value=1000):
        run(callback(expired))
    expired.delete.assert_awaited_once()
    assert get_state(2) == {}


def test_hosting_2fa_text_input_retries_and_operations(handler_bot, event_factory):
    register(handler_bot)
    callback = handler_bot.find("hosting_2fa_text_input")
    base = {
        "waiting_hosting_2fa_input": True,
        "hosting_2fa_phone": "+123",
        "hosting_2fa_created_at": 100,
        "hosting_2fa_attempts": 0,
    }
    set_state(1, **base, hosting_2fa_action="change")
    invalid = event_factory(sender_id=1, text="one")
    with patch("handlers.hosting_handlers.time.time", return_value=100):
        run(callback(invalid))
    invalid.delete.assert_awaited_once()
    assert get_state(1)["hosting_2fa_attempts"] == 1

    set_state(2, **base, hosting_2fa_action="set")
    empty = event_factory(sender_id=2, text="")
    with patch("handlers.hosting_handlers.time.time", return_value=100):
        run(callback(empty))
    assert get_state(2)["hosting_2fa_attempts"] == 1

    set_state(3, **base, hosting_2fa_action="change")
    wrong = event_factory(sender_id=3, text="old new")
    with patch("handlers.hosting_handlers.time.time", return_value=100), patch(
        "handlers.hosting_handlers.AccountManager.change_hosted_2fa",
        new=AsyncMock(return_value="旧二级密码错误"),
    ):
        run(callback(wrong))
    assert get_state(3)["hosting_2fa_attempts"] == 1

    set_state(4, **base, hosting_2fa_action="change")
    clear = event_factory(sender_id=4, text="old clear")
    with patch("handlers.hosting_handlers.time.time", return_value=100), patch(
        "handlers.hosting_handlers.AccountManager.clear_hosted_2fa",
        new=AsyncMock(return_value="cleared"),
    ) as operation:
        run(callback(clear))
    operation.assert_awaited_once_with(4, "+123", "old")
    assert get_state(4) == {}

    set_state(5, **base, hosting_2fa_action="set")
    success = event_factory(sender_id=5, text="new-password")
    with patch("handlers.hosting_handlers.time.time", return_value=100), patch(
        "handlers.hosting_handlers.AccountManager.set_hosted_2fa",
        new=AsyncMock(return_value="set"),
    ) as operation:
        run(callback(success))
    operation.assert_awaited_once_with(5, "+123", "new-password")
    assert get_state(5) == {}
