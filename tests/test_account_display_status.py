# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import asyncio

from accounts import account_manager as account_manager_module
from accounts.account_manager import AccountManager


def test_hosting_status_prioritizes_connection_then_freeze(monkeypatch):
    monkeypatch.setattr(account_manager_module, "HOSTING_OPERATION_MIN_AGE_SECONDS", 24 * 60 * 60)

    assert AccountManager.get_hosting_status_text(1, "+100", {"runtime_status": "offline"}) == "🔴 连接中断"
    assert AccountManager.get_hosting_status_text(
        1,
        "+100",
        {"runtime_status": "online", "health_status": "frozen", "created_at": 0},
    ) == "❄️ 账户冻结"


def test_hosting_status_uses_24_hour_account_age(monkeypatch):
    now = 2_000_000.0
    monkeypatch.setattr(account_manager_module.time, "time", lambda: now)
    monkeypatch.setattr(account_manager_module, "HOSTING_OPERATION_MIN_AGE_SECONDS", 24 * 60 * 60)

    restricted = AccountManager.get_hosting_status_text(
        1,
        "+100",
        {"runtime_status": "online", "health_status": "alive", "created_at": now - 12 * 60 * 60},
    )
    operable = AccountManager.get_hosting_status_text(
        1,
        "+100",
        {"runtime_status": "online", "health_status": "alive", "created_at": now - 24 * 60 * 60},
    )

    assert restricted == "⛔️ 操作受限 · 剩余12小时"
    assert operable == "🟢 可操作"


def test_hosting_status_shows_only_hours_until_under_one_hour(monkeypatch):
    now = 2_000_000.0
    monkeypatch.setattr(account_manager_module.time, "time", lambda: now)
    monkeypatch.setattr(account_manager_module, "HOSTING_OPERATION_MIN_AGE_SECONDS", 24 * 60 * 60)

    assert AccountManager.get_hosting_status_text(
        1,
        "+100",
        {
            "runtime_status": "online",
            "health_status": "alive",
            "created_at": now - (11 * 60 + 30) * 60,
        },
    ) == "⛔️ 操作受限 · 剩余12小时"
    assert AccountManager.get_hosting_status_text(
        1,
        "+100",
        {
            "runtime_status": "online",
            "health_status": "alive",
            "created_at": now - (23 * 60 + 30) * 60,
        },
    ) == "⛔️ 操作受限 · 剩余30分钟"


def test_transfer_recipient_preserves_real_creation_time(monkeypatch):
    now = 2_000_000.0
    monkeypatch.setattr(account_manager_module, "HOSTING_OPERATION_MIN_AGE_SECONDS", 24 * 60 * 60)
    source_created_at = now - 2 * 60 * 60

    created_at = account_manager_module.transfer_recipient_created_at(source_created_at)

    assert created_at == source_created_at
    monkeypatch.setattr(account_manager_module.time, "time", lambda: now)
    assert AccountManager.get_hosting_status_text(
        1,
        "+100",
        {
            "runtime_status": "online",
            "health_status": "alive",
            "created_at": created_at,
        },
    ) == "⛔️ 操作受限 · 剩余22小时"


def test_transfer_recipient_missing_creation_time_starts_full_restriction(monkeypatch):
    now = 2_000_000.0
    monkeypatch.setattr(account_manager_module.time, "time", lambda: now)

    assert account_manager_module.transfer_recipient_created_at(None) == now


def test_transfer_requires_one_hour_after_each_success(monkeypatch, tmp_path):
    user_id = 102
    phone = "+200"
    now = 2_000_000.0
    metadata_path = tmp_path / "hosted_account_metadata.json"
    monkeypatch.setattr(account_manager_module, "HOSTED_ACCOUNT_METADATA_FILE", str(metadata_path))
    monkeypatch.setattr(account_manager_module, "ACCOUNT_TRANSFER_MIN_AGE_SECONDS", 24 * 60 * 60)
    monkeypatch.setattr(account_manager_module, "TRANSFER_RECIPIENT_RESTRICTION_SECONDS", 60 * 60)
    monkeypatch.setattr(account_manager_module.time, "time", lambda: now)
    monkeypatch.setattr(account_manager_module.DataManager, "is_admin", lambda _user_id: False)
    AccountManager._hosted_metadata = None

    try:
        AccountManager.set_hosted_account_created_at(user_id, phone, now - 48 * 60 * 60)
        assert AccountManager.get_account_transfer_remaining_seconds(user_id, phone) == 0

        AccountManager.set_hosted_account_last_transferred_at(user_id, phone, now)
        assert AccountManager.get_account_transfer_remaining_seconds(user_id, phone) == 60 * 60

        monkeypatch.setattr(account_manager_module.time, "time", lambda: now + 60 * 60)
        assert AccountManager.get_account_transfer_remaining_seconds(user_id, phone) == 0
    finally:
        AccountManager._hosted_metadata = None


