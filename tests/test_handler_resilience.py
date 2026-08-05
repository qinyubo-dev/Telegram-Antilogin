# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import unittest
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon.errors import (
    AuthTokenExpiredError,
    MessageDeleteForbiddenError,
    MessageIdInvalidError,
    MessageNotModifiedError,
    QueryIdInvalidError,
    SessionPasswordNeededError,
)

from accounts.models import AccountTransferResult
from handlers.account_handlers import (
    _classify_upload_file,
    cancel_pending_login_flow,
    delete_qr_message_strict,
    edit_status_or_send,
    finish_qr_login,
    setup_account_handlers,
)
from handlers.antilogin_handlers import setup_antilogin_handlers
from handlers.admin_handlers import setup_admin_handlers
from handlers import admin_handlers
from handlers.bot_handlers import setup_bot_handlers
from handlers.handler_utils import (
    clear_state,
    delete_prompt_message,
    delete_remembered_start_command,
    delete_sensitive_message,
    get_state,
    remember_start_command_message,
    safe_answer_callback,
    safe_edit,
    safe_edit_message,
    set_state,
)
from handlers.transfer_handlers import setup_transfer_handlers
from handlers.vip_handlers import setup_vip_handlers


class CallbackBot:
    def __init__(self):
        self.callbacks = {}
        self.edit_message = AsyncMock(
            side_effect=lambda _user_id, message, _text, **_kwargs: message
        )
        self.send_message = AsyncMock()

    def on(self, _event):
        def register(callback):
            self.callbacks[callback.__name__] = callback
            return callback

        return register


class SafeEditTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_callback_answer_is_ignored(self):
        event = SimpleNamespace(
            sender_id=123,
            message_id=456,
            answer=AsyncMock(side_effect=QueryIdInvalidError(None)),
        )
        with self.assertLogs("handlers.handler_utils", level="INFO"):
            self.assertFalse(await safe_answer_callback(event))

    async def test_message_not_modified_is_ignored(self):
        event = SimpleNamespace(
            edit=AsyncMock(side_effect=MessageNotModifiedError(None))
        )

        self.assertIsNone(await safe_edit(event, "unchanged"))
        event.edit.assert_awaited_once_with("unchanged")

    async def test_other_errors_are_propagated(self):
        event = SimpleNamespace(edit=AsyncMock(side_effect=ConnectionError("down")))

        with self.assertRaisesRegex(ConnectionError, "down"):
            await safe_edit(event, "new content")

    async def test_invalid_message_is_ignored_only_when_requested(self):
        event = SimpleNamespace(
            sender_id=123,
            message_id=456,
            edit=AsyncMock(side_effect=MessageIdInvalidError(None)),
        )

        with self.assertRaises(MessageIdInvalidError):
            await safe_edit(event, "new content")

        with self.assertLogs("handlers.handler_utils", level="INFO"):
            self.assertIsNone(
                await safe_edit(event, "new content", ignore_invalid=True)
            )
        self.assertEqual(event.edit.await_count, 2)

    async def test_safe_edit_message_returns_current_message_for_noop(self):
        current = SimpleNamespace(id=88)
        event = SimpleNamespace(
            edit=AsyncMock(side_effect=MessageNotModifiedError(None)),
            get_message=AsyncMock(return_value=current),
        )

        result = await safe_edit_message(event, "unchanged")

        self.assertIs(result, current)
        event.get_message.assert_awaited_once_with()

    async def test_safe_edit_message_propagates_other_errors(self):
        event = SimpleNamespace(
            edit=AsyncMock(side_effect=ConnectionError("down")),
            get_message=AsyncMock(),
        )
        with self.assertRaisesRegex(ConnectionError, "down"):
            await safe_edit_message(event, "new content")
        event.get_message.assert_not_awaited()


class MessageDeletionLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_input_failure_is_warning_with_error_type(self):
        event = SimpleNamespace(
            sender_id=123,
            id=456,
            delete=AsyncMock(side_effect=PermissionError("denied")),
        )

        with self.assertLogs("handlers.handler_utils", level="WARNING") as captured:
            result = await delete_sensitive_message(event, "2FA input")

        self.assertFalse(result)
        output = "\n".join(captured.output)
        self.assertIn("无法删除敏感输入", output)
        self.assertIn("用户ID=123", output)
        self.assertIn("消息ID=456", output)
        self.assertIn("异常类型=PermissionError", output)
        self.assertNotIn("denied", output)

    async def test_prompt_failure_is_info_with_error_type(self):
        event = SimpleNamespace(
            sender_id=123,
            message_id=789,
            delete=AsyncMock(side_effect=RuntimeError("gone")),
        )

        with self.assertLogs("handlers.handler_utils", level="INFO") as captured:
            result = await delete_prompt_message(event, "2FA prompt")

        self.assertFalse(result)
        output = "\n".join(captured.output)
        self.assertIn("INFO:handlers.handler_utils:提示消息无法删除", output)
        self.assertIn("消息ID=789", output)
        self.assertIn("异常类型=RuntimeError", output)
        self.assertNotIn("gone", output)

    async def test_invalid_status_message_warns_and_sends_replacement(self):
        message = SimpleNamespace(id=77)
        bot = SimpleNamespace(
            edit_message=AsyncMock(side_effect=MessageIdInvalidError(None)),
            send_message=AsyncMock(return_value="replacement"),
        )
        with self.assertLogs("handlers.account_handlers", level="WARNING") as captured:
            result = await edit_status_or_send(bot, 913, message, "new content")

        self.assertEqual(result, "replacement")
        bot.send_message.assert_awaited_once_with(913, "new content")
        output = "\n".join(captured.output)
        self.assertIn("user_id=913", output)
        self.assertIn("message_id=77", output)
        self.assertNotIn("Traceback", output)


class StartCommandReplacementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.user_id = 917
        self.bot = CallbackBot()
        await setup_bot_handlers(self.bot, SimpleNamespace())

    async def test_new_start_deletes_old_and_becomes_current(self):
        old_start = SimpleNamespace(delete=AsyncMock())
        new_start = SimpleNamespace(
            sender_id=self.user_id,
            delete=AsyncMock(),
            respond=AsyncMock(),
            get_sender=AsyncMock(return_value=SimpleNamespace(first_name="Test")),
        )
        remember_start_command_message(self.user_id, old_start)

        with patch(
            "handlers.bot_handlers.cancel_pending_login_flow",
            new=AsyncMock(return_value=SimpleNamespace(ok=True, reason="start")),
        ), patch(
            "handlers.bot_handlers.AccountManager.check_access", return_value=True
        ), patch(
            "handlers.bot_handlers.AccountManager.get_user_accounts", return_value={}
        ), patch(
            "handlers.bot_handlers.AccountManager.get_quota_status",
            return_value={"used": 0, "quota": 1},
        ), patch(
            "handlers.bot_handlers.DataManager.is_admin", return_value=False
        ):
            await self.bot.callbacks["start"](new_start)

        old_start.delete.assert_awaited_once()
        self.assertTrue(await delete_remembered_start_command(self.user_id))
        new_start.delete.assert_awaited_once()

    async def test_cleanup_failure_keeps_old_start(self):
        old_start = SimpleNamespace(delete=AsyncMock())
        event = SimpleNamespace(
            sender_id=self.user_id,
            respond=AsyncMock(),
        )
        remember_start_command_message(self.user_id, old_start)

        try:
            with patch(
                "handlers.bot_handlers.cancel_pending_login_flow",
                new=AsyncMock(return_value=SimpleNamespace(ok=False, reason="busy")),
            ):
                await self.bot.callbacks["start"](event)

            old_start.delete.assert_not_awaited()
        finally:
            await delete_remembered_start_command(self.user_id)

    async def test_unauthorized_start_deletes_old_without_remembering_new(self):
        old_start = SimpleNamespace(delete=AsyncMock())
        event = SimpleNamespace(
            sender_id=self.user_id,
            delete=AsyncMock(),
            respond=AsyncMock(),
        )
        remember_start_command_message(self.user_id, old_start)

        with patch(
            "handlers.bot_handlers.cancel_pending_login_flow",
            new=AsyncMock(return_value=SimpleNamespace(ok=True, reason="start")),
        ), patch(
            "handlers.bot_handlers.AccountManager.check_access", return_value=False
        ):
            await self.bot.callbacks["start"](event)

        old_start.delete.assert_awaited_once()
        self.assertFalse(await delete_remembered_start_command(self.user_id))
        event.delete.assert_not_awaited()


class NewDeviceCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_fetches_message_before_updating_result(self):
        bot = CallbackBot()
        await setup_antilogin_handlers(bot)
        event = SimpleNamespace(
            sender_id=123,
            data=b"nda:a:8613800138000:456",
            get_message=AsyncMock(
                return_value=SimpleNamespace(
                    raw_text=(
                        "new-device prompt\n\n"
                        "请选择是否允许此设备登录。"
                    )
                )
            ),
            answer=AsyncMock(),
            edit=AsyncMock(side_effect=MessageNotModifiedError(None)),
        )

        with patch(
            "handlers.antilogin_handlers.AccountManager.get_user_accounts",
            return_value={"+8613800138000": {}},
        ), patch(
            "handlers.antilogin_handlers.AccountManager.resolve_new_authorization",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "resolved": True,
                    "message": "authorization allowed",
                }
            ),
        ):
            await bot.callbacks["new_device_authorization_action"](event)

        event.get_message.assert_awaited_once_with()
        event.answer.assert_awaited_once_with("authorization allowed")
        event.edit.assert_awaited_once_with(
            "new-device prompt\n\nauthorization allowed", buttons=None
        )


