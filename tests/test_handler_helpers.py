# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from telethon.errors import MessageNotModifiedError, QueryIdInvalidError

from handlers import bot_handlers, handler_utils, hosting_handlers, transfer_handlers, vip_handlers


def run(awaitable):
    return asyncio.run(awaitable)


def button_data(rows):
    return [[button.data for button in row] for row in rows]


def test_handler_pagination_clamps_pages_and_builds_navigation():
    assert handler_utils.paginate_items([], -1) == ([], 0, 0)
    items = list(range(60))
    page_items, page, max_page = handler_utils.paginate_items(items, 99, 25)
    assert page == max_page == 2
    assert page_items == list(range(50, 60))
    assert handler_utils.pagination_buttons("prefix", 0, 0) == []
    first = handler_utils.pagination_buttons("prefix", 0, 2)
    assert [button.data for button in first] == [b"pagination_noop", b"prefix_1"]
    middle = handler_utils.pagination_buttons("prefix", 1, 2)
    assert [button.data for button in middle] == [b"prefix_0", b"pagination_noop", b"prefix_2"]
    last = handler_utils.pagination_buttons("prefix", 2, 2)
    assert [button.data for button in last] == [b"prefix_1", b"pagination_noop"]


def test_state_clear_partial_and_back_button_language():
    handler_utils.set_state(7, one=1, two=2)
    handler_utils.clear_state(7, "one")
    assert handler_utils.get_state(7) == {"two": 2}
    handler_utils.clear_state(7, "missing")
    handler_utils.clear_state(7, "two")
    assert handler_utils.get_state(7) == {}
    handler_utils.clear_state(99, "missing")
    assert handler_utils.back_to_main_buttons(language="en")[0][0].data == b"back_to_main"


def test_safe_answer_success_and_expired_query(event_factory):
    event = event_factory()
    assert run(handler_utils.safe_answer_callback(event, "ok"))
    event.answer.assert_awaited_once_with("ok")
    event.answer.side_effect = QueryIdInvalidError(request=None)
    assert not run(handler_utils.safe_answer_callback(event))


def test_edit_or_respond_falls_back_on_edit_failure(event_factory):
    event = event_factory()
    event.edit.side_effect = RuntimeError("edit")
    event.respond.return_value = "replacement"
    assert run(handler_utils.edit_or_respond(event, "text")) == "replacement"
    event.respond.assert_awaited_once_with("text")


def test_safe_edit_message_returns_message_after_noop(event_factory):
    event = event_factory(get_message=AsyncMock(return_value="current"))
    event.edit.side_effect = MessageNotModifiedError(request=None)
    assert run(handler_utils.safe_edit_message(event, "same")) == "current"


def test_delete_remembered_start_command_missing_success_and_failure():
    assert not run(handler_utils.delete_remembered_start_command(700))
    message = SimpleNamespace(delete=AsyncMock())
    handler_utils.remember_start_command_message(700, message)
    assert run(handler_utils.delete_remembered_start_command(700))
    message.delete.assert_awaited_once_with()
    failing = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("delete")))
    handler_utils.remember_start_command_message(701, failing)
    assert not run(handler_utils.delete_remembered_start_command(701))


def test_delete_remembered_main_menu_missing_success_and_failure():
    assert not run(handler_utils.delete_remembered_main_menu(702))
    message = SimpleNamespace(delete=AsyncMock())
    handler_utils.remember_main_menu_message(702, message)
    assert run(handler_utils.delete_remembered_main_menu(702))
    message.delete.assert_awaited_once_with()
    failing = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("delete")))
    handler_utils.remember_main_menu_message(703, failing)
    assert not run(handler_utils.delete_remembered_main_menu(703))