def test_delete_account_removes_creation_metadata(monkeypatch, tmp_path):
    user_id = 101
    phone = "+100"
    metadata_path = tmp_path / "hosted_account_metadata.json"
    monkeypatch.setattr(account_manager_module, "HOSTED_ACCOUNT_METADATA_FILE", str(metadata_path))
    monkeypatch.setattr(AccountManager, "check_access", staticmethod(lambda _user_id: True))
    AccountManager._hosted_metadata = None
    account_manager_module.user_accounts[user_id] = {
        phone: {
            "client": None,
            "session_file": None,
            "display_phone": phone,
        }
    }
    AccountManager.set_hosted_account_created_at(user_id, phone, 1234.0)

    try:
        result = asyncio.run(AccountManager.delete_account(user_id, phone))

        assert result.startswith("🗑")
        assert AccountManager.get_hosted_account_created_at(
            user_id, phone, create_if_missing=False
        ) is None
    finally:
        account_manager_module.user_accounts.pop(user_id, None)
        AccountManager._hosted_metadata = None


def test_system_invalid_session_cleanup_removes_all_account_metadata(
    monkeypatch, tmp_path
):
    user_id = 103
    phone = "+300"
    metadata_path = tmp_path / "hosted_account_metadata.json"
    monkeypatch.setattr(account_manager_module, "HOSTED_ACCOUNT_METADATA_FILE", str(metadata_path))
    AccountManager._hosted_metadata = None
    account_manager_module.user_accounts[user_id] = {
        phone: {
            "client": None,
            "session_file": None,
            "display_phone": phone,
        }
    }
    AccountManager.set_hosted_account_created_at(user_id, phone, 1234.0)
    AccountManager.set_hosted_account_last_transferred_at(user_id, phone, 2345.0)

    try:
        asyncio.run(
            AccountManager.cleanup_invalid_hosted_session(
                user_id, phone, notify_user=False
            )
        )

        assert AccountManager.get_hosted_account_created_at(
            user_id, phone, create_if_missing=False
        ) is None
        assert AccountManager.get_hosted_account_last_transferred_at(
            user_id, phone
        ) is None
    finally:
        account_manager_module.user_accounts.pop(user_id, None)
        AccountManager._hosted_metadata = None


def test_compact_hosting_status_hides_redundant_labels(monkeypatch):
    now = 2_000_000.0
    monkeypatch.setattr(account_manager_module.time, "time", lambda: now)
    monkeypatch.setattr(account_manager_module, "HOSTING_OPERATION_MIN_AGE_SECONDS", 24 * 60 * 60)

    assert AccountManager.get_compact_hosting_status_text(
        1,
        "+100",
        {"runtime_status": "online", "health_status": "alive", "created_at": now - 24 * 60 * 60},
    ) == "🟢"
    assert AccountManager.get_compact_hosting_status_text(
        1,
        "+100",
        {"runtime_status": "online", "health_status": "frozen", "created_at": now},
    ) == "❄️"
    assert AccountManager.get_compact_hosting_status_text(
        1,
        "+100",
        {"runtime_status": "online", "health_status": "alive", "created_at": now - 12 * 60 * 60},
    ) == "⛔️ 剩余12小时"


def test_antilogin_status_describes_effective_protection(monkeypatch):
    now = 2_000_000.0
    monkeypatch.setattr(account_manager_module.time, "time", lambda: now)

    assert AccountManager.get_antilogin_status_text(
        {"runtime_status": "online", "anti_login": True}
    ) == "🛡️ 防护中"
    assert AccountManager.get_antilogin_status_text(
        {
            "runtime_status": "online",
            "anti_login": True,
            "temporary_mode": "pause",
            "temporary_until": now + 10 * 60,
        }
    ) == "⏸️ 暂停中 · 剩余10分钟"
    assert AccountManager.get_antilogin_status_text(
        {"runtime_status": "offline", "anti_login": True}
    ) == "⚠️ 防护未生效 · 连接中断"


def test_antilogin_status_icon_is_compact(monkeypatch):
    now = 2_000_000.0
    monkeypatch.setattr(account_manager_module.time, "time", lambda: now)

    assert AccountManager.get_antilogin_status_icon(
        {"runtime_status": "online", "anti_login": True}
    ) == "🛡️"
    assert AccountManager.get_antilogin_status_icon(
        {
            "runtime_status": "online",
            "anti_login": True,
            "temporary_mode": "pause",
            "temporary_until": now + 60,
        }
    ) == "⏸️"
    assert AccountManager.get_antilogin_status_icon(
        {"runtime_status": "offline", "anti_login": True}
    ) == "⚠️"
    assert AccountManager.get_antilogin_status_icon(
        {"runtime_status": "online", "anti_login": False}
    ) == "⚪"
