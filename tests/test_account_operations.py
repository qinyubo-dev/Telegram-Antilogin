# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    FreshResetAuthorisationForbiddenError,
    SessionPasswordNeededError,
    SessionRevokedError,
)

from accounts import account_manager as module
from accounts.account_manager import AccountManager, user_accounts


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def _cleanup():
    user_accounts.clear()
    module.code_waiters.clear()
    module.hosting_action_cooldowns.clear()
    module.session_locks.clear()
    module.account_operation_locks.clear()
    for mapping in (module.code_fetch_tasks, module.pause_tasks):
        for task in list(mapping.values()):
            task.cancel()
        mapping.clear()
    yield
    user_accounts.clear()
    module.code_waiters.clear()
    module.hosting_action_cooldowns.clear()
    module.session_locks.clear()
    module.account_operation_locks.clear()


def account(client=True, **values):
    result = {
        "client": SimpleNamespace() if client else None,
        "runtime_status": "online",
        "display_phone": "+123",
        "session_file": "1_123.session",
        "anti_login": True,
    }
    result.update(values)
    return result


@pytest.mark.parametrize("case", ["absent", "healthy", "fatal", "busy", "restored", "confirm_fatal", "offline", "no_client"])
def test_existing_account_checks(case):
    if case == "absent":
        accounts = {}
    else:
        accounts = {"+123": account(client=case != "no_client", original_session_path="x.session")}
    health = {
        "healthy": {"ok": True, "status": "alive", "reason": "ok"},
        "fatal": {"ok": False, "status": "revoked", "reason": "bad"},
    }.get(case, {"ok": False, "status": "network", "reason": "down"})
    restored = case == "restored"
    restore_reason = "revoked" if case == "confirm_fatal" else "network"
    with patch.object(AccountManager, "get_user_accounts", return_value=accounts), patch.object(
        AccountManager, "validate_client_session", new=AsyncMock(return_value=health)
    ), patch.object(AccountManager, "cleanup_invalid_hosted_session", new=AsyncMock()), patch.object(
        AccountManager, "_cancel_client_task", new=AsyncMock()
    ), patch.object(
        AccountManager, "_safe_disconnect_client", new=AsyncMock(return_value=case != "busy")
    ), patch.object(AccountManager, "_start_connection_watcher_task"), patch.object(
        AccountManager,
        "create_client_from_session",
        new=AsyncMock(return_value=(None, "+123", restored, "" if restored else restore_reason)),
    ), patch.object(AccountManager, "mark_hosted_session_offline", new=AsyncMock()):
        result = run(AccountManager.check_existing_account_for_add(1, "+123"))
    if case in {"absent", "fatal", "confirm_fatal"}:
        assert result.action == "allow"
    else:
        assert result.action == "block"


def test_operability_guards_and_messages():
    with patch.object(AccountManager, "check_access", return_value=False):
        assert not AccountManager.ensure_account_operable(1, "+1")[0]
    with patch.object(AccountManager, "check_access", return_value=True), patch.object(
        AccountManager, "get_user_accounts", return_value={}
    ):
        assert "不存在" in AccountManager.ensure_account_operable(1, "+1")[4]
    offline = account(runtime_status="offline", offline_reason="network")
    with patch.object(AccountManager, "check_access", return_value=True), patch.object(
        AccountManager, "get_user_accounts", return_value={"+1": offline}
    ):
        assert "离线" in AccountManager.ensure_account_operable(1, "+1")[4]
        assert AccountManager.ensure_account_operable(1, "+1", allow_delete=True)[0]
    assert "失效" in AccountManager._hosted_operation_invalid_message("+1")
    assert "network" in AccountManager._hosted_operation_offline_message("+1", "network")


@pytest.mark.parametrize("case", ["guard", "missing_client", "healthy", "fatal", "transient"])
def test_ensure_hosted_client_ready(case):
    info = account(client=case != "missing_client")
    operable = case != "guard"
    health = {
        "ok": case == "healthy",
        "status": "revoked" if case == "fatal" else "network",
        "reason": "reason",
    }
    with patch.object(
        AccountManager,
        "ensure_account_operable",
        return_value=(operable, {"+123": info}, "+123", info, "guarded"),
    ), patch.object(AccountManager, "mark_hosted_session_offline", new=AsyncMock()), patch.object(
        AccountManager, "validate_client_session", new=AsyncMock(return_value=health)
    ), patch.object(AccountManager, "cleanup_invalid_hosted_session", new=AsyncMock()):
        result = run(AccountManager.ensure_hosted_client_ready(1, "+123", "kick"))
    assert result[0] is (case == "healthy")


@pytest.mark.parametrize(
    "error",
    [
        AuthKeyUnregisteredError(None),
        SessionRevokedError(None),
        SessionPasswordNeededError(None),
        FreshResetAuthorisationForbiddenError(None),
        FloodWaitError(None, capture=8),
        asyncio.TimeoutError(),
        ConnectionError("down"),
        RuntimeError("boom"),
    ],
)
def test_hosted_operation_error_mapping(error):
    with patch.object(AccountManager, "cleanup_invalid_hosted_session", new=AsyncMock()), patch.object(
        AccountManager, "_set_hosting_cooldown"
    ):
        assert run(AccountManager.handle_hosted_operation_error(1, "+1", object(), "kick", error))


