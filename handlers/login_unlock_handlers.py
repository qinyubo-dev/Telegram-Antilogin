# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import asyncio
import logging

from telethon import TelegramClient, events
from telethon.tl.custom import Button

from accounts import account_runtime
from accounts.account_manager import AccountManager
from accounts.login_code_rate_limiter import (
    LoginCodeRateLimitMessage,
    login_code_request_rate_limiter,
    render_login_code_rate_limit,
)
from handlers.account_handlers import cancel_pending_login_flow
from handlers.handler_utils import (
    back_button,
    clear_state,
    delete_sensitive_message,
    forget_flow_message,
    get_state,
    require_access,
    remember_flow_message,
    safe_edit,
    set_state,
)
from localization import t
from reminders.login_unlock_reminder import (
    format_limit,
    format_offset,
    parse_utc,
    phone_key,
)
from storage.data_manager import DataManager
from user_timezones import (
    TIMEZONE_BY_CALLBACK,
    TIMEZONE_CHOICES,
    timezone_text,
)


logger = logging.getLogger(__name__)
LOGIN_UNLOCK_CUSTOM_EMOJI_ID = 5778605968208170641
LOGIN_UNLOCK_ADD_CUSTOM_EMOJI_ID = 5775937998948404844
LOGIN_UNLOCK_CANCEL_TIMEOUT_SECONDS = 5.0
_login_unlock_probe_tasks = {}


def _language(user_id: int) -> str:
    return DataManager.get_user_language(user_id)


async def cancel_login_unlock_flow(user_id: int) -> None:
    """Cancel an in-flight manual unlock probe for the user."""
    task = _login_unlock_probe_tasks.pop(int(user_id), None)
    if not task or task is asyncio.current_task() or task.done():
        return
    task.cancel()
    done, _ = await asyncio.wait(
        {task}, timeout=LOGIN_UNLOCK_CANCEL_TIMEOUT_SECONDS
    )
    if not done:
        logger.warning(
            "取消登录解锁检测任务超时，继续清理用户流程: 用户ID=%s, 超时=%s秒",
            user_id,
            LOGIN_UNLOCK_CANCEL_TIMEOUT_SECONDS,
        )
        return
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.info("取消登录解锁检测任务时任务已异常结束: 用户ID=%s", user_id)


