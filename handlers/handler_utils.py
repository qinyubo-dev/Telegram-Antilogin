# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import logging

from telethon.errors import (
    MessageIdInvalidError,
    MessageNotModifiedError,
    QueryIdInvalidError,
    UnauthorizedError,
)
from telethon.tl.custom import Button

from accounts.account_manager import AccountManager
from accounts import account_runtime
from accounts.account_runtime import get_default_runtime
from storage.data_manager import DataManager
from localization import t


logger = logging.getLogger(__name__)

BACK_CUSTOM_EMOJI_ID = 5877629862306385808


def back_button(data, user_id=None, language=None):
    language = language or (
        DataManager.get_user_language(user_id) if user_id is not None else "zh"
    )
    return Button.inline(t(language, "common.back"), data, icon=BACK_CUSTOM_EMOJI_ID)


def back_to_main_buttons(user_id=None, language=None):
    return [[back_button(b"back_to_main", user_id=user_id, language=language)]]


_user_states = get_default_runtime().user_states
_start_command_messages = {}
_main_menu_messages = {}
_flow_messages = {}


def set_state(user_id: int, **values):
    _user_states[user_id] = values


def paginate_items(items, page: int, page_size: int = 25):
    total = len(items)
    max_page = max(0, (total - 1) // page_size) if total else 0
    page = max(0, min(page, max_page))
    start = page * page_size
    return items[start:start + page_size], page, max_page


def pagination_buttons(prefix: str, page: int, max_page: int, language: str = "zh"):
    if max_page <= 0:
        return []

    nav = []
    if page > 0:
        nav.append(Button.inline(t(language, "common.previous"), f"{prefix}_{page - 1}".encode()))
    nav.append(Button.inline(f"{page + 1}/{max_page + 1}", b"pagination_noop"))
    if page < max_page:
        nav.append(Button.inline(t(language, "common.next"), f"{prefix}_{page + 1}".encode()))
    return nav


async def safe_edit(event, *args, ignore_invalid: bool = False, **kwargs):
    try:
        return await event.edit(*args, **kwargs)
    except MessageNotModifiedError:
        return None
    except MessageIdInvalidError:
        if not ignore_invalid:
            raise
        logger.info(
            "Skipped editing a stale callback message: user_id=%s, message_id=%s",
            getattr(event, "sender_id", "unknown"),
            getattr(event, "message_id", getattr(event, "id", "unknown")),
        )
        return None


async def safe_answer_callback(event, *args, **kwargs) -> bool:
    """Acknowledge a callback while treating an expired query as a harmless race."""
    try:
        await event.answer(*args, **kwargs)
        return True
    except QueryIdInvalidError:
        logger.info(
            "回调确认已过期: 用户ID=%s, 消息ID=%s",
            getattr(event, "sender_id", "unknown"),
            getattr(event, "message_id", getattr(event, "id", "unknown")),
        )
        return False


async def safe_edit_message(event, *args, **kwargs):
    """Edit a callback message and retain a usable message on a no-op edit."""
    try:
        return await event.edit(*args, **kwargs)
    except MessageNotModifiedError:
        return await event.get_message()


async def _delete_message_best_effort(
    event, context: str, *, sensitive: bool
) -> bool:
    try:
        await event.delete()
        return True
    except Exception as error:
        log = logger.warning if sensitive else logger.info
        log(
            "%s: 用户ID=%s, 消息ID=%s, 场景=%s, 异常类型=%s",
            "无法删除敏感输入" if sensitive else "提示消息无法删除",
            getattr(event, "sender_id", "unknown"),
            getattr(event, "id", getattr(event, "message_id", "unknown")),
            context,
            type(error).__name__,
        )
        return False


async def delete_sensitive_message(event, context: str = "sensitive input") -> bool:
    """Best-effort deletion for a user message containing a secret."""
    return await _delete_message_best_effort(event, context, sensitive=True)


async def delete_prompt_message(event, context: str = "prompt") -> bool:
    """Best-effort deletion for a non-sensitive bot prompt."""
    return await _delete_message_best_effort(event, context, sensitive=False)


async def edit_or_respond(event, *args, **kwargs):
    try:
        return await safe_edit(event, *args, **kwargs)
    except UnauthorizedError as e:
        account_runtime.raise_notify_bot_fatal(
            e, "编辑主 Bot 消息时发现授权失效"
        )
    except Exception:
        try:
            return await event.respond(*args, **kwargs)
        except UnauthorizedError as e:
            account_runtime.raise_notify_bot_fatal(
                e, "回复主 Bot 消息时发现授权失效"
            )


def get_state(user_id: int):
    return _user_states.get(user_id, {})


def clear_state(user_id: int, *keys):
    if not keys:
        _user_states.pop(user_id, None)
        return
    state = _user_states.get(user_id)
    if not state:
        return
    for key in keys:
        state.pop(key, None)
    if not state:
        _user_states.pop(user_id, None)


def remember_start_command_message(user_id: int, message) -> None:
    """Remember the latest /start command that opened the current menu flow."""
    _start_command_messages[int(user_id)] = message


async def delete_remembered_start_command(user_id: int) -> bool:
    """Delete and forget the /start command associated with the current flow."""
    message = _start_command_messages.pop(int(user_id), None)
    if not message:
        return False
    try:
        await message.delete()
        return True
    except Exception:
        logger.warning("无法删除扫码流程对应的 /start 消息: 用户ID=%s", user_id)
        return False


def remember_main_menu_message(user_id: int, message) -> None:
    """Remember the latest main-menu message sent for a user."""
    if message is not None:
        _main_menu_messages[int(user_id)] = message


async def delete_remembered_main_menu(user_id: int) -> bool:
    """Delete and forget the previously sent main-menu message."""
    message = _main_menu_messages.pop(int(user_id), None)
    if not message:
        return False
    try:
        await message.delete()
        return True
    except Exception:
        logger.info("无法删除旧的主菜单消息: 用户ID=%s", user_id)
        return False


def remember_flow_message(user_id: int, message) -> None:
    """Track a message that belongs only to an unfinished interaction flow."""
    if message is None:
        return
    messages = _flow_messages.setdefault(int(user_id), [])
    if not any(_same_message(message, existing) for existing in messages):
        messages.append(message)


def forget_flow_message(user_id: int, message=None) -> None:
    """Stop tracking one flow message, or every flow message for the user."""
    key = int(user_id)
    if message is None:
        _flow_messages.pop(key, None)
        return
    messages = [
        existing for existing in _flow_messages.get(key, [])
        if not _same_message(existing, message)
    ]
    if messages:
        _flow_messages[key] = messages
    else:
        _flow_messages.pop(key, None)


def _same_message(left, right) -> bool:
    if left is right:
        return True
    left_id = getattr(left, "id", None)
    right_id = getattr(right, "id", None)
    left_chat_id = getattr(left, "chat_id", None)
    right_chat_id = getattr(right, "chat_id", None)
    return (
        left_id is not None
        and right_id is not None
        and left_id == right_id
        and left_chat_id == right_chat_id
    )


async def delete_remembered_flow_messages(user_id: int, preserve_message=None) -> bool:
    """Best-effort deletion of messages belonging to an abandoned flow."""
    messages = _flow_messages.pop(int(user_id), [])
    ok = True
    for message in messages:
        if _same_message(message, preserve_message):
            continue
        if not await _delete_message_best_effort(
            message, "abandoned interaction flow", sensitive=False
        ):
            ok = False
    return ok


async def require_access(event, alert: bool = False) -> bool:
    if not account_runtime.is_ready():
        language = DataManager.get_user_language(event.sender_id)
        if hasattr(event, "answer"):
            await event.answer(t(language, "common.starting"), alert=alert)
        else:
            await event.respond(t(language, "common.starting"))
        return False

    if AccountManager.check_access(event.sender_id):
        return True

    if hasattr(event, "answer"):
        await event.answer(t(DataManager.get_user_language(event.sender_id), "common.no_access"), alert=alert)
    else:
        await event.respond(t(DataManager.get_user_language(event.sender_id), "common.no_access"))
    return False


async def require_admin(event, alert: bool = False) -> bool:
    if DataManager.is_admin(event.sender_id):
        return True

    language = DataManager.get_user_language(event.sender_id)
    if hasattr(event, "answer"):
        await event.answer(t(language, "admin.access_denied"), alert=alert)
    else:
        await event.respond(t(language, "admin.access_denied"))
    return False
