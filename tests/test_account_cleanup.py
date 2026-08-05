# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import asyncio
import os
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import functions

from accounts.account_manager import AccountManager
from accounts.models import AccountCleanupResult
from handlers.hosting_handlers import setup_hosting_handlers


class CleanupClient:
    def __init__(self, dialogs=None, contacts=None):
        self.dialogs = list(dialogs or [])
        self.contacts = contacts or SimpleNamespace(contacts=[], users=[])
        self.requests = []
        self.deleted_dialogs = []

    async def get_dialogs(self):
        return self.dialogs

    async def delete_dialog(self, entity):
        self.deleted_dialogs.append(entity)

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.contacts.GetContactsRequest):
            return self.contacts
        if isinstance(request, functions.contacts.DeleteContactsRequest):
            return SimpleNamespace()
        if isinstance(request, functions.contacts.BlockRequest):
            return True
        raise AssertionError(f"unexpected request: {request!r}")


class AccountCleanupOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_chats_delete_dialogs_and_block_only_bots(self):
        bot = SimpleNamespace(id=1, bot=True)
        user = SimpleNamespace(id=2, bot=False)
        client = CleanupClient(
            dialogs=[
                SimpleNamespace(id=1, entity=bot),
                SimpleNamespace(id=2, entity=user),
            ]
        )
        result = AccountCleanupResult(status="failed")

        with patch("accounts.account_manager.asyncio.sleep", new=AsyncMock()):
            await AccountManager._clean_hosted_account_operations(
                10, "+10001", client, "chats", result
            )

        self.assertEqual(result.chats_deleted, 2)
        self.assertEqual(client.deleted_dialogs, [bot, user])
        blocks = [r for r in client.requests if isinstance(r, functions.contacts.BlockRequest)]
        self.assertEqual(len(blocks), 1)
        self.assertIs(blocks[0].id, bot)

    async def test_contacts_are_deleted_in_one_batch(self):
        users = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        contacts = SimpleNamespace(
            contacts=[SimpleNamespace(user_id=1), SimpleNamespace(user_id=2)],
            users=users,
        )
        client = CleanupClient(contacts=contacts)
        result = AccountCleanupResult(status="failed")

        await AccountManager._clean_hosted_account_operations(
            10, "+10001", client, "contacts", result
        )

        self.assertEqual(result.contacts_deleted, 2)
        deletes = [
            r for r in client.requests if isinstance(r, functions.contacts.DeleteContactsRequest)
        ]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0].id, users)

    async def test_all_runs_categories_in_order(self):
        sequence = []

        class OrderedClient(CleanupClient):
            async def get_dialogs(self):
                sequence.append("chats")
                return []

            async def __call__(self, request):
                if isinstance(request, functions.contacts.GetContactsRequest):
                    sequence.append("contacts")
                return await super().__call__(request)

        result = AccountCleanupResult(status="failed")
        await AccountManager._clean_hosted_account_operations(
            10, "+10001", OrderedClient(), "all", result
        )
        self.assertEqual(sequence, ["chats", "contacts"])

    async def test_invalid_type_does_not_check_or_use_client(self):
        with patch.object(
            AccountManager, "ensure_hosted_client_ready", new=AsyncMock()
        ) as ready:
            result = await AccountManager.clean_hosted_account(10, "+10001", "invalid")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors, ["无效的清理类型"])
        ready.assert_not_awaited()

    async def test_timeout_keeps_partial_counts(self):
        async def slow_operations(_user_id, _phone, _client, _clean_type, result):
            result.chats_deleted = 1
            await asyncio.sleep(1)

        with patch.object(
            AccountManager, "_check_hosting_cooldown", return_value=None
        ), patch.object(
            AccountManager, "get_hosting_clean_remaining_seconds", return_value=0
        ), patch.object(
            AccountManager,
            "ensure_account_operable",
            return_value=(True, {}, "+10001", {}, ""),
        ), patch.object(
            AccountManager,
            "ensure_hosted_client_ready",
            new=AsyncMock(return_value=(True, {}, "+10001", {}, object(), "")),
        ), patch.object(
            AccountManager, "_clean_hosted_account_operations", side_effect=slow_operations
        ), patch("accounts.account_manager.HOSTING_CLEAN_TIMEOUT_SECONDS", 0.01):
            result = await AccountManager.clean_hosted_account(10, "+10001", "chats")

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.chats_deleted, 1)
        self.assertIn("超过 0.01 秒", result.errors[0])

    async def test_cooldown_prevents_readiness_check(self):
        with patch.object(
            AccountManager, "_check_hosting_cooldown", return_value="⏳ 请稍后再试"
        ), patch.object(
            AccountManager, "get_hosting_clean_remaining_seconds", return_value=0
        ), patch.object(
            AccountManager,
            "ensure_account_operable",
            return_value=(True, {}, "+10002", {}, ""),
        ), patch.object(
            AccountManager, "ensure_hosted_client_ready", new=AsyncMock()
        ) as ready:
            result = await AccountManager.clean_hosted_account(11, "+10002", "all")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors, ["⏳ 请稍后再试"])
        ready.assert_not_awaited()

    async def test_failed_readiness_returns_existing_operation_message(self):
        with patch.object(
            AccountManager, "_check_hosting_cooldown", return_value=None
        ), patch.object(
            AccountManager, "get_hosting_clean_remaining_seconds", return_value=0
        ), patch.object(
            AccountManager,
            "ensure_account_operable",
            return_value=(True, {}, "+10003", {}, ""),
        ), patch.object(
            AccountManager,
            "ensure_hosted_client_ready",
            new=AsyncMock(return_value=(False, {}, "+10003", None, None, "❌ 托管会话离线")),
        ):
            result = await AccountManager.clean_hosted_account(12, "+10003", "chats")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors, ["❌ 托管会话离线"])

    async def test_protocol_younger_than_one_hour_is_rejected_server_side(self):
        account = {"created_at": 10_000.0, "runtime_status": "online"}
        with patch.object(
            AccountManager,
            "ensure_account_operable",
            return_value=(True, {"+10004": account}, "+10004", account, ""),
        ), patch("accounts.account_manager.time.time", return_value=13_599.0), patch.object(
            AccountManager, "_check_hosting_cooldown"
        ) as cooldown, patch.object(
            AccountManager, "ensure_hosted_client_ready", new=AsyncMock()
        ) as ready:
            result = await AccountManager.clean_hosted_account(13, "+10004", "all")

        self.assertEqual(result.status, "failed")
        self.assertIn("协议创建未满 1 小时", result.errors[0])
        cooldown.assert_not_called()
        ready.assert_not_awaited()

    def test_protocol_exactly_one_hour_old_is_allowed(self):
        account = {"created_at": 10_000.0}
        with patch("accounts.account_manager.HOSTING_CLEAN_MIN_AGE_SECONDS", 3600):
            remaining = AccountManager.get_hosting_clean_remaining_seconds(
                13, "+10004", account, now=13_600.0
            )
        self.assertEqual(remaining, 0)

    async def test_connection_error_uses_hosted_operation_error_handler(self):
        result = AccountCleanupResult(status="failed")
        with patch.object(
            AccountManager,
            "handle_hosted_operation_error",
            new=AsyncMock(return_value="❌ 连接暂时不可用"),
        ) as handle:
            await AccountManager._record_cleanup_operation_error(
                result, 10, "+10001", object(), "获取对话失败", OSError("down")
            )

        handle.assert_awaited_once()
        self.assertEqual(result.errors, ["获取对话失败：❌ 连接暂时不可用"])


