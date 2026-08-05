# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import asyncio
import os
import logging
from logging.handlers import TimedRotatingFileHandler
import threading
import time
import telethon
from telethon import TelegramClient
from telethon.errors import AccessTokenInvalidError, ApiIdInvalidError
from storage.data_manager import DataManager
from storage.admin_audit import AdminAuditLog
from accounts.account_manager import AccountManager
from accounts.login_code_rate_limiter import login_code_request_rate_limiter
from accounts import account_runtime
from payments.payment_system import PaymentSystem
from reminders.reminder_system import ReminderSystem
from reminders.login_unlock_reminder import LoginUnlockReminderSystem
from handlers.bot_handlers import setup_bot_handlers
from localization import t
from project_info import PROJECT_ATTRIBUTION
import settings as config


class HighlightErrorFormatter(logging.Formatter):
    """让错误级别日志更醒目，同时不需要改动各处 logger 调用。"""

    ERROR_LABELS = {
        logging.ERROR: "!!! ERROR !!!",
        logging.CRITICAL: "!!! CRITICAL !!!",
    }

    def format(self, record):
        original_levelname = record.levelname
        if record.levelno >= logging.CRITICAL:
            record.levelname = self.ERROR_LABELS[logging.CRITICAL]
        elif record.levelno >= logging.ERROR:
            record.levelname = self.ERROR_LABELS[logging.ERROR]
        try:
            return super().format(record)
        finally:
            record.levelname = original_levelname


class SuppressTelethonOlderMessageFilter(logging.Filter):
    def filter(self, record):
        return not (
            record.name == "telethon.network.mtprotostate"
            and "Server resent the older message" in record.getMessage()
        )


class SuppressTelethonMissingChannelHashFilter(logging.Filter):
    """Ignore catch-up noise for unrelated channels missing a cached access hash."""

    def filter(self, record):
        return not (
            record.name == "telethon.client.telegrambaseclient"
            and "No access_hash in cache for channel" in record.getMessage()
            and "will not catch up" in record.getMessage()
        )


class SuppressTelethonTransientUpdateFilter(logging.Filter):
    """Ignore channel update recovery because this service does not use channels."""

    def filter(self, record):
        message = record.getMessage()
        channel_difference_failure = (
            record.name.startswith("telethon.")
            and "GetChannelDifferenceRequest" in message
        )
        premature_difference_end = (
            record.name == "telethon.client.updates"
            and "channel updates" in message
            and "ending getting difference prematurely" in message
        )
        return not (channel_difference_failure or premature_difference_end)


class SuppressTelethonRoutineWarningFilter(logging.Filter):
    """Hide known recoverable Telethon warnings that require no operator action."""

    def filter(self, record):
        if record.levelno != logging.WARNING:
            return True

        message = record.getMessage()
        connection_closed = (
            record.name == "telethon.network.connection.connection"
            and "Server closed the connection:" in message
            and (
                "Connection reset by peer" in message
                or "0 bytes read on a total of" in message
            )
        )
        authorization_lookup_failed = (
            record.name == "telethon.client.users"
            and "RpcMcgetFailError" in message
            and "caused by GetAuthorizationsRequest" in message
        )
        logged_out_channel_difference = (
            record.name == "telethon.client.updates"
            and "Cannot get difference for channel" in message
            and "since the account is not logged in" in message
            and "AuthKeyUnregisteredError" in message
        )
        return not (
            connection_closed
            or authorization_lookup_failed
            or logged_out_channel_difference
        )


def _is_telethon_msgid_retry(record: logging.LogRecord) -> bool:
    return (
        record.name == "telethon.client.users"
        and "MsgidDecreaseRetryError" in record.getMessage()
    )


class SuppressTelethonMsgidRetryFromConsoleFilter(logging.Filter):
    def filter(self, record):
        return not _is_telethon_msgid_retry(record)


class DowngradeTelethonMsgidRetryFilter(logging.Filter):
    def __init__(self, minimum_level: int):
        super().__init__()
        self.minimum_level = minimum_level

    def filter(self, record):
        if not _is_telethon_msgid_retry(record):
            return True
        if self.minimum_level > logging.DEBUG:
            return False
        record.levelno = logging.DEBUG
        record.levelname = logging.getLevelName(logging.DEBUG)
        return True


