# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import json
from pathlib import Path

from migrate_runtime_data import (
    apply_migration,
    build_migration,
)


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_migration_is_idempotent_and_preserves_order_history(tmp_path):
    user_path = tmp_path / "user_data.json"
    order_path = tmp_path / "storage" / "payment_orders.json"
    metadata_path = tmp_path / "storage" / "hosted_account_metadata.json"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "8_123.session").write_bytes(b"session")

    _write(
        user_path,
        {
            "8": {
                "is_vip": True,
                "vip_expiry": "2999-01-01T00:00:00",
                "vip_added": "2025-01-01T00:00:00",
                "vip_days": 30,
            },
            "vip_prices": {"30": {"price": 1}},
            "payment_orders": {"embedded": {"status": "pending", "amount": "2"}},
            "system_settings": {"expiry_reminder_days": 3},
        },
    )
    _write(
        order_path,
        {
            "legacy": {
                "type": "vip_purchase",
                "vip_days": 30,
                "status": "paid",
                "amount": "3",
                "processed": True,
                "historical_note": "keep",
            }
        },
    )
    _write(
        metadata_path,
        {
            "version": 5,
            "accounts": {
                "8:123": {"pending_authorizations": {}},
                "9:456": {
                    "pending_authorizations": {},
                    "last_transferred_at": None,
                },
            },
        },
    )

    outputs, report = build_migration(
        user_path, order_path, metadata_path, sessions_dir
    )
    assert report["user_count"] == 1
    assert report["active_subscription_count"] == 1
    assert report["order_count"] == 2
    assert report["legacy_orders_converted"] == 1
    assert report["metadata_removed"] == 1
    assert outputs[metadata_path]["version"] == 6
    assert len(apply_migration(outputs)) == 3

    users = json.loads(user_path.read_text(encoding="utf-8"))
    assert users["schema_version"] == 1
    assert users["8"]["subscription"]["plan_id"] == "pro"
    assert not {"is_vip", "vip_expiry", "vip_added", "vip_days"} & users["8"].keys()

    stored_orders = json.loads(order_path.read_text(encoding="utf-8"))
    legacy = stored_orders["orders"]["legacy"]
    assert legacy["type"] == "subscription_purchase"
    assert legacy["legacy_origin"] == "vip_purchase"
    assert legacy["period_days"] == 30
    assert legacy["historical_note"] == "keep"
    assert legacy["amount"] == "3"
    assert legacy["status"] == "paid"

    backup_count = len(list(tmp_path.rglob("*.migration.backup")))
    outputs_again, report_again = build_migration(
        user_path, order_path, metadata_path, sessions_dir
    )
    assert report_again["changed_files"] == []
    assert apply_migration(outputs_again) == []
    assert len(list(tmp_path.rglob("*.migration.backup"))) == backup_count


def test_metadata_recovery_state_is_never_pruned(tmp_path):
    user_path = tmp_path / "user_data.json"
    order_path = tmp_path / "orders.json"
    metadata_path = tmp_path / "metadata.json"
    sessions_dir = tmp_path / "sessions"
    _write(user_path, {"schema_version": 1})
    _write(order_path, {"schema_version": 1, "orders": {}})
    _write(
        metadata_path,
        {
            "version": 5,
            "accounts": {
                "1:1": {"pending_authorizations": {"x": {}}},
                "2:2": {"last_transferred_at": 123},
                "3:3": {},
            },
        },
    )
    outputs, report = build_migration(
        user_path, order_path, metadata_path, sessions_dir
    )
    assert report["metadata_removed"] == 1
    assert outputs[metadata_path]["version"] == 6
    assert set(outputs[metadata_path]["accounts"]) == {"1:1", "2:2"}
