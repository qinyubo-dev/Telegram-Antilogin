# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telethon.errors import FloodWaitError

from accounts.login_code_rate_limiter import (
    LoginCodeRateLimitResult,
    LoginCodeRequestRateLimiter,
    render_login_code_rate_limit,
)
from storage import data_manager as dm
from storage.data_manager import DataManager


@pytest.fixture
def rate_storage(tmp_path, monkeypatch):
    previous_data = dm.user_data
    previous_loaded = dm.data_load_succeeded
    monkeypatch.setattr(dm, "DATA_FILE", str(tmp_path / "users.json"))
    dm.user_data = DataManager._default_data()
    dm.user_data[1] = {"language": "zh"}
    dm.data_load_succeeded = True
    yield
    dm.user_data = previous_data
    dm.data_load_succeeded = previous_loaded


def test_interval_boundary_and_rejected_checks_do_not_consume(rate_storage):
    limiter = LoginCodeRequestRateLimiter()
    assert limiter.acquire(1, now=1_000).allowed

    rejected = limiter.check(1, now=1_014.1)
    assert not rejected.allowed
    assert rejected.reason == "interval"
    assert rejected.retry_after_seconds == 1
    assert DataManager.get_login_code_request_timestamps(1) == [1_000.0]

    assert limiter.check(1, now=1_015).allowed
    assert limiter.acquire(1, now=1_015).allowed
    assert DataManager.get_login_code_request_timestamps(1) == [1_000.0, 1_015.0]


def test_rolling_window_limit_and_expiry(rate_storage):
    limiter = LoginCodeRequestRateLimiter()
    for index in range(15):
        assert limiter.acquire(1, now=1_000 + index * 15).allowed

    rejected = limiter.acquire(1, now=1_225)
    assert not rejected.allowed
    assert rejected.reason == "window"
    assert rejected.retry_after_seconds == 3_375
    assert len(DataManager.get_login_code_request_timestamps(1)) == 15

    assert limiter.acquire(1, now=4_600).allowed
    refreshed = DataManager.get_login_code_request_timestamps(1)
    assert len(refreshed) == 15
    assert refreshed[0] == 1_015.0 and refreshed[-1] == 4_600.0


def test_history_is_shared_by_user_and_survives_new_limiter(rate_storage):
    first = LoginCodeRequestRateLimiter()
    second = LoginCodeRequestRateLimiter()
    assert first.acquire(1, now=2_000).allowed
    assert not second.check(1, now=2_001).allowed

    dm.user_data[2] = {"language": "en"}
    assert second.check(2, now=2_001).allowed


def test_admin_bypasses_without_persisting(rate_storage):
    limiter = LoginCodeRequestRateLimiter()
    with patch.object(DataManager, "is_admin", return_value=True), patch.object(
        DataManager, "set_login_code_request_timestamps"
    ) as save:
        for _ in range(20):
            assert limiter.acquire(1, now=3_000).allowed
    save.assert_not_called()


def test_storage_failure_is_fail_closed(rate_storage):
    limiter = LoginCodeRequestRateLimiter()
    with patch.object(
        DataManager, "set_login_code_request_timestamps", return_value=False
    ):
        result = limiter.acquire(1, now=4_000)
    assert not result.allowed
    assert result.reason == "storage"


def test_startup_prunes_expired_history(rate_storage):
    dm.user_data[1]["login_code_request_timestamps"] = [1.0, 4_999.0]
    limiter = LoginCodeRequestRateLimiter()
    assert limiter.prune_all(now=5_000)
    assert DataManager.get_login_code_request_timestamps(1) == [4_999.0]


def test_rate_messages_are_bilingual_and_include_rule(rate_storage):
    limiter = LoginCodeRequestRateLimiter()
    assert limiter.acquire(1, now=6_000).allowed
    result = limiter.check(1, now=6_001)
    zh = render_login_code_rate_limit(result, "zh")
    en = render_login_code_rate_limit(result, "en")
    assert "14 秒" in zh and "滚动 60 分钟 最多 15 次" in zh
    assert "14 seconds" in en and "15 requests per rolling 60 minutes" in en


