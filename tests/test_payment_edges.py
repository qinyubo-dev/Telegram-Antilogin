# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from payments.payment_system import PaymentSystem, _display_percent


def make_payment(orders=None) -> PaymentSystem:
    with patch("payments.payment_system.DataManager.get_payment_orders", return_value=orders or {}):
        return PaymentSystem()


def run(awaitable):
    return asyncio.run(awaitable)


def test_display_percent_formats_and_falls_back():
    for value, expected in [("8.0", "8"), ("8.25", "8.2"), (None, "None"), ("invalid", "invalid")]:
        assert _display_percent(value) == expected


def test_active_order_classification():
    cases = [
        ({}, True),
        ({"legacy_origin": "vip_purchase"}, False),
        ({"processed": True}, False),
        ({"status": "cancelled"}, False),
        ({"status": "expired"}, False),
        ({"status": "paid"}, True),
        ({"needs_manual_review": True}, True),
        ({"auto_check_stopped": True}, False),
    ]
    for order, active in cases:
        assert PaymentSystem._is_active_order(order) is active


def test_save_and_rebuild_prunes_inactive_retry_and_unlocked_locks():
    payment = make_payment({"active": {}, "closed": {"status": "cancelled"}})
    payment._order_retry_state = {"active": {}, "closed": {}}
    payment._order_locks = {"active": asyncio.Lock(), "closed": asyncio.Lock()}
    with patch("payments.payment_system.DataManager.save_payment_orders", return_value=True):
        assert payment._save_orders()
    assert payment._active_order_ids == {"active"}
    assert set(payment._order_retry_state) == {"active"}
    assert set(payment._order_locks) == {"active"}

    with patch("payments.payment_system.DataManager.save_payment_orders", return_value=False):
        assert not payment._save_orders()


def test_lock_factories_reuse_normalized_keys_and_retry_due_logic():
    payment = make_payment()
    assert payment._get_order_lock("o") is payment._get_order_lock("o")
    assert payment._get_subscription_user_lock("7") is payment._get_subscription_user_lock(7)
    assert payment._order_check_is_due("missing", 10)
    payment._order_retry_state["o"] = {"next_check_at": 11}
    assert not payment._order_check_is_due("o", 10)
    assert payment._order_check_is_due("o", 11)


def test_retry_state_honors_backoff_cap_retry_after_and_reset():
    payment = make_payment()
    payment.poll_interval = 2
    payment.retry_backoff_max = 4
    payment._update_order_retry("o", {"retryable": True}, 10)
    assert payment.get_order_retry_snapshot("o") == {"failures": 1, "next_check_at": 12}
    payment._update_order_retry("o", {"retryable": True, "retry_after": 9}, 12)
    assert payment.get_order_retry_snapshot("o") == {"failures": 2, "next_check_at": 21}
    snapshot = payment.get_order_retry_snapshot("o")
    snapshot["failures"] = 99
    assert payment.get_order_retry_snapshot("o")["failures"] == 2
    payment._update_order_retry("o", {"success": True}, 13)
    assert payment.get_order_retry_snapshot("o") == {}


def test_open_subscription_order_and_bot_binding():
    payment = make_payment(
        {
            "other": {"type": "other", "user_id": 1},
            "closed": {"type": "subscription_purchase", "user_id": 1, "status": "expired"},
            "open": {"type": "subscription_purchase", "user_id": "2"},
        }
    )
    assert not payment._has_open_subscription_order(1)
    assert payment._has_open_subscription_order(2)
    bot = object()
    payment.set_bot(bot)
    assert payment.bot is bot

    assert not payment.bind_order_message("missing", 1, 2)
    with patch.object(payment, "_save_orders", return_value=True):
        assert payment.bind_order_message("open", "3", "4")
    assert payment.pending_orders["open"]["order_message_chat_id"] == 3
    assert payment.pending_orders["open"]["order_message_id"] == 4


