# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import asyncio
import logging
import os
import sqlite3
import tempfile
from logging.handlers import TimedRotatingFileHandler
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon.errors import (
    AccessTokenInvalidError,
    ApiIdInvalidError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    FreshResetAuthorisationForbiddenError,
    FrozenMethodInvalidError,
    QueryIdInvalidError,
    SessionRevokedError,
    UserIsBlockedError,
)

from accounts import account_runtime
from accounts.account_manager import AccountManager, HOSTED_SESSION_CLIENT_KWARGS
import bot_main
from bot_main import (
    ProcessInstanceLock,
    RateLimitTelethonSyncWarningsFilter,
    SanitizeTelethonProtocolErrorFilter,
    SuppressTelethonTransientUpdateFilter,
)
from reminders.reminder_system import ReminderSystem
from handlers.handler_utils import edit_or_respond
from payments.payment_system import PaymentSystem
from storage import data_manager
from storage.data_manager import DataManager


class FakeBot:
    def __init__(self, error=None):
        self.error = error
        self.messages = []

    async def send_message(self, user_id, message):
        if self.error:
            raise self.error
        self.messages.append((user_id, message))


def expiring_user(user_id=123):
    return {
        "user_id": user_id,
        "days_left": 1,
        "expiry": datetime.now() + timedelta(days=1),
    }


class AccountRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_guard_keeps_running_after_expired_callback_answer(self):
        continued = False

        async def callback(event):
            nonlocal continued
            await event.answer()
            continued = True

        event = SimpleNamespace(
            sender_id=123,
            answer=AsyncMock(side_effect=QueryIdInvalidError(None)),
        )
        guarded = account_runtime._guard_notify_bot_handler(callback)
        await guarded(event)
        self.assertTrue(continued)

    async def asyncSetUp(self):
        account_runtime.set_notify_bot(object())

    async def test_safe_send_marks_healthy(self):
        sent = await AccountManager._safe_send_bot_message(
            FakeBot(), 123, "hello", "test"
        )
        self.assertTrue(sent)
        self.assertEqual(account_runtime.get_notify_bot_health().status, "healthy")

    async def test_startup_credential_errors_are_fatal_and_safely_logged(self):
        for error_type in (AccessTokenInvalidError, ApiIdInvalidError):
            with self.subTest(error_type=error_type.__name__):
                account_runtime.set_notify_bot(object())
                start_bot = type(
                    "StartBot", (), {"start": AsyncMock(side_effect=error_type(None))}
                )()
                with patch.object(bot_main, "bot", start_bot), patch.object(
                    bot_main, "BOT_TOKEN", "TOP-SECRET-BOT-TOKEN"
                ), self.assertLogs(bot_main.logger.name, level="CRITICAL") as captured:
                    with self.assertRaises(account_runtime.NotifyBotFatalError):
                        await bot_main._start_notify_bot()

                output = "\n".join(captured.output)
                self.assertIn(error_type.__name__, output)
                self.assertNotIn("TOP-SECRET-BOT-TOKEN", output)
                health = account_runtime.get_notify_bot_health()
                self.assertEqual(health.status, "fatal")
                self.assertEqual(health.error_type, error_type.__name__)

    async def test_startup_network_error_keeps_original_behavior(self):
        error = ConnectionError("retries exhausted")
        start_bot = type(
            "StartBot", (), {"start": AsyncMock(side_effect=error)}
        )()
        with patch.object(bot_main, "bot", start_bot):
            with self.assertRaises(ConnectionError) as raised:
                await bot_main._start_notify_bot()
        self.assertIs(raised.exception, error)

    async def test_readonly_pending_session_is_closed_and_can_be_discarded(self):
        session = SimpleNamespace(close=Mock())
        client = SimpleNamespace(
            disconnect=AsyncMock(
                side_effect=sqlite3.OperationalError(
                    "attempt to write a readonly database"
                )
            ),
            session=session,
        )

        result = await AccountManager._disconnect_pending_client(
            client, "incomplete-account:test"
        )

        self.assertTrue(result)
        session.close.assert_called_once_with()

    async def test_safe_send_propagates_fatal_authorization(self):
        for error_type in (SessionRevokedError, AuthKeyUnregisteredError):
            with self.subTest(error_type=error_type.__name__):
                account_runtime.set_notify_bot(object())
                bot = FakeBot(error_type(None))
                with self.assertRaises(account_runtime.NotifyBotFatalError):
                    await AccountManager._safe_send_bot_message(
                        bot, 123, "hello", "test"
                    )
                health = await asyncio.wait_for(
                    account_runtime.wait_notify_bot_fatal(), timeout=0.1
                )
                self.assertEqual(health.status, "fatal")
                self.assertEqual(health.error_type, error_type.__name__)

    async def test_main_waiter_turns_fatal_state_into_process_error(self):
        never_disconnect = asyncio.Event()

        class ConnectedBot:
            async def run_until_disconnected(self):
                await never_disconnect.wait()

        with patch.object(bot_main, "bot", ConnectedBot()):
            waiter = asyncio.create_task(bot_main._wait_for_runtime_termination())
            await asyncio.sleep(0)
            account_runtime.mark_notify_bot_fatal(SessionRevokedError(None))
            with self.assertRaises(account_runtime.NotifyBotFatalError):
                await waiter

    async def test_main_waiter_propagates_disconnection_error(self):
        class DisconnectedBot:
            async def run_until_disconnected(self):
                raise ConnectionError("retries exhausted")

        with patch.object(bot_main, "bot", DisconnectedBot()):
            with self.assertRaisesRegex(ConnectionError, "retries exhausted"):
                await bot_main._wait_for_runtime_termination()

    async def test_main_waiter_treats_clean_disconnect_as_process_error(self):
        class DisconnectedBot:
            async def run_until_disconnected(self):
                return None

        with patch.object(bot_main, "bot", DisconnectedBot()):
            with self.assertRaisesRegex(RuntimeError, "连接已终止"):
                await bot_main._wait_for_runtime_termination()

    async def test_fatal_state_is_sticky(self):
        account_runtime.mark_notify_bot_fatal(SessionRevokedError(None))
        account_runtime.mark_notify_bot_healthy()
        account_runtime.mark_notify_bot_degraded(ConnectionError("late"))
        self.assertEqual(account_runtime.get_notify_bot_health().status, "fatal")

    async def test_handler_guard_marks_inbound_update_healthy(self):
        first_builder = object()
        second_builder = object()

        async def first_handler(event):
            return event

        async def second_handler(event):
            return event

        bot = HandlerBot([
            (first_handler, first_builder),
            (second_handler, second_builder),
        ])
        self.assertEqual(account_runtime.install_notify_bot_handler_guards(bot), 2)
        wrapped, retained_builder = bot.registrations[0]
        marker = object()
        self.assertIs(await wrapped(marker), marker)
        self.assertIs(retained_builder, first_builder)
        self.assertIs(bot.registrations[1][1], second_builder)
        self.assertEqual(
            [callback.__name__ for callback, _ in bot.registrations],
            ["first_handler", "second_handler"],
        )
        self.assertEqual(account_runtime.get_notify_bot_health().status, "healthy")
        self.assertEqual(account_runtime.install_notify_bot_handler_guards(bot), 0)
        self.assertEqual(len(bot.registrations), 2)

    async def test_handler_guard_publishes_direct_event_fatal_error(self):
        async def handler(_event):
            raise SessionRevokedError(None)

        bot = HandlerBot([(handler, object())])
        account_runtime.install_notify_bot_handler_guards(bot)
        wrapped, _ = bot.registrations[0]
        with self.assertRaises(account_runtime.NotifyBotFatalError):
            await wrapped(object())
        health = await asyncio.wait_for(
            account_runtime.wait_notify_bot_fatal(), timeout=0.1
        )
        self.assertEqual(health.status, "fatal")

    async def test_edit_or_respond_does_not_swallow_unauthorized(self):
        event = type(
            "Event",
            (),
            {
                "edit": AsyncMock(side_effect=SessionRevokedError(None)),
                "respond": AsyncMock(),
            },
        )()
        with self.assertRaises(account_runtime.NotifyBotFatalError):
            await edit_or_respond(event, "message")
        event.respond.assert_not_awaited()