def test_flow_message_cleanup_deduplicates_preserves_and_tolerates_failure():
    message = SimpleNamespace(id=1, chat_id=10, delete=AsyncMock())
    duplicate = SimpleNamespace(id=1, chat_id=10, delete=AsyncMock())
    handler_utils.remember_flow_message(704, message)
    handler_utils.remember_flow_message(704, duplicate)
    assert run(handler_utils.delete_remembered_flow_messages(704))
    message.delete.assert_awaited_once_with()
    duplicate.delete.assert_not_awaited()

    preserved = SimpleNamespace(id=2, chat_id=10, delete=AsyncMock())
    handler_utils.remember_flow_message(705, preserved)
    assert run(handler_utils.delete_remembered_flow_messages(705, preserved))
    preserved.delete.assert_not_awaited()

    failing = SimpleNamespace(
        id=3, chat_id=10, sender_id=705,
        delete=AsyncMock(side_effect=RuntimeError("delete")),
    )
    handler_utils.remember_flow_message(705, failing)
    assert not run(handler_utils.delete_remembered_flow_messages(705))


def test_require_access_starting_allowed_and_denied(event_factory):
    event = event_factory(sender_id=1)
    with patch("handlers.handler_utils.account_runtime.is_ready", return_value=False):
        assert not run(handler_utils.require_access(event, alert=True))
    event.answer.assert_awaited_once()

    event.answer.reset_mock()
    with patch("handlers.handler_utils.account_runtime.is_ready", return_value=True), patch(
        "handlers.handler_utils.AccountManager.check_access", return_value=True
    ):
        assert run(handler_utils.require_access(event))
    event.answer.assert_not_awaited()

    class MessageEvent:
        sender_id = 2
        respond = AsyncMock()

    message_event = MessageEvent()
    with patch("handlers.handler_utils.account_runtime.is_ready", return_value=True), patch(
        "handlers.handler_utils.AccountManager.check_access", return_value=False
    ):
        assert not run(handler_utils.require_access(message_event))
    message_event.respond.assert_awaited_once()


def test_require_admin_callback_and_message_paths(event_factory):
    event = event_factory(sender_id=1)
    with patch("handlers.handler_utils.DataManager.is_admin", return_value=True):
        assert run(handler_utils.require_admin(event))
    with patch("handlers.handler_utils.DataManager.is_admin", return_value=False):
        assert not run(handler_utils.require_admin(event, alert=True))
    class MessageEvent:
        sender_id = 2
        respond = AsyncMock()
    message = MessageEvent()
    with patch("handlers.handler_utils.DataManager.is_admin", return_value=False):
        assert not run(handler_utils.require_admin(message))
    message.respond.assert_awaited_once()


def test_transfer_remaining_text_boundaries():
    for seconds, fragment in [(0, "0"), (60, "1"), (3600, "1"), (3660, "1")]:
        assert fragment in transfer_handlers._remaining_text(seconds)


def test_base36_round_trip():
    for value in [0, 1, 35, 36, 123456789]:
        assert transfer_handlers._base36_decode(transfer_handlers._base36_encode(value)) == value


def test_inline_transfer_callback_roundtrip_tamper_expiry_and_validation():
    callback = transfer_handlers.build_inline_transfer_callback(123, "+86 138-0013-8000", 500)
    parsed, status = transfer_handlers.parse_inline_transfer_callback(callback, now=100)
    assert status == "ok"
    assert parsed == (123, "+8613800138000", 500)
    tampered = callback[:-1] + bytes([callback[-1] ^ 1])
    assert transfer_handlers.parse_inline_transfer_callback(tampered, now=100)[1] == "invalid"
    assert transfer_handlers.parse_inline_transfer_callback(callback, now=500)[1] == "expired"
    assert transfer_handlers.parse_inline_transfer_callback(b"bad", now=1)[1] == "invalid"
    assert transfer_handlers.parse_inline_transfer_callback(b"itr:0:1:123456:bad", now=1)[1] == "invalid"
    with pytest.raises(ValueError):
        transfer_handlers.build_inline_transfer_callback(36**50, "1" * 15, 36**50)


