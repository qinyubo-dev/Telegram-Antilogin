# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from accounts import account_manager as module
from accounts.account_manager import AccountManager, user_accounts, user_states
from accounts.models import SessionCleanupResult


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def _cleanup():
    user_accounts.clear()
    user_states.clear()
    module.session_locks.clear()
    module.client_tasks.clear()
    yield
    user_accounts.clear()
    user_states.clear()
    module.session_locks.clear()
    module.client_tasks.clear()


def test_pending_path_and_active_paths(tmp_path):
    pending = tmp_path / "pending"
    pending.mkdir()
    inside = pending / "x.session"
    outside = tmp_path / "x.session"
    with patch.object(module, "PENDING_SESSIONS_DIR", str(pending)):
        assert AccountManager._is_pending_session_path(str(inside))
        assert not AccountManager._is_pending_session_path(str(outside))
        assert not AccountManager._is_pending_session_path("")
        user_states[1] = {"pending_session_path": str(inside), "auth_client": None}
        assert os.path.abspath(inside) in AccountManager._active_pending_session_paths()


def test_stale_pending_cleanup_results(tmp_path):
    missing = tmp_path / "missing"
    with patch.object(module, "PENDING_SESSIONS_DIR", str(missing)):
        assert AccountManager.cleanup_stale_pending_sessions().reason == "missing_dir"

    old = tmp_path / "old.session"
    old.write_bytes(b"x")
    young = tmp_path / "young.session"
    young.write_bytes(b"x")
    os.utime(old, (1, 1))
    with patch.object(module, "PENDING_SESSIONS_DIR", str(tmp_path)), patch.object(
        module.time, "time", return_value=10_000
    ), patch.object(AccountManager, "_active_pending_session_paths", return_value={os.path.abspath(young)}), patch.object(
        AccountManager,
        "_remove_session_files_checked",
        return_value=SessionCleanupResult(True, "remove", "removed"),
    ):
        result = AccountManager.cleanup_stale_pending_sessions(100)
    assert result.reason == "removed:1"

    with patch.object(module, "PENDING_SESSIONS_DIR", str(tmp_path)), patch.object(
        module.time, "time", return_value=10_000
    ), patch.object(AccountManager, "_active_pending_session_paths", return_value=set()), patch.object(
        AccountManager,
        "_remove_session_files_checked",
        return_value=SessionCleanupResult(False, "remove", "failed"),
    ):
        result = AccountManager.cleanup_stale_pending_sessions(100)
    assert not result.ok


@pytest.mark.parametrize("case", ["none", "disconnect", "remove", "success", "hosted"])
def test_cleanup_pending_login_state(tmp_path, case):
    pending = tmp_path / "pending"
    pending.mkdir()
    path = pending / "x.session"
    path.write_bytes(b"x")
    client = object()
    if case != "none":
        user_states[1] = {"auth_client": client, "auth_phone": "+1", "pending_session_path": str(path)}
    disconnect_ok = case != "disconnect"
    remove_ok = case != "remove"
    with patch.object(module, "PENDING_SESSIONS_DIR", str(pending)), patch.object(
        AccountManager, "_disconnect_pending_client", new=AsyncMock(return_value=disconnect_ok)
    ), patch.object(AccountManager, "_safe_disconnect_client", new=AsyncMock(return_value=disconnect_ok)), patch.object(
        AccountManager,
        "_remove_session_files_checked",
        return_value=SessionCleanupResult(remove_ok, "remove", "removed" if remove_ok else "failed"),
    ):
        if case == "hosted":
            user_states[1]["pending_session_path"] = str(tmp_path / "hosted.session")
        result = run(AccountManager.cleanup_pending_login_state(1))
    assert result.ok is (case in {"none", "success", "hosted"})


