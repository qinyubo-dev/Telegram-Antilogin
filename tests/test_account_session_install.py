# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telethon.errors import SessionRevokedError

from accounts import account_manager as module
from accounts.account_manager import AccountManager, user_accounts
from accounts.models import SessionCleanupResult


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def _cleanup():
    user_accounts.clear()
    module.session_locks.clear()
    AccountManager._quota_locks.clear()
    yield
    user_accounts.clear()
    module.session_locks.clear()
    AccountManager._quota_locks.clear()


def fake_client():
    return SimpleNamespace(is_connected=lambda: True, session=SimpleNamespace(filename="x.session"))


@pytest.mark.parametrize("case", ["denied", "bad_health", "missing_phone", "success", "invalid", "error"])
def test_inspect_uploaded_session(case):
    client = fake_client()
    health = {
        "ok": case in {"missing_phone", "success"},
        "status": "unauthorized",
        "me": SimpleNamespace(phone="123" if case == "success" else None),
    }
    constructor_error = RuntimeError("bad") if case in {"invalid", "error"} else None
    with patch.object(AccountManager, "check_access", return_value=case != "denied"), patch.object(
        module, "TelegramClient", side_effect=constructor_error, return_value=client
    ), patch.object(
        AccountManager, "validate_client_session", new=AsyncMock(return_value=health)
    ), patch.object(AccountManager, "_safe_disconnect_client", new=AsyncMock()), patch.object(
        AccountManager, "_is_uploaded_session_format_error", return_value=case == "invalid"
    ):
        phone, reason = run(AccountManager.inspect_uploaded_session("x.session", 1))
    if case == "success":
        assert phone == "+123" and reason == "ok"
    else:
        assert phone is None


@pytest.mark.parametrize("case", ["inspect", "quota", "busy", "success", "failed"])
def test_install_uploaded_session_primary_paths(tmp_path, case):
    upload = tmp_path / "upload.session"
    upload.write_bytes(b"new")
    existing = tmp_path / "1_123.session"
    if case in {"busy", "success", "failed"}:
        existing.write_bytes(b"old")
        user_accounts[1] = {
            "+123": {
                "client": fake_client(),
                "anti_login": False,
                "original_session_path": str(existing),
            }
        }
    inspect = (None, "invalid") if case == "inspect" else ("+123", "ok")
    created = (object(), "+123", case == "success", "bad")
    with patch.object(module, "SESSIONS_DIR", str(tmp_path)), patch.object(
        AccountManager, "inspect_uploaded_session", new=AsyncMock(return_value=inspect)
    ), patch.object(AccountManager, "can_add_hosted_account", return_value=case != "quota"), patch.object(
        AccountManager, "_cancel_client_task", new=AsyncMock()
    ), patch.object(AccountManager, "_cancel_account_auxiliary_tasks", new=AsyncMock()), patch.object(
        AccountManager, "_safe_disconnect_client", new=AsyncMock(return_value=case != "busy")
    ), patch.object(AccountManager, "_start_connection_watcher_task"), patch.object(
        AccountManager, "create_client_from_session", new=AsyncMock(return_value=created)
    ):
        result = run(AccountManager.install_uploaded_session(str(upload), 1))
    assert result[2] is (case == "success")
    if case == "busy":
        assert result[3] == "existing_session_busy"


@pytest.mark.parametrize("case", ["denied", "temp_health", "busy", "target_health", "monitor", "selection", "success"])
def test_create_client_from_session_paths(tmp_path, case):
    source = tmp_path / "incoming.session"
    source.write_bytes(b"session")
    temp = fake_client()
    hosted = fake_client()
    health_results = [
        {"ok": case != "temp_health", "status": "invalid", "me": SimpleNamespace(phone="123")},
        {"ok": case != "target_health", "status": "revoked", "me": SimpleNamespace(phone="123")},
    ]
    with patch.object(module, "SESSIONS_DIR", str(tmp_path / "hosted")), patch.object(
        AccountManager, "check_access", return_value=case != "denied"
    ), patch.object(module, "TelegramClient", side_effect=[temp, hosted]), patch.object(
        AccountManager, "validate_client_session", new=AsyncMock(side_effect=health_results)
    ), patch.object(
        AccountManager, "_safe_disconnect_client", new=AsyncMock(return_value=case != "busy")
    ), patch.object(AccountManager, "get_hosted_account_created_at", return_value=1), patch.object(
        AccountManager, "get_hosted_account_source", return_value="unknown"
    ), patch.object(
        AccountManager, "setup_monitoring", new=AsyncMock(return_value=case != "monitor")
    ), patch.object(AccountManager, "ensure_account_selected", return_value=case != "selection"), patch.object(
        AccountManager, "_cancel_client_task", new=AsyncMock()
    ), patch.object(AccountManager, "_start_connection_watcher_task"), patch.object(
        AccountManager, "set_hosted_account_source"
    ):
        result = run(
            AccountManager.create_client_from_session(
                str(source), 1, detailed=True, account_source="upload"
            )
        )
    assert result[2] is (case == "success")
    if case == "success":
        assert user_accounts[1]["+123"]["source"] == "upload"


