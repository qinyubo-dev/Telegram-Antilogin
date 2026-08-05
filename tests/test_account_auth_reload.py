# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from telethon.errors import (
    FloodWaitError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from accounts import account_manager as module
from accounts.account_manager import AccountManager, user_accounts, user_states
from accounts.models import ExistingAccountCheck
from storage.data_manager import DataManager


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def _cleanup():
    user_accounts.clear()
    user_states.clear()
    module.session_locks.clear()
    module.account_operation_locks.clear()
    yield
    user_accounts.clear()
    user_states.clear()
    module.session_locks.clear()
    module.account_operation_locks.clear()


def client_stub(**values):
    defaults = {
        "connect": AsyncMock(),
        "is_user_authorized": AsyncMock(return_value=False),
        "send_code_request": AsyncMock(return_value=SimpleNamespace(type="app")),
        "sign_in": AsyncMock(),
        "get_me": AsyncMock(return_value=SimpleNamespace(phone="123")),
        "session": SimpleNamespace(filename="pending.session"),
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_authenticate_authorized_code_and_failures():
    authorized = client_stub(is_user_authorized=AsyncMock(return_value=True))
    with patch.object(AccountManager, "promote_pending_client", new=AsyncMock()) as promote:
        assert run(AccountManager.authenticate(authorized, "+1", 1)).startswith("✅")
    promote.assert_awaited_once()

    pending = client_stub()
    assert run(AccountManager.authenticate(pending, "+2", 2)).startswith("📱")
    assert user_states[2]["waiting_code"]

    flood = FloodWaitError(None, capture=7)
    client = client_stub(send_code_request=AsyncMock(side_effect=flood))
    with patch.object(AccountManager, "cleanup_incomplete_account", new=AsyncMock()) as cleanup:
        assert "7" in run(AccountManager.authenticate(client, "+3", 3))
    cleanup.assert_awaited_once()

    client = client_stub(send_code_request=AsyncMock(side_effect=RuntimeError("send")))
    with patch.object(AccountManager, "cleanup_incomplete_account", new=AsyncMock()):
        assert "send" in run(AccountManager.authenticate(client, "+4", 4))

    client = client_stub(connect=AsyncMock(side_effect=RuntimeError("connect")))
    with patch.object(AccountManager, "cleanup_incomplete_account", new=AsyncMock()):
        assert "connect" in run(AccountManager.authenticate(client, "+5", 5))


def test_authentication_results_use_stable_english_messages():
    pending = client_stub()
    with patch.object(DataManager, "get_user_language", return_value="en"):
        result = run(AccountManager.authenticate(pending, "+2", 2))
    assert result.startswith("📱 Login code sent")
    assert "5 minutes" in result

    flood = FloodWaitError(None, capture=7)
    client = client_stub(send_code_request=AsyncMock(side_effect=flood))
    with patch.object(DataManager, "get_user_language", return_value="en"), patch.object(
        module.account_runtime, "get_login_unlock_reminder_system", return_value=None
    ), patch.object(AccountManager, "cleanup_incomplete_account", new=AsyncMock()):
        limited = run(AccountManager.authenticate(client, "+3", 3))
    assert "7 seconds" in limited
    assert "请求过于频繁" not in limited

    state = code_state(8)
    state["auth_client"].sign_in.side_effect = PhoneCodeInvalidError(None)
    with patch.object(DataManager, "get_user_language", return_value="en"):
        invalid = run(AccountManager.handle_code(8, "1234"))
    assert "invalid" in invalid.lower()
    assert "Attempts remaining: 4" in invalid


def code_state(user_id=1, **updates):
    state = {
        "waiting_code": True,
        "auth_client": client_stub(),
        "auth_phone": "+123",
        "display_phone": "+123",
        "pending_session_path": "pending.session",
        "code_attempts": 0,
        "max_code_attempts": 5,
    }
    state.update(updates)
    user_states[user_id] = state
    return state


def test_handle_code_guards_success_and_password():
    assert "过期" in run(AccountManager.handle_code(1, "1234"))
    code_state(code_attempts=5)
    with patch.object(AccountManager, "cleanup_incomplete_account", new=AsyncMock()) as cleanup:
        assert "次数过多" in run(AccountManager.handle_code(1, "1234"))
    cleanup.assert_awaited_once()

    code_state()
    assert "格式无效" in run(AccountManager.handle_code(1, "x"))

    code_state()
    with patch.object(AccountManager, "promote_pending_client", new=AsyncMock()):
        assert run(AccountManager.handle_code(1, "1234")).startswith("✅")
    assert 1 not in user_states

    state = code_state()
    state["auth_client"].sign_in.side_effect = SessionPasswordNeededError(None)
    assert "两步验证" in run(AccountManager.handle_code(1, "1234"))
    assert state["waiting_password"]

    code_state()
    with patch.object(
        AccountManager, "promote_pending_client", new=AsyncMock(side_effect=RuntimeError("promote"))
    ), patch.object(AccountManager, "cleanup_incomplete_account", new=AsyncMock()):
        assert "托管初始化失败" in run(AccountManager.handle_code(1, "1234"))


@pytest.mark.parametrize(
    ("error", "attempts", "fragment"),
    [
        (PhoneCodeInvalidError(None), 0, "验证码无效"),
        (PhoneCodeInvalidError(None), 4, "次数过多"),
        (PhoneCodeExpiredError(None), 0, "已过期"),
        (PhoneCodeEmptyError(None), 0, "不能为空"),
        (FloodWaitError(None, capture=6), 0, "6"),
        (RuntimeError("bad"), 0, "登录失败"),
        (RuntimeError("bad"), 4, "尝试次数已用完"),
    ],
)
def test_handle_code_error_variants(error, attempts, fragment):
    state = code_state(code_attempts=attempts)
    state["auth_client"].sign_in.side_effect = error
    with patch.object(AccountManager, "cleanup_incomplete_account", new=AsyncMock()):
        assert fragment in run(AccountManager.handle_code(1, "1234"))


def password_state(user_id=1, **updates):
    state = {
        "waiting_password": True,
        "auth_client": client_stub(),
        "auth_phone": "+123",
        "display_phone": "+123",
        "pending_session_path": "pending.session",
        "password_attempts": 0,
        "max_password_attempts": 5,
    }
    state.update(updates)
    user_states[user_id] = state
    return state


def test_handle_password_guards_success_and_qr():
    assert "过期" in run(AccountManager.handle_password(1, "pw"))
    password_state()
    assert "不能为空" in run(AccountManager.handle_password(1, "  "))

    password_state()
    with patch.object(AccountManager, "promote_pending_client", new=AsyncMock()):
        assert run(AccountManager.handle_password(1, "pw")).startswith("✅")

    state = password_state(auth_phone="", display_phone="", qr_login=True)
    with patch.object(
        AccountManager,
        "check_existing_account_for_add",
        new=AsyncMock(return_value=ExistingAccountCheck("allow", "+123", "ok")),
    ), patch.object(AccountManager, "promote_pending_client", new=AsyncMock()):
        assert run(AccountManager.handle_password(1, "pw")).startswith("✅")
    assert state["auth_phone"] == "+123"

    state = password_state(auth_phone="", display_phone="")
    state["auth_client"].get_me.return_value = SimpleNamespace(phone=None)
    with patch.object(AccountManager, "cleanup_incomplete_account", new=AsyncMock()):
        assert "无法读取" in run(AccountManager.handle_password(1, "pw"))

    state = password_state(qr_login=True)
    with patch.object(
        AccountManager,
        "check_existing_account_for_add",
        new=AsyncMock(return_value=ExistingAccountCheck("block", "+123", "duplicate")),
    ), patch.object(AccountManager, "cleanup_incomplete_account", new=AsyncMock()):
        assert run(AccountManager.handle_password(1, "pw")) == "duplicate"


def test_handle_password_errors_and_retry_limit():
    state = password_state()
    state["auth_client"].sign_in.side_effect = RuntimeError("bad password")
    assert "剩余尝试次数" in run(AccountManager.handle_password(1, "pw"))

    state = password_state(password_attempts=4)
    state["auth_client"].sign_in.side_effect = RuntimeError("bad password")
    with patch.object(AccountManager, "cleanup_incomplete_account", new=AsyncMock()):
        assert "次数过多" in run(AccountManager.handle_password(1, "pw"))

    password_state(password_verified=True)
    with patch.object(
        AccountManager, "promote_pending_client", new=AsyncMock(side_effect=RuntimeError("promote"))
    ), patch.object(AccountManager, "cleanup_incomplete_account", new=AsyncMock()):
        assert "托管初始化失败" in run(AccountManager.handle_password(1, "pw"))

    state = password_state(password_verified=True, auth_phone="")
    state["auth_client"].get_me.side_effect = RuntimeError("profile")
    assert "初始化失败" in run(AccountManager.handle_password(1, "pw"))


def test_load_all_sessions_branches(tmp_path):
    missing = tmp_path / "new"
    with patch.object(module, "SESSIONS_DIR", str(missing)):
        run(AccountManager.load_all_sessions())
    assert missing.exists()

    names = ["bad.session", "x_1.session", "1_11.session", "2_22.session", "3_33.session", "4_44.session"]
    for name in names:
        (tmp_path / name).write_bytes(b"")

    async def inaccessible(path, _phone):
        return "invalid" if "1_11" in path else "network"

    async def create(path, *_args, **_kwargs):
        if "3_33" in path:
            return None, "+33", False, "unauthorized"
        return object(), "+44", True, ""

    with patch.object(module, "SESSIONS_DIR", str(tmp_path)), patch.object(
        AccountManager, "check_access", side_effect=lambda uid: uid >= 3
    ), patch.object(
        AccountManager, "is_account_selected", side_effect=lambda uid, phone: uid != 2
    ), patch.object(
        AccountManager, "check_inaccessible_session_file", new=AsyncMock(side_effect=inaccessible)
    ), patch.object(AccountManager, "create_client_from_session", new=AsyncMock(side_effect=create)), patch.object(
        AccountManager, "backup_session_file", new=AsyncMock()
    ) as backup, patch.object(
        AccountManager, "notify_session_unavailable", new=AsyncMock()
    ) as notify:
        run(AccountManager.load_all_sessions())
    assert backup.await_count >= 2
    notify.assert_awaited_once()


def test_reload_accounts_mixed_results(tmp_path):
    for name in ("a.session", "b.session", "c.session", "d.session"):
        (tmp_path / name).write_bytes(b"")
    old = client_stub()
    user_accounts[1] = {
        "+1": {"session_file": "a.session", "client": old, "display_phone": "a", "anti_login": True},
        "+2": {"session_file": "b.session", "client": old, "display_phone": "b"},
        "+3": {"session_file": "c.session", "client": old, "display_phone": "c"},
        "+4": {"session_file": "d.session", "client": old, "display_phone": "d"},
        "+5": {"client": old, "display_phone": "e"},
    }

    async def create(path, *_args, **_kwargs):
        if path.endswith("a.session"):
            return SimpleNamespace(_last_health_status="alive", _last_freeze_info=None), "+1", True, ""
        if path.endswith("b.session"):
            return SimpleNamespace(_last_health_status="frozen", _last_freeze_info={}), "+2", True, ""
        if path.endswith("c.session"):
            return None, "+3", False, "invalid"
        return None, "+4", False, "network"

    with patch.object(module, "SESSIONS_DIR", str(tmp_path)), patch.object(
        AccountManager, "_cancel_client_task", new=AsyncMock()
    ), patch.object(AccountManager, "_safe_disconnect_client", new=AsyncMock()), patch.object(
        AccountManager, "create_client_from_session", new=AsyncMock(side_effect=create)
    ), patch.object(AccountManager, "notify_session_unavailable", new=AsyncMock()), patch.object(
        AccountManager, "backup_session_file", new=AsyncMock()
    ), patch.object(AccountManager, "setup_monitoring", new=AsyncMock()), patch.object(
        AccountManager, "_start_connection_watcher_task"
    ):
        result = run(AccountManager.reload_user_accounts_detail(1))
    assert result["success"] == 2
    assert result["failed"] == 3
    assert result["alive_count"] == 1
    assert result["frozen_count"] == 1
    assert result["dead_count"] == 3

    assert run(AccountManager.reload_user_accounts_detail(99)) == {
        "total": 0,
        "success": 0,
        "failed": 0,
    }

def test_reload_does_not_reopen_session_when_disconnect_fails(tmp_path):
    path = tmp_path / "busy.session"
    path.write_bytes(b"")
    old_client = client_stub(is_connected=Mock(return_value=True))
    user_accounts[1] = {
        "+1": {
            "session_file": path.name,
            "client": old_client,
            "display_phone": "+1",
            "anti_login": True,
        }
    }

    with patch.object(module, "SESSIONS_DIR", str(tmp_path)), patch.object(
        AccountManager, "_cancel_client_task", new=AsyncMock()
    ), patch.object(
        AccountManager, "_safe_disconnect_client", new=AsyncMock(return_value=False)
    ), patch.object(
        AccountManager, "create_client_from_session", new=AsyncMock()
    ) as create, patch.object(
        AccountManager, "setup_monitoring", new=AsyncMock(return_value=True)
    ), patch.object(
        AccountManager, "_start_connection_watcher_task"
    ):
        result = run(AccountManager.reload_user_accounts_detail(1))

    assert result["failed"] == 1
    assert result["fail_reasons"] == {"session_busy": 1}
    create.assert_not_awaited()

def test_disable_account_success_and_missing(tmp_path):
    path = tmp_path / "x.session"
    path.write_bytes(b"")
    user_accounts[1] = {
        "+1": {"client": client_stub(), "session_file": path.name}
    }
    with patch.object(module, "SESSIONS_DIR", str(tmp_path)), patch.object(
        AccountManager, "_cancel_client_task", new=AsyncMock()
    ), patch.object(AccountManager, "_cancel_account_auxiliary_tasks", new=AsyncMock()), patch.object(
        AccountManager, "_safe_disconnect_client", new=AsyncMock()
    ), patch.object(AccountManager, "backup_session_file", new=AsyncMock()) as backup:
        assert run(AccountManager.disable_account(1, "+1"))
    backup.assert_awaited_once()
    assert not run(AccountManager.disable_account(1, "+1"))
