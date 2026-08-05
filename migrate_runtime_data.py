# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import settings


USER_DATA_SCHEMA_VERSION = 1
PAYMENT_ORDERS_SCHEMA_VERSION = 1
LEGACY_USER_FIELDS = {"is_vip", "vip_expiry", "vip_added", "vip_days"}


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return copy.deepcopy(default)
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} 顶层必须是 JSON 对象")
    return value


def migrate_users(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    source = copy.deepcopy(raw)
    embedded_orders = source.pop("payment_orders", {})
    source.pop("vip_prices", None)
    migrated = {
        "schema_version": USER_DATA_SCHEMA_VERSION,
        "subscription_catalog": source.get("subscription_catalog", {}),
        "subscription_periods": source.get("subscription_periods", {}),
        "system_settings": source.get("system_settings", {}),
    }
    migrated_users = 0
    user_count = 0
    active_count = 0
    now = datetime.now()
    for key, value in source.items():
        if key in {
            "schema_version",
            "subscription_catalog",
            "subscription_periods",
            "system_settings",
        }:
            continue
        try:
            int(key)
        except (TypeError, ValueError):
            raise ValueError(f"未知用户数据键: {key}")
        if not isinstance(value, dict):
            raise ValueError(f"用户 {key} 的数据必须是对象")
        user_count += 1
        user = copy.deepcopy(value)
        subscription = user.get("subscription")
        if not isinstance(subscription, dict):
            expiry_text = user.get("vip_expiry")
            try:
                expiry = datetime.fromisoformat(expiry_text) if expiry_text else None
            except (TypeError, ValueError):
                expiry = None
            if user.get("is_vip") and expiry and expiry > now:
                subscription = {
                    "plan_id": "pro",
                    "quota": None,
                    "starts_at": user.get("vip_added") or now.isoformat(),
                    "expires_at": expiry.isoformat(),
                    "selected_accounts": [],
                }
                user["subscription"] = subscription
                migrated_users += 1
        if isinstance(subscription, dict):
            subscription.pop("migrated_from_legacy", None)
            try:
                if datetime.fromisoformat(subscription["expires_at"]) > now:
                    active_count += 1
            except (KeyError, TypeError, ValueError):
                pass
        for field in LEGACY_USER_FIELDS:
            user.pop(field, None)
        migrated[str(int(key))] = user
    return migrated, {
        "user_count": user_count,
        "active_subscription_count": active_count,
        "legacy_users_converted": migrated_users,
        "embedded_orders": (
            embedded_orders if isinstance(embedded_orders, dict) else {}
        ),
    }


def migrate_orders(
    raw: dict[str, Any], embedded_orders: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    if raw.get("schema_version") == PAYMENT_ORDERS_SCHEMA_VERSION:
        source_orders = raw.get("orders")
        if not isinstance(source_orders, dict):
            raise ValueError("支付订单文件缺少 orders 对象")
        orders = copy.deepcopy(source_orders)
    else:
        orders = copy.deepcopy(raw)
    for order_id, order in (embedded_orders or {}).items():
        orders.setdefault(str(order_id), copy.deepcopy(order))

    before = {
        str(order_id): (
            order.get("amount"),
            order.get("status"),
        )
        for order_id, order in orders.items()
        if isinstance(order, dict)
    }
    converted = 0
    for order_id, order in orders.items():
        if not isinstance(order, dict):
            raise ValueError(f"订单 {order_id} 的数据必须是对象")
        if order.get("type") != "vip_purchase":
            continue
        period_days = order.pop("vip_days", order.get("period_days", 30))
        order.update(
            {
                "type": "subscription_purchase",
                "plan_id": "pro",
                "quota": None,
                "period_days": int(period_days),
                "legacy_origin": "vip_purchase",
            }
        )
        converted += 1

    after = {
        str(order_id): (
            order.get("amount"),
            order.get("status"),
        )
        for order_id, order in orders.items()
    }
    if before != after:
        raise ValueError("迁移改变了订单 ID、金额或状态")
    return {
        "schema_version": PAYMENT_ORDERS_SCHEMA_VERSION,
        "orders": orders,
    }, {
        "order_count": len(orders),
        "legacy_orders_converted": converted,
    }


def _session_account_keys(sessions_dir: Path) -> set[str]:
    keys = set()
    if not sessions_dir.exists():
        return keys
    for path in sessions_dir.glob("*.session"):
        parts = path.stem.split("_", 1)
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            keys.add(f"{int(parts[0])}:{parts[1]}")
    return keys


def prune_metadata(
    raw: dict[str, Any], session_keys: set[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    accounts = raw.get("accounts", {})
    if not isinstance(accounts, dict):
        raise ValueError("托管账户 metadata 缺少 accounts 对象")
    kept = {}
    removed = 0
    for key, value in accounts.items():
        if not isinstance(value, dict):
            kept[key] = value
            continue
        has_recovery_state = bool(
            value.get("pending_authorizations")
            or value.get("last_transferred_at")
        )
        if key in session_keys or has_recovery_state:
            kept[key] = value
        else:
            removed += 1
    result = copy.deepcopy(raw)
    result["version"] = 6
    result["accounts"] = kept
    return result, {
        "metadata_before": len(accounts),
        "metadata_kept": len(kept),
        "metadata_removed": removed,
    }


def build_migration(
    user_path: Path,
    order_path: Path,
    metadata_path: Path,
    sessions_dir: Path,
) -> tuple[dict[Path, dict[str, Any]], dict[str, Any]]:
    users_raw = _read_json(user_path, {})
    users, user_report = migrate_users(users_raw)
    orders_raw = _read_json(order_path, {})
    orders, order_report = migrate_orders(
        orders_raw, user_report.pop("embedded_orders")
    )
    metadata_raw = _read_json(metadata_path, {"version": 6, "accounts": {}})
    metadata, metadata_report = prune_metadata(
        metadata_raw, _session_account_keys(sessions_dir)
    )
    outputs = {
        user_path: users,
        order_path: orders,
        metadata_path: metadata,
    }
    changed = [
        str(path)
        for path, value in outputs.items()
        if _read_json(path, {}) != value
    ]
    report = {
        **user_report,
        **order_report,
        **metadata_report,
        "changed_files": changed,
    }
    return outputs, report


def _stage_json(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        return Path(temp_name)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def apply_migration(outputs: dict[Path, dict[str, Any]]) -> list[Path]:
    changed = {
        path: value
        for path, value in outputs.items()
        if _read_json(path, {}) != value
    }
    if not changed:
        return []
    staged = {path: _stage_json(path, value) for path, value in changed.items()}
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        for path in changed:
            if path.exists():
                backup = path.with_name(
                    f"{path.name}.{timestamp}.migration.backup"
                )
                suffix = 1
                while backup.exists():
                    backup = path.with_name(
                        f"{path.name}.{timestamp}.{suffix}.migration.backup"
                    )
                    suffix += 1
                shutil.copy2(path, backup)
        for path, temp_path in staged.items():
            os.replace(temp_path, path)
        return list(changed)
    finally:
        for temp_path in staged.values():
            temp_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将 Anti-Login 运行数据幂等迁移到当前 schema"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="只检查并输出迁移摘要")
    mode.add_argument("--apply", action="store_true", help="备份后应用迁移")
    args = parser.parse_args()

    outputs, report = build_migration(
        Path(settings.DATA_FILE),
        Path(settings.PAYMENT_ORDERS_FILE),
        Path(settings.HOSTED_ACCOUNT_METADATA_FILE),
        Path(settings.SESSIONS_DIR),
    )
    if args.apply:
        report["applied_files"] = [
            str(path) for path in apply_migration(outputs)
        ]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