def test_inline_phone_normalization():
    cases = [
        ("+86 (138) 0013-8000", "+8613800138000"),
        (" 123 ", ""),
        ("letters", ""),
        ("1" * 30, ""),
        (None, ""),
    ]
    for text, expected in cases:
        assert transfer_handlers._inline_phone_input(text) == expected


def test_transfer_thumb_failure_text_and_receiver_name():
    thumb = transfer_handlers._inline_transfer_thumb()
    assert thumb.mime_type == "image/jpeg"
    assert thumb.attributes[0].w == 320
    messages = {
        transfer_handlers._inline_source_failure_text(code)
        for code in ("not_owned", "source_not_vip", "uploaded_session_not_transferable", "too_new", "other")
    }
    assert len(messages) == 5
    assert transfer_handlers._receiver_display_name(
        SimpleNamespace(first_name=" Ada ", last_name=" Lovelace "), 7
    ) == "AdaLovelace"
    assert transfer_handlers._receiver_display_name(SimpleNamespace(), 7) == "7"


def test_expire_inline_transfer_card_alert_survives_edit_failure(event_factory):
    event = event_factory(sender_id=1)
    with patch("handlers.transfer_handlers.safe_edit", new=AsyncMock(side_effect=RuntimeError)):
        run(transfer_handlers._expire_inline_transfer_card(event, "expired"))
    event.answer.assert_awaited_once()


def test_clear_transfer_and_hosting_states():
    handler_utils.set_state(
        10,
        waiting_account_transfer_target=True,
        account_transfer_pending=True,
        unrelated=True,
    )
    transfer_handlers.clear_account_transfer_state(10)
    assert handler_utils.get_state(10) == {"unrelated": True}
    handler_utils.set_state(
        11,
        waiting_hosting_2fa_input=True,
        hosting_2fa_action="set",
        unrelated=True,
    )
    hosting_handlers.clear_hosting_2fa_state(11)
    assert handler_utils.get_state(11) == {"unrelated": True}


def test_hosting_button_sets_online_offline_clean_and_2fa():
    with patch("handlers.hosting_handlers.AccountManager.is_account_online", return_value=True):
        online = hosting_handlers.hosting_account_buttons("+123", {})
    assert [row[0].data for row in online] == [
        b"hosting_kick_123",
        b"hosting_code_123",
        b"hosting_2fa_123",
        b"hosting_clean_menu_123",
        b"hosting_menu",
    ]
    with patch("handlers.hosting_handlers.AccountManager.is_account_online", return_value=False):
        offline = hosting_handlers.hosting_account_buttons("+123", {})
    assert button_data(offline) == [[b"hosting_menu"]]
    clean = hosting_handlers.hosting_clean_buttons("+123")
    assert button_data(clean)[-1] == [b"hosting_sel_123"]
    twofa = hosting_handlers.hosting_2fa_buttons("+123")
    assert [row[0].data for row in twofa] == [
        b"hosting_2fa_reset_123",
        b"hosting_2fa_change_123",
        b"hosting_2fa_set_123",
        b"hosting_sel_123",
    ]


def test_hosting_cleanup_result_renders_counts_errors_and_local_file():
    for status in ["success", "partial", "failed", "unknown"]:
        result = SimpleNamespace(
            status=status,
            chats_deleted=1,
            contacts_deleted=2,
            errors=["one", "two", "three", "four", "five"],
        )
        text = hosting_handlers.hosting_cleanup_result_text("+123", "all", result)
        assert "+123" in text and "删除对话：1" in text
        assert "另有 2 项错误" in text


