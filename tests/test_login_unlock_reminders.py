# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import pytest

from accounts import account_runtime
from handlers import login_unlock_handlers
from reminders.login_unlock_reminder import (
    LoginUnlockReminderSystem,
    ReminderScheduleValidationError,
    beijing_text,
    parse_schedule_offsets,
)
from user_timezones import TIMEZONE_CHOICES, timezone_text
from storage import data_manager as dm
from storage.data_manager import DataManager


def run(awaitable):
    return asyncio.run(awaitable)


def test_cancel_login_unlock_flow_timeout_does_not_block():
    async def scenario():
        release = asyncio.Event()

        async def stubborn_probe():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        task = asyncio.create_task(stubborn_probe())
        await asyncio.sleep(0)
        login_unlock_handlers._login_unlock_probe_tasks[91] = task
        with patch.object(
            login_unlock_handlers, "LOGIN_UNLOCK_CANCEL_TIMEOUT_SECONDS", 0.0
        ):
            await login_unlock_handlers.cancel_login_unlock_flow(91)
        assert not task.done()
        release.set()
        await task

    run(scenario())


@pytest.fixture(autouse=True)
def restore_runtime_reminder_system():
    previous = account_runtime.get_login_unlock_reminder_system()
    yield
    account_runtime.set_login_unlock_reminder_system(previous)


@pytest.fixture
def reminder_storage(tmp_path, monkeypatch):
    previous_data = dm.user_data
    previous_loaded = dm.data_load_succeeded
    previous_index = dm.subscription_expiry_index
    monkeypatch.setattr(dm, "DATA_FILE", str(tmp_path / "users.json"))
    dm.user_data = DataManager._default_data()
    dm.data_load_succeeded = True
    dm.subscription_expiry_index = {}
    expiry = datetime.now() + timedelta(days=30)
    dm.user_data[1] = {
        "language": "zh",
        "subscription": {
            "plan_id": "go",
            "quota": 2,
            "starts_at": datetime.now().isoformat(),
            "expires_at": expiry.isoformat(),
        },
    }
    DataManager.rebuild_subscription_index()
    yield
    dm.user_data = previous_data
    dm.data_load_succeeded = previous_loaded
    dm.subscription_expiry_index = previous_index


@pytest.mark.parametrize(
    ("text", "count", "expected"),
    [
        ("2m", 1, [120]),
        ("60m 10m 30s", 3, [3600, 600, 30]),
        ("43200m 1m 1s", 3, [2592000, 60, 1]),
    ],
)
def test_parse_schedule_offsets(text, count, expected):
    assert parse_schedule_offsets(text, count) == expected


@pytest.mark.parametrize(
    ("text", "count", "code"),
    [
        ("1m", 2, "item_count"),
        ("30s 1m", 2, "seconds_last"),
        ("1m 60s", 2, "seconds_range"),
        ("43201m", 1, "minutes_range"),
        ("10m 10m", 2, "descending"),
        ("1m 2m", 2, "descending"),
        ("10", 1, "format"),
    ],
)
def test_parse_schedule_offsets_rejects_invalid_values(text, count, code):
    with pytest.raises(ReminderScheduleValidationError) as caught:
        parse_schedule_offsets(text, count)
    assert caught.value.code == code


def test_schedule_uses_official_seconds_beijing_time_and_independent_quota(reminder_storage):
    system = LoginUnlockReminderSystem(AsyncMock())
    now = datetime(2026, 8, 2, 4, 0, 0, tzinfo=timezone.utc)

    result = run(system.schedule(1, "+8613800000001", 600, now=now))
    assert result.status == "scheduled"
    assert result.unlock_at == now + timedelta(seconds=600)
    assert result.reminder_times == (now + timedelta(seconds=480),)
    assert beijing_text(result.unlock_at) == "2026-08-02 12:10:00"
    assert result.used == 1 and result.limit == 3

    updated = run(system.schedule(1, "+86 138 0000 0001", 900, now=now))
    assert updated.status == "scheduled" and updated.used == 1
    assert len(DataManager.get_login_unlock_reminders(1)) == 1

    assert run(system.schedule(1, "+8613800000002", 600, now=now)).status == "scheduled"
    assert run(system.schedule(1, "+8613800000003", 600, now=now)).status == "scheduled"
    full = run(system.schedule(1, "+8613800000004", 600, now=now))
    assert full.status == "full" and full.used == 3


def test_timezone_defaults_follow_language_and_explicit_choice_wins(reminder_storage):
    assert DataManager.get_user_timezone(1) == "Asia/Shanghai"
    dm.user_data[1]["language"] = "en"
    assert DataManager.get_user_timezone(1) == "Europe/London"

    assert DataManager.set_user_timezone(1, "America/Los_Angeles")
    dm.user_data[1]["language"] = "zh"
    assert DataManager.get_user_timezone(1) == "America/Los_Angeles"
    assert not DataManager.set_user_timezone(1, "Invalid/Timezone")