def test_user_order_summaries_ignore_invalid_rows_sort_and_copy():
    payment = make_payment(
        {
            "bad": "row",
            "invalid": {"user_id": "x", "created_time": 99},
            "old": {"user_id": 7, "created_time": 1, "amount": "1"},
            "new": {"user_id": "7", "created_time": 2, "legacy_origin": "vip_purchase"},
        }
    )
    result = payment.get_user_order_summaries(7, limit=1)
    assert result["total"] == 2
    assert result["items"][0]["order_id"] == "new"
    assert result["items"][0]["legacy_read_only"]
    result["items"][0]["status"] = "changed"
    assert "status" not in payment.pending_orders["new"]
    assert payment.get_user_order_summaries(7, limit=-1)["items"] == []


def test_delete_expired_message_covers_guards_success_and_failure():
    async def exercise():
        payment = make_payment({"o": {}})
        await payment._delete_expired_order_message("o")
        payment.bot = SimpleNamespace(delete_messages=AsyncMock())
        await payment._delete_expired_order_message("o")
        payment.pending_orders["o"].update(
            {"order_message_chat_id": "1", "order_message_id": "2"}
        )
        with patch.object(payment, "_save_orders", return_value=True):
            await payment._delete_expired_order_message("o")
        payment.bot.delete_messages.assert_awaited_once_with(1, [2])
        assert payment.pending_orders["o"]["order_message_deleted"]
        await payment._delete_expired_order_message("o")

        payment.pending_orders["failed"] = {
            "order_message_chat_id": 1,
            "order_message_id": 2,
        }
        payment.bot.delete_messages.side_effect = RuntimeError("delete")
        await payment._delete_expired_order_message("failed")
        assert "order_message_deleted" not in payment.pending_orders["failed"]

    run(exercise())


def test_notify_admins_deduplicates_persists_and_marks_transient_failure():
    async def exercise():
        payment = make_payment({"o": {"notified": [1, "bad"]}})
        await payment._notify_admins("message", "notified", "o")
        payment.bot = SimpleNamespace(send_message=AsyncMock(side_effect=[None, ConnectionError("down")]))
        with patch("payments.payment_system.config.ADMIN_IDS", [1, 2, 3]), patch.object(
            payment, "_save_orders", return_value=True
        ) as save, patch("payments.payment_system.account_runtime.mark_notify_bot_healthy") as healthy, patch(
            "payments.payment_system.account_runtime.mark_notify_bot_degraded"
        ) as degraded:
            await payment._notify_admins("message", "notified", "o")
        payment.bot.send_message.assert_any_await(2, "message")
        payment.bot.send_message.assert_any_await(3, "message")
        healthy.assert_called_once_with()
        degraded.assert_called_once()
        save.assert_called_once_with()
        assert payment.pending_orders["o"]["notified"] == [1, 2]
        await payment._notify_admins("message", "notified", "missing")

    run(exercise())


def test_admin_notification_helpers_cover_guards_custom_seat_and_unknown_reason():
    async def exercise():
        payment = make_payment(
            {
                "skip": {"type": "other"},
                "new": {
                    "type": "subscription_purchase",
                    "change_type": "new",
                    "plan_id": "plus",
                    "quota": 15,
                    "addon": 5,
                    "period_days": 90,
                    "amount": "4",
                    "coin": "USDT",
                    "user_id": 8,
                },
                "bad": {"user_id": 8, "amount": "1", "status": "paid"},
            }
        )
        with patch.object(payment, "_notify_admins", new=AsyncMock()) as notify:
            await payment._notify_admin_new_subscription("skip")
            notify.assert_not_awaited()
            await payment._notify_admin_new_subscription("new")
            builder = notify.await_args.args[0]
            assert "15 席" in builder(1, "zh")
            assert "Custom seats: 15" in builder(2, "en")
            await payment._notify_admin_order_exception("bad", "custom_reason")
            builder = notify.await_args.args[0]
            assert "custom_reason" in builder(1, "zh")
            assert "custom_reason" in builder(2, "en")

    run(exercise())