class CallbackBot:
    def __init__(self):
        self.callbacks = {}

    def on(self, _event):
        def register(callback):
            self.callbacks[callback.__name__] = callback
            return callback
        return register


def cleanup_event(data: bytes, pattern: bytes):
    return SimpleNamespace(
        sender_id=10,
        data=data,
        pattern_match=re.match(pattern, data),
        answer=AsyncMock(),
        edit=AsyncMock(),
    )


class AccountCleanupHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = CallbackBot()
        await setup_hosting_handlers(self.bot)

    async def test_clean_menu_contains_supported_choices(self):
        event = cleanup_event(b"hosting_clean_menu_10001", rb"^hosting_clean_menu_(\d+)$")
        account = {"display_phone": "+10 001", "runtime_status": "online"}
        with patch.object(AccountManager, "check_access", return_value=True), patch.object(
            AccountManager, "get_user_accounts", return_value={"+10001": account}
        ), patch.object(AccountManager, "is_account_online", return_value=True), patch.object(
            AccountManager, "get_hosting_clean_remaining_seconds", return_value=0
        ):
            await self.bot.callbacks["hosting_clean_menu"](event)

        buttons = event.edit.await_args.kwargs["buttons"]
        callback_data = [button.data for row in buttons for button in row]
        self.assertIn(b"hosting_clean_pick_chats_10001", callback_data)
        self.assertIn(b"hosting_clean_pick_contacts_10001", callback_data)
        self.assertIn(b"hosting_clean_pick_all_10001", callback_data)

    async def test_confirmation_runs_cleanup_and_renders_result(self):
        pattern = rb"^hosting_clean_confirm_(chats|contacts|all)_(\d+)$"
        event = cleanup_event(b"hosting_clean_confirm_all_10001", pattern)
        result = AccountCleanupResult(
            status="success",
            chats_deleted=3,
            contacts_deleted=2,
        )
        with patch.object(
            AccountManager, "clean_hosted_account", new=AsyncMock(return_value=result)
        ) as clean:
            await self.bot.callbacks["hosting_clean_confirm"](event)

        clean.assert_awaited_once_with(10, "+10001", "all")
        self.assertEqual(event.edit.await_count, 2)
        rendered = event.edit.await_args.args[0]
        self.assertIn("清理完成", rendered)
        self.assertIn("删除对话：3", rendered)

    async def test_offline_account_cannot_open_clean_menu(self):
        event = cleanup_event(b"hosting_clean_menu_10001", rb"^hosting_clean_menu_(\d+)$")
        account = {"display_phone": "+10 001", "runtime_status": "offline"}
        with patch.object(AccountManager, "check_access", return_value=True), patch.object(
            AccountManager, "get_user_accounts", return_value={"+10001": account}
        ), patch.object(AccountManager, "is_account_online", return_value=False):
            await self.bot.callbacks["hosting_clean_menu"](event)

        event.answer.assert_awaited_once_with("❌ 托管会话离线，无法清理", alert=True)
        event.edit.assert_not_awaited()

    async def test_young_protocol_cannot_open_clean_menu(self):
        event = cleanup_event(b"hosting_clean_menu_10001", rb"^hosting_clean_menu_(\d+)$")
        account = {"display_phone": "+10 001", "runtime_status": "online"}
        with patch.object(AccountManager, "check_access", return_value=True), patch.object(
            AccountManager, "get_user_accounts", return_value={"+10001": account}
        ), patch.object(AccountManager, "is_account_online", return_value=True), patch.object(
            AccountManager, "get_hosting_clean_remaining_seconds", return_value=1800
        ):
            await self.bot.callbacks["hosting_clean_menu"](event)

        event.answer.assert_awaited_once_with(
            "⏳ 协议创建未满 1 小时，清理功能将在 30 分钟后可用",
            alert=True,
        )
        event.edit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