def test_bot_menu_helpers_admin_and_user_layout():
    with patch("handlers.bot_handlers.DataManager.get_user_language", return_value="en"), patch(
        "handlers.bot_handlers.DataManager.is_admin", return_value=False
    ):
        regular = bot_handlers.main_menu_buttons(1)
    assert len(regular) == 4
    regular_data = {button.data for row in regular for button in row}
    assert b"more_menu" in regular_data
    assert [[button.data for button in row] for row in regular] == [
        [b"add_account", b"list_accounts"],
        [b"hosting_menu", b"antilogin_settings"],
        [b"account_transfer_accounts", b"vip_center"],
        [b"login_unlock_menu", b"more_menu"],
    ]
    more_entry = next(
        button for row in regular for button in row if button.data == b"more_menu"
    )
    assert more_entry.style.icon == 5884123981706956210
    assert {
        b"reload_user_accounts", b"customer_support", b"show_help", b"language_menu"
    }.isdisjoint(regular_data)
    with patch("handlers.bot_handlers.DataManager.get_user_language", return_value="en"):
        more = bot_handlers.more_menu_buttons(1)
    assert len(more) == 3
    assert {
        b"reload_user_accounts", b"customer_support", b"show_help", b"language_menu"
    } == {button.data for row in more[:-1] for button in row}
    with patch("handlers.bot_handlers.DataManager.get_user_language", return_value="zh"), patch(
        "handlers.bot_handlers.DataManager.is_admin", return_value=True
    ):
        admin = bot_handlers.main_menu_buttons(1)
    assert len(admin) == 5 and admin[-1][0].data == b"admin_panel"
    assert len(bot_handlers.language_buttons()[0]) == 2
    markup = bot_handlers.support_profile_markup("input", include_back=True, language="en")
    assert len(markup.rows) == 2
    assert len(bot_handlers.support_profile_markup("input", include_back=False).rows) == 1


def test_bot_quota_and_menu_text_helpers():
    with patch(
        "handlers.bot_handlers.AccountManager.get_quota_status",
        return_value={"used": 3, "quota": None},
    ):
        assert "3" in bot_handlers.hosting_quota_status_text(1, "en")
    with patch(
        "handlers.bot_handlers.AccountManager.get_quota_status",
        return_value={"used": 1, "quota": 2},
    ):
        assert "1" in bot_handlers.hosting_quota_status_text(1, "zh")
    assert "Alice" in bot_handlers.main_menu_text("Alice", 7, "1 / 2", 1)


def test_render_home_denied_and_allowed(event_factory):
    denied = event_factory(sender_id=1)
    with patch("handlers.bot_handlers.AccountManager.check_access", return_value=False):
        run(bot_handlers.render_home(object(), denied, edit=False))
    denied.respond.assert_awaited_once()

    allowed = event_factory(
        sender_id=1,
        get_sender=AsyncMock(return_value=SimpleNamespace(first_name="Alice")),
    )
    with patch("handlers.bot_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.bot_handlers.AccountManager.get_user_accounts",
        return_value={"1": {"anti_login": True}, "2": {"anti_login": False}},
    ), patch("handlers.bot_handlers.hosting_quota_status_text", return_value="1 / 2"), patch(
        "handlers.bot_handlers.main_menu_buttons", return_value=[]
    ), patch("handlers.bot_handlers.safe_edit", new=AsyncMock(return_value="edited")) as edit:
        assert run(bot_handlers.render_home(object(), allowed, edit=True)) == "edited"
    edit.assert_awaited_once()


def test_resolve_support_user_direct_fallback_and_mismatch():
    bot = SimpleNamespace(
        get_input_entity=AsyncMock(return_value="direct"),
        get_entity=AsyncMock(),
    )
    assert run(bot_handlers.resolve_support_user(bot)) == "direct"

    bot.get_input_entity = AsyncMock(side_effect=[ValueError, "fallback"])
    bot.get_entity = AsyncMock(return_value=SimpleNamespace(id=bot_handlers.SUPPORT_USER_ID))
    assert run(bot_handlers.resolve_support_user(bot)) == "fallback"

    bot.get_input_entity = AsyncMock(side_effect=ValueError)
    bot.get_entity = AsyncMock(return_value=SimpleNamespace(id=1))
    with pytest.raises(ValueError):
        run(bot_handlers.resolve_support_user(bot))