def test_admin_notifications_use_each_recipient_language_and_keep_deduplication():
    async def exercise():
        payment = make_payment({
            "o": {
                "type": "subscription_purchase", "change_type": "new",
                "plan_id": "go", "quota": 2, "period_days": 30,
                "amount": "1", "coin": "USDT", "user_id": 8,
            }
        })
        payment.bot = SimpleNamespace(send_message=AsyncMock())
        with patch("payments.payment_system.config.ADMIN_IDS", [1, 2]), patch(
            "payments.payment_system.DataManager.get_user_language",
            side_effect=lambda admin_id: "zh" if admin_id == 1 else "en",
        ), patch.object(payment, "_save_orders", return_value=True):
            await payment._notify_admin_new_subscription("o")
            await payment._notify_admin_new_subscription("o")
        calls = payment.bot.send_message.await_args_list
        assert len(calls) == 2
        assert "新订阅开通" in calls[0].args[1]
        assert "New subscription" in calls[1].args[1]
        assert payment.pending_orders["o"]["admin_new_subscription_notified_to"] == [1, 2]

    run(exercise())


def test_signature_values_and_response_summary_redact_secrets():
    payload = {
        "sign": "ignored",
        "flag": True,
        "amount": Decimal("1.20"),
        "none": None,
        "empty": "",
        "nested": {"token": "secret", "value": False},
    }
    assert PaymentSystem._flatten_signature_values(payload) == {
        "flag": "true",
        "amount": "1.20",
        "nested.token": "secret",
        "nested.value": "false",
    }
    summary = PaymentSystem._safe_response_summary(
        '  {"token":super-secret, "sign"=ABC, "value":1}  ', limit=200
    )
    assert "super-secret" not in summary
    assert "ABC" not in summary


def test_query_methods_validate_provider_data():
    cases = [
        ("query_order_by_id", {}, "未知状态"),
        ("query_order_by_id", {"status": "9"}, "未知状态"),
        ("query_order_by_id", {"status": 0, "order_id": "o"}, "pending"),
        ("query_order_by_unique_id", {}, "未知状态"),
        ("query_order_by_unique_id", {"status": None}, "未知状态"),
        ("query_order_by_unique_id", {"status": "1", "order_id": "o"}, "paid"),
    ]
    payment = make_payment()
    for method, data, expected in cases:
        with patch.object(payment, "_signed_request", new=AsyncMock(return_value={"success": True, "data": data})):
            result = run(getattr(payment, method)("value"))
        assert expected in (result.get("error") or result.get("status"))


def test_paid_order_validation_reports_exact_mismatch():
    cases = [
        ({"order_id": "bad", "unique_id": "u", "coin": "USDT", "amount": "1"}, False, "order_id"),
        ({"order_id": "o", "unique_id": "bad", "coin": "USDT", "amount": "1"}, False, "unique_id"),
        ({"order_id": "o", "unique_id": "u", "coin": "BTC", "amount": "1"}, False, "coin"),
        ({"order_id": "o", "unique_id": "u", "coin": "USDT", "amount": "bad"}, False, "金额校验"),
        ({"order_id": "o", "unique_id": "u", "coin": "USDT", "amount": "2"}, False, "金额不一致"),
        ({"order_id": "o", "unique_id": "u", "coin": "usdt", "amount": "1.0"}, True, ""),
    ]
    payment = make_payment({"o": {"unique_id": "u", "coin": "USDT", "amount": "1"}})
    for remote, valid, fragment in cases:
        actual_valid, error = payment._validate_paid_order("o", remote)
        assert actual_valid is valid
        assert fragment in error


def test_check_order_status_short_circuits_local_terminal_states():
    cases = [
        (None, "订单不存在"),
        ({"legacy_origin": "vip_purchase"}, "遗留订阅订单"),
        ({"processed": True}, "paid"),
        ({"status": "expired"}, "订单已过期"),
        ({"status": "cancelled"}, "订单已取消"),
    ]
    for order, expected in cases:
        payment = make_payment({} if order is None else {"o": order})
        result = run(payment.check_order_status("o"))
        assert expected in str(result.values())


@pytest.mark.parametrize("order_type", [None, "unknown"])
def test_process_paid_order_rejects_missing_and_unknown_types(order_type):
    orders = {} if order_type is None else {"o": {"status": "paid", "type": order_type}}
    payment = make_payment(orders)
    with patch.object(payment, "_notify_admin_order_exception", new=AsyncMock()) as notify:
        assert not run(payment.process_paid_order("o"))
    if order_type is not None:
        notify.assert_awaited_once()


