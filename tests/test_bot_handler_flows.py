# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telethon import events

from handlers import bot_handlers
from handlers.handler_utils import clear_state, set_state


def run(awaitable):
    return asyncio.run(awaitable)


def register(handler_bot):
    setup_names = (
        "setup_account_handlers",
        "setup_vip_handlers",
        "setup_payment_handlers",
        "setup_admin_handlers",
        "setup_hosting_handlers",
        "setup_antilogin_handlers",
        "setup_transfer_handlers",
    )
    with ExitStack() as stack:
        for name in setup_names:
            stack.enter_context(patch.object(bot_handlers, name, new=AsyncMock()))
        run(bot_handlers.setup_bot_handlers(handler_bot, SimpleNamespace()))
    return handler_bot


@pytest.fixture(autouse=True)
def _state():
    clear_state(1001)
    yield
    clear_state(1001)


def test_sender_cache_success_failure_and_noop(handler_bot, event_factory):
    bot = register(handler_bot)
    sender = SimpleNamespace(id=1001)
    good = event_factory(get_sender=AsyncMock(return_value=sender))
    with patch("handlers.bot_handlers.UserProfileCache.set_entity") as save:
        run(bot.find("cache_message_sender")(good))
    save.assert_called_once_with(sender)

    bad = event_factory(get_sender=AsyncMock(side_effect=RuntimeError("gone")))
    run(bot.find("cache_callback_sender")(bad))
    noop = event_factory(data=b"pagination_noop")
    run(bot.find("pagination_noop")(noop))
    noop.answer.assert_awaited_once_with()


def test_more_menu_access_and_layout(handler_bot, event_factory):
    bot = register(handler_bot)
    denied = event_factory(data=b"more_menu")
    with patch("handlers.bot_handlers.AccountManager.check_access", return_value=False):
        run(bot.find("more_menu")(denied))
    denied.answer.assert_awaited_once_with("❌ 您没有使用权限", alert=True)

    event = event_factory(data=b"more_menu")
    edit = AsyncMock()
    with patch(
        "handlers.bot_handlers.AccountManager.check_access", return_value=True
    ), patch("handlers.bot_handlers.safe_edit", new=edit):
        run(bot.find("more_menu")(event))
    event.answer.assert_awaited_once_with()
    buttons = edit.await_args.kwargs["buttons"]
    assert [[button.data for button in row] for row in buttons] == [
        [b"reload_user_accounts", b"customer_support"],
        [b"show_help", b"language_menu"],
        [b"back_to_main"],
    ]
    back = buttons[-1][0]
    assert back.text == "返回"
    assert back.style.icon == 5877629862306385808


@pytest.mark.parametrize("reason", ["qr_message_delete_failed", "pending_client_busy"])
def test_start_cleanup_failure(handler_bot, event_factory, reason):
    bot = register(handler_bot)
    event = event_factory(get_sender=AsyncMock())
    result = SimpleNamespace(ok=False, reason=reason)
    with patch(
        "handlers.bot_handlers.cancel_pending_login_flow", new=AsyncMock(return_value=result)
    ), patch("handlers.bot_handlers.AccountManager.cleanup_stale_pending_sessions"):
        run(bot.find("start")(event))
    event.respond.assert_awaited_once()


def test_start_language_failure_and_success(handler_bot, event_factory):
    bot = register(handler_bot)
    result = SimpleNamespace(ok=True, reason="")
    event = event_factory(get_sender=AsyncMock(return_value=SimpleNamespace(lang_code="en")))
    common = [
        patch("handlers.bot_handlers.cancel_pending_login_flow", new=AsyncMock(return_value=result)),
        patch("handlers.bot_handlers.delete_remembered_main_menu", new=AsyncMock()),
        patch("handlers.bot_handlers.delete_remembered_start_command", new=AsyncMock()),
        patch("handlers.bot_handlers.AccountManager.cleanup_stale_pending_sessions"),
    ]
    with ExitStack() as stack:
        for item in common:
            stack.enter_context(item)
        stack.enter_context(
            patch("handlers.bot_handlers.DataManager.initialize_user_language", return_value=False)
        )
        run(bot.find("start")(event))
    event.respond.assert_awaited_once()

    event = event_factory(get_sender=AsyncMock(return_value=SimpleNamespace(lang_code="zh")))
    with patch(
        "handlers.bot_handlers.cancel_pending_login_flow", new=AsyncMock(return_value=result)
    ), patch(
        "handlers.bot_handlers.delete_remembered_main_menu", new=AsyncMock()
    ) as delete_main_menu, patch(
        "handlers.bot_handlers.delete_remembered_start_command", new=AsyncMock()
    ), patch(
        "handlers.bot_handlers.DataManager.initialize_user_language", return_value=True
    ), patch(
        "handlers.bot_handlers.AccountManager.check_access", return_value=True
    ), patch(
        "handlers.bot_handlers.AccountManager.cleanup_stale_pending_sessions"
    ), patch("handlers.bot_handlers.remember_start_command_message") as remember, patch(
        "handlers.bot_handlers.remember_main_menu_message"
    ) as remember_main_menu, patch(
        "handlers.bot_handlers.render_home", new=AsyncMock(return_value="main-menu")
    ) as render:
        run(bot.find("start")(event))
    delete_main_menu.assert_awaited_once_with(1001)
    remember.assert_called_once_with(1001, event)
    render.assert_awaited_once()
    remember_main_menu.assert_called_once_with(1001, "main-menu")