class SanitizeTelethonProtocolErrorFilter(logging.Filter):
    """Remove raw Telegram payload bytes from parser warnings before logging."""

    def filter(self, record):
        message = record.getMessage()
        if (
            record.name == "telethon.client.updates"
            and "matching Constructor ID" in message
            and "Remaining bytes:" in message
        ):
            safe_message = message.split("Remaining bytes:", 1)[0].rstrip()
            record.msg = f"{safe_message} Raw payload omitted."
            record.args = ()
        return True


class RateLimitTelethonSyncWarningsFilter(logging.Filter):
    """Keep the first sync warning per window and summarize suppressed repeats."""

    SIGNATURES = (
        (
            "telethon.network.mtprotosender",
            "connecting failed: TimeoutError",
            "ConnectTimeoutError",
        ),
        (
            "telethon.network.connection.connection",
            "Connection reset by peer",
            "ConnectionResetByPeer",
        ),
        (
            "telethon.network.connection.connection",
            "0 bytes read on a total of",
            "ConnectionClosedBeforeHeader",
        ),
        (
            "telethon.network.mtprotosender",
            "wrong session ID",
            "WrongSessionId",
        ),
        (
            "telethon.network.mtprotosender",
            "invalid new nonce hash",
            "InvalidNewNonceHash",
        ),
        (
            "telethon.client.updates",
            "matching Constructor ID",
            "TypeNotFound",
        ),
    )

    def __init__(self, window_seconds: int):
        super().__init__()
        self.window_seconds = max(1, window_seconds)
        self._states = {}
        self._lock = threading.Lock()

    def filter(self, record):
        message = record.getMessage()
        signature = next(
            (
                key
                for logger_name, message_fragment, key in self.SIGNATURES
                if record.name == logger_name and message_fragment in message
            ),
            None,
        )
        if not signature:
            return True

        state_key = (record.name, signature)
        now = time.monotonic()
        with self._lock:
            state = self._states.get(state_key)
            if state is not None:
                last_at, suppressed = state
                if now - last_at < self.window_seconds:
                    self._states[state_key] = (last_at, suppressed + 1)
                    return False
            else:
                suppressed = 0
            self._states[state_key] = (now, 0)

        if suppressed:
            record.msg = f"{message}（过去窗口已抑制 {suppressed} 条同类告警）"
            record.args = ()
        return True


def configure_logging():
    formatter = HighlightErrorFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_level_name = os.getenv("BOT_LOG_FILE_LEVEL", "DEBUG").upper()
    file_level = getattr(logging, file_level_name, logging.DEBUG)
    log_directory = os.getenv(
        "BOT_LOG_DIR",
        str(getattr(config, "BOT_LOG_DIR", "logs")),
    )
    os.makedirs(log_directory, exist_ok=True)
    log_path = os.path.join(log_directory, "bot_runtime.log")
    suppress_older_message_filter = SuppressTelethonOlderMessageFilter()
    suppress_missing_channel_hash_filter = SuppressTelethonMissingChannelHashFilter()
    suppress_transient_update_filter = SuppressTelethonTransientUpdateFilter()
    suppress_routine_warning_filter = SuppressTelethonRoutineWarningFilter()
    sync_warning_window = int(
        os.getenv(
            "TELETHON_SYNC_WARNING_WINDOW_SECONDS",
            str(getattr(config, "TELETHON_SYNC_WARNING_WINDOW_SECONDS", 1800)),
        )
    )
    log_retention_days = max(
        1,
        int(
            os.getenv(
                "BOT_LOG_RETENTION_DAYS",
                str(getattr(config, "BOT_LOG_RETENTION_DAYS", 30)),
            )
        ),
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(suppress_older_message_filter)
    stream_handler.addFilter(suppress_missing_channel_hash_filter)
    stream_handler.addFilter(suppress_transient_update_filter)
    stream_handler.addFilter(suppress_routine_warning_filter)
    stream_handler.addFilter(SuppressTelethonMsgidRetryFromConsoleFilter())
    stream_handler.addFilter(SanitizeTelethonProtocolErrorFilter())
    stream_handler.addFilter(RateLimitTelethonSyncWarningsFilter(sync_warning_window))

    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=log_retention_days,
        encoding="utf-8",
        utc=False,
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(suppress_older_message_filter)
    file_handler.addFilter(suppress_missing_channel_hash_filter)
    file_handler.addFilter(suppress_transient_update_filter)
    file_handler.addFilter(suppress_routine_warning_filter)
    file_handler.addFilter(DowngradeTelethonMsgidRetryFilter(file_level))
    file_handler.addFilter(SanitizeTelethonProtocolErrorFilter())
    file_handler.addFilter(RateLimitTelethonSyncWarningsFilter(sync_warning_window))

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)
    root_logger.setLevel(logging.DEBUG)

    logging.getLogger(__name__).setLevel(logging.INFO)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("telethon.client.updates").setLevel(logging.WARNING)
    logging.getLogger("telethon.extensions").setLevel(logging.WARNING)
    logging.getLogger("telethon.network").setLevel(logging.WARNING)
    logging.getLogger("telethon.network.mtprotosender").setLevel(logging.WARNING)
    logging.getLogger("telethon.network.connection").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


