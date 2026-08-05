# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from storage import admin_audit
from storage import data_manager as dm
from storage import user_profile_cache as profile_module
from storage.admin_audit import AdminAuditLog
from storage.data_manager import DataManager
from storage.user_profile_cache import UserProfileCache


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    data_file = tmp_path / "user_data.json"
    orders_file = tmp_path / "payment_orders.json"
    monkeypatch.setattr(dm, "DATA_FILE", str(data_file))
    monkeypatch.setattr(dm, "PAYMENT_ORDERS_FILE", str(orders_file))
    dm.user_data = DataManager._default_data()
    dm.data_load_succeeded = True
    dm.payment_orders = {}
    dm.payment_orders_load_succeeded = True
    dm.subscription_expiry_index = {}
    yield SimpleNamespace(data_file=data_file, orders_file=orders_file)


@pytest.fixture
def isolated_audit(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(admin_audit, "ADMIN_AUDIT_FILE", str(path))
    AdminAuditLog._last_prune_date = None
    return path


@pytest.fixture
def isolated_profiles(tmp_path, monkeypatch):
    path = tmp_path / "profiles.json"
    monkeypatch.setattr(profile_module, "PROFILE_CACHE_FILE", str(path))
    UserProfileCache._loaded = False
    UserProfileCache._profiles = {}
    return path


def test_phone_masking_boundaries():
    for value, expected in [("", ""), ("12", "***12"), ("+86 138-0013-8000", "***8000")]:
        assert admin_audit.mask_phone(value) == expected


def test_phone_digest_is_stable_and_empty_safe():
    assert admin_audit.phone_digest("") == ""
    assert admin_audit.phone_digest("+1 (234)") == admin_audit.phone_digest("1234")


def test_audit_sanitize_handles_nested_secrets_phones_dates_and_objects():
    now = datetime.now()
    value = {
        "token": "secret",
        "phone": "+8613800138000",
        "selected_accounts": ["+100", "+200"],
        "phone_digest": "already-hashed",
        "nested": {"created": now, "values": {1, 2}, "object": object()},
    }
    sanitized = admin_audit._sanitize(value)
    assert sanitized["token"] == "[REDACTED]"
    assert sanitized["phone"] == "***8000"
    assert sanitized["selected_accounts"] == ["***00", "***00"]
    assert sanitized["phone_digest"] == "already-hashed"
    assert sanitized["nested"]["created"] == now.isoformat()
    assert sorted(sanitized["nested"]["values"]) == [1, 2]
    assert isinstance(sanitized["nested"]["object"], str)


def test_audit_error_sanitization_redacts_urls_numbers_and_bounds_length():
    assert admin_audit._sanitize_error(None) is None
    text = admin_audit._sanitize_error(
        "open https://example.test/token for +86 138 0013 8000 " + "x" * 600
    )
    assert "https://" not in text
    assert "138" not in text
    assert len(text) == 500


def test_audit_record_result_validation_and_missing_context(isolated_audit):
    with pytest.raises(ValueError):
        AdminAuditLog.record_result(None, "unknown", admin_id=1, action="x")
    assert not AdminAuditLog.record_result(None, "failed")


def test_audit_skips_first_configured_admin_without_reporting_failure(isolated_audit):
    with patch.object(admin_audit.config, "ADMIN_IDS", [101, 202]):
        audit_id = AdminAuditLog.record_attempt(101, "user.search", "user", 7)
        assert audit_id
        assert AdminAuditLog.record_result(
            audit_id,
            "success",
            admin_id=101,
            action="user.search",
            target_type="user",
            target_id=7,
        )
        assert AdminAuditLog.record_result(
            None,
            "success",
            admin_id=101,
            action="user.search",
            target_type="user",
            target_id=7,
        )
        assert not isolated_audit.exists()

        second_audit_id = AdminAuditLog.record_attempt(202, "user.search", "user", 8)
        assert AdminAuditLog.record_result(
            second_audit_id,
            "success",
            admin_id=202,
            action="user.search",
            target_type="user",
            target_id=8,
        )

    entries = AdminAuditLog._read_entries_locked()
    assert [entry["admin_id"] for entry in entries] == [202, 202]


def test_audit_read_query_filters_and_error_fallback(isolated_audit):
    assert AdminAuditLog._read_entries_locked() == []
    isolated_audit.write_text(
        "not-json\n"
        + json.dumps({"audit_id": "1", "timestamp": "2026-01-01", "admin_id": 1, "result": "attempt"})
        + "\n"
        + json.dumps({"audit_id": "2", "timestamp": "2026-01-02", "admin_id": 2, "result": "success"})
        + "\n",
        encoding="utf-8",
    )
    result = AdminAuditLog.query(
        {"admin_id": 2, "result": "success", "exclude_attempt": True},
        page=99,
        page_size=999,
    )
    assert result["total"] == 1
    assert result["page"] == 0
    assert result["items"][0]["audit_id"] == "2"
    with patch("builtins.open", side_effect=OSError("read")):
        assert AdminAuditLog._read_entries_locked() == []


def test_audit_append_and_prune_failures_are_non_fatal(isolated_audit, tmp_path):
    with patch("builtins.open", side_effect=OSError("write")):
        assert not AdminAuditLog._append({"audit_id": "x"})

    assert AdminAuditLog.prune()
    old = (datetime.now() - timedelta(days=20)).isoformat()
    isolated_audit.write_text(json.dumps({"timestamp": old}) + "\n", encoding="utf-8")
    temp = tmp_path / "leftover.tmp"

    def fake_mkstemp(**kwargs):
        descriptor = __import__("os").open(temp, __import__("os").O_CREAT | __import__("os").O_WRONLY)
        return descriptor, str(temp)

    with patch("storage.admin_audit.tempfile.mkstemp", side_effect=fake_mkstemp), patch(
        "storage.admin_audit.os.replace", side_effect=OSError("replace")
    ):
        assert not AdminAuditLog.prune(1)
    assert not temp.exists()
    assert admin_audit.audit_file_path() == str(isolated_audit.resolve())


def test_profile_load_missing_corrupt_and_cached(isolated_profiles):
    assert UserProfileCache.get(1) == (False, None)
    isolated_profiles.write_text("not-json", encoding="utf-8")
    UserProfileCache._loaded = False
    assert UserProfileCache.get(1) == (False, None)
    assert UserProfileCache._loaded
    # Once loaded, the cache must not re-read a changed file.
    isolated_profiles.write_text(json.dumps({"1": {"display_name": "new"}}), encoding="utf-8")
    assert UserProfileCache.get(1) == (False, None)


def test_profile_get_rejects_invalid_and_stale_entries(isolated_profiles):
    entries = [
        "invalid",
        {},
        {"updated_at": "invalid"},
        {"updated_at": (datetime.now() - timedelta(days=4)).isoformat()},
    ]
    UserProfileCache._loaded = True
    for entry in entries:
        UserProfileCache._profiles = {"1": entry}
        assert UserProfileCache.get(1) == (False, None)


def test_profile_copy_entity_iteration_and_unchanged_write(isolated_profiles):
    current = datetime.now().isoformat()
    UserProfileCache._loaded = True
    UserProfileCache._profiles = {
        "1": {"display_name": "Alice", "username": "alice", "updated_at": current},
        "bad": {"display_name": "bad"},
        "2": "bad",
    }
    profile = UserProfileCache.get_profile(1)
    profile["display_name"] = "changed"
    assert UserProfileCache.get_profile(1)["display_name"] == "Alice"
    assert UserProfileCache.get_profile(999) is None
    assert list(UserProfileCache.iter_profiles()) == [
        (1, {"display_name": "Alice", "username": "alice", "updated_at": current})
    ]
    with patch.object(UserProfileCache, "_save") as save:
        assert not UserProfileCache.set_profile(1, " Alice ", "@ALICE")
    save.assert_not_called()
    assert not UserProfileCache.set_entity(SimpleNamespace(id=None))
    with patch.object(UserProfileCache, "set_profile", return_value=True) as set_profile:
        assert UserProfileCache.set_entity(
            SimpleNamespace(id=3, first_name=" Ada ", last_name=" Lovelace ", username="ADA")
        )
    set_profile.assert_called_once_with(3, "Ada Lovelace", "ADA")


def test_profile_save_failure_removes_temporary_file(isolated_profiles, tmp_path):
    UserProfileCache._loaded = True
    UserProfileCache._profiles = {"1": {"display_name": "Alice"}}
    temp = tmp_path / "profile.tmp"

    def fake_mkstemp(**kwargs):
        descriptor = __import__("os").open(temp, __import__("os").O_CREAT | __import__("os").O_WRONLY)
        return descriptor, str(temp)

    with patch("storage.user_profile_cache.tempfile.mkstemp", side_effect=fake_mkstemp), patch(
        "storage.user_profile_cache.os.replace", side_effect=OSError("replace")
    ):
        UserProfileCache._save()
    assert not temp.exists()


def test_load_user_data_creates_clean_files_and_defaults(isolated_storage):
    dm.data_load_succeeded = False
    dm.payment_orders_load_succeeded = False
    assert DataManager.load_user_data()
    assert isolated_storage.data_file.exists()
    assert isolated_storage.orders_file.exists()
    assert dm.user_data["schema_version"] == dm.USER_DATA_SCHEMA_VERSION
    assert dm.data_load_succeeded and dm.payment_orders_load_succeeded


def test_load_user_data_validates_schema_keys_users_and_legacy_fields(isolated_storage):
    cases = [
        [],
        {"schema_version": 99},
        {"schema_version": 1, "1": []},
        {"schema_version": 1, "1": {"is_vip": True}},
        {"schema_version": 1, "unknown": {}},
    ]
    for index, payload in enumerate(cases):
        isolated_storage.data_file.write_text(json.dumps(payload), encoding="utf-8")
        dm.data_load_succeeded = False
        assert not DataManager.load_user_data(), index
        assert not dm.data_load_succeeded
        assert dm.user_data["schema_version"] == 1


def test_load_user_data_converts_user_keys_defaults_catalog_and_calls_orders(isolated_storage):
    isolated_storage.data_file.write_text(
        json.dumps({"schema_version": 1, "7": {"language": "en"}}), encoding="utf-8"
    )
    with patch.object(DataManager, "load_payment_orders", return_value=True) as load_orders:
        assert DataManager.load_user_data()
    assert dm.user_data[7]["language"] == "en"
    assert "subscription_catalog" in dm.user_data
    assert "subscription_periods" in dm.user_data
    load_orders.assert_called_once_with()


def test_backup_data_file_uses_unique_suffix(isolated_storage):
    isolated_storage.data_file.write_text("data", encoding="utf-8")
    with patch("storage.data_manager.datetime") as clock:
        clock.now.return_value.strftime.return_value = "stamp"
        first = Path(DataManager._backup_data_file("bad"))
        second = Path(DataManager._backup_data_file("bad"))
    assert first.name.endswith("bad.stamp.backup")
    assert second.name.endswith("bad.stamp.1.backup")
    assert first.read_text(encoding="utf-8") == "data"
    isolated_storage.data_file.unlink()
    assert DataManager._backup_data_file() == ""


def test_save_user_data_guard_serialization_filter_and_legacy_rejection(isolated_storage):
    dm.data_load_succeeded = False
    assert not DataManager.save_user_data()
    dm.data_load_succeeded = True
    dm.user_data = {
        "schema_version": 1,
        "payment_orders": {"old": {}},
        "vip_prices": {},
        7: {"language": "en"},
    }
    assert DataManager.save_user_data()
    saved = json.loads(isolated_storage.data_file.read_text(encoding="utf-8"))
    assert saved["7"] == {"language": "en"}
    assert "payment_orders" not in saved and "vip_prices" not in saved

    dm.user_data[7]["vip_expiry"] = "legacy"
    assert not DataManager.save_user_data()


def test_save_user_data_replace_failure_removes_temp(isolated_storage, tmp_path):
    temp = tmp_path / "user.tmp"

    def fake_mkstemp(**kwargs):
        descriptor = __import__("os").open(temp, __import__("os").O_CREAT | __import__("os").O_WRONLY)
        return descriptor, str(temp)

    with patch("storage.data_manager.tempfile.mkstemp", side_effect=fake_mkstemp), patch(
        "storage.data_manager.os.replace", side_effect=OSError("replace")
    ):
        assert not DataManager.save_user_data()
    assert not temp.exists()


def test_load_user_data_propagates_payment_order_failure_and_backup_failure(isolated_storage):
    isolated_storage.data_file.write_text(
        json.dumps({"schema_version": 1}), encoding="utf-8"
    )
    with patch.object(DataManager, "load_payment_orders", return_value=False):
        assert not DataManager.load_user_data()
    assert not dm.data_load_succeeded

    isolated_storage.data_file.unlink()
    with patch.object(DataManager, "load_payment_orders", return_value=False):
        assert not DataManager.load_user_data()

    isolated_storage.data_file.write_text("invalid", encoding="utf-8")
    with patch.object(DataManager, "_backup_data_file", side_effect=OSError("backup")):
        assert not DataManager.load_user_data()


def test_subscription_index_handles_invalid_expired_empty_and_mutation(isolated_storage):
    future = datetime.now() + timedelta(days=3)
    past = datetime.now() - timedelta(days=3)
    dm.user_data = {
        "system": {},
        1: {},
        2: {"subscription": {}},
        3: {"subscription": {"expires_at": "invalid"}},
        4: {"subscription": {"expires_at": past.isoformat()}},
        5: {"subscription": {"expires_at": future.isoformat()}},
    }
    DataManager.rebuild_subscription_index()
    assert set(dm.subscription_expiry_index) == {5}
    DataManager._set_subscription_index("bad", future)
    DataManager._set_subscription_index(5, future, active=False)
    assert dm.subscription_expiry_index == {}
    DataManager._set_subscription_index(6, SimpleNamespace(timestamp=Mock(side_effect=ValueError)))
    assert 6 not in dm.subscription_expiry_index

    with patch.object(DataManager, "rebuild_subscription_index") as rebuild:
        assert list(DataManager.iter_subscription_users()) == []
    rebuild.assert_called_once_with()
    dm.subscription_expiry_index = {7: future.timestamp()}
    assert list(DataManager.iter_subscription_users())[0][0] == 7


def test_language_initialization_and_selection_rollback_edges(isolated_storage):
    dm.data_load_succeeded = False
    assert DataManager.initialize_user_language(1, "en")
    assert 1 not in dm.user_data
    dm.data_load_succeeded = True
    dm.user_data[1] = {"language": "en", "other": 1}
    with patch.object(DataManager, "save_user_data", return_value=True) as save:
        assert DataManager.initialize_user_language(1, "zh")
    save.assert_not_called()
    assert not DataManager.set_user_language(1, "xx")

    with patch.object(DataManager, "save_user_data", return_value=False):
        assert not DataManager.initialize_user_language(2, "en")
        assert 2 not in dm.user_data
        assert not DataManager.set_user_language(1, "zh")
    assert dm.user_data[1] == {"language": "en", "other": 1}


def test_active_subscription_cache_and_invalid_expiry_edges(isolated_storage):
    future = datetime.now() + timedelta(days=2)
    past = datetime.now() - timedelta(days=2)
    dm.subscription_expiry_index = {1: future.timestamp(), 2: past.timestamp()}
    assert DataManager.has_active_subscription(1)
    assert not DataManager.has_active_subscription(2)
    assert not DataManager.has_active_subscription(3)
    dm.user_data.update(
        {
            4: {"subscription": {"expires_at": future.isoformat()}},
            5: {"subscription": {"expires_at": past.isoformat()}},
            6: {"subscription": {"expires_at": "invalid"}},
            7: {},
        }
    )
    assert DataManager.has_active_subscription(4)
    assert not DataManager.has_active_subscription(5)
    assert not DataManager.has_active_subscription(6)
    assert not DataManager.has_active_subscription(7)


def test_catalog_validation_rejects_invalid_values(isolated_storage):
    catalogs = [
        {"go": {"price": 0}},
        {"go": {"quota": 0}},
        {"plus": {"addon_unit_price": 0}},
        {"plus": {"min_addon": 0}},
        {"go": {"price": "bad"}},
    ]
    for catalog in catalogs:
        assert not DataManager.set_subscription_catalog(catalog)


def test_catalog_and_period_save_failure_restore_absent_and_existing_values(isolated_storage):
    catalog = DataManager.default_subscription_catalog()
    dm.user_data.pop("subscription_catalog", None)
    with patch.object(DataManager, "save_user_data", return_value=False):
        assert not DataManager.set_subscription_catalog(catalog)
    assert "subscription_catalog" not in dm.user_data

    previous = DataManager.default_subscription_periods()
    dm.user_data["subscription_periods"] = copy.deepcopy(previous)
    changed = copy.deepcopy(previous)
    changed[90]["discount_percent"] = "10"
    with patch.object(DataManager, "save_user_data", return_value=False):
        assert not DataManager.set_subscription_periods(changed)
    assert dm.user_data["subscription_periods"] == previous


def test_period_validation_rejects_missing_and_invalid_values(isolated_storage):
    invalid_periods = [
        {},
        {30: 1, 90: 0, 180: 0, 365: 0},
        {30: 0, 90: -1, 180: 0, 365: 0},
        {30: 0, 90: 100, 180: 0, 365: 0},
        {30: 0, 90: "bad", 180: 0, 365: 0},
    ]
    for periods in invalid_periods:
        assert not DataManager.set_subscription_periods(periods)


def test_period_reader_falls_back_per_invalid_entry(isolated_storage):
    dm.user_data["subscription_periods"] = {
        "30": {"discount_percent": "0"},
        90: "bad",
        180: {"discount_percent": "invalid"},
    }
    periods = DataManager.get_subscription_periods()
    assert periods[30]["discount_percent"] == "0"
    assert periods[90]["discount_percent"] == "8"
    assert periods[180]["discount_percent"] == "18"
    assert periods[365]["discount_percent"] == "25"


@pytest.mark.parametrize(
    ("plan", "quota", "days", "fragment"),
    [
        ("unknown", None, 30, "未知"),
        ("plus", 11, 30, "至少增加"),
        ("plus", 5, 30, "不能低于"),
        ("go", 3, 30, "不支持自定义"),
        ("go", None, "bad", "周期无效"),
        ("go", None, 31, "不支持"),
    ],
)
def test_quote_subscription_rejects_invalid_plan_quota_and_period(
    isolated_storage, plan, quota, days, fragment
):
    with pytest.raises(ValueError) as captured:
        DataManager.quote_subscription(plan, quota, days)
    assert fragment in str(captured.value)


def test_remaining_billing_segments_filters_sorts_and_falls_back(isolated_storage):
    now = datetime.now()
    future = now + timedelta(days=10)
    raw = {
        "billing_segments": [
            {},
            {"starts_at": now.isoformat(), "expires_at": (now - timedelta(days=1)).isoformat(), "monthly_price": "1"},
            {"starts_at": future.isoformat(), "expires_at": (future + timedelta(days=1)).isoformat(), "monthly_price": "0"},
            {"starts_at": (now - timedelta(days=1)).isoformat(), "expires_at": future.isoformat(), "monthly_price": "1.20", "tag": "ok"},
        ]
    }
    segments = DataManager._remaining_billing_segments(raw, now)
    assert len(segments) == 1
    assert segments[0]["starts_at"] == now.isoformat()
    assert segments[0]["monthly_price"] == "1.2"
    assert DataManager._remaining_billing_segments({}, now) == []
    assert DataManager._remaining_billing_segments(
        {"expires_at": (now - timedelta(days=1)).isoformat(), "plan_id": "go", "quota": 2}, now
    ) == []
    fallback = DataManager._remaining_billing_segments(
        {"expires_at": future.isoformat(), "plan_id": "go", "quota": 2}, now
    )
    assert fallback[0]["price_source"] == "catalog_fallback"


def test_subscription_read_hosting_and_classification_edges(isolated_storage, monkeypatch):
    monkeypatch.setattr(dm, "ADMIN_IDS", [99])
    assert DataManager.get_subscription(99)["plan_id"] == "admin"
    assert DataManager.get_hosting_quota(99) is None
    assert DataManager.get_subscription(1) is None
    dm.user_data[1] = {"subscription": {"plan_id": "go", "expires_at": "invalid"}}
    assert DataManager.get_subscription(1) is None
    assert not DataManager.get_subscription(1, include_inactive=True)["active"]
    assert DataManager.get_hosting_quota(1) == 0

    with patch.object(DataManager, "get_subscription", return_value=None):
        assert DataManager.classify_subscription_change(1, "go", 2) == "new"
    scheduled = {"scheduled": {"plan_id": "go", "quota": 2}}
    with patch.object(DataManager, "get_subscription", return_value=scheduled):
        assert DataManager.classify_subscription_change(1, "go", 2) == "scheduled_renewal"
        assert DataManager.classify_subscription_change(1, "plus", 10) == "conflict"


@pytest.mark.parametrize(
    "args",
    [
        ("bad", "go", 2),
        (0, "go", 2),
        (1, "unknown", 2),
        (1, "go", "bad"),
        (1, "go", 0),
    ],
)
def test_apply_subscription_rejects_invalid_direct_inputs(isolated_storage, args):
    user_id, plan, quota = args
    assert not DataManager.apply_subscription(
        user_id, plan, quota, validate_catalog=False, billing_price="1"
    )


@pytest.mark.parametrize("price", [None, "bad", 0, -1])
def test_apply_subscription_rejects_invalid_billing_price(isolated_storage, price):
    if price is None:
        with patch.object(DataManager, "quote_subscription", side_effect=ValueError):
            assert not DataManager.apply_subscription(
                1, "go", 2, validate_catalog=False, billing_price=None
            )
    else:
        assert not DataManager.apply_subscription(
            1, "go", 2, validate_catalog=False, billing_price=price
        )


def test_subscription_ids_selection_snapshot_and_restore_edges(isolated_storage):
    dm.user_data.update(
        {
            1: {"subscription": {"quota": 2}},
            2: {},
            "system": {},
        }
    )
    assert DataManager.get_subscription_user_ids() == [1]
    assert DataManager.get_all_user_ids() == [1, 2]
    assert not DataManager.set_selected_accounts(2, ["1"])
    assert not DataManager.set_selected_accounts(1, ["1", "2", "3"])
    with patch.object(DataManager, "save_user_data", return_value=True):
        assert DataManager.set_selected_accounts(1, ["+1", "1", "bad", "+2"], finalize=False)
    assert dm.user_data[1]["subscription"]["selected_accounts"] == ["1", "2"]
    assert dm.user_data[1]["subscription"]["selection_required"]
    snapshot = DataManager.get_raw_subscription_snapshot(1)
    snapshot["quota"] = 99
    assert dm.user_data[1]["subscription"]["quota"] == 2
    assert DataManager.get_raw_subscription_snapshot(2) is None
    assert DataManager.subscription_snapshots_match({1: {"quota": 2, "selected_accounts": ["1", "2"], "selection_required": True}})

    before = copy.deepcopy(dm.user_data)
    with patch.object(DataManager, "save_user_data", return_value=False):
        assert not DataManager.restore_subscription_snapshots({1: None, 3: {"quota": 1}})
    assert dm.user_data == before


def test_transfer_and_reconcile_selected_accounts_rollback_edges(isolated_storage):
    source = {"quota": 2, "selected_accounts": ["111", "222"]}
    target = {"quota": 1, "selected_accounts": [], "selection_required": True}
    dm.user_data.update({1: {"subscription": source}, 2: {"subscription": target}})
    snapshots = {1: copy.deepcopy(source), 2: copy.deepcopy(target)}
    assert not DataManager.transfer_selected_account(1, 2, "bad", snapshots, [])
    assert not DataManager.transfer_selected_account(
        1, 2, "111", {1: {"changed": True}}, []
    )
    before = copy.deepcopy(dm.user_data)
    with patch.object(DataManager, "save_user_data", return_value=False):
        assert not DataManager.transfer_selected_account(
            1, 2, "111", snapshots, ["111", "333"]
        )
    assert dm.user_data == before

    assert DataManager.reconcile_selected_accounts({99: ["1"]})
    with patch.object(DataManager, "save_user_data", return_value=False):
        assert not DataManager.reconcile_selected_accounts({1: ["222"]})
    assert dm.user_data[1]["subscription"] == before[1]["subscription"]


def test_delete_user_and_subscription_user_views(isolated_storage):
    future = datetime.now() + timedelta(days=2)
    past = datetime.now() - timedelta(days=1)
    dm.user_data.update(
        {
            1: {"subscription": {"expires_at": future.isoformat()}},
            2: {"subscription": {"expires_at": future.isoformat(), "starts_at": "invalid"}},
        }
    )
    assert DataManager.delete_user_data(99)
    with patch.object(DataManager, "save_user_data", return_value=False):
        assert not DataManager.delete_user_data(1)
    assert 1 in dm.user_data
    with patch.object(DataManager, "save_user_data", return_value=True):
        assert DataManager.delete_user_data(1)
    assert 1 not in dm.user_data

    dm.subscription_expiry_index = {2: future.timestamp(), 3: past.timestamp()}
    users = DataManager.get_all_subscription_users()
    assert users[0]["user_id"] == 2 and users[0]["total_days"] == 0
    expiring = DataManager.get_expiring_subscription_users(3)
    assert expiring[0]["user_id"] == 2 and expiring[0]["total_days"] == 0


def test_reminder_configuration_and_state_rollbacks(isolated_storage):
    dm.user_data.pop("system_settings", None)
    with patch.object(DataManager, "save_user_data", return_value=True):
        assert DataManager.set_expiry_reminder_days(5)
    assert DataManager.get_expiry_reminder_days() == 5
    dm.user_data.pop("system_settings")
    assert DataManager.get_expiry_reminder_days() == 3
    with patch.object(DataManager, "save_user_data", side_effect=RuntimeError):
        assert not DataManager.set_expiry_reminder_days(2)

    expiry = datetime.now() + timedelta(days=2)
    assert not DataManager.mark_expiry_reminder_sent(99, expiry, 2)
    dm.user_data[1] = {"subscription": {}}
    with patch.object(DataManager, "save_user_data", return_value=False):
        assert not DataManager.mark_expiry_reminder_sent(1, expiry, 2)
    assert "expiry_reminder" not in dm.user_data[1]["subscription"]
    dm.user_data[1]["subscription"]["expiry_reminder"] = {"old": True}
    with patch.object(DataManager, "save_user_data", return_value=False):
        assert not DataManager.mark_expiry_reminder_sent(1, expiry, 2)
    assert dm.user_data[1]["subscription"]["expiry_reminder"] == {"old": True}


@pytest.mark.parametrize(
    "payload",
    [[], {"schema_version": 99}, {"schema_version": 1}, {"schema_version": 1, "orders": []}],
)
def test_load_payment_orders_validates_schema_and_shape(isolated_storage, payload):
    isolated_storage.orders_file.write_text(json.dumps(payload), encoding="utf-8")
    assert not DataManager.load_payment_orders()
    assert not dm.payment_orders_load_succeeded


def test_load_payment_orders_valid_file_and_missing_save_failure(isolated_storage):
    isolated_storage.orders_file.write_text(
        json.dumps({"schema_version": 1, "orders": {"o": {}}}), encoding="utf-8"
    )
    with patch.object(DataManager, "recover_payment_fulfillments", return_value=True) as recover:
        assert DataManager.load_payment_orders()
    assert dm.payment_orders == {"o": {}}
    recover.assert_called_once_with()
    isolated_storage.orders_file.unlink()
    with patch.object(DataManager, "save_payment_orders", return_value=False):
        assert not DataManager.load_payment_orders()


def test_load_payment_orders_backup_failure_is_ignored(isolated_storage):
    isolated_storage.orders_file.write_text("invalid", encoding="utf-8")
    with patch("storage.data_manager.shutil.copy2", side_effect=OSError):
        assert not DataManager.load_payment_orders()


def test_save_payment_orders_guards_legacy_and_temp_cleanup(isolated_storage, tmp_path):
    dm.payment_orders_load_succeeded = False
    assert not DataManager.save_payment_orders({})
    dm.payment_orders_load_succeeded = True
    assert DataManager.save_payment_orders(None)
    assert json.loads(isolated_storage.orders_file.read_text(encoding="utf-8"))["orders"] == {}
    assert not DataManager.save_payment_orders({"old": {"type": "vip_purchase"}})

    temp = tmp_path / "orders.tmp"

    def fake_mkstemp(**kwargs):
        descriptor = __import__("os").open(temp, __import__("os").O_CREAT | __import__("os").O_WRONLY)
        return descriptor, str(temp)

    with patch("storage.data_manager.tempfile.mkstemp", side_effect=fake_mkstemp), patch(
        "storage.data_manager.os.replace", side_effect=OSError("replace")
    ):
        assert not DataManager.save_payment_orders({"o": {}})
    assert not temp.exists()


@pytest.mark.parametrize(
    "order",
    [
        None,
        {"processed": True},
        {"type": "other"},
        {"type": "subscription_purchase"},
        {"type": "subscription_purchase", "user_id": 1, "plan_id": "bad", "quota": 1, "coin": "USDT", "amount": 1},
        {"type": "subscription_purchase", "user_id": 1, "plan_id": "go", "quota": 0, "coin": "USDT", "amount": 1},
        {"type": "subscription_purchase", "user_id": 1, "plan_id": "go", "quota": 2, "coin": "BTC", "amount": 1},
        {"type": "subscription_purchase", "user_id": 1, "plan_id": "go", "quota": 2, "coin": "USDT", "amount": 0},
    ],
)
def test_fulfillment_rejects_invalid_order_shapes(isolated_storage, order):
    orders = {} if order is None else {"o": order}
    assert not DataManager.fulfill_subscription_payment("o", orders)


def test_fulfillment_applying_and_upgrade_manual_review_edges(isolated_storage):
    applying = {
        "type": "subscription_purchase",
        "user_id": 1,
        "plan_id": "go",
        "quota": 2,
        "coin": "USDT",
        "amount": 1,
        "fulfillment_state": "applying",
    }
    with patch.object(DataManager, "recover_payment_fulfillments", return_value=True) as recover:
        assert not DataManager.fulfill_subscription_payment("o", {"o": applying})
    recover.assert_called_once_with()

    upgrade = copy.deepcopy(applying)
    upgrade.pop("fulfillment_state")
    upgrade["billing_mode"] = "prorated_upgrade"
    upgrade["upgrade_snapshot"] = {"target_segments": [{}]}
    with patch.object(DataManager, "apply_prorated_upgrade", return_value=False), patch.object(
        DataManager, "save_payment_orders", return_value=True
    ):
        assert not DataManager.fulfill_subscription_payment("o", {"o": upgrade})
    assert upgrade["needs_manual_review"]


def test_recover_payment_fulfillments_corrupt_older_and_save_failures(isolated_storage):
    future = (datetime.now() + timedelta(days=10)).isoformat()
    later = (datetime.now() + timedelta(days=20)).isoformat()
    dm.user_data[1] = {"subscription": {"expires_at": later}, "keep": True}
    dm.payment_orders = {
        "skip": {},
        "corrupt": {"fulfillment_state": "applying"},
        "older": {
            "fulfillment_state": "applying",
            "fulfillment_user_id": 1,
            "fulfillment_user": {"subscription": {"expires_at": future}, "replace": True},
        },
        "new": {
            "fulfillment_state": "applying",
            "fulfillment_user_id": 2,
            "fulfillment_user": {"subscription": {"expires_at": future}},
        },
    }
    with patch.object(DataManager, "save_user_data", return_value=False):
        assert not DataManager.recover_payment_fulfillments()
    assert "replace" not in dm.user_data[1]
    with patch.object(DataManager, "save_user_data", return_value=True), patch.object(
        DataManager, "save_payment_orders", return_value=True
    ):
        assert DataManager.recover_payment_fulfillments()
    assert dm.payment_orders["new"]["processed"]