def test_process_payment_test_success_and_save_failure():
    for saved in (True, False):
        payment = make_payment({"o": {"status": "paid", "type": "payment_test"}})
        with patch.object(payment, "_save_orders", return_value=saved), patch.object(
            payment, "_notify_admin_order_exception", new=AsyncMock()
        ) as notify:
            assert run(payment.process_paid_order("o")) is saved
        assert payment.pending_orders["o"]["processed"]
        if saved:
            assert "o" in payment.processed_orders
        else:
            notify.assert_awaited_once_with("o", "fulfillment_failed")


def test_custom_plus_detection():
    cases = [
        ({"plan_id": "go"}, False),
        ({"plan_id": "plus", "addon": 1}, True),
        ({"plan_id": "plus", "quota": 11}, True),
        ({"plan_id": "plus", "quota": "bad"}, False),
    ]
    with patch(
        "payments.payment_system.DataManager.get_subscription_catalog",
        return_value={"plus": {"quota": 10}},
    ):
        for quote, custom in cases:
            assert PaymentSystem._is_custom_plus(quote) is custom


def test_cancel_order_terminal_and_persistence_edges():
    cases = [
        ({}, 7, "不属于"),
        ({"user_id": 8}, 7, "不属于"),
        ({"user_id": 7, "processed": True}, 7, "无法取消"),
        ({"user_id": 7, "status": "cancelled"}, 7, "cancelled"),
    ]
    for order, user_id, expected in cases:
        payment = make_payment({"o": order} if order else {})
        result = run(payment.cancel_order("o", user_id))
        assert expected in str(result.values())

    payment = make_payment({"o": {"user_id": 7, "status": "pending"}})
    with patch.object(
        payment, "_check_order_status_unlocked", new=AsyncMock(return_value={"success": True, "status": "pending"})
    ), patch.object(payment, "_save_orders", return_value=False):
        result = run(payment.cancel_order("o", 7))
    assert not result["success"]
    assert payment.pending_orders["o"] == {"user_id": 7, "status": "pending"}


def test_monitor_once_handles_invalid_legacy_review_expired_and_active_orders():
    now = 10_000.0
    orders = {
        "invalid": "row",
        "processed": {"processed": True},
        "review": {"needs_manual_review": True, "created_time": now},
        "expired": {"status": "expired", "created_time": now},
        "cancelled": {"status": "cancelled", "created_time": now},
        "legacy": {},
        "old": {"status": "pending", "created_time": 0},
        "stopped": {"status": "pending", "created_time": 9_500},
        "active": {"status": "pending", "created_time": now},
    }
    payment = make_payment(orders)
    payment.auto_check_window = 100
    payment.order_expiry_window = 1_000
    payment._active_order_ids = set(orders)
    with patch("payments.payment_system.time.time", return_value=now), patch.object(
        payment, "_save_orders", return_value=True
    ) as save, patch.object(
        payment, "_notify_admin_order_exception", new=AsyncMock()
    ) as notify, patch.object(
        payment, "_delete_expired_order_message", new=AsyncMock()
    ) as delete, patch.object(
        payment, "_expire_order_if_due", new=AsyncMock(return_value=True)
    ) as expire, patch.object(
        payment, "check_order_status", new=AsyncMock(return_value={"success": True, "status": "pending"})
    ) as check:
        run(payment._monitor_pending_orders_once())
    assert payment.pending_orders["legacy"]["auto_check_stop_reason"] == "legacy_order_missing_created_time"
    assert payment.pending_orders["stopped"]["auto_check_stopped"]
    notify.assert_awaited_once()
    delete.assert_awaited_once_with("expired")
    expire.assert_awaited_once_with("old", now)
    assert {call.args[0] for call in check.await_args_list} == {"active", "review"}
    assert save.called


def test_monitor_lifecycle_is_idempotent_and_closes_session():
    async def exercise():
        payment = make_payment()
        blocker = asyncio.Event()

        async def monitor():
            await blocker.wait()

        payment._monitor_pending_orders = monitor
        first = await payment.start_monitoring()
        assert await payment.start_monitoring() is first
        session = SimpleNamespace(closed=False, close=AsyncMock())
        payment._session = session
        await payment.stop_monitoring()
        assert first.cancelled()
        session.close.assert_awaited_once_with()
        assert payment._session is None
        await payment.stop_monitoring()

    run(exercise())


