# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from payments.payment_handlers import _finish_audit, setup_payment_handlers


def build_payment_system(**changes):
    values = {
        "return_url": "https://example.test/return",
        "pending_orders": {},
        "processed_orders": set(),
        "create_payment_link": AsyncMock(),
        "check_order_status": AsyncMock(),
        "find_order_by_unique_id": AsyncMock(),
        "get_order_snapshot": Mock(return_value=None),
        "list_admin_orders": Mock(return_value={"total": 0}),
        "_rebuild_active_order_ids": Mock(),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def register(handler_bot, payment_system):
    asyncio.run(setup_payment_handlers(handler_bot, payment_system))
    return handler_bot


def invoke(callback, event):
    return asyncio.run(callback(event))


def test_finish_audit_supplies_actor_action_and_target(event_factory):
    event = event_factory(sender_id=77)
    with patch("payments.payment_handlers.AdminAuditLog.record_result", return_value=True) as record:
        assert _finish_audit("audit", event, "action", "success", "order", "o-1")
    record.assert_called_once_with(
        "audit",
        "success",
        admin_id=77,
        action="action",
        target_type="order",
        target_id="o-1",
    )


def test_all_payment_commands_reject_non_admins(handler_bot, event_factory):
    payment = build_payment_system()
    register(handler_bot, payment)
    for handler_name in ["test_payment", "check_order_status", "reload_data_command", "check_unique_id", "list_orders"]:
        event = event_factory(text="/ignored")
        with patch(
            "payments.payment_handlers.require_admin", new=AsyncMock(return_value=False)
        ):
            invoke(handler_bot.find(handler_name), event)
        event.respond.assert_not_awaited()


@pytest.mark.parametrize(
    ("result", "audited", "fragment"),
    [
        ({"success": True, "order_id": "order-1", "pay_url": "https://pay"}, True, "order-1"),
        ({"success": True, "order_id": "order-1", "pay_url": "https://pay"}, False, "审计记录失败"),
        ({"success": False, "error": "provider down"}, True, "provider down"),
        ({"success": False}, True, "未知错误"),
    ],
)
def test_payment_test_command_records_result_and_never_grants_rights(
    handler_bot, event_factory, result, audited, fragment
):
    payment = build_payment_system(create_payment_link=AsyncMock(return_value=result))
    register(handler_bot, payment)
    event = event_factory(sender_id=42, text="/test_payment")
    with patch(
        "payments.payment_handlers.require_admin", new=AsyncMock(return_value=True)
    ), patch(
        "payments.payment_handlers.AdminAuditLog.record_attempt", return_value="audit"
    ), patch("payments.payment_handlers._finish_audit", return_value=audited) as finish, patch(
        "payments.payment_handlers.time.time_ns", return_value=1_234_000_000
    ), patch("payments.payment_handlers.secrets.token_hex", return_value="fixed"):
        invoke(handler_bot.find("test_payment"), event)

    request = payment.create_payment_link.await_args.kwargs
    assert request["amount"] == "0.01"
    assert request["unique_id"] == "test-42-1234-fixed"
    assert request["_order_metadata"] == {
        "user_id": 42,
        "type": "payment_test",
        "test_order": True,
    }
    assert fragment in event.respond.await_args.args[0]
    finish.assert_called_once()


@pytest.mark.parametrize(
    ("text", "result", "audited", "fragment"),
    [
        ("/check_order", None, True, "格式错误"),
        ("/check_order order-1", {"success": True, "status": "paid"}, True, "完成处理"),
        ("/check_order order-1", {"success": True, "status": "pending"}, False, "审计记录失败"),
        ("/check_order order-1", {"success": False, "error": "bad signature"}, True, "bad signature"),
        ("/check_order order-1", {"success": False}, True, "未知错误"),
    ],
)
def test_check_order_command_covers_validation_and_provider_outcomes(
    handler_bot, event_factory, text, result, audited, fragment
):
    snapshots = [
        {"status": "pending", "processed": False},
        {"status": "paid", "processed": True},
        {"status": "paid", "processed": True},
    ]
    payment = build_payment_system(
        check_order_status=AsyncMock(return_value=result),
        get_order_snapshot=Mock(side_effect=snapshots),
    )
    register(handler_bot, payment)
    event = event_factory(text=text)
    with patch(
        "payments.payment_handlers.require_admin", new=AsyncMock(return_value=True)
    ), patch(
        "payments.payment_handlers.AdminAuditLog.record_attempt", return_value="audit"
    ), patch("payments.payment_handlers._finish_audit", return_value=audited) as finish:
        invoke(handler_bot.find("check_order_status"), event)

    assert fragment in event.respond.await_args.args[0]
    if result is None:
        payment.check_order_status.assert_not_awaited()
        assert finish.call_args.kwargs["error"] == "missing_order_id"
    else:
        payment.check_order_status.assert_awaited_once_with("order-1")


@pytest.mark.parametrize(("loaded", "audited", "fragment"), [(True, True, "重新加载完成"), (True, False, "审计记录失败"), (False, True, "重新加载失败")])
def test_reload_data_rebuilds_only_after_success(
    handler_bot, event_factory, loaded, audited, fragment
):
    payment = build_payment_system()
    register(handler_bot, payment)
    event = event_factory(text="/reload_data")
    orders = {
        "processed": {"processed": True},
        "pending": {"processed": False},
    }
    with patch(
        "payments.payment_handlers.require_admin", new=AsyncMock(return_value=True)
    ), patch(
        "payments.payment_handlers.AdminAuditLog.record_attempt", return_value="audit"
    ), patch("payments.payment_handlers._finish_audit", return_value=audited), patch(
        "payments.payment_handlers.DataManager.load_user_data", return_value=loaded
    ), patch(
        "payments.payment_handlers.DataManager.get_payment_orders", return_value=orders
    ):
        invoke(handler_bot.find("reload_data_command"), event)

    assert fragment in event.respond.await_args.args[0]
    if loaded:
        assert payment.pending_orders == orders
        assert payment.processed_orders == {"processed"}
        payment._rebuild_active_order_ids.assert_called_once_with()
    else:
        payment._rebuild_active_order_ids.assert_not_called()


@pytest.mark.parametrize(
    ("text", "order_id", "fragment"),
    [
        ("/check_unique_id", None, "格式错误"),
        ("/check_unique_id unique-1", None, "未找到"),
        ("/check_unique_id unique-1", "order-1", "12.5 USDT"),
    ],
)
def test_unique_id_lookup_handles_missing_absent_and_found_orders(
    handler_bot, event_factory, text, order_id, fragment
):
    payment = build_payment_system(
        find_order_by_unique_id=AsyncMock(return_value=order_id),
        pending_orders={
            "order-1": {
                "status": "paid",
                "user_id": 88,
                "amount": "12.5",
                "coin": "USDT",
            }
        },
    )
    register(handler_bot, payment)
    event = event_factory(text=text)
    with patch(
        "payments.payment_handlers.require_admin", new=AsyncMock(return_value=True)
    ), patch(
        "payments.payment_handlers.AdminAuditLog.record_attempt", return_value="audit"
    ), patch("payments.payment_handlers._finish_audit", return_value=True):
        invoke(handler_bot.find("check_unique_id"), event)
    assert fragment in event.respond.await_args.args[0]


def test_list_orders_uses_paginated_admin_center(handler_bot, event_factory):
    payment = build_payment_system(list_admin_orders=Mock(return_value={"total": 37}))
    register(handler_bot, payment)
    event = event_factory(text="/list_orders")
    with patch(
        "payments.payment_handlers.require_admin", new=AsyncMock(return_value=True)
    ), patch(
        "payments.payment_handlers.AdminAuditLog.record_attempt", return_value="audit"
    ), patch("payments.payment_handlers._finish_audit", return_value=True):
        invoke(handler_bot.find("list_orders"), event)

    assert "37" in event.respond.await_args.args[0]
    buttons = event.respond.await_args.kwargs["buttons"]
    assert buttons[0][0].data == b"admin_orders_all_0"
    payment.list_admin_orders.assert_called_once_with("all", page_size=1)
