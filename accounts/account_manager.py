# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import asyncio
import json
import re
import os
import time
import math
import shutil
import logging
import sqlite3
import tempfile
import threading
import uuid
import zlib
import copy
from contextlib import AsyncExitStack
from typing import Dict, List, Optional
from telethon import Button, TelegramClient, events, functions, types
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyInvalidError,
    AuthKeyUnregisteredError,
    FreshResetAuthorisationForbiddenError,
    FrozenMethodInvalidError,
    HashInvalidError,
    FloodWaitError,
    MsgidDecreaseRetryError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    RPCError,
    SessionExpiredError,
    SessionPasswordNeededError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.errors.common import TypeNotFoundError
from storage.data_manager import DataManager
from localization import t
from datetime import datetime, timedelta, timezone
import settings as config
from accounts import account_runtime
from accounts.account_session_files import safe_remove_session_files, session_related_paths
from accounts.models import (
    AccountCleanupResult,
    AccountTransferResult,
    ExistingAccountCheck,
    SessionCleanupResult,
)
from accounts.login_code_monitor import extract_sign_in_codes
from accounts.login_code_rate_limiter import (
    login_code_request_rate_limiter,
    render_login_code_rate_limit,
)
from accounts.account_runtime import (
    client_tasks,
    account_operation_locks,
    code_fetch_tasks,
    code_waiters,
    hosting_action_cooldowns,
    pause_tasks,
    session_locks,
    user_accounts,
    user_states,
)

logger = logging.getLogger(__name__)
LOG_MONITOR_CONFIRMATIONS = os.getenv("BOT_LOG_MONITOR_CONFIRMATIONS", "").lower() in {"1", "true", "yes", "on"}
MONITOR_CONFIRM_LOG_INTERVAL_SECONDS = 10 * 60

# 使用配置中的常量
API_ID = config.API_ID
API_HASH = config.API_HASH
SESSIONS_DIR = config.SESSIONS_DIR
PENDING_SESSIONS_DIR = os.path.join(SESSIONS_DIR, "pending")
SESSION_HEALTH_RETRY_ATTEMPTS = getattr(config, "SESSION_HEALTH_RETRY_ATTEMPTS", 2)
SESSION_HEALTH_RETRY_DELAY_SECONDS = getattr(config, "SESSION_HEALTH_RETRY_DELAY_SECONDS", 3)
PENDING_SESSION_MAX_AGE_SECONDS = 15 * 60
SESSION_CLIENT_KWARGS = {
    "device_model": config.SESSION_DEVICE_MODEL,
    "system_version": config.SESSION_SYSTEM_VERSION,
    "app_version": config.SESSION_APP_VERSION,
    "lang_code": config.SESSION_LANG_CODE,
    "system_lang_code": config.SESSION_SYSTEM_LANG_CODE,
}
HOSTED_SESSION_CLIENT_KWARGS = {
    **SESSION_CLIENT_KWARGS,
    # Login-code recovery is handled by the targeted 777000 backfill below.
    # Full catch-up causes unrelated GetChannelDifferenceRequest sync storms.
    "catch_up": False,
}
LOGIN_CODE_BACKFILL_WINDOW_SECONDS = getattr(
    config, "LOGIN_CODE_BACKFILL_WINDOW_SECONDS", 5 * 60
)
LOGIN_CODE_INVALIDATE_ATTEMPTS = getattr(config, "LOGIN_CODE_INVALIDATE_ATTEMPTS", 3)
LOGIN_CODE_INVALIDATE_RETRY_DELAYS = tuple(
    getattr(config, "LOGIN_CODE_INVALIDATE_RETRY_DELAYS", (1, 2))
)
LOGIN_CODE_DEDUP_RETENTION_SECONDS = getattr(
    config, "LOGIN_CODE_DEDUP_RETENTION_SECONDS", 10 * 60
)
LOGIN_MONITOR_STATE_FILE = getattr(
    config,
    "LOGIN_MONITOR_STATE_FILE",
    os.path.join("storage", "login_monitor_state.json"),
)
HOSTED_ACCOUNT_METADATA_FILE = getattr(
    config,
    "HOSTED_ACCOUNT_METADATA_FILE",
    os.path.join("storage", "hosted_account_metadata.json"),
)
ACCOUNT_TRANSFER_MIN_AGE_SECONDS = getattr(
    config, "ACCOUNT_TRANSFER_MIN_AGE_SECONDS", 24 * 60 * 60
)
HOSTING_OPERATION_MIN_AGE_SECONDS = getattr(
    config, "HOSTING_OPERATION_MIN_AGE_SECONDS", 24 * 60 * 60
)
HOSTING_CLEAN_TIMEOUT_SECONDS = getattr(config, "HOSTING_CLEAN_TIMEOUT_SECONDS", 120)
HOSTING_CLEAN_MIN_AGE_SECONDS = getattr(config, "HOSTING_CLEAN_MIN_AGE_SECONDS", 60 * 60)
TRANSFER_RECIPIENT_RESTRICTION_SECONDS = getattr(
    config, "TRANSFER_RECIPIENT_RESTRICTION_SECONDS", 60 * 60
)
ACCOUNT_TRANSFER_JOURNAL_FILE = getattr(
    config,
    "ACCOUNT_TRANSFER_JOURNAL_FILE",
    os.path.join("storage", "account_transfer_journal.json"),
)
NEW_DEVICE_ACTION_ATTEMPTS = 3
NEW_DEVICE_ACTION_RETRY_DELAYS = (1, 2)

# 修改手机号正则表达式，支持带空格的格式
PHONE_REGEX = re.compile(r'^\+\d{1,4}[\s\d]{5,15}$')

def transfer_recipient_created_at(
    source_created_at: Optional[float], now: Optional[float] = None
) -> float:
    """Preserve the account's real creation time when ownership changes."""
    if source_created_at is not None:
        return float(source_created_at)
    return float(time.time() if now is None else now)


