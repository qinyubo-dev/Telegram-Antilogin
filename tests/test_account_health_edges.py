# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    SessionPasswordNeededError,
    SessionRevokedError,
)

from accounts import account_manager as module
from accounts.account_manager import AccountManager


def run(awaitable):
    return asyncio.run(awaitable)


def health_client(**updates):
    values = {
        "is_connected": lambda: True,
        "connect": AsyncMock(),
        "is_user_authorized": AsyncMock(return_value=True),
        "get_me": AsyncMock(return_value=SimpleNamespace(id=1, username="u", phone="123")),
    }
    values.update(updates)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("case", ["connect", "unauthorized", "no_user", "alive"])
def test_validate_client_primary_states(case):
    client = health_client(
        is_connected=lambda: case != "connect",
        is_user_authorized=AsyncMock(return_value=case != "unauthorized"),
        get_me=AsyncMock(return_value=None if case == "no_user" else SimpleNamespace(id=1, username="u", phone="123")),
    )
    result = run(AccountManager.validate_client_session(client, "+1", retry_attempts=0))
    assert result["status"] == {"connect": "alive", "unauthorized": "unauthorized", "no_user": "no_user", "alive": "alive"}[case]
    if case == "connect":
        client.connect.assert_awaited_once()


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (asyncio.TimeoutError(), "timeout"),
        (SessionRevokedError(None), "revoked"),
        (AuthKeyUnregisteredError(None), "unauthorized"),
        (FloodWaitError(None, capture=4), "flood_wait"),
        (SessionPasswordNeededError(None), "2fa"),
        (RuntimeError("boom"), "error"),
    ],
)
def test_validate_client_error_states(error, status):
    client = health_client(is_user_authorized=AsyncMock(side_effect=error))
    result = run(AccountManager.validate_client_session(client, retry_attempts=0))
    assert result["status"] == status


def test_backup_name_and_available_path(tmp_path):
    name = AccountManager._session_backup_name("x.session", "bad reason")
    assert "bad-reason" in name
    first = tmp_path / "x.bak"
    first.write_bytes(b"")
    assert AccountManager._available_backup_path(str(tmp_path), "x.bak").endswith("x.1.bak")


@pytest.mark.parametrize("frozen", [False, True])
def test_freeze_status(frozen):
    values = []
    if frozen:
        values = [
            {"key": "freeze_since_date", "value": {"_": "JsonNumber", "value": 1}},
            {"key": "freeze_until_date", "value": {"_": "JsonNumber", "value": 2}},
            {"key": "ignored", "value": {"_": "JsonString", "value": "x"}},
        ]
    response = SimpleNamespace(to_json=lambda: json.dumps({"config": {"value": values}}))
    client = AsyncMock(return_value=response)
    result = run(AccountManager.check_account_freeze_status(client, "+1"))
    assert result["status"] == ("frozen" if frozen else "alive")
    client.side_effect = RuntimeError("rpc")
    assert not run(AccountManager.check_account_freeze_status(client))["ok"]


@pytest.mark.parametrize("case", ["health", "invalid", "error"])
def test_inaccessible_session_probe(case):
    client = health_client()
    constructor_error = RuntimeError("bad") if case in {"invalid", "error"} else None
    with patch.object(module, "TelegramClient", side_effect=constructor_error, return_value=client), patch.object(
        AccountManager, "validate_client_session", new=AsyncMock(return_value={"status": "revoked"})
    ), patch.object(AccountManager, "_is_uploaded_session_format_error", return_value=case == "invalid"), patch.object(
        AccountManager, "_safe_disconnect_client", new=AsyncMock()
    ):
        status = run(AccountManager.check_inaccessible_session_file("x.session"))
    assert status == {"health": "revoked", "invalid": "invalid", "error": "error"}[case]


def test_session_unavailable_notifications():
    with patch("accounts.account_manager.account_runtime.get_notify_bot", return_value=None):
        run(AccountManager.notify_session_unavailable(1, "+1"))
    bot = object()
    with patch("accounts.account_manager.account_runtime.get_notify_bot", return_value=bot), patch.object(
        AccountManager, "_safe_send_bot_message", new=AsyncMock(return_value=True)
    ) as send:
        run(AccountManager.notify_session_unavailable(1, "+1", source="startup"))
    send.assert_awaited_once()
    with patch("accounts.account_manager.account_runtime.get_notify_bot", return_value=bot), patch.object(
        AccountManager, "_safe_send_bot_message", new=AsyncMock(return_value=False)
    ):
        run(AccountManager.notify_session_unavailable(1, "+1"))


@pytest.mark.parametrize("case", ["plain", "buttons", "entity", "transient", "unknown"])
def test_safe_send_bot_message_paths(case):
    error = None
    if case == "entity":
        error = ValueError("Could not find the input entity")
    elif case == "transient":
        error = ConnectionError("down")
    elif case == "unknown":
        error = RuntimeError("boom")
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=error, return_value="message"))
    with patch("accounts.account_manager.account_runtime.mark_notify_bot_healthy"), patch(
        "accounts.account_manager.account_runtime.mark_notify_bot_degraded"
    ):
        result = run(
            AccountManager._safe_send_bot_message(
                bot, 1, "text", "test", buttons=[] if case == "buttons" else None,
                return_message=case == "plain",
            )
        )
    if error:
        assert result is False
    elif case == "plain":
        assert result == "message"
    else:
        assert result is True


def test_phone_formatting():
    assert AccountManager.normalize_phone("+1 (234) 567-8901") == "+12345678901"
    assert AccountManager.format_phone_display("+12345678901") == "+1 234 567 8901"
    assert AccountManager.format_phone_display("+86123") == "+86123"
