# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from handlers import admin_handlers
from handlers.handler_utils import clear_state, get_state, set_state


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def _cleanup():
    clear_state(1001)
    admin_handlers._pending_admin_actions.clear()
    yield
    clear_state(1001)
    admin_handlers._pending_admin_actions.clear()


def catalog():
    return {
        "go": {"price": "1", "quota": 2},
        "plus": {"price": "2", "quota": 10, "addon_unit_price": "0.2", "min_addon": 5},
        "pro": {"price": "3", "quota": None},
    }


def periods():
    return {
        30: {"discount_percent": "0"},
        90: {"discount_percent": "5"},
        180: {"discount_percent": "10"},
        365: {"discount_percent": "15"},
    }


def test_config_value_parsing():
    cases = [("2", "integer", 2), ("1.20", "decimal", "1.2"), ("0", "discount", "0")]
    for value, kind, expected in cases:
        assert admin_handlers._parse_config_value(value, kind) == expected


def test_config_value_rejects_invalid():
    cases = [
        ("x", "integer"),
        ("0", "integer"),
        ("nan", "decimal"),
        ("0", "decimal"),
        ("100", "discount"),
        ("-1", "discount"),
    ]
    for value, kind in cases:
        with pytest.raises(ValueError):
            admin_handlers._parse_config_value(value, kind)


def test_config_flow_helpers():
    for target in ["go", "plus", "pro", "discounts"]:
        before = periods() if target == "discounts" else catalog()
        field = admin_handlers._SUBSCRIPTION_CONFIG_FIELDS[target][0][0]
        flow = {
            "target": target,
            "before": before,
            "values": {field: "8"},
            "index": 0,
        }
        assert admin_handlers._config_flow_prompt(flow)
        assert admin_handlers._config_flow_prompt(flow, "bad").startswith("❌")
        assert "修改前" in admin_handlers._config_flow_preview(flow)
        if target != "discounts":
            assert admin_handlers._config_plan_text(catalog(), target)

    english_flow = {
        "target": "go", "before": catalog(), "values": {"price": "2"}, "index": 0,
    }
    assert "Edit GO" in admin_handlers._config_flow_prompt(english_flow, language="en")
    assert "Before" in admin_handlers._config_flow_preview(english_flow, language="en")


def test_display_name_cache_entity_and_failure():
    bot = SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace(first_name="A", last_name="B", username="ab")))
    with patch("handlers.admin_handlers.UserProfileCache.get", return_value=(True, "cached")):
        assert run(admin_handlers._get_user_display_name(bot, 1)) == "cached"
    with patch("handlers.admin_handlers.UserProfileCache.get", return_value=(False, None)), patch(
        "handlers.admin_handlers.UserProfileCache.set_profile"
    ) as save:
        assert run(admin_handlers._get_user_display_name(bot, 1)) == "A B"
    save.assert_called_once()
    bot.get_entity.side_effect = RuntimeError("missing")
    with patch("handlers.admin_handlers.UserProfileCache.get", return_value=(False, None)):
        assert run(admin_handlers._get_user_display_name(bot, 1)) is None


def test_admin_action_queue_replace_expire_and_buttons():
    with patch("handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"), patch(
        "handlers.admin_handlers._audit_result"
    ) as audit, patch("handlers.admin_handlers.secrets.token_hex", side_effect=["one", "two"]), patch(
        "handlers.admin_handlers.time.time", return_value=100
    ):
        first = admin_handlers._queue_admin_action(1, "subscription.grant", 2, {"x": 1}, None)
        second = admin_handlers._queue_admin_action(1, "subscription.delete", 2, {}, {"plan": "go"})
    audit.assert_called_once()
    assert admin_handlers._confirmation_buttons(second)[0][0].data.endswith(b"two")
    assert "无订阅" in admin_handlers._grant_confirmation_text(
        2, {"plan_name": "GO", "quota": 2}, 30, None
    )
    assert admin_handlers._take_admin_action(1, "wrong")[1] == "missing"
    with patch("handlers.admin_handlers.time.time", return_value=1000), patch(
        "handlers.admin_handlers._audit_result"
    ):
        assert admin_handlers._take_admin_action(1, "two")[1] == "expired"