@pytest.mark.parametrize(
    "error",
    [FloodWaitError(None, capture=30), ConnectionError("offline")],
)
def test_telegram_attempt_failures_consume_one_slot(rate_storage, error):
    from accounts.account_manager import AccountManager

    client = SimpleNamespace(
        connect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=False),
        send_code_request=AsyncMock(side_effect=error),
        session=SimpleNamespace(filename="pending.session"),
    )
    with patch(
        "accounts.login_code_rate_limiter.time.time", return_value=7_000
    ), patch(
        "accounts.account_manager.account_runtime.get_login_unlock_reminder_system",
        return_value=None,
    ), patch.object(
        AccountManager, "cleanup_incomplete_account", new=AsyncMock()
    ):
        asyncio.run(AccountManager.authenticate(client, "+8613800000003", 1))
        asyncio.run(AccountManager.authenticate(client, "+8613800000003", 1))
    assert client.send_code_request.await_count == 1
    assert DataManager.get_login_code_request_timestamps(1) == [7_000.0]


def test_phone_login_precheck_keeps_flow_and_skips_client(handler_bot, event_factory):
    from handlers.account_handlers import setup_account_handlers
    from handlers.handler_utils import clear_state, get_state, set_state

    user_id = 7_001
    prompt = SimpleNamespace(id=1)
    set_state(user_id, adding_account=True, phone_prompt_message=prompt)
    event = event_factory(sender_id=user_id, text="+8613800000001", file=None)
    asyncio.run(setup_account_handlers(handler_bot))
    limited = LoginCodeRateLimitResult(
        allowed=False, reason="interval", retry_after_seconds=10
    )
    edit = AsyncMock(return_value=prompt)
    try:
        with patch(
            "handlers.account_handlers.require_access",
            new=AsyncMock(return_value=True),
        ), patch(
            "handlers.account_handlers.AccountManager.check_existing_account_for_add",
            new=AsyncMock(return_value=SimpleNamespace(action="allow", message="")),
        ), patch(
            "handlers.account_handlers.login_code_request_rate_limiter.check",
            return_value=limited,
        ), patch(
            "handlers.account_handlers.edit_status_or_send", new=edit
        ), patch(
            "handlers.account_handlers.AccountManager.create_new_client",
            new=AsyncMock(),
        ) as create_client:
            asyncio.run(handler_bot.find("handle_account_messages")(event))
        create_client.assert_not_awaited()
        assert get_state(user_id).get("adding_account")
        assert "10 秒" in edit.await_args.args[3]
    finally:
        clear_state(user_id)


def test_manual_probe_precheck_deletes_phone_and_keeps_flow(
    handler_bot, event_factory
):
    from accounts import account_runtime
    from handlers.handler_utils import clear_state, get_state, set_state
    from handlers.login_unlock_handlers import setup_login_unlock_handlers

    class FakeSystem:
        async def reconcile_user(self, _user_id):
            return True

        def quota_status(self, _user_id, _phone=""):
            return {"used": 0, "limit": 3, "existing": False, "full": False}

    user_id = 7_002
    previous = account_runtime.get_login_unlock_reminder_system()
    account_runtime.set_login_unlock_reminder_system(FakeSystem())
    set_state(user_id, login_unlock_manual_phone=True)
    event = event_factory(sender_id=user_id, text="+8613800000002")
    asyncio.run(setup_login_unlock_handlers(handler_bot))
    limited = LoginCodeRateLimitResult(
        allowed=False, reason="window", retry_after_seconds=120
    )
    try:
        with patch(
            "handlers.login_unlock_handlers.require_access",
            new=AsyncMock(return_value=True),
        ), patch(
            "handlers.login_unlock_handlers.login_code_request_rate_limiter.check",
            return_value=limited,
        ), patch(
            "handlers.login_unlock_handlers.AccountManager.create_new_client",
            new=AsyncMock(),
        ) as create_client:
            asyncio.run(handler_bot.find("login_unlock_manual_phone")(event))
        create_client.assert_not_awaited()
        event.delete.assert_awaited_once()
        assert get_state(user_id).get("login_unlock_manual_phone")
        assert "120 秒" in event.respond.await_args.args[0]
    finally:
        clear_state(user_id)
        account_runtime.set_login_unlock_reminder_system(previous)


