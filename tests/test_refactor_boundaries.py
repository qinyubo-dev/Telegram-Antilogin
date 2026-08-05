# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from accounts import account_runtime
from accounts.account_runtime import AccountRuntime
from accounts.login_code_monitor import extract_sign_in_codes
from handlers.handler_utils import clear_state, get_state, require_access, set_state
from storage import data_manager
from storage.data_manager import (
    DataManager,
    PAYMENT_ORDERS_SCHEMA_VERSION,
    USER_DATA_SCHEMA_VERSION,
)


class LoginCodeTests(unittest.TestCase):
    def test_extracts_plain_and_hyphenated_codes(self):
        self.assertEqual(extract_sign_in_codes("Code 12345 or 1-2-3-4-5-6"), ["12345", "123456"])

    def test_ignores_parenthesized_identifier_and_wrong_lengths(self):
        self.assertEqual(extract_sign_in_codes("id (12345), 1234, 12345678"), [])


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_cancels_tasks_and_clears_registries(self):
        runtime = AccountRuntime()
        task = asyncio.create_task(asyncio.Event().wait())
        runtime.register_task(runtime.client_tasks, "account", task)
        await runtime.close()
        self.assertTrue(task.cancelled())
        self.assertEqual(runtime.client_tasks, {})

    async def test_registering_replacement_cancels_previous_task(self):
        runtime = AccountRuntime()
        first = asyncio.create_task(asyncio.Event().wait())
        second = asyncio.create_task(asyncio.Event().wait())
        runtime.register_task(runtime.client_tasks, "account", first)
        runtime.register_task(runtime.client_tasks, "account", second)
        await asyncio.sleep(0)
        self.assertTrue(first.cancelled())
        await runtime.close()

    async def test_access_is_blocked_until_hosted_sessions_finish_loading(self):
        event = SimpleNamespace(sender_id=7, respond=AsyncMock())
        account_runtime.mark_not_ready()
        with patch.object(DataManager, "get_user_language", return_value="zh"):
            self.assertFalse(await require_access(event))
        event.respond.assert_awaited_once()


class StateStoreTests(unittest.TestCase):
    def test_state_helpers_round_trip(self):
        set_state(7, flow="wait_login_code", phone="+12345", token="x")
        self.assertEqual(
            get_state(7),
            {"flow": "wait_login_code", "phone": "+12345", "token": "x"},
        )
        clear_state(7, "token")
        self.assertNotIn("token", get_state(7))
        clear_state(7)
        self.assertEqual(get_state(7), {})


class CurrentSchemaTests(unittest.TestCase):
    def test_current_schema_round_trip(self):
        old_data = data_manager.user_data
        old_loaded = data_manager.data_load_succeeded
        old_orders = data_manager.payment_orders
        old_orders_loaded = data_manager.payment_orders_load_succeeded
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "users.json")
                orders_path = os.path.join(directory, "payment_orders.json")
                payload = {
                    "schema_version": USER_DATA_SCHEMA_VERSION,
                    "42": {"name": "test"},
                    "subscription_catalog": DataManager.default_subscription_catalog(),
                    "subscription_periods": DataManager.default_subscription_periods(),
                    "system_settings": {"expiry_reminder_days": 3},
                }
                with open(path, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream)
                with open(orders_path, "w", encoding="utf-8") as stream:
                    json.dump(
                        {
                            "schema_version": PAYMENT_ORDERS_SCHEMA_VERSION,
                            "orders": {"order-1": {"status": "pending"}},
                        },
                        stream,
                    )
                with patch.object(data_manager, "DATA_FILE", path), patch.object(
                    data_manager, "PAYMENT_ORDERS_FILE", orders_path
                ):
                    self.assertTrue(DataManager.load_user_data())
                    self.assertEqual(data_manager.user_data[42]["name"], "test")
                    self.assertTrue(DataManager.save_user_data())
                with open(path, encoding="utf-8") as stream:
                    saved = json.load(stream)
                self.assertEqual(saved.get("42"), payload["42"])
                self.assertEqual(saved["schema_version"], USER_DATA_SCHEMA_VERSION)
                with open(orders_path, encoding="utf-8") as stream:
                    stored_orders = json.load(stream)
                self.assertEqual(
                    stored_orders,
                    {
                        "schema_version": PAYMENT_ORDERS_SCHEMA_VERSION,
                        "orders": {"order-1": {"status": "pending"}},
                    },
                )
        finally:
            data_manager.user_data = old_data
            data_manager.data_load_succeeded = old_loaded
            data_manager.payment_orders = old_orders
            data_manager.payment_orders_load_succeeded = old_orders_loaded
            DataManager.rebuild_subscription_index()

    def test_legacy_schema_is_rejected_without_startup_backup(self):
        old_data = data_manager.user_data
        old_loaded = data_manager.data_load_succeeded
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "users.json")
                with open(path, "w", encoding="utf-8") as stream:
                    json.dump({"42": {"is_vip": True}}, stream)
                with patch.object(data_manager, "DATA_FILE", path):
                    self.assertFalse(DataManager.load_user_data())
                self.assertEqual(
                    [name for name in os.listdir(directory) if name.endswith(".backup")],
                    [],
                )
        finally:
            data_manager.user_data = old_data
            data_manager.data_load_succeeded = old_loaded
            DataManager.rebuild_subscription_index()


if __name__ == "__main__":
    unittest.main()
