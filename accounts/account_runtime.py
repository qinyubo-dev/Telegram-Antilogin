# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import asyncio
import functools
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

from telethon.errors import (
    AccessTokenInvalidError,
    ApiIdInvalidError,
    AuthKeyDuplicatedError,
    AuthKeyInvalidError,
    AuthKeyUnregisteredError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    QueryIdInvalidError,
)


logger = logging.getLogger(__name__)

NOTIFY_BOT_FATAL_ERRORS = (
    AccessTokenInvalidError,
    ApiIdInvalidError,
    SessionRevokedError,
    SessionExpiredError,
    AuthKeyUnregisteredError,
    AuthKeyInvalidError,
    AuthKeyDuplicatedError,
    UserDeactivatedError,
    UserDeactivatedBanError,
)


class NotifyBotFatalError(RuntimeError):
    """Raised when the main notification Bot cannot recover without re-authorization."""

    def __init__(self, message: str, original_error: Exception | None = None):
        super().__init__(message)
        self.original_error = original_error


@dataclass(frozen=True)
class NotifyBotHealth:
    status: str
    error_type: Optional[str] = None
    reason: Optional[str] = None
    changed_at: Optional[datetime] = None


# 全局存储
@dataclass
class AccountRuntime:
    """Mutable account state owned by one running bot instance."""

    user_accounts: Dict[int, Dict[str, Dict]] = field(default_factory=dict)
    user_states: Dict[int, Dict] = field(default_factory=dict)
    client_tasks: Dict[str, asyncio.Task] = field(default_factory=dict)
    session_locks: Dict[str, asyncio.Lock] = field(default_factory=dict)
    account_operation_locks: Dict[str, asyncio.Lock] = field(default_factory=dict)
    code_waiters: Dict[str, set] = field(default_factory=dict)
    pause_tasks: Dict[str, asyncio.Task] = field(default_factory=dict)
    hosting_action_cooldowns: Dict[str, float] = field(default_factory=dict)
    code_fetch_tasks: Dict[str, asyncio.Task] = field(default_factory=dict)
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def task_registries(self):
        return (
            self.client_tasks,
            self.pause_tasks,
            self.code_fetch_tasks,
        )

    def register_task(self, registry: Dict[str, asyncio.Task], key: str, task: asyncio.Task):
        previous = registry.get(key)
        if previous and previous is not task and not previous.done():
            previous.cancel()
        registry[key] = task

        def discard(done_task: asyncio.Task):
            if registry.get(key) is done_task:
                registry.pop(key, None)

        task.add_done_callback(discard)
        return task

    async def cancel_task(self, registry: Dict[str, asyncio.Task], key: str) -> None:
        task = registry.pop(key, None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        self.ready_event.clear()
        tasks = []
        for registry in self.task_registries:
            tasks.extend(task for task in registry.values() if not task.done())
            registry.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.account_operation_locks.clear()


_default_runtime = AccountRuntime()


def get_default_runtime() -> AccountRuntime:
    return _default_runtime


def mark_ready() -> None:
    _default_runtime.ready_event.set()


def mark_not_ready() -> None:
    _default_runtime.ready_event.clear()


def is_ready() -> bool:
    return _default_runtime.ready_event.is_set()


user_accounts = _default_runtime.user_accounts
user_states = _default_runtime.user_states
client_tasks = _default_runtime.client_tasks
session_locks = _default_runtime.session_locks
account_operation_locks = _default_runtime.account_operation_locks

# 主动获取验证码模式
code_waiters = _default_runtime.code_waiters  # phone -> set(user_id)

# 反登录暂停（30分钟）管理
pause_tasks = _default_runtime.pause_tasks
hosting_action_cooldowns = _default_runtime.hosting_action_cooldowns
code_fetch_tasks = _default_runtime.code_fetch_tasks

_notify_bot = None
_login_unlock_reminder_system = None
_notify_bot_health = NotifyBotHealth(
    status="starting",
    changed_at=datetime.now(timezone.utc),
)
_notify_bot_fatal_event = asyncio.Event()


def set_notify_bot(bot):
    global _notify_bot, _notify_bot_health, _notify_bot_fatal_event
    _notify_bot = bot
    _notify_bot_health = NotifyBotHealth(
        status="starting",
        changed_at=datetime.now(timezone.utc),
    )
    _notify_bot_fatal_event = asyncio.Event()


def get_notify_bot():
    return _notify_bot


def set_login_unlock_reminder_system(system) -> None:
    global _login_unlock_reminder_system
    _login_unlock_reminder_system = system


def get_login_unlock_reminder_system():
    return _login_unlock_reminder_system


def get_notify_bot_health() -> NotifyBotHealth:
    return _notify_bot_health


def is_notify_bot_fatal_error(error: Exception) -> bool:
    if isinstance(error, NotifyBotFatalError):
        return True
    return isinstance(error, NOTIFY_BOT_FATAL_ERRORS)


def raise_notify_bot_fatal(error: Exception, context: str) -> None:
    """Publish a fatal main-Bot failure and raise the canonical runtime error."""
    original_error = getattr(error, "original_error", None) or error
    mark_notify_bot_fatal(original_error)
    if isinstance(error, NotifyBotFatalError):
        raise error
    raise NotifyBotFatalError(context, original_error=error) from error


def _guard_notify_bot_handler(callback):
    @functools.wraps(callback)
    async def guarded(event):
        # Receiving an update proves the main Bot is usable without an
        # application-level polling request.
        mark_notify_bot_healthy()
        answer = getattr(event, "answer", None)
        if callable(answer) and not getattr(event, "_safe_callback_answer_installed", False):
            async def safe_answer(*args, **kwargs):
                try:
                    return await answer(*args, **kwargs)
                except QueryIdInvalidError:
                    logger.info(
                        "回调确认已过期: handler=%s, user_id=%s",
                        callback.__name__,
                        getattr(event, "sender_id", "unknown"),
                    )
                    return None

            try:
                event.answer = safe_answer
                event._safe_callback_answer_installed = True
            except (AttributeError, TypeError):
                pass
        try:
            return await callback(event)
        except NotifyBotFatalError as error:
            raise_notify_bot_fatal(error, f"主 Bot 事件处理器授权失效: {callback.__name__}")
        except NOTIFY_BOT_FATAL_ERRORS as error:
            raise_notify_bot_fatal(error, f"主 Bot 事件处理器授权失效: {callback.__name__}")

    return guarded


def install_notify_bot_handler_guards(bot) -> int:
    """Wrap all currently registered handlers with a fatal-error boundary.

    Only Telethon's public handler-management APIs are used. Registration
    order and each original EventBuilder are retained, and repeated calls are
    intentionally a no-op.
    """
    if getattr(bot, "_notify_bot_handler_guards_installed", False):
        return 0

    registrations = list(bot.list_event_handlers())
    guarded_callbacks = {}
    for callback, _ in registrations:
        if callback not in guarded_callbacks:
            guarded_callbacks[callback] = _guard_notify_bot_handler(callback)
            bot.remove_event_handler(callback)

    for callback, event_builder in registrations:
        bot.add_event_handler(guarded_callbacks[callback], event_builder)

    bot._notify_bot_handler_guards_installed = True
    return len(registrations)


def mark_notify_bot_healthy() -> NotifyBotHealth:
    global _notify_bot_health
    if _notify_bot_health.status != "fatal":
        _notify_bot_health = NotifyBotHealth(
            status="healthy",
            changed_at=datetime.now(timezone.utc),
        )
    return _notify_bot_health


def mark_notify_bot_degraded(error: Exception) -> NotifyBotHealth:
    global _notify_bot_health
    if _notify_bot_health.status != "fatal":
        _notify_bot_health = NotifyBotHealth(
            status="degraded",
            error_type=type(error).__name__,
            reason=str(error)[:240],
            changed_at=datetime.now(timezone.utc),
        )
    return _notify_bot_health


def mark_notify_bot_fatal(error: Exception) -> NotifyBotHealth:
    global _notify_bot_health
    if _notify_bot_health.status != "fatal":
        _notify_bot_health = NotifyBotHealth(
            status="fatal",
            error_type=type(error).__name__,
            reason=str(error)[:240],
            changed_at=datetime.now(timezone.utc),
        )
        logger.critical(
            "主 Bot 授权永久失效: 错误类型=%s, 错误=%s",
            _notify_bot_health.error_type,
            _notify_bot_health.reason,
        )
        _notify_bot_fatal_event.set()
    return _notify_bot_health


async def wait_notify_bot_fatal() -> NotifyBotHealth:
    await _notify_bot_fatal_event.wait()
    return _notify_bot_health