def test_code_fetch_start_stop_and_guards():
    with patch.object(AccountManager, "check_access", return_value=False):
        assert "无权限" in AccountManager._start_code_fetch_unlocked(1, "+1")
    with patch.object(AccountManager, "check_access", return_value=True), patch.object(
        AccountManager, "get_user_accounts", return_value={}
    ):
        assert "不存在" in AccountManager._start_code_fetch_unlocked(1, "+1")

    async def scenario():
        user_accounts[1] = {"+1": account(temporary_mode="pause", temporary_until=9999999999)}
        with patch.object(AccountManager, "check_access", return_value=True):
            result = await AccountManager.start_code_fetch(1, "+1")
            assert result.startswith("✅")
            assert user_accounts[1]["+1"]["temporary_mode"] == "code_fetch"
            result = await AccountManager.stop_code_fetch(1, "+1")
            assert result.startswith("✅")
            assert user_accounts[1]["+1"]["temporary_mode"] == "pause"

    run(scenario())


def test_cooldown_extends_and_status_helpers():
    with patch.object(module.time, "time", return_value=100):
        assert AccountManager._check_hosting_cooldown(1, "+1", "x", 10) is None
        assert "10" in AccountManager._check_hosting_cooldown(1, "+1", "x", 10)
        AccountManager._set_hosting_cooldown(1, "+1", "x", 20)
    assert module.hosting_action_cooldowns["x_1_+1"] == 120

    assert AccountManager.get_antilogin_status_text({}, 1)
    assert AccountManager.get_antilogin_status_icon({}) == "⚠️"
    for mode, icon in (("paused", "⏸️"), ("code_fetch", "🔵"), ("normal", "🛡️")):
        info = account()
        with patch.object(AccountManager, "get_account_mode", return_value=mode):
            assert AccountManager.get_antilogin_status_icon(info) == icon
            assert AccountManager.get_antilogin_status_text(info, 1)


def test_pause_resume_guards_and_success():
    for access, accounts, fragment in ((False, {}, ""), (True, {}, "")):
        with patch.object(AccountManager, "check_access", return_value=access), patch.object(
            AccountManager, "get_user_accounts", return_value=accounts
        ):
            assert run(AccountManager._pause_anti_login_unlocked(1, "+1"))
            assert AccountManager._resume_anti_login_unlocked(1, "+1")

    async def scenario():
        user_accounts[1] = {"+1": account()}
        with patch.object(AccountManager, "check_access", return_value=True):
            assert await AccountManager.pause_anti_login(1, "+1", minutes=30)
            assert user_accounts[1]["+1"]["temporary_mode"] == "pause"
            assert await AccountManager.resume_anti_login(1, "+1")
            assert "temporary_mode" not in user_accounts[1]["+1"]

    run(scenario())


def test_delete_account_guards_and_success(tmp_path):
    with patch.object(AccountManager, "check_access", return_value=False):
        assert "无权限" in run(AccountManager._delete_account_unlocked(1, "+1"))
    with patch.object(AccountManager, "check_access", return_value=True), patch.object(
        AccountManager, "get_user_accounts", return_value={}
    ):
        assert "不存在" in run(AccountManager._delete_account_unlocked(1, "+1"))

    client = SimpleNamespace(
        is_connected=lambda: False,
        connect=AsyncMock(side_effect=RuntimeError("down")),
        log_out=AsyncMock(side_effect=RuntimeError("logout")),
    )
    user_accounts[1] = {"+1": account(client=False)}
    user_accounts[1]["+1"]["client"] = client
    with patch.object(AccountManager, "check_access", return_value=True), patch.object(
        AccountManager, "_cancel_client_task", new=AsyncMock()
    ), patch.object(AccountManager, "_cancel_account_auxiliary_tasks", new=AsyncMock()), patch.object(
        AccountManager, "remove_hosted_account_metadata"
    ), patch.object(module, "SESSIONS_DIR", str(tmp_path)):
        assert run(AccountManager.delete_account(1, "+1")).startswith("🗑")
    assert "+1" not in user_accounts[1]


@pytest.mark.parametrize("case", ["denied", "missing", "cooldown", "not_ready", "success", "error"])
def test_kick_other_sessions(case):
    client = AsyncMock()
    accounts = {"+1": account()}
    ready = (True, accounts, "+1", accounts["+1"], client, "")
    if case == "not_ready":
        ready = (False, accounts, "+1", accounts["+1"], None, "offline")
    with patch.object(AccountManager, "check_access", return_value=case != "denied"), patch.object(
        AccountManager, "get_user_accounts", return_value={} if case == "missing" else accounts
    ), patch.object(
        AccountManager, "_check_hosting_cooldown", return_value="wait" if case == "cooldown" else None
    ), patch.object(
        AccountManager, "ensure_hosted_client_ready", new=AsyncMock(return_value=ready)
    ), patch.object(
        AccountManager, "handle_hosted_operation_error", new=AsyncMock(return_value="mapped")
    ):
        if case == "error":
            client.side_effect = RuntimeError("rpc")
        result = run(AccountManager._kick_other_sessions_unlocked(1, "+1"))
    assert result
