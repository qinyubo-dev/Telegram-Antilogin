# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from accounts import account_manager as module
from accounts.account_manager import AccountManager, user_accounts


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def _cleanup():
    user_accounts.clear()
    module.code_waiters.clear()
    AccountManager._login_message_locks.clear()
    module.account_operation_locks.clear()
    yield
    user_accounts.clear()
    module.code_waiters.clear()
    AccountManager._login_message_locks.clear()
    module.account_operation_locks.clear()


@pytest.mark.parametrize("case", ["stale", "duplicate", "no_code", "inactive", "no_bot_success", "no_bot_failed", "bot_success", "bot_failed"])
def test_process_login_message_outcomes(case):
    client = object()
    info = {"client": client, "runtime_status": "online", "anti_login": True}
    user_accounts[1] = {"+1": info}
    if case == "stale":
        info["client"] = object()
    codes = [] if case == "no_code" else ["12345"]
    invalidated = case not in {"no_bot_failed", "bot_failed"}
    bot = None if case.startswith("no_bot") or case in {"stale", "duplicate", "no_code", "inactive"} else object()
    message = SimpleNamespace(id=10, text="code", media=None)
    module.code_waiters["+1"] = {1}
    with patch.object(AccountManager, "get_user_accounts", return_value=user_accounts[1]), patch.object(
        AccountManager, "_is_login_message_processed", return_value=case == "duplicate"
    ), patch("accounts.account_manager.extract_sign_in_codes", return_value=codes), patch.object(
        AccountManager, "is_anti_login_active", return_value=case != "inactive"
    ), patch.object(
        AccountManager, "_invalidate_sign_in_codes", new=AsyncMock(return_value=invalidated)
    ), patch("accounts.account_manager.account_runtime.get_notify_bot", return_value=bot), patch.object(
        AccountManager, "_safe_send_bot_message", new=AsyncMock(return_value=True)
    ), patch.object(AccountManager, "_mark_login_message_processed") as mark:
        result = run(AccountManager._process_login_message(client, message, "+1", 1))
    expected = {
        "stale": "stale", "duplicate": "duplicate", "no_code": "skipped", "inactive": "skipped",
        "no_bot_success": "success", "no_bot_failed": "failed", "bot_success": "success", "bot_failed": "failed",
    }[case]
    assert result == expected
    if case not in {"stale", "duplicate"}:
        mark.assert_called_once()


class MessageClient:
    def __init__(self, messages=None, error=None):
        self.messages = messages or []
        self.error = error

    def iter_messages(self, *_args, **_kwargs):
        async def generate():
            if self.error:
                raise self.error
            for message in self.messages:
                yield message
        return generate()


def test_backfill_messages_recent_old_missing_and_error():
    now = datetime.now(timezone.utc)
    messages = [
        SimpleNamespace(id=1, date=None),
        SimpleNamespace(id=2, date=(now - timedelta(seconds=1)).replace(tzinfo=None)),
        SimpleNamespace(id=3, date=now - timedelta(days=1)),
    ]
    with patch.object(AccountManager, "_process_login_message", new=AsyncMock(return_value="success")):
        assert run(AccountManager._backfill_login_messages(MessageClient(messages), "+1", 1, "start")) == 1
    assert run(AccountManager._backfill_login_messages(MessageClient(error=RuntimeError("rpc")), "+1", 1, "start")) == 0


def test_reconnect_backfill_install_and_callback():
    assert not AccountManager._install_reconnect_backfill(SimpleNamespace(_sender=None), "+1", 1)
    original = AsyncMock()
    sender = SimpleNamespace(_auto_reconnect_callback=original)
    client = SimpleNamespace(_sender=sender)
    with patch.object(AccountManager, "_backfill_login_messages", new=AsyncMock()) as backfill:
        assert AccountManager._install_reconnect_backfill(client, "+1", 1)
        run(sender._auto_reconnect_callback())
        assert AccountManager._install_reconnect_backfill(client, "+1", 1)
    original.assert_awaited_once()
    backfill.assert_awaited_once()


def test_authorization_detail_helpers():
    aware = datetime.now(timezone.utc)
    update = SimpleNamespace(date=aware, device=" Phone ", location=" Earth ")
    details = AccountManager._authorization_update_details(update)
    assert details["device_name"] == "Phone"
    assert details["location"] == "Earth"
    fallback = AccountManager._authorization_update_details(SimpleNamespace())
    assert fallback["detected_at"]
