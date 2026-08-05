# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from telethon.errors import AccessTokenInvalidError

import bot_main


def run(awaitable):
    return asyncio.run(awaitable)


def record(name, message, level=logging.WARNING):
    return logging.LogRecord(name, level, __file__, 1, message, (), None)


def test_formatter_and_log_filters(mutable_clock):
    formatter = bot_main.HighlightErrorFormatter("%(levelname)s:%(message)s")
    item = record("x", "boom", logging.ERROR)
    assert "!!! ERROR !!!" in formatter.format(item)
    assert item.levelname == "ERROR"

    assert not bot_main.SuppressTelethonOlderMessageFilter().filter(
        record("telethon.network.mtprotostate", "Server resent the older message")
    )
    assert not bot_main.SuppressTelethonMissingChannelHashFilter().filter(
        record("telethon.client.telegrambaseclient", "No access_hash in cache for channel X will not catch up")
    )
    transient = bot_main.SuppressTelethonTransientUpdateFilter()
    assert not transient.filter(record("telethon.x", "GetChannelDifferenceRequest"))
    assert not transient.filter(record("telethon.client.updates", "channel updates ending getting difference prematurely"))
    assert transient.filter(record("other", "normal"))

    msgid = record("telethon.client.users", "MsgidDecreaseRetryError")
    assert not bot_main.SuppressTelethonMsgidRetryFromConsoleFilter().filter(msgid)
    assert not bot_main.DowngradeTelethonMsgidRetryFilter(logging.INFO).filter(msgid)
    assert bot_main.DowngradeTelethonMsgidRetryFilter(logging.DEBUG).filter(msgid)
    assert msgid.levelno == logging.DEBUG

    limiter = bot_main.RateLimitTelethonSyncWarningsFilter(10)
    warning = record("telethon.network.mtprotosender", "connecting failed: TimeoutError")
    with patch("bot_main.time.monotonic", side_effect=[0, 1, 20]):
        assert limiter.filter(warning)
        assert not limiter.filter(warning)
        assert limiter.filter(warning)
    assert "抑制 1" in warning.getMessage()


@pytest.mark.parametrize(
    ("logger_name", "message"),
    [
        (
            "telethon.network.connection.connection",
            "Server closed the connection: [Errno 104] Connection reset by peer",
        ),
        (
            "telethon.network.connection.connection",
            "Server closed the connection: 0 bytes read on a total of 8 expected bytes",
        ),
        (
            "telethon.client.users",
            "Telegram is having internal issues RpcMcgetFailError: please try again "
            "later. (caused by GetAuthorizationsRequest)",
        ),
        (
            "telethon.client.updates",
            "Cannot get difference for channel 2617570183 since the account is not "
            "logged in: AuthKeyUnregisteredError",
        ),
    ],
)
def test_routine_telethon_warnings_are_suppressed(logger_name, message):
    warning_filter = bot_main.SuppressTelethonRoutineWarningFilter()
    assert not warning_filter.filter(record(logger_name, message))


def test_routine_warning_filter_keeps_unrelated_or_more_severe_events():
    warning_filter = bot_main.SuppressTelethonRoutineWarningFilter()
    unrelated = record(
        "telethon.client.users",
        "RpcMcgetFailError (caused by GetHistoryRequest)",
    )
    severe = record(
        "telethon.network.connection.connection",
        "Server closed the connection: Connection reset by peer",
        logging.ERROR,
    )
    assert warning_filter.filter(unrelated)
    assert warning_filter.filter(severe)


def test_configure_logging_and_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("BOT_LOG_FILE_LEVEL", "INFO")
    bot_main.configure_logging()
    root = logging.getLogger()
    assert len(root.handlers) == 2
    for handler in list(root.handlers):
        handler.close()
    root.handlers.clear()

    monkeypatch.setenv("BOT_BUILD_REVISION", "build-123")
    assert bot_main._build_revision() == "build-123"
    monkeypatch.delenv("BOT_BUILD_REVISION")
    with patch("bot_main.open", side_effect=OSError):
        assert bot_main._build_revision() == "unknown"


def test_process_lock_acquire_release(tmp_path):
    lock = bot_main.ProcessInstanceLock(str(tmp_path / "instance.lock"))
    assert lock.acquire()
    lock.release()
    lock.release()


@pytest.mark.parametrize("case", ["inactive", "overquota", "autoselect", "selection_suspend", "finite", "unlimited"])
def test_reconcile_subscription_paths(case):
    subscription = {
        "active": case != "inactive",
        "quota": None if case == "unlimited" else 1,
        "selected_accounts": ["1", "2"] if case == "overquota" else [],
        "selection_required": case == "selection_suspend",
    }
    phones = {"1", "2"} if case == "selection_suspend" else {"1"}
    with patch("bot_main.DataManager.get_subscription", return_value=subscription), patch(
        "bot_main.AccountManager.hosted_account_phones", return_value=phones
    ), patch("bot_main.DataManager.set_selected_accounts", return_value=True) as select, patch(
        "bot_main.AccountManager.suspend_user_accounts", new=AsyncMock()
    ) as suspend, patch(
        "bot_main.AccountManager.resume_selected_accounts", new=AsyncMock()
    ) as resume:
        run(bot_main._reconcile_user_subscription(1))
    if case in {"inactive", "selection_suspend"}:
        suspend.assert_awaited()
    if case == "inactive":
        resume.assert_not_awaited()
    elif case == "selection_suspend":
        resume.assert_not_awaited()
    else:
        resume.assert_awaited_once()


