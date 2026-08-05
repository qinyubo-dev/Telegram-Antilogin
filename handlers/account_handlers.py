# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import logging
import os
import tempfile
import asyncio
import uuid
from io import BytesIO
from datetime import datetime

from telethon import TelegramClient, events
from telethon.errors import (
    AuthTokenExpiredError,
    MessageDeleteForbiddenError,
    MessageIdInvalidError,
    MessageNotModifiedError,
)
from telethon.errors import SessionPasswordNeededError
from telethon.tl.custom import Button

from accounts.account_manager import AccountManager
from accounts.login_code_rate_limiter import (
    LoginCodeRateLimitMessage,
    login_code_request_rate_limiter,
    render_login_code_rate_limit,
)
from accounts.models import SessionCleanupResult
from accounts.session_upload import (
    SessionImportResult,
    ZipSessionUploadError,
    extract_zip_session_entry,
    find_zip_session_entries,
    is_upload_size_allowed,
    render_zip_import_summary,
    render_zip_upload_error,
    safe_archive_label,
)
from handlers.handler_utils import (
    back_button,
    clear_state,
    delete_remembered_start_command,
    delete_sensitive_message,
    get_state,
    require_access,
    safe_edit,
    set_state,
)
from storage.data_manager import DataManager
from localization import localized_result, t


logger = logging.getLogger(__name__)


class QrDependencyError(RuntimeError):
    pass


QR_LOGIN_TIMEOUT_SECONDS = 120
QR_MESSAGE_DELETE_ATTEMPTS = 3
_qr_flow_locks = {}
LOGIN_UI_MESSAGE_KEYS = (
    "phone_prompt_message",
    "phone_input_message",
    "login_status_message",
    "password_prompt_message",
    "qr_status_message",
)
def _language(user_id: int) -> str:
    return DataManager.get_user_language(user_id)


def add_account_method_buttons(user_id=None):
    language = _language(user_id) if user_id is not None else "zh"
    return [
        [Button.inline(t(language, "account.add.phone"), b"add_account_phone", icon=5877316724830768997)],
        [Button.inline(t(language, "account.add.qr"), b"add_account_qr", icon=5987917196469213507)],
        [Button.inline(t(language, "account.add.upload"), b"upload_session", icon=6030822047150512346)],
        [back_button(b"back_to_main", language=language)],
    ]


def back_to_add_methods_buttons(user_id=None):
    language = _language(user_id) if user_id is not None else "zh"
    return [[back_button(b"back_to_add_methods", language=language)]]


def _get_qr_flow_lock(user_id: int) -> asyncio.Lock:
    lock = _qr_flow_locks.get(int(user_id))
    if lock is None:
        lock = asyncio.Lock()
        _qr_flow_locks[int(user_id)] = lock
    return lock


def _active_qr_state(user_id: int, flow_id: str, allow_cancelled: bool = False):
    state = get_state(user_id)
    if not state or state.get("qr_flow_id") != flow_id:
        return None
    if not allow_cancelled and state.get("qr_cancel_requested"):
        return None
    return state


async def delete_qr_message_strict(message) -> bool:
    """Delete a QR message with retries; never fall back to editing it."""
    if not message:
        return True
    last_error = None
    for attempt in range(QR_MESSAGE_DELETE_ATTEMPTS):
        try:
            await message.delete()
            return True
        except MessageIdInvalidError:
            return True
        except MessageDeleteForbiddenError:
            # Telegram reports this as a permanent condition (for example when
            # the referenced message is a service/non-owned message). Retrying
            # can never remove it and used to trap every later /start in the
            # same delete_failed state. The pending authorization is cleaned
            # before cancellation reaches this helper, so the QR is no longer
            # usable even when Telegram refuses to remove its message.
            logger.warning(
                "Telegram permanently refused QR message deletion; treating it as retired: "
                "message_id=%s, chat_id=%s",
                getattr(message, "id", None),
                getattr(message, "chat_id", None),
            )
            return True
        except Exception as error:
            last_error = error
            if attempt + 1 < QR_MESSAGE_DELETE_ATTEMPTS:
                await asyncio.sleep(0.1 * (attempt + 1))
    logger.error(
        "连续 %s 次删除二维码消息失败: error_type=%s, message_id=%s, chat_id=%s",
        QR_MESSAGE_DELETE_ATTEMPTS,
        type(last_error).__name__ if last_error else "UnknownError",
        getattr(message, "id", None),
        getattr(message, "chat_id", None),
    )
    return False


