# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telethon import types

from handlers import transfer_handlers
from handlers.handler_utils import clear_state, get_state, set_state


def run(awaitable):
    return asyncio.run(awaitable)


def register(handler_bot):
    run(transfer_handlers.setup_transfer_handlers(handler_bot))
    return handler_bot


def inline_event(event_factory, *, text="", pm=True):
    return event_factory(
        text=text,
        query=SimpleNamespace(
            peer_type=types.InlineQueryPeerTypePM() if pm else object()
        ),
        builder=SimpleNamespace(article=AsyncMock(return_value="article")),
    )


@pytest.mark.parametrize(
    ("text", "pm", "validation_ok"),
    [("+123456", False, True), ("", True, True), ("bad", True, True), ("+123456", True, False)],
)
def test_inline_transfer_query_notices(handler_bot, event_factory, text, pm, validation_ok):
    register(handler_bot)
    event = inline_event(event_factory, text=text, pm=pm)
    validation = SimpleNamespace(ok=validation_ok, message="failure", phone="+123456")
    with patch(
        "handlers.transfer_handlers.AccountManager.validate_account_transfer_offer",
        return_value=validation,
    ):
        run(handler_bot.find("inline_transfer_query")(event))
    event.answer.assert_awaited_once()
    assert event.answer.await_args.kwargs == {"cache_time": 0, "private": True}


def test_inline_transfer_query_builds_signed_offer(handler_bot, event_factory):
    register(handler_bot)
    event = inline_event(event_factory, text="+123456", pm=True)
    validation = SimpleNamespace(ok=True, phone="+123456")
    with patch(
        "handlers.transfer_handlers.AccountManager.validate_account_transfer_offer",
        return_value=validation,
    ), patch("handlers.transfer_handlers.time.time", return_value=100):
        run(handler_bot.find("inline_transfer_query")(event))
    buttons = event.builder.article.await_args.kwargs["buttons"]
    offer, status = transfer_handlers.parse_inline_transfer_callback(
        buttons[0][0].data, now=101
    )
    assert status == "ok" and offer[1] == "+123456"


@pytest.mark.parametrize("status", ["invalid", "expired"])
def test_inline_receive_invalid_and_expired(handler_bot, event_factory, status):
    register(handler_bot)
    event = event_factory(data=b"bad")
    with patch(
        "handlers.transfer_handlers.parse_inline_transfer_callback",
        return_value=(None, status),
    ):
        run(handler_bot.find("inline_transfer_receive")(event))
    event.answer.assert_awaited_once()


def test_inline_receive_source_self_access_validation_and_transfer_results(
    handler_bot, event_factory
):
    register(handler_bot)
    callback = handler_bot.find("inline_transfer_receive")
    offer = (1, "+123456", 999)

    source_failed = event_factory(sender_id=2, data=b"offer")
    with patch("handlers.transfer_handlers.parse_inline_transfer_callback", return_value=(offer, "ok")), patch(
        "handlers.transfer_handlers.AccountManager.validate_account_transfer_offer",
        return_value=SimpleNamespace(ok=False, code="not_owned"),
    ), patch(
        "handlers.transfer_handlers._expire_inline_transfer_card", new=AsyncMock()
    ) as expire:
        run(callback(source_failed))
    expire.assert_awaited_once()

    self_event = event_factory(sender_id=1, data=b"offer")
    with patch("handlers.transfer_handlers.parse_inline_transfer_callback", return_value=(offer, "ok")), patch(
        "handlers.transfer_handlers.AccountManager.validate_account_transfer_offer",
        return_value=SimpleNamespace(ok=True),
    ):
        run(callback(self_event))
    self_event.answer.assert_awaited_once()

    denied = event_factory(sender_id=2, data=b"offer")
    with patch("handlers.transfer_handlers.parse_inline_transfer_callback", return_value=(offer, "ok")), patch(
        "handlers.transfer_handlers.AccountManager.validate_account_transfer_offer",
        return_value=SimpleNamespace(ok=True),
    ), patch("handlers.transfer_handlers.AccountManager.check_access", return_value=False):
        run(callback(denied))

    invalid = event_factory(sender_id=2, data=b"offer")
    with patch("handlers.transfer_handlers.parse_inline_transfer_callback", return_value=(offer, "ok")), patch(
        "handlers.transfer_handlers.AccountManager.validate_account_transfer_offer",
        return_value=SimpleNamespace(ok=True),
    ), patch("handlers.transfer_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.transfer_handlers.AccountManager.validate_account_transfer",
        return_value=SimpleNamespace(ok=False, code="other", message="invalid"),
    ):
        run(callback(invalid))
    assert invalid.answer.await_args.args[0] == "invalid"

    failed = event_factory(sender_id=2, data=b"offer")
    with patch("handlers.transfer_handlers.parse_inline_transfer_callback", return_value=(offer, "ok")), patch(
        "handlers.transfer_handlers.AccountManager.validate_account_transfer_offer",
        return_value=SimpleNamespace(ok=True),
    ), patch("handlers.transfer_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.transfer_handlers.AccountManager.validate_account_transfer",
        return_value=SimpleNamespace(ok=True),
    ), patch(
        "handlers.transfer_handlers.AccountManager.transfer_account",
        new=AsyncMock(return_value=SimpleNamespace(ok=False, message="failed")),
    ):
        run(callback(failed))
    failed.answer.assert_awaited_once()

    success = event_factory(
        sender_id=2,
        data=b"offer",
        get_sender=AsyncMock(return_value=SimpleNamespace(first_name="Receiver")),
    )
    with patch("handlers.transfer_handlers.parse_inline_transfer_callback", return_value=(offer, "ok")), patch(
        "handlers.transfer_handlers.AccountManager.validate_account_transfer_offer",
        return_value=SimpleNamespace(ok=True),
    ), patch("handlers.transfer_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.transfer_handlers.AccountManager.validate_account_transfer",
        return_value=SimpleNamespace(ok=True),
    ), patch(
        "handlers.transfer_handlers.AccountManager.transfer_account",
        new=AsyncMock(return_value=SimpleNamespace(ok=True, message="ok", phone="+123456")),
    ), patch("handlers.transfer_handlers.safe_edit", new=AsyncMock()) as edit:
        run(callback(success))
    assert "Receiver" in edit.await_args.args[1]