class HandlerBot:
    def __init__(self, registrations):
        self.registrations = list(registrations)

    def list_event_handlers(self):
        return list(self.registrations)

    def remove_event_handler(self, callback, event=None):
        before = len(self.registrations)
        self.registrations = [
            item for item in self.registrations if item[0] is not callback
        ]
        return before - len(self.registrations)

    def add_event_handler(self, callback, event):
        self.registrations.append((callback, event))


class MainBotStartupTests(unittest.IsolatedAsyncioTestCase):
    def test_periodic_main_bot_probe_is_removed(self):
        self.assertFalse(hasattr(bot_main, "_monitor_notify_bot_health"))
        self.assertFalse(hasattr(bot_main, "_probe_notify_bot_health"))
        self.assertFalse(hasattr(bot_main, "_validate_notify_bot_startup"))
        self.assertFalse(hasattr(bot_main, "bot_health_task"))

    def test_main_bot_reconnect_policy_is_explicit_and_bounded(self):
        self.assertEqual(
            bot_main.MAIN_BOT_CLIENT_KWARGS,
            {
                "auto_reconnect": True,
                "connection_retries": 5,
                "retry_delay": 3,
                "catch_up": False,
            },
        )


class PaymentFatalPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        account_runtime.set_notify_bot(object())

    async def test_admin_notification_does_not_swallow_fatal_error(self):
        payment = PaymentSystem()
        payment.bot = FakeBot(SessionRevokedError(None))
        payment.pending_orders = {"order": {"type": "test"}}
        with patch("payments.payment_system.config.ADMIN_IDS", [123]):
            with self.assertRaises(account_runtime.NotifyBotFatalError):
                await payment._notify_admins("message", "notified", "order")
        self.assertEqual(account_runtime.get_notify_bot_health().status, "fatal")


class ReminderSystemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        account_runtime.set_notify_bot(object())
        self.original_user_data = data_manager.user_data
        self.expiring = expiring_user()
        data_manager.user_data = {
            self.expiring["user_id"]: {
                "subscription": {
                    "plan_id": "pro",
                    "quota": None,
                    "expires_at": self.expiring["expiry"].isoformat(),
                }
            }
        }
        self.data_patches = (
            patch(
                "reminders.reminder_system.DataManager.get_expiry_reminder_days",
                return_value=3,
            ),
            patch(
                "reminders.reminder_system.DataManager.get_expiring_subscription_users",
                return_value=[self.expiring],
            ),
            patch.object(DataManager, "save_user_data", return_value=True),
        )
        for item in self.data_patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.data_patches):
            item.stop()
        data_manager.user_data = self.original_user_data
        DataManager.rebuild_subscription_index()

    async def test_permanent_user_failure_does_not_make_bot_fatal(self):
        reminders = ReminderSystem(FakeBot(UserIsBlockedError(None)))
        failed = await reminders.check_expiring_vip()
        self.assertFalse(failed)
        self.assertEqual(account_runtime.get_notify_bot_health().status, "starting")
        self.assertFalse(reminders.failed_reminder_cooldowns)
        self.assertEqual(len(reminders.sent_reminders), 1)

    async def test_transient_failure_uses_global_backoff(self):
        reminders = ReminderSystem(FakeBot(ConnectionError("network down")))
        failed = await reminders.check_expiring_vip()
        self.assertTrue(failed)
        self.assertGreater(reminders.global_retry_after, datetime.now().timestamp())
        self.assertFalse(reminders.failed_reminder_cooldowns)
        self.assertEqual(account_runtime.get_notify_bot_health().status, "degraded")

    async def test_flood_wait_uses_server_delay(self):
        reminders = ReminderSystem(FakeBot(FloodWaitError(None, capture=7)))
        before = datetime.now().timestamp()
        failed = await reminders.check_expiring_vip()
        self.assertTrue(failed)
        self.assertGreaterEqual(reminders.global_retry_after, before + 7)

    async def test_fatal_bot_error_is_not_stored_as_user_cooldown(self):
        reminders = ReminderSystem(FakeBot(SessionRevokedError(None)))
        with self.assertRaises(account_runtime.NotifyBotFatalError):
            await reminders.check_expiring_vip()
        self.assertFalse(reminders.failed_reminder_cooldowns)
        self.assertFalse(reminders.sent_reminders)
        self.assertEqual(account_runtime.get_notify_bot_health().status, "fatal")

    async def test_start_monitoring_is_idempotent(self):
        gate = asyncio.Event()
        reminders = ReminderSystem(FakeBot())
        reminders._monitor_reminders = AsyncMock(side_effect=lambda: gate.wait())
        first = await reminders.start_monitoring()
        second = await reminders.start_monitoring()
        self.assertIs(first, second)
        await reminders.stop_monitoring()

    async def test_successful_reminder_is_not_repeated_after_restart(self):
        first_bot = FakeBot()
        first = ReminderSystem(first_bot)
        self.assertFalse(await first.check_expiring_vip())
        self.assertEqual(len(first_bot.messages), 1)

        restarted_bot = FakeBot()
        restarted = ReminderSystem(restarted_bot)
        self.assertFalse(await restarted.check_expiring_vip())
        self.assertEqual(restarted_bot.messages, [])


class SyncResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        account_runtime.hosting_action_cooldowns.clear()

    async def test_flood_wait_extends_action_cooldown(self):
        before = datetime.now().timestamp()
        message = await AccountManager.handle_hosted_operation_error(
            42,
            "+123456789",
            object(),
            "set_2fa",
            FloodWaitError(None, capture=321),
        )
        key = "set_2fa_42_+123456789"
        self.assertGreaterEqual(
            account_runtime.hosting_action_cooldowns[key], before + 321
        )
        self.assertIn("321 秒后", message)
        blocked = AccountManager._check_hosting_cooldown(
            42, "+123456789", "set_2fa", 60
        )
        self.assertIn("操作过于频繁", blocked)

    async def test_fresh_session_reset_is_expected_and_keeps_session(self):
        with patch.object(
            AccountManager, "cleanup_invalid_hosted_session", new=AsyncMock()
        ) as cleanup:
            message = await AccountManager.handle_hosted_operation_error(
                42,
                "+123456789",
                object(),
                "kick_sessions",
                FreshResetAuthorisationForbiddenError(None),
            )
        cleanup.assert_not_awaited()
        self.assertIn("会话创建时间过短", message)

    async def test_frozen_hosted_operation_returns_clear_message_without_cleanup(self):
        with patch.object(
            AccountManager, "cleanup_invalid_hosted_session", new=AsyncMock()
        ) as cleanup:
            message = await AccountManager.handle_hosted_operation_error(
                42,
                "+123456789",
                object(),
                "kick_sessions",
                FrozenMethodInvalidError(None),
            )
        cleanup.assert_not_awaited()
        self.assertIn("冻结", message)

    async def test_sqlite_locked_disconnect_retries(self):
        client = SimpleNamespace(
            disconnect=AsyncMock(
                side_effect=[sqlite3.OperationalError("database is locked"), None]
            )
        )
        with patch("accounts.account_manager.asyncio.sleep", new=AsyncMock()) as sleep:
            self.assertTrue(
                await AccountManager._safe_disconnect_client(client, "test")
            )
        self.assertEqual(client.disconnect.await_count, 2)
        sleep.assert_awaited_once()

    async def test_readonly_disconnect_force_closes_sqlite_handle(self):
        session = SimpleNamespace(close=Mock())
        client = SimpleNamespace(
            disconnect=AsyncMock(
                side_effect=sqlite3.OperationalError(
                    "attempt to write a readonly database"
                )
            ),
            session=session,
        )

        self.assertTrue(await AccountManager._safe_disconnect_client(client, "hosted"))
        session.close.assert_called_once_with()

    async def test_missing_session_recovery_marks_offline_without_rebuild(self):
        phone = "+123456789"
        client = SimpleNamespace(disconnect=AsyncMock())
        account_runtime.user_accounts[42] = {
            phone: {
                "client": client,
                "original_session_path": "renamed-away.session",
            }
        }
        try:
            with patch(
                "accounts.account_manager.os.path.exists", return_value=False
            ), patch.object(
                AccountManager, "mark_hosted_session_offline", new=AsyncMock(return_value=True)
            ) as offline, patch.object(
                AccountManager, "notify_hosted_session_offline", new=AsyncMock()
            ) as notify, patch.object(
                AccountManager, "create_client_from_session", new=AsyncMock()
            ) as rebuild:
                result = await AccountManager._recover_hosted_client_once(
                    42, phone, client, "OperationalError"
                )
            self.assertEqual(result, "missing_session")
            offline.assert_awaited_once()
            notify.assert_awaited_once()
            rebuild.assert_not_awaited()
        finally:
            account_runtime.user_accounts.pop(42, None)

    async def test_session_install_stops_when_temporary_client_stays_busy(self):
        temporary_client = object()
        with patch.object(AccountManager, "check_access", return_value=True), patch(
            "accounts.account_manager.TelegramClient", return_value=temporary_client
        ), patch.object(
            AccountManager,
            "validate_client_session",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "me": SimpleNamespace(phone="123456789"),
                }
            ),
        ), patch.object(
            AccountManager, "_safe_disconnect_client", new=AsyncMock(return_value=False)
        ), patch(
            "accounts.account_manager.shutil.move"
        ) as move:
            result = await AccountManager.create_client_from_session(
                "busy.session", 42, detailed=True
            )
        self.assertEqual(result, (None, "+123456789", False, "session_busy"))
        move.assert_not_called()

    async def test_protocol_recovery_rebuilds_exactly_once(self):
        phone = "+123456789"
        failed_client = SimpleNamespace(disconnect=AsyncMock())
        account_runtime.user_accounts[42] = {
            phone: {
                "client": failed_client,
                "original_session_path": "recover.session",
                "health_status": "alive",
                "source": "upload",
            }
        }
        try:
            with patch(
                "accounts.account_manager.os.path.exists", return_value=True
            ), patch.object(
                AccountManager, "_safe_disconnect_client", new=AsyncMock(return_value=True)
            ), patch.object(
                AccountManager,
                "create_client_from_session",
                new=AsyncMock(return_value=(object(), phone, True, None)),
            ) as rebuild:
                self.assertTrue(
                    await AccountManager._recover_protocol_session_once(
                        42, phone, failed_client, "TypeNotFoundError"
                    )
                )
            rebuild.assert_awaited_once()
        finally:
            account_runtime.user_accounts.pop(42, None)

    def test_process_instance_lock_rejects_second_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "instance.lock")
            first = ProcessInstanceLock(path)
            second = ProcessInstanceLock(path)
            self.assertTrue(first.acquire())
            try:
                self.assertFalse(second.acquire())
            finally:
                first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_runtime_log_rotates_daily(self):
        bot_main.configure_logging()
        try:
            handlers = [
                handler
                for handler in logging.getLogger().handlers
                if isinstance(handler, TimedRotatingFileHandler)
            ]
            self.assertEqual(len(handlers), 1)
            handler = handlers[0]
            self.assertEqual(handler.when, "MIDNIGHT")
            self.assertEqual(handler.interval, 24 * 60 * 60)
            self.assertEqual(handler.backupCount, 30)
            self.assertEqual(handler.suffix, "%Y-%m-%d")
            self.assertFalse(handler.utc)
            self.assertEqual(
                handler.baseFilename,
                str(bot_main.os.path.abspath(bot_main.config.BOT_LOG_DIR + "/bot_runtime.log")),
            )
        finally:
            for handler in logging.getLogger().handlers[:]:
                logging.getLogger().removeHandler(handler)
                handler.close()

    def test_hosted_clients_disable_full_catch_up(self):
        self.assertIs(HOSTED_SESSION_CLIENT_KWARGS["catch_up"], False)

    def test_runtime_has_no_session_rebuild_registry(self):
        self.assertFalse(hasattr(account_runtime.get_default_runtime(), "recovery_tasks"))
        self.assertFalse(hasattr(AccountManager, "_offline_recovery_loop"))
        self.assertFalse(hasattr(AccountManager, "_recover_offline_session_once"))

    async def test_transient_hosted_operation_does_not_force_offline(self):
        client = object()
        with patch.object(
            AccountManager,
            "mark_hosted_session_offline",
            new=AsyncMock(),
        ) as mark_offline:
            message = await AccountManager.handle_hosted_operation_error(
                123,
                "+123456789",
                client,
                "test_action",
                ConnectionError("temporary network failure"),
            )
        mark_offline.assert_not_awaited()
        self.assertIn("暂时不可用", message)

    async def test_active_client_probe_does_not_force_manual_reconnect(self):
        class ActiveClient:
            def is_connected(self):
                return True

            async def is_user_authorized(self):
                raise asyncio.TimeoutError()

        with patch.object(
            AccountManager,
            "_reconnect_client_for_retry",
            new=AsyncMock(),
        ) as reconnect:
            result = await AccountManager.validate_client_session(
                ActiveClient(),
                "+123456789",
                retry_attempts=2,
                reconnect_on_transient=False,
            )
        reconnect.assert_not_awaited()
        self.assertEqual(result["status"], "timeout")

    async def test_reconnect_backfill_failure_does_not_escape_callback(self):
        original_callback = AsyncMock()
        sender = type("Sender", (), {"_auto_reconnect_callback": original_callback})()
        client = type("Client", (), {"_sender": sender})()

        with patch.object(
            AccountManager,
            "_backfill_login_messages",
            new=AsyncMock(side_effect=ConnectionError("temporary")),
        ):
            self.assertTrue(AccountManager._install_reconnect_backfill(client, "+123", 1))
            await sender._auto_reconnect_callback()
        original_callback.assert_awaited_once()

    async def test_new_account_monitoring_skips_immediate_backfill(self):
        original_callback = AsyncMock()
        sender = type("Sender", (), {"_auto_reconnect_callback": original_callback})()

        class FakeClient:
            def __init__(self):
                self._sender = sender

            def on(self, _event):
                return lambda handler: handler

        client = FakeClient()
        backfill = AsyncMock()
        with patch.object(AccountManager, "_backfill_login_messages", new=backfill):
            self.assertTrue(
                await AccountManager.setup_monitoring(
                    client, "+123", 1, backfill_recent=False
                )
            )

        backfill.assert_not_awaited()
        self.assertTrue(client._login_reconnect_backfill_installed)

        with patch.object(AccountManager, "_backfill_login_messages", new=backfill):
            await sender._auto_reconnect_callback()
        backfill.assert_awaited_once_with(client, "+123", 1, source="reconnect")

    async def test_restored_account_monitoring_keeps_startup_backfill(self):
        sender = type("Sender", (), {"_auto_reconnect_callback": AsyncMock()})()

        class FakeClient:
            def __init__(self):
                self._sender = sender

            def on(self, _event):
                return lambda handler: handler

        client = FakeClient()
        backfill = AsyncMock()
        with patch.object(AccountManager, "_backfill_login_messages", new=backfill):
            self.assertTrue(await AccountManager.setup_monitoring(client, "+456", 2))

        backfill.assert_awaited_once_with(client, "+456", 2, source="startup")

    def test_sync_warning_filter_suppresses_and_summarizes_repeats(self):
        warning_filter = RateLimitTelethonSyncWarningsFilter(1800)

        def record(
            logger_name="telethon.network.connection.connection",
            message="Server closed the connection: Connection reset by peer",
        ):
            return logging.LogRecord(
                logger_name,
                logging.WARNING,
                __file__,
                1,
                message,
                (),
                None,
            )

        self.assertTrue(warning_filter.filter(record()))
        self.assertFalse(warning_filter.filter(record()))
        warning_filter._states[
            ("telethon.network.connection.connection", "ConnectionResetByPeer")
        ] = (-warning_filter.window_seconds - 1.0, 1)
        summarized = record()
        self.assertTrue(warning_filter.filter(summarized))
        self.assertIn("已抑制 1 条", summarized.getMessage())

    def test_type_not_found_filter_removes_raw_payload_and_rate_limits(self):
        sanitizer = SanitizeTelethonProtocolErrorFilter()
        limiter = RateLimitTelethonSyncWarningsFilter(1800)
        message = (
            "Could not find a matching Constructor ID 3ae56482. "
            "Remaining bytes: b'sensitive-chat-data'"
        )
        first = logging.LogRecord(
            "telethon.client.updates", logging.WARNING, __file__, 1, message, (), None
        )
        repeated = logging.LogRecord(
            "telethon.client.updates", logging.WARNING, __file__, 1, message, (), None
        )

        self.assertTrue(sanitizer.filter(first))
        self.assertNotIn("sensitive-chat-data", first.getMessage())
        self.assertIn("Raw payload omitted", first.getMessage())
        self.assertTrue(limiter.filter(first))
        sanitizer.filter(repeated)
        self.assertFalse(limiter.filter(repeated))

    def test_transient_update_filter_suppresses_non_actionable_failures(self):
        warning_filter = SuppressTelethonTransientUpdateFilter()

        def record(logger_name, message):
            return logging.LogRecord(
                logger_name,
                logging.WARNING,
                __file__,
                1,
                message,
                (),
                None,
            )

        targets = (
            "Telegram is having internal issues HistoryGetFailedError: "
            "Fetching of history failed (caused by GetChannelDifferenceRequest)",
            "Telegram is having internal issues RpcMcgetFailError "
            "(caused by GetChannelDifferenceRequest)",
            "PersistentTimestampOutdatedError: Persistent timestamp outdated "
            "(caused by GetChannelDifferenceRequest)",
            "ServerError: RPCError -500: No workers running "
            "(caused by GetChannelDifferenceRequest)",
            "UnexpectedChannelDifferenceError "
            "(caused by GetChannelDifferenceRequest)",
        )
        for target in targets:
            with self.subTest(message=target):
                self.assertFalse(
                    warning_filter.filter(record("telethon.client.users", target))
                )

        updates_target = (
            "Getting difference for channel updates 123 caused ValueError; "
            "ending getting difference prematurely until server issues are resolved"
        )
        self.assertFalse(
            warning_filter.filter(record("telethon.client.updates", updates_target))
        )

        non_targets = (
            (
                "telethon.client.users",
                "HistoryGetFailedError (caused by GetHistoryRequest)",
            ),
            ("accounts.account_manager", targets[0]),
        )
        for logger_name, message in non_targets:
            with self.subTest(logger_name=logger_name, message=message):
                self.assertTrue(warning_filter.filter(record(logger_name, message)))

    def test_sync_warning_filter_covers_upstream_failure_signatures(self):
        warning_filter = RateLimitTelethonSyncWarningsFilter(1800)
        cases = (
            (
                "telethon.network.mtprotosender",
                "Attempt 2 at connecting failed: TimeoutError: ",
            ),
            (
                "telethon.network.connection.connection",
                "Server closed the connection: [Errno 104] Connection reset by peer",
            ),
        )

        for logger_name, message in cases:
            with self.subTest(logger_name=logger_name, message=message):
                first = logging.LogRecord(
                    logger_name, logging.WARNING, __file__, 1, message, (), None
                )
                repeated = logging.LogRecord(
                    logger_name, logging.WARNING, __file__, 1, message, (), None
                )
                self.assertTrue(warning_filter.filter(first))
                self.assertFalse(warning_filter.filter(repeated))

    def test_sync_warning_filter_keeps_unrelated_and_fatal_errors(self):
        warning_filter = RateLimitTelethonSyncWarningsFilter(300)
        messages = (
            (
                "telethon.network.connection.connection",
                logging.WARNING,
                "Server closed the connection: 0 bytes read",
            ),
            (
                "accounts.account_runtime",
                logging.CRITICAL,
                "SessionRevokedError: authorization invalidated",
            ),
            (
                "telethon.client.telegrambaseclient",
                logging.ERROR,
                "sqlite3.OperationalError: attempt to write a readonly database",
            ),
        )

        for logger_name, level, message in messages:
            record = logging.LogRecord(
                logger_name, level, __file__, 1, message, (), None
            )
            self.assertTrue(warning_filter.filter(record))


if __name__ == "__main__":
    unittest.main()