@pytest.mark.parametrize("case", ["disconnect", "pending", "hosted", "session_file", "none"])
def test_cleanup_incomplete_account(tmp_path, case):
    pending = tmp_path / "pending"
    pending.mkdir()
    pending_path = pending / "x.session"
    pending_path.write_bytes(b"x")
    hosted_path = tmp_path / "hosted.session"
    hosted_path.write_bytes(b"x")
    client = object()
    session_path = str(pending_path if case in {"disconnect", "pending"} else hosted_path)
    user_states[1] = {"pending_session_path": session_path}
    user_accounts[1] = {"+1": {"client": client, "session_file": "hosted.session", "original_session_path": session_path}}
    if case == "session_file":
        user_accounts[1]["+1"].pop("original_session_path")
        user_states[1].clear()
    if case == "none":
        user_accounts[1]["+1"].pop("original_session_path")
        user_accounts[1]["+1"].pop("session_file")
        user_states[1].clear()
    with patch.object(module, "PENDING_SESSIONS_DIR", str(pending)), patch.object(
        AccountManager, "_cancel_client_task", new=AsyncMock()
    ), patch.object(
        AccountManager, "_disconnect_pending_client", new=AsyncMock(return_value=case != "disconnect")
    ), patch.object(AccountManager, "_safe_disconnect_client", new=AsyncMock(return_value=True)), patch.object(
        AccountManager,
        "_remove_session_files_checked",
        return_value=SessionCleanupResult(True, "remove", "removed"),
    ), patch.object(AccountManager, "remove_hosted_account_metadata"):
        result = run(AccountManager.cleanup_incomplete_account(1, "+1", client))
    assert result.ok is (case != "disconnect")


def test_cleanup_invalid_hosted_session_and_notification(tmp_path):
    path = tmp_path / "1_1.session"
    path.write_bytes(b"x")
    client = object()
    user_accounts[1] = {"+1": {"client": client, "original_session_path": str(path), "display_phone": "shown"}}
    with patch.object(AccountManager, "_cancel_account_auxiliary_tasks", new=AsyncMock()), patch.object(
        AccountManager, "_safe_disconnect_client", new=AsyncMock()
    ), patch.object(AccountManager, "backup_session_file", new=AsyncMock()) as backup, patch.object(
        AccountManager, "remove_hosted_account_metadata"
    ), patch.object(AccountManager, "notify_session_unavailable", new=AsyncMock()) as notify:
        run(AccountManager.cleanup_invalid_hosted_session(1, "+1", reason="revoked"))
    backup.assert_awaited_once()
    notify.assert_awaited_once_with(1, "shown", source="connection_watcher")
    assert 1 not in user_accounts


def test_mark_offline_stale_first_and_repeat():
    current = object()
    stale = object()
    user_accounts[1] = {"+1": {"client": current, "runtime_status": "online"}}
    with patch.object(AccountManager, "_safe_disconnect_client", new=AsyncMock()):
        assert not run(AccountManager.mark_hosted_session_offline(1, "+1", stale))
        assert run(AccountManager.mark_hosted_session_offline(1, "+1", current, "network"))
        assert not run(AccountManager.mark_hosted_session_offline(1, "+1", current, "network"))
    assert user_accounts[1]["+1"]["offline_reason"] == "network"


def test_offline_notification_no_bot_success_and_failure():
    with patch("accounts.account_manager.account_runtime.get_notify_bot", return_value=None):
        run(AccountManager.notify_hosted_session_offline(1, "+1"))
    bot = object()
    with patch("accounts.account_manager.account_runtime.get_notify_bot", return_value=bot), patch.object(
        AccountManager, "_safe_send_bot_message", new=AsyncMock()
    ) as send:
        run(AccountManager.notify_hosted_session_offline(1, "+1"))
    send.assert_awaited_once()
    with patch("accounts.account_manager.account_runtime.get_notify_bot", return_value=bot), patch.object(
        AccountManager, "_safe_send_bot_message", new=AsyncMock(side_effect=RuntimeError("send"))
    ):
        run(AccountManager.notify_hosted_session_offline(1, "+1"))