def test_transfer_account_list_denied_empty_and_statuses(handler_bot, event_factory):
    register(handler_bot)
    callback = handler_bot.find("transfer_accounts")
    denied = event_factory(sender_id=1, data=b"account_transfer_accounts")
    with patch("handlers.transfer_handlers.AccountManager.check_access", return_value=False):
        run(callback(denied))
    empty = event_factory(sender_id=1, data=b"account_transfer_accounts")
    with patch("handlers.transfer_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.transfer_handlers.user_accounts", {1: {}}
    ), patch("handlers.transfer_handlers.safe_edit", new=AsyncMock()) as edit:
        run(callback(empty))
    edit.assert_awaited_once()

    accounts = {
        "+111111": {"display_phone": "locked"},
        "+222222": {"display_phone": "young"},
        "+333333": {"display_phone": "ready"},
    }
    with patch("handlers.transfer_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.transfer_handlers.user_accounts", {1: accounts}
    ), patch(
        "handlers.transfer_handlers.AccountManager.is_uploaded_transfer_locked",
        side_effect=lambda user, phone: phone == "+111111",
    ), patch(
        "handlers.transfer_handlers.AccountManager.get_account_transfer_remaining_seconds",
        side_effect=lambda user, phone: 60 if phone == "+222222" else 0,
    ), patch("handlers.transfer_handlers.safe_edit", new=AsyncMock()) as edit:
        run(callback(event_factory(sender_id=1, data=b"account_transfer_accounts")))
    labels = [button.text for row in edit.await_args.kwargs["buttons"] for button in row]
    assert any("🔒" in label for label in labels)
    assert any("✅" in label for label in labels)


@pytest.mark.parametrize("case", ["denied", "invalid", "missing", "locked", "young", "success"])
def test_transfer_select_guards_and_success(handler_bot, event_factory, case):
    register(handler_bot)
    data = b"bad" if case == "invalid" else b"account_transfer_select_123456"
    event = event_factory(sender_id=1, data=data)
    accounts = {} if case == "missing" else {"+123456": {}}
    with patch("handlers.transfer_handlers.AccountManager.check_access", return_value=case != "denied"), patch(
        "handlers.transfer_handlers.user_accounts", {1: accounts}
    ), patch(
        "handlers.transfer_handlers.AccountManager.is_uploaded_transfer_locked",
        return_value=case == "locked",
    ), patch(
        "handlers.transfer_handlers.AccountManager.get_account_transfer_remaining_seconds",
        return_value=60 if case == "young" else 0,
    ), patch("handlers.transfer_handlers.time.time", return_value=100), patch(
        "handlers.transfer_handlers.safe_edit", new=AsyncMock()
    ) as edit:
        run(handler_bot.find("transfer_select")(event))
    if case == "success":
        assert get_state(1)["account_transfer_phone"] == "+123456"
        edit.assert_awaited_once()
        clear_state(1)
    else:
        event.answer.assert_awaited_once()