def test_order_status_and_timestamp():
    cases = [
        ({"legacy_read_only": True}, "遗留"),
        ({"processed": True}, "已完成"),
        ({"needs_manual_review": True}, "待处理"),
        ({"status": "pending"}, "待支付"),
        ({"status": "expired"}, "已过期"),
        ({"status": "cancelled"}, "已取消"),
        ({"status": "other"}, "other"),
    ]
    for order, text in cases:
        assert text in admin_handlers._order_status_text(order)
    assert admin_handlers._format_timestamp("bad") == "未知"
    assert admin_handlers._format_timestamp("bad", "en") == "Unknown"
    assert admin_handlers._order_status_text({"status": "pending"}, "en") == "⏳ Awaiting payment"
    assert admin_handlers._format_timestamp(0).startswith("1970")


def test_audit_display_is_localized_without_mutating_raw_entries():
    entries = [{
        "timestamp": "2026-01-01", "admin_id": 1, "action": "order.recheck",
        "target_type": "order", "target_id": "o1", "result": "failed",
        "before": {"status": "pending"}, "after": None, "metadata": {},
        "error": "fulfillment_failed",
    }]
    before = __import__("copy").deepcopy(entries)
    text = admin_handlers._format_audit_entries(entries, "en")
    assert "Audit details" in text
    assert "Check order again" in text
    assert "Payment was confirmed, but fulfillment failed." in text
    assert entries == before


def test_known_users_and_resumable_count():
    payment = SimpleNamespace(pending_orders={"a": {"user_id": "4"}, "bad": "broken"})
    with patch("handlers.admin_handlers.DataManager.get_all_user_ids", return_value=[1]), patch.object(
        admin_handlers.config, "ADMIN_IDS", [2], create=True
    ), patch.dict(admin_handlers.user_accounts, {3: {}}, clear=True), patch(
        "handlers.admin_handlers.UserProfileCache.iter_profiles", return_value=[(5, {})]
    ):
        assert admin_handlers._known_user_ids(payment) == {1, 2, 3, 4, 5}

    with patch("handlers.admin_handlers.AccountManager.check_access", return_value=False):
        assert admin_handlers._resumable_account_count(1, {}, {}) == 0
    subscription = {"quota": 2, "selected_accounts": ["1", "2"]}
    with patch("handlers.admin_handlers.AccountManager.check_access", return_value=True), patch(
        "handlers.admin_handlers.AccountManager.hosted_account_phones", return_value={"1", "2", "3"}
    ):
        assert admin_handlers._resumable_account_count(1, subscription, {"+1": {}}) == 1
        assert admin_handlers._resumable_account_count(1, {"quota": 1, "selection_required": True}, {}) == 0


def test_search_admin_users_ranks_and_entity_fallback():
    payment = SimpleNamespace(pending_orders={})
    profiles = [
        (1, {"username": "alice", "display_name": "Alice A"}),
        (2, {"username": "alicia", "display_name": "Alicia"}),
    ]
    bot = SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace(id=2, username="remote")))
    with patch("handlers.admin_handlers._known_user_ids", return_value={1, 2}), patch(
        "handlers.admin_handlers.UserProfileCache.iter_profiles", return_value=profiles
    ), patch.dict(admin_handlers.user_accounts, {1: {"+123": {}}}, clear=True):
        assert run(admin_handlers._search_admin_users(bot, payment, "1"))[0]["match_type"] == "user_id"
        assert run(admin_handlers._search_admin_users(bot, payment, "+123"))[0]["match_type"] == "phone"
        assert len(run(admin_handlers._search_admin_users(bot, payment, "ali"))) == 2


def test_handle_admin_user_search_success_empty_and_failure(event_factory):
    payment = SimpleNamespace()
    bot = SimpleNamespace()
    for result in ([{"user_id": 2, "match_type": "user_id", "display_name": "A", "username": None}], [], RuntimeError("search")):
        set_state(1001, admin_user_search=True)
        event = event_factory(text="2")
        search = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
        with patch("handlers.admin_handlers.DataManager.is_admin", return_value=True), patch(
            "handlers.admin_handlers._search_admin_users", new=search
        ), patch("handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"), patch(
            "handlers.admin_handlers._audit_result", return_value=True
        ):
            assert run(admin_handlers.handle_admin_message(event, bot, payment))
        event.respond.assert_awaited_once()


