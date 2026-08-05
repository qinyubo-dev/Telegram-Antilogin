# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import admin_handlers
from handlers.admin_handlers import _queue_admin_action, _search_admin_users, _take_admin_action
from payments.payment_system import PaymentSystem
from storage import admin_audit
from storage.admin_audit import AdminAuditLog
from storage.user_profile_cache import UserProfileCache


class AdminAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp_dir.name, "audit.jsonl")
        self.path_patch = patch("storage.admin_audit.ADMIN_AUDIT_FILE", self.path)
        self.path_patch.start()
        AdminAuditLog._last_prune_date = None

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_attempt_result_query_and_redaction(self):
        audit_id = AdminAuditLog.record_attempt(
            7, "user.search", "user",
            metadata={
                "phone_masked": "+86 13800138000",
                "phone_hash": "abc123",
                "session": "secret-session",
            },
        )
        self.assertTrue(audit_id)
        self.assertTrue(AdminAuditLog.record_result(
            audit_id, "failed", target_id="8",
            before={"selected_accounts": ["8613800138000"]},
            error="failed for +86 13800138000 at https://example.test/pay",
        ))

        result = AdminAuditLog.query({"exclude_attempt": True}, page=0, page_size=25)
        self.assertEqual(result["total"], 1)
        entry = result["items"][0]
        self.assertEqual(entry["result"], "failed")
        self.assertNotIn("13800138000", entry["error"])
        self.assertNotIn("https://", entry["error"])
        self.assertNotIn("8613800138000", json.dumps(entry["before"]))

        history = AdminAuditLog.get_by_audit_id(audit_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["metadata"]["session"], "[REDACTED]")
        self.assertEqual(history[0]["metadata"]["phone_hash"], "abc123")

    def test_prune_keeps_invalid_lines_and_recent_records(self):
        old = {
            "audit_id": "old", "timestamp": (datetime.now() - timedelta(days=181)).isoformat()
        }
        recent = {"audit_id": "new", "timestamp": datetime.now().isoformat()}
        with open(self.path, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(old) + "\n")
            stream.write("not-json\n")
            stream.write(json.dumps(recent) + "\n")

        self.assertTrue(AdminAuditLog.prune(180))
        with open(self.path, encoding="utf-8") as stream:
            content = stream.read()
        self.assertNotIn('"old"', content)
        self.assertIn("not-json", content)
        self.assertIn('"new"', content)


class AdminConfirmationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp_dir.name, "audit.jsonl")
        self.path_patch = patch("storage.admin_audit.ADMIN_AUDIT_FILE", self.path)
        self.path_patch.start()
        admin_handlers._pending_admin_actions.clear()

    def tearDown(self):
        admin_handlers._pending_admin_actions.clear()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_token_is_owner_bound_and_single_use(self):
        pending = _queue_admin_action(7, "subscription.delete", 42, {}, {"active": True})
        missing, error = _take_admin_action(8, pending["token"])
        self.assertIsNone(missing)
        self.assertEqual(error, "missing")
        taken, error = _take_admin_action(7, pending["token"])
        self.assertEqual(taken["target_user_id"], 42)
        self.assertIsNone(error)
        replay, error = _take_admin_action(7, pending["token"])
        self.assertIsNone(replay)
        self.assertEqual(error, "missing")

    def test_expired_token_is_cancelled_and_removed(self):
        pending = _queue_admin_action(7, "subscription.delete", 42, {}, {"active": True})
        pending["created_at"] -= 301
        taken, error = _take_admin_action(7, pending["token"])
        self.assertIsNone(taken)
        self.assertEqual(error, "expired")
        result = AdminAuditLog.query({"exclude_attempt": True})
        self.assertEqual(result["items"][0]["result"], "cancelled")
        self.assertEqual(result["items"][0]["error"], "confirmation_expired")


class AdminOrderQueryTests(unittest.TestCase):
    def setUp(self):
        self.payment = PaymentSystem()
        now = time.time()
        self.payment.pending_orders = {
            "review": {"status": "paid", "processed": False, "created_time": now - 3, "user_id": 1},
            "manual": {"status": "pending", "needs_manual_review": True, "created_time": now - 2, "user_id": 2},
            "active": {"status": "pending", "processed": False, "created_time": now - 1, "user_id": 2},
            "done": {"status": "paid", "processed": True, "created_time": now, "user_id": 3},
            "closed": {"status": "expired", "processed": False, "created_time": now - 4, "user_id": 4},
        }

    def test_categories_and_newest_first(self):
        self.assertEqual(self.payment.list_admin_orders("review")["total"], 2)
        self.assertEqual(self.payment.list_admin_orders("active")["total"], 1)
        self.assertEqual(self.payment.list_admin_orders("completed")["total"], 1)
        self.assertEqual(self.payment.list_admin_orders("closed")["total"], 1)
        self.assertEqual(
            [item["order_id"] for item in self.payment.list_admin_orders("all")["items"]],
            ["done", "active", "manual", "review", "closed"],
        )

    def test_exact_search_and_snapshots_are_immutable(self):
        result = self.payment.list_admin_orders("all", query="2")
        self.assertEqual({item["order_id"] for item in result["items"]}, {"manual", "active"})
        snapshot = self.payment.get_order_snapshot("active")
        snapshot["status"] = "changed"
        self.assertEqual(self.payment.pending_orders["active"]["status"], "pending")

    def test_legacy_vip_orders_are_read_only_closed_or_completed(self):
        now = time.time()
        self.payment.pending_orders.update({
            "legacy-open": {
                "type": "subscription_purchase", "legacy_origin": "vip_purchase",
                "status": "paid", "processed": False,
                "created_time": now,
            },
            "legacy-done": {
                "type": "subscription_purchase", "legacy_origin": "vip_purchase",
                "status": "paid", "processed": True,
                "created_time": now,
            },
        })
        self.assertNotIn(
            "legacy-open",
            {item["order_id"] for item in self.payment.list_admin_orders("review")["items"]},
        )
        closed = self.payment.list_admin_orders("closed")["items"]
        self.assertTrue(next(item for item in closed if item["order_id"] == "legacy-open")["legacy_read_only"])
        self.assertIn(
            "legacy-done",
            {item["order_id"] for item in self.payment.list_admin_orders("completed")["items"]},
        )


