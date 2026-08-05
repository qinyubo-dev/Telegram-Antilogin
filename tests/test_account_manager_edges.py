# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from accounts import account_manager as module
from accounts.account_manager import AccountManager, user_accounts


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def _runtime_cleanup():
    user_accounts.clear()
    module.client_tasks.clear()
    AccountManager._hosted_metadata = None
    AccountManager._login_monitor_state = None
    yield
    user_accounts.clear()
    module.client_tasks.clear()
    AccountManager._hosted_metadata = None
    AccountManager._login_monitor_state = None


def test_hosted_phones_quota_and_selection(tmp_path):
    (tmp_path / "1_123.session").write_bytes(b"")
    (tmp_path / "1_bad.session").write_bytes(b"")
    (tmp_path / "2_999.session").write_bytes(b"")
    user_accounts[1] = {"+456": {}}
    with patch.object(module, "SESSIONS_DIR", str(tmp_path)):
        assert AccountManager.hosted_account_phones(1) == {"123", "456"}
    with patch.object(module, "SESSIONS_DIR", str(tmp_path / "missing")):
        assert AccountManager.hosted_account_phones(1) == {"456"}

    with patch.object(AccountManager, "hosted_account_phones", return_value={"1", "2"}), patch.object(
        module.DataManager, "get_hosting_quota", return_value=2
    ):
        assert AccountManager.get_quota_status(1)["full"]
        assert AccountManager.can_add_hosted_account(1, "+1")
        assert not AccountManager.can_add_hosted_account(1, "+3")
        assert "2 / 2" in AccountManager.quota_error_message(1)

    subscriptions = [
        None,
        {"quota": None},
        {"quota": 1, "selection_required": True, "selected_accounts": ["1"]},
        {"quota": 1, "selected_accounts": ["1"]},
    ]
    expected = [False, True, False, True]
    for subscription, value in zip(subscriptions, expected):
        with patch.object(module.DataManager, "is_admin", return_value=False), patch.object(
            module.DataManager, "get_subscription", return_value=subscription
        ):
            assert AccountManager.is_account_selected(1, "+1") is value
    with patch.object(module.DataManager, "is_admin", return_value=True):
        assert AccountManager.is_account_selected(1, "+1")


@pytest.mark.parametrize(
    ("subscription", "hosted", "phone", "expected"),
    [
        (None, set(), "+1", False),
        ({"quota": None}, set(), "+1", True),
        ({"quota": 1}, set(), "", False),
        ({"quota": 1, "selected_accounts": ["1"]}, {"1"}, "+1", True),
        ({"quota": 1, "selected_accounts": ["2"]}, {"1", "2"}, "+1", False),
        ({"quota": 1, "selection_required": True}, {"1", "2"}, "+1", False),
    ],
)
def test_ensure_selected_variants(subscription, hosted, phone, expected):
    with patch.object(module.DataManager, "is_admin", return_value=False), patch.object(
        module.DataManager, "get_subscription", return_value=subscription
    ), patch.object(AccountManager, "hosted_account_phones", return_value=hosted), patch.object(
        module.DataManager, "set_selected_accounts", return_value=True
    ):
        assert AccountManager.ensure_account_selected(1, phone) is expected


def test_login_monitor_state_corrupt_prune_and_save(tmp_path):
    path = tmp_path / "login.json"
    path.write_text("bad", encoding="utf-8")
    with patch.object(module, "LOGIN_MONITOR_STATE_FILE", str(path)):
        assert AccountManager._load_login_monitor_state_locked() == {}
        AccountManager._login_monitor_state = {"old": 1.0, "new": 10_000.0}
        with patch.object(module, "LOGIN_CODE_DEDUP_RETENTION_SECONDS", 100):
            assert AccountManager._prune_login_monitor_state_locked(
                AccountManager._login_monitor_state, 1000
            )
        assert AccountManager._save_login_monitor_state_locked(
            AccountManager._login_monitor_state
        )
        AccountManager._login_monitor_state = None
        assert AccountManager._load_login_monitor_state_locked() == {"new": 10_000.0}

    with patch.object(module, "LOGIN_MONITOR_STATE_FILE", str(tmp_path / "blocked")), patch(
        "accounts.account_manager.tempfile.mkstemp", side_effect=OSError("disk")
    ):
        assert not AccountManager._save_login_monitor_state_locked({})

    assert not AccountManager._is_login_message_processed(1, "+1", None)
    AccountManager._mark_login_message_processed(1, "+1", None)