async def delete_qr_and_send_new(
    bot: TelegramClient,
    user_id: int,
    qr_message,
    text: str,
    **kwargs,
):
    """Strict QR replacement: confirmed deletion first, then a brand-new message."""
    if not await delete_qr_message_strict(qr_message):
        return None
    return await bot.send_message(user_id, text, **kwargs)


async def _rollback_promoted_qr_account(user_id: int, phone: str) -> None:
    try:
        result = await AccountManager.delete_account(user_id, phone)
        if not result.startswith("🗑"):
            logger.error("取消扫码登录后回滚托管账户失败: 用户ID=%s, 结果=%s", user_id, result)
    except Exception:
        logger.exception("取消扫码登录后回滚托管账户异常: 用户ID=%s", user_id)


def _store_qr_delete_failure(user_id: int, flow_id: str, qr_message) -> None:
    set_state(
        user_id,
        qr_login=True,
        qr_flow_id=flow_id,
        qr_phase="delete_failed",
        qr_cancel_requested=False,
        qr_message_delete_failed=True,
        qr_status_message=qr_message,
    )


def _same_message(left, right) -> bool:
    if left is right:
        return True
    left_id = getattr(left, "id", None)
    right_id = getattr(right, "id", None)
    return left_id is not None and right_id is not None and left_id == right_id


async def _delete_message(message, context: str) -> bool:
    if not message:
        return True
    try:
        await message.delete()
        return True
    except Exception:
        logger.warning("无法删除登录流程消息: 场景=%s", context)
        return False


async def remember_or_delete_sensitive_input(state: dict, message, context: str) -> None:
    """Delete a code/password input now and retain it for a later retry on failure."""
    if await delete_sensitive_message(message, context):
        return
    state.setdefault("pending_sensitive_messages", []).append(message)


async def cleanup_login_ui(state: dict, preserve_message=None) -> None:
    """Best-effort cleanup of bot login prompts and undeleted sensitive inputs."""
    messages = []
    for key in LOGIN_UI_MESSAGE_KEYS:
        message = state.get(key)
        if message:
            messages.append((message, key))
    messages.extend(
        (message, "sensitive_input")
        for message in state.get("pending_sensitive_messages", [])
        if message
    )
    messages.extend(
        (message, "login_aux_message")
        for message in state.get("login_aux_messages", [])
        if message
    )

    seen = set()
    for message, context in messages:
        identity = getattr(message, "id", None) or id(message)
        if identity in seen or _same_message(message, preserve_message):
            continue
        seen.add(identity)
        await _delete_message(message, context)


async def cancel_pending_login_flow(user_id: int, reason: str, preserve_message=None):
    """Cancel pending authorization and only then remove its related UI messages."""
    live_state = get_state(user_id)
    is_qr_flow = bool(live_state.get("qr_login") and live_state.get("qr_flow_id"))
    if is_qr_flow:
        # Set this before the first await so a concurrently completed scan loses to cancel.
        live_state["qr_cancel_requested"] = True

    lock = _get_qr_flow_lock(user_id) if is_qr_flow else None

    async def cleanup():
        state = dict(live_state)
        cleanup_result = await AccountManager.cleanup_pending_login_state(
            user_id, reason=reason
        )
        if not cleanup_result.ok:
            return cleanup_result

        if is_qr_flow:
            qr_message = state.get("qr_status_message")
            if not await delete_qr_message_strict(qr_message):
                set_state(
                    user_id,
                    qr_login=True,
                    qr_flow_id=state.get("qr_flow_id"),
                    qr_phase="delete_failed",
                    qr_cancel_requested=True,
                    qr_message_delete_failed=True,
                    qr_status_message=qr_message,
                )
                return SessionCleanupResult(
                    ok=False,
                    action="cleanup_pending_login",
                    reason="qr_message_delete_failed",
                )
            state["qr_status_message"] = None

        await cleanup_login_ui(
            state,
            preserve_message=None if is_qr_flow else preserve_message,
        )
        clear_state(user_id)
        return cleanup_result

    if lock:
        async with lock:
            return await cleanup()
    return await cleanup()