def test_support_success_and_failure(handler_bot, event_factory):
    bot = register(handler_bot)
    for name in ["support_command", "customer_support"]:
        callback = bot.find(name)
        success = event_factory()
        with patch(
            "handlers.bot_handlers.resolve_support_user", new=AsyncMock(return_value="entity")
        ), patch("handlers.bot_handlers.support_profile_markup", return_value="buttons"), patch(
            "handlers.bot_handlers.safe_answer_callback", new=AsyncMock()
        ), patch("handlers.bot_handlers.safe_edit", new=AsyncMock()) as edit:
            run(callback(success))
        if name == "support_command":
            success.respond.assert_awaited_once()
        else:
            edit.assert_awaited_once()

        failure = event_factory()
        with patch(
            "handlers.bot_handlers.resolve_support_user", new=AsyncMock(side_effect=ValueError)
        ), patch("handlers.bot_handlers.safe_answer_callback", new=AsyncMock()), patch(
            "handlers.bot_handlers.safe_edit", new=AsyncMock()
        ) as edit:
            run(callback(failure))
        if name == "support_command":
            failure.respond.assert_awaited_once()
        else:
            edit.assert_awaited_once()


def test_help_and_language_flows(handler_bot, event_factory):
    bot = register(handler_bot)
    help_event = event_factory(data=b"show_help")
    with patch("handlers.bot_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("show_help")(help_event))
    edit.assert_awaited_once()

    help_command = event_factory(text="/help")
    with patch("handlers.bot_handlers.t", return_value="help text"):
        run(bot.find("help_command")(help_command))
    help_command.respond.assert_awaited_once_with("help text")
    help_command.delete.assert_not_awaited()

    command = event_factory()
    with patch(
        "handlers.bot_handlers.cancel_user_flow_for_language", new=AsyncMock(return_value=True)
    ):
        with pytest.raises(events.StopPropagation):
            run(bot.find("language_command")(command))
    command.respond.assert_awaited_once()

    blocked = event_factory()
    with patch(
        "handlers.bot_handlers.cancel_user_flow_for_language", new=AsyncMock(return_value=False)
    ):
        with pytest.raises(events.StopPropagation):
            run(bot.find("language_command")(blocked))
    blocked.respond.assert_awaited_once()

    for allowed in (False, True):
        menu = event_factory(data=b"language_menu")
        with patch(
            "handlers.bot_handlers.cancel_user_flow_for_language",
            new=AsyncMock(return_value=allowed),
        ), patch("handlers.bot_handlers.safe_answer_callback", new=AsyncMock()), patch(
            "handlers.bot_handlers.safe_edit", new=AsyncMock()
        ) as edit:
            run(bot.find("language_menu")(menu))
        edit.assert_awaited_once()


def test_language_set_failure_and_success(handler_bot, event_factory):
    bot = register(handler_bot)
    failed = event_factory(data=b"language_set_en")
    with patch("handlers.bot_handlers.DataManager.set_user_language", return_value=False):
        run(bot.find("language_set")(failed))
    assert failed.answer.await_args.kwargs == {"alert": True}

    success = event_factory(data=b"language_set_en")
    with patch("handlers.bot_handlers.DataManager.set_user_language", return_value=True), patch(
        "handlers.bot_handlers.render_home", new=AsyncMock()
    ) as render:
        run(bot.find("language_set")(success))
    render.assert_awaited_once()


def test_back_to_main_cleanup_paths(handler_bot, event_factory):
    bot = register(handler_bot)
    event = event_factory(get_message=AsyncMock(return_value="message"))
    with patch(
        "handlers.bot_handlers.cancel_pending_login_flow",
        new=AsyncMock(return_value=SimpleNamespace(ok=False, reason="qr_message_delete_failed")),
    ):
        run(bot.find("back_to_main")(event))
    assert event.answer.await_args.kwargs == {"alert": True}

    set_state(1001, qr_login=True, qr_flow_id="flow")
    qr = event_factory(get_message=AsyncMock())
    with patch(
        "handlers.bot_handlers.cancel_pending_login_flow",
        new=AsyncMock(return_value=SimpleNamespace(ok=True, reason="")),
    ), patch("handlers.bot_handlers.render_home", new=AsyncMock()) as render:
        run(bot.find("back_to_main")(qr))
    qr.get_message.assert_not_awaited()
    assert render.await_args.kwargs["edit"] is False


@pytest.mark.parametrize("case", ["denied", "empty", "accounts"])
def test_list_accounts_paths(handler_bot, event_factory, case):
    bot = register(handler_bot)
    event = event_factory(data=b"list_accounts")
    accounts = (
        {"+1": {"display_phone": "+1", "last_reload": 0}}
        if case == "accounts"
        else {}
    )
    with patch(
        "handlers.bot_handlers.AccountManager.check_access", return_value=case != "denied"
    ), patch("handlers.bot_handlers.AccountManager.get_user_accounts", return_value=accounts), patch(
        "handlers.bot_handlers.AccountManager.get_antilogin_status_icon", return_value="🛡"
    ), patch("handlers.bot_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("list_accounts_callback")(event))
    if case == "denied":
        edit.assert_not_awaited()
    else:
        edit.assert_awaited_once()


@pytest.mark.parametrize("case", ["admin", "active", "inactive", "overquota"])
def test_vip_center_statuses(handler_bot, event_factory, case):
    bot = register(handler_bot)
    event = event_factory(data=b"vip_center")
    subscription = {
        "plan_id": "plus",
        "plan_name": "Plus",
        "expires_at": "2030-01-01",
        "quota": 1,
    }
    with patch("handlers.bot_handlers.DataManager.is_admin", return_value=case == "admin"), patch(
        "handlers.bot_handlers.DataManager.has_active_subscription", return_value=case == "active"
    ), patch(
        "handlers.bot_handlers.DataManager.get_subscription",
        return_value=subscription if case in {"active", "overquota"} else None,
    ), patch(
        "handlers.bot_handlers.DataManager.get_subscription_badge", return_value="badge"
    ), patch(
        "handlers.bot_handlers.AccountManager.hosted_account_phones",
        return_value={"1", "2"} if case == "overquota" else set(),
    ), patch("handlers.bot_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("vip_center")(event))
    edit.assert_awaited_once()
    if case == "overquota":
        assert any(
            button.data == b"subscription_accounts"
            for row in edit.await_args.kwargs["buttons"]
            for button in row
        )


def test_subscription_selection_guards_toggle_and_apply(handler_bot, event_factory):
    bot = register(handler_bot)
    no_subscription = event_factory(data=b"subscription_accounts")
    with patch("handlers.bot_handlers.DataManager.get_subscription", return_value=None):
        run(bot.find("subscription_accounts")(no_subscription))
    assert no_subscription.answer.await_args.kwargs == {"alert": True}

    subscription = {"quota": 1, "selected_accounts": ["1"]}
    page = event_factory(data=b"subscription_accounts")
    with patch("handlers.bot_handlers.DataManager.get_subscription", return_value=subscription), patch(
        "handlers.bot_handlers.AccountManager.hosted_account_phones", return_value={"1", "2"}
    ), patch("handlers.bot_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("subscription_accounts")(page))
    edit.assert_awaited_once()

    limited = event_factory(data=b"sub_select_2")
    with patch("handlers.bot_handlers.DataManager.get_subscription", return_value=subscription):
        run(bot.find("subscription_toggle_account")(limited))
    assert limited.answer.await_args.kwargs == {"alert": True}

    remove = event_factory(data=b"sub_select_1")
    with patch("handlers.bot_handlers.DataManager.get_subscription", return_value=subscription), patch(
        "handlers.bot_handlers.DataManager.set_selected_accounts", return_value=False
    ):
        run(bot.find("subscription_toggle_account")(remove))
    assert remove.answer.await_args.kwargs == {"alert": True}

    done = event_factory(data=b"subscription_selection_done")
    with patch("handlers.bot_handlers.DataManager.get_subscription", return_value=subscription), patch(
        "handlers.bot_handlers.DataManager.set_selected_accounts", return_value=True
    ), patch(
        "handlers.bot_handlers.AccountManager.suspend_user_accounts", new=AsyncMock()
    ), patch(
        "handlers.bot_handlers.AccountManager.resume_selected_accounts", new=AsyncMock(return_value=1)
    ), patch("handlers.bot_handlers.safe_edit", new=AsyncMock()):
        run(bot.find("subscription_selection_done")(done))
    assert done.answer.await_args_list[0].kwargs == {"alert": True}


def test_message_dispatch_and_manual_reload(handler_bot, event_factory):
    bot = register(handler_bot)
    command = event_factory(text="/x")
    run(bot.find("handle_messages")(command))
    with patch("handlers.bot_handlers.handle_admin_message", new=AsyncMock(return_value=True)) as admin:
        run(bot.find("handle_messages")(event_factory(text="input")))
    admin.assert_awaited_once()

    denied = event_factory(data=b"reload_user_accounts")
    with patch("handlers.bot_handlers.AccountManager.check_access", return_value=False):
        run(bot.find("manual_reload_callback")(denied))

    for stats in ({"success": 2, "failed": 1}, RuntimeError("reload failed")):
        event = event_factory(data=b"reload_user_accounts")
        reload_mock = AsyncMock(side_effect=stats) if isinstance(stats, Exception) else AsyncMock(return_value=stats)
        with patch("handlers.bot_handlers.AccountManager.check_access", return_value=True), patch(
            "handlers.bot_handlers.AccountManager.reload_user_accounts_detail", new=reload_mock
        ), patch("handlers.bot_handlers.safe_edit", new=AsyncMock()) as edit:
            run(bot.find("manual_reload_callback")(event))
        edit.assert_awaited_once()
