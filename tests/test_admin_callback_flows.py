# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from handlers import admin_handlers
from handlers.handler_utils import clear_state, set_state


def run(awaitable):
    return asyncio.run(awaitable)


def payment_stub(**overrides):
    order = {
        "order_id": "o1",
        "user_id": 2,
        "status": "pending",
        "processed": False,
        "amount": "1",
        "coin": "USDT",
        "type": "subscription",
        "plan_id": "go",
        "period_days": 30,
        "created_time": 1,
    }
    values = {
        "pending_orders": {"o1": order},
        "get_admin_report": Mock(return_value={"amounts": {"USDT": "1"}, "new_paid_users": 1, "start_time": 1, "end_time": 2}),
        "list_admin_orders": Mock(return_value={"items": [order], "total": 1, "page": 0, "max_page": 0}),
        "get_order_snapshot": Mock(return_value=order),
        "get_order_retry_snapshot": Mock(return_value=None),
        "check_order_status": AsyncMock(return_value={"success": True}),
        "process_paid_order": AsyncMock(return_value=True),
        "cancel_order": AsyncMock(return_value={"success": True}),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def register(handler_bot, payment=None):
    payment = payment or payment_stub()
    run(admin_handlers.setup_admin_handlers(handler_bot, payment))
    return handler_bot, payment


@pytest.fixture(autouse=True)
def _cleanup():
    clear_state(1001)
    admin_handlers._pending_admin_actions.clear()
    admin_handlers.user_accounts.clear()
    yield
    clear_state(1001)
    admin_handlers._pending_admin_actions.clear()
    admin_handlers.user_accounts.clear()


def common_patches():
    return (
        patch("handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)),
        patch("handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"),
        patch("handlers.admin_handlers._audit_result", return_value=True),
    )


def test_admin_panel_denied_and_success(handler_bot, event_factory):
    bot, payment = register(handler_bot)
    denied = event_factory(data=b"admin_panel")
    with patch("handlers.admin_handlers.DataManager.is_admin", return_value=False):
        run(bot.find("admin_panel")(denied))
    assert denied.answer.await_args.kwargs == {"alert": True}

    admin_handlers.user_accounts[2] = {"+1": {"anti_login": True}}
    success = event_factory(data=b"admin_panel")
    with patch("handlers.admin_handlers.DataManager.is_admin", return_value=True), patch(
        "handlers.admin_handlers.DataManager.get_all_subscription_users", return_value=[2]
    ), patch("handlers.admin_handlers.edit_or_respond", new=AsyncMock()) as edit:
        run(bot.find("admin_panel")(success))
    edit.assert_awaited_once()


def test_admin_panel_renders_english_from_saved_language(handler_bot, event_factory):
    bot, _ = register(handler_bot)
    event = event_factory(data=b"admin_panel")
    with patch("handlers.admin_handlers.DataManager.is_admin", return_value=True), patch(
        "handlers.admin_handlers.DataManager.get_user_language", return_value="en"
    ), patch("handlers.admin_handlers.DataManager.get_all_subscription_users", return_value=[]), patch(
        "handlers.admin_handlers.edit_or_respond", new=AsyncMock()
    ) as edit:
        run(bot.find("admin_panel")(event))
    assert "Admin Console" in edit.await_args.args[1]
    labels = [button.text for row in edit.await_args.kwargs["buttons"] for button in row]
    assert "Orders" in labels
    assert "Back" in labels


def test_admin_prompts_reports_and_commands_render_english(handler_bot, event_factory):
    bot, _ = register(handler_bot)
    with patch("handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)), patch(
        "handlers.admin_handlers.DataManager.get_user_language", return_value="en"
    ), patch("handlers.admin_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("admin_report")(event_factory(data=b"admin_report_7")))
        run(bot.find("admin_order_search")(event_factory(data=b"admin_order_search")))
        run(bot.find("admin_user_search")(event_factory(data=b"admin_user_search")))
        run(bot.find("admin_subscription_grant_help")(
            event_factory(data=b"admin_subscription_grant_help")
        ))
        run(bot.find("admin_subscription_delete_help")(
            event_factory(data=b"admin_subscription_delete_help")
        ))
    texts = [call.args[1] for call in edit.await_args_list]
    assert any("Operations · Last 7 days" in text for text in texts)
    assert any("Search orders" in text for text in texts)
    assert any("Global user search" in text for text in texts)
    assert any("Grant subscription" in text for text in texts)
    assert any("Delete subscription" in text for text in texts)

    for command, handler_name, expected in (
        ("/sub", "sub_command", "Format: /sub user_id"),
        ("/delsub", "delsub_command", "Format: /delsub user_id"),
    ):
        event = event_factory(text=command)
        with patch("handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)), patch(
            "handlers.admin_handlers.DataManager.get_user_language", return_value="en"
        ), patch("handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"), patch(
            "handlers.admin_handlers._audit_result", return_value=True
        ):
            run(bot.find(handler_name)(event))
        assert expected in event.respond.await_args.args[0]


def test_admin_report(handler_bot, event_factory):
    days = 30
    bot, _ = register(handler_bot)
    event = event_factory(data=f"admin_report_{days}".encode())
    with common_patches()[0], patch("handlers.admin_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("admin_report")(event))
    assert str(days) in str(edit.await_args.args[1]) or days == 1


@pytest.mark.parametrize("category", ["review", "all"])
def test_order_lists_and_search_page(handler_bot, event_factory, category):
    bot, payment = register(handler_bot)
    event = event_factory(data=f"admin_orders_{category}_0".encode())
    with common_patches()[0], common_patches()[1], common_patches()[2], patch(
        "handlers.admin_handlers.edit_or_respond", new=AsyncMock()
    ) as edit:
        run(bot.find("admin_orders")(event))
    edit.assert_awaited_once()

    set_state(1001, admin_order_query="o1")
    page = event_factory(data=b"admin_order_search_page_0")
    with common_patches()[0], common_patches()[1], common_patches()[2], patch(
        "handlers.admin_handlers.edit_or_respond", new=AsyncMock()
    ):
        run(bot.find("admin_order_search_page")(page))
    assert payment.list_admin_orders.call_count == 2


def test_order_search_prompt(handler_bot, event_factory):
    bot, _ = register(handler_bot)
    event = event_factory(data=b"admin_order_search")
    with common_patches()[0], patch("handlers.admin_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("admin_order_search")(event))
    edit.assert_awaited_once()


@pytest.mark.parametrize("case", ["missing", "legacy", "paid", "processed"])
def test_order_detail_variants(handler_bot, event_factory, case):
    order = None if case == "missing" else {
        "order_id": "o1", "user_id": 2, "status": "paid" if case == "paid" else "pending",
        "processed": case == "processed", "amount": "1", "coin": "USDT",
        "legacy_origin": "vip_purchase" if case == "legacy" else None,
        "manual_review_reason": "reason" if case == "paid" else None,
    }
    payment = payment_stub(
        get_order_snapshot=Mock(return_value=order),
        get_order_retry_snapshot=Mock(return_value={"failures": 2, "next_check_at": 3} if case == "paid" else None),
    )
    bot, _ = register(handler_bot, payment)
    event = event_factory(data=b"admin_order_detail_o1")
    with common_patches()[0], common_patches()[1], common_patches()[2], patch(
        "handlers.admin_handlers.safe_edit", new=AsyncMock()
    ) as edit:
        run(bot.find("admin_order_detail")(event))
    if case == "missing":
        edit.assert_not_awaited()
    else:
        edit.assert_awaited_once()


def test_order_recheck_success_and_audit_warning(handler_bot, event_factory):
    bot, payment = register(handler_bot)
    event = event_factory(data=b"admin_order_check_o1")
    with common_patches()[0], common_patches()[1], patch(
        "handlers.admin_handlers._audit_result", return_value=False
    ), patch("handlers.admin_handlers.safe_edit", new=AsyncMock()):
        run(bot.find("admin_order_check")(event))
    payment.check_order_status.assert_awaited_once_with("o1")
    event.respond.assert_awaited_once()


@pytest.mark.parametrize("valid", [False, True])
def test_order_retry_prompt(handler_bot, event_factory, valid):
    order = {"order_id": "o1", "user_id": 2, "status": "paid", "processed": False} if valid else {"status": "pending"}
    bot, _ = register(handler_bot, payment_stub(get_order_snapshot=Mock(return_value=order)))
    event = event_factory(data=b"admin_order_retry_o1")
    with common_patches()[0], patch("handlers.admin_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("admin_order_retry_prompt")(event))
    if valid:
        edit.assert_awaited_once()
    else:
        edit.assert_not_awaited()


@pytest.mark.parametrize("case", ["changed", "success", "failed"])
def test_order_retry_confirm(handler_bot, event_factory, case):
    order = {"order_id": "o1", "status": "paid", "processed": False, "user_id": 2}
    if case == "changed":
        order = {"status": "pending"}
    payment = payment_stub(
        get_order_snapshot=Mock(return_value=order),
        process_paid_order=AsyncMock(return_value=case == "success"),
    )
    bot, _ = register(handler_bot, payment)
    event = event_factory(data=b"admin_order_retry_confirm_o1")
    with common_patches()[0], common_patches()[1], common_patches()[2], patch(
        "handlers.admin_handlers.safe_edit", new=AsyncMock()
    ):
        run(bot.find("admin_order_retry_confirm")(event))
    if case == "changed":
        payment.process_paid_order.assert_not_awaited()
    else:
        payment.process_paid_order.assert_awaited_once()


def queue(action="subscription.grant", before=None, params=None):
    return admin_handlers._queue_admin_action(
        1001,
        action,
        2,
        params or {"plan_id": "go", "days": 30, "quota": 2},
        before,
        audit_id="audit",
    )


def test_admin_action_cancel_missing_and_success(handler_bot, event_factory):
    bot, _ = register(handler_bot)
    missing = event_factory(data=b"admin_action_cancel_bad")
    with common_patches()[0]:
        run(bot.find("admin_action_cancel")(missing))
    pending = queue()
    success = event_factory(data=f"admin_action_cancel_{pending['token']}".encode())
    with common_patches()[0], common_patches()[2], patch(
        "handlers.admin_handlers.safe_edit", new=AsyncMock()
    ) as edit:
        run(bot.find("admin_action_cancel")(success))
    edit.assert_awaited_once()


@pytest.mark.parametrize("action", ["subscription.grant", "subscription.delete", "accounts.suspend", "unknown"])
def test_admin_action_confirm_variants(handler_bot, event_factory, action):
    bot, _ = register(handler_bot)
    before = None
    params = {"plan_id": "go", "days": 30, "quota": 2}
    if action == "unknown":
        pending = queue()
        pending["action"] = "unknown"
    else:
        pending = queue(action, before, params)
    event = event_factory(data=f"admin_action_confirm_{pending['token']}".encode())
    with common_patches()[0], patch(
        "handlers.admin_handlers.DataManager.get_subscription", return_value=before
    ), patch("handlers.admin_handlers.DataManager.grant_subscription", return_value=True), patch(
        "handlers.admin_handlers.DataManager.delete_subscription", return_value=True
    ), patch("handlers.admin_handlers.AccountManager.suspend_user_accounts", new=AsyncMock(return_value=2)), patch(
        "handlers.admin_handlers.AccountManager.resume_selected_accounts", new=AsyncMock()
    ), patch("handlers.admin_handlers._audit_result", return_value=True), patch(
        "handlers.admin_handlers.safe_edit", new=AsyncMock()
    ) as edit:
        run(bot.find("admin_action_confirm")(event))
    edit.assert_awaited_once()


def test_admin_action_confirm_state_changed(handler_bot, event_factory):
    bot, _ = register(handler_bot)
    pending = queue(before={"plan": "go"})
    event = event_factory(data=f"admin_action_confirm_{pending['token']}".encode())
    with common_patches()[0], patch(
        "handlers.admin_handlers.DataManager.get_subscription", return_value={"plan": "pro"}
    ), common_patches()[2]:
        run(bot.find("admin_action_confirm")(event))
    assert event.answer.await_args.kwargs == {"alert": True}


def test_user_search_results_and_detail(handler_bot, event_factory):
    payment = payment_stub()
    payment.get_user_order_summaries = Mock(return_value={"total": 1, "items": [payment.pending_orders["o1"]]})
    bot, _ = register(handler_bot, payment)
    prompt = event_factory(data=b"admin_user_search")
    with common_patches()[0], patch("handlers.admin_handlers.safe_edit", new=AsyncMock()):
        run(bot.find("admin_user_search")(prompt))

    expired = event_factory(data=b"admin_user_search_results")
    with common_patches()[0]:
        run(bot.find("admin_user_search_results")(expired))
    set_state(1001, admin_user_results=[{"user_id": 2, "display_name": "A", "username": None}])
    results = event_factory(data=b"admin_user_search_results")
    with common_patches()[0], patch("handlers.admin_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("admin_user_search_results")(results))
    edit.assert_awaited_once()

    admin_handlers.user_accounts[2] = {"+1": {"anti_login": True, "health_status": "alive"}}
    detail = event_factory(data=b"admin_user_detail_2")
    subscription = {"active": True, "plan_id": "go", "plan_name": "GO", "quota": 2, "scheduled": {"plan_id": "pro", "quota": None}}
    with common_patches()[0], common_patches()[1], common_patches()[2], patch(
        "handlers.admin_handlers._get_user_display_name", new=AsyncMock()
    ), patch("handlers.admin_handlers.UserProfileCache.get_profile", return_value={"display_name": "A", "username": "a"}), patch(
        "handlers.admin_handlers.DataManager.get_subscription", return_value=subscription
    ), patch("handlers.admin_handlers.DataManager.is_admin", return_value=False), patch(
        "handlers.admin_handlers._resumable_account_count", return_value=1
    ), patch("handlers.admin_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("admin_user_detail")(detail))
    edit.assert_awaited_once()


@pytest.mark.parametrize("case", ["admin", "missing", "existing"])
def test_user_subscription_menu(handler_bot, event_factory, case):
    bot, _ = register(handler_bot)
    event = event_factory(data=b"admin_user_subscription_2")
    subscription = None if case == "missing" else {"active": case == "existing", "plan_id": "go", "expires_at": "2030"}
    with common_patches()[0], patch("handlers.admin_handlers.DataManager.is_admin", return_value=case == "admin"), patch(
        "handlers.admin_handlers.DataManager.get_subscription", return_value=subscription
    ), patch("handlers.admin_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("admin_user_subscription")(event))
    if case == "admin":
        edit.assert_not_awaited()
    else:
        edit.assert_awaited_once()


def test_user_account_and_plan_menus(handler_bot, event_factory):
    bot, _ = register(handler_bot)
    admin_handlers.user_accounts[2] = {"+1": {}}
    event = event_factory(data=b"admin_user_accounts_2")
    with common_patches()[0], patch("handlers.admin_handlers.DataManager.get_subscription", return_value={}), patch(
        "handlers.admin_handlers._resumable_account_count", return_value=1
    ), patch("handlers.admin_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("admin_user_accounts")(event))
    assert len(edit.await_args.kwargs["buttons"]) == 4

    for data, name in ((b"admin_user_sub_2", "admin_user_sub"), (b"admin_user_plan_2_plus", "admin_user_plan")):
        event = event_factory(data=data)
        with common_patches()[0], patch("handlers.admin_handlers.safe_edit", new=AsyncMock()) as edit:
            run(bot.find(name)(event))
        edit.assert_awaited_once()


@pytest.mark.parametrize("exists", [False, True])
def test_user_delete_prompt(handler_bot, event_factory, exists):
    bot, _ = register(handler_bot)
    event = event_factory(data=b"admin_user_delete_2")
    with common_patches()[0], patch(
        "handlers.admin_handlers.DataManager.get_subscription",
        return_value={"plan_id": "go", "expires_at": "2030"} if exists else None,
    ), patch("handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"), patch(
        "handlers.admin_handlers.safe_edit", new=AsyncMock()
    ) as edit:
        run(bot.find("admin_user_delete")(event))
    assert edit.await_count == int(exists)


@pytest.mark.parametrize("name", ["admin_user_reload", "admin_user_resume"])
@pytest.mark.parametrize("fails", [False, True])
def test_user_runtime_actions(handler_bot, event_factory, name, fails):
    bot, _ = register(handler_bot)
    event = event_factory(data=(b"admin_user_reload_2" if name.endswith("reload") else b"admin_user_resume_2"))
    reload_mock = AsyncMock(side_effect=RuntimeError("bad")) if fails else AsyncMock(return_value={"total": 1, "success": 1, "failed": 0})
    resume_mock = AsyncMock(side_effect=RuntimeError("bad")) if fails else AsyncMock(return_value=1)
    with common_patches()[0], common_patches()[1], common_patches()[2], patch(
        "handlers.admin_handlers.AccountManager.reload_user_accounts_detail", new=reload_mock
    ), patch("handlers.admin_handlers.AccountManager.resume_selected_accounts", new=resume_mock), patch(
        "handlers.admin_handlers.safe_edit", new=AsyncMock()
    ) as edit:
        run(bot.find(name)(event))
    edit.assert_awaited_once()


def test_user_suspend_and_orders(handler_bot, event_factory):
    bot, payment = register(handler_bot)
    suspend = event_factory(data=b"admin_user_suspend_2")
    with common_patches()[0], patch("handlers.admin_handlers.DataManager.get_subscription", return_value=None), patch(
        "handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"
    ), patch("handlers.admin_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("admin_user_suspend")(suspend))
    edit.assert_awaited_once()

    orders = event_factory(data=b"admin_user_orders_2_0")
    with common_patches()[0], patch("handlers.admin_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("admin_user_orders")(orders))
    payment.list_admin_orders.assert_called()
    edit.assert_awaited_once()


def test_audit_callbacks(handler_bot, event_factory, tmp_path):
    bot, _ = register(handler_bot)
    bot.send_file = AsyncMock()
    query = {"items": [{"audit_id": "abc", "result": "success", "action": "x", "target_id": "2"}], "total": 1, "page": 0, "max_page": 0}
    with common_patches()[0], patch("handlers.admin_handlers.AdminAuditLog.query", return_value=query), patch(
        "handlers.admin_handlers.safe_edit", new=AsyncMock()
    ) as edit:
        run(bot.find("admin_audit")(event_factory(data=b"admin_audit_0")))
        run(bot.find("admin_audit_clear")(event_factory(data=b"admin_audit_clear")))
    assert edit.await_count == 2

    with common_patches()[0], patch("handlers.admin_handlers.safe_edit", new=AsyncMock()):
        run(bot.find("admin_audit_filter")(event_factory(data=b"admin_audit_filter")))

    for entries in ([], [{"audit_id": "abc"}]):
        detail = event_factory(data=b"admin_audit_detail_abc")
        with common_patches()[0], patch("handlers.admin_handlers.AdminAuditLog.get_by_audit_id", return_value=entries), patch(
            "handlers.admin_handlers.safe_edit", new=AsyncMock()
        ):
            run(bot.find("admin_audit_detail")(detail))

    path = tmp_path / "audit.jsonl"
    path.write_text("{}", encoding="utf-8")
    original_bytes = path.read_bytes()
    with common_patches()[0], patch("handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"), patch(
        "handlers.admin_handlers.AdminAuditLog.prune"
    ), patch("handlers.admin_handlers.audit_file_path", return_value=str(path)), patch(
        "handlers.admin_handlers._audit_result", return_value=True
    ):
        run(bot.find("admin_audit_download")(event_factory(data=b"admin_audit_download")))
    bot.send_file.assert_awaited_once()
    assert path.read_bytes() == original_bytes

    entries = [{
        "audit_id": "abc", "timestamp": "2026-01-01", "admin_id": 1,
        "action": "order.recheck", "target_type": "order", "target_id": "o1",
        "result": "success", "before": None, "after": None, "metadata": {},
        "error": None,
    }]
    detail = event_factory(data=b"admin_audit_detail_abc")
    with common_patches()[0], patch(
        "handlers.admin_handlers.DataManager.get_user_language", return_value="en"
    ), patch("handlers.admin_handlers.AdminAuditLog.get_by_audit_id", return_value=entries), patch(
        "handlers.admin_handlers.safe_edit", new=AsyncMock()
    ) as edit:
        run(bot.find("admin_audit_detail")(detail))
    assert "Audit details" in edit.await_args.args[1]


def config_catalog():
    return {
        "go": {"price": "1", "quota": 2},
        "plus": {"price": "2", "quota": 10, "addon_unit_price": "0.2", "min_addon": 5},
        "pro": {"price": "3", "quota": None},
    }


def config_periods():
    return {30: {"discount_percent": "0"}, 90: {"discount_percent": "5"}, 180: {"discount_percent": "10"}, 365: {"discount_percent": "15"}}


def test_subscription_config_render_and_edit(handler_bot, event_factory):
    bot, _ = register(handler_bot)
    with common_patches()[0], patch("handlers.admin_handlers.DataManager.get_subscription_catalog", return_value=config_catalog()), patch(
        "handlers.admin_handlers.DataManager.get_subscription_periods", return_value=config_periods()
    ), patch("handlers.admin_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("admin_subscription_config")(event_factory(data=b"admin_subscription_config")))
        for target in ("go", "plus", "pro", "discounts"):
            run(bot.find("admin_subscription_config_edit")(event_factory(data=f"admin_subscription_config_edit_{target}".encode())))
    assert edit.await_count == 5


@pytest.mark.parametrize("case", ["missing", "expired", "changed", "save_failed", "success"])
def test_subscription_config_confirm_paths(handler_bot, event_factory, case):
    bot, _ = register(handler_bot)
    before = config_catalog()
    flow = {"target": "go", "before": before, "values": {"price": "2", "quota": 3}, "index": 2, "stage": "preview", "started_at": 100}
    if case == "missing":
        flow = None
    if flow:
        set_state(1001, admin_subscription_config_flow=flow)
    current = config_catalog()
    if case == "changed":
        current["go"]["price"] = "9"
    with common_patches()[0], patch("handlers.admin_handlers.time.time", return_value=1000 if case == "expired" else 100), patch(
        "handlers.admin_handlers.DataManager.get_subscription_catalog", return_value=current
    ), patch("handlers.admin_handlers.DataManager.get_subscription_periods", return_value=config_periods()), patch(
        "handlers.admin_handlers.DataManager.set_subscription_catalog", return_value=case != "save_failed"
    ), patch("handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"), patch(
        "handlers.admin_handlers._audit_result", return_value=True
    ), patch("handlers.admin_handlers.safe_edit", new=AsyncMock()):
        run(bot.find("admin_subscription_config_confirm")(event_factory(data=b"admin_subscription_config_confirm")))


@pytest.mark.parametrize("success", [False, True])
def test_reminder_settings_and_update(handler_bot, event_factory, success):
    bot, _ = register(handler_bot)
    with common_patches()[0], patch("handlers.admin_handlers.DataManager.get_expiry_reminder_days", return_value=3), patch(
        "handlers.admin_handlers.DataManager.get_expiring_subscription_users", return_value=[]
    ), patch("handlers.admin_handlers.DataManager.set_expiry_reminder_days", return_value=success), patch(
        "handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"
    ), patch("handlers.admin_handlers._audit_result", return_value=True), patch(
        "handlers.admin_handlers.safe_edit", new=AsyncMock()
    ):
        run(bot.find("admin_reminder_settings")(event_factory(data=b"admin_reminder_settings")))
        run(bot.find("set_reminder_days")(event_factory(data=b"set_reminder_7")))


@pytest.mark.parametrize("text", ["/sub", "/sub x go 30", "/sub 2 bad 30", "/sub 2 plus 30 15", "/sub 2 pro 30"])
def test_sub_command_inputs(handler_bot, event_factory, text):
    bot, _ = register(handler_bot)
    event = event_factory(text=text)
    with common_patches()[0], patch("handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"), patch(
        "handlers.admin_handlers._audit_result", return_value=True
    ), patch("handlers.admin_handlers.DataManager.is_admin", return_value=False), patch(
        "handlers.admin_handlers.DataManager.quote_subscription",
        return_value={"plan_id": "plus", "plan_name": "PLUS", "quota": 15},
    ), patch("handlers.admin_handlers.DataManager.get_subscription", return_value=None):
        run(bot.find("sub_command")(event))
    event.respond.assert_awaited_once()


@pytest.mark.parametrize("text,existing", [("/delsub", True), ("/delsub x", True), ("/delsub 2", False), ("/delsub 2", True)])
def test_delsub_command_inputs(handler_bot, event_factory, text, existing):
    bot, _ = register(handler_bot)
    event = event_factory(text=text)
    with common_patches()[0], patch("handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"), patch(
        "handlers.admin_handlers._audit_result", return_value=True
    ), patch("handlers.admin_handlers.DataManager.is_admin", return_value=False), patch(
        "handlers.admin_handlers.DataManager.get_subscription", return_value={"plan_id": "go"} if existing else None
    ):
        run(bot.find("delsub_command")(event))
    event.respond.assert_awaited_once()


@pytest.mark.parametrize("has_users", [False, True])
def test_vip_list_and_help_callbacks(handler_bot, event_factory, has_users):
    payment = payment_stub()
    payment.get_user_order_summaries = Mock(return_value={"total": 0, "items": []})
    bot, _ = register(handler_bot, payment)
    users = [{"user_id": 2}] if has_users else []
    with common_patches()[0], patch("handlers.admin_handlers.DataManager.get_all_subscription_users", return_value=users), patch(
        "handlers.admin_handlers._get_user_display_name", new=AsyncMock(return_value="User")
    ), patch("handlers.admin_handlers.AccountManager.get_quota_status", return_value={"used": 1}), patch(
        "handlers.admin_handlers.edit_or_respond", new=AsyncMock()
    ) as edit:
        run(bot.find("admin_list_vip")(event_factory(data=b"admin_list_vip")))
    edit.assert_awaited_once()

    for name, data in (("admin_subscription_grant_help", b"admin_subscription_grant_help"), ("admin_subscription_delete_help", b"admin_subscription_delete_help")):
        with common_patches()[0], patch("handlers.admin_handlers.safe_edit", new=AsyncMock()) as safe:
            run(bot.find(name)(event_factory(data=data)))
        safe.assert_awaited_once()