def test_expire_order_short_circuits_ineligible_rows():
    orders = [
        None,
        {"processed": True, "created_time": 0},
        {"status": "paid", "created_time": 0},
        {"status": "cancelled", "created_time": 0},
        {"status": "expired", "created_time": 0},
        {"status": "pending"},
        {"status": "pending", "created_time": 99},
    ]
    for order in orders:
        payment = make_payment({} if order is None else {"o": order})
        payment.order_expiry_window = 10
        with patch.object(payment, "_check_order_status_unlocked", new=AsyncMock()) as check:
            assert not run(payment._expire_order_if_due("o", now=100))
        check.assert_not_awaited()


def test_expire_order_rolls_back_failed_save_and_scans_only_stale_subscription_orders():
    async def exercise():
        payment = make_payment(
            {
                "o": {"status": "pending", "created_time": 0},
                "other": {"type": "other", "user_id": 7, "created_time": 0},
                "fresh": {
                    "type": "subscription_purchase",
                    "user_id": 7,
                    "created_time": 99,
                },
                "stale": {
                    "type": "subscription_purchase",
                    "user_id": "7",
                    "created_time": 0,
                },
            }
        )
        payment.order_expiry_window = 10
        before = dict(payment.pending_orders["o"])
        with patch.object(
            payment,
            "_check_order_status_unlocked",
            new=AsyncMock(return_value={"success": True, "status": "pending"}),
        ), patch.object(payment, "_save_orders", return_value=False):
            assert not await payment._expire_order_if_due("o", now=100)
        assert payment.pending_orders["o"] == before

        with patch("payments.payment_system.time.time", return_value=100), patch.object(
            payment, "_expire_order_if_due", new=AsyncMock()
        ) as expire:
            await payment._expire_stale_subscription_orders_for_user(7)
        expire.assert_awaited_once_with("stale", 100)

    run(exercise())


def test_api_parser_rejects_non_object_and_wrong_merchant_before_signature():
    payment = make_payment()
    assert payment._parse_api_response("endpoint", "[]")["error_kind"] == "invalid_response"
    payload = {"status": "success", "code": 200, "id": "wrong", "sign": "x" * 64}
    result = payment._parse_api_response("endpoint", __import__("json").dumps(payload))
    assert result["error_kind"] == "security_error"
    assert "商户" in result["error"]


def test_signed_request_handles_open_circuit_and_unexpected_client_error():
    async def exercise():
        payment = make_payment()
        payment._provider_open_until = 50
        with patch.object(
            payment, "_reserve_provider_request", new=AsyncMock(return_value=(False, None))
        ), patch("payments.payment_system.time.monotonic", return_value=40):
            result = await payment._signed_request("endpoint", {})
        assert result["error_kind"] == "circuit_open"
        assert result["retry_after"] == 10

        with patch.object(
            payment, "_reserve_provider_request", new=AsyncMock(return_value=(True, None))
        ), patch.object(
            payment, "_get_session", new=AsyncMock(side_effect=ValueError("boom"))
        ), patch.object(payment, "_record_provider_result", new=AsyncMock()) as record:
            result = await payment._signed_request("endpoint", {})
        assert result["error_kind"] == "internal_error"
        record.assert_awaited_once()

    run(exercise())


@pytest.mark.parametrize(
    ("kwargs", "provider", "fragment"),
    [
        ({"unique_id": "", "amount": 1, "name": "n", "return_url": "r"}, None, "必填"),
        ({"unique_id": "u", "amount": 0, "name": "n", "return_url": "r"}, None, "大于0"),
        ({"unique_id": "u", "amount": 1, "name": "n", "return_url": "r"}, {"success": False, "error": "down"}, "down"),
        ({"unique_id": "u", "amount": 1, "name": "n", "return_url": "r"}, {"success": True, "data": []}, "数据不完整"),
        ({"unique_id": "u", "amount": 1, "name": "n", "return_url": "r"}, {"success": True, "data": {}}, "数据不完整"),
    ],
)
def test_create_payment_link_validation_and_provider_shape(kwargs, provider, fragment):
    payment = make_payment()
    context = (
        patch.object(payment, "_signed_request", new=AsyncMock(return_value=provider))
        if provider is not None
        else patch.object(payment, "_signed_request", new=AsyncMock())
    )
    with context as request:
        result = run(payment.create_payment_link(**kwargs))
    assert fragment in result["error"]
    if provider is None:
        request.assert_not_awaited()