def test_create_client_freeze_and_exception_paths(tmp_path):
    source = tmp_path / "incoming.session"
    source.write_bytes(b"session")
    clients = [fake_client(), fake_client()]
    health = {"ok": True, "me": SimpleNamespace(phone="123")}
    with patch.object(module, "SESSIONS_DIR", str(tmp_path / "hosted")), patch.object(
        AccountManager, "check_access", return_value=True
    ), patch.object(module, "TelegramClient", side_effect=clients), patch.object(
        AccountManager, "validate_client_session", new=AsyncMock(return_value=health)
    ), patch.object(AccountManager, "_safe_disconnect_client", new=AsyncMock(return_value=True)), patch.object(
        AccountManager,
        "check_account_freeze_status",
        new=AsyncMock(return_value={"ok": True, "status": "frozen", "freeze_info": {"x": 1}}),
    ), patch.object(AccountManager, "get_hosted_account_created_at", return_value=1), patch.object(
        AccountManager, "get_hosted_account_source", return_value="login"
    ), patch.object(
        AccountManager, "setup_monitoring", new=AsyncMock(return_value=True)
    ), patch.object(AccountManager, "ensure_account_selected", return_value=True), patch.object(
        AccountManager, "_cancel_client_task", new=AsyncMock()
    ), patch.object(AccountManager, "_start_connection_watcher_task"):
        result = run(AccountManager.create_client_from_session(str(source), 1, detailed=True, check_freeze=True))
    assert result[2] and result[0]._last_health_status == "frozen"

    source = tmp_path / "bad.session"
    source.write_bytes(b"bad")
    with patch.object(AccountManager, "check_access", return_value=True), patch.object(
        module, "TelegramClient", side_effect=SessionRevokedError(None)
    ), patch.object(AccountManager, "backup_session_file", new=AsyncMock()) as backup:
        result = run(AccountManager.create_client_from_session(str(source), 1, detailed=True))
    assert result[3] == "revoked"
    backup.assert_awaited_once()

    source = tmp_path / "invalid.session"
    source.write_bytes(b"bad")
    with patch.object(AccountManager, "check_access", return_value=True), patch.object(
        module, "TelegramClient", side_effect=RuntimeError("sqlite")
    ), patch.object(AccountManager, "_is_uploaded_session_format_error", return_value=True):
        result = run(AccountManager.create_client_from_session(str(source), 1, detailed=True))
    assert result[3] == "invalid"


@pytest.mark.parametrize("case", ["quota", "busy", "health", "monitor", "selection", "success"])
def test_promote_pending_client_paths(tmp_path, case):
    pending_path = tmp_path / "pending.session"
    pending_path.write_bytes(b"session")
    pending = fake_client()
    hosted = fake_client()
    with patch.object(module, "SESSIONS_DIR", str(tmp_path / "hosted")), patch.object(
        AccountManager, "can_add_hosted_account", return_value=case != "quota"
    ), patch.object(AccountManager, "quota_error_message", return_value="quota"), patch.object(
        AccountManager, "_cancel_client_task", new=AsyncMock()
    ), patch.object(
        AccountManager, "_safe_disconnect_client", new=AsyncMock(return_value=case != "busy")
    ), patch.object(module, "TelegramClient", return_value=hosted), patch.object(
        AccountManager,
        "validate_client_session",
        new=AsyncMock(return_value={"ok": case != "health", "status": "bad"}),
    ), patch.object(AccountManager, "get_hosted_account_created_at", return_value=1), patch.object(
        AccountManager, "setup_monitoring", new=AsyncMock(return_value=case != "monitor")
    ), patch.object(AccountManager, "ensure_account_selected", return_value=case != "selection"), patch.object(
        AccountManager, "_start_connection_watcher_task"
    ), patch.object(AccountManager, "set_hosted_account_source"):
        if case == "quota":
            with pytest.raises(PermissionError):
                run(AccountManager.promote_pending_client(pending, "+123", 1, pending_session_path=str(pending_path)))
        elif case in {"busy", "health", "monitor", "selection"}:
            with pytest.raises(RuntimeError):
                run(AccountManager.promote_pending_client(pending, "+123", 1, pending_session_path=str(pending_path)))
        else:
            result = run(AccountManager.promote_pending_client(pending, "+123", 1, pending_session_path=str(pending_path)))
            assert result is hosted


def test_backup_and_new_client_constructors(tmp_path):
    session = tmp_path / "x.session"
    session.write_bytes(b"data")
    with patch.object(module, "SESSIONS_DIR", str(tmp_path)), patch.object(
        AccountManager, "should_backup_session", return_value=False
    ):
        assert not run(AccountManager.backup_session_file(str(session), "network"))
    with patch.object(module, "SESSIONS_DIR", str(tmp_path)), patch.object(
        AccountManager, "should_backup_session", return_value=True
    ):
        assert run(AccountManager.backup_session_file(str(session), "revoked"))
    assert not session.exists()

    client = fake_client()
    with patch.object(module, "PENDING_SESSIONS_DIR", str(tmp_path)), patch.object(
        AccountManager, "check_access", return_value=False
    ):
        with pytest.raises(PermissionError):
            run(AccountManager.create_new_client("+1", 1))
        with pytest.raises(PermissionError):
            run(AccountManager.create_qr_client(1))

    stale = tmp_path / "1_1.session"
    stale.write_bytes(b"x")
    with patch.object(module, "PENDING_SESSIONS_DIR", str(tmp_path)), patch.object(
        AccountManager, "check_access", return_value=True
    ), patch.object(module, "TelegramClient", return_value=client), patch.object(
        AccountManager,
        "_remove_session_files_checked",
        return_value=SessionCleanupResult(True, "remove", "removed"),
    ):
        assert run(AccountManager.create_new_client("+1", 1)) is client
        assert client._pending_display_phone
    qr = tmp_path / "1_qr.session"
    qr.write_bytes(b"x")
    with patch.object(module, "PENDING_SESSIONS_DIR", str(tmp_path)), patch.object(
        AccountManager, "check_access", return_value=True
    ), patch.object(module, "TelegramClient", return_value=client), patch.object(
        AccountManager,
        "_remove_session_files_checked",
        return_value=SessionCleanupResult(False, "remove", "failed"),
    ):
        with pytest.raises(RuntimeError):
            run(AccountManager.create_qr_client(1))