def test_timezone_text_formats_selected_region():
    value = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    assert timezone_text(value, "Asia/Shanghai") == "2026-01-02 20:00:00"
    assert timezone_text(value, "America/New_York") == "2026-01-02 07:00:00"


def test_timezone_selector_covers_24_global_zones():
    assert len(TIMEZONE_CHOICES) == 24
    assert len({name for _, name in TIMEZONE_CHOICES}) == 24


def test_timezone_selector_displays_raw_iana_names(handler_bot, event_factory):
    from handlers.login_unlock_handlers import setup_login_unlock_handlers

    run(setup_login_unlock_handlers(handler_bot))
    event = event_factory(sender_id=1, data=b"login_unlock_timezone")
    safe_edit = AsyncMock()
    with patch(
        "handlers.login_unlock_handlers.require_access",
        new=AsyncMock(return_value=True),
    ), patch(
        "handlers.login_unlock_handlers.DataManager.get_user_timezone",
        return_value="Europe/London",
    ), patch("handlers.login_unlock_handlers.safe_edit", new=safe_edit):
        run(handler_bot.find("login_unlock_timezone")(event))

    buttons = safe_edit.await_args.kwargs["buttons"]
    labels = [button.text.removeprefix("✓ ") for row in buttons[:-1] for button in row]
    assert labels == [timezone_name for _, timezone_name in TIMEZONE_CHOICES]


def test_short_wait_is_immediate_and_does_not_use_slot(reminder_storage):
    system = LoginUnlockReminderSystem(AsyncMock())
    now = datetime.now(timezone.utc)
    result = run(system.schedule(1, "+8613800000001", 120, now=now))
    assert result.status == "immediate"
    assert DataManager.get_login_unlock_reminders(1) == {}


def test_recalculate_applies_new_future_nodes_without_backfill(reminder_storage):
    system = LoginUnlockReminderSystem(AsyncMock())
    now = datetime.now(timezone.utc)
    run(system.schedule(1, "+8613800000001", 3600, now=now))
    dm.user_data["system_settings"]["login_unlock_reminder_schedule"] = {
        "count": 3,
        "offsets_seconds": [7200, 600, 30],
    }

    assert run(system.recalculate_all(now=now + timedelta(seconds=1)))
    record = next(iter(DataManager.get_login_unlock_reminders(1).values()))
    assert [node["offset_seconds"] for node in record["nodes"]] == [600, 30]


def test_due_delivery_removes_completed_record(reminder_storage):
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=object())
    system = LoginUnlockReminderSystem(bot)
    now = datetime.now(timezone.utc)
    dm.user_data["system_settings"]["login_unlock_reminder_schedule"] = {
        "count": 1,
        "offsets_seconds": [1],
    }
    run(system.schedule(1, "+8613800000001", 3, now=now))

    async def deliver():
        due, _ = await system._collect_due(now + timedelta(seconds=2, milliseconds=100))
        assert len(due) == 1
        sent = await system._send(due[0][0], due[0][2], due[0][3])
        await system._finish_delivery(due[0], sent, now + timedelta(seconds=2, milliseconds=200))

    run(deliver())
    assert DataManager.get_login_unlock_reminders(1) == {}
    bot.send_message.assert_awaited_once()


def test_failed_delivery_retries_and_inflight_cancel_is_rejected(reminder_storage):
    system = LoginUnlockReminderSystem(AsyncMock())
    now = datetime.now(timezone.utc)
    dm.user_data["system_settings"]["login_unlock_reminder_schedule"] = {
        "count": 1,
        "offsets_seconds": [60],
    }
    run(system.schedule(1, "+8613800000001", 180, now=now))

    async def fail_once():
        due, _ = await system._collect_due(now + timedelta(seconds=120, milliseconds=100))
        assert len(due) == 1
        assert not await system.remove(1, "+8613800000001")
        await system._finish_delivery(
            due[0], False, now + timedelta(seconds=120, milliseconds=200)
        )

    run(fail_once())
    record = next(iter(DataManager.get_login_unlock_reminders(1).values()))
    assert record["nodes"][0]["retry_at"] is not None


def test_expired_subscription_cancels_records(reminder_storage):
    system = LoginUnlockReminderSystem(AsyncMock())
    now = datetime.now(timezone.utc)
    run(system.schedule(1, "+8613800000001", 600, now=now))
    dm.user_data[1]["subscription"]["expires_at"] = (datetime.now() - timedelta(seconds=1)).isoformat()
    DataManager.rebuild_subscription_index()
    assert run(system.reconcile_user(1, now=now + timedelta(seconds=1)))
    assert DataManager.get_login_unlock_reminders(1) == {}