def test_create_payment_link_save_failure_notifies_admin_and_keeps_metadata():
    payment = make_payment()
    provider = {
        "success": True,
        "data": {"order_id": 7, "pay_url": "https://pay", "status": 0},
    }
    with patch.object(
        payment, "_signed_request", new=AsyncMock(return_value=provider)
    ), patch.object(payment, "_save_orders", return_value=False), patch.object(
        payment, "_notify_admin_order_exception", new=AsyncMock()
    ) as notify:
        result = run(
            payment.create_payment_link(
                unique_id="u",
                amount="1.00",
                name="name",
                return_url="return",
                _order_metadata={"user_id": 9},
            )
        )
    assert not result["success"]
    assert payment.pending_orders["7"]["user_id"] == 9
    notify.assert_awaited_once_with("7", "local_order_save_failed")


def test_query_methods_propagate_failures_and_reject_non_mapping_data():
    payment = make_payment()
    for method in ["query_order_by_id", "query_order_by_unique_id"]:
        with patch.object(
            payment, "_signed_request", new=AsyncMock(return_value={"success": False, "error": "down"})
        ):
            assert run(getattr(payment, method)("value"))["error"] == "down"
        with patch.object(
            payment, "_signed_request", new=AsyncMock(return_value={"success": True, "data": []})
        ):
            assert "不完整" in run(getattr(payment, method)("value"))["error"]


def test_check_order_fallback_without_unique_id_and_paid_validation_failure():
    payment = make_payment({"o": {"amount": "1", "coin": "USDT"}})
    with patch.object(
        payment,
        "query_order_by_id",
        new=AsyncMock(return_value={"success": False, "fallback_allowed": True}),
    ):
        result = run(payment.check_order_status("o"))
    assert "unique_id" in result["error"]

    payment.pending_orders["o"]["unique_id"] = "u"
    remote = {
        "success": True,
        "status": "paid",
        "order_id": "wrong",
        "unique_id": "u",
        "coin": "USDT",
        "amount": "1",
    }
    with patch.object(payment, "query_order_by_id", new=AsyncMock(return_value=remote)), patch.object(
        payment, "_notify_admin_order_exception", new=AsyncMock()
    ) as notify:
        result = run(payment.check_order_status("o"))
    assert not result["success"]
    assert payment.pending_orders["o"]["needs_manual_review"]
    notify.assert_awaited_once_with("o", "paid_order_validation_failed")


@pytest.mark.parametrize(
    ("remote", "saved", "expected"),
    [
        ({"success": True, "status": "paid"}, True, "paid"),
        ({"success": False, "error": "provider"}, True, "无法确认"),
        ({"success": True, "status": "pending"}, True, "cancelled"),
    ],
)
def test_cancel_order_remote_outcomes(remote, saved, expected):
    payment = make_payment({"o": {"user_id": 7, "status": "pending"}})
    with patch.object(
        payment, "_check_order_status_unlocked", new=AsyncMock(return_value=remote)
    ), patch.object(payment, "_save_orders", return_value=saved):
        result = run(payment.cancel_order("o", 7))
    assert expected in str(result.values())


def test_find_order_by_unique_id_and_monitor_done_outcomes():
    payment = make_payment({"one": {"unique_id": "u"}, "two": {"unique_id": "v"}})
    assert run(payment.find_order_by_unique_id("v")) == "two"
    assert run(payment.find_order_by_unique_id("missing")) is None

    cancelled = Mock()
    cancelled.cancelled.return_value = True
    payment._monitoring_done(cancelled)
    cancelled.exception.assert_not_called()

    failed = Mock()
    failed.cancelled.return_value = False
    failed.exception.return_value = RuntimeError("boom")
    payment._monitoring_done(failed)
    failed.exception.assert_called_once_with()
