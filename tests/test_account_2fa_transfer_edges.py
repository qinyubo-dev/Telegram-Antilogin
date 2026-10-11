# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
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
    module.account_operation_locks.clear()
    yield
    user_accounts.clear()
    module.account_operation_locks.clear()


def ready(client=None, ok=True, message="offline"):
    client = client or SimpleNamespace(edit_2fa=AsyncMock())
    return (ok, {}, "+1", {}, client if ok else None, "" if ok else message)


@pytest.mark.parametrize("method", ["change", "set", "clear"])
def test_2fa_input_ready_success_and_errors(method):
    target = {
        "change": AccountManager._change_hosted_2fa_unlocked,
        "set": AccountManager._set_hosted_2fa_unlocked,
        "clear": AccountManager._clear_hosted_2fa_unlocked,
    }[method]
    args = (1, "+1", "old", "new") if method == "change" else (1, "+1", "new")
    if method == "clear":
        args = (1, "+1", "old")

    invalid_args = (1, "+1", "", "") if method == "change" else (1, "+1", "")
    assert run(target(*invalid_args)).startswith("❌")

    with patch.object(AccountManager, "ensure_hosted_client_ready", new=AsyncMock(return_value=ready(ok=False))):
        assert run(target(*args)) == "offline"

    client = SimpleNamespace(edit_2fa=AsyncMock())
    with patch.object(AccountManager, "ensure_hosted_client_ready", new=AsyncMock(return_value=ready(client))):
        assert run(target(*args)).startswith("✅")

    client.edit_2fa.side_effect = ValueError("password hash invalid")
    with patch.object(AccountManager, "ensure_hosted_client_ready", new=AsyncMock(return_value=ready(client))):
        assert "密码" in run(target(*args))

    client.edit_2fa.side_effect = RuntimeError("rpc")
    with patch.object(AccountManager, "ensure_hosted_client_ready", new=AsyncMock(return_value=ready(client))), patch.object(
        AccountManager, "handle_hosted_operation_error", new=AsyncMock(return_value="mapped")
    ):
        assert run(target(*args)) == "mapped"


@pytest.mark.parametrize("method,args", [
    (AccountManager.change_hosted_2fa, (1, "+1", "old", "new")),
    (AccountManager.set_hosted_2fa, (1, "+1", "new")),
    (AccountManager.clear_hosted_2fa, (1, "+1", "old")),
])
def test_public_2fa_wrappers(method, args):
    with patch.object(AccountManager, "ensure_hosted_client_ready", new=AsyncMock(return_value=ready())):
        assert run(method(*args)).startswith("✅")


def test_password_error_detection_and_access_helpers():
    assert AccountManager._is_2fa_password_invalid_error(ValueError("invalid password"))
    assert not AccountManager._is_2fa_password_invalid_error(ValueError("network"))
    with patch.object(module.DataManager, "is_admin", return_value=False), patch.object(
        module.DataManager, "has_active_subscription", return_value=True
    ):
        assert AccountManager.check_access(1)
    assert AccountManager._digits_only("+1 (23)") == "123"
    assert AccountManager._find_account_key_by_digits({"+123": {}}, "123") == "+123"
    assert AccountManager._find_account_key_by_digits({}, "123") is None


@pytest.mark.parametrize("case", ["denied", "invalid", "not_owned", "upload", "minutes", "hours", "mixed", "ready"])
def test_transfer_offer_validation(case):
    accounts = {"+123": {}}
    access = case != "denied"
    phone = "" if case == "invalid" else "+123"
    if case == "not_owned":
        accounts = {}
    remaining = {"minutes": 120, "hours": 7200, "mixed": 7260}.get(case, 0)
    with patch.object(AccountManager, "check_access", return_value=access), patch.object(
        AccountManager, "is_uploaded_transfer_locked", return_value=case == "upload"
    ), patch.object(
        AccountManager, "get_account_transfer_remaining_seconds", return_value=remaining
    ):
        user_accounts[1] = accounts
        result = AccountManager.validate_account_transfer_offer(1, phone)
    assert result.ok is (case == "ready")


@pytest.mark.parametrize(
    "case",
    ["source_denied", "invalid", "same", "target_denied", "quota", "source_bad", "duplicate", "session", "ready"],
)
def test_transfer_validation_guards(tmp_path, case):
    user_accounts[1] = {"+123": {}}
    user_accounts[2] = {"+123": {}} if case == "duplicate" else {}

    def access(uid):
        if case == "source_denied" and uid == 1:
            return False
        if case == "target_denied" and uid == 2:
            return False
        return True

    phone = "" if case == "invalid" else "+123"
    target = 1 if case == "same" else 2
    source_ok = case != "source_bad"
    source_result = SimpleNamespace(ok=source_ok, to_user_id=0)
    if case == "session":
        (tmp_path / "2_123.session").write_bytes(b"x")
    with patch.object(module, "SESSIONS_DIR", str(tmp_path)), patch.object(
        AccountManager, "check_access", side_effect=access
    ), patch.object(AccountManager, "can_add_hosted_account", return_value=case != "quota"), patch.object(
        AccountManager, "quota_error_message", return_value="quota"
    ), patch.object(AccountManager, "validate_account_transfer_offer", return_value=source_result):
        result = AccountManager.validate_account_transfer(1, phone, target)
    assert result.ok is (case == "ready")

