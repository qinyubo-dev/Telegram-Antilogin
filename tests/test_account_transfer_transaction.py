# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import asyncio
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import accounts.account_manager as account_manager_module
import storage.data_manager as data_manager_module
from accounts import account_runtime
from accounts.account_manager import AccountManager
from accounts.models import AccountTransferResult
from storage.data_manager import DataManager


class FakeClient:
    def __init__(self):
        self.connected = True
        self.calls = []

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def edit_2fa(self, **kwargs):
        self.calls.append(kwargs)


class AccountTransferTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.sessions = root / "sessions"
        self.sessions.mkdir()
        self.metadata = root / "hosted.json"
        self.journal = root / "transfer-journal.json"
        self.data_file = root / "user-data.json"
        self.patches = [
            patch.object(account_manager_module, "SESSIONS_DIR", str(self.sessions)),
            patch.object(account_manager_module, "HOSTED_ACCOUNT_METADATA_FILE", str(self.metadata)),
            patch.object(account_manager_module, "ACCOUNT_TRANSFER_JOURNAL_FILE", str(self.journal)),
            patch.object(data_manager_module, "DATA_FILE", str(self.data_file)),
        ]
        for item in self.patches:
            item.start()
        self.previous_loaded = data_manager_module.data_load_succeeded
        data_manager_module.data_load_succeeded = True
        self.previous_user_data = copy.deepcopy(data_manager_module.user_data)
        data_manager_module.user_data.clear()
        AccountManager._hosted_metadata = None
        account_runtime.user_accounts.clear()
        account_runtime.session_locks.clear()
        account_runtime.account_operation_locks.clear()
        self.from_user = 100
        self.to_user = 200
        self.phone = "+8613800138000"
        self.digits = "8613800138000"
        expiry = "2099-01-01T00:00:00"
        data_manager_module.user_data.update({
            self.from_user: {
                "subscription": {
                    "plan_id": "go", "quota": 2, "expires_at": expiry,
                    "selected_accounts": [self.digits], "selection_required": False,
                }
            },
            self.to_user: {
                "subscription": {
                    "plan_id": "plus", "quota": 10, "expires_at": expiry,
                    "selected_accounts": [], "selection_required": False,
                }
            },
        })
        DataManager.rebuild_subscription_index()

    async def asyncTearDown(self):
        tasks = list(account_runtime.pause_tasks.values()) + list(account_runtime.code_fetch_tasks.values())
        account_runtime.pause_tasks.clear()
        account_runtime.code_fetch_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        account_runtime.user_accounts.clear()
        account_runtime.session_locks.clear()
        account_runtime.account_operation_locks.clear()
        AccountManager._hosted_metadata = None
        data_manager_module.user_data.clear()
        data_manager_module.user_data.update(self.previous_user_data)
        data_manager_module.data_load_succeeded = self.previous_loaded
        DataManager.rebuild_subscription_index()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def install_source(self, *, mode=None):
        source_path = self.sessions / f"{self.from_user}_{self.digits}.session"
        source_path.write_bytes(b"session")
        client = FakeClient()
        info = {
            "client": client,
            "anti_login": True,
            "session_file": source_path.name,
            "original_session_path": str(source_path),
            "display_phone": self.phone,
            "runtime_status": "online",
        }
        if mode:
            info.update({"temporary_mode": mode, "temporary_until": time.time() + 600})
        account_runtime.user_accounts[self.from_user] = {self.phone: info}
        AccountManager._replace_hosted_metadata_records({
            AccountManager._hosted_metadata_key(self.from_user, self.phone): {
                "created_at": time.time() - 48 * 3600,
                "source": "login",
                "authorization_baseline_initialized": True,
                "known_authorization_hashes": ["1"],
                "pending_authorizations": {},
            }
        })
        return client, info, source_path

    async def test_success_moves_files_seats_and_uses_safe_reset(self):
        source_client, _, source_path = self.install_source(mode="pause")
        target_client = FakeClient()
        observed = {}

        async def create_target(path, user_id, **kwargs):
            observed.update(kwargs)
            account_runtime.user_accounts[user_id] = {
                self.phone: {
                    "client": target_client,
                    "anti_login": True,
                    "session_file": Path(path).name,
                    "original_session_path": path,
                    "runtime_status": "online",
                    "temporary_mode": "pause",
                }
            }
            return target_client, self.phone, True, "ok"

        ready = AccountTransferResult(True, "ready", "ready", self.phone, self.from_user, self.to_user)
        with patch.object(AccountManager, "validate_account_transfer", return_value=ready), patch.object(
            AccountManager, "create_client_from_session", new=AsyncMock(side_effect=create_target)
        ), patch.object(
            AccountManager, "_safe_disconnect_client", new=AsyncMock(return_value=True)
        ), patch.object(account_runtime, "get_notify_bot", return_value=None):
            result = await AccountManager.transfer_account(
                self.from_user, self.phone, self.to_user
            )

        self.assertTrue(result.ok)
        self.assertFalse(source_path.exists())
        self.assertTrue((self.sessions / f"{self.to_user}_{self.digits}.session").exists())
        self.assertEqual(observed["backfill_recent"], False)
        self.assertEqual(observed["ensure_selected"], False)
        target = account_runtime.user_accounts[self.to_user][self.phone]
        self.assertTrue(target["anti_login"])
        self.assertNotIn("temporary_mode", target)
        source_sub = DataManager.get_raw_subscription_snapshot(self.from_user)
        target_sub = DataManager.get_raw_subscription_snapshot(self.to_user)
        self.assertNotIn(self.digits, source_sub["selected_accounts"])
        self.assertIn(self.digits, target_sub["selected_accounts"])
        self.assertIsNone(AccountManager.get_hosted_account_metadata_record(self.from_user, self.phone))
        target_meta = AccountManager.get_hosted_account_metadata_record(self.to_user, self.phone)
        self.assertIsNotNone(target_meta)
        self.assertEqual(AccountManager._load_transfer_journal_locked()["transactions"], {})
        self.assertTrue(source_client.connected)

    async def test_disconnect_failure_restores_runtime_tasks_and_metadata(self):
        _, source_info, source_path = self.install_source(mode="pause")
        ready = AccountTransferResult(True, "ready", "ready", self.phone, self.from_user, self.to_user)
        with patch.object(AccountManager, "validate_account_transfer", return_value=ready), patch.object(
            AccountManager, "_safe_disconnect_client", new=AsyncMock(return_value=False)
        ), patch.object(AccountManager, "_start_connection_watcher_task") as watcher:
            result = await AccountManager.transfer_account(
                self.from_user, self.phone, self.to_user
            )

        self.assertEqual(result.code, "source_disconnect_failed")
        self.assertTrue(source_path.exists())
        self.assertEqual(source_info["temporary_mode"], "pause")
        self.assertIn(f"pause_{self.from_user}_{self.phone}", account_runtime.pause_tasks)
        watcher.assert_called_once()
        self.assertIsNotNone(AccountManager.get_hosted_account_metadata_record(self.from_user, self.phone))
        self.assertIsNone(AccountManager.get_hosted_account_metadata_record(self.to_user, self.phone))
        self.assertEqual(AccountManager._load_transfer_journal_locked()["transactions"], {})

    async def test_target_load_failure_rolls_back_code_fetch_state(self):
        _, _, source_path = self.install_source(mode="code_fetch")
        source_snapshot = DataManager.get_raw_subscription_snapshot(self.from_user)
        ready = AccountTransferResult(True, "ready", "ready", self.phone, self.from_user, self.to_user)

        async def create(path, user_id, **kwargs):
            if user_id == self.to_user:
                return None, self.phone, False, "monitoring_failed"
            client = FakeClient()
            account_runtime.user_accounts[user_id] = {
                self.phone: {
                    "client": client,
                    "anti_login": True,
                    "session_file": Path(path).name,
                    "original_session_path": path,
                    "runtime_status": "online",
                }
            }
            return client, self.phone, True, "ok"

        with patch.object(AccountManager, "validate_account_transfer", return_value=ready), patch.object(
            AccountManager, "create_client_from_session", new=AsyncMock(side_effect=create)
        ), patch.object(
            AccountManager, "_safe_disconnect_client", new=AsyncMock(return_value=True)
        ), patch.object(AccountManager, "_start_connection_watcher_task"):
            result = await AccountManager.transfer_account(
                self.from_user, self.phone, self.to_user
            )

        self.assertEqual(result.code, "target_load_failed")
        self.assertTrue(source_path.exists())
        source = account_runtime.user_accounts[self.from_user][self.phone]
        self.assertEqual(source["temporary_mode"], "code_fetch")
        self.assertIn(self.from_user, account_runtime.code_waiters[self.phone])
        self.assertIn(f"code_fetch_{self.from_user}_{self.phone}", account_runtime.code_fetch_tasks)
        self.assertEqual(DataManager.get_raw_subscription_snapshot(self.from_user), source_snapshot)
        self.assertEqual(AccountManager._load_transfer_journal_locked()["transactions"], {})

    async def test_metadata_commit_failure_restores_files_and_both_subscriptions(self):
        _, _, source_path = self.install_source()
        subscriptions = {
            self.from_user: DataManager.get_raw_subscription_snapshot(self.from_user),
            self.to_user: DataManager.get_raw_subscription_snapshot(self.to_user),
        }
        ready = AccountTransferResult(True, "ready", "ready", self.phone, self.from_user, self.to_user)
        original_replace = AccountManager._replace_hosted_metadata_records
        replace_calls = 0

        def replace(records):
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 2:
                return False
            return original_replace(records)

        async def create(path, user_id, **kwargs):
            client = FakeClient()
            account_runtime.user_accounts[user_id] = {
                self.phone: {
                    "client": client,
                    "anti_login": True,
                    "session_file": Path(path).name,
                    "original_session_path": path,
                    "runtime_status": "online",
                }
            }
            return client, self.phone, True, "ok"

        with patch.object(AccountManager, "validate_account_transfer", return_value=ready), patch.object(
            AccountManager, "create_client_from_session", new=AsyncMock(side_effect=create)
        ), patch.object(
            AccountManager, "_safe_disconnect_client", new=AsyncMock(return_value=True)
        ), patch.object(
            AccountManager, "_replace_hosted_metadata_records", side_effect=replace
        ), patch.object(AccountManager, "_start_connection_watcher_task"):
            result = await AccountManager.transfer_account(
                self.from_user, self.phone, self.to_user
            )

        self.assertEqual(result.code, "metadata_failed")
        self.assertTrue(source_path.exists())
        self.assertFalse((self.sessions / f"{self.to_user}_{self.digits}.session").exists())
        self.assertEqual(DataManager.get_raw_subscription_snapshot(self.from_user), subscriptions[self.from_user])
        self.assertEqual(DataManager.get_raw_subscription_snapshot(self.to_user), subscriptions[self.to_user])
        self.assertEqual(AccountManager._load_transfer_journal_locked()["transactions"], {})

    async def test_concurrent_subscription_change_is_not_overwritten_by_rollback(self):
        _, _, source_path = self.install_source()
        ready = AccountTransferResult(True, "ready", "ready", self.phone, self.from_user, self.to_user)

        async def create(path, user_id, **kwargs):
            client = FakeClient()
            account_runtime.user_accounts[user_id] = {
                self.phone: {
                    "client": client,
                    "anti_login": True,
                    "session_file": Path(path).name,
                    "original_session_path": path,
                    "runtime_status": "online",
                }
            }
            data_manager_module.user_data[self.to_user]["subscription"]["plan_id"] = "pro"
            data_manager_module.user_data[self.to_user]["subscription"]["quota"] = None
            self.assertTrue(DataManager.save_user_data())
            return client, self.phone, True, "ok"

        with patch.object(AccountManager, "validate_account_transfer", return_value=ready), patch.object(
            AccountManager, "create_client_from_session", new=AsyncMock(side_effect=create)
        ), patch.object(
            AccountManager, "_safe_disconnect_client", new=AsyncMock(return_value=True)
        ), patch.object(AccountManager, "_start_connection_watcher_task"):
            result = await AccountManager.transfer_account(
                self.from_user, self.phone, self.to_user
            )

        self.assertEqual(result.code, "subscription_state_changed")
        self.assertTrue(source_path.exists())
        current = DataManager.get_raw_subscription_snapshot(self.to_user)
        self.assertEqual(current["plan_id"], "pro")
        self.assertIsNone(current["quota"])

    async def test_transfer_waits_for_same_account_operation_lock(self):
        lock = AccountManager._get_account_operation_lock(self.from_user, self.phone)
        ready = AccountTransferResult(True, "done", "done")
        await lock.acquire()
        try:
            with patch.object(
                AccountManager, "_transfer_account_unlocked", new=AsyncMock(return_value=ready)
            ) as transfer:
                task = asyncio.create_task(AccountManager.transfer_account(
                    self.from_user, self.phone, self.to_user
                ))
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                transfer.assert_not_awaited()
                lock.release()
                result = await task
                self.assertTrue(result.ok)
                transfer.assert_awaited_once()
        finally:
            if lock.locked():
                lock.release()

    async def test_initial_journal_failure_does_not_mutate_account(self):
        client, _, source_path = self.install_source()
        ready = AccountTransferResult(
            True, "ready", "ready", self.phone, self.from_user, self.to_user
        )

        with patch.object(
            AccountManager, "validate_account_transfer", return_value=ready
        ), patch.object(
            AccountManager, "_upsert_transfer_transaction", return_value=False
        ), patch.object(
            AccountManager, "_safe_disconnect_client", new=AsyncMock()
        ) as disconnect:
            result = await AccountManager.transfer_account(
                self.from_user, self.phone, self.to_user
            )

        self.assertEqual(result.code, "journal_failed")
        self.assertTrue(source_path.exists())
        self.assertIs(account_runtime.user_accounts[self.from_user][self.phone]["client"], client)
        self.assertNotIn(self.to_user, account_runtime.user_accounts)
        disconnect.assert_not_awaited()

    async def test_queued_sensitive_operation_revalidates_ownership(self):
        client, _, _ = self.install_source()
        lock = AccountManager._get_account_operation_lock(self.from_user, self.phone)
        await lock.acquire()
        try:
            task = asyncio.create_task(AccountManager.set_hosted_2fa(
                self.from_user, self.phone, "new password"
            ))
            await asyncio.sleep(0)
            account_runtime.user_accounts[self.from_user].pop(self.phone)
            lock.release()
            message = await task
        finally:
            if lock.locked():
                lock.release()
        self.assertIn("不存在", message)
        self.assertEqual(client.calls, [])

    def test_startup_recovers_uncommitted_files_metadata_and_seats(self):
        _, _, source_path = self.install_source()
        target_path = self.sessions / f"{self.to_user}_{self.digits}.session"
        AccountManager._move_session_files(str(source_path), str(target_path))
        source_key = AccountManager._hosted_metadata_key(self.from_user, self.phone)
        target_key = AccountManager._hosted_metadata_key(self.to_user, self.phone)
        source_meta = AccountManager.get_hosted_account_metadata_record(self.from_user, self.phone)
        subscriptions = {
            self.from_user: DataManager.get_raw_subscription_snapshot(self.from_user),
            self.to_user: DataManager.get_raw_subscription_snapshot(self.to_user),
        }
        self.assertTrue(DataManager.transfer_selected_account(
            self.from_user, self.to_user, self.phone, subscriptions, [self.digits]
        ))
        self.assertTrue(AccountManager._replace_hosted_metadata_records({
            source_key: None,
            target_key: AccountManager._safe_transfer_target_metadata(source_meta, time.time()),
        }))
        transaction = {
            "id": "crash-test",
            "phase": "subscriptions_saved",
            "from_user_id": self.from_user,
            "to_user_id": self.to_user,
            "phone": self.phone,
            "source_path": str(source_path),
            "target_path": str(target_path),
            "metadata_snapshots": {source_key: source_meta, target_key: None},
            "subscription_snapshots": {str(k): v for k, v in subscriptions.items()},
        }
        self.assertTrue(AccountManager._upsert_transfer_transaction(transaction))

        self.assertTrue(AccountManager.recover_incomplete_account_transfers())

        self.assertTrue(source_path.exists())
        self.assertFalse(target_path.exists())
        self.assertIsNotNone(AccountManager.get_hosted_account_metadata_record(self.from_user, self.phone))
        self.assertIsNone(AccountManager.get_hosted_account_metadata_record(self.to_user, self.phone))
        self.assertEqual(
            DataManager.get_raw_subscription_snapshot(self.from_user), subscriptions[self.from_user]
        )
        self.assertEqual(AccountManager._load_transfer_journal_locked()["transactions"], {})

    def test_startup_finalizes_committed_transaction_without_rollback(self):
        target_path = self.sessions / f"{self.to_user}_{self.digits}.session"
        target_path.write_bytes(b"committed")
        source_path = self.sessions / f"{self.from_user}_{self.digits}.session"
        transaction = {
            "id": "committed-test",
            "phase": "committed",
            "source_path": str(source_path),
            "target_path": str(target_path),
        }
        self.assertTrue(AccountManager._upsert_transfer_transaction(transaction))

        self.assertTrue(AccountManager.recover_incomplete_account_transfers())

        self.assertTrue(target_path.exists())
        self.assertFalse(source_path.exists())
        self.assertEqual(AccountManager._load_transfer_journal_locked()["transactions"], {})

    def test_corrupt_primary_journal_falls_back_to_backup(self):
        transaction = {
            "id": "backup-test",
            "phase": "prepared",
            "from_user_id": self.from_user,
            "to_user_id": self.to_user,
            "phone": self.phone,
        }
        self.assertTrue(AccountManager._upsert_transfer_transaction(transaction))
        self.journal.write_text("{broken", encoding="utf-8")

        journal = AccountManager._load_transfer_journal_locked()

        self.assertEqual(journal["transactions"]["backup-test"]["phase"], "prepared")

    def test_reconcile_removes_historical_ghost_seat(self):
        data_manager_module.user_data[self.from_user]["subscription"]["selected_accounts"] = [
            self.digits, "999999"
        ]
        self.assertTrue(DataManager.reconcile_selected_accounts({
            self.from_user: [self.digits]
        }))
        selected = DataManager.get_raw_subscription_snapshot(self.from_user)["selected_accounts"]
        self.assertEqual(selected, [self.digits])


if __name__ == "__main__":
    unittest.main()