def test_automatic_authentication_schedules_flood_wait(reminder_storage):
    from telethon.errors import FloodWaitError
    from accounts.account_manager import AccountManager

    client = SimpleNamespace(
        connect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=False),
        send_code_request=AsyncMock(side_effect=FloodWaitError(None, capture=600)),
        session=SimpleNamespace(filename="pending.session"),
    )
    system = LoginUnlockReminderSystem(AsyncMock())
    account_runtime.set_login_unlock_reminder_system(system)
    with patch.object(AccountManager, "cleanup_incomplete_account", new=AsyncMock()):
        result = run(AccountManager.authenticate(client, "+8613800000001", 1))
    assert "🕒 解限时间：" in result
    assert "北京时间" not in result
    assert len(DataManager.get_login_unlock_reminders(1)) == 1


def test_main_menu_login_unlock_button_uses_requested_icon():
    from handlers.bot_handlers import main_menu_buttons

    with patch("handlers.bot_handlers.DataManager.get_user_language", return_value="zh"), patch(
        "handlers.bot_handlers.DataManager.is_admin", return_value=False
    ):
        buttons = [button for row in main_menu_buttons(1) for button in row]
    target = next(button for button in buttons if button.data == b"login_unlock_menu")
    assert target.text == "解限提醒"
    assert target.style.icon == 5778605968208170641


def test_login_unlock_add_button_uses_new_name_and_icon(handler_bot, event_factory):
    from handlers.login_unlock_handlers import setup_login_unlock_handlers

    class FakeSystem:
        async def list_records(self, _user_id):
            return []

        def quota_status(self, _user_id, _phone=""):
            return {"used": 0, "limit": 3, "existing": False, "full": False}

    account_runtime.set_login_unlock_reminder_system(FakeSystem())
    run(setup_login_unlock_handlers(handler_bot))
    event = event_factory(sender_id=1, data=b"login_unlock_menu")
    current_message = SimpleNamespace()
    event.get_message = AsyncMock(return_value=current_message)
    safe_edit = AsyncMock()
    with patch(
        "handlers.login_unlock_handlers.require_access",
        new=AsyncMock(return_value=True),
    ), patch(
        "handlers.login_unlock_handlers.cancel_pending_login_flow",
        new=AsyncMock(return_value=SimpleNamespace(ok=True)),
    ), patch("handlers.login_unlock_handlers.safe_edit", new=safe_edit):
        run(handler_bot.find("login_unlock_menu")(event))

    buttons = safe_edit.await_args.kwargs["buttons"]
    target = next(button for row in buttons for button in row if button.data == b"login_unlock_add")
    assert target.text == "添加"
    assert target.style.icon == 5775937998948404844


def test_login_unlock_list_opens_account_detail_before_cancel(
    handler_bot, event_factory
):
    from handlers.login_unlock_handlers import setup_login_unlock_handlers

    record = {
        "phone": "+29067278",
        "unlock_at": "2026-08-05T12:19:21+00:00",
        "nodes": [{
            "offset_seconds": 120,
            "remind_at": "2026-08-05T12:17:21+00:00",
        }],
    }

    class FakeSystem:
        async def list_records(self, _user_id):
            return [record]

        def quota_status(self, _user_id, _phone=""):
            return {"used": 1, "limit": 3, "existing": False, "full": False}

    account_runtime.set_login_unlock_reminder_system(FakeSystem())
    run(setup_login_unlock_handlers(handler_bot))
    safe_edit = AsyncMock()

    menu_event = event_factory(sender_id=1, data=b"login_unlock_menu")
    menu_event.get_message = AsyncMock(return_value=SimpleNamespace())
    with patch(
        "handlers.login_unlock_handlers.require_access",
        new=AsyncMock(return_value=True),
    ), patch(
        "handlers.login_unlock_handlers.cancel_pending_login_flow",
        new=AsyncMock(return_value=SimpleNamespace(ok=True)),
    ), patch(
        "handlers.login_unlock_handlers.DataManager.get_user_timezone",
        return_value="Asia/Shanghai",
    ), patch("handlers.login_unlock_handlers.safe_edit", new=safe_edit):
        run(handler_bot.find("login_unlock_menu")(menu_event))

        menu_buttons = safe_edit.await_args.kwargs["buttons"]
        account_button = next(
            button for row in menu_buttons for button in row
            if button.data == b"login_unlock_detail_29067278"
        )
        assert account_button.text == "📱 +29067278"
        assert not any(
            button.data.startswith(b"login_unlock_cancel_")
            for row in menu_buttons for button in row
        )

        detail_event = event_factory(
            sender_id=1, data=b"login_unlock_detail_29067278"
        )
        run(handler_bot.find("login_unlock_detail")(detail_event))

    detail_text = safe_edit.await_args.args[1]
    detail_buttons = safe_edit.await_args.kwargs["buttons"]
    assert "🕒 解限：2026-08-05 20:19:21" in detail_text
    assert "🔔 将在解限前 2 分钟提醒您" in detail_text
    assert detail_buttons[0][0].text == "取消提醒"
    assert detail_buttons[0][0].data == b"login_unlock_cancel_29067278"


