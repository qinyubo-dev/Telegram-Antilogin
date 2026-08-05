# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import asyncio
import logging
from datetime import datetime, timedelta
from enum import Enum

from telethon.errors import (
    FloodWaitError,
    InputUserDeactivatedError,
    RPCError,
    UserIsBlockedError,
)

from accounts import account_runtime
from storage.data_manager import DataManager
from localization import t

logger = logging.getLogger(__name__)

UNREACHABLE_REMINDER_COOLDOWN_SECONDS = 24 * 3600
RETRYABLE_REMINDER_COOLDOWN_SECONDS = 3600
PERMANENT_UNREACHABLE_REMINDER_REASONS = {
    "user_deleted",
    "bot_blocked",
    "user_deactivated",
}
UNREACHABLE_REMINDER_REASON_LABELS = {
    "entity_not_found": "找不到entity",
    "user_deleted": "用户删除",
    "bot_blocked": "bot被拉黑",
    "user_deactivated": "用户停用",
}


class ReminderFailureKind(str, Enum):
    PERMANENT_USER = "permanent_user"
    TEMPORARY_USER = "temporary_user"
    BOT_FATAL = "bot_fatal"
    FLOOD_WAIT = "flood_wait"
    TELEGRAM_TRANSIENT = "telegram_transient"
    UNKNOWN = "unknown"