def test_cancel_language_flow_and_login():
    with patch(
        "handlers.bot_handlers.cancel_login_unlock_flow", new=AsyncMock()
    ), patch(
        "handlers.bot_handlers.cancel_pending_login_flow",
        new=AsyncMock(return_value=SimpleNamespace(ok=True)),
    ), patch(
        "handlers.bot_handlers.delete_remembered_flow_messages", new=AsyncMock()
    ):
        assert run(bot_handlers.cancel_user_flow_for_language(1))
    with patch(
        "handlers.bot_handlers.cancel_login_unlock_flow", new=AsyncMock()
    ), patch(
        "handlers.bot_handlers.cancel_pending_login_flow",
        new=AsyncMock(return_value=SimpleNamespace(ok=False)),
    ):
        assert not run(bot_handlers.cancel_user_flow_for_language(1))


def test_cancel_all_user_flows_preserves_message_and_skips_ui_on_critical_failure():
    preserved = SimpleNamespace(id=8, chat_id=1)
    result = SimpleNamespace(ok=True)
    with patch("handlers.bot_handlers.get_state", return_value={}), patch(
        "handlers.bot_handlers.cancel_login_unlock_flow", new=AsyncMock()
    ) as cancel_unlock, patch(
        "handlers.bot_handlers.cancel_pending_login_flow",
        new=AsyncMock(return_value=result),
    ) as cancel_login, patch(
        "handlers.bot_handlers.delete_remembered_flow_messages", new=AsyncMock()
    ) as delete_ui:
        assert run(bot_handlers.cancel_all_user_flows(9, "back", preserved)) is result
    cancel_unlock.assert_awaited_once_with(9)
    cancel_login.assert_awaited_once_with(9, reason="back", preserve_message=preserved)
    delete_ui.assert_awaited_once_with(9, preserve_message=preserved)

    with patch("handlers.bot_handlers.get_state", return_value={}), patch(
        "handlers.bot_handlers.cancel_login_unlock_flow", new=AsyncMock()
    ), patch(
        "handlers.bot_handlers.cancel_pending_login_flow",
        new=AsyncMock(return_value=SimpleNamespace(ok=False)),
    ), patch(
        "handlers.bot_handlers.delete_remembered_flow_messages", new=AsyncMock()
    ) as delete_ui:
        run(bot_handlers.cancel_all_user_flows(9, "start"))
    delete_ui.assert_not_awaited()


def test_vip_percent_and_quota_helpers():
    for value in ["8.0", "8.25", "bad", None]:
        assert isinstance(vip_handlers._percent_text(value), str)
    assert vip_handlers._quota_text(None)
    assert vip_handlers._quota_text(5)


def test_vip_catalog_current_subscription_and_success_texts():
    assert "USDT" in vip_handlers._catalog_text()
    admin = vip_handlers._current_subscription_text({"plan_id": "admin", "quota": None})
    assert "管理权限" in admin
    future = (datetime.now() + timedelta(days=5)).isoformat()
    current = vip_handlers._current_subscription_text(
        {"plan_id": "go", "quota": 2, "expires_at": future}
    )
    assert "2" in current
    invalid = vip_handlers._current_subscription_text(
        {"plan_id": "other", "plan_name": "Other", "quota": 1, "expires_at": "bad"}
    )
    assert "Other" in invalid
    payment = SimpleNamespace(
        pending_orders={"o": {"amount": "1", "coin": "USDT", "billing_mode": "prorated_upgrade"}}
    )
    with patch(
        "handlers.vip_handlers.DataManager.get_subscription",
        return_value={"plan_id": "plus", "expires_at": future},
    ):
        text = vip_handlers._success_message(payment, 1, "o")
    assert "o" in text and "1" in text