def test_admin_reminder_button_names_and_unlock_child_has_no_icon(
    handler_bot, event_factory
):
    from handlers.admin_handlers import setup_admin_handlers

    run(setup_admin_handlers(handler_bot, None))
    event = event_factory(sender_id=1, data=b"admin_reminder_settings")
    safe_edit = AsyncMock()
    with patch(
        "handlers.admin_handlers.require_admin",
        new=AsyncMock(return_value=True),
    ), patch("handlers.admin_handlers.safe_edit", new=safe_edit):
        run(handler_bot.find("admin_reminder_settings")(event))

    buttons = safe_edit.await_args.kwargs["buttons"]
    target = next(
        button for row in buttons for button in row
        if button.data == b"admin_login_unlock_reminder_settings"
    )
    assert target.text == "登录解限提醒"
    assert target.style.icon is None

    from localization import t

    assert t("zh", "admin.panel.reminders") == "提醒设置"
    assert t("en", "admin.panel.reminders") == "Reminder settings"


def test_manual_handler_probes_phone_and_does_not_continue_login(
    handler_bot, event_factory
):
    from handlers.handler_utils import get_state
    from handlers.login_unlock_handlers import setup_login_unlock_handlers
    from accounts.account_manager import AccountManager

    class FakeSystem:
        async def list_records(self, _user_id):
            return []

        def quota_status(self, _user_id, _phone=""):
            return {"used": 0, "limit": 3, "existing": False, "full": False}

        async def remove(self, _user_id, _phone):
            return True

        async def reconcile_user(self, _user_id):
            return True

    account_runtime.set_login_unlock_reminder_system(FakeSystem())
    run(setup_login_unlock_handlers(handler_bot))
    add_event = event_factory(sender_id=1, data=b"login_unlock_add")
    with patch.object(AccountManager, "check_access", return_value=True):
        run(handler_bot.find("login_unlock_add")(add_event))
    assert get_state(1)["login_unlock_manual_phone"]

    status = SimpleNamespace(edit=AsyncMock())
    message = event_factory(
        sender_id=1,
        text="+8613800000001",
        respond=AsyncMock(return_value=status),
    )
    client = object()
    with patch.object(AccountManager, "check_access", return_value=True), patch.object(
        AccountManager, "create_new_client", new=AsyncMock(return_value=client)
    ) as create_client, patch.object(
        AccountManager,
        "probe_login_unlock",
        new=AsyncMock(return_value="probe complete"),
    ) as probe:
        run(handler_bot.find("login_unlock_manual_phone")(message))
    create_client.assert_awaited_once_with("+8613800000001", 1)
    probe.assert_awaited_once_with(client, "+8613800000001", 1)
    status.edit.assert_awaited_once()
    message.delete.assert_awaited_once()
    assert not get_state(1)


def test_admin_login_unlock_schedule_input_is_saved_and_recalculated(
    handler_bot, event_factory
):
    from handlers.admin_handlers import handle_admin_message, setup_admin_handlers
    from handlers.handler_utils import get_state

    system = SimpleNamespace(recalculate_all=AsyncMock(return_value=True))
    account_runtime.set_login_unlock_reminder_system(system)
    run(setup_admin_handlers(handler_bot, None))
    count_event = event_factory(sender_id=1, data=b"admin_login_unlock_count_3")
    with patch("handlers.admin_handlers.DataManager.is_admin", return_value=True):
        run(handler_bot.find("admin_login_unlock_count")(count_event))
    assert get_state(1)["admin_login_unlock_reminder_count"] == 3

    input_event = event_factory(sender_id=1, text="60m 10m 30s")
    with patch("handlers.admin_handlers.DataManager.is_admin", return_value=True), patch(
        "handlers.admin_handlers.DataManager.get_login_unlock_reminder_schedule",
        side_effect=[
            {"count": 1, "offsets_seconds": [120]},
            {"count": 3, "offsets_seconds": [3600, 600, 30]},
        ],
    ), patch(
        "handlers.admin_handlers.DataManager.set_login_unlock_reminder_schedule",
        return_value=True,
    ) as save, patch(
        "handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"
    ), patch(
        "handlers.admin_handlers._audit_result", return_value=True
    ):
        assert run(handle_admin_message(input_event, None, None))
    save.assert_called_once_with(3, [3600, 600, 30])
    system.recalculate_all.assert_awaited_once()