def test_transfer_target_input_ignores_times_out_validates_and_stages(handler_bot, event_factory):
    register(handler_bot)
    callback = handler_bot.find("transfer_target_input")
    inactive = event_factory(sender_id=1, text="2")
    run(callback(inactive))
    inactive.respond.assert_not_awaited()
    set_state(1, waiting_account_transfer_target=True, account_transfer_created_at=100)
    command = event_factory(sender_id=1, text="/cancel")
    run(callback(command))
    command.respond.assert_not_awaited()
    with patch("handlers.transfer_handlers.time.time", return_value=1000):
        expired = event_factory(sender_id=1, text="2")
        run(callback(expired))
    assert get_state(1) == {}

    set_state(1, waiting_account_transfer_target=True, account_transfer_created_at=100, account_transfer_phone="+123456")
    invalid_id = event_factory(sender_id=1, text="abc")
    with patch("handlers.transfer_handlers.time.time", return_value=100):
        run(callback(invalid_id))
    set_state(1, waiting_account_transfer_target=True, account_transfer_created_at=100, account_transfer_phone="+123456")
    invalid = event_factory(sender_id=1, text="2")
    with patch("handlers.transfer_handlers.time.time", return_value=100), patch(
        "handlers.transfer_handlers.AccountManager.validate_account_transfer",
        return_value=SimpleNamespace(ok=False, message="invalid"),
    ):
        run(callback(invalid))
    assert invalid.respond.await_args.args[0] == "invalid"

    set_state(1, waiting_account_transfer_target=True, account_transfer_created_at=100, account_transfer_phone="+123456")
    success = event_factory(sender_id=1, text="2")
    with patch("handlers.transfer_handlers.time.time", return_value=100), patch(
        "handlers.transfer_handlers.AccountManager.validate_account_transfer",
        return_value=SimpleNamespace(ok=True, phone="+123456"),
    ):
        run(callback(success))
    assert get_state(1)["account_transfer_pending"]
    clear_state(1)


def test_transfer_confirm_none_expired_failure_and_success(handler_bot, event_factory):
    register(handler_bot)
    callback = handler_bot.find("transfer_confirm")
    none = event_factory(sender_id=1)
    run(callback(none))
    none.answer.assert_awaited_once()
    set_state(1, account_transfer_pending=True, account_transfer_created_at=1)
    expired = event_factory(sender_id=1)
    with patch("handlers.transfer_handlers.time.time", return_value=1000), patch(
        "handlers.transfer_handlers.safe_edit", new=AsyncMock()
    ) as edit:
        run(callback(expired))
    edit.assert_awaited_once()

    for ok in (False, True):
        set_state(
            1,
            account_transfer_pending=True,
            account_transfer_created_at=100,
            account_transfer_phone="+123456",
            account_transfer_to_user_id=2,
        )
        event = event_factory(sender_id=1)
        with patch("handlers.transfer_handlers.time.time", return_value=100), patch(
            "handlers.transfer_handlers.AccountManager.transfer_account",
            new=AsyncMock(return_value=SimpleNamespace(ok=ok, message="result")),
        ), patch(
            "handlers.transfer_handlers.delete_remembered_start_command",
            new=AsyncMock(return_value=True),
        ) as cleanup, patch("handlers.transfer_handlers.safe_edit", new=AsyncMock()) as edit:
            run(callback(event))
        assert get_state(1) == {}
        if ok:
            cleanup.assert_awaited_once_with(1)
            assert edit.await_args.kwargs["buttons"] is None
        else:
            cleanup.assert_not_awaited()


def test_transfer_cancel_clears_state_and_renders_list(handler_bot, event_factory):
    register(handler_bot)
    set_state(1, account_transfer_pending=True)
    event = event_factory(sender_id=1)
    with patch("handlers.transfer_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.transfer_handlers.user_accounts", {1: {}}
    ), patch("handlers.transfer_handlers.safe_edit", new=AsyncMock()):
        run(handler_bot.find("transfer_cancel")(event))
    assert get_state(1) == {}