def test_handle_order_search_and_audit_filter(event_factory):
    payment = SimpleNamespace(
        list_admin_orders=Mock(return_value={"items": [{"order_id": "o", "status": "pending"}], "page": 0, "max_page": 0, "total": 1})
    )
    set_state(1001, admin_order_search=True)
    event = event_factory(text="o")
    with patch("handlers.admin_handlers.DataManager.is_admin", return_value=True), patch(
        "handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"
    ), patch("handlers.admin_handlers._audit_result", return_value=True):
        assert run(admin_handlers.handle_admin_message(event, payment_system=payment))
    payment.list_admin_orders.assert_called_once()

    set_state(1001, admin_audit_filter=True)
    event = event_factory(text="管理员：1 动作：重新查单 目标：2 ignored:x")
    with patch("handlers.admin_handlers.DataManager.is_admin", return_value=True), patch(
        "handlers.admin_handlers.AdminAuditLog.query",
        return_value={"items": [{"audit_id": "a", "result": "ok", "action": "test", "target_id": "2"}], "total": 1},
    ):
        assert run(admin_handlers.handle_admin_message(event))
    filters = get_state(1001)["admin_audit_filters"]
    assert filters["admin_id"] == "1"
    assert filters["action"] == "order.recheck"
    assert filters["target_id"] == "2"


def test_parse_audit_filters_accepts_localized_actions_and_legacy_codes():
    assert admin_handlers._parse_audit_filters("重新查单", "zh")["action"] == "order.recheck"
    assert admin_handlers._parse_audit_filters(
        "action:Retry fulfillment admin:7", "en"
    ) == {
        "exclude_attempt": True,
        "action": "order.retry_fulfillment",
        "admin_id": "7",
    }
    assert admin_handlers._parse_audit_filters(
        "admin:7 action:order.detail target:o-1", "zh"
    ) == {
        "exclude_attempt": True,
        "admin_id": "7",
        "action": "order.detail",
        "target_id": "o-1",
    }


def test_handle_config_flow_expired_invalid_and_preview(event_factory):
    base = {
        "target": "go",
        "before": catalog(),
        "values": {},
        "index": 0,
        "stage": "input",
        "started_at": 100,
    }
    for state, text in ((dict(base, started_at=0), "1"), (base, "bad")):
        set_state(1001, admin_subscription_config_flow=state)
        event = event_factory(text=text)
        with patch("handlers.admin_handlers.DataManager.is_admin", return_value=True), patch(
            "handlers.admin_handlers.time.time", return_value=1000 if state["started_at"] == 0 else 100
        ):
            assert run(admin_handlers.handle_admin_message(event))
        event.respond.assert_awaited_once()

    flow = dict(base, index=1, values={"price": "2"})
    set_state(1001, admin_subscription_config_flow=flow)
    event = event_factory(text="3")
    with patch("handlers.admin_handlers.DataManager.is_admin", return_value=True), patch(
        "handlers.admin_handlers.time.time", return_value=100
    ):
        assert run(admin_handlers.handle_admin_message(event))
    assert get_state(1001)["admin_subscription_config_flow"]["stage"] == "preview"


def test_handle_subscription_grant_input(event_factory):
    for text in ("bad input extra", "30"):
        set_state(1001, admin_user_subscription_input=True, target_user_id=2, plan_id="go")
        event = event_factory(text=text)
        with patch(
            "handlers.admin_handlers.DataManager.quote_subscription",
            return_value={"plan_id": "go", "plan_name": "GO", "quota": 2},
        ), patch("handlers.admin_handlers.DataManager.get_subscription", return_value=None), patch(
            "handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"
        ):
            assert run(admin_handlers.handle_admin_message(event))
        event.respond.assert_awaited_once()
    assert admin_handlers._pending_admin_actions[1001]["action"] == "subscription.grant"
