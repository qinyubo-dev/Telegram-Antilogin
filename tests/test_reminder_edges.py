# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telethon.errors import FloodWaitError, InputUserDeactivatedError, UserIsBlockedError

from reminders.reminder_system import ReminderFailureKind, ReminderSystem


def run(awaitable):
    return asyncio.run(awaitable)


def test_unreachable_classification():
    cases = [
        (UserIsBlockedError(None), "bot_blocked"),
        (InputUserDeactivatedError(None), "user_deactivated"),
        (RuntimeError("Could not find the input entity"), "entity_not_found"),
        (RuntimeError("specified user was deleted"), "user_deleted"),
        (RuntimeError("normal"), None),
    ]
    for error, reason in cases:
        assert ReminderSystem._classify_unreachable_user_error(error) == reason


def test_send_error_classification():
    with patch("reminders.reminder_system.account_runtime.is_notify_bot_fatal_error", return_value=True):
        assert ReminderSystem._classify_send_error(RuntimeError())[0] == ReminderFailureKind.BOT_FATAL
    assert ReminderSystem._classify_send_error(FloodWaitError(None, capture=3))[0] == ReminderFailureKind.FLOOD_WAIT
    assert ReminderSystem._classify_send_error(ConnectionError())[0] == ReminderFailureKind.TELEGRAM_TRANSIENT
    assert ReminderSystem._classify_send_error(RuntimeError())[0] == ReminderFailureKind.UNKNOWN


def expiring_user(user_id=1, days=1):
    return {"user_id": user_id, "days_left": days, "expiry": datetime.now() + timedelta(days=days)}


@pytest.mark.parametrize("case", ["today", "soon", "persist", "permanent", "temporary", "flood", "transient", "unknown"])
def test_check_expiring_vip_outcomes(case):
    error = None
    if case == "permanent":
        error = UserIsBlockedError(None)
    elif case == "temporary":
        error = RuntimeError("Could not find the input entity")
    elif case == "flood":
        error = FloodWaitError(None, capture=9)
    elif case == "transient":
        error = ConnectionError("down")
    elif case == "unknown":
        error = RuntimeError("boom")
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=error))
    system = ReminderSystem(bot)
    user = expiring_user(days=0 if case == "today" else 1)
    with patch("reminders.reminder_system.DataManager.get_expiry_reminder_days", return_value=3), patch(
        "reminders.reminder_system.DataManager.get_expiring_subscription_users", return_value=[user]
    ), patch("reminders.reminder_system.DataManager.was_expiry_reminder_sent", return_value=False), patch(
        "reminders.reminder_system.DataManager.mark_expiry_reminder_sent", return_value=case != "persist"
    ), patch("reminders.reminder_system.DataManager.delete_user_data", return_value=True), patch(
        "reminders.reminder_system.account_runtime.mark_notify_bot_healthy"
    ), patch("reminders.reminder_system.account_runtime.mark_notify_bot_degraded"):
        failed = run(system.check_expiring_vip())
    if case in {"today", "soon", "permanent", "temporary"}:
        assert not failed
    else:
        assert failed
    if case == "flood":
        assert system.global_retry_after > 0
    if case in {"temporary", "unknown"}:
        assert 1 in system.failed_reminder_cooldowns


def test_global_cooldown_duplicate_and_cleanup():
    bot = SimpleNamespace(send_message=AsyncMock())
    system = ReminderSystem(bot)
    system.global_retry_after = datetime.now().timestamp() + 100
    with patch("reminders.reminder_system.DataManager.get_expiry_reminder_days", return_value=3), patch(
        "reminders.reminder_system.DataManager.get_expiring_subscription_users", return_value=[expiring_user()]
    ):
        assert run(system.check_expiring_vip())
    bot.send_message.assert_not_awaited()

    system.sent_reminders = {"1_20000101", "bad"}
    system.failed_reminder_cooldowns = {1: 0, 2: datetime.now().timestamp() + 100}
    system._cleanup_old_reminders()
    assert system.sent_reminders == set()
    assert 1 not in system.failed_reminder_cooldowns


def test_monitoring_lifecycle_and_done_callback():
    async def scenario():
        system = ReminderSystem(object())
        with patch.object(system, "_monitor_reminders", new=AsyncMock(return_value=None)):
            task = await system.start_monitoring()
            assert await system.start_monitoring() is task
            await asyncio.sleep(0)
            system._monitoring_done(task)
        system.monitoring_task = asyncio.create_task(asyncio.sleep(10))
        await system.stop_monitoring()
        assert system.monitoring_task is None

    run(scenario())