class AdminReportTests(unittest.TestCase):
    def setUp(self):
        self.payment = PaymentSystem()
        self.now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        today = self.now.timestamp()
        two_days_ago = (self.now - timedelta(days=2)).timestamp()
        ten_days_ago = (self.now - timedelta(days=10)).timestamp()
        self.payment.pending_orders = {
            "new-usdt": {
                "type": "subscription_purchase", "user_id": 1, "status": "paid",
                "processed": True, "fulfilled_time": today, "amount": "1.25", "coin": "USDT",
            },
            "new-trx": {
                "type": "subscription_purchase", "user_id": 2, "status": "paid",
                "processed": True, "fulfilled_time": today, "amount": "2.5", "coin": "TRX",
            },
            "returning-old": {
                "type": "subscription_purchase", "user_id": 3, "status": "paid",
                "processed": True, "fulfilled_time": ten_days_ago, "amount": "1", "coin": "USDT",
            },
            "returning-today": {
                "type": "subscription_purchase", "user_id": 3, "status": "paid",
                "processed": True, "fulfilled_time": today, "amount": "3.75", "coin": "USDT",
            },
            "week-only": {
                "type": "subscription_purchase", "user_id": 4, "status": "paid",
                "processed": True, "fulfilled_time": two_days_ago, "amount": "4", "coin": "USDT",
            },
            "pending": {
                "type": "subscription_purchase", "user_id": 5, "status": "pending",
                "processed": False, "fulfilled_time": today, "amount": "99", "coin": "USDT",
            },
            "legacy": {
                "type": "subscription_purchase", "legacy_origin": "vip_purchase",
                "user_id": 6, "status": "paid",
                "processed": True, "fulfilled_time": today, "amount": "99", "coin": "USDT",
            },
            "test": {
                "type": "payment_test", "user_id": 7, "status": "paid",
                "processed": True, "fulfilled_time": today, "amount": "99", "coin": "USDT",
            },
        }

    def test_report_amounts_and_first_time_payers(self):
        today = self.payment.get_admin_report(1, now=self.now.timestamp())
        self.assertEqual(today["amounts"], {"TRX": "2.5", "USDT": "5"})
        self.assertEqual(today["new_paid_users"], 2)

        week = self.payment.get_admin_report(7, now=self.now.timestamp())
        self.assertEqual(week["amounts"]["USDT"], "9")
        self.assertEqual(week["new_paid_users"], 3)

        month = self.payment.get_admin_report(30, now=self.now.timestamp())
        self.assertEqual(month["new_paid_users"], 4)

    def test_report_rejects_unsupported_window(self):
        with self.assertRaises(ValueError):
            self.payment.get_admin_report(2, now=self.now.timestamp())


class AdminUserSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_path = os.path.join(self.temp_dir.name, "profiles.json")
        self.path_patch = patch("storage.user_profile_cache.PROFILE_CACHE_FILE", self.cache_path)
        self.path_patch.start()
        UserProfileCache._loaded = False
        UserProfileCache._profiles = {}
        UserProfileCache.set_profile(10, "Alice Chen", "Alice")
        UserProfileCache.set_profile(11, "Bob", "Builder")
        self.payment = PaymentSystem()
        self.payment.pending_orders = {}

    async def asyncTearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_id_username_name_and_full_phone(self):
        accounts = {10: {"+86 138-0013-8000": {}}}
        bot = SimpleNamespace(get_entity=AsyncMock())
        with patch("handlers.admin_handlers.user_accounts", accounts), patch(
            "handlers.admin_handlers.DataManager.get_all_user_ids", return_value=[10, 11]
        ), patch("handlers.admin_handlers.config.ADMIN_IDS", []):
            self.assertEqual((await _search_admin_users(bot, self.payment, "10"))[0]["user_id"], 10)
            self.assertEqual((await _search_admin_users(bot, self.payment, "@ALICE"))[0]["user_id"], 10)
            self.assertEqual((await _search_admin_users(bot, self.payment, "Alice C"))[0]["user_id"], 10)
            self.assertEqual((await _search_admin_users(bot, self.payment, "+8613800138000"))[0]["user_id"], 10)

    async def test_resolved_unknown_telegram_user_is_rejected(self):
        bot = SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace(
            id=999, first_name="Outside", last_name=None, username="outside"
        )))
        with patch("handlers.admin_handlers.user_accounts", {}), patch(
            "handlers.admin_handlers.DataManager.get_all_user_ids", return_value=[10]
        ), patch("handlers.admin_handlers.config.ADMIN_IDS", []):
            self.assertEqual(await _search_admin_users(bot, self.payment, "@outside"), [])


if __name__ == "__main__":
    unittest.main()
