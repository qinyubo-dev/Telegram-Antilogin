# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import os
from pathlib import Path

import config as _local_config


PROJECT_ROOT = Path(__file__).resolve().parent


def _configured(name: str, default):
    return getattr(_local_config, name, default)


def _positive_int(name: str, default: int) -> int:
    value = int(_configured(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _positive_float(name: str, default: float) -> float:
    value = float(_configured(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _resolve_root(value: object) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _runtime_path(name: str, default: str) -> str:
    path = Path(str(_configured(name, default))).expanduser()
    if not path.is_absolute():
        path = DATA_ROOT / path
    return str(path.resolve())


DATA_ROOT = _resolve_root(
    os.getenv(
        "ANTI_LOGIN_DATA_ROOT",
        _configured("DATA_ROOT", PROJECT_ROOT),
    )
)

# Built-in application defaults. Values explicitly defined in config.py remain
# supported as deployment overrides, including advanced legacy overrides.
API_ID = int(_configured("API_ID", 2040))
API_HASH = str(_configured("API_HASH", "b18441a1ff607e10a989891a5462e627"))
BOT_TOKEN = str(_configured("BOT_TOKEN", ""))
MERCHANT_ID = str(_configured("MERCHANT_ID", ""))
PAYMENT_TOKEN = str(_configured("PAYMENT_TOKEN", ""))
ADMIN_IDS = [int(user_id) for user_id in _configured("ADMIN_IDS", [])]

# Runtime paths. Explicit legacy config paths remain valid, but relative paths
# are now anchored to DATA_ROOT instead of the process working directory.
SESSIONS_DIR = _runtime_path("SESSIONS_DIR", "sessions")
DATA_FILE = _runtime_path("DATA_FILE", "user_data.json")
PAYMENT_ORDERS_FILE = _runtime_path(
    "PAYMENT_ORDERS_FILE", os.path.join("storage", "payment_orders.json")
)
ADMIN_AUDIT_FILE = _runtime_path(
    "ADMIN_AUDIT_FILE", os.path.join("storage", "admin_audit.jsonl")
)
LOGIN_MONITOR_STATE_FILE = _runtime_path(
    "LOGIN_MONITOR_STATE_FILE", os.path.join("storage", "login_monitor_state.json")
)
HOSTED_ACCOUNT_METADATA_FILE = _runtime_path(
    "HOSTED_ACCOUNT_METADATA_FILE",
    os.path.join("storage", "hosted_account_metadata.json"),
)
ACCOUNT_TRANSFER_JOURNAL_FILE = _runtime_path(
    "ACCOUNT_TRANSFER_JOURNAL_FILE",
    os.path.join("storage", "account_transfer_journal.json"),
)
USER_PROFILE_CACHE_FILE = _runtime_path(
    "USER_PROFILE_CACHE_FILE",
    os.path.join("storage", "user_profile_cache.json"),
)
BOT_LOG_DIR = _runtime_path("BOT_LOG_DIR", "logs")
BOT_SESSION_PATH = _runtime_path("BOT_SESSION_PATH", "bot")

# Logging.
TELETHON_SYNC_WARNING_WINDOW_SECONDS = _positive_int(
    "TELETHON_SYNC_WARNING_WINDOW_SECONDS", 1800
)
BOT_LOG_RETENTION_DAYS = _positive_int("BOT_LOG_RETENTION_DAYS", 30)

# OkayPay.
PAYMENT_RETURN_URL = str(
    _configured("PAYMENT_RETURN_URL", "https://t.me/QinShield_Bot")
)
PAYMENT_POLL_INTERVAL_SECONDS = _positive_float(
    "PAYMENT_POLL_INTERVAL_SECONDS", 5
)
PAYMENT_AUTO_CHECK_WINDOW_SECONDS = _positive_float(
    "PAYMENT_AUTO_CHECK_WINDOW_SECONDS", 300
)
PAYMENT_ORDER_EXPIRY_SECONDS = _positive_float(
    "PAYMENT_ORDER_EXPIRY_SECONDS", 2 * 60 * 60
)
PAYMENT_REQUEST_TIMEOUT_SECONDS = _positive_float(
    "PAYMENT_REQUEST_TIMEOUT_SECONDS", 30
)
PAYMENT_REQUEST_CONCURRENCY = _positive_int("PAYMENT_REQUEST_CONCURRENCY", 2)
PAYMENT_RETRY_BACKOFF_MAX_SECONDS = _positive_float(
    "PAYMENT_RETRY_BACKOFF_MAX_SECONDS", 60
)
PAYMENT_PROVIDER_FAILURE_THRESHOLD = _positive_int(
    "PAYMENT_PROVIDER_FAILURE_THRESHOLD", 3
)
PAYMENT_PROVIDER_COOLDOWN_SECONDS = _positive_float(
    "PAYMENT_PROVIDER_COOLDOWN_SECONDS", 30
)

# Hosted sessions.
SESSION_LOAD_CONCURRENCY = _positive_int("SESSION_LOAD_CONCURRENCY", 5)
SESSION_RELOAD_CONCURRENCY = _positive_int(
    "SESSION_RELOAD_CONCURRENCY", SESSION_LOAD_CONCURRENCY
)
SESSION_HEALTH_RETRY_ATTEMPTS = _positive_int(
    "SESSION_HEALTH_RETRY_ATTEMPTS", 2
)
SESSION_HEALTH_RETRY_DELAY_SECONDS = _positive_float(
    "SESSION_HEALTH_RETRY_DELAY_SECONDS", 3
)
SESSION_DEVICE_MODEL = str(_configured("SESSION_DEVICE_MODEL", "QinShield"))
SESSION_SYSTEM_VERSION = str(_configured("SESSION_SYSTEM_VERSION", "Debian GNU/Linux 12"))
SESSION_APP_VERSION = str(_configured("SESSION_APP_VERSION", "12.7"))
SESSION_LANG_CODE = str(_configured("SESSION_LANG_CODE", "en"))
SESSION_SYSTEM_LANG_CODE = str(_configured("SESSION_SYSTEM_LANG_CODE", "en"))

# Login monitoring and account operations.
LOGIN_CODE_BACKFILL_WINDOW_SECONDS = _positive_int(
    "LOGIN_CODE_BACKFILL_WINDOW_SECONDS", 5 * 60
)
LOGIN_CODE_INVALIDATE_ATTEMPTS = _positive_int(
    "LOGIN_CODE_INVALIDATE_ATTEMPTS", 3
)
LOGIN_CODE_INVALIDATE_RETRY_DELAYS = tuple(
    int(value) for value in _configured("LOGIN_CODE_INVALIDATE_RETRY_DELAYS", (1, 2))
)
LOGIN_CODE_DEDUP_RETENTION_SECONDS = _positive_int(
    "LOGIN_CODE_DEDUP_RETENTION_SECONDS", 10 * 60
)
HOSTING_CLEAN_TIMEOUT_SECONDS = _positive_float(
    "HOSTING_CLEAN_TIMEOUT_SECONDS", 120
)
HOSTING_CLEAN_MIN_AGE_SECONDS = _positive_float(
    "HOSTING_CLEAN_MIN_AGE_SECONDS", 60 * 60
)
ACCOUNT_TRANSFER_MIN_AGE_SECONDS = _positive_float(
    "ACCOUNT_TRANSFER_MIN_AGE_SECONDS", 24 * 60 * 60
)
HOSTING_OPERATION_MIN_AGE_SECONDS = _positive_float(
    "HOSTING_OPERATION_MIN_AGE_SECONDS", 24 * 60 * 60
)
TRANSFER_RECIPIENT_RESTRICTION_SECONDS = _positive_float(
    "TRANSFER_RECIPIENT_RESTRICTION_SECONDS", 60 * 60
)

# Login unlock reminders use a quota pool independent from hosted accounts.
_login_unlock_limits = _configured(
    "LOGIN_UNLOCK_MONITOR_LIMITS",
    {"go": 3, "plus": 15, "pro": None},
)
LOGIN_UNLOCK_MONITOR_LIMITS = {
    "go": int(_login_unlock_limits.get("go", 3)),
    "plus": int(_login_unlock_limits.get("plus", 15)),
    "pro": None,
}
if LOGIN_UNLOCK_MONITOR_LIMITS["go"] <= 0 or LOGIN_UNLOCK_MONITOR_LIMITS["plus"] <= 0:
    raise ValueError("LOGIN_UNLOCK_MONITOR_LIMITS values must be greater than zero")
LOGIN_UNLOCK_RETRY_SECONDS = _positive_int("LOGIN_UNLOCK_RETRY_SECONDS", 30)

# Phone login and manual unlock probes share one persisted per-user request
# budget because both call Telegram's login-code endpoint.
LOGIN_CODE_REQUEST_MIN_INTERVAL_SECONDS = _positive_int(
    "LOGIN_CODE_REQUEST_MIN_INTERVAL_SECONDS", 15
)
LOGIN_CODE_REQUEST_WINDOW_SECONDS = _positive_int(
    "LOGIN_CODE_REQUEST_WINDOW_SECONDS", 60 * 60
)
LOGIN_CODE_REQUEST_MAX_PER_WINDOW = _positive_int(
    "LOGIN_CODE_REQUEST_MAX_PER_WINDOW", 15
)
if LOGIN_CODE_REQUEST_MIN_INTERVAL_SECONDS > LOGIN_CODE_REQUEST_WINDOW_SECONDS:
    raise ValueError(
        "LOGIN_CODE_REQUEST_MIN_INTERVAL_SECONDS must not exceed "
        "LOGIN_CODE_REQUEST_WINDOW_SECONDS"
    )

ADMIN_AUDIT_RETENTION_DAYS = _positive_int(
    "ADMIN_AUDIT_RETENTION_DAYS", 180
)


def validate_runtime_settings() -> None:
    missing = []
    if API_ID <= 0:
        missing.append("API_ID")
    if not API_HASH or API_HASH == "your_api_hash":
        missing.append("API_HASH")
    if not BOT_TOKEN or BOT_TOKEN == "your_bot_token":
        missing.append("BOT_TOKEN")
    if not MERCHANT_ID or MERCHANT_ID == "your_merchant_id":
        missing.append("MERCHANT_ID")
    if not PAYMENT_TOKEN or PAYMENT_TOKEN == "your_payment_token":
        missing.append("PAYMENT_TOKEN")
    if missing:
        raise RuntimeError(
            "Missing required configuration values: " + ", ".join(missing)
        )
