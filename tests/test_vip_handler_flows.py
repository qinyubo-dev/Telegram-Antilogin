# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from handlers import vip_handlers
from handlers.handler_utils import clear_state, get_state, set_state


def run(awaitable):
    return asyncio.run(awaitable)


def payment_stub(**overrides):
    values = {
        "pending_orders": {},
        "create_subscription_payment": AsyncMock(),
        "check_order_status": AsyncMock(),
        "cancel_order": AsyncMock(),
        "bind_order_message": Mock(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def register(handler_bot, payment=None):
    payment = payment or payment_stub()
    run(vip_handlers.setup_vip_handlers(handler_bot, payment))
    return handler_bot, payment


@pytest.fixture(autouse=True)
def _clear_handler_state():
    clear_state(1001)
    yield
    clear_state(1001)


def test_buy_vip_renders_catalog_and_falls_back_to_response(handler_bot, event_factory):
    bot, _ = register(handler_bot)
    set_state(1001, stale=True)
    event = event_factory(data=b"buy_vip")
    with patch("handlers.vip_handlers.DataManager.get_user_language", return_value="zh"), patch(
        "handlers.vip_handlers.DataManager.get_subscription", return_value={"active": True}
    ), patch("handlers.vip_handlers._catalog_text", return_value="catalog"), patch(
        "handlers.vip_handlers._current_subscription_text", return_value="current"
    ), patch("handlers.vip_handlers.safe_edit", new=AsyncMock(side_effect=RuntimeError("gone"))):
        run(bot.find("buy_vip")(event))
    assert get_state(1001) == {}
    assert "catalogcurrent" in event.respond.await_args.args[0]


@pytest.mark.parametrize("plan_id", ["go", "plus", "pro"])
def test_plan_selection_renders_periods(handler_bot, event_factory, plan_id):
    bot, _ = register(handler_bot)
    event = event_factory(data=f"buy_sub_{plan_id}".encode())
    quote = {"plan_id": plan_id, "quota": 5, "price": "1"}
    with patch("handlers.vip_handlers.DataManager.quote_subscription", return_value=quote), patch(
        "handlers.vip_handlers.DataManager.classify_subscription_change", return_value="new"
    ), patch(
        "handlers.vip_handlers.DataManager.get_subscription_periods",
        return_value={30: {}, 90: {}, 180: {}, 365: {}},
    ), patch("handlers.vip_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("select_plan")(event))
    assert get_state(1001)["subscription_plan_id"] == plan_id
    assert edit.await_count == 1


def test_upgrade_plan_creates_and_binds_order(handler_bot, event_factory):
    payment = payment_stub()
    payment.create_subscription_payment.return_value = {
        "success": True,
        "order_id": "o1",
        "pay_url": "https://pay.invalid/o1",
    }
    payment.pending_orders["o1"] = {
        "amount": "2.5",
        "quota": 10,
        "billing_mode": "prorated_upgrade",
        "change_type": "upgrade",
        "period_days": 30,
    }
    bot, _ = register(handler_bot, payment)
    event = event_factory(data=b"buy_sub_pro", chat_id=88, message_id=99)
    quote = {"plan_id": "pro", "quota": None, "price": "2.5"}
    rendered = SimpleNamespace(id=101)
    with patch("handlers.vip_handlers.DataManager.quote_subscription", return_value=quote), patch(
        "handlers.vip_handlers.DataManager.classify_subscription_change", return_value="upgrade"
    ), patch("handlers.vip_handlers.safe_edit_message", new=AsyncMock(return_value=rendered)):
        run(bot.find("select_plan")(event))
    payment.create_subscription_payment.assert_awaited_once_with(1001, "pro", None, period_days=30)
    payment.bind_order_message.assert_called_once_with("o1", 88, 101)


def test_period_selection_expired_and_valid(handler_bot, event_factory):
    payment = payment_stub()
    payment.create_subscription_payment.return_value = {
        "success": False,
        "error": "provider down",
    }
    bot, _ = register(handler_bot, payment)
    expired = event_factory(data=b"subscription_period_90")
    run(bot.find("select_subscription_period")(expired))
    assert expired.answer.await_args.kwargs == {"alert": True}

    set_state(
        1001,
        subscription_period_selection=True,
        subscription_plan_id="go",
        subscription_quota=2,
    )
    valid = event_factory(data=b"subscription_period_90")
    with patch("handlers.vip_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("select_subscription_period")(valid))
    payment.create_subscription_payment.assert_awaited_once_with(1001, "go", 2, period_days=90)
    assert "provider down" in edit.await_args.args[1]
    assert get_state(1001) == {}


def test_period_page_edit_failure_responds(handler_bot, event_factory):
    bot, _ = register(handler_bot)
    event = event_factory(data=b"buy_sub_go")
    quote = {"plan_id": "go", "quota": 5, "price": "1"}
    with patch("handlers.vip_handlers.DataManager.quote_subscription", return_value=quote), patch(
        "handlers.vip_handlers.DataManager.classify_subscription_change", return_value="renewal"
    ), patch(
        "handlers.vip_handlers.DataManager.get_subscription_periods",
        return_value={30: {}, 90: {}, 180: {}, 365: {}},
    ), patch("handlers.vip_handlers.safe_edit", new=AsyncMock(side_effect=RuntimeError)):
        run(bot.find("select_plan")(event))
    event.respond.assert_awaited_once()


def test_plus_custom_and_quota_input_paths(handler_bot, event_factory):
    bot, _ = register(handler_bot)
    custom = event_factory(data=b"buy_sub_plus_custom")
    with patch(
        "handlers.vip_handlers.DataManager.get_subscription_catalog",
        return_value={"plus": {"quota": 5, "min_addon": 2, "addon_unit_price": "0.5"}},
    ), patch("handlers.vip_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("plus_custom")(custom))
    assert get_state(1001)["subscription_plus_quota"] is True
    assert edit.await_count == 1

    callback = bot.find("plus_quota_input")
    command = event_factory(text="/cancel")
    run(callback(command))
    assert command.respond.await_count == 0

    invalid = event_factory(text="nope")
    run(callback(invalid))
    invalid.respond.assert_awaited_once()
    assert get_state(1001) == {}

    set_state(1001, subscription_plus_quota=True)
    valid = event_factory(text="12")
    quote = {"plan_id": "plus", "quota": 12, "price": "3"}
    with patch("handlers.vip_handlers.DataManager.quote_subscription", return_value=quote), patch(
        "handlers.vip_handlers.DataManager.classify_subscription_change", return_value="new"
    ), patch(
        "handlers.vip_handlers.DataManager.get_subscription_periods",
        return_value={30: {}, 90: {}, 180: {}, 365: {}},
    ), patch("handlers.vip_handlers.safe_edit", new=AsyncMock()):
        run(callback(valid))
    assert get_state(1001)["subscription_quota"] == 12


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"success": True, "status": "paid"}, "edit"),
        ({"success": True, "status": "pending"}, "unpaid"),
        ({"success": False, "error": "bad gateway"}, "bad gateway"),
    ],
)
def test_payment_check_results(handler_bot, event_factory, status, expected):
    order = {"user_id": 1001, "amount": "1", "coin": "USDT"}
    payment = payment_stub(pending_orders={"o1": order})
    payment.check_order_status.return_value = status
    bot, _ = register(handler_bot, payment)
    event = event_factory(data=b"check_payment_o1")
    with patch("handlers.vip_handlers.safe_edit", new=AsyncMock()) as edit, patch(
        "handlers.vip_handlers.DataManager.get_subscription",
        return_value={"plan_id": "go", "expires_at": "2030-01-01"},
    ):
        run(bot.find("check_payment_status")(event))
    if expected == "edit":
        edit.assert_awaited_once()
    elif expected == "unpaid":
        assert event.answer.await_args.kwargs == {"alert": True}
    else:
        assert expected in event.answer.await_args.args[0]


def test_billing_text_variants():
    standard = {
        "billing_mode": "standard",
        "period_days": 365,
        "actual_discount_percent": "10",
        "list_price": "12",
        "amount": "10.8",
    }
    assert "实际节省 10%" in vip_handlers._upgrade_order_text(standard, "zh")
    assert "10.8" in vip_handlers._upgrade_order_text(standard, "en")
    standard["period_days"] = 30
    assert "标准原价" not in vip_handlers._upgrade_order_text(standard, "zh")

    upgrade = {
        "billing_mode": "prorated_upgrade",
        "amount": "2",
        "upgrade_snapshot": {
            "target_expires_at": "invalid",
            "billable_days": 3,
            "source_value": "1",
            "target_value": "3",
            "uses_catalog_fallback": True,
        },
    }
    assert "历史权益" in vip_handlers._upgrade_order_text(upgrade, "zh")
    assert "invalid" in vip_handlers._upgrade_order_text(upgrade, "en")


def test_payment_check_wrong_owner_and_manual_confirm(handler_bot, event_factory):
    payment = payment_stub(pending_orders={"o1": {"user_id": 77}})
    bot, _ = register(handler_bot, payment)
    wrong = event_factory(data=b"check_payment_o1")
    run(bot.find("check_payment_status")(wrong))
    payment.check_order_status.assert_not_awaited()

    payment.pending_orders["o1"]["user_id"] = 1001
    payment.check_order_status.return_value = {"success": True, "status": "pending"}
    manual = event_factory(data=b"manual_confirm_o1")
    run(bot.find("manual_confirm_payment")(manual))
    payment.check_order_status.assert_awaited_once_with("o1")


@pytest.mark.parametrize("result", [{"success": True}, {"status": "paid"}, {"error": "no"}])
def test_cancel_order_results(handler_bot, event_factory, result):
    payment = payment_stub(
        pending_orders={"o1": {"user_id": 1001, "amount": "1", "coin": "USDT"}}
    )
    payment.cancel_order.return_value = result
    bot, _ = register(handler_bot, payment)
    event = event_factory(data=b"cancel_order_o1")
    with patch("handlers.vip_handlers.safe_edit", new=AsyncMock()) as edit, patch(
        "handlers.vip_handlers.DataManager.get_subscription",
        return_value={"plan_id": "go", "expires_at": "2030-01-01"},
    ), patch("handlers.vip_handlers._catalog_text", return_value="catalog"):
        run(bot.find("cancel_payment_order")(event))
    if result.get("success") or result.get("status") == "paid":
        edit.assert_awaited_once()
    else:
        assert event.answer.await_args.args[0] == "no"