class AccountManager:
    """账户管理类"""
    PHONE_REGEX = PHONE_REGEX
    _login_message_locks: Dict[str, asyncio.Lock] = {}
    _login_monitor_state_guard = threading.Lock()
    _login_monitor_state = None
    _hosted_metadata_guard = threading.Lock()
    _hosted_metadata = None
    _quota_locks: Dict[int, asyncio.Lock] = {}
    _authorization_locks: Dict[str, asyncio.Lock] = {}
    _transfer_journal_guard = threading.RLock()

    @staticmethod
    def _get_quota_lock(user_id: int) -> asyncio.Lock:
        lock = AccountManager._quota_locks.get(int(user_id))
        if lock is None:
            lock = asyncio.Lock()
            AccountManager._quota_locks[int(user_id)] = lock
        return lock

    @staticmethod
    def _get_authorization_lock(user_id: int, phone: str) -> asyncio.Lock:
        key = AccountManager._hosted_metadata_key(user_id, phone)
        lock = AccountManager._authorization_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            AccountManager._authorization_locks[key] = lock
        return lock

    @staticmethod
    def _get_account_operation_lock(user_id: int, phone: str) -> asyncio.Lock:
        key = AccountManager._hosted_metadata_key(user_id, phone)
        lock = account_operation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            account_operation_locks[key] = lock
        return lock

    @staticmethod
    def hosted_account_phones(user_id: int) -> set:
        """Return hosted phones from runtime and retained session files."""
        phones = {
            AccountManager._digits_only(phone)
            for phone in user_accounts.get(int(user_id), {}).keys()
        }
        prefix = f"{int(user_id)}_"
        try:
            for name in os.listdir(SESSIONS_DIR):
                if not name.startswith(prefix) or not name.endswith('.session'):
                    continue
                digits = name[len(prefix):-len('.session')]
                if digits.isdigit():
                    phones.add(digits)
        except FileNotFoundError:
            pass
        return {phone for phone in phones if phone}

    @staticmethod
    def get_quota_status(user_id: int) -> Dict:
        used = len(AccountManager.hosted_account_phones(user_id))
        quota = DataManager.get_hosting_quota(user_id)
        return {
            'used': used, 'quota': quota,
            'remaining': None if quota is None else max(0, int(quota) - used),
            'full': quota is not None and used >= int(quota),
        }

    @staticmethod
    def can_add_hosted_account(user_id: int, phone: str = '') -> bool:
        digits = AccountManager._digits_only(phone)
        if digits and digits in AccountManager.hosted_account_phones(user_id):
            return True
        status = AccountManager.get_quota_status(user_id)
        return not status['full']

    @staticmethod
    def quota_error_message(user_id: int) -> str:
        status = AccountManager.get_quota_status(user_id)
        language = DataManager.get_user_language(user_id)
        quota = (
            t(language, "plans.quota_unlimited")
            if status['quota'] is None else status['quota']
        )
        return t(
            language,
            "hosting.quota_full",
            used=status['used'],
            quota=quota,
        )

    @staticmethod
    def is_account_selected(user_id: int, phone: str) -> bool:
        if DataManager.is_admin(user_id):
            return True
        subscription = DataManager.get_subscription(user_id)
        if not subscription:
            return False
        quota = subscription.get('quota')
        if quota is None:
            return True
        if subscription.get('selection_required'):
            return False
        selected = set(subscription.get('selected_accounts') or [])
        return AccountManager._digits_only(phone) in selected

    @staticmethod
    def ensure_account_selected(user_id: int, phone: str) -> bool:
        """Enroll a newly hosted account in a finite subscription when capacity allows."""
        if DataManager.is_admin(user_id):
            return True
        subscription = DataManager.get_subscription(user_id)
        if not subscription:
            return False
        quota = subscription.get('quota')
        if quota is None:
            return True

        digits = AccountManager._digits_only(phone)
        if not digits:
            return False
        hosted = AccountManager.hosted_account_phones(user_id)
        selected = [
            item for item in subscription.get('selected_accounts') or []
            if item in hosted
        ]
        if digits in selected:
            return True

        if subscription.get('selection_required'):
            if len(hosted) > int(quota):
                return False
            selected = sorted(hosted)
        else:
            selected.append(digits)
        if len(selected) > int(quota):
            return False
        return DataManager.set_selected_accounts(user_id, selected, finalize=True)

    @staticmethod
    async def suspend_user_accounts(user_id: int, keep_selected: bool = False) -> int:
        suspended = 0
        for phone, info in list(user_accounts.get(user_id, {}).items()):
            if keep_selected and AccountManager.is_account_selected(user_id, phone):
                continue
            task_key = f"{user_id}_{phone}"
            async with AccountManager._get_session_lock(task_key):
                await AccountManager._cancel_client_task(task_key)
                await AccountManager._cancel_account_auxiliary_tasks(user_id, phone)
                client = info.get('client')
                if client:
                    await AccountManager._safe_disconnect_client(
                        client, f"subscription-suspend:{user_id}:{phone}", timeout=10
                    )
                user_accounts.get(user_id, {}).pop(phone, None)
                suspended += 1
        if user_id in user_accounts and not user_accounts[user_id]:
            user_accounts.pop(user_id, None)
        return suspended

    @staticmethod
    async def resume_selected_accounts(user_id: int) -> int:
        if not AccountManager.check_access(user_id):
            return 0
        subscription = DataManager.get_subscription(user_id) or {}
        quota = subscription.get('quota')
        all_phones = sorted(AccountManager.hosted_account_phones(user_id))
        selected = list(subscription.get('selected_accounts') or [])
        if quota is not None:
            if subscription.get('selection_required'):
                return 0
            hosted = set(all_phones)
            selected = [digits for digits in selected if digits in hosted][:int(quota)]
            if not selected:
                return 0
        elif not selected:
            selected = all_phones
        resumed = 0
        for digits in selected:
            phone = f"+{digits}"
            if AccountManager._find_account_key_by_digits(user_accounts.get(user_id, {}), digits):
                continue
            path = os.path.join(SESSIONS_DIR, f"{user_id}_{digits}.session")
            if not os.path.exists(path):
                continue
            _, _, success, _ = await AccountManager.create_client_from_session(
                path, user_id, detailed=True
            )
            resumed += int(bool(success))
        return resumed

    @staticmethod
    def _get_session_lock(lock_key: str) -> asyncio.Lock:
        """按账户/会话操作键返回一个异步锁。"""
        if lock_key not in session_locks:
            session_locks[lock_key] = asyncio.Lock()
        return session_locks[lock_key]

    @staticmethod
    def _canonical_session_lock_key(session_path: str) -> str:
        return f"path:{os.path.normcase(os.path.realpath(os.path.abspath(session_path)))}"

    @staticmethod
    def _get_session_path_lock(session_path: str) -> asyncio.Lock:
        return AccountManager._get_session_lock(
            AccountManager._canonical_session_lock_key(session_path)
        )

    @staticmethod
    def _get_login_message_lock(user_id: int, phone: str) -> asyncio.Lock:
        lock_key = f"{user_id}_{AccountManager.normalize_phone(phone)}"
        if lock_key not in AccountManager._login_message_locks:
            AccountManager._login_message_locks[lock_key] = asyncio.Lock()
        return AccountManager._login_message_locks[lock_key]

    @staticmethod
    def _login_message_state_key(user_id: int, phone: str, message_id: int) -> str:
        return f"{user_id}:{AccountManager.normalize_phone(phone)}:{message_id}"

    @staticmethod
    def _load_login_monitor_state_locked() -> Dict[str, float]:
        if AccountManager._login_monitor_state is not None:
            return AccountManager._login_monitor_state

        processed = {}
        try:
            if os.path.exists(LOGIN_MONITOR_STATE_FILE):
                with open(LOGIN_MONITOR_STATE_FILE, "r", encoding="utf-8") as state_file:
                    payload = json.load(state_file)
                raw_processed = payload.get("processed", {}) if isinstance(payload, dict) else {}
                processed = {
                    str(key): float(value)
                    for key, value in raw_processed.items()
                    if isinstance(value, (int, float))
                }
        except Exception as error:
            logger.warning(f"登录监控去重状态读取失败，将使用空状态: {error}")

        AccountManager._login_monitor_state = processed
        return processed

    @staticmethod
    def _prune_login_monitor_state_locked(processed: Dict[str, float], now: float) -> bool:
        cutoff = now - LOGIN_CODE_DEDUP_RETENTION_SECONDS
        expired = [key for key, timestamp in processed.items() if timestamp < cutoff]
        for key in expired:
            processed.pop(key, None)
        return bool(expired)

    @staticmethod
    def _save_login_monitor_state_locked(processed: Dict[str, float]) -> bool:
        state_dir = os.path.dirname(os.path.abspath(LOGIN_MONITOR_STATE_FILE)) or "."
        tmp_path = None
        try:
            os.makedirs(state_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=os.path.basename(LOGIN_MONITOR_STATE_FILE) + ".",
                suffix=".tmp",
                dir=state_dir,
                text=True,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as state_file:
                json.dump({"version": 1, "processed": processed}, state_file)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(tmp_path, LOGIN_MONITOR_STATE_FILE)
            return True
        except Exception as error:
            logger.warning(f"登录监控去重状态保存失败: {error}")
            return False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def _is_login_message_processed(user_id: int, phone: str, message_id: int) -> bool:
        if message_id is None:
            return False
        key = AccountManager._login_message_state_key(user_id, phone, message_id)
        with AccountManager._login_monitor_state_guard:
            processed = AccountManager._load_login_monitor_state_locked()
            now = time.time()
            changed = AccountManager._prune_login_monitor_state_locked(processed, now)
            if changed:
                AccountManager._save_login_monitor_state_locked(processed)
            return key in processed

    @staticmethod
    def _mark_login_message_processed(user_id: int, phone: str, message_id: int) -> None:
        if message_id is None:
            return
        key = AccountManager._login_message_state_key(user_id, phone, message_id)
        with AccountManager._login_monitor_state_guard:
            processed = AccountManager._load_login_monitor_state_locked()
            now = time.time()
            AccountManager._prune_login_monitor_state_locked(processed, now)
            processed[key] = now
            AccountManager._save_login_monitor_state_locked(processed)

    @staticmethod
    def _hosted_metadata_key(user_id: int, phone: str) -> str:
        digits = AccountManager._digits_only(phone)
        return f"{int(user_id)}:{digits}"

    @staticmethod
    def _load_hosted_metadata_locked() -> Dict[str, Dict]:
        if AccountManager._hosted_metadata is not None:
            return AccountManager._hosted_metadata

        metadata = {}
        try:
            if os.path.exists(HOSTED_ACCOUNT_METADATA_FILE):
                with open(HOSTED_ACCOUNT_METADATA_FILE, "r", encoding="utf-8") as state_file:
                    payload = json.load(state_file)
                raw_accounts = payload.get("accounts", {}) if isinstance(payload, dict) else {}
                for key, value in raw_accounts.items():
                    if isinstance(value, (int, float)):
                        metadata[str(key)] = {
                            "created_at": float(value),
                            "source": "unknown",
                        }
                    elif isinstance(value, dict):
                        created_at = value.get("created_at")
                        if not isinstance(created_at, (int, float)):
                            continue
                        source = str(value.get("source") or "unknown").strip().lower()
                        known_hashes = value.get("known_authorization_hashes", [])
                        if not isinstance(known_hashes, list):
                            known_hashes = []
                        pending = value.get("pending_authorizations", {})
                        if not isinstance(pending, dict):
                            pending = {}
                        clean_pending = {
                            str(auth_hash): dict(details)
                            for auth_hash, details in pending.items()
                            if isinstance(details, dict)
                        }
                        metadata[str(key)] = {
                            "created_at": float(created_at),
                            "source": source if source in {"upload", "login", "unknown"} else "unknown",
                            "last_transferred_at": (
                                float(value["last_transferred_at"])
                                if isinstance(value.get("last_transferred_at"), (int, float))
                                else None
                            ),
                            "authorization_baseline_initialized": bool(
                                value.get("authorization_baseline_initialized", False)
                            ),
                            "known_authorization_hashes": sorted(
                                {str(auth_hash) for auth_hash in known_hashes}
                            ),
                            "pending_authorizations": clean_pending,
                        }
        except Exception as error:
            logger.warning(f"托管账户元数据读取失败，将使用空状态: {error}")

        AccountManager._hosted_metadata = metadata
        return metadata

    @staticmethod
    def _save_hosted_metadata_locked(metadata: Dict[str, Dict]) -> bool:
        state_dir = os.path.dirname(os.path.abspath(HOSTED_ACCOUNT_METADATA_FILE)) or "."
        tmp_path = None
        try:
            os.makedirs(state_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=os.path.basename(HOSTED_ACCOUNT_METADATA_FILE) + ".",
                suffix=".tmp",
                dir=state_dir,
                text=True,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as state_file:
                json.dump({"version": 6, "accounts": metadata}, state_file)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(tmp_path, HOSTED_ACCOUNT_METADATA_FILE)
            return True
        except Exception as error:
            logger.error(f"托管账户元数据保存失败: {error}")
            return False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def get_hosted_account_created_at(
        user_id: int, phone: str, create_if_missing: bool = True
    ) -> Optional[float]:
        key = AccountManager._hosted_metadata_key(user_id, phone)
        with AccountManager._hosted_metadata_guard:
            metadata = AccountManager._load_hosted_metadata_locked()
            record = metadata.get(key)
            created_at = record.get("created_at") if isinstance(record, dict) else None
            if created_at is None and create_if_missing:
                created_at = time.time()
                metadata[key] = {
                    "created_at": created_at,
                    "source": "unknown",
                    "authorization_baseline_initialized": False,
                    "known_authorization_hashes": [],
                    "pending_authorizations": {},
                }
                AccountManager._save_hosted_metadata_locked(metadata)
            return created_at

    @staticmethod
    def set_hosted_account_created_at(user_id: int, phone: str, created_at: float) -> bool:
        key = AccountManager._hosted_metadata_key(user_id, phone)
        with AccountManager._hosted_metadata_guard:
            metadata = AccountManager._load_hosted_metadata_locked()
            record = metadata.get(key) if isinstance(metadata.get(key), dict) else {}
            record.update({
                "created_at": float(created_at),
                "source": str(record.get("source") or "unknown"),
            })
            metadata[key] = record
            return AccountManager._save_hosted_metadata_locked(metadata)

    @staticmethod
    def get_hosted_account_source(user_id: int, phone: str) -> str:
        key = AccountManager._hosted_metadata_key(user_id, phone)
        with AccountManager._hosted_metadata_guard:
            metadata = AccountManager._load_hosted_metadata_locked()
            record = metadata.get(key)
            if not isinstance(record, dict):
                return "unknown"
            return str(record.get("source") or "unknown")

    @staticmethod
    def set_hosted_account_source(user_id: int, phone: str, source: str) -> bool:
        normalized_source = str(source or "unknown").strip().lower()
        if normalized_source not in {"upload", "login", "unknown"}:
            normalized_source = "unknown"
        key = AccountManager._hosted_metadata_key(user_id, phone)
        with AccountManager._hosted_metadata_guard:
            metadata = AccountManager._load_hosted_metadata_locked()
            record = metadata.get(key) if isinstance(metadata.get(key), dict) else {}
            record.update({
                "created_at": float(record.get("created_at") or time.time()),
                "source": normalized_source,
            })
            metadata[key] = record
            return AccountManager._save_hosted_metadata_locked(metadata)

    @staticmethod
    def get_hosted_authorization_state(user_id: int, phone: str) -> Dict:
        """Return a detached snapshot of the persisted device-authorization state."""
        key = AccountManager._hosted_metadata_key(user_id, phone)
        with AccountManager._hosted_metadata_guard:
            metadata = AccountManager._load_hosted_metadata_locked()
            record = metadata.get(key)
            if not isinstance(record, dict):
                created_at = time.time()
                record = {
                    "created_at": created_at,
                    "source": "unknown",
                    "authorization_baseline_initialized": False,
                    "known_authorization_hashes": [],
                    "pending_authorizations": {},
                }
                metadata[key] = record
                AccountManager._save_hosted_metadata_locked(metadata)
            return {
                "initialized": bool(record.get("authorization_baseline_initialized", False)),
                "known_hashes": {
                    str(auth_hash)
                    for auth_hash in record.get("known_authorization_hashes", [])
                },
                "pending": {
                    str(auth_hash): dict(details)
                    for auth_hash, details in record.get("pending_authorizations", {}).items()
                    if isinstance(details, dict)
                },
            }

    @staticmethod
    def save_hosted_authorization_state(
        user_id: int,
        phone: str,
        known_hashes,
        pending: Dict,
        initialized: bool = True,
    ) -> bool:
        """Atomically persist known and pending Telegram authorizations."""
        key = AccountManager._hosted_metadata_key(user_id, phone)
        with AccountManager._hosted_metadata_guard:
            metadata = AccountManager._load_hosted_metadata_locked()
            record = metadata.get(key) if isinstance(metadata.get(key), dict) else {}
            record.update({
                "created_at": float(record.get("created_at") or time.time()),
                "source": str(record.get("source") or "unknown"),
                "authorization_baseline_initialized": bool(initialized),
                "known_authorization_hashes": sorted(
                    {str(auth_hash) for auth_hash in known_hashes}
                ),
                "pending_authorizations": {
                    str(auth_hash): dict(details)
                    for auth_hash, details in pending.items()
                    if isinstance(details, dict)
                },
            })
            metadata[key] = record
            return AccountManager._save_hosted_metadata_locked(metadata)

    @staticmethod
    def remove_hosted_account_metadata(user_id: int, phone: str) -> bool:
        key = AccountManager._hosted_metadata_key(user_id, phone)
        with AccountManager._hosted_metadata_guard:
            metadata = AccountManager._load_hosted_metadata_locked()
            if key not in metadata:
                return True
            metadata.pop(key, None)
            return AccountManager._save_hosted_metadata_locked(metadata)

    @staticmethod
    def get_hosted_account_metadata_record(user_id: int, phone: str) -> Optional[Dict]:
        key = AccountManager._hosted_metadata_key(user_id, phone)
        with AccountManager._hosted_metadata_guard:
            record = AccountManager._load_hosted_metadata_locked().get(key)
            return copy.deepcopy(record) if isinstance(record, dict) else None

    @staticmethod
    def _replace_hosted_metadata_records(records: Dict[str, Optional[Dict]]) -> bool:
        """Replace multiple metadata records with one atomic file save."""
        with AccountManager._hosted_metadata_guard:
            metadata = AccountManager._load_hosted_metadata_locked()
            previous = {
                str(key): copy.deepcopy(metadata.get(str(key)))
                for key in records
            }
            for raw_key, record in records.items():
                key = str(raw_key)
                if record is None:
                    metadata.pop(key, None)
                else:
                    metadata[key] = copy.deepcopy(record)
            if AccountManager._save_hosted_metadata_locked(metadata):
                return True
            for key, record in previous.items():
                if record is None:
                    metadata.pop(key, None)
                else:
                    metadata[key] = record
            return False

    @staticmethod
    def _safe_transfer_target_metadata(
        source_record: Optional[Dict], transferred_at: float
    ) -> Dict:
        source_record = source_record or {}
        return {
            "created_at": float(source_record.get("created_at") or transferred_at),
            "source": str(source_record.get("source") or "unknown"),
            "last_transferred_at": float(transferred_at),
            "authorization_baseline_initialized": False,
            "known_authorization_hashes": [],
            "pending_authorizations": {},
        }

    @staticmethod
    def _load_transfer_journal_locked() -> Dict:
        backup_path = ACCOUNT_TRANSFER_JOURNAL_FILE + ".backup"
        if not os.path.exists(ACCOUNT_TRANSFER_JOURNAL_FILE) and not os.path.exists(backup_path):
            return {"version": 1, "transactions": {}}
        last_error = None
        for path in (ACCOUNT_TRANSFER_JOURNAL_FILE, backup_path):
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as stream:
                    payload = json.load(stream)
                if not isinstance(payload, dict) or payload.get("version") != 1:
                    raise ValueError("unsupported account transfer journal")
                transactions = payload.get("transactions")
                if not isinstance(transactions, dict):
                    raise ValueError("invalid account transfer journal")
                if path == backup_path:
                    logger.warning("主账户转移日志不可用，已从备份读取")
                return {"version": 1, "transactions": transactions}
            except Exception as error:
                last_error = error
                logger.exception("读取账户转移事务日志失败: %s", path)
        raise last_error or OSError("account transfer journal unavailable")

    @staticmethod
    def _save_transfer_journal_locked(payload: Dict) -> bool:
        directory = os.path.dirname(os.path.abspath(ACCOUNT_TRANSFER_JOURNAL_FILE)) or "."
        os.makedirs(directory, exist_ok=True)

        def _write(path: str) -> bool:
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(
                    prefix=os.path.basename(path) + ".",
                    suffix=".tmp",
                    dir=directory,
                    text=True,
                )
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp_path, path)
                return True
            except Exception:
                logger.exception("保存账户转移事务日志失败: %s", path)
                return False
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

        primary_ok = _write(ACCOUNT_TRANSFER_JOURNAL_FILE)
        backup_ok = primary_ok and _write(ACCOUNT_TRANSFER_JOURNAL_FILE + ".backup")
        return primary_ok and backup_ok

    @staticmethod
    def _upsert_transfer_transaction(transaction: Dict) -> bool:
        with AccountManager._transfer_journal_guard:
            try:
                payload = AccountManager._load_transfer_journal_locked()
            except Exception:
                return False
            payload["transactions"][str(transaction["id"])] = copy.deepcopy(transaction)
            return AccountManager._save_transfer_journal_locked(payload)

    @staticmethod
    def _remove_transfer_transaction(transaction_id: str) -> bool:
        with AccountManager._transfer_journal_guard:
            try:
                payload = AccountManager._load_transfer_journal_locked()
            except Exception:
                return False
            payload["transactions"].pop(str(transaction_id), None)
            return AccountManager._save_transfer_journal_locked(payload)

    @staticmethod
    def get_hosted_account_last_transferred_at(
        user_id: int, phone: str
    ) -> Optional[float]:
        key = AccountManager._hosted_metadata_key(user_id, phone)
        with AccountManager._hosted_metadata_guard:
            metadata = AccountManager._load_hosted_metadata_locked()
            record = metadata.get(key)
            transferred_at = (
                record.get("last_transferred_at") if isinstance(record, dict) else None
            )
            return float(transferred_at) if transferred_at is not None else None

    @staticmethod
    def set_hosted_account_last_transferred_at(
        user_id: int, phone: str, transferred_at: float
    ) -> bool:
        key = AccountManager._hosted_metadata_key(user_id, phone)
        with AccountManager._hosted_metadata_guard:
            metadata = AccountManager._load_hosted_metadata_locked()
            record = metadata.get(key) if isinstance(metadata.get(key), dict) else {}
            record.update({
                "created_at": float(record.get("created_at") or time.time()),
                "source": str(record.get("source") or "unknown"),
                "last_transferred_at": float(transferred_at),
            })
            metadata[key] = record
            return AccountManager._save_hosted_metadata_locked(metadata)

    @staticmethod
    def get_account_transfer_remaining_seconds(user_id: int, phone: str) -> int:
        if DataManager.is_admin(user_id):
            return 0
        now = time.time()
        created_at = AccountManager.get_hosted_account_created_at(user_id, phone)
        age = max(0, now - float(created_at or now))
        initial_remaining = max(0, math.ceil(ACCOUNT_TRANSFER_MIN_AGE_SECONDS - age))
        transferred_at = AccountManager.get_hosted_account_last_transferred_at(
            user_id, phone
        )
        cooldown_remaining = 0
        if transferred_at is not None:
            cooldown_age = max(0, now - transferred_at)
            cooldown_remaining = max(
                0,
                math.ceil(TRANSFER_RECIPIENT_RESTRICTION_SECONDS - cooldown_age),
            )
        return max(initial_remaining, cooldown_remaining)

    @staticmethod
    def get_hosting_clean_remaining_seconds(
        user_id: int,
        phone: str,
        acc_info: Optional[Dict] = None,
        now: Optional[float] = None,
    ) -> int:
        """返回托管协议创建满一小时前，清理功能仍需等待的秒数。"""
        created_at = (acc_info or {}).get("created_at")
        if created_at is None:
            created_at = AccountManager.get_hosted_account_created_at(user_id, phone)
        current_time = time.time() if now is None else float(now)
        age = max(0, current_time - float(created_at or current_time))
        return max(0, math.ceil(HOSTING_CLEAN_MIN_AGE_SECONDS - age))

    @staticmethod
    def hosting_clean_age_message(
        remaining_seconds: int, language: str = "zh"
    ) -> str:
        remaining_seconds = max(1, int(remaining_seconds))
        if remaining_seconds < 60:
            wait_text = t(language, "hosting.wait_seconds", seconds=remaining_seconds)
        else:
            total_minutes = math.ceil(remaining_seconds / 60)
            hours, minutes = divmod(total_minutes, 60)
            wait_text = t(language, "hosting.wait_hours", hours=hours) if hours else ""
            if minutes:
                wait_text += (" " if wait_text else "") + t(
                    language, "hosting.wait_minutes", minutes=minutes
                )
        return t(language, "hosting.clean_age", wait=wait_text)

    @staticmethod
    def is_uploaded_transfer_locked(user_id: int, phone: str) -> bool:
        return (
            AccountManager.get_hosted_account_source(user_id, phone) == "upload"
            and not DataManager.is_admin(user_id)
        )

    @staticmethod
    async def _cancel_client_task(task_key: str):
        """操作会话文件前，先取消并等待连接监听任务退出。"""
        task = client_tasks.pop(task_key, None)
        if not task:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(f"客户端任务退出失败: {task_key}")

    @staticmethod
    def _track_client_task(task_key: str, task: asyncio.Task):
        client_tasks[task_key] = task

        def _cleanup(done_task: asyncio.Task):
            if client_tasks.get(task_key) is done_task:
                client_tasks.pop(task_key, None)
            if done_task.cancelled():
                return
            error = done_task.exception()
            if error:
                logger.error(f"连接监听任务异常退出: {task_key}: {error}")

        task.add_done_callback(_cleanup)

    @staticmethod
    def _start_connection_watcher_task(user_id: int, phone: str, client: TelegramClient):
        task_key = f"{user_id}_{phone}"
        task = asyncio.create_task(AccountManager.watch_client_connection(client, phone, user_id))
        AccountManager._track_client_task(task_key, task)
        return task

    @staticmethod
    async def _cancel_task(task: asyncio.Task):
        """Cancel and wait for a non-client helper task."""
        if not task:
            return
        if task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("辅助任务退出失败")

    @staticmethod
    async def _cancel_account_auxiliary_tasks(user_id: int, phone: str):
        """Cancel per-account pause/code-fetch/device helpers and clear waiters."""
        normalized_phone = AccountManager.normalize_phone(phone)

        pkey = f"pause_{user_id}_{normalized_phone}"
        pt = pause_tasks.pop(pkey, None)
        await AccountManager._cancel_task(pt)

        ckey = f"code_fetch_{user_id}_{normalized_phone}"
        ct = code_fetch_tasks.pop(ckey, None)
        await AccountManager._cancel_task(ct)

        waiters = code_waiters.get(normalized_phone)
        if waiters and user_id in waiters:
            waiters.remove(user_id)
            if not waiters:
                code_waiters.pop(normalized_phone, None)

    @staticmethod
    def _is_recoverable_session_runtime_error(error: Exception) -> bool:
        """Return whether one hosted client may be rebuilt without deleting its Session."""
        text = str(error).lower()
        if isinstance(error, sqlite3.OperationalError):
            return any(
                marker in text
                for marker in (
                    "attempt to write a readonly database",
                    "readonly database",
                    "database is locked",
                    "disk i/o error",
                )
            )
        return isinstance(error, TypeNotFoundError) or any(
            marker in text
            for marker in (
                "wrong session id",
                "invalid new nonce hash",
                "matching constructor id",
            )
        )

    @staticmethod
    def _close_client_session(client: TelegramClient, label: str = "") -> bool:
        """Close Telethon's sqlite handle after disconnect failed while saving state."""
        session = getattr(client, "session", None)
        if not session:
            return False
        try:
            session.close()
            logger.warning("已强制关闭异常会话数据库连接: %s", label)
            return True
        except Exception:
            logger.exception("强制关闭异常会话数据库连接失败: %s", label)
            return False

    @staticmethod
    async def _safe_disconnect_client(
        client: TelegramClient,
        label: str = "",
        timeout: int = 10,
        sqlite_lock_attempts: int = 3,
    ) -> bool:
        """Disconnect a Telethon client with a timeout so cleanup cannot hang forever."""
        if not client:
            return True
        attempts = max(1, int(sqlite_lock_attempts))
        for attempt in range(1, attempts + 1):
            try:
                await asyncio.wait_for(client.disconnect(), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                logger.warning(f"断开连接超时: {label}")
                return False
            except sqlite3.OperationalError as error:
                is_locked = "database is locked" in str(error).lower()
                if is_locked and attempt < attempts:
                    logger.warning(
                        "断开连接时会话数据库被占用，准备重试: %s, 尝试=%d/%d",
                        label,
                        attempt,
                        attempts,
                    )
                    await asyncio.sleep(0.2 * attempt)
                    continue
                if AccountManager._is_recoverable_session_runtime_error(error):
                    logger.warning(
                        "断开连接时会话数据库不可写，尝试关闭句柄: %s, 尝试=%d/%d, 错误=%s",
                        label,
                        attempt,
                        attempts,
                        type(error).__name__,
                    )
                    return AccountManager._close_client_session(client, label)
                logger.warning(
                    "断开连接时会话数据库异常: %s, 错误=%s",
                    label,
                    str(error)[:120],
                    exc_info=True,
                )
                return False
            except Exception:
                logger.exception(f"断开连接失败: {label}")
                return False
        return False

    @staticmethod
    def _is_pending_session_discardable_error(error: Exception) -> bool:
        if not isinstance(error, sqlite3.DatabaseError):
            return False
        text = str(error).lower()
        return (
            "no such table" in text
            or "file is not a database" in text
            or "database disk image is malformed" in text
            or "readonly database" in text
        )

    @staticmethod
    def _is_uploaded_session_format_error(error: Exception) -> bool:
        text = str(error).lower()
        if isinstance(error, sqlite3.DatabaseError):
            return True
        return (
            "too many values to unpack" in text
            or "not enough values to unpack" in text
            or "file is not a database" in text
            or "database disk image is malformed" in text
            or "no such table" in text
        )

    @staticmethod
    async def _disconnect_pending_client(client: TelegramClient, label: str = "", timeout: int = 10) -> bool:
        """Disconnect a temporary pending-login client; corrupt pending sqlite may be discarded."""
        if not client:
            return True
        try:
            await asyncio.wait_for(client.disconnect(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(f"临时会话断开超时: {label}")
            return False
        except Exception as e:
            if AccountManager._is_pending_session_discardable_error(e):
                try:
                    session = getattr(client, "session", None)
                    if session:
                        session.close()
                except Exception as close_error:
                    logger.warning(
                        "强制关闭临时会话数据库失败: %s, error_type=%s",
                        label,
                        type(close_error).__name__,
                    )
                    return False
                logger.warning(
                    "临时会话数据库不可用，已关闭并允许清理: %s, error_type=%s",
                    label,
                    type(e).__name__,
                )
                return True
            logger.exception(f"临时会话断开失败: {label}")
            return False

    @staticmethod
    async def _invalidate_sign_in_codes(
        client: TelegramClient, codes: List[str], phone: str
    ) -> bool:
        """Invalidate Telegram login codes without forwarding them to another chat."""
        if not codes:
            return False

        last_error = None
        for attempt in range(LOGIN_CODE_INVALIDATE_ATTEMPTS):
            try:
                result = await client(
                    functions.account.InvalidateSignInCodesRequest(codes=codes)
                )
                if result:
                    logger.debug(f"已使登录验证码失效: {phone}")
                    return True
                last_error = "Telegram returned False"
            except asyncio.CancelledError:
                raise
            except Exception as error:
                last_error = error

            if attempt < LOGIN_CODE_INVALIDATE_ATTEMPTS - 1:
                delay_index = min(attempt, len(LOGIN_CODE_INVALIDATE_RETRY_DELAYS) - 1)
                delay = LOGIN_CODE_INVALIDATE_RETRY_DELAYS[delay_index]
                logger.warning(
                    f"登录验证码失效请求失败，准备重试: {phone}, "
                    f"尝试={attempt + 1}/{LOGIN_CODE_INVALIDATE_ATTEMPTS}"
                )
                await asyncio.sleep(delay)

        logger.error(
            f"登录验证码失效请求最终失败: {phone}, "
            f"尝试={LOGIN_CODE_INVALIDATE_ATTEMPTS}, 错误={last_error}"
        )
        return False

    @staticmethod
    def _is_fatal_session_status(status: str) -> bool:
        return status in ("unauthorized", "revoked", "2fa")

    @staticmethod
    def _invalid_session_reason_from_error(error: Exception) -> Optional[str]:
        """Return a cleanup reason only for errors that unambiguously invalidate a session."""
        if isinstance(error, SessionRevokedError):
            return "revoked"
        if isinstance(error, SessionPasswordNeededError):
            return "2fa"
        if isinstance(error, (UserDeactivatedError, UserDeactivatedBanError)):
            return "disabled"
        if isinstance(
            error,
            (
                AuthKeyUnregisteredError,
                AuthKeyInvalidError,
                AuthKeyDuplicatedError,
                SessionExpiredError,
            ),
        ):
            return "invalid"
        return None

    @staticmethod
    async def _reconnect_client_for_retry(
        client: TelegramClient,
        label: str,
        reason: str,
        delay: float = SESSION_HEALTH_RETRY_DELAY_SECONDS,
        connect_timeout: int = 15,
    ) -> bool:
        """Reset a Telethon connection before retrying transient health probes."""
        logger.warning(f"Transient Telegram session error; reconnecting before retry: {label}, reason={reason}")
        await AccountManager._safe_disconnect_client(client, f"health-retry:{label}:{reason}", timeout=5)
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await asyncio.wait_for(client.connect(), timeout=connect_timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(f"Reconnect timed out before health retry: {label}, reason={reason}")
            return False
        except Exception as e:
            logger.warning(f"Reconnect failed before health retry: {label}, reason={reason}, error={e}")
            return False

    @staticmethod
    def should_backup_session(reason: str = "", status: str = "", source: str = "") -> bool:
        """判断托管会话删除前是否需要归档备份。"""
        value = (reason or status or "").strip().lower()
        source = (source or "").strip().lower()
        backup_reasons = {"unauthorized", "revoked", "2fa", "invalid", "disabled"}
        no_backup_reasons = {
            "timeout",
            "error",
            "flood_wait",
            "no_user",
            "startup_error",
            "pending_cancelled",
            "code_invalid",
            "code_expired",
            "busy",
            "winerror32",
            "permission_error",
        }
        if value in no_backup_reasons or source == "pending":
            return False
        return value in backup_reasons

    @staticmethod
    def _session_backup_name(filename: str, reason: str, when: datetime = None) -> str:
        """为无效或停用的托管会话生成可读备份文件名。"""
        safe_reason = re.sub(r"[^A-Za-z0-9_-]+", "-", str(reason or "unknown")).strip("-") or "unknown"
        timestamp = (when or datetime.now()).strftime("%m%d-%H%M")
        return f"{filename}.{safe_reason}.{timestamp}.bak"

    @staticmethod
    def _available_backup_path(backup_dir: str, backup_name: str) -> str:
        """Return the requested backup path, adding a numeric suffix only on collision."""
        backup_path = os.path.join(backup_dir, backup_name)
        if not os.path.exists(backup_path):
            return backup_path

        stem, extension = os.path.splitext(backup_name)
        suffix = 1
        while True:
            candidate = os.path.join(backup_dir, f"{stem}.{suffix}{extension}")
            if not os.path.exists(candidate):
                return candidate
            suffix += 1

    @staticmethod
    async def validate_client_session(
        client: TelegramClient,
        phone: str = "",
        connect_timeout: int = 30,
        auth_timeout: int = 20,
        me_timeout: int = 20,
        retry_attempts: int = SESSION_HEALTH_RETRY_ATTEMPTS,
        retry_delay: float = SESSION_HEALTH_RETRY_DELAY_SECONDS,
        reconnect_on_transient: bool = True,
    ) -> Dict:
        """Validate connectivity, authorization, and account identity without status probes."""
        start = time.time()
        label = phone or "unknown"
        try:
            if not client.is_connected():
                await asyncio.wait_for(client.connect(), timeout=connect_timeout)

            authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=auth_timeout)
            if not authorized:
                return {
                    "ok": False,
                    "status": "unauthorized",
                    "reason": "session unauthorized",
                    "elapsed": time.time() - start,
                }

            me = await asyncio.wait_for(client.get_me(), timeout=me_timeout)
            if not me:
                return {
                    "ok": False,
                    "status": "no_user",
                    "reason": "cannot fetch user info",
                    "elapsed": time.time() - start,
                }

            return {
                "ok": True,
                "status": "alive",
                "reason": "alive",
                "elapsed": time.time() - start,
                "me": me,
                "user_id": getattr(me, "id", None),
                "username": getattr(me, "username", None),
                "phone": getattr(me, "phone", None),
            }
        except asyncio.TimeoutError:
            logger.debug(f"会话健康检查超时: {label}")
            if reconnect_on_transient and retry_attempts > 0:
                recovered = await AccountManager._reconnect_client_for_retry(
                    client,
                    label,
                    "timeout",
                    delay=retry_delay,
                    connect_timeout=connect_timeout,
                )
                if recovered:
                    return await AccountManager.validate_client_session(
                        client,
                        phone,
                        connect_timeout=connect_timeout,
                        auth_timeout=auth_timeout,
                        me_timeout=me_timeout,
                        retry_attempts=retry_attempts - 1,
                        retry_delay=retry_delay,
                        reconnect_on_transient=reconnect_on_transient,
                    )
            return {
                "ok": False,
                "status": "timeout",
                "reason": "network timeout",
                "elapsed": time.time() - start,
            }
        except SessionRevokedError:
            logger.debug(f"会话健康检查发现会话已撤销: {label}")
            return {
                "ok": False,
                "status": "revoked",
                "reason": "session revoked",
                "elapsed": time.time() - start,
            }
        except AuthKeyUnregisteredError:
            logger.debug(f"会话健康检查发现授权密钥未注册: {label}")
            return {
                "ok": False,
                "status": "unauthorized",
                "reason": "auth key unregistered",
                "elapsed": time.time() - start,
            }
        except FloodWaitError as e:
            logger.debug(f"会话健康检查触发限流等待: {label}, 等待秒数={e.seconds}")
            return {
                "ok": False,
                "status": "flood_wait",
                "reason": f"flood wait {e.seconds}s",
                "elapsed": time.time() - start,
            }
        except SessionPasswordNeededError:
            logger.debug(f"会话健康检查发现需要二级密码: {label}")
            return {
                "ok": False,
                "status": "2fa",
                "reason": "2FA required",
                "elapsed": time.time() - start,
            }
        except MsgidDecreaseRetryError as e:
            logger.debug(f"会话健康检查遇到 MsgidDecreaseRetryError: {label}, 错误={e}")
            if reconnect_on_transient and retry_attempts > 0:
                recovered = await AccountManager._reconnect_client_for_retry(
                    client,
                    label,
                    "msgid_decrease",
                    delay=retry_delay,
                    connect_timeout=connect_timeout,
                )
                if recovered:
                    return await AccountManager.validate_client_session(
                        client,
                        phone,
                        connect_timeout=connect_timeout,
                        auth_timeout=auth_timeout,
                        me_timeout=me_timeout,
                        retry_attempts=retry_attempts - 1,
                        retry_delay=retry_delay,
                        reconnect_on_transient=reconnect_on_transient,
                    )
            return {
                "ok": False,
                "status": "timeout",
                "reason": "msgid decrease retry exhausted",
                "elapsed": time.time() - start,
            }
        except Exception as e:
            logger.debug(f"会话健康检查异常: {label}, 错误={e}")
            return {
                "ok": False,
                "status": "error",
                "reason": str(e)[:80],
                "elapsed": time.time() - start,
            }

    @staticmethod
    async def check_account_freeze_status(client: TelegramClient, phone: str = "", timeout: int = 15) -> Dict:
        """Query Telegram account freeze metadata for an explicit manual reload."""
        label = phone or "unknown"
        try:
            app_config = await asyncio.wait_for(
                client(functions.help.GetAppConfigRequest(hash=0)),
                timeout=timeout,
            )
            config_json = json.loads(app_config.to_json())
            freeze_since = None
            freeze_until = None

            for item in config_json.get("config", {}).get("value", []):
                key = item.get("key")
                value = item.get("value", {})
                if value.get("_") != "JsonNumber":
                    continue
                if key == "freeze_since_date":
                    freeze_since = value.get("value")
                elif key == "freeze_until_date":
                    freeze_until = value.get("value")

            freeze_info = None
            if freeze_since and freeze_until:
                freeze_info = {"since": freeze_since, "until": freeze_until}

            return {
                "ok": True,
                "status": "frozen" if freeze_info else "alive",
                "freeze_info": freeze_info,
            }
        except Exception as error:
            logger.debug(f"手动重载冻结状态查询失败: {label}, 错误={error}")
            return {
                "ok": False,
                "status": "unknown",
                "reason": str(error)[:80],
            }

    @staticmethod
    async def check_inaccessible_session_file(session_path: str, phone_hint: str = "") -> str:
        """Probe a no-access session without loading it into monitoring."""
        client = None
        try:
            client = TelegramClient(
                session_path,
                API_ID,
                API_HASH,
                auto_reconnect=True,
                **SESSION_CLIENT_KWARGS,
            )
            health = await AccountManager.validate_client_session(client, phone_hint or os.path.basename(session_path))
            return health.get("status", "error")
        except Exception as e:
            if AccountManager._is_uploaded_session_format_error(e):
                return "invalid"
            logger.debug(f"无权限会话探测失败: {session_path}, 错误={e}")
            return "error"
        finally:
            try:
                if client and client.is_connected():
                    await AccountManager._safe_disconnect_client(client, f"inaccessible-probe:{session_path}")
            except Exception:
                pass

    @staticmethod
    async def notify_session_unavailable(user_id: int, phone: str, source: str = "reload"):
        """Notify a user when a hosted account session can no longer be authorized."""
        bot = account_runtime.get_notify_bot()
        if not bot:
            logger.info(f"通知机器人未绑定，跳过会话不可用通知: 用户ID={user_id}, 手机号={phone}")
            return

        language = DataManager.get_user_language(user_id)
        source_key = {
            "startup": "notify.session_source.startup",
            "manual_reload": "notify.session_source.manual_reload",
        }.get(source, "notify.session_source.connection")

        display_phone = AccountManager.format_phone_display(phone or "unknown")
        msg = t(
            language,
            "notify.session_unavailable",
            source=t(language, source_key),
            phone=display_phone,
        )

        try:
            sent = await AccountManager._safe_send_bot_message(
                bot, user_id, msg, context=f"session_unavailable:{phone}"
            )
            if not sent:
                return
            logger.debug(f"会话不可用通知已发送: 用户ID={user_id}, 手机号={display_phone}, 来源={source}")
        except account_runtime.NotifyBotFatalError:
            raise
        except Exception as e:
            logger.warning(f"发送会话不可用通知失败: 用户ID={user_id}, 手机号={display_phone}, 错误={e}")

    @staticmethod
    def set_notify_bot(bot):
        account_runtime.set_notify_bot(bot)
        logger.info("✅ 账户管理器已绑定通知机器人")

    @staticmethod
    async def _safe_send_bot_message(
        bot,
        user_id: int,
        text: str,
        context: str,
        buttons=None,
        return_message: bool = False,
    ):
        try:
            if buttons is None:
                message = await bot.send_message(user_id, text)
            else:
                message = await bot.send_message(user_id, text, buttons=buttons)
            account_runtime.mark_notify_bot_healthy()
            return message if return_message else True
        except account_runtime.NotifyBotFatalError:
            raise
        except account_runtime.NOTIFY_BOT_FATAL_ERRORS as e:
            account_runtime.raise_notify_bot_fatal(
                e, f"主 Bot 授权失效，通知已停止: context={context}"
            )
        except ValueError as e:
            if "Could not find the input entity" in str(e):
                logger.warning(
                    f"无法向用户发送通知，bot 尚未解析到实体: user_id={user_id}, context={context}"
                )
            else:
                logger.warning(
                    f"机器人通知失败，目标实体无效: user_id={user_id}, context={context}, 错误={e}"
                )
            return False
        except (RPCError, ConnectionError, TimeoutError, OSError) as e:
            account_runtime.mark_notify_bot_degraded(e)
            logger.warning(
                f"机器人通知临时失败 user_id={user_id}, context={context}, "
                f"错误类型={type(e).__name__}, 错误={str(e)[:160]}"
            )
            return False
        except Exception as e:
            logger.exception(f"机器人通知失败 user_id={user_id}, context={context}: {e}")
            return False
    
    @staticmethod
    def normalize_phone(phone: str) -> str:
        """标准化手机号格式，移除空格"""
        # 移除所有空格和特殊字符，只保留数字和+号
        normalized = re.sub(r'[^\d+]', '', phone)
        return normalized
    
    @staticmethod
    def format_phone_display(phone: str) -> str:
        """格式化手机号显示（带空格）"""
        normalized = AccountManager.normalize_phone(phone)
        # 如果是+1开头的美国号码，格式化为 +1 XXX XXX XXXX
        if normalized.startswith('+1') and len(normalized) == 12:  # +1 + 10位数字
            return f"+1 {normalized[2:5]} {normalized[5:8]} {normalized[8:]}"
        # 其他格式保持原样或简单处理
        return normalized

    @staticmethod
    def is_account_online(acc_info: Dict) -> bool:
        """判断托管账户是否可用于主动操作。"""
        if not acc_info:
            return False
        return acc_info.get("runtime_status", "online") != "offline"

    @staticmethod
    def _cleanup_temporary_state(acc_info: Dict) -> None:
        if not acc_info:
            return

        now = time.time()
        mode = acc_info.get("temporary_mode")
        until = acc_info.get("temporary_until")

        if not mode:
            return

        if until and until > now:
            return

        if mode == "code_fetch":
            previous_mode = acc_info.pop("code_fetch_previous_mode", None)
            previous_until = acc_info.pop("code_fetch_previous_until", None)
            if previous_mode == "pause" and previous_until and previous_until > now:
                acc_info["temporary_mode"] = "pause"
                acc_info["temporary_until"] = previous_until
                return

        acc_info.pop("temporary_mode", None)
        acc_info.pop("temporary_until", None)
        acc_info.pop("code_fetch_previous_mode", None)
        acc_info.pop("code_fetch_previous_until", None)

    @staticmethod
    def get_account_mode(acc_info: Dict) -> str:
        """清理过期临时状态后，返回 normal、paused 或 code_fetch。"""
        if not acc_info:
            return "normal"
        AccountManager._cleanup_temporary_state(acc_info)
        mode = acc_info.get("temporary_mode")
        if mode == "pause":
            return "paused"
        if mode == "code_fetch":
            return "code_fetch"
        return "normal"

    @staticmethod
    def get_hosting_status_text(user_id: int, phone: str, acc_info: Dict) -> str:
        """返回托管工具使用的账户状态，24 小时内统一标记为操作受限。"""
        language = DataManager.get_user_language(user_id)
        if not acc_info or not AccountManager.is_account_online(acc_info):
            return t(language, "hosting.offline")

        health_status = acc_info.get("health_status")
        client = acc_info.get("client")
        if not health_status and client:
            health_status = getattr(client, "_last_health_status", None)
        if health_status == "frozen" or acc_info.get("freeze_info"):
            return t(language, "hosting.frozen")

        created_at = acc_info.get("created_at")
        if created_at is None:
            created_at = AccountManager.get_hosted_account_created_at(user_id, phone)
        age = max(0, time.time() - float(created_at or time.time()))
        remaining = max(0, math.ceil(HOSTING_OPERATION_MIN_AGE_SECONDS - age))
        if remaining:
            total_minutes = max(1, math.ceil(remaining / 60))
            hours, minutes = divmod(total_minutes, 60)
            if hours:
                wait_text = t(language, "transfer.remaining_hours", hours=hours)
            else:
                wait_text = t(language, "transfer.remaining_minutes", minutes=minutes)
            return t(language, "hosting.restricted", wait=wait_text)

        return t(language, "hosting.available")

    @staticmethod
    def get_compact_hosting_status_text(user_id: int, phone: str, acc_info: Dict) -> str:
        """返回托管账户选择列表使用的精简状态。"""
        status = AccountManager.get_hosting_status_text(user_id, phone, acc_info)
        if status.startswith("⛔️ 操作受限 · "):
            return status.replace("⛔️ 操作受限 · ", "⛔️ ", 1)
        if status.startswith("⛔️ Wait "):
            return status.replace("⛔️ Wait ", "⛔️ ", 1)
        if status.startswith("🔴"):
            return "🔴"
        if status.startswith("❄️"):
            return "❄️"
        if status.startswith("🟢"):
            return "🟢"
        return status

    @staticmethod
    async def check_existing_account_for_add(user_id: int, phone: str) -> ExistingAccountCheck:
        """判断添加该手机号时应阻止、复核还是允许。"""
        normalized_phone = AccountManager.normalize_phone(phone)
        accounts = AccountManager.get_user_accounts(user_id)
        acc_info = accounts.get(normalized_phone)
        display_phone = AccountManager.format_phone_display(normalized_phone)
        language = DataManager.get_user_language(user_id)

        if not acc_info:
            return ExistingAccountCheck(action="allow", phone=normalized_phone, message="")

        client = acc_info.get("client")
        if client:
            health = await AccountManager.validate_client_session(
                client,
                normalized_phone,
                reconnect_on_transient=False,
            )
            status = health.get("status", "error")
            reason = health.get("reason", "unknown")
            if health.get("ok"):
                acc_info["runtime_status"] = "online"
                acc_info["offline_reason"] = None
                acc_info["offline_at"] = None
                return ExistingAccountCheck(
                    action="block",
                    phone=normalized_phone,
                    status=status,
                    reason=reason,
                    message=t(language, "hosting.account_healthy", phone=display_phone),
                )

            if AccountManager._is_fatal_session_status(status):
                await AccountManager.cleanup_invalid_hosted_session(
                    user_id=user_id,
                    phone=normalized_phone,
                    client=client,
                    reason=status or "invalid",
                    source="add_account",
                    notify_user=False,
                )
                return ExistingAccountCheck(
                    action="allow",
                    phone=normalized_phone,
                    status=status,
                    reason=reason,
                    message="",
                )

            task_key = f"{user_id}_{normalized_phone}"
            await AccountManager._cancel_client_task(task_key)
            disconnected = await AccountManager._safe_disconnect_client(
                client,
                f"add-account-confirm-before-probe:{normalized_phone}:{status}",
                timeout=5,
            )
            if not disconnected:
                AccountManager._start_connection_watcher_task(
                    user_id, normalized_phone, client
                )
                return ExistingAccountCheck(
                    action="block",
                    phone=normalized_phone,
                    status="busy",
                    reason="disconnect_failed",
                    message=t(language, "hosting.account_busy", phone=display_phone),
                )

            session_path = acc_info.get("original_session_path")
            if not session_path and acc_info.get("session_file"):
                session_path = os.path.join(SESSIONS_DIR, acc_info["session_file"])
            if not session_path:
                confirm = {"ok": False, "status": "error", "reason": "missing session path"}
            else:
                _, restored_phone, restored, restore_reason = (
                    await AccountManager.create_client_from_session(
                        session_path,
                        user_id,
                        detailed=True,
                        preserved_health_status=acc_info.get("health_status"),
                        preserved_freeze_info=acc_info.get("freeze_info"),
                        account_source=acc_info.get("source"),
                    )
                )
                confirm = {
                    "ok": bool(restored),
                    "status": "alive" if restored else (restore_reason or "error"),
                    "reason": "reloaded" if restored else (restore_reason or "reload failed"),
                    "phone": restored_phone,
                }
            confirm_status = confirm.get("status", "error")
            confirm_reason = confirm.get("reason", "unknown")
            logger.debug(
                f"添加账号复核探测: 手机号={normalized_phone}, "
                f"状态={confirm_status}, 是否正常={confirm.get('ok')}, 原因={confirm_reason}"
            )

            if confirm.get("ok"):
                return ExistingAccountCheck(
                    action="block",
                    phone=normalized_phone,
                    status=confirm_status,
                    reason=confirm_reason,
                    message=t(language, "hosting.account_rechecked", phone=display_phone),
                )

            if AccountManager._is_fatal_session_status(confirm_status):
                await AccountManager.cleanup_invalid_hosted_session(
                    user_id=user_id,
                    phone=normalized_phone,
                    client=client,
                    reason=confirm_status or "invalid",
                    source="add_account_confirm",
                    notify_user=False,
                )
                return ExistingAccountCheck(
                    action="allow",
                    phone=normalized_phone,
                    status=confirm_status,
                    reason=confirm_reason,
                    message="",
                )

            reason = confirm_status or status or reason or "error"
            await AccountManager.mark_hosted_session_offline(
                user_id=user_id,
                phone=normalized_phone,
                client=acc_info.get("client"),
                reason=reason,
            )
            logger.warning(
                "托管会话受控重建失败，已标记离线并保留 session: "
                "用户ID=%s, 手机号=%s, 原因=%s",
                user_id,
                normalized_phone,
                reason,
            )
        else:
            reason = acc_info.get("offline_reason") or "missing_client"
            await AccountManager.mark_hosted_session_offline(
                user_id=user_id,
                phone=normalized_phone,
                reason=reason,
            )

        return ExistingAccountCheck(
            action="block",
            phone=normalized_phone,
            status="offline",
            reason=reason,
            message=t(
                language,
                "hosting.account_existing_offline",
                phone=display_phone,
            ),
        )

    @staticmethod
    def is_anti_login_active(acc_info: Dict) -> bool:
        """判断 777000 消息是否应作为反登录提醒转发。"""
        if not AccountManager.is_account_online(acc_info):
            return False
        if not acc_info.get("anti_login", True):
            return False
        return AccountManager.get_account_mode(acc_info) == "normal"

    @staticmethod
    def ensure_account_operable(user_id: int, phone: str, allow_delete: bool = False):
        """为账户操作返回 (ok, accounts, normalized_phone, acc_info, message)。"""
        normalized_phone = AccountManager.normalize_phone(phone)
        language = DataManager.get_user_language(user_id)
        if not AccountManager.check_access(user_id):
            return False, {}, normalized_phone, None, t(language, "hosting.no_access")

        accounts = AccountManager.get_user_accounts(user_id)
        acc_info = accounts.get(normalized_phone)
        if not acc_info:
            return False, accounts, normalized_phone, None, t(language, "protection.no_account")

        if not allow_delete and not AccountManager.is_account_online(acc_info):
            reason = acc_info.get("offline_reason") or "unknown"
            return False, accounts, normalized_phone, acc_info, t(
                language,
                "hosting.operation_offline",
                phone=AccountManager.format_phone_display(normalized_phone),
                reason=reason,
            )

        AccountManager.get_account_mode(acc_info)
        return True, accounts, normalized_phone, acc_info, ""

    @staticmethod
    def _hosted_operation_invalid_message(phone: str, language: str = "zh") -> str:
        return t(
            language,
            "hosting.operation_invalid",
            phone=AccountManager.format_phone_display(phone),
        )

    @staticmethod
    def _hosted_operation_offline_message(
        phone: str, reason: str = "error", language: str = "zh"
    ) -> str:
        return t(
            language,
            "hosting.operation_offline",
            phone=AccountManager.format_phone_display(phone),
            reason=reason or "unknown",
        )

    @staticmethod
    async def ensure_hosted_client_ready(user_id: int, phone: str, action: str):
        """敏感托管操作前返回 (ok, accounts, normalized_phone, acc_info, client, message)。"""
        language = DataManager.get_user_language(user_id)
        ok, accounts, normalized_phone, acc_info, message = AccountManager.ensure_account_operable(user_id, phone)
        if not ok:
            return False, accounts, normalized_phone, acc_info, None, message

        client = acc_info.get("client") if acc_info else None
        if not client:
            await AccountManager.mark_hosted_session_offline(
                user_id=user_id,
                phone=normalized_phone,
                reason=f"{action}:missing_client",
            )
            return (
                False,
                accounts,
                normalized_phone,
                acc_info,
                None,
                AccountManager._hosted_operation_offline_message(
                    normalized_phone, "missing_client", language
                ),
            )

        health = await AccountManager.validate_client_session(
            client,
            normalized_phone,
            reconnect_on_transient=False,
        )
        status = health.get("status", "error")
        reason = health.get("reason", "unknown")
        if health.get("ok"):
            acc_info["runtime_status"] = "online"
            acc_info["offline_reason"] = None
            acc_info["offline_at"] = None
            logger.debug(f"托管操作预检查通过: 操作={action}, 手机号={normalized_phone}")
            return True, accounts, normalized_phone, acc_info, client, ""

        if status in ("unauthorized", "revoked", "2fa"):
            logger.warning(
                f"托管操作发现无效会话: 操作={action}, "
                f"手机号={normalized_phone}, 状态={status}, 原因={reason}"
            )
            await AccountManager.cleanup_invalid_hosted_session(
                user_id=user_id,
                phone=normalized_phone,
                client=client,
                reason=status or "invalid",
                source=action,
            )
            return (
                False,
                accounts,
                normalized_phone,
                acc_info,
                None,
                AccountManager._hosted_operation_invalid_message(
                    normalized_phone, language
                ),
            )

        logger.warning(
            f"托管操作暂不可用: 操作={action}, 手机号={normalized_phone}, "
            f"状态={status}, 原因={reason}"
        )
        logger.info(
            "托管操作预检查临时失败，保留客户端并交由 Telethon 自动重连: "
            "操作=%s, 手机号=%s, 原因=%s",
            action,
            normalized_phone,
            status or "error",
        )
        return (
            False,
            accounts,
            normalized_phone,
            acc_info,
            None,
            AccountManager._hosted_operation_offline_message(
                normalized_phone, status or "error", language
            ),
        )

    @staticmethod
    async def handle_hosted_operation_error(
        user_id: int,
        phone: str,
        client: TelegramClient,
        action: str,
        error: Exception,
    ) -> str:
        """Convert hosted operation RPC failures into user-facing state transitions."""
        normalized_phone = AccountManager.normalize_phone(phone)
        language = DataManager.get_user_language(user_id)
        if isinstance(error, FrozenMethodInvalidError):
            logger.warning(
                "Telegram 冻结账号拒绝托管操作: 操作=%s, 手机号=%s",
                action,
                normalized_phone,
            )
            return t(
                DataManager.get_user_language(user_id),
                "hosting.operation_frozen",
            )
        if isinstance(error, (AuthKeyUnregisteredError, SessionRevokedError, SessionPasswordNeededError)):
            status = "2fa" if isinstance(error, SessionPasswordNeededError) else "unauthorized"
            if isinstance(error, SessionRevokedError):
                status = "revoked"
            logger.warning(
                f"托管操作 RPC 调用时发现无效会话: 操作={action}, "
                f"手机号={normalized_phone}, 状态={status}, 错误={error}"
            )
            await AccountManager.cleanup_invalid_hosted_session(
                user_id=user_id,
                phone=normalized_phone,
                client=client,
                reason=status,
                source=action,
            )
            return AccountManager._hosted_operation_invalid_message(
                normalized_phone, language
            )

        if isinstance(error, FreshResetAuthorisationForbiddenError):
            logger.warning(
                "Telegram 拒绝使用过新的会话重置其他授权: 操作=%s, 手机号=%s",
                action,
                normalized_phone,
            )
            return t(language, "hosting.session_too_new")

        if AccountManager._is_recoverable_session_runtime_error(error):
            recovery = await AccountManager._recover_hosted_client_once(
                user_id,
                normalized_phone,
                client,
                type(error).__name__,
                operation_locked=True,
            )
            if recovery in ("recovered", "stale_client"):
                return t(
                    DataManager.get_user_language(user_id),
                    "hosting.recovered_retry",
                )
            return AccountManager._hosted_operation_offline_message(
                normalized_phone, recovery, language
            )

        if isinstance(error, (asyncio.TimeoutError, FloodWaitError, ConnectionError, OSError)):
            reason = "flood_wait" if isinstance(error, FloodWaitError) else "connection_error"
            if isinstance(error, FloodWaitError):
                wait_seconds = max(1, int(error.seconds))
                AccountManager._set_hosting_cooldown(
                    user_id, normalized_phone, action, wait_seconds
                )
            logger.warning(
                f"托管操作 RPC 调用时暂不可用: 操作={action}, "
                f"手机号={normalized_phone}, 原因={reason}, 错误={error}"
            )
            logger.info(
                "托管操作遇到临时连接错误，保留客户端并交由 Telethon 自动重连: "
                "操作=%s, 手机号=%s, 原因=%s",
                action,
                normalized_phone,
                reason,
            )
            if isinstance(error, FloodWaitError):
                return t(language, "hosting.rate_limited", seconds=wait_seconds)
            return AccountManager._hosted_operation_offline_message(
                normalized_phone, reason, language
            )

        logger.exception(f"托管操作失败: 操作={action}, 手机号={normalized_phone}")
        return AccountManager._hosted_operation_offline_message(
            normalized_phone, "operation_error", language
        )

    @staticmethod
    def _remove_session_files_checked(session_path: str) -> SessionCleanupResult:
        """Remove a temporary session and report whether every existing file was removed."""
        ok = True
        for path in session_related_paths(session_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
                if os.path.exists(path):
                    ok = False
            except Exception:
                logger.exception(f"删除会话文件失败: {path}")
                ok = False
        reason = "removed" if ok else "remove_failed"
        return SessionCleanupResult(ok=ok, action="remove_session_files", reason=reason, path=session_path)

    @staticmethod
    def _client_session_path(client: TelegramClient = None) -> Optional[str]:
        """Best-effort lookup for the sqlite session path backing a Telethon client."""
        if not client:
            return None
        pending_path = getattr(client, "_pending_session_path", None)
        if pending_path:
            return pending_path
        session = getattr(client, "session", None)
        return getattr(session, "filename", None)

    @staticmethod
    def _move_session_files(source_path: str, target_path: str):
        """Move a Telethon sqlite session and any sidecar files to a new base path."""
        if not source_path or not target_path:
            return
        if os.path.abspath(source_path) == os.path.abspath(target_path):
            return

        os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)

        source_paths = session_related_paths(source_path)
        if not any(os.path.exists(src) for src in source_paths):
            raise FileNotFoundError(f"session files not found: {source_path}")

        target_paths = session_related_paths(target_path)
        rollback_paths = []
        moved_targets = []
        moved_sources = []
        rollback_suffix = f".rollback.{int(time.time() * 1000)}"

        try:
            for dst in target_paths:
                if os.path.exists(dst):
                    rollback_path = dst + rollback_suffix
                    shutil.move(dst, rollback_path)
                    rollback_paths.append((rollback_path, dst))
                    moved_targets.append(dst)

            for src, dst in zip(source_paths, target_paths):
                if os.path.exists(src):
                    shutil.move(src, dst)
                    moved_sources.append((dst, src))
        except Exception:
            for dst, src in reversed(moved_sources):
                try:
                    if os.path.exists(dst) and not os.path.exists(src):
                        shutil.move(dst, src)
                except Exception:
                    logger.exception(f"回滚源会话失败: {dst} -> {src}")

            for rollback_path, dst in reversed(rollback_paths):
                try:
                    if os.path.exists(rollback_path) and not os.path.exists(dst):
                        shutil.move(rollback_path, dst)
                except Exception:
                    logger.exception(f"回滚目标会话失败: {rollback_path} -> {dst}")
            raise

        for rollback_path, _ in rollback_paths:
            safe_remove_session_files(rollback_path)

    @staticmethod
    def _rollback_transfer_files(
        source_path: str,
        target_path: str,
    ) -> bool:
        """Best-effort rollback for session transfer artifacts."""
        try:
            AccountManager._move_session_files(target_path, source_path)
            return True
        except Exception:
            logger.exception("账户转让失败后回滚 session 文件失败")
            return False

    @staticmethod
    def _is_pending_session_path(session_path: str) -> bool:
        if not session_path:
            return False
        try:
            pending_root = os.path.abspath(PENDING_SESSIONS_DIR)
            candidate = os.path.abspath(session_path)
            return os.path.commonpath([pending_root, candidate]) == pending_root
        except Exception:
            return False

    @staticmethod
    def _active_pending_session_paths() -> set:
        paths = set()
        for state in user_states.values():
            path = state.get("pending_session_path")
            client_path = AccountManager._client_session_path(state.get("auth_client"))
            for candidate in (path, client_path):
                if candidate and AccountManager._is_pending_session_path(candidate):
                    paths.add(os.path.abspath(candidate))
        return paths

    @staticmethod
    def cleanup_stale_pending_sessions(max_age_seconds: int = PENDING_SESSION_MAX_AGE_SECONDS) -> SessionCleanupResult:
        """Remove pending login sessions older than the fixed grace window and not tied to user state."""
        if not os.path.exists(PENDING_SESSIONS_DIR):
            return SessionCleanupResult(ok=True, action="cleanup_stale_pending", reason="missing_dir")

        now = time.time()
        active_paths = AccountManager._active_pending_session_paths()
        removed = 0
        failed = 0
        first_failed_path = ""
        seen_roots = set()

        for name in os.listdir(PENDING_SESSIONS_DIR):
            path = os.path.join(PENDING_SESSIONS_DIR, name)
            if not os.path.isfile(path):
                continue
            root_path = re.sub(r"-(journal|wal|shm)$", "", path)
            if root_path in seen_roots:
                continue
            seen_roots.add(root_path)
            if not root_path.endswith(".session"):
                continue
            if os.path.abspath(root_path) in active_paths:
                continue

            related_paths = [p for p in session_related_paths(root_path) if os.path.exists(p)]
            if not related_paths:
                continue
            newest_mtime = max(os.path.getmtime(p) for p in related_paths)
            if now - newest_mtime < max_age_seconds:
                continue

            result = AccountManager._remove_session_files_checked(root_path)
            if result.ok:
                removed += 1
            else:
                failed += 1
                first_failed_path = first_failed_path or root_path

        if failed:
            return SessionCleanupResult(
                ok=False,
                action="cleanup_stale_pending",
                reason=f"remove_failed:{failed}",
                path=first_failed_path,
            )
        return SessionCleanupResult(
            ok=True,
            action="cleanup_stale_pending",
            reason=f"removed:{removed}",
        )

    @staticmethod
    async def cleanup_pending_login_state(user_id: int, reason: str = "reset") -> SessionCleanupResult:
        """Cancel the user's unfinished login flow and remove only its pending session files."""
        state = user_states.get(user_id)
        if not state:
            return SessionCleanupResult(ok=True, action="cleanup_pending_login", reason="no_state")

        client = state.get("auth_client")
        phone = state.get("auth_phone") or ""
        session_path = state.get("pending_session_path") or AccountManager._client_session_path(client)
        normalized_phone = AccountManager.normalize_phone(phone) if phone else ""
        task_key = f"{user_id}_{normalized_phone or 'pending'}"
        qr_wait_task = state.get("qr_wait_task")
        if qr_wait_task and qr_wait_task is not asyncio.current_task():
            await AccountManager._cancel_task(qr_wait_task)

        async with AccountManager._get_session_lock(task_key):
            is_pending_session = AccountManager._is_pending_session_path(session_path or "")
            if client:
                label = f"pending-login:{reason}:{user_id}:{normalized_phone or 'unknown'}"
                if is_pending_session:
                    disconnected = await AccountManager._disconnect_pending_client(client, label, timeout=10)
                else:
                    disconnected = await AccountManager._safe_disconnect_client(client, label, timeout=10)
                if not disconnected:
                    logger.warning(
                        f"清理临时登录状态时拒绝删除被占用的会话文件: "
                        f"用户ID={user_id}, 手机号={normalized_phone}, 路径={session_path}"
                    )
                    return SessionCleanupResult(
                        ok=False,
                        action="cleanup_pending_login",
                        reason="disconnect_failed",
                        path=session_path or "",
                    )

            if session_path and is_pending_session:
                remove_result = AccountManager._remove_session_files_checked(session_path)
                if not remove_result.ok:
                    logger.warning(
                        f"清理临时登录状态时未能删除全部会话文件: "
                        f"用户ID={user_id}, 手机号={normalized_phone}, 路径={session_path}"
                    )
                    return SessionCleanupResult(
                        ok=False,
                        action="cleanup_pending_login",
                        reason=remove_result.reason,
                        path=session_path,
                    )

            user_states.pop(user_id, None)
            logger.debug(
                f"临时登录状态已清理: 用户ID={user_id}, "
                f"手机号={normalized_phone or '未知'}, 原因={reason}"
            )
            return SessionCleanupResult(
                ok=True,
                action="cleanup_pending_login",
                reason=reason,
                path=session_path or "",
            )

    @staticmethod
    async def cleanup_incomplete_account(user_id: int, phone: str, client: TelegramClient = None) -> SessionCleanupResult:
        """Clean memory and session files for an account that failed during add/login."""
        normalized_phone = AccountManager.normalize_phone(phone)
        task_key = f"{user_id}_{normalized_phone}"
        async with AccountManager._get_session_lock(task_key):
            await AccountManager._cancel_client_task(task_key)

            accounts = user_accounts.get(user_id, {})
            acc_info = accounts.get(normalized_phone, {})
            session_file = acc_info.get("session_file")
            state = user_states.get(user_id, {})
            session_path = (
                acc_info.get("original_session_path")
                or state.get("pending_session_path")
                or AccountManager._client_session_path(client)
            )
            cleanup_client = client or acc_info.get("client")
            is_pending_session = AccountManager._is_pending_session_path(session_path or "")
            account_removed = False

            if cleanup_client:
                label = f"incomplete-account:{user_id}:{normalized_phone}"
                if is_pending_session:
                    disconnected = await AccountManager._disconnect_pending_client(cleanup_client, label, timeout=10)
                else:
                    disconnected = await AccountManager._safe_disconnect_client(cleanup_client, label, timeout=10)
                if not disconnected:
                    return SessionCleanupResult(
                        ok=False,
                        action="cleanup_incomplete_account",
                        reason="disconnect_failed",
                        path=session_path or "",
                    )

            is_same_client = bool(cleanup_client and acc_info.get("client") is cleanup_client)
            if accounts and normalized_phone in accounts and (is_pending_session or is_same_client):
                accounts.pop(normalized_phone, None)
                account_removed = True
                if not accounts:
                    user_accounts.pop(user_id, None)

            if session_path:
                if AccountManager._is_pending_session_path(session_path):
                    remove_result = AccountManager._remove_session_files_checked(session_path)
                    if remove_result.ok:
                        user_states.pop(user_id, None)
                        AccountManager.remove_hosted_account_metadata(
                            user_id, normalized_phone
                        )
                    return SessionCleanupResult(
                        ok=remove_result.ok,
                        action="cleanup_incomplete_account",
                        reason=remove_result.reason,
                        path=session_path,
                    )
                safe_remove_session_files(session_path)
                user_states.pop(user_id, None)
                AccountManager.remove_hosted_account_metadata(user_id, normalized_phone)
                return SessionCleanupResult(
                    ok=True,
                    action="cleanup_incomplete_account",
                    reason="removed",
                    path=session_path,
                )
            elif session_file:
                session_path = os.path.join(SESSIONS_DIR, session_file)
                safe_remove_session_files(session_path)
                user_states.pop(user_id, None)
                AccountManager.remove_hosted_account_metadata(user_id, normalized_phone)
                return SessionCleanupResult(
                    ok=True,
                    action="cleanup_incomplete_account",
                    reason="removed",
                    path=session_path,
                )
            user_states.pop(user_id, None)
            if account_removed:
                AccountManager.remove_hosted_account_metadata(user_id, normalized_phone)
            return SessionCleanupResult(ok=True, action="cleanup_incomplete_account", reason="no_session")


    @staticmethod
    async def cleanup_invalid_hosted_session(
        user_id: int,
        phone: str,
        client: TelegramClient = None,
        reason: str = "invalid",
        source: str = "connection_watcher",
        notify_user: bool = True,
    ):
        """Disconnect, notify, backup/remove files, and forget a hosted account with an invalid session."""
        normalized_phone = AccountManager.normalize_phone(phone)
        task_key = f"{user_id}_{normalized_phone}"
        notify_after_cleanup = False
        notify_display_phone = normalized_phone
        async with AccountManager._get_session_lock(task_key):
            accounts = user_accounts.get(user_id, {})
            acc_info = accounts.get(normalized_phone, {})
            session_file = acc_info.get("session_file")
            session_path = acc_info.get("original_session_path")
            display_phone = acc_info.get("display_phone", normalized_phone)
            cleanup_client = client or acc_info.get("client")
            notify_after_cleanup = bool(notify_user)
            notify_display_phone = display_phone

            if not session_path and session_file:
                session_path = os.path.join(SESSIONS_DIR, session_file)
            if not session_path and normalized_phone:
                session_path = os.path.join(SESSIONS_DIR, f"{user_id}_{normalized_phone.replace('+', '')}.session")

            existing_task = client_tasks.get(task_key)
            current_task = asyncio.current_task()
            if existing_task and existing_task is not current_task:
                await AccountManager._cancel_client_task(task_key)
            elif existing_task is current_task:
                client_tasks.pop(task_key, None)
            await AccountManager._cancel_account_auxiliary_tasks(
                user_id, normalized_phone
            )

            if cleanup_client:
                await AccountManager._safe_disconnect_client(
                    cleanup_client,
                    f"invalid-session:{normalized_phone}:{reason}",
                    timeout=10,
                )

            if session_path and os.path.exists(session_path):
                await AccountManager.backup_session_file(session_path, reason or "invalid")

            if accounts and normalized_phone in accounts:
                accounts.pop(normalized_phone, None)
                if not accounts:
                    user_accounts.pop(user_id, None)
            AccountManager.remove_hosted_account_metadata(user_id, normalized_phone)

            logger.debug(
                f"无效托管会话已清理: 用户ID={user_id}, "
                f"手机号={normalized_phone}, 原因={reason}"
            )

        if notify_after_cleanup:
            await AccountManager.notify_session_unavailable(user_id, notify_display_phone, source=source)

    @staticmethod
    async def mark_hosted_session_offline(
        user_id: int,
        phone: str,
        client: TelegramClient = None,
        reason: str = "error",
    ) -> bool:
        """Record a final offline state without scheduling Session rebuilds."""
        normalized_phone = AccountManager.normalize_phone(phone)
        task_key = f"{user_id}_{normalized_phone}"
        async with AccountManager._get_session_lock(task_key):
            accounts = user_accounts.get(user_id, {})
            acc_info = accounts.get(normalized_phone)
            if client is not None and acc_info is not None and acc_info.get("client") is not client:
                logger.debug(
                    f"忽略旧客户端的离线事件: 用户ID={user_id}, 手机号={normalized_phone}"
                )
                return False

            existing_task = client_tasks.get(task_key)
            current_task = asyncio.current_task()
            if existing_task and existing_task is not current_task:
                await AccountManager._cancel_client_task(task_key)
            elif existing_task is current_task:
                client_tasks.pop(task_key, None)

            cleanup_client = client or (acc_info or {}).get("client")
            if cleanup_client:
                await AccountManager._safe_disconnect_client(
                    cleanup_client,
                    f"offline-session:{normalized_phone}:{reason}",
                    timeout=10,
                )

            was_offline = bool(acc_info and acc_info.get("runtime_status") == "offline")
            if acc_info is not None:
                acc_info["runtime_status"] = "offline"
                acc_info["offline_reason"] = reason or "error"
                acc_info["offline_at"] = time.time()

            message = (
                f"托管会话已标记离线: 用户ID={user_id}, "
                f"手机号={normalized_phone}, 原因={reason}"
            )
            if was_offline:
                logger.debug(message)
            else:
                logger.warning(message)
            return bool(acc_info is not None and not was_offline)

    @staticmethod
    async def notify_hosted_session_offline(user_id: int, phone: str, reason: str = "error"):
        """Notify a user when a hosted account is offline after the recovery window."""
        bot = account_runtime.get_notify_bot()
        if not bot:
            logger.info(f"通知机器人未绑定，跳过托管离线通知: 用户ID={user_id}, 手机号={phone}")
            return

        try:
            await AccountManager._safe_send_bot_message(
                bot,
                user_id,
                t(DataManager.get_user_language(user_id), "notify.offline",
                  phone=AccountManager.format_phone_display(phone),
                  reason=reason or "unknown"),
                context=f"hosted_session_offline:{phone}",
            )
        except account_runtime.NotifyBotFatalError:
            raise
        except Exception as e:
            logger.warning(f"发送托管离线通知失败: 用户ID={user_id}, 手机号={phone}, 错误={e}")


    @staticmethod
    def _schedule_code_fetch_expiry(user_id: int, phone: str, expires_at: float) -> None:
        task_key = f"code_fetch_{user_id}_{phone}"
        old_task = code_fetch_tasks.get(task_key)
        if old_task:
            old_task.cancel()

        async def _auto_stop():
            try:
                await asyncio.sleep(max(0, expires_at - time.time()))
                current_acc = user_accounts.get(user_id, {}).get(phone)
                waiters = code_waiters.get(phone)
                if waiters and user_id in waiters:
                    waiters.remove(user_id)
                    if not waiters:
                        code_waiters.pop(phone, None)
                if current_acc:
                    AccountManager._cleanup_temporary_state(current_acc)
                bot = account_runtime.get_notify_bot()
                if bot:
                    try:
                        await AccountManager._safe_send_bot_message(
                            bot, user_id,
                            t(DataManager.get_user_language(user_id), "notify.code_mode_ended", phone=phone),
                            context=f"code_fetch_auto_stop:{phone}",
                        )
                    except account_runtime.NotifyBotFatalError:
                        raise
                    except Exception:
                        logger.exception("发送获取验证码自动结束提醒失败: 用户ID=%s, 手机号=%s", user_id, phone)
            except asyncio.CancelledError:
                raise
            finally:
                code_fetch_tasks.pop(task_key, None)

        code_fetch_tasks[task_key] = asyncio.create_task(_auto_stop())

    @staticmethod
    def _schedule_pause_expiry(user_id: int, phone: str, expires_at: float) -> None:
        task_key = f"pause_{user_id}_{phone}"
        old_task = pause_tasks.get(task_key)
        if old_task:
            old_task.cancel()

        async def _auto_resume():
            try:
                await asyncio.sleep(max(0, expires_at - time.time()))
                account = user_accounts.get(user_id, {}).get(phone)
                if not account:
                    return
                if account.get("temporary_mode") == "pause" and account.get("temporary_until", 0) <= time.time():
                    account.pop("temporary_mode", None)
                    account.pop("temporary_until", None)
                    bot = account_runtime.get_notify_bot()
                    if bot:
                        await AccountManager._safe_send_bot_message(
                            bot, user_id,
                            t(DataManager.get_user_language(user_id), "notify.protection_resumed", phone=account.get("display_phone", phone)),
                            context=f"anti_login_auto_resume:{phone}",
                        )
            except asyncio.CancelledError:
                raise
            except account_runtime.NotifyBotFatalError:
                raise
            except Exception:
                logger.exception("自动恢复暂停状态失败")
            finally:
                pause_tasks.pop(task_key, None)

        pause_tasks[task_key] = asyncio.create_task(_auto_resume())

    @staticmethod
    def _snapshot_transfer_runtime(acc_info: Dict) -> Dict:
        keys = (
            "anti_login", "temporary_mode", "temporary_until",
            "code_fetch_previous_mode", "code_fetch_previous_until",
        )
        return {key: copy.deepcopy(acc_info.get(key)) for key in keys if key in acc_info}

    @staticmethod
    def _restore_transfer_runtime(user_id: int, phone: str, acc_info: Dict, snapshot: Dict) -> None:
        keys = (
            "anti_login", "temporary_mode", "temporary_until",
            "code_fetch_previous_mode", "code_fetch_previous_until",
        )
        for key in keys:
            acc_info.pop(key, None)
        acc_info.update(copy.deepcopy(snapshot))
        mode = acc_info.get("temporary_mode")
        expires_at = float(acc_info.get("temporary_until") or 0)
        if mode and expires_at <= time.time():
            AccountManager._cleanup_temporary_state(acc_info)
        elif mode == "pause":
            AccountManager._schedule_pause_expiry(user_id, phone, expires_at)
        elif mode == "code_fetch":
            code_waiters.setdefault(phone, set()).add(user_id)
            AccountManager._schedule_code_fetch_expiry(user_id, phone, expires_at)
        client = acc_info.get("client")
        if client:
            AccountManager._start_connection_watcher_task(user_id, phone, client)

    @staticmethod
    async def start_code_fetch(user_id: int, phone: str) -> str:
        phone = AccountManager.normalize_phone(phone)
        async with AccountManager._get_account_operation_lock(user_id, phone):
            return AccountManager._start_code_fetch_unlocked(user_id, phone)

    @staticmethod
    def _start_code_fetch_unlocked(user_id: int, phone: str) -> str:
        """进入主动获取验证码模式：自动关闭该号的反登录转发"""
        language = DataManager.get_user_language(user_id)
        if not AccountManager.check_access(user_id):
            return t(language, "hosting.no_access")
        accounts = AccountManager.get_user_accounts(user_id)
        phone = AccountManager.normalize_phone(phone)
        if phone not in accounts:
            return t(language, "protection.no_account")
        # 关闭反登录转发
        acc = accounts[phone]
        if not AccountManager.is_account_online(acc):
            return AccountManager.ensure_account_operable(user_id, phone)[4]
        AccountManager.get_account_mode(acc)
        if acc.get("temporary_mode") == "pause":
            acc["code_fetch_previous_mode"] = "pause"
            acc["code_fetch_previous_until"] = acc.get("temporary_until")
        else:
            acc.pop("code_fetch_previous_mode", None)
            acc.pop("code_fetch_previous_until", None)
        acc["temporary_mode"] = "code_fetch"
        acc["temporary_until"] = time.time() + 30 * 60
        # 记录等待者
        code_waiters.setdefault(phone, set()).add(user_id)
        task_key = f"code_fetch_{user_id}_{phone}"
        old_task = code_fetch_tasks.get(task_key)
        if old_task:
            old_task.cancel()

        async def _auto_stop():
            try:
                await asyncio.sleep(30 * 60)
                current_accounts = user_accounts.get(user_id, {})
                current_acc = current_accounts.get(phone)
                waiters = code_waiters.get(phone)
                if waiters and user_id in waiters:
                    waiters.remove(user_id)
                    if not waiters:
                        code_waiters.pop(phone, None)
                if current_acc:
                    AccountManager._cleanup_temporary_state(current_acc)
                bot = account_runtime.get_notify_bot()
                if bot:
                    try:
                        await AccountManager._safe_send_bot_message(
                            bot,
                            user_id,
                            t(DataManager.get_user_language(user_id), "notify.code_mode_ended", phone=phone),
                            context=f"code_fetch_auto_stop:{phone}",
                        )
                    except account_runtime.NotifyBotFatalError:
                        raise
                    except Exception:
                        logger.exception(f"发送获取验证码自动结束提醒失败: 用户ID={user_id}, 手机号={phone}")
            except asyncio.CancelledError:
                raise
            finally:
                code_fetch_tasks.pop(task_key, None)

        code_fetch_tasks[task_key] = asyncio.create_task(_auto_stop())
        return t(language, "hosting.code_started")

    @staticmethod
    async def stop_code_fetch(user_id: int, phone: str) -> str:
        phone = AccountManager.normalize_phone(phone)
        async with AccountManager._get_account_operation_lock(user_id, phone):
            return AccountManager._stop_code_fetch_unlocked(user_id, phone)

    @staticmethod
    def _stop_code_fetch_unlocked(user_id: int, phone: str) -> str:
        """退出主动获取验证码模式：自动开启该号的反登录转发"""
        language = DataManager.get_user_language(user_id)
        if not AccountManager.check_access(user_id):
            return t(language, "hosting.no_access")
        accounts = AccountManager.get_user_accounts(user_id)
        phone = AccountManager.normalize_phone(phone)
        if phone not in accounts:
            return t(language, "protection.no_account")
        # 移除等待者
        if not AccountManager.is_account_online(accounts[phone]):
            return AccountManager.ensure_account_operable(user_id, phone)[4]
        s = code_waiters.get(phone)
        if s and user_id in s:
            s.remove(user_id)
            if not s:
                code_waiters.pop(phone, None)
        task_key = f"code_fetch_{user_id}_{phone}"
        code_task = code_fetch_tasks.pop(task_key, None)
        if code_task:
            code_task.cancel()
        # 开启反登录转发
        acc = accounts[phone]
        if acc.get("temporary_mode") == "code_fetch":
            previous_mode = acc.pop("code_fetch_previous_mode", None)
            previous_until = acc.pop("code_fetch_previous_until", None)
            if previous_mode == "pause" and previous_until and previous_until > time.time():
                acc["temporary_mode"] = "pause"
                acc["temporary_until"] = previous_until
            else:
                acc.pop("temporary_mode", None)
                acc.pop("temporary_until", None)
        return t(language, "hosting.code_stopped")

    @staticmethod
    def _check_hosting_cooldown(user_id: int, phone: str, action: str, seconds: int) -> Optional[str]:
        phone = AccountManager.normalize_phone(phone)
        key = f"{action}_{user_id}_{phone}"
        now = time.time()
        until = hosting_action_cooldowns.get(key, 0)
        if until > now:
            remaining = int(math.ceil(until - now))
            return t(
                DataManager.get_user_language(user_id),
                "hosting.cooldown",
                seconds=remaining,
            )
        hosting_action_cooldowns[key] = now + seconds
        return None

    @staticmethod
    def _set_hosting_cooldown(
        user_id: int, phone: str, action: str, seconds: int
    ) -> None:
        """Extend an operation cooldown without shortening an existing server wait."""
        phone = AccountManager.normalize_phone(phone)
        key = f"{action}_{user_id}_{phone}"
        until = time.time() + max(1, int(seconds))
        hosting_action_cooldowns[key] = max(
            float(hosting_action_cooldowns.get(key, 0) or 0),
            until,
        )

    @staticmethod
    def get_antilogin_status_text(acc_info: Dict, user_id: int = None) -> str:
        """返回反登录当前状态文案"""
        language = DataManager.get_user_language(user_id) if user_id is not None else "zh"
        if not acc_info:
            return t(language, "protection.unknown")
        if not AccountManager.is_account_online(acc_info):
            return t(language, "protection.offline")
        mode = AccountManager.get_account_mode(acc_info)
        if mode == "paused":
            until = acc_info.get("temporary_until") or time.time()
            mins = max(1, math.ceil((until - time.time()) / 60))
            return t(language, "protection.paused", minutes=mins)
        if mode == "code_fetch":
            return t(language, "protection.code_mode")
        return t(language, "protection.active") if acc_info.get("anti_login") else t(language, "protection.off")

    @staticmethod
    def get_antilogin_status_icon(acc_info: Dict) -> str:
        """返回账户列表使用的精简防护状态图标。"""
        if not acc_info or not AccountManager.is_account_online(acc_info):
            return "⚠️"
        mode = AccountManager.get_account_mode(acc_info)
        if mode == "paused":
            return "⏸️"
        if mode == "code_fetch":
            return "🔵"
        return "🛡️" if acc_info.get("anti_login") else "⚪"

    @staticmethod
    async def pause_anti_login(user_id: int, phone: str, minutes: int = 30) -> str:
        phone = AccountManager.normalize_phone(phone)
        async with AccountManager._get_account_operation_lock(user_id, phone):
            return await AccountManager._pause_anti_login_unlocked(
                user_id, phone, minutes
            )

    @staticmethod
    async def _pause_anti_login_unlocked(user_id: int, phone: str, minutes: int = 30) -> str:
        """暂停反登录监控转发 N 分钟，到期后自动恢复开启"""
        language = DataManager.get_user_language(user_id)
        if not AccountManager.check_access(user_id):
            return t(language, "common.no_access")
        accounts = AccountManager.get_user_accounts(user_id)
        phone = AccountManager.normalize_phone(phone)
        if phone not in accounts:
            return t(language, "protection.no_account")

        acc = accounts[phone]
        if not AccountManager.is_account_online(acc):
            return AccountManager.ensure_account_operable(user_id, phone)[4]
        temporary_until = time.time() + minutes * 60
        acc["temporary_mode"] = "pause"
        acc["temporary_until"] = temporary_until

        # 取消旧任务
        task_key = f"pause_{user_id}_{phone}"
        old = pause_tasks.get(task_key)
        if old:
            old.cancel()

        async def _auto_resume():
            try:
                await asyncio.sleep(minutes * 60)
                # 到点再次确认：仍然是暂停状态，才自动开启
                a = AccountManager.get_user_accounts(user_id).get(phone)
                if not a:
                    return
                if a.get("temporary_mode") == "pause" and a.get("temporary_until", 0) <= time.time():
                    a.pop("temporary_mode", None)
                    a.pop("temporary_until", None)
                    # 通知用户（如果 bot 已绑定）
                    bot = account_runtime.get_notify_bot()
                    if bot:
                        try:
                            display_phone = a.get("display_phone", phone)
                            await AccountManager._safe_send_bot_message(
                                bot,
                                user_id,
                                t(DataManager.get_user_language(user_id), "notify.protection_resumed", phone=display_phone),
                                context=f"anti_login_auto_resume:{phone}",
                            )
                        except account_runtime.NotifyBotFatalError:
                            raise
                        except Exception:
                            logger.exception(
                                f"发送反登录自动恢复通知失败: 用户ID={user_id}, 手机号={phone}"
                            )
            except asyncio.CancelledError:
                return
            except account_runtime.NotifyBotFatalError:
                raise
            except Exception:
                logger.exception("自动恢复暂停状态失败")
            finally:
                pause_tasks.pop(task_key, None)

        pause_tasks[task_key] = asyncio.create_task(_auto_resume())
        return t(language, "protection.pause_set", minutes=minutes)

    @staticmethod
    async def resume_anti_login(user_id: int, phone: str) -> str:
        phone = AccountManager.normalize_phone(phone)
        async with AccountManager._get_account_operation_lock(user_id, phone):
            return AccountManager._resume_anti_login_unlocked(user_id, phone)

    @staticmethod
    def _resume_anti_login_unlocked(user_id: int, phone: str) -> str:
        """手动开启反登录监控（同时取消暂停）"""
        language = DataManager.get_user_language(user_id)
        if not AccountManager.check_access(user_id):
            return t(language, "common.no_access")
        accounts = AccountManager.get_user_accounts(user_id)
        phone = AccountManager.normalize_phone(phone)
        if phone not in accounts:
            return t(language, "protection.no_account")
        acc = accounts[phone]
        if not AccountManager.is_account_online(acc):
            return AccountManager.ensure_account_operable(user_id, phone)[4]
        acc.pop("temporary_mode", None)
        acc.pop("temporary_until", None)
        acc.pop("code_fetch_previous_mode", None)
        acc.pop("code_fetch_previous_until", None)
        acc["anti_login"] = True

        task_key = f"pause_{user_id}_{phone}"
        pause_task = pause_tasks.get(task_key)
        if pause_task:
            pause_task.cancel()
            pause_tasks.pop(task_key, None)

        return t(language, "protection.enabled")

    @staticmethod
    async def delete_account(user_id: int, phone: str) -> str:
        phone = AccountManager.normalize_phone(phone)
        async with AccountManager._get_account_operation_lock(user_id, phone):
            return await AccountManager._delete_account_unlocked(user_id, phone)

    @staticmethod
    async def _delete_account_unlocked(user_id: int, phone: str) -> str:
        """删除账户：主动远程登出 Telegram 当前设备 + 取消任务并删除本地 session 文件"""
        language = DataManager.get_user_language(user_id)
        if not AccountManager.check_access(user_id):
            return t(language, "hosting.no_access")
        accounts = AccountManager.get_user_accounts(user_id)
        phone = AccountManager.normalize_phone(phone)
        if phone not in accounts:
            return t(language, "protection.no_account")

        acc = accounts.get(phone)
        client = acc.get("client")
        session_file = acc.get("session_file")
        display_phone = acc.get("display_phone", phone)
        lock_key = f"{user_id}_{phone}"

        async with AccountManager._get_session_lock(lock_key):
            # 1) 取消 keep_alive 任务并等待退出，避免边重连边删除 SQLite 文件
            await AccountManager._cancel_client_task(lock_key)

            # 2) 取消暂停/取码辅助任务
            await AccountManager._cancel_account_auxiliary_tasks(user_id, phone)

            # 3) 断开并退出会话（尽力）
            try:
                if client:
                    try:
                        # 主动远程登出当前设备（撤销当前 session）
                        try:
                            if not client.is_connected():
                                await client.connect()
                        except Exception:
                            # 连接失败也继续走后续清理
                            pass
                        try:
                            await asyncio.wait_for(client.log_out(), timeout=10)
                        except Exception:
                            # log_out 失败则至少断开连接
                            pass
                        try:
                            await AccountManager._safe_disconnect_client(
                                client,
                                f"delete-account:{user_id}:{phone}",
                                timeout=10,
                            )
                        except Exception:
                            pass
                    except Exception:
                        pass
            finally:
                # 4) 从内存移除
                accounts.pop(phone, None)

            # 5) 删除 session 文件及 SQLite 附属文件
            try:
                if session_file:
                    safe_remove_session_files(os.path.join(SESSIONS_DIR, session_file))
            except Exception:
                logger.exception("删除会话文件失败")
            AccountManager.remove_hosted_account_metadata(user_id, phone)

        return t(language, "hosting.account_deleted", phone=display_phone)

    @staticmethod
    async def kick_other_sessions(user_id: int, phone: str) -> str:
        phone = AccountManager.normalize_phone(phone)
        async with AccountManager._get_account_operation_lock(user_id, phone):
            return await AccountManager._kick_other_sessions_unlocked(user_id, phone)

    @staticmethod
    async def _kick_other_sessions_unlocked(user_id: int, phone: str) -> str:
        """踢出该号其他会话（保留当前托管 session）"""
        language = DataManager.get_user_language(user_id)
        if not AccountManager.check_access(user_id):
            return t(language, "hosting.no_access")
        accounts = AccountManager.get_user_accounts(user_id)
        phone = AccountManager.normalize_phone(phone)
        if phone not in accounts:
            return t(language, "protection.no_account")
        cooldown_msg = AccountManager._check_hosting_cooldown(user_id, phone, "kick_sessions", 3 * 60)
        if cooldown_msg:
            return cooldown_msg
        ok, accounts, phone, acc_info, client, message = await AccountManager.ensure_hosted_client_ready(
            user_id,
            phone,
            "kick_sessions",
        )
        if not ok:
            return message
        try:
            await client(functions.auth.ResetAuthorizationsRequest())
            return t(language, "hosting.sessions_kicked")
        except Exception as e:
            return await AccountManager.handle_hosted_operation_error(user_id, phone, client, "kick_sessions", e)

    @staticmethod
    def _cleanup_error(label: str, error: Exception) -> str:
        detail = str(error).replace("\n", " ").strip() or error.__class__.__name__
        return f"{label}：{detail[:160]}"

    @staticmethod
    def _cleanup_should_stop(error: Exception) -> bool:
        return isinstance(
            error,
            (
                asyncio.TimeoutError,
                FloodWaitError,
                ConnectionError,
                OSError,
                AuthKeyUnregisteredError,
                SessionRevokedError,
                SessionPasswordNeededError,
            ),
        )

    @staticmethod
    def _cleanup_session_is_invalid(error: Exception) -> bool:
        return isinstance(
            error,
            (AuthKeyUnregisteredError, SessionRevokedError, SessionPasswordNeededError),
        )

    @staticmethod
    async def _record_cleanup_operation_error(
        result: AccountCleanupResult,
        user_id: int,
        phone: str,
        client: TelegramClient,
        label: str,
        error: Exception,
    ) -> None:
        if AccountManager._cleanup_should_stop(error):
            message = await AccountManager.handle_hosted_operation_error(
                user_id, phone, client, "clean_account", error
            )
            compact_message = " ".join(message.split())[:200]
            result.errors.append(f"{label}：{compact_message}")
            return
        result.errors.append(AccountManager._cleanup_error(label, error))

    @staticmethod
    def _finalize_cleanup_result(result: AccountCleanupResult) -> AccountCleanupResult:
        if not result.errors:
            result.status = "success"
        elif result.chats_deleted or result.contacts_deleted:
            result.status = "partial"
        else:
            result.status = "failed"
        return result

    @staticmethod
    async def _clean_hosted_account_operations(
        user_id: int,
        phone: str,
        client: TelegramClient,
        clean_type: str,
        result: AccountCleanupResult,
    ) -> None:
        language = DataManager.get_user_language(user_id)
        if clean_type in {"chats", "all"}:
            try:
                dialogs = await client.get_dialogs()
            except Exception as error:
                await AccountManager._record_cleanup_operation_error(
                    result, user_id, phone, client,
                    t(language, "hosting.clean_error.dialogs"), error
                )
                if AccountManager._cleanup_session_is_invalid(error):
                    return
                dialogs = []

            for dialog in dialogs:
                entity = getattr(dialog, "entity", None)
                try:
                    await client.delete_dialog(entity or dialog.id)
                    result.chats_deleted += 1
                    if entity is not None and bool(getattr(entity, "bot", False)):
                        try:
                            await client(functions.contacts.BlockRequest(id=entity))
                        except Exception as error:
                            await AccountManager._record_cleanup_operation_error(
                                result, user_id, phone, client,
                                t(language, "hosting.clean_error.block_bot"), error
                            )
                            if AccountManager._cleanup_should_stop(error):
                                if AccountManager._cleanup_session_is_invalid(error):
                                    return
                                break
                except Exception as error:
                    await AccountManager._record_cleanup_operation_error(
                        result, user_id, phone, client,
                        t(language, "hosting.clean_error.delete_chat"), error
                    )
                    if AccountManager._cleanup_should_stop(error):
                        if AccountManager._cleanup_session_is_invalid(error):
                            return
                        break
                await asyncio.sleep(0.1)

        if clean_type in {"contacts", "all"}:
            try:
                contacts = await client(functions.contacts.GetContactsRequest(hash=0))
                contact_ids = {contact.user_id for contact in contacts.contacts}
                input_users = [user for user in contacts.users if user.id in contact_ids]
                missing_count = len(contact_ids) - len(input_users)
                if input_users:
                    await client(functions.contacts.DeleteContactsRequest(id=input_users))
                    result.contacts_deleted += len(input_users)
                if missing_count:
                    result.errors.append(t(
                        language,
                        "hosting.clean_error.missing_contacts",
                        count=missing_count,
                    ))
            except Exception as error:
                await AccountManager._record_cleanup_operation_error(
                    result, user_id, phone, client,
                    t(language, "hosting.clean_error.delete_contacts"), error
                )
                if AccountManager._cleanup_session_is_invalid(error):
                    return

    @staticmethod
    async def clean_hosted_account(
        user_id: int,
        phone: str,
        clean_type: str,
    ) -> AccountCleanupResult:
        phone = AccountManager.normalize_phone(phone)
        async with AccountManager._get_account_operation_lock(user_id, phone):
            return await AccountManager._clean_hosted_account_unlocked(
                user_id, phone, clean_type
            )

    @staticmethod
    async def _clean_hosted_account_unlocked(
        user_id: int,
        phone: str,
        clean_type: str,
    ) -> AccountCleanupResult:
        """清理单个在线托管账户的数据，但保留当前托管会话。"""
        result = AccountCleanupResult(status="failed")
        if clean_type not in {"chats", "contacts", "all"}:
            result.errors.append(t(
                DataManager.get_user_language(user_id),
                "hosting.clean_error.invalid_type",
            ))
            return result

        normalized_phone = AccountManager.normalize_phone(phone)
        lock_key = f"{user_id}_{normalized_phone}"
        async with AccountManager._get_session_lock(lock_key):
            ok, _, normalized_phone, acc_info, message = AccountManager.ensure_account_operable(
                user_id, normalized_phone
            )
            if not ok:
                result.errors.append(message)
                return result
            remaining = AccountManager.get_hosting_clean_remaining_seconds(
                user_id, normalized_phone, acc_info
            )
            if remaining:
                result.errors.append(AccountManager.hosting_clean_age_message(
                    remaining, DataManager.get_user_language(user_id)
                ))
                return result

            cooldown_msg = AccountManager._check_hosting_cooldown(
                user_id, normalized_phone, "clean_account", 3 * 60
            )
            if cooldown_msg:
                result.errors.append(cooldown_msg)
                return result

            ok, _, normalized_phone, _, client, message = (
                await AccountManager.ensure_hosted_client_ready(
                    user_id,
                    normalized_phone,
                    "clean_account",
                )
            )
            if not ok:
                result.errors.append(message)
                return result

            try:
                await asyncio.wait_for(
                    AccountManager._clean_hosted_account_operations(
                        user_id,
                        normalized_phone,
                        client,
                        clean_type,
                        result,
                    ),
                    timeout=HOSTING_CLEAN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                result.errors.append(t(
                    DataManager.get_user_language(user_id),
                    "hosting.clean_timeout_result",
                    seconds=HOSTING_CLEAN_TIMEOUT_SECONDS,
                ))
            except Exception as error:
                message = await AccountManager.handle_hosted_operation_error(
                    user_id,
                    normalized_phone,
                    client,
                    "clean_account",
                    error,
                )
                result.errors.append(message)

        return AccountManager._finalize_cleanup_result(result)


    @staticmethod
    async def request_2fa_reset(user_id: int, phone: str) -> str:
        phone = AccountManager.normalize_phone(phone)
        async with AccountManager._get_account_operation_lock(user_id, phone):
            return await AccountManager._request_2fa_reset_unlocked(user_id, phone)

    @staticmethod
    async def _request_2fa_reset_unlocked(user_id: int, phone: str) -> str:
        """触发 Telegram 官方的“忘记二级密码/等待期重置”(通常 7 天)。"""
        language = DataManager.get_user_language(user_id)
        if not AccountManager.check_access(user_id):
            return t(language, "hosting.no_access")

        accounts = AccountManager.get_user_accounts(user_id)
        phone = AccountManager.normalize_phone(phone)
        if phone not in accounts:
            return t(language, "protection.no_account")
        cooldown_msg = AccountManager._check_hosting_cooldown(user_id, phone, "reset_2fa", 60 * 60)
        if cooldown_msg:
            return cooldown_msg

        ok, accounts, phone, acc_info, client, message = await AccountManager.ensure_hosted_client_ready(
            user_id,
            phone,
            "reset_2fa",
        )
        if not ok:
            return message
        try:
            result = await client(functions.account.ResetPasswordRequest())

            # Telethon 会把 date 字段解析为 datetime（UTC）
            if isinstance(result, types.account.ResetPasswordRequestedWait):
                until = result.until_date
                # until 可能是 datetime 或 int(Unix)
                if hasattr(until, "strftime"):
                    until_str = until.strftime("%Y-%m-%d %H:%M:%S UTC")
                else:
                    until_str = str(until)
                return t(language, "hosting.password_reset_wait", time=until_str)

            if isinstance(result, types.account.ResetPasswordFailedWait):
                retry = result.retry_date
                if hasattr(retry, "strftime"):
                    retry_str = retry.strftime("%Y-%m-%d %H:%M:%S UTC")
                else:
                    retry_str = str(retry)
                return t(language, "hosting.password_reset_retry", time=retry_str)

            # ResetPasswordOk：可能表示无需等待/已无二级密码/或已完成
            if isinstance(result, types.account.ResetPasswordOk):
                return t(language, "hosting.password_reset_ok")

            # 兜底
            return t(language, "hosting.password_reset_sent")
        except Exception as e:
            return await AccountManager.handle_hosted_operation_error(user_id, phone, client, "reset_2fa", e)

    @staticmethod
    def _is_2fa_password_invalid_error(error: Exception) -> bool:
        text = f"{error.__class__.__name__} {str(error)}".lower()
        return (
            "passwordhashinvalid" in text
            or "password hash invalid" in text
            or "invalid password" in text
            or "password invalid" in text
        )

    @staticmethod
    async def change_hosted_2fa(user_id: int, phone: str, old_password: str, new_password: str) -> str:
        phone = AccountManager.normalize_phone(phone)
        async with AccountManager._get_account_operation_lock(user_id, phone):
            return await AccountManager._change_hosted_2fa_unlocked(
                user_id, phone, old_password, new_password
            )

    @staticmethod
    async def _change_hosted_2fa_unlocked(user_id: int, phone: str, old_password: str, new_password: str) -> str:
        """Change an existing hosted account 2FA password."""
        language = DataManager.get_user_language(user_id)
        old_password = (old_password or "").strip()
        new_password = (new_password or "").strip()
        if not old_password or not new_password:
            return t(language, "hosting.password_change_format")

        ok, accounts, phone, acc_info, client, message = await AccountManager.ensure_hosted_client_ready(
            user_id,
            phone,
            "change_2fa",
        )
        if not ok:
            return message

        try:
            await client.edit_2fa(current_password=old_password, new_password=new_password)
            return t(language, "hosting.password_changed")
        except Exception as e:
            if AccountManager._is_2fa_password_invalid_error(e):
                return t(language, "hosting.password_old_wrong")
            return await AccountManager.handle_hosted_operation_error(user_id, phone, client, "change_2fa", e)

    @staticmethod
    async def set_hosted_2fa(user_id: int, phone: str, new_password: str) -> str:
        phone = AccountManager.normalize_phone(phone)
        async with AccountManager._get_account_operation_lock(user_id, phone):
            return await AccountManager._set_hosted_2fa_unlocked(user_id, phone, new_password)

    @staticmethod
    async def _set_hosted_2fa_unlocked(user_id: int, phone: str, new_password: str) -> str:
        """Set a new 2FA password for a hosted account without current 2FA."""
        language = DataManager.get_user_language(user_id)
        new_password = (new_password or "").strip()
        if not new_password:
            return t(language, "hosting.password_new_empty")

        ok, accounts, phone, acc_info, client, message = await AccountManager.ensure_hosted_client_ready(
            user_id,
            phone,
            "set_2fa",
        )
        if not ok:
            return message

        try:
            await client.edit_2fa(new_password=new_password)
            return t(language, "hosting.password_set")
        except Exception as e:
            if AccountManager._is_2fa_password_invalid_error(e):
                return t(language, "hosting.password_already_set")
            return await AccountManager.handle_hosted_operation_error(user_id, phone, client, "set_2fa", e)

    @staticmethod
    async def clear_hosted_2fa(user_id: int, phone: str, old_password: str) -> str:
        phone = AccountManager.normalize_phone(phone)
        async with AccountManager._get_account_operation_lock(user_id, phone):
            return await AccountManager._clear_hosted_2fa_unlocked(user_id, phone, old_password)

    @staticmethod
    async def _clear_hosted_2fa_unlocked(user_id: int, phone: str, old_password: str) -> str:
        """Clear the 2FA password for a hosted account."""
        language = DataManager.get_user_language(user_id)
        old_password = (old_password or "").strip()
        if not old_password:
            return t(language, "hosting.password_old_empty")

        ok, accounts, phone, acc_info, client, message = await AccountManager.ensure_hosted_client_ready(
            user_id,
            phone,
            "clear_2fa",
        )
        if not ok:
            return message

        try:
            await client.edit_2fa(current_password=old_password, new_password=None)
            return t(language, "hosting.password_cleared")
        except Exception as e:
            if AccountManager._is_2fa_password_invalid_error(e):
                return t(language, "hosting.password_old_wrong")
            return await AccountManager.handle_hosted_operation_error(user_id, phone, client, "clear_2fa", e)

    @staticmethod
    def check_access(user_id: int) -> bool:
        """检查用户访问权限"""
        return DataManager.is_admin(user_id) or DataManager.has_active_subscription(user_id)

    @staticmethod
    def _digits_only(phone: str) -> str:
        """提取手机号纯数字（用于 session 文件名）"""
        return re.sub(r"\D", "", phone or "")

    @staticmethod
    def _find_account_key_by_digits(accounts: Dict[str, Dict], digits: str) -> Optional[str]:
        """在账户字典中通过 digits 匹配到实际的 phone key（accounts 的 key 可能带 +）。"""
        if not accounts or not digits:
            return None
        for k in accounts.keys():
            if AccountManager._digits_only(k) == digits:
                return k
        return None

    @staticmethod
    def validate_account_transfer_offer(
        from_user_id: int, phone_input: str
    ) -> AccountTransferResult:
        """Validate that an owned account may be offered for transfer."""
        digits = AccountManager._digits_only(phone_input)
        phone = f"+{digits}" if digits else ""
        result_kwargs = {
            "phone": phone,
            "from_user_id": from_user_id,
        }

        if not AccountManager.check_access(from_user_id):
            return AccountTransferResult(False, "source_not_vip", "❌ 只有VIP或管理员可以转让账户", **result_kwargs)
        if not digits:
            return AccountTransferResult(False, "invalid_phone", "❌ 账户格式错误", **result_kwargs)

        from_accounts = user_accounts.get(from_user_id, {})
        from_phone_key = AccountManager._find_account_key_by_digits(from_accounts, digits)
        if not from_phone_key:
            return AccountTransferResult(False, "not_owned", "❌ 该账户不在你名下", **result_kwargs)

        if AccountManager.is_uploaded_transfer_locked(from_user_id, from_phone_key):
            return AccountTransferResult(
                False,
                "uploaded_session_not_transferable",
                "❌ 通过上传 Session 添加的账户不允许转让",
                **result_kwargs,
            )

        remaining = AccountManager.get_account_transfer_remaining_seconds(
            from_user_id, from_phone_key
        )
        if remaining > 0:
            total_minutes = math.ceil(remaining / 60)
            hours, minutes = divmod(total_minutes, 60)
            if hours and minutes:
                wait_text = f"{hours}小时{minutes}分钟"
            elif hours:
                wait_text = f"{hours}小时"
            else:
                wait_text = f"{minutes}分钟"
            return AccountTransferResult(
                False,
                "too_new",
                f"❌ 该账户尚未满足转让时间限制，还需等待约{wait_text}",
                **result_kwargs,
            )

        return AccountTransferResult(True, "ready", "可以发起转让", **result_kwargs)

    @staticmethod
    def validate_account_transfer(
        from_user_id: int, phone_input: str, to_user_id: int
    ) -> AccountTransferResult:
        """Validate an owned-account transfer without changing runtime state."""
        digits = AccountManager._digits_only(phone_input)
        phone = f"+{digits}" if digits else ""
        result_kwargs = {
            "phone": phone,
            "from_user_id": from_user_id,
            "to_user_id": to_user_id,
        }

        if not AccountManager.check_access(from_user_id):
            return AccountTransferResult(False, "source_not_vip", "❌ 只有VIP或管理员可以转让账户", **result_kwargs)
        if not digits:
            return AccountTransferResult(False, "invalid_phone", "❌ 账户格式错误", **result_kwargs)
        if from_user_id == to_user_id:
            return AccountTransferResult(False, "same_user", "❌ 不能将账户转让给自己", **result_kwargs)
        if not AccountManager.check_access(to_user_id):
            return AccountTransferResult(False, "target_not_vip", "❌ 目标用户没有VIP权限，无法转让", **result_kwargs)
        if not AccountManager.can_add_hosted_account(to_user_id, phone):
            return AccountTransferResult(
                False, 'target_quota_full', AccountManager.quota_error_message(to_user_id),
                **result_kwargs,
            )

        source_validation = AccountManager.validate_account_transfer_offer(
            from_user_id, phone
        )
        if not source_validation.ok:
            source_validation.to_user_id = to_user_id
            return source_validation

        target_accounts = user_accounts.get(to_user_id, {})
        if AccountManager._find_account_key_by_digits(target_accounts, digits):
            return AccountTransferResult(False, "target_duplicate", "❌ 目标用户名下已存在该账户", **result_kwargs)

        target_path = os.path.join(SESSIONS_DIR, f"{to_user_id}_{digits}.session")
        if any(os.path.exists(path) for path in session_related_paths(target_path)):
            return AccountTransferResult(False, "target_session_exists", "❌ 目标用户名下已存在该账户会话", **result_kwargs)

        return AccountTransferResult(True, "ready", "可以转让", **result_kwargs)

    @staticmethod
    async def _restore_transfer_source(
        source_path: str,
        from_user_id: int,
        phone: str,
        anti_login: bool,
    ) -> bool:
        try:
            _, restored_phone, success, _ = await AccountManager.create_client_from_session(
                source_path, from_user_id, detailed=True
            )
            if success:
                restored = user_accounts.get(from_user_id, {}).get(restored_phone or phone)
                if restored is not None:
                    restored["anti_login"] = anti_login
                return True
        except Exception:
            logger.exception(f"转让失败后恢复原账户异常: 用户ID={from_user_id}, 手机号={phone}")
        return False

    @staticmethod
    def _transaction_subscription_snapshots(transaction: Dict) -> Dict[int, Optional[Dict]]:
        return {
            int(user_id): copy.deepcopy(snapshot)
            for user_id, snapshot in (transaction.get("subscription_snapshots") or {}).items()
        }

    @staticmethod
    def _restore_transfer_files_from_transaction(transaction: Dict) -> bool:
        source_path = transaction["source_path"]
        target_path = transaction["target_path"]
        try:
            for source_part, target_part in zip(
                session_related_paths(source_path), session_related_paths(target_path)
            ):
                source_exists = os.path.exists(source_part)
                target_exists = os.path.exists(target_part)
                if target_exists and not source_exists:
                    os.makedirs(os.path.dirname(source_part) or ".", exist_ok=True)
                    shutil.move(target_part, source_part)
                elif target_exists and source_exists:
                    logger.critical(
                        "转移恢复发现源和目标 Session 组件同时存在: %s, %s",
                        transaction.get("id"),
                        os.path.basename(source_part),
                    )
                    return False

            return True
        except Exception:
            logger.exception("恢复账户转移文件失败: %s", transaction.get("id"))
            return False

    @staticmethod
    def _restore_transfer_persistent_state(transaction: Dict) -> bool:
        metadata_ok = AccountManager._replace_hosted_metadata_records(
            transaction.get("metadata_snapshots") or {}
        )
        subscriptions_ok = True
        if str(transaction.get("phase") or "prepared") in {
            "subscriptions_writing", "subscriptions_saved", "committed"
        }:
            subscriptions_ok = DataManager.restore_subscription_snapshots(
                AccountManager._transaction_subscription_snapshots(transaction)
            )
        return metadata_ok and subscriptions_ok

    @staticmethod
    def recover_incomplete_account_transfers() -> bool:
        """Recover durable transfer transactions before hosted sessions are loaded."""
        with AccountManager._transfer_journal_guard:
            try:
                payload = AccountManager._load_transfer_journal_locked()
            except Exception:
                return False
            transactions = list(payload.get("transactions", {}).items())
            all_ok = True
            for transaction_id, transaction in transactions:
                phase = str(transaction.get("phase") or "prepared")
                if phase == "committed":
                    source_exists = any(
                        os.path.exists(path)
                        for path in session_related_paths(transaction["source_path"])
                    )
                    target_exists = os.path.exists(transaction["target_path"])
                    if target_exists and not source_exists:
                        payload["transactions"].pop(transaction_id, None)
                    else:
                        logger.critical("已提交账户转移的文件状态不一致: %s", transaction_id)
                        all_ok = False
                    continue

                files_ok = AccountManager._restore_transfer_files_from_transaction(transaction)
                state_ok = files_ok and AccountManager._restore_transfer_persistent_state(transaction)
                if state_ok:
                    payload["transactions"].pop(transaction_id, None)
                else:
                    logger.critical("账户转移启动恢复失败，事务已保留: %s", transaction_id)
                    all_ok = False
            if not AccountManager._save_transfer_journal_locked(payload):
                return False
            return all_ok

    @staticmethod
    def reconcile_historical_subscription_selections() -> bool:
        hosted = {
            int(user_id): sorted(AccountManager.hosted_account_phones(int(user_id)))
            for user_id in DataManager.get_subscription_user_ids()
        }
        return DataManager.reconcile_selected_accounts(hosted)

    @staticmethod
    async def _rollback_live_transfer(
        transaction: Dict,
        source_runtime: Dict,
        source_client: Optional[TelegramClient],
    ) -> bool:
        from_user_id = int(transaction["from_user_id"])
        to_user_id = int(transaction["to_user_id"])
        phone = str(transaction["phone"])
        target_task_key = f"{to_user_id}_{phone}"
        await AccountManager._cancel_client_task(target_task_key)
        await AccountManager._cancel_account_auxiliary_tasks(to_user_id, phone)
        target_info = user_accounts.get(to_user_id, {}).pop(phone, None)
        if target_info and target_info.get("client"):
            await AccountManager._safe_disconnect_client(
                target_info["client"], f"transfer-rollback-target:{to_user_id}:{phone}"
            )
        if to_user_id in user_accounts and not user_accounts[to_user_id]:
            user_accounts.pop(to_user_id, None)

        files_ok = AccountManager._restore_transfer_files_from_transaction(transaction)
        state_ok = files_ok and AccountManager._restore_transfer_persistent_state(transaction)
        if not state_ok:
            return False

        source_info = user_accounts.get(from_user_id, {}).get(phone)
        if source_info is None:
            try:
                _, restored_phone, success, _ = await AccountManager.create_client_from_session(
                    transaction["source_path"],
                    from_user_id,
                    detailed=True,
                    account_source=transaction.get("source_origin"),
                    backfill_recent=False,
                    ensure_selected=False,
                )
            except Exception:
                logger.exception("重新加载转移源账户失败")
                success = False
                restored_phone = None
            if not success:
                return False
            source_info = user_accounts.get(from_user_id, {}).get(restored_phone or phone)
        elif source_client and source_info.get("client") is source_client:
            try:
                if not source_client.is_connected():
                    await source_client.connect()
            except Exception:
                logger.exception("重新连接转移源账户失败")
                return False
        if not source_info:
            return False
        AccountManager._restore_transfer_runtime(
            from_user_id, phone, source_info, source_runtime
        )
        return AccountManager._remove_transfer_transaction(transaction["id"])

    @staticmethod
    async def transfer_account(
        from_user_id: int,
        phone_input: str,
        to_user_id: int,
        notify_target: bool = True,
    ) -> AccountTransferResult:
        phone = AccountManager.normalize_phone(phone_input)
        async with AccountManager._get_quota_lock(to_user_id):
            operation_keys = sorted({
                AccountManager._hosted_metadata_key(from_user_id, phone),
                AccountManager._hosted_metadata_key(to_user_id, phone),
            })
            async with AsyncExitStack() as stack:
                for key in operation_keys:
                    lock = account_operation_locks.get(key)
                    if lock is None:
                        lock = asyncio.Lock()
                        account_operation_locks[key] = lock
                    await stack.enter_async_context(lock)
                return await AccountManager._transfer_account_unlocked(
                    from_user_id, phone, to_user_id, notify_target
                )

    @staticmethod
    async def _transfer_account_unlocked(
        from_user_id: int,
        phone_input: str,
        to_user_id: int,
        notify_target: bool = True,
    ) -> AccountTransferResult:
        """Transfer one hosted account as a durable, rollback-capable transaction."""
        checked = AccountManager.validate_account_transfer(from_user_id, phone_input, to_user_id)
        if not checked.ok:
            return checked

        digits = AccountManager._digits_only(phone_input)
        phone = f"+{digits}"
        source_lock_key = f"{from_user_id}_{phone}"
        target_lock_key = f"{to_user_id}_{phone}"
        async with AsyncExitStack() as stack:
            for lock_key in sorted({source_lock_key, target_lock_key}):
                await stack.enter_async_context(AccountManager._get_session_lock(lock_key))

            checked = AccountManager.validate_account_transfer(from_user_id, phone, to_user_id)
            if not checked.ok:
                return checked
            from_accounts = user_accounts.get(from_user_id, {})
            from_phone_key = AccountManager._find_account_key_by_digits(from_accounts, digits)
            source_info = from_accounts.get(from_phone_key)
            if not source_info:
                return AccountTransferResult(False, "not_owned", "账户已不在转出用户所属列表", phone, from_user_id, to_user_id)
            source_client = source_info.get("client")
            source_runtime = AccountManager._snapshot_transfer_runtime(source_info)
            source_path = source_info.get("original_session_path") or os.path.join(
                SESSIONS_DIR, source_info.get("session_file") or f"{from_user_id}_{digits}.session"
            )
            target_path = os.path.join(SESSIONS_DIR, f"{to_user_id}_{digits}.session")
            if not any(os.path.exists(path) for path in session_related_paths(source_path)):
                return AccountTransferResult(False, "source_session_missing", "未找到该账户的 Session 文件", phone, from_user_id, to_user_id)

            source_key = AccountManager._hosted_metadata_key(from_user_id, phone)
            target_key = AccountManager._hosted_metadata_key(to_user_id, phone)
            source_metadata = AccountManager.get_hosted_account_metadata_record(from_user_id, phone)
            target_metadata = AccountManager.get_hosted_account_metadata_record(to_user_id, phone)
            transferred_at = time.time()
            safe_target_metadata = AccountManager._safe_transfer_target_metadata(source_metadata, transferred_at)
            subscription_snapshots = {
                from_user_id: DataManager.get_raw_subscription_snapshot(from_user_id),
                to_user_id: DataManager.get_raw_subscription_snapshot(to_user_id),
            }
            transaction = {
                "id": uuid.uuid4().hex,
                "phase": "prepared",
                "created_at": transferred_at,
                "from_user_id": int(from_user_id),
                "to_user_id": int(to_user_id),
                "phone": phone,
                "source_path": source_path,
                "target_path": target_path,
                "session_moved": False,
                "source_origin": str((source_metadata or {}).get("source") or "unknown"),
                "source_runtime": source_runtime,
                "metadata_snapshots": {source_key: source_metadata, target_key: target_metadata},
                "subscription_snapshots": {
                    str(user_id): snapshot for user_id, snapshot in subscription_snapshots.items()
                },
            }
            if not AccountManager._upsert_transfer_transaction(transaction):
                return AccountTransferResult(False, "journal_failed", "无法安全创建转移事务，请稍后重试", phone, from_user_id, to_user_id)
            if not AccountManager._replace_hosted_metadata_records({target_key: safe_target_metadata}):
                AccountManager._remove_transfer_transaction(transaction["id"])
                return AccountTransferResult(False, "metadata_failed", "无法保存转移状态，请稍后重试", phone, from_user_id, to_user_id)

            await AccountManager._cancel_client_task(source_lock_key)
            await AccountManager._cancel_account_auxiliary_tasks(from_user_id, from_phone_key)
            if source_client:
                disconnected = await AccountManager._safe_disconnect_client(
                    source_client, f"transfer-old:{from_user_id}:{phone}", timeout=10
                )
                if not disconnected:
                    metadata_restored = AccountManager._replace_hosted_metadata_records(
                        transaction["metadata_snapshots"]
                    )
                    AccountManager._restore_transfer_runtime(from_user_id, phone, source_info, source_runtime)
                    if not metadata_restored or not AccountManager._remove_transfer_transaction(
                        transaction["id"]
                    ):
                        return AccountTransferResult(False, "rollback_failed", "转移中止但状态恢复未完成，请立即联系管理员", phone, from_user_id, to_user_id)
                    return AccountTransferResult(False, "source_disconnect_failed", "当前托管 Session 正在使用中，请稍后重试", phone, from_user_id, to_user_id)
            from_accounts.pop(from_phone_key, None)

            failure_code = "move_failed"
            try:
                AccountManager._move_session_files(source_path, target_path)
                transaction["session_moved"] = True
                transaction["phase"] = "files_moved"
                if not AccountManager._upsert_transfer_transaction(transaction):
                    raise OSError("failed to persist files_moved transfer stage")

                _, loaded_phone, success, load_reason = await AccountManager.create_client_from_session(
                    target_path,
                    to_user_id,
                    detailed=True,
                    account_source=transaction["source_origin"],
                    backfill_recent=False,
                    ensure_selected=False,
                )
                if not success:
                    failure_code = "target_load_failed"
                    raise RuntimeError(load_reason or "target client load failed")
                target_info = user_accounts.get(to_user_id, {}).get(loaded_phone or phone)
                if not target_info:
                    raise RuntimeError("target runtime account missing")
                target_info.update({
                    "anti_login": True,
                    "created_at": safe_target_metadata["created_at"],
                    "source": safe_target_metadata["source"],
                    "last_transferred_at": transferred_at,
                })
                for key in (
                    "temporary_mode", "temporary_until", "code_fetch_previous_mode",
                    "code_fetch_previous_until",
                ):
                    target_info.pop(key, None)
                transaction["phase"] = "target_loaded"
                if not AccountManager._upsert_transfer_transaction(transaction):
                    raise OSError("failed to persist target_loaded transfer stage")

                if not DataManager.subscription_snapshots_match(subscription_snapshots):
                    failure_code = "subscription_state_changed"
                    raise RuntimeError("subscription changed during transfer")
                transaction["phase"] = "subscriptions_writing"
                if not AccountManager._upsert_transfer_transaction(transaction):
                    raise OSError("failed to persist subscriptions_writing transfer stage")
                if not DataManager.transfer_selected_account(
                    from_user_id,
                    to_user_id,
                    phone,
                    subscription_snapshots,
                    sorted(AccountManager.hosted_account_phones(to_user_id)),
                ):
                    failure_code = "subscription_save_failed"
                    raise RuntimeError("failed to transfer subscription seat")
                transaction["phase"] = "subscriptions_saved"
                if not AccountManager._upsert_transfer_transaction(transaction):
                    raise OSError("failed to persist subscriptions_saved transfer stage")

                if not AccountManager._replace_hosted_metadata_records({source_key: None, target_key: safe_target_metadata}):
                    failure_code = "metadata_failed"
                    raise RuntimeError("failed to commit transfer metadata")
                transaction["phase"] = "committed"
                if not AccountManager._upsert_transfer_transaction(transaction):
                    failure_code = "journal_failed"
                    raise RuntimeError("failed to persist committed transfer stage")
            except Exception:
                logger.exception("账户转移事务失败: %s", transaction["id"])
                restored = await AccountManager._rollback_live_transfer(transaction, source_runtime, source_client)
                if not restored:
                    return AccountTransferResult(False, "rollback_failed", "转移失败且自动恢复未完成，请立即联系管理员", phone, from_user_id, to_user_id)
                return AccountTransferResult(False, failure_code, "账户转移失败，原账户已恢复", phone, from_user_id, to_user_id)

            if not AccountManager._remove_transfer_transaction(transaction["id"]):
                logger.warning("已提交转移事务日志暂未清除，将在启动时确认: %s", transaction["id"])

        target_notified = False
        bot = account_runtime.get_notify_bot()
        if bot and notify_target:
            target_notified = await AccountManager._safe_send_bot_message(
                bot,
                to_user_id,
                t(DataManager.get_user_language(to_user_id), "notify.transfer_received", phone=phone, source=from_user_id),
                context=f"account_transfer_target:{phone}",
            )
        message = t(DataManager.get_user_language(from_user_id), "transfer.success", phone=phone, target=to_user_id)
        if not target_notified:
            message += "\n\n" + t(
                DataManager.get_user_language(from_user_id),
                "transfer.notify_failed",
            )
        return AccountTransferResult(True, "success", message, phone, from_user_id, to_user_id, target_notified=target_notified)

    @staticmethod
    async def _transfer_account_legacy_unlocked(
        from_user_id: int,
        phone_input: str,
        to_user_id: int,
        notify_target: bool = True,
    ) -> AccountTransferResult:
        """Transfer one hosted account between VIP users with rollback on failure."""
        initial = AccountManager.validate_account_transfer(
            from_user_id, phone_input, to_user_id
        )
        if not initial.ok:
            return initial

        digits = AccountManager._digits_only(phone_input)
        phone = f"+{digits}"
        source_lock_key = f"{from_user_id}_{phone}"
        target_lock_key = f"{to_user_id}_{phone}"
        lock_keys = sorted({source_lock_key, target_lock_key})

        async with AsyncExitStack() as stack:
            for lock_key in lock_keys:
                await stack.enter_async_context(AccountManager._get_session_lock(lock_key))

            checked = AccountManager.validate_account_transfer(
                from_user_id, phone, to_user_id
            )
            if not checked.ok:
                return checked

            from_accounts = user_accounts.get(from_user_id, {})
            from_phone_key = AccountManager._find_account_key_by_digits(from_accounts, digits)
            source_info = dict(from_accounts.get(from_phone_key, {}))
            source_client = source_info.get("client")
            anti_login = bool(source_info.get("anti_login", True))
            source_created_at = AccountManager.get_hosted_account_created_at(
                from_user_id, from_phone_key
            )
            source_origin = AccountManager.get_hosted_account_source(
                from_user_id, from_phone_key
            )
            source_path = source_info.get("original_session_path")
            if not source_path:
                source_file = source_info.get("session_file") or f"{from_user_id}_{digits}.session"
                source_path = os.path.join(SESSIONS_DIR, source_file)
            target_path = os.path.join(SESSIONS_DIR, f"{to_user_id}_{digits}.session")
            if not any(os.path.exists(path) for path in session_related_paths(source_path)):
                return AccountTransferResult(
                    False, "source_session_missing", "❌ 未找到该账户的session文件",
                    phone, from_user_id, to_user_id
                )

            await AccountManager._cancel_client_task(source_lock_key)
            await AccountManager._cancel_account_auxiliary_tasks(from_user_id, from_phone_key)
            if source_client:
                disconnected = await AccountManager._safe_disconnect_client(
                    source_client,
                    f"transfer-old:{from_user_id}:{from_phone_key}",
                    timeout=10,
                )
                if not disconnected:
                    try:
                        if source_client.is_connected():
                            AccountManager._start_connection_watcher_task(
                                from_user_id, from_phone_key, source_client
                            )
                    except Exception:
                        logger.exception("转让中止后恢复连接监听失败")
                    return AccountTransferResult(
                        False,
                        "source_disconnect_failed",
                        "❌ 当前托管会话正在使用中，请稍后重试",
                        phone,
                        from_user_id,
                        to_user_id,
                    )
            from_accounts.pop(from_phone_key, None)

            session_moved = False
            try:
                AccountManager._move_session_files(source_path, target_path)
                session_moved = True
            except Exception:
                logger.exception("转让账户时移动 session 失败")
                rollback_ok = True
                if session_moved:
                    rollback_ok = AccountManager._rollback_transfer_files(
                        source_path,
                        target_path,
                    )
                restored = await AccountManager._restore_transfer_source(
                    source_path, from_user_id, phone, anti_login
                ) if rollback_ok else False
                message = (
                    "❌ 转让失败，账户已恢复"
                    if restored
                    else "❌ 转让失败，文件回滚或原账户恢复失败，请联系管理员"
                )
                return AccountTransferResult(
                    False, "move_failed", message, phone, from_user_id, to_user_id
                )

            try:
                _, loaded_phone, success, failure_reason = await AccountManager.create_client_from_session(
                    target_path, to_user_id, detailed=True
                )
            except Exception as error:
                logger.exception("转让后加载目标账户异常")
                success = False
                failure_reason = type(error).__name__
                loaded_phone = None

            if not success:
                user_accounts.get(to_user_id, {}).pop(loaded_phone or phone, None)
                rollback_ok = AccountManager._rollback_transfer_files(
                    source_path,
                    target_path,
                )
                if not rollback_ok:
                    return AccountTransferResult(
                        False,
                        "rollback_failed",
                        "❌ 转让失败且 session 回滚失败，请立即联系管理员",
                        phone,
                        from_user_id,
                        to_user_id,
                    )
                AccountManager.remove_hosted_account_metadata(to_user_id, phone)
                AccountManager.set_hosted_account_created_at(
                    from_user_id, phone, source_created_at or time.time()
                )
                restored = await AccountManager._restore_transfer_source(
                    source_path, from_user_id, phone, anti_login
                )
                message = (
                    "❌ 转让失败，账户已恢复"
                    if restored
                    else "❌ 转让失败，原账户恢复失败，请联系管理员"
                )
                logger.warning(
                    f"目标账户加载失败并回滚: 手机号={phone}, 原因={failure_reason}"
                )
                return AccountTransferResult(
                    False, "target_load_failed", message, phone, from_user_id, to_user_id
                )

            AccountManager.remove_hosted_account_metadata(from_user_id, phone)
            new_created_at = transfer_recipient_created_at(source_created_at)
            transferred_at = time.time()
            AccountManager.set_hosted_account_created_at(to_user_id, phone, new_created_at)
            AccountManager.set_hosted_account_source(to_user_id, phone, source_origin)
            AccountManager.set_hosted_account_last_transferred_at(
                to_user_id, phone, transferred_at
            )
            target_info = user_accounts.get(to_user_id, {}).get(loaded_phone or phone)
            if target_info is not None:
                target_info["created_at"] = new_created_at
                target_info["source"] = source_origin
                target_info["last_transferred_at"] = transferred_at

        target_notified = False
        bot = account_runtime.get_notify_bot()
        if bot and notify_target:
            target_notified = await AccountManager._safe_send_bot_message(
                bot,
                to_user_id,
                t(DataManager.get_user_language(to_user_id), "notify.transfer_received",
                  phone=phone, source=from_user_id),
                context=f"account_transfer_target:{phone}",
            )

        message = t(DataManager.get_user_language(from_user_id), "transfer.success",
                    phone=phone, target=to_user_id)
        if not target_notified:
            message += "\n\n" + t(
                DataManager.get_user_language(from_user_id),
                "transfer.notify_failed",
            )
        return AccountTransferResult(
            True,
            "success",
            message,
            phone,
            from_user_id,
            to_user_id,
            target_notified=target_notified,
        )
    
    @staticmethod
    def get_user_accounts(user_id: int) -> Dict[str, Dict]:
        """获取用户账户，检查权限"""
        if not AccountManager.check_access(user_id):
            return {}
            
        if user_id not in user_accounts:
            user_accounts[user_id] = {}
        return user_accounts[user_id]

    @staticmethod
    async def inspect_uploaded_session(session_path: str, user_id: int) -> tuple:
        """Validate an uploaded session without changing hosted files or runtime state."""
        if not AccountManager.check_access(user_id):
            return None, "permission"

        probe_client = None
        try:
            probe_client = TelegramClient(
                session_path,
                API_ID,
                API_HASH,
                auto_reconnect=True,
                **SESSION_CLIENT_KWARGS,
            )
            health = await AccountManager.validate_client_session(
                probe_client, os.path.basename(session_path)
            )
            if not health.get("ok"):
                return None, health.get("status", "error")
            me = health.get("me")
            raw_phone = getattr(me, "phone", None)
            if not raw_phone:
                return None, "missing_phone"
            return AccountManager.normalize_phone(f"+{raw_phone}"), "ok"
        except Exception as error:
            if AccountManager._is_uploaded_session_format_error(error):
                logger.debug(f"上传会话文件格式无效 {session_path}: {error}")
                return None, "invalid"
            logger.warning(f"上传会话预校验失败 {session_path}: {error}")
            return None, "error"
        finally:
            if probe_client:
                await AccountManager._safe_disconnect_client(
                    probe_client, f"upload-probe:{session_path}"
                )

    @staticmethod
    async def install_uploaded_session(session_path: str, user_id: int) -> tuple:
        async with AccountManager._get_quota_lock(user_id):
            return await AccountManager._install_uploaded_session_unlocked(session_path, user_id)

    @staticmethod
    async def _install_uploaded_session_unlocked(session_path: str, user_id: int) -> tuple:
        """Install an uploaded session, restoring the previous hosted session on failure."""
        normalized_phone, failure_reason = await AccountManager.inspect_uploaded_session(
            session_path, user_id
        )
        if not normalized_phone:
            return None, None, False, failure_reason

        if not AccountManager.can_add_hosted_account(user_id, normalized_phone):
            return None, normalized_phone, False, 'quota_full'

        task_key = f"{user_id}_{normalized_phone}"
        digits = AccountManager._digits_only(normalized_phone)
        target_path = os.path.join(SESSIONS_DIR, f"{user_id}_{digits}.session")
        rollback_path = os.path.join(
            SESSIONS_DIR,
            f".{user_id}_{digits}.{uuid.uuid4().hex}.upload-rollback.session",
        )

        async with AccountManager._get_session_lock(task_key):
            accounts = user_accounts.get(user_id, {})
            existing_key = AccountManager._find_account_key_by_digits(accounts, digits)
            existing_info = dict(accounts.get(existing_key, {})) if existing_key else {}
            existing_client = existing_info.get("client")
            existing_anti_login = bool(existing_info.get("anti_login", True))
            existing_path = existing_info.get("original_session_path") or target_path
            old_files_saved = False
            had_existing_files = any(
                os.path.exists(path)
                for path in session_related_paths(existing_path)
            )

            await AccountManager._cancel_client_task(task_key)
            if existing_key:
                await AccountManager._cancel_account_auxiliary_tasks(user_id, existing_key)

            if existing_client:
                disconnected = await AccountManager._safe_disconnect_client(
                    existing_client,
                    f"upload-replace:{user_id}:{normalized_phone}",
                    timeout=10,
                )
                if not disconnected:
                    AccountManager._start_connection_watcher_task(
                        user_id, existing_key or normalized_phone, existing_client
                    )
                    return None, normalized_phone, False, "existing_session_busy"

            try:
                if had_existing_files:
                    AccountManager._move_session_files(existing_path, rollback_path)
                    old_files_saved = True
                if existing_key:
                    accounts.pop(existing_key, None)

                client, loaded_phone, success, reason = await AccountManager.create_client_from_session(
                    session_path,
                    user_id,
                    detailed=True,
                    account_source="upload",
                )
                if success:
                    if old_files_saved:
                        safe_remove_session_files(rollback_path)
                    return client, loaded_phone, True, reason

                raise RuntimeError(reason or "upload_install_failed")
            except Exception as error:
                logger.warning(
                    f"上传会话替换失败，准备恢复旧会话: 用户ID={user_id}, "
                    f"手机号={normalized_phone}, 错误={error}"
                )
                if old_files_saved or not had_existing_files:
                    safe_remove_session_files(target_path)
                restored = False
                if old_files_saved:
                    try:
                        AccountManager._move_session_files(rollback_path, existing_path)
                    except Exception:
                        logger.exception(
                            f"上传失败后恢复旧会话文件异常: 用户ID={user_id}, 手机号={normalized_phone}"
                        )
                if had_existing_files and any(
                    os.path.exists(path)
                    for path in session_related_paths(existing_path)
                ):
                    try:
                        _, restored_phone, restored, _ = await AccountManager.create_client_from_session(
                            existing_path,
                            user_id,
                            detailed=True,
                        )
                        if restored:
                            restored_info = user_accounts.get(user_id, {}).get(
                                restored_phone or normalized_phone
                            )
                            if restored_info is not None:
                                restored_info["anti_login"] = existing_anti_login
                    except Exception:
                        logger.exception(
                            f"上传失败后重新加载旧会话异常: 用户ID={user_id}, 手机号={normalized_phone}"
                        )
                reason = "replace_failed_restored" if restored else "replace_failed"
                return None, normalized_phone, False, reason
    
    @staticmethod
    async def create_client_from_session(
        session_path: str,
        user_id: int,
        detailed: bool = False,
        check_freeze: bool = False,
        preserved_health_status: str = None,
        preserved_freeze_info: Dict = None,
        account_source: str = None,
        backfill_recent: Optional[bool] = None,
        ensure_selected: bool = True,
    ) -> tuple:
        """Serialize every open/move/reopen sequence by canonical session path."""
        async with AccountManager._get_session_path_lock(session_path):
            return await AccountManager._create_client_from_session_unlocked(
                session_path,
                user_id,
                detailed=detailed,
                check_freeze=check_freeze,
                preserved_health_status=preserved_health_status,
                preserved_freeze_info=preserved_freeze_info,
                account_source=account_source,
                backfill_recent=backfill_recent,
                ensure_selected=ensure_selected,
            )

    @staticmethod
    async def _create_client_from_session_unlocked(
        session_path: str,
        user_id: int,
        detailed: bool = False,
        check_freeze: bool = False,
        preserved_health_status: str = None,
        preserved_freeze_info: Dict = None,
        account_source: str = None,
        backfill_recent: Optional[bool] = None,
        ensure_selected: bool = True,
    ) -> tuple:
        """从session文件创建客户端，返回 (client, phone, success)

        关键点：
        - 先用“上传的 session_path(可能是临时文件)”连接并读取手机号
        - 断开后将 session 移动到目标目录
        - 再用目标路径重新创建长期运行的 client，避免临时文件被删除导致监控/重连不稳定
        """
        # 检查权限
        if not AccountManager.check_access(user_id):
            result = (None, None, False, "permission")
            return result if detailed else result[:3]

        temp_client = None
        client = None
        normalized_phone = None
        new_session_path = None
        moved_session = False
        try:
            # 1) 先用上传的session文件读取账号信息
            temp_client = TelegramClient(
                session_path,
                API_ID,
                API_HASH,
                auto_reconnect=True,
                **SESSION_CLIENT_KWARGS,
            )
            health = await AccountManager.validate_client_session(temp_client, os.path.basename(session_path))
            if not health.get("ok"):
                await AccountManager._safe_disconnect_client(temp_client, f"temp:{session_path}")
                result = (None, None, False, health.get("status", "error"))
                return result if detailed else result[:3]

            me = health.get("me")
            phone = f"+{me.phone}"

            # 2) 生成目标session路径（使用标准化手机号）
            normalized_phone = AccountManager.normalize_phone(phone)
            new_session_name = f"{user_id}_{normalized_phone.replace('+', '')}.session"
            new_session_path = os.path.join(SESSIONS_DIR, new_session_name)

            # 3) 断开临时client，释放session文件占用
            disconnected = await AccountManager._safe_disconnect_client(
                temp_client, f"temp:{session_path}"
            )
            if not disconnected:
                result = (None, normalized_phone, False, "session_busy")
                return result if detailed else result[:3]
            temp_client = None

            # 4) 移动session到目标目录（推荐move；更避免临时文件被外层删除）
            os.makedirs(SESSIONS_DIR, exist_ok=True)
            if session_path != new_session_path:
                if os.path.exists(new_session_path):
                    safe_remove_session_files(new_session_path)
                shutil.move(session_path, new_session_path)
                moved_session = True

            # 5) 使用目标路径重新创建长期运行client
            client = TelegramClient(
                new_session_path,
                API_ID,
                API_HASH,
                auto_reconnect=True,
                **HOSTED_SESSION_CLIENT_KWARGS,
            )
            health = await AccountManager.validate_client_session(client, normalized_phone)
            if not health.get("ok"):
                await AccountManager._safe_disconnect_client(client, f"target:{new_session_path}")
                if moved_session:
                    safe_remove_session_files(new_session_path)
                result = (None, None, False, health.get("status", "error"))
                return result if detailed else result[:3]

            health_status = preserved_health_status or "alive"
            freeze_info = preserved_freeze_info
            if check_freeze:
                freeze_result = await AccountManager.check_account_freeze_status(client, normalized_phone)
                if freeze_result.get("ok"):
                    health_status = freeze_result.get("status", "alive")
                    freeze_info = freeze_result.get("freeze_info")

            client._last_health_status = health_status
            client._last_freeze_info = freeze_info
            created_at = AccountManager.get_hosted_account_created_at(
                user_id, normalized_phone
            )
            source = account_source or AccountManager.get_hosted_account_source(
                user_id, normalized_phone
            )

            # 6) 存储账户信息（使用标准化手机号作为键）
            if user_id not in user_accounts:
                user_accounts[user_id] = {}

            user_accounts[user_id][normalized_phone] = {
                'client': client,
                'anti_login': True,
                'session_file': new_session_name,
                'last_reload': time.time(),
                'original_session_path': new_session_path,
                'display_phone': AccountManager.format_phone_display(phone),  # 存储显示格式
                'runtime_status': 'online',
                'offline_reason': None,
                'offline_at': None,
                'health_status': health_status,
                'freeze_info': freeze_info,
                'created_at': created_at,
                'source': source,
            }

            # 7) 设置监控并启动客户端保持运行
            if not await AccountManager.setup_monitoring(
                client,
                normalized_phone,
                user_id,
                backfill_recent=(
                    account_source != "upload"
                    if backfill_recent is None
                    else bool(backfill_recent)
                ),
            ):
                user_accounts[user_id].pop(normalized_phone, None)
                if not user_accounts[user_id]:
                    user_accounts.pop(user_id, None)
                await AccountManager._safe_disconnect_client(client, f"monitoring-failed:{new_session_path}")
                if moved_session:
                    safe_remove_session_files(new_session_path)
                result = (None, None, False, "monitoring_failed")
                return result if detailed else result[:3]

            if ensure_selected and not AccountManager.ensure_account_selected(
                user_id, normalized_phone
            ):
                user_accounts[user_id].pop(normalized_phone, None)
                if not user_accounts[user_id]:
                    user_accounts.pop(user_id, None)
                await AccountManager._safe_disconnect_client(
                    client, f"selection-failed:{new_session_path}"
                )
                result = (None, None, False, "selection_failed")
                return result if detailed else result[:3]

            # 启动客户端保持运行的任务
            task_key = f"{user_id}_{normalized_phone}"
            await AccountManager._cancel_client_task(task_key)
            AccountManager._start_connection_watcher_task(user_id, normalized_phone, client)

            if account_source:
                AccountManager.set_hosted_account_source(user_id, normalized_phone, account_source)
                user_accounts[user_id][normalized_phone]["source"] = account_source

            result = (client, normalized_phone, True, "ok")
            return result if detailed else result[:3]

        except SessionRevokedError:
            logger.warning(f"会话已撤销: {session_path}")
            await AccountManager.backup_session_file(session_path, "revoked")
            result = (None, None, False, "revoked")
            return result if detailed else result[:3]

        except Exception as e:
            if AccountManager._is_uploaded_session_format_error(e):
                logger.warning(f"会话文件格式无效 {session_path}: {str(e)}")
                try:
                    if client and client.is_connected():
                        await AccountManager._safe_disconnect_client(client, f"invalid-format:{session_path}")
                except Exception:
                    pass
                try:
                    if temp_client and temp_client.is_connected():
                        await AccountManager._safe_disconnect_client(temp_client, f"invalid-format-temp:{session_path}")
                except Exception:
                    pass
                if moved_session and new_session_path:
                    safe_remove_session_files(new_session_path)
                result = (None, None, False, "invalid")
                return result if detailed else result[:3]

            logger.error(f"创建客户端失败 {session_path}: {str(e)}")
            try:
                if client and client.is_connected():
                    await AccountManager._safe_disconnect_client(client, f"create-failed:{session_path}")
            except Exception:
                pass
            if moved_session and new_session_path:
                safe_remove_session_files(new_session_path)
            result = (None, None, False, "error")
            return result if detailed else result[:3]

        finally:
            # 兜底：确保临时client被断开
            try:
                if temp_client and temp_client.is_connected():
                    await AccountManager._safe_disconnect_client(temp_client, f"finally-temp:{session_path}")
            except Exception:
                pass

    @staticmethod
    async def promote_pending_client(
        pending_client: TelegramClient,
        phone: str,
        user_id: int,
        display_phone: str = "",
        pending_session_path: str = "",
    ) -> TelegramClient:
        """Move an authorized pending login session into the hosted session area."""
        normalized_phone = AccountManager.normalize_phone(phone)
        display_phone = display_phone or AccountManager.format_phone_display(normalized_phone)
        pending_session_path = pending_session_path or AccountManager._client_session_path(pending_client)
        session_name = f"{user_id}_{normalized_phone.replace('+', '')}.session"
        session_path = os.path.join(SESSIONS_DIR, session_name)
        task_key = f"{user_id}_{normalized_phone}"

        async with AccountManager._get_quota_lock(user_id):
            if not AccountManager.can_add_hosted_account(user_id, normalized_phone):
                raise PermissionError(AccountManager.quota_error_message(user_id))
            return await AccountManager._promote_pending_client_locked(
                pending_client, normalized_phone, user_id, display_phone, pending_session_path,
            )

    @staticmethod
    async def _promote_pending_client_locked(
        pending_client: TelegramClient, normalized_phone: str, user_id: int,
        display_phone: str, pending_session_path: str,
    ) -> TelegramClient:
        session_name = f"{user_id}_{normalized_phone.replace('+', '')}.session"
        session_path = os.path.join(SESSIONS_DIR, session_name)
        task_key = f"{user_id}_{normalized_phone}"
        async with AccountManager._get_session_lock(task_key):
            await AccountManager._cancel_client_task(task_key)
            disconnected = await AccountManager._safe_disconnect_client(
                pending_client,
                f"promote-pending:{normalized_phone}",
                timeout=10,
            )
            if not disconnected:
                raise RuntimeError("pending session is still busy; please restart login and try again")

            os.makedirs(SESSIONS_DIR, exist_ok=True)
            try:
                AccountManager._move_session_files(pending_session_path, session_path)
            except PermissionError as e:
                raise RuntimeError(f"pending session file is busy: {e}") from e

            client = TelegramClient(
                session_path,
                API_ID,
                API_HASH,
                auto_reconnect=True,
                **HOSTED_SESSION_CLIENT_KWARGS,
            )
            health = await AccountManager.validate_client_session(client, normalized_phone)
            if not health.get("ok"):
                await AccountManager._safe_disconnect_client(client, f"promote-health:{session_path}")
                raise RuntimeError(f"promoted session health failed: {health.get('status')}")

            client._last_health_status = "alive"
            client._last_freeze_info = None
            created_at = AccountManager.get_hosted_account_created_at(
                user_id, normalized_phone
            )
            source = "login"

            if user_id not in user_accounts:
                user_accounts[user_id] = {}

            user_accounts[user_id][normalized_phone] = {
                'client': client,
                'anti_login': True,
                'session_file': session_name,
                'last_reload': time.time(),
                'original_session_path': session_path,
                'display_phone': display_phone,
                'runtime_status': 'online',
                'offline_reason': None,
                'offline_at': None,
                'health_status': 'alive',
                'freeze_info': None,
                'created_at': created_at,
                'source': source,
            }

            if not await AccountManager.setup_monitoring(
                client, normalized_phone, user_id, backfill_recent=False
            ):
                user_accounts[user_id].pop(normalized_phone, None)
                if not user_accounts[user_id]:
                    user_accounts.pop(user_id, None)
                await AccountManager._safe_disconnect_client(client, f"promote-monitoring:{session_path}")
                raise RuntimeError("login monitoring registration failed")
            if not AccountManager.ensure_account_selected(user_id, normalized_phone):
                user_accounts[user_id].pop(normalized_phone, None)
                if not user_accounts[user_id]:
                    user_accounts.pop(user_id, None)
                await AccountManager._safe_disconnect_client(
                    client, f"promote-selection:{session_path}"
                )
                raise RuntimeError("subscription account selection failed")
            AccountManager._start_connection_watcher_task(user_id, normalized_phone, client)
            AccountManager.set_hosted_account_source(user_id, normalized_phone, source)

            return client

    @staticmethod
    async def backup_session_file(session_path: str, reason: str):
        """备份session文件而不是直接删除"""
        try:
            if not AccountManager.should_backup_session(reason=reason):
                logger.debug(f"跳过会话备份: 路径={session_path}, 原因={reason}")
                return False

            related_paths = [
                p for p in session_related_paths(session_path)
                if os.path.exists(p)
            ]
            if related_paths:
                backup_dir = os.path.join(SESSIONS_DIR, 'backup')
                if not os.path.exists(backup_dir):
                    os.makedirs(backup_dir)

                backup_time = datetime.now()
                for p in related_paths:
                    filename = os.path.basename(p)
                    backup_name = AccountManager._session_backup_name(filename, reason, when=backup_time)
                    backup_path = AccountManager._available_backup_path(backup_dir, backup_name)

                    shutil.copy2(p, backup_path)
                    logger.info(f"会话文件已备份: {backup_path}")

                    # 只删除原文件，不删除备份
                    os.remove(p)
                return True
            return False
        except Exception as e:
            logger.error(f"备份会话文件失败: {str(e)}")
            return False
    
    @staticmethod
    async def create_new_client(phone: str, user_id: int) -> TelegramClient:
        """创建新客户端"""
        # 检查权限
        if not AccountManager.check_access(user_id):
            raise PermissionError(t(
                DataManager.get_user_language(user_id), "account.no_access"
            ))
        
        # 标准化手机号
        normalized_phone = AccountManager.normalize_phone(phone)
        display_phone = AccountManager.format_phone_display(phone)
            
        session_name = f"{user_id}_{normalized_phone.replace('+', '')}.session"
        os.makedirs(PENDING_SESSIONS_DIR, exist_ok=True)
        session_path = os.path.join(PENDING_SESSIONS_DIR, session_name)

        task_key = f"{user_id}_{normalized_phone}"
        async with AccountManager._get_session_lock(task_key):
            if any(os.path.exists(path) for path in session_related_paths(session_path)):
                remove_result = AccountManager._remove_session_files_checked(session_path)
                if not remove_result.ok:
                    raise RuntimeError(t(
                        DataManager.get_user_language(user_id),
                        "account.session_releasing",
                    ))

            client = TelegramClient(
                session_path,
                API_ID,
                API_HASH,
                auto_reconnect=True,
                **SESSION_CLIENT_KWARGS,
            )
        
        client._pending_session_path = session_path
        client._pending_display_phone = display_phone
        
        return client

    @staticmethod
    async def create_qr_client(user_id: int) -> TelegramClient:
        """为扫码登录创建临时客户端。"""
        if not AccountManager.check_access(user_id):
            raise PermissionError(t(
                DataManager.get_user_language(user_id), "account.no_access"
            ))

        session_name = f"{user_id}_qr.session"
        os.makedirs(PENDING_SESSIONS_DIR, exist_ok=True)
        session_path = os.path.join(PENDING_SESSIONS_DIR, session_name)

        task_key = f"{user_id}_qr"
        async with AccountManager._get_session_lock(task_key):
            if any(os.path.exists(path) for path in session_related_paths(session_path)):
                remove_result = AccountManager._remove_session_files_checked(session_path)
                if not remove_result.ok:
                    raise RuntimeError(t(
                        DataManager.get_user_language(user_id),
                        "account.qr_session_releasing",
                    ))

            client = TelegramClient(
                session_path,
                API_ID,
                API_HASH,
                auto_reconnect=True,
                **SESSION_CLIENT_KWARGS,
            )

        client._pending_session_path = session_path
        client._pending_display_phone = "QR login"

        return client

    @staticmethod
    async def _process_login_message(
        client: TelegramClient,
        message,
        phone: str,
        user_id: int,
        source: str = "live",
    ) -> str:
        normalized_phone = AccountManager.normalize_phone(phone)
        async with AccountManager._get_account_operation_lock(user_id, normalized_phone):
            return await AccountManager._process_login_message_unlocked(
                client, message, normalized_phone, user_id, source
            )

    @staticmethod
    async def _process_login_message_unlocked(
        client: TelegramClient,
        message,
        phone: str,
        user_id: int,
        source: str = "live",
    ) -> str:
        """Process one 777000 message exactly once across live and backfill paths."""
        normalized_phone = AccountManager.normalize_phone(phone)
        message_id = getattr(message, "id", None)
        lock = AccountManager._get_login_message_lock(user_id, normalized_phone)

        async with lock:
            accounts = AccountManager.get_user_accounts(user_id)
            acc_info = accounts.get(normalized_phone)
            if not acc_info or acc_info.get("client") is not client:
                return "stale"
            if AccountManager._is_login_message_processed(
                user_id, normalized_phone, message_id
            ):
                return "duplicate"

            try:
                text = getattr(message, "text", None) or ""
                sign_in_codes = (
                    extract_sign_in_codes(text)
                    if getattr(message, "media", None) is None
                    else []
                )
                code = sign_in_codes[0] if sign_in_codes else "未知"
                masked_code = f"{code[:-1]}*" if sign_in_codes else code
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if sign_in_codes:
                    logger.debug(
                        f"检测到 777000 登录码: {normalized_phone}, "
                        f"验证码={code}, 来源={source}"
                    )

                bot = account_runtime.get_notify_bot()
                waiters = {
                    uid
                    for uid in (code_waiters.get(normalized_phone) or set())
                    if uid == user_id
                }
                if bot and waiters and sign_in_codes:
                    for uid in list(waiters):
                        try:
                            await AccountManager._safe_send_bot_message(
                                bot,
                                uid,
                                t(DataManager.get_user_language(uid), "notify.login_code",
                                  code=code, phone=normalized_phone, time=now),
                                context=f"code_fetch:{normalized_phone}",
                            )
                        except account_runtime.NotifyBotFatalError:
                            raise
                        except Exception:
                            logger.exception(
                                f"推送验证码失败: 用户ID={uid}, 手机号={normalized_phone}"
                            )

                if not sign_in_codes or not AccountManager.is_anti_login_active(acc_info):
                    return "skipped"

                invalidated = await AccountManager._invalidate_sign_in_codes(
                    client, sign_in_codes, normalized_phone
                )

                bot = account_runtime.get_notify_bot()
                if not bot:
                    logger.info(f"未绑定通知机器人，跳过异常登录提醒: {normalized_phone}")
                    return "success" if invalidated else "failed"

                if invalidated:
                    alert_text = t(DataManager.get_user_language(user_id), "notify.login_blocked",
                                   code=masked_code, phone=normalized_phone, time=now)
                    context = f"login_handler:{normalized_phone}"
                    result = "success"
                else:
                    alert_text = t(DataManager.get_user_language(user_id), "notify.login_block_failed",
                                   code=masked_code, phone=normalized_phone, time=now)
                    context = f"login_handler_failed:{normalized_phone}"
                    result = "failed"

                if await AccountManager._safe_send_bot_message(
                    bot, user_id, alert_text, context=context
                ):
                    logger.debug(f"📨 已通过机器人通知用户 user_id={user_id}")
                return result
            finally:
                AccountManager._mark_login_message_processed(
                    user_id, normalized_phone, message_id
                )

    @staticmethod
    async def _backfill_login_messages(
        client: TelegramClient,
        phone: str,
        user_id: int,
        source: str,
    ) -> int:
        """Fetch and process recent 777000 messages after startup or reconnect."""
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=LOGIN_CODE_BACKFILL_WINDOW_SECONDS
        )
        recent_messages = []
        try:
            async for message in client.iter_messages(777000, limit=None):
                message_date = getattr(message, "date", None)
                if message_date is None:
                    continue
                if message_date.tzinfo is None:
                    message_date = message_date.replace(tzinfo=timezone.utc)
                if message_date < cutoff:
                    break
                recent_messages.append(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                f"补查 777000 最近消息失败: 手机号={phone}, 来源={source}, 错误={error}"
            )
            return 0

        processed = 0
        for message in reversed(recent_messages):
            outcome = await AccountManager._process_login_message(
                client, message, phone, user_id, source=source
            )
            if outcome not in {"duplicate", "stale"}:
                processed += 1

        if processed:
            logger.debug(
                f"已补查 777000 最近消息: 手机号={phone}, "
                f"来源={source}, 处理={processed}"
            )
        return processed

    @staticmethod
    def _install_reconnect_backfill(
        client: TelegramClient, phone: str, user_id: int
    ) -> bool:
        """Extend Telethon's reconnect callback with a recent-message backfill."""
        if getattr(client, "_login_reconnect_backfill_installed", False):
            return True
        sender = getattr(client, "_sender", None)
        original_callback = getattr(sender, "_auto_reconnect_callback", None)
        if sender is None or not callable(original_callback):
            logger.warning(f"无法安装重连补查回调: {phone}")
            return False

        async def reconnect_callback():
            await original_callback()
            try:
                await AccountManager._backfill_login_messages(
                    client, phone, user_id, source="reconnect"
                )
            except Exception:
                logger.exception(f"重连后补查 777000 消息失败: {phone}")

        sender._auto_reconnect_callback = reconnect_callback
        client._login_reconnect_backfill_installed = True
        return True

    @staticmethod
    def _authorization_update_details(update) -> Dict[str, str]:
        detected = getattr(update, "date", None)
        if isinstance(detected, datetime):
            if detected.tzinfo is not None:
                detected = detected.astimezone()
            detected_at = detected.strftime("%Y-%m-%d %H:%M:%S")
        else:
            detected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return {
            "device_name": str(getattr(update, "device", "") or "").strip(),
            "location": str(getattr(update, "location", "") or "").strip(),
            "detected_at": detected_at,
        }

    @staticmethod
    def _authorization_details(authorization) -> Dict[str, str]:
        detected = getattr(authorization, "date_active", None)
        if isinstance(detected, datetime):
            if detected.tzinfo is not None:
                detected = detected.astimezone()
            detected_at = detected.strftime("%Y-%m-%d %H:%M:%S")
        else:
            detected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        device = (
            getattr(authorization, "device_model", None)
            or getattr(authorization, "app_name", None)
            or getattr(authorization, "platform", None)
            or ""
        )
        location = ", ".join(
            part
            for part in (
                str(getattr(authorization, "region", "") or "").strip(),
                str(getattr(authorization, "country", "") or "").strip(),
            )
            if part
        )
        return {
            "device_name": str(device).strip(),
            "location": location,
            "detected_at": detected_at,
        }

    @staticmethod
    def _new_device_message(user_id: int, phone: str, details: Dict, suffix: str) -> str:
        language = DataManager.get_user_language(user_id)
        detected_at = details.get("detected_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return t(language, "device.prompt",
                 phone=AccountManager.format_phone_display(phone),
                 device=details.get('device_name') or t(language, "device.unknown"),
                 location=details.get('location') or t(language, "device.location_unknown"),
                 time=detected_at, suffix=suffix)

    @staticmethod
    async def _send_new_device_prompt(
        user_id: int, phone: str, auth_hash: str, details: Dict
    ):
        bot = account_runtime.get_notify_bot()
        if not bot:
            logger.info(f"未绑定通知机器人，稍后重试新设备提醒: {phone}")
            return None
        digits = AccountManager._digits_only(phone)
        language = DataManager.get_user_language(user_id)
        buttons = [[
            Button.inline(t(language, "device.allow"), f"nda:a:{digits}:{auth_hash}".encode("ascii")),
            Button.inline(t(language, "device.deny"), f"nda:r:{digits}:{auth_hash}".encode("ascii")),
        ]]
        text = AccountManager._new_device_message(
            user_id, phone, details, t(language, "device.choose")
        )
        for attempt in range(NEW_DEVICE_ACTION_ATTEMPTS):
            message = await AccountManager._safe_send_bot_message(
                bot,
                user_id,
                text,
                context=f"new_device_prompt:{phone}:{auth_hash}",
                buttons=buttons,
                return_message=True,
            )
            if message:
                return message
            if attempt < NEW_DEVICE_ACTION_ATTEMPTS - 1:
                await asyncio.sleep(NEW_DEVICE_ACTION_RETRY_DELAYS[attempt])
        return None

    @staticmethod
    async def _send_new_device_notice(
        user_id: int, phone: str, details: Dict, suffix: str, context: str
    ) -> bool:
        bot = account_runtime.get_notify_bot()
        if not bot:
            logger.info(f"未绑定通知机器人，跳过新设备通知: {phone}")
            return False
        text = AccountManager._new_device_message(user_id, phone, details, suffix)
        for attempt in range(NEW_DEVICE_ACTION_ATTEMPTS):
            sent = await AccountManager._safe_send_bot_message(
                bot, user_id, text, context=context
            )
            if sent:
                return True
            if attempt < NEW_DEVICE_ACTION_ATTEMPTS - 1:
                await asyncio.sleep(NEW_DEVICE_ACTION_RETRY_DELAYS[attempt])
        return False

    @staticmethod
    async def _apply_new_authorization_action(
        client: TelegramClient, auth_hash: int, allow: bool
    ) -> str:
        """Confirm or revoke one authorization with bounded immediate retries."""
        last_error = None
        for attempt in range(NEW_DEVICE_ACTION_ATTEMPTS):
            try:
                if allow:
                    await client(functions.account.ChangeAuthorizationSettingsRequest(
                        hash=int(auth_hash), confirmed=True
                    ))
                else:
                    await client(functions.account.ResetAuthorizationRequest(
                        hash=int(auth_hash)
                    ))
                return "applied"
            except asyncio.CancelledError:
                raise
            except HashInvalidError:
                return "missing"
            except FrozenMethodInvalidError:
                return "frozen"
            except account_runtime.NOTIFY_BOT_FATAL_ERRORS:
                raise
            except FloodWaitError as error:
                last_error = error
                delay = max(1, int(error.seconds))
            except Exception as error:
                last_error = error
                if attempt >= NEW_DEVICE_ACTION_ATTEMPTS - 1:
                    break
                delay = NEW_DEVICE_ACTION_RETRY_DELAYS[attempt]
            if attempt < NEW_DEVICE_ACTION_ATTEMPTS - 1:
                await asyncio.sleep(delay)
        raise last_error or RuntimeError("authorization action failed")

    @staticmethod
    async def _process_new_authorization_update(
        client: TelegramClient, update, phone: str, user_id: int
    ) -> str:
        normalized_phone = AccountManager.normalize_phone(phone)
        async with AccountManager._get_account_operation_lock(user_id, normalized_phone):
            return await AccountManager._process_new_authorization_update_unlocked(
                client, update, normalized_phone, user_id
            )

    @staticmethod
    async def _process_new_authorization_update_unlocked(
        client: TelegramClient, update, phone: str, user_id: int
    ) -> str:
        """Apply new-device policy directly from UpdateNewAuthorization."""
        normalized_phone = AccountManager.normalize_phone(phone)
        lock = AccountManager._get_authorization_lock(user_id, normalized_phone)
        async with lock:
            acc_info = user_accounts.get(user_id, {}).get(normalized_phone)
            if not acc_info or acc_info.get("client") is not client:
                return "stale"
            if not AccountManager.is_account_online(acc_info):
                return "offline"

            auth_hash = str(update.hash)
            state = AccountManager.get_hosted_authorization_state(user_id, normalized_phone)
            known_hashes = set(state["known_hashes"])
            pending = dict(state["pending"])
            if getattr(update, "unconfirmed", None) is not True:
                # Telegram only asks clients to present an allow/reject prompt for
                # unconfirmed authorizations. Confirmation updates omit the
                # date/device/location fields and must not be treated as a new login.
                known_hashes.add(auth_hash)
                pending.pop(auth_hash, None)
                AccountManager.save_hosted_authorization_state(
                    user_id, normalized_phone, known_hashes, pending, initialized=True
                )
                logger.debug(
                    "忽略已确认的设备授权状态更新: 手机号=%s, hash=%s",
                    normalized_phone,
                    auth_hash,
                )
                return "confirmed"
            if auth_hash in known_hashes or auth_hash in pending:
                return "duplicate"

            details = AccountManager._authorization_update_details(update)
            mode = AccountManager.get_account_mode(acc_info)
            protection_active = bool(acc_info.get("anti_login", True)) and mode == "normal"

            if protection_active:
                outcome = await AccountManager._apply_new_authorization_action(
                    client, update.hash, allow=False
                )
                if outcome == "frozen":
                    return "frozen"
                known_hashes.add(auth_hash)
                AccountManager.save_hosted_authorization_state(
                    user_id, normalized_phone, known_hashes, pending, initialized=True
                )
                suffix = (
                    t(DataManager.get_user_language(user_id), "device.auto_denied")
                    if outcome == "applied"
                    else t(
                        DataManager.get_user_language(user_id),
                        "device.already_expired",
                    )
                )
                await AccountManager._send_new_device_notice(
                    user_id, normalized_phone, details, suffix,
                    context=f"new_device_auto_reject:{normalized_phone}:{auth_hash}",
                )
                return "rejected"

            pending[auth_hash] = {
                **details,
                "first_seen_at": time.time(),
                "message_id": None,
            }
            AccountManager.save_hosted_authorization_state(
                user_id, normalized_phone, known_hashes, pending, initialized=True
            )
            message = await AccountManager._send_new_device_prompt(
                user_id, normalized_phone, auth_hash, details
            )
            if message:
                pending[auth_hash]["message_id"] = getattr(message, "id", None)
                AccountManager.save_hosted_authorization_state(
                    user_id, normalized_phone, known_hashes, pending, initialized=True
                )
            return "pending"

    @staticmethod
    async def resolve_new_authorization(
        user_id: int, phone: str, auth_hash: str, allow: bool
    ) -> Dict:
        normalized_phone = AccountManager.normalize_phone(phone)
        async with AccountManager._get_account_operation_lock(user_id, normalized_phone):
            return await AccountManager._resolve_new_authorization_unlocked(
                user_id, normalized_phone, auth_hash, allow
            )

    @staticmethod
    async def _resolve_new_authorization_unlocked(
        user_id: int, phone: str, auth_hash: str, allow: bool
    ) -> Dict:
        """Resolve one pending new-device prompt idempotently."""
        normalized_phone = AccountManager.normalize_phone(phone)
        lock = AccountManager._get_authorization_lock(user_id, normalized_phone)
        async with lock:
            acc_info = user_accounts.get(user_id, {}).get(normalized_phone)
            if not acc_info:
                return {
                    "ok": False,
                    "resolved": False,
                    "message": t(
                        DataManager.get_user_language(user_id),
                        "protection.device_missing",
                    ),
                }
            client = acc_info.get("client")
            if not client or not AccountManager.is_account_online(acc_info):
                return {
                    "ok": False,
                    "resolved": False,
                    "message": t(
                        DataManager.get_user_language(user_id),
                        "hosting.offline",
                    ),
                }

            state = AccountManager.get_hosted_authorization_state(user_id, normalized_phone)
            known_hashes = set(state["known_hashes"])
            pending = dict(state["pending"])
            details = pending.get(str(auth_hash))
            if not details:
                return {
                    "ok": True,
                    "resolved": True,
                    "message": t(
                        DataManager.get_user_language(user_id),
                        "device.expired",
                    ),
                }

            outcome = await AccountManager._apply_new_authorization_action(
                client, int(auth_hash), allow=allow
            )
            if outcome == "frozen":
                return {
                    "ok": False,
                    "resolved": False,
                    "message": t(
                        DataManager.get_user_language(user_id),
                        "device.frozen",
                    ),
                }
            if outcome == "missing":
                result_message = t(DataManager.get_user_language(user_id), "device.expired")
            elif allow:
                known_hashes.add(str(auth_hash))
                result_message = t(DataManager.get_user_language(user_id), "device.allowed")
            else:
                result_message = t(DataManager.get_user_language(user_id), "device.denied")
            pending.pop(str(auth_hash), None)
            AccountManager.save_hosted_authorization_state(
                user_id, normalized_phone, known_hashes, pending, initialized=True
            )
            return {"ok": True, "resolved": True, "message": result_message}

    @staticmethod
    def _install_new_authorization_handler(
        client: TelegramClient, phone: str, user_id: int
    ) -> bool:
        if getattr(client, "_new_authorization_handler_registered", False):
            return True

        @client.on(events.Raw(types=types.UpdateNewAuthorization))
        async def new_authorization_handler(update):
            try:
                await AccountManager._process_new_authorization_update(
                    client, update, phone, user_id
                )
            except account_runtime.NotifyBotFatalError:
                raise
            except account_runtime.NOTIFY_BOT_FATAL_ERRORS as error:
                reason = AccountManager._invalid_session_reason_from_error(error) or "unauthorized"
                await AccountManager.cleanup_invalid_hosted_session(
                    user_id,
                    phone,
                    client=client,
                    reason=reason,
                    source="new_authorization_event",
                )
            except Exception:
                logger.exception(f"处理新设备授权事件失败: {phone}")

        client._new_authorization_handler_registered = True
        return True
    
    @staticmethod
    async def setup_monitoring(
        client: TelegramClient,
        phone: str,
        user_id: int,
        backfill_recent: bool = True,
    ):
        """注册实时监控，并按需补查接入前的最近消息。"""
        try:
            AccountManager._install_new_authorization_handler(client, phone, user_id)
            if hasattr(client, '_login_handler_registered') and client._login_handler_registered:
                if LOG_MONITOR_CONFIRMATIONS:
                    now = time.time()
                    last_logged_at = getattr(client, "_login_handler_confirm_logged_at", 0)
                    if now - last_logged_at >= MONITOR_CONFIRM_LOG_INTERVAL_SECONDS:
                        logger.debug(f"监控处理器已注册: {phone}")
                        client._login_handler_confirm_logged_at = now
                return True

            @client.on(events.NewMessage(from_users=[777000]))
            async def login_handler(event):
                """处理 777000 登录通知并告警"""
                try:
                    await AccountManager._process_login_message(
                        client, event.message, phone, user_id, source="live"
                    )
                except account_runtime.NotifyBotFatalError:
                    raise
                except Exception:
                    logger.exception(f"处理登录通知异常 {phone}")


            # 标记为已注册
            client._login_handler_registered = True
            AccountManager._install_reconnect_backfill(client, phone, user_id)
            if backfill_recent:
                await AccountManager._backfill_login_messages(
                    client, phone, user_id, source="startup"
                )
            logger.info(f"✅ 已设置登录监控: {phone}")
            return True
            
        except Exception as e:
            logger.error(f"设置监控失败 {phone}: {str(e)}")
            return False

    @staticmethod
    async def watch_client_connection(client: TelegramClient, phone: str, user_id: int):
        """Use Telethon's native lifecycle to wait for a final disconnection."""
        reason = "disconnected"
        invalid_reason = None
        recoverable_runtime_error = False
        try:
            await client.run_until_disconnected()
        except asyncio.CancelledError:
            logger.debug(f"连接监听任务已取消: 手机号={phone}")
            raise
        except Exception as error:
            reason = type(error).__name__ or "connection_error"
            invalid_reason = AccountManager._invalid_session_reason_from_error(error)
            recoverable_runtime_error = (
                AccountManager._is_recoverable_session_runtime_error(error)
            )
            if not invalid_reason:
                logger.warning(
                    f"Telethon 连接已终止: 手机号={phone}, "
                    f"错误类型={reason}, 错误={str(error)[:120]}"
                )
        else:
            logger.warning(f"Telethon 连接已终止: 手机号={phone}")

        if invalid_reason:
            logger.debug(
                f"Telethon 已确认托管 Session 失效: 手机号={phone}, "
                f"错误类型={reason}, 处理原因={invalid_reason}"
            )
            await AccountManager.cleanup_invalid_hosted_session(
                user_id=user_id,
                phone=phone,
                client=client,
                reason=invalid_reason,
                source="connection_watcher",
            )
            return

        if recoverable_runtime_error:
            recovery = await AccountManager._recover_hosted_client_once(
                user_id, phone, client, reason
            )
            logger.info(
                "托管客户端单次恢复结束: 用户ID=%s, 手机号=%s, 结果=%s",
                user_id,
                AccountManager.normalize_phone(phone),
                recovery,
            )
            return

        transitioned = await AccountManager.mark_hosted_session_offline(
            user_id=user_id,
            phone=phone,
            client=client,
            reason=reason,
        )
        if transitioned:
            await AccountManager.notify_hosted_session_offline(
                user_id=user_id,
                phone=phone,
                reason=reason,
            )

    @staticmethod
    async def _recover_hosted_client_once(
        user_id: int,
        phone: str,
        failed_client: TelegramClient,
        reason: str,
        *,
        operation_locked: bool = False,
    ) -> str:
        """Rebuild one failed hosted client once without deleting or moving its Session."""
        normalized_phone = AccountManager.normalize_phone(phone)
        if not operation_locked:
            async with AccountManager._get_account_operation_lock(
                user_id, normalized_phone
            ):
                return await AccountManager._recover_hosted_client_once(
                    user_id,
                    normalized_phone,
                    failed_client,
                    reason,
                    operation_locked=True,
                )

        acc_info = user_accounts.get(user_id, {}).get(normalized_phone)
        if not acc_info or acc_info.get("client") is not failed_client:
            return "stale_client"
        session_path = acc_info.get("original_session_path")
        if not session_path and acc_info.get("session_file"):
            session_path = os.path.join(SESSIONS_DIR, acc_info["session_file"])
        if not session_path or not os.path.exists(session_path):
            result = "missing_session"
            transitioned = await AccountManager.mark_hosted_session_offline(
                user_id, normalized_phone, failed_client, result
            )
            if transitioned:
                await AccountManager.notify_hosted_session_offline(
                    user_id, normalized_phone, result
                )
            return result

        logger.warning(
            "检测到可恢复的托管会话异常，执行一次客户端重建: "
            "用户ID=%s, 手机号=%s, 原因=%s",
            user_id,
            normalized_phone,
            reason,
        )
        task_key = f"{user_id}_{normalized_phone}"
        watcher = client_tasks.get(task_key)
        if watcher is asyncio.current_task():
            client_tasks.pop(task_key, None)
        else:
            await AccountManager._cancel_client_task(task_key)

        if not await AccountManager._safe_disconnect_client(
            failed_client,
            f"hosted-recovery:{user_id}:{normalized_phone}",
            timeout=5,
        ):
            result = "disconnect_failed"
        else:
            _, _, success, failure_reason = await AccountManager.create_client_from_session(
                session_path,
                user_id,
                detailed=True,
                preserved_health_status=acc_info.get("health_status"),
                preserved_freeze_info=acc_info.get("freeze_info"),
                account_source=acc_info.get("source"),
            )
            if success:
                logger.info(
                    "托管客户端单次恢复成功: 用户ID=%s, 手机号=%s",
                    user_id,
                    normalized_phone,
                )
                return "recovered"
            result = "rebuild_failed"
            logger.warning(
                "托管客户端单次恢复失败: 用户ID=%s, 手机号=%s, 原因=%s",
                user_id,
                normalized_phone,
                failure_reason,
            )

        transitioned = await AccountManager.mark_hosted_session_offline(
            user_id, normalized_phone, failed_client, result
        )
        if transitioned:
            await AccountManager.notify_hosted_session_offline(
                user_id, normalized_phone, result
            )
        return result

    @staticmethod
    async def _recover_protocol_session_once(
        user_id: int,
        phone: str,
        failed_client: TelegramClient,
        reason: str,
    ) -> bool:
        """Backward-compatible boolean wrapper for the unified recovery path."""
        return (
            await AccountManager._recover_hosted_client_once(
                user_id, phone, failed_client, reason
            )
        ) == "recovered"

    @staticmethod
    async def authenticate(client: TelegramClient, phone: str, user_id: int):
        """认证流程"""
        language = DataManager.get_user_language(user_id)
        normalized_phone = AccountManager.normalize_phone(phone)
        display_phone = AccountManager.format_phone_display(phone)
        pending_session_path = AccountManager._client_session_path(client)
        try:
            await client.connect()
            
            if await client.is_user_authorized():
                await AccountManager.promote_pending_client(
                    client,
                    normalized_phone,
                    user_id,
                    display_phone=display_phone,
                    pending_session_path=pending_session_path,
                )
                
                return t(language, "auth.already_logged_in")
            
            try:
                # 使用标准化手机号发送验证码
                rate_limit = login_code_request_rate_limiter.acquire(user_id)
                if not rate_limit.allowed:
                    await AccountManager.cleanup_incomplete_account(
                        user_id, normalized_phone, client
                    )
                    return render_login_code_rate_limit(rate_limit, language)
                sent_code = await client.send_code_request(normalized_phone)
                logger.debug(f"验证码已发送到 {normalized_phone}, 类型: {sent_code.type}")

                # 只有 Telegram 接受验证码请求后，才进入等待验证码状态。
                user_states[user_id] = {
                    'auth_phone': normalized_phone,
                    'auth_client': client,
                    'display_phone': display_phone,
                    'pending_session_path': pending_session_path,
                    'waiting_code': True,
                    'auth_start_time': time.time(),
                    'code_attempts': 0,
                    'max_code_attempts': 5
                }
                
                return t(language, "auth.code_sent")
                
            except FloodWaitError as e:
                wait_time = e.seconds
                reminder_system = account_runtime.get_login_unlock_reminder_system()
                reminder_result = None
                if reminder_system:
                    reminder_result = await reminder_system.schedule(
                        user_id, normalized_phone, wait_time
                    )
                await AccountManager.cleanup_incomplete_account(user_id, normalized_phone, client)
                if reminder_result is not None:
                    return reminder_system.render_schedule_result(
                        reminder_result,
                        DataManager.get_user_language(user_id),
                        DataManager.get_user_timezone(user_id),
                    )
                return t(language, "auth.rate_limited", seconds=wait_time)
            except Exception as e:
                logger.error(f"发送验证码失败 {phone}: {str(e)}")
                await AccountManager.cleanup_incomplete_account(user_id, normalized_phone, client)
                return t(language, "auth.send_code_failed", error=str(e))
                
        except Exception as e:
            logger.error(f"认证过程异常 {phone}: {str(e)}")
            await AccountManager.cleanup_incomplete_account(user_id, normalized_phone, client)
            return t(language, "auth.failed", error=str(e))

    @staticmethod
    async def probe_login_unlock(client: TelegramClient, phone: str, user_id: int) -> str:
        """Probe Telegram's official login-code limit without continuing login."""
        normalized_phone = AccountManager.normalize_phone(phone)
        language = DataManager.get_user_language(user_id)
        reminder_system = account_runtime.get_login_unlock_reminder_system()
        if reminder_system is None:
            await AccountManager.cleanup_incomplete_account(
                user_id, normalized_phone, client
            )
            return t(language, "login_unlock.unavailable")

        try:
            await client.connect()
            if await client.is_user_authorized():
                if not await reminder_system.remove(user_id, normalized_phone):
                    return t(language, "login_unlock.manual_remove_failed")
                return t(
                    language,
                    "login_unlock.manual_available",
                    phone=AccountManager.format_phone_display(normalized_phone),
                )
            rate_limit = login_code_request_rate_limiter.acquire(user_id)
            if not rate_limit.allowed:
                return render_login_code_rate_limit(rate_limit, language)
            await client.send_code_request(normalized_phone)
            if not await reminder_system.remove(user_id, normalized_phone):
                return t(language, "login_unlock.manual_remove_failed")
            return t(
                language,
                "login_unlock.manual_available",
                phone=AccountManager.format_phone_display(normalized_phone),
            )
        except FloodWaitError as error:
            result = await reminder_system.schedule(
                user_id, normalized_phone, max(1, int(error.seconds))
            )
            return reminder_system.render_schedule_result(
                result, language, DataManager.get_user_timezone(user_id)
            )
        except Exception as error:
            logger.warning(
                "手动检测登录限制失败: 用户ID=%s, 手机号=%s, 错误类型=%s",
                user_id,
                normalized_phone,
                type(error).__name__,
            )
            return t(language, "login_unlock.manual_failed")
        finally:
            await AccountManager.cleanup_incomplete_account(
                user_id, normalized_phone, client
            )
    
    @staticmethod
    async def handle_code(user_id: int, code: str):
        """处理验证码"""
        language = DataManager.get_user_language(user_id)
        if user_id not in user_states or not user_states[user_id].get('waiting_code'):
            return t(language, "auth.code_request_expired")
        
        state = user_states[user_id]
        client = state['auth_client']
        phone = state['auth_phone']
        display_phone = state.get('display_phone', AccountManager.format_phone_display(phone))
        pending_session_path = state.get('pending_session_path') or AccountManager._client_session_path(client)
        
        state['code_attempts'] += 1
        if state['code_attempts'] > state['max_code_attempts']:
            # 清理半成品账号 + session 文件
            await AccountManager.cleanup_incomplete_account(user_id, phone, client)
            return t(language, "auth.code_attempts_exhausted")
        
        code = code.strip()
        if not code.isdigit() or len(code) < 4:
            return t(
                language,
                "auth.code_format_invalid",
                remaining=state['max_code_attempts'] - state['code_attempts'],
            )
        
        try:
            # 使用标准化手机号登录
            normalized_phone = AccountManager.normalize_phone(phone)
            await client.sign_in(normalized_phone, code)
            
            try:
                await AccountManager.promote_pending_client(
                    client,
                    normalized_phone,
                    user_id,
                    display_phone=display_phone,
                    pending_session_path=pending_session_path,
                )
            except Exception as promote_error:
                await AccountManager.cleanup_incomplete_account(user_id, normalized_phone, client)
                return t(language, "auth.hosting_init_failed", error=promote_error)

            user_states.pop(user_id, None)
            
            return t(
                language,
                "auth.login_success",
                phone=AccountManager.format_phone_display(phone),
            )
            
        except SessionPasswordNeededError:
            state['waiting_code'] = False
            state['waiting_password'] = True
            state['code_attempts'] = 0
            state['password_attempts'] = 0
            state['max_password_attempts'] = 5
            return t(language, "auth.password_required")
            
        except PhoneCodeInvalidError:
            remaining = state['max_code_attempts'] - state['code_attempts']
            if remaining > 0:
                return t(language, "auth.code_invalid", remaining=remaining)
            else:
                # 清理半成品账号 + session 文件
                await AccountManager.cleanup_incomplete_account(user_id, phone, client)
                return t(language, "auth.code_attempts_exhausted")
                
        except PhoneCodeExpiredError:
            # 清理半成品账号 + session 文件
            await AccountManager.cleanup_incomplete_account(user_id, phone, client)
            return t(language, "auth.code_expired")
            
        except PhoneCodeEmptyError:
            return t(language, "auth.code_empty")
            
        except FloodWaitError as e:
            wait_time = e.seconds
            return t(language, "auth.operation_rate_limited", seconds=wait_time)
            
        except Exception as e:
            logger.error(f"验证码处理异常 {phone}: {str(e)}")
            remaining = state['max_code_attempts'] - state['code_attempts']
            if remaining > 0:
                return t(
                    language,
                    "auth.login_failed_retry",
                    error=str(e),
                    remaining=remaining,
                )
            else:
                await AccountManager.cleanup_incomplete_account(user_id, phone, client)
                return t(language, "auth.login_failed_exhausted", error=str(e))
    
    @staticmethod
    async def handle_password(user_id: int, password: str):
        """处理密码"""
        language = DataManager.get_user_language(user_id)
        if user_id not in user_states or not user_states[user_id].get('waiting_password'):
            return t(language, "auth.password_request_expired")
        
        state = user_states[user_id]
        client = state['auth_client']
        phone = state.get('auth_phone') or ""
        display_phone = state.get('display_phone') or (AccountManager.format_phone_display(phone) if phone else "")
        pending_session_path = state.get('pending_session_path') or AccountManager._client_session_path(client)
        
        password = password.strip()
        if not password:
            return t(language, "auth.password_empty")
        
        if not state.get("password_verified"):
            try:
                await client.sign_in(password=password)
                state["password_verified"] = True
            except Exception as e:
                logger.debug(f"二级密码验证失败: 手机号={phone or '未知'}, 错误={e}")
                state["password_attempts"] = state.get("password_attempts", 0) + 1
                max_attempts = state.get("max_password_attempts", 5)
                remaining = max_attempts - state["password_attempts"]
                if remaining <= 0:
                    await AccountManager.cleanup_incomplete_account(user_id, phone, client)
                    return t(language, "auth.password_attempts_exhausted")
                return t(language, "auth.password_invalid", remaining=remaining)

        try:
            if not phone:
                # QR 登录在等待二级密码前通常拿不到手机号，密码通过后再从授权会话读取。
                me = await client.get_me()
                raw_phone = getattr(me, "phone", None)
                if not raw_phone:
                    await AccountManager.cleanup_incomplete_account(user_id, phone, client)
                    return t(language, "auth.phone_unavailable")
                phone = f"+{raw_phone}"
                state["auth_phone"] = phone
                display_phone = AccountManager.format_phone_display(phone)

            normalized_phone = AccountManager.normalize_phone(phone)
            if state.get("qr_login"):
                # QR 路径没有手机号输入阶段，密码通过后仍要复用添加账号的查重保护。
                existing = await AccountManager.check_existing_account_for_add(user_id, normalized_phone)
                if existing.action == "block":
                    await AccountManager.cleanup_incomplete_account(user_id, normalized_phone, client)
                    return existing.message

            try:
                await AccountManager.promote_pending_client(
                    client,
                    normalized_phone,
                    user_id,
                    display_phone=display_phone,
                    pending_session_path=pending_session_path,
                )
            except Exception as promote_error:
                await AccountManager.cleanup_incomplete_account(user_id, normalized_phone, client)
                return t(language, "auth.hosting_init_failed", error=promote_error)

            user_states.pop(user_id, None)
            
            return t(
                language,
                "auth.login_success",
                phone=AccountManager.format_phone_display(phone),
            )
            
        except Exception as e:
            logger.exception(f"二级密码验证通过后初始化失败: 手机号={phone or '未知'}")
            return t(language, "auth.password_init_failed", error=str(e))


    @staticmethod
    async def load_all_sessions():
        """加载所有session文件（并发 + 限流，加快启动）"""
        if not os.path.exists(SESSIONS_DIR):
            os.makedirs(SESSIONS_DIR)
            return

        session_files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.session') and 'backup' not in f]
        concurrency = getattr(config, "SESSION_LOAD_CONCURRENCY", 6)
        logger.info(f"找到 {len(session_files)} 个会话文件，开始加载（并发={concurrency}）...")

        sem = asyncio.Semaphore(concurrency)

        async def _load_one(session_file: str):
            async with sem:
                try:
                    if '_' not in session_file:
                        return

                    user_id_str = session_file.split('_')[0]
                    try:
                        user_id = int(user_id_str)
                    except ValueError:
                        return

                    session_path = os.path.join(SESSIONS_DIR, session_file)
                    phone_hint = "+" + session_file.split('_', 1)[1].split('.', 1)[0]

                    if not AccountManager.check_access(user_id):
                        status = await AccountManager.check_inaccessible_session_file(session_path, phone_hint)
                        if AccountManager._is_fatal_session_status(status) or status == "invalid":
                            logger.warning(
                                f"用户 {user_id} 无权限且会话无效，已清理: {session_file}, 状态={status}"
                            )
                            await AccountManager.backup_session_file(session_path, status or "invalid")
                        else:
                            logger.info(
                                f"用户 {user_id} 无权限，会话状态={status}，跳过加载: {session_file}"
                            )
                        return

                    if not AccountManager.is_account_selected(user_id, phone_hint):
                        logger.info("用户 %s 的账户 %s 因订阅配额选择保持暂停", user_id, phone_hint)
                        return

                    client, phone, success, failure_reason = await AccountManager.create_client_from_session(
                        session_path, user_id, detailed=True
                    )

                    if success:
                        logger.info(f"✅ 加载成功: {phone}")
                    else:
                        logger.warning(f"❌ 会话加载失败: {session_file}, 原因={failure_reason}")
                        if AccountManager._is_fatal_session_status(failure_reason):
                            await AccountManager.notify_session_unavailable(user_id, phone_hint, source="startup")
                            await AccountManager.backup_session_file(session_path, "invalid")
                        else:
                            await AccountManager.mark_hosted_session_offline(
                                user_id,
                                phone_hint,
                                None,
                                reason=failure_reason or "startup_error",
                            )

                except Exception as e:
                    logger.error(f"加载失败 {session_file}: {str(e)}")

        tasks = [asyncio.create_task(_load_one(f)) for f in session_files]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    @staticmethod
    async def reload_user_accounts_detail(user_id: int, source: str = "manual_reload") -> Dict[str, int]:
        """重新加载指定用户下的所有账户（并发 + 限流）"""
        logger.info(f"开始重新加载用户 {user_id} 的账户...")

        if user_id not in user_accounts:
            logger.info(f"用户 {user_id} 无账户可加载")
            return {"total": 0, "success": 0, "failed": 0}

        # 并发数：复用你现有配置名，或单独加一个 RELOAD_CONCURRENCY
        concurrency = getattr(config, "SESSION_RELOAD_CONCURRENCY",
                              getattr(config, "SESSION_LOAD_CONCURRENCY", 6))
        sem = asyncio.Semaphore(concurrency)
        accounts_lock = asyncio.Lock()

        # 先拍一份快照，避免遍历过程中被并发任务修改字典导致 RuntimeError
        items = list(user_accounts[user_id].items())
        total_count = len(items)

        logger.debug(f"当前用户 {user_id} 共有 {total_count} 个账户，开始重载（并发={concurrency}）...")

        success_count = 0
        fail_count = 0
        alive_count = 0
        frozen_count = 0
        dead_count = 0
        details = []
        fail_reasons: Dict[str, int] = {}

        async def _reload_one(phone: str, acc_info: dict):
            nonlocal success_count, fail_count, alive_count, frozen_count, dead_count

            async with AsyncExitStack() as stack:
                await stack.enter_async_context(sem)
                await stack.enter_async_context(
                    AccountManager._get_account_operation_lock(user_id, phone)
                )
                display_phone = acc_info.get('display_phone', phone)
                try:
                    session_file = acc_info['session_file']
                    session_path = os.path.join(SESSIONS_DIR, session_file)
                    anti_login_setting = acc_info.get('anti_login')
                    preserved_health_status = acc_info.get('health_status') or 'alive'
                    preserved_freeze_info = acc_info.get('freeze_info')
                    check_freeze = source == "manual_reload"

                    logger.debug(f"重新加载账户: {display_phone}")

                    task_key = f"{user_id}_{phone}"
                    async with AccountManager._get_session_lock(task_key):
                        # 取消旧任务并等待退出，确保旧 client 不再触碰 session 文件
                        await AccountManager._cancel_client_task(task_key)

                        old_client = acc_info.get('client')
                        disconnected = True
                        if old_client:
                            disconnected = await AccountManager._safe_disconnect_client(
                                old_client,
                                f"reload-old:{user_id}:{phone}",
                                timeout=10,
                            )

                        if not disconnected:
                            new_client, new_phone, success, failure_reason = (
                                None,
                                phone,
                                False,
                                "session_busy",
                            )
                        else:
                            new_client, new_phone, success, failure_reason = await AccountManager.create_client_from_session(
                                session_path,
                                user_id,
                                detailed=True,
                                check_freeze=check_freeze,
                                preserved_health_status=preserved_health_status,
                                preserved_freeze_info=preserved_freeze_info,
                            )

                    if success:
                        # 更新共享结构（加锁）
                        async with accounts_lock:
                            # 可能已经被其它任务删掉了，先检查
                            if user_id in user_accounts and phone in user_accounts[user_id]:
                                user_accounts[user_id][phone]['anti_login'] = anti_login_setting
                                user_accounts[user_id][phone]['last_reload'] = time.time()
                                user_accounts[user_id][phone]['display_phone'] = display_phone
                                user_accounts[user_id][phone]['runtime_status'] = 'online'
                                user_accounts[user_id][phone]['offline_reason'] = None
                                user_accounts[user_id][phone]['offline_at'] = None
                                user_accounts[user_id][phone]['health_status'] = getattr(new_client, "_last_health_status", "alive") or "alive"
                                user_accounts[user_id][phone]['freeze_info'] = getattr(new_client, "_last_freeze_info", None)
                                # 如果 create_client_from_session 返回了新的 client/phone，你也可以按需同步：
                                user_accounts[user_id][phone]['client'] = new_client
                                # user_accounts[user_id][phone]['phone'] = new_phone  # 看你结构是否需要

                        success_count += 1
                        health_status = getattr(new_client, "_last_health_status", "alive") or "alive"
                        if health_status == "frozen":
                            frozen_count += 1
                        else:
                            alive_count += 1
                        details.append({"phone": display_phone, "status": "success", "reason": health_status})
                        logger.debug(f"✅ 重载成功: {display_phone}")
                    else:
                        fail_count += 1
                        dead_count += 1
                        fail_reasons[failure_reason] = fail_reasons.get(failure_reason, 0) + 1
                        details.append({"phone": display_phone, "status": "failed", "reason": failure_reason})
                        logger.warning(f"❌ 重载失败: {display_phone}, 原因={failure_reason}")

                        if AccountManager._is_fatal_session_status(failure_reason):
                            await AccountManager.notify_session_unavailable(user_id, display_phone, source=source)
                            if os.path.exists(session_path):
                                await AccountManager.backup_session_file(session_path, "invalid")

                            # 从共享结构删除（加锁）
                            async with accounts_lock:
                                if user_id in user_accounts and phone in user_accounts[user_id]:
                                    del user_accounts[user_id][phone]
                                    if not user_accounts[user_id]:
                                        del user_accounts[user_id]
                        else:
                            # 临时错误（网络/API/连接问题）不删除正常账户，恢复旧 client 的连接监听任务。
                            try:
                                old_client = acc_info.get('client')
                                if old_client and old_client.is_connected():
                                    await AccountManager.setup_monitoring(old_client, phone, user_id)
                                    AccountManager._start_connection_watcher_task(user_id, phone, old_client)
                                else:
                                    await AccountManager.mark_hosted_session_offline(
                                        user_id,
                                        phone,
                                        old_client,
                                        failure_reason or "reload_failed",
                                    )
                            except Exception as restore_error:
                                logger.warning(f"恢复旧账户连接监听失败 {display_phone}: {restore_error}")

                except Exception as e:
                    fail_count += 1
                    dead_count += 1
                    fail_reasons["exception"] = fail_reasons.get("exception", 0) + 1
                    details.append({"phone": display_phone, "status": "failed", "reason": "exception"})
                    logger.error(f"重载账户异常 {display_phone}: {str(e)}")

        tasks = [asyncio.create_task(_reload_one(phone, acc_info)) for phone, acc_info in items]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        logger.info(f"✅ 重载完成: 成功 {success_count}, 失败 {fail_count}, 总计 {total_count}")
        return {
            "total": total_count,
            "success": success_count,
            "failed": fail_count,
            "alive_count": alive_count,
            "frozen_count": frozen_count,
            "dead_count": dead_count,
            "details": details,
            "fail_reasons": fail_reasons,
        }

    @staticmethod
    async def disable_account(user_id: int, phone: str):
        """禁用账户"""
        if user_id in user_accounts and phone in user_accounts[user_id]:
            client = user_accounts[user_id][phone]['client']
            task_key = f"{user_id}_{phone}"
            async with AccountManager._get_session_lock(task_key):
                await AccountManager._cancel_client_task(task_key)
                await AccountManager._cancel_account_auxiliary_tasks(user_id, phone)
                await AccountManager._safe_disconnect_client(
                    client,
                    f"disable-account:{user_id}:{phone}",
                    timeout=10,
                )

                session_file = user_accounts[user_id][phone]['session_file']
                session_path = os.path.join(SESSIONS_DIR, session_file)

                if os.path.exists(session_path):
                    await AccountManager.backup_session_file(session_path, "disabled")

                del user_accounts[user_id][phone]
                if not user_accounts[user_id]:
                    del user_accounts[user_id]
            
            return True
        return False
