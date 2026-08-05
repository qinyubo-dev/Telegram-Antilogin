# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.errors import FrozenMethodInvalidError, HashInvalidError
from telethon.tl import functions, types

import accounts.account_manager as account_manager_module
from accounts.account_manager import AccountManager, user_accounts


def new_authorization(auth_hash, *, device="Desktop", location="Shanghai, China"):
    return types.UpdateNewAuthorization(
        hash=auth_hash,
        unconfirmed=True,
        date=datetime.now(timezone.utc),
        device=device,
        location=location,
    )


class FakeEventClient:
    def __init__(self):
        self.requests = []
        self.handlers = []
        self.missing_hashes = set()
        self.failing_hashes = set()
        self.frozen_hashes = set()

    def on(self, builder):
        def decorator(handler):
            self.handlers.append((builder, handler))
            return handler
        return decorator

    async def __call__(self, request):
        if isinstance(request, functions.account.GetAuthorizationsRequest):
            raise AssertionError("event-driven path must not query all authorizations")
        self.requests.append(request)
        auth_hash = getattr(request, "hash", None)
        if auth_hash in self.missing_hashes:
            raise HashInvalidError(request)
        if auth_hash in self.failing_hashes:
            raise ConnectionError("temporary failure")
        if auth_hash in self.frozen_hashes:
            raise FrozenMethodInvalidError(request)
        return True


class NewDeviceEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.metadata_path = str(Path(self.temp_dir.name) / "hosted.json")
        self.path_patch = patch.object(
            account_manager_module, "HOSTED_ACCOUNT_METADATA_FILE", self.metadata_path
        )
        self.path_patch.start()
        AccountManager._hosted_metadata = None
        AccountManager._authorization_locks.clear()
        user_accounts.clear()
        self.user_id = 123
        self.phone = "+8613800138000"

    def tearDown(self):
        user_accounts.clear()
        AccountManager._hosted_metadata = None
        AccountManager._authorization_locks.clear()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def install_account(self, client, *, age_seconds=0, anti_login=True, mode=None):
        created_at = time.time() - age_seconds
        AccountManager.set_hosted_account_created_at(self.user_id, self.phone, created_at)
        account = {
            "client": client,
            "anti_login": anti_login,
            "runtime_status": "online",
            "created_at": created_at,
            "display_phone": self.phone,
        }
        if mode:
            account.update({
                "temporary_mode": mode,
                "temporary_until": time.time() + 1800,
            })
        user_accounts[self.user_id] = {self.phone: account}
        return account

    async def test_active_protection_event_revokes_exact_hash_without_age_gate(self):
        client = FakeEventClient()
        self.install_account(client, age_seconds=0)
        update = new_authorization(101, device="New Phone")

        with patch.object(
            AccountManager, "_send_new_device_notice", new=AsyncMock(return_value=True)
        ) as notice:
            result = await AccountManager._process_new_authorization_update(
                client, update, self.phone, self.user_id
            )

        self.assertEqual(result, "rejected")
        self.assertEqual(len(client.requests), 1)
        self.assertIsInstance(client.requests[0], functions.account.ResetAuthorizationRequest)
        self.assertEqual(client.requests[0].hash, 101)
        notice.assert_awaited_once()

    async def test_confirmed_update_is_not_treated_as_a_new_login(self):
        client = FakeEventClient()
        self.install_account(client, age_seconds=48 * 60 * 60, mode="pause")
        AccountManager.save_hosted_authorization_state(
            self.user_id,
            self.phone,
            set(),
            {"103": {"device_name": "Pending device"}},
            initialized=True,
        )
        update = types.UpdateNewAuthorization(
            hash=103,
            unconfirmed=None,
            date=None,
            device=None,
            location=None,
        )

        with patch.object(
            AccountManager, "_send_new_device_notice", new=AsyncMock()
        ) as notice, patch.object(
            AccountManager, "_send_new_device_prompt", new=AsyncMock()
        ) as prompt:
            result = await AccountManager._process_new_authorization_update(
                client, update, self.phone, self.user_id
            )

        self.assertEqual(result, "confirmed")
        self.assertEqual(client.requests, [])
        notice.assert_not_awaited()
        prompt.assert_not_awaited()
        state = AccountManager.get_hosted_authorization_state(self.user_id, self.phone)
        self.assertIn("103", state["known_hashes"])
        self.assertEqual(state["pending"], {})

    async def test_inactive_modes_create_prompt_and_duplicate_is_ignored(self):
        for index, (anti_login, mode) in enumerate(
            ((True, "pause"), (False, None), (True, "code_fetch")), start=1
        ):
            with self.subTest(anti_login=anti_login, mode=mode):
                client = FakeEventClient()
                self.install_account(
                    client,
                    age_seconds=48 * 60 * 60,
                    anti_login=anti_login,
                    mode=mode,
                )
                update = new_authorization(200 + index)
                prompt_message = SimpleNamespace(id=300 + index)
                with patch.object(
                    AccountManager,
                    "_send_new_device_prompt",
                    new=AsyncMock(return_value=prompt_message),
                ) as prompt:
                    first = await AccountManager._process_new_authorization_update(
                        client, update, self.phone, self.user_id
                    )
                    user_accounts[self.user_id][self.phone].pop("temporary_mode", None)
                    user_accounts[self.user_id][self.phone].pop("temporary_until", None)
                    duplicate = await AccountManager._process_new_authorization_update(
                        client, update, self.phone, self.user_id
                    )

                self.assertEqual(first, "pending")
                self.assertEqual(duplicate, "duplicate")
                prompt.assert_awaited_once()
                self.assertEqual(client.requests, [])
                user_accounts.clear()
                AccountManager.remove_hosted_account_metadata(self.user_id, self.phone)

    async def test_allow_confirms_and_reject_revokes_without_query(self):
        client = FakeEventClient()
        self.install_account(client, age_seconds=48 * 60 * 60, mode="pause")
        AccountManager.save_hosted_authorization_state(
            self.user_id,
            self.phone,
            set(),
            {"301": {"device_name": "A"}, "302": {"device_name": "B"}},
            initialized=True,
        )

        allowed = await AccountManager.resolve_new_authorization(
            self.user_id, self.phone, "301", allow=True
        )
        rejected = await AccountManager.resolve_new_authorization(
            self.user_id, self.phone, "302", allow=False
        )
        duplicate = await AccountManager.resolve_new_authorization(
            self.user_id, self.phone, "302", allow=False
        )

        self.assertTrue(allowed["resolved"])
        self.assertTrue(rejected["resolved"])
        self.assertTrue(duplicate["resolved"])
        self.assertIsInstance(
            client.requests[0], functions.account.ChangeAuthorizationSettingsRequest
        )
        self.assertTrue(client.requests[0].confirmed)
        self.assertIsInstance(client.requests[1], functions.account.ResetAuthorizationRequest)
        state = AccountManager.get_hosted_authorization_state(self.user_id, self.phone)
        self.assertIn("301", state["known_hashes"])
        self.assertEqual(state["pending"], {})

    async def test_hash_invalid_is_resolved_as_expired(self):
        client = FakeEventClient()
        client.missing_hashes.add(401)
        self.install_account(client, age_seconds=48 * 60 * 60, mode="pause")
        AccountManager.save_hosted_authorization_state(
            self.user_id, self.phone, set(), {"401": {"device_name": "Gone"}}
        )

        result = await AccountManager.resolve_new_authorization(
            self.user_id, self.phone, "401", allow=False
        )

        self.assertTrue(result["resolved"])
        self.assertIn("失效", result["message"])
        state = AccountManager.get_hosted_authorization_state(self.user_id, self.phone)
        self.assertEqual(state["pending"], {})

    async def test_rpc_failure_keeps_pending_for_retry(self):
        client = FakeEventClient()
        client.failing_hashes.add(402)
        self.install_account(client, age_seconds=48 * 60 * 60, mode="pause")
        AccountManager.save_hosted_authorization_state(
            self.user_id, self.phone, set(), {"402": {"device_name": "Retry"}}
        )

        with patch("accounts.account_manager.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(ConnectionError):
                await AccountManager.resolve_new_authorization(
                    self.user_id, self.phone, "402", allow=False
                )

        state = AccountManager.get_hosted_authorization_state(self.user_id, self.phone)
        self.assertIn("402", state["pending"])

    async def test_frozen_action_is_not_retried_and_keeps_pending(self):
        client = FakeEventClient()
        client.frozen_hashes.add(403)
        self.install_account(client, age_seconds=48 * 60 * 60, mode="pause")
        AccountManager.save_hosted_authorization_state(
            self.user_id, self.phone, set(), {"403": {"device_name": "Frozen"}}
        )

        result = await AccountManager.resolve_new_authorization(
            self.user_id, self.phone, "403", allow=False
        )

        self.assertFalse(result["resolved"])
        self.assertIn("冻结", result["message"])
        self.assertEqual(len(client.requests), 1)
        state = AccountManager.get_hosted_authorization_state(self.user_id, self.phone)
        self.assertIn("403", state["pending"])

    async def test_raw_handler_registration_is_idempotent(self):
        client = FakeEventClient()
        self.assertTrue(
            AccountManager._install_new_authorization_handler(
                client, self.phone, self.user_id
            )
        )
        self.assertTrue(
            AccountManager._install_new_authorization_handler(
                client, self.phone, self.user_id
            )
        )
        self.assertEqual(len(client.handlers), 1)

    async def test_notification_uses_bounded_immediate_retries(self):
        with patch.object(
            AccountManager,
            "_safe_send_bot_message",
            new=AsyncMock(side_effect=[False, False, True]),
        ) as sender, patch(
            "accounts.account_manager.account_runtime.get_notify_bot",
            return_value=object(),
        ), patch("accounts.account_manager.asyncio.sleep", new=AsyncMock()) as sleep:
            sent = await AccountManager._send_new_device_notice(
                self.user_id,
                self.phone,
                {"device_name": "A", "location": "B"},
                "done",
                "test",
            )

        self.assertTrue(sent)
        self.assertEqual(sender.await_count, 3)
        self.assertEqual(sleep.await_count, 2)


if __name__ == "__main__":
    unittest.main()