class ProcessInstanceLock:
    """Cross-platform advisory lock held for the complete bot process lifetime."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._stream = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        stream = open(self.path, "a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            stream.close()
            return False
        self._stream = stream
        return True

    def release(self) -> None:
        stream = self._stream
        self._stream = None
        if not stream:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _build_revision() -> str:
    revision = os.getenv("BOT_BUILD_REVISION", "").strip()
    if revision:
        return revision
    try:
        git_dir = os.path.join(os.path.dirname(__file__), ".git")
        with open(os.path.join(git_dir, "HEAD"), "r", encoding="ascii") as stream:
            head = stream.read().strip()
        if head.startswith("ref: "):
            with open(os.path.join(git_dir, head[5:]), "r", encoding="ascii") as stream:
                head = stream.read().strip()
        return head[:12] if head else "unknown"
    except OSError:
        return "unknown"

# 主 Bot 与托管账号统一读取本地配置中的 Telegram API 凭证。
API_ID = config.API_ID
API_HASH = config.API_HASH
BOT_TOKEN = config.BOT_TOKEN
MAIN_BOT_CLIENT_KWARGS = {
    "auto_reconnect": True,
    "connection_retries": 5,
    "retry_delay": 3,
    "catch_up": False,
}

bot = None
payment_system = None
reminder_system = None
login_unlock_reminder_system = None
subscription_task = None


async def _reconcile_user_subscription(user_id: int) -> None:
    subscription = DataManager.get_subscription(user_id, include_inactive=True) or {}
    if not subscription.get('active'):
        await AccountManager.suspend_user_accounts(user_id)
        return

    quota = subscription.get('quota')
    if quota is not None:
        phones = sorted(AccountManager.hosted_account_phones(user_id))
        selected = list(subscription.get('selected_accounts') or [])
        if len(selected) > int(quota):
            DataManager.set_selected_accounts(user_id, [], finalize=False)
            subscription['selection_required'] = True
        elif not selected and phones and len(phones) <= int(quota):
            # Repair finite subscriptions created with an empty enabled list.
            DataManager.set_selected_accounts(user_id, phones, finalize=True)
            subscription['selection_required'] = False
        if subscription.get('selection_required'):
            if len(phones) <= int(quota):
                DataManager.set_selected_accounts(user_id, phones, finalize=True)
            else:
                await AccountManager.suspend_user_accounts(user_id)
                return
        await AccountManager.suspend_user_accounts(user_id, keep_selected=True)
    await AccountManager.resume_selected_accounts(user_id)


async def _monitor_subscriptions():
    while True:
        try:
            DataManager.activate_due_subscriptions()
            for user_id in DataManager.get_subscription_user_ids():
                await _reconcile_user_subscription(user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('订阅协调任务执行失败')
        await asyncio.sleep(60)

async def _wait_for_runtime_termination():
    disconnected_task = asyncio.create_task(bot.run_until_disconnected())
    fatal_task = asyncio.create_task(account_runtime.wait_notify_bot_fatal())
    done, pending = await asyncio.wait(
        {disconnected_task, fatal_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    if fatal_task in done:
        health = fatal_task.result()
        raise account_runtime.NotifyBotFatalError(
            f"主 Bot 已进入 fatal 状态: {health.error_type}: {health.reason}"
        )

    error = disconnected_task.exception()
    if error:
        raise error
    raise RuntimeError("主 Bot 连接已终止")


async def cleanup():
    """停止后台任务、持久化数据，并断开主 Bot 与托管客户端。"""
    global subscription_task
    logger.info("开始清理运行资源...")

    if subscription_task:
        subscription_task.cancel()
        await asyncio.gather(subscription_task, return_exceptions=True)
        subscription_task = None

    if reminder_system:
        try:
            await reminder_system.stop_monitoring()
            logger.debug("提醒系统已停止")
        except Exception as e:
            logger.error(f"停止提醒系统失败: {str(e)}")

    if login_unlock_reminder_system:
        try:
            await login_unlock_reminder_system.stop_monitoring()
            account_runtime.set_login_unlock_reminder_system(None)
            logger.debug("登录解限提醒系统已停止")
        except Exception as e:
            logger.error(f"停止登录解限提醒系统失败: {str(e)}")

    if payment_system:
        try:
            await payment_system.stop_monitoring()
            logger.debug("支付订单监控已停止")
        except Exception as e:
            logger.error(f"停止支付订单监控失败: {str(e)}")

    try:
        DataManager.save_user_data()
        logger.debug("用户数据已保存")
    except Exception as e:
        logger.error(f"保存用户数据失败: {str(e)}")

    try:
        from accounts.account_manager import AccountManager, user_accounts
        runtime = account_runtime.get_default_runtime()

        connection_watcher_tasks = list(runtime.client_tasks.items())
        pause_helper_tasks = list(runtime.pause_tasks.items())
        code_fetch_helper_tasks = list(runtime.code_fetch_tasks.items())
        await runtime.close()
        logger.info(
            f"后台账户任务已取消: "
            f"连接监听={len(connection_watcher_tasks)}, "
            f"暂停={len(pause_helper_tasks)}, 获取验证码={len(code_fetch_helper_tasks)}"
        )

        disconnect_jobs = []
        for user_id, accounts in list(user_accounts.items()):
            for phone, acc_info in list(accounts.items()):
                client = acc_info.get('client')
                if client:
                    disconnect_jobs.append(
                        AccountManager._safe_disconnect_client(client, f"cleanup:{user_id}:{phone}")
                    )

        if disconnect_jobs:
            results = await asyncio.gather(*disconnect_jobs, return_exceptions=True)
            ok_count = sum(1 for item in results if item is True)
            logger.info(f"客户端断开完成: {ok_count}/{len(disconnect_jobs)}")

    except Exception as e:
        logger.error(f"清理客户端连接失败: {str(e)}")

    if bot:
        try:
            await bot.disconnect()
            logger.info("Bot客户端已断开")
        except Exception as e:
            logger.error(f"断开Bot客户端失败: {str(e)}")

    logger.info("清理完成")


async def _start_notify_bot() -> None:
    try:
        await bot.start(bot_token=BOT_TOKEN)
    except (AccessTokenInvalidError, ApiIdInvalidError) as error:
        logger.critical(
            "主 Bot Telegram 凭据无效，进程将退出: error_type=%s",
            type(error).__name__,
        )
        account_runtime.raise_notify_bot_fatal(
            error, "主 Bot Telegram 凭据无效，进程将退出"
        )


async def main():
    global bot, payment_system, reminder_system, login_unlock_reminder_system, subscription_task
    instance_lock = None
    try:
        account_runtime.mark_not_ready()
        config.validate_runtime_settings()
        configure_logging()

        # 确保会话目录存在
        if not os.path.exists(config.SESSIONS_DIR):
            os.makedirs(config.SESSIONS_DIR)
            logger.debug(f"创建会话目录: {config.SESSIONS_DIR}")

        instance_lock = ProcessInstanceLock(
            os.path.join(config.SESSIONS_DIR, ".anti_login.instance.lock")
        )
        if not instance_lock.acquire():
            raise RuntimeError(
                "另一个 AntiLogin 进程正在使用同一 sessions 目录，拒绝重复启动"
            )
        logger.info(
            "运行实例锁已取得: build=%s, telethon=%s, sessions=%s",
            _build_revision(),
            telethon.__version__,
            os.path.abspath(config.SESSIONS_DIR),
        )

        stale_pending_result = AccountManager.cleanup_stale_pending_sessions()
        if not stale_pending_result.ok:
            logger.warning(
                f"清理过期临时登录session失败: "
                f"reason={stale_pending_result.reason}, path={stale_pending_result.path}"
            )
        
        # 加载用户数据
        if not DataManager.load_user_data():
            raise RuntimeError("用户数据文件不可安全使用，已停止启动以避免覆盖真实数据")
        if not login_code_request_rate_limiter.prune_all():
            raise RuntimeError(
                "登录验证码请求限流记录清理失败，已停止启动以避免绕过频率限制"
            )
        if not AccountManager.recover_incomplete_account_transfers():
            raise RuntimeError("账户转移事务恢复失败，已停止启动以避免重复加载 Session")
        if not AccountManager.reconcile_historical_subscription_selections():
            raise RuntimeError("历史订阅账户选择清理失败，已停止启动以避免覆盖用户数据")
        if not AdminAuditLog.prune():
            logger.error("管理员审计日志启动清理失败，将在后续写入时重试")
        logger.debug("用户数据加载完成")
        
        # 先创建主 Bot 客户端，确保依赖它的模块从构造开始即持有有效客户端。
        # 使用 Telethon 原生客户端并固定复用 bot.session。
        bot = TelegramClient(
            config.BOT_SESSION_PATH,
            API_ID,
            API_HASH,
            **MAIN_BOT_CLIENT_KWARGS,
        )

        # 初始化支付系统
        payment_system = PaymentSystem()
        logger.debug("支付系统初始化完成")
        
        # 初始化提醒系统
        reminder_system = ReminderSystem(bot)
        logger.debug("提醒系统初始化完成")
        login_unlock_reminder_system = LoginUnlockReminderSystem(bot)
        account_runtime.set_login_unlock_reminder_system(login_unlock_reminder_system)
        logger.debug("登录解限提醒系统初始化完成")
        
        AccountManager.set_notify_bot(bot)
        payment_system.set_bot(bot)
        await setup_bot_handlers(bot, payment_system)
        guarded_count = account_runtime.install_notify_bot_handler_guards(bot)
        logger.debug("已为 %d 个主 Bot 事件处理器安装致命错误边界", guarded_count)
        logger.debug("Telegram客户端初始化完成")
        
        # 启动机器人
        await _start_notify_bot()
        account_runtime.mark_notify_bot_healthy()
        logger.info("🤖 机器人已启动 · %s", PROJECT_ATTRIBUTION)
        
        # 加载所有session
        logger.debug("开始加载所有session文件...")
        await AccountManager.load_all_sessions()
        account_runtime.mark_ready()
        logger.info("所有session文件加载完成")
        
        await payment_system.start_monitoring()
        logger.info("✅ 支付系统自动检测已启动（5秒间隔，持续5分钟）")
        
        # 启动提醒系统监控
        await reminder_system.start_monitoring()
        logger.info("✅ 提醒系统监控已启动")
        await login_unlock_reminder_system.start_monitoring()
        logger.info("✅ 登录解限提醒监控已启动")
        subscription_task = asyncio.create_task(
            _monitor_subscriptions(), name='subscription-coordinator'
        )

        # 托管连接由 Telethon 自动重连和事件监听维护；重新加载只保留手动触发。
        logger.debug("自动重载已禁用；使用 Telethon 连接监听和手动重新加载")
        
        # 发送启动成功消息给管理员
        try:
            for admin_id in config.ADMIN_IDS:
                await bot.send_message(
                    admin_id,
                    t(DataManager.get_user_language(admin_id), "admin.notification.startup")
                    + f"\n\n{PROJECT_ATTRIBUTION}"
                )
        except account_runtime.NOTIFY_BOT_FATAL_ERRORS as error:
            account_runtime.raise_notify_bot_fatal(
                error, "发送启动通知时发现主 Bot 授权失效"
            )
        except Exception as e:
            account_runtime.mark_notify_bot_degraded(e)
            logger.error(f"发送启动通知失败: {str(e)}")
        
        # 保持运行
        logger.info("机器人进入运行状态...")
        await _wait_for_runtime_termination()
        
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception:
        logger.exception("程序异常退出")
        raise
    finally:
        try:
            await cleanup()
        finally:
            if instance_lock:
                instance_lock.release()

if __name__ == '__main__':
    asyncio.run(main())