class ReminderSystem:
    """订阅到期提醒系统"""
    
    def __init__(self, bot):
        self.bot = bot
        self.monitoring_task = None
        self.sent_reminders = set()  # 记录已发送的提醒
        self.failed_reminder_cooldowns = {}  # user_id -> next retry timestamp
        self.global_retry_after = 0.0

    @staticmethod
    def _classify_unreachable_user_error(error: Exception) -> str | None:
        if isinstance(error, UserIsBlockedError):
            return "bot_blocked"
        if isinstance(error, InputUserDeactivatedError):
            return "user_deactivated"
        error_name = error.__class__.__name__.lower()
        error_text = f"{error_name} {str(error).lower()}"
        if (
            "could not find the input entity" in error_text
            or "entity not found" in error_text
            or "cannot find any entity" in error_text
        ):
            return "entity_not_found"
        if (
            "specified user was deleted" in error_text
            or "user was deleted" in error_text
            or "userdeleted" in error_text
        ):
            return "user_deleted"
        if (
            "bot was blocked by the user" in error_text
            or "blocked by the user" in error_text
            or "userisblocked" in error_text
        ):
            return "bot_blocked"
        if (
            "user is deactivated" in error_text
            or "inputuserdeactivated" in error_text
            or "userdeactivated" in error_text
        ):
            return "user_deactivated"
        return None

    @classmethod
    def _classify_send_error(cls, error: Exception) -> tuple[ReminderFailureKind, str]:
        if account_runtime.is_notify_bot_fatal_error(error):
            return ReminderFailureKind.BOT_FATAL, "bot_authorization"
        if isinstance(error, FloodWaitError):
            return ReminderFailureKind.FLOOD_WAIT, "flood_wait"

        unreachable_reason = cls._classify_unreachable_user_error(error)
        if unreachable_reason in PERMANENT_UNREACHABLE_REMINDER_REASONS:
            return ReminderFailureKind.PERMANENT_USER, unreachable_reason
        if unreachable_reason:
            return ReminderFailureKind.TEMPORARY_USER, unreachable_reason

        if isinstance(error, (RPCError, ConnectionError, TimeoutError, OSError)):
            return ReminderFailureKind.TELEGRAM_TRANSIENT, type(error).__name__
        return ReminderFailureKind.UNKNOWN, type(error).__name__

    def _cleanup_permanently_unreachable_user(self, user_id: int, reason: str) -> bool:
        self.failed_reminder_cooldowns.pop(user_id, None)
        if reason == "user_deleted":
            self.sent_reminders = {
                reminder_key
                for reminder_key in self.sent_reminders
                if not reminder_key.startswith(f"{user_id}_")
            }
            return DataManager.delete_user_data(user_id)
        return True
    
    async def check_expiring_vip(self) -> bool:
        """检查即将到期的VIP用户"""
        try:
            had_failures = False
            unreachable_failures = []
            permanently_skipped_failures = []
            retryable_failures = []
            reminder_days = DataManager.get_expiry_reminder_days()
            expiring_users = DataManager.get_expiring_subscription_users(reminder_days)
            if self.global_retry_after > datetime.now().timestamp():
                return True
            
            for user_info in expiring_users:
                user_id = user_info['user_id']
                days_left = user_info['days_left']
                expiry_date = user_info['expiry']
                now_ts = datetime.now().timestamp()

                if self.failed_reminder_cooldowns.get(user_id, 0) > now_ts:
                    continue
                
                # 生成提醒标识
                reminder_key = f"{user_id}_{expiry_date.strftime('%Y%m%d')}"
                
                # 检查是否已经发送过提醒
                if (
                    reminder_key not in self.sent_reminders
                    and not DataManager.was_expiry_reminder_sent(user_id, expiry_date)
                ):
                    try:
                        if days_left == 0:
                            message = t(DataManager.get_user_language(user_id), "reminder.today",
                                        expiry=expiry_date.strftime('%Y-%m-%d %H:%M'))
                        else:
                            message = t(DataManager.get_user_language(user_id), "reminder.soon",
                                        days=days_left,
                                        expiry=expiry_date.strftime('%Y-%m-%d %H:%M'))
                        
                        await self.bot.send_message(user_id, message)
                        account_runtime.mark_notify_bot_healthy()
                        if not DataManager.mark_expiry_reminder_sent(
                            user_id, expiry_date, days_left
                        ):
                            had_failures = True
                            logger.error("到期提醒已发送但持久化失败: 用户ID=%s", user_id)
                            continue
                        self.sent_reminders.add(reminder_key)
                        logger.info(f"✅ 已发送到期提醒给用户 {user_id}, 剩余 {days_left} 天")
                        
                    except Exception as e:
                        failure_kind, reason = self._classify_send_error(e)
                        if failure_kind == ReminderFailureKind.BOT_FATAL:
                            original_error = getattr(e, "original_error", None) or e
                            account_runtime.mark_notify_bot_fatal(original_error)
                            if isinstance(e, account_runtime.NotifyBotFatalError):
                                raise
                            raise account_runtime.NotifyBotFatalError(
                                "到期提醒发现主 Bot 授权失效",
                                original_error=e,
                            ) from e
                        if failure_kind == ReminderFailureKind.TELEGRAM_TRANSIENT:
                            account_runtime.mark_notify_bot_degraded(e)
                        if failure_kind == ReminderFailureKind.PERMANENT_USER:
                            self.sent_reminders.add(reminder_key)
                            cleaned = self._cleanup_permanently_unreachable_user(user_id, reason)
                            if not cleaned:
                                had_failures = True
                            permanently_skipped_failures.append((user_id, reason))
                        elif failure_kind == ReminderFailureKind.TEMPORARY_USER:
                            self.failed_reminder_cooldowns[user_id] = now_ts + UNREACHABLE_REMINDER_COOLDOWN_SECONDS
                            unreachable_failures.append((user_id, reason))
                        elif failure_kind == ReminderFailureKind.FLOOD_WAIT:
                            wait_seconds = max(1, int(getattr(e, "seconds", RETRYABLE_REMINDER_COOLDOWN_SECONDS)))
                            self.global_retry_after = now_ts + wait_seconds
                            logger.warning("到期提醒触发 FloodWait，全局暂停 %d 秒", wait_seconds)
                            had_failures = True
                            break
                        elif failure_kind == ReminderFailureKind.TELEGRAM_TRANSIENT:
                            self.global_retry_after = now_ts + RETRYABLE_REMINDER_COOLDOWN_SECONDS
                            logger.warning(
                                "到期提醒 Telegram 临时故障，全局暂停 %d 秒: %s: %s",
                                RETRYABLE_REMINDER_COOLDOWN_SECONDS,
                                reason,
                                str(e)[:160],
                            )
                            had_failures = True
                            break
                        else:
                            self.failed_reminder_cooldowns[user_id] = now_ts + RETRYABLE_REMINDER_COOLDOWN_SECONDS
                            retryable_failures.append((user_id, f"{reason}: {e}"))
                            logger.exception("发送到期提醒发生未知异常: 用户ID=%s", user_id)
                            had_failures = True

            if unreachable_failures:
                logger.debug(
                    "到期提醒跳过 %d 个不可达用户，已暂停重试 24 小时: %s",
                    len(unreachable_failures),
                    ", ".join(
                        f"{user_id}({UNREACHABLE_REMINDER_REASON_LABELS.get(reason, reason)})"
                        for user_id, reason in unreachable_failures[:20]
                    ),
                )
            if permanently_skipped_failures:
                logger.info(
                    "到期提醒永久跳过 %d 个不可达用户: %s",
                    len(permanently_skipped_failures),
                    ", ".join(
                        f"{user_id}({UNREACHABLE_REMINDER_REASON_LABELS.get(reason, reason)})"
                        for user_id, reason in permanently_skipped_failures[:20]
                    ),
                )
            for user_id, error in retryable_failures:
                logger.warning(f"发送到期提醒临时失败 {user_id}: {error}")
            
            # 清理过期的提醒记录（超过到期时间1天的记录）
            self._cleanup_old_reminders()
            return had_failures
            
        except account_runtime.NotifyBotFatalError:
            raise
        except Exception:
            logger.exception("检查到期VIP失败")
            return True
    
    def _cleanup_old_reminders(self):
        """清理过期的提醒记录"""
        current_time = datetime.now()
        keys_to_remove = []
        
        for reminder_key in self.sent_reminders:
            try:
                user_id_str, date_str = reminder_key.split('_')
                expiry_date = datetime.strptime(date_str, '%Y%m%d')
                
                # 如果到期时间已经过去1天，清理记录
                if current_time > expiry_date + timedelta(days=1):
                    keys_to_remove.append(reminder_key)
            except:
                keys_to_remove.append(reminder_key)
        
        for key in keys_to_remove:
            self.sent_reminders.remove(key)

        now_ts = current_time.timestamp()
        expired_users = [
            user_id for user_id, retry_ts in self.failed_reminder_cooldowns.items()
            if retry_ts <= now_ts
        ]
        for user_id in expired_users:
            self.failed_reminder_cooldowns.pop(user_id, None)
    
    async def start_monitoring(self):
        """启动提醒监控"""
        if self.monitoring_task and not self.monitoring_task.done():
            logger.debug("VIP到期提醒监控已在运行，跳过重复启动")
            return self.monitoring_task
        logger.debug("启动VIP到期提醒监控...")
        self.monitoring_task = asyncio.create_task(
            self._monitor_reminders(), name="vip-expiry-reminders"
        )
        self.monitoring_task.add_done_callback(self._monitoring_done)
        return self.monitoring_task

    def _monitoring_done(self, task: asyncio.Task):
        if task.cancelled():
            return
        error = task.exception()
        if error:
            logger.error(
                "VIP到期提醒监控意外退出: %s: %s",
                type(error).__name__,
                str(error)[:160],
            )
        else:
            logger.error("VIP到期提醒监控意外结束但未返回错误")

    async def _monitor_reminders(self):
        """监控提醒（索引驱动：按“下一次需要提醒的时间”睡眠，避免固定全量扫描）"""
        while True:
            try:
                # 先做一次检查（会只遍历 VIP 索引）
                had_failures = await self.check_expiring_vip()

                # 计算下一次需要唤醒的时间点
                reminder_days = DataManager.get_expiry_reminder_days()
                now = datetime.now()
                next_wakeup = None

                for user_id, expiry_date in DataManager.iter_subscription_users():
                    reminder_key = f"{user_id}_{expiry_date.strftime('%Y%m%d')}"
                    if (
                        reminder_key in self.sent_reminders
                        or DataManager.was_expiry_reminder_sent(user_id, expiry_date)
                    ):
                        continue

                    window_start = expiry_date - timedelta(days=reminder_days)
                    if window_start > now:
                        if next_wakeup is None or window_start < next_wakeup:
                            next_wakeup = window_start

                retry_timestamps = [
                    retry_at
                    for retry_at in self.failed_reminder_cooldowns.values()
                    if retry_at > now.timestamp()
                ]
                if retry_timestamps:
                    retry_wakeup = datetime.fromtimestamp(min(retry_timestamps))
                    if next_wakeup is None or retry_wakeup < next_wakeup:
                        next_wakeup = retry_wakeup

                if self.global_retry_after > now.timestamp():
                    sleep_seconds = max(
                        1, int(self.global_retry_after - now.timestamp())
                    )
                elif had_failures:
                    # 有发送失败时，稍后重试
                    sleep_seconds = 300
                elif next_wakeup is None:
                    # 没有待提醒的用户，退化为较低频率巡检（防止错过新增/变更）
                    sleep_seconds = 6 * 3600
                else:
                    sleep_seconds = max(60, int((next_wakeup - now).total_seconds()))

                    # 给一个上限，避免睡太久（期间可能有新增/续费）
                    sleep_seconds = min(sleep_seconds, 6 * 3600)

                await asyncio.sleep(sleep_seconds)

            except account_runtime.NotifyBotFatalError:
                raise
            except Exception:
                logger.exception("提醒监控异常")
                await asyncio.sleep(300)  # 出错后等待5分钟
    
    async def stop_monitoring(self):
        """停止提醒监控"""
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                logger.debug("提醒监控已停止")
            finally:
                self.monitoring_task = None