async def update_login_status(
    bot: TelegramClient,
    user_id: int,
    status_message,
    text: str,
    **kwargs,
):
    """Edit the tracked login message, falling back to a replacement only if needed."""
    message = await edit_status_or_send(bot, user_id, status_message, text, **kwargs)
    state = get_state(user_id)
    if state and message:
        state["login_status_message"] = message
    return message


def _classify_upload_file(event_file):
    file_name = (getattr(event_file, "name", "") or "") if event_file else ""
    lower_name = file_name.lower()
    return (
        file_name,
        lower_name.endswith(".session"),
        lower_name.endswith(".zip"),
    )


async def edit_status_or_send(bot: TelegramClient, user_id: int, message, text: str, **kwargs):
    try:
        if message:
            return await bot.edit_message(user_id, message, text, **kwargs)
    except MessageNotModifiedError:
        return message
    except MessageIdInvalidError:
        logger.warning(
            "扫码状态消息不可编辑，改为发送新消息: user_id=%s, message_id=%s",
            user_id,
            getattr(message, "id", None),
        )
    except Exception:
        logger.exception(f"编辑扫码状态消息失败，改为发送新消息: 用户ID={user_id}")

    file = kwargs.pop("file", None)
    if file:
        if hasattr(file, "seek"):
            file.seek(0)
        return await bot.send_file(user_id, file, caption=text, **kwargs)
    return await bot.send_message(user_id, text, **kwargs)


def build_qr_image(url: str) -> BytesIO:
    try:
        import qrcode
    except ImportError as e:
        raise QrDependencyError from e

    image = qrcode.make(url)
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    output.name = "telegram-login-qr.png"
    return output