def test_metadata_load_normalizes_and_handles_corruption(tmp_path):
    path = tmp_path / "metadata.json"
    payload = {
        "accounts": {
            "1:1": 10,
            "1:2": {
                "created_at": 20,
                "source": "INVALID",
                "last_transferred_at": "bad",
                "known_authorization_hashes": ["b", "a", "a"],
                "pending_authorizations": {"ok": {"device": "x"}, "bad": 1},
            },
            "1:3": {"created_at": "bad"},
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    with patch.object(module, "HOSTED_ACCOUNT_METADATA_FILE", str(path)):
        metadata = AccountManager._load_hosted_metadata_locked()
        assert metadata["1:1"]["source"] == "unknown"
        assert metadata["1:2"]["known_authorization_hashes"] == ["a", "b"]
        assert metadata["1:2"]["pending_authorizations"] == {"ok": {"device": "x"}}
        assert "1:3" not in metadata
        assert AccountManager._save_hosted_metadata_locked(metadata)

    path.write_text("broken", encoding="utf-8")
    AccountManager._hosted_metadata = None
    with patch.object(module, "HOSTED_ACCOUNT_METADATA_FILE", str(path)):
        assert AccountManager._load_hosted_metadata_locked() == {}


def test_metadata_record_operations_and_rollback():
    AccountManager._hosted_metadata = {}
    with patch.object(AccountManager, "_save_hosted_metadata_locked", return_value=True):
        created = AccountManager.get_hosted_account_created_at(1, "+1")
        assert created is not None
        assert AccountManager.set_hosted_account_source(1, "+1", "upload")
        assert AccountManager.get_hosted_account_source(1, "+1") == "upload"
        assert AccountManager.set_hosted_account_source(1, "+1", "bad")
        assert AccountManager.get_hosted_account_source(1, "+1") == "unknown"
        assert AccountManager.set_hosted_account_last_transferred_at(1, "+1", 12)
        assert AccountManager.get_hosted_account_last_transferred_at(1, "+1") == 12
        assert AccountManager.remove_hosted_account_metadata(1, "+1")

    AccountManager._hosted_metadata = {"1:1": {"created_at": 1}}
    with patch.object(AccountManager, "_save_hosted_metadata_locked", return_value=False):
        assert not AccountManager._replace_hosted_metadata_records(
            {"1:1": None, "1:2": {"created_at": 2}}
        )
    assert AccountManager._hosted_metadata == {"1:1": {"created_at": 1}}

def test_clean_age_and_transfer_guards():
    assert "秒" in AccountManager.hosting_clean_age_message(5)
    assert "小时" in AccountManager.hosting_clean_age_message(3601)
    with patch.object(module.DataManager, "is_admin", return_value=True):
        assert AccountManager.get_account_transfer_remaining_seconds(1, "+1") == 0
    with patch.object(AccountManager, "get_hosted_account_source", return_value="upload"), patch.object(
        module.DataManager, "is_admin", return_value=False
    ):
        assert AccountManager.is_uploaded_transfer_locked(1, "+1")


def test_task_tracking_and_cancellation():
    async def scenario():
        task = asyncio.create_task(asyncio.sleep(10))
        AccountManager._track_client_task("one", task)
        assert module.client_tasks["one"] is task
        await AccountManager._cancel_client_task("one")
        assert "one" not in module.client_tasks

        async def boom():
            raise RuntimeError("task failed")

        failed = asyncio.create_task(boom())
        AccountManager._track_client_task("failed", failed)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    run(scenario())