class SafeHandlerEditTests(unittest.IsolatedAsyncioTestCase):
    async def test_vip_noop_edit_keeps_payment_message_id(self):
        bot = CallbackBot()
        payment_system = SimpleNamespace(
            create_subscription_payment=AsyncMock(
                return_value={
                    "success": True,
                    "order_id": "order-1",
                    "pay_url": "https://pay.example/order-1",
                }
            ),
            pending_orders={
                "order-1": {
                    "change_type": "new",
                    "plan_id": "go",
                    "quota": 2,
                    "amount": "1",
                    "coin": "USDT",
                }
            },
            bind_order_message=Mock(),
        )
        await setup_vip_handlers(bot, payment_system)
        set_state(
            912,
            subscription_period_selection=True,
            subscription_plan_id="go",
            subscription_quota=2,
        )
        current_message = SimpleNamespace(id=44)
        event = SimpleNamespace(
            sender_id=912,
            chat_id=913,
            message_id=43,
            data=b"subscription_period_30",
            answer=AsyncMock(),
            edit=AsyncMock(side_effect=MessageNotModifiedError(None)),
            get_message=AsyncMock(return_value=current_message),
            respond=AsyncMock(),
        )

        await bot.callbacks["select_subscription_period"](event)

        event.get_message.assert_awaited_once_with()
        event.respond.assert_not_awaited()
        payment_system.bind_order_message.assert_called_once_with("order-1", 913, 44)

    async def test_admin_menu_noop_edit_does_not_send_replacement(self):
        bot = CallbackBot()
        await setup_admin_handlers(bot)
        event = SimpleNamespace(
            sender_id=914,
            answer=AsyncMock(),
            edit=AsyncMock(side_effect=MessageNotModifiedError(None)),
            respond=AsyncMock(),
        )
        catalog = {
            "go": {"price": "1", "quota": 2},
            "plus": {
                "price": "2",
                "quota": 10,
                "addon_unit_price": "0.1",
                "min_addon": 5,
            },
            "pro": {"price": "3", "quota": None},
        }
        periods = {
            90: {"discount_percent": 8},
            180: {"discount_percent": 15},
            365: {"discount_percent": 25},
        }

        with patch(
            "handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription_catalog",
            return_value=catalog,
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription_periods",
            return_value=periods,
        ):
            await bot.callbacks["admin_subscription_config"](event)

        event.edit.assert_awaited_once()
        event.respond.assert_not_awaited()


class AccountDeletionResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_delete_removes_remembered_start_command(self):
        user_id = 916
        bot = CallbackBot()
        await setup_antilogin_handlers(bot)
        start_message = SimpleNamespace(delete=AsyncMock())
        remember_start_command_message(user_id, start_message)
        event = SimpleNamespace(
            sender_id=user_id,
            data=b"antilogin_del_confirm_+85298363057",
            answer=AsyncMock(),
            delete=AsyncMock(),
            edit=AsyncMock(),
        )

        with patch(
            "handlers.antilogin_handlers.AccountManager.check_access",
            return_value=True,
        ), patch(
            "handlers.antilogin_handlers.AccountManager.delete_account",
            new=AsyncMock(return_value="🗑 +85298363057 删除成功！"),
        ):
            await bot.callbacks["antilogin_delete_confirm"](event)

        start_message.delete.assert_awaited_once()
        event.delete.assert_awaited_once()
        bot.send_message.assert_awaited_once_with(
            user_id, "🗑 +85298363057 删除成功！"
        )


class AccountTransferStartCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_transfer_success_deletes_sender_start(self):
        user_id = 918
        bot = CallbackBot()
        await setup_transfer_handlers(bot)
        start_message = SimpleNamespace(delete=AsyncMock())
        remember_start_command_message(user_id, start_message)
        set_state(
            user_id,
            account_transfer_pending=True,
            account_transfer_phone="+8613800138000",
            account_transfer_to_user_id=919,
            account_transfer_created_at=10**12,
        )
        event = SimpleNamespace(
            sender_id=user_id,
            answer=AsyncMock(),
            edit=AsyncMock(),
        )

        try:
            with patch(
                "handlers.transfer_handlers.time.time", return_value=10**12
            ), patch(
                "handlers.transfer_handlers.AccountManager.transfer_account",
                new=AsyncMock(
                    return_value=AccountTransferResult(
                        ok=True,
                        code="transferred",
                        message="✅ 转让成功",
                        phone="+8613800138000",
                        from_user_id=user_id,
                        to_user_id=919,
                    )
                ),
            ):
                await bot.callbacks["transfer_confirm"](event)

            start_message.delete.assert_awaited_once()
            event.edit.assert_awaited_once_with(
                "✅ 转让成功", buttons=None, parse_mode="md"
            )
        finally:
            clear_state(user_id)

    async def test_transfer_failure_keeps_sender_start(self):
        user_id = 920
        bot = CallbackBot()
        await setup_transfer_handlers(bot)
        start_message = SimpleNamespace(delete=AsyncMock())
        remember_start_command_message(user_id, start_message)
        set_state(
            user_id,
            account_transfer_pending=True,
            account_transfer_phone="+8613800138000",
            account_transfer_to_user_id=921,
            account_transfer_created_at=10**12,
        )
        event = SimpleNamespace(
            sender_id=user_id,
            answer=AsyncMock(),
            edit=AsyncMock(),
        )

        try:
            with patch(
                "handlers.transfer_handlers.time.time", return_value=10**12
            ), patch(
                "handlers.transfer_handlers.AccountManager.transfer_account",
                new=AsyncMock(
                    return_value=AccountTransferResult(
                        ok=False,
                        code="failed",
                        message="❌ 转让失败",
                        phone="+8613800138000",
                        from_user_id=user_id,
                        to_user_id=921,
                    )
                ),
            ):
                await bot.callbacks["transfer_confirm"](event)

            start_message.delete.assert_not_awaited()
            event.edit.assert_awaited_once()
        finally:
            await delete_remembered_start_command(user_id)
            clear_state(user_id)

    async def test_start_delete_failure_does_not_hide_transfer_success(self):
        user_id = 922
        bot = CallbackBot()
        await setup_transfer_handlers(bot)
        set_state(
            user_id,
            account_transfer_pending=True,
            account_transfer_phone="+8613800138000",
            account_transfer_to_user_id=923,
            account_transfer_created_at=10**12,
        )
        event = SimpleNamespace(
            sender_id=user_id,
            answer=AsyncMock(),
            edit=AsyncMock(),
        )

        try:
            with patch(
                "handlers.transfer_handlers.time.time", return_value=10**12
            ), patch(
                "handlers.transfer_handlers.AccountManager.transfer_account",
                new=AsyncMock(
                    return_value=AccountTransferResult(
                        ok=True,
                        code="transferred",
                        message="✅ 转让成功",
                    )
                ),
            ), patch(
                "handlers.transfer_handlers.delete_remembered_start_command",
                new=AsyncMock(return_value=False),
            ):
                await bot.callbacks["transfer_confirm"](event)

            event.edit.assert_awaited_once_with(
                "✅ 转让成功", buttons=None, parse_mode="md"
            )
        finally:
            clear_state(user_id)


class UploadFileClassificationTests(unittest.TestCase):
    def test_supported_and_unsupported_file_names(self):
        cases = (
            (None, ("", False, False)),
            (SimpleNamespace(name=None), ("", False, False)),
            (SimpleNamespace(name=""), ("", False, False)),
            (SimpleNamespace(name="notes.txt"), ("notes.txt", False, False)),
            (SimpleNamespace(name="account.SESSION"), ("account.SESSION", True, False)),
            (SimpleNamespace(name="accounts.ZIP"), ("accounts.ZIP", False, True)),
        )

        for event_file, expected in cases:
            with self.subTest(event_file=event_file):
                self.assertEqual(_classify_upload_file(event_file), expected)


class SessionUploadFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_upload_does_not_reference_phone_before_inspection(self):
        user_id = 918
        bot = CallbackBot()
        await setup_account_handlers(bot)
        event = SimpleNamespace(
            sender_id=user_id,
            text=None,
            file=SimpleNamespace(name="account.session", size=10),
            respond=AsyncMock(),
            download_media=AsyncMock(),
        )
        install = AsyncMock(return_value=(None, None, False, "invalid"))

        with patch(
            "handlers.account_handlers.require_access",
            new=AsyncMock(return_value=True),
        ), patch(
            "handlers.account_handlers.cancel_pending_login_flow",
            new=AsyncMock(return_value=SimpleNamespace(ok=True)),
        ), patch(
            "handlers.account_handlers.AccountManager.install_uploaded_session",
            new=install,
        ):
            await bot.callbacks["handle_account_messages"](event)

        install.assert_awaited_once()
        responses = "\n".join(str(call.args[0]) for call in event.respond.await_args_list)
        self.assertNotIn("cannot access local variable 'phone'", responses)


class QrLoginResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_auth_token_is_handled_as_qr_timeout(self):
        user_id = 909
        status_message = SimpleNamespace(id=29, delete=AsyncMock())
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock()))
        )
        flow_id = "qr-expired"
        set_state(
            user_id,
            qr_login=True,
            qr_flow_id=flow_id,
            qr_phase="waiting",
            qr_cancel_requested=False,
            waiting_qr=True,
            qr_status_message=status_message,
        )
        qr_login = SimpleNamespace(
            wait=AsyncMock(side_effect=AuthTokenExpiredError(request=None))
        )

        try:
            with patch(
                "handlers.account_handlers.AccountManager.cleanup_pending_login_state",
                new=AsyncMock(return_value=SimpleNamespace(ok=True, reason="qr_timeout")),
            ) as cleanup:
                await finish_qr_login(
                    bot,
                    user_id,
                    SimpleNamespace(),
                    qr_login,
                    status_message=status_message,
                    flow_id=flow_id,
                )

            cleanup.assert_awaited_once_with(user_id, reason="qr_timeout")
            status_message.delete.assert_awaited_once()
            bot.send_message.assert_awaited_once()
            self.assertIn("二维码已过期", bot.send_message.await_args.args[1])
            self.assertEqual(get_state(user_id), {})
        finally:
            clear_state(user_id)

    async def test_success_message_has_no_back_button(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        status_message = SimpleNamespace(delete=AsyncMock())
        start_command_message = SimpleNamespace(delete=AsyncMock())
        remember_start_command_message(123, start_command_message)
        qr_login = SimpleNamespace(
            wait=AsyncMock(return_value=SimpleNamespace(phone="8613800138000"))
        )
        client = SimpleNamespace()
        existing = SimpleNamespace(action="allow", message="")
        flow_id = "qr-success"
        set_state(
            123,
            qr_login=True,
            qr_flow_id=flow_id,
            qr_phase="waiting",
            qr_cancel_requested=False,
            waiting_qr=True,
            qr_status_message=status_message,
        )

        try:
            with patch(
                "handlers.account_handlers.AccountManager.check_existing_account_for_add",
                new=AsyncMock(return_value=existing),
            ), patch(
                "handlers.account_handlers.AccountManager.promote_pending_client",
                new=AsyncMock(),
            ):
                await finish_qr_login(
                    bot,
                    123,
                    client,
                    qr_login,
                    status_message=status_message,
                    flow_id=flow_id,
                )
        finally:
            clear_state(123)

        status_message.delete.assert_awaited_once()
        start_command_message.delete.assert_awaited_once()
        bot.send_message.assert_awaited_once()
        self.assertNotIn("buttons", bot.send_message.await_args.kwargs)

    async def test_stale_flow_result_does_not_touch_current_qr(self):
        user_id = 910
        bot = CallbackBot()
        current_message = SimpleNamespace(id=31, delete=AsyncMock())
        stale_message = SimpleNamespace(id=30, delete=AsyncMock())
        set_state(
            user_id,
            qr_login=True,
            qr_flow_id="new-flow",
            qr_phase="waiting",
            qr_cancel_requested=False,
            waiting_qr=True,
            qr_status_message=current_message,
        )
        qr_login = SimpleNamespace(
            wait=AsyncMock(return_value=SimpleNamespace(phone="8613800138000"))
        )

        try:
            with patch(
                "handlers.account_handlers.AccountManager.check_existing_account_for_add",
                new=AsyncMock(),
            ) as existing_check, patch(
                "handlers.account_handlers.AccountManager.promote_pending_client",
                new=AsyncMock(),
            ) as promote:
                await finish_qr_login(
                    bot,
                    user_id,
                    SimpleNamespace(),
                    qr_login,
                    status_message=stale_message,
                    flow_id="old-flow",
                )

            existing_check.assert_not_awaited()
            promote.assert_not_awaited()
            stale_message.delete.assert_not_awaited()
            current_message.delete.assert_not_awaited()
            bot.send_message.assert_not_awaited()
        finally:
            clear_state(user_id)

    async def test_two_factor_deletes_qr_before_sending_password_prompt(self):
        user_id = 911
        actions = []
        status_message = SimpleNamespace(
            id=32,
            delete=AsyncMock(side_effect=lambda: actions.append("delete_qr")),
        )
        prompt_message = SimpleNamespace(id=33, delete=AsyncMock())

        async def send_prompt(*_args, **_kwargs):
            actions.append("send_prompt")
            return prompt_message

        bot = SimpleNamespace(send_message=AsyncMock(side_effect=send_prompt))
        flow_id = "qr-2fa"
        set_state(
            user_id,
            qr_login=True,
            qr_flow_id=flow_id,
            qr_phase="waiting",
            qr_cancel_requested=False,
            waiting_qr=True,
            pending_session_path="pending.session",
            qr_status_message=status_message,
        )
        qr_login = SimpleNamespace(
            wait=AsyncMock(side_effect=SessionPasswordNeededError(request=None))
        )

        try:
            await finish_qr_login(
                bot,
                user_id,
                SimpleNamespace(),
                qr_login,
                status_message=status_message,
                flow_id=flow_id,
            )

            self.assertEqual(actions, ["delete_qr", "send_prompt"])
            self.assertTrue(get_state(user_id)["waiting_password"])
            self.assertEqual(get_state(user_id)["qr_phase"], "waiting_password")
            self.assertIsNone(get_state(user_id)["qr_status_message"])
            self.assertIs(
                get_state(user_id)["password_prompt_message"], prompt_message
            )
        finally:
            clear_state(user_id)

    async def test_return_during_commit_rolls_back_and_sends_new_menu(self):
        user_id = 912
        bot = CallbackBot()
        await setup_account_handlers(bot)
        status_message = SimpleNamespace(id=34, delete=AsyncMock())
        flow_id = "qr-race"
        set_state(
            user_id,
            qr_login=True,
            qr_flow_id=flow_id,
            qr_phase="waiting",
            qr_cancel_requested=False,
            waiting_qr=True,
            qr_status_message=status_message,
        )
        qr_login = SimpleNamespace(
            wait=AsyncMock(return_value=SimpleNamespace(phone="8613800138000"))
        )
        commit_started = asyncio.Event()
        release_commit = asyncio.Event()

        async def promote(*_args, **_kwargs):
            commit_started.set()
            await release_commit.wait()

        event = SimpleNamespace(
            sender_id=user_id,
            answer=AsyncMock(),
            edit=AsyncMock(),
            get_message=AsyncMock(),
        )

        with patch(
            "handlers.account_handlers.AccountManager.check_existing_account_for_add",
            new=AsyncMock(
                return_value=SimpleNamespace(action="allow", message="")
            ),
        ), patch(
            "handlers.account_handlers.AccountManager.promote_pending_client",
            new=AsyncMock(side_effect=promote),
        ), patch(
            "handlers.account_handlers.AccountManager.delete_account",
            new=AsyncMock(return_value="🗑 删除成功！"),
        ) as rollback, patch(
            "handlers.account_handlers.AccountManager.cleanup_pending_login_state",
            new=AsyncMock(return_value=SimpleNamespace(ok=True, reason="back")),
        ):
            finish_task = asyncio.create_task(
                finish_qr_login(
                    bot,
                    user_id,
                    SimpleNamespace(),
                    qr_login,
                    status_message=status_message,
                    flow_id=flow_id,
                )
            )
            await commit_started.wait()
            back_task = asyncio.create_task(
                bot.callbacks["back_to_add_methods_callback"](event)
            )
            await asyncio.sleep(0)
            self.assertTrue(get_state(user_id)["qr_cancel_requested"])
            release_commit.set()
            await asyncio.gather(finish_task, back_task)

        rollback.assert_awaited_once_with(user_id, "+8613800138000")
        status_message.delete.assert_awaited_once()
        event.edit.assert_not_awaited()
        event.get_message.assert_not_awaited()
        bot.edit_message.assert_not_awaited()
        bot.send_message.assert_awaited_once()
        self.assertIn("请选择添加账户方式", bot.send_message.await_args.args[1])


class QrMessageDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_delete_failure_is_retried(self):
        message = SimpleNamespace(
            delete=AsyncMock(side_effect=[ConnectionError("down"), None])
        )

        self.assertTrue(await delete_qr_message_strict(message))
        self.assertEqual(message.delete.await_count, 2)

    async def test_permanent_delete_forbidden_is_retired_without_retry(self):
        message = SimpleNamespace(
            id=35,
            chat_id=913,
            delete=AsyncMock(side_effect=MessageDeleteForbiddenError(None)),
        )

        with self.assertLogs("handlers.account_handlers", level="WARNING") as captured:
            self.assertTrue(await delete_qr_message_strict(message))

        message.delete.assert_awaited_once_with()
        self.assertIn("permanently refused", "\n".join(captured.output))

    async def test_start_recovers_from_permanently_undeletable_qr_message(self):
        user_id = 918
        bot = CallbackBot()
        await setup_bot_handlers(bot, SimpleNamespace())
        status_message = SimpleNamespace(
            id=21137,
            chat_id=user_id,
            delete=AsyncMock(side_effect=MessageDeleteForbiddenError(None)),
        )
        event = SimpleNamespace(
            sender_id=user_id,
            delete=AsyncMock(),
            respond=AsyncMock(),
            get_sender=AsyncMock(return_value=SimpleNamespace(first_name="Test")),
        )
        set_state(
            user_id,
            qr_login=True,
            qr_flow_id="qr-delete-forbidden",
            qr_phase="delete_failed",
            qr_cancel_requested=True,
            qr_message_delete_failed=True,
            qr_status_message=status_message,
        )

        try:
            with patch(
                "handlers.account_handlers.AccountManager.cleanup_pending_login_state",
                new=AsyncMock(return_value=SimpleNamespace(ok=True, reason="start")),
            ), patch(
                "handlers.bot_handlers.AccountManager.cleanup_stale_pending_sessions"
            ), patch(
                "handlers.bot_handlers.DataManager.initialize_user_language",
                return_value=True,
            ), patch(
                "handlers.bot_handlers.AccountManager.check_access", return_value=True
            ), patch(
                "handlers.bot_handlers.delete_remembered_start_command",
                new=AsyncMock(),
            ), patch(
                "handlers.bot_handlers.remember_start_command_message"
            ) as remember, patch(
                "handlers.bot_handlers.render_home", new=AsyncMock()
            ) as render:
                await bot.callbacks["start"](event)

            status_message.delete.assert_awaited_once_with()
            self.assertEqual(get_state(user_id), {})
            remember.assert_called_once_with(user_id, event)
            render.assert_awaited_once_with(bot, event, edit=False)
        finally:
            clear_state(user_id)

    async def test_persistent_delete_failure_logs_safe_context(self):
        message = SimpleNamespace(
            id=35,
            chat_id=913,
            delete=AsyncMock(side_effect=ConnectionError("TOP-SECRET-CONTENT")),
        )
        with self.assertLogs("handlers.account_handlers", level="ERROR") as captured:
            self.assertFalse(await delete_qr_message_strict(message))
        output = "\n".join(captured.output)
        self.assertIn("ConnectionError", output)
        self.assertIn("message_id=35", output)
        self.assertIn("chat_id=913", output)
        self.assertNotIn("TOP-SECRET-CONTENT", output)

    async def test_persistent_delete_failure_blocks_new_menu(self):
        user_id = 913
        bot = CallbackBot()
        await setup_account_handlers(bot)
        status_message = SimpleNamespace(
            id=35,
            delete=AsyncMock(side_effect=ConnectionError("down")),
        )
        set_state(
            user_id,
            qr_login=True,
            qr_flow_id="qr-delete-fail",
            qr_phase="waiting",
            qr_cancel_requested=False,
            waiting_qr=True,
            qr_status_message=status_message,
        )
        event = SimpleNamespace(
            sender_id=user_id,
            answer=AsyncMock(),
            edit=AsyncMock(),
            get_message=AsyncMock(),
        )

        try:
            with patch(
                "handlers.account_handlers.AccountManager.cleanup_pending_login_state",
                new=AsyncMock(return_value=SimpleNamespace(ok=True, reason="back")),
            ):
                await bot.callbacks["back_to_add_methods_callback"](event)

            self.assertEqual(status_message.delete.await_count, 3)
            bot.send_message.assert_not_awaited()
            event.edit.assert_not_awaited()
            event.answer.assert_awaited_once_with(
                "二维码消息清理失败，请点击返回重试", alert=True
            )
            self.assertEqual(get_state(user_id)["qr_phase"], "delete_failed")
            self.assertIs(get_state(user_id)["qr_status_message"], status_message)
        finally:
            clear_state(user_id)


class SensitivePasswordDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_login_deletes_verified_2fa_message(self):
        user_id = 901
        bot = CallbackBot()
        await setup_account_handlers(bot)
        status_message = SimpleNamespace(id=10, delete=AsyncMock())
        password_prompt_message = SimpleNamespace(delete=AsyncMock())
        set_state(
            user_id,
            waiting_password=True,
            login_status_message=status_message,
            password_prompt_message=password_prompt_message,
        )
        event = SimpleNamespace(
            sender_id=user_id,
            text="correct-password",
            file=None,
            delete=AsyncMock(),
            respond=AsyncMock(),
        )

        async def verify_password(_user_id, _password):
            flow_state = get_state(user_id)
            flow_state["password_verified"] = True
            clear_state(user_id)
            return "✅ 登录成功！"

        try:
            with patch(
                "handlers.account_handlers.AccountManager.handle_password",
                new=AsyncMock(side_effect=verify_password),
            ):
                await bot.callbacks["handle_account_messages"](event)
        finally:
            clear_state(user_id)

        event.delete.assert_awaited_once()
        password_prompt_message.delete.assert_awaited_once()
        bot.edit_message.assert_awaited_once_with(
            user_id, status_message, "✅ 登录成功！", buttons=None
        )
        event.respond.assert_not_awaited()

    async def test_qr_password_error_edits_new_prompt_not_qr(self):
        user_id = 914
        bot = CallbackBot()
        await setup_account_handlers(bot)
        password_prompt = SimpleNamespace(id=36, delete=AsyncMock())
        set_state(
            user_id,
            qr_login=True,
            qr_flow_id="qr-password",
            qr_phase="waiting_password",
            qr_cancel_requested=False,
            waiting_password=True,
            qr_status_message=None,
            password_prompt_message=password_prompt,
        )
        event = SimpleNamespace(
            sender_id=user_id,
            text="wrong-password",
            file=None,
            delete=AsyncMock(),
            respond=AsyncMock(),
        )

        try:
            with patch(
                "handlers.account_handlers.AccountManager.handle_password",
                new=AsyncMock(return_value="❌ 二级密码错误，请重新输入"),
            ):
                await bot.callbacks["handle_account_messages"](event)

            event.delete.assert_awaited_once()
            bot.edit_message.assert_awaited_once_with(
                user_id,
                password_prompt,
                "❌ 二级密码错误，请重新输入",
                buttons=None,
            )
            event.respond.assert_not_awaited()
            self.assertIsNone(get_state(user_id)["qr_status_message"])
        finally:
            clear_state(user_id)

    async def test_start_cancel_wins_during_qr_password_commit(self):
        user_id = 915
        bot = CallbackBot()
        await setup_account_handlers(bot)
        password_prompt = SimpleNamespace(id=37, delete=AsyncMock())
        set_state(
            user_id,
            qr_login=True,
            qr_flow_id="qr-password-race",
            qr_phase="waiting_password",
            qr_cancel_requested=False,
            waiting_password=True,
            auth_phone="+8613800138000",
            qr_status_message=None,
            password_prompt_message=password_prompt,
        )
        event = SimpleNamespace(
            sender_id=user_id,
            text="correct-password",
            file=None,
            delete=AsyncMock(),
            respond=AsyncMock(),
        )
        password_started = asyncio.Event()
        release_password = asyncio.Event()

        async def complete_password(_user_id, _password):
            password_started.set()
            await release_password.wait()
            clear_state(user_id)
            return "✅ 登录成功！"

        with patch(
            "handlers.account_handlers.AccountManager.handle_password",
            new=AsyncMock(side_effect=complete_password),
        ), patch(
            "handlers.account_handlers.AccountManager.delete_account",
            new=AsyncMock(return_value="🗑 删除成功！"),
        ) as rollback, patch(
            "handlers.account_handlers.AccountManager.cleanup_pending_login_state",
            new=AsyncMock(return_value=SimpleNamespace(ok=True, reason="start")),
        ):
            password_task = asyncio.create_task(
                bot.callbacks["handle_account_messages"](event)
            )
            await password_started.wait()
            cancel_task = asyncio.create_task(
                cancel_pending_login_flow(user_id, "start")
            )
            await asyncio.sleep(0)
            self.assertTrue(get_state(user_id)["qr_cancel_requested"])
            release_password.set()
            result, _ = await asyncio.gather(cancel_task, password_task)

        self.assertTrue(result.ok)
        rollback.assert_awaited_once_with(user_id, "+8613800138000")
        password_prompt.delete.assert_awaited_once()
        bot.edit_message.assert_not_awaited()
        bot.send_message.assert_not_awaited()


class PhoneLoginMessageFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_phone_prompt_has_back_button_and_is_tracked(self):
        user_id = 902
        bot = CallbackBot()
        await setup_account_handlers(bot)
        status_message = SimpleNamespace(id=11, delete=AsyncMock())
        event = SimpleNamespace(
            sender_id=user_id,
            answer=AsyncMock(),
            edit=AsyncMock(return_value=status_message),
            get_message=AsyncMock(return_value=status_message),
        )

        try:
            with patch(
                "handlers.account_handlers.require_access",
                new=AsyncMock(return_value=True),
            ), patch(
                "handlers.account_handlers.AccountManager.cleanup_pending_login_state",
                new=AsyncMock(return_value=SimpleNamespace(ok=True)),
            ):
                await bot.callbacks["add_account_phone_callback"](event)

            self.assertTrue(get_state(user_id)["adding_account"])
            self.assertIs(get_state(user_id)["phone_prompt_message"], status_message)
            self.assertIsNotNone(event.edit.await_args.kwargs["buttons"])
        finally:
            clear_state(user_id)

    async def test_invalid_phone_edits_prompt_and_keeps_input_state(self):
        user_id = 903
        bot = CallbackBot()
        await setup_account_handlers(bot)
        status_message = SimpleNamespace(id=12, delete=AsyncMock())
        set_state(
            user_id,
            adding_account=True,
            phone_prompt_message=status_message,
        )
        event = SimpleNamespace(
            sender_id=user_id,
            text="invalid",
            file=None,
            respond=AsyncMock(),
        )

        try:
            with patch(
                "handlers.account_handlers.require_access",
                new=AsyncMock(return_value=True),
            ):
                await bot.callbacks["handle_account_messages"](event)

            self.assertTrue(get_state(user_id)["adding_account"])
            bot.edit_message.assert_awaited_once()
            self.assertIn("手机号格式无效", bot.edit_message.await_args.args[2])
            self.assertIsNotNone(bot.edit_message.await_args.kwargs["buttons"])
            event.respond.assert_not_awaited()
        finally:
            clear_state(user_id)

    async def test_valid_phone_removes_back_button_and_sends_code_status(self):
        user_id = 907
        bot = CallbackBot()
        await setup_account_handlers(bot)
        status_message = SimpleNamespace(id=14, delete=AsyncMock())
        login_status_message = SimpleNamespace(id=16, delete=AsyncMock())
        bot.send_message.return_value = login_status_message
        client = SimpleNamespace()
        set_state(
            user_id,
            adding_account=True,
            phone_prompt_message=status_message,
        )
        event = SimpleNamespace(
            sender_id=user_id,
            text="+8613800138000",
            file=None,
            respond=AsyncMock(),
        )

        async def begin_code_wait(_client, _phone, _user_id):
            set_state(user_id, waiting_code=True)
            return (
                "📱 验证码已发送，请输入您收到的验证码：\n\n"
                "⚠️ 注意：验证码通常在5分钟内有效"
            )

        try:
            with patch(
                "handlers.account_handlers.require_access",
                new=AsyncMock(return_value=True),
            ), patch(
                "handlers.account_handlers.AccountManager.check_existing_account_for_add",
                new=AsyncMock(
                    return_value=SimpleNamespace(action="allow", message="")
                ),
            ), patch(
                "handlers.account_handlers.AccountManager.create_new_client",
                new=AsyncMock(return_value=client),
            ), patch(
                "handlers.account_handlers.AccountManager.authenticate",
                new=AsyncMock(side_effect=begin_code_wait),
            ):
                await bot.callbacks["handle_account_messages"](event)

            self.assertTrue(get_state(user_id)["waiting_code"])
            self.assertIs(
                get_state(user_id)["phone_prompt_message"], status_message
            )
            self.assertIs(
                get_state(user_id)["login_status_message"], login_status_message
            )
            self.assertIs(get_state(user_id)["phone_input_message"], event)
            bot.edit_message.assert_awaited_once_with(
                user_id,
                status_message,
                "📱 请输入手机号 (如+8612345678900 或 +1 234 567 8888):",
                buttons=None,
            )
            bot.send_message.assert_awaited_once_with(
                user_id,
                "📱 验证码已发送，请输入您收到的验证码：\n\n"
                "⚠️ 注意：验证码通常在5分钟内有效",
                buttons=None,
            )
            event.respond.assert_not_awaited()
        finally:
            clear_state(user_id)

    async def test_rejected_phone_keeps_back_button_and_does_not_send_status(self):
        user_id = 910
        bot = CallbackBot()
        await setup_account_handlers(bot)
        phone_prompt_message = SimpleNamespace(id=17, delete=AsyncMock())
        set_state(
            user_id,
            adding_account=True,
            phone_prompt_message=phone_prompt_message,
        )
        event = SimpleNamespace(
            sender_id=user_id,
            text="+8613800138000",
            file=None,
            respond=AsyncMock(),
        )

        try:
            with patch(
                "handlers.account_handlers.require_access",
                new=AsyncMock(return_value=True),
            ), patch(
                "handlers.account_handlers.AccountManager.check_existing_account_for_add",
                new=AsyncMock(
                    return_value=SimpleNamespace(action="allow", message="")
                ),
            ), patch(
                "handlers.account_handlers.AccountManager.create_new_client",
                new=AsyncMock(return_value=SimpleNamespace()),
            ), patch(
                "handlers.account_handlers.AccountManager.authenticate",
                new=AsyncMock(return_value="❌ 发送验证码失败: rejected"),
            ):
                await bot.callbacks["handle_account_messages"](event)

            bot.edit_message.assert_awaited_once_with(
                user_id,
                phone_prompt_message,
                "❌ 发送验证码失败: rejected",
                buttons=bot.edit_message.await_args.kwargs["buttons"],
            )
            self.assertIsNotNone(bot.edit_message.await_args.kwargs["buttons"])
            bot.send_message.assert_not_awaited()
        finally:
            clear_state(user_id)

    async def test_already_authorized_sends_success_then_cleans_first_and_start(self):
        user_id = 911
        bot = CallbackBot()
        await setup_account_handlers(bot)
        phone_prompt_message = SimpleNamespace(id=18, delete=AsyncMock())
        success_message = SimpleNamespace(id=19, delete=AsyncMock())
        start_message = SimpleNamespace(delete=AsyncMock())
        bot.send_message.return_value = success_message
        remember_start_command_message(user_id, start_message)
        set_state(
            user_id,
            adding_account=True,
            phone_prompt_message=phone_prompt_message,
        )
        event = SimpleNamespace(
            sender_id=user_id,
            text="+8613800138000",
            file=None,
            delete=AsyncMock(),
            respond=AsyncMock(),
        )

        with patch(
            "handlers.account_handlers.require_access",
            new=AsyncMock(return_value=True),
        ), patch(
            "handlers.account_handlers.AccountManager.check_existing_account_for_add",
            new=AsyncMock(return_value=SimpleNamespace(action="allow", message="")),
        ), patch(
            "handlers.account_handlers.AccountManager.create_new_client",
            new=AsyncMock(return_value=SimpleNamespace()),
        ), patch(
            "handlers.account_handlers.AccountManager.authenticate",
            new=AsyncMock(return_value="✅ 账户已登录！"),
        ):
            await bot.callbacks["handle_account_messages"](event)

        bot.edit_message.assert_awaited_once_with(
            user_id,
            phone_prompt_message,
            "📱 请输入手机号 (如+8612345678900 或 +1 234 567 8888):",
            buttons=None,
        )
        bot.send_message.assert_awaited_once_with(
            user_id, "✅ 账户已登录！", buttons=None
        )
        phone_prompt_message.delete.assert_awaited_once()
        success_message.delete.assert_not_awaited()
        event.delete.assert_awaited_once()
        start_message.delete.assert_awaited_once()
        self.assertEqual(get_state(user_id), {})

    async def test_code_success_edits_same_message_and_deletes_sensitive_input(self):
        user_id = 904
        bot = CallbackBot()
        await setup_account_handlers(bot)
        phone_prompt_message = SimpleNamespace(id=12, delete=AsyncMock())
        status_message = SimpleNamespace(id=13, delete=AsyncMock())
        start_message = SimpleNamespace(delete=AsyncMock())
        phone_input_message = SimpleNamespace(delete=AsyncMock())
        remember_start_command_message(user_id, start_message)
        set_state(
            user_id,
            waiting_code=True,
            phone_prompt_message=phone_prompt_message,
            login_status_message=status_message,
            phone_input_message=phone_input_message,
        )
        event = SimpleNamespace(
            sender_id=user_id,
            text="12345",
            file=None,
            delete=AsyncMock(),
            respond=AsyncMock(),
        )

        async def complete_login(_user_id, _code):
            clear_state(user_id)
            return "✅ +86 138 0013 8000 登录成功！"

        with patch(
            "handlers.account_handlers.AccountManager.handle_code",
            new=AsyncMock(side_effect=complete_login),
        ):
            await bot.callbacks["handle_account_messages"](event)

        event.delete.assert_awaited_once()
        phone_prompt_message.delete.assert_awaited_once()
        phone_input_message.delete.assert_awaited_once()
        bot.edit_message.assert_awaited_once_with(
            user_id,
            status_message,
            "✅ +86 138 0013 8000 登录成功！",
            buttons=None,
        )
        start_message.delete.assert_awaited_once()
        event.respond.assert_not_awaited()

    async def test_wrong_password_is_deleted_and_edits_same_message(self):
        user_id = 908
        bot = CallbackBot()
        await setup_account_handlers(bot)
        status_message = SimpleNamespace(id=15, delete=AsyncMock())
        set_state(
            user_id,
            waiting_password=True,
            login_status_message=status_message,
        )
        event = SimpleNamespace(
            sender_id=user_id,
            text="wrong-password",
            file=None,
            delete=AsyncMock(),
            respond=AsyncMock(),
        )

        try:
            with patch(
                "handlers.account_handlers.AccountManager.handle_password",
                new=AsyncMock(
                    return_value="❌ 二级密码错误，请重新输入 (剩余尝试次数: 4)"
                ),
            ):
                await bot.callbacks["handle_account_messages"](event)

            event.delete.assert_awaited_once()
            bot.edit_message.assert_awaited_once_with(
                user_id,
                status_message,
                "❌ 二级密码错误，请重新输入 (剩余尝试次数: 4)",
                buttons=None,
            )
            self.assertTrue(get_state(user_id)["waiting_password"])
            event.respond.assert_not_awaited()
        finally:
            clear_state(user_id)

    async def test_password_success_edits_status_and_deletes_first_and_start(self):
        user_id = 912
        bot = CallbackBot()
        await setup_account_handlers(bot)
        phone_prompt_message = SimpleNamespace(id=28, delete=AsyncMock())
        status_message = SimpleNamespace(id=29, delete=AsyncMock())
        phone_input_message = SimpleNamespace(id=30, delete=AsyncMock())
        start_message = SimpleNamespace(delete=AsyncMock())
        remember_start_command_message(user_id, start_message)
        set_state(
            user_id,
            waiting_password=True,
            phone_prompt_message=phone_prompt_message,
            login_status_message=status_message,
            phone_input_message=phone_input_message,
        )
        event = SimpleNamespace(
            sender_id=user_id,
            text="correct-password",
            file=None,
            delete=AsyncMock(),
            respond=AsyncMock(),
        )

        async def complete_login(_user_id, _password):
            clear_state(user_id)
            return "✅ +86 138 0013 8000 登录成功！"

        with patch(
            "handlers.account_handlers.AccountManager.handle_password",
            new=AsyncMock(side_effect=complete_login),
        ):
            await bot.callbacks["handle_account_messages"](event)

        event.delete.assert_awaited_once()
        bot.edit_message.assert_awaited_once_with(
            user_id,
            status_message,
            "✅ +86 138 0013 8000 登录成功！",
            buttons=None,
        )
        phone_prompt_message.delete.assert_awaited_once()
        status_message.delete.assert_not_awaited()
        phone_input_message.delete.assert_awaited_once()
        start_message.delete.assert_awaited_once()


class LoginCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_cancel_deletes_auxiliary_login_messages(self):
        user_id = 905
        status_message = SimpleNamespace(id=21, delete=AsyncMock())
        phone_prompt = SimpleNamespace(id=20, delete=AsyncMock())
        password_prompt = SimpleNamespace(id=22, delete=AsyncMock())
        sensitive_input = SimpleNamespace(id=23, delete=AsyncMock())
        phone_input = SimpleNamespace(id=27, delete=AsyncMock())
        set_state(
            user_id,
            waiting_password=True,
            phone_prompt_message=phone_prompt,
            login_status_message=status_message,
            password_prompt_message=password_prompt,
            pending_sensitive_messages=[sensitive_input],
            phone_input_message=phone_input,
        )

        with patch(
            "handlers.account_handlers.AccountManager.cleanup_pending_login_state",
            new=AsyncMock(return_value=SimpleNamespace(ok=True)),
        ):
            result = await cancel_pending_login_flow(user_id, "start")

        self.assertTrue(result.ok)
        phone_prompt.delete.assert_awaited_once()
        status_message.delete.assert_awaited_once()
        password_prompt.delete.assert_awaited_once()
        sensitive_input.delete.assert_awaited_once()
        phone_input.delete.assert_awaited_once()
        self.assertEqual(get_state(user_id), {})

    async def test_failed_cancel_preserves_state_and_messages(self):
        user_id = 906
        status_message = SimpleNamespace(id=24, delete=AsyncMock())
        set_state(
            user_id,
            waiting_code=True,
            login_status_message=status_message,
        )

        try:
            with patch(
                "handlers.account_handlers.AccountManager.cleanup_pending_login_state",
                new=AsyncMock(return_value=SimpleNamespace(ok=False)),
            ):
                result = await cancel_pending_login_flow(user_id, "start")

            self.assertFalse(result.ok)
            status_message.delete.assert_not_awaited()
            self.assertTrue(get_state(user_id)["waiting_code"])
        finally:
            clear_state(user_id)

    async def test_cancel_preserves_callback_message_for_menu_edit(self):
        user_id = 909
        status_message = SimpleNamespace(id=25, delete=AsyncMock())
        password_prompt = SimpleNamespace(id=26, delete=AsyncMock())
        set_state(
            user_id,
            waiting_password=True,
            login_status_message=status_message,
            password_prompt_message=password_prompt,
        )

        with patch(
            "handlers.account_handlers.AccountManager.cleanup_pending_login_state",
            new=AsyncMock(return_value=SimpleNamespace(ok=True)),
        ):
            result = await cancel_pending_login_flow(
                user_id, "back_to_main", preserve_message=status_message
            )

        self.assertTrue(result.ok)
        status_message.delete.assert_not_awaited()
        password_prompt.delete.assert_awaited_once()



class AdminUserDetailMenuTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _button_texts(buttons):
        return [button.text for row in buttons for button in row]

    async def test_detail_uses_compact_state_aware_primary_actions(self):
        bot = CallbackBot()
        payment_system = SimpleNamespace(
            get_user_order_summaries=Mock(return_value={"total": 12, "items": []})
        )
        await setup_admin_handlers(bot, payment_system)
        event = SimpleNamespace(
            sender_id=1,
            data=b"admin_user_detail_123",
            answer=AsyncMock(),
            edit=AsyncMock(),
        )
        subscription = {
            "active": True,
            "plan_id": "plus",
            "expires_at": "2026-08-30T12:00:00",
            "quota": 5,
        }
        accounts = {123: {"+8613800138000": {"anti_login": True}}}

        with patch(
            "handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)
        ), patch(
            "handlers.admin_handlers._get_user_display_name", new=AsyncMock(return_value=None)
        ), patch(
            "handlers.admin_handlers.UserProfileCache.get_profile", return_value={}
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription", return_value=subscription
        ), patch(
            "handlers.admin_handlers.DataManager.is_admin", return_value=False
        ), patch(
            "handlers.admin_handlers.AccountManager.hosted_account_phones",
            return_value={"8613800138000", "8613800138001"},
        ), patch(
            "handlers.admin_handlers.user_accounts", accounts
        ), patch(
            "handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"
        ), patch(
            "handlers.admin_handlers._audit_result", return_value=True
        ):
            await bot.callbacks["admin_user_detail"](event)

        buttons = event.edit.await_args.kwargs["buttons"]
        texts = self._button_texts(buttons)
        self.assertEqual(texts[:3], ["💎 管理订阅", "🧾 订单 · 12", "⚙️ 账户操作"])
        self.assertNotIn("删除订阅", texts)
        self.assertNotIn("重新加载", texts)
        self.assertNotIn("停用账户", texts)
        self.assertNotIn("恢复已选账户", texts)

    async def test_detail_directly_offers_grant_and_hides_empty_sections(self):
        bot = CallbackBot()
        payment_system = SimpleNamespace(
            get_user_order_summaries=Mock(return_value={"total": 0, "items": []})
        )
        await setup_admin_handlers(bot, payment_system)
        event = SimpleNamespace(
            sender_id=1,
            data=b"admin_user_detail_123",
            answer=AsyncMock(),
            edit=AsyncMock(),
        )

        with patch(
            "handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)
        ), patch(
            "handlers.admin_handlers._get_user_display_name", new=AsyncMock(return_value=None)
        ), patch(
            "handlers.admin_handlers.UserProfileCache.get_profile", return_value={}
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription", return_value=None
        ), patch(
            "handlers.admin_handlers.DataManager.is_admin", return_value=False
        ), patch(
            "handlers.admin_handlers.AccountManager.hosted_account_phones", return_value=set()
        ), patch(
            "handlers.admin_handlers.user_accounts", {}
        ), patch(
            "handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"
        ), patch(
            "handlers.admin_handlers._audit_result", return_value=True
        ):
            await bot.callbacks["admin_user_detail"](event)

        buttons = event.edit.await_args.kwargs["buttons"]
        texts = self._button_texts(buttons)
        self.assertEqual(texts[0], "💎 发放订阅")
        self.assertNotIn("⚙️ 账户操作", texts)
        self.assertFalse(any(text.startswith("🧾 订单") for text in texts))
        self.assertEqual(buttons[0][0].data, b"admin_user_sub_123")

    async def test_secondary_menus_group_subscription_and_account_actions(self):
        bot = CallbackBot()
        await setup_admin_handlers(bot)
        subscription_event = SimpleNamespace(
            sender_id=1,
            data=b"admin_user_subscription_123",
            answer=AsyncMock(),
            edit=AsyncMock(),
        )
        account_event = SimpleNamespace(
            sender_id=1,
            data=b"admin_user_accounts_123",
            answer=AsyncMock(),
            edit=AsyncMock(),
        )
        subscription = {
            "active": True,
            "plan_id": "plus",
            "expires_at": "2026-08-30T12:00:00",
            "quota": 5,
            "selected_accounts": ["8613800138001"],
        }
        accounts = {123: {"+8613800138000": {"anti_login": True}}}

        with patch(
            "handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)
        ), patch(
            "handlers.admin_handlers.DataManager.is_admin", return_value=False
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription", return_value=subscription
        ), patch(
            "handlers.admin_handlers.AccountManager.check_access", return_value=True
        ), patch(
            "handlers.admin_handlers.AccountManager.hosted_account_phones",
            return_value={"8613800138000", "8613800138001"},
        ), patch(
            "handlers.admin_handlers.user_accounts", accounts
        ):
            await bot.callbacks["admin_user_subscription"](subscription_event)
            await bot.callbacks["admin_user_accounts"](account_event)

        subscription_texts = self._button_texts(
            subscription_event.edit.await_args.kwargs["buttons"]
        )
        self.assertIn("延期 / 变更订阅", subscription_texts)
        self.assertIn("⚠️ 删除订阅", subscription_texts)

        account_texts = self._button_texts(
            account_event.edit.await_args.kwargs["buttons"]
        )
        self.assertIn("🔄 重连运行账户", account_texts)
        self.assertIn("▶️ 恢复已选托管账户", account_texts)
        self.assertIn("⚠️ 停用全部运行账户", account_texts)
        account_body = account_event.edit.await_args.args[0]
        self.assertIn("运行中：1 个", account_body)
        self.assertIn("可恢复：1 个", account_body)


class AdminPanelConfigurationFlowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _catalog():
        return {
            "go": {"name": "Go", "price": "0.6", "quota": 2, "coin": "USDT"},
            "plus": {
                "name": "Plus", "price": "1", "quota": 10, "coin": "USDT",
                "addon_unit_price": "0.1", "min_addon": 5,
            },
            "pro": {"name": "Pro", "price": "3", "quota": None, "coin": "USDT"},
        }

    @staticmethod
    def _periods():
        return {
            30: {"discount_percent": "0"},
            90: {"discount_percent": "8"},
            180: {"discount_percent": "18"},
            365: {"discount_percent": "25"},
        }

    async def test_admin_panel_uses_accurate_labels_compact_buttons_and_clears_state(self):
        bot = CallbackBot()
        payment_system = SimpleNamespace(
            list_admin_orders=Mock(return_value={"total": 2}),
            get_admin_report=Mock(return_value={
                "amounts": {"USDT": "4.5"}, "new_paid_users": 1,
            }),
        )
        await setup_admin_handlers(bot, payment_system)
        event = SimpleNamespace(
            sender_id=1, answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock(),
        )
        set_state(1, admin_user_search=True, admin_subscription_config_flow={"stale": True})

        with patch(
            "handlers.admin_handlers.DataManager.is_admin", return_value=True
        ), patch(
            "handlers.admin_handlers.DataManager.get_all_subscription_users",
            return_value=[{"user_id": 2}],
        ), patch("handlers.admin_handlers.user_accounts", {}):
            await bot.callbacks["admin_panel"](event)

        text = event.edit.await_args.args[0]
        self.assertIn("已加载账户：0 个", text)
        self.assertIn("已开启保护：0 个", text)
        self.assertIn("订阅收入：4.5 USDT", text)
        self.assertNotIn("🔴", text)
        self.assertNotIn("在线账户", text)
        buttons = event.edit.await_args.kwargs["buttons"]
        self.assertEqual(
            [[button.text for button in row] for row in buttons[:-1]],
            [
                ["订单中心", "用户搜索"],
                ["发放订阅", "删除订阅"],
                ["订阅用户", "方案设置"],
                ["运营报表", "审计日志"],
                ["提醒设置", "刷新数据"],
            ],
        )
        button_by_text = {button.text: button for row in buttons for button in row}
        expected_icons = {
            "订单中心": 5877485980901971030,
            "用户搜索": 5874960879434338403,
            "方案设置": 5879841310902324730,
            "运营报表": 5994378914636500516,
            "审计日志": 5877597667231534929,
            "刷新数据": 5877410604225924969,
        }
        for label, icon in expected_icons.items():
            self.assertEqual(button_by_text[label].style.icon, icon)
        self.assertEqual(get_state(1), {})

    async def test_all_config_targets_collect_values_without_saving(self):
        bot = CallbackBot()
        await setup_admin_handlers(bot)
        cases = {
            "go": (["0.7", "3"], {"price": "0.7", "quota": 3}),
            "plus": (
                ["1.5", "12", "0.2", "4"],
                {"price": "1.5", "quota": 12, "addon_unit_price": "0.2", "min_addon": 4},
            ),
            "pro": (["4"], {"price": "4"}),
            "discounts": (
                ["5", "10", "15"],
                {90: "5", 180: "10", 365: "15"},
            ),
        }

        with patch(
            "handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)
        ), patch(
            "handlers.admin_handlers.DataManager.is_admin", return_value=True
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription_catalog",
            return_value=self._catalog(),
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription_periods",
            return_value=self._periods(),
        ), patch(
            "handlers.admin_handlers.DataManager.set_subscription_catalog"
        ) as set_catalog, patch(
            "handlers.admin_handlers.DataManager.set_subscription_periods"
        ) as set_periods:
            for target, (inputs, expected) in cases.items():
                start = SimpleNamespace(
                    sender_id=1,
                    data=f"admin_subscription_config_edit_{target}".encode(),
                    answer=AsyncMock(), edit=AsyncMock(),
                )
                await bot.callbacks["admin_subscription_config_edit"](start)
                for value in inputs:
                    message = SimpleNamespace(sender_id=1, text=value, respond=AsyncMock())
                    self.assertTrue(await admin_handlers.handle_admin_message(message))
                flow = get_state(1)["admin_subscription_config_flow"]
                self.assertEqual(flow["stage"], "preview")
                candidate = admin_handlers._config_flow_candidate(flow)
                if target == "discounts":
                    self.assertEqual(
                        {days: candidate[days]["discount_percent"] for days in expected},
                        expected,
                    )
                else:
                    for field, value in expected.items():
                        self.assertEqual(candidate[target][field], value)
                clear_state(1)

        set_catalog.assert_not_called()
        set_periods.assert_not_called()

    async def test_invalid_value_retries_and_confirm_is_the_only_save_point(self):
        bot = CallbackBot()
        await setup_admin_handlers(bot)
        before = self._catalog()
        after = self._catalog()
        after["go"].update({"price": "0.7", "quota": 3})

        with patch(
            "handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)
        ), patch(
            "handlers.admin_handlers.DataManager.is_admin", return_value=True
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription_catalog",
            side_effect=[before, before, after, after],
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription_periods",
            return_value=self._periods(),
        ), patch(
            "handlers.admin_handlers.DataManager.set_subscription_catalog",
            return_value=True,
        ) as save, patch(
            "handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"
        ), patch(
            "handlers.admin_handlers._audit_result", return_value=True
        ) as audit_result:
            start = SimpleNamespace(
                sender_id=1, data=b"admin_subscription_config_edit_go",
                answer=AsyncMock(), edit=AsyncMock(),
            )
            await bot.callbacks["admin_subscription_config_edit"](start)

            invalid = SimpleNamespace(sender_id=1, text="0", respond=AsyncMock())
            await admin_handlers.handle_admin_message(invalid)
            self.assertEqual(get_state(1)["admin_subscription_config_flow"]["index"], 0)
            self.assertIn("必须大于 0", invalid.respond.await_args.args[0])

            for value in ("0.7", "3"):
                message = SimpleNamespace(sender_id=1, text=value, respond=AsyncMock())
                await admin_handlers.handle_admin_message(message)
            save.assert_not_called()

            confirm = SimpleNamespace(
                sender_id=1, data=b"admin_subscription_config_confirm",
                answer=AsyncMock(), edit=AsyncMock(),
            )
            await bot.callbacks["admin_subscription_config_confirm"](confirm)

        saved = save.await_args.args[0] if isinstance(save, AsyncMock) else save.call_args.args[0]
        self.assertEqual(saved["go"]["price"], "0.7")
        self.assertEqual(saved["go"]["quota"], 3)
        audit_result.assert_called_once()
        self.assertEqual(audit_result.call_args.kwargs["before"], before)
        self.assertEqual(audit_result.call_args.kwargs["after"], after)
        self.assertEqual(get_state(1), {})

    async def test_config_conflict_and_expiry_never_overwrite_current_values(self):
        bot = CallbackBot()
        await setup_admin_handlers(bot)
        before = self._catalog()
        changed = self._catalog()
        changed["go"]["price"] = "9"
        flow = {
            "target": "go", "before": before,
            "values": {"price": "0.7", "quota": 3},
            "index": 2, "stage": "preview", "started_at": 1,
        }

        with patch(
            "handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)
        ), patch(
            "handlers.admin_handlers.time.time", return_value=1000
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription_catalog",
            return_value=changed,
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription_periods",
            return_value=self._periods(),
        ), patch(
            "handlers.admin_handlers.DataManager.set_subscription_catalog"
        ) as save:
            set_state(1, admin_subscription_config_flow=flow)
            confirm = SimpleNamespace(
                sender_id=1, data=b"admin_subscription_config_confirm",
                answer=AsyncMock(), edit=AsyncMock(),
            )
            await bot.callbacks["admin_subscription_config_confirm"](confirm)

        save.assert_not_called()
        self.assertEqual(get_state(1), {})
        self.assertIn("配置已过期", confirm.answer.await_args.args[0])

        conflict_flow = copy.deepcopy(flow)
        conflict_flow["started_at"] = 999
        with patch(
            "handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)
        ), patch(
            "handlers.admin_handlers.time.time", return_value=1000
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription_catalog",
            return_value=changed,
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription_periods",
            return_value=self._periods(),
        ), patch(
            "handlers.admin_handlers.DataManager.set_subscription_catalog"
        ) as conflict_save:
            set_state(1, admin_subscription_config_flow=conflict_flow)
            conflict = SimpleNamespace(
                sender_id=1, data=b"admin_subscription_config_confirm",
                answer=AsyncMock(), edit=AsyncMock(),
            )
            await bot.callbacks["admin_subscription_config_confirm"](conflict)

        conflict_save.assert_not_called()
        self.assertEqual(get_state(1), {})
        self.assertIn("其他管理员修改", conflict.answer.await_args.args[0])

    async def test_removed_config_commands_are_not_registered(self):
        bot = CallbackBot()
        await setup_admin_handlers(bot)
        self.assertNotIn("sub_plan_command", bot.callbacks)
        self.assertNotIn("sub_discount_command", bot.callbacks)
        self.assertIn("sub_command", bot.callbacks)
        self.assertIn("delsub_command", bot.callbacks)

    async def test_config_save_failure_keeps_original_and_records_failure(self):
        bot = CallbackBot()
        await setup_admin_handlers(bot)
        before = self._periods()
        flow = {
            "target": "discounts", "before": before,
            "values": {"90": "5", "180": "10", "365": "15"},
            "index": 3, "stage": "preview", "started_at": 999,
        }

        with patch(
            "handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)
        ), patch(
            "handlers.admin_handlers.time.time", return_value=1000
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription_catalog",
            return_value=self._catalog(),
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription_periods",
            return_value=before,
        ), patch(
            "handlers.admin_handlers.DataManager.set_subscription_periods",
            return_value=False,
        ) as save, patch(
            "handlers.admin_handlers.AdminAuditLog.record_attempt", return_value="audit"
        ), patch(
            "handlers.admin_handlers._audit_result", return_value=True
        ) as audit_result:
            set_state(1, admin_subscription_config_flow=flow)
            confirm = SimpleNamespace(
                sender_id=1, data=b"admin_subscription_config_confirm",
                answer=AsyncMock(), edit=AsyncMock(),
            )
            await bot.callbacks["admin_subscription_config_confirm"](confirm)

        save.assert_called_once()
        self.assertEqual(get_state(1), {})
        self.assertEqual(audit_result.call_args.args[3], "failed")
        self.assertEqual(audit_result.call_args.kwargs["before"], before)
        self.assertIn("原配置保持不变", confirm.edit.await_args.args[0])


class SubscriptionAdminCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_log_download_handler_is_not_registered(self):
        bot = CallbackBot()
        await setup_admin_handlers(bot)
        self.assertNotIn("bot_runtime_log", bot.callbacks)

    async def test_sub_grants_exact_days_and_custom_plus_quota(self):
        bot = CallbackBot()
        await setup_admin_handlers(bot)
        event = SimpleNamespace(
            sender_id=1,
            text="/sub 123456 plus 45 15",
            respond=AsyncMock(),
        )

        with patch(
            "handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)
        ), patch(
            "handlers.admin_handlers.DataManager.is_admin", return_value=False
        ), patch(
            "handlers.admin_handlers.DataManager.quote_subscription",
            return_value={'plan_name': 'PLUS', 'quota': 15},
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription", return_value=None
        ), patch(
            "handlers.admin_handlers.DataManager.grant_subscription", return_value=True
        ) as grant:
            await bot.callbacks['sub_command'](event)
            grant.assert_not_called()
            pending = admin_handlers._pending_admin_actions[1]
            confirm = SimpleNamespace(
                sender_id=1,
                data=f"admin_action_confirm_{pending['token']}".encode(),
                answer=AsyncMock(),
                edit=AsyncMock(),
            )
            await bot.callbacks['admin_action_confirm'](confirm)

        grant.assert_called_once_with(123456, 'plus', 45, 15)
        self.assertIn("45 天", event.respond.await_args.args[0])

    async def test_subscription_commands_reject_non_admins(self):
        bot = CallbackBot()
        await setup_admin_handlers(bot)
        sub_event = SimpleNamespace(sender_id=2, text="/sub 123 go 1", respond=AsyncMock())
        delete_event = SimpleNamespace(sender_id=2, text="/delsub 123", respond=AsyncMock())

        with patch(
            "handlers.admin_handlers.require_admin", new=AsyncMock(return_value=False)
        ), patch(
            "handlers.admin_handlers.DataManager.grant_subscription"
        ) as grant, patch(
            "handlers.admin_handlers.DataManager.delete_subscription"
        ) as delete:
            await bot.callbacks['sub_command'](sub_event)
            await bot.callbacks['delsub_command'](delete_event)

        grant.assert_not_called()
        delete.assert_not_called()

    async def test_sub_rejects_invalid_and_out_of_range_parameters(self):
        bot = CallbackBot()
        await setup_admin_handlers(bot)
        invalid_commands = (
            "/sub 0 go 1",
            "/sub 123 unknown 1",
            "/sub 123 go 0",
            "/sub 123 go 1 9",
            "/sub 123 pro 999999999",
        )

        with patch(
            "handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)
        ), patch(
            "handlers.admin_handlers.DataManager.is_admin", return_value=False
        ), patch(
            "handlers.admin_handlers.DataManager.grant_subscription"
        ) as grant:
            for command in invalid_commands:
                event = SimpleNamespace(sender_id=1, text=command, respond=AsyncMock())
                await bot.callbacks['sub_command'](event)
                self.assertIn("❌", event.respond.await_args.args[0])

        grant.assert_not_called()

    async def test_delsub_suspends_before_deleting_subscription(self):
        bot = CallbackBot()
        await setup_admin_handlers(bot)
        event = SimpleNamespace(sender_id=1, text="/delsub 123456", respond=AsyncMock())
        operations = []

        async def suspend(_user_id):
            operations.append('suspend')
            return 2

        def delete(_user_id):
            operations.append('delete')
            return True

        with patch(
            "handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)
        ), patch(
            "handlers.admin_handlers.DataManager.is_admin", return_value=False
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription",
            return_value={'active': True},
        ), patch(
            "handlers.admin_handlers.AccountManager.suspend_user_accounts",
            new=AsyncMock(side_effect=suspend),
        ), patch(
            "handlers.admin_handlers.DataManager.delete_subscription",
            side_effect=delete,
        ):
            await bot.callbacks['delsub_command'](event)
            self.assertEqual(operations, [])
            pending = admin_handlers._pending_admin_actions[1]
            confirm = SimpleNamespace(
                sender_id=1,
                data=f"admin_action_confirm_{pending['token']}".encode(),
                answer=AsyncMock(),
                edit=AsyncMock(),
            )
            await bot.callbacks['admin_action_confirm'](confirm)

        self.assertEqual(operations, ['suspend', 'delete'])
        self.assertIn("确认删除订阅", event.respond.await_args.args[0])

    async def test_delsub_restores_runtime_when_persistence_fails(self):
        bot = CallbackBot()
        await setup_admin_handlers(bot)
        event = SimpleNamespace(sender_id=1, text="/delsub 123456", respond=AsyncMock())

        with patch(
            "handlers.admin_handlers.require_admin", new=AsyncMock(return_value=True)
        ), patch(
            "handlers.admin_handlers.DataManager.is_admin", return_value=False
        ), patch(
            "handlers.admin_handlers.DataManager.get_subscription",
            return_value={'active': True},
        ), patch(
            "handlers.admin_handlers.AccountManager.suspend_user_accounts",
            new=AsyncMock(return_value=1),
        ), patch(
            "handlers.admin_handlers.DataManager.delete_subscription", return_value=False
        ), patch(
            "handlers.admin_handlers.AccountManager.resume_selected_accounts",
            new=AsyncMock(return_value=1),
        ) as resume:
            await bot.callbacks['delsub_command'](event)
            pending = admin_handlers._pending_admin_actions[1]
            confirm = SimpleNamespace(
                sender_id=1,
                data=f"admin_action_confirm_{pending['token']}".encode(),
                answer=AsyncMock(),
                edit=AsyncMock(),
            )
            await bot.callbacks['admin_action_confirm'](confirm)

        resume.assert_awaited_once_with(123456)
        self.assertIn("操作失败", confirm.edit.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