async def setup_login_unlock_handlers(bot: TelegramClient):
    async def render_menu(event, *, answer: bool = True):
        user_id = event.sender_id
        language = _language(user_id)
        system = account_runtime.get_login_unlock_reminder_system()
        if system is None:
            if answer:
                await event.answer(t(language, "login_unlock.unavailable"), alert=True)
            return
        records = await system.list_records(user_id)
        quota = system.quota_status(user_id)
        timezone_name = DataManager.get_user_timezone(user_id)
        buttons = [[
            Button.inline(
                t(language, "login_unlock.manual_add"),
                b"login_unlock_add",
                icon=LOGIN_UNLOCK_ADD_CUSTOM_EMOJI_ID,
            ),
            Button.inline(
                t(language, "login_unlock.timezone_button"),
                b"login_unlock_timezone",
            ),
        ]]
        for record in records:
            unlock_at = parse_utc(record.get("unlock_at"))
            if unlock_at is None:
                continue
            digits = phone_key(record.get("phone", ""))
            if digits:
                buttons.append([Button.inline(
                    t(
                        language,
                        "login_unlock.account_button",
                        phone=record.get("phone", ""),
                    ),
                    f"login_unlock_detail_{digits}".encode(),
                )])
        buttons.append([back_button(b"back_to_main", language=language)])
        text = t(
            language,
            "login_unlock.menu",
            used=quota["used"],
            limit=format_limit(quota["limit"], language),
            timezone=timezone_name,
            items=(
                t(language, "login_unlock.choose_account")
                if records else t(language, "login_unlock.empty")
            ),
        )
        if answer:
            await event.answer()
        await safe_edit(event, text, buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=b"login_unlock_menu"))
    async def login_unlock_menu(event):
        if not await require_access(event, alert=True):
            return
        current_message = (
            await event.get_message() if hasattr(event, "get_message") else None
        )
        cleanup = await cancel_pending_login_flow(
            event.sender_id,
            reason="login_unlock_menu",
            preserve_message=current_message,
        )
        if not cleanup.ok:
            await event.answer(
                t(_language(event.sender_id), "start.session_releasing"), alert=True
            )
            return
        await render_menu(event)

    @bot.on(events.CallbackQuery(pattern=rb"^login_unlock_detail_\d+$"))
    async def login_unlock_detail(event):
        if not await require_access(event, alert=True):
            return
        user_id = event.sender_id
        language = _language(user_id)
        digits = event.data.decode().removeprefix("login_unlock_detail_")
        system = account_runtime.get_login_unlock_reminder_system()
        if system is None:
            await event.answer(t(language, "login_unlock.unavailable"), alert=True)
            return
        records = await system.list_records(user_id)
        record = next(
            (item for item in records if phone_key(item.get("phone", "")) == digits),
            None,
        )
        unlock_at = parse_utc(record.get("unlock_at")) if record else None
        if record is None or unlock_at is None:
            await event.answer(t(language, "login_unlock.not_found"), alert=True)
            await render_menu(event, answer=False)
            return

        timezone_name = DataManager.get_user_timezone(user_id)
        reminder_lines = [
            t(
                language,
                "login_unlock.list_node",
                remaining=format_offset(node.get("offset_seconds", 1), language),
            )
            for node in record.get("nodes", [])
            if parse_utc(node.get("remind_at")) is not None
        ]
        text = t(
            language,
            "login_unlock.list_item",
            phone=record.get("phone", ""),
            unlock=timezone_text(unlock_at, timezone_name),
            nodes="\n".join(reminder_lines),
        )
        await event.answer()
        await safe_edit(event, text, buttons=[
            [Button.inline(
                t(language, "login_unlock.cancel_item"),
                f"login_unlock_cancel_{digits}".encode(),
            )],
            [back_button(b"login_unlock_menu", language=language)],
        ])

    @bot.on(events.CallbackQuery(pattern=rb"^login_unlock_timezone$"))
    async def login_unlock_timezone(event):
        if not await require_access(event, alert=True):
            return
        language = _language(event.sender_id)
        selected = DataManager.get_user_timezone(event.sender_id)
        timezone_buttons = []
        for callback, timezone_name in TIMEZONE_CHOICES:
            label = timezone_name
            if timezone_name == selected:
                label = t(language, "login_unlock.timezone_selected", timezone=label)
            timezone_buttons.append(Button.inline(
                label, f"login_unlock_timezone_set_{callback}".encode()
            ))
        buttons = [
            timezone_buttons[index:index + 2]
            for index in range(0, len(timezone_buttons), 2)
        ]
        buttons.append([back_button(b"login_unlock_menu", language=language)])
        await event.answer()
        await safe_edit(
            event,
            t(
                language,
                "login_unlock.timezone_menu",
                timezone=selected,
            ),
            buttons=buttons,
        )

    @bot.on(events.CallbackQuery(pattern=rb"^login_unlock_timezone_set_[a-z_]+$"))
    async def login_unlock_timezone_set(event):
        language = _language(event.sender_id)
        callback = event.data.decode().removeprefix("login_unlock_timezone_set_")
        timezone_name = TIMEZONE_BY_CALLBACK.get(callback)
        if timezone_name is None or not DataManager.set_user_timezone(
            event.sender_id, timezone_name
        ):
            await event.answer(t(language, "login_unlock.timezone_save_failed"), alert=True)
            return
        await event.answer(t(language, "login_unlock.timezone_saved"))
        await render_menu(event, answer=False)

    @bot.on(events.CallbackQuery(pattern=b"login_unlock_add"))
    async def login_unlock_add(event):
        if not await require_access(event, alert=True):
            return
        language = _language(event.sender_id)
        set_state(event.sender_id, login_unlock_manual_phone=True)
        await event.answer()
        await safe_edit(
            event,
            t(language, "login_unlock.manual_prompt"),
            buttons=[[back_button(b"login_unlock_menu", language=language)]],
        )

    @bot.on(events.CallbackQuery(pattern=rb"login_unlock_cancel_\d+"))
    async def login_unlock_cancel(event):
        if not await require_access(event, alert=True):
            return
        language = _language(event.sender_id)
        digits = event.data.decode().removeprefix("login_unlock_cancel_")
        system = account_runtime.get_login_unlock_reminder_system()
        if system is None or not await system.remove(event.sender_id, digits):
            await event.answer(t(language, "login_unlock.cancel_failed"), alert=True)
            return
        await event.answer(t(language, "login_unlock.cancelled"))
        await render_menu(event, answer=False)

    @bot.on(events.NewMessage)
    async def login_unlock_manual_phone(event):
        user_id = event.sender_id
        state = get_state(user_id)
        if not state.get("login_unlock_manual_phone"):
            return
        if event.text and event.text.startswith("/"):
            return
        language = _language(user_id)
        phone = (event.text or "").strip()
        await delete_sensitive_message(event, "login unlock phone")
        if not await require_access(event):
            return
        if not AccountManager.PHONE_REGEX.match(phone):
            await event.respond(
                t(language, "account.phone.invalid"),
                buttons=[[back_button(b"login_unlock_menu", language=language)]],
            )
            return

        system = account_runtime.get_login_unlock_reminder_system()
        if system is None:
            clear_state(user_id)
            await event.respond(t(language, "login_unlock.unavailable"))
            return
        await system.reconcile_user(user_id)
        quota = system.quota_status(user_id, phone)
        if quota["full"]:
            clear_state(user_id)
            await event.respond(
                t(
                    language,
                    "login_unlock.manual_full",
                    used=quota["used"],
                    limit=format_limit(quota["limit"], language),
                ),
                buttons=[[back_button(b"login_unlock_menu", language=language)]],
            )
            return

        rate_limit = login_code_request_rate_limiter.check(user_id)
        if not rate_limit.allowed:
            await event.respond(
                render_login_code_rate_limit(rate_limit, language),
                buttons=[[back_button(b"login_unlock_menu", language=language)]],
            )
            return

        clear_state(user_id)
        status = await event.respond(t(language, "login_unlock.manual_checking"))
        remember_flow_message(user_id, status)
        task = asyncio.current_task()
        _login_unlock_probe_tasks[int(user_id)] = task
        try:
            client = await AccountManager.create_new_client(phone, user_id)
            result = await AccountManager.probe_login_unlock(client, phone, user_id)
            if isinstance(result, LoginCodeRateLimitMessage):
                set_state(user_id, login_unlock_manual_phone=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("创建登录解限检测客户端失败: 用户ID=%s", user_id)
            result = t(language, "login_unlock.manual_failed")
        try:
            await status.edit(
                result,
                buttons=[[back_button(b"login_unlock_menu", language=language)]],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.info("登录解锁结果消息已不可编辑: 用户ID=%s", user_id)
        finally:
            if _login_unlock_probe_tasks.get(int(user_id)) is task:
                _login_unlock_probe_tasks.pop(int(user_id), None)
        forget_flow_message(user_id, status)