def test_runtime_termination_fatal_and_disconnect_error():
    async def scenario(fatal):
        bot_main.bot = SimpleNamespace(run_until_disconnected=AsyncMock())
        health = SimpleNamespace(error_type="Auth", reason="bad")
        with patch(
            "bot_main.account_runtime.wait_notify_bot_fatal",
            new=AsyncMock(return_value=health if fatal else None),
        ), patch("bot_main.asyncio.wait") as wait_mock:
            disconnected = asyncio.create_task(bot_main.bot.run_until_disconnected())
            fatal_task = asyncio.create_task(bot_main.account_runtime.wait_notify_bot_fatal())
            await asyncio.gather(disconnected, fatal_task)
            wait_mock.return_value = ({fatal_task} if fatal else {disconnected}, set())
            created = iter((disconnected, fatal_task))

            def use_existing(coroutine):
                coroutine.close()
                return next(created)

            with patch("bot_main.asyncio.create_task", side_effect=use_existing):
                with pytest.raises((bot_main.account_runtime.NotifyBotFatalError, RuntimeError)):
                    await bot_main._wait_for_runtime_termination()

    run(scenario(True))
    run(scenario(False))


def test_cleanup_all_resources():
    async def scenario():
        bot_main.subscription_task = asyncio.create_task(asyncio.sleep(10))
        bot_main.reminder_system = SimpleNamespace(stop_monitoring=AsyncMock())
        bot_main.payment_system = SimpleNamespace(stop_monitoring=AsyncMock())
        bot_main.bot = SimpleNamespace(disconnect=AsyncMock())
        client = object()
        runtime = SimpleNamespace(
            client_tasks={"a": 1},
            pause_tasks={"b": 1},
            code_fetch_tasks={"c": 1},
            close=AsyncMock(),
        )
        with patch("bot_main.DataManager.save_user_data"), patch(
            "bot_main.account_runtime.get_default_runtime", return_value=runtime
        ), patch("accounts.account_manager.user_accounts", {1: {"+1": {"client": client}}}), patch(
            "accounts.account_manager.AccountManager._safe_disconnect_client",
            new=AsyncMock(return_value=True),
        ):
            await bot_main.cleanup()
        assert bot_main.subscription_task is None
        runtime.close.assert_awaited_once()
        bot_main.bot.disconnect.assert_awaited_once()

    run(scenario())


def test_start_notify_bot_invalid_token():
    bot_main.bot = SimpleNamespace(start=AsyncMock(side_effect=AccessTokenInvalidError(None)))
    with patch("bot_main.account_runtime.raise_notify_bot_fatal") as fatal:
        run(bot_main._start_notify_bot())
    fatal.assert_called_once()


def test_main_startup_and_shutdown(tmp_path, monkeypatch):
    class Lock:
        def __init__(self, _path):
            self.released = False

        def acquire(self):
            return True

        def release(self):
            self.released = True

    bot = SimpleNamespace(send_message=AsyncMock())
    payment = SimpleNamespace(set_bot=Mock(), start_monitoring=AsyncMock())
    reminder = SimpleNamespace(start_monitoring=AsyncMock())
    cleanup = AsyncMock()
    monkeypatch.setattr(
        bot_main.DataManager, "get_user_language",
        staticmethod(lambda admin_id: "zh" if admin_id == 1 else "en"),
    )
    with patch.object(bot_main.config, "SESSIONS_DIR", str(tmp_path)), patch.object(
        bot_main.config, "BOT_SESSION_PATH", str(tmp_path / "bot.session"), create=True
    ), patch.object(bot_main.config, "ADMIN_IDS", [1, 2], create=True), patch.object(
        bot_main.config, "validate_runtime_settings"
    ), patch("bot_main.configure_logging"), patch("bot_main.ProcessInstanceLock", Lock), patch(
        "bot_main.DataManager.load_user_data", return_value=True
    ), patch("bot_main.AccountManager.recover_incomplete_account_transfers", return_value=True), patch(
        "bot_main.AccountManager.reconcile_historical_subscription_selections", return_value=True
    ), patch(
        "bot_main.AccountManager.cleanup_stale_pending_sessions",
        return_value=SimpleNamespace(ok=True, reason="", path=""),
    ), patch("bot_main.AdminAuditLog.prune", return_value=True), patch(
        "bot_main.TelegramClient", return_value=bot
    ), patch("bot_main.PaymentSystem", return_value=payment), patch(
        "bot_main.ReminderSystem", return_value=reminder
    ), patch("bot_main.setup_bot_handlers", new=AsyncMock()), patch(
        "bot_main.account_runtime.install_notify_bot_handler_guards", return_value=1
    ), patch("bot_main._start_notify_bot", new=AsyncMock()), patch(
        "bot_main.AccountManager.load_all_sessions", new=AsyncMock()
    ), patch("bot_main._wait_for_runtime_termination", new=AsyncMock(side_effect=RuntimeError("stop"))), patch(
        "bot_main.cleanup", new=cleanup
    ):
        with pytest.raises(RuntimeError, match="stop"):
            run(bot_main.main())
    cleanup.assert_awaited_once()
    payment.start_monitoring.assert_awaited_once()
    reminder.start_monitoring.assert_awaited_once()
    assert bot.send_message.await_count == 2
    assert "系统已完成初始化" in bot.send_message.await_args_list[0].args[1]
    assert "initialization complete" in bot.send_message.await_args_list[1].args[1]
    bot_main.subscription_task = None