def test_phone_login_late_race_restores_retry_flow(handler_bot, event_factory):
    from handlers.account_handlers import setup_account_handlers
    from handlers.handler_utils import clear_state, get_state, set_state

    user_id = 7_003
    prompt = SimpleNamespace(id=3)
    set_state(user_id, adding_account=True, phone_prompt_message=prompt)
    event = event_factory(sender_id=user_id, text="+8613800000003", file=None)
    asyncio.run(setup_account_handlers(handler_bot))
    raced_text = render_login_code_rate_limit(
        LoginCodeRateLimitResult(
            allowed=False, reason="interval", retry_after_seconds=15
        ),
        "zh",
    )

    async def lose_race(_client, _phone, _user_id):
        clear_state(user_id)
        return raced_text

    try:
        with patch(
            "handlers.account_handlers.require_access",
            new=AsyncMock(return_value=True),
        ), patch(
            "handlers.account_handlers.AccountManager.check_existing_account_for_add",
            new=AsyncMock(return_value=SimpleNamespace(action="allow", message="")),
        ), patch(
            "handlers.account_handlers.login_code_request_rate_limiter.check",
            return_value=LoginCodeRateLimitResult(allowed=True),
        ), patch(
            "handlers.account_handlers.AccountManager.create_new_client",
            new=AsyncMock(return_value=object()),
        ), patch(
            "handlers.account_handlers.AccountManager.authenticate",
            new=AsyncMock(side_effect=lose_race),
        ), patch(
            "handlers.account_handlers.edit_status_or_send",
            new=AsyncMock(return_value=prompt),
        ):
            asyncio.run(handler_bot.find("handle_account_messages")(event))
        assert get_state(user_id).get("adding_account")
        assert get_state(user_id).get("phone_prompt_message") is prompt
    finally:
        clear_state(user_id)


def test_manual_probe_late_race_restores_retry_flow(handler_bot, event_factory):
    from accounts import account_runtime
    from handlers.handler_utils import clear_state, get_state, set_state
    from handlers.login_unlock_handlers import setup_login_unlock_handlers

    class FakeSystem:
        async def reconcile_user(self, _user_id):
            return True

        def quota_status(self, _user_id, _phone=""):
            return {"used": 0, "limit": 3, "existing": False, "full": False}

    user_id = 7_004
    previous = account_runtime.get_login_unlock_reminder_system()
    account_runtime.set_login_unlock_reminder_system(FakeSystem())
    set_state(user_id, login_unlock_manual_phone=True)
    status = SimpleNamespace(edit=AsyncMock())
    event = event_factory(
        sender_id=user_id,
        text="+8613800000004",
        respond=AsyncMock(return_value=status),
    )
    asyncio.run(setup_login_unlock_handlers(handler_bot))
    raced_text = render_login_code_rate_limit(
        LoginCodeRateLimitResult(
            allowed=False, reason="interval", retry_after_seconds=15
        ),
        "zh",
    )
    try:
        with patch(
            "handlers.login_unlock_handlers.require_access",
            new=AsyncMock(return_value=True),
        ), patch(
            "handlers.login_unlock_handlers.login_code_request_rate_limiter.check",
            return_value=LoginCodeRateLimitResult(allowed=True),
        ), patch(
            "handlers.login_unlock_handlers.AccountManager.create_new_client",
            new=AsyncMock(return_value=object()),
        ), patch(
            "handlers.login_unlock_handlers.AccountManager.probe_login_unlock",
            new=AsyncMock(return_value=raced_text),
        ):
            asyncio.run(handler_bot.find("login_unlock_manual_phone")(event))
        assert get_state(user_id).get("login_unlock_manual_phone")
        status.edit.assert_awaited_once()
    finally:
        clear_state(user_id)
        account_runtime.set_login_unlock_reminder_system(previous)