async def finish_qr_login(
    bot: TelegramClient,
    user_id: int,
    client: TelegramClient,
    qr_login,
    status_message=None,
    flow_id: str = "",
):
    """Finish one QR flow without ever editing its disposable QR message."""
    flow_id = flow_id or get_state(user_id).get("qr_flow_id", "")
    lock = _get_qr_flow_lock(user_id)
    promoted_phone = ""
    language = _language(user_id)

    async def send_terminal_result(text: str, reason: str, **kwargs):
        state = _active_qr_state(user_id, flow_id)
        if not state:
            return None
        state["qr_phase"] = "finished"
        cleanup_result = await AccountManager.cleanup_pending_login_state(
            user_id, reason=reason
        )
        if not cleanup_result.ok or state.get("qr_cancel_requested"):
            return None
        set_state(user_id, **state)
        state = get_state(user_id)
        if not await delete_qr_message_strict(status_message):
            _store_qr_delete_failure(user_id, flow_id, status_message)
            return None
        state["qr_status_message"] = None
        if state.get("qr_cancel_requested"):
            return None
        result_message = await bot.send_message(user_id, text, **kwargs)
        if state.get("qr_cancel_requested"):
            await _delete_message(result_message, "cancelled_qr_result")
            return None
        clear_state(user_id)
        return result_message

    try:
        user = await qr_login.wait(timeout=QR_LOGIN_TIMEOUT_SECONDS)
        async with lock:
            state = _active_qr_state(user_id, flow_id)
            if not state:
                return

            phone = getattr(user, "phone", None)
            if not phone:
                me = await client.get_me()
                if state.get("qr_cancel_requested"):
                    return
                phone = getattr(me, "phone", None)
            if not phone:
                await send_terminal_result(
                    t(language, "account.qr.no_phone"),
                    "qr_no_phone",
                    buttons=back_to_add_methods_buttons(user_id),
                )
                return

            phone = f"+{phone}"
            existing = await AccountManager.check_existing_account_for_add(user_id, phone)
            if state.get("qr_cancel_requested"):
                return
            if existing.action == "block":
                await send_terminal_result(
                    localized_result(language, existing.message),
                    "qr_existing_account",
                    buttons=back_to_add_methods_buttons(user_id),
                    parse_mode="md",
                )
                return

            state["qr_phase"] = "committing"
            state["qr_commit_started"] = True
            await AccountManager.promote_pending_client(
                client,
                phone,
                user_id,
                display_phone=AccountManager.format_phone_display(phone),
            )
            promoted_phone = phone
            if state.get("qr_cancel_requested"):
                await _rollback_promoted_qr_account(user_id, phone)
                promoted_phone = ""
                return

            if not await delete_qr_message_strict(status_message):
                await _rollback_promoted_qr_account(user_id, phone)
                promoted_phone = ""
                _store_qr_delete_failure(user_id, flow_id, status_message)
                return
            state["qr_status_message"] = None
            if state.get("qr_cancel_requested"):
                await _rollback_promoted_qr_account(user_id, phone)
                promoted_phone = ""
                return

            success_message = await bot.send_message(
                user_id,
                t(language, "account.qr.success",
                  phone=AccountManager.format_phone_display(phone),
                  time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
            )
            if state.get("qr_cancel_requested"):
                await _delete_message(success_message, "cancelled_qr_success")
                await _rollback_promoted_qr_account(user_id, phone)
                promoted_phone = ""
                return

            await delete_remembered_start_command(user_id)
            if state.get("qr_cancel_requested"):
                await _delete_message(success_message, "cancelled_qr_success")
                await _rollback_promoted_qr_account(user_id, phone)
                promoted_phone = ""
                return
            state["qr_phase"] = "finished"
            state["qr_commit_started"] = False
            promoted_phone = ""
            clear_state(user_id)
    except SessionPasswordNeededError:
        async with lock:
            state = _active_qr_state(user_id, flow_id)
            if not state:
                return
            if not await delete_qr_message_strict(status_message):
                await AccountManager.cleanup_pending_login_state(
                    user_id, reason="qr_2fa_message_delete_failed"
                )
                _store_qr_delete_failure(user_id, flow_id, status_message)
                return

            pending_session_path = (
                state.get("pending_session_path")
                or AccountManager._client_session_path(client)
            )
            state.update(
                auth_phone=state.get("auth_phone", ""),
                auth_client=client,
                pending_session_path=pending_session_path,
                waiting_qr=False,
                waiting_password=True,
                qr_login=True,
                qr_phase="waiting_password",
                qr_status_message=None,
                password_attempts=0,
                max_password_attempts=5,
            )
            if state.get("qr_cancel_requested"):
                return
            password_prompt_message = await bot.send_message(
                user_id,
                t(language, "account.qr.password"),
            )
            if state.get("qr_cancel_requested"):
                await _delete_message(password_prompt_message, "cancelled_qr_2fa_prompt")
                return
            state["password_prompt_message"] = password_prompt_message
    except (asyncio.TimeoutError, AuthTokenExpiredError):
        async with lock:
            if not _active_qr_state(user_id, flow_id):
                return
            await send_terminal_result(
                t(language, "account.qr.expired"),
                "qr_timeout",
                buttons=back_to_add_methods_buttons(user_id),
            )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("扫码登录失败: 用户ID=%s", user_id)
        async with lock:
            if promoted_phone:
                await _rollback_promoted_qr_account(user_id, promoted_phone)
                promoted_phone = ""
            state = _active_qr_state(user_id, flow_id)
            if not state:
                return
            state["qr_commit_started"] = False
            await send_terminal_result(
                t(language, "account.qr.login_failed", error=str(e)),
                "qr_error",
                buttons=back_to_add_methods_buttons(user_id),
            )


async def setup_account_handlers(bot: TelegramClient):
    """Register account add/login/session upload handlers."""

    async def show_add_account_methods(event):
        language = _language(event.sender_id)
        await event.answer()
        await safe_edit(
            event,
            t(language, "account.add.choose"),
            buttons=add_account_method_buttons(event.sender_id),
        )

    @bot.on(events.CallbackQuery(pattern=rb"^add_account$"))
    async def add_account_callback(event):
        user_id = event.sender_id

        if not await require_access(event, alert=True):
            return

        AccountManager.cleanup_stale_pending_sessions()
        cleanup_result = await cancel_pending_login_flow(user_id, reason="add_account")
        if not cleanup_result.ok:
            await event.answer(t(_language(user_id), "start.session_releasing"), alert=True)
            return

        await show_add_account_methods(event)

    @bot.on(events.CallbackQuery(pattern=rb"^back_to_add_methods$"))
    async def back_to_add_methods_callback(event):
        user_id = event.sender_id
        state = get_state(user_id)
        is_qr_flow = bool(state.get("qr_login") and state.get("qr_flow_id"))
        if is_qr_flow:
            state["qr_cancel_requested"] = True
        elif not await require_access(event, alert=True):
            return

        current_message = None if is_qr_flow else await event.get_message()
        cleanup_result = await cancel_pending_login_flow(
            user_id,
            reason="back_to_add_methods",
            preserve_message=None if is_qr_flow else current_message,
        )
        if not cleanup_result.ok:
            message = (
                t(_language(user_id), "back.cleanup_failed")
                if cleanup_result.reason == "qr_message_delete_failed"
                else t(_language(user_id), "start.session_releasing")
            )
            await event.answer(message, alert=True)
            return

        if is_qr_flow:
            await event.answer()
            await bot.send_message(
                user_id,
                t(_language(user_id), "account.add.choose"),
                buttons=add_account_method_buttons(user_id),
            )
        else:
            await show_add_account_methods(event)

    @bot.on(events.CallbackQuery(pattern=rb"^add_account_phone$"))
    async def add_account_phone_callback(event):
        user_id = event.sender_id

        if not await require_access(event, alert=True):
            return

        AccountManager.cleanup_stale_pending_sessions()
        cleanup_result = await cancel_pending_login_flow(
            user_id, reason="add_account_phone"
        )
        if not cleanup_result.ok:
            await event.answer(t(_language(user_id), "start.session_releasing"), alert=True)
            return

        set_state(user_id, adding_account=True)
        await event.answer()
        status_message = await safe_edit(event,
            t(_language(user_id), "account.phone.prompt"),
            buttons=back_to_add_methods_buttons(user_id),
        )
        if not status_message:
            status_message = await event.get_message()
        get_state(user_id)["phone_prompt_message"] = status_message

    @bot.on(events.CallbackQuery(pattern=rb"^add_account_qr$"))
    async def add_account_qr_callback(event):
        user_id = event.sender_id

        if not await require_access(event, alert=True):
            return

        AccountManager.cleanup_stale_pending_sessions()
        cleanup_result = await cancel_pending_login_flow(
            user_id, reason="add_account_qr"
        )
        if not cleanup_result.ok:
            await event.answer(t(_language(user_id), "start.session_releasing"), alert=True)
            return

        await event.answer()
        status_message = await safe_edit(event, t(_language(user_id), "account.qr.generating"), buttons=back_to_add_methods_buttons(user_id))
        if not status_message:
            status_message = await event.get_message()

        flow_id = uuid.uuid4().hex
        set_state(
            user_id,
            auth_phone="",
            waiting_qr=True,
            qr_login=True,
            qr_flow_id=flow_id,
            qr_phase="generating",
            qr_cancel_requested=False,
            qr_status_message=status_message,
        )

        try:
            async with _get_qr_flow_lock(user_id):
                client = await AccountManager.create_qr_client(user_id)
                state = _active_qr_state(user_id, flow_id, allow_cancelled=True)
                if state:
                    state.update(
                        auth_client=client,
                        pending_session_path=AccountManager._client_session_path(client),
                    )
                state = _active_qr_state(user_id, flow_id)
                if not state:
                    return
                await client.connect()
                if state.get("qr_cancel_requested"):
                    return
                qr_login = await client.qr_login()
                if state.get("qr_cancel_requested"):
                    return
                qr_image = build_qr_image(qr_login.url)
                qr_text = t(_language(user_id), "account.qr.instructions")
                status_message = await edit_status_or_send(
                    bot,
                    user_id,
                    status_message,
                    qr_text,
                    file=qr_image,
                    buttons=back_to_add_methods_buttons(user_id),
                )
                state["qr_status_message"] = status_message
                if state.get("qr_cancel_requested"):
                    return
                state.update(
                    qr_phase="waiting",
                )
                wait_task = asyncio.create_task(
                    finish_qr_login(
                        bot,
                        user_id,
                        client,
                        qr_login,
                        status_message,
                        flow_id,
                    )
                )
                state["qr_wait_task"] = wait_task
        except Exception as e:
            logger.exception(f"生成扫码登录二维码失败: 用户ID={user_id}")
            await AccountManager.cleanup_pending_login_state(user_id, reason="qr_create_error")
            error_text = (
                t(_language(user_id), "account.qr.dependency_missing")
                if isinstance(e, QrDependencyError) else str(e)
            )
            replacement = await delete_qr_and_send_new(
                bot,
                user_id,
                status_message,
                t(_language(user_id), "account.qr.failed", error=error_text),
                buttons=back_to_add_methods_buttons(user_id),
            )
            if replacement is None:
                _store_qr_delete_failure(user_id, flow_id, status_message)

    @bot.on(events.CallbackQuery(pattern=rb"^upload_session$"))
    async def upload_session_callback(event):
        user_id = event.sender_id
        if not await require_access(event, alert=True):
            return

        cleanup_result = await cancel_pending_login_flow(
            user_id, reason="upload_session"
        )
        if not cleanup_result.ok:
            await event.answer(t(_language(user_id), "start.session_releasing"), alert=True)
            return

        await event.answer()
        await safe_edit(event,
            t(_language(user_id), "account.upload.prompt"),
            buttons=back_to_add_methods_buttons(user_id),
        )

    @bot.on(events.NewMessage)
    async def handle_account_messages(event):
        user_id = event.sender_id

        if event.text and event.text.startswith('/'):
            return

        state = get_state(user_id)
        file_name, is_session_upload, is_zip_upload = _classify_upload_file(event.file)
        is_supported_upload = is_session_upload or is_zip_upload

        if state.get('adding_account') and not is_supported_upload:
            phone = (event.text or "").strip()
            phone_prompt_message = state.get("phone_prompt_message")

            if not await require_access(event):
                return

            if not AccountManager.PHONE_REGEX.match(phone):
                phone_prompt_message = await edit_status_or_send(
                    bot,
                    user_id,
                    phone_prompt_message,
                    t(_language(user_id), "account.phone.invalid"),
                    buttons=back_to_add_methods_buttons(user_id),
                )
                state["phone_prompt_message"] = phone_prompt_message
                return

            try:
                existing = await AccountManager.check_existing_account_for_add(user_id, phone)
                if existing.action == "block":
                    clear_state(user_id)
                    await update_login_status(
                        bot,
                        user_id,
                        phone_prompt_message,
                        localized_result(_language(user_id), existing.message),
                        buttons=back_to_add_methods_buttons(user_id),
                        parse_mode="md",
                    )
                    return

                rate_limit = login_code_request_rate_limiter.check(user_id)
                if not rate_limit.allowed:
                    phone_prompt_message = await edit_status_or_send(
                        bot,
                        user_id,
                        phone_prompt_message,
                        render_login_code_rate_limit(
                            rate_limit, _language(user_id)
                        ),
                        buttons=back_to_add_methods_buttons(user_id),
                    )
                    state["phone_prompt_message"] = phone_prompt_message
                    return

                state["phone_input_message"] = event
                client = await AccountManager.create_new_client(phone, user_id)
                result = await AccountManager.authenticate(client, phone, user_id)
                phone_accepted = bool(
                    get_state(user_id).get("waiting_code")
                ) or result.startswith("✅")

                if not phone_accepted:
                    raced_rate_limit = isinstance(
                        result, LoginCodeRateLimitMessage
                    )
                    phone_prompt_message = await edit_status_or_send(
                        bot,
                        user_id,
                        phone_prompt_message,
                        localized_result(_language(user_id), result),
                        buttons=back_to_add_methods_buttons(user_id),
                    )
                    retry_state = get_state(user_id)
                    if raced_rate_limit and not retry_state:
                        set_state(
                            user_id,
                            adding_account=True,
                            phone_prompt_message=phone_prompt_message,
                        )
                        retry_state = get_state(user_id)
                    if retry_state:
                        retry_state["phone_prompt_message"] = phone_prompt_message
                    return

                try:
                    edited_prompt = await bot.edit_message(
                        user_id,
                        phone_prompt_message,
                        t(_language(user_id), "account.phone.prompt"),
                        buttons=None,
                    )
                except MessageNotModifiedError:
                    edited_prompt = phone_prompt_message
                if edited_prompt:
                    phone_prompt_message = edited_prompt

                auth_state = get_state(user_id)
                if auth_state:
                    auth_state["phone_prompt_message"] = phone_prompt_message
                    auth_state["phone_input_message"] = event

                status_message = await bot.send_message(user_id, localized_result(_language(user_id), result), buttons=None)
                if auth_state:
                    auth_state["login_status_message"] = status_message
                if result.startswith("✅"):
                    clear_state(user_id)
                    await cleanup_login_ui(state, preserve_message=status_message)
                    await delete_remembered_start_command(user_id)
            except Exception as e:
                await AccountManager.cleanup_pending_login_state(
                    user_id, reason="phone_login_error"
                )
                await update_login_status(
                    bot,
                    user_id,
                    phone_prompt_message,
                    t(_language(user_id), "account.client_failed", error=str(e)),
                    buttons=back_to_add_methods_buttons(user_id),
                )
            return

        if state.get('waiting_code') and not is_supported_upload:
            status_message = state.get("login_status_message")
            await remember_or_delete_sensitive_input(
                state, event, "account login code"
            )
            result = await AccountManager.handle_code(user_id, event.text or "")
            current_state = get_state(user_id)
            if current_state:
                current_state["login_status_message"] = status_message
            status_message = await update_login_status(
                bot,
                user_id,
                status_message,
                localized_result(_language(user_id), result),
                buttons=None,
            )
            if not current_state and result.startswith("✅"):
                await cleanup_login_ui(state, preserve_message=status_message)
            if result.startswith("✅"):
                await delete_remembered_start_command(user_id)
            return

        if state.get('waiting_password') and not is_supported_upload:
            qr_login = bool(state.get("qr_login"))
            status_message = state.get("login_status_message")
            qr_password_status_message = None
            await remember_or_delete_sensitive_input(
                state, event, "account login 2FA"
            )
            if qr_login:
                flow_id = state.get("qr_flow_id", "")
                async with _get_qr_flow_lock(user_id):
                    if not _active_qr_state(user_id, flow_id):
                        return
                    result = await AccountManager.handle_password(
                        user_id, event.text or ""
                    )
                    if state.get("qr_cancel_requested"):
                        if result.startswith("✅") and state.get("auth_phone"):
                            await _rollback_promoted_qr_account(
                                user_id, state["auth_phone"]
                            )
                        return

                    current_state = get_state(user_id)
                    flow_finished = not current_state
                    if flow_finished:
                        set_state(user_id, **state)
                        state = get_state(user_id)
                        current_state = state

                    previous_prompt = state.get("password_prompt_message")
                    response = await edit_status_or_send(
                        bot,
                        user_id,
                        previous_prompt,
                        localized_result(_language(user_id), result),
                        buttons=None,
                    )
                    qr_password_status_message = response
                    state["password_prompt_message"] = response
                    if previous_prompt and previous_prompt is not response:
                        state.setdefault("login_aux_messages", []).append(
                            previous_prompt
                        )
                    if state.get("qr_cancel_requested"):
                        await _delete_message(response, "cancelled_qr_2fa_result")
                        if result.startswith("✅") and state.get("auth_phone"):
                            await _rollback_promoted_qr_account(
                                user_id, state["auth_phone"]
                            )
                        return

                    if flow_finished:
                        await cleanup_login_ui(
                            state, preserve_message=qr_password_status_message
                        )
                        if result.startswith("✅"):
                            await delete_remembered_start_command(user_id)
                        clear_state(user_id)
                return
            else:
                result = await AccountManager.handle_password(user_id, event.text or "")
                current_state = get_state(user_id)
                if current_state:
                    current_state["login_status_message"] = status_message
                status_message = await update_login_status(
                    bot,
                    user_id,
                    status_message,
                    localized_result(_language(user_id), result),
                    buttons=None,
                )

            if not current_state and result.startswith("✅"):
                await cleanup_login_ui(
                    state,
                    preserve_message=status_message,
                )
            if result.startswith("✅"):
                await delete_remembered_start_command(user_id)
            return

        if is_supported_upload:
            if not await require_access(event):
                await delete_sensitive_message(event, "unauthorized session upload")
                return

            declared_size = int(getattr(event.file, "size", 0) or 0)
            if not is_upload_size_allowed(file_name, declared_size):
                limit_text = "200 KB" if is_zip_upload else "40 KB"
                await delete_sensitive_message(event, "oversized session upload")
                await event.respond(t(_language(user_id), "account.upload.too_large", file=file_name, limit=limit_text), parse_mode="md")
                return

            cleanup_result = await cancel_pending_login_flow(
                user_id, reason="upload_file"
            )
            if not cleanup_result.ok:
                await delete_sensitive_message(event, "session upload while login cleanup blocked")
                await event.respond(t(_language(user_id), "account.upload.releasing"))
                return

            temp_path = None
            upload_message_deleted = False
            try:
                await event.respond(t(_language(user_id), "account.upload.processing"))

                suffix = ".zip" if is_zip_upload else ".session"
                fd, temp_path = tempfile.mkstemp(suffix=suffix)
                os.close(fd)
                await event.download_media(file=temp_path)
                upload_message_deleted = await delete_sensitive_message(
                    event, "downloaded session upload"
                )

                actual_size = os.path.getsize(temp_path)
                if not is_upload_size_allowed(file_name, actual_size):
                    limit_text = "200 KB" if is_zip_upload else "40 KB"
                    await event.respond(t(_language(user_id), "account.upload.too_large", file=file_name, limit=limit_text), parse_mode="md")
                    return
                logger.debug(f"用户 {user_id} 上传文件: {file_name}")

                if is_zip_upload:
                    try:
                        entries = find_zip_session_entries(temp_path)
                    except ZipSessionUploadError as error:
                        await event.respond(render_zip_upload_error(error, _language(user_id)))
                        return

                    results = []
                    for entry in entries:
                        label = safe_archive_label(
                            entry.filename, language=_language(user_id)
                        )
                        extracted_path = None
                        try:
                            extracted_path = extract_zip_session_entry(temp_path, entry)
                            _, phone, success, failure_reason = await AccountManager.install_uploaded_session(
                                extracted_path,
                                user_id,
                            )
                            results.append(
                                SessionImportResult(
                                    success=success,
                                    label=label,
                                    phone=phone,
                                    reason=failure_reason,
                                )
                            )
                        except ZipSessionUploadError:
                            results.append(SessionImportResult(False, label))
                        except Exception as error:
                            logger.warning(
                                f"ZIP 内 Session 处理失败: 用户ID={user_id}, "
                                f"文件={label}, 错误={error}"
                            )
                            results.append(SessionImportResult(False, label))
                        finally:
                            if extracted_path and os.path.exists(extracted_path):
                                try:
                                    os.unlink(extracted_path)
                                except OSError:
                                    logger.warning(f"清理 ZIP Session 临时文件失败: {extracted_path}")

                    await event.respond(render_zip_import_summary(results, _language(user_id)))
                    return

                client, phone, success, failure_reason = await AccountManager.install_uploaded_session(
                    temp_path,
                    user_id,
                )

                if success:
                    accounts = AccountManager.get_user_accounts(user_id)
                    if phone in accounts:
                        display_phone = accounts[phone].get('display_phone', phone)
                        language = _language(user_id)
                        anti_login_status = t(language, "account.upload.on") if accounts[phone]['anti_login'] else t(language, "account.upload.off")

                        await event.respond(
                            t(language, "account.upload.success", phone=display_phone,
                              status=anti_login_status,
                              time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                        )
                        logger.info(f"会话上传成功: {display_phone}")
                    else:
                        await event.respond(t(_language(user_id), "account.upload.no_info"))
                elif failure_reason == "invalid":
                    await event.respond(
                        t(_language(user_id), "account.upload.invalid_format", file=file_name)
                    )
                    logger.warning(f"会话文件格式无效: {file_name}")
                elif failure_reason == "existing_session_busy":
                    await event.respond(t(_language(user_id), "account.upload.busy"))
                elif failure_reason == "replace_failed_restored":
                    await event.respond(t(_language(user_id), "account.upload.restore_ok"))
                elif failure_reason == "replace_failed":
                    await event.respond(t(_language(user_id), "account.upload.restore_failed"))
                elif failure_reason == "quota_full":
                    await event.respond(localized_result(_language(user_id), AccountManager.quota_error_message(user_id)))
                else:
                    await event.respond(t(_language(user_id), "account.upload.invalid"))
                    logger.warning(f"会话文件无效: {event.file.name}")

            except Exception as e:
                await event.respond(t(_language(user_id), "account.upload.failed", error=str(e)))
                logger.error(f"处理会话文件失败: {str(e)}")
            finally:
                if not upload_message_deleted:
                    await delete_sensitive_message(event, "session upload final cleanup")
                try:
                    if temp_path and os.path.exists(temp_path):
                        os.unlink(temp_path)
                except Exception:
                    pass
            return
